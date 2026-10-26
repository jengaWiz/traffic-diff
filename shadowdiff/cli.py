"""Command-line interface."""

import typer

from shadowdiff import __version__

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
