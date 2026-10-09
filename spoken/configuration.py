"""Validation of SpokenContentType configuration -- run on EVERY save.

* ``code``: a slug, permanent once any item uses the type;
* ``recipe_key``: must resolve in the production render registry, and
  ``recipe_params`` may only override that recipe's declared parameters,
  with the declared types and bounds (the render engine re-validates at
  render time);
* media limits may only tighten the ProductionMedia platform ceilings;
* an ENABLED type that projects into the library (current_edition /
  accumulating) needs a ``target_category`` -- a disabled type may be saved
  half-configured, which keeps creation and configuration safe.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError

from production import policy as media_policy
from production.services import render

from .guard import SpokenIntegrityError


class ConfigurationError(ValidationError):
    """The type's configuration is not acceptable. ``code`` is stable."""


def _fail(field, code, message):
    raise ConfigurationError({field: [ValidationError(message, code=code)]}, code=code)


def check_recipe(recipe_key, recipe_params) -> dict:
    try:
        recipe = render.get(recipe_key)
    except render.UnknownRecipe:
        _fail("recipe_key", "unknown_recipe", f"{recipe_key!r} is not a registered recipe")
    try:
        return recipe.resolve(recipe_params)
    except render.RecipeParameterError as exc:
        _fail("recipe_params", exc.code, str(exc))


def check_limits(content_type) -> None:
    ceiling = Decimal(media_policy.PLATFORM_MAX_DURATION_SECONDS)
    for field in ("min_duration_seconds", "max_duration_seconds"):
        value = getattr(content_type, field)
        if value is not None and not (Decimal(0) < Decimal(value) <= ceiling):
            _fail(field, "limit_out_of_range", f"must be greater than 0 and at most {ceiling} seconds")
    low, high = content_type.min_duration_seconds, content_type.max_duration_seconds
    if low is not None and high is not None and Decimal(low) > Decimal(high):
        _fail("min_duration_seconds", "limit_order", "the minimum duration exceeds the maximum")
    if content_type.max_bytes is not None and not (0 < content_type.max_bytes <= media_policy.PLATFORM_MAX_BYTES):
        _fail("max_bytes", "limit_out_of_range", f"must be 1..{media_policy.PLATFORM_MAX_BYTES} bytes")


def check_projection(content_type) -> None:
    if content_type.enabled and content_type.projects_to_library and content_type.target_category_id is None:
        _fail("target_category", "target_category_required",
              "an enabled type that delivers to the library needs a target category")


def check_code_permanence(content_type) -> None:
    if content_type._state.adding:
        return
    model = type(content_type)
    stored = model._base_manager.filter(pk=content_type.pk).values_list("code", flat=True).first()
    if stored is not None and stored != content_type.code and content_type.items.exists():
        raise SpokenIntegrityError(f"content type {stored!r} is in use; its code is permanent")


def check_before_save(content_type) -> None:
    check_code_permanence(content_type)
    if not isinstance(content_type.recipe_params, dict):
        _fail("recipe_params", "invalid_parameters", "recipe parameters must be a JSON object")
    check_recipe(content_type.recipe_key, content_type.recipe_params)
    check_limits(content_type)
    check_projection(content_type)


def media_policy_for(content_type) -> media_policy.MediaPolicy:
    """The ProductionMedia intake/validation bounds this type imposes."""
    return media_policy.MediaPolicy(
        max_bytes=content_type.max_bytes or media_policy.PLATFORM_MAX_BYTES,
        min_duration_seconds=float(content_type.min_duration_seconds) if content_type.min_duration_seconds else None,
        max_duration_seconds=float(content_type.max_duration_seconds) if content_type.max_duration_seconds else None,
        require_engine_decode=True,
    )
