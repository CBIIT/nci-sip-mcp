"""Opt-in evidence for code executed from an independently installed child environment."""

from __future__ import annotations

import atexit
import hashlib
import json
import marshal
import os
import shutil
import sys
import sysconfig
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import CodeType
from typing import Literal, TypedDict, TypeGuard
from uuid import uuid4

VERSION = 1
DOMAINS = {"unit", "selftest", "fixture"}
PHASES = {"baseline", "candidate", "producer"}
RUN_ID_LENGTH = 32
DIGEST_LENGTH = 64
MIN_SOURCE_COMPONENTS = 2
MIN_ACCEPTANCE_SOURCE_COMPONENTS = 3


@dataclass(frozen=True, slots=True)
class ChildRoute:
    source: str
    module: str
    environment: Path


@dataclass(frozen=True, slots=True)
class Producer:
    version: int
    run_id: str
    domain: str
    shard: str
    phase: str


@dataclass(frozen=True, slots=True)
class ChildTrace:
    version: int
    run_id: str
    invocation: str
    domain: str
    shard: str
    phase: str
    test_phase: str
    node: str
    source: str
    module: str
    path: str
    source_digest: str
    bytecode_digest: str
    environment: str
    executable: str
    lines: tuple[int, ...]
    outcome: Literal["reached", "unreached", "invalid"]
    detail: str


class ProducerRecord(TypedDict):
    version: int
    run_id: str
    domain: str
    shard: str
    phase: str


class ChildTraceRecord(TypedDict):
    version: int
    run_id: str
    invocation: str
    domain: str
    shard: str
    phase: str
    test_phase: str
    node: str
    source: str
    module: str
    path: str
    source_digest: str
    bytecode_digest: str
    environment: str
    executable: str
    lines: list[int]
    outcome: Literal["reached", "unreached", "invalid"]
    detail: str


def trace_directory(data_file: Path) -> Path:
    return data_file.parent / f"mutation-child-traces-{data_file.name.lstrip('.')}"


def stage_hook(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__), directory / "_mutation_child_trace.py")
    (directory / "sitecustomize.py").write_text(
        "from _mutation_child_trace import install_child_trace\ninstall_child_trace()\n"
    )


def prepare_run(
    data_file: Path, hook_directory: Path, domain: str, phase: str, shard: str
) -> dict[str, str]:
    if domain not in DOMAINS or phase not in PHASES or not shard:
        raise ValueError("Invalid child trace run identity")
    directory = trace_directory(data_file)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError(f"Child trace directory is not empty: {directory}")
    stage_hook(hook_directory)
    producer = Producer(VERSION, uuid4().hex, domain, shard, phase)
    write_json(directory / "producer.json", producer_record(producer))
    return {
        "MUTATION_CHILD_TRACE_DIR": str(directory.resolve()),
        "MUTATION_CHILD_TRACE_HOOK": str(hook_directory.resolve()),
        "MUTATION_CHILD_TRACE_RUN": producer.run_id,
        "MUTATION_CHILD_TRACE_DOMAIN": producer.domain,
        "MUTATION_CHILD_TRACE_SHARD": producer.shard,
        "MUTATION_CHILD_TRACE_PHASE": producer.phase,
    }


def producer_record(producer: Producer) -> ProducerRecord:
    return {
        "version": producer.version,
        "run_id": producer.run_id,
        "domain": producer.domain,
        "shard": producer.shard,
        "phase": producer.phase,
    }


def parse_producer(value: dict[str, object]) -> Producer:
    if not valid_producer_record(value):
        raise ValueError("Invalid child trace producer record")
    producer = Producer(
        value["version"], value["run_id"], value["domain"], value["shard"], value["phase"]
    )
    if not valid_producer_identity(producer):
        raise ValueError("Invalid child trace producer identity")
    return producer


def valid_producer_record(value: dict[str, object]) -> TypeGuard[ProducerRecord]:
    keys = {"version", "run_id", "domain", "shard", "phase"}
    return (
        set(value) == keys
        and type(value["version"]) is int
        and isinstance(value["run_id"], str)
        and isinstance(value["domain"], str)
        and isinstance(value["shard"], str)
        and isinstance(value["phase"], str)
    )


def valid_producer_identity(producer: Producer) -> bool:
    if not valid_producer_fields(producer):
        return False
    return valid_run_id(producer.run_id)


def valid_producer_fields(producer: Producer) -> bool:
    return (
        producer.version == VERSION
        and producer.domain in DOMAINS
        and bool(producer.shard)
        and producer.phase in PHASES
    )


