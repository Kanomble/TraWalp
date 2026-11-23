"""Static repository dependency inventory. No imports of the inspected code."""

import ast
import hashlib
import json
import re
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

from f01_storage_census import REPO, csv_out

FILE_TOKEN = re.compile(
    r"(?<![\w.-])[\w.-]+\.(?:py|ps1|jsonl?|md|csv|sqlite3?|db|ya?ml|toml|txt)(?![\w.-])"
)
SQL = re.compile(
    r"\b(?:CREATE TABLE(?: IF NOT EXISTS)?|INSERT INTO|UPDATE|DELETE FROM|"
    r"REFERENCES|FROM|JOIN|ALTER TABLE)\s+[\`\[\"]?([a-z_]\w*)",
    re.I,
)
DYNAMIC = {"import_module", "__import__", "spec_from_file_location", "runpy.run_path"}
CALL_PREFIX = ("subprocess.", "os.spawn", "runpy.", "importlib.")
READABLE = {".py", ".md", ".json", ".jsonl", ".csv", ".toml", ".txt", ".yaml", ".yml", ".ps1"}
LIMIT = 1024**2


def put_json(path, payload):
    with path.open("x", encoding="utf-8", newline="\n") as out:
        json.dump(payload, out, indent=2, ensure_ascii=True)
        out.write("\n")


def module_paths(paths):
    result = defaultdict(set)
    for p in paths:
        if p.suffix != ".py":
            continue
        parts = p.with_suffix("").parts
        if parts[0] == "src":
            parts = parts[1:]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        result[".".join(parts)].add(p.as_posix())
        result[parts[-1]].add(p.as_posix())
    return result


