"""The active-message aggregate render (iPortal Phase D2A).

Everything here starts from an execution's FROZEN composition snapshot --
never from the current active set, the current ReferenceAudio registration of
a key, a filename or a path:

* ``frozen_inputs`` -- the exact ordered inputs: the snapshot's stinger pin
  (when present: {key, reference, media, sha256}), then every component
  {message, kind, media, sha256} in frozen order;
* ``expected_digest`` -- the canonical provenance digest a rendition of
  exactly those inputs carries (production.services.render.provenance's
  assembly document: assembly key/version, render engine, the fixed output
  format and every input's role, identity and SHA-256, in order);
* ``find_reusable`` -- an already-made VALID, PRESENT rendition whose stored
  provenance digest is that digest (see "Reuse" below);
* ``render`` -- a new rendition through isadoraair-render's ``assemble``
  operation (render.client: every input opened by exact identity and
  hash-verified, the stinger by its pinned registration UUID; the service
  re-verifies every byte) and the sole rendition intake (validated by
  isadoraair-validation);
* ``attribute_input_failure`` -- after the service reports a descriptor
  mismatch, re-verify the frozen inputs to name the one that is unusable.

Reuse: a rendition's ``recipe_params_digest`` is the SHA-256 of its whole
provenance document, so a digest equal to ``expected_digest`` proves the
complete ordered input identity (and the assembly version and engine) -- not
merely a matching composition-digest string. The lookup is bounded: it goes
through the rendition's ``derived_from`` foreign key (indexed; always the
first message's media) and then the exact digest, kind, recipe key/version,
executor marker, validity and presence. Finally its STORED bytes are re-hashed
against the row's SHA-256 (media_io.verify_integrity) -- outside every
database lock -- and a missing, size-inconsistent or corrupt candidate is
never reused.

Nothing here touches the database's domain rows: binding is
``services.bind_execution_mix`` (authority, locks, provenance re-proof).
"""
from __future__ import annotations

from decimal import Decimal

from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged
from production.models import ProductionMedia, ReferenceAudio
from production.services import media_io
from production.services.render import client, protocol, provenance, rendition
from production.services.render.engine import RenderError

ASSEMBLY_KEY = "active_message_mix"
# The render's own deadline: generous for a 20-minute aggregate (the service
# applies the same 120 s + 6x formula and its own hard caps).
MIN_TIMEOUT_SECONDS = 120.0


def assembly():
    return provenance.assembly(ASSEMBLY_KEY)


def frozen_inputs(snapshot: dict) -> tuple[dict | None, list[dict]]:
    """(stinger pin or None, ordered components) exactly as frozen."""
    stinger = snapshot.get("stinger")
    pin = None if stinger is None else {name: stinger[name] for name in ("key", "reference", "media", "sha256")}
    components = [{name: component[name] for name in ("message", "kind", "media", "sha256")}
                  for component in snapshot["components"]]
    return pin, components


def provenance_inputs(snapshot: dict) -> list[dict]:
    pin, components = frozen_inputs(snapshot)
    inputs = [] if pin is None else [{"role": "stinger", **pin}]
    inputs += [{"role": "message", **component} for component in components]
    return [protocol.assembly_input_of(entry) for entry in inputs]


def expected_document(snapshot: dict) -> dict:
    return provenance.assembly_document(assembly(), provenance_inputs(snapshot))


def expected_digest(snapshot: dict) -> str:
    return provenance.digest(expected_document(snapshot))


def first_message_media(snapshot: dict) -> str:
    return snapshot["components"][0]["media"]


def proves(media: ProductionMedia, snapshot: dict) -> bool:
    """True only when ``media`` is a rendition whose stored provenance proves
    it is exactly the aggregate of this frozen snapshot."""
    recipe = assembly()
    return (media.kind == ProductionMedia.KIND_RENDITION
            and media.recipe_key == recipe.key and media.recipe_version == recipe.version
            and media.recipe_params_digest == expected_digest(snapshot)
            and str(media.derived_from_id) == first_message_media(snapshot)
            and media.toolchain.endswith(protocol.EXECUTOR_MARKER))


def _bytes_intact(media) -> bool:
    """The stored bytes still exist, have the recorded size and hash to the
    recorded SHA-256 (production's own integrity check -- the media row is
    the only authority). Slow for a long aggregate: callers hold no lock."""
    try:
        media_io.verify_integrity(media)
    except (MediaPurged, MediaInconsistent):
        return False
    return True


def find_reusable(snapshot: dict):
    """The oldest valid, present rendition proven to be exactly this
    aggregate AND whose stored bytes still verify, or None. A missing,
    size-inconsistent or corrupt candidate is skipped (never bound); the
    caller then renders afresh. Call with no database lock held."""
    recipe = assembly()
    candidates = (ProductionMedia.objects
                  .filter(derived_from_id=first_message_media(snapshot), kind=ProductionMedia.KIND_RENDITION,
                          recipe_key=recipe.key, recipe_version=recipe.version,
                          recipe_params_digest=expected_digest(snapshot),
                          validation_state=ProductionMedia.VALIDATION_VALID,
                          retention_state=ProductionMedia.RETENTION_PRESENT)
                  .order_by("created_at", "id")[:5])
    return next((media for media in candidates if proves(media, snapshot) and _bytes_intact(media)), None)


def timeout_seconds(snapshot: dict) -> float:
    seconds = float(Decimal(snapshot.get("accounted_seconds") or 0))
    return min(max(MIN_TIMEOUT_SECONDS, 120.0 + 6.0 * seconds), protocol.MAX_TIMEOUT_SECONDS)


def render(execution, *, request=client.request_assembly):
    """Render the frozen aggregate of ``execution`` into a new VALID
    rendition (RenderResult). No database lock is held: this is the long part.
    Raises RenderError."""
    snapshot = execution.composition_snapshot
    pin, components = frozen_inputs(snapshot)
    output = request(ASSEMBLY_KEY, stinger=pin, components=components, execution=execution.pk,
                     timeout_seconds=timeout_seconds(snapshot))
    try:
        source = ProductionMedia.objects.get(pk=output.source_id)
    except ProductionMedia.DoesNotExist:
        output.close()
        raise RenderError(client.INPUT_UNAVAILABLE, "the first message's media no longer exists") from None
    return rendition.ingest(output, source)


def _intact(media_id, sha256) -> bool:
    try:
        opened = media_io.open_media(media_id, require_valid=True)
    except (MediaPurged, MediaNotValidated, MediaInconsistent):
        return False
    try:
        return opened.media.sha256 == sha256 and client._hash_file(opened.file) == sha256
    finally:
        opened.close()


def attribute_input_failure(snapshot: dict) -> str:
    """Which frozen input is unusable now: client.STINGER_UNAVAILABLE,
    client.INPUT_UNAVAILABLE, or "" (all inputs verify again)."""
    pin, components = frozen_inputs(snapshot)
    if pin is not None:
        row = ReferenceAudio.objects.filter(pk=pin["reference"]).values_list("key", "media_id", "sha256").first()
        if row is None or (row[0], str(row[1]), row[2]) != (pin["key"], pin["media"], pin["sha256"]) \
                or not _intact(pin["media"], pin["sha256"]):
            return client.STINGER_UNAVAILABLE
    for component in components:
        if not _intact(component["media"], component["sha256"]):
            return client.INPUT_UNAVAILABLE
    return ""
