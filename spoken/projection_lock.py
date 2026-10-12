"""Cross-transaction serialization for one spoken projection.

A delivery must commit its unavailable barrier before touching the stable
library pathname.  A PostgreSQL session advisory lock spans that commit and
the later publish/finalize transaction.  Reconciliation takes the same lock
non-blockingly: failure means a live operation owns the barrier; success means
any committed ``retiring`` state was abandoned and is safe to roll back.

The row locks in :mod:`spoken.delivery` remain the write-order authority.  The
advisory lock only closes the otherwise unavoidable gap between transactions
and coordinates the engine's shared deck-open lock.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

from django.db import connection, transaction


# "SPKN" in an independent two-int PostgreSQL advisory-lock namespace.
_NAMESPACE = 0x53504B4E


def _signed_int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def key(asset_id) -> tuple[int, int]:
    """A stable lock key.  A rare low-word collision only over-serializes."""
    return _signed_int32(_NAMESPACE), _signed_int32(uuid.UUID(str(asset_id)).int)


def _postgres() -> None:
    if connection.vendor != "postgresql":
        raise RuntimeError("spoken projection serialization requires PostgreSQL advisory locks")


@contextmanager
def session(asset_id, *, shared=False, wait=True):
    """Hold an asset lock across transaction boundaries.

    Yields ``False`` only for a non-blocking attempt that found another
    session holding a conflicting lock.  PostgreSQL releases this lock if the
    process/connection dies, which is the crash witness reconciliation needs.
    """
    _postgres()
    first, second = key(asset_id)
    suffix = "_shared" if shared else ""
    function = f"pg_advisory_lock{suffix}" if wait else f"pg_try_advisory_lock{suffix}"
    unlock = f"pg_advisory_unlock{suffix}"
    acquired = False
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {function}(%s, %s)", [first, second])
        result = cursor.fetchone()[0]
        acquired = True if wait else bool(result)
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT {unlock}(%s, %s)", [first, second])
                if not cursor.fetchone()[0]:
                    raise RuntimeError("spoken projection advisory lock was not held at release")


class DeckLease:
    """The engine's shared deck-open lock, bound to the backend that took it.

    A session advisory lock lives exactly as long as its PostgreSQL session,
    so the lease is only as good as the connection object it was taken on:
    ``intact()`` is a query-free identity check that the same open
    connection is still the thread's Django connection.
    """

    def __init__(self, asset_id, raw, backend_pid):
        self.asset_id = asset_id
        self.backend_pid = backend_pid
        self.release_problem = None
        self._raw = raw

    def intact(self) -> bool:
        raw = connection.connection
        return raw is not None and raw is self._raw and not raw.closed

    def release(self) -> str | None:
        """Unlock on the owning session.  Never raises: a deck may already be
        live.  Returns None, or a description of what went wrong."""
        if not self.intact():
            return "the session holding the deck-open lock ended before release"
        first, second = key(self.asset_id)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock_shared(%s, %s)", [first, second])
                released = cursor.fetchone()[0]
        except Exception as exc:                                  # noqa: BLE001 -- reported by the caller
            return f"deck-open lock release failed: {type(exc).__name__}: {exc}"
        return None if released else "the owning session no longer held the deck-open lock"


@contextmanager
def deck_lease(asset_id):
    """The engine's non-blocking shared lock for one deck open.

    Yields None when promotion/reconcile holds the exclusive form (the engine
    must not wait on a projection).  Otherwise yields a :class:`DeckLease`;
    release problems are recorded on ``lease.release_problem`` instead of
    raised, because the caller's deck may already be on air.
    """
    _postgres()
    first, second = key(asset_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock_shared(%s, %s), pg_backend_pid()", [first, second])
        acquired, backend_pid = cursor.fetchone()
    if not acquired:
        yield None
        return
    lease = DeckLease(asset_id, connection.connection, backend_pid)
    try:
        yield lease
    finally:
        lease.release_problem = lease.release()


def transaction_lock(asset_id, *, shared=False, wait=True) -> bool:
    """Take the asset lock for the current transaction."""
    _postgres()
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("transaction advisory lock requires transaction.atomic()")
    first, second = key(asset_id)
    suffix = "_shared" if shared else ""
    function = f"pg_advisory_xact_lock{suffix}" if wait else f"pg_try_advisory_xact_lock{suffix}"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {function}(%s, %s)", [first, second])
        result = cursor.fetchone()[0]
        return True if wait else bool(result)


def try_transaction(asset_id, *, shared=False) -> bool:
    """Take the asset lock for the current transaction, without waiting."""
    return transaction_lock(asset_id, shared=shared, wait=False)
