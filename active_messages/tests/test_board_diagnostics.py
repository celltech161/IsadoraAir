"""D3E-D: the Operator Board's read-only diagnostics -- D3E-C's reconciliation
classified for an operator, the managed worker as Monitoring last validated
it, and one airing's publication / retirement / air evidence.

Real publications (d3ec_support.Lifecycle: real render, publish, admission,
deck evidence); no state is written raw except where a test needs a condition
the services never produce, and then it says so."""
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from django.db import DatabaseError, connection, models
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from active_messages import diagnostics, playout, quarantine, reconcile, retention, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastMessageEvent
from library.models import Track
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle
from .support import Domain

BOARD = "/iportal/messages/board/"
DIAG = BOARD + "diagnostics/"
DATA = DIAG + "data/"


def content(response) -> str:
    """The diagnostics page's own content (not the site's navigation)."""
    html = response.content.decode()
    start = html.index('<div class="amb" id="amDiagnostics">')
    end = html.find("</main>", start)
    return html[start:end if end != -1 else len(html)]


def warm(client):
    """The site template creates its UI-theme singleton on the very first page
    any station ever renders (library.UITheme) -- not a diagnostics write."""
    client.get(BOARD + "diagnostics/")


def writes(queries):
    return [q["sql"] for q in queries if q["sql"].lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE")]


class Fixtures:
    def setUp(self):
        super().setUp()
        override = self.settings(SECURE_SSL_REDIRECT=False)
        override.enable()
        self.addCleanup(override.disable)
        from django.contrib.auth.models import Group

        from library.models import GroupAccess
        for group in Group.objects.filter(name__startswith="test-"):          # the station grants the iPortal
            GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/iportal/"})

    def operator(self):
        client = Client()
        client.force_login(self.controller)
        return client

    def inspected(self, client=None):
        response = (client or self.operator()).get(DATA + "?inspect=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.json()["inspection"]

    def codes(self, inspection):
        return {item["code"]: item["class"] for item in inspection["findings"]}

    def interrupted_retirement(self):
        execution, publication, _item = self.completed()
        with mock.patch.object(retention, "_remove_publication_bytes", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.retire(execution)
        return execution, publication

    def pending(self):
        """A retirement whose cleanup died right after moving the verified file aside."""
        execution, publication = self.interrupted_retirement()

        def checkpoint(stage):
            if stage == "after_move":
                raise SystemExit("killed after the move")
        with mock.patch.object(quarantine, "_checkpoint", side_effect=checkpoint), self.assertRaises(SystemExit):
            retention.finish_retirements(apply=True)
        root = self.library_root / "UrgentPA" / quarantine.ROOT
        operation = sorted(os.listdir(root))[-1]
        return execution, publication, root / operation / quarantine.PAYLOAD

    def legacy_track(self, **fields):
        """A pre-existing reference the write guard never saw (Django's base QuerySet)."""
        self._legacy = getattr(self, "_legacy", 0) + 1
        values = dict(title=f"Legacy {self._legacy}", artist=self.song_artist,
                      filepath=str(self.library_root / "Legacy" / f"legacy-{self._legacy}.wav"),
                      filename=f"legacy-{self._legacy}.wav")
        values.update(fields)
        (track,) = models.QuerySet(Track).bulk_create([Track(**values)])
        return track


# -- reconciliation display ----------------------------------------------------------------------------------

class InspectionStateTests(Fixtures, Domain, TestCase):
    def test_no_inspection_has_been_performed_by_a_plain_visit(self):
        with mock.patch.object(reconcile, "reconcile") as ran:
            response = self.operator().get(DIAG)
        ran.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(response, 'data-inspection-state="not_inspected"')
        self.assertContains(response, "No inspection has been performed")
        self.assertNotContains(response, 'data-empty="healthy"')

    def test_a_completed_empty_inspection_says_so(self):
        response = self.operator().get(DIAG + "?inspect=1")
        self.assertContains(response, 'data-inspection-state="complete"')
        self.assertContains(response, 'data-empty="healthy"')
        inspection = self.inspected()
        self.assertEqual((inspection["state"], inspection["summary"]["findings"]), ("complete", 0))
        self.assertTrue(inspection["generated_at"])
        self.assertFalse(inspection["deep"])

    def test_a_database_failure_is_unavailable_never_healthy(self):
        with mock.patch.object(reconcile, "reconcile", side_effect=DatabaseError("server closed the connection")):
            response = self.operator().get(DIAG + "?inspect=1")
            inspection = self.inspected()
        self.assertContains(response, 'data-inspection-state="unavailable"')
        self.assertContains(response, "this is not a healthy result")
        self.assertNotContains(response, "server closed the connection")       # no internal detail
        self.assertEqual((inspection["state"], inspection["reason"]), ("unavailable", "DatabaseError"))
        self.assertNotIn("findings", inspection)

    def test_native_airing_stays_closed_and_is_reported(self):
        self.assertFalse(services.NATIVE_AIRING_AVAILABLE)                    # the source constant
        response = self.operator().get(DATA)
        self.assertEqual(response.json()["native_airing"], "closed")
        self.assertContains(self.operator().get(DIAG), "Native airing is closed")

    def test_the_limit_cannot_be_raised_by_a_request(self):
        with mock.patch.object(reconcile, "reconcile", wraps=reconcile.reconcile) as ran:
            self.operator().get(DATA + "?limit=999999&deep=1&inspect=1")
        self.assertEqual(ran.call_args.kwargs["limit"], diagnostics.DIAGNOSTIC_LIMIT)
        self.assertFalse(ran.call_args.kwargs["deep"])                         # never hashing from the web

    def test_an_escaped_finding_never_injects_markup(self):
        report = reconcile.Report(deep=False, generated_at=timezone.now(), limit=1)
        report.add("namespace_file", "<img src=x>", "namespace_entry_unexpected", "warning", "unknown",
                   "<script>alert(1)</script>", action="<b>do</b>")
        with mock.patch.object(reconcile, "reconcile", return_value=report):
            response = self.operator().get(DIAG + "?inspect=1")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<img src=x>")


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReconciliationDisplayTests(Fixtures, Lifecycle, TestCase):
    """Each D3E-C condition reaches the operator with its severity, class and wording."""

    def assert_finding(self, inspection, code, kind, severity=None):
        found = [item for item in inspection["findings"] if item["code"] == code]
        self.assertTrue(found, f"{code} not reported: {self.codes(inspection)}")
        self.assertEqual(found[0]["class"], kind)
        self.assertEqual(found[0]["class_label"], diagnostics.CLASSES[kind][0])
        self.assertTrue(found[0]["title"] and found[0]["interpretation"])
        if severity:
            self.assertEqual(found[0]["severity"], severity)
        return found[0]

    def test_a_retained_publication_is_a_retirement_candidate_linked_to_its_airing(self):
        execution, publication, _item = self.completed()
        item = self.assert_finding(self.inspected(), "publication_retained", "retirement_candidate", "info")
        self.assertEqual(item["link"]["url"], f"{BOARD}airings/{execution.pk}/")

    def test_expected_pruning(self):
        execution, publication, _item = self.completed()
        self.file(publication).unlink()
        Track._base_manager.filter(pk=publication.track_id_snapshot).delete()      # the library's own deletion
        self.assert_finding(self.inspected(), "publication_pruned", "expected_pruning", "info")

    def test_integrity_damage(self):
        execution, publication, _item = self.completed()
        self.file(publication).unlink()                                    # gone while its Track remains
        inspection = self.inspected()
        self.assert_finding(inspection, "publication_file_missing", "integrity_damage", "warning")
        self.assertEqual(inspection["summary"]["integrity_damage"], 1)
        self.assertGreaterEqual(inspection["summary"]["attention"], 1)
        response = self.operator().get(DIAG + "?inspect=1")
        self.assertContains(response, "Published file is missing")
        self.assertNotContains(response, str(self.library_root))           # no server paths

    def test_in_flight_retirement_cleanup(self):
        self.interrupted_retirement()
        self.assert_finding(self.inspected(), "retired_bytes_remaining", "in_flight", "info")

    def test_a_cleanly_retired_publication_is_healthy_history(self):
        execution, publication, _item = self.completed()
        self.retire(execution)
        inspection = self.inspected()
        self.assertNotIn("retired_bytes_remaining", self.codes(inspection))
        self.assertEqual(inspection["summary"]["healthy"].get("publication_retired"), 1)

    def test_cleanup_candidate(self):
        from production.services import media_io
        execution = self.ready_execution()
        message = execution.composition_snapshot["components"][0]["message"]
        placed = self.library_root / playout.relpath_for(execution.pk)
        placed.parent.mkdir(parents=True, exist_ok=True)
        opened = media_io.open_media(execution.mix_media)
        try:
            placed.write_bytes(opened.file.read())
        finally:
            opened.close()
        services.clear_message(message, actor=self.admin)
        os.utime(placed, (time.time() - 3 * 86400,) * 2)
        inspection = self.inspected()
        found = self.assert_finding(inspection, "namespace_orphan_file", "cleanup_candidate", "warning")
        self.assertGreaterEqual(inspection["summary"]["candidates"], 1)
        self.assertEqual(found["link"]["url"], f"{BOARD}airings/{execution.pk}/")

    def test_quarantine_pending(self):
        self.pending()
        inspection = self.inspected()
        self.assert_finding(inspection, "quarantine_pending", "quarantine_pending", "info")
        self.assertGreater(inspection["summary"]["bytes"]["quarantine_reclaimable"], 0)

    def test_quarantine_identity_mismatch(self):
        execution, publication, payload = self.pending()
        manifest = payload.with_name(quarantine.MANIFEST)
        record = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**record, "ino": record["ino"] + 1}))
        self.assert_finding(self.inspected(), "quarantine_identity_mismatch", "integrity_damage", "warning")

    def test_quarantine_owned_by_a_track_needs_manual_recovery(self):
        execution, publication, payload = self.pending()
        self.legacy_track(waveform_path=str(payload))
        inspection = self.inspected()
        self.assert_finding(inspection, "quarantine_owned_by_track", "manual_recovery", "warning")
        self.assertGreater(inspection["summary"]["bytes"]["quarantine_manual"], 0)

    def test_unverifiable_ownership_makes_the_inspection_incomplete(self):
        self.pending()
        loop = self.library_root.parent / "loop.json"
        loop.symlink_to(loop)
        self.legacy_track(waveform_path=str(loop))
        inspection = self.inspected()
        self.assert_finding(inspection, "ownership_unverifiable", "unknown")
        self.assertEqual(inspection["state"], "incomplete")
        self.assertGreaterEqual(inspection["unverifiable"], 1)

    def test_storage_failure_is_reported_not_hidden(self):
        self.completed()
        with self.settings(LIBRARY_ROOT=str(self.library_root.parent / "absent")):
            inspection = self.inspected()
            response = self.operator().get(DIAG + "?inspect=1")
        self.assertEqual(inspection["state"], "incomplete")
        self.assert_finding(inspection, "library_unavailable", "unknown")
        self.assertContains(response, "data-incomplete")
        self.assertNotContains(response, str(self.library_root))

    def test_a_bounded_inspection_says_it_is_partial(self):
        self.completed()
        self.completed()
        with mock.patch.object(diagnostics, "DIAGNOSTIC_LIMIT", 1):
            inspection = self.inspected()
            response = self.operator().get(DIAG + "?inspect=1")
        self.assertEqual(inspection["state"], "incomplete")
        self.assertIn("publications", inspection["truncated"])
        self.assertContains(response, "limit reached for")

    def test_one_shared_ownership_scan_per_inspection(self):
        for _ in range(3):
            execution, publication, _item = self.completed()
            self.retire(execution)
        with mock.patch.object(retention, "track_references", wraps=retention.track_references) as single, \
                mock.patch.object(retention, "references_by_location",
                                  wraps=retention.references_by_location) as shared:
            self.inspected()
        self.assertEqual(shared.call_count, 1)                          # every publication in one pass
        self.assertEqual(single.call_count, 0)

    def test_the_shared_scan_matches_the_per_location_rules(self):
        execution, publication = self.interrupted_retirement()
        alias = self.library_root.parent / "alias-urgentpa"
        alias.symlink_to(self.library_root / "UrgentPA")
        self.legacy_track(waveform_path=str(alias / self.file(publication).name))
        other = self.file(publication).with_name("am-" + "0" * 32 + ".flac")
        locations = [retention.canonical_destination(publication.library_relpath), other]
        shared = retention.references_by_location(locations)
        for location in locations:
            self.assertEqual(sorted(shared[str(location)]),
                             sorted(retention.track_references(location, retention._identity(location))))
        self.assertTrue(shared[str(locations[0])])


