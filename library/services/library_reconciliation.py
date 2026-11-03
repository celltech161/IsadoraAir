"""Read-only Library reconciliation scan (roadmap 3.11 Phase B; see docs/LIBRARY_RECONCILIATION.md).

``Scanner(...).run()`` walks the managed media tree under ``LIBRARY_ROOT`` (never following a symlink, never
leaving the root), compares it with the committed Track rows, groups possible duplicates and gathers relink
evidence for missing Tracks, and returns a JSON-serializable report.  ``write_report``/``load_report`` keep
reports under ``REPORTS_ROOT/library-reconciliation/``: written atomically, the last complete one named by
``latest.json``; an interrupted or failed scan never replaces it.

Nothing here writes a Track row, a file, a cue marker, readiness, a waveform cache, ``Track.file_hash`` or a
``DuplicateCandidate``.  The only content reads are the bounded byte hashing of equal-size files (duplicate
evidence) and the bounded decodes of relink candidates (``audio_identity``), both done by the operator-run
command, never in a web request.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.utils import timezone

from library.services import audio_identity, waveform_cache

SCHEMA = "isadoraair.library_reconciliation"
SCHEMA_VERSION = 1
REPORT_DIRNAME = "library-reconciliation"

#: The file types the importer registers (library.management.commands.import_songs.SUPPORTED_EXT).
SUPPORTED_EXT = frozenset({".mp3", ".flac", ".wav", ".m4a", ".ogg", ".oga", ".aiff", ".aif", ".mp2", ".alac"})
_SPOKEN_NAME = re.compile(r"spoken-[0-9a-f]{32}\.flac")
#: A file changed this recently may be mid-delivery (track_deletion.SOURCE_FRESHNESS_GRACE_SECONDS).
FRESH_SECONDS = 60
MAX_CANDIDATES = 5
DUPLICATE_GROUP_LIMIT = 25
DURATION_TOLERANCE_SECONDS = 2.0
#: At least this share (and SUSPECT_MIN) of local Tracks missing marks the storage itself suspect.
SUSPECT_FRACTION, SUSPECT_MIN = 0.5, 20
HASH_CHUNK = 1 << 20
TRACK_CHUNK = 2000
DEFAULT_MAX_DECODES = 100
DEFAULT_MAX_HASH_BYTES = 20 * (1 << 30)
LIST_LIMIT = 200                       # samples kept for counted-only categories

TYPES = ("missing", "moved", "unregistered", "duplicate", "category_drift", "shared_source", "protected",
         "in_delivery", "uncertain")


class ScanError(Exception):
    """The scan cannot produce a trustworthy report (e.g. the Library root is unavailable)."""


class ScanBusy(ScanError):
    """Another scan holds the scan lock."""


def report_dir() -> Path:
    return Path(settings.REPORTS_ROOT) / REPORT_DIRNAME


def library_root() -> Path:
    return Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))


# --------------------------------------------------------------------------- report storage

def _write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_report(report: dict) -> Path:
    """Store a finished report; only a complete (or suspect) scan becomes ``latest``."""
    directory = report_dir()
    path = directory / f"scan-{report['scan_id']}.json"
    _write_json_atomic(path, report)
    pointer = {"scan_id": report["scan_id"], "file": path.name, "status": report["status"],
               "finished_at": report.get("finished_at")}
    _write_json_atomic(directory / "last_attempt.json", pointer)
    if report["status"] in ("complete", "suspect"):
        _write_json_atomic(directory / "latest.json", pointer)
    return path


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_REPORT_CACHE: dict = {}


def load_report(scan_id: str | None = None) -> dict | None:
    """The latest complete report (or ``scan_id``'s), parsed once per file version."""
    directory = report_dir()
    if scan_id is None:
        pointer = _read_json(directory / "latest.json")
        if not isinstance(pointer, dict):
            return None
        scan_id = pointer.get("scan_id")
    if not isinstance(scan_id, str) or not re.fullmatch(r"[0-9A-Za-z-]{1,64}", scan_id):
        return None
    path = directory / f"scan-{scan_id}.json"
    try:
        info = path.stat()
    except OSError:
        return None
    key = (str(path), info.st_mtime_ns, info.st_size)
    if _REPORT_CACHE.get("key") != key:
        report = _read_json(path)
        if not isinstance(report, dict) or report.get("schema") != SCHEMA:
            return None
        _REPORT_CACHE.clear()
        _REPORT_CACHE.update(key=key, report=report)
    return _REPORT_CACHE["report"]


def scan_status() -> dict:
    """What the page shows about scanning: the last attempt, the latest complete scan, a running one."""
    directory = report_dir()
    running = _read_json(directory / "running.json")
    if isinstance(running, dict):
        holder = _lock_holder_alive(directory)
        running["alive"] = holder
    return {"latest": _read_json(directory / "latest.json"), "last_attempt": _read_json(directory / "last_attempt.json"),
            "running": running if isinstance(running, dict) else None}


def _lock_holder_alive(directory: Path) -> bool:
    """True while a scan process holds the scan lock (a crashed scan's progress file is then stale)."""
    try:
        fd = os.open(directory / ".scan.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def scan_lock():
    directory = report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".scan.lock", os.O_RDWR | os.O_CREAT, 0o640)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScanBusy("another library reconciliation scan is running") from None
        yield
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- helpers

@dataclass
class FileEntry:
    path: str
    rel: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    registered: bool = False

    @property
    def suffix(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def describe(self) -> dict:
        return {"path": self.path, "rel": self.rel, "size": self.size, "mtime_ns": self.mtime_ns}


def normalized_title(title) -> str:
    text = unicodedata.normalize("NFKC", title or "").casefold()
    return " ".join(text.split())


def track_fingerprint(row: dict) -> dict:
    """The Track fields a relink depends on, as the scan saw them."""
    return {key: row.get(key) for key in ("filepath", "filename", "format", "waveform_path", "cue_marker_revision")}


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, type(exc).__name__)


def _protection_predicates():
    """Callables naming paths and Track ids that belong to another subsystem (absent apps: nothing)."""
    spoken_tracks, spoken_relpaths = set(), set()
    try:
        SpokenAsset = apps.get_model("spoken", "SpokenAsset")
    except LookupError:
        SpokenAsset = None
    if SpokenAsset is not None:
        for track_id, relpath in SpokenAsset._base_manager.values_list("track_id", "library_relpath").iterator():
            if track_id is not None:
                spoken_tracks.add(track_id)
            if relpath:
                spoken_relpaths.add(relpath)
    am_tracks = set()
    try:
        Publication = apps.get_model("active_messages", "ActiveBroadcastPublication")
    except LookupError:
        Publication = None
    if Publication is not None:
        for track_id, snapshot in Publication._base_manager.values_list("track_id", "track_id_snapshot").iterator():
            am_tracks.update(t for t in (track_id, snapshot) if t is not None)
    try:
        from active_messages import playout, track_guard
    except ImportError:              # pragma: no cover - the app is part of the project
        track_guard = None
    if track_guard is not None:
        # Resolved once per scan (the guard's helpers resolve them on every call): the quarantine roots and
        # the publication namespace directory.  The guard's own predicate still decides inside the namespace.
        quarantine_roots = tuple(track_guard._quarantine_roots())
        namespace = playout._library_root() / playout.CATEGORY_CODE
        namespaces = {str(namespace), os.path.realpath(namespace)}

    def path_owner(path: str, rel: str, resolved: str | None = None) -> str | None:
        """``resolved``: the path's realpath (the walk's paths are already canonical)."""
        name = os.path.basename(path)
        if _SPOKEN_NAME.fullmatch(name) or rel in spoken_relpaths:
            return "spoken projection file"
        if track_guard is not None:
            resolved = resolved or os.path.abspath(path)
            try:
                if any(resolved == root or resolved.startswith(root + os.sep) for root in quarantine_roots):
                    return "Active Messages quarantine"
                if os.path.dirname(resolved) in namespaces and track_guard.reserved_destination(path) is not None:
                    return "Active Messages publication namespace"
            except Exception:        # an ownership check that cannot decide is treated as protected
                return "ownership could not be determined"
        return None

    def track_owner(track_id: int) -> str | None:
        if track_id in spoken_tracks:
            return "projected spoken Track"
        if track_id in am_tracks:
            return "Active Messages publication Track"
        return None

    return path_owner, track_owner


def track_audio_evidence(track_id, filepath) -> dict:
    """What a Track's own records say about its last identified audio (read only).

    ``identity`` is the decoded-audio revision its version-2 waveform cache recorded for exactly
    ``filepath``, when nothing contradicts it: no cue-point approval names other audio.  Otherwise
    ``identity`` is None and ``problems`` says why (a relink then stays manual)."""
    from library.models import Track

    evidence = {"identity": None, "source_size": None, "problems": []}
    cache = waveform_cache.canonical_path(Path(settings.WAVEFORMS_DIR), track_id)
    payload, failure = waveform_cache.read(cache)
    if failure is None:
        failure = waveform_cache.check_payload(payload, track_id)
    if failure is not None or payload.get("schema") != waveform_cache.SCHEMA:
        evidence["problems"].append("no version-2 waveform cache records this Track's audio identity"
                                    + (f" ({failure.reason})" if failure is not None else ""))
        return evidence
    source = payload.get("source") or {}
    if isinstance(source.get("size"), int):
        evidence["source_size"] = source["size"]
    if source.get("path") != filepath:
        evidence["problems"].append("the waveform cache was made from a different file path")
    revision = audio_identity.revision(payload.get("audio_identity"))
    if revision is None:
        evidence["problems"].append("the waveform cache has no usable decoded-audio identity")
    state = Track.objects.filter(pk=track_id).values_list("cue_marker_state", flat=True).first() or {}
    others = sorted({entry.get("audio_revision") for entry in state.values()
                     if isinstance(entry, dict) and entry.get("audio_revision")} - {revision})
    if revision is not None and others:
        evidence["problems"].append("cue-point approvals refer to different audio")
    if not evidence["problems"]:
        evidence["identity"] = revision
    return evidence


# --------------------------------------------------------------------------- the scan

class Scanner:
    def __init__(self, *, root=None, max_decodes=DEFAULT_MAX_DECODES, max_hash_bytes=DEFAULT_MAX_HASH_BYTES,
                 progress=None, now_ns=None):
        self.root_setting = Path(root) if root is not None else library_root()
        self.max_decodes = max_decodes
        self.max_hash_bytes = max_hash_bytes
        self.progress = progress or (lambda phase, done: None)
        self.now_ns = now_ns or time.time_ns
        self.findings: list[dict] = []
        self.counts = defaultdict(int)
        self.errors: list[str] = []
        self.files: dict[str, FileEntry] = {}
        self.unsupported = defaultdict(int)
        self.samples = defaultdict(list)
        self.decodes = 0
        self.hashed_bytes = 0

    # -- utilities
    def add(self, kind, reason, **fields):
        finding = {"id": f"F{len(self.findings) + 1:06d}", "type": kind, "reason": reason, **fields}
        self.findings.append(finding)
        self.counts[kind] += 1
        return finding

    def fresh(self, entry: FileEntry) -> bool:
        return self.now_ns() - max(entry.mtime_ns, entry.ctime_ns) < FRESH_SECONDS * 1_000_000_000

    # -- 1. the root
    def check_root(self):
        try:
            resolved = self.root_setting.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ScanError(f"the Library root {self.root_setting} is unavailable ({exc})") from None
        if not resolved.is_dir():
            raise ScanError(f"the Library root {resolved} is not a directory")
        if not os.access(resolved, os.R_OK | os.X_OK):
            raise ScanError(f"the Library root {resolved} cannot be read")
        try:
            with os.scandir(resolved) as it:
                next(it, None)
        except OSError as exc:
            raise ScanError(f"the Library root {resolved} cannot be listed ({_errno_name(exc)})") from None
        self.root = str(resolved)

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root)

    # -- 2. the walk
    def walk(self):
        stack, seen = [self.root], 0
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    entries = list(entries)
            except OSError as exc:
                self.add("uncertain", f"directory cannot be listed ({_errno_name(exc)})",
                         file={"path": directory, "rel": self.rel(directory)})
                continue
            for entry in entries:
                path = entry.path
                try:
                    if entry.name.startswith("."):
                        kind = "directory" if entry.is_dir(follow_symlinks=False) else "file"
                        self.counts["hidden_entries"] += 1
                        if len(self.samples["hidden"]) < LIST_LIMIT:
                            self.samples["hidden"].append({"rel": self.rel(path), "kind": kind})
                        continue
                    if entry.is_symlink():
                        self.add("uncertain", "symbolic link: not followed (it may point outside the Library)",
                                 file={"path": path, "rel": self.rel(path)})
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    self.add("uncertain", f"cannot be inspected ({_errno_name(exc)})",
                             file={"path": path, "rel": self.rel(path)})
                    continue
                if not stat.S_ISREG(info.st_mode):
                    self.add("uncertain", "not a regular file", file={"path": path, "rel": self.rel(path)})
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix not in SUPPORTED_EXT:
                    self.unsupported[suffix or "(none)"] += 1
                    continue
                self.files[path] = FileEntry(path, self.rel(path), info.st_dev, info.st_ino, info.st_size,
                                             info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
                seen += 1
                if seen % 2000 == 0:
                    self.progress("walk", seen)
        self.counts["files_total"] = len(self.files)

    # -- 3. the Tracks
    def tracks(self):
        from library.models import Track

        path_owner, track_owner = _protection_predicates()
        self.path_owner = path_owner
        fields = ("id", "filepath", "filename", "title", "artist__name", "artist_id", "album__title", "category__code",
                  "duration_seconds", "format", "file_hash", "waveform_path", "ready2air", "isrc", "remote_url",
                  "cue_marker_revision")
        by_inode = defaultdict(list)
        self.missing, self.metadata = [], []
        local = done = 0
        for values in Track.objects.order_by("id").values_list(*fields).iterator(chunk_size=TRACK_CHUNK):
            row = dict(zip(fields, values))
            done += 1
            if done % TRACK_CHUNK == 0:
                self.progress("tracks", done)
            summary = self.track_summary(row)
            owner = track_owner(row["id"])
            path = row["filepath"] or ""
            if owner or row["remote_url"]:
                self.add("protected", owner or "remote-hosted Track (remote_url)", track=summary)
                continue
            if not path:
                self.add("uncertain", "the Track has no file path", track=summary)
                continue
            absolute = os.path.abspath(path)
            if not (absolute + os.sep).startswith(self.root + os.sep) or absolute == self.root:
                self.add("protected", "the Track's file is outside the managed Library root", track=summary)
                continue
            try:
                resolved = os.path.realpath(path)
            except (OSError, ValueError):
                resolved = None
            owner = path_owner(path, self.rel(absolute), resolved)
            if owner:
                self.add("protected", owner, track=summary)
                continue
            local += 1
            self.metadata.append((row["id"], row["artist_id"], normalized_title(row["title"]), row["duration_seconds"],
                                  (row["isrc"] or "").strip().upper()))
            alias = resolved != absolute or absolute != path
            entry = self.files.get(path)
            if entry is not None:
                entry.registered = True
                by_inode[(entry.dev, entry.ino)].append(row["id"])
                if alias:
                    self.add("shared_source", "the Track's path is an alias (not its canonical path)", track=summary)
                if entry.size == 0:
                    self.add("uncertain", "the Track's file is empty (0 bytes)", track=summary, file=entry.describe())
                elif not os.access(path, os.R_OK):
                    self.add("uncertain", "the Track's file cannot be read", track=summary, file=entry.describe())
                elif self.fresh(entry):
                    self.add("in_delivery", "the Track's file changed within the last minute (delivery or replacement "
                             "in progress?)", track=summary, file=entry.describe())
                else:
                    self.counts["healthy"] += 1
                self.check_drift(row, summary, entry)
                continue
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                self.missing.append((row, summary))
                continue
            except OSError as exc:
                self.add("uncertain", f"the Track's file cannot be inspected ({_errno_name(exc)})", track=summary)
                continue
            if stat.S_ISLNK(info.st_mode) or alias:
                self.add("shared_source", "the Track's path is reached through a symbolic link", track=summary)
            else:
                self.add("uncertain", "the Track's file exists but was not in the scanned tree (hidden folder?)",
                         track=summary)
        self.counts["tracks_total"] = done
        self.counts["tracks_local"] = local
        for inode, track_ids in by_inode.items():
            if len(track_ids) > 1:
                self.add("shared_source", "several Tracks name the same file (hardlinks)", track_ids=sorted(track_ids))

    def track_summary(self, row) -> dict:
        return {"id": row["id"], "title": row["title"], "artist": row["artist__name"] or "",
                "album": row["album__title"] or "", "category": row["category__code"] or "",
                "duration": row["duration_seconds"], "format": row["format"], "filepath": row["filepath"],
                "ready2air": row["ready2air"], "file_hash": row["file_hash"] or "",
                "fingerprint": track_fingerprint(row)}

    def check_drift(self, row, summary, entry):
        parts = entry.rel.split(os.sep)
        folder = parts[0] if len(parts) > 1 else ""
        if row["category__code"] and folder != row["category__code"]:
            self.add("category_drift", f"the file is in '{folder or '(Library root)'}', the primary category is "
                     f"'{row['category__code']}'", track=summary, file=entry.describe())

    # -- 4. unregistered files and storage sanity
    def unregistered(self):
        registered_inodes = {(e.dev, e.ino) for e in self.files.values() if e.registered}
        self.unregistered_entries = []
        for entry in self.files.values():
            if entry.registered:
                continue
            owner = self.path_owner(entry.path, entry.rel)
            if owner:
                self.add("protected", owner, file=entry.describe())
            elif (entry.dev, entry.ino) in registered_inodes:
                self.add("shared_source", "a hardlink of a registered Track's file", file=entry.describe())
            elif entry.size == 0:
                self.add("uncertain", "empty file (0 bytes)", file=entry.describe())
            elif self.fresh(entry):
                self.add("in_delivery", "changed within the last minute: may still be arriving", file=entry.describe())
            else:
                self.unregistered_entries.append(entry)

    def storage_suspect(self) -> bool:
        local = self.counts["tracks_local"]
        return len(self.missing) >= SUSPECT_MIN and len(self.missing) >= SUSPECT_FRACTION * max(local, 1)

    # -- 5. missing Tracks and relink evidence
    def missing_tracks(self, suspect: bool):
        if suspect:
            for row, summary in self.missing:
                self.add("uncertain", "the Track's file is absent, but so are most others: the Library storage may "
                         "be unavailable", track=summary)
            return
        by_name, by_size = defaultdict(list), defaultdict(list)
        for entry in self.unregistered_entries:
            by_name[entry.name].append(entry)
            by_size[(entry.size, entry.suffix)].append(entry)
        claimed = set()
        for row, summary in self.missing:
            evidence = self.track_evidence(row)
            summary["evidence"] = evidence
            suffix = os.path.splitext(row["filepath"])[1].lower()
            pool = {e.path: e for e in by_name.get(os.path.basename(row["filepath"]), [])}
            if evidence.get("source_size"):
                pool.update({e.path: e for e in by_size.get((evidence["source_size"], suffix), [])})
            candidates = sorted(pool.values(), key=lambda e: e.rel)
            if not candidates:
                self.add("missing", "the Track's file does not exist; no candidate file was found", track=summary)
                continue
            if len(candidates) > MAX_CANDIDATES:
                self.add("moved", f"{len(candidates)} possible files: too many to compare automatically; review "
                         "manually", track=summary, candidates=[self.candidate(c, row, evidence, decode=False)
                                                                 for c in candidates[:MAX_CANDIDATES]],
                         relink=None)
                continue
            described = [self.candidate(c, row, evidence) for c in candidates]
            eligible = [i for i, c in enumerate(described) if c["eligible"]]
            if len(eligible) == 1:
                relink, reason = eligible[0], "a candidate file has the Track's decoded audio: relink can be reviewed"
                claimed.add(described[eligible[0]]["path"])
            elif len(eligible) > 1:
                relink, reason = None, "more than one candidate has the Track's decoded audio: choose manually"
            else:
                relink, reason = None, "candidate files found, but none proves to be this Track's audio: manual review"
            self.add("moved", reason, track=summary, candidates=described, relink=relink)
        for entry in self.unregistered_entries:
            self.add("unregistered", "a supported audio file no Track names", file=entry.describe(),
                     relink_candidate=entry.path in claimed)

    def track_evidence(self, row) -> dict:
        return track_audio_evidence(row["id"], row["filepath"])

    def candidate(self, entry: FileEntry, row, evidence, *, decode=True) -> dict:
        reasons = []
        suffix = os.path.splitext(row["filepath"])[1].lower()
        if entry.suffix != suffix:
            reasons.append(f"different format ({entry.suffix} instead of {suffix})")
        reasons += evidence["problems"]
        described = {"path": entry.path, "rel": entry.rel, "size": entry.size, "name_matches":
                     entry.name == os.path.basename(row["filepath"]), "identity_match": None, "witness": None,
                     "eligible": False}
        if not reasons and decode:
            if self.decodes >= self.max_decodes:
                reasons.append("not compared: this scan's decode budget was used up (rerun with --max-decodes)")
            else:
                self.decodes += 1
                self.progress("decode", self.decodes)
                before = audio_identity.stat_source(entry.path)
                identity, problem = audio_identity.compute_or_none(entry.path)
                after = audio_identity.stat_source(entry.path)
                if before is None or not audio_identity.same_witness(before, after):
                    reasons.append("the file changed while it was being compared")
                elif identity is None:
                    reasons.append(f"its audio could not be identified ({problem})")
                else:
                    described["identity"] = audio_identity.revision(identity)
                    described["identity_match"] = described["identity"] == evidence["identity"]
                    described["witness"] = after
                    if not described["identity_match"]:
                        reasons.append("its decoded audio differs from the Track's recorded audio")
        elif not decode and not reasons:
            reasons.append("not compared")
        described["reasons"] = reasons
        described["eligible"] = not reasons and described["identity_match"] is True
        return described

    # -- 6. duplicates
    def duplicates(self):
        from library.models import DuplicateCandidate, Track

        groups = []
        # Exact bytes: only files whose size collides, hardlinks counted once.
        by_size = defaultdict(dict)
        for entry in self.files.values():
            if entry.size > 0:
                by_size[entry.size].setdefault((entry.dev, entry.ino), entry)
        path_track = {}
        for values in Track.objects.values_list("id", "filepath").iterator(chunk_size=TRACK_CHUNK):
            if values[1] in self.files:
                path_track[values[1]] = values[0]
        unhashed = 0
        for size, inodes in sorted(by_size.items()):
            if len(inodes) < 2:
                continue
            digests = defaultdict(list)
            for entry in inodes.values():
                if self.hashed_bytes + entry.size > self.max_hash_bytes:
                    unhashed += 1
                    continue
                try:
                    digests[self._hash(entry.path)].append(entry)
                except OSError as exc:
                    self.add("uncertain", f"cannot be read for duplicate comparison ({_errno_name(exc)})",
                             file=entry.describe())
            for digest, entries in digests.items():
                if len(entries) > 1:
                    groups.append({"confidence": "exact", "reason": "identical file bytes (hashed in this scan)",
                                   "files": [dict(e.describe(), track_id=path_track.get(e.path), sha256=digest)
                                             for e in sorted(entries, key=lambda e: e.rel)]})
        if unhashed:
            self.errors.append(f"{unhashed} equal-size file(s) were not hashed: byte budget reached "
                               "(rerun with a larger --max-hash-gb)")
        # Metadata: same artist and normalized title, durations within the tolerance; and shared ISRCs.
        by_key, by_isrc = defaultdict(list), defaultdict(list)
        for track_id, artist_id, title, duration, isrc in self.metadata:
            if artist_id and title:
                by_key[(artist_id, title)].append((track_id, duration))
            if isrc:
                by_isrc[isrc].append((track_id, duration))
        for key, members in by_key.items():
            if len(members) < 2:
                continue
            if len(members) > DUPLICATE_GROUP_LIMIT:
                groups.append({"confidence": "uncertain", "reason": f"{len(members)} Tracks share this artist and "
                               "title: too many to pair; review manually", "track_ids": sorted(m[0] for m in members)})
                continue
            for cluster in _duration_clusters(members):
                groups.append({"confidence": "likely", "reason": "same artist and title, durations within 2 s",
                               "track_ids": cluster})
        seen_clusters = {tuple(g["track_ids"]) for g in groups if "track_ids" in g}
        for isrc, members in by_isrc.items():
            if len(members) < 2 or len(members) > DUPLICATE_GROUP_LIMIT:
                continue
            clusters = _duration_clusters(members)
            for cluster in clusters:
                if tuple(cluster) not in seen_clusters:
                    groups.append({"confidence": "likely", "reason": f"same ISRC {isrc}, durations within 2 s",
                                   "track_ids": cluster})
            clustered = {t for c in clusters for t in c}
            if len(clustered) < len(members):
                groups.append({"confidence": "uncertain", "reason": f"ISRC {isrc} is shared by Tracks of different "
                               "durations (an ISRC collision or a different edit)",
                               "track_ids": sorted(m[0] for m in members)})
        self.upgrade_with_decoded_identity(groups)
        self.attach_decisions(groups, DuplicateCandidate)
        for group in groups:
            self.add("duplicate", group["reason"], **{k: v for k, v in group.items() if k != "reason"})

    def _hash(self, path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(HASH_CHUNK):
                digest.update(chunk)
                self.hashed_bytes += len(chunk)
        return digest.hexdigest()

    def upgrade_with_decoded_identity(self, groups):
        """Within a likely group, trusted caches with one identity prove the same audio; different ones disprove it."""
        from library.models import Track

        ids = {t for g in groups if g["confidence"] == "likely" for t in g["track_ids"]}
        identity = {}
        for track in Track.objects.filter(pk__in=ids).iterator(chunk_size=500):
            verdict = waveform_cache.validate(track)
            if verdict.trusted:
                identity[track.pk] = audio_identity.revision(verdict.payload.get("audio_identity"))
        for group in groups:
            if group["confidence"] != "likely":
                continue
            known = {identity.get(t) for t in group["track_ids"]}
            if None not in known and len(known) == 1:
                group.update(confidence="audio_equivalent",
                             reason=group["reason"] + "; decoded audio identical (trusted waveform caches)")
            elif None not in known and len(known) > 1:
                group.update(confidence="uncertain",
                             reason=group["reason"] + "; but the decoded audio differs (trusted waveform caches)")

    def attach_decisions(self, groups, DuplicateCandidate):
        pairs = {}
        ids = set()
        for group in groups:
            members = group.get("track_ids") or [f["track_id"] for f in group.get("files", []) if f.get("track_id")]
            group["members"] = sorted(members)
            ids.update(members)
        for a, b, resolution, applied in DuplicateCandidate.objects.filter(
                track_a_id__in=ids, track_b_id__in=ids).values_list("track_a_id", "track_b_id", "resolution", "applied"):
            pairs[(a, b)] = {"resolution": resolution, "applied": applied}
        for group in groups:
            members = group["members"]
            decisions = {f"{a}-{b}": pairs[(a, b)] for i, a in enumerate(members) for b in members[i + 1:]
                         if (a, b) in pairs}
            group["decisions"] = decisions
            total_pairs = len(members) * (len(members) - 1) // 2
            not_dup = sum(1 for d in decisions.values() if d["resolution"] == "keep_both")
            group["open"] = total_pairs > 0 and not_dup < total_pairs
            group["pending_deletion_decisions"] = sorted(
                k for k, d in decisions.items() if d["resolution"] in ("keep_a", "keep_b") and not d["applied"])

    # -- run
    def run(self) -> dict:
        started = timezone.now()
        scan_id = _new_scan_id(started)
        timings, mark = {}, time.perf_counter()

        def lap(name):
            nonlocal mark
            now = time.perf_counter()
            timings[name] = round(now - mark, 3)
            mark = now

        self.check_root()
        self.progress("walk", 0)
        self.walk()
        lap("walk")
        self.progress("tracks", 0)
        self.tracks()
        self.unregistered()
        lap("tracks")
        suspect = self.storage_suspect()
        self.missing_tracks(suspect)
        lap("missing_and_relink_evidence")
        self.progress("duplicates", 0)
        self.duplicates()
        lap("duplicates")
        self.counts["unsupported_files"] = sum(self.unsupported.values())
        open_duplicates = sum(1 for f in self.findings if f["type"] == "duplicate" and f.get("open"))
        return {
            "schema": SCHEMA, "schema_version": SCHEMA_VERSION, "scan_id": scan_id,
            "status": "suspect" if suspect else "complete",
            "status_reason": ("most Tracks' files are absent: the Library storage may be unmounted or unavailable; "
                              "missing Tracks are reported as uncertain") if suspect else "",
            "started_at": started.isoformat(), "finished_at": timezone.now().isoformat(),
            "library_root": self.root,
            "counts": {**{t: self.counts.get(t, 0) for t in TYPES},
                       **{k: self.counts.get(k, 0) for k in ("healthy", "tracks_total", "tracks_local",
                                                             "files_total", "hidden_entries", "unsupported_files")},
                       "duplicate_groups_open": open_duplicates,
                       "relink_reviewable": sum(1 for f in self.findings if f["type"] == "moved"
                                                and f.get("relink") is not None)},
            "timings_seconds": timings,
            "budgets": {"decodes": self.decodes, "max_decodes": self.max_decodes,
                        "hashed_bytes": self.hashed_bytes, "max_hash_bytes": self.max_hash_bytes},
            "unsupported_extensions": dict(sorted(self.unsupported.items())),
            "hidden_samples": self.samples["hidden"],
            "errors": self.errors,
            "findings": self.findings,
        }


def _new_scan_id(when) -> str:
    """Unique even for two scans in one second: a later scan must never reuse an earlier report's id."""
    return when.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)


def _duration_clusters(members):
    """Track-id groups of two or more whose known durations lie within the tolerance of their neighbour."""
    known = sorted((d, t) for t, d in members if d is not None)
    clusters, current = [], []
    for duration, track_id in known:
        if current and duration - current[-1][0] > DURATION_TOLERANCE_SECONDS:
            if len(current) > 1:
                clusters.append(sorted(t for _d, t in current))
            current = []
        current.append((duration, track_id))
    if len(current) > 1:
        clusters.append(sorted(t for _d, t in current))
    return clusters


def run_scan(**kwargs) -> dict:
    """Run one scan under the scan lock and store its report (a failure stores a failed attempt)."""
    directory = report_dir()
    with scan_lock():
        started = time.time()
        running_path = directory / "running.json"

        def progress(phase, done):
            if done % 2000 == 0 or phase in ("decode",):
                _write_json_atomic(running_path, {"pid": os.getpid(), "started": started, "phase": phase,
                                                  "done": done, "heartbeat": time.time()})
            if kwargs.get("echo"):
                kwargs["echo"](phase, done)

        def failed(reason):
            return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "status": "failed",
                    "scan_id": _new_scan_id(timezone.now()),
                    "status_reason": reason, "started_at": None, "finished_at": timezone.now().isoformat(),
                    "counts": {}, "findings": [], "errors": [reason]}

        scanner = Scanner(progress=progress, **{k: v for k, v in kwargs.items() if k != "echo"})
        try:
            report = scanner.run()
        except ScanError as exc:
            report = failed(str(exc))
        except Exception as exc:
            # Recorded as a failed attempt (the last complete report stays "latest"), then re-raised.
            write_report(failed(f"the scan stopped with an unexpected error ({type(exc).__name__}: {exc})"))
            raise
        finally:
            with contextlib.suppress(FileNotFoundError):
                running_path.unlink()
        write_report(report)
        return report
