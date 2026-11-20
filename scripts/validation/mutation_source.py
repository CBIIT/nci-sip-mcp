"""Fixed AST operators, byte-accurate edits, scope and normalized exemption identities."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

import yaml

ROOTS = ("src/", "scripts/", "acceptance/src/")
COMPARISONS = {
    ast.Eq: "!=",
    ast.NotEq: "==",
    ast.Lt: ">=",
    ast.LtE: ">",
    ast.Gt: "<=",
    ast.GtE: "<",
    ast.Is: "is not",
    ast.IsNot: "is",
    ast.In: "not in",
    ast.NotIn: "in",
}
SYMBOLS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}


# The #509 rename changed these names across the whole tree, so every touched line would
# otherwise enter the mutation scope. `git diff -M` pairs old and new files so that a hunk whose
# removed lines equal its added lines after these substitutions can be recognised as a pure
# rename; this applies only where the old path maps to the new path through this table, and
# every other rename, move or split selects the whole new file. Remove in #519 once #509 has merged.
RENAMES = (
    ("nci_si_mcp", "nci_sip_mcp"),
    ("nci_si_acceptance", "nci_sip_acceptance"),
    ("nci-si-mcp", "nci-sip-mcp"),
    ("nci-si-install", "nci-sip-install"),
    ("nci-si-acceptance", "nci-sip-acceptance"),
    ("NCI_SI_", "NCI_SIP_"),
    ("without_nci_si_settings", "without_nci_sip_settings"),
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _fingerprint_value(value: object) -> str:
    if isinstance(value, ast.AST):
        return fingerprint_dump(value)
    if isinstance(value, list):
        return f"[{', '.join(_fingerprint_value(item) for item in value)}]"
    return repr(value)


def fingerprint_dump(node: ast.AST) -> str:
    fields = []
    for name in node._fields:
        try:
            value: object = getattr(node, name)
        except AttributeError:
            continue
        default: object = getattr(type(node), name, ...)
        if value is None and default is None:
            continue
        if _is_default_empty_list(node, name, value):
            continue
        fields.append(f"{name}={_fingerprint_value(value)}")
    return f"{type(node).__name__}({', '.join(fields)})"


def _is_default_empty_list(node: ast.AST, name: str, value: object) -> bool:
    if not isinstance(value, list) or value:
        return False
    field_types: object = getattr(type(node), "_field_types", {})
    if not isinstance(field_types, dict) or name not in field_types:
        return False
    field_type: object = field_types[name]
    return getattr(field_type, "__origin__", ...) is list


@dataclass(frozen=True)
class Mutant:
    file: str
    qualname: str
    operator: str
    site: str
    context: str
    original: str
    mutated: str
    line: int
    end_line: int
    start: int
    end: int
    before: str
    after: str

    def key(self) -> dict[str, str]:
        return {
            key: str(value)
            for key, value in asdict(self).items()
            if key in {"file", "qualname", "operator", "site", "context", "original", "mutated"}
        }


def eligible(path: str) -> bool:
    parts = Path(path).parts
    return (
        path.startswith(ROOTS)
        and path.endswith(".py")
        and not any(part in {"tests", "selftests", "generated", "__pycache__"} for part in parts)
        and not Path(path).name.startswith("test_")
    )


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(  # noqa: S603 - fixed git subcommands, refs are arguments
        ["git", "-C", str(root), *args],  # noqa: S607
        text=True,
        stderr=subprocess.PIPE,
    )


def scope(root: Path, paths: list[str], base: str, head: str) -> dict[str, set[int] | None]:
    if paths:
        result = {}
        for path in paths:
            resolved = (root / path).resolve().relative_to(root)
            name = resolved.as_posix()
            if not eligible(name) or not (root / name).is_file():
                raise ValueError(f"Not an eligible Python module: {path}")
            result[name] = None
        return result
    merge_base = git(root, "merge-base", base, head).strip()
    diff = git(
        root,
        "-c",
        "core.quotepath=off",
        "diff",
        "--no-ext-diff",
        "--unified=0",
        "-M",
        merge_base,
        head,
        "--",
        *ROOTS,
    )
    return diff_lines(diff)


def apply_renames(text: str) -> str:
    for before, after in RENAMES:
        text = text.replace(before, after)
    return text


def renamed_only(old: Sequence[str], new: Sequence[str]) -> bool:
    """True when the removed lines differ from the added lines only by the RENAMES table.

    Equal lines are not a rename: a line-ending or end-of-file change must stay selected.
    """
    return old != new and tuple(apply_renames(line) for line in old) == tuple(new)


@dataclass(frozen=True, slots=True)
class Hunk:
    """One zero-context diff hunk: its file, the new-side line range, and its changed lines."""

    file: str
    start: int
    count: int
    removed: tuple[str, ...]
    added: tuple[str, ...]

    def is_table_rename(self) -> bool:
        return renamed_only(self.removed, self.added)


def hunk_body(lines: Iterator[str], count: int) -> list[str]:
    body: list[str] = []
    while len(body) < count and (line := next(lines, None)) is not None:
        if not line.startswith("\\"):
            body.append(line)
    return body


def hunk_header(line: str) -> tuple[int, int, int]:
    match = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
    if not match:
        raise ValueError(f"Unparseable hunk header: {line}")
    return int(match[1] or 1), int(match[2]), int(match[3] or 1)


def file_name(line: str, name: str) -> str:
    if line.startswith("diff --git"):
        return ""
    if line.startswith("+++ /dev/null"):
        return "/dev/null"
    # git appends a tab to the name when the path contains a space
    return line[6:].removesuffix("\t") if line.startswith("+++ b/") else name


def hunks(diff: str) -> Iterator[Hunk]:
    name = ""
    lines = iter(diff.split("\n"))
    for line in lines:
        name = file_name(line, name)
        if line.startswith("@@"):
            yield read_hunk(line, name, lines)


def read_hunk(line: str, name: str, lines: Iterator[str]) -> Hunk:
    removed_count, start, count = hunk_header(line)
    if not name:
        raise ValueError(f"Hunk without a file name: {line}")
    body = hunk_body(lines, removed_count + count)
    removed = tuple(item[1:] for item in body if item.startswith("-"))
    added = tuple(item[1:] for item in body if item.startswith("+"))
    return Hunk(name, start, count, removed, added)


def rename_pairs(diff: str) -> dict[str, str]:
    """Map each new path git paired with an old path to that old path."""
    pairs = re.findall(r"^rename from (.+)\nrename to (.+)$", diff, re.MULTILINE)
    return {new: old for old, new in pairs}


def moved_files(moves: dict[str, str]) -> dict[str, set[int] | None]:
    return {new: None for new, old in moves.items() if eligible(new) and apply_renames(old) != new}


def diff_lines(diff: str) -> dict[str, set[int] | None]:
    moves = rename_pairs(diff)
    result = moved_files(moves)
    for item in hunks(diff):
        if eligible(item.file) and not item.is_table_rename():
            lines = result.setdefault(item.file, set())
            if lines is not None:
                lines.update(range(item.start, item.start + item.count))
    return result


def segment(source: bytes, node: ast.expr | ast.stmt) -> tuple[int, int, str]:
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[: node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[: cast("int", node.end_lineno) - 1])) + cast(
        "int", node.end_col_offset
    )
    return start, end, source[start:end].decode()


def text(node: ast.AST) -> str:
    return ast.unparse(node)


def comparisons(node: ast.Compare):
    values = [node.left, *node.comparators]
    for index in range(len(node.ops)):
        pieces = [f"({text(values[0])})"]
        for position, item in enumerate(node.ops):
            symbol = COMPARISONS[type(item)] if position == index else SYMBOLS[type(item)]
            pieces.append(f"{symbol} ({text(values[position + 1])})")
        yield f"comparison:{index}", " ".join(pieces)
    yield "comparison:negate", f"not ({text(node)})"


def booleans(node: ast.BoolOp):
    connector = " or " if isinstance(node.op, ast.And) else " and "
    yield "boolean:swap", connector.join(f"({text(value)})" for value in node.values)
    original = " and " if isinstance(node.op, ast.And) else " or "
    for index in range(len(node.values)):
        yield (
            f"boolean:drop:{index}",
            original.join(
                f"({text(value)})"
                for position, value in enumerate(node.values)
                if position != index
            ),
        )


def string_role(node: ast.Constant, parent: ast.AST) -> bool:
    if isinstance(parent, ast.Dict):
        return node in parent.keys
    if isinstance(parent, ast.Subscript):
        return parent.slice is node
    if not isinstance(parent, ast.Call):
        return False
    name = text(parent.func).rsplit(".", 1)[-1]
    return name in {"get", "getenv", "setdefault", "pop", "open", "fdopen"}


def constants(node: ast.Constant, parent: ast.AST):
    if isinstance(node.value, bool):
        yield "boolean:literal", repr(not node.value)
    elif type(node.value) is int and isinstance(parent, ast.Compare):
        for delta in (-1, 1):
            yield f"integer:{delta:+}", repr(node.value + delta)
    elif isinstance(node.value, str) and string_role(node, parent):
        yield "string:key-mode-setting", repr(node.value + "__mutated")


def replacements(node: ast.AST, parent: ast.AST):
    if isinstance(node, ast.Compare):
        yield from comparisons(node)
    elif isinstance(node, ast.BoolOp):
        yield from booleans(node)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield "not:remove", f"({text(node.operand)})"
    elif isinstance(node, ast.Constant):
        yield from constants(node, parent)
    else:
        yield from statements(node)


def statements(node: ast.AST):
    if isinstance(node, ast.Return) and node.value is not None:
        for value in ("None", "True", "False"):
            yield f"return:{value}", f"return {value}"
    elif isinstance(node, ast.Raise):
        yield "raise:remove", "pass"
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        yield "call:remove", "pass"


def nodes(node: ast.AST, qualname: str = "", owner: ast.AST | None = None, site: str = ""):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        qualname = f"{qualname}.{node.name}".lstrip(".")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner, site = node, ""
    for field, value in ast.iter_fields(node):
        children = value if isinstance(value, list) else [value]
        for index, child in enumerate(children):
            if isinstance(child, ast.AST):
                context = enclosing_context(node, child, owner)
                address = site_address(node, site, field, index)
                yield child, node, qualname, context, address
                yield from nodes(child, qualname, context, address)


def site_address(node: ast.AST, site: str, field: str, index: int) -> str:
    return "" if isinstance(node, ast.Module) else f"{site}/{field}/{index}"


def enclosing_context(node: ast.AST, child: ast.AST, owner: ast.AST | None):
    return child if isinstance(node, ast.Module) else owner


def candidates(path: str, source: bytes) -> tuple[list[Mutant], list[dict]]:
    tree = ast.parse(source)
    guards = entry_guards(tree)
    result, diagnostics = [], []
    for node, parent, qualname, owner, site in nodes(tree):
        if not isinstance(node, (ast.expr, ast.stmt)):
            continue
        for operator, after in replacements(node, parent):
            start, end, before = segment(source, node)
            changed = source[:start] + after.encode() + source[end:]
            rejected = candidate_rejection(node, after, changed, path, guards)
            if rejected:
                diagnostics.append(
                    {"file": path, "line": node.lineno, "operator": operator, **rejected}
                )
                continue
            result.append(
                Mutant(
                    path,
                    qualname or "<module>",
                    operator,
                    site,
                    digest(fingerprint_dump(cast("ast.AST", owner))),
                    digest(fingerprint_dump(node)),
                    digest(fingerprint_dump(ast.parse(after))),
                    node.lineno,
                    int(getattr(node, "end_lineno", node.lineno)),
                    start,
                    end,
                    before,
                    after,
                )
            )
    return result, diagnostics


def entry_guards(tree: ast.Module) -> set[int]:
    return {
        id(node.test) for node in tree.body if isinstance(node, ast.If) and entry_guard(node.test)
    }


def candidate_rejection(
    node: ast.AST, after: str, changed: bytes, path: str, guards: set[int]
) -> dict:
    if id(node) in guards:
        return {"outcome": "REJECTED", "reason": "entry-point guard"}
    return rejection(node, after, changed, path)


def entry_guard(node: ast.AST) -> bool:
    match node:
        case ast.Compare(
            left=ast.Name(id="__name__"),
            ops=[ast.Eq()],
            comparators=[ast.Constant(value="__main__")],
        ):
            return True
        case ast.Compare(
            left=ast.Constant(value="__main__"),
            ops=[ast.Eq()],
            comparators=[ast.Name(id="__name__")],
        ):
            return True
    return False


def rejection(node: ast.AST, after: str, changed: bytes, path: str) -> dict:
    replacement = ast.parse(after).body[0]
    if isinstance(replacement, ast.Expr):
        replacement = replacement.value
    if ast.dump(node) == ast.dump(replacement):
        return {"outcome": "NO_OP"}
    try:
        compile(changed, path, "exec")
    except SyntaxError as error:
        return {"outcome": "INVALID", "message": error.msg}
    return {}


def allowlist(root: Path, path: Path) -> list[dict[str, str]]:
    try:
        entries = yaml.safe_load(path.read_text()) if path.exists() else []
    except yaml.YAMLError as error:
        raise ValueError(f"Malformed allowlist YAML: {path}") from error
    if not isinstance(entries, list):
        raise ValueError("Allowlist must be a YAML list")
    known: dict[str, Counter] = {}
    seen = set()
    for entry in entries:
        validate_entry(root, entry, known, seen)
    return entries


def validate_entry(root: Path, entry: dict, known: dict, seen: set) -> None:
    entry_fields(entry)
    name = entry["file"]
    if not eligible(name) or not (root / name).resolve().is_relative_to(root):
        raise ValueError(f"Invalid allowlist path: {name}")
    if name not in known:
        mutants, _ = candidates(name, (root / name).read_bytes())
        known[name] = Counter(map(mutant_key, mutants))
    key = tuple(sorted({key: entry[key] for key in entry.keys() - {"reason"}}.items()))
    if key in seen or known[name][key] != 1:
        raise ValueError(f"Stale, duplicate or ambiguous allowlist entry: {name} {entry['site']}")
    seen.add(key)


def mutant_key(mutant: Mutant) -> tuple:
    return tuple(sorted(mutant.key().items()))


def entry_fields(entry: dict) -> None:
    fields = {"file", "qualname", "operator", "site", "context", "original", "mutated", "reason"}
    if not isinstance(entry, dict) or set(entry) != fields:
        raise ValueError("Malformed allowlist entry fields")
    if any(
        not isinstance(value, str) or not value.strip() or "\n" in value for value in entry.values()
    ):
        raise ValueError("Allowlist values must be nonempty single-line strings")