# -- the read-only contract -----------------------------------------------------------------------------------

@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ReadOnlyContractTests(Fixtures, Lifecycle, TestCase):
    def test_diagnostics_write_nothing_and_run_no_maintenance(self):
        from monitoring.models import SystemEvent
        self.pending()
        execution, publication, _item = self.completed()
        self.file(publication).unlink()                                   # integrity damage, too
        client = self.operator()
        warm(client)
        events, system_events = ActiveBroadcastMessageEvent.objects.count(), SystemEvent.objects.count()
        executions = list(ActiveBroadcastExecution.objects.order_by("pk").values())
        before = self.disk()
        quarantine_root = self.library_root / "UrgentPA" / quarantine.ROOT
        held = sorted(os.listdir(quarantine_root))
        forbidden = [mock.patch.object(retention, name, side_effect=AssertionError(name)) for name in
                     ("retire_publication", "finish_retirements", "sweep_publication_namespace",
                      "purge_unbound_aggregates", "recover_quarantine", "_remove_publication_bytes")]
        forbidden += [mock.patch.object(quarantine, name, side_effect=AssertionError(name))
                      for name in ("remove", "recover", "reclaim")]
        forbidden += [mock.patch.object(reconcile, "attention", side_effect=AssertionError("attention")),
                      mock.patch.object(playout, "dispatch", side_effect=AssertionError("dispatch")),
                      mock.patch("django.core.management.call_command", side_effect=AssertionError("command")),
                      mock.patch("subprocess.run", side_effect=AssertionError("subprocess")),
                      mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess"))]
        for patcher in forbidden:
            patcher.start()
            self.addCleanup(patcher.stop)
        with CaptureQueriesContext(connection) as queries:
            for _ in range(3):                                           # repeated requests are harmless
                self.assertEqual(client.get(DIAG + "?inspect=1").status_code, 200)
                self.assertEqual(client.get(DATA).status_code, 200)
                self.assertEqual(client.get(DATA + "?inspect=1").status_code, 200)
                self.assertEqual(client.get(DIAG).status_code, 200)
                self.assertEqual(client.get(BOARD + "status/").status_code, 200)
                self.assertEqual(client.get(f"{BOARD}airings/{execution.pk}/").status_code, 200)
        self.assertEqual(writes(queries), [])
        self.assertEqual(self.disk(), before)
        self.assertEqual(sorted(os.listdir(quarantine_root)), held)
        self.assertEqual((ActiveBroadcastMessageEvent.objects.count(), SystemEvent.objects.count()),
                         (events, system_events))
        self.assertEqual(list(ActiveBroadcastExecution.objects.order_by("pk").values()), executions)

    def test_diagnostics_never_touch_the_policy_singleton_path(self):
        """services.current_policy() would INSERT the singleton when missing:
        the diagnostics read the gate with heartbeat.gate_open_read_only()."""
        client = self.operator()
        warm(client)
        with mock.patch.object(services, "current_policy", side_effect=AssertionError("current_policy")), \
                CaptureQueriesContext(connection) as queries:
            self.assertEqual(client.get(DIAG + "?inspect=1").status_code, 200)
            self.assertEqual(client.get(DATA).status_code, 200)
            self.assertEqual(client.get(DIAG).status_code, 200)
        self.assertEqual(writes(queries), [])

    def test_there_is_no_destructive_endpoint(self):
        client = self.operator()
        for url in (DIAG, DATA, DIAG + "?inspect=1"):
            for method in ("post", "put", "delete"):
                with self.subTest(url=url, method=method):
                    self.assertEqual(getattr(client, method)(url).status_code, 405)
        response = client.get(DIAG + "?inspect=1")
        self.assertNotIn("<form", content(response))                    # nothing to submit at all
        self.assertNotIn("<button", content(response))


# -- authorization ------------------------------------------------------------------------------------------

class AuthorizationTests(Fixtures, Domain, TestCase):
    URLS = (DIAG, DIAG + "?inspect=1", DATA, BOARD + "status/")

    def test_anonymous_producer_and_expired_sessions_get_nothing(self):
        producer = Client()
        producer.force_login(self.producer)
        expired = self.operator()
        expired.logout()
        for label, client in (("anonymous", Client()), ("producer", producer), ("expired", expired)):
            for url in self.URLS:
                with self.subTest(who=label, url=url), \
                        mock.patch.object(reconcile, "reconcile", side_effect=AssertionError("inspected")):
                    response = client.get(url)
                    self.assertIn(response.status_code, (302, 403))
                    self.assertNotIn(b"inspection", response.content)

    def test_control_and_staff_compatibility(self):
        staff = Client()
        staff.force_login(self.admin)
        for client in (self.operator(), staff):
            for url in self.URLS:
                with self.subTest(url=url):
                    self.assertEqual(client.get(url).status_code, 200)

    def test_unexpected_identifiers_and_parameters(self):
        client = self.operator()
        self.assertEqual(client.get(f"{BOARD}airings/{uuid.uuid4()}/").status_code, 404)
        self.assertEqual(client.get(f"{BOARD}airings/not-a-uuid/").status_code, 404)
        for query in ("?inspect=yes", "?inspect=1&inspect=0", "?inspect=%00", "?path=/etc/passwd&inspect=1",
                      "?page=-1&inspect=1", "?limit=abc"):
            with self.subTest(query=query):
                response = client.get(DIAG + query)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "root:")


