"""Selected local startup evidence and advisory, revision-bound configuration proposals."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from nci_sip_mcp.config import Settings
from nci_sip_mcp.config_snapshot import PROPOSABLE, read_snapshot

MAX_VALUE = 100


class ConfigurationConflictError(ValueError):
    """The submitted form no longer names the selected recorded target/revision."""


class SnapshotInvalidError(OSError):
    """The selected startup snapshot is malformed.

    An OSError, not a ValueError: like a missing snapshot it is unusable evidence (503), not the
    caller's bad request, which the portal answers with 400.
    """


class LocalConfiguration:
    def __init__(self, path: Path | None):
        self.path = path

    def read(self) -> dict[str, Any]:
        if self.path is None:
            raise OSError("No target startup snapshot selected")
        # Never substitute companion settings or expose the source path or body.
        try:
            return read_snapshot(self.path)
        except OSError:
            raise OSError("Selected target startup evidence is unreadable") from None
        except ValueError:
            raise SnapshotInvalidError("Selected target startup evidence is malformed") from None

    def propose(self, *, instance: str, revision: str, setting: str, value: str) -> dict[str, Any]:
        snapshot = self.read()
        if (instance, revision) != (snapshot["instance"], snapshot["revision"]):
            raise ConfigurationConflictError("Target or revision changed; reload configuration")
        after = _proposed_value(setting, value)
        return {
            "instance": instance,
            "revision": revision,
            "captured_at": snapshot["captured_at"],
            "setting": setting,
            "before": snapshot["settings"][setting]["value"],
            "after": after,
            "restart_required": True,
            "applied": False,
        }


def _proposed_value(setting: str, value: str) -> str | int | float:
    if setting not in PROPOSABLE or not isinstance(value, str) or len(value) > MAX_VALUE:
        raise ValueError("Only approved operational settings can be proposed")
    defaults = Settings()
    kind = type(getattr(defaults, setting))
    converted = kind(value)
    validated = replace(defaults, **{setting: converted})
    return getattr(validated, setting)
