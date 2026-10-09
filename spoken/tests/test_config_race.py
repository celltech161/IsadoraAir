"""Content-type configuration versus item creation and render completion,
with two real PostgreSQL sessions.

SpokenContentType.save() locks the persisted row before it checks code
permanence and validates, and holds the lock until commit; create_item and
complete_processing lock the same row. Session A is paused at a seam inside
its transaction (configuration.check_recipe for a save, transitions._bounded
for create_item, transitions._verify_render for a completion); whether
session B then waits is read from pg_stat_activity.

The invariant every schedule must keep: no item exists under a type whose
code changed after its first item, and no rendition is bound unless its
provenance matches the configuration that is current when it is bound.
"""
import io
import threading
import unittest
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase

from production.models import ProductionMedia, ReferenceAudio
from production.services import intake, reference_audio, render
from production.tests import sessions
from production.tests.support import IsolatedMediaRootMixin, ffmpeg_available
from spoken import configuration, guard, transitions
from spoken.models import ItemState, SpokenContentType, SpokenItem

from . import support as audio
from .test_domain import make_type, media

User = get_user_model()


class RenameRaceTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.ctype = make_type(code="race")

    def rename_to(self, code, ctype=None):
        ctype = ctype or self.ctype

        def target():
            row = SpokenContentType.objects.get(pk=ctype.pk)
            row.code = code
            row.save()
        return target

    def create_item(self, ctype=None):
        ctype = ctype or self.ctype
        return lambda: transitions.create_item(ctype.pk, actor=self.owner)

    def assert_invariant(self, ctype=None):
        """No item may predate the save that changed its type's code (the
        save stamps updated_at as it writes; an item stamps created_at)."""
        ctype = ctype or self.ctype
        stored = SpokenContentType.objects.get(pk=ctype.pk)
        items = list(SpokenItem.objects.filter(content_type=ctype).values_list("created_at", flat=True))
        renamed = stored.code != ctype.code
        if renamed:
            self.assertFalse([created for created in items if created < stored.updated_at],
                             "an item existed before its type's code changed")
        return renamed, len(items)

    def test_rename_checked_first_makes_item_creation_wait(self):
        seam = sessions.Seam(configuration.check_recipe, "renamer")
        with mock.patch.object(configuration, "check_recipe", seam):
            a, a_box = sessions.run_thread("renamer", self.rename_to("renamed"))
            self.assertTrue(seam.reached.wait(sessions.WAIT))           # A: "unused" checked, lock held
            b, b_box = sessions.run_thread("creator", self.create_item())
            observed = sessions.blocked_or_done(b)
            seam.release.set()
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
        self.assertEqual((a_box["error"], b_box["error"]), (None, None))
        self.assertEqual(self.assert_invariant(), (True, 1))
        self.assertEqual(observed, "blocked")
        self.assertEqual(b_box["result"].content_type.code, "renamed")  # created against the renamed type

    def test_item_created_first_makes_the_rename_wait_and_refuse(self):
        seam = sessions.Seam(transitions._bounded, "creator")
        with mock.patch.object(transitions, "_bounded", seam):
            a, a_box = sessions.run_thread("creator", self.create_item())
            self.assertTrue(seam.reached.wait(sessions.WAIT))           # A: type locked, item inserted
            b, b_box = sessions.run_thread("renamer", self.rename_to("renamed"))
            observed = sessions.blocked_or_done(b)
            seam.release.set()
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
        self.assertIsNone(a_box["error"])
        self.assertIsInstance(b_box["error"], guard.SpokenIntegrityError)
        self.assertEqual(self.assert_invariant(), (False, 1))
        self.assertEqual(observed, "blocked")

    def test_both_started_together_only_safe_outcomes(self):
        outcomes = set()
        for round_number in range(8):
            ctype = make_type(code=f"race-{round_number}")
            barrier = threading.Barrier(2)

            def go(target):
                def run():
                    barrier.wait(sessions.WAIT)
                    return target()
                return run
            a, a_box = sessions.run_thread("renamer", go(self.rename_to(f"renamed-{round_number}", ctype)))
            b, b_box = sessions.run_thread("creator", go(self.create_item(ctype)))
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
            self.assertIsNone(b_box["error"])
            if a_box["error"] is not None:
                self.assertIsInstance(a_box["error"], guard.SpokenIntegrityError)
            renamed, items = self.assert_invariant(ctype)
            self.assertEqual(items, 1)
            self.assertEqual(renamed, a_box["error"] is None)
            outcomes.add("renamed-then-item" if renamed else "item-then-refused")
        self.assertTrue(outcomes <= {"renamed-then-item", "item-then-refused"})

    def test_a_rolled_back_rename_releases_the_lock_and_a_retry_sees_the_item(self):
        seam = sessions.Seam(configuration.check_recipe, "renamer", abort=True)
        with mock.patch.object(configuration, "check_recipe", seam):
            a, a_box = sessions.run_thread("renamer", self.rename_to("renamed"))
            self.assertTrue(seam.reached.wait(sessions.WAIT))
            b, b_box = sessions.run_thread("creator", self.create_item())
            self.assertEqual(sessions.blocked_or_done(b), "blocked")
            seam.release.set()
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
        self.assertIsInstance(a_box["error"], sessions.Abort)
        self.assertIsNone(b_box["error"])
        self.assertEqual(SpokenContentType.objects.get(pk=self.ctype.pk).code, "race")
        with self.assertRaises(guard.SpokenIntegrityError):                # the retry observes the use
            self.rename_to("renamed")()
        self.assertEqual(self.assert_invariant(), (False, 1))

    def test_a_save_failing_in_the_database_rolls_back_and_releases(self):
        make_type(code="taken")
        with self.assertRaises(IntegrityError):
            self.rename_to("taken")()                                      # unique code
        self.assertEqual(SpokenContentType.objects.get(pk=self.ctype.pk).code, "race")
        b, b_box = sessions.run_thread("creator", self.create_item())
        b.join(sessions.WAIT)
        self.assertIsNone(b_box["error"])


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class CompletionConfigurationRaceTests(IsolatedMediaRootMixin, TransactionTestCase):
    """A reconfiguration (or a bed replacement) and a render completion are
    serialized: either the completion commits under the old configuration and
    the change follows, or the change commits first and the old render is
    refused (render_outdated). Never a bind against superseded provenance."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("owner")
        reference_audio.register("bed.3min_ctr", self.bed(1.0))
        self.ctype = make_type(code="ctr", recipe_key="3min_ctr")
        item = transitions.create_item(self.ctype, actor=self.owner)
        item = transitions.record_validation(transitions.submit(item, media(), actor=self.owner))
        self.item, self.token = transitions.start_processing(item, lease_seconds=300)
        with transaction.atomic():
            expected = render.expected("3min_ctr", {})
        self.rendition = media(kind=ProductionMedia.KIND_RENDITION, derived_from=self.item.submission_media,
                               recipe_key="3min_ctr", recipe_version=expected.recipe_version,
                               recipe_params_digest=expected.digest)
        self.evidence = render.RenderEvidence(
            rendition_id=str(self.rendition.pk), source_id=str(self.item.submission_media_id),
            recipe_key="3min_ctr", recipe_version=expected.recipe_version, provenance_digest=expected.digest)

    @staticmethod
    def bed(seconds):
        return intake.ingest_stream(io.BytesIO(audio.bed(seconds)), kind=ProductionMedia.KIND_BED,
                                    validate=True).media

    def complete(self):
        return transitions.complete_processing(self.item, lease_token=self.token, evidence=self.evidence)

    def reconfigure(self):
        row = SpokenContentType.objects.get(pk=self.ctype.pk)
        row.recipe_params = {"bed_gain_db": -20.0}
        row.save()

    def replace_bed(self, media_row):
        return lambda: reference_audio.register("bed.3min_ctr", media_row)

    def change_time(self):
        """When the configuration/reference change was written."""
        newest = ReferenceAudio.objects.filter(key="bed.3min_ctr").order_by("-registered_at").first()
        return max(SpokenContentType.objects.get(pk=self.ctype.pk).updated_at, newest.registered_at)

    def assert_never_bound_against_superseded_provenance(self):
        item = SpokenItem.objects.get(pk=self.item.pk)
        if item.state == ItemState.PROCESSED:
            bound_at = item.events.get(event="processed").created_at
            self.assertLess(bound_at, self.change_time(), "bound after the provenance it matched was superseded")
        else:
            self.assertEqual((item.state, item.rendition_media_id), (ItemState.PROCESSING, None))
        return item.state

    def run_pair(self, first_name, first, second_name, second, seam_owner, seam_name):
        seam = sessions.Seam(getattr(seam_owner, seam_name), first_name)
        with mock.patch.object(seam_owner, seam_name, seam):
            a, a_box = sessions.run_thread(first_name, first)
            self.assertTrue(seam.reached.wait(sessions.WAIT))
            b, b_box = sessions.run_thread(second_name, second)
            observed = sessions.blocked_or_done(b)
            seam.release.set()
            a.join(sessions.WAIT)
            b.join(sessions.WAIT)
        return observed, a_box, b_box

    def test_completion_first_holds_off_the_reconfiguration(self):
        observed, a, b = self.run_pair("completer", self.complete, "configurer", self.reconfigure,
                                       transitions, "_verify_render")
        self.assertEqual((a["error"], b["error"], observed), (None, None, "blocked"))
        self.assertEqual(self.assert_never_bound_against_superseded_provenance(), ItemState.PROCESSED)

    def test_reconfiguration_first_makes_the_completion_wait_and_refuse(self):
        observed, a, b = self.run_pair("configurer", self.reconfigure, "completer", self.complete,
                                       configuration, "check_recipe")
        self.assertEqual((a["error"], observed), (None, "blocked"))
        self.assertIsInstance(b["error"], transitions.EvidenceRefused)
        self.assertEqual(b["error"].code, "render_outdated")
        self.assertEqual(self.assert_never_bound_against_superseded_provenance(), ItemState.PROCESSING)

    def test_completion_first_holds_off_a_bed_replacement(self):
        replacement = self.bed(2.0)
        observed, a, b = self.run_pair("completer", self.complete, "replacer", self.replace_bed(replacement),
                                       transitions, "_verify_render")
        self.assertEqual((a["error"], b["error"], observed), (None, None, "blocked"))
        self.assertEqual(self.assert_never_bound_against_superseded_provenance(), ItemState.PROCESSED)

    def test_bed_replacement_first_makes_the_completion_refuse(self):
        from production.services import retention
        replacement = self.bed(2.0)
        observed, a, b = self.run_pair("replacer", self.replace_bed(replacement), "completer", self.complete,
                                       retention, "lock_for_binding")
        self.assertEqual((a["error"], observed), (None, "blocked"))
        self.assertEqual(getattr(b["error"], "code", None), "render_outdated")
        self.assertEqual(self.assert_never_bound_against_superseded_provenance(), ItemState.PROCESSING)