# -- worker health (Monitoring's validated verdict) ---------------------------------------------------------

class MonitoringStateFixture(Fixtures):
    """A disposable Monitoring state file and the worker's Monitoring check."""

    def setUp(self):
        super().setUp()
        from monitoring.models import MonitorCheck
        holder = tempfile.TemporaryDirectory(prefix="am-monitoring-")
        self.addCleanup(holder.cleanup)
        self.state = Path(holder.name) / "monitoring_state.json"
        self.check = MonitorCheck.objects.filter(kind="systemd", systemd_unit=diagnostics.WORKER_UNIT).first() \
            or MonitorCheck.objects.create(kind="systemd", systemd_unit=diagnostics.WORKER_UNIT,
                                           name="Active Message Worker (test)")
        patcher = mock.patch.object(diagnostics, "MONITORING_STATE_PATH", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self, status="ok", **detail):
        detail = {"active_state": "active", "sub_state": "running", "reason": "idle", "phase": "polling",
                  "outcome": "idle", "heartbeat_age_seconds": 3.1, "native_airing": "closed", "n_restarts": 0,
                  **detail}
        return {"id": self.check.pk, "name": self.check.name, "kind": "systemd", "status": status, "detail": detail}

    def publish(self, status="ok", *, age=2.0, **detail):
        self.state.write_text(json.dumps({"timestamp": time.time() - age, "checks": [self.entry(status, **detail)]}))

    def health(self):
        return diagnostics.worker_health()


