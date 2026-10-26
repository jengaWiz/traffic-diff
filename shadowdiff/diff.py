"""Structural response diff with noise filtering.

Walks two JSON documents and reports each difference with a JSON path and a
kind. Ignore rules from ``Config`` drop known noise (timestamps, generated IDs,
reordered lists) so only meaningful changes are reported.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from shadowdiff.config import Config

VALUE_CHANGED = "value_changed"
TYPE_CHANGED = "type_changed"
FIELD_ADDED = "field_added"
FIELD_REMOVED = "field_removed"
STATUS_CHANGED = "status_changed"

BREAKING_KINDS = frozenset({TYPE_CHANGED, FIELD_REMOVED, STATUS_CHANGED})

VALUE_REGEXES = {
    "uuid": re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
    ),
    "iso_timestamp": re.compile(
        r"^\d{4}-\d{2}-\d{2}"
        r"([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
    ),
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INDEX = re.compile(r"\[(\d+|[^\[\]]*=[^\[\]]*)\]")
_MISSING = object()


@dataclass(frozen=True)
class Difference:
    path: str
    kind: str
    old: Any = None
    new: Any = None
    old_type: str | None = None
    new_type: str | None = None

    @property
    def breaking(self) -> bool:
        return self.kind in BREAKING_KINDS

    def to_dict(self) -> dict[str, Any]:
        d = {"path": self.path, "kind": self.kind, "old": self.old, "new": self.new}
        if self.kind == TYPE_CHANGED:
            d["old_type"] = self.old_type
            d["new_type"] = self.new_type
        d["breaking"] = self.breaking
        return d


def compare(
    old_status: int, old_body: bytes, new_status: int, new_body: bytes, config: Config
) -> list[Difference]:
    """Compare a recorded response with a replayed one."""
    if old_status != new_status:
        return [Difference("$", STATUS_CHANGED, old_status, new_status)]

    old_doc, old_is_json = _parse(old_body)
    new_doc, new_is_json = _parse(new_body)
    if old_is_json and new_is_json:
        return diff_json(old_doc, new_doc, config)
    if old_is_json != new_is_json:
        old_t = json_type(old_doc) if old_is_json else "text"
        new_t = json_type(new_doc) if new_is_json else "text"
        return [Difference("$", TYPE_CHANGED, old_doc, new_doc, old_t, new_t)]
    if old_doc == new_doc or _same_value_pattern(old_doc, new_doc, config):
        return []
    return [Difference("$", VALUE_CHANGED, old_doc, new_doc)]


def diff_json(old: Any, new: Any, config: Config, path: str = "$") -> list[Difference]:
    """Recursively diff two parsed JSON values."""
    out: list[Difference] = []
    _walk(old, new, path, config, out)
    return out


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def normalize_path(path: str) -> str:
    """Replace concrete list positions/keys with [*] so paths can be grouped."""
    return _INDEX.sub("[*]", path)


def _parse(body: bytes) -> tuple[Any, bool]:
    if body:
        try:
            return json.loads(body), True
        except ValueError:
            pass
    return body.decode("utf-8", errors="replace"), False


def _walk(old: Any, new: Any, path: str, config: Config, out: list[Difference]) -> None:
    old_t, new_t = json_type(old), json_type(new)
    if old_t != new_t:
        out.append(Difference(path, TYPE_CHANGED, old, new, old_t, new_t))
    elif old_t == "object":
        _walk_object(old, new, path, config, out)
    elif old_t == "array":
        _walk_array(old, new, path, config, out)
    elif old != new and not _same_value_pattern(old, new, config):
        out.append(Difference(path, VALUE_CHANGED, old, new))


def _walk_object(old: dict, new: dict, path: str, config: Config, out: list[Difference]) -> None:
    for key in sorted(old.keys() | new.keys()):
        child = _child_path(path, key)
        if _field_ignored(key, child, config.ignore_fields):
            continue
        o, n = old.get(key, _MISSING), new.get(key, _MISSING)
        if n is _MISSING:
            out.append(Difference(child, FIELD_REMOVED, old=o))
        elif o is _MISSING:
            out.append(Difference(child, FIELD_ADDED, new=n))
        else:
            _walk(o, n, child, config, out)


def _walk_array(old: list, new: list, path: str, config: Config, out: list[Difference]) -> None:
    rule = _unordered_rule(path, config)
    if rule is not _MISSING:
        key = rule
        if key is None:
            _walk_multiset(old, new, path, out)
            return
        old_by_key, new_by_key = _index_by(old, key), _index_by(new, key)
        if old_by_key is not None and new_by_key is not None:
            for k in sorted(old_by_key.keys() | new_by_key.keys(), key=json.dumps):
                child = f"{path}[{key}={json.dumps(k)}]"
                o, n = old_by_key.get(k, _MISSING), new_by_key.get(k, _MISSING)
                if n is _MISSING:
                    out.append(Difference(child, FIELD_REMOVED, old=o))
                elif o is _MISSING:
                    out.append(Difference(child, FIELD_ADDED, new=n))
                else:
                    _walk(o, n, child, config, out)
            return
        # Items without a unique key: fall back to positional comparison.

    for i in range(max(len(old), len(new))):
        child = f"{path}[{i}]"
        if i >= len(new):
            out.append(Difference(child, FIELD_REMOVED, old=old[i]))
        elif i >= len(old):
            out.append(Difference(child, FIELD_ADDED, new=new[i]))
        else:
            _walk(old[i], new[i], child, config, out)


def _walk_multiset(old: list, new: list, path: str, out: list[Difference]) -> None:
    canon = [json.dumps(x, sort_keys=True) for x in old]
    new_canon = [json.dumps(x, sort_keys=True) for x in new]
    removed = Counter(canon) - Counter(new_canon)
    added = Counter(new_canon) - Counter(canon)
    for i, c in enumerate(canon):
        if removed[c]:
            removed[c] -= 1
            out.append(Difference(f"{path}[{i}]", FIELD_REMOVED, old=old[i]))
    for i, c in enumerate(new_canon):
        if added[c]:
            added[c] -= 1
            out.append(Difference(f"{path}[{i}]", FIELD_ADDED, new=new[i]))


def _index_by(items: list, key: str) -> dict | None:
    """Map item[key] -> item, or None if any item lacks a unique, hashable key."""
    index: dict = {}
    for item in items:
        if not isinstance(item, dict) or key not in item:
            return None
        k = item[key]
        if isinstance(k, dict | list) or k in index:
            return None
        index[k] = item
    return index


def _unordered_rule(path: str, config: Config) -> Any:
    if not config.unordered_lists:
        return _MISSING
    if path in config.unordered_lists:
        return config.unordered_lists[path]
    return config.unordered_lists.get(normalize_path(path), _MISSING)


def _field_ignored(key: str, path: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern.startswith("$"):
            rx = _glob(pattern)
            if rx.match(path) or rx.match(normalize_path(path)):
                return True
        elif _glob(pattern).match(key):
            return True
    return False


@lru_cache(maxsize=256)
def _glob(pattern: str) -> re.Pattern:
    """Compile a glob where only * and ? are special (brackets are literal)."""
    parts = (".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.compile("".join(parts) + r"\Z")


def _same_value_pattern(old: Any, new: Any, config: Config) -> bool:
    if not (isinstance(old, str) and isinstance(new, str)):
        return False
    return any(
        VALUE_REGEXES[name].match(old) and VALUE_REGEXES[name].match(new)
        for name in config.ignore_values
    )


def _child_path(path: str, key: str) -> str:
    if _IDENTIFIER.match(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key)}]"
