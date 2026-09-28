"""orgs/guard.sh trusts nothing the environment chooses: the alias must be a plain name, the
check runs with the sandbox image's own Python in isolated mode, and only its confirmation line
(not its exit status) lets a setup script continue. Nothing here runs sf or Docker."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forcebench import REPO_ROOT, org

GUARD = REPO_ROOT / "orgs" / "guard.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not installed")


def _bash(
    script: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, "-c", f'set -euo pipefail; . "{GUARD}"; {script}'],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("alias", ["-h", "--help", "-o", "x;touch PWNED", "a b", "../x", "é"])
def test_an_alias_that_is_not_a_plain_name_is_refused_first(alias, tmp_path):
    done = _bash('fb_guard "$FB_ORG"; echo PASSED', {"FB_ORG": alias}, cwd=tmp_path)
    assert done.returncode == 1 and "PASSED" not in done.stdout
    assert "is not a valid org alias" in done.stderr
    assert not (tmp_path / "PWNED").exists()


def test_a_profile_that_is_not_a_plain_name_is_refused():
    done = _bash('fb_guard fb-grader-1 "../base"; echo PASSED')
    assert done.returncode == 1 and "is not a valid org profile" in done.stderr


def test_the_alias_pattern_is_the_python_one():
    shell = re.search(r"\^\[A-Za-z0-9\]\[A-Za-z0-9_\.-\]\*\$", GUARD.read_text())
    assert shell, "guard.sh checks aliases with ^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    assert org._ALIAS_RE.pattern == "[A-Za-z0-9][A-Za-z0-9_.-]*"


@pytest.mark.parametrize(
    ("out", "alias", "profile", "ok"),
    [
        ("FORCEBENCH_ORG_LOCK_OK fb-grader-1", "fb-grader-1", "", True),
        ("FORCEBENCH_ORG_LOCK_OK fb-grader-1 base", "fb-grader-1", "base", True),
        ("", "fb-grader-1", "", False),  # PYTHON=/bin/true: exit 0, nothing printed
        ("usage: python -m forcebench.org [-h] {check} ...", "fb-grader-1", "", False),  # -h
        ("FORCEBENCH_ORG_LOCK_OK other", "fb-grader-1", "", False),
        ("FORCEBENCH_ORG_LOCK_OK fb-grader-1", "fb-grader-1", "base", False),
        ("FORCEBENCH_ORG_LOCK_OK fb-grader-1 base", "fb-grader-1", "", False),
        ("x\nFORCEBENCH_ORG_LOCK_OK fb-grader-1", "fb-grader-1", "", False),
        ("FORCEBENCH_ORG_LOCK_OK fb-grader-1\nx", "fb-grader-1", "", False),
        ("FORCEBENCH_ORG_LOCK_OK *", "fb-grader-1", "", False),
    ],
)
def test_only_the_exact_confirmation_line_passes(out, alias, profile, ok):
    done = _bash(
        'fb_guard_confirmed "$OUT" "$ALIAS" "$PROFILE" && echo YES || echo NO',
        {"OUT": out, "ALIAS": alias, "PROFILE": profile},
    )
    assert done.stdout.strip() == ("YES" if ok else "NO"), done.stderr


def test_the_environment_cannot_choose_the_interpreter():
    text = GUARD.read_text()
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(re.search(r"\$\{?PYTHON\b|python3", ln) for ln in code)
    assert re.search(r'"\$FB_GUARD_PYTHON" -I -c "\$FB_GUARD_CODE"', text)
    done = _bash(
        'printf "%s" "$FB_GUARD_PYTHON"',
        {"FB_GUARD_PYTHON": "/bin/true", "PYTHON": "/bin/true", "FB_PYTHON": "/bin/true"},
    )
    assert done.stdout == "/opt/venv/bin/python"
    # the image's venv (UV_PROJECT_ENVIRONMENT), which the offline container runs too
    assert "UV_PROJECT_ENVIRONMENT=/opt/venv" in (REPO_ROOT / "docker" / "Dockerfile").read_text()
    assert "/opt/venv/bin/python -m forcebench" in (REPO_ROOT / "Makefile").read_text()


def _check(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """The check exactly as guard.sh runs it, with this interpreter standing in for the image's."""
    code = _bash('printf "%s" "$FB_GUARD_CODE"').stdout
    return subprocess.run(
        [sys.executable, "-I", "-c", code, str(REPO_ROOT / "src"), *argv],
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_an_exit_status_of_zero_is_not_a_confirmation():
    """The review's case: `check -h` exits 0 (argparse help) without checking anything."""
    done = _check("check", "-h")
    assert done.returncode == 0 and "usage" in done.stdout
    assert "FORCEBENCH_ORG_LOCK_OK" not in done.stdout


def test_the_check_refuses_here_and_prints_no_confirmation():
    if org.in_sandbox():
        pytest.skip("running inside the sandbox image")
    for argv in (["check", "--", "fb-grader-1"], ["check", "--", "-h"]):
        done = _check(*argv, env={"FORCEBENCH_SANDBOX": "1"})
        assert done.returncode == 1 and done.stdout == ""
        assert "refusing to touch" in done.stderr


def test_isolated_mode_ignores_code_put_on_the_python_path(tmp_path):
    """-I: a sitecustomize on PYTHONPATH (or any PYTHON* variable) cannot print the line."""
    (tmp_path / "sitecustomize.py").write_text(
        "print('FORCEBENCH_ORG_LOCK_OK fb-grader-1'); raise SystemExit(0)\n"
    )
    done = _check("check", "--", "fb-grader-1", env={"PYTHONPATH": str(tmp_path)})
    assert "FORCEBENCH_ORG_LOCK_OK" not in done.stdout
    # without -I it would have been
    tricked = subprocess.run(
        [sys.executable, "-c", "pass"],
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "FORCEBENCH_ORG_LOCK_OK" in tricked.stdout


def test_base_setup_runs_seed_with_the_guards_interpreter():
    base = (REPO_ROOT / "orgs" / "base" / "setup.sh").read_text()
    assert "PYTHON" not in base.replace("FB_PYTHON", "")
    assert base.count('"$PY" -I data/seed.py') == 3 and "PY=$FB_PYTHON" in base


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_org_scripts_are_shellcheck_clean():
    scripts = [str(GUARD), *map(str, sorted((REPO_ROOT / "orgs").glob("*/setup.sh")))]
    done = subprocess.run(
        ["shellcheck", "-x", "-s", "bash", *scripts], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout
