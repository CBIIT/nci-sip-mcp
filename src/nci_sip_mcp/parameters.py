"""What a tool parameter means, stated next to the parameter in the handler signature.

The core package has no dependencies, so a signature cannot carry pydantic's `Field`. It carries
`Described` metadata in `Annotated[...]` instead; `server.py` turns it into the description and
the schema keywords of the served input schema, and the CLI ignores it. The nested records of an
argument (TypedDicts) are described with `describe_fields`, which the server adds to the schema.
"""

from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from .validation import RELEASE_FORM, TERMINOLOGY_FORM


@dataclass(frozen=True)
class Described:
    """The plain-word description of a parameter and the constraints the schema states."""

    description: str
    pattern: str | None = None
    min_items: int | None = None
    max_items: int | None = None

    def field_arguments(self) -> dict[str, Any]:
        """The arguments of pydantic's `Field` that state this."""

        stated = {
            "description": self.description,
            "pattern": self.pattern,
            "min_length": self.min_items,
            "max_length": self.max_items,
        }
        return {key: value for key, value in stated.items() if value is not None}


def count_bound(what: str, default: int, maximum: int) -> Described:
    """A count the tool bounds: its default and maximum, which the schema does not enforce."""

    return Described(
        f"{what} Default {default}, at most {maximum}; a larger value is applied as {maximum}."
    )


# Nested records of the arguments, by TypedDict name: the description of each of its fields.
FIELD_DESCRIPTIONS: dict[str, dict[str, str]] = {}


def describe_fields(record: type, **descriptions: str) -> None:
    """State what each field of the TypedDict `record` means; every field needs a text."""

    if record.__name__ in FIELD_DESCRIPTIONS:
        raise TypeError(f"{record.__name__} is described twice")
    if set(descriptions) != set(record.__annotations__):
        raise TypeError(f"{record.__name__} needs one description for each of its fields")
    FIELD_DESCRIPTIONS[record.__name__] = descriptions


Terminology = Annotated[
    str,
    Described(
        "Short lowercase name of the terminology, for example ncit.", pattern=TERMINOLOGY_FORM
    ),
]
Code = Annotated[
    str,
    Described("Code of the concept in that terminology, for example C3262 for NCIt."),
]
Release = Annotated[
    str | None,
    Described(
        "Release of the terminology to read, for example 26.06e. "
        "For NCIt, leave it unset to use the current release of the configured "
        "channel, kept for stateful handshake HTTP sessions and stdio connections. "
        "Sessionless 2026-07-28 HTTP resolves per call, even in stateful mode; "
        "pass the release from the first result's provenance to keep it stable. "
        "Other terminologies need it.",
        pattern=RELEASE_FORM,
    ),
]
NcitRelease = Annotated[
    str | None,
    Described(
        "NCIt release to use, for example 26.06e. Leave it unset to use the current release "
        "of the configured channel, kept for stateful handshake HTTP sessions "
        "and stdio connections. "
        "Sessionless 2026-07-28 HTTP resolves per call, even in stateful mode; "
        "pass the release from the first result's provenance to keep it stable.",
        pattern=RELEASE_FORM,
    ),
]
RegistryRelease = Annotated[
    str | None,
    Described(
        "caDSR publishes no registry-level release yet (C-1); leave unset. Once one exists "
        "it becomes mandatory and a mismatch fails closed, as release does for NCIt."
    ),
]
Cursor = Annotated[
    str | None,
    Described(
        "The nextCursor of the previous page, passed back unchanged to get the next page. "
        "Leave unset for the first page."
    ),
]


class SchemeFilter(TypedDict):
    publicId: str
    version: str


describe_fields(
    SchemeFilter,
    publicId="Public id of the classification scheme, for example 2200621.",
    version="Version of the classification scheme, for example 1.0.",
)


# The one owner of the filter keys search_data_elements takes; the matching tools add a scheme.
class SearchFilters(TypedDict, total=False):
    context: str
    workflowStatus: str
    registrationStatus: str
    valueDomainType: str


class MatchFilters(SearchFilters, total=False):
    classificationScheme: SchemeFilter


_SEARCH_FILTER_TEXTS = {
    "context": "Only data elements of this caDSR context, for example NCIP.",
    "workflowStatus": "Only data elements with this workflow status, for example RELEASED.",
    "registrationStatus": (
        "Registration status as a free string, for example Standard, Superceded or Superseded; "
        "passed unchanged as a match header. Nonempty search filters are unsupported (OP-C03)."
    ),
    "valueDomainType": "Only data elements with this value domain type, for example Enumerated.",
}
describe_fields(SearchFilters, **_SEARCH_FILTER_TEXTS)
describe_fields(
    MatchFilters,
    **_SEARCH_FILTER_TEXTS,
    classificationScheme="Only data elements in this classification scheme; give both its "
    "publicId and version.",
)
