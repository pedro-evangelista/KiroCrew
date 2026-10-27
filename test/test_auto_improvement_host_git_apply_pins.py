"""The host-side ``git apply`` calls run no program the clone's own config names.

``git apply`` reads the index (spawning ``core.fsmonitor``) and passes file content
through ``filter.<name>.clean``/``smudge`` (measured on git 2.47). These two call
sites take the patch on stdin, so they bypass ``_git`` and carried a bare argv. Each
test plants marker programs in a real clone, proves an unpinned ``git apply`` runs
them, then drives the real call site and asserts the marker never ran.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.apps.builtins.auto_improvement.backend import commit as commit_mod
from kiro_crew.apps.builtins.auto_improvement.spine import driver_commit

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="marker programs are POSIX shell scripts"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
]


def _g(cwd: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    env.update(git_maintenance_off_entries(env))
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, input=stdin, capture_output=True, timeout=30
    )


def _poisoned_clone(tmp_path: Path) -> tuple[Path, Path, str]:
    """``(clone, marker, patch)``: a clone on branch ``work`` with planted programs."""
    marker = tmp_path / "MARKER"
    prog = tmp_path / "planted.sh"
    prog.write_text(
        "#!/bin/sh\n"
        f'echo "$0 $*" >> "{marker}"\n'
        'if [ -f "$1" ]; then cat "$1"; else cat; fi\n'
    )
    prog.chmod(0o755)
    up = tmp_path / "up"
    up.mkdir()
    _g(up, "init", "-q", "-b", "work")
    (up / "a.txt").write_text("one\n")
    (up / ".gitattributes").write_text("* filter=pwn\n")
    _g(up, "add", "-A")
    _g(up, "commit", "-qm", "one")
    clone = tmp_path / "clone"
    _g(tmp_path, "clone", "-q", str(up), str(clone))
    (clone / "a.txt").write_text("two\n")
    patch = _g(clone, "diff", "--no-ext-diff").stdout.decode()
    _g(clone, "checkout", "--", "a.txt")
    assert patch, "fixture produced no patch"
    for key in ("core.fsmonitor", "filter.pwn.clean", "filter.pwn.smudge"):
        _g(clone, "config", key, str(prog))
    # The fixture is live: an unpinned apply runs the planted program.
    _g(clone, "apply", "--check", stdin=patch.encode())
    assert marker.exists(), "fixture is inert: bare `git apply` ran no marker"
    marker.unlink()
    return clone, marker, patch


def test_materialize_queued_diff_apply_runs_no_planted_program(tmp_path, monkeypatch):
    clone, marker, patch = _poisoned_clone(tmp_path)
    monkeypatch.setattr(commit_mod, "resolve_origin_url", lambda _config: "")

    out = commit_mod.materialize_queued_diff(clone=clone, branch="work", config={}, diff_text=patch)
    assert out == {"ok": True, "base": "origin/work"}
    assert (clone / "a.txt").read_text() == "two\n"
    assert not marker.exists(), marker.read_text()


@pytest.mark.parametrize("extra", [[], ["--3way"]], ids=["plain", "3way"])
def test_driver_winner_apply_runs_no_planted_program(tmp_path, extra):
    clone, marker, patch = _poisoned_clone(tmp_path)

    done = subprocess.run(
        driver_commit._apply_argv(clone, extra),
        input=patch,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    assert done.returncode == 0, done.stderr
    assert (clone / "a.txt").read_text() == "two\n"
    assert not marker.exists(), marker.read_text()


def test_stage_winner_uses_the_pinned_apply(tmp_path):
    clone, marker, patch = _poisoned_clone(tmp_path)
    driver = SimpleNamespace(clone=clone, branch="work", log=SimpleNamespace(error=print))

    assert driver_commit._stage_winner(driver, SimpleNamespace(diff=patch)) is True
    assert (clone / "a.txt").read_text() == "two\n"
    assert not marker.exists(), marker.read_text()


def _marker(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "MARKER2"
    prog = tmp_path / "planted2.sh"
    prog.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\ncat >/dev/null\nexit 1\n')
    prog.chmod(0o755)
    return marker, prog


def test_commit_runs_no_repo_gpg_program(tmp_path):
    # With signing switched on in the clone's config, `commit` execs `gpg.program`.
    from kiro_crew.apps.builtins.auto_improvement.spine import git_safety

    clone, marker, _patch = _poisoned_clone(tmp_path)
    gpg_marker, gpg = _marker(tmp_path)
    _g(clone, "config", "commit.gpgSign", "true")
    _g(clone, "config", "gpg.program", str(gpg))
    _g(clone, "commit", "-q", "--allow-empty", "-m", "bare")
    assert gpg_marker.exists(), "fixture is inert: a bare commit ran no gpg.program"
    gpg_marker.unlink()
    marker.unlink(missing_ok=True)  # the bare commit also ran the planted fsmonitor

    done = _g(clone, *git_safety.git_argv(clone, "commit", "-q", "--allow-empty", "-m", "x")[1:])
    assert done.returncode == 0, done.stderr
    assert not gpg_marker.exists(), gpg_marker.read_text()
    assert not marker.exists(), marker.read_text()


def test_three_way_apply_runs_no_repo_merge_driver(tmp_path):
    # `apply --3way` falls back to a blob merge, which runs `merge.<name>.driver`
    # for a path whose attributes say `merge=<name>`.
    clone, marker, patch = _poisoned_clone(tmp_path)
    merge_marker, driver = _marker(tmp_path)
    (clone / ".gitattributes").write_text("* filter=pwn merge=pwn\n")
    _g(clone, "config", "merge.pwn.driver", f"{driver} %O %A %B")
    # Drift the target so the plain apply cannot place the hunk and --3way merges.
    (clone / "a.txt").write_text("drifted\n")
    _g(clone, "-c", "core.fsmonitor=false", "-c", "filter.pwn.clean=cat", "commit", "-qam", "d")
    if marker.exists():
        marker.unlink()

    done = subprocess.run(
        driver_commit._apply_argv(clone, ["--3way"]),
        input=patch,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # A conflict is the expected outcome; what matters is which program decided it.
    assert done.returncode in (0, 1), done.stderr
    assert not merge_marker.exists(), merge_marker.read_text()
    assert not marker.exists(), marker.read_text()


def test_nested_repo_driver_is_refused(tmp_path):
    # `add -A`/`status` start a child git in an initialized nested repo, which runs a
    # filter its OWN config declares; the clone's attributes pin does not reach it.
    from kiro_crew.apps.builtins.auto_improvement.spine import git_safety

    clone, marker, _patch = _poisoned_clone(tmp_path)
    _nested_marker, prog = _marker(tmp_path)
    nested = clone / "n"
    nested.mkdir()
    _g(nested, "init", "-q")
    (nested / "x.txt").write_text("x\n")
    (nested / ".gitattributes").write_text("* filter=evil\n")
    _g(nested, "add", "-A")
    _g(nested, "commit", "-qm", "n")
    _g(clone, "-c", "core.fsmonitor=false", "-c", "filter.pwn.clean=cat", "add", "n")
    _g(nested, "config", "filter.evil.clean", str(prog))

    with pytest.raises(git_safety.GitSafetyError, match="filter.evil.clean"):
        git_safety.git_argv(clone, "add", "-A")


def test_nested_repo_driver_is_refused_without_a_trusted_git(tmp_path, monkeypatch):
    # The app runs bare `git`; the probe runs that same binary, so a host whose git
    # is outside the trusted system dirs is still checked rather than waved through.
    from kiro_crew import platform_compat
    from kiro_crew.apps.builtins.auto_improvement.spine import git_safety

    clone, _marker_path, _patch = _poisoned_clone(tmp_path)
    _nested_marker, prog = _marker(tmp_path)
    nested = clone / "n"
    nested.mkdir()
    _g(nested, "init", "-q")
    (nested / "x.txt").write_text("x\n")
    (nested / ".gitattributes").write_text("* filter=evil\n")
    _g(nested, "add", "-A")
    _g(nested, "commit", "-qm", "n")
    _g(clone, "-c", "core.fsmonitor=false", "-c", "filter.pwn.clean=cat", "add", "n")
    _g(nested, "config", "filter.evil.clean", str(prog))
    monkeypatch.setattr(platform_compat, "trusted_git_bin", lambda: None)

    with pytest.raises(git_safety.GitSafetyError, match="filter.evil.clean"):
        git_safety.git_argv(clone, "add", "-A")
