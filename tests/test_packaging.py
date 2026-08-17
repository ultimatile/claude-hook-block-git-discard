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

It was reachable: the repository's default branch carried this `pyproject.toml`
before it carried the package.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    ["block_git_discard/__init__.py", "block_git_discard/hook.py"],
)
def test_the_wheel_carries_what_the_entry_point_needs(wheel: Path, module: str) -> None:
    """`block-git-discard = "block_git_discard:main"` reaches `main` through
    `__init__`, which imports it from `hook`. A wheel missing either resolves the
    console script and then fails at import time, which is a non-zero exit."""
    assert module in zipfile.ZipFile(wheel).namelist()


def test_the_console_script_is_declared(wheel: Path) -> None:
    """The entry point is metadata, so it survives the package being absent --
    which is why its presence alone proves nothing and the tests above exist."""
    with zipfile.ZipFile(wheel) as zf:
        entry = next(n for n in zf.namelist() if n.endswith("entry_points.txt"))
        assert "block-git-discard" in zf.read(entry).decode()
