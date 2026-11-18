"""NCI Semantic Infrastructure MCP server."""

import logging
from importlib.metadata import version

__all__ = ["__version__"]

# The version of the installed distribution, which the build derives from the git
# tag. The package must be installed: a bare source tree has no version.
try:
    __version__ = version("nci-si-mcp")
except UnicodeDecodeError:
    if "__version__" in globals():
        globals().pop("__version__")
        raise

    def __getattr__(name: str) -> str:
        if name == "__version__":
            return version("nci-si-mcp")
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Stay silent unless the application (the CLI or MCP server) configures logging.
logging.getLogger(__name__).addHandler(logging.NullHandler())
