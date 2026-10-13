"""Executions: the deterministic composition, due materialization, Replay Now
idempotency, staleness, and the cadence rules applied from air evidence."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from active_messages import composition, services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent

from .support import Domain, media

X = ActiveBroadcastExecution


class CompositionTests(Domain, TestCase):
    def test_urgent_before_public_address_then_oldest_activation_then_uuid(self):
        now = timezone.now()
        pa_old = self.active(self.producer, kind="public_address", now=now)
        urgent_new = self.active(self.producer, kind="urgent", now=now + timedelta(seconds=2))
        urgent_old = self.active(self.producer2, kind="urgent", now=now + timedelta(seconds=1))
        pa_new = self.active(self.producer2, kind="public_address", now=now + timedelta(seconds=3))
        snapshot = services.snapshot_active_composition()["snapshot"]
        self.assertEqual(composition.message_ids(snapshot),
                         [str(urgent_old.pk), str(urgent_new.pk), str(pa_old.pk), str(pa_new.pk)])

    def test_uuid_breaks_activation_ties(self):
        now = timezone.now()
        rows = [self.active(producer, kind="urgent", now=now) for producer in (self.producer, self.producer2)]
        snapshot = services.snapshot_active_composition()["snapshot"]
        self.assertEqual(composition.message_ids(snapshot), sorted(str(row.pk) for row in rows))

    def test_the_snapshot_names_exact_immutable_identities_and_no_path(self):
        message = self.active(kind="urgent")
        snapshot = services.snapshot_active_composition()["snapshot"]
        component = snapshot["components"][0]
        self.assertEqual((component["media"], component["sha256"]), (str(message.message_media_id),
                                                                    message.message_media.sha256))
        text = composition.canonical_json(snapshot)
        for forbidden in ("storage_key", "/srv/", "path", ".flac", ".mp3"):
            self.assertNotIn(forbidden, text)

    def test_the_digest_is_deterministic_and_ignores_generation_and_display_fields(self):
        self.active(self.producer, kind="urgent")
        first = services.snapshot_active_composition()
        services.set_default_replay_interval(900, actor=self.admin)            # no composition change
        self.assertEqual(services.snapshot_active_composition()["digest"], first["digest"])
        changed = dict(first["snapshot"], components=[dict(first["snapshot"]["components"][0],
                                                           producer_username="renamed")])
        self.assertEqual(composition.digest(changed), first["digest"])
        other_media = dict(first["snapshot"], components=[dict(first["snapshot"]["components"][0], sha256="c" * 64)])
        self.assertNotEqual(composition.digest(other_media), first["digest"])

    def test_the_stinger_is_pinned_only_ahead_of_urgent_material(self):
        import io

        from production.models import ProductionMedia
        from production.services import intake, reference_audio
        from spoken.tests import support as audio
        # Real, validated bytes: registration hashes them (the validation service is up under the runner).
        bed = intake.ingest_stream(io.BytesIO(audio.bed(1.0)), kind=ProductionMedia.KIND_BED, validate=True).media
        reference_audio.register("stinger", bed, actor=self.admin)
        services.set_stinger_key("stinger", actor=self.admin)
        self.active(self.producer, kind="public_address")
        self.assertIsNone(services.snapshot_active_composition()["snapshot"]["stinger"])
        self.active(self.producer2, kind="urgent")
        pin = services.snapshot_active_composition()["snapshot"]["stinger"]
        self.assertEqual((pin["key"], pin["media"], pin["sha256"]), ("stinger", str(bed.pk), bed.sha256))

    def test_an_unavailable_stinger_is_recorded_and_attention_raised_but_urgent_material_still_airs(self):
        from monitoring.models import SystemEvent
        services.set_stinger_key("missing-stinger", actor=self.admin)
        self.active(kind="urgent")
        with self.captureOnCommitCallbacks(execute=True):
            execution = services.materialize_due_execution()
        self.assertIsNone(execution.composition_snapshot["stinger"])
        self.assertEqual(execution.composition_snapshot["stinger_unavailable"], "reference_missing")
        self.assertTrue(SystemEvent.objects.filter(category="active_messages", level="warning").exists())


class MaterializationTests(Domain, TestCase):
    def test_nothing_active_or_nothing_due_creates_nothing(self):
        self.assertIsNone(services.materialize_due_execution())
        now = timezone.now()
        message = self.active(now=now)
        execution = services.materialize_due_execution(now=now)
        self.complete(execution, at=now + timedelta(seconds=40))
        self.assertIsNone(services.materialize_due_execution(now=now + timedelta(minutes=5)))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).next_due_at,
                         now + timedelta(seconds=40) + timedelta(seconds=1200))

    def test_a_due_set_makes_exactly_one_execution_and_repeats_coalesce(self):
        now = timezone.now()
        one = self.active(self.producer, now=now)
        two = self.active(self.producer2, kind="urgent", now=now)
        execution = services.materialize_due_execution(now=now)
        self.assertEqual((execution.state, execution.reason, execution.component_count, execution.due_at),
                         ("pending_render", "scheduled", 2, now))
        self.assertEqual(composition.message_ids(execution.composition_snapshot), [str(two.pk), str(one.pk)])
        self.assertEqual(execution.composition_digest, composition.digest(execution.composition_snapshot))
        self.assertEqual(execution.policy_generation, self.policy().generation)
        self.assertIsNone(execution.mix_media_id)
        self.assertEqual(services.materialize_due_execution(now=now).pk, execution.pk)
        self.assertEqual(X.objects.count(), 1)

    def test_a_snapshot_never_changes_after_creation(self):
        now = timezone.now()
        message = self.active(self.producer, now=now)
        execution = services.materialize_due_execution(now=now)
        frozen = execution.composition_snapshot
        services.clear_message(message, actor=self.admin)
        self.active(self.producer2)
        stored = X.objects.get(pk=execution.pk)
        self.assertEqual((stored.composition_snapshot, stored.composition_digest),
                         (frozen, execution.composition_digest))

    def test_clear_beats_a_pending_execution(self):
        now = timezone.now()
        message = self.active(now=now)
        execution = services.materialize_due_execution(now=now)
        services.clear_message(message, actor=self.admin)
        stored = X.objects.get(pk=execution.pk)
        self.assertEqual((stored.state, stored.failure_code, stored.open_slot), ("suppressed", "stale_generation", None))
        self.assertIsNone(services.materialize_due_execution(now=now))
        self.assertIn("execution_suppressed", self.events(execution=execution))

    def test_a_composition_change_rebuilds_the_due_airing_for_the_new_set(self):
        now = timezone.now()
        self.active(self.producer, now=now)
        first = services.materialize_due_execution(now=now)
        second_message = self.active(self.producer2, now=now)
        second = services.materialize_due_execution(now=now)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(X.objects.get(pk=first.pk).state, "suppressed")
        self.assertIn(str(second_message.pk), composition.message_ids(second.composition_snapshot))

    def test_expiry_beats_due_materialization(self):
        now = timezone.now()
        expiring = self.active(self.producer, expires_at=now + timedelta(minutes=1), now=now)
        staying = self.active(self.producer2, now=now)
        execution = services.materialize_due_execution(now=now + timedelta(minutes=2))
        self.assertEqual(composition.message_ids(execution.composition_snapshot), [str(staying.pk)])
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=expiring.pk).state, "expired")

    def test_expiry_of_the_whole_set_suppresses_its_pending_airing(self):
        now = timezone.now()
        self.active(expires_at=now + timedelta(minutes=1), now=now)
        execution = services.materialize_due_execution(now=now)
        services.expire_due_messages(now=now + timedelta(minutes=2))
        self.assertEqual(X.objects.get(pk=execution.pk).state, "suppressed")
        self.assertIsNone(services.materialize_due_execution(now=now + timedelta(minutes=2)))

    def complete(self, execution, *, at):
        started, token = self.put_on_air(execution, started_at=at - timedelta(seconds=30))
        return services.record_air_completed(started, attempt_token=token, ended_at=at,
                                             confirmed_duration_seconds=Decimal("30.000"))


class ManualReplayTests(Domain, TestCase):
    def test_replay_now_is_idempotent_on_its_request_token(self):
        message = self.active()
        services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())   # take the due airing
        token = uuid.uuid4()
        first = services.request_manual_replay(actor=self.admin, request_token=token)
        again = services.request_manual_replay(actor=self.admin, request_token=str(token))
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(X.objects.count(), 1)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(request_token=token).count(), 1)
        self.assertTrue(message)

    def test_a_token_still_resolves_to_its_execution_after_that_airing_finished(self):
        now = timezone.now()
        self.active(now=now)
        token = uuid.uuid4()
        execution = services.request_manual_replay(actor=self.admin, request_token=token, now=now)
        self.assertEqual(execution.reason, "manual")
        started, attempt = self.put_on_air(execution, started_at=now)
        services.record_air_completed(started, attempt_token=attempt, ended_at=now + timedelta(seconds=30),
                                      confirmed_duration_seconds=Decimal("30"))
        replayed = services.request_manual_replay(actor=self.admin, request_token=token, now=now + timedelta(minutes=1))
        self.assertEqual(replayed.pk, execution.pk)                     # a retried POST never airs it twice
        self.assertEqual(X.objects.count(), 1)

    def test_an_unfinished_current_airing_absorbs_replay_now(self):
        self.active()
        scheduled = services.materialize_due_execution()
        token = uuid.uuid4()
        self.assertEqual(services.request_manual_replay(actor=self.admin, request_token=token).pk, scheduled.pk)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.get(request_token=token).event, "replay_coalesced")

    def test_replay_now_needs_an_active_message_a_uuid_token_and_its_own_token(self):
        with self.assertRaises(services.NothingActive):
            services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        with self.assertRaises(services.InvalidRequest):
            services.request_manual_replay(actor=self.admin, request_token="not-a-uuid")
        self.active()
        token = uuid.uuid4()
        services.request_manual_replay(actor=self.admin, request_token=token)
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user("other-admin", is_staff=True)
        with self.assertRaises(services.InvalidRequest) as caught:
            services.request_manual_replay(actor=other, request_token=token)
        self.assertEqual(caught.exception.code, "request_token_conflict")

    def test_replay_does_not_change_the_generation_or_due_times(self):
        message = self.active()
        generation, due = self.policy().generation, message.next_due_at
        services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        self.assertEqual(self.policy().generation, generation)
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=message.pk).next_due_at, due)

    def test_an_engine_held_airing_of_an_older_composition_makes_replay_busy(self):
        self.active(self.producer)
        execution = services.materialize_due_execution()
        self.put_on_air(execution)
        self.active(self.producer2)                                      # new generation while on air
        with self.assertRaises(services.ExecutionBusy):
            services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())


class AirEvidenceCadenceTests(Domain, TestCase):
    def test_completion_sets_completed_air_to_next_due_for_each_still_active_participant(self):
        now = timezone.now()
        stays = self.active(self.producer, now=now)
        services.set_default_replay_interval(600, actor=self.admin)
        cleared = self.active(self.producer2, now=now)
        execution = services.materialize_due_execution(now=now)
        started, token = self.put_on_air(execution, started_at=now + timedelta(minutes=3))
        late = self.active(self.producer, kind="urgent", now=now + timedelta(minutes=3, seconds=10))
        services.clear_message(cleared, actor=self.admin)                # clear while on air: not cut, no future
        ended = now + timedelta(minutes=4)
        done = services.record_air_completed(started, attempt_token=token, ended_at=ended,
                                             confirmed_duration_seconds=Decimal("60.000"))
        self.assertEqual((done.state, done.open_slot, done.ended_at), ("completed", None, ended))
        stays = ActiveBroadcastMessage.objects.get(pk=stays.pk)
        self.assertEqual((stays.last_completed_at, stays.next_due_at), (ended, ended + timedelta(seconds=1200)))
        self.assertEqual(ActiveBroadcastMessage.objects.get(pk=cleared.pk).next_due_at, None)
        late = ActiveBroadcastMessage.objects.get(pk=late.pk)
        self.assertEqual((late.last_completed_at, late.next_due_at), (None, now + timedelta(minutes=3, seconds=10)))
        self.assertIn("aired", self.events(stays))

    def test_completion_evidence_is_idempotent_and_bound_to_the_current_attempt(self):
        self.active()
        execution = services.materialize_due_execution()
        started, token = self.put_on_air(execution)
        ended = started.started_at + timedelta(seconds=30)
        with self.assertRaises(services.EvidenceRefused):
            services.record_air_completed(started, attempt_token=uuid.uuid4(), ended_at=ended,
                                          confirmed_duration_seconds=Decimal("30"))
        with self.assertRaises(services.EvidenceRefused):
            services.record_air_completed(started, attempt_token=token, ended_at=started.started_at - timedelta(1),
                                          confirmed_duration_seconds=Decimal("30"))
        services.record_air_completed(started, attempt_token=token, ended_at=ended, confirmed_duration_seconds=Decimal("30"))
        services.record_air_completed(started, attempt_token=token, ended_at=ended, confirmed_duration_seconds=Decimal("30"))
        self.assertEqual(self.events(execution=execution).count("air_completed"), 1)
        with self.assertRaises(services.InvalidTransition):
            services.record_air_interrupted(started, attempt_token=token, last_evidence_at=ended)

    def test_a_pending_airing_cannot_be_reported_as_aired(self):
        self.active()
        execution = services.materialize_due_execution()
        with self.assertRaises(services.InvalidTransition):
            services.record_air_completed(execution, attempt_token=uuid.uuid4(), ended_at=timezone.now(),
                                          confirmed_duration_seconds=Decimal("1"))

    def test_an_interrupted_airing_is_not_re_aired_at_once(self):
        now = timezone.now()
        message = self.active(now=now)
        execution = services.materialize_due_execution(now=now)
        started, token = self.put_on_air(execution, started_at=now + timedelta(seconds=5))
        last = now + timedelta(seconds=15)
        services.record_air_interrupted(started, attempt_token=token, last_evidence_at=last)
        message = ActiveBroadcastMessage.objects.get(pk=message.pk)
        self.assertEqual((message.last_completed_at, message.next_due_at), (None, last + timedelta(seconds=1200)))
        self.assertIsNone(services.materialize_due_execution(now=last + timedelta(seconds=1)))
        self.assertIsNotNone(services.materialize_due_execution(now=last + timedelta(seconds=1200)))
