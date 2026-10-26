"""Command-line interface."""

import typer

from shadowdiff import __version__
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
