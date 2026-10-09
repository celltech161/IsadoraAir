"""The isadoraair-render client: ask the render service for one render.

The caller names AUTHORITATIVE things -- a source ProductionMedia, a registered
recipe and its parameters -- and this module turns them into a protocol
request: it opens the source and every pinned reference (ReferenceAudio,
hash-verified) as READ-ONLY descriptors, computes the canonical provenance it
expects (render.provenance -- the same helper completion uses) and sends both.

It accepts an answer only if:

* the socket is the service's (same account; SO_PEERCRED) in its 0700 runtime
  directory -- "local" is never authentication on its own;
* exactly one descriptor came back, a memfd SEALED against any further write,
  growth or shrinkage -- so the bytes hashed and the bytes ingested are the
  same bytes, held open from the service to the media store (never a path);
* its size and SHA-256 are the ones the service reported;
* recipe, version and provenance digest are exactly what was asked for.

The result is an ExecutorOutput: an identity-sealed, single-use value that only
render.rendition will turn into a rendition (a hand-built one is refused).
"""
from __future__ import annotations

import array
import fcntl
import json
import os
import secrets
import select
import socket
import stat
import sys
import time
import weakref
from dataclasses import dataclass

from ...models import ProductionMedia
from .. import media_io, reference_audio
from ...errors import MediaInconsistent, MediaNotValidated, MediaPurged
from . import protocol, provenance, registry
from . import params as params_mod
from .engine import RenderError
from .executor import SEALS, fd_sha256

CONNECT_TIMEOUT_SECONDS = 5.0
RESPONSE_GRACE_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 1800.0


@dataclass(frozen=True, eq=False)
class ExecutorOutput:
    """What the render service returned for one request (never authority by
    itself: render.rendition accepts only outputs this module issued)."""

    fd: int
    request_id: str
    source_id: str
    recipe_key: str
    recipe_version: int
    digest: str
    provenance_json: str
    toolchain: str
    sha256: str
    size: int
    measured_input: dict

    def close(self) -> None:
        finalizer = _FINALIZERS.pop(self, None)
        if finalizer is not None:
            finalizer()                     # closes the descriptor exactly once


_ISSUED = weakref.WeakSet()
_FINALIZERS = weakref.WeakKeyDictionary()


def issued(output) -> bool:
    return output in _ISSUED


def consume(output) -> None:
    """Single use: render.rendition takes an output exactly once."""
    if output not in _ISSUED:
        raise RenderError("rendition_authority", "only outputs issued by the render client become renditions")
    _ISSUED.discard(output)


def _socket_path() -> str:
    from ..render_service import socket_path
    return socket_path()


def _open_references(plan, resolved):
    opened = {}
    try:
        for name, key in sorted(provenance.reference_keys(plan, resolved).items()):
            try:
                opened[name] = reference_audio.resolve(key)
            except reference_audio.ReferenceAudioError as exc:
                raise RenderError(exc.code, str(exc)) from exc
        return opened
    except BaseException:
        for reference in opened.values():
            reference.close()
        raise


def _media_entry(row: ProductionMedia) -> dict:
    return {"media": str(row.pk), "sha256": row.sha256, "bytes": row.byte_size,
            "duration_seconds": str(row.decoded_duration_seconds)}


def _receive(sock, give_up):
    data, fds = bytearray(), []
    while not data.endswith(b"\n"):
        left = give_up - time.monotonic()
        if left <= 0:
            raise RenderError("service_unavailable", "the render service did not answer in time", retryable=True)
        readable, _, _ = select.select([sock], [], [], min(left, 1.0))
        if not readable:
            continue
        try:
            chunk, ancdata, flags, _ = sock.recvmsg(protocol.REPLY_LIMIT_BYTES, socket.CMSG_SPACE(4 * 4),
                                                    socket.MSG_CMSG_CLOEXEC)
        except OSError as exc:
            raise RenderError("service_unavailable", f"render service connection failed: {exc.strerror}",
                              retryable=True) from None
        for level, kind, payload in ancdata:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                usable = len(payload) - len(payload) % 4
                fds.extend(int.from_bytes(payload[i:i + 4], sys.byteorder, signed=True)
                           for i in range(0, usable, 4))
        if flags & socket.MSG_CTRUNC:
            _close_all(fds)
            raise RenderError("descriptor_invalid", "the render service sent too many descriptors")
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > protocol.REPLY_LIMIT_BYTES:
            _close_all(fds)
            raise RenderError("service_unavailable", "oversized reply from the render service", retryable=True)
    return bytes(data), fds


