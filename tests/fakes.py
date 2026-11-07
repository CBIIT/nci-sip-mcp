"""Shared test doubles. Nothing here touches the network."""

from copy import deepcopy
from urllib.parse import urlencode

from nci_si_mcp.config import DEFAULT_EXCLUSION_ROLE_CODES
from nci_si_mcp.evs import INDEX_INCLUDE, LOOKUP_INCLUDE, EVSNotFoundError, verify_content
from nci_si_mcp.release import ReleaseContext
from nci_si_mcp.ssis import CADSR_GRAPH, NCIT_GRAPH, SSISClient


class FakeIndex:
    """In-memory read snapshots; no storage or operator API."""

    def __init__(self, manifest, hits):
        self.manifest = manifest
        self.hits = hits

    def get_active_manifest(self):
        return self.manifest

    def verify_active(self, provider):
        return self.manifest

    def get_concept_snapshot(self, code):
        concept = next((hit.concept for hit in self.hits if hit.concept.code == code), None)
        return self.manifest, concept

    def search_page(
        self,
        query,
        embedding_provider,
        limit=10,
        mode="hybrid",
        *,
        requested_release=None,
        offset=0,
        build_id=None,
        retired_status=None,
    ):
        hits = [
            hit
            for hit in self.hits
            if retired_status in (None, hit.concept.raw.get("conceptStatus"))
        ]
        return hits[offset : offset + limit], len(hits), self.manifest


class FakeSSIS(SSISClient):
    """Offline graph rows; records the actual remaining query allowance."""

    def __init__(self, settings):
        super().__init__(settings)
        self.graphs = [
            {"graph": NCIT_GRAPH, "version": "26.06e", "date": "June 29, 2026"},
            {"graph": CADSR_GRAPH, "date": "2026-06-01"},
        ]
        self.elements = []
        self.values = []
        self.calls = []

    def get_graph_identities(self):
        self.calls.append(("identities",))
        return deepcopy(self.graphs)

    def find_data_elements(self, concept_code, *, expand_descendants=False, maximum=1000):
        self.calls.append(("elements", concept_code, expand_descendants, maximum))
        return deepcopy(self.elements[: maximum + 1])

    def find_permissible_values(self, concept_code, *, expand_descendants=False, maximum=1000):
        self.calls.append(("values", concept_code, expand_descendants, maximum))
        return deepcopy(self.values[: maximum + 1])


def catalogue_rows(kind, version="26.06e", terminology="ncit"):
    codes = {"role": DEFAULT_EXCLUSION_ROLE_CODES, "association": ("A1",)}
    return [
        {"code": code, "name": code, "terminology": terminology, "version": version}
        for code in codes[kind]
    ]


# Fields EVS returns only when the `include` parameter asks for them.
OPTIONAL_FIELDS = frozenset(
    {
        "definitions",
        "synonyms",
        "properties",
        "maps",
        "parents",
        "children",
        "roles",
        "inverseRoles",
        "associations",
        "inverseAssociations",
    }
)


def release(version="26.06e", date="2026-06-29", channel="monthly", terminology="ncit"):
    return ReleaseContext(
        terminology=terminology,
        channel=channel,
        version=version,
        date=date,
        pinned_terminology=f"{terminology}_{version}",
    )


def terminology_row(version="26.06e", date="2026-06-29", latest=True, **tags):
    """A row of EVS's terminology listing; the tags default to the monthly channel."""

    return {
        "terminology": "ncit",
        "version": version,
        "date": date,
        "name": f"NCI Thesaurus {version}",
        "terminologyVersion": f"ncit_{version}",
        "latest": latest,
        "tags": tags or {"monthly": "true"},
    }


def concept(code, name=None, version="26.06e", **fields):
    """An EVS concept payload; pass optional fields such as `children=[...]` by name."""

    payload = {
        "code": code,
        "name": name or f"Concept {code}",
        "terminology": "ncit",
        "version": version,
    }
    payload.update(fields)
    return payload


def terminology_row_matches(row, terminology, latest, tag):
    """Whether a listing row passes the filters a listing request names."""

    return (
        terminology in (None, row["terminology"])
        and (row["latest"] or not latest)
        and (tag is None or row["tags"].get(tag) == "true")
    )


