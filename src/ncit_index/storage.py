"""Snapshot schema and explicit migration of the legacy concatenated index."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module
from itertools import groupby
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import IndexCompatibilityError, IndexStorageError
from .records import Concept as NcitConcept
from .records import IndexManifest

SCHEMA_VERSION = 6
FIELD_KINDS = ("name", "synonym", "definition")


def vector_bytes(vector: Sequence[float]) -> bytes:
    """Round all providers consistently to the stored little-endian float32 format."""
    if hasattr(vector, "tobytes"):  # a NumPy row from a model provider
        return _array_bytes(vector)
    if not all(math.isfinite(value) for value in vector):
        raise IndexCompatibilityError("An embedding contains non-finite values")
    try:
        return struct.pack(f"<{len(vector)}f", *vector)
    except OverflowError, struct.error:
        raise IndexCompatibilityError("An embedding value cannot be stored as float32") from None


def _array_bytes(vector: Any) -> bytes:
    np = import_module("numpy")  # an array exists only where NumPy is installed
    if not np.isfinite(vector).all():
        raise IndexCompatibilityError("An embedding contains non-finite values")
    with np.errstate(over="ignore"):
        rounded = vector.astype("<f4")
    if not np.isfinite(rounded).all():
        raise IndexCompatibilityError("An embedding value cannot be stored as float32")
    return rounded.tobytes()


def name_key(text: str) -> str:
    """Exact names ignore case and collapsed whitespace after Unicode NFC."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def concept_fields(concept: NcitConcept) -> list[tuple[str, str]]:
    """Embed identical text once; precedence is name, synonym, then definition."""
    candidates = [("name", concept.preferred_name)]
    candidates.extend(("synonym", item["name"]) for item in concept.evidence.get("synonyms", []))
    candidates.extend(
        ("definition", item["definition"]) for item in concept.evidence.get("definitions", [])
    )
    by_text: dict[str, str] = {}
    for kind, text in candidates:
        if text.strip():
            by_text.setdefault(text, kind)
    return [(kind, text) for text, kind in by_text.items()]


def fts_table(build_id: str) -> str:
    """Only internal hexadecimal ids can form SQL identifiers, never caller input."""
    if not re.fullmatch(r"[0-9a-f]{32}", build_id):
        raise ValueError("Invalid stored index build identifier")
    return f"fts_{build_id}"


def create_fts(conn: sqlite3.Connection, build_id: str) -> None:
    # Per-build FTS keeps an inactive snapshot from changing active BM25 statistics.
    conn.execute(
        f"CREATE VIRTUAL TABLE {fts_table(build_id)} USING fts5("
        "code UNINDEXED, kind UNINDEXED, text, tokenize = 'unicode61')"
    )


def create_schema(conn: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE manifests (build_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
        "active INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'complete', "
        "CHECK (active = 0 OR state = 'complete'))",
        "CREATE UNIQUE INDEX manifests_single_active ON manifests(active) WHERE active = 1",
        "CREATE TABLE concepts (build_id TEXT NOT NULL, code TEXT NOT NULL, "
        "payload TEXT NOT NULL, name_key TEXT NOT NULL, status TEXT, PRIMARY KEY (build_id, code))",
        "CREATE INDEX concepts_name ON concepts(build_id, name_key)",
        "CREATE INDEX concepts_status ON concepts(build_id, status, code)",
        "CREATE TABLE fields (id INTEGER PRIMARY KEY, build_id TEXT NOT NULL, "
        "code TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, position INTEGER NOT NULL)",
        "CREATE INDEX fields_concept ON fields(build_id, code)",
        "CREATE TABLE concept_vectors (build_id TEXT NOT NULL, code TEXT NOT NULL, "
        "kinds BLOB NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (build_id, code))",
        "CREATE INDEX concept_vectors_scan ON concept_vectors(build_id)",
    )
    for statement in statements:
        conn.execute(statement)


def holds_index(conn: sqlite3.Connection) -> bool:
    """Whether the file already held an index of any schema, as opposed to being new and empty."""
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'manifests'").fetchone() is not None
    )


def migrate(conn: sqlite3.Connection) -> None:
    """Retain every legacy raw concept and manifest, without relabelling its vectors."""
    if not holds_index(conn):
        create_schema(conn)
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(manifests)")}
    if "build_id" in columns:
        _migrate_field_vectors(conn)
        return
    _migrate_legacy(conn)


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE manifests RENAME TO legacy_manifests")
    conn.execute("ALTER TABLE concepts RENAME TO legacy_concepts")
    for name in ("manifests_single_active", "vector_lsh_lookup"):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    for name in ("concepts_fts", "vector_lsh"):
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    create_schema(conn)
    rows = conn.execute("SELECT * FROM legacy_manifests ORDER BY release_version").fetchall()
    selected = _legacy_active(rows)
    for row in rows:
        _migrate_build(conn, row, selected)
    old_count = conn.execute("SELECT COUNT(*) FROM legacy_concepts").fetchone()[0]
    new_count = conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0]
    if old_count != new_count:
        raise IndexCompatibilityError(
            "Legacy index has concepts without a manifest; repair before migration"
        )
    conn.execute("DROP TABLE legacy_concepts")
    conn.execute("DROP TABLE legacy_manifests")


