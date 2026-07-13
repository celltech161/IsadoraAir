"""Weekly show-prep for Midnight in the Desert (Saturday 10pm).

Walks /srv/isadoraair/mitd_artbell/ in chronological order, concatenates
the next episode's parts, splits the result into three fixed-name FLACs
(part1/part2/part3) sized to fit the three MITD broadcast hours, and
updates the corresponding Track rows so RBDS/UI show the episode's guest
and topic.

State (which folder was used most recently) lives in a small JSON file
next to the output files, so the walker resumes across restarts and
wraps around when it reaches the end of the archive.
"""
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from library.models import Album, Artist, Category, Track


ARCHIVE_ROOT = Path("/srv/isadoraair/mitd_artbell")
STAGE_ROOT = Path("/srv/isadoraair/music/MITD")
STATE_FILE = STAGE_ROOT / ".mitd_state.json"

# 55 / 55 / 30 min = 2:20 total, leaves ~5 min headroom per hour for
# legal ID / weather / spot drops in the user's rotation clock.
DEFAULT_DURATIONS = (55 * 60, 55 * 60, 30 * 60)

# Normalized target format for the concatenated intermediate. Matches the
# rest of the library's talk content and lets us `-c copy` slice the
# resulting FLAC without re-encoding a second time.
TARGET_SR = 44100
TARGET_CH = 2

# Folder name shape: "YYYY-MM-DD MITD [#] (Topic)".
# One folder in the archive has "MITD#" (a typo, kept as-is on disk), so
# the `#` after MITD is optional.
FOLDER_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) MITD#? \((?P<topic>.+)\)$")

# "Part 1", "Part1", "(Part 1)", "Part1.flac" — case-insensitive, optional
# space/paren before the number. Groups(1) is the integer.
PART_RE = re.compile(r"[Pp]art\s*\(?(\d+)\)?")