class FakeEVS:
    """In-memory stand-in for EVSClient that records the calls it receives.

    `calls` holds (method, terminology, argument) tuples and `includes` the
    include string of each concept request. Like EVS, a concept request returns
    only the optional fields its include string names, and a batch request
    returns only the known concepts. EVS keeps no order in a batch, so the fake
    answers the known concepts in request order rotated by one: code that pairs
    answers with requests by position fails here. Set `errors[method]` to an exception to make
    that method fail. The terminology listing is `rows` when set, otherwise the one latest
    monthly row of `release`; like EVS it honours the `terminology`, `latest` and `tag` filters,
    and it is read afresh on every call.
    """

    def __init__(self, concepts=(), version="26.06e", descendants=None):
        self.release = release(version)
        self.rows = None
        self.concepts = {item["code"]: item for item in concepts}
        self.descendants = descendants or {}
        self.errors = {}
        self.calls = []
        self.includes = []
        self.catalogues = None
        self.max_response_bytes = 1_000_000

    def uri(self, path, params=None):
        query = "?" + urlencode(params, doseq=True) if params else ""
        return f"https://evs.test{path}{query}"

    def _record(self, method, terminology=None, argument=None):
        self.calls.append((method, terminology, argument))
        if method in self.errors:
            raise self.errors[method]

    def _concept(self, code, include):
        wanted = set(include.split(","))
        if "summary" in wanted:
            wanted |= {"definitions", "synonyms", "properties"}
        dropped = OPTIONAL_FIELDS - wanted
        return {key: value for key, value in self.concepts[code].items() if key not in dropped}

    def get_api_version(self):
        self._record("get_api_version")
        return {"version": "test"}

    def get_relationship_catalogue(self, release, kind):
        self._record("get_relationship_catalogue", release.pinned_terminology, kind)
        rows = (
            catalogue_rows(kind, release.version, release.terminology)
            if self.catalogues is None
            else self.catalogues[kind]
        )
        verify_content(rows, release)
        return rows

    def get_terminologies(self, terminology=None, *, latest=False, tag=None):
        self._record("get_terminologies", terminology, (latest, tag))
        rows = self.rows
        if rows is None:
            rows = [
                terminology_row(
                    self.release.version, self.release.date, **{self.release.channel: "true"}
                )
            ]
        matching = (terminology_row_matches(row, terminology, latest, tag) for row in rows)
        return [row for row, kept in zip(rows, matching, strict=True) if kept]

    def get_concept(self, code, release, include=LOOKUP_INCLUDE):
        self._record("get_concept", release.pinned_terminology, code)
        self.includes.append(include)
        if code not in self.concepts:
            raise EVSNotFoundError(f"{code} not found")
        raw = self._concept(code, include)
        verify_content([raw], release)
        return raw

    def get_concepts_by_codes(self, codes, release, include=INDEX_INCLUDE):
        codes = list(codes)
        self._record("get_concepts_by_codes", release.pinned_terminology, codes)
        self.includes.append(include)
        known = [
            self._concept(code, include) for code in dict.fromkeys(codes) if code in self.concepts
        ]
        verify_content(known, release)
        return known[1:] + known[:1]

    def get_descendants(self, code, max_level, release):
        self._record("get_descendants", release.pinned_terminology, (code, max_level))
        return [item for item in self.descendants.get(code, []) if item["level"] <= max_level]


def code_map(identifier="123", **extra):
    return {
        "CDE Public ID": identifier,
        "Version": "2",
        "CRDC Name": "Map",
        "Used By": "CRDC, CIP, ",
        "permissibleValues": [
            {"Permissible Value": "A", "Concept Code": "C1:C2"},
            {"Permissible Value": "B", "Concept Code": None},
        ],
    } | extra


def data_element(public_id="123", **extra):
    return {
        "publicId": public_id,
        "version": "2",
        "longName": "Contract fixture",
        "context": "TEST",
        "workflowStatus": "RETIRED ARCHIVED",
        "registrationStatus": "Application",
        "dateCreated": "2020-01-01",
        "dateModified": "2026-08-25",
        **extra,
    }


def primary_element(value="To a large extent", codes=None, **fields):
    """Contract-crafted metadata, not a recording of CDE 2178439."""
    return data_element(
        fields.pop("publicId", "2178439"),
        ValueDomain={
            "PermissibleValues": [
                {
                    "publicId": "123",
                    "value": value,
                    "ValueMeaning": {
                        "publicId": "456",
                        "version": "1",
                        "longName": "Crafted value meaning",
                        "Concepts": codes
                        if codes is not None
                        else [
                            {
                                "conceptCode": "C49508",
                                "evsSource": "NCI_CONCEPT_CODE",
                                "primaryIndicator": "No",
                                "displayOrder": "1",
                            },
                            {
                                "conceptCode": "C25332",
                                "evsSource": "NCI_CONCEPT_CODE",
                                "primaryIndicator": "Yes",
                                "displayOrder": "0",
                            },
                            {
                                "conceptCode": "C65107",
                                "evsSource": "NCI_CONCEPT_CODE",
                                "primaryIndicator": "No",
                                "displayOrder": "2",
                            },
                        ],
                    },
                }
            ]
        },
        **fields,
    )


def form(**extra):
    raw = data_element()
    return (
        {k: v for k, v in raw.items() if k != "publicId"}
        | {
            "publicID": "123",
            "modules": [{"name": "Module", "questions": [{"text": "Q"}]}],
        }
        | extra
    )


def cde_row(identifier="123", **extra):
    return data_element(identifier, score=0.75, ruleDescription="Exact", matchedText="Q") | extra


def cde_response(name="Q", matches=None):
    return {"matchResults": {"entity": name, "matches": [] if matches is None else matches}}


def vm_row(**extra):
    return {
        "itemType": "Concept",
        "itemId": "123",
        "version": "2",
        "matchedName": "Value",
        "ruleDescription": "Exact",
        "concept": "C123",
        "evsSource": "NCI_CONCEPT_CODE",
        "context": "TEST",
        "workflowStatus": "RETIRED ARCHIVED",
        "registrationStatus": None,
        "crosswalkCode": "NA",
        "crosswalkDescription": "NA",
        **extra,
    }


def vm_response(name="Q", matches=None):
    return {"matchResults": [{"name": name, "matches": [] if matches is None else matches}]}


def selected_environment():
    """Independent recorded policy input shared by benchmark record and evidence contracts."""
    return {
        "name": "evs-only",
        "origins": {
            "evs": "https://example.test/api",
            "evs-fhir": "https://example.test/fhir/r4",
            "cadsr": None,
            "cadsr-ftp": None,
            "cadsr-contracts": None,
            "ssis": None,
            "ssis-sparql": None,
        },
        "policy_sha256": "a" * 64,
    }
