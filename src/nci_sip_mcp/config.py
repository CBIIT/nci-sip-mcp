"""Runtime configuration for the NCI SIP MCP server."""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ncit_index.embeddings import normalize_embedding_settings

from .audit import JsonFormatter
from .validation import (
    HTTP_AUTH_MODES,
    HTTP_SESSIONS,
    PROFILES,
    RELEASE_CHANNELS,
    TRANSPORTS,
    UPSTREAM_MODES,
)

# Upper bound for the timeout and backoff settings; socket timeouts overflow far above it.
MAX_SECONDS = 3600
# Leave headroom below common 60-second MCP client timeouts. Reassess client/gateway
# deadlines before raising this owner-approved per-call elapsed-time allowance.
DEFAULT_CALL_DEADLINE_SECONDS = 50.0
MAX_ATTEMPTS = 10
# A response is read into memory whole before it is parsed, so the limit is capped.
MAX_RESPONSE_BYTES = 1024**3


def _has_plain_characters(value: str) -> bool:
    # urlsplit drops tabs and newlines, and urllib rejects them later. A query
    # or a fragment would swallow the path that is appended to the base URL.
    return value.isascii() and value.isprintable() and not any(char in value for char in " ?#")


def _is_base_url(value: str) -> bool:
    """Whether urllib can request `value` and append a path to it."""

    if not _has_plain_characters(value):
        return False
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        host.encode("idna")  # an empty or over-long label raises UnicodeError
        port = url.port  # None, or 0 to 65535: anything else raises
    except ValueError:
        return False
    return url.scheme in ("http", "https") and bool(host) and "@" not in url.netloc and port != 0


def _require_between(name: str, value: float, low: int, high: int) -> None:
    # Written so that NaN fails the comparison.
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


DEFAULT_EVS_BASE_URL = "https://api-evsrest.nci.nih.gov"
DEFAULT_EMBEDDING_MODEL = "hashing"
# The production base URL of each surface, as acceptance/fixtures/manifest.yaml records them. The
# server adds the platform's own paths: /api/v1/... to EVS, FHIR operations to the FHIR base,
# /NCIAPI/1.0/api/... to caDSR, /CDE/XML/releasedCDEsXML-OD.zip to the FTP base and
# /sparql to the SPARQL base.
PRODUCTION_BASE_URLS = {
    "evs_base_url": DEFAULT_EVS_BASE_URL,
    "evs_fhir_base_url": "https://api-evsrest.nci.nih.gov/fhir/r4",
    "cadsr_base_url": "https://cadsrapi.cancer.gov/rad",
    "cadsr_ftp_url": "https://cadsr.nci.nih.gov/ftp/caDSR_Downloads",
    "ssis_sparql_url": "https://shared.semantics.cancer.gov",
}


class UpstreamConfigurationError(ValueError):
    """Invalid installer-facing upstream selection."""


def _upstream_base_url(name: str, mode: str) -> str:
    variable = BASE_URL_VARIABLES[name]
    value = _env_url(variable)
    if not value and mode == "fixture":
        raise UpstreamConfigurationError(f"Fixture mode requires {variable}")
    if value and not _is_base_url(value):
        raise UpstreamConfigurationError(f"{variable} must be a plain credential-free http(s) URL")
    return value or PRODUCTION_BASE_URLS[name]


# Each base URL field with the variable that sets it.
BASE_URL_VARIABLES = {
    "evs_base_url": "NCI_SIP_EVS_BASE_URL",
    "evs_fhir_base_url": "NCI_SIP_EVS_FHIR_BASE_URL",
    "cadsr_base_url": "NCI_SIP_CADSR_BASE_URL",
    "cadsr_ftp_url": "NCI_SIP_CADSR_FTP_URL",
    "ssis_sparql_url": "NCI_SIP_SSIS_SPARQL_URL",
}
# NCIt's exclusion relationships are exactly R135 to R142 (convention A5.7).
DEFAULT_EXCLUSION_ROLE_CODES = tuple(f"R{number}" for number in range(135, 143))
ROLE_CODE_RE = re.compile(r"R[0-9]+")


def _require_choice(name: str, value: str, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(sorted(allowed))}, not {value!r}")


def _require_seconds(name: str, value: float) -> None:
    # Written so that NaN fails the comparison.
    if not 0 < value <= MAX_SECONDS:
        raise ValueError(f"{name} must be greater than zero and at most {MAX_SECONDS}")


MIN_SECRET_LENGTH = 8


