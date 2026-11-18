"""Make a tiny external test model and sample index, entirely offline."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from sentence_transformers import SentenceTransformer
from sentence_transformers.sentence_transformer.modules import BoW

from nci_sip_mcp.config import configure_logging
from nci_sip_mcp.evs import normalize_concept
from nci_sip_mcp.indexing import open_index
from ncit_index.embeddings import SentenceTransformersProvider

root = Path("/assets")
configure_logging("INFO")
if os.geteuid() == 0:
    raise RuntimeError("Smoke must run as the image's unprivileged user")
try:
    Path("/root-write-probe").write_text("must fail")
except OSError:
    pass
else:
    raise RuntimeError("Container root filesystem is writable")
SentenceTransformer(modules=[BoW(["neoplasm", "cancer", "tumor"])]).save(str(root / "model"))
with patch("socket.socket.connect", side_effect=AssertionError("Model attempted network access")):
    provider = SentenceTransformersProvider(str(root / "model"), local_files_only=True)
    try:
        SentenceTransformersProvider(str(root / "missing-model"), local_files_only=True)
    except OSError, ValueError:
        pass  # Missing external assets must fail even with network calls forbidden.
    else:
        raise RuntimeError("Missing model unexpectedly loaded")
index = open_index(root / "data")
concept = normalize_concept(
    json.loads(Path("/concept.json").read_text())["response"]["body"], None, "active_cache"
)
index.upsert_concepts([concept], None, provider)
manifest = index.upsert_concepts([concept], None, provider)
results = index.search("neoplasm", provider, limit=1, mode="vector")
if not results or results[0].concept.code != "C3262":
    raise RuntimeError("The externally loaded test model did not retrieve the fixture concept")
(root / "manifest.json").write_text(json.dumps(manifest.to_dict()))
(root / "builds.json").write_text(json.dumps([build.build_id for build in index.list_builds()]))
