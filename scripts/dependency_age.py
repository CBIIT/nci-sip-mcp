"""Enforce dependency ages, temporary exact-version security exceptions and their removal."""

import json
import re
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_AGE = timedelta(days=5)
PYTHON_LOCKS = (
    Path("container/requirements-amd64.txt"),
    Path("container/companion-requirements.txt"),
)
NPM_LOCK = Path("docs/site-assets/package-lock.json")
NORMALISE_PYTHON_NAME = re.compile(r"[-_.]+")
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
ADVISORY = re.compile(r"^(?:GHSA-[a-z0-9]{4}(?:-[a-z0-9]{4}){2}|CVE-\d{4}-\d{4,}|PYSEC-\d{4}-\d+)$")
type Fetch = Callable[[str], dict[str, Any]]
type MetadataFetch = Callable[[str], dict[str, Any] | str]


@dataclass(frozen=True, order=True)
class Package:
    ecosystem: str
    name: str
    version: str


@dataclass(frozen=True)
class DirectWheel:
    filename: str
    sha256: str
    index_url: str


type Dependency = Package | DirectWheel


@dataclass(frozen=True)
class SecurityException:
    version: str
    advisory: str


def _python_name(name: str) -> str:
    return NORMALISE_PYTHON_NAME.sub("-", name).lower()


def _pdm_packages(path: Path) -> set[Package]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for row in data["package"]:
        if {"url", "git"}.intersection(row):
            raise ValueError(f"{path}: unsupported dependency entry: {row.get('name', row)}")
    return {
        Package("pypi", _python_name(row["name"]), row["version"])
        for row in data["package"]
        if "name" in row and "version" in row and "path" not in row
    }


def _requirements_packages(path: Path) -> set[Dependency]:
    packages: set[Dependency] = set()
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        packages.add(_requirement_package(path, line))
    return packages


def _requirement_package(path: Path, line: str) -> Dependency:
    if match := PIN.match(line):
        return Package("pypi", _python_name(match[1]), match[2])
    if match := re.match(r"^([A-Za-z0-9_.-]+)\s+@\s+(https://\S+)", line):
        digest = re.search(r"--hash=sha256:([a-f0-9]{64})", line)
        if digest is None:
            raise ValueError(f"{path}: {match[1]} direct URL needs a sha256 hash")
        filename = unquote(urlsplit(match[2]).path.rsplit("/", 1)[-1])
        index = match[2].rsplit("/", 1)[0] + "/" + quote(_python_name(match[1]), safe="") + "/"
        return DirectWheel(filename, digest[1], index)
    raise ValueError(f"{path}: unsupported dependency entry: {line}")


def _npm_packages(path: Path) -> set[Package]:
    records = json.loads(path.read_text(encoding="utf-8"))["packages"]
    return {
        _npm_package(path, key, row)
        for key, row in records.items()
        if "node_modules/" in key and "version" in row
    }


def _npm_package(path: Path, key: str, row: dict[str, Any]) -> Package:
    resolved = urlsplit(row.get("resolved", ""))
    if resolved.scheme != "https" or resolved.netloc != "registry.npmjs.org":
        raise ValueError(f"{path}: unsupported dependency entry: {key}")
    return Package("npm", row.get("name", key.rsplit("node_modules/", 1)[1]), row["version"])


def locked_packages(root: Path = ROOT) -> list[Dependency]:
    """Unique registry releases and direct wheels in every committed dependency lock."""

    return sorted(
        {package for packages in inventories(root).values() for package in packages},
        key=_package_label,
    )


def inventories(root: Path) -> dict[str, list[Dependency]]:
    parsers = {
        Path("pdm.lock"): _pdm_packages,
        **dict.fromkeys(PYTHON_LOCKS, _requirements_packages),
        NPM_LOCK: _npm_packages,
    }
    result = {}
    for relative, parser in parsers.items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"{relative}: is missing")
        packages = sorted(parser(path), key=_package_label)
        if not packages:
            raise ValueError(f"{relative}: contains no registry releases")
        result[str(relative)] = packages
    return result


def parse_exceptions(records: object) -> dict[str, SecurityException]:
    """Validate the temporary security-fix exceptions from project configuration."""

    if not isinstance(records, dict):
        raise ValueError("Dependency age exceptions must be a table")
    return dict(_parse_exception(name, record) for name, record in records.items())


