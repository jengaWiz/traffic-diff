"""Record against demo v1 and replay against v1/v2, all in-process."""

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from demo.app import create_app
from shadowdiff.cli import app as cli
from shadowdiff.config import load_config
from shadowdiff.proxy import create_app as create_proxy
from shadowdiff.replay import run_replay
from shadowdiff.report import build_report, endpoint_template, render

PATHS = (
    ["/health", "/products", "/products?category=books&limit=5", "/products/9999"]
    + [f"/products/{i}" for i in range(1, 21)]
    + [f"/orders/{i}" for i in range(1, 11)]
)


@pytest.fixture
def recorded(tmp_path):
    from shadowdiff.store import Store

    db = str(tmp_path / "e2e.db")
    store = Store(db)
    proxy = create_proxy(
        "http://v1", store, "demo", transport=httpx.ASGITransport(app=create_app("v1"))
    )
    with TestClient(proxy) as client:
        for path in PATHS:
            client.get(path)
    assert len(store.exchanges("demo")) == len(PATHS)
    return store, db


def _replay(store, version):
    transport = httpx.ASGITransport(app=create_app(version))
    return run_replay(
        store, "demo", f"http://{version}", load_config("shadowdiff.yaml"), transport=transport
    )


def test_v1_vs_v2_flags_exactly_the_planted_bugs(recorded):
    store, _ = recorded
    summary = _replay(store, "v2")
    report = build_report(store, summary.run_id)

    breaking = {(g.endpoint, g.path, g.detail, g.count) for g in report.breaking}
    assert breaking == {
        ("GET /products/{id}", "$.price", "type changed number → string", 20),
        ("GET /products/{id}", "$.in_stock", "field removed", 20),
    }
    assert [(g.path, g.kind) for g in report.warnings] == [("$.available", "field_added")]
    assert summary.failed == 20
    assert report.exit_code == 1
    md = render(report, "md")
    assert "**FAIL**" in md and "`$.price`" in md
    assert "<code>$.price</code>" in render(report, "html")


def test_v1_vs_v1_reports_nothing(recorded):
    store, _ = recorded
    summary = _replay(store, "v1")
    report = build_report(store, summary.run_id)
    assert report.breaking == [] and report.warnings == [] and report.errors == {}
    assert summary.passed == len(PATHS)
    assert report.exit_code == 0


def test_unreachable_target_counts_as_failure(recorded):
    store, _ = recorded

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    summary = run_replay(
        store, "demo", "http://down", load_config(None), transport=httpx.MockTransport(refuse)
    )
    report = build_report(store, summary.run_id)
    assert summary.failed == len(PATHS)
    assert list(report.errors.values()) == [len(PATHS)]
    assert report.exit_code == 1


def test_report_cli_exit_codes(recorded, tmp_path):
    store, db = recorded
    bad = _replay(store, "v2").run_id
    good = _replay(store, "v1").run_id
    runner = CliRunner()

    out = tmp_path / "r.md"
    result = runner.invoke(cli, ["report", "--run", str(bad), "--db", db, "-o", str(out)])
    assert result.exit_code == 1, result.output
    assert "BREAKING GET /products/{id} $.price" in result.output
    assert out.read_text().startswith("# shadowdiff report")

    out = tmp_path / "r.html"
    args = ["report", "--run", str(good), "--db", db, "--format", "html", "-o", str(out)]
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert out.read_text().startswith("<!doctype html>")

    # Defaults to the latest run.
    result = runner.invoke(cli, ["report", "--db", db, "-o", str(tmp_path / "x.md")])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["report", "--run", "999", "--db", db])
    assert result.exit_code == 2


def test_replay_cli_unknown_session(tmp_path):
    result = CliRunner().invoke(
        cli,
        ["replay", "--session", "nope", "--target", "http://x", "--db", str(tmp_path / "a.db")],
    )
    assert result.exit_code == 2
    assert "no recorded exchanges" in result.output


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/products/42", "GET /products/{id}"),
        ("GET", "/orders/7/lines/3", "GET /orders/{id}/lines/{id}"),
        ("GET", "/users/0dbf5e58-ae10-4c86-8fdf-27540b4cb3f9", "GET /users/{id}"),
        ("GET", "/products", "GET /products"),
        ("GET", "/v1/health", "GET /v1/health"),
    ],
)
def test_endpoint_template(method, path, expected):
    assert endpoint_template(method, path) == expected
