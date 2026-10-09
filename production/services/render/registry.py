"""The code-backed recipe registry.

A recipe is a stable ``key``, an integer ``version`` (bumped whenever its
output semantics change), a declared parameter schema and a ``plan`` function
that turns resolved parameters into a fixed RenderPlan. Recipes are registered
in code (domains register theirs at app start-up); configuration can only
choose a key and override declared parameters -- it can never add a step,
a filter, a command or a path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import params as params_mod
from .steps import RenderPlan

RECIPE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class UnknownRecipe(LookupError):
    code = "unknown_recipe"


@dataclass(frozen=True)
class Recipe:
    key: str
    version: int
    title: str
    plan: Callable[[dict], RenderPlan]
    params: dict = field(default_factory=dict)
    description: str = ""

    def __post_init__(self):
        if not RECIPE_KEY_RE.fullmatch(self.key):
            raise ValueError(f"invalid recipe key {self.key!r}")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("a recipe version is a positive integer")
        params_mod.check_names(self.params)

    def resolve(self, raw) -> dict:
        return params_mod.resolve(self.params, raw)

    def build(self, resolved: dict) -> RenderPlan:
        plan = self.plan(resolved)
        if not isinstance(plan, RenderPlan):
            raise TypeError(f"recipe {self.key} did not produce a RenderPlan")
        return plan

    def reference_params(self) -> list[str]:
        return [name for name, param in self.params.items() if isinstance(param, params_mod.ReferenceParam)]

    def describe(self) -> dict:
        return {"key": self.key, "version": self.version, "title": self.title, "description": self.description,
                "params": {name: param.describe() for name, param in self.params.items()}}


_REGISTRY: dict[str, Recipe] = {}


def register(recipe: Recipe) -> Recipe:
    existing = _REGISTRY.get(recipe.key)
    if existing is not None and existing is not recipe:
        raise ValueError(f"recipe {recipe.key!r} is already registered")
    _REGISTRY[recipe.key] = recipe
    return recipe


def get(key) -> Recipe:
    if not isinstance(key, str) or key not in _REGISTRY:
        raise UnknownRecipe(f"unknown recipe {str(key)[:64]!r}")
    return _REGISTRY[key]


def keys() -> list[str]:
    return sorted(_REGISTRY)
