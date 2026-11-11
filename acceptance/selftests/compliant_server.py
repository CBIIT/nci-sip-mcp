"""A server of the evs profile that meets the protocol gates and the cross-cutting
requirements, for the harness's own tests.

`COMPLIANT_SERVER_DEFECT` names one defect, so that a test can show the test concerned failing:

    missing-tool      lists one tool of the profile too few                       (P-1)
    no-error-shape    an outputSchema that refuses the error record                 (P-2)
    declares-no-shape an outputSchema that admits any object                        (P-2)
    one-code          an outputSchema whose error code admits one value only        (P-2)
    placeholder       a description holding TODO                                    (P-3)
    generated-title   an outputSchema titled with a type expression, RootModel[...]  (P-3)
    misnamed          a tool whose name is not verb-led and lowercase               (P-4)
    no-ttl            tools/list with ttlMs 0                                       (P-5)
    concept-empty     concept lookup succeeds with no identity                      (get_concept-3)
    concept-error     concept lookup returns an upstream failure                    (get_concept-3)
    concept-no-name   concept lookup omits its name                                 (get_concept-3)
    concept-wrong-uri concept provenance names another concept's URI                (X-7)
    concept-null-provenance concept provenance is null                              (X-7)
    concept-list-provenance concept provenance is a list                            (X-7)
    listing-changes   tools/list loses a tool after the first call                  (P-6)
    redescribed       a tool's description changes after the first call             (P-6)
    hides-tools       tools/list loses a tool once a call finds the platform down   (P-6)
    no-correlation    the correlation identifier is not sent upstream               (P-7)
    destructive       destructiveHint true                                          (P-10)
    not-idempotent    idempotentHint false                                          (P-10)
    closed-world      openWorldHint false                                           (P-10)
    parameter-renamed get_concept takes conceptCode in place of code                (P-12)
    release-required-schema get_concept still requires the now-optional release    (P-12)
    quotes-not-offered a description that assigns a value not offered in backticks (P-11)
    lists-not-offered an input schema whose enum holds a value not offered          (P-11)
    patterns-not-offered an input schema whose pattern admits a value not offered   (P-11)
    wrong-release     items name another release than the one requested             (X-1)
    wrong-terminology items name another terminology than the one requested         (X-1)
    invalid-result    a result its outputSchema refuses                             (X-6)
    no-served-by      provenance without servedBy                                   (X-7)
    no-correlation   provenance without a generated correlation id                  (X-7)
    wrong-correlation provenance ignores the caller's correlation id                (X-7)
    no-source-uri    a live concept without its upstream URL                        (X-7)
    no-upstream      a live concept without the origin its answer supplied          (X-7)
    uri-not-a-url    a live concept whose sourceUri is text, not an absolute URL    (X-7)
    live-as-index    a live response claims index origin to evade its obligations   (X-7)
    cache-loses-origin a repeated live answer loses its original source facts       (X-7)
    cache-swaps-origin a repeated live answer names another valid source URI        (X-7)
    itemless-origin  an empty page invents an item's upstream origin                (X-7)
    bad-timestamp     a retrievedAt that is no ISO-8601 timestamp                   (X-7)
    no-polarity       an item reached by traversal without polarity                 (X-7)
    nothing-reached   a traversal whose items are all at depth 0                    (X-7)
    prefixed-code     codes written NCIT:C4817                                      (X-9)
    repeated-request  each call asks EVS twice for the same thing                   (X-11)
    asks-nothing      calls that ask EVS nothing                                    (X-11)
    uncached-result   results with ttlMs 0                                          (X-13)
    private-scope     results with cacheScope private                               (X-13)
    list-result       a result that is a bare list of its items                     (X-14)
    unknown-as-empty  an unknown release answered as an empty success               (X-2)
    accepts-mismatch  content of another release served as asked                    (X-3)
    outage-as-empty   an unavailable EVS answered as an empty success               (X-5)
    unshaped-error    an error that is no error record                              (X-6)
    keyless           the licence key is not sent                                   (X-12)
    logs-key          the request headers, licence key included, written to the log (X-12)
    leaks-key         EVS's refusal, which repeats the request headers, in the error (X-12)
    files-key         the request headers written to a file in the data directory  (X-12)
    key-in-text       the licence key in a success's text block                     (X-12)
    key-in-meta       the licence key in every result's _meta                       (X-12)
    content-with-error content beside the error record                              (X-2)
    empty-as-error    a query that matches nothing answered as not_found            (X-4)
    empty-without-provenance an empty result without its provenance                 (X-4)
    upstream-renamed  EVS's origin fields passed on under other names               (X-8)
    truncation-flag-only a bound reached reported as occurred and nothing else      (X-10)
    omitted-unknown   how much was left out given as "unknown"                      (X-10)
    exact-missing     a bound reached reported without exact                        (X-10)
    reached-over      more counted against a bound than its limit                  (X-10)
    upstream-altered  EVS's origin fields passed on with other values               (X-8)
    gives-up          no retry after a 429                                          (X-16)
    no-backoff        a retry after a 429 without the wait it asks for              (X-16)
    no-next-cursor    a page smaller than the result without nextCursor            (X-17)
    cursor-repeats    the cursor's page repeats the page before                     (X-17)
    cursor-other-release the cursor's page names another release                    (X-17)
    cursor-empty      the cursor's page is empty                                    (X-17)
    cursor-ignores-release a cursor presented with another release is served        (X-17)
    cursor-offset-only a cursor presented with other arguments is served            (X-17)
    cursor-refuses-default a cursor presented with a default given is refused       (X-17)
    cursor-inherits   a cursor fills arguments left out from the first call          (X-17)
    wrong-default     a left-out argument with a stated default served otherwise    (X-20)
    empty-with-cursor a query that matches nothing answered with nextCursor          (X-4)
    raised-to-one     a bounded argument below one served as one                    (X-18)
    release-refused   an omitted NCIt release is refused                            (X-22)
    implicit-public   an implicit release gets a positive public cache hint        (X-22)
    session-reresolves implicit calls rediscover instead of holding a session pin  (X-22)
    session-overwrite an explicit release overwrites the implicit session pin      (X-22)
    session-withdrawal-empty a withdrawn session release becomes an empty success  (X-22)
    unchecked-identifiers an identifier off its stated form served as given          (X-23)
    checks-after-asking an identifier off its form refused only after a request carried it (X-23)
    unencoded-code    a code put into the path as it is, a slash in it included     (X-23)
    asks-in-path-then-refuses an identifier off its form sent in a path, then refused (X-23)
    asks-in-form-then-refuses an identifier off its form sent in a form, then refused (X-23)
    unencoded-text    free text put into the query string as it is, but for spaces  (X-24)
    text-twice        free text sent as its own field and inside another             (X-24)
    joins-listing     a licensed item with the listing's licence text, given or not (X-19)
    attribution-everywhere every item with the listing's licence text, NCIt's included (X-19)
    drops-attribution the licence text EVS gives with an item left out             (X-19)
    alters-attribution the licence text EVS gives with an item cut short            (X-19)
    attributes-all-but-last the licence text EVS gives left off the last item         (X-19)
    attributes-ncit   NCIt items with a licence text of the server's own            (X-19)
    sticky-attribution an item EVS gave no text with carries the last text it gave   (X-19)
    no-prompts        no prompts capability: prompts/list is a method not found      (P-8)
    prompt-outside-profile a prompt, naming tools the profile lacks, in a profile with none (P-8)
    prompt-missing    one prompt of the profile not listed                          (P-8)
    argument-undeclared a prompt whose first argument prompts/list does not declare (P-8)
    prompt-omits-tool the last tool a prompt states left out of its messages         (P-8)
    prompt-empty      prompts/get returns no message                                (P-8)
    no-resources      no resources capability: resources/list is a method not found (P-8)
    uri-differs       the concept's URI template listed without its release          (P-8)
    resource-no-ttl   resources/read results without ttlMs and cacheScope           (P-9)
    resource-no-provenance resource content without its provenance                  (P-9)
    resource-other-content resource content of another concept than the one read    (P-9)
    resource-wrong-mime resource content in another MIME type                       (P-9)
    resource-ignores-version cadsr://data-element/{publicId}/{version} gives the latest (P-9)
    crosswalk-cut     the crosswalk resource cut at 100 code maps                   (P-9)
    manifest-other-release the manifest names another release than the one asked    (P-9)
    registry-identifier the registry resource names a registry identifier, in its body
                      and in its provenance's release                                (P-9)
    resource-private-scope resource hints with cacheScope private                   (P-9)
    resource-other-ttl resource hints of the other class: 0 for pinned, long for unpinned (P-9)
    resource-ttl-in-meta the hint in the result's _meta, not in its fields          (P-9)
    bad-timestamp     (also) a retrievedAt in a resource's provenance that is no timestamp (P-9)
    manifest-no-embedding the manifest without its embedding                        (P-9)
    manifest-unbuilt  a manifest of no concepts, built at no time                   (P-9)
    unmatched-uri-content a URI no template matches answered with content           (P-9)
    templates-as-resources templates listed by resources/list, none by templates/list (P-8)
    prompt-reordered  the first two tools a prompt states named in the other order   (P-8)
    prompts-no-ttl    prompts/list with ttlMs 0                                     (P-5)
    resources-no-ttl  resources/list with ttlMs 0                                   (P-5)
    templates-no-ttl  resources/templates/list with ttlMs 0                         (P-5)
    lists-private     the four lists with cacheScope private                        (P-5)
    cadsr-unrelated-failure data element lookup sends Accept but fails all the same   (X-15)
    cadsr-no-accept   data element lookup leaves Accept out and reports the HTML    (X-15)
    cadsr-html-accepted data element lookup leaves Accept out and succeeds on HTML  (X-15)

`COMPLIANT_SERVER_CADSR=1` makes a data element lookup ask caDSR (`NCI_SI_CADSR_BASE_URL`) for
`/NCIAPI/1.0/api/DataElement/{publicId}`, naming Accept: application/json, and answer
upstream_unavailable where the body is no data element (HTML); without it every call asks EVS.

`COMPLIANT_SERVER_PAGE_SIZE` makes prompts/list, resources/list and resources/templates/list
page, that many items to a page with a nextCursor: a server that pages passes all the same (M6.1).

`COMPLIANT_SERVER_TRANSPORT=http` serves streamable HTTP on 127.0.0.1 at `COMPLIANT_SERVER_PORT`
in place of stdio, as a remote server. It answers 401 unless the Authorization header is
`COMPLIANT_SERVER_AUTHORIZATION` (where that is set), and appends the header of each request to
the file `COMPLIANT_SERVER_AUTH_LOG` names.

`COMPLIANT_SERVER_NO_CACHE=1` makes the server ask EVS again for a call it has answered.

`COMPLIANT_SERVER_PROFILE` names the profile the server serves, evs by default: the tools it
lists, the resources it serves and the prompts it lists (those whose every tool the profile
has, so none in evs). A resource's content is its tool's answer, called as spec/resources.yaml
says; a prompt's messages are its template with the arguments filled in.

`unpinned-mismatch` is no defect for a tool without a pinned form upstream: it answers an
unknown release with release_mismatch, as such a tool can only verify an unpinned answer (X-2).

A call asks EVS for `/api/v1/version`, or for the concept of a licensed terminology (or its
search, for a call that names no code) with the licence key, unless the same call was
answered before: the server caches by call, as A9.4 allows. An answer that is not content
is an error record: 404 release_not_available, another version than the release asked for
release_mismatch, a timeout timeout, anything else
upstream_unavailable; a 429 is waited out once. Content has items where the tool's `items`
say: the concept asked about and, at depth 1 for a traversal tool, one it reaches. What the
suite's calls (tests/calls.yaml) say of EVS's answers shapes it: their `upstream` fields go
into provenance, their `empty` arguments match nothing, their `truncating` arguments
reach a bound, and under their `paged` arguments a first page carries a cursor to a second,
of other concepts; the cursor carries the arguments it was issued for, as applied (an
optional argument left out as the default the specification states), and presented with
others it is refused. A bounded argument below one is refused, and an item carries the licence
text EVS's answer gives with the concept asked for, and none of its own.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

import anyio
import mcp_types as types
import uvicorn
import yaml
from mcp.server.caching import CacheHint
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from starlette.responses import PlainTextResponse

from nci_si_acceptance.results import EXPORT, SHORT_TTL, UNPINNED_REGISTRY
from nci_si_acceptance.spec import (
    PROMPTS,
    RECORDS,
    RESOURCES,
    TOOLS,
    defaults,
    parameters,
    profile_tools,
    prompts_of,
    resource_call,
    resources_of,
    uri_variables,
)

DEFECT = os.environ.get("COMPLIANT_SERVER_DEFECT", "")
PROFILE = os.environ.get("COMPLIANT_SERVER_PROFILE", "evs")
PAGE_SIZE = int(os.environ.get("COMPLIANT_SERVER_PAGE_SIZE", "0"))
TRANSPORT = os.environ.get("COMPLIANT_SERVER_TRANSPORT", "stdio")
PORT = int(os.environ.get("COMPLIANT_SERVER_PORT", "0"))
# A server that remembers nothing between calls, so that a harness run after another finds it
# asking EVS again (the probe of a remote server sees a request only from a server that asks).
NO_CACHE = bool(os.environ.get("COMPLIANT_SERVER_NO_CACHE"))
AUTHORIZATION = os.environ.get("COMPLIANT_SERVER_AUTHORIZATION")
AUTHORIZATION_LOG = os.environ.get("COMPLIANT_SERVER_AUTH_LOG")
EVS = os.environ["NCI_SI_EVS_BASE_URL"]
CADSR = (
    os.environ.get("NCI_SI_CADSR_BASE_URL") if os.environ.get("COMPLIANT_SERVER_CADSR") else None
)
TIMEOUT = float(os.environ.get("NCI_SI_TIMEOUT_SECONDS", "10"))
LICENCE_KEY = os.environ.get("NCI_SI_EVS_LICENSE_KEY")
LICENSED = {"mdr"}
# The licensed placeholder concept and its child (license/restricted, license/attributed).
LICENSED_CODES = ["10000000", "10000001"]
# What an upstream request that got no HTTP answer counts as.
CLOSED, TIMED_OUT = 0, -1
# How an unknown release is reported: by a tool without a pinned form, as a mismatch (X-2).
UNKNOWN_RELEASE = "release_mismatch" if DEFECT == "unpinned-mismatch" else "release_not_available"
# A failure the defect turns into an empty success.
SWALLOWED = {"unknown-as-empty": "release_not_available", "outage-as-empty": "upstream_unavailable"}
# A request body, where there is one.
type Body = bytes | None
# The release EVS serves as current, the caDSR export's date, the latest version of data
# element 2200604 (an older one is 1), and the data elements of the crosswalk today.
CURRENT, EXPORT_DATE, LATEST_VERSION, CODE_MAPS = "26.09d", "2026-07-01", "4", 105
# A registry release caDSR does not publish, which the registry-identifier defect names.
UNPUBLISHED = "2026.07.02"
# The ttlMs of release-pinned content (long); SHORT_TTL is that of content no release pins.
LONG_TTL = 86_400_000
# The suite's calls (tests/calls.yaml): what each tool's upstream answers would say.
CALLS = yaml.safe_load((Path(__file__).parent.parent / "tests" / "calls.yaml").read_text())
# Whether each call so far reached EVS, and EVS's answer to each call already answered.
reached = []
answered: dict[str, dict] = {}
session_state: dict[str, str] = {}
# The licence texts EVS gave so far (the sticky-attribution defect).
given: list[str] = []


def _json(raw: bytes) -> dict:
    """A body as JSON; one that is not (caDSR's HTML) as an empty object."""

    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def _ask(
    path: str,
    headers: dict[str, str],
    body: bytes | None = None,
    method: str = "GET",
    base: str = EVS,
) -> tuple[int, dict, dict]:
    """One request to EVS (or `base`): its status (or CLOSED, TIMED_OUT), body and headers."""

    request = urllib.request.Request(base + path, body, headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, _json(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"{}"), dict(error.headers)
    except OSError as error:
        # A timeout comes as itself, or inside a URLError as its reason.
        reason = getattr(error, "reason", error)
        return (TIMED_OUT if isinstance(reason, TimeoutError) else CLOSED), {}, {}


def _request(name: str, arguments: dict, correlation: str) -> tuple[str, dict[str, str], Body]:
    """The path a call asks EVS for, its headers, and its free text as a JSON body (A7.7): for
    NCIt the release query; for another terminology the concept, its code one encoded segment
    (A7.6), with the licence key where the terminology is licensed."""

    headers = {} if DEFECT == "no-correlation" else {"X-Correlation-ID": correlation}
    terminology = arguments.get("terminology")
    query, body = _texts(name, arguments, headers)
    if terminology in (None, "ncit"):
        return "/api/v1/version" + query, headers, body
    if terminology in LICENSED:
        _licensed_headers(headers)
    # The concept a call names, the first of the codes it names, or else the search.
    code = arguments.get("code") or next(iter(arguments.get("codes", [])), "search")
    segment = code if DEFECT == "unencoded-code" else quote(code, safe="")
    path = f"/api/v1/concept/{terminology}_{arguments.get('release')}/{segment}{query}"
    return path, headers, body


def _licensed_headers(headers: dict[str, str]) -> None:
    if LICENCE_KEY and DEFECT != "keyless":
        headers["X-EVSRESTAPI-License-Key"] = LICENCE_KEY
    _leak(headers)


def _texts(name: str, arguments: dict, headers: dict[str, str]) -> tuple[str, Body]:
    """The call's free-text arguments as a query string and a body: a JSON body of one field
    each, sent with the request (a GET, as EVS's fixtures are asked)."""

    texts = {
        key: arguments[key]
        for key in TOOLS[name].get("free_text", [])
        if isinstance(arguments.get(key), str)
    }
    if not texts:
        return "", None
    if DEFECT == "unencoded-text":
        query = "&".join(f"{key}={text.replace(' ', '%20')}" for key, text in texts.items())
        return "?" + query, None
    if DEFECT == "text-twice":
        texts |= {"filter": "name:" + next(iter(texts.values()))}
    headers["Content-Type"] = "application/json"
    return "", json.dumps(texts).encode()


def _leak(headers: dict[str, str]) -> None:
    """The request headers, licence key included, written where the defect says (X-12)."""

    if DEFECT == "logs-key":
        sys.stderr.write(f"asking with {headers}\n")
        sys.stderr.flush()
    if DEFECT == "files-key":
        (Path(os.environ["NCI_SI_DATA_DIR"]) / "requests.txt").write_text(str(headers))


def _asked(name: str, arguments: dict, correlation: str) -> tuple[int, dict]:
    """EVS's answer to a call, after one wait and retry on 429 (A6.5)."""

    if CADSR and name == "get_data_element":
        return _asked_cadsr(arguments)
    path, headers, sent = _request(name, arguments, correlation)
    status, body, answer_headers = _ask(path, headers, sent)
    if status == HTTPStatus.TOO_MANY_REQUESTS and DEFECT != "gives-up":
        time.sleep(0 if DEFECT == "no-backoff" else float(answer_headers.get("Retry-After", 0)))
        status, body, _ = _ask(path, headers, sent)
    if DEFECT == "repeated-request":
        _ask(path, headers, sent)
    reached.append(status == HTTPStatus.OK)
    return status, body


def _asked_cadsr(arguments: dict) -> tuple[int, dict]:
    """caDSR's answer to a data element lookup, which asks for JSON (X-15) unless the defect
    leaves Accept out; caDSR answers any other request with HTML, which is no answer."""

    leaves_out = DEFECT in ("cadsr-no-accept", "cadsr-html-accepted")
    headers = {} if leaves_out else {"Accept": "application/json"}
    status, body, _ = _ask(
        f"/NCIAPI/1.0/api/DataElement/{arguments['publicId']}", headers, base=CADSR
    )
    unusable = not body and DEFECT != "cadsr-html-accepted"
    return (
        HTTPStatus.BAD_GATEWAY if unusable or DEFECT == "cadsr-unrelated-failure" else status
    ), body


def _failure(status: int, body: dict, arguments: dict) -> str | None:
    """The error code of EVS's answer, or None for content of the release asked for."""

    if status == TIMED_OUT:
        return "timeout"
    if status == HTTPStatus.NOT_FOUND:
        return UNKNOWN_RELEASE
    if status != HTTPStatus.OK:
        return "upstream_unavailable"
    return _mismatch(body, arguments)


def _mismatch(body: dict, arguments: dict) -> str | None:
    asked = arguments.get("release")
    mismatched = asked is not None and body.get("version", asked) != asked
    return "release_mismatch" if mismatched and DEFECT != "accepts-mismatch" else None


def _input_schema(name: str) -> dict:
    names, required = parameters(name)
    if DEFECT == "parameter-renamed" and name == "get_concept":
        names, required = names - {"code"} | {"conceptCode"}, required - {"code"} | {"conceptCode"}
    if DEFECT == "release-required-schema" and name == "get_concept":
        required |= {"release"}
    listed = {
        "lists-not-offered": {"enum": _not_offered(name)},
        "patterns-not-offered": {"pattern": f"^({'|'.join(_not_offered(name))})$"},
    }.get(DEFECT, {})
    return {
        "type": "object",
        "properties": {key: dict(listed) for key in names},
        "required": sorted(required),
    }


# A result is either an error record or a success.
CODES = RECORDS["error"]["fields"]["code"]["values"]
ERROR = {
    "type": "object",
    "required": ["code", "message"],
    "properties": {"code": {"enum": CODES[:1] if DEFECT == "one-code" else CODES}},
}
# A success is anything but an error record, so that a list result reaches the suite (X-14).
OUTPUT_SCHEMAS = {
    "": {
        "oneOf": [
            {"type": "object", "required": ["error"], "properties": {"error": ERROR}},
            {"not": {"type": "object", "required": ["error"]}},
        ],
    },
    "no-error-shape": {"type": "object", "required": ["provenance"]},
    "declares-no-shape": {"type": "object"},
    "generated-title": {
        "title": "RootModel[Annotated[Union[Concept, ErrorResult], FieldInfo(required=True)]]",
        "type": "object",
        "oneOf": [
            {"type": "object", "required": ["error"], "properties": {"error": ERROR}},
            {"not": {"type": "object", "required": ["error"]}},
        ],
    },
}


def _description(name: str) -> str:
    if DEFECT == "placeholder":
        return "TODO"
    when = " Called before." if DEFECT == "redescribed" and reached else ""
    # What the tool does not offer may be said in words, never shown as a value (P-11).
    lacking = "".join(f" It does not offer {value}." for value in _not_offered(name))
    if DEFECT == "quotes-not-offered":
        lacking = "".join(f" Try `key={value}`." for value in _not_offered(name))
    return f"The {name} tool of EVS.{when}{lacking}"


def _not_offered(name: str) -> list[str]:
    return [value for values in TOOLS[name].get("not_offered", {}).values() for value in values]


def _tool(name: str) -> types.Tool:
    return types.Tool(
        name=name,
        description=_description(name),
        input_schema=_input_schema(name),
        output_schema=OUTPUT_SCHEMAS.get(DEFECT, OUTPUT_SCHEMAS[""]),
        annotations=types.ToolAnnotations(
            read_only_hint=True,
            destructive_hint=DEFECT == "destructive",
            idempotent_hint=DEFECT != "not-idempotent",
            open_world_hint=DEFECT != "closed-world",
        ),
    )


def _names() -> list[str]:
    names = sorted(profile_tools(PROFILE))
    if DEFECT == "misnamed":
        names.append("ConceptLookup")
    dropped = {
        "missing-tool": True,
        "listing-changes": bool(reached),
        "hides-tools": False in reached,
    }
    return names[1:] if dropped.get(DEFECT) else names


async def list_tools(_context, _params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[_tool(name) for name in _names()])


def _retrieved() -> str:
    return "today" if DEFECT == "bad-timestamp" else datetime.now(UTC).isoformat()


def _provenance(name: str, arguments: dict, correlation: str, answer: dict) -> dict:
    release = "26.08e" if DEFECT == "wrong-release" else arguments.get("release")
    terminology = "mdr" if DEFECT == "wrong-terminology" else arguments.get("terminology")
    provenance = {
        "release": {"terminology": terminology, "identifier": release},
        "source": "evs_rest",
        "retrievedAt": _retrieved(),
        "servedBy": "live",
        "correlationId": correlation,
        "upstream": _upstream(name, arguments),
        "sourceUri": "https://example.invalid/api/v1/version",
    }
    omitted = {
        "no-served-by": "servedBy",
        "no-correlation": "correlationId",
        "no-upstream": "upstream",
    }.get(DEFECT)
    if omitted:
        del provenance[omitted]
    if DEFECT == "wrong-correlation":
        provenance["correlationId"] = "not-the-callers-correlation"
    if DEFECT == "live-as-index":
        provenance.update(source="evs_index", servedBy="index")
        provenance.pop("upstream", None)
    return provenance | _attribution(terminology, answer)


def _attribution(terminology: str | None, answer: dict) -> dict:
    """The licence text EVS's answer gives with the concept asked for, as the item's
    attribution (X-19)."""

    text = _remembered(_own_text(terminology) or _given_text(answer))
    if not text or DEFECT == "drops-attribution":
        return {}
    return {"attribution": text[:40] if DEFECT == "alters-attribution" else text}


def _own_text(terminology: str | None) -> str | None:
    """The licence text a defect attaches where EVS gave none."""

    if DEFECT == "attributes-ncit" and terminology not in LICENSED:
        return "Licensed by this server."
    joined = DEFECT == "joins-listing" and terminology in LICENSED
    return _licence_text(LICENSED) if joined or DEFECT == "attribution-everywhere" else None


def _remembered(text: str | None) -> str | None:
    """`text`; under the sticky defect, the last text given where there is none."""

    if text:
        given.append(text)
        return text
    return given[-1] if DEFECT == "sticky-attribution" and given else None


def _given_text(answer: dict) -> str | None:
    """The licence text EVS gives with the concept, or the first concept of a search."""

    return (answer.get("concepts") or [answer])[0].get("licenseText")


def _licence_text(terminologies: set[str]) -> str:
    """The licence text the terminology listing gives the first of `terminologies`."""

    _, rows, _ = _ask("/api/v1/metadata/terminologies", {})
    return next(
        row["metadata"]["licenseText"] for row in rows if row["terminology"] in terminologies
    )


def _upstream(name: str, arguments: dict) -> dict:
    """What EVS says of the origin of the call's items, as the suite's calls list it, the
    pin (`$terminology`, `$release`) taken from the call."""

    listed = CALLS.get(name, {}).get(
        "upstream", {"terminology": "$terminology", "version": "$release"}
    )
    supplied = {
        key: arguments.get(value[1:]) if str(value).startswith("$") else value
        for key, value in listed.items()
    }
    if DEFECT == "upstream-renamed":
        return {f"evs{key.title()}": value for key, value in supplied.items()}
    if DEFECT == "upstream-altered":
        return {key: f"{value}+" for key, value in supplied.items()}
    return supplied


def _matches_nothing(name: str, arguments: dict) -> bool:
    empty = CALLS.get(name, {}).get("empty")
    return bool(empty) and all(arguments.get(key) == value for key, value in empty.items())


def _truncation(name: str, arguments: dict) -> dict:
    """The truncation field of a tool that bounds its result: a bound reached where the call
    sets one of the suite's truncating arguments."""

    if name == "get_code_map":
        return _code_map_cut(arguments)
    if "truncation" not in TOOLS[name]["returns"]:
        return {}
    truncating = CALLS.get(name, {}).get("truncating", {"arguments": {}})
    limits = [
        value for key, value in truncating["arguments"].items() if arguments.get(key) == value
    ]
    if not limits:
        return {"truncation": {"occurred": False}}
    return {"truncation": _reached(truncating["bound"], limits[0])}


def _code_map_cut(arguments: dict) -> dict:
    """The truncation of the crosswalk's code maps, cut at the call's limit."""

    limit = arguments.get("limit", defaults("get_code_map")["limit"])
    if limit >= CODE_MAPS:
        return {"truncation": {"occurred": False}}
    cut = {"occurred": True, "bound": "results", "limit": limit, "reached": limit}
    return {"truncation": cut | {"omitted": CODE_MAPS - limit, "exact": True}}


def _reached(bound: str, limit: int) -> dict:
    """The truncation record of a bound reached, with one item left out."""

    if DEFECT == "truncation-flag-only":
        return {"occurred": True}
    reached = limit + 1 if DEFECT == "reached-over" else limit
    record = {"occurred": True, "bound": bound, "limit": limit, "reached": reached}
    record |= {"omitted": "unknown" if DEFECT == "omitted-unknown" else 1}
    return record | ({} if DEFECT == "exact-missing" else {"exact": True})


def _items(name: str, provenance: dict, codes: tuple[str, str] = ("C4817", "C3262")) -> list[dict]:
    """The concept `codes` names first and, for a traversal tool, the one it names second,
    reached at depth 1."""

    code, reached = codes
    code = f"NCIT:{code}" if DEFECT == "prefixed-code" else code
    items = [{"code": code, "terminology": "ncit", "provenance": provenance}]
    if TOOLS[name].get("traversal"):
        how = _how()
        items[0]["provenance"] = provenance | {"depth": 0}
        depth = 0 if DEFECT == "nothing-reached" else 1
        items.append(
            {
                "code": reached,
                "terminology": "ncit",
                "provenance": provenance | {"depth": depth} | how,
            }
        )
    return items


def _placed(steps: list[str], items: list[dict]) -> object:
    """`items` placed under `steps`: none is the first item itself, and a step ending in []
    spreads them over a list."""

    if not steps:
        return items[0]
    step, rest = steps[0], steps[1:]
    if step.endswith("[]"):
        value = [_placed(rest, [item]) for item in items]
    else:
        value = _placed(rest, items)
    return {step.removesuffix("[]"): value}


def _content(name: str, arguments: dict, correlation: str, answer: dict) -> object:
    if DEFECT == "invalid-result":
        return {"error": "not an error record"}
    provenance = _provenance(name, arguments, correlation, answer)
    items = _items_of_call(name, arguments, provenance)
    if DEFECT == "list-result":
        return items
    content = _shaped(name, items)
    if name == "get_concept":
        content = _identified_concept(content, arguments)
    # With no item to carry it, the result carries the provenance itself (M3.2).
    if not items and DEFECT != "empty-without-provenance":
        content["provenance"] = _empty_origin(name, provenance)
    return content | _truncation(name, arguments) | _next_cursor(name, arguments, items)


def _empty_origin(name: str, provenance: dict) -> dict:
    """An empty page has no item origin; a read resource may still have one."""

    if not CALLS[name].get("emptyOrigin"):
        provenance.pop("upstream", None)
    if DEFECT == "itemless-origin":
        provenance["upstream"] = {"version": "invented-item-version"}
    return provenance


def _identified_concept(content: dict, arguments: dict) -> dict:
    """A named concept for the bounded discovery-to-content acceptance journey."""
    if DEFECT == "concept-empty":
        return {}
    content |= {"name": "Ewing sarcoma", "active": True}
    terminology = arguments["terminology"]
    release = arguments["release"]
    code = quote(arguments["code"], safe="")
    source = f"https://example.invalid/api/v1/concept/{terminology}_{release}/{code}"
    content["provenance"]["sourceUri"] = source
    if DEFECT in ("no-source-uri", "live-as-index"):
        del content["provenance"]["sourceUri"]
    if DEFECT == "concept-wrong-uri":
        content["provenance"]["sourceUri"] = f"{source}-other"
    if DEFECT == "uri-not-a-url":
        content["provenance"]["sourceUri"] = "n/a"
    if DEFECT == "concept-no-name":
        del content["name"]
    malformed = {"concept-null-provenance": None, "concept-list-provenance": []}
    if DEFECT in malformed:
        content["provenance"] = malformed[DEFECT]
    return content


def _registry(provenance: dict) -> dict:
    """The provenance of a caDSR item: the registry alone is its release (X-21)."""

    return provenance | {"release": dict(UNPINNED_REGISTRY), "source": "cadsr_rest"}


def _data_element(arguments: dict, provenance: dict) -> list[dict]:
    """A data element at the version asked for, the latest by default."""

    version = arguments.get("version", LATEST_VERSION)
    public_id = arguments.get("publicId", "2200604")
    return [{"publicId": public_id, "version": version, "provenance": _registry(provenance)}]


def _code_maps(arguments: dict, provenance: dict) -> list[dict]:
    """The crosswalk's code maps, as many as the limit allows of its 105."""

    limit = arguments.get("limit", defaults("get_code_map")["limit"])
    return [
        {
            "dataElement": {"publicId": str(1000 + number), "version": "1"},
            "provenance": _registry(provenance),
        }
        for number in range(min(limit, CODE_MAPS))
    ]


def _release(_arguments: dict, provenance: dict) -> list[dict]:
    """The current release as resolve_release names it."""

    named = provenance | {"release": {"terminology": "ncit", "identifier": CURRENT}}
    record = {"terminology": "ncit", "channel": "monthly", "version": CURRENT}
    return [record | {"date": EXPORT_DATE, "alternatives": [], "provenance": named}]


def _registry_state(_arguments: dict, _provenance: dict) -> list[dict]:
    """The registry's unpinned state with the export's date; the answer has no provenance."""

    return [{"published": False, "generatedAt": EXPORT_DATE, "sourceDistribution": EXPORT}]


# The tools whose answers have the shape of their records, not a concept's.
RECORDS_OF = {
    "get_data_element": _data_element,
    "get_code_map": _code_maps,
    "resolve_release": _release,
    "resolve_registry_release": _registry_state,
}


def _items_of_call(name: str, arguments: dict, provenance: dict) -> list[dict]:
    if _matches_nothing(name, arguments):
        return []
    if name in RECORDS_OF:
        return RECORDS_OF[name](arguments, provenance)
    if arguments.get("terminology") in LICENSED:
        return _licensed_items(name, arguments, provenance)
    return _page(name, arguments, provenance)


def _licensed_items(name: str, arguments: dict, provenance: dict) -> list[dict]:
    """The licensed concepts a call asks for: its code or codes, both placeholders for a search;
    a traversal reaches the child of 10000000 (license/restricted)."""

    if TOOLS[name].get("traversal"):
        reached = provenance | {"depth": 1} | _how()
        items = [{"code": "10000001", "terminology": "mdr", "provenance": reached}]
    else:
        codes = (
            [arguments["code"]] if "code" in arguments else arguments.get("codes", LICENSED_CODES)
        )
        items = [{"code": code, "terminology": "mdr", "provenance": provenance} for code in codes]
    if DEFECT == "attributes-all-but-last":
        last = items[-1]["provenance"]
        items[-1] = items[-1] | {
            "provenance": {k: v for k, v in last.items() if k != "attribution"}
        }
    return items


def _how() -> dict:
    """How an item reached by traversal was reached."""

    how = {"relationship": {"code": "R101"}, "direction": "outward", "polarity": "positive"}
    if DEFECT == "no-polarity":
        del how["polarity"]
    return how


def _page(name: str, arguments: dict, provenance: dict) -> list[dict]:
    """The items of a call: with a cursor, those of the page after the first."""

    if DEFECT == "wrong-default" and set(defaults(name)) - set(arguments):
        return _items(name, provenance, ("C2991", "C9118"))
    if "cursor" not in arguments or DEFECT == "cursor-repeats":
        return _items(name, provenance)
    if DEFECT == "cursor-empty":
        return []
    if DEFECT == "cursor-other-release":
        provenance = provenance | {"release": provenance["release"] | {"identifier": "26.08e"}}
    return _items(name, provenance, ("C2991", "C9118"))


def _next_cursor(name: str, arguments: dict, items: list[dict]) -> dict:
    """The cursor of a paged call's first page, which is smaller than its result: it carries
    the arguments the page was asked with, as applied."""

    cursor = {"nextCursor": f"page-2@{json.dumps(_applied(name, arguments), sort_keys=True)}"}
    if DEFECT == "empty-with-cursor" and not items:
        return cursor
    first = items and _paging(name, arguments) and "cursor" not in arguments
    return cursor if first and DEFECT != "no-next-cursor" else {}


def _paging(name: str, arguments: dict) -> bool:
    """Whether a call sets the suite's paged arguments, under which a page is smaller than
    the result."""

    paged = CALLS.get(name, {}).get("paged", [])
    return any(all(arguments.get(k) == v for k, v in entry["arguments"].items()) for entry in paged)


def _applied(name: str, arguments: dict) -> dict:
    """A call's arguments as applied: an optional argument left out is its default."""

    stated = {} if DEFECT == "cursor-refuses-default" else defaults(name)
    return stated | {key: value for key, value in arguments.items() if key != "cursor"}


def _cursor_refused(name: str, arguments: dict) -> bool:
    """Whether a cursor is presented with other arguments than those it was issued for."""

    cursor = arguments.get("cursor")
    if cursor is None or DEFECT == "cursor-offset-only":
        return False
    issued, now = json.loads(cursor.partition("@")[2]), _applied(name, arguments)
    if DEFECT == "cursor-inherits":
        now = issued | {key: value for key, value in arguments.items() if key != "cursor"}
    if DEFECT == "cursor-ignores-release":
        issued, now = issued | {"release": None}, now | {"release": None}
    return issued != now


def _shaped(name: str, items: list[dict]) -> dict:
    """A result of `name` with `items` where its `items` paths say."""

    paths = TOOLS[name].get("items", ["."])
    placed = [
        _placed([step for step in path.split(".") if step], _path_items(name, path, items))
        for path in paths
    ]
    return placed[0] if len(placed) == 1 else _merged(placed)


def _path_items(name: str, path: str, items: list[dict]) -> list[dict]:
    """Bare edges and release reports have no item origin to pass through."""

    if path not in CALLS.get(name, {}).get("originless", []):
        return items
    return [
        item | {"provenance": {k: v for k, v in item["provenance"].items() if k != "upstream"}}
        for item in items
    ]


def _merged(parts: list) -> dict:
    """Results of several item paths, such as nodes and edges, as one."""

    return {key: value for part in parts for key, value in part.items()}


def _error(code: str, status: int, body: dict, correlation: str) -> dict:
    said = f": {json.dumps(body)}" if DEFECT == "leaks-key" else ""
    record = {"code": code, "message": f"EVS answered {status}{said}", "correlationId": correlation}
    beside = {"concept": {"code": "C4817"}} if DEFECT == "content-with-error" else {}
    return {"error": "EVS failed" if DEFECT == "unshaped-error" else record} | beside


def _refusal(name: str, arguments: dict, correlation: str) -> dict | None:
    """The error a call gets without asking EVS, if any."""

    if DEFECT == "empty-as-error" and _matches_nothing(name, arguments):
        return _error("not_found", HTTPStatus.NOT_FOUND, {}, correlation)
    if (
        _cursor_refused(name, arguments)
        or _below_one(name, arguments)
        or _unpinned(name, arguments)
        or _malformed(name, arguments)
    ):
        return _error("invalid_request", HTTPStatus.BAD_REQUEST, {}, correlation)
    return None


def _malformed(name: str, arguments: dict) -> bool:
    """Whether an identifier is off the form its tool states (A7.6)."""

    if DEFECT == "unchecked-identifiers":
        return False
    forms = TOOLS[name].get("patterns", {}).items()
    off = [key for key, form in forms if not _formed(form, arguments, key)]
    if off:
        _probe(arguments)
    return bool(off)


def _probe(arguments: dict) -> None:
    """The requests a defect makes with the call's values before it refuses the call."""

    for value in _strings(arguments):
        if DEFECT == "checks-after-asking":
            _ask("/api/v1/version?" + urlencode({"probe": value}), {})
        if DEFECT == "asks-in-path-then-refuses":
            # Only what a URL cannot carry is encoded: a space, a fragment mark, a newline.
            encoded = value.replace(" ", "%20").replace("#", "%23").replace("\n", "%0A")
            _ask("/api/v1/version/" + encoded, {})
        if DEFECT == "asks-in-form-then-refuses":
            form = {"Content-Type": "application/x-www-form-urlencoded"}
            _ask("/api/v1/version", form, urlencode({"probe": value}).encode(), "POST")


def _strings(arguments: dict) -> list[str]:
    """The call's string values, a list's elements included."""

    values = [each for value in arguments.values() for each in _listed(value)]
    return [value for value in values if isinstance(value, str)]


def _listed(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _formed(form: str | dict, arguments: dict, key: str) -> bool:
    """Whether the argument has its form: by the call's terminology where the form is."""

    pattern = form.get(arguments.get("terminology")) if isinstance(form, dict) else form
    given = arguments.get(key)
    values = given if isinstance(given, list) else [given]
    return pattern is None or given is None or all(re.fullmatch(pattern, str(v)) for v in values)


def _unpinned(name: str, arguments: dict) -> bool:
    names = parameters(name)[0]
    return "release" in names and arguments.get("release") is None and DEFECT == "release-refused"


def _below_one(name: str, arguments: dict) -> bool:
    bounded = TOOLS[name].get("bounds", {})
    below = [key for key in bounded if key in arguments and arguments[key] < 1]
    return bool(below) and DEFECT != "raised-to-one"


def _answer(name: str, arguments: dict, correlation: str) -> tuple[object, bool]:
    """The content of a call and whether it is an error; a call answered before is not
    asked again, as A9.4 allows."""

    if name == "get_concept" and DEFECT == "concept-error":
        return _error("upstream_unavailable", 503, {}, correlation), True
    if arguments.get("code") == "C90000001":
        return _session_answer(arguments, correlation)
    return _ordinary_answer(name, arguments, correlation)


def _ordinary_answer(name: str, arguments: dict, correlation: str) -> tuple[object, bool]:
    call = json.dumps([name, arguments], sort_keys=True)
    cached = call in answered
    if refusal := _refusal(name, arguments, correlation):
        return refusal, True
    arguments = _implicit_arguments(name, arguments)
    if call not in answered and DEFECT != "asks-nothing":
        status, body = _asked(name, arguments, correlation)
        if code := _failure(status, body, arguments):
            if code == SWALLOWED.get(DEFECT):
                return {}, False
            return _error(code, status, body, correlation), True
        answered[call] = body
    content = _content(name, arguments, correlation, answered.get(call, {}))
    if NO_CACHE:
        answered.clear()
    return _cached_origin(content, cached), False


def _cached_origin(content: object, cached: bool) -> object:
    """One cache-hit defect; the normal stub keeps the original live source facts."""

    if cached and DEFECT == "cache-loses-origin" and isinstance(content, dict):
        provenance = content.get("provenance", {})
        provenance.pop("sourceUri", None)
        provenance.pop("upstream", None)
    if cached and DEFECT == "cache-swaps-origin" and isinstance(content, dict):
        provenance = content.get("provenance", {})
        provenance["sourceUri"] = f"{provenance['sourceUri']}?repeated"
    return content


def _implicit_arguments(name: str, arguments: dict) -> dict:
    if "release" in parameters(name)[0] and arguments.get("release") is None:
        return arguments | {"release": CURRENT}
    return arguments


def _session_pin(arguments: dict) -> str:
    if explicit := arguments.get("release"):
        if DEFECT == "session-overwrite":
            session_state["release"] = explicit
        return explicit
    if "release" not in session_state or DEFECT == "session-reresolves":
        _, rows, _ = _ask(
            "/api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly", {}
        )
        session_state["release"] = rows[0]["version"]
    return session_state["release"]


def _session_answer(arguments: dict, correlation: str) -> tuple[object, bool]:
    """The X-22 session fixture uses actual pinned content, not the version probe."""
    pin = _session_pin(arguments)
    path = f"/api/v1/concept/ncit_{pin}/C90000001?include=minimal"
    status, body, _ = _ask(path, {"X-Correlation-ID": correlation})
    if status == HTTPStatus.NOT_FOUND:
        error = _error("release_not_available", status, body, correlation)
        error["error"].update(
            details={"requested": pin, "source": "evs"},
            message="Start a new session or name a release.",
        )
        if DEFECT == "session-withdrawal-empty":
            return {}, False
        return error, True
    provenance = _provenance("get_concept", arguments | {"release": pin}, correlation, body)
    return body | {"provenance": provenance}, False


def _meta() -> dict:
    meta = {
        "ttlMs": 0 if DEFECT == "uncached-result" else 86_400_000,
        "cacheScope": "private" if DEFECT == "private-scope" else "public",
    }
    return meta | ({"licence": LICENCE_KEY} if DEFECT == "key-in-meta" else {})


async def call_tool(_context, params: types.CallToolRequestParams) -> types.CallToolResult:
    correlation = (params.meta or {}).get("correlationId", uuid4().hex)
    content, failed = _answer(params.name, params.arguments or {}, correlation)
    text = json.dumps(content) + (f" {LICENCE_KEY}" if DEFECT == "key-in-text" else "")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=content,
        is_error=failed,
        _meta=_tool_meta(params.name, params.arguments or {}),
    )


def _tool_meta(name: str, arguments: dict) -> dict:
    if DEFECT == "implicit-public":
        return _meta()
    if "release" in parameters(name)[0] and arguments.get("release") is None:
        return {"ttlMs": 0, "cacheScope": "private"}
    return _meta()


def _uris() -> list[tuple[str, str]]:
    """Each resource of the profile with each of its URI templates."""

    return [
        (key, uri) for key, resource in resources_of(PROFILE).items() for uri in resource["uri"]
    ]


def _advertised(uri: str) -> str:
    """The template as the server lists it: the concept's without its release, under the
    uri-differs defect."""

    differing = DEFECT == "uri-differs" and uri.startswith("ncit://concept/")
    return "ncit://concept/{code}" if differing else uri


def _name(key: str, uri: str) -> str:
    """A resource's name: its key, with the variables of a second template added."""

    return "-".join([key, *uri_variables(uri)]) if len(RESOURCES[key]["uri"]) > 1 else key


def _slice(items: list, params: types.PaginatedRequestParams | None) -> tuple[list, str | None]:
    """The page of `items` the request asks for and the cursor of the next, where the server
    pages (COMPLIANT_SERVER_PAGE_SIZE); else all of them."""

    if not PAGE_SIZE:
        return items, None
    start = int(params.cursor or 0) if params else 0
    end = start + PAGE_SIZE
    return items[start:end], str(end) if end < len(items) else None


def _listed_as(templated: bool) -> list[tuple[str, str]]:
    """The URIs resources/list names (templated False) or resources/templates/list names. The
    templates-as-resources defect lists them all as resources."""

    if DEFECT == "templates-as-resources":
        return [] if templated else _uris()
    return [(key, uri) for key, uri in _uris() if bool(uri_variables(uri)) == templated]


async def list_resources(
    _context, params: types.PaginatedRequestParams | None
) -> types.ListResourcesResult:
    resources = [
        types.Resource(name=_name(key, uri), uri=_advertised(uri), mime_type=RESOURCES[key]["mime"])
        for key, uri in _listed_as(templated=False)
    ]
    page, cursor = _slice(resources, params)
    return types.ListResourcesResult(resources=page, next_cursor=cursor)


async def list_resource_templates(
    _context, params: types.PaginatedRequestParams | None
) -> types.ListResourceTemplatesResult:
    templates = [
        types.ResourceTemplate(
            name=_name(key, uri), uri_template=_advertised(uri), mime_type=RESOURCES[key]["mime"]
        )
        for key, uri in _listed_as(templated=True)
    ]
    page, cursor = _slice(templates, params)
    return types.ListResourceTemplatesResult(resource_templates=page, next_cursor=cursor)


def _matched(uri: str) -> tuple[str, str, dict[str, str]]:
    """The resource and template a URI is an instance of, with the values of its variables."""

    for key, template in _uris():
        pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(template))
        if found := re.fullmatch(pattern, uri):
            return key, template, found.groupdict()
    raise MCPError(types.INVALID_PARAMS, f"no such resource: {uri}")


# What identifies an item, and another value for it (the resource-other-content defect).
OTHER = {"code": "C9999", "publicId": "999", "version": "0.0", "generatedAt": "1999-01-01"}


def _renamed(value: object) -> object:
    """`value` with whatever identifies an item another (the resource-other-content defect)."""

    if isinstance(value, dict):
        return {k: OTHER.get(k, v) if isinstance(v, str) else _renamed(v) for k, v in value.items()}
    return [_renamed(item) for item in value] if isinstance(value, list) else value


def _unproven(value: object) -> object:
    """`value` without any provenance (the resource-no-provenance defect)."""

    if isinstance(value, dict):
        return {k: _unproven(v) for k, v in value.items() if k != "provenance"}
    return [_unproven(item) for item in value] if isinstance(value, list) else value


def _manifest(tool_answer: dict, values: dict[str, str]) -> dict:
    """The index manifest of the release asked for. It is not the tool's answer: that supplies
    the provenance beside which the index's own is stated."""

    version = "26.08e" if DEFECT == "manifest-other-release" else values["release"]
    release = {"terminology": "ncit", "identifier": version}
    manifest = {
        "terminology": "ncit",
        "version": version,
        "concepts": 0 if DEFECT == "manifest-unbuilt" else 5,
        "embedding": {"provider": "stub-provider", "model": "stub-model", "dimensions": 8},
        "builtAt": "never" if DEFECT == "manifest-unbuilt" else datetime.now(UTC).isoformat(),
        "provenance": tool_answer["provenance"]
        | {"release": release, "source": "evs_index", "servedBy": "index"},
    }
    if DEFECT == "manifest-no-embedding":
        del manifest["embedding"]
    return manifest


def _registry_resource(tool_answer: dict, _values: dict[str, str]) -> dict:
    """The registry's state, to which the resource adds the provenance the tool's answer lacks:
    the export folder's listing is its source (A3.8.2)."""

    release = dict(UNPINNED_REGISTRY)
    state = dict(tool_answer)
    if DEFECT == "registry-identifier":
        state["identifier"] = release["identifier"] = UNPUBLISHED
    provenance = {
        "release": release,
        "source": "cadsr_export",
        "retrievedAt": _retrieved(),
        "servedBy": "live",
        "correlationId": uuid4().hex,
    }
    return state | {"provenance": provenance}


def _as_resource(key: str, tool_answer: dict, values: dict[str, str]) -> dict:
    """What a resource holds, given its tool's answer: the answer itself for most."""

    builders = {"index_manifest": _manifest, "registry": _registry_resource}
    return builders.get(key, lambda answer, _values: answer)(tool_answer, values)


def _read_call(key: str, template: str, values: dict[str, str]) -> tuple[str, dict]:
    """The tool call a resource's content is made from, as the defects alter it."""

    tool, arguments = resource_call(key, template, values)
    if DEFECT == "crosswalk-cut":
        arguments = arguments | {"limit": 100}
    if DEFECT == "resource-ignores-version":
        arguments = {name: value for name, value in arguments.items() if name != "version"}
    return tool, arguments


def _hint(key: str) -> dict:
    """The caching hint of a resource: the class M2.2 gives what it holds (a long ttlMs for
    release-pinned content, a short one for content no release pins), as the defects alter it."""

    pinned = RESOURCES[key]["holds"] == "release-pinned"
    other = DEFECT == "resource-other-ttl"
    ttl = LONG_TTL if pinned else SHORT_TTL
    scope = "private" if DEFECT == "resource-private-scope" else "public"
    return {"ttl_ms": (0 if pinned else LONG_TTL) if other else ttl, "cache_scope": scope}


def _read_result(key: str, contents: list) -> types.ReadResourceResult:
    """The result of resources/read: the hint as fields of the result, or where a defect puts
    it, in the _meta or nowhere."""

    hint = _hint(key)
    if DEFECT == "resource-no-ttl":
        return types.ReadResourceResult(contents=contents)
    if DEFECT == "resource-ttl-in-meta":
        meta = {"ttlMs": hint["ttl_ms"], "cacheScope": hint["cache_scope"]}
        return types.ReadResourceResult(contents=contents, _meta=meta)
    return types.ReadResourceResult(contents=contents, **hint)


async def read_resource(
    _context, params: types.ReadResourceRequestParams
) -> types.ReadResourceResult:
    try:
        key, template, values = _matched(params.uri)
    except MCPError:
        if DEFECT != "unmatched-uri-content":
            raise
        return _read_result("concept", [types.TextResourceContents(uri=params.uri, text="{}")])
    tool, arguments = _read_call(key, template, values)
    content, failed = _answer(
        tool, arguments, (params.meta or {}).get("correlationId", uuid4().hex)
    )
    if failed:
        raise MCPError(types.INVALID_PARAMS, json.dumps(content))
    content = _as_resource(key, content, values)
    content = {"resource-other-content": _renamed, "resource-no-provenance": _unproven}.get(
        DEFECT, lambda each: each
    )(content)
    mime = "text/plain" if DEFECT == "resource-wrong-mime" else RESOURCES[key]["mime"]
    contents = [
        types.TextResourceContents(uri=params.uri, mime_type=mime, text=json.dumps(content))
    ]
    return _read_result(key, contents)


def _stated_prompts() -> dict[str, dict]:
    """The prompts the server lists: those of its profile, one too few under prompt-missing,
    and under prompt-outside-profile one that names tools the profile lacks."""

    prompts = dict(prompts_of(PROFILE))
    if DEFECT == "prompt-outside-profile":
        prompts["protocol_authoring"] = PROMPTS["protocol_authoring"]
    if DEFECT == "prompt-missing":
        prompts.pop(next(iter(prompts)))
    return prompts


def _declared(prompt: dict) -> list[types.PromptArgument]:
    arguments = prompt["arguments"][1:] if DEFECT == "argument-undeclared" else prompt["arguments"]
    return [
        types.PromptArgument(
            name=each["name"], description=each["description"], required=each["required"]
        )
        for each in arguments
    ]


async def list_prompts(
    _context, params: types.PaginatedRequestParams | None
) -> types.ListPromptsResult:
    prompts = [
        types.Prompt(
            name=name,
            title=prompt["title"],
            description=prompt["adds"],
            arguments=_declared(prompt),
        )
        for name, prompt in _stated_prompts().items()
    ]
    page, cursor = _slice(prompts, params)
    return types.ListPromptsResult(prompts=page, next_cursor=cursor)


class _Blank(dict):
    """The arguments of a prompt: one not given is empty text."""

    def __missing__(self, key: str) -> str:
        return ""


def _swapped(text: str, first: str, second: str) -> str:
    """`text` with the names of two tools exchanged (the prompt-reordered defect)."""

    return text.replace(first, "\0").replace(second, first).replace("\0", second)


async def get_prompt(_context, params: types.GetPromptRequestParams) -> types.GetPromptResult:
    prompt = _stated_prompts()[params.name]
    text = prompt["template"].format_map(_Blank(params.arguments or {}))
    if DEFECT == "prompt-omits-tool":
        text = text.replace(prompt["tools"][-1], "the last tool")
    if DEFECT == "prompt-reordered":
        text = _swapped(text, *prompt["tools"][:2])
    content = types.TextContent(type="text", text=text)
    message = types.PromptMessage(role="user", content=content)
    return types.GetPromptResult(messages=[] if DEFECT == "prompt-empty" else [message])


def _handlers() -> dict:
    """The handlers the server registers; a defect leaves out a capability."""

    handlers = {"on_list_tools": list_tools, "on_call_tool": call_tool}
    if DEFECT != "no-prompts":
        handlers |= {"on_list_prompts": list_prompts, "on_get_prompt": get_prompt}
    if DEFECT != "no-resources":
        handlers |= {
            "on_list_resources": list_resources,
            "on_list_resource_templates": list_resource_templates,
            "on_read_resource": read_resource,
        }
    return handlers


# The list methods, and the defect that gives each a ttlMs of 0.
LISTS = {
    "tools/list": "no-ttl",
    "prompts/list": "prompts-no-ttl",
    "resources/list": "resources-no-ttl",
    "resources/templates/list": "templates-no-ttl",
}


def _list_hint(uncached: str) -> CacheHint:
    scope = "private" if DEFECT == "lists-private" else "public"
    return CacheHint(ttl_ms=0 if uncached == DEFECT else LONG_TTL, scope=scope)


SERVER = Server(
    "compliant-server",
    cache_hints={method: _list_hint(uncached) for method, uncached in LISTS.items()},
    **_handlers(),
)


def _note_authorization(given: str) -> None:
    if AUTHORIZATION_LOG:
        with Path(AUTHORIZATION_LOG).open("a", encoding="utf-8") as log:
            log.write(given + "\n")


def _checked(app):
    """The app, answering 401 to a request without the expected Authorization header, and
    noting the header each request brought."""

    async def checked(scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"authorization", b"").decode()
            _note_authorization(given)
            if AUTHORIZATION is not None and given != AUTHORIZATION:
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)

    return checked


async def main() -> None:
    if TRANSPORT == "http":
        config = uvicorn.Config(
            _checked(SERVER.streamable_http_app()), host="127.0.0.1", port=PORT, log_level="warning"
        )
        await uvicorn.Server(config).serve()
        return
    async with stdio_server() as (read, write):
        await SERVER.run(read, write, SERVER.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
