"""SQLite-backed concept cache and local retrieval index.

Builds are immutable snapshots. Activation retains the previous snapshot for rollback.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from itertools import batched, chain, groupby
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from .embeddings import EmbeddingProvider
from .errors import (
    IndexBuildError,
    IndexCapabilityError,
    IndexCompatibilityError,
    IndexCursorError,
    IndexEvaluationError,
    IndexModelRevisionError,
    IndexReleaseError,
    IndexStateError,
    IndexStorageError,
    NoActiveIndexError,
)
from .records import Concept as NcitConcept
from .records import IndexManifest, SearchHit, utc_now_iso
from .scoring import rank_page
from .storage import (
    FIELD_KINDS,
    SCHEMA_VERSION,
    build_lease,
    clean_stale_builds,
    concept_fields,
    create_fts,
    delete_build,
    fts_table,
    holds_index,
    migrate,
    name_key,
    vector_bytes,
)


def require_index_release(manifest: IndexManifest, requested: str | None) -> None:
    if requested is not None and manifest.release_version != requested:
        raise IndexReleaseError(requested, manifest.release_version)


# SQL built with f-strings below interpolates only table names and predicates
# written in this module, or lists of "?" placeholders; every value is bound.

_SQL_CHUNK = 500


def _distinct_concepts(concepts: Iterable[NcitConcept]) -> list[NcitConcept]:
    """Keep one concept per code, as sample updates have always done."""

    by_code = {item.code: item for item in concepts}
    concepts = list(by_code.values())
    if not concepts:
        raise IndexBuildError("No concepts were provided for indexing")
    return concepts


def _build_release(first: NcitConcept) -> str:
    """The release of a build, named by its first concept; `_prepare_fields` checks the rest."""
    if not first.release_version:
        raise IndexBuildError("Indexed concepts must include a release version")
    return first.release_version


def _embed(embedding_provider: EmbeddingProvider, texts: list[str]) -> Sequence[Sequence[float]]:
    vectors = embedding_provider.embed(texts)
    if len(vectors) != len(texts):
        raise IndexCompatibilityError("Embedding provider returned an unexpected vector count")
    dimensions = len(vectors[0])
    if dimensions < 1 or any(len(vector) != dimensions for vector in vectors):
        raise IndexCompatibilityError("Embedding provider returned inconsistent vector dimensions")
    return vectors


def _query_vector(
    embedding_provider: EmbeddingProvider, manifest: IndexManifest, query: str
) -> Sequence[float]:
    query_vectors = embedding_provider.embed([query])
    if len(query_vectors) != 1:
        raise IndexCompatibilityError(
            "Embedding provider returned an unexpected query vector count"
        )
    query_vector = query_vectors[0]
    if not manifest.embedding_matches(
        embedding_provider.name, embedding_provider.model, len(query_vector)
    ):
        raise IndexCompatibilityError("Query embedding dimensions do not match the active index")
    return query_vector


def _store_fields(
    conn: sqlite3.Connection,
    build_id: str,
    fields: list[tuple[str, str, str]],
    vectors: Sequence[Sequence[float]],
) -> None:
    paired = zip(fields, vectors, strict=True)
    for code, items in groupby(paired, lambda item: item[0][0]):
        kinds, blobs = [], []
        for position, ((_, kind, text), vector) in enumerate(items):
            cursor = conn.execute(
                "INSERT INTO fields(build_id, code, kind, text, position) VALUES (?, ?, ?, ?, ?)",
                (build_id, code, kind, text, position),
            )
            conn.execute(
                f"INSERT INTO {fts_table(build_id)}(rowid, code, kind, text) VALUES (?, ?, ?, ?)",  # noqa: S608
                (cursor.lastrowid, code, kind, text),
            )
            kinds.append(FIELD_KINDS.index(kind))
            blobs.append(vector_bytes(vector))
        conn.execute(
            "INSERT INTO concept_vectors VALUES (?, ?, ?, ?)",
            (build_id, code, bytes(kinds), b"".join(blobs)),
        )


def _prepare_fields(
    concepts: tuple[NcitConcept, ...],
    release: str,
    seen: set[str],
) -> list[tuple[str, str, str]]:
    fields = []
    for concept in concepts:
        if concept.release_version != release:
            raise IndexBuildError("Cannot mix concept release versions in one index build")
        if not concept.code or concept.code in seen:
            raise IndexBuildError("Index build contains a missing or duplicate concept code")
        seen.add(concept.code)
        text_fields = concept_fields(concept)
        if not text_fields:
            raise IndexBuildError(f"Concept {concept.code} has no indexable text")
        fields.extend((concept.code, kind, text) for kind, text in text_fields)
    return fields


def _write_batch(
    conn: sqlite3.Connection,
    build_id: str,
    concepts: tuple[NcitConcept, ...],
    fields: list[tuple[str, str, str]],
    vectors: Sequence[Sequence[float]],
) -> None:
    conn.executemany(
        "INSERT INTO concepts VALUES (?, ?, ?, ?, ?)",
        [
            (
                build_id,
                item.code,
                json.dumps(item.to_stored()),
                name_key(item.preferred_name),
                item.raw.get("conceptStatus"),
            )
            for item in concepts
        ],
    )
    _store_fields(conn, build_id, fields, vectors)


def _check_dimensions(previous: int, width: int, compatible: IndexManifest | None) -> None:
    if previous and width != previous:
        raise IndexCompatibilityError("Embedding dimensions changed during the build")
    if compatible and compatible.embedding_dimensions not in (None, width):
        raise IndexCompatibilityError(
            f"Embedding mismatch; rebuild {compatible.index_path} with index-rebuild"
        )


def _sample_base_is_active(conn: sqlite3.Connection, active: IndexManifest | None) -> bool:
    """Whether the build a sample extended is still the active one."""
    row = conn.execute("SELECT build_id FROM manifests WHERE active = 1").fetchone()
    return (row[0] if row else None) == (active.build_id if active else None)


# SQLite result codes of the database file itself; other codes (SQL usage, constraints) are bugs.
_STORAGE_ERROR_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_NOTADB,
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_READONLY,
    }
)


class LocalIndex:
    def __init__(
        self, data_dir: Path, *, on_event: Callable[[str, dict[str, Any]], None] | None = None
    ) -> None:
        self.on_event = on_event
        # (event, build) pairs already recorded by this process, so a probe loop stays quiet.
        self._noted: set[tuple[str, str]] = set()
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "nci_si.sqlite3"
        self._init_db()

    def _event(self, name: str, **details: Any) -> None:
        if self.on_event is not None:
            self.on_event(name, details)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back and close; database failures retain their file context."""
        try:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.row_factory = sqlite3.Row
                with conn:
                    yield conn
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # The primary result code is the low byte; the rest names the sub-case.
            if (exc.sqlite_errorcode or 0) & 0xFF in _STORAGE_ERROR_CODES:
                raise IndexStorageError(f"{exc} ({self.db_path})") from exc
            raise

    def _init_db(self) -> None:
        migration: tuple[int, float] | None = None
        with self._connect() as conn:
            if self._schema_version(conn) == SCHEMA_VERSION:
                return
            if conn.execute("PRAGMA page_count").fetchone()[0] == 0:
                # Larger pages avoid overflow-page reads for per-concept vector BLOBs.
                conn.execute("PRAGMA page_size = 65536")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            # Another opener may have migrated while this one waited for the lock, so the
            # version this migration starts from is the one read under it.
            version = self._schema_version(conn)
            if version != SCHEMA_VERSION:
                migration = self._announce_migration(conn, version)
                migrate(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Past the commit: a start record without this one means the migration rolled back.
        if migration is not None:
            self._event(
                "index_migration_finished",
                from_version=migration[0],
                to_version=SCHEMA_VERSION,
                elapsed_seconds=monotonic() - migration[1],
            )

    def _announce_migration(
        self, conn: sqlite3.Connection, version: int
    ) -> tuple[int, float] | None:
        """Record the start of a migration of an existing file; a new empty file is only created."""
        if not holds_index(conn):
            return None
        self._event("index_migration_started", from_version=version, to_version=SCHEMA_VERSION)
        return version, monotonic()

    def _schema_version(self, conn: sqlite3.Connection) -> int:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise IndexCompatibilityError(
                f"Index schema {version} at {self.db_path} is newer than supported {SCHEMA_VERSION}"
            )
        return version

    @staticmethod
    def _manifest(row: sqlite3.Row) -> IndexManifest:
        return replace(
            IndexManifest.from_payload(json.loads(row["payload"])), active=bool(row["active"])
        )

    @staticmethod
    def _active_manifest(conn: sqlite3.Connection) -> IndexManifest | None:
        row = conn.execute(
            "SELECT payload, active FROM manifests WHERE active = 1 AND state = 'complete'"
        ).fetchone()
        return LocalIndex._manifest(row) if row else None

    def get_active_manifest(self) -> IndexManifest | None:
        with self._connect() as conn:
            return self._active_manifest(conn)

    def verify_active(self, provider: EmbeddingProvider) -> IndexManifest:
        """Open and validate the active search build without embedding or upstream I/O."""
        with self._connect() as conn:
            return self._searchable_manifest(conn, provider, None, None)

    def list_builds(self) -> list[IndexManifest]:
        """Completed snapshots, including the rollback target and pending activation."""
        with self._connect() as conn:
            return [
                self._manifest(row)
                for row in conn.execute(
                    "SELECT payload, active FROM manifests WHERE state = 'complete'"
                )
            ]

    def evaluation_inputs(
        self, build_id: str, expected_codes: set[str]
    ) -> tuple[IndexManifest, list[str]]:
        """Read the candidate identity and missing judgments from one database snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            manifest = _completed_manifest(conn, build_id)
            present = _concept_payloads(conn, build_id, sorted(expected_codes))
            return manifest, sorted(expected_codes - present.keys())

    def record_evaluation(self, build_id: str, report: dict[str, Any]) -> IndexManifest:
        """Persist an evaluation only while the same immutable candidate still exists."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            manifest = _completed_manifest(conn, build_id)
            identity = evaluation_identity(manifest)
            if any(report.get(key) != value for key, value in identity.items()):
                raise IndexBuildError("Evaluation identity does not match the candidate build")
            updated = replace(
                manifest,
                evaluation_version=report["evaluation_version"],
                evaluation_score=report["evaluation_score"],
                evaluation_report=report,
            )
            conn.execute(
                "UPDATE manifests SET payload = ? WHERE build_id = ?",
                (json.dumps(updated.to_dict()), build_id),
            )
            return updated

    def build(
        self,
        raw_concepts: Iterable[NcitConcept],
        release_date: str | None,
        embedding_provider: EmbeddingProvider,
        expected_release_version: str | None = None,
        *,
        build_kind: Literal["sample", "production"] = "sample",
    ) -> IndexManifest:
        """Embed outside transactions; interrupted builds stay hidden until cleaned."""
        concepts = iter(raw_concepts)
        first = next(concepts, None)
        if first is None:
            raise IndexBuildError("No concepts were provided for indexing")
        release = _build_release(first)
        if expected_release_version is not None and release != expected_release_version:
            raise IndexCompatibilityError(
                "Concept payload release did not match the selected release"
            )
        return self._build_stream(
            chain([first], concepts),
            release_date,
            embedding_provider,
            release,
            build_kind=build_kind,
        )

    def _build_stream(
        self,
        concepts: Iterable[NcitConcept],
        release_date: str | None,
        provider: EmbeddingProvider,
        release: str,
        compatible: IndexManifest | None = None,
        *,
        build_kind: Literal["legacy", "sample", "production"] = "sample",
        unclassified_source_build: str | None = None,
    ) -> IndexManifest:
        build_id = uuid4().hex
        manifest = IndexManifest(
            terminology="ncit",
            release_version=release,
            release_date=release_date,
            embedding_provider=provider.name,
            embedding_model=provider.model,
            concept_count=0,
            built_at=utc_now_iso(),
            index_path=str(self.db_path),
            build_id=build_id,
            build_kind=build_kind,
            unclassified_source_build=unclassified_source_build,
            embedding_revision=provider.revision,
        )
        with build_lease(self.data_dir, build_id):
            self._start_build(manifest)
            count, dimensions = self._write_batches(manifest, concepts, provider, compatible)
            completed = replace(manifest, concept_count=count, embedding_dimensions=dimensions)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE manifests SET state = 'complete', payload = ? WHERE build_id = ?",
                    (json.dumps(completed.to_dict()), build_id),
                )
            return completed

    def _start_build(self, manifest: IndexManifest) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clean_stale_builds(conn, self.data_dir)
            create_fts(conn, manifest.build_id)
            conn.execute(
                "INSERT INTO manifests VALUES (?, ?, 0, 'building')",
                (manifest.build_id, json.dumps(manifest.to_dict())),
            )

    def _write_batches(
        self,
        manifest: IndexManifest,
        concepts: Iterable[NcitConcept],
        provider: EmbeddingProvider,
        compatible: IndexManifest | None,
    ) -> tuple[int, int]:
        seen: set[str] = set()
        dimensions = 0
        for number, batch in enumerate(batched(concepts, 64, strict=False), 1):
            fields = _prepare_fields(batch, manifest.release_version, seen)
            vectors = _embed(provider, [text for _, _, text in fields])
            width = len(vectors[0])
            _check_dimensions(dimensions, width, compatible)
            with self._connect() as conn:
                _write_batch(conn, manifest.build_id, batch, fields, vectors)
            dimensions = width
            if number % 16 == 0:
                self._event(
                    "index_embedding_progress",
                    build_id=manifest.build_id,
                    concepts=len(seen),
                    batches=number,
                )
        return len(seen), dimensions

    def activate(self, build_id: str) -> IndexManifest:
        """Atomically activate a completed snapshot and retain only its predecessor.

        The other completed builds are hidden in the same transaction and removed after it
        commits, so a failed removal can never undo the activation.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            manifest, switched = self._activate(conn, build_id)
        self._announce_activation(manifest, switched)
        self._remove_superseded()
        return manifest

    def pending_removal(self) -> list[str]:
        """Builds hidden by an activation whose removal has not completed."""
        with self._connect() as conn:
            rows = conn.execute("SELECT build_id FROM manifests WHERE state = 'superseded'")
            return sorted(row[0] for row in rows)

    def _remove_superseded(self) -> None:
        """Delete superseded builds one transaction each; stop at the first storage failure.

        Runs after every activation, including one that changed nothing, so a removal that
        was interrupted is retried by the next activation of any build.
        """
        pending = self.pending_removal()
        for position, build_id in enumerate(pending):
            try:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    delete_build(conn, build_id)
            except IndexStorageError:
                self._event("index_build_removal_pending", build_ids=pending[position:])
                return

    def _activate(self, conn: sqlite3.Connection, build_id: str) -> tuple[IndexManifest, bool]:
        row = conn.execute(
            "SELECT payload, active FROM manifests WHERE build_id = ? AND state = 'complete'",
            (build_id,),
        ).fetchone()
        if not row:
            raise IndexStateError(
                "Unknown completed build; list available builds with index-builds"
            )
        manifest = LocalIndex._manifest(row)
        _require_evaluated(manifest)
        if manifest.active:
            return manifest, False
        previous = conn.execute("SELECT build_id FROM manifests WHERE active = 1").fetchone()
        predecessor = previous["build_id"] if previous else build_id
        conn.execute("UPDATE manifests SET active = 0 WHERE active = 1")
        conn.execute("UPDATE manifests SET active = 1 WHERE build_id = ?", (build_id,))
        conn.execute(
            "UPDATE manifests SET state = 'superseded' "
            "WHERE state = 'complete' AND build_id NOT IN (?, ?)",
            (build_id, predecessor),
        )
        return replace(manifest, active=True), True

    def _announce_activation(self, manifest: IndexManifest, switched: bool) -> None:
        """Called after the commit: a record of an activation that was rolled back would lie."""
        if switched:
            self._event(
                "index_activated", build_id=manifest.build_id, release=manifest.release_version
            )

    def rebuild(self, build_id: str, provider: EmbeddingProvider) -> IndexManifest:
        """Offline rebuild from stored payloads; explicit activation is a separate step."""
        with TemporaryFile(mode="w+t", encoding="utf-8") as spool:
            with self._connect() as conn:
                conn.execute("BEGIN")
                row = conn.execute(
                    "SELECT payload, active FROM manifests "
                    "WHERE build_id = ? AND state = 'complete'",
                    (build_id,),
                ).fetchone()
                if not row:
                    raise IndexStateError("Unknown build; list available builds with index-builds")
                old = self._manifest(row)
                for item in conn.execute(
                    "SELECT payload FROM concepts WHERE build_id = ? ORDER BY code", (build_id,)
                ):
                    spool.write(item[0] + "\n")
            spool.seek(0)
            concepts = (NcitConcept(**json.loads(line)) for line in spool)
            return self._build_stream(
                concepts,
                old.release_date,
                provider,
                old.release_version,
                build_kind="sample" if old.build_kind == "sample" else "production",
                unclassified_source_build=(
                    old.build_id if old.build_kind not in {"sample", "production"} else None
                ),
            )

    def upsert_concepts(
        self,
        raw_concepts: Iterable[NcitConcept],
        release_date: str | None,
        embedding_provider: EmbeddingProvider,
        expected_release_version: str | None = None,
    ) -> IndexManifest:
        """Developer sample update: build the combined same-release sample, then activate."""
        concepts = _distinct_concepts(raw_concepts)
        release = _build_release(concepts[0])
        if expected_release_version is not None and release != expected_release_version:
            raise IndexCompatibilityError(
                "Concept payload release did not match the selected release"
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            active = _stored_dimensions(conn, self._active_manifest(conn))
            if active and active.build_kind != "sample":
                raise IndexEvaluationError(
                    f"Sample updates cannot modify {active.build_kind} build {active.build_id}; "
                    "recreate the sample with index-sample in a separate data directory"
                )
            combined = self._sample_concepts(conn, active, concepts, embedding_provider, release)
        compatible = active if active and active.release_version == release else None
        built = self._build_stream(combined, release_date, embedding_provider, release, compatible)
        return self._activate_sample(built.build_id, active)

    def _activate_sample(self, build_id: str, base: IndexManifest | None) -> IndexManifest:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if _sample_base_is_active(conn, base):
                manifest, switched = self._activate(conn, build_id)
            else:
                # Committed before the error is raised: the unreferenced snapshot is not kept.
                delete_build(conn, build_id)
                manifest, switched = None, False
        if manifest is None:
            raise IndexStateError(
                "The active index changed during sample indexing; retry the sample"
            )
        self._announce_activation(manifest, switched)
        self._remove_superseded()
        return manifest

    @staticmethod
    def _sample_concepts(
        conn: sqlite3.Connection,
        active: IndexManifest | None,
        concepts: list[NcitConcept],
        provider: EmbeddingProvider,
        release: str,
    ) -> list[NcitConcept]:
        if active is None or active.release_version != release:
            return concepts
        if active.needs_rebuild:
            raise IndexCapabilityError("Run index-rebuild before updating a legacy sample.")
        if not active.embedding_matches(provider.name, provider.model):
            raise IndexCompatibilityError(
                f"Embedding mismatch; rebuild {active.index_path} with index-rebuild"
            )
        previous = {
            row["code"]: NcitConcept(**json.loads(row["payload"]))
            for row in conn.execute(
                "SELECT code, payload FROM concepts WHERE build_id = ?", (active.build_id,)
            )
        }
        previous.update((concept.code, concept) for concept in concepts)
        return list(previous.values())

    def get_concept_snapshot(self, code: str) -> tuple[IndexManifest | None, NcitConcept | None]:
        """Read the cache and the release used to validate it in one snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            manifest = self._active_manifest(conn)
            row = conn.execute(
                "SELECT concepts.payload FROM concepts JOIN manifests USING (build_id) "
                "WHERE manifests.active = 1 AND concepts.code = ?",
                (code,),
            ).fetchone()
        return manifest, NcitConcept(**json.loads(row[0])) if row else None

    def search(
        self,
        query: str,
        embedding_provider: EmbeddingProvider,
        limit: int = 10,
        mode: str = "hybrid",
    ) -> list[SearchHit]:
        return self.search_page(query, embedding_provider, limit, mode)[0]

    def search_page(
        self,
        query: str,
        embedding_provider: EmbeddingProvider,
        limit: int = 10,
        mode: str = "hybrid",
        *,
        requested_release: str | None = None,
        offset: int = 0,
        build_id: str | None = None,
        retired_status: str | None = None,
    ) -> tuple[list[SearchHit], int, IndexManifest]:
        """Read the active identity, complete ranking and page in one snapshot."""
        with self._connect() as conn:
            conn.execute("PRAGMA temp_store = FILE")
            conn.execute("BEGIN")
            manifest = self._searchable_manifest(
                conn, embedding_provider, build_id, requested_release
            )
            require_index_release(manifest, requested_release)
            hits, total = _ranked_page(
                conn, manifest, embedding_provider, query, limit, mode, offset, retired_status
            )
            return hits, total, manifest

    def search_build(
        self,
        build_id: str,
        query: str,
        provider: EmbeddingProvider,
        limit: int = 10,
        mode: str = "hybrid",
    ) -> tuple[list[SearchHit], IndexManifest]:
        """Evaluate a completed candidate without changing activation or rollback state."""
        with self._connect() as conn:
            conn.execute("PRAGMA temp_store = FILE")
            conn.execute("BEGIN")
            manifest = _completed_manifest(conn, build_id)
            _require_searchable(manifest, provider)
            self._require_revision(manifest, provider)
            hits, _ = _ranked_page(conn, manifest, provider, query, limit, mode)
            return hits, manifest

    def _searchable_manifest(
        self,
        conn: sqlite3.Connection,
        provider: EmbeddingProvider,
        build_id: str | None,
        requested_release: str | None,
    ) -> IndexManifest:
        manifest = self._active_manifest(conn)
        if manifest is None:
            raise NoActiveIndexError("No active NCIt index is available")
        if build_id is not None and build_id != manifest.build_id:
            raise IndexCursorError(
                requested_release or manifest.release_version, manifest.release_version
            )
        _require_searchable(manifest, provider)
        self._require_revision(manifest, provider)
        return manifest

    def _require_revision(self, manifest: IndexManifest, provider: EmbeddingProvider) -> None:
        """Refuse a model revision other than the stored one; note a build that stores none."""
        stored, runtime = manifest.embedding_revision, provider.revision
        if stored is None:
            self._note("index_model_revision_unknown", manifest, runtime_revision=runtime)
        elif stored != runtime:
            self._note(
                "index_model_revision_mismatch",
                manifest,
                stored_revision=stored,
                runtime_revision=runtime,
            )
            if runtime is None:
                raise IndexModelRevisionError(
                    f"Index model revision {stored} cannot be compared: "
                    "the serving model revision cannot be determined"
                )
            raise IndexModelRevisionError(
                f"Index model revision {stored} does not match the serving model revision {runtime}"
            )

    def _note(self, event: str, manifest: IndexManifest, **details: str | None) -> None:
        if (event, manifest.build_id) not in self._noted:
            self._noted.add((event, manifest.build_id))
            self._event(event, build_id=manifest.build_id, **details)