def qualified(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{qualified(node.value)}.{node.attr}"
    return ""


def classification(path, suffix):
    section = path.split("/")[0]
    if section == "src" or section == "config" or path in {"pyproject.toml", "requirements.txt"}:
        return "KEEP_COMPATIBILITY" if "canonical_migration" in path else "KEEP_ACTIVE"
    if section == "tests":
        return "KEEP_COMPATIBILITY"
    if (
        "migration" in path
        or "copy_rehearsal" in path
        or "acceptance" in path
        or path.startswith("reports/baselines/")
    ):
        return "KEEP_EVIDENCE"
    if section in {"scripts", "docs"} or suffix == ".py":
        return "KEEP_RESEARCH"
    return "KEEP_EVIDENCE"


def graph(storage, output, tracked):
    selections = {"src", "docs", "scripts", "tests", "config", "reports"}
    records = {
        row["relative"]: row
        for row in storage
        if row["root"] == "repo"
        and row["file_type"] in {"FILE", "SQLITE_OR_SIDECAR"}
        and (row["relative"].split("/")[0] in selections or "/" not in row["relative"])
        and "__pycache__" not in row["relative"].lower()
        and not Path(row["relative"]).name.startswith(".")
        and not row["relative"].startswith("docs/tools/f01_")
    }
    modules = module_paths(Path(p) for p in records)
    candidates = defaultdict(set)
    for row in storage:
        if row["file_type"] != "DIRECTORY":
            candidates[Path(row["path"]).name.lower()].add(row["path"])
    nodes, edges, python_files, gaps = [], set(), [], []
    duplicates, defs, indirect_uses, symbol_consumers = defaultdict(list), [], Counter(), Counter()
    skip = []
    for path, row in sorted(records.items()):
        candidate = Path(path)
        suffix = candidate.suffix.lower()
        refs, imports, tables, dynamic, cli = set(), set(), set(), set(), set()
        kind = classification(path, suffix)
        reason = (
            "REPOSITORY_CODE_INVENTORY_MEMBER"
            if suffix == ".py" and candidate.parts[0] in {"src", "scripts", "config"}
            else "KEEP_WORKFLOW_OR_EVIDENCE"
        )
        if suffix in READABLE and (int(row["logical_bytes"]) <= LIMIT or suffix == ".py"):
            try:
                raw = (REPO / path).read_bytes()
                txt = raw.decode(
                    "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                )
            except (OSError, UnicodeError) as exc:
                gaps.append({"path": path, "error": type(exc).__name__})
                txt = ""
            refs.update(token.lower() for token in FILE_TOKEN.findall(txt))
            for ref in refs:
                matches = candidates.get(ref, ())
                if len(matches) > 20:
                    edges.add((path, f"basename:{ref}", "AMBIGUOUS_FILE_TOKEN"))
                    continue
                for target in matches:
                    if target.casefold() != row["path"].casefold():
                        edges.add((path, target, "TEXT_BASENAME_CANDIDATE"))
            if suffix in {".md", ".ps1"}:
                prose = re.sub(r"(?ms)^\s*```.*?^\s*```[^\r\n]*", "", txt)
                for match in re.finditer(r"\]\(([^)]+)\)", prose):
                    target = match.group(1).split("#")[0].strip("<>")
                    if not target or "://" in target or target.startswith(("/", "$")):
                        continue
                    target = re.sub(r":\d+$", "", target)
                    resolved = (REPO / candidate.parent / target).resolve()
                    if resolved.is_relative_to(REPO) and not resolved.exists():
                        gaps.append(
                            {"path": path, "error": "ABSENT_RELATIVE_LINK", "target": target}
                        )
        elif suffix in READABLE:
            skip.append({"path": path, "reason": "GENERATED_TEXT_ABOVE_SCAN_LIMIT"})
        if suffix == ".py":
            python_files.append(path)
            try:
                tree = ast.parse(txt)
            except SyntaxError:
                gaps.append({"path": path, "error": "PYTHON_AST_PARSE_FAILED"})
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(it.name for it in node.names)
                    if isinstance(node, ast.ImportFrom):
                        indirect_uses.update(it.name for it in node.names)
                        parent = node.module or ""
                        if node.level:
                            parts = list(candidate.parent.parts)
                            if parts and parts[0] == "src":
                                parts = parts[1:]
                            parts = parts[: len(parts) - node.level + 1]
                            parent = ".".join([*parts, *([parent] if parent else [])])
                        if parent:
                            imports.add(parent)
                        imports.update(
                            name
                            for it in node.names
                            if (name := f"{parent}.{it.name}".strip(".")) in modules
                        )
                    if isinstance(node, ast.Call):
                        name = qualified(node.func)
                        if name:
                            symbol_consumers[name.rsplit(".", 1)[-1]] += 1
                        if name.split(".")[-1] in DYNAMIC or name.startswith(CALL_PREFIX):
                            dynamic.add(f"{name}@{node.lineno}")
                        if (
                            name.endswith(".add_parser")
                            and node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                        ):
                            cli.add(node.args[0].value)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        tables.update(n.lower() for n in SQL.findall(node.value))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        fn = f"{path}:{node.lineno}:{node.name}"
                        defs.append((node.name, fn))
                        if len(node.body) > 2:
                            contents = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                            duplicates[hashlib.sha256(contents.encode()).hexdigest()].append(fn)
                for imp in imports:
                    destinations = modules.get(imp, ())
                    for destination in destinations:
                        if destination != path:
                            edges.add((path, destination, "PYTHON_IMPORT_EXACT_OR_BASENAME"))
                    if not destinations:
                        edges.add((path, imp, "EXTERNAL_OR_UNRESOLVED_IMPORT"))
                edges.update((path, table, "SQL_TABLE_STATIC") for table in tables)
                edges.update((path, name, "CLI_REGISTRATION_LITERAL") for name in cli)
        nodes.append(
            {
                "path": path,
                "absolute_path": row["path"],
                "suffix": suffix,
                "tracked": path in tracked,
                "logical_bytes": row["logical_bytes"],
                "class": kind,
                "reason": reason,
                "imports": len(imports),
                "dynamic_or_subprocess_sites": len(dynamic),
                "sql_table_tokens": len(tables),
                "cli_literals": len(cli),
                "text_tokens": len(refs),
            }
        )
        edges.update((path, call, "DYNAMIC_OR_SUBPROCESS_SITE") for call in dynamic)
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf8"))["project"]
    for name, binding in project.get("scripts", {}).items():
        edges.add((f"CLI:{name}", binding, "PACKAGING_ENTRYPOINT"))
    csv_out(output / "repository_inventory.csv", nodes)
    dup = [items for items in duplicates.values() if len({i.split(":")[0] for i in items}) > 1]
    audit_candidates = {
        "removals_proven_safe": [],
        "identical_ast_bodies_across_files_review_only": sorted(dup),
        "apparent_unreferenced_private_functions_review_only": [
            fn
            for name, fn in defs
            if name.startswith("_")
            and not name.startswith("__")
            and not (symbol_consumers[name] or indirect_uses[name])
        ],
        "findings": [
            {
                "code": "HISTORICAL_VARIANTS_COLLIDE_IN_LEGACY_BAR_KEY",
                "paths": ["src/trading_system/data/database.py", "src/trading_system/data/sync.py"],
                "risk": "Historical production bar primary key has no feed or adjustment column",
            },
            {
                "code": "ARCHIVE_TARGET_BINDING_RESTORE",
                "paths": ["src/trading_system/data/sec_persistence.py"],
                "risk": "Fresh path or file identity does not admit old pending requests",
            },
        ],
        "reason": (
            "Static symbol matches cannot exclude callbacks, dynamic entrypoints, manual runbooks "
            "or historical fingerprint membership."
        ),
    }
    put_json(output / "dead_code_candidates.json", audit_candidates)
    put_json(
        output / "dependency_graph.json",
        {
            "inventory_contract": "F01_STATIC_CONSERVATIVE_V1",
            "packaging_entrypoints": project.get("scripts", {}),
            "dependencies": project.get("dependencies", []),
            "optional_dependencies": project.get("optional-dependencies", {}),
            "nodes": [node["path"] for node in nodes],
            "edges": [{"from": a, "to": b, "kind": c} for a, b, c in sorted(edges)],
            "inspection_gaps": gaps,
            "generated_text_not_read": skip,
            "dynamic_policy": (
                "Paths can be basename candidates. Membership, callback invocation and "
                "external runtime reachability cannot be proved by AST alone."
            ),
        },
    )
    py = Counter(n["class"] for n in nodes if n["suffix"] == ".py")
    return {
        "files": len(nodes),
        "python_files": len(python_files),
        "python_classes": dict(py),
        "edges": len(edges),
        "script_python_files": sum(p.startswith("scripts/") for p in python_files),
        "report_python_files": sum(p.startswith("reports/") for p in python_files),
        "ast_duplicates": len(dup),
        "private_candidates": len(
            audit_candidates["apparent_unreferenced_private_functions_review_only"]
        ),
        "docs": sum(n["suffix"] == ".md" for n in nodes),
        "text_references": edges,
        "inspection_gaps": gaps,
    }
