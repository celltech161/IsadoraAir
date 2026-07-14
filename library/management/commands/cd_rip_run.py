"""Detached rip runner: spawned by the /api/cd/rip-start/ endpoint,
runs whipper end-to-end, then post-processes each output FLAC (writes
the operator-edited tags via mutagen, moves it to LIBRARY_ROOT/
<category_code>/, creates a Track row). Updates the CDRipJob row so
the frontend can poll progress. All exceptions propagate to a single
top-level catch that marks the job as error state so the UI never
gets stuck showing 'running'."""
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.text import get_valid_filename

from library.models import Album, Artist, CDRipJob, Genre, Track


# Whipper's --track-template. See `whipper cd rip --help` for the full
# variable set -- we deliberately produce a flat filename (no artist/
# album subdirs) inside the staging dir so post-processing can find
# and iterate the output files without walking a tree of one-off dirs.
TRACK_TEMPLATE = "%t. %n"
DISC_TEMPLATE = "__disc"


def _unique_dest(dest_dir, filename):
    """Same pattern as _unique_destination in views.py -- auto-suffix
    on collision rather than clobber. Duplicated here to keep this
    command import-cheap (avoids pulling in views.py)."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _parse_accuraterip_from_log(log_path, n_tracks):
    """Read whipper's per-disc log and extract each track's
    AccurateRip verdict. Returns a list of length n_tracks with
    entries 'match', 'nomatch', 'notfound', or '' for tracks the
    log didn't mention. Best-effort -- log format has shifted
    between whipper versions."""
    verdicts = [""] * n_tracks
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return verdicts
    # Whipper log lines look roughly like:
    #   Track  1:  0.9998 R    [1B7A0F62]
    #     Accurately ripped (confidence 42) [1B7A0F62] [AccurateRip v2]
    #   Track  2:  ...
    #     Track not present in AccurateRip database
    for line in text.splitlines():
        m = re.match(r"\s*Track\s+(\d+)\b", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n_tracks and not verdicts[idx]:
                verdicts[idx] = "pending"
            continue
        low = line.lower()
        # Find the LAST track we saw that's still "pending" a verdict
        # -- the AccurateRip line always follows its Track header.
        try:
            pending_idx = max(i for i, v in enumerate(verdicts) if v == "pending")
        except ValueError:
            continue
        if "accurately ripped" in low or "accuraterip verified" in low:
            verdicts[pending_idx] = "match"
        elif "not present in accuraterip" in low or "no matching accuraterip" in low:
            verdicts[pending_idx] = "notfound"
        elif "no accuraterip match" in low or "did not match" in low:
            verdicts[pending_idx] = "nomatch"
    # Anything still 'pending' means the whipper log had a track header
    # but no verdict line before EOF -- report as blank.
    return [v if v != "pending" else "" for v in verdicts]


class Command(BaseCommand):
    help = "Run one CD rip end-to-end. Invoked by the /api/cd/rip-start/ endpoint as a detached subprocess -- NOT meant to be called by hand."

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int)

    def handle(self, *args, **options):
        job_id = options["job_id"]
        try:
            job = CDRipJob.objects.get(pk=job_id)
        except CDRipJob.DoesNotExist:
            raise CommandError(f"No CDRipJob with id={job_id}")

        try:
            self._run(job)
        except Exception as exc:
            job.state = "error"
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.finished_at = timezone.now()
            job.save(update_fields=["state", "error_message", "finished_at"])
            raise

    def _run(self, job):
        job.state = "running"
        job.status_message = "Starting whipper..."
        job.save(update_fields=["state", "status_message"])

        staging = Path(job.staging_dir)
        staging.mkdir(parents=True, exist_ok=True)

        # Build whipper args. -R locks whipper into the MB release the
        # operator picked in detect (if any); -U lets it proceed on
        # unmatched discs. --cdr and --keep-going make it more tolerant
        # of stubborn discs.
        args = [
            "whipper", "cd", "rip",
            "-d", job.device,
            "-O", str(staging),
            "--track-template", TRACK_TEMPLATE,
            "--disc-template", DISC_TEMPLATE,
            "-U", "--cdr", "--keep-going",
        ]
        if job.mb_release_id:
            args.extend(["-R", job.mb_release_id])

        proc = subprocess.Popen(
            args, cwd=str(staging),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        job.whipper_pid = proc.pid
        job.save(update_fields=["whipper_pid"])

        # Capture whipper's stdout (mostly progress noise) into a log
        # file for post-mortem. We don't try to parse individual track
        # progress from it -- too fragile; the poll endpoint reports
        # progress based on how many FLACs have landed in staging.
        log_path = staging / "whipper.stdout.log"
        with open(log_path, "w") as logf:
            for line in proc.stdout:
                logf.write(line)
                # Cheap heuristic status update: whipper mentions
                # 'Track N of M' lines during the rip.
                m = re.search(r"[Tt]rack\s+(\d+)\s+of\s+(\d+)", line)
                if m:
                    cur, tot = int(m.group(1)), int(m.group(2))
                    if (cur != job.progress_current_track
                            or tot != job.progress_total_tracks):
                        job.progress_current_track = cur
                        job.progress_total_tracks = tot
                        job.status_message = f"Ripping track {cur}/{tot}..."
                        job.save(update_fields=[
                            "progress_current_track",
                            "progress_total_tracks",
                            "status_message",
                        ])
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"whipper exited {proc.returncode}. Last log lines: "
                + "\n".join(log_path.read_text(errors='replace').splitlines()[-10:])
            )

        # Discover output. Whipper writes to <staging>/<AlbumArtist>/
        # <Album>/<TrackTemplate>.flac usually -- but with `-U` and
        # unmatched discs it may write directly under staging. Recurse
        # to find every FLAC.
        flacs = sorted(staging.rglob("*.flac"),
                       key=lambda p: (p.parent, p.name))
        if not flacs:
            raise RuntimeError(f"whipper finished but no FLAC files landed in {staging}")

        # AccurateRip verdicts from whipper's log.
        rip_log = next(staging.rglob("*.log"), None)
        verdicts = _parse_accuraterip_from_log(rip_log, len(flacs)) if rip_log else [""] * len(flacs)

        # Post-process: apply operator-edited tags, move into library,
        # create Track rows.
        library_root = Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))
        dest_dir = library_root / job.category.code
        dest_dir.mkdir(parents=True, exist_ok=True)

        op_tracks = {int(t.get("position", 0)): t for t in job.album_meta.get("tracks", [])}
        album_meta = job.album_meta
        album_title = album_meta.get("album_title", "")
        album_artist = album_meta.get("album_artist", "") or "Unknown Artist"
        album_year = album_meta.get("year")
        album_genre = album_meta.get("genre", "")

        album_obj = None
        if album_title:
            album_obj, _ = Album.objects.get_or_create(
                title=album_title, album_artist=album_artist,
                defaults={"year": album_year},
            )
        genre_obj = None
        if album_genre:
            genre_obj, _ = Genre.objects.get_or_create(name=album_genre)

        job.progress_total_tracks = len(flacs)
        job.save(update_fields=["progress_total_tracks"])

        for i, src in enumerate(flacs):
            # Recover the track position from the filename (we set the
            # template to '%t. %n' so files start "01. ", "02. ", etc.).
            m = re.match(r"^(\d+)\.", src.name)
            position = int(m.group(1)) if m else (i + 1)
            op = op_tracks.get(position, {})
            title = op.get("title") or src.stem.split(". ", 1)[-1]
            artist_name = op.get("artist") or album_artist
            artist_obj, _ = Artist.objects.get_or_create(name=artist_name)

            # Write our tags (may overwrite whipper's own MB-derived
            # tags -- intentional: operator edits are authoritative).
            try:
                from mutagen.flac import FLAC
                audio = FLAC(str(src))
                audio["artist"] = artist_name
                audio["title"] = title
                if album_title:
                    audio["album"] = album_title
                if album_artist:
                    audio["albumartist"] = album_artist
                if album_year:
                    audio["date"] = str(album_year)
                if album_genre:
                    audio["genre"] = album_genre
                audio["tracknumber"] = str(position)
                audio.save()
            except Exception as exc:
                # Non-fatal: we still have the FLAC, DB is authoritative
                # for tags anyway.
                self.stdout.write(f"  [tag] track {position} failed: {exc}")

            safe_name = get_valid_filename(f"{artist_name} - {title}.flac")
            dest_path = _unique_dest(dest_dir, safe_name)
            try:
                shutil.move(str(src), str(dest_path))
            except Exception as exc:
                raise RuntimeError(f"Failed to move {src} -> {dest_path}: {exc}")

            track = Track.objects.create(
                filepath=str(dest_path),
                filename=dest_path.name,
                format="flac",
                title=title,
                artist=artist_obj,
                album=album_obj,
                genre=genre_obj,
                year=album_year,
                track_number=position,
                category=job.category,
                # 'Warn but keep the rip' per operator preference:
                # tracks land ready2air=False by default so a human
                # reviews before rotation. Doubly appropriate when
                # AccurateRip didn't verify -- see the accurate_rip
                # bucket the frontend surfaces.
                ready2air=False,
            )
            job.created_track_ids.append(track.id)
            if i < len(verdicts):
                job.accurate_rip_matches.append(verdicts[i])
            else:
                job.accurate_rip_matches.append("")
            job.progress_current_track = i + 1
            job.status_message = f"Post-processed {i+1}/{len(flacs)}: {title}"
            job.save(update_fields=[
                "created_track_ids", "accurate_rip_matches",
                "progress_current_track", "status_message",
            ])

        job.state = "done"
        job.status_message = f"Ripped {len(flacs)} tracks."
        job.finished_at = timezone.now()
        job.save(update_fields=["state", "status_message", "finished_at"])
