#!/usr/bin/env -S uv run --no-project --script
# /// script
# requires-python = ">=3.11"
# ///
# PreToolUse hook: bounce a git command that would discard uncommitted work.
#
# `git checkout -- <file>` restores the file from the index, and takes every
# OTHER uncommitted change in that file with it. The revert's granularity is the
# file; the intent's is almost always one hunk. Nothing warns, and the loss reads
# as though the edit never applied. That has cost real work more than once.
#
# This does not pattern-match "dangerous-looking" commands. It runs the same
# read-only query git would and denies only when something is actually at stake,
# so a checkout with nothing to lose is never in the way.
#
# SCOPE IS DELIBERATELY NARROW. The shapes below are the ones an agent actually
# types. Covering every git command that can destroy content -- plumbing such as
# `read-tree -u` and `checkout-index -f`, `--ignore-skip-worktree-bits`,
# `--git-dir`/`--work-tree` overrides, sequencer aborts, `clean.requireForce=false`
# -- means reproducing git's own worktree/index normalization (filters, eol,
# symlinks, gitlinks, sparse entries). That buys nothing here, because agents do
# not type those. Shapes outside the list PASS THROUGH by design, not by
# oversight. Likewise, content git does not report as changed is not protected:
# `assume-unchanged`, `skip-worktree` and lossy clean filters hide it from the
# query this hook asks, and chasing them costs far more than it returns.
#
# The reason is the real payload. It names what is at stake, routes to a
# granularity-matched alternative FIRST, and only then offers the override --
# because the agent, not the human who would see a permission prompt, is the one
# who knows which hunk was its own throwaway edit.
#
# FAIL-CLOSED once a shape is recognized, deliberately against the convention of
# every other hook here. The others let malformed input through because being
# wrong costs a redo; this one guards an irreversible loss. The guarantee has a
# floor worth stating: a hook that exits non-zero is reported as a non-blocking
# error and the command then runs, so every path after recognition must reach
# print() rather than raise. Failures before main() -- import, syntax, an
# unusable interpreter -- cannot be caught from inside this file at all.

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# A script's own directory is normally on sys.path, but not under
# PYTHONSAFEPATH=1, where the sibling import below would raise. The harness
# reports that as a non-blocking error and runs the command anyway, so a silently
# disabled guard is the worst outcome. Put the directory back explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_tokens import is_separator, tokenize  # noqa: E402

# The override token, read from the RAW command string. It cannot be read from
# the token list: shlex treats `#` as a comment and drops the rest of the line,
# so `git checkout -- f # ack:...` tokenizes without the token entirely.
ACK_RE = re.compile(r"#\s*ack:([0-9a-f]{16})\b")

# Subcommands the HOOK itself may run. Checked at the single subprocess entry
# rather than asserted in a comment, so an edit reaching for a mutating query
# fails loudly instead of quietly mutating the repository this hook protects.
READ_ONLY = frozenset({"diff", "ls-files", "rev-parse"})

# Characters the shell expands after this hook has seen the text, so a pathspec
# containing one cannot be forwarded to git as written. Globs are excluded on
# purpose: git's own glob matches at least as much as the shell's, so forwarding
# one over-detects rather than under-detects.
UNEXPANDED = re.compile(r"[{}$`~]")


class Unmeasurable(Exception):
    """Raised when a recognized shape cannot be measured. Always becomes a deny."""


