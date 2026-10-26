"""Grouped regression report for a replay run, rendered as Markdown or HTML."""

import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shadowdiff.diff import normalize_path
from shadowdiff.store import Exchange, Result, Run, Store

_ID_SEGMENT = re.compile(
    r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,})$",
    re.IGNORECASE,
)
_EXAMPLE_CHARS = 80


def endpoint_template(method: str, path: str) -> str:
    """'GET /products/42' -> 'GET /products/{id}'."""
    segments = ["{id}" if _ID_SEGMENT.match(s) else s for s in path.split("/")]
    return f"{method} {'/'.join(segments)}"


@dataclass
class DiffGroup:
    endpoint: str
    path: str
    kind: str
    breaking: bool
    detail: str
    count: int = 0
    example_url: str = ""
    example_old: Any = None
    example_new: Any = None


@dataclass
class EndpointStats:
    requests: int = 0
    passed: int = 0
    failed: int = 0


@dataclass
class Report:
    run: Run
    breaking: list[DiffGroup] = field(default_factory=list)
    warnings: list[DiffGroup] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    endpoints: dict[str, EndpointStats] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 1 if self.run.failed else 0


def build_report(store: Store, run_id: int) -> Report:
    run = store.run(run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    results = store.results(run_id)
    exchanges = store.exchanges_by_id([r.exchange_id for r in results])
    return group_results(run, results, exchanges)


def group_results(run: Run, results: list[Result], exchanges: dict[int, Exchange]) -> Report:
    groups: dict[tuple, DiffGroup] = {}
    errors: dict[str, int] = defaultdict(int)
    endpoints: dict[str, EndpointStats] = defaultdict(EndpointStats)

    for result in results:
        ex = exchanges[result.exchange_id]
        endpoint = endpoint_template(ex.method, ex.path)
        stats = endpoints[endpoint]
        stats.requests += 1
        if result.breaking:
            stats.failed += 1
        else:
            stats.passed += 1
        if result.error:
            errors[result.error] += 1

        # Count each group at most once per request, even if a list repeats the diff.
        seen: set[tuple] = set()
        for d in result.diffs:
            detail = _detail(d)
            key = (endpoint, normalize_path(d["path"]), d["kind"], detail)
            if key in seen:
                continue
            seen.add(key)
            group = groups.get(key)
            if group is None:
                group = groups[key] = DiffGroup(
                    endpoint=endpoint,
                    path=key[1],
                    kind=d["kind"],
                    breaking=d["breaking"],
                    detail=detail,
                    example_url=ex.path + (f"?{ex.query}" if ex.query else ""),
                    example_old=d.get("old"),
                    example_new=d.get("new"),
                )
            group.count += 1

    ordered = sorted(groups.values(), key=lambda g: (-g.count, g.endpoint, g.path))
    return Report(
        run=run,
        breaking=[g for g in ordered if g.breaking],
        warnings=[g for g in ordered if not g.breaking],
        errors=dict(sorted(errors.items(), key=lambda kv: -kv[1])),
        endpoints=dict(sorted(endpoints.items())),
    )


def _detail(d: dict) -> str:
    kind = d["kind"]
    if kind == "type_changed":
        return f"type changed {d['old_type']} → {d['new_type']}"
    if kind == "status_changed":
        return f"status changed {d['old']} → {d['new']}"
    if kind == "field_removed":
        return "field removed"
    if kind == "field_added":
        return "field added"
    return "value changed"


# --- rendering -------------------------------------------------------------------
#
# The report is built once as a list of simple blocks, then rendered to either
# format. Inline `code` spans in text are honoured by both renderers.


def _blocks(report: Report) -> list[tuple]:
    run = report.run
    started = datetime.fromtimestamp(run.started_at or 0, UTC).strftime("%Y-%m-%d %H:%M UTC")
    duration = (run.finished_at or 0) - (run.started_at or 0)
    errors = sum(report.errors.values())

    if report.breaking or errors:
        verdict = (
            f"**FAIL**: {len(report.breaking)} breaking change(s) across {run.failed} "
            f"request(s)" + (f", {errors} request error(s)" if errors else "")
        )
    else:
        verdict = "**PASS**: no breaking changes"

    blocks: list[tuple] = [
        ("h1", f"shadowdiff report: run {run.id}"),
        (
            "p",
            f"Session `{run.session}` replayed against `{run.target}` "
            f"at {started} (concurrency {run.concurrency}).",
        ),
        ("p", verdict),
        (
            "table",
            ["Replayed", "Passed", "Failed", "Breaking changes", "Warnings", "Duration"],
            [
                [
                    run.done,
                    run.passed,
                    run.failed,
                    len(report.breaking),
                    len(report.warnings),
                    f"{duration:.2f}s",
                ]
            ],
        ),
    ]

    for title, groups in [("Breaking changes", report.breaking), ("Warnings", report.warnings)]:
        blocks.append(("h2", title))
        if not groups:
            blocks.append(("p", "None."))
            continue
        blocks.append(
            (
                "table",
                ["Endpoint", "Path", "Change", "Requests", "Example"],
                [
                    [
                        f"`{g.endpoint}`",
                        f"`{g.path}`",
                        g.detail,
                        g.count,
                        f"`{g.example_url}`: {_example(g)}",
                    ]
                    for g in groups
                ],
            )
        )

    if report.errors:
        blocks.append(("h2", "Request errors"))
        blocks.append(
            ("table", ["Error", "Requests"], [[f"`{e}`", n] for e, n in report.errors.items()])
        )

    blocks.append(("h2", "By endpoint"))
    blocks.append(
        (
            "table",
            ["Endpoint", "Requests", "Passed", "Failed"],
            [[f"`{name}`", s.requests, s.passed, s.failed] for name, s in report.endpoints.items()],
        )
    )
    return blocks


def _example(g: DiffGroup) -> str:
    if g.kind == "field_added":
        return f"added `{_short(g.example_new)}`"
    if g.kind == "field_removed":
        return f"was `{_short(g.example_old)}`"
    return f"`{_short(g.example_old)}` → `{_short(g.example_new)}`"


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    text = text if len(text) <= _EXAMPLE_CHARS else text[: _EXAMPLE_CHARS - 1] + "…"
    # Keep table cells intact in Markdown.
    return text.replace("|", "\\|").replace("`", "'")


def render_markdown(report: Report) -> str:
    out = []
    for block in _blocks(report):
        kind = block[0]
        if kind == "h1":
            out.append(f"# {block[1]}")
        elif kind == "h2":
            out.append(f"## {block[1]}")
        elif kind == "p":
            out.append(block[1])
        elif kind == "table":
            headers, rows = block[1], block[2]
            lines = ["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
            lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
            out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


_CODE = re.compile(r"`([^`]*)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")

_HTML_STYLE = """
body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 1100px;
       padding: 0 1rem; color: #1f2328; background: #fff; }
table { border-collapse: collapse; margin: 0.5rem 0 1.5rem; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
code { background: #f6f8fa; padding: 1px 4px; border-radius: 4px; font-size: 13px; }
@media (prefers-color-scheme: dark) {
  body { color: #e6edf3; background: #0d1117; }
  th, code { background: #161b22; }
  th, td { border-color: #30363d; }
}
"""


def _inline_html(text: Any) -> str:
    text = html.escape(str(text).replace("\\|", "|"), quote=False)
    text = _CODE.sub(r"<code>\1</code>", text)
    return _BOLD.sub(r"<strong>\1</strong>", text)


def render_html(report: Report) -> str:
    body = []
    for block in _blocks(report):
        kind = block[0]
        if kind in ("h1", "h2"):
            body.append(f"<{kind}>{_inline_html(block[1])}</{kind}>")
        elif kind == "p":
            body.append(f"<p>{_inline_html(block[1])}</p>")
        elif kind == "table":
            head = "".join(f"<th>{_inline_html(h)}</th>" for h in block[1])
            rows = "".join(
                "<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in row) + "</tr>"
                for row in block[2]
            )
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>shadowdiff run {report.run.id}</title><style>{_HTML_STYLE}</style></head>"
        f"<body>{''.join(body)}</body></html>\n"
    )


def render(report: Report, fmt: str = "md") -> str:
    if fmt == "md":
        return render_markdown(report)
    if fmt == "html":
        return render_html(report)
    raise ValueError(f"unknown report format {fmt!r}; use md or html")