class WorkerHealthTests(MonitoringStateFixture, Domain, TestCase):
    def test_ok_with_a_verified_current_heartbeat(self):
        self.publish()
        worker = self.health()
        self.assertEqual((worker["state"], worker["identity"], worker["heartbeat"]), ("ok", "verified", "current"))
        self.assertEqual(self.operator().get(BOARD + "status/").json()["worker"]["state"], "ok")

    def test_unknown_identity_is_never_health(self):
        self.publish("unknown", reason="identity_unavailable")
        worker = self.health()
        self.assertEqual((worker["state"], worker["identity"]), ("unknown", "unknown"))

    def test_a_heartbeat_from_another_process(self):
        self.publish("critical", reason="heartbeat_not_current")
        worker = self.health()
        self.assertEqual((worker["state"], worker["identity"], worker["heartbeat"]),
                         ("critical", "mismatch", "not_current"))

    def test_missing_heartbeat_and_crash_history(self):
        self.publish("critical", reason="no_heartbeat")
        self.assertEqual(self.health()["heartbeat"], "missing")
        self.publish("critical", reason="crash_looping")
        self.assertTrue(self.health()["recovery"])
        self.publish("warning", reason="restarted", n_restarts=3)
        worker = self.health()
        self.assertEqual((worker["state"], worker["recovery"], worker["detail"]["n_restarts"]), ("warning", True, 3))

    def test_stale_monitoring_is_only_the_last_known_verdict(self):
        self.publish(age=120)
        worker = self.health()
        self.assertEqual((worker["state"], worker["status"]), ("stale", "ok"))
        self.assertIn("out of date", worker["label"])

    def test_unavailable_unconfigured_disabled_and_unobserved(self):
        self.assertEqual(self.health()["state"], "monitoring_unavailable")          # no file
        self.state.write_text("{not json")
        self.assertEqual(self.health()["state"], "monitoring_unavailable")
        self.state.unlink()
        real = self.state.with_name("real.json")
        real.write_text(json.dumps({"timestamp": time.time(), "checks": []}))
        self.state.symlink_to(real)                                                  # never followed
        self.assertEqual(self.health()["state"], "monitoring_unavailable")
        self.state.unlink()
        self.state.write_text(json.dumps({"timestamp": time.time(), "checks": []}))
        self.assertEqual(self.health()["state"], "not_observed")
        self.check.enabled = False
        self.check.save()
        self.assertEqual(self.health()["state"], "disabled")
        self.check.delete()
        self.assertEqual(self.health()["state"], "not_configured")

    def test_the_board_never_reads_the_heartbeat_or_runs_systemctl(self):
        from active_messages import heartbeat
        self.publish()
        with mock.patch.object(heartbeat, "read_state", side_effect=AssertionError("heartbeat")), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess")):
            client = self.operator()
            for url in (BOARD, BOARD + "status/", DIAG):
                self.assertEqual(client.get(url).status_code, 200)
        response = self.operator().get(DIAG)
        self.assertIn("/monitoring/", content(response))
        self.assertNotIn("Restart</", content(response))                  # no restart control here