def valid_run_id(value: str) -> bool:
    return len(value) == RUN_ID_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def child_trace_record(trace: ChildTrace) -> ChildTraceRecord:
    return ChildTraceRecord(
        version=trace.version,
        run_id=trace.run_id,
        invocation=trace.invocation,
        domain=trace.domain,
        shard=trace.shard,
        phase=trace.phase,
        test_phase=trace.test_phase,
        node=trace.node,
        source=trace.source,
        module=trace.module,
        path=trace.path,
        source_digest=trace.source_digest,
        bytecode_digest=trace.bytecode_digest,
        environment=trace.environment,
        executable=trace.executable,
        lines=list(trace.lines),
        outcome=trace.outcome,
        detail=trace.detail,
    )


def child_environment(route: ChildRoute) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    if "MUTATION_CHILD_TRACE_RUN" not in environment:
        return environment
    request, config = trace_request(route, environment)
    directory = Path(environment["MUTATION_CHILD_TRACE_DIR"])
    write_json(directory / f"{request['invocation']}.request.json", request)
    write_json(directory / f"{request['invocation']}.config.json", config)
    environment["PYTHONPATH"] = environment["MUTATION_CHILD_TRACE_HOOK"]
    environment["MUTATION_CHILD_TRACE_CONFIG"] = str(
        directory / f"{request['invocation']}.config.json"
    )
    return environment


def trace_request(
    route: ChildRoute, environment: dict[str, str]
) -> tuple[dict[str, object], dict[str, object]]:
    source = canonical_source(route.source)
    if not valid_module(route.module):
        raise ValueError("Invalid installed child module")
    context = environment.get("MUTATION_COVERAGE_CONTEXT", "")
    node, separator, phase = context.rpartition("|")
    if (
        separator != "|"
        or not node
        or phase not in {"run", "setup", "teardown"}
        or not valid_test_node(node, environment["MUTATION_CHILD_TRACE_DOMAIN"])
    ):
        raise ValueError("Installed child trace has no owning test context")
    invocation = uuid4().hex
    base = {
        "version": VERSION,
        "run_id": environment["MUTATION_CHILD_TRACE_RUN"],
        "invocation": invocation,
        "domain": environment["MUTATION_CHILD_TRACE_DOMAIN"],
        "shard": environment["MUTATION_CHILD_TRACE_SHARD"],
        "phase": environment["MUTATION_CHILD_TRACE_PHASE"],
        "node": node,
        "source": source,
        "module": route.module,
        "environment": str(route.environment.resolve()),
        "test_phase": phase,
    }
    request = base
    config = {
        **base,
        "record": str(Path(environment["MUTATION_CHILD_TRACE_DIR"]) / f"{invocation}.result.json"),
    }
    return request, config


