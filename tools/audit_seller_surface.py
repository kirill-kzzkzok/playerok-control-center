"""Deterministic source inventory, explicitly NOT runtime route acceptance."""

import ast
import hashlib
import json
from pathlib import Path

from export_ui_routes import inventory

ROOT = Path(__file__).resolve().parents[1]


def inspect(path):
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    calls, imports, definitions, states = set(), set(), [], set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append({"name": node.name, "line": node.lineno, "end": node.end_lineno})
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if any(
                word in name
                for word in (
                    "call",
                    "execute",
                    "enqueue",
                    "setting",
                    "answer",
                    "edit_",
                    "send_",
                    "transaction",
                    "reserve",
                    "finish",
                    "claim",
                    "set_state",
                )
            ):
                calls.add(name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.isupper()
            and 2 < len(node.value) < 60
        ):
            states.add(node.value)
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "lines": len(source.splitlines()),
        "imports": sorted(imports),
        "definitions": sorted(definitions, key=lambda value: value["line"]),
        "boundary_calls": sorted(calls),
        "state_literals": sorted(states),
    }


def main():
    modules = [inspect(p) for folder in ("app", "playerokapi") for p in sorted((ROOT / folder).rglob("*.py"))]
    tests = [inspect(p) for p in sorted((ROOT / "tests").glob("test_*.py"))]
    sql = [
        {"path": p.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted((ROOT / "app/database").rglob("*.sql"))
    ]
    buttons = inventory()
    output = ROOT / "docs/SELLER_SOURCE_INVENTORY.json"
    output.write_text(
        json.dumps(
            {
                "scope": "structural inventory; not semantic review or execution proof",
                "production": modules,
                "sql": sql,
                "tests": tests,
                "buttons": buttons,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "production_modules": len(modules),
                "production_lines": sum(m["lines"] for m in modules),
                "sql_files": len(sql),
                "test_modules": len(tests),
                "test_cases_declared": sum(
                    d["name"].startswith("test_") for m in tests for d in m["definitions"]
                ),
                "button_declarations": len(buttons),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