def _require_searchable(manifest: IndexManifest, provider: EmbeddingProvider) -> None:
    if manifest.needs_rebuild:
        raise IndexCapabilityError(
            "Run index-rebuild with this build id, then index-activate, to enable field search.",
        )
    if not manifest.embedding_matches(provider.name, provider.model):
        raise IndexCompatibilityError(
            "Index embedding provider/model does not match runtime configuration"
        )


def evaluation_identity(manifest: IndexManifest) -> dict[str, Any]:
    return {
        "build_id": manifest.build_id,
        "release": manifest.release_version,
        "embedding_provider": manifest.embedding_provider,
        "embedding_model": manifest.embedding_model,
        "embedding_dimensions": manifest.embedding_dimensions,
        "concept_count": manifest.concept_count,
    }


def _completed_manifest(conn: sqlite3.Connection, build_id: str) -> IndexManifest:
    row = conn.execute(
        "SELECT payload, active FROM manifests WHERE build_id = ? AND state = 'complete'",
        (build_id,),
    ).fetchone()
    if row is None:
        raise IndexEvaluationError("Evaluation build is unavailable; list completed builds")
    return LocalIndex._manifest(row)


def _require_evaluated(manifest: IndexManifest) -> None:
    if manifest.build_kind in {"sample", "legacy"}:
        return
    if manifest.build_kind != "production":
        raise IndexEvaluationError(
            f"Build {manifest.build_id} has an unknown classification; rebuild and evaluate it"
        )
    if not _has_passing_evaluation(manifest):
        raise IndexEvaluationError(
            "Production activation requires a passing evaluation. " + evaluation_next_step(manifest)
        )


