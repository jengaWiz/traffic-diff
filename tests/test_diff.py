import json

import pytest

from shadowdiff.config import Config, ConfigError, load_config
from shadowdiff.diff import compare, diff_json

NO_RULES = Config()


def kinds(diffs):
    return sorted((d.path, d.kind) for d in diffs)


# --- basic structural diff ---------------------------------------------------


def test_identical_documents_have_no_diffs():
    doc = {"a": 1, "b": [1, 2, {"c": None}], "d": {"e": "x"}}
    assert diff_json(doc, json.loads(json.dumps(doc)), NO_RULES) == []


def test_value_changed():
    [d] = diff_json({"price": 10}, {"price": 12}, NO_RULES)
    assert (d.path, d.kind, d.old, d.new) == ("$.price", "value_changed", 10, 12)
    assert not d.breaking


def test_type_changed_number_to_string():
    [d] = diff_json({"price": 9.99}, {"price": "9.99"}, NO_RULES)
    assert (d.path, d.kind) == ("$.price", "type_changed")
    assert (d.old_type, d.new_type) == ("number", "string")
    assert d.breaking


def test_int_and_float_are_both_numbers():
    assert diff_json({"n": 1}, {"n": 1.0}, NO_RULES) == []


def test_bool_is_not_a_number():
    [d] = diff_json({"flag": True}, {"flag": 1}, NO_RULES)
    assert d.kind == "type_changed"


def test_null_to_value_is_type_change():
    [d] = diff_json({"x": None}, {"x": "set"}, NO_RULES)
    assert (d.kind, d.old_type, d.new_type) == ("type_changed", "null", "string")


def test_field_added_is_warning():
    [d] = diff_json({"a": 1}, {"a": 1, "b": 2}, NO_RULES)
    assert (d.path, d.kind, d.new) == ("$.b", "field_added", 2)
    assert not d.breaking


def test_field_removed_is_breaking():
    [d] = diff_json({"a": 1, "b": 2}, {"a": 1}, NO_RULES)
    assert (d.path, d.kind, d.old) == ("$.b", "field_removed", 2)
    assert d.breaking


def test_rename_is_remove_plus_add():
    diffs = diff_json({"in_stock": True}, {"available": True}, NO_RULES)
    assert kinds(diffs) == [("$.available", "field_added"), ("$.in_stock", "field_removed")]


def test_nested_paths():
    old = {"order": {"lines": [{"qty": 1}, {"qty": 2}]}}
    new = {"order": {"lines": [{"qty": 1}, {"qty": 3}]}}
    [d] = diff_json(old, new, NO_RULES)
    assert d.path == "$.order.lines[1].qty"


def test_non_identifier_keys_are_quoted():
    [d] = diff_json({"a b": 1}, {"a b": 2}, NO_RULES)
    assert d.path == '$["a b"]'


def test_ordered_list_length_change():
    diffs = diff_json({"xs": [1, 2, 3]}, {"xs": [1, 2]}, NO_RULES)
    assert kinds(diffs) == [("$.xs[2]", "field_removed")]
    diffs = diff_json({"xs": [1]}, {"xs": [1, 5]}, NO_RULES)
    assert kinds(diffs) == [("$.xs[1]", "field_added")]


def test_top_level_type_change():
    [d] = diff_json([1], {"a": 1}, NO_RULES)
    assert (d.path, d.kind) == ("$", "type_changed")


# --- ignore rules ------------------------------------------------------------


def test_ignore_field_globs():
    rules = Config(ignore_fields=("*_at", "request_id"))
    old = {"created_at": "a", "request_id": "x", "meta": {"updated_at": 1}, "v": 1}
    new = {"created_at": "b", "meta": {"updated_at": "2"}, "v": 1, "request_id": "y"}
    assert diff_json(old, new, rules) == []


