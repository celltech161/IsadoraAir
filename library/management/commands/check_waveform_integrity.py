"""Read-only check for suspect waveform JSON files.

Flags files that cannot be a genuine analysis of the Track they are named for:
unreadable or non-finite data, a mismatched track id, left/right length
mismatch, an analysed span that disagrees with the Track's known duration, and
the flat, far-too-short envelope that mocked test audio produced in the
2026-10-29 incident (see docs/TEST_ISOLATION.md).

It is a diagnostic only. It never calls ``analyze_one_track``, never repicks
cue points, never writes, renames or deletes a file, and never writes to the
database (files are opened ``rb``; the only query is one ``SELECT`` of
``id, duration_seconds``). A short or quiet clip is NOT accused: a flat
envelope is only a *suspect* when the analysed span also contradicts the
Track's known duration. Files without a Track row are reported as notes, never
as corruption.

Because it reads station storage, scanning a protected station directory
requires the explicit ``--allow-production-read`` flag.

    manage.py check_waveform_integrity [--waveforms-dir DIR] [--ids ID ...]
                                       [--limit N] [--json] [--fail-on-suspect]
                                       [--allow-production-read]
"""
import json
import math
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.models import Track

#: finding code -> severity
SEVERITY = {
    "INVALID_JSON": "suspect",
    "NOT_AN_OBJECT": "suspect",
    "ID_MISMATCH": "suspect",
    "MISSING_FIELDS": "suspect",
    "NON_FINITE": "suspect",
    "LENGTH_MISMATCH": "suspect",
    "DURATION_MISMATCH": "suspect",
    "SYNTHETIC_FLAT_SHORT": "suspect",
    "ORPHAN_FILE": "note",
    "FLAT_BUT_CONSISTENT": "note",
    "LEGACY_NO_ENVELOPE": "note",
}
#: analysed span may differ from the known duration by this much before it is a mismatch
DURATION_TOLERANCE_SECONDS = 3.0
DURATION_TOLERANCE_FRACTION = 0.05
FLAT_DB_SPREAD = 0.01


class _NonFinite(ValueError):
    pass


def _reject_constant(name):
    raise _NonFinite(name)


def _numbers(value):
    return value if isinstance(value, list) else []


def _is_flat(samples, envelope):
    if envelope:
        if not all(isinstance(v, (int, float)) for v in envelope):
            return False
        return max(envelope) - min(envelope) < FLAT_DB_SPREAD and len(set(samples)) <= 1
    return len(samples) > 1 and len(set(samples)) == 1


def inspect_file(path, track_id, known_duration):
    """Return the list of finding codes for one waveform file (read-only)."""
    findings = []
    try:
        with open(path, "rb") as stream:
            payload = json.loads(stream.read().decode("utf-8"), parse_constant=_reject_constant)
    except _NonFinite:
        return ["NON_FINITE"]
    except (OSError, ValueError, UnicodeDecodeError):
        return ["INVALID_JSON"]
    if not isinstance(payload, dict):
        return ["NOT_AN_OBJECT"]

    if payload.get("track_id") != track_id:
        findings.append("ID_MISMATCH")
    left, right = _numbers(payload.get("samples_left")), _numbers(payload.get("samples_right"))
    envelope = _numbers(payload.get("envelope_db"))
    span = payload.get("analysis_duration")
    if not left or not right or not isinstance(span, (int, float)) or isinstance(span, bool):
        findings.append("MISSING_FIELDS")
        return findings
    if len(left) != len(right):
        findings.append("LENGTH_MISMATCH")
    numeric = [v for v in left + right + envelope + [span] if isinstance(v, (int, float))]
    if len(numeric) != len(left) + len(right) + len(envelope) + 1 or not all(math.isfinite(v) for v in numeric):
        findings.append("NON_FINITE")
        return findings
    if "envelope_db" not in payload:
        findings.append("LEGACY_NO_ENVELOPE")

    flat = _is_flat(left, envelope) and _is_flat(right, [])
    mismatch = False
    if known_duration:
        tolerance = max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE_FRACTION * known_duration)
        mismatch = abs(span - known_duration) > tolerance
        if mismatch:
            findings.append("DURATION_MISMATCH")
    if flat:
        if known_duration and span < 0.5 * known_duration:
            findings.append("SYNTHETIC_FLAT_SHORT")
        elif not mismatch:
            findings.append("FLAT_BUT_CONSISTENT")
    return findings


class Command(BaseCommand):
    help = "Read-only: list waveform JSON files that cannot be a genuine analysis of their Track."

    def add_arguments(self, parser):
        parser.add_argument("--waveforms-dir", help="Directory to scan (default: settings.WAVEFORMS_DIR).")
        parser.add_argument("--ids", type=int, nargs="*", help="Only these track ids.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many files (0 = all).")
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")
        parser.add_argument("--fail-on-suspect", action="store_true", help="Exit status 1 if any suspect found.")
        parser.add_argument("--allow-production-read", action="store_true",
                            help="Explicit authority to read a protected station directory.")

    def handle(self, *args, **options):
        directory = os.path.realpath(options["waveforms_dir"] or settings.WAVEFORMS_DIR)
        if not os.path.isdir(directory):
            raise CommandError(f"not a directory: {directory}")
        from isadoraair import test_isolation  # stdlib-only: knows which roots are station storage
        protected = test_isolation.protected_match(directory)
        if protected and not options["allow_production_read"]:
            raise CommandError(
                f"{directory} is inside station storage ({protected}); this read-only check needs "
                "--allow-production-read to scan it")

        wanted = set(options["ids"]) if options["ids"] else None
        entries = []
        with os.scandir(directory) as scan:
            for entry in scan:
                stem, dot, ext = entry.name.rpartition(".")
                if dot and ext == "json" and stem.isdigit() and entry.is_file(follow_symlinks=False):
                    if wanted is None or int(stem) in wanted:
                        entries.append((int(stem), entry.path))
        entries.sort()
        if options["limit"] > 0:
            entries = entries[:options["limit"]]

        durations = dict(Track.objects.filter(id__in=[i for i, _ in entries]).values_list("id", "duration_seconds"))
        report = []
        for track_id, path in entries:
            if track_id not in durations:
                codes = ["ORPHAN_FILE"]
                # An orphan can still be unreadable or non-finite; report that too.
                codes += [c for c in inspect_file(path, track_id, None) if SEVERITY[c] == "suspect" and c != "ID_MISMATCH"]
            else:
                codes = inspect_file(path, track_id, durations[track_id])
            if codes:
                report.append({"track_id": track_id, "file": path, "findings": codes,
                               "severity": "suspect" if any(SEVERITY[c] == "suspect" for c in codes) else "note"})

        suspects = [r for r in report if r["severity"] == "suspect"]
        summary = {"directory": directory, "scanned": len(entries), "suspect": len(suspects),
                   "notes": len(report) - len(suspects)}
        if options["json"]:
            self.stdout.write(json.dumps({"summary": summary, "findings": report}, indent=2, sort_keys=True))
        else:
            for row in report:
                self.stdout.write(f"{row['severity'].upper():7} track {row['track_id']}: {', '.join(row['findings'])}")
            self.stdout.write(f"scanned {summary['scanned']} file(s) in {directory}: "
                              f"{summary['suspect']} suspect, {summary['notes']} note(s). Read-only: nothing was changed.")
        if suspects and options["fail_on_suspect"]:
            raise SystemExit(1)
