"""Guards against a class of bug that only shows up outside the test runner.

pytest puts the repo root on sys.path, so `import targetapp` succeeds under test even
when the installed distribution does not actually ship it. `cua serve-app` has no such
help - uvicorn imports "targetapp.app:app" by name from the installed environment.

These tests therefore run in a subprocess with the working directory set somewhere
else, which is the only way to reproduce what a grader's shell actually does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _run_from_elsewhere(code: str) -> subprocess.CompletedProcess[str]:
    """Execute `code` with cwd outside the repo, so only installed packages resolve."""
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_targetapp_is_importable_from_the_installed_distribution() -> None:
    result = _run_from_elsewhere(
        "import targetapp.app as m; print(m.app.title)",
    )
    assert result.returncode == 0, (
        f"`targetapp` is not installed, so `cua serve-app` will fail with "
        f"ModuleNotFoundError.\nstderr:\n{result.stderr}"
    )
    assert "CoreLink Servicing Console" in result.stdout


def test_targetapp_templates_ship_with_the_package() -> None:
    """Jinja templates are resolved relative to __file__; they must be packaged too."""
    result = _run_from_elsewhere(
        "from pathlib import Path; import targetapp;"
        "d = Path(targetapp.__file__).parent / 'templates';"
        "names = sorted(p.name for p in d.glob('*.html'));"
        "print(len(names)); print(','.join(names))"
    )
    assert result.returncode == 0, result.stderr
    count, names = result.stdout.strip().splitlines()
    assert int(count) >= 13, f"expected the full template set, found {count}: {names}"
    for required in ("frameset.html", "search.html", "results.html", "detail.html"):
        assert required in names


def test_cua_package_is_importable_from_the_installed_distribution() -> None:
    result = _run_from_elsewhere("import cua.cli as m; print(type(m.app).__name__)")
    assert result.returncode == 0, result.stderr
    assert "Typer" in result.stdout


def test_the_default_policy_ships_with_the_package() -> None:
    """Guardrails are data, so they are a packaging concern too. A wheel that omits
    policy.default.yaml would start with no allowlist at all."""
    result = _run_from_elsewhere(
        "from cua.policy.engine import load_policy;"
        "p = load_policy();"
        "print(len(p.allowed_origins)); print(len(p.irreversible_signals))"
    )
    assert result.returncode == 0, result.stderr
    origins, signals = (int(x) for x in result.stdout.split())
    assert origins > 0, "shipped policy has an empty origin allowlist"
    assert signals > 0, "shipped policy has no irreversible-action signals"


def test_the_cli_reads_dotenv(tmp_path: Path) -> None:
    """Copying .env.example to .env has to actually do something.

    It did not: the example file existed and the README told people to copy it, but
    nothing ever loaded it - so a correctly-filled key produced a "missing credentials"
    error with no clue why. A config file that looks like it works and does not is worse
    than no config file.
    """
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test-from-dotenv\n")
    result = subprocess.run(
        [sys.executable, "-c", "import cua.cli, os; print(os.environ.get('OPENAI_API_KEY'))"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"},
    )
    assert result.returncode == 0, result.stderr
    assert "sk-test-from-dotenv" in result.stdout


def test_a_real_environment_variable_beats_the_dotenv_file(tmp_path: Path) -> None:
    """The precedence a deployment expects: the file is a convenience for local work,
    not something that can override what the platform injected."""
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n")
    result = subprocess.run(
        [sys.executable, "-c", "import cua.cli, os; print(os.environ.get('OPENAI_API_KEY'))"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "OPENAI_API_KEY": "sk-from-real-env"},
    )
    assert result.returncode == 0, result.stderr
    assert "sk-from-real-env" in result.stdout