def test_ignored_field_added_or_removed_is_ignored():
    rules = Config(ignore_fields=("request_id",))
    assert diff_json({"request_id": "x"}, {}, rules) == []
    assert diff_json({}, {"request_id": "x"}, rules) == []


def test_ignore_field_exact_name_does_not_overmatch():
    rules = Config(ignore_fields=("id",))
    old = {"id": 1, "user_id": 1}
    new = {"id": 2, "user_id": 2}
    assert kinds(diff_json(old, new, rules)) == [("$.user_id", "value_changed")]


def test_ignore_field_by_full_path():
    rules = Config(ignore_fields=("$.meta.*",))
    old = {"meta": {"a": 1, "b": 2}, "c": 1}
    new = {"meta": {"a": 9, "b": 9}, "c": 2}
    assert kinds(diff_json(old, new, rules)) == [("$.c", "value_changed")]


def test_ignore_uuid_values():
    rules = Config(ignore_values=("uuid",))
    old = {"trace": "0dbf5e58-ae10-4c86-8fdf-27540b4cb3f9"}
    new = {"trace": "9a1c3f2e-1b7d-4e55-a0c4-3d2e1f0b9c88"}
    assert diff_json(old, new, rules) == []


def test_ignore_iso_timestamp_values():
    rules = Config(ignore_values=("iso_timestamp",))
    for old, new in [
        ("2026-10-25T10:00:00Z", "2026-10-25T10:00:01.123456+00:00"),
        ("2026-10-25 10:00:00", "2026-10-26T11:30:00.5"),
        ("2026-10-25", "2026-10-26"),
    ]:
        assert diff_json({"t": old}, {"t": new}, rules) == [], (old, new)


def test_value_pattern_only_ignores_when_both_sides_match():
    rules = Config(ignore_values=("uuid", "iso_timestamp"))
    [d] = diff_json({"t": "2026-10-25T10:00:00Z"}, {"t": "not a timestamp"}, rules)
    assert d.kind == "value_changed"
    [d] = diff_json({"u": "0dbf5e58-ae10-4c86-8fdf-27540b4cb3f9"}, {"u": 5}, rules)
    assert d.kind == "type_changed"


def test_value_patterns_do_not_hide_other_changes():
    rules = Config(ignore_values=("uuid",))
    [d] = diff_json({"name": "alice"}, {"name": "bob"}, rules)
    assert d.kind == "value_changed"


# --- unordered lists ---------------------------------------------------------


def test_reordered_list_by_key_has_no_diffs():
    rules = Config(unordered_lists={"$.items": "id"})
    old = {"items": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}]}
    new = {"items": [{"id": 3, "v": "c"}, {"id": 1, "v": "a"}, {"id": 2, "v": "b"}]}
    assert diff_json(old, new, rules) == []


def test_reordered_list_without_rule_is_reported():
    old = {"items": [{"id": 1}, {"id": 2}]}
    new = {"items": [{"id": 2}, {"id": 1}]}
    assert len(diff_json(old, new, NO_RULES)) == 2


def test_unordered_list_reports_changes_by_key():
    rules = Config(unordered_lists={"$.items": "id"})
    old = {"items": [{"id": 1, "price": 5}, {"id": 2, "price": 6}]}
    new = {"items": [{"id": 3, "price": 7}, {"id": 1, "price": "5"}]}
    assert kinds(diff_json(old, new, rules)) == [
        ("$.items[id=1].price", "type_changed"),
        ("$.items[id=2]", "field_removed"),
        ("$.items[id=3]", "field_added"),
    ]


def test_unordered_list_key_is_used_even_if_field_is_ignored():
    rules = Config(ignore_fields=("id",), unordered_lists={"$.items": "id"})
    old = {"items": [{"id": 1, "v": 1}, {"id": 2, "v": 2}]}
    new = {"items": [{"id": 2, "v": 2}, {"id": 1, "v": 1}]}
    assert diff_json(old, new, rules) == []