class Command(BaseCommand):
    help = (
        "Refresh /srv/isadoraair/music/MITD/part{1,2,3}.flac from the next "
        "archived Art Bell MITD episode, updating DB metadata to match."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would run without changing files or the DB.",
        )
        parser.add_argument(
            "--force-folder", default=None,
            help=(
                "Skip the state-file walker and process this specific "
                "folder name under the archive root instead."
            ),
        )
        parser.add_argument(
            "--durations", default=None,
            help=(
                "Comma-separated seconds for part1,part2,part3. Default: "
                f"{','.join(str(d) for d in DEFAULT_DURATIONS)} (55/55/30 min)."
            ),
        )

    def handle(self, *args, **opts):
        durations = self._parse_durations(opts["durations"])
        dry_run = opts["dry_run"]

        if not ARCHIVE_ROOT.is_dir():
            raise CommandError(f"Archive root missing: {ARCHIVE_ROOT}")
        STAGE_ROOT.mkdir(parents=True, exist_ok=True)

        folder = self._pick_folder(opts["force_folder"])
        parts_in = self._collect_parts(folder)
        if not parts_in:
            self.stdout.write(self.style.WARNING(
                f"No audio files in {folder.name} — skipping, state unchanged."
            ))
            return

        meta = self._parse_folder(folder.name)
        self.stdout.write(f"Episode: {folder.name}")
        self.stdout.write(f"  date={meta['date']} topic={meta['topic']!r}")
        self.stdout.write(f"  inputs: {len(parts_in)} file(s)")
        for p in parts_in:
            self.stdout.write(f"    {p.name}")
        self.stdout.write(
            f"  targets: part1={durations[0]}s part2={durations[1]}s "
            f"part3={durations[2]}s"
        )

        if dry_run:
            self.stdout.write(self.style.NOTICE("--dry-run: stopping before ffmpeg."))
            return

        with tempfile.TemporaryDirectory(prefix="mitd_prep_") as tmp:
            whole = Path(tmp) / "whole.flac"
            self._concat(parts_in, whole)
            whole_dur = self._probe_duration(whole)
            self.stdout.write(f"  concatenated: {whole_dur:.1f}s")

            offset = 0.0
            for i, target in enumerate(durations, start=1):
                dest = STAGE_ROOT / f"part{i}.flac"
                if offset >= whole_dur:
                    self.stdout.write(self.style.WARNING(
                        f"  part{i}: episode ended at {whole_dur:.1f}s "
                        f"before this slice; writing silence would be worse "
                        f"than leaving old file, but users expect a fresh "
                        f"file — writing an empty-but-valid FLAC instead."
                    ))
                    self._write_silence(dest, seconds=1)
                else:
                    take = min(target, whole_dur - offset)
                    self._slice(whole, offset, take, dest)
                    self.stdout.write(
                        f"  part{i}: {take:.1f}s -> {dest.name}"
                    )
                offset += target

        self._update_db(meta)
        self._write_state(folder, meta)
        self.stdout.write(self.style.SUCCESS(
            f"Prepped MITD {meta['date']} — {meta['topic']}"
        ))

    # ----- Folder selection ------------------------------------------------

    def _pick_folder(self, forced_name):
        if forced_name:
            folder = ARCHIVE_ROOT / forced_name
            if not folder.is_dir():
                raise CommandError(f"--force-folder not found: {folder}")
            return folder

        candidates = sorted(
            (p for p in ARCHIVE_ROOT.iterdir()
             if p.is_dir() and FOLDER_RE.match(p.name)),
            key=lambda p: p.name,
        )
        if not candidates:
            raise CommandError(
                f"No episode folders matching 'YYYY-MM-DD MITD (...)' in {ARCHIVE_ROOT}"
            )

        state = self._read_state()
        last = state.get("last_folder") if state else None

        # Walk forward from `last`, wrapping once at end-of-list, skipping
        # empty folders (like the "(no show)" placeholders) so a missing
        # week doesn't stick the cursor forever. Give up after one full
        # loop so an archive with zero non-empty folders raises instead
        # of infinite-looping.
        start_idx = 0
        if last is not None:
            for i, c in enumerate(candidates):
                if c.name > last:
                    start_idx = i
                    break
            else:
                self.stdout.write(self.style.NOTICE(
                    f"Wrapped end of archive — restarting from earliest "
                    f"({candidates[0].name})."
                ))
                start_idx = 0

        for offset in range(len(candidates)):
            c = candidates[(start_idx + offset) % len(candidates)]
            if self._collect_parts(c):
                if offset > 0:
                    self.stdout.write(self.style.NOTICE(
                        f"Skipped {offset} empty folder(s) to reach {c.name}."
                    ))
                return c
        raise CommandError("No non-empty episode folders in archive.")

    def _collect_parts(self, folder):
        flacs = [p for p in folder.iterdir()
                 if p.is_file() and p.suffix.lower() == ".flac"
                 and not p.name.endswith(".sfk")]

        def sort_key(p):
            m = PART_RE.search(p.stem)
            # Files with no "Part N" fall to the end sorted by name — the
            # single-file episodes ("Art's Final Show" etc.) all use "Part
            # N" too, so this is really only for future weirdness.
            return (0, int(m.group(1))) if m else (1, p.name)

        return sorted(flacs, key=sort_key)

    def _parse_folder(self, name):
        m = FOLDER_RE.match(name)
        if not m:
            raise CommandError(f"Folder name doesn't match expected shape: {name!r}")
        return {"date": m.group("date"), "topic": m.group("topic").strip()}

    # ----- ffmpeg ----------------------------------------------------------

    def _concat(self, inputs, dest):
        # Build a single filter_complex chain that decodes+concatenates all
        # inputs and resamples to a uniform 44.1kHz stereo s16 stream —
        # the archive is mixed sample rates / channel counts, so we can't
        # rely on the concat demuxer's stream-copy path.
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for p in inputs:
            args += ["-i", str(p)]
        chain = "".join(f"[{i}:a]" for i in range(len(inputs)))
        chain += (
            f"concat=n={len(inputs)}:v=0:a=1,"
            f"aresample={TARGET_SR},"
            f"aformat=sample_fmts=s16:channel_layouts=stereo[out]"
        )
        args += [
            "-filter_complex", chain,
            "-map", "[out]",
            "-c:a", "flac", "-compression_level", "5",
            str(dest),
        ]
        subprocess.run(args, check=True)

    def _slice(self, src, start_s, dur_s, dest):
        # -ss before -i is a fast seek (FLAC is intra-only, so it's
        # sample-accurate too). We re-encode the slice rather than
        # `-c copy` because ffmpeg's FLAC stream-copy doesn't rewrite
        # STREAMINFO.total_samples — leaving the wrong duration in the
        # header, which the library's mutagen-based scanner would then
        # persist as Track.duration_seconds and the log builder would
        # schedule against, blowing past the hour clock. Encode speed
        # is ~10x realtime, so a 55-min slice costs a few seconds.
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start_s:.3f}", "-i", str(src),
                "-t", f"{dur_s:.3f}",
                "-c:a", "flac", "-compression_level", "5",
                str(dest),
            ],
            check=True,
        )

    def _probe_duration(self, path):
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return float(out)

    def _write_silence(self, dest, seconds):
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"anullsrc=r={TARGET_SR}:cl=stereo",
                "-t", str(seconds), "-c:a", "flac",
                str(dest),
            ],
            check=True,
        )

    # ----- DB --------------------------------------------------------------

    def _update_db(self, meta):
        artist, _ = Artist.objects.get_or_create(name="Midnight in the Desert")
        album, _ = Album.objects.get_or_create(
            title=f"MITD {meta['date']}",
            album_artist="Midnight in the Desert",
            defaults={"year": int(meta["date"][:4])},
        )
        # If the album already existed with a stale year (edge case: two
        # different Aug-3 shows in different years), keep the year current.
        year = int(meta["date"][:4])
        if album.year != year:
            album.year = year
            album.save(update_fields=["year"])

        category = Category.objects.filter(code="MITD").first()
        if category is None:
            raise CommandError(
                "Category 'MITD' does not exist — create it in admin first."
            )

        topic = meta["topic"]
        for i in (1, 2, 3):
            path = STAGE_ROOT / f"part{i}.flac"
            duration = self._probe_duration(path)
            defaults = {
                "filename": path.name,
                "format": "flac",
                "title": self._format_title(topic, i),
                "artist": artist,
                "album": album,
                "year": year,
                "track_number": i,
                "duration_seconds": duration,
                "category": category,
                # ready2air is normally a human-review gate, but these
                # files are refreshed weekly by an unattended timer and
                # need to be schedulable the moment they land — otherwise
                # the log builder would skip them and category-fill would
                # pull stale content instead.
                "ready2air": True,
                # Reset analysis marks — the running isadoraair-analyze.timer
                # will pick these up within a minute and recompute cue
                # points and waveform for the new content.
                "next_start_seconds": None,
                "cue_in_seconds": 0,
                "cue_out_seconds": None,
                "intro_until_seconds": None,
                "sweep_start_seconds": None,
                "outro_starts_seconds": None,
                "hook_in_seconds": None,
                "hook_out_seconds": None,
            }
            track, created = Track.objects.update_or_create(
                filepath=str(path), defaults=defaults,
            )
            verb = "created" if created else "updated"
            self.stdout.write(f"  DB {verb}: {track.title}")

    def _format_title(self, topic, part):
        # For "Guest-Topic" folder names, prettify the ASCII hyphen to an
        # em-dash for RBDS. Skip the swap for names that don't fit the
        # pattern ("Open Lines", "no show", "Art's Final Show ...").
        if "-" in topic:
            left, _, right = topic.partition("-")
            if left.strip() and right.strip():
                return f"{left.strip()} — {right.strip()} (Part {part})"
        return f"{topic} (Part {part})"

    # ----- State file ------------------------------------------------------

    def _parse_durations(self, raw):
        if raw is None:
            return DEFAULT_DURATIONS
        try:
            parts = [int(x) for x in raw.split(",")]
        except ValueError as e:
            raise CommandError(f"--durations must be comma-separated ints: {e}")
        if len(parts) != 3 or any(p <= 0 for p in parts):
            raise CommandError("--durations needs exactly 3 positive ints")
        return tuple(parts)

    def _read_state(self):
        if not STATE_FILE.exists():
            return None
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError as e:
            raise CommandError(f"State file {STATE_FILE} is corrupt: {e}")

    def _write_state(self, folder, meta):
        new = {
            "last_folder": folder.name,
            "episode_date": meta["date"],
            "topic": meta["topic"],
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new, indent=2))
        tmp.replace(STATE_FILE)
