"""The distribution has to carry the hook, and nothing checks that by itself.

`[tool.hatch.build.targets.wheel] packages` names a directory. When that
directory is absent the build does not fail -- it succeeds and emits a wheel
holding only metadata. Installing that wheel also succeeds, and it still creates
the `block-git-discard` console script, because the entry point is declared in
metadata rather than discovered from code. The script then raises
`ModuleNotFoundError` on every invocation and exits non-zero.

For this project that chain ends somewhere worse than a broken tool. A
PreToolUse hook that exits non-zero is reported by the harness as a non-blocking
error and the command runs anyway, so an install that looks clean at every step
produces a guard that is silently not guarding -- the exact outcome the hook
exists to prevent, arrived at through its own packaging.

Nothing about that outcome announces itself: a tree carrying this
`pyproject.toml` without the package it declares installs, resolves its console
script, and guards nothing.
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
    """`block-git-discard = "block_git_discard:main"` reaches `main` through
    `__init__`, which imports it from `hook`, which imports `tokenize` and
    `is_separator` from `shell_tokens`. `python -m block_git_discard` is the
    second door and reaches `main` through `__main__`. A wheel missing any of
    them resolves its entry point and then fails at import time -- a non-zero
    exit, which the harness reports as a non-blocking error before running the
    command.

    Every module either door needs is listed rather than the first links of one,
    because a module left off this list is checked by nothing: the wheel is built
    from `[tool.hatch.build.targets.wheel]`, and a packaging change that drops a
    module produces exactly the failure above with every other test still
    green."""
    assert module in zipfile.ZipFile(wheel).namelist()


def test_the_module_entry_denies_a_discarding_command(tmp_path: Path) -> None:
    """`python -m block_git_discard` is the door reached when the console script
    is not on path, and it enters through `__main__` rather than through the
    entry point every other test here exercises. Nothing else runs that module,
    so a `__main__` that stops calling `main` prints nothing, exits 0, and lets
    the command run -- the fail-open shape read as an allow."""
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
