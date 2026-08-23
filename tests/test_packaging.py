"""The distribution has to carry the hook, and nothing checks that by itself.

`[tool.hatch.build.targets.wheel] packages` names a directory. Absent, the build
still succeeds and emits a wheel of pure metadata, which still installs and still
creates the console script — the entry point being metadata rather than
discovered from code. Every invocation then raises `ModuleNotFoundError` and exits
non-zero, which the harness reports as a non-blocking error before running the
command.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, VENV_BIN, repo_holding_work, run_hook_process


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel this checkout actually builds."""
    out = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"uv build failed:\n{proc.stdout}\n{proc.stderr}")
    built = list(out.glob("*.whl"))
    assert len(built) == 1, built
    return built[0]


def test_the_wheel_carries_the_package(wheel: Path) -> None:
    """A wheel of pure metadata is the failure this file exists for."""
    names = zipfile.ZipFile(wheel).namelist()
    modules = [n for n in names if n.startswith("block_git_discard/")]
    assert modules, (
        "the wheel holds no package -- every entry is metadata:\n  "
        + "\n  ".join(names)
    )


@pytest.mark.parametrize(
    "module",
    [
        "block_git_discard/__init__.py",
        "block_git_discard/hook.py",
        "block_git_discard/shell_tokens.py",
        "block_git_discard/__main__.py",
    ],
)
def test_the_wheel_carries_what_the_entry_points_need(wheel: Path, module: str) -> None:
    """Both doors reach `main` through modules the wheel has to carry: the console
    script through `__init__` and `hook`, `python -m` through `__main__`.

    Every module either needs is listed rather than the first links of one, a module
    left off being checked by nothing.
    """
    assert module in zipfile.ZipFile(wheel).namelist()


def test_the_module_entry_denies_a_discarding_command(tmp_path: Path) -> None:
    """`python -m block_git_discard` enters through `__main__`, which nothing else
    here runs: one that stops calling `main` prints nothing and exits 0.
    """
    repo = repo_holding_work(tmp_path / "r")
    proc = run_hook_process(
        [str(VENV_BIN / "python"), "-m", "block_git_discard"],
        "git reset --hard",
        repo,
        cwd=repo,
    )
    assert proc.returncode == 0, proc.stderr
    decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny", proc.stdout


def test_the_console_script_is_declared(wheel: Path) -> None:
    """The entry point is metadata, so it survives the package being absent --
    which is why its presence alone proves nothing and the tests above exist."""
    with zipfile.ZipFile(wheel) as zf:
        entry = next(n for n in zf.namelist() if n.endswith("entry_points.txt"))
        assert "block-git-discard" in zf.read(entry).decode()