# -- historical evidence ---------------------------------------------------------------------------------------

@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class HistoricalEvidenceTests(Fixtures, Lifecycle, TestCase):
    def detail(self, execution):
        if not hasattr(self, "_client"):
            self._client = self.operator()
        response = self._client.get(f"{BOARD}airings/{execution.pk}/")
        self.assertEqual(response.status_code, 200)
        return response

    def test_a_present_publication_with_air_evidence(self):
        execution, publication, _item = self.completed()
        response = self.detail(execution)
        self.assertContains(response, 'data-publication-state="present"')
        self.assertContains(response, "confirmed air evidence")
        self.assertContains(response, publication.sha256)

    def test_an_intentional_retirement_is_not_a_failure_and_keeps_the_history(self):
        execution, publication, item = self.completed()
        before = self.history()
        self.retire(execution)
        response = self.detail(execution)
        self.assertContains(response, 'data-publication-state="retired"')
        self.assertContains(response, "intentional retirement, not a failure")
        self.assertContains(response, "(via maintenance_command)")
        self.assertContains(response, "Publication retired (maintenance)")              # the event, labelled
        self.assertContains(response, "no longer in the library")
        self.assertContains(response, "confirmed air evidence")                         # air evidence kept
        self.assertContains(response, "(Track since removed)")
        self.assertContains(response, f"#{publication.track_id_snapshot} (snapshot)")
        insertion = ActiveBroadcastInsertion.objects.get(execution=execution)
        self.assertContains(response, str(insertion.attempt_token)[:8])
        self.assertContains(response, f"#{insertion.log_item_id_snapshot}")
        self.assertEqual(self.history()["executions"], before["executions"])            # never rewritten

    def test_unexpectedly_missing_is_damage(self):
        execution, publication, _item = self.completed()
        self.file(publication).unlink()
        response = self.detail(execution)
        self.assertContains(response, 'data-publication-state="damaged"')
        self.assertContains(response, "publication_file_missing")

    def test_pruned_through_the_library(self):
        execution, publication, _item = self.completed()
        self.file(publication).unlink()
        Track._base_manager.filter(pk=publication.track_id_snapshot).delete()
        self.assertContains(self.detail(execution), 'data-publication-state="pruned"')

    def test_retirement_cleanup_pending_and_quarantine(self):
        execution, publication = self.interrupted_retirement()
        self.assertContains(self.detail(execution), 'data-publication-state="retired_cleanup_pending"')
        execution, publication, _payload = self.pending()
        self.assertContains(self.detail(execution), 'data-publication-state="retired_cleanup_pending"')

    def test_an_airing_that_never_aired(self):
        execution, publication, _item = self.accepted()
        response = self.detail(execution)
        self.assertContains(response, "this airing never aired")
        self.assertContains(response, "No play evidence.")

    def test_never_published(self):
        execution = self.ready_execution()
        response = self.detail(execution)
        self.assertContains(response, 'data-publication-state="none"')
        self.assertContains(response, "No insertion was recorded.")

    def test_detail_queries_do_not_grow_with_history(self):
        execution, publication, _item = self.completed()
        self.detail(execution)                                            # warm (site theme singleton)
        with CaptureQueriesContext(connection) as first:
            self.detail(execution)
        for _ in range(3):
            self.completed()
        with CaptureQueriesContext(connection) as later:
            self.detail(execution)
        self.assertEqual(len(later), len(first))
        self.assertEqual(writes(first) + writes(later), [])


