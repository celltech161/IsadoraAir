"""2.22B -- the shared recorder/editor core, exercised through a scratch,
NON-VoiceTrack consumer: the recorder works for any domain that supplies an
adapter, and nothing in it knows about VoiceTrack (B1, B5, B7, B15, B16, B19).
"""
import io
import json
import os
import re
import time
import uuid
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from production.errors import IntakeError
from production.models import ProductionMedia
from production.policy import MediaPolicy
from production.recorder import registry, views
from production.recorder.contracts import (
    Conflict, CurrentAudio, RecorderError, RecordingAdapter, RecordingContext, SubjectNotFound,
)
from production.services import layout, validation

from .support import IsolatedMediaRootMixin, fixture

User = get_user_model()
URLS = "production.tests.recorder_test_urls"
TAKE = "/api/scratch/take/"


class ScratchPadAdapter(RecordingAdapter):
    """A minimal second consumer: one bound media per named slot, kept in memory."""

    committed: dict = {}

    def __init__(self):
        super().__init__(key="scratch-pad", label="Scratch pad", description="test consumer", entry_url="/scratch/")

    def authorize(self, user, operation):
        return getattr(user, "username", "") != "denied" and not (operation == "export"
                                                                    and user.username == "no-export")

    def resolve(self, params):
        slot = str(params.get("slot", ""))
        if not re.fullmatch(r"[a-z]{1,8}", slot):
            raise SubjectNotFound("bad_subject", "slot required")
        return slot

    def subject_params(self, subject):
        return {"slot": subject}

    def media_policy(self, subject):
        return MediaPolicy(max_bytes=4 * 1024 * 1024, max_duration_seconds=30)

    def _revision(self, slot):
        return str(self.committed.get(slot) or "absent")

    def context(self, request, subject):
        media_id = self.committed.get(subject)
        current = CurrentAudio("production_media", "current", None, str(media_id), "") if media_id else None
        return RecordingContext(adapter=self.key, subject={"slot": subject}, title=f"Slot {subject}",
                                purpose="testing",
                                allowed_operations=("open", "record", "import", "edit", "save", "export"),
                                max_duration_seconds=30, max_bytes=4 * 1024 * 1024,
                                revision=self._revision(subject), current=current)

    def can_access_media(self, user, subject, media):
        return media.owner_id == user.pk or self.committed.get(subject) == media.pk

    def commit(self, user, subject, media, expected_revision):
        if expected_revision != self._revision(subject):
            raise Conflict("stale_revision", "changed", current_revision=self._revision(subject))
        if not media.is_valid:
            raise RecorderError("media_not_valid", "not valid", status=409)
        self.committed[subject] = media.pk


