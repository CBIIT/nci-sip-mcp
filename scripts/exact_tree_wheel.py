"""Build a wheel from the exact working tree, committed or not, without touching its index."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def _git(root: Path, *arguments: str, env: dict[str, str] | None = None) -> bytes:
    return subprocess.run(  # noqa: S603 - fixed git subcommands on a project path
        ["git", "-C", str(root), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        env=env,
    ).stdout


class WheelBuildError(subprocess.CalledProcessError):
    """A failed offline wheel build whose message carries pip's own error output."""

    def __str__(self) -> str:
        return f"{super().__str__()}\n{self.stderr.decode(errors='replace').strip()}"


def exact_tree_wheel(root: Path, dist_dir: Path, subdir: str = ".") -> Path:
    """Wheel of `root/subdir` built offline from a tree object of the working tree.

    A temporary index takes every non-ignored file, so uncommitted edits and untracked files
    are included and the repository's own index and history are untouched. The archive has no
    git metadata, so a version derived from tags falls back to its configured value. `dist_dir`
    must be empty; a failed pip build raises `WheelBuildError` carrying pip's stderr.
    """
    if dist_dir.exists() and any(dist_dir.iterdir()):
        raise ValueError(f"dist_dir must be empty, but {dist_dir} has content")
    with tempfile.TemporaryDirectory() as scratch:
        index = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(root, "add", "-A", env=index)
        tree = _git(root, "write-tree", env=index).decode().strip()
        source = Path(scratch) / "tree"
        archive = _git(root, "archive", "--format=tar", tree)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter="data")
        built = subprocess.run(  # noqa: S603 - offline wheel build of a fresh archive
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--wheel-dir",
                str(dist_dir),
                str(source / subdir),
            ],
            check=False,
            capture_output=True,
            env={**os.environ, "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1"},
        )
        if built.returncode != 0:
            raise WheelBuildError(built.returncode, built.args, built.stdout, built.stderr)
    (wheel,) = dist_dir.glob("*.whl")
    return wheel