def _require_role_codes(name: str, codes: tuple[str, ...]) -> None:
    # Only the form is checked: the release's relationship catalogue is not built yet.
    if not codes or not all(ROLE_CODE_RE.fullmatch(code) for code in codes):
        raise ValueError(f"{name} must be a comma-separated list of role codes such as R135")


def _require_redactable(name: str, secret: str) -> None:
    # Audit redaction replaces the secret as a substring of every log record; a short one
    # would mangle unrelated output.
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"{name} must hold a secret of at least {MIN_SECRET_LENGTH} characters")


def _require_licence_key(key: str | None) -> None:
    # The messages of the two secrets name the variable only: a secret never reaches an error.
    if key is None:
        return
    if not (key and key.isascii() and key.isprintable() and not any(c.isspace() for c in key)):
        raise ValueError("NCI_SIP_EVS_LICENSE_KEY must be a key without whitespace")
    _require_redactable("NCI_SIP_EVS_LICENSE_KEY", key)


def _require_credential(credential: str | None) -> None:
    if credential is None:
        return
    user, colon, password = credential.partition(":")
    if not (colon and user and password and credential.isprintable()):
        raise ValueError("NCI_SIP_CADSR_CREDENTIAL must have the form user:password")
    _require_redactable("NCI_SIP_CADSR_CREDENTIAL", password)


def _resolve_base_urls(settings: Settings) -> None:
    """Fill each unset base URL: the production default in live mode, an error in fixture mode.

    A fixture-mode server therefore never reaches a production host by accident.
    """

    for name, variable in BASE_URL_VARIABLES.items():
        value = getattr(settings, name)
        if not value:
            if settings.upstream_mode == "fixture":
                raise ValueError(f"{variable} must be given when NCI_SIP_UPSTREAM_MODE is fixture")
            value = PRODUCTION_BASE_URLS[name]
        elif not _is_base_url(value):
            # The value is not echoed: a URL can carry a password.
            raise ValueError(
                f"{variable} must be a plain http(s) URL without credentials, query or fragment"
            )
        object.__setattr__(settings, name, value)


def upstream_base_urls(*fields: str) -> tuple[str, ...]:
    """Validate and return selected upstream URLs without reading unrelated settings."""

    mode = os.getenv("NCI_SIP_UPSTREAM_MODE", "live")
    try:
        _require_choice("NCI_SIP_UPSTREAM_MODE", mode, UPSTREAM_MODES)
    except ValueError as error:
        raise UpstreamConfigurationError(str(error)) from None
    return tuple(_upstream_base_url(name, mode) for name in fields)


def _env_number[Number: (int, float)](
    name: str, default: str, parse: Callable[[str], Number]
) -> Number:
    value = os.getenv(name, default)
    try:
        return parse(value)
    except ValueError:
        kind = "an integer" if parse is int else "a number"
        raise ValueError(f"{name} must be {kind}, not {value!r}") from None


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "0")
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1, not {value!r}")
    return value == "1"


def _env_optional(name: str) -> str | None:
    """A variable that is unset, or set and not blank."""

    value = os.getenv(name)
    if value is not None and not value.strip():
        raise ValueError(f"{name} must not be empty; unset it instead")
    return value


def _env_url(variable: str) -> str:
    """The base URL a variable gives without a trailing slash, or "" where it is unset."""

    try:
        value = _env_optional(variable)
    except ValueError as error:
        raise UpstreamConfigurationError(str(error)) from None
    return "" if value is None else value.rstrip("/") or value


def _env_role_codes() -> tuple[str, ...]:
    value = os.getenv("NCI_SIP_EXCLUSION_ROLE_CODES")
    if value is None:
        return DEFAULT_EXCLUSION_ROLE_CODES
    return tuple(code.strip() for code in value.split(","))


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    return default if value is None else tuple(part.strip() for part in value.split(","))


DEFAULT_HTTP_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
DEFAULT_HTTP_ORIGINS = tuple("http://" + host for host in DEFAULT_HTTP_HOSTS)


def _require_authorities(name: str, values: tuple[str, ...], *, origin: bool) -> None:
    if not values:
        raise ValueError(f"{name} must list at least one authority")
    for value in values:
        url = value if origin else "http://" + value
        # The SDK supports a wildcard port, never a wildcard hostname.
        checked = url.removesuffix(":*")
        if not _is_base_url(checked) or "*" in checked or urlsplit(checked).path:
            raise ValueError(f"{name} must contain authorities without paths or credentials")