@override_settings(ROOT_URLCONF=URLS, SECURE_SSL_REDIRECT=False)
class RecorderCoreTests(IsolatedMediaRootMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.adapter = registry.register(ScratchPadAdapter())

    @classmethod
    def tearDownClass(cls):
        registry._ADAPTERS.pop("scratch-pad", None)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        ScratchPadAdapter.committed = {}
        self.user = User.objects.create_user("producer", password="x", is_staff=True)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.client.get("/scratch/?slot=a")              # sets the CSRF cookie
        self.csrf = self.client.cookies["csrftoken"].value

    def upload(self, body, *, mode="record", ctype="audio/wav", extra="", client=None, **headers):
        client = client or self.client
        return client.generic("POST", f"{TAKE}?slot=a&mode={mode}{extra}", body, content_type=ctype,
                              HTTP_X_CSRFTOKEN=self.csrf, **headers)

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json", HTTP_X_CSRFTOKEN=self.csrf)

    # -- registry / neutrality ---------------------------------------------------
    def test_registry_accepts_only_explicit_well_formed_adapters(self):
        for key in ("", "Bad", "x", "a/b", "voicetrack.models.VoiceTrack"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.register(type("A", (RecordingAdapter,), {})(key=key))
        with self.assertRaises(TypeError):
            registry.register(object())
        with self.assertRaises(SubjectNotFound):
            registry.get("no-such-tool")

    def test_the_recorder_core_knows_nothing_about_any_consumer(self):
        """No consumer import and no consumer identifier in recorder CODE
        (comments/docstrings may explain the design; code may not depend on it)."""
        import ast
        import tokenize
        root = Path(__file__).resolve().parents[1]
        for path in (root / "recorder").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                    self.assertFalse([n for n in names if n.split(".")[0] in ("library", "authz")], path.name)
            with open(path, "rb") as handle:
                code = " ".join(tok.string for tok in tokenize.tokenize(handle.readline)
                                if tok.type not in (tokenize.COMMENT, tokenize.STRING))
            for word in ("VoiceTrack", "voicetrack", "track_id", "intro", "outro"):
                self.assertNotIn(word, code, f"{path.name} names a consumer concept: {word}")
        for path in list((root / "static" / "production" / "iportal").glob("*.js")) \
                + list((root / "templates" / "production" / "iportal").glob("*.html")):
            text = path.read_text().lower()
            for word in ("voicetrack", "voice track", "track_id", "intro", "outro"):
                self.assertNotIn(word, text, f"{path.name} names a consumer concept: {word}")

    def test_the_workspace_index_lists_only_authorized_tools(self):
        page = self.client.get("/iportal/")
        self.assertContains(page, "Scratch pad")
        denied = Client()
        denied.force_login(User.objects.create_user("denied", password="x", is_staff=True))
        self.assertNotContains(denied.get("/iportal/"), "Scratch pad")

    def test_the_workstation_renders_the_domain_context_and_api(self):
        page = self.client.get("/scratch/?slot=a")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Slot a")
        self.assertContains(page, "/api/scratch/take/")
        self.assertContains(page, "PREVIEW")
        self.assertContains(page, "ON AIR")
        self.assertEqual(self.client.get("/scratch/?slot=BAD!").status_code, 404)

    # -- intake ------------------------------------------------------------------------
    def test_a_recorded_take_streams_validates_and_never_binds(self):
        response = self.upload(fixture("wav16_mono.wav"))
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["media"]
        media = ProductionMedia.objects.get(pk=data["media_id"])
        self.assertEqual((media.kind, media.owner_id, media.validation_state),
                         (ProductionMedia.KIND_RECORDING, self.user.pk, "valid"))
        self.assertEqual(ScratchPadAdapter.committed, {})              # a take is never bound by upload
        self.assertNotIn("storage_key", response.content.decode())   # never exposed

    def test_csrf_and_authentication_are_enforced(self):
        no_token = self.client.generic("POST", f"{TAKE}?slot=a&mode=record", fixture("wav16_mono.wav"),
                                       content_type="audio/wav")
        self.assertEqual(no_token.status_code, 403)
        anonymous = Client()
        self.assertIn(anonymous.post(f"{TAKE}?slot=a&mode=record").status_code, (302, 403))
        denied = Client(enforce_csrf_checks=False)
        denied.force_login(User.objects.create_user("denied", password="x", is_staff=True))
        self.assertEqual(denied.generic("POST", f"{TAKE}?slot=a&mode=record", b"x", content_type="audio/wav")
                         .status_code, 403)
        self.assertFalse(ProductionMedia.objects.exists())

    def test_bounds_are_enforced_before_reading_the_body(self):
        with mock.patch.object(views, "_ExactLengthReader", side_effect=AssertionError("body was read")):
            too_big = self.upload(b"x" * 16, CONTENT_LENGTH=str(5 * 1024 * 1024))
            bad_type = self.upload(b"RIFF", ctype="text/html")
            bad_mode = self.upload(b"RIFF", mode="purge")
        self.assertEqual((too_big.status_code, too_big.json()["error"]), (413, "too_large"))
        self.assertEqual(bad_type.status_code, 415)
        self.assertEqual(bad_mode.status_code, 400)
        self.assertFalse(ProductionMedia.objects.exists())
        self.assertEqual(self.list_files("incoming"), [])

    def test_a_cancelled_or_interrupted_upload_creates_nothing(self):
        body = fixture("wav16_mono.wav")
        response = self.upload(body[: len(body) // 2], CONTENT_LENGTH=str(len(body)))
        self.assertEqual((response.status_code, response.json()["error"]), (400, "source_read_failed"))
        self.assertFalse(ProductionMedia.objects.exists())
        self.assertEqual(self.list_files("incoming"), [])
        self.assertEqual(self.list_files("media"), [])

    def test_invalid_and_malformed_media_are_rejected_without_a_row(self):
        for label, body, ctype in (("garbage", b"\x00" * 4096, "application/octet-stream"),
                                   ("html as wav", b"<html>not audio</html>" * 50, "audio/wav"),
                                   ("truncated wav", fixture("wav16_mono.wav")[:600], "audio/wav")):
            with self.subTest(label):
                response = self.upload(body, mode="import", ctype=ctype)
                self.assertEqual(response.status_code, 422, response.content)
        self.assertFalse(ProductionMedia.objects.exists())

    def test_excessive_duration_is_refused_by_the_domain_policy(self):
        with mock.patch.object(ScratchPadAdapter, "media_policy",
                               return_value=MediaPolicy(max_bytes=4 * 1024 * 1024, max_duration_seconds=1)):
            response = self.upload(fixture("wav16_mono.wav"))
        self.assertEqual((response.status_code, response.json()["error"]), (422, "too_long"))
        self.assertFalse(ProductionMedia.objects.exists())

    def test_a_retryable_capability_failure_is_saved_unvalidated_and_can_be_retried(self):
        blocked = validation.ValidationOutcome(validation.STATUS_INFRASTRUCTURE, "engine_capability_unavailable", {})
        with mock.patch.object(validation, "_analyze_path", return_value=blocked):
            response = self.upload(fixture("wav16_mono.wav"))
        self.assertEqual(response.status_code, 202, response.content)
        take = response.json()["media"]
        self.assertTrue(take["retryable"])
        self.assertEqual(take["validation_state"], "unvalidated")
        refused = self.post_json("/api/scratch/commit/", {"subject": {"slot": "a"}, "media_id": take["media_id"],
                                                          "revision": "absent"})
        self.assertEqual(refused.status_code, 409)                      # not bindable while unvalidated
        retried = self.post_json("/api/scratch/revalidate/", {"subject": {"slot": "a"}, "media_id": take["media_id"]})
        self.assertEqual(retried.json()["media"]["validation_state"], "valid")
        committed = self.post_json("/api/scratch/commit/", {"subject": {"slot": "a"}, "media_id": take["media_id"],
                                                            "revision": "absent"})
        self.assertEqual(committed.status_code, 200, committed.content)

    def test_an_edit_is_an_immutable_derivative_with_provenance(self):
        parent = ProductionMedia.objects.get(pk=self.upload(fixture("wav16_mono.wav")).json()["media"]["media_id"])
        parent_bytes = layout.resolve_storage_path(parent.storage_key).read_bytes()
        ops = json.dumps(["trim-keep", "normalize"])
        response = self.upload(fixture("flac.flac"), mode="edit", ctype="audio/flac",
                               extra=f"&derived_from={parent.pk}&operations={ops}")
        self.assertEqual(response.status_code, 201, response.content)
        child = ProductionMedia.objects.get(pk=response.json()["media"]["media_id"])
        self.assertEqual((child.kind, child.derived_from_id, child.recipe_key), ("edit", parent.pk, "iportal.editor"))
        self.assertTrue(child.recipe_params_digest)
        self.assertEqual(layout.resolve_storage_path(parent.storage_key).read_bytes(), parent_bytes)

    def test_an_edit_cannot_derive_from_media_the_user_may_not_access(self):
        other = User.objects.create_user("other", password="x", is_staff=True)
        stranger = ProductionMedia.objects.create  # noqa: F841 -- never used to create (immutability is Phase-A tested)
        from production.services import intake
        foreign = intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind="recording", owner=other).media
        for bad in (str(foreign.pk), str(uuid.uuid4()), "not-a-uuid"):
            with self.subTest(parent=bad):
                response = self.upload(fixture("wav16_mono.wav"), mode="edit", extra=f"&derived_from={bad}")
                self.assertEqual(response.status_code, 404)
        self.assertEqual(ProductionMedia.objects.count(), 1)

    def test_malformed_edit_provenance_is_refused(self):
        parent = self.upload(fixture("wav16_mono.wav")).json()["media"]["media_id"]
        for ops in ("not json", json.dumps({"a": 1}), json.dumps(["x" * 40]), json.dumps(["op"] * 65)):
            with self.subTest(ops=ops[:20]):
                response = self.upload(fixture("wav16_mono.wav"), mode="edit",
                                       extra=f"&derived_from={parent}&operations={ops}")
                self.assertEqual(response.status_code, 400)

    # -- commit / conflict -----------------------------------------------------------
    def test_commit_uses_the_domain_revision_and_reports_conflicts(self):
        first = self.upload(fixture("wav16_mono.wav")).json()["media"]["media_id"]
        second = self.upload(fixture("flac.flac"), ctype="audio/flac").json()["media"]["media_id"]
        ok = self.post_json("/api/scratch/commit/", {"subject": {"slot": "a"}, "media_id": first, "revision": "absent"})
        self.assertEqual(ok.status_code, 200)
        stale = self.post_json("/api/scratch/commit/", {"subject": {"slot": "a"}, "media_id": second,
                                                        "revision": "absent"})
        self.assertEqual((stale.status_code, stale.json()["error"]), (409, "stale_revision"))
        self.assertEqual(str(ScratchPadAdapter.committed["a"]), first)

    def test_commit_of_someone_elses_unbound_take_is_refused(self):
        other = User.objects.create_user("other", password="x", is_staff=True)
        from production.services import intake
        foreign = intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind="recording", owner=other).media
        response = self.post_json("/api/scratch/commit/", {"subject": {"slot": "a"}, "media_id": str(foreign.pk),
                                                           "revision": "absent"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ScratchPadAdapter.committed, {})

    # -- preview / range / export -----------------------------------------------------
    def test_preview_is_authorized_range_capable_and_never_a_path(self):
        media_id = self.upload(fixture("wav16_mono.wav")).json()["media"]["media_id"]
        full = self.client.get(f"/api/scratch/media/{media_id}/?slot=a")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(b"".join(full.streaming_content)[:4], b"RIFF")
        part = self.client.get(f"/api/scratch/media/{media_id}/?slot=a", HTTP_RANGE="bytes=0-9")
        self.assertEqual((part.status_code, part["Content-Range"].split("/")[0]), (206, "bytes 0-9"))
        self.assertEqual(len(b"".join(part.streaming_content)), 10)
        self.assertEqual(self.client.get(f"/api/scratch/media/{media_id}/?slot=a",
                                         HTTP_RANGE="bytes=999999999-").status_code, 416)
        stranger = Client()
        stranger.force_login(User.objects.create_user("other", password="x", is_staff=True))
        self.assertEqual(stranger.get(f"/api/scratch/media/{media_id}/?slot=a").status_code, 404)

    def test_download_needs_the_export_permission(self):
        media_id = self.upload(fixture("wav16_mono.wav")).json()["media"]["media_id"]
        ok = self.client.get(f"/api/scratch/media/{media_id}/?slot=a&download=1")
        self.assertIn("attachment", ok["Content-Disposition"])
        limited = Client()
        limited_user = User.objects.create_user("no-export", password="x", is_staff=True)
        limited.force_login(limited_user)
        self.assertEqual(limited.get(f"/api/scratch/media/{media_id}/?slot=a&download=1").status_code, 403)

    # -- bounded retention of abandoned server artifacts ------------------------------
    def test_abandoned_partial_uploads_are_swept_with_the_phase_a_grace(self):
        """The sweeper ages files by max(mtime, ctime), which a test cannot
        backdate -- so the sweeper's clock is advanced instead."""
        from datetime import timedelta
        from django.utils import timezone as dj_tz
        layout.ensure_layout()
        stale = layout.incoming_dir() / f"{uuid.uuid4().hex}.part"
        stale.write_bytes(b"abandoned")
        later = dj_tz.now() + timedelta(hours=25)
        views._last_sweep["at"] = 0.0
        with mock.patch("production.services.reconcile.timezone.now", return_value=later):
            self.upload(fixture("wav16_mono.wav"))
        self.assertFalse(stale.exists())                                 # past the 24 h grace: reclaimed
        again = layout.incoming_dir() / f"{uuid.uuid4().hex}.part"
        again.write_bytes(b"abandoned too")
        with mock.patch("production.services.reconcile.timezone.now", return_value=later):
            self.upload(fixture("wav16_mono.wav"))
        self.assertTrue(again.exists())                                   # throttled: at most once per interval
