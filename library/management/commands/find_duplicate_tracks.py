import hashlib
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import IntegrityError

from library.models import DuplicateCandidate, Track

HASH_CHUNK_SIZE = 1024 * 1024  # 1MB
DURATION_TOLERANCE_SECONDS = 2.0


def _hash_file(filepath):
    digest = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_pair(track_a, track_b):
    """Always store the lower-id track as track_a -- keeps a pair from
    getting recorded twice (A,B) and (B,A) across separate runs."""
    return (track_a, track_b) if track_a.id < track_b.id else (track_b, track_a)


class Command(BaseCommand):
    help = (
        "Find likely-duplicate tracks -- exact file-hash matches first, then "
        "a title/artist/duration fallback for anything not hash-matched. "
        "Only ever creates DuplicateCandidate rows for manual review in admin "
        "-- never deletes a file or a Track. See apply_duplicate_resolutions "
        "for actually acting on a reviewed decision."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Limit how many missing file_hash values to backfill this run (0 = no limit). "
                 "Hashing the whole library reads every file once -- expect this to take a "
                 "real while the first time; safe to run repeatedly to make progress.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        self._backfill_hashes(limit)
        exact_count = self._find_exact_duplicates()
        probable_count = self._find_probable_duplicates()
        self.stdout.write(
            f"\nDone. New exact-match candidates: {exact_count}, "
            f"new probable-match candidates: {probable_count}."
        )

    def _backfill_hashes(self, limit):
        qs = Track.objects.filter(file_hash="").exclude(filepath="").order_by("id")
        if limit:
            qs = qs[:limit]
        tracks = list(qs)

        self.stdout.write(f"Hashing {len(tracks)} track(s) missing file_hash...")
        hashed = 0
        missing_file = 0
        for i, track in enumerate(tracks, start=1):
            fp = Path(track.filepath)
            if not fp.is_file():
                missing_file += 1
                continue
            try:
                track.file_hash = _hash_file(fp)
                track.save(update_fields=["file_hash"])
                hashed += 1
            except OSError as exc:
                self.stderr.write(f"  [WARN] Failed to hash {fp}: {exc}")

            if i % 500 == 0:
                self.stdout.write(f"  ... {i}/{len(tracks)} processed")

        self.stdout.write(f"Hashed {hashed} file(s), {missing_file} had no file on disk.")

    def _find_exact_duplicates(self):
        groups = defaultdict(list)
        for track_id, file_hash in Track.objects.exclude(file_hash="").values_list("id", "file_hash"):
            groups[file_hash].append(track_id)

        created = 0
        for file_hash, track_ids in groups.items():
            if len(track_ids) < 2:
                continue
            tracks = list(Track.objects.filter(id__in=track_ids))
            for i in range(len(tracks)):
                for j in range(i + 1, len(tracks)):
                    a, b = _canonical_pair(tracks[i], tracks[j])
                    try:
                        _, was_created = DuplicateCandidate.objects.get_or_create(
                            track_a=a, track_b=b, defaults={"confidence": "exact"},
                        )
                        created += int(was_created)
                    except IntegrityError:
                        pass
        return created

    def _find_probable_duplicates(self):
        # Only tracks NOT already covered by an exact hash match -- an
        # empty/blank file_hash means it was never hashed OR its hash is
        # unique (no exact-match group), either way metadata is the only
        # signal left for these.
        exact_pair_ids = set()
        for c in DuplicateCandidate.objects.filter(confidence="exact"):
            exact_pair_ids.add((c.track_a_id, c.track_b_id))

        groups = defaultdict(list)
        for track in Track.objects.select_related("artist").filter(ready2air=True):
            if not track.artist_id or not track.title:
                continue
            key = (track.title.strip().lower(), track.artist_id)
            groups[key].append(track)

        created = 0
        for key, tracks in groups.items():
            if len(tracks) < 2:
                continue
            for i in range(len(tracks)):
                for j in range(i + 1, len(tracks)):
                    t1, t2 = tracks[i], tracks[j]
                    d1, d2 = t1.duration_seconds, t2.duration_seconds
                    if d1 is None or d2 is None:
                        continue
                    if abs(d1 - d2) > DURATION_TOLERANCE_SECONDS:
                        continue
                    a, b = _canonical_pair(t1, t2)
                    if (a.id, b.id) in exact_pair_ids:
                        continue  # already recorded as an exact match, don't downgrade it
                    try:
                        _, was_created = DuplicateCandidate.objects.get_or_create(
                            track_a=a, track_b=b, defaults={"confidence": "probable"},
                        )
                        created += int(was_created)
                    except IntegrityError:
                        pass
        return created