def _has_passing_evaluation(manifest: IndexManifest) -> bool:
    report = manifest.evaluation_report or {}
    expected = evaluation_identity(manifest) | {"evaluation_version": manifest.evaluation_version}
    identity_matches = all(report.get(key) == value for key, value in expected.items())
    return report.get("passed") is True and bool(manifest.evaluation_version) and identity_matches


def evaluation_next_step(manifest: IndexManifest) -> str:
    if manifest.unclassified_source_build:
        return (
            f"Snapshot {manifest.unclassified_source_build} is unclassified. "
            f"Evaluate rebuild {manifest.build_id}, or recreate it with index-sample "
            "in a separate data directory."
        )
    return f"Evaluate build {manifest.build_id} with a matching calibration before activation."


def _ranked_page(
    conn: sqlite3.Connection,
    manifest: IndexManifest,
    embedding_provider: EmbeddingProvider,
    query: str,
    limit: int,
    mode: str,
    offset: int = 0,
    retired_status: str | None = None,
) -> tuple[list[SearchHit], int]:
    vector = _query_vector(embedding_provider, manifest, query) if mode != "bm25" else None
    ranked, total = rank_page(conn, manifest, query, vector, mode, offset, limit, retired_status)
    payloads = _concept_payloads(conn, manifest.build_id, [code for code, _ in ranked])
    hits = [
        SearchHit(
            concept=payloads[code],
            score=field.score,
            rank=offset + number,
            score_components={"bm25": field.bm25, "vector": field.vector},
            matched_on=field.kind,
        )
        for number, (code, field) in enumerate(ranked, 1)
    ]
    return hits, total


def _stored_dimensions(
    conn: sqlite3.Connection, manifest: IndexManifest | None
) -> IndexManifest | None:
    if manifest is None or manifest.embedding_dimensions is not None:
        return manifest
    row = conn.execute(
        "SELECT vector, kinds FROM concept_vectors WHERE build_id = ? LIMIT 1", (manifest.build_id,)
    ).fetchone()
    if row:
        return replace(manifest, embedding_dimensions=len(row[0]) // (4 * len(row[1])))
    return manifest


def _concept_payloads(
    conn: sqlite3.Connection, build: str, codes: list[str]
) -> dict[str, NcitConcept]:
    result = {}
    for chunk in batched(codes, _SQL_CHUNK, strict=False):
        for row in conn.execute(
            "SELECT code, payload FROM concepts WHERE build_id = ? "  # noqa: S608
            f"AND code IN ({','.join('?' for _ in chunk)})",
            [build, *chunk],
        ):
            result[row["code"]] = NcitConcept(**json.loads(row["payload"]))
    return result
