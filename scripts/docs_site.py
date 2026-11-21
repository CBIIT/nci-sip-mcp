"""Build public documentation from reviewed sources, independently of operational evidence."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from scripts.docs_links import rewrite_page
from scripts.docs_stories import stage_story_pages
from scripts.formats import RELEASE_TAG, SHA1_HEX
from scripts.site_assets import FONT_FILES

MAX_PAGE_BYTES = 2_097_152


def _page_path(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or path.suffix != ".md":
        raise ValueError("Documentation sources must be relative Markdown paths")
    return _unlinked(root, path)


def _unlinked(root: Path, path: Path) -> Path:
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Documentation sources and assets cannot traverse symlinks")
    return current


def _page(root: Path, name: str) -> bytes:
    with _page_path(root, name).open("rb") as source:
        raw = source.read(MAX_PAGE_BYTES + 1)
    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError("Documentation page exceeds the staging limit")
    raw.decode("utf-8")
    return raw


def stage_pages(root: Path, destination: Path, pages: list[str]) -> None:
    """Preflight every source before creating a new staging directory; copy no other files."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Documentation staging must use a new directory")
    if not pages or len(set(pages)) != len(pages):
        raise ValueError("Documentation needs a unique nonempty page allowlist")
    content = [(name, _page(root, name)) for name in pages]
    destination.mkdir()
    for name, raw in content:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def build_identity(
    source_commit: str,
    package_version: str,
    *,
    dirty: bool,
    release_tag: str | None = None,
    tag_commit: str | None = None,
) -> dict[str, Any]:
    """Keep installed metadata separate from a release label verified against source."""
    if re.fullmatch(SHA1_HEX, source_commit) is None:
        raise ValueError("Documentation needs its full source commit")
    if re.fullmatch(r"\d+\.\d+\.\d+[a-zA-Z0-9.+-]*", package_version) is None:
        raise ValueError("Invalid installed package version")
    if type(dirty) is not bool:
        raise ValueError("Dirty source state must be explicitly recorded")
    _release_label(source_commit, dirty, release_tag, tag_commit)
    return {
        "schema": 1,
        "source_commit": source_commit,
        "package_version": package_version,
        "dirty": dirty,
        "channel": "release" if release_tag else "development",
        "release_tag": release_tag,
    }


def _release_label(source: str, dirty: bool, tag: str | None, target: str | None) -> None:
    if tag is None:
        if target is not None:
            raise ValueError("A tag commit requires a release tag")
        return
    if re.fullmatch(RELEASE_TAG, tag) is None:
        raise ValueError("Invalid release tag")
    if dirty or target != source:
        raise ValueError("Release documentation requires clean source at the verified tag")


def navigation_pages(nav: list[dict[str, Any]]) -> list[str]:
    """The reviewed navigation is also the complete public page allowlist."""
    pages: list[str] = []
    for entry in nav:
        for value in entry.values():
            pages.extend([value] if isinstance(value, str) else navigation_pages(value))
    return pages


def _git(root: Path, *arguments: str) -> str:
    # Arguments come only from the fixed source-identity commands below; tags are validated.
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("Git is required to identify documentation source")
    return subprocess.check_output(  # noqa: S603
        [executable, *arguments], cwd=root, text=True
    ).strip()


def source_identity(root: Path, tag: str | None) -> dict[str, Any]:
    if tag is not None and re.fullmatch(RELEASE_TAG, tag) is None:
        raise ValueError("Invalid release tag")
    target = _git(root, "rev-parse", "--verify", f"{tag}^{{commit}}") if tag else None
    return build_identity(
        _git(root, "rev-parse", "HEAD"),
        version("nci-sip-mcp"),
        dirty=bool(_git(root, "status", "--porcelain")),
        release_tag=tag,
        tag_commit=target,
    )


def _assets(root: Path, workspace: Path) -> None:
    source = root / "docs/site-assets"
    assets = workspace / "content/assets"
    assets.mkdir()
    files = {
        "ncids.css": source / "ncids.css",
        "site.css": source / "site.css",
        "search-placement.js": source / "search-placement.js",
        "mermaid.min.js": source / "node_modules/.cache/nci-si/mermaid.min.js",
        "mermaid.min.js.LEGAL.txt": source / "node_modules/.cache/nci-si/mermaid.min.js.LEGAL.txt",
        "mermaid-LICENSE.txt": source / "node_modules/mermaid/LICENSE",
    }
    for name in FONT_FILES:
        files[name] = source / "fonts" / name
    if not files["mermaid.min.js"].is_file():
        # `npm ci` clears node_modules/.cache, where the build step puts the runtime.
        raise FileNotFoundError(
            "The Mermaid runtime is not built (npm ci clears it): "
            "run `npm run build --prefix docs/site-assets` and retry."
        )
    for name, path in files.items():
        _unlinked(root, path.relative_to(root))
        shutil.copyfile(path, assets / name)
    (workspace / "overrides").mkdir()
    template = _unlinked(root, Path("docs/site-assets/main.html"))
    shutil.copyfile(template, workspace / "overrides/main.html")


def _prepare(root: Path, workspace: Path, identity: dict[str, Any]) -> None:
    config = _unlinked(root, Path("docs/site-assets/zensical.toml")).read_text()
    pages = navigation_pages(tomllib.loads(config)["project"]["nav"])
    stage_pages(root, workspace / "content", pages)
    if "docs/behavioural-tests.md" in pages:
        stage_story_pages(workspace / "content")
    _assets(root, workspace)
    label = identity["release_tag"] or "development"
    label += " · " + identity["source_commit"][:12]
    if identity["dirty"]:
        label += " · modified source"
    config += "\n[project.extra.build]\nlabel = " + json.dumps(label) + "\n"
    (workspace / "zensical.toml").write_text(config)


def build_site(root: Path, output: Path, *, release_tag: str | None = None) -> Path:
    """Build in fresh isolated staging and publish only after every page link validates."""
    if output.exists() or output.is_symlink():
        raise FileExistsError("Choose a new documentation output directory")
    identity = source_identity(root, release_tag)
    scratch = root / "tmp"
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="docs-build-", dir=scratch) as temporary:
        workspace = Path(temporary)
        _prepare(root, workspace, identity)
        subprocess.run(  # noqa: S603 - fixed installed generator and owned configuration
            [
                sys.executable,
                "-m",
                "zensical",
                "build",
                "--strict",
                "--clean",
                "-f",
                str(workspace / "zensical.toml"),
            ],
            check=True,
            cwd=root,
        )
        site = workspace / "site"
        # The generator's global 404 uses root-absolute assets. A host supplies its
        # own error page; this relocatable artifact contains only reviewed routes.
        (site / "404.html").unlink()
        rewritten = {
            path: rewrite_page(path.read_text(), path, site, root, identity["source_commit"])
            for path in site.rglob("*.html")
        }
        for path, content in rewritten.items():
            path.write_text(content)
        (site / "build.json").write_text(json.dumps(identity, indent=2) + "\n")
        shutil.copytree(site, output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/docs-site"))
    parser.add_argument("--release-tag")
    args = parser.parse_args()
    print(
        build_site(
            Path(__file__).resolve().parents[1], args.output.resolve(), release_tag=args.release_tag
        )
    )


if __name__ == "__main__":
    main()
