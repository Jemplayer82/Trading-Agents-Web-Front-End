"""Regression tests for the tier/* blocking pre-commit hook."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "git-hooks" / "pre-commit"
BASH = shutil.which("bash")
GIT = shutil.which("git")


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """Create a fresh git repository for the hook to inspect."""
    if not BASH or not GIT:
        pytest.skip("bash and git executables must be available on PATH")
    if not HOOK_PATH.exists():
        pytest.skip(f"pre-commit hook not found at {HOOK_PATH}")

    repo_dir = tmp_path / "scratch-repo"
    repo_dir.mkdir()
    subprocess.run(
        [GIT, "init"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return repo_dir


def _run_hook(repo_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the real pre-commit hook script inside the scratch repository."""
    return subprocess.run(
        [BASH, HOOK_PATH.as_posix()],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_pre_commit_hook_allows_non_tier_branch(scratch_repo: Path) -> None:
    """Normal feature branches should be allowed to commit."""
    subprocess.run(
        [GIT, "checkout", "-b", "feature/normal-dev"],
        cwd=scratch_repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result = _run_hook(scratch_repo)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("branch", ["tier/1", "tier/2"])
def test_pre_commit_hook_blocks_tier_branches(scratch_repo: Path, branch: str) -> None:
    """tier/* branches are generated artifacts and must be blocked."""
    subprocess.run(
        [GIT, "checkout", "-b", branch],
        cwd=scratch_repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    result = _run_hook(scratch_repo)
    assert result.returncode == 1, result.stderr
    assert "ERROR:" in result.stderr
    assert "tier/*" in result.stderr
    assert "generated" in result.stderr.lower()
