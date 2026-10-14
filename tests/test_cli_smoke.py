"""Smoke tests for the command line entry point.

Only ever runs `--help`, which argparse handles before any network access, so
these tests never contact a target. The tool's authorization prompt and its
refusal to run unconfirmed are left untouched.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "stress_my_site.py"


@pytest.mark.parametrize("subcommand", [[], ["break"], ["requests"], ["takedown"]])
def test_help_exits_zero(subcommand: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *subcommand, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage:")
