"""Loading of shadowdiff.yaml ignore rules."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALUE_PATTERNS = ("uuid", "iso_timestamp")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    # Globs matched against a field name (e.g. "*_at"), or against the full
    # JSON path when the pattern starts with "$" (e.g. "$.meta.*").
    ignore_fields: tuple[str, ...] = ()
    # Named value patterns: a change between two values matching the same
    # pattern is noise (e.g. one UUID replaced by another).
    ignore_values: tuple[str, ...] = ()
    # JSON path of a list (indices written as [*]) -> key to match items by,
    # or None to compare the list as a sorted multiset.
    unordered_lists: dict[str, str | None] = field(default_factory=dict)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    path = Path(path)
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    unknown = set(data) - {"ignore_fields", "ignore_values", "unordered_lists"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(sorted(unknown))}")

    ignore_values = tuple(data.get("ignore_values") or ())
    bad = [v for v in ignore_values if v not in VALUE_PATTERNS]
    if bad:
        raise ConfigError(
            f"{path}: unknown ignore_values {bad}; supported: {', '.join(VALUE_PATTERNS)}"
        )

    unordered = data.get("unordered_lists") or {}
    if not isinstance(unordered, dict):
        raise ConfigError(f"{path}: unordered_lists must map a list path to a key")

    return Config(
        ignore_fields=tuple(str(f) for f in data.get("ignore_fields") or ()),
        ignore_values=ignore_values,
        unordered_lists={str(k): (None if v is None else str(v)) for k, v in unordered.items()},
    )
