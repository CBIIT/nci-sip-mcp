"""Run only the verified furnished CLI with ambient proxy routing disabled."""

import sys
from pathlib import Path
from urllib.request import OpenerDirector, ProxyHandler, build_opener

import nci_si_acceptance
from nci_si_acceptance.request_native import native_policy


def main() -> int:
    with native_policy():
        return _run()


def _run() -> int:
    from nci_si_mcp import http_client  # noqa: PLC0415 - authenticated bootstrap first.

    root = Path(__file__).resolve().parents[3]
    expected = {
        Path(nci_si_acceptance.__file__): root / "acceptance/src/nci_si_acceptance/__init__.py",
        Path(http_client.__file__): root / "src/nci_si_mcp/http_client.py",
    }
    if Path(sys.prefix).resolve() != (root / ".venv").resolve():
        raise ValueError("controlled live launcher requires the furnished project environment")
    if any(actual.resolve() != wanted.resolve() for actual, wanted in expected.items()):
        raise ValueError("controlled live launcher imported an unexpected module origin")
    if not isinstance(http_client._OPENER, OpenerDirector):
        raise ValueError("controlled live transport integration is unavailable")
    # This private adapter is guarded by the real-child regression, including redirects.
    http_client._OPENER = build_opener(ProxyHandler({}), http_client._SameOriginRedirects())
    arguments = sys.argv[1:]
    if arguments == ["serve"]:
        arguments = ["serve", "--transport", "stdio"]
    elif not _prepare(arguments):
        raise ValueError("controlled live launcher requires a furnished operation")
    sys.argv = ["nci-si-mcp", *arguments]
    from nci_si_mcp.cli import main as cli_main  # noqa: PLC0415 - proxy setup first.

    return cli_main()


def _prepare(arguments: list[str]) -> bool:
    from nci_si_mcp.validation import NCIT_CODE_RE  # noqa: PLC0415 - legacy prepare only.

    return (
        len(arguments) > 1
        and arguments[0] == "index-sample"
        and all(NCIT_CODE_RE.fullmatch(code) for code in arguments[1:])
    )


if __name__ == "__main__":
    raise SystemExit(main())
