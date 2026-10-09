"""The shared, code-backed audio render engine (iPortal Phase C).

Domain-neutral: it renders one ProductionMedia through a registered recipe
into a new immutable rendition. Domains register recipes and choose which to
use; they never add steps, filters, commands or paths.
"""
from .engine import ENGINE_VERSION, RenderError, RenderResult, parse_loudnorm, render  # noqa: F401
from .params import NumberParam, RecipeParameterError, ReferenceParam  # noqa: F401
from .registry import Recipe, UnknownRecipe, get, keys, register  # noqa: F401
from .steps import (  # noqa: F401
    BedMode, BedUnderlay, DelayVoice, EncodeFlac, Fade, Highpass, Loudnorm, RenderPlan, TrimSilence,
)