def git(cwd: str, *args: str) -> str:
    if not args or args[0] not in READ_ONLY:
        raise Unmeasurable(f"hook attempted a non-read-only git query: {args!r}")
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise Unmeasurable(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        raise Unmeasurable(f"`git {args[0]}` failed: {proc.stderr.strip()}")
    return proc.stdout


def simple_commands(tokens: list[str]) -> list[tuple[str, list[str]]]:
    """Split a token list into (preceding separator, argv) simple commands."""
    out: list[tuple[str, list[str]]] = []
    sep, cur = "", []
    for tok in tokens:
        if is_separator(tok):
            if cur:
                out.append((sep, cur))
            sep, cur = tok, []
        else:
            cur.append(tok)
    if cur:
        out.append((sep, cur))
    return out


def strip_global_opts(argv: list[str]) -> tuple[list[str], list[str], bool]:
    """Peel git's own options off the front.

    Returns (`-C` dirs, subcommand argv, relocated). `relocated` marks
    `--git-dir` / `--work-tree` / `--namespace`, which move what the subcommand
    acts on, so a measurement taken in the resolved cwd would describe a
    different tree. This function never raises: the caller decides, because
    refusing has to happen after the verb is recognized, not before -- otherwise
    the refusal is indistinguishable from "not a shape we cover" and the command
    would be let through.
    """
    cdirs: list[str] = []
    relocated = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-C", "-c") and i + 1 < len(argv):
            if tok == "-C":
                cdirs.append(argv[i + 1])
            i += 2
        elif tok.startswith(("--git-dir", "--work-tree", "--namespace")):
            relocated = True
            i += 1
        elif tok.startswith("-"):
            i += 1
        else:
            break
    return cdirs, argv[i:], relocated


# Subcommands this hook covers at all. Recognition turns on the verb alone, with
# no repository access, so that everything after it can fail closed.
COVERED = frozenset({"checkout", "restore", "reset", "clean"})


def covered_verb(argv: list[str]) -> str | None:
    """The covered subcommand named here, if any. Never raises, never runs git."""
    _, rest, _ = strip_global_opts(argv)
    return rest[0] if rest and rest[0] in COVERED else None


def split_at_ddash(rest: list[str]) -> tuple[list[str], list[str] | None]:
    """Return (tokens before `--`, tokens after `--` or None when absent)."""
    if "--" in rest:
        idx = rest.index("--")
        return rest[:idx], rest[idx + 1 :]
    return rest, None


def expand_short(flags: list[str]) -> set[str]:
    """Expand bundled short flags: `-SW` -> {'S', 'W'}. Long flags kept whole."""
    out: set[str] = set()
    for tok in flags:
        if tok.startswith("--"):
            out.add(tok)
        elif tok.startswith("-") and len(tok) > 1:
            out.update(tok[1:])
    return out


def is_ref(cwd: str, name: str) -> bool:
    try:
        git(cwd, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    except Unmeasurable:
        return False
    return True


def paths_of(cwd: str, rest: list[str]) -> list[str] | None:
    """Pathspecs a checkout-like invocation targets, or None if it targets no path.

    With `--` present the answer is exactly what follows it. Without, git resolves
    each operand as a ref first and only falls back to a path, so the same order
    is used here; an invocation whose operands are all refs is a branch switch,
    which preserves uncommitted work and is not this hook's business.
    """
    before, after = split_at_ddash(rest)
    if after is not None:
        return after or None
    operands = [t for t in before if not t.startswith("-")]
    return [t for t in operands if not is_ref(cwd, t)] or None


def stake_for(cwd: str, argv: list[str]) -> tuple[str, list[str]] | None:
    """What this invocation would destroy, as (query kind, pathspecs).

    Returns None when the shape is outside the list this hook covers, or when it
    is one of the covered verbs in a form that destroys nothing.
    """
    _, rest, relocated = strip_global_opts(argv)
    if not rest:
        return None
    if relocated:
        raise Unmeasurable("--git-dir/--work-tree relocates the tree to measure")
    verb, args = rest[0], rest[1:]
    flags = expand_short([a for a in args if a.startswith("-") and a != "--"])

    # Interactive forms pick hunks, so they never take collateral.
    if "p" in flags or "--patch" in flags:
        return None
    if "--pathspec-from-file" in flags or any(
        a.startswith("--pathspec-from-file") for a in args
    ):
        raise Unmeasurable("pathspecs come from a file this hook cannot read")

    if verb in ("checkout", "restore"):
        if verb == "restore":
            staged = "S" in flags or "--staged" in flags
            worktree = "W" in flags or "--worktree" in flags
            # `--staged` alone rewrites the index and leaves the file on disk, so
            # the content survives; only a worktree write can take it away.
            if staged and not worktree:
                return None
            paths = [a for a in args if not a.startswith("-")] or None
            _, after = split_at_ddash(args)
            paths = after or paths
        else:
            # Branch creation keeps the working tree; only a path form discards.
            if flags & {"b", "B"} or {"--branch"} & flags:
                return None
            paths = paths_of(cwd, args)
        return ("worktree", paths or []) if paths is not None else None

    if verb == "reset":
        return ("worktree", []) if "--hard" in flags else None

    if verb == "clean":
        if "n" in flags or "--dry-run" in flags:
            return None
        if not (flags & {"f"} or "--force" in flags):
            return None
        if "X" in flags:
            return ("untracked-ignored-only", [])
        if "x" in flags:
            return ("untracked-all", [])
        return ("untracked", [])

    return None


LS_FLAGS = {
    "untracked": ["--others", "--exclude-standard"],
    "untracked-all": ["--others"],
    "untracked-ignored-only": ["--others", "--ignored", "--exclude-standard"],
}


def measure(cwd: str, kind: str, pathspecs: list[str]) -> tuple[list[str], str, str]:
    """Return (affected paths, a display summary, a content fingerprint).

    The fingerprint is what the override token is bound to, and it has to change
    whenever the content at stake changes. A `--stat` summary does not: editing a
    line leaves `a.txt | 2 +-` byte-for-byte identical, which would keep an old
    token valid over different content. The raw patch is used instead -- it also
    covers binaries, whose `index <old>..<new>` line moves with the content even
    though no hunk text is produced.
    """
    if kind == "worktree":
        # A pathspec the shell would still expand cannot be handed to git as
        # written, so narrowing is dropped and the whole tree is measured. That
        # over-detects, which costs one extra round trip; forwarding it as-is
        # would under-detect, which costs the work itself.
        specs = [] if any(UNEXPANDED.search(p) for p in pathspecs) else pathspecs
        tail = ["--", *specs] if specs else []
        names = [ln for ln in git(cwd, "diff", "--name-only", *tail).splitlines() if ln]
        if not names:
            return [], "", ""
        summary = git(cwd, "diff", "--stat", *tail).rstrip("\n")
        return names, summary, git(cwd, "diff", *tail)
    names = [ln for ln in git(cwd, "ls-files", *LS_FLAGS[kind]).splitlines() if ln]
    return names, "\n".join(f" {n}" for n in names), "\n".join(names)


def hunk_headers(cwd: str, paths: list[str]) -> list[str]:
    """`@@` headers for the first few affected files. Binary files yield none."""
    out: list[str] = []
    for path in paths[:3]:
        try:
            diff = git(cwd, "diff", "-U0", "--", path)
        except Unmeasurable:
            continue
        heads = [ln for ln in diff.splitlines() if ln.startswith("@@")][:4]
        if heads:
            out.append(f"  {path}")
            out.extend(f"    {h}" for h in heads)
    return out


def resolve_cwd(payload_cwd: str, before: list[tuple[str, list[str]]]) -> str:
    """Apply `cd` from the simple commands preceding this one.

    Only unconditional sequencing is followed. After `||` the shell may or may not
    have run the `cd`, so replaying it would measure a directory the command never
    reached; that is refused rather than guessed at.
    """
    cwd = Path(payload_cwd)
    for sep, argv in before:
        if sep == "||":
            raise Unmeasurable("a conditional `cd` leaves the directory ambiguous")
        if argv and argv[0] == "cd":
            targets = [a for a in argv[1:] if not a.startswith("-")]
            if not targets:
                raise Unmeasurable("bare `cd` targets the home directory")
            cwd = (
                (cwd / targets[0]).resolve()
                if not Path(targets[0]).is_absolute()
                else Path(targets[0])
            )
    return str(cwd)


def build_reason(
    kind: str,
    names: list[str],
    summary: str,
    hunks: list[str],
    token: str,
) -> str:
    what = (
        "untracked file(s)" if kind.startswith("untracked") else "uncommitted change(s)"
    )
    lines = [
        f"Blocked: this would discard {what} that cannot be recovered afterwards.",
        "",
        f"At stake ({len(names)} file(s)):",
        summary,
    ]
    if hunks:
        lines += ["", "Hunks:", *hunks]
    if kind == "worktree":
        lines += [
            "",
            "If only part of this should go, do NOT discard the whole file:",
            "  git restore -p <path>          choose hunks interactively",
            "  git diff -- <path> > keep.patch    save a copy, then discard",
            "  git apply -R keep.patch        reverse one saved patch",
        ]
    else:
        lines += [
            "",
            "To keep any of these, move them aside before cleaning.",
        ]
    # Only the token is echoed, never the command it came from. Echoing the whole
    # command reads as "run this again", which for a compound line re-runs the
    # earlier parts too -- and any of those that touch the tree change what is at
    # stake, invalidating the very token being offered.
    lines += [
        "",
        "If discarding all of it is intended, append this and re-run:",
        f"  # ack:{token}",
        "",
        "The token is an override, not a confirmation that the list above was read.",
    ]
    return "\n".join(lines)


def emit_deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> None:
    # Before a destructive shape is recognized, anything unexpected is allowed
    # through, matching the other hooks here. After recognition the polarity
    # flips: see the header.
    try:
        data = json.load(sys.stdin)
        command = data.get("tool_input", {}).get("command", "")
        if not command:
            return
        payload_cwd = data.get("cwd") or ""
        commands = simple_commands(tokenize(command))
    except Exception:  # noqa: BLE001
        return

    for idx, (_, argv) in enumerate(commands):
        if not argv or Path(argv[0]).name != "git":
            continue
        # Recognition turns on the verb alone and touches no repository, so that
        # a failure anywhere below is unambiguously "a covered shape we could not
        # measure" rather than "a shape we do not cover", and can deny.
        if covered_verb(argv[1:]) is None:
            continue

        try:
            if not payload_cwd:
                raise Unmeasurable("the payload carried no working directory")
            cwd = resolve_cwd(payload_cwd, commands[:idx])
            cdirs, _, _ = strip_global_opts(argv[1:])
            for d in cdirs:
                cwd = str(Path(cwd) / d)
            if not Path(cwd).is_dir():
                raise Unmeasurable(f"resolved directory does not exist: {cwd}")
            stake = stake_for(cwd, argv[1:])
            if stake is None:
                continue  # a covered verb, in a form that discards nothing
            kind, pathspecs = stake
            names, summary, fingerprint = measure(cwd, kind, pathspecs)
            if not names:
                continue  # nothing at stake; let it run
            payload = "\0".join(
                [
                    git(cwd, "rev-parse", "--absolute-git-dir").strip(),
                    ACK_RE.sub("", command).strip(),
                    fingerprint,
                ]
            )
            token = hashlib.sha256(payload.encode()).hexdigest()[:16]
            supplied = ACK_RE.search(command)
            if supplied and supplied.group(1) == token:
                continue  # override presented, and still describes this content
            hunks = hunk_headers(cwd, names) if kind == "worktree" else []
            emit_deny(build_reason(kind, names, summary, hunks, token))
            return
        except BaseException as exc:  # noqa: BLE001
            emit_deny(
                "Blocked: this command can discard uncommitted work, and the hook "
                f"could not determine what is at stake ({exc}).\n"
                "Denied rather than allowed, because the loss would be "
                "irreversible. Re-run from the repository root, or check the "
                "changes yourself with `git status` before discarding."
            )
            return


if __name__ == "__main__":
    main()
