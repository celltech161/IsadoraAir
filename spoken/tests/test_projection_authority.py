"""C2B -- projection authority: SpokenAsset / SpokenAssetEdition and the
projection-owned columns of projected Tracks change only through the
projection service's exact grants; a C2 item transition executes only with a
projection witness the guard re-checks; ordinary Tracks are untouched."""
import json
import unittest
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.test import Client

from library.models import Track, TrackWriteRefused
from spoken import delivery, guard, transitions
from spoken.guard import SpokenIntegrityError
from spoken.models import AssetState, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem

from .projection_support import FFMPEG, ProjectionFixtures
from .test_domain import make_category

S = ItemState


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class OrdinaryPathTests(ProjectionFixtures):
    def setUp(self):
        super().setUp()
        self.ctype = self.make_type()
        self.item, self.result = self.delivered(self.ctype)
        self.asset = SpokenAsset.objects.get(pk=self.result.asset_id)
        self.edition = SpokenAssetEdition.objects.get(pk=self.result.edition_id)

    def assert_refused(self, label, attempt):
        with self.subTest(path=label), self.assertRaises(SpokenIntegrityError), transaction.atomic():
            attempt()

    def test_assets_and_editions_never_change_through_the_orm(self):
        asset, edition, ctype = self.asset, self.edition, self.ctype
        fresh = uuid.uuid4()
        attempts = {
            "asset save": lambda: (setattr(asset, "generation", 9), asset.save()),
            "asset create": lambda: SpokenAsset.objects.create(id=fresh, content_type=ctype, mode="current_edition",
                                                               slot_key="x", library_relpath=f"A/spoken-{fresh.hex}.flac"),
            "asset update": lambda: SpokenAsset.objects.filter(pk=asset.pk).update(state=AssetState.RETIRED),
            "asset base manager update": lambda: SpokenAsset._base_manager.filter(pk=asset.pk).update(generation=5),
            "asset unlink a live Track": lambda: SpokenAsset.objects.filter(pk=asset.pk).update(track=None),
            "asset bulk_create": lambda: SpokenAsset.objects.bulk_create([SpokenAsset(content_type=ctype)]),
            "asset bulk_update": lambda: SpokenAsset.objects.bulk_update([asset], ["generation"]),
            "asset delete": lambda: asset.delete(),
            "asset queryset delete": lambda: SpokenAsset.objects.filter(pk=asset.pk).delete(),
            "asset reverse manager": lambda: ctype.assets.create(mode="current_edition", slot_key="y",
                                                                 library_relpath=f"A/spoken-{fresh.hex}.flac"),
            "asset get_or_create": lambda: SpokenAsset.objects.get_or_create(
                content_type=ctype, slot_key="z", defaults={"library_relpath": f"A/spoken-{fresh.hex}.flac"}),
            "asset update_or_create": lambda: SpokenAsset.objects.update_or_create(
                pk=asset.pk, defaults={"generation": 7}),
            "edition save": lambda: (setattr(edition, "state", EditionState.SUPERSEDED), edition.save()),
            "edition create": lambda: SpokenAssetEdition.objects.create(
                asset=asset, item=self.item, rendition=self.item.rendition_media, generation=9),
            "edition update": lambda: SpokenAssetEdition.objects.filter(pk=edition.pk).update(state="aborted"),
            "edition reverse manager": lambda: asset.editions.create(
                item=self.item, rendition=self.item.rendition_media, generation=10),
            "edition bulk_update": lambda: SpokenAssetEdition.objects.bulk_update([edition], ["state"]),
            "edition delete": lambda: edition.delete(),
            "edition queryset delete": lambda: SpokenAssetEdition.objects.all().delete(),
        }
        for label, attempt in attempts.items():
            self.assert_refused(label, attempt)
        asset.refresh_from_db()
        edition.refresh_from_db()
        self.assertEqual((asset.state, asset.generation, edition.state), (AssetState.LIVE, 1, EditionState.ACTIVE))

    def test_projected_track_owned_columns_are_refused_and_library_columns_are_not(self):
        track = Track.objects.get(pk=self.result.track_id)
        title = track.title
        other = Track.objects.create(filepath="/x/ordinary.mp3", filename="ordinary.mp3", title="Song",
                                     artist=track.artist)
        refused = {
            "save a title": lambda: (setattr(track, "title", "hand edit"), track.save()),
            "save readiness": lambda: (setattr(track, "ready2air", False), track.save(update_fields=["ready2air"])),
            "update category": lambda: Track.objects.filter(pk=track.pk).update(category=make_category("Other")),
            "update filepath": lambda: Track.objects.filter(pk=track.pk).update(filepath="/elsewhere.flac"),
            "bulk_update artist": lambda: Track.objects.bulk_update([track], ["artist"]),
            "update_or_create (an import)": lambda: Track.objects.update_or_create(
                filepath=track.filepath, defaults={"title": "from tags"}),
        }
        for label, attempt in refused.items():
            with self.subTest(path=label), self.assertRaises(TrackWriteRefused), transaction.atomic():
                attempt()
            track.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual((track.title, track.ready2air), (title, True))
        track.rotation_weight = 5                                                     # the library's own columns
        track.save()
        Track.objects.filter(pk=track.pk).update(play_count=F("play_count") + 1)
        other.title, other.ready2air = "Renamed", True                                 # ordinary Tracks: untouched
        other.save()
        Track.objects.filter(pk=other.pk).update(category=make_category("Other"))
        track.refresh_from_db()
        self.assertEqual((track.play_count, track.rotation_weight), (1, 5))

    def test_the_library_edit_view_refuses_before_moving_the_file(self):
        staff = get_user_model().objects.create_superuser("libstaff", "x@example.invalid", "pw")
        client = Client()
        client.force_login(staff)
        track = Track.objects.get(pk=self.result.track_id)
        response = client.patch(f"/api/tracks/{track.pk}/", json.dumps({"category_id": make_category("Other").pk}),
                                content_type="application/json", secure=True)
        self.assertIn(response.status_code, (403, 409))
        self.assertTrue(self.library_file(self.asset.library_relpath).exists())
        track.refresh_from_db()
        self.assertEqual(track.category.code, "LocalNews")

    def test_library_import_skips_projected_files(self):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("import_songs", str(self.library), stdout=out, stderr=out)
        self.assertEqual(Track.objects.filter(filepath__contains="spoken-").count(), 1)
        self.assertNotIn("ERROR", out.getvalue())


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class HandBuiltGrantTests(ProjectionFixtures):
    """Grants are private primitives; even a hand-built one cannot describe a
    move the projection does not make, and a C2 item write is executed only
    when the database witnesses it."""

    def item_write(self, row, to_state, **values):
        locked = dict(guard.snapshot(row))
        changes = {"state": to_state, **values}
        return guard.ItemWrite(item_id=row.pk, from_state=row.state, to_state=to_state,
                               values=tuple(sorted(changes.items())),
                               expected=tuple(sorted({**locked, **changes}.items())))

    def test_a_plain_item_write_can_never_deliver(self):
        row = SpokenItem.objects.get(pk=self.processed(self.make_type()).pk)
        with self.assertRaises(SpokenIntegrityError):
            with guard._authorized(self.item_write(row, S.DELIVERED)):
                guard._execute()

    def test_an_unwitnessed_delivery_or_removal_is_refused(self):
        ctype = self.make_type()
        processed = SpokenItem.objects.get(pk=self.processed(ctype).pk)
        live, result = self.delivered(ctype)
        live = SpokenItem.objects.get(pk=live.pk)
        cases = {
            "delivered without an edition": guard.ProjectedItemWrite(
                write=self.item_write(processed, S.DELIVERED), asset_id=None, edition_id=None),
            "delivered by another item's edition": guard.ProjectedItemWrite(
                write=self.item_write(processed, S.DELIVERED), asset_id=uuid.UUID(result.asset_id),
                edition_id=uuid.UUID(result.edition_id)),
            "recalled while still live": guard.ProjectedItemWrite(
                write=self.item_write(live, S.RECALLED), asset_id=None, edition_id=None),
            "superseded while still current": guard.ProjectedItemWrite(
                write=self.item_write(live, S.SUPERSEDED), asset_id=uuid.UUID(result.asset_id),
                edition_id=uuid.UUID(result.edition_id)),
        }
        for label, record in cases.items():
            with self.subTest(case=label), self.assertRaises(SpokenIntegrityError), transaction.atomic():
                with guard._authorized(record):
                    guard._execute()
        self.assertEqual((SpokenItem.objects.get(pk=processed.pk).state, SpokenItem.objects.get(pk=live.pk).state),
                         (S.PROCESSED, S.DELIVERED))

    def test_moves_the_projection_never_makes_are_refused_at_authorization(self):
        ctype = self.make_type()
        _, result = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        locked = {f.attname: guard.freeze(f, asset.__dict__[f.attname]) for f in guard.authoritative_asset_fields()}
        superseded = SpokenItem.objects.get(pk=self.delivered(ctype)[0].pk)
        records = {
            "re-promotion": guard.ProjectedItemWrite(
                write=guard.ItemWrite(item_id=superseded.pk, from_state=S.SUPERSEDED, to_state=S.DELIVERED,
                                      values=(("state", S.DELIVERED),), expected=()), asset_id=None, edition_id=None),
            "an asset created live": guard.AssetCreate(asset_id=uuid.uuid4(), values=tuple(sorted(
                {**{k: v for k, v in locked.items() if k not in guard.ASSET_GENERATED}}.items()))),
            "a detached asset brought back to live": guard.AssetWrite(
                asset_id=asset.pk, match=(("generation", 1), ("state", "detached")), values=(("state", "live"),),
                expected=tuple(sorted({**locked, "state": "live"}.items()))),
            "a generation decrease": guard.AssetWrite(
                asset_id=asset.pk, match=(("generation", 2), ("state", "live")), values=(("generation", 1),),
                expected=tuple(sorted({**locked, "generation": 1}.items()))),
            "an edition reactivated": guard.EditionWrite(edition_id=uuid.uuid4(), from_state="superseded",
                                                         values=(("state", "active"),), expected=()),
            "a Track write of a library column": guard.TrackWrite(track_id=1, values=(("play_count", 9),),
                                                                  expected=()),
        }
        for label, record in records.items():
            with self.subTest(case=label), self.assertRaises(SpokenIntegrityError):
                with guard._authorized(record):
                    pass

    def test_a_grant_is_executed_only_inside_its_transaction_once(self):
        ctype = self.make_type()
        _, result = self.delivered(ctype)
        track = Track.objects.get(pk=result.track_id)
        current = delivery._owned_now(track.pk)
        record = guard.TrackWrite(track_id=track.pk, values=(("title", "x"),),
                                  expected=tuple(sorted({**current, "title": "x"}.items())))
        with transaction.atomic(), guard._authorized(record):
            guard._execute()
            with self.assertRaises(SpokenIntegrityError):
                guard._execute()                                          # single use
        self.assertEqual(Track.objects.get(pk=track.pk).title, "x")
