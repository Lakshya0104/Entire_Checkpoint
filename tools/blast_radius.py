#!/usr/bin/env python3
"""Local blast-radius analysis, used when `entire graph impact` is unavailable.

This is NOT a replacement for Entire Graph and does not pretend to be one. It
walks this repo's Python AST to answer one narrow question before an edit:
which modules reference this symbol, and what does the symbol itself call?

Entire Graph does far more (type consumers, data flows, cross-language). When
the CLI is present, use it — `witness snapshot` / `witness recheck` record its
output as evidence. This tool exists so that "the CLI is missing" never becomes
an excuse to edit a shared code path without looking first.

Usage:
    python3 tools/blast_radius.py format_evidence AgentRunner.run
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH = [ROOT / "backend"]


def python_files() -> list[Path]:
    out: list[Path] = []
    for base in SEARCH:
        out.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(out)


def _defs_in(tree: ast.AST) -> dict[str, ast.AST]:
    """Map both `name` and `Class.name` to their definition node."""
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[f"{node.name}.{child.name}"] = child
                    found.setdefault(child.name, child)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, node)
    return found


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def analyse(symbol: str) -> dict:
    bare = symbol.split(".")[-1]
    defined_in: list[str] = []
    callers: list[tuple[str, str]] = []
    callees: set[str] = set()

    for path in python_files():
        rel = str(path.relative_to(ROOT))
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue

        defs = _defs_in(tree)
        if symbol in defs or bare in defs:
            defined_in.append(rel)
            target = defs.get(symbol) or defs[bare]
            callees |= _called_names(target)

        # Who references it, and from inside which enclosing function?
        for name, node in defs.items():
            if "." in name:
                continue
            if bare in _called_names(node) and name != bare:
                callers.append((rel, name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == bare:
                if rel not in [c[0] for c in callers] and rel not in defined_in:
                    callers.append((rel, "<module-level reference>"))
                break

    return {
        "symbol": symbol,
        "defined_in": defined_in,
        "callers": sorted(set(callers)),
        "callees": sorted(c for c in callees if not c.startswith("_") or c == bare),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="+")
    args = ap.parse_args()

    print("NOTE: local AST fallback — `entire graph impact` was unavailable.\n"
          "      Narrower than Entire Graph; use the CLI where present.\n")
    for sym in args.symbols:
        r = analyse(sym)
        print(f"── {r['symbol']}")
        if not r["defined_in"]:
            print("   not found in this repo\n")
            continue
        print(f"   defined in : {', '.join(r['defined_in'])}")
        if r["callers"]:
            print("   callers    :")
            for path, fn in r["callers"]:
                print(f"                {path} -> {fn}")
        else:
            print("   callers    : none found")
        if r["callees"]:
            print(f"   calls      : {', '.join(r['callees'][:12])}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