def _migrate_field_vectors(conn: sqlite3.Connection) -> None:
    """Preserve schema-5 field ids/FTS, snapshots and activation while changing vector encoding."""
    conn.execute("ALTER TABLE fields RENAME TO json_fields")
    conn.execute("DROP INDEX fields_concept")
    conn.execute(
        "CREATE TABLE fields (id INTEGER PRIMARY KEY, build_id TEXT NOT NULL, "
        "code TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, position INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE concept_vectors (build_id TEXT NOT NULL, code TEXT NOT NULL, "
        "kinds BLOB NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (build_id, code))"
    )
    conn.execute("CREATE INDEX concept_vectors_scan ON concept_vectors(build_id)")
    rows = conn.execute("SELECT * FROM json_fields ORDER BY build_id, code, id")
    for (build, code), fields in groupby(rows, lambda row: (row["build_id"], row["code"])):
        _migrate_concept_vectors(conn, build, code, list(fields))
    conn.execute("DROP TABLE json_fields")
    conn.execute("DROP TABLE vector_lsh")
    conn.execute("CREATE INDEX fields_concept ON fields(build_id, code)")
    conn.execute("ALTER TABLE concepts ADD COLUMN status TEXT")
    conn.execute("UPDATE concepts SET status = json_extract(payload, '$.raw.conceptStatus')")
    conn.execute("CREATE INDEX concepts_status ON concepts(build_id, status, code)")


def _migrate_concept_vectors(
    conn: sqlite3.Connection,
    build: str,
    code: str,
    rows: list[sqlite3.Row],
) -> None:
    vectors = []
    kinds = []
    for position, row in enumerate(rows):
        conn.execute("INSERT INTO fields VALUES (?, ?, ?, ?, ?, ?)", (*tuple(row)[:5], position))
        vectors.append(vector_bytes(json.loads(row["vector"])))
        kinds.append(FIELD_KINDS.index(row["kind"]))
    conn.execute(
        "INSERT INTO concept_vectors VALUES (?, ?, ?, ?)",
        (build, code, bytes(kinds), b"".join(vectors)),
    )


def _legacy_active(rows: list[sqlite3.Row]) -> str | None:
    active = [row["release_version"] for row in rows if row["active"]]
    return active[-1] if active else None


def _migrate_build(conn: sqlite3.Connection, row: sqlite3.Row, active: str | None) -> None:
    manifest = replace(
        IndexManifest.from_payload(json.loads(row["payload"])),
        build_id=uuid4().hex,
        needs_rebuild=True,
        active=row["release_version"] == active,
    )
    if manifest.embedding_dimensions is None:
        vector = conn.execute(
            "SELECT vector FROM legacy_concepts WHERE release_version = ? LIMIT 1",
            (row["release_version"],),
        ).fetchone()
        if vector:
            manifest = replace(manifest, embedding_dimensions=len(json.loads(vector[0])))
    conn.execute(
        "INSERT INTO manifests(build_id, payload, active) VALUES (?, ?, ?)",
        (manifest.build_id, json.dumps(manifest.to_dict()), int(manifest.active)),
    )
    for item in conn.execute(
        "SELECT code, payload FROM legacy_concepts WHERE release_version = ?",
        (row["release_version"],),
    ):
        payload = json.loads(item["payload"])
        conn.execute(
            "INSERT INTO concepts VALUES (?, ?, ?, ?, ?)",
            (
                manifest.build_id,
                item["code"],
                item["payload"],
                name_key(payload["preferred_name"]),
                payload.get("raw", {}).get("conceptStatus"),
            ),
        )


def delete_build(conn: sqlite3.Connection, build_id: str) -> None:
    for table in ("concepts", "fields", "concept_vectors", "manifests"):
        conn.execute(f"DELETE FROM {table} WHERE build_id = ?", (build_id,))  # noqa: S608
    conn.execute(f"DROP TABLE IF EXISTS {fts_table(build_id)}")


@contextmanager
def build_lease(directory: Path, build_id: str) -> Iterator[None]:
    """A separate SQLite lock distinguishes a running writer from a stale build.

    No lock on the index is held while embedding. SQLite releases this small
    sidecar lock even after process death, on every supported operating system.
    """
    fts_table(build_id)
    path = directory / f"build-{build_id}.sqlite3"
    lock = _open_lease(path)
    try:
        yield
    finally:
        lock.close()
        path.unlink(missing_ok=True)


def _open_lease(path: Path) -> sqlite3.Connection:
    lock = None
    try:
        lock = sqlite3.connect(str(path), timeout=0)
        lock.execute("BEGIN EXCLUSIVE")
    except sqlite3.Error as exc:
        if lock is not None:
            lock.close()
        if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
            raise
        raise IndexStorageError(f"{exc} ({path})") from exc
    return lock


def clean_stale_builds(conn: sqlite3.Connection, directory: Path) -> None:
    """Called under the index write lock; a live builder keeps its private lease."""
    rows = conn.execute("SELECT build_id FROM manifests WHERE state = 'building'").fetchall()
    for row in rows:
        try:
            with build_lease(directory, row[0]):
                delete_build(conn, row[0])
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
