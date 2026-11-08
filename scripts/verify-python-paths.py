#!/usr/bin/env python3
"""Ban in-code sys.path hacks and ad-hoc repo-root discovery (2026-11-08).

Rule: no first-party module may mutate sys.path at import time, and no
module may climb from __file__ to the repo root, except the canonical
places that own those jobs:

- deep-agent/cli.py::bootstrap() — adds deep-agent/ for frozen consumers
  (coder.py is Issue-7 frozen) + pinned by tests/test_cli.py::TestBootstrap.
- deep-agent/cli.py::repo_root() — the one checkout-root derivation (#540
  per-checkout hermeticity: .env, chroma_db).
- backend/api/routers/chat.py::_repo_root() — spawn paths for the
  deep_chat child (env-overridable via DEEP_AGENT_DIR).
- tests/test_deep_agent_paths.py::derived_root() — the test oracle that
  independently re-derives the root to pin repo_root() (a copy here is
  the point of the test).

Everywhere else: entry points declare the search path (Dockerfile ENV
PYTHONPATH, package.json test scripts, compose env), tests run from the
repo root with root-relative literals, and cross-component imports resolve
through those entry points. Rationale: per-file sys.path inserts made
imports depend on execution order and cwd, hid missing launcher config,
and broke silently when files moved.

Exit 1 listing file:line on any violation. stdlib only.

Split-statement climbing is also banned: a local assigned a
__file__-derived value (here = abspath(__file__)) must never be
climbed later (dirname(here)) — derive the root once via
deep-agent/cli.py::repo_root instead.
"""

import ast
import os
import sys

FIRST_PARTY = ("backend", "deep-agent", "tests", "scripts")

SYS_PATH_MUTATORS = {"append", "insert", "extend", "remove", "pop", "clear"}

# (relpath, function-or-None) pairs that own the two canonical jobs above.
ALLOW_PATH_MUTATION = {("deep-agent/cli.py", "bootstrap")}
ALLOW_ROOT_CLIMB = {
    ("deep-agent/cli.py", "repo_root"),
    ("backend/api/routers/chat.py", "_repo_root"),
    ("tests/test_deep_agent_paths.py", "derived_root"),
}


def _func_scope(node, parents):
    for parent in reversed(parents):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
    return None


def _is_sys_path(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def check_tree(root):
    violations = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d
            not in (
                ".venv",
                "__pycache__",
                "node_modules",
                ".git",
                "chroma_db",
                ".td_rag",
            )
        )
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            continue
        if rel_dir.split(os.sep)[0] not in FIRST_PARTY:
            continue
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root)
            with open(path, encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=rel)
                except SyntaxError as exc:
                    violations.append(f"{rel}:{exc.lineno}: unparseable")
                    continue
            violations.extend(check_file(rel, tree))
    return violations


def _scope_node(parents):
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return parents[0]


def _tainted_locals(tree):
    """Names assigned __file__-derived values, keyed by scope node.

    Split-statement climbing stashes the __file__ expression in a
    local first (here = abspath(__file__)) and climbs later
    (dirname(dirname(here))), so the per-statement check cannot see
    the link. Only plain-name targets count (mod.__file__ probes in
    tests never feed a climb).
    """
    tainted = {}

    def collect(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                targets, value = node.targets, node.value
            if value is not None and _has_dunder(value):
                names = set()
                for target in targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        names.update(
                            e.id for e in target.elts if isinstance(e, ast.Name)
                        )
                if names:
                    tainted.setdefault(id(scope), set()).update(names)
        for child in ast.iter_child_nodes(node):
            collect(child, scope)

    collect(tree, tree)
    return tainted


def _split_climb(node, tainted):
    """dirname() applied to a __file__-derived local (abspath() on an
    absolute path only normalizes, so it is not a climb)."""
    if not isinstance(node, ast.stmt) or not tainted:
        return False
    for call in ast.walk(node):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "dirname"
        ):
            if any(isinstance(n, ast.Name) and n.id in tainted for n in ast.walk(call)):
                return True
    return False


