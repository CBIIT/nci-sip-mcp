"""Project benchmark evidence without inventing deployment telemetry."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from scripts.benchmark_records import PHASE_LABELS, require
from scripts.benchmark_report import Absent, Fingerprint, Report, selection_fingerprint
from scripts.evidence_benchmark_common import comparable
from scripts.evidence_envelope import decode_json, validate_envelope


def project_benchmark(
    envelope: bytes,
    report: bytes | None,
    *,
    selection: bytes | None = None,
) -> dict[str, Any]:
    """Validate measured samples and original selection; hashes do not establish producer trust."""
    record = validate_envelope(envelope, report)
    require(record["kind"] == "benchmark", "kind")
    if report is None:
        return record | {
            "mode": None,
            "transport": None,
            "cases": None,
            "missing": None,
            "inventory_complete": False,
            "selection_verified": False,
            "comparison_ready": False,
            "fingerprint": asdict(Fingerprint()),
        }
    native = Report.from_dict(decode_json(report), record["state"])
    result = record | {
        "mode": native.mode,
        "transport": native.transport,
        "cases": [case.project() for case in native.cases],
        "missing": native.missing(),
        "inventory_complete": native.complete and record["state"] == "completed",
        "selection_verified": selection is not None,
        "fingerprint": asdict(selection_fingerprint(selection, record["selection_sha256"], native)),
    }
    if native.environment != Absent.FIELD:
        result["environment"] = native.environment
    if native.transport == "streamable-http":
        result["phase_labels"] = PHASE_LABELS
    result["comparison_ready"] = comparable(result, result)[0]
    return result
