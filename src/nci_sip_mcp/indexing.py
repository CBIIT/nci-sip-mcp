"""Operator full-build reconciliation before the first index write."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any, TextIO

from ncit_index.index import LocalIndex
from ncit_index.records import IndexManifest

from .audit import emit
from .context import Context
from .evs import EVSResponseError, normalize_concept
from .index_adapter import index_event
from .release import ReleaseContext
from .validation import NCIT_CODE_RE

logger = logging.getLogger(__name__)


def open_index(data_dir: Path) -> LocalIndex:
    """Open local operator storage separately from the serving read port."""
    return LocalIndex(data_dir, on_event=index_event)


def full_build(context: Context, release: ReleaseContext) -> IndexManifest:
    """Spool verified pages, then embed into an inactive snapshot.

    Temporary storage bounds memory without changing the database before all pages
    reconcile. A shifted search page is an error, never a silently incomplete build.
    The operator operation has no tool-call request budget.
    """
    with TemporaryFile(mode="w+t", encoding="utf-8") as spool:
        _download(context, release, spool)
        spool.seek(0)
        return open_index(context.settings.data_dir).build(
            (normalize_concept(json.loads(line), release.date, "active_cache") for line in spool),
            release.date,
            context.embedding_provider,
            expected_release_version=release.version,
            build_kind="production",
        )


def _download(context: Context, release: ReleaseContext, spool: TextIO) -> None:
    total, rows = context.evs.get_index_page(release, 0)
    seen: set[str] = set()
    pages = 0
    while True:
        _page(rows, seen, total, spool)
        pages += 1
        if pages % 10 == 0 or len(seen) == total:
            emit(
                logger,
                logging.INFO,
                "index_download_progress",
                pages=pages,
                concepts=len(seen),
                total=total,
                release=release.version,
            )
        if len(seen) == total:
            return
        reported, rows = context.evs.get_index_page(release, len(seen))
        if reported != total:
            raise EVSResponseError("EVS index page total changed during the build; retry the build")


def _page(rows: list[dict[str, Any]], seen: set[str], total: int, spool: TextIO) -> None:
    if len(rows) != min(1000, total - len(seen)):
        raise EVSResponseError(
            "EVS index page was incomplete or exceeded its total; retry the build"
        )
    for raw in rows:
        code = _page_code(raw)
        if code in seen:
            raise EVSResponseError(
                "EVS index pages contain a duplicate code and omit content; retry the build"
            )
        seen.add(code)
        spool.write(json.dumps(raw) + "\n")


def _page_code(raw: dict[str, Any]) -> str:
    code = raw.get("code")
    name = raw.get("name")
    if not isinstance(code, str) or not NCIT_CODE_RE.fullmatch(code):
        raise EVSResponseError("EVS index page contains a missing or invalid NCIt code")
    if not isinstance(name, str) or not name.strip():
        raise EVSResponseError("EVS index page contains a missing preferred name")
    return code
