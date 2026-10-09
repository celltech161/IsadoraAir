"""Typed, bounded recipe parameters.

A recipe declares every parameter it accepts. Configuration (e.g. a spoken
content type's ``recipe_params``) may only override declared parameters, and
only with a value of the declared type inside the declared bounds. Nothing a
parameter accepts can become a filter expression, a command or a path: numbers
are numbers (formatted by the step library), and a reference parameter is a
slug resolved through the ReferenceAudio registry.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ...models import REFERENCE_KEY_PATTERN

_REFERENCE_KEY_RE = re.compile(REFERENCE_KEY_PATTERN)
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class RecipeParameterError(ValueError):
    """``recipe_params`` violates the recipe's declared schema. ``code`` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NumberParam:
    """A finite number in [minimum, maximum]. ``integer`` refuses fractions.
    Booleans and strings are never numbers."""

    default: float
    minimum: float
    maximum: float
    integer: bool = False
    help: str = ""

    def __post_init__(self):
        if not (self.minimum <= self.default <= self.maximum):
            raise ValueError("a parameter default must lie within its bounds")

    def validate(self, name, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RecipeParameterError("wrong_parameter_type", f"{name} must be a number")
        if not math.isfinite(value):
            raise RecipeParameterError("wrong_parameter_type", f"{name} must be a finite number")
        if self.integer and value != int(value):
            raise RecipeParameterError("wrong_parameter_type", f"{name} must be a whole number")
        if not (self.minimum <= value <= self.maximum):
            raise RecipeParameterError("parameter_out_of_bounds",
                                       f"{name} must be between {self.minimum} and {self.maximum}")
        return int(value) if self.integer else float(value)

    def describe(self) -> dict:
        return {"type": "integer" if self.integer else "number", "default": self.default,
                "minimum": self.minimum, "maximum": self.maximum, "help": self.help}


@dataclass(frozen=True)
class ReferenceParam:
    """The key of station reference audio (a bed). A slug -- never a path."""

    default: str
    help: str = ""

    def __post_init__(self):
        if not _REFERENCE_KEY_RE.fullmatch(self.default):
            raise ValueError("a reference default must be a reference key")

    def validate(self, name, value):
        if not isinstance(value, str):
            raise RecipeParameterError("wrong_parameter_type", f"{name} must be a reference key")
        if not _REFERENCE_KEY_RE.fullmatch(value):
            raise RecipeParameterError("invalid_reference_key", f"{name} must be a reference key, not a path")
        return value

    def describe(self) -> dict:
        return {"type": "reference", "default": self.default, "help": self.help}


def check_names(params: dict) -> None:
    for name in params:
        if not _PARAM_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid parameter name {name!r}")


def resolve(schema: dict, raw) -> dict:
    """Declared defaults overridden by ``raw`` (a JSON object), validated.
    Returns a canonical dict with every declared parameter present."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RecipeParameterError("invalid_parameters", "recipe parameters must be a JSON object")
    unknown = sorted(str(key) for key in raw if key not in schema)
    if unknown:
        raise RecipeParameterError("unknown_parameter", f"unknown recipe parameter(s): {', '.join(unknown)[:200]}")
    resolved = {}
    for name, param in schema.items():
        resolved[name] = param.validate(name, raw[name]) if name in raw else param.validate(name, param.default)
    return resolved
