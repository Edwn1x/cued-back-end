"""
Phase 6 — the proof that makes deletion safe. The single-agent brain (the reactive
loop + its tools + the proactive heartbeat + the nightly maintenance) must have ZERO
dependency on the legacy multi-agent pipeline. If its transitive import closure is
disjoint from the doomed set, deleting that set is removing dead weight the brain
already doesn't touch — not surgery.

This is evidence-independent: it passed before any flag flip and stays true after the
deletion commits land. The AST walk catches imports anywhere — top-level AND the lazy
in-function form — which a runtime import check would miss.

Phase 6 Commit C landed the pipeline deletion: this file now also asserts the doomed
modules are GONE (importing the entrypoint raises ModuleNotFoundError) and adds `app`
to the roots (its inbound path no longer reaches any legacy module).
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The live single-agent path: the inbound webhook brain (`app`) + the reactive loop +
# its tools + the proactive heartbeat + nightly maintenance. `scheduler` went coach-free
# in Commit A; `app` lost its last legacy callers in Commit C.
ROOTS = ["app", "agent_loop", "agent_tools", "heartbeat", "consolidation", "episodic", "scheduler"]

# The legacy pipeline deleted in Phase 6 Commit C.
DOOMED = {"orchestrator", "coach", "skill_loader", "tone_analyzer", "agents"}


def _module_path(modname: str):
    """Resolve a first-party module name to its .py file, or None if not local."""
    top = modname.split(".")[0]
    candidates = [
        os.path.join(REPO, *modname.split(".")) + ".py",
        os.path.join(REPO, modname.replace(".", os.sep), "__init__.py"),
        os.path.join(REPO, top + ".py"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _imports_of(path: str) -> set:
    """Every module name imported anywhere in a file (top-level or in-function)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
    return names


def _closure(roots) -> set:
    """Transitive first-party import closure over local modules."""
    seen, stack = set(), list(roots)
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_path(mod)
        if not path:
            continue  # stdlib / third-party — not first-party, stop descending
        for imp in _imports_of(path):
            if _module_path(imp) and imp not in seen:
                stack.append(imp)
    return seen


def _doomed_hits(closure) -> set:
    return {m for m in closure if m.split(".")[0] in DOOMED}


def test_single_agent_path_does_not_import_legacy_pipeline():
    closure = _closure(ROOTS)
    hits = _doomed_hits(closure)
    assert hits == set(), (
        f"the single-agent path reaches the doomed legacy pipeline: {sorted(hits)}. "
        f"Deletion is only safe while this stays empty."
    )


def test_roots_all_resolve():
    """Guard against a rename silently making the isolation check vacuous."""
    for r in ROOTS:
        assert _module_path(r), f"root module {r!r} not found — isolation check would be hollow"


def test_doomed_set_is_deleted():
    """Phase 6 Commit C: the legacy pipeline is GONE. No .py on disk for any doomed
    module, and importing the entrypoint raises."""
    still_present = {m for m in DOOMED if _module_path(m)}
    assert still_present == set(), (
        f"legacy modules still on disk after the deletion commit: {sorted(still_present)}"
    )
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("orchestrator")