def test_unordered_list_path_with_wildcard_index():
    rules = Config(unordered_lists={"$.orders[*].lines": "sku"})
    old = {"orders": [{"lines": [{"sku": "a"}, {"sku": "b"}]}]}
    new = {"orders": [{"lines": [{"sku": "b"}, {"sku": "a"}]}]}
    assert diff_json(old, new, rules) == []


def test_unordered_list_without_key():
    rules = Config(unordered_lists={"$.tags": None})
    assert diff_json({"tags": ["a", "b", "c"]}, {"tags": ["c", "a", "b"]}, rules) == []
    diffs = diff_json({"tags": ["a", "b"]}, {"tags": ["b", "z"]}, rules)
    assert kinds(diffs) == [("$.tags[0]", "field_removed"), ("$.tags[1]", "field_added")]


def test_unordered_list_falls_back_to_ordered_when_key_missing():
    rules = Config(unordered_lists={"$.items": "id"})
    old = {"items": [{"name": "x"}, {"name": "y"}]}
    new = {"items": [{"name": "x"}, {"name": "z"}]}
    assert kinds(diff_json(old, new, rules)) == [("$.items[1].name", "value_changed")]


# --- full response comparison ------------------------------------------------


def test_compare_status_changed_is_breaking_and_short_circuits():
    [d] = compare(200, b'{"a": 1}', 500, b'{"error": "boom"}', NO_RULES)
    assert (d.path, d.kind, d.old, d.new) == ("$", "status_changed", 200, 500)
    assert d.breaking


def test_compare_json_bodies():
    diffs = compare(200, b'{"a": 1}', 200, b'{"a": "1"}', NO_RULES)
    assert kinds(diffs) == [("$.a", "type_changed")]


def test_compare_text_bodies():
    assert compare(200, b"hello", 200, b"hello", NO_RULES) == []
    [d] = compare(200, b"hello", 200, b"world", NO_RULES)
    assert (d.path, d.kind, d.old, d.new) == ("$", "value_changed", "hello", "world")


def test_compare_json_vs_text_is_type_change():
    [d] = compare(200, b'{"a": 1}', 200, b"<html>oops</html>", NO_RULES)
    assert (d.kind, d.old_type, d.new_type) == ("type_changed", "object", "text")


def test_compare_empty_bodies():
    assert compare(204, b"", 204, b"", NO_RULES) == []


def test_difference_serializes():
    [d] = diff_json({"p": 1}, {"p": "1"}, NO_RULES)
    assert d.to_dict() == {
        "path": "$.p",
        "kind": "type_changed",
        "old": 1,
        "new": "1",
        "old_type": "number",
        "new_type": "string",
        "breaking": True,
    }


# --- realistic noise -----------------------------------------------------------


def test_demo_noise_is_fully_filtered_by_example_config():
    rules = load_config("shadowdiff.yaml")
    old = {
        "items": [{"id": 1, "price": 5.0, "updated_at": "2026-10-01T12:00:00Z"}, {"id": 2}],
        "request_id": "0dbf5e58-ae10-4c86-8fdf-27540b4cb3f9",
        "served_at": "2026-10-25T10:00:00.123+00:00",
    }
    new = {
        "items": [{"id": 2}, {"id": 1, "price": 5.0, "updated_at": "2026-10-01T12:00:00Z"}],
        "request_id": "9a1c3f2e-1b7d-4e55-a0c4-3d2e1f0b9c88",
        "served_at": "2026-10-25T10:00:07.999+00:00",
    }
    assert compare(200, json.dumps(old).encode(), 200, json.dumps(new).encode(), rules) == []


# --- config loading ------------------------------------------------------------


def test_load_config_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml") == Config()


def test_load_config_rejects_unknown_value_pattern(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("ignore_values: [uuid, emails]\n")
    with pytest.raises(ConfigError, match="emails"):
        load_config(p)


def test_load_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("ignore_feilds: [id]\n")
    with pytest.raises(ConfigError, match="ignore_feilds"):
        load_config(p)
