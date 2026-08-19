"""Test support: run the hook the way the harness runs it.

The hook is exercised as a SUBPROCESS rather than by importing `main` and calling
it, and that is not incidental. Its central guarantee is that every path after
recognition reaches `print()` and exits 0 -- a hook that exits non-zero is
reported by Claude Code as a non-blocking error and the command then runs, which
is the exact failure this hook exists to prevent. Called in-process, a raise
would surface as a test error; called as a subprocess, it surfaces as the
fail-open it actually is. So the subject under test is the installed console
script, byte for byte what settings.json invokes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Protocol

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_BIN = PROJECT_ROOT / ".venv" / "bin"


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment the hook is launched with.

    The console script's shebang is an absolute path into the project venv, so
    unlike a `#!/usr/bin/env` script the interpreter is fixed and PATH cannot
    redirect it. PATH still matters for what the hook itself shells out to --
    `git` -- so the ambient environment is passed through rather than trimmed.
    """
    return {**os.environ, **(extra or {})}


def run_hook_process(
    argv: list[str],
    command: str,
    payload_cwd: Path | str | None = None,
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a hook process against `command` and hand back what it produced.

    Four call sites start one: the console script, the module entry, the scope
    enumeration, and a build with an internal deliberately broken. They differ
    in argv and in what they assert, and in nothing else -- so the payload shape
    and the capture live here, and the result comes back unexamined.

    `payload_cwd` is omitted from the payload when None rather than sent empty:
    a payload carrying no working directory is one of the shapes the hook has to
    answer for, so the harness has to be able to send it.
    """
    payload: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    if payload_cwd is not None:
        payload["cwd"] = str(payload_cwd)
    return subprocess.run(
        argv,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=child_env(extra_env),
        cwd=cwd,
    )


class HookRunner(Protocol):
    def __call__(
        self,
        hook: str,
        command: str,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
        *,
        payload_cwd: Path | str | None = None,
    ) -> str | None: ...


def _run_hook(
    hook: str,
    command: str,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
    *,
    payload_cwd: Path | str | None = None,
) -> str | None:
    """Run the hook against a Bash command; return its deny reason, or None if allowed.

    `hook` names the console script under test, resolved in the project venv so
    the suite tests this checkout rather than whatever version happens to be
    installed globally.

    `cwd` sets the subprocess's directory; `payload_cwd` sets the `cwd` field of
    the JSON payload. The harness supplies both and they normally agree, but a
    hook that resolves paths from the payload has to keep working when they do
    not, so they are controlled separately here. `payload_cwd` is keyword-only:
    an existing caller passes `cwd` positionally, and both are path-typed, so a
    positional insertion would silently rebind it with nothing to catch the swap.
    """
    script = VENV_BIN / hook
    if not script.exists():
        pytest.fail(
            f"{script} is missing -- run `uv sync` so the console script under "
            f"test exists in this checkout's venv."
        )
    proc = run_hook_process(
        [str(script)], command, payload_cwd, cwd=cwd, extra_env=extra_env
    )
    assert proc.returncode == 0, f"{hook} exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    if not out:
        return None
    payload = json.loads(out)
    hook_output = payload["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny"
    reason = hook_output["permissionDecisionReason"]
    assert isinstance(reason, str) and reason
    return reason


@pytest.fixture(scope="session")
def deny_reason() -> HookRunner:
    """(hook, command) -> the hook's deny reason, or None if it allowed the command."""
    return _run_hook


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


def init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", ".")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    return path


def commit_all(repo: Path, message: str = "c1") -> None:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)


def dirty(repo: Path, name: str = "a.txt", text: str = "DIRTY\n") -> Path:
    (repo / name).write_text(text)
    return repo / name


def repo_holding_work(path: Path, text: str = "PRECIOUS\n") -> Path:
    """A repository with an uncommitted change in it, built at `path`.

    The construct three test modules need: a tree that has something to lose, so
    that a refusal has a subject and an allow is a measurable failure.
    """
    r = init(path)
    (r / "a.txt").write_text("v1\n")
    commit_all(r)
    dirty(r, text=text)
    return r
