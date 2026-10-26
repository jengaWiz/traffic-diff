from typer.testing import CliRunner

from shadowdiff import __version__
from shadowdiff.cli import app


def test_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
