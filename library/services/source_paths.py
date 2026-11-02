"""Per-source-path coordination between Track registration and post-delete media cleanup (3.11, L1).

A deleted Track's audio is unlinked *after* its row deletion commits.  A different transaction that
registers a Track at the same ``filepath`` is invisible to that cleanup until it commits, so without
coordination the cleanup can remove audio a Track is in the middle of claiming.  Both sides therefore
take the same PostgreSQL *transaction-scoped* advisory lock, keyed by the path:

* **Registration** -- ``Track.save()`` (create, or a ``filepath`` change), ``Track.objects.bulk_create`` and
  a literal ``filepath`` in ``QuerySet.update``/``bulk_update`` take the lock inside their transaction
  before the write, and keep it until that transaction ends (commit, rollback or a lost connection all
  release it; nothing can leak).
* **Cleanup** -- takes the lock in its own short transaction (bounded wait), and only then checks for a
  live owner and unlinks.  A registration that holds the lock makes the cleanup wait until it has
  committed (the owner is then visible) or rolled back (the path is genuinely free).

The lock is per path (64-bit hash), never global, and is taken after a caller's row locks and before any
waveform-cache lock; cleanup holds no database row lock, so there is no cycle with registration.  A wait is
always bounded (``lock_timeout``): the cleanup retains the file, a registration fails visibly.

It does not cover a file an outside program exposes at the final path before it registers a Track (the
syndicated ingest scripts copy the file, then start ``sync_track_file``).  That interval is covered by the
cleanup's conservative freshness rule in :mod:`library.services.track_deletion`.
"""
from __future__ import annotations

import hashlib
import os

from django.db import connections

#: A registration waits at most this long for a cleanup (or another registration) of the same path.
REGISTRATION_LOCK_TIMEOUT_MS = 10_000
#: A cleanup waits at most this long for a registration in flight; on timeout it retains the file.
CLEANUP_LOCK_TIMEOUT_MS = 5_000


def lock_key(path) -> int:
    """Deterministic signed 64-bit advisory-lock key for a source path (same string => same key)."""
    digest = hashlib.blake2b(os.fsencode(os.path.normpath(os.fspath(path))), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def acquire(path, using: str, *, timeout_ms: int) -> None:
    """Take the path's transaction-scoped advisory lock; the caller must be inside ``atomic``.

    Raises ``django.db.OperationalError`` if the lock is not obtained within ``timeout_ms`` (the surrounding
    transaction is then aborted, which releases anything it held)."""
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT current_setting('lock_timeout')")
        previous = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{int(timeout_ms)}ms"])
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key(path)])
        # The bound applies to this acquisition only, not to later statements of the caller's transaction.
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])