def _require_http(settings: Settings) -> None:
    _require_http_auth(settings)
    _require_choice("NCI_SIP_TRANSPORT", settings.transport, TRANSPORTS)
    _require_choice("NCI_SIP_HTTP_SESSIONS", settings.http_sessions, HTTP_SESSIONS)
    if not re.fullmatch(r"[A-Za-z0-9.:_-]+", settings.http_host):
        raise ValueError("NCI_SIP_HTTP_HOST must be a bind address")
    _require_between("NCI_SIP_HTTP_PORT", settings.http_port, 1, 65535)
    _require_between("NCI_SIP_HTTP_MAX_REQUEST_BYTES", settings.http_max_request_bytes, 1, 1024**3)
    _require_authorities("NCI_SIP_HTTP_ALLOWED_HOSTS", settings.http_allowed_hosts, origin=False)
    _require_authorities("NCI_SIP_HTTP_ALLOWED_ORIGINS", settings.http_allowed_origins, origin=True)


def require_local_stdio_access(settings: Settings) -> None:
    """Refuse to serve trusted-local stdio when HTTP authentication is required."""

    if settings.http_auth_mode == "required":
        raise ValueError(
            "NCI_SIP_HTTP_AUTH_MODE=required cannot serve over trusted-local stdio; "
            "use --transport streamable-http or another auth mode"
        )


def _require_http_auth(settings: Settings) -> None:
    _require_choice("NCI_SIP_HTTP_AUTH_MODE", settings.http_auth_mode, HTTP_AUTH_MODES)
    if not settings.http_auth_factory:
        return
    if settings.http_auth_mode != "required":
        raise ValueError("NCI_SIP_HTTP_AUTH_FACTORY requires required authentication mode")
    if not re.fullmatch(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", settings.http_auth_factory
    ):
        raise ValueError("NCI_SIP_HTTP_AUTH_FACTORY must name an installed module:factory")