def _close_all(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _verify_output(fd: int, answer: dict) -> None:
    try:
        info = os.fstat(fd)
        seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
    except OSError:
        raise RenderError("descriptor_invalid", "the rendition descriptor is not a sealed memfd") from None
    if not stat.S_ISREG(info.st_mode) or seals & SEALS != SEALS:
        raise RenderError("descriptor_invalid", "the rendition descriptor is not sealed against change")
    output = answer.get("output")
    if not isinstance(output, dict) or info.st_size != output.get("bytes") or fd_sha256(fd) != output.get("sha256"):
        raise RenderError("descriptor_mismatch", "the rendition bytes are not the ones the service reported")


def request_render(source, recipe_key, recipe_params=None, *, item=None, lease=None,
                   timeout_seconds=None) -> ExecutorOutput:
    """Render ``source`` (a ProductionMedia or its id) through the render
    service. Raises RenderError (stable code; ``retryable``)."""
    try:
        recipe, resolved, plan = provenance.prepare(recipe_key, recipe_params)
    except registry.UnknownRecipe as exc:
        raise RenderError("unknown_recipe", str(exc)) from exc
    except params_mod.RecipeParameterError as exc:
        raise RenderError(exc.code, str(exc)) from exc
    try:
        opened = media_io.open_media(source, require_valid=True)
    except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
        raise RenderError("source_not_usable", "the source media is missing, purged or not valid") from exc
    references = {}
    sock = None
    try:
        references = _open_references(plan, resolved)
        pins = {name: reference.provenance() for name, reference in references.items()}
        expected = provenance.digest(provenance.document(recipe, resolved, pins))
        timeout = float(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        request_id = secrets.token_hex(16)
        body = {
            "v": protocol.PROTOCOL_VERSION, "op": "render", "request_id": request_id,
            "correlation": {"item": None if item is None else str(getattr(item, "pk", item)),
                            "lease": None if lease is None else str(lease)},
            "source": _media_entry(opened.media), "recipe": {"key": recipe.key, "version": recipe.version},
            "params": dict(recipe_params or {}),
            "references": {name: {**_media_entry(reference.media), "key": reference.key,
                                  "reference": reference.reference_id}
                           for name, reference in references.items()},
            "expected_digest": expected, "timeout": min(timeout, protocol.MAX_TIMEOUT_SECONDS),
        }
        line = (json.dumps(body, sort_keys=True) + "\n").encode()
        fds = [opened.file.fileno(), *(references[name].file.fileno() for name in sorted(references))]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(_socket_path())
            from ..confinement import peer_uid
            if peer_uid(sock) != os.geteuid():
                raise RenderError("service_unavailable", "the render socket belongs to another account",
                                  retryable=True)
            sock.sendmsg([line], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
        except BlockingIOError:
            raise RenderError("busy", "the render service's accept queue is full", retryable=True) from None
        except OSError as exc:
            raise RenderError("service_unavailable", f"render service unavailable: {exc.strerror or exc}",
                              retryable=True) from None
    finally:
        opened.file.close()
        for reference in references.values():
            reference.close()

    try:
        data, fds = _receive(sock, time.monotonic() + timeout + RESPONSE_GRACE_SECONDS)
    finally:
        sock.close()
    try:
        answer = json.loads(data.decode("utf-8")) if data else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or answer.get("v") != protocol.PROTOCOL_VERSION:
        _close_all(fds)
        raise RenderError("service_unavailable", "the render service gave no valid answer", retryable=True)
    if answer.get("status") != "ok":
        _close_all(fds)
        code = answer.get("code") if answer.get("code") in protocol.CODES else "render_failed"
        raise RenderError(code, f"the render service refused or failed ({code})",
                          retryable=protocol.CODES.get(code, False), stage=str(answer.get("stage", ""))[:40])
    if len(fds) != 1:
        _close_all(fds)
        raise RenderError("descriptor_invalid", "a successful render returns exactly one descriptor")
    fd = fds[0]
    try:
        _verify_output(fd, answer)
        document = answer.get("provenance")
        if (answer.get("request_id") != request_id
                or answer.get("recipe") != {"key": recipe.key, "version": recipe.version}
                or answer.get("digest") != expected or not isinstance(document, dict)
                or provenance.digest(document) != expected):
            raise RenderError("provenance_mismatch", "the render service answered for another render")
        toolchain = answer.get("toolchain")
        if not isinstance(toolchain, str) or not toolchain.endswith(protocol.EXECUTOR_MARKER) \
                or len(toolchain) > 200:
            raise RenderError("provenance_mismatch", "the render answer carries no executor identity")
        measured = answer.get("measured_input") if isinstance(answer.get("measured_input"), dict) else {}
    except BaseException:
        os.close(fd)
        raise
    output = ExecutorOutput(fd=fd, request_id=request_id, source_id=str(opened.media.pk), recipe_key=recipe.key,
                            recipe_version=recipe.version, digest=expected,
                            provenance_json=json.dumps(document, sort_keys=True), toolchain=toolchain,
                            sha256=answer["output"]["sha256"], size=answer["output"]["bytes"],
                            measured_input=measured)
    _ISSUED.add(output)
    _FINALIZERS[output] = weakref.finalize(output, _close_quiet, fd)
    return output


def _close_quiet(fd):
    try:
        os.close(fd)
    except OSError:
        pass
