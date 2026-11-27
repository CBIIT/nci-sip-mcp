# The interim NCIt semantic index

For operators building an NCIt index and reviewers assessing its retrieval evidence. This guide
explains the current field/model policy and the complete build-to-rollback task. For a small
smoke installation, use the [local guide's index sequence](local-guide.md#index); for storage
and request internals, see the [architecture](../ARCHITECTURE.md#build-a-local-index).

## Why the index exists

The [recorded EVS contract and semantic-search request](upstream/evs.md#evs-semantic) do not
supply the semantic retrieval required here. M4.1 is the explicit interim exception to A9.2's
restriction on local indexes and derived rankings ([specification](specification.md#the-local-ncit-index-the-one-interim-exception)).
The index uses content from one recorded EVS NCIt release; results identify that release and
`source: evs_index`. It neither invents terminology content nor silently replaces a live answer.

The intended long-term home is EVS. The [handover contract](index-migration.md) describes the
portable read boundary and future ownership, not an implemented hosted endpoint or delivery
date. EVS lexical search, local semantic retrieval and graph traversal answer different questions;
a semantic score does not establish a terminology relationship or a clinical conclusion.

## What is embedded

The build extracts the preferred name from EVS `name`, synonym text from each returned
`synonyms[].name`, and definition text from `definitions[].definition`. Missing or empty synonym
and definition entries supply no field. Nonblank text becomes a separate field vector:

| Field | Text policy | Match label |
|---|---|---|
| Preferred name | One returned name | `name` |
| Synonym | Each returned nonempty synonym name | `synonym` |
| Definition | Each returned nonempty definition | `definition` |

Identical text is embedded once **within a concept**, with name before synonym before definition
when the same string occurs in several fields. Whitespace-only fields are excluded. Text is
otherwise preserved: deduplication is exact-string, not case folding or whitespace normalization,
and equal text in different concepts remains separate. Concept codes, relations, hierarchy and
other properties are not concatenated into an embedding. Raw EVS payloads remain available for
an explicit offline rebuild; absent content is not filled in.

The implementation is [`evs.py`](../src/nci_sip_mcp/evs.py) for extraction and
[`storage.py`](../src/ncit_index/storage.py) for field selection. The
[index builder](../src/ncit_index/index.py) processes batches of **64 concepts**, encoding all
of their selected fields outside write transactions. This is a concept-batch size, not an
explicit sentence-transformers inference batch size. It validates vector count and dimensions,
and stores finite little-endian float32 vectors, grouped by concept with field positions.
The complete query string is passed separately to the same provider/model, subject to that
model's tokenization and truncation limits.

## Model, rationale and reproducibility limits

The default deterministic `hashing` provider (`hashing-128`) supports tests and local smoke runs
without model downloads. It is not the real-model retrieval-quality baseline. The optional
`sentence-transformers` provider can load a configured model; the recorded full-corpus
calibration used **`cambridgeltl/SapBERT-from-PubMedBERT-fulltext`**, **768 dimensions**, revision
**`090663c3ae57bf35ffe4d0d468a2a88d03051a4d`**.

The [SapBERT paper](https://aclanthology.org/2021.naacl-main.334/) describes aligning biomedical
entity-name representations through synonym supervision from UMLS. The
[model card at the recorded revision](https://huggingface.co/cambridgeltl/SapBERT-from-PubMedBERT-fulltext/blob/090663c3ae57bf35ffe4d0d468a2a88d03051a4d/README.md)
identifies its PubMedBERT abstract/full-text base and English UMLS training. These provide a
technical rationale for trying it on NCIt names and synonyms. This is a present rationale;
the repository's retained calibration and decision records contain no comparative model-selection
bakeoff. They establish neither superiority over alternatives nor clinical or sponsor validation.
Definitions provide additional indexed fields, but the model card's entity-name task does not
by itself establish quality for arbitrary long definitions or downstream workflows.

The actual [provider](../src/ncit_index/embeddings.py) loads a
`SentenceTransformer` from the model directory or the cached Hub snapshot, found in the cache sentence-transformers
downloads to (`SENTENCE_TRANSFORMERS_HOME` when set, else the Hugging Face hub cache; a cached
snapshot is used as it is; refresh the cache to update the model, since only an empty cache downloads) and calls `encode` with `normalize_embeddings=True` and
`show_progress_bar=False`, then converts the result to float32. Pooling, tokenization, truncation
and inference batching are delegated to the loaded model/library; this wrapper specifies no
CLS pooling or model-card example length. Do not copy those example settings into a description
of the historical calibration without evidence that they were used.

Runtime compatibility checks **provider, model name and dimensions**, and the model **revision**
of every build that stores one: it is refused unless the serving revision is known and equal. A build records the commit of the model files that embedded it, read
from the same cache (a Hub model's `snapshots/<commit>` folder; no download or Hub
call is added for it). The cases:

- Stored and runtime revisions known and equal: served.
- Both known and different: search, evaluation and `/ready` refuse with an
  `IndexModelRevisionError` naming both, and one `index_model_revision_mismatch` record per build
  and process is logged. Serve the model files the build was embedded with, or run `index-rebuild`
  with the serving model and activate its evaluated build.
- Stored known, runtime unknown (a custom provider that cannot tell): refused the same way,
  because the two cannot be compared.
- Stored unknown (a build made before this check): served after the
  checks above, and one `index_model_revision_unknown` record per build and process is logged. A
  stored build is never backfilled; rebuild to record a revision. `index-builds` shows
  `embedding_revision` (`null` when unknown). The hashing provider's revision names its algorithm.

A model supplied as a directory is identified by its content: the revision is `sha256:` followed
by the SHA-256 of the lines `<relative path>\0<file SHA-256 hex>\n` for every file. The path
is relative to the directory (which may itself be a link), uses `/` as separator on every
platform and has no leading `./`; the hex is lowercase; the lines are sorted as strings by code
point, joined without separator and encoded as UTF-8 before hashing. A file link counts as the content of its target under the link's own path (a Hub
snapshot folder of links into `blobs/` hashes like a copy); location and modification time do not
count, a changed byte, a rename or an added file does. A directory link, a broken link, a special
file or an empty directory is refused, naming the path. Copy a model directory unchanged: the
path comparison is exact, including case. The hash covers the files as read once, when the
provider is created (at build and at start-up), so the container serving `/model` refuses to start
when the directory it finds then differs from the one the index was embedded with. The directory,
including the targets of its links, must stay unchanged while a process runs: a later or
concurrent change is not detected. On one proxy sample (the cached `all-mpnet-base-v2` snapshot,
438.7 MB in 11 files, warm file cache, one machine; not the SapBERT model) hashing took about
0.17 s; this is not an upper bound, since a cold cache or network storage can be slower. A build made before this check stores no
revision and stays in the "stored unknown" case until it is rebuilt. Preserve the actual
software versions and evaluation data externally when reproducing a run; the commands below
alone do not provide a byte-reproducible model pin.

## How retrieval uses the fields

MCP lexical/typeahead queries retain EVS ordering and do not use this index. The CLI's local
`bm25` mode uses SQLite FTS5; semantic mode (internally `vector`) scores every indexed field
against the query vector. Hybrid combines per-query normalized components as
`0.55 * BM25 + 0.45 * vector`. Local lexical candidate selection uses only the first 64 tokens;
this 64-token preselection does not apply to the string passed to the vector encoder. The exact scan has no approximate-neighbor candidate cap.

Each concept appears once, with its winning field in `matchedOn`. Field-score ties prefer name,
then synonym, then definition. Exact preferred-name comparison separately applies Unicode NFC,
case folding and whitespace collapsing: an exact name scores 1 and wins ties, with concept code
ordering remaining ties. That comparison does not rewrite the text sent to the embedding model.
Scores rank one query; they are not comparable across queries or calibrated probabilities.
Paging retains a single build/release identity, and replacing a build expires its indexed cursors.
See [scoring and paging internals](../ARCHITECTURE.md#search) for the complete mechanics.

The [October 5, 2026 calibration notes](evaluation/ncit-v1-notes.md) and
[original report](evaluation/ncit-v1-full-corpus.json) cover NCIt 26.09d: 213,524 concepts and
928,520 deduplicated fields. **That candidate was never activated.** The twelve engineering
queries, recorded rankings and latency experiment are historical evidence, not a report of a
currently installed index or a new benchmark. The [evaluation protocol](retrieval-evaluation.md)
owns metric definitions, exact floors and SME validation. Acceptance checks protocol behavior;
hashing CI checks the evaluation machinery; neither establishes real-model retrieval quality.

## Build, evaluate and activate

Use a Git checkout with its tags and the [checkout prerequisites](../QUICKSTART.md#platforms-and-prerequisites).
For the first build, choose a writable **absolute** data directory below; replace the example
path. Developer samples need their own separate directory. Full-build candidates and the active
snapshot coexist in **one index database**, distinguished by build IDs, so atomic activation and
rollback use that same directory. Reuse it for subsequent candidates; do not create a fresh
directory for each build. Obtain model assets and authorize EVS access before this operator task. It can be expensive and requires disk space for
the database, download spool and retained builds. Installing the optional embeddings group and
constructing its provider may download model assets; opening the index database itself does not.

```bash
pdm install -G embeddings
export NCI_SIP_DATA_DIR=/absolute/path/to/dedicated-ncit-index
export NCI_SIP_EMBEDDING_PROVIDER=sentence-transformers
export NCI_SIP_EMBEDDING_MODEL=cambridgeltl/SapBERT-from-PubMedBERT-fulltext
pdm run nci-sip-mcp release-info
```

**Stop and inspect the selected release before building.** The shipped calibration matches
NCIt 26.09d/SapBERT/768 dimensions. If the selected release or model differs, do not start a full
build under those floors: agree a separately reviewed [full-corpus calibration](retrieval-evaluation.md#calibration-and-ci)
first. For a
smoke check only, use the separate sample path instead. `index-build` resolves the configured
monthly/weekly channel again; it has no arbitrary historical release argument. If the release
changes between preflight and build, its evaluation must still match the actual pinned candidate.

With the intended release/model and applicable calibration confirmed, build and list candidates:

```bash
pdm run nci-sip-mcp index-build
pdm run nci-sip-mcp index-builds
```

The build downloads unfiltered release-pinned EVS search pages of 1000 and reconciles each page's release, unique codes and
stable total before the first index write. A shifted/incomplete download fails. It then embeds
into a `building` snapshot and marks it complete only after all batches succeed. Interrupted
partial builds never serve or activate; stale work is reaped after checking its builder lease.

A full build is an **inactive production candidate**. Here “production” is the internal build
classification, not a statement that the server is deployed in production. `index-build`
automatically evaluates it and returns `buildId`, manifest and evaluation report on success.
Missing calibration or a failed evaluation must be resolved before activation, not bypassed. A completed candidate can remain inactive after such failure;
inspect `index-builds` and the error's next step. The existing active build continues serving.

Set `BUILD_ID` to the exact completed candidate ID returned/listed, then inspect its manifest and
report: release, provider/model/dimensions, corpus count, evaluation version, missing expected
concepts and pass/fail. Record the former active ID for rollback before changing it.

```bash
pdm run nci-sip-mcp evaluate --build-id "$BUILD_ID"
pdm run nci-sip-mcp index-activate "$BUILD_ID"
pdm run nci-sip-mcp index-builds
```

Evaluation reads that candidate without activating it. Activation requires a passing report
bound to its build/release/embedding identity and changes the active snapshot atomically.
**Activation retains only the selected build and the former active build.** The other
completed snapshots are hidden at once (they no longer appear in `index-builds` and cannot be
activated) and deleted after the activation commits. A failed deletion never undoes the
activation: after a storage failure while deleting, the result lists the builds still awaiting
removal in `pendingRemoval`, the log carries a warning `index_build_removal_pending`, and their
disk space stays in use until a later activation, for example `index-activate` of the already active build, removes them. A removal
frees pages inside the SQLite file for reuse; the file itself does not shrink. Preserve
needed evidence/backups before activation; do not assume every listed candidate will remain
available. Verify search/readiness with the same embedding settings
and intended release after activating; distribute matching assets separately to any serving replica.

For rollback, set `PREVIOUS_BUILD_ID` to the retained predecessor, keep its matching embedding
settings and model files (the revision check applies to a rollback too), and explicitly
activate it:

```bash
pdm run nci-sip-mcp index-activate "$PREVIOUS_BUILD_ID"
```

A developer `index-sample` instead activates its named subset and has no production evaluation
claim. It refuses to update an active production or unclassified build. Use its
[separate sample data path](local-guide.md#index); do not treat a successful five-concept smoke
run as a complete NCIt candidate. Plain `evaluate` reports on the active build, while
`evaluate --build-id` selects a completed candidate explicitly.

## Rebuilds and future handover

To re-embed stored raw concepts with selected settings, set `SOURCE_BUILD_ID` to a completed
snapshot and run `pdm run nci-sip-mcp index-rebuild "$SOURCE_BUILD_ID"`. This avoids an EVS download,
not model loading; make the model assets available. It creates another inactive build and
requires fresh evaluation for production classification before explicit activation. Rebuilding
an unclassified legacy snapshot creates a production candidate; retain the original for rollback.
For an old developer sample, recreate it separately with `index-sample` instead.

Legacy schema migration preserves cached lookup data, but old concatenated vectors require
explicit rebuild before field search (`capability_unavailable`). Opening a database never
re-embeds it. A migration on open logs `index_migration_started` and
`index_migration_finished` (see [deployment](deployment.md)). For the recorded SQLite page-layout experiment and conversion precautions, use
the [calibration reproduction notes](evaluation/ncit-v1-notes.md#reproducing-the-evidence).

Recalibrate after model/release/field-policy or judgment changes; retain full per-query reports,
asset/software identities and approval evidence. Historical 26.09d results cannot certify a newer
release. Follow the [SME protocol](retrieval-evaluation.md#nci-sme-validation-protocol) before
calling judgments SME-validated, and the [EVS migration guide](index-migration.md) for a future
hosted replacement. That handover, adapter and switch remain unimplemented; current commands
continue to operate the local index.