# -- corrective pass: untrusted Monitoring state and explicit inspection ----------------------------------

HEALTHY = {"state": "ok", "identity": "verified", "heartbeat": "current"}


class MonitoringStateSafetyTests(MonitoringStateFixture, Domain, TestCase):
    """Independent review P1: a future-dated or malformed Monitoring document
    must never yield a current healthy verdict, nor crash a board request."""

    def assert_untrusted(self, worker, problem=None):
        self.assertEqual(worker["state"], "monitoring_unavailable", worker)
        self.assertEqual((worker["identity"], worker["heartbeat"]), ("unknown", "unknown"))
        self.assertIsNone(worker["observed_age_seconds"])
        if problem:
            self.assertEqual(worker["problem"], problem)

    def raw(self, text):
        self.state.write_text(text)

    def document(self, **root):
        self.raw(json.dumps({"timestamp": time.time() - 2, "checks": [self.entry()], **root}))

    def test_a_fresh_and_an_exactly_current_observation_are_healthy(self):
        self.publish(age=2.0)
        worker = self.health()
        self.assertEqual({key: worker[key] for key in HEALTHY}, HEALTHY)
        now = time.time()
        self.raw(json.dumps({"timestamp": now, "checks": [self.entry()]}))
        worker = diagnostics.worker_health(now=now)
        self.assertEqual({key: worker[key] for key in HEALTHY}, HEALTHY)
        self.assertEqual(worker["observed_age_seconds"], 0.0)

    def test_a_future_observation_is_never_current_health(self):
        for ahead in (100, 0.5, 0.001):
            with self.subTest(ahead=ahead):
                now = time.time()
                self.raw(json.dumps({"timestamp": now + ahead, "checks": [self.entry()]}))
                self.assert_untrusted(diagnostics.worker_health(now=now), "timestamp_in_future")

    def test_a_stale_observation_is_only_the_last_known_verdict(self):
        self.publish(age=120)
        worker = self.health()
        self.assertEqual((worker["state"], worker["status"]), ("stale", "ok"))
        self.assertNotEqual(worker["state"], "ok")

    def test_untrusted_timestamps_fail_closed(self):
        cases = {
            "missing": "{\"checks\": []}",
            "null": '{"timestamp": null, "checks": []}',
            "iso string": '{"timestamp": "2026-10-20T03:00:00+00:00", "checks": []}',
            "naive string": '{"timestamp": "2026-10-20 03:00:00", "checks": []}',
            "numeric string": '{"timestamp": "1760000000", "checks": []}',
            "boolean": '{"timestamp": true, "checks": []}',
            "object": '{"timestamp": {"t": 1}, "checks": []}',
            "zero": '{"timestamp": 0, "checks": []}',
            "negative": '{"timestamp": -5, "checks": []}',
            "NaN": '{"timestamp": NaN, "checks": []}',
            "Infinity": '{"timestamp": Infinity, "checks": []}',
            "-Infinity": '{"timestamp": -Infinity, "checks": []}',
            "beyond 2100": '{"timestamp": 4102444801, "checks": []}',
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.raw(text.replace("[]", json.dumps([self.entry()])))
                expected = "timestamp_missing" if label in ("missing", "null") else "timestamp_invalid"
                self.assert_untrusted(self.health(), expected)          # rejected by validation, not by a crash

    def test_malformed_checks_never_raise_or_look_healthy(self):
        problem = {"an unknown status": "entry_invalid", "detail not an object": "entry_invalid",
                   "root is a list": "unreadable", "root is a number": "unreadable",
                   "partially written": "unreadable", "corrupt": "unreadable"}
        cases = {
            "checks: 1": '{"timestamp": TS, "checks": 1}',
            "checks: null": '{"timestamp": TS, "checks": null}',
            "checks: {}": '{"timestamp": TS, "checks": {}}',
            "checks: text": '{"timestamp": TS, "checks": "text"}',
            "missing checks": '{"timestamp": TS}',
            "a number in the list": '{"timestamp": TS, "checks": [1, ENTRY]}',
            "null in the list": '{"timestamp": TS, "checks": [null, ENTRY]}',
            "an unexpected object": '{"timestamp": TS, "checks": [{"unexpected": true}, ENTRY]}',
            "a boolean id": '{"timestamp": TS, "checks": [{"id": true, "status": "ok"}, ENTRY]}',
            "a duplicate entry": '{"timestamp": TS, "checks": [ENTRY, ENTRY]}',
            "an unknown status": '{"timestamp": TS, "checks": [BOGUS]}',
            "detail not an object": '{"timestamp": TS, "checks": [BADDETAIL]}',
            "root is a list": '[ENTRY]',
            "root is a number": '7',
            "partially written": '{"timestamp": TS, "checks": [ENTRY',
            "corrupt": '{not json',
        }
        entry = json.dumps(self.entry())
        bogus = json.dumps({**self.entry(), "status": "excellent"})
        bad_detail = json.dumps({**self.entry(), "detail": ["active"]})
        for label, text in cases.items():
            with self.subTest(label):
                self.raw(text.replace("TS", str(time.time() - 2)).replace("ENTRY", entry)
                         .replace("BOGUS", bogus).replace("BADDETAIL", bad_detail))
                self.assert_untrusted(self.health(), problem.get(label, "checks_invalid"))

    def test_valid_documents_keep_their_specific_states(self):
        self.raw(json.dumps({"timestamp": time.time() - 2, "checks": []}))
        self.assertEqual(self.health()["state"], "not_observed")
        for status, reason, expected, identity in (
                ("warning", "restarted", "warning", "verified"),            # Monitoring bound the heartbeat first
                ("critical", "crash_looping", "critical", "verified"),
                ("critical", "stale", "critical", "verified"),
                ("unknown", "identity_unavailable", "unknown", "unknown"),
                ("critical", "heartbeat_not_current", "critical", "mismatch")):
            with self.subTest(status=status, reason=reason):
                self.publish(status, reason=reason)
                worker = self.health()
                self.assertEqual((worker["state"], worker["identity"]), (expected, identity))
                self.assertNotEqual(worker["state"], "ok")
        self.check.enabled = False
        self.check.save()
        self.assertEqual(self.health()["state"], "disabled")
        self.check.delete()
        self.assertEqual(self.health()["state"], "not_configured")

    def test_every_board_surface_fails_closed_without_an_exception(self):
        client = self.operator()
        warm(client)
        future = json.dumps({"timestamp": time.time() + 100, "checks": [self.entry()]})
        malformed = {"future": future, "checks: 1": json.dumps({"timestamp": time.time() - 2, "checks": 1}),
                     "root list": "[1]", "partial": '{"timestamp": 1, "checks": ['}
        for label, text in malformed.items():
            with self.subTest(label):
                self.publish()                                             # a healthy verdict first ...
                self.assertEqual(client.get(BOARD + "status/").json()["worker"]["state"], "ok")
                self.raw(text)                                             # ... then the untrusted document
                with CaptureQueriesContext(connection) as queries:
                    board = client.get(BOARD)
                    status = client.get(BOARD + "status/")
                    page = client.get(DIAG)
                    data = client.get(DATA)
                for response in (board, status, page, data):
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(b"Traceback", response.content)
                    self.assertNotIn(b"TypeError", response.content)
                self.assertContains(board, 'data-worker-state="monitoring_unavailable"')
                self.assertNotContains(board, ">Worker OK<")
                self.assertEqual(status.json()["worker"]["state"], "monitoring_unavailable")
                self.assertEqual(status.json()["worker"]["class"], "off")
                self.assertEqual(data.json()["worker"]["state"], "monitoring_unavailable")
                self.assertNotEqual(data.json()["worker"]["identity"], "verified")
                self.assertContains(page, 'data-worker-state="monitoring_unavailable"')
                self.assertNotIn("Verified", content(page))
                self.assertEqual(writes(queries), [])


class ReproductionRecordTests(MonitoringStateFixture, Domain, TestCase):
    """The independent review's exact reproductions, kept verbatim."""

    def test_review_case_a_future_by_100_seconds(self):
        now = time.time()
        self.state.write_text(json.dumps({"timestamp": now + 100, "checks": [self.entry()]}))
        worker = diagnostics.worker_health(now=now)
        self.assertNotEqual(worker["state"], "ok", worker)
        self.assertNotEqual(worker["identity"], "verified", worker)
        self.assertNotEqual(worker["heartbeat"], "current", worker)
        self.assertIsNone(worker["observed_age_seconds"], worker)

    def test_review_case_b_checks_is_an_integer(self):
        self.state.write_text(json.dumps({"timestamp": time.time() - 2, "checks": 1}))
        client = self.operator()
        for url in (BOARD, BOARD + "status/", DIAG, DATA):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_review_p2_plain_json_get_does_not_inspect(self):
        with mock.patch.object(diagnostics, "inspect", wraps=diagnostics.inspect) as ran:
            self.operator().get(DATA)
        self.assertEqual(ran.call_count, 0)


class InspectionOptInTests(Fixtures, Domain, TestCase):
    """Independent review P2: neither diagnostics route inspects unless an
    authorized CONTROL user asks with exactly ``inspect=1``."""

    def forbid(self):
        patchers = [mock.patch.object(diagnostics, "inspect", side_effect=AssertionError("inspected")),
                    mock.patch.object(reconcile, "reconcile", side_effect=AssertionError("reconciled")),
                    mock.patch.object(retention, "references_by_location",
                                      side_effect=AssertionError("ownership scan"))]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_plain_json_request_does_not_inspect(self):
        self.forbid()
        response = self.operator().get(DATA)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        body = response.json()
        self.assertIsNone(body["inspection"])
        self.assertEqual(body["status"], "not_inspected")
        self.assertIn("worker", body)

    def test_a_plain_html_request_does_not_inspect(self):
        self.forbid()
        response = self.operator().get(DIAG)
        self.assertContains(response, 'data-inspection-state="not_inspected"')

    def test_only_exactly_inspect_1_inspects(self):
        client = self.operator()
        for query in ("", "?inspect=0", "?inspect=false", "?inspect=true", "?inspect=", "?inspect=yes",
                      "?inspect=1&inspect=0", "?inspect=0&inspect=1", "?inspect=1&inspect=1", "?inspect= 1",
                      "?foo=bar", "?Inspect=1"):
            for url in (DIAG, DATA):
                with self.subTest(url=url + query), \
                        mock.patch.object(diagnostics, "inspect", side_effect=AssertionError("inspected")):
                    self.assertEqual(client.get(url + query).status_code, 200)
        for url in (DIAG + "?inspect=1", DATA + "?inspect=1", DATA + "?inspect=1&foo=bar"):
            with self.subTest(url=url), mock.patch.object(diagnostics, "inspect", wraps=diagnostics.inspect) as ran:
                response = client.get(url)
                self.assertEqual(ran.call_count, 1)
                self.assertEqual(response["Cache-Control"], "no-store")
        body = client.get(DATA + "?inspect=1").json()
        self.assertEqual((body["status"], body["inspection"]["state"]), ("complete", "complete"))

    def test_unauthorized_requests_never_inspect(self):
        producer = Client()
        producer.force_login(self.producer)
        self.forbid()
        for client in (Client(), producer):
            for url in (DIAG + "?inspect=1", DATA + "?inspect=1"):
                with self.subTest(url=url):
                    self.assertIn(client.get(url).status_code, (302, 403))

    def test_status_polling_never_inspects(self):
        self.forbid()
        client = self.operator()
        for _ in range(3):
            self.assertEqual(client.get(BOARD + "status/").status_code, 200)
            self.assertEqual(client.get(BOARD + "status/?inspect=1").status_code, 200)
