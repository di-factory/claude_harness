"""Solution spec v1: schema, loader (pack + instance merge) and validation."""

from .errors import Issue, SpecError
from .loader import PackCatalog, ResolvedSpec, load_instance, load_pack, resolved_from_data
from .schema import SolutionSpec

__all__ = [
    "Issue",
    "PackCatalog",
    "ResolvedSpec",
    "SolutionSpec",
    "SpecError",
    "load_instance",
    "load_pack",
    "resolved_from_data",
]