@dataclass(frozen=True, slots=True)
class Settings:
    """The server's configuration.

    A base URL left empty takes its production default in live mode and is an error in fixture
    mode. The two credentials are left out of `repr`, so no string form shows them.
    """

    profile: str = "unified"
    upstream_mode: str = "live"
    evs_base_url: str = ""
    evs_fhir_base_url: str = ""
    cadsr_base_url: str = ""
    cadsr_ftp_url: str = ""
    ssis_sparql_url: str = ""
    release_channel: str = "monthly"
    exclusion_role_codes: tuple[str, ...] = DEFAULT_EXCLUSION_ROLE_CODES
    evs_license_key: str | None = field(default=None, repr=False)
    cadsr_credential: str | None = field(default=None, repr=False)
    data_dir: Path = Path(".nci-si-mcp")
    embedding_provider: str = "hashing"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    timeout_seconds: float = 30.0
    call_deadline_seconds: float = DEFAULT_CALL_DEADLINE_SECONDS
    match_timeout_seconds: float = 45.0
    evs_max_attempts: int = 3
    evs_retry_backoff_seconds: float = 0.25
    evs_max_response_bytes: int = 10 * 1024 * 1024
    index_batch_size: int = 100
    log_level: str = "INFO"
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    http_sessions: str = "stateful"
    http_max_request_bytes: int = 4 * 1024 * 1024
    http_allowed_hosts: tuple[str, ...] = DEFAULT_HTTP_HOSTS
    http_allowed_origins: tuple[str, ...] = DEFAULT_HTTP_ORIGINS
    http_require_index: bool = False
    http_auth_mode: str = "trusted-local"
    http_auth_factory: str | None = None

    def __post_init__(self) -> None:
        _require_choice("NCI_SIP_PROFILE", self.profile, PROFILES)
        _require_choice("NCI_SIP_UPSTREAM_MODE", self.upstream_mode, UPSTREAM_MODES)
        _require_choice("NCI_SIP_RELEASE_CHANNEL", self.release_channel, RELEASE_CHANNELS)
        _resolve_base_urls(self)
        _require_role_codes("NCI_SIP_EXCLUSION_ROLE_CODES", self.exclusion_role_codes)
        _require_licence_key(self.evs_license_key)
        _require_credential(self.cadsr_credential)
        _require_seconds("NCI_SIP_TIMEOUT_SECONDS", self.timeout_seconds)
        _require_seconds("NCI_SIP_CALL_DEADLINE_SECONDS", self.call_deadline_seconds)
        _require_seconds("NCI_SIP_MATCH_TIMEOUT_SECONDS", self.match_timeout_seconds)
        _require_between("NCI_SIP_EVS_MAX_ATTEMPTS", self.evs_max_attempts, 1, MAX_ATTEMPTS)
        _require_between(
            "NCI_SIP_EVS_RETRY_BACKOFF_SECONDS", self.evs_retry_backoff_seconds, 0, MAX_SECONDS
        )
        _require_between(
            "NCI_SIP_EVS_MAX_RESPONSE_BYTES", self.evs_max_response_bytes, 1, MAX_RESPONSE_BYTES
        )
        if self.index_batch_size < 1:
            raise ValueError("NCI_SIP_INDEX_BATCH_SIZE must be at least 1")
        if self.log_level.upper() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("NCI_SIP_LOG_LEVEL must be a standard logging level")
        normalize_embedding_settings(self.embedding_provider, self.embedding_model)
        _require_http(self)

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = os.getenv("NCI_SIP_DATA_DIR", ".nci-si-mcp")
        if not data_dir.strip():
            raise ValueError("NCI_SIP_DATA_DIR must not be empty")
        try:
            expanded_data_dir = Path(data_dir).expanduser()
        except RuntimeError as exc:
            raise ValueError(
                f"NCI_SIP_DATA_DIR names a home directory that cannot be resolved: {data_dir!r}"
            ) from exc
        urls = {name: _env_url(variable) for name, variable in BASE_URL_VARIABLES.items()}
        return cls(
            profile=os.getenv("NCI_SIP_PROFILE", "unified"),
            upstream_mode=os.getenv("NCI_SIP_UPSTREAM_MODE", "live"),
            **urls,
            release_channel=os.getenv("NCI_SIP_RELEASE_CHANNEL", "monthly"),
            exclusion_role_codes=_env_role_codes(),
            evs_license_key=_env_optional("NCI_SIP_EVS_LICENSE_KEY"),
            cadsr_credential=_env_optional("NCI_SIP_CADSR_CREDENTIAL"),
            data_dir=expanded_data_dir,
            embedding_provider=os.getenv("NCI_SIP_EMBEDDING_PROVIDER", "hashing"),
            embedding_model=os.getenv("NCI_SIP_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            timeout_seconds=_env_number("NCI_SIP_TIMEOUT_SECONDS", "30", float),
            call_deadline_seconds=_env_number(
                "NCI_SIP_CALL_DEADLINE_SECONDS", str(DEFAULT_CALL_DEADLINE_SECONDS), float
            ),
            match_timeout_seconds=_env_number("NCI_SIP_MATCH_TIMEOUT_SECONDS", "45", float),
            evs_max_attempts=_env_number("NCI_SIP_EVS_MAX_ATTEMPTS", "3", int),
            evs_retry_backoff_seconds=_env_number(
                "NCI_SIP_EVS_RETRY_BACKOFF_SECONDS", "0.25", float
            ),
            evs_max_response_bytes=_env_number(
                "NCI_SIP_EVS_MAX_RESPONSE_BYTES", str(10 * 1024 * 1024), int
            ),
            index_batch_size=_env_number("NCI_SIP_INDEX_BATCH_SIZE", "100", int),
            log_level=os.getenv("NCI_SIP_LOG_LEVEL", "INFO").upper(),
            transport=os.getenv("NCI_SIP_TRANSPORT", "stdio"),
            http_host=os.getenv("NCI_SIP_HTTP_HOST", "127.0.0.1"),
            http_port=_env_number("NCI_SIP_HTTP_PORT", "8000", int),
            http_sessions=os.getenv("NCI_SIP_HTTP_SESSIONS", "stateful"),
            http_max_request_bytes=_env_number("NCI_SIP_HTTP_MAX_REQUEST_BYTES", "4194304", int),
            http_allowed_hosts=_env_list("NCI_SIP_HTTP_ALLOWED_HOSTS", DEFAULT_HTTP_HOSTS),
            http_allowed_origins=_env_list("NCI_SIP_HTTP_ALLOWED_ORIGINS", DEFAULT_HTTP_ORIGINS),
            http_require_index=_env_flag("NCI_SIP_HTTP_REQUIRE_INDEX"),
            http_auth_mode=os.getenv("NCI_SIP_HTTP_AUTH_MODE", "trusted-local"),
            http_auth_factory=_env_optional("NCI_SIP_HTTP_AUTH_FACTORY"),
        )


def configure_logging(level: str) -> None:
    """Configure diagnostics on stderr so MCP stdout remains protocol-only."""

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=getattr(logging, level.upper()), handlers=[handler])
    # Diagnostic verbosity must not disable the required completion record.
    logging.getLogger("nci_sip_mcp.audit").setLevel(logging.INFO)