def canonical_source(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or not source_suffix(value):
        raise ValueError("Invalid installed child source path")
    return value


def source_suffix(source: str) -> PurePosixPath:
    parts = PurePosixPath(source).parts
    if (
        len(parts) >= MIN_ACCEPTANCE_SOURCE_COMPONENTS
        and parts[0] == "acceptance"
        and parts[1] == "src"
    ):
        return PurePosixPath(*parts[2:])
    if len(parts) >= MIN_SOURCE_COMPONENTS and parts[0] in {"src", "scripts"}:
        return PurePosixPath(*parts[1:])
    raise ValueError("Installed child source must belong to a code root")


def valid_module(value: str) -> bool:
    return bool(value) and all(part.isidentifier() for part in value.split("."))


def valid_test_node(node: str, domain: str) -> bool:
    prefix = {"unit": "tests/", "selftest": "selftests/", "fixture": "tests/"}.get(domain)
    if prefix is None:
        return False
    path = node.split("::", 1)[0]
    return (
        "::" in node
        and path.startswith(prefix)
        and PurePosixPath(path).is_relative_to(PurePosixPath(prefix))
        and ".." not in PurePosixPath(path).parts
        and str(PurePosixPath(path)) == path
    )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n")


def read_producer(directory: Path) -> Producer:
    return parse_producer(read_object(directory / "producer.json"))


def validate_child_producer(producer: Producer, traces: list[ChildTrace]) -> None:
    if len({trace.invocation for trace in traces}) != len(traces):
        raise ValueError("Duplicate installed child trace invocation")
    for trace in traces:
        if not child_trace_matches_producer(producer, trace):
            raise ValueError("Installed child trace belongs to another producer or route")


def child_trace_matches_producer(producer: Producer, trace: ChildTrace) -> bool:
    if not trace_producer_identity_matches(producer, trace):
        return False
    return (
        trace.outcome != "invalid"
        and valid_test_node(trace.node, trace.domain)
        and valid_child_route(trace)
    )


def trace_producer_identity_matches(producer: Producer, trace: ChildTrace) -> bool:
    return (
        trace.version == VERSION
        and trace.run_id == producer.run_id
        and trace.domain == producer.domain
        and trace.shard == producer.shard
        and trace.phase == producer.phase
    )


def valid_child_route(trace: ChildTrace) -> bool:
    canonical_source(trace.source)
    if not valid_module(trace.module):
        return False
    if trace.outcome == "unreached":
        return unreached_route_is_empty(trace)
    return reached_route_is_valid(trace)


def unreached_route_is_empty(trace: ChildTrace) -> bool:
    return not any((trace.path, trace.source_digest, trace.bytecode_digest, trace.lines))


def reached_route_is_valid(trace: ChildTrace) -> bool:
    suffix = source_suffix(trace.source).parts
    path = Path(trace.path)
    return (
        trace.outcome == "reached"
        and path.is_absolute()
        and path.parts[-len(suffix) :] == suffix
        and Path(trace.environment).is_absolute()
        and Path(trace.executable).is_absolute()
        and valid_digest(trace.source_digest)
        and valid_digest(trace.bytecode_digest)
    )


def valid_digest(value: str) -> bool:
    return len(value) == DIGEST_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def read_traces(data_file: Path) -> tuple[Producer, list[ChildTrace]]:
    directory = trace_directory(data_file)
    producer = read_producer(directory)
    requests = {
        path.name.removesuffix(".request.json"): read_object(path)
        for path in directory.glob("*.request.json")
    }
    configs = {
        path.name.removesuffix(".config.json"): read_object(path)
        for path in directory.glob("*.config.json")
    }
    results = {
        path.name.removesuffix(".result.json"): parse_trace(read_object(path))
        for path in directory.glob("*.result.json")
    }
    if set(requests) != set(configs) or set(requests) != set(results):
        raise ValueError("Installed child request is missing its result")
    validate_trace_files(directory, requests)
    validate_trace_configs(directory, requests, configs)
    traces = list(results.values())
    validate_trace_ownership(producer, requests, results)
    validate_child_producer(producer, traces)
    return producer, traces


def validate_trace_files(directory: Path, requests: dict[str, dict[str, object]]) -> None:
    allowed = {"producer.json"}
    for invocation in requests:
        allowed.update(
            {
                f"{invocation}.request.json",
                f"{invocation}.config.json",
                f"{invocation}.result.json",
            }
        )
    if any(path.name not in allowed for path in directory.iterdir()):
        raise ValueError("Unexpected installed child trace file")


def validate_trace_configs(
    directory: Path, requests: dict[str, dict[str, object]], configs: dict[str, dict[str, object]]
) -> None:
    for invocation, request in requests.items():
        config = configs[invocation]
        if (
            set(config) != set(request) | {"record"}
            or any(config.get(key) != value for key, value in request.items())
            or config.get("record") != str(directory / f"{invocation}.result.json")
        ):
            raise ValueError("Installed child trace config does not match its request")


def validate_trace_ownership(
    producer: Producer, requests: dict[str, dict[str, object]], results: dict[str, ChildTrace]
) -> None:
    for invocation, request in requests.items():
        result = results[invocation]
        if not request_matches_producer(producer, invocation, request) or not trace_matches_request(
            result, request
        ):
            raise ValueError("Installed child trace belongs to another producer or route")
        if result.outcome == "invalid":
            raise ValueError(f"Installed child trace is invalid: {result.detail}")


def request_matches_producer(
    producer: Producer, invocation: str, request: dict[str, object]
) -> bool:
    return (
        request.get("invocation") == invocation
        and request.get("run_id") == producer.run_id
        and request.get("domain") == producer.domain
        and request.get("shard") == producer.shard
        and request.get("phase") == producer.phase
    )


def trace_matches_request(result: ChildTrace, request: dict[str, object]) -> bool:
    return all(
        getattr(result, key) == request.get(key)
        for key in (
            "run_id",
            "invocation",
            "domain",
            "shard",
            "phase",
            "test_phase",
            "node",
            "source",
            "module",
            "environment",
        )
    )


def read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Installed child trace record must be an object")
    return value


def parse_trace(value: dict[str, object]) -> ChildTrace:
    if not is_child_trace_record(value):
        raise ValueError("Invalid installed child trace record")
    return ChildTrace(
        value["version"],
        value["run_id"],
        value["invocation"],
        value["domain"],
        value["shard"],
        value["phase"],
        value["test_phase"],
        value["node"],
        value["source"],
        value["module"],
        value["path"],
        value["source_digest"],
        value["bytecode_digest"],
        value["environment"],
        value["executable"],
        tuple(value["lines"]),
        value["outcome"],
        value["detail"],
    )


def is_child_trace_record(value: dict[str, object]) -> TypeGuard[ChildTraceRecord]:
    keys = set(ChildTraceRecord.__annotations__)
    strings = keys - {"version", "lines", "outcome"}
    return (
        set(value) == keys
        and type(value["version"]) is int
        and trace_strings_are_valid(value, strings)
        and trace_lines_are_valid(value)
        and trace_outcome_is_valid(value)
    )


def trace_strings_are_valid(value: dict[str, object], keys: set[str]) -> bool:
    return all(isinstance(value[key], str) for key in keys)


def trace_lines_are_valid(value: dict[str, object]) -> bool:
    lines = value["lines"]
    return (
        isinstance(lines, list)
        and all(type(line) is int and line >= 1 for line in lines)
        and lines == sorted(set(lines))
    )


def trace_outcome_is_valid(value: dict[str, object]) -> bool:
    return value["outcome"] in ("reached", "unreached", "invalid")


def verified_module_source(frame, config: dict[str, object]) -> tuple[Path, bytes, CodeType]:
    path = Path(frame.f_code.co_filename).resolve()
    environment = Path(str(config["environment"])).resolve()
    suffix = source_suffix(str(config["source"])).parts
    if not installed_module_path(path, environment, suffix):
        raise ValueError("Module did not execute from the declared installed environment")
    source = path.read_bytes()
    compiled = compile(
        source,
        str(path),
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )
    if frame.f_code != compiled:
        raise ValueError("Loaded bytecode does not match the installed source")
    return path, source, compiled


def installed_module_path(path: Path, environment: Path, suffix: tuple[str, ...]) -> bool:
    return (
        Path(sys.prefix).resolve() == environment
        and Path(sys.prefix).resolve() != Path(sys.base_prefix).resolve()
        and path.is_relative_to(environment)
        and path.is_relative_to(Path(sysconfig.get_paths()["purelib"]).resolve())
        and path.parts[-len(suffix) :] == suffix
    )


class _ChildTracer:
    def __init__(self, config: dict[str, object]):
        self.config = config
        self.lines: set[int] = set()
        self.path = ""
        self.source_digest = ""
        self.bytecode_digest = ""
        self.outcome: Literal["reached", "unreached", "invalid"] = "unreached"
        self.detail = ""
        self.module_seen = False

    def trace(self, frame, event, _argument):
        if event == "call" and frame.f_code.co_name == "<module>":
            self.module_call(frame)
        if event == "line" and self.matches(frame):
            self.lines.add(frame.f_lineno)
        return self.trace

    def module_call(self, frame) -> None:
        if frame.f_globals.get("__name__") != self.config["module"]:
            return
        self.module_seen = True
        try:
            path, source, compiled = verified_module_source(frame, self.config)
            self.path = str(path)
            self.source_digest = hashlib.sha256(source).hexdigest()
            self.bytecode_digest = hashlib.sha256(marshal.dumps(compiled)).hexdigest()
            self.outcome = "reached"
        except (OSError, ValueError, SyntaxError) as error:
            self.outcome = "invalid"
            self.detail = str(error)

    def matches(self, frame) -> bool:
        return (
            self.outcome == "reached"
            and frame.f_globals.get("__name__") == self.config["module"]
            and str(Path(frame.f_code.co_filename).resolve()) == self.path
        )

    def finish(self) -> None:
        record = {
            key: self.config[key]
            for key in (
                "version",
                "run_id",
                "invocation",
                "domain",
                "shard",
                "phase",
                "test_phase",
                "node",
                "source",
                "module",
                "environment",
            )
        }
        record.update(
            path=self.path,
            source_digest=self.source_digest,
            bytecode_digest=self.bytecode_digest,
            executable=str(Path(sys.executable).resolve()),
            lines=sorted(self.lines),
            outcome=self.outcome if self.module_seen else "unreached",
            detail=self.detail,
        )
        write_json(Path(str(self.config["record"])), record)


def install_child_trace() -> None:
    config = read_object(Path(os.environ["MUTATION_CHILD_TRACE_CONFIG"]))
    tracer = _ChildTracer(config)
    sys.settrace(tracer.trace)
    threading.settrace(tracer.trace)
    atexit.register(tracer.finish)