def _parse_exception(raw_name: object, record: object) -> tuple[str, SecurityException]:
    if not isinstance(raw_name, str) or not isinstance(record, dict):
        raise ValueError("Each dependency age exception must be a package table")
    version, advisory = record.get("version"), record.get("advisory")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{raw_name} exception needs a version")
    if not isinstance(advisory, str) or ADVISORY.fullmatch(advisory) is None:
        raise ValueError(f"{raw_name} exception needs a valid advisory")
    return _python_name(raw_name), SecurityException(version, advisory)


def read_exceptions(root: Path = ROOT) -> dict[str, SecurityException]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    records = (
        project.get("tool", {}).get("nci-si", {}).get("dependency-age", {}).get("exceptions", {})
    )
    return parse_exceptions(records)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _checked_times(values: list[object], missing: str) -> tuple[list[datetime], str]:
    if not values:
        return [], missing
    times = [_timestamp(value) for value in values]
    if any(value is None for value in times):
        return [], "has an invalid upload time"
    return [value for value in times if value is not None], ""


def _pypi_times(package: Package, metadata: dict[str, Any]) -> tuple[list[datetime], str]:
    rows = metadata.get("urls", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return [], "has malformed registry metadata"
    values = [row.get("upload_time_iso_8601") for row in rows]
    return _checked_times(values, "has no distribution upload time")


def _npm_times(package: Package, metadata: dict[str, Any]) -> tuple[list[datetime], str]:
    times = metadata.get("time", {})
    if not isinstance(times, dict):
        return [], "has malformed registry metadata"
    value = times.get(package.version)
    return _checked_times([] if value is None else [value], "has no release upload time")


def _metadata_url(package: Dependency) -> str:
    if isinstance(package, DirectWheel):
        return package.index_url
    encoded = quote(package.name, safe="")
    if package.ecosystem == "pypi":
        return f"https://pypi.org/pypi/{encoded}/{quote(package.version, safe='')}/json"
    return f"https://registry.npmjs.org/{encoded}"


def age_findings(
    packages: list[Dependency],
    now: datetime,
    fetch: MetadataFetch,
    exceptions: dict[str, SecurityException] | None = None,
) -> list[str]:
    """Explain unverified or too-young dependencies and expired security exceptions."""

    findings = []
    cutoff = now - MINIMUM_AGE
    exceptions = exceptions or {}
    for package in dict.fromkeys(packages):
        exception = exceptions.get(package.name) if isinstance(package, Package) else None
        if finding := _age_finding(package, cutoff, fetch, exception):
            findings.append(finding)
    return findings


def _age_finding(
    package: Dependency,
    cutoff: datetime,
    fetch: MetadataFetch,
    exception: SecurityException | None,
) -> str:
    label = _package_label(package)
    times, error = _release_times(package, fetch)
    if error:
        return f"{label} {error}"
    if _matches_exception(package, exception):
        return _exception_finding(label, times, cutoff)
    return (
        f"{label} was uploaded inside the five-day window"
        if any(uploaded > cutoff for uploaded in times)
        else ""
    )


def _package_label(package: Dependency) -> str:
    return (
        f"wheel:{package.filename}"
        if isinstance(package, DirectWheel)
        else f"{package.ecosystem}:{package.name}=={package.version}"
    )


def _matches_exception(package: Dependency, exception: SecurityException | None) -> bool:
    return (
        isinstance(package, Package)
        and package.ecosystem == "pypi"
        and exception is not None
        and package.version == exception.version
    )


def _exception_finding(label: str, times: list[datetime], cutoff: datetime) -> str:
    return (
        f"{label} is five days old; remove this exception"
        if all(uploaded <= cutoff for uploaded in times)
        else ""
    )


class _WheelLinks(HTMLParser):
    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.matches: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        href = urlsplit(values.get("href") or "")
        if unquote(href.path.rsplit("/", 1)[-1]) == self.filename:
            self.matches.append(values)


def _wheel_times(package: DirectWheel, metadata: object) -> tuple[list[datetime], str]:
    if not isinstance(metadata, str):
        return [], "has malformed index metadata"
    links = _WheelLinks(package.filename)
    links.feed(metadata)
    if not links.matches:
        return [], "has no matching index filename"
    if any(_wheel_hash(row) != package.sha256 for row in links.matches):
        return [], "index sha256 differs from the lock"
    return _wheel_upload_times(links.matches)


def _wheel_upload_times(matches: list[dict[str, str | None]]) -> tuple[list[datetime], str]:
    values: list[object] = [row.get("data-upload-time") for row in matches]
    if any(value is None for value in values):
        return [], "has no distribution upload time"
    return _checked_times(values, "has no distribution upload time")


def _wheel_hash(row: dict[str, str | None]) -> str:
    fragment = urlsplit(row.get("href") or "").fragment
    return parse_qs(fragment).get("sha256", [""])[0]


def _release_times(package: Dependency, fetch: MetadataFetch) -> tuple[list[datetime], str]:
    try:
        metadata = fetch(_metadata_url(package))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [], f"metadata could not be verified ({type(error).__name__})"
    if isinstance(package, DirectWheel):
        return _wheel_times(package, metadata)
    if not isinstance(metadata, dict):
        return [], "has malformed registry metadata"
    return (
        _pypi_times(package, metadata)
        if package.ecosystem == "pypi"
        else _npm_times(package, metadata)
    )


def release_upload_time(package: Package, fetch: Fetch) -> datetime:
    """Newest verified artifact upload time for an exact registry release."""

    times, error = _release_times(package, fetch)
    if error:
        raise ValueError(f"{package.name} upload time could not be read: {error}")
    return max(times)


def fetch_json(url: str) -> dict[str, Any]:
    """Read public package metadata over the fixed HTTPS registry URLs."""

    if not url.startswith("https://"):
        raise ValueError("Registry metadata URL must use HTTPS")
    request = urllib.request.Request(  # noqa: S310 - HTTPS is required immediately above.
        url, headers={"User-Agent": "nci-sip-mcp-dependency-age"}
    )
    with urllib.request.urlopen(  # noqa: S310 - the Request contains the checked HTTPS URL.
        request, timeout=30
    ) as response:
        return json.load(response)


def fetch_metadata(url: str) -> dict[str, Any] | str:
    if not url.endswith("/"):
        return fetch_json(url)
    # Index URLs are constructed only from the parser's HTTPS direct-wheel requirements.
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - parser requires HTTPS.
        return response.read().decode("utf-8")


def run(
    packages: list[Dependency],
    exceptions: dict[str, SecurityException],
    now: datetime,
    fetch: MetadataFetch,
) -> int:
    findings = age_findings(packages, now, fetch, exceptions)
    for finding in findings:
        print(finding, file=sys.stderr)
    return int(bool(findings))


def main() -> int:
    return check(ROOT, read_exceptions(), datetime.now(UTC), fetch_metadata)


def check(
    root: Path,
    exceptions: dict[str, SecurityException],
    now: datetime,
    fetch: MetadataFetch,
) -> int:
    try:
        locks = inventories(root)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    packages = sorted({package for rows in locks.values() for package in rows}, key=_package_label)
    if unmatched := _unmatched_exceptions(packages, exceptions):
        for finding in unmatched:
            print(finding, file=sys.stderr)
        return 1
    status = run(packages, exceptions, now, fetch)
    for name, rows in locks.items():
        print(_summary(name, rows, exceptions))
    return status


def _unmatched_exceptions(
    packages: list[Dependency], exceptions: dict[str, SecurityException]
) -> list[str]:
    locked = {
        (package.name, package.version)
        for package in packages
        if isinstance(package, Package) and package.ecosystem == "pypi"
    }
    return [
        f"{name}=={exception.version} matches no locked PyPI release; remove this exception"
        for name, exception in sorted(exceptions.items())
        if (name, exception.version) not in locked
    ]


def _summary(
    name: str, packages: list[Dependency], exceptions: dict[str, SecurityException]
) -> str:
    matched = [
        f"{package.name} {package.version}, {exceptions[package.name].advisory}"
        for package in packages
        if isinstance(package, Package)
        and _matches_exception(package, exceptions.get(package.name))
    ]
    suffix = f" ({'; '.join(matched)})" if matched else ""
    noun = "exception" if len(matched) == 1 else "exceptions"
    return f"{name}: {len(packages)} releases checked, {len(matched)} {noun}{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
