"""Command-line interface."""

from enum import StrEnum
from pathlib import Path

import typer

from shadowdiff import __version__
from shadowdiff.config import ConfigError, load_config
from shadowdiff.report import build_report, render
from shadowdiff.store import Store

app = typer.Typer(
    help="Record API traffic, replay it, and report meaningful diffs.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """shadowdiff: record → replay → smart diff → report."""


@app.command()
def version() -> None:
    """Print the shadowdiff version."""
    typer.echo(__version__)


DbOption = typer.Option("shadowdiff.db", "--db", help="SQLite database file.")


@app.command()
def record(
    target: str = typer.Option(..., help="Upstream base URL to forward to."),
    session: str = typer.Option("default", help="Session name to group recordings under."),
    host: str = typer.Option("127.0.0.1", help="Interface to listen on."),
    port: int = typer.Option(8080, help="Port to listen on."),
    db: str = DbOption,
) -> None:
    """Run a recording proxy in front of TARGET."""
    import uvicorn

    from shadowdiff.proxy import create_app

    store = Store(db)
    typer.echo(f"Recording session '{session}': http://{host}:{port} -> {target} (db: {db})")
    uvicorn.run(
        create_app(target, store, session),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    store.close()


class Format(StrEnum):
    md = "md"
    html = "html"


def _write_report(store: Store, run_id: int, fmt: Format, output: Path | None) -> int:
    report = build_report(store, run_id)
    output = output or Path(f"report.{fmt.value}")
    output.write_text(render(report, fmt.value))
    verdict = "FAIL" if report.exit_code else "PASS"
    typer.echo(
        f"{verdict}: {len(report.breaking)} breaking change(s), "
        f"{len(report.warnings)} warning(s), {sum(report.errors.values())} error(s). "
        f"Report: {output}"
    )
    for g in report.breaking:
        typer.echo(f"  BREAKING {g.endpoint} {g.path}: {g.detail} on {g.count} request(s)")
    return report.exit_code


@app.command()
def replay(
    session: str = typer.Option(..., help="Recorded session to replay."),
    target: str = typer.Option(..., help="Base URL of the version under test."),
    concurrency: int = typer.Option(20, min=1, help="Maximum requests in flight."),
    timeout: float = typer.Option(10.0, min=0.1, help="Per-request timeout in seconds."),
    config: Path = typer.Option(Path("shadowdiff.yaml"), help="Ignore-rules file."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Report file path."),
    fmt: Format = typer.Option(Format.md, "--format", help="Report format."),
    db: str = DbOption,
) -> None:
    """Replay SESSION against TARGET, diff responses and write a report.

    Exits 1 if any breaking difference or request error is found.
    """
    from shadowdiff.replay import run_replay

    try:
        rules = load_config(config)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    store = Store(db)
    try:
        summary = run_replay(store, session, target, rules, concurrency, timeout)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(
        f"Run {summary.run_id}: replayed {summary.total} request(s) in "
        f"{summary.replay_seconds:.2f}s ({summary.requests_per_second:.0f} req/s), "
        f"{summary.passed} passed, {summary.failed} failed"
    )
    code = _write_report(store, summary.run_id, fmt, output)
    store.close()
    raise typer.Exit(code)


@app.command()
def report(
    run: int | None = typer.Option(None, "--run", help="Run ID (default: latest run)."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Report file path."),
    fmt: Format = typer.Option(Format.md, "--format", help="Report format."),
    db: str = DbOption,
) -> None:
    """Write the report for a replay run. Exits 1 if it has breaking diffs."""
    store = Store(db)
    run_id = run if run is not None else store.latest_run_id()
    if run_id is None or store.run(run_id) is None:
        typer.echo("error: no such run" if run is not None else "error: no runs yet", err=True)
        raise typer.Exit(2)
    code = _write_report(store, run_id, fmt, output)
    store.close()
    raise typer.Exit(code)