def check_file(rel, tree):
    violations = []
    parents = []
    tainted = _tainted_locals(tree)

    def visit(node):
        scope_name = _func_scope(node, parents)
        key = (rel, scope_name)
        # sys.path mutation: sys.path.append/insert/...() calls.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in SYS_PATH_MUTATORS and _is_sys_path(node.func.value):
                if key not in ALLOW_PATH_MUTATION:
                    violations.append(
                        f"{rel}:{node.lineno}: sys.path.{node.func.attr} "
                        f"mutation (declare the path at the entry point)"
                    )
        # sys.path assignment/aug-assign/del: sys.path = ..., += ..., del.
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = (
                [node.target]
                if isinstance(node, (ast.AnnAssign, ast.AugAssign))
                else node.targets
            )
            for target in targets:
                if _is_sys_path(target) and key not in ALLOW_PATH_MUTATION:
                    violations.append(
                        f"{rel}:{node.lineno}: sys.path assignment "
                        f"(declare the path at the entry point)"
                    )
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if _is_sys_path(target) and key not in ALLOW_PATH_MUTATION:
                    violations.append(
                        f"{rel}:{node.lineno}: del sys.path "
                        f"(declare the path at the entry point)"
                    )
        # Repo-root climbing from __file__: ".."/pardir constants or
        # stacked .parent climbing in the same statement.
        if key not in ALLOW_ROOT_CLIMB and _climbs_from_dunder(node):
            violations.append(
                f"{rel}:{node.lineno}: __file__ repo-root climbing "
                f"(use root-relative literals; root derivation lives in "
                f"deep-agent/cli.py::repo_root)"
            )
        # Split-statement climbing: a __file__-derived local climbed
        # later in the same scope (here = abspath(__file__),
        # then dirname(here)).
        if key not in ALLOW_ROOT_CLIMB and _split_climb(
            node, tainted.get(id(_scope_node(parents)), ())
        ):
            violations.append(
                f"{rel}:{node.lineno}: __file__ split-statement climbing "
                f"(derive the root once via "
                f"deep-agent/cli.py::repo_root"
            )

    def walk(node):
        parents.append(node)
        visit(node)
        for child in ast.iter_child_nodes(node):
            walk(child)
        parents.pop()

    walk(tree)
    return violations


def _has_dunder(node):
    """__file__ as a bare name or a module attribute (mod.__file__)."""
    return any(
        (isinstance(n, ast.Name) and n.id == "__file__")
        or (isinstance(n, ast.Attribute) and n.attr == "__file__")
        for n in ast.walk(node)
    )


def _climbs_from_dunder(node):
    if not isinstance(node, ast.stmt):
        return False
    if not _has_dunder(node):
        return False
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n.value in ("..", "../..") or "pardir" in n.value:
                return True
            parts = n.value.replace("\\", "/").split("/")
            if ".." in parts:
                return True
        if isinstance(n, ast.Attribute) and n.attr == "pardir":
            return True
    # Nested dirname(): one dirname() stays in the module dir (package
    # data); two or more wrapping a __file__-derived value climbs out.
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "dirname"
        ):
            depth, inner = 0, n
            while (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "dirname"
            ):
                depth += 1
                inner = inner.args[0] if inner.args else None
            if depth >= 2 and inner is not None and _has_dunder(inner):
                return True
    # .parents[N]: parents[0] is the module dir (fine); N>=1 climbs.
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Attribute)
            and n.value.attr == "parents"
        ):
            idx = n.slice
            if isinstance(idx, ast.Constant) and isinstance(idx.value, int):
                if idx.value >= 1:
                    return True
            else:
                return True
    # Stacked .parent access: x.parent.parent (more than one hop).
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "parent":
            depth = 0
            inner = n
            while isinstance(inner, ast.Attribute) and inner.attr == "parent":
                depth += 1
                inner = inner.value
            if depth >= 2:
                return True
    return False


def main():
    # Invoked from the repo root (like every script here): the root is
    # the cwd, located by marker files rather than __file__ climbing.
    root = os.getcwd()
    for marker in ("skills-lock.json", "docker-compose.yml"):
        if not os.path.isfile(os.path.join(root, marker)):
            print(
                f"verify-python-paths: run from the repo root "
                f"(missing {marker} in {root})"
            )
            return 2
    violations = check_tree(root)
    if violations:
        print("verify-python-paths: violations found:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("verify-python-paths: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
