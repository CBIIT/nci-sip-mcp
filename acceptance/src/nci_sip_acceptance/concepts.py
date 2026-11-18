"""The concept rules: EVS concept requests answered from one recording per concept.

EVS's concept endpoints return any projection (`include`) of a concept, alone
(`/api/v1/concept/{terminology}/{code}`) or in a batch (`/api/v1/concept/{terminology}
?list=`), and one relation list of a concept on its own
(`/api/v1/concept/{terminology}/{code}/roles`, operations OP-E10, E11,
E14 to E17, E20). Rather than one recording per request form, the fixture set records
each concept once, and three rules compose the answer EVS gives; `record.py` checks the
composition against real projections, batches and relation lists:

- project by include: an answer holds the base keys and the keys each include value
  adds (the manifest's table); a request without include gets the default. EVS
  leaves out an empty list, and so does a recording.
- select by list: a batch answer holds each requested code once and leaves out a
  code EVS does not know; EVS refuses a list of more than 1,000 codes (400), counted
  before duplicates are dropped, and so do the rules. EVS keeps no order: the same
  request answered in two orders a minute apart on 2 October 2026, mostly but not
  always lexicographic. The rules answer the found concepts in request order rotated
  by one, which differs from it whenever two are found, so that pairing answers with
  requests by position fails here rather than only sometimes against EVS.
- one relation: the list under that key of the concept, or an empty list where it has
  none, for the relations the manifest names (verified 2 October 2026: each equals
  its include projection; `history` answers in another shape and is not one).

A recording covers the keys its own include names. The rules answer only what the
recordings cover, so a missing recording is never taken for an empty answer: a key a
recording does not cover, an include value outside the table, a code without a
recording, or any other parameter leaves the request unanswered. A recording of a 404
says EVS does not know the code: a request for that code alone gets the 404, and a
batch leaves the code out; a relation of it goes unanswered, since EVS's 404 then names
another path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Callable

# The most codes EVS answers in one batch (verified 2 October 2026: 1,001 answer 400).
MAX_BATCH = 1000
CONCEPT_PATH = re.compile(
    r"/api/v1/concept/(?P<terminology>[^/]+)(?:/(?P<code>[^/]+)(?:/(?P<relation>[^/]+))?)?"
)
# The one name in a concept's place that is an endpoint of its own.
SEARCH = "search"


@dataclass(frozen=True, slots=True)
class Recording:
    """One concept as recorded: its fixture, the answer EVS gave, and the keys it covers."""

    name: str
    status: int
    body: Any
    covers: frozenset[str]


type Find = Callable[[str, str], Recording | None]


@dataclass(frozen=True, slots=True)
class Answer:
    names: list[str]
    status: int
    body: Any


@dataclass(frozen=True, slots=True)
class ConceptRules:
    """The include table: the keys every answer holds, and those each include value adds;
    and the relations answered on their own."""

    base: frozenset[str]
    include: dict[str, frozenset[str]]
    default: str
    relations: frozenset[str]

    @classmethod
    def from_manifest(cls, section: Any) -> Self:
        """The rules from the manifest's `evs.concepts` section."""

        try:
            rules = cls(
                frozenset(section["base"]),
                {value: frozenset(keys) for value, keys in section["include"].items()},
                section["default"],
                frozenset(section["relations"]),
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(
                "manifest.yaml: evs.concepts has base, include, default and relations"
            ) from error
        if rules.default not in rules.include:
            raise ValueError("manifest.yaml: evs.concepts.default is one of its include values")
        return rules

    def keys(self, include: str) -> frozenset[str] | None:
        """The keys an include value names; None for a value outside the table."""

        values = include.split(",")
        if not all(value in self.include for value in values):
            return None
        return self.base.union(*(self.include[value] for value in values))

    def composes(self, path: str, params: dict[str, list[str]]) -> bool:
        """Whether the rules answer this request when the recordings hold its concepts:
        one concept or a batch at an include they know, or a relation list they know."""

        match = CONCEPT_PATH.fullmatch(path)
        if match is None or match["code"] == SEARCH:
            return False
        if match["relation"]:
            return not params and match["relation"] in self.relations
        include = params.get("include", [self.default])[0]
        return _composable(params, single=match["code"] is not None) and bool(self.keys(include))

    def recording_problem(self, request: dict[str, Any], status: int, body: Any) -> str | None:
        """What makes a fixture unusable as a concept recording, if anything."""

        match = CONCEPT_PATH.fullmatch(request["path"])
        plain = (request["surface"], request["method"]) == ("evs", "GET")
        if match is None or not plain or not _one_concept(match):
            return "a concept recording is a GET of /api/v1/concept/{terminology}/{code} from evs"
        return self._include_problem(request.get("params", {})) or _answer_problem(
            match["code"], status, body
        )

    def _include_problem(self, params: dict[str, list[str]]) -> str | None:
        if set(params) != {"include"} or len(params["include"]) != 1:
            return "a concept recording names exactly the include it was recorded with"
        if self.keys(params["include"][0]) is None:
            return f"include {params['include'][0]} is outside the manifest's table"
        return None

    def answer(self, path: str, params: dict[str, list[str]], find: Find) -> Answer | None:
        """EVS's answer to a concept request, composed from the recordings; None if they cannot."""

        match = CONCEPT_PATH.fullmatch(path)
        if match is None:
            return None
        terminology, code, relation = match["terminology"], match["code"], match["relation"]
        if relation:
            return self._relation(find(terminology, code), relation, params)
        if not _composable(params, single=code is not None):
            return None
        keys = self.keys(params.get("include", [self.default])[0])
        if keys is None:
            return None
        if code:
            return _single(find(terminology, code), keys)
        listed = dict.fromkeys(params["list"][0].split(","))
        return _batch([find(terminology, each) for each in listed], keys)

    def _relation(
        self, recording: Recording | None, relation: str, params: dict[str, list[str]]
    ) -> Answer | None:
        if recording is None or recording.status != HTTPStatus.OK:
            return None
        if params or relation not in self.relations or relation not in recording.covers:
            return None
        return Answer([recording.name], HTTPStatus.OK, recording.body.get(relation, []))


def _one_concept(match: re.Match[str]) -> bool:
    return match["code"] is not None and match["relation"] is None


def recording_key(path: str) -> tuple[str, str]:
    """The terminology and code a concept recording's request path names."""

    match = CONCEPT_PATH.fullmatch(path)
    if match is None or not _one_concept(match):
        raise ValueError(f"{path} is not the path of one concept")
    return match["terminology"], match["code"]


def project(concept: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in concept.items() if key in keys}


def _answer_problem(code: str, status: int, body: Any) -> str | None:
    if status not in (HTTPStatus.OK, HTTPStatus.NOT_FOUND):
        return "a concept recording answers 200 or 404"
    if status == HTTPStatus.OK and (not isinstance(body, dict) or body.get("code") != code):
        return f"a concept recording's body is the concept {code}"
    return None


def _composable(params: dict[str, list[str]], *, single: bool) -> bool:
    """Whether the rules know the request's parameters: include, and for a batch a list
    EVS would answer."""

    if any(len(values) != 1 for values in params.values()):
        return False
    if single:
        return set(params) <= {"include"}
    if "list" not in params or not set(params) <= {"include", "list"}:
        return False
    return len(params["list"][0].split(",")) <= MAX_BATCH


def _usable(recording: Recording | None, keys: frozenset[str]) -> TypeGuard[Recording]:
    if recording is None:
        return False
    return recording.status == HTTPStatus.NOT_FOUND or keys <= recording.covers


def _single(recording: Recording | None, keys: frozenset[str]) -> Answer | None:
    if not _usable(recording, keys):
        return None
    if recording.status == HTTPStatus.NOT_FOUND:
        return Answer([recording.name], recording.status, recording.body)
    return Answer([recording.name], HTTPStatus.OK, project(recording.body, keys))


def _batch(recordings: list[Recording | None], keys: frozenset[str]) -> Answer | None:
    if not all(_usable(recording, keys) for recording in recordings):
        return None
    found = [recording for recording in recordings if recording is not None]
    return Answer([each.name for each in found], HTTPStatus.OK, _known(found, keys))


def _known(recordings: list[Recording], keys: frozenset[str]) -> list[dict[str, Any]]:
    """The projected concepts of a batch, rotated by one; the codes EVS does not know
    are left out."""

    known = [project(each.body, keys) for each in recordings if each.status == HTTPStatus.OK]
    return known[1:] + known[:1]
