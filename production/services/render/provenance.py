"""THE canonical render provenance -- shared by rendering and by completion.

A rendition's ``recipe_params_digest`` is the canonical SHA-256 of one
document that names every output-changing input:

    {"recipe": key, "version": recipe version, "engine": ENGINE_VERSION,
     "params": the fully resolved parameters (defaults included),
     "references": {param: {"key", "reference", "media", "sha256"}}}

``render()`` builds it from the references it actually opened and
hash-verified; a consuming domain completing a render recomputes it with
``expected()`` from its AUTHORITATIVE configuration and the references that
are current NOW, and compares. Any difference -- another recipe version,
other parameters, a replaced bed -- means the rendition is not the render the
domain currently authorizes.

``RenderEvidence`` is the primitive claim a render hands back (and that C2
will carry over a socket). It is NOT authority: anyone can construct one. A
consumer must check it against the persisted ProductionMedia row and its own
``expected()`` result -- never trust it alone.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import intake, reference_audio
from . import registry

ENGINE_VERSION = 1


@dataclass(frozen=True)
class RenderEvidence:
    rendition_id: str
    source_id: str
    recipe_key: str
    recipe_version: int
    provenance_digest: str

    def __post_init__(self):
        for name in ("rendition_id", "source_id", "recipe_key", "provenance_digest"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"render evidence {name} must be a string")
        if isinstance(self.recipe_version, bool) or not isinstance(self.recipe_version, int):
            raise TypeError("render evidence recipe_version must be an integer")

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ExpectedRender:
    recipe_key: str
    recipe_version: int
    params: dict
    references: dict
    document: dict
    digest: str


def prepare(recipe_key, recipe_params):
    """(recipe, resolved parameters, plan). Raises registry.UnknownRecipe or
    params.RecipeParameterError."""
    recipe = registry.get(recipe_key)
    resolved = recipe.resolve(recipe_params)
    return recipe, resolved, recipe.build(resolved)


def reference_keys(plan, resolved) -> dict:
    """{parameter name: ReferenceAudio key} for the references the plan USES."""
    if plan.bed is None:
        return {}
    return {plan.bed.reference_param: resolved[plan.bed.reference_param]}


def document(recipe, resolved, references) -> dict:
    return {"recipe": recipe.key, "version": recipe.version, "engine": ENGINE_VERSION, "params": resolved,
            "references": dict(references)}


def digest(doc) -> str:
    return intake.digest_recipe_params(doc)


def expected(recipe_key, recipe_params, *, pin=reference_audio.current_pin) -> ExpectedRender:
    """What a render of ``recipe_key``/``recipe_params`` against the CURRENT
    references produces. ``pin(key)`` returns a reference's provenance identity;
    the default reads it under the key's shared lock (call inside
    transaction.atomic()). Raises UnknownRecipe, RecipeParameterError or
    reference_audio.ReferenceAudioError."""
    recipe, resolved, plan = prepare(recipe_key, recipe_params)
    keys = reference_keys(plan, resolved)
    if pin is reference_audio.current_pin:
        # All the keys' shared locks first, in the one global order
        # (reference_audio.lock_keys), before any is read: a future
        # multi-reference recipe can never deadlock a concurrent completion.
        reference_audio.lock_keys(keys.values(), shared=True)
    references = {param: pin(key) for param, key in keys.items()}
    doc = document(recipe, resolved, references)
    return ExpectedRender(recipe_key=recipe.key, recipe_version=recipe.version, params=resolved,
                          references=references, document=doc, digest=digest(doc))
