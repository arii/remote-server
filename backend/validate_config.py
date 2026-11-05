"""Validate loose repo configs against config/schema.json (issue #202).

Stdlib only. Implements the schema subset the file uses: type,
required, properties, items, enum, minimum, minProperties, local
$ref, and forbidSubstring (rejects a substring anywhere in the
file's JSON, e.g. "@latest" in a pinned config). The .env.example
section instead checks that every non-defaulted ${VAR} in
docker-compose.yml is documented there.
"""

import json
import os
import re
import sys

SCHEMA_REL = os.path.join("config", "schema.json")
ENV_SECTION = ".env.example"
COMPOSE_FILE = "docker-compose.yml"
VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(:[-?][^}]*)?\}"
                    r"|([A-Za-z_][A-Za-z0-9_]*))")


def _resolve(node, defs):
    ref = node.get("$ref", "")
    if ref.startswith("#/$defs/"):
        return defs.get(ref[len("#/$defs/"):], {})
    return node


def _check(data, node, defs, path, errors):
    node = _resolve(node, defs)
    want = node.get("type")
    if want == "object":
        if not isinstance(data, dict):
            errors.append(f"{path}: want object")
            return
        for key in node.get("required", []):
            if key not in data:
                errors.append(f"{path}: missing {key!r}")
        props = node.get("properties", {})
        for key, value in data.items():
            if key in props:
                _check(value, props[key], defs, f"{path}.{key}", errors)
        floor = node.get("minProperties")
        if floor is not None and len(data) < floor:
            errors.append(f"{path}: want >= {floor} properties")
    elif want == "array":
        if not isinstance(data, list):
            errors.append(f"{path}: want array")
            return
        for i, value in enumerate(data):
            _check(value, node.get("items", {}), defs,
                   f"{path}[{i}]", errors)
    elif want == "string":
        if not isinstance(data, str):
            errors.append(f"{path}: want string")
    elif want == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            errors.append(f"{path}: want integer")
    elif want == "boolean":
        if not isinstance(data, bool):
            errors.append(f"{path}: want boolean")
    if "enum" in node and data not in node["enum"]:
        errors.append(f"{path}: not one of {node['enum']!r}")
    floor = node.get("minimum")
    if floor is not None and isinstance(data, (int, float)) \
            and data < floor:
        errors.append(f"{path}: want >= {floor}")


def check_env_keys(root):
    """Vars compose needs (no default) missing from .env.example."""
    with open(os.path.join(root, COMPOSE_FILE), encoding="utf-8") as f:
        compose = f.read()
    # $$ is an escaped dollar, not interpolation.
    compose = compose.replace("$$", "")
    needed = set()
    for m in VAR_RE.finditer(compose):
        if m.group(3):
            # $VAR form carries no default.
            needed.add(m.group(3))
        elif not m.group(2):
            # ${VAR} without :- / :? carries no default.
            needed.add(m.group(1))
    try:
        with open(os.path.join(root, ENV_SECTION), encoding="utf-8") as f:
            documented = set(re.findall(r"^#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=",
                                         f.read(), re.M))
    except OSError:
        return [f"{ENV_SECTION}: file missing"]
    skip = {"HOME", "PATH", "PWD", "USER", "HOSTNAME"}
    return sorted(f"{ENV_SECTION}: undocumented ${v}"
                  for v in needed - documented - skip)


def _check_forbidden(data, needle, path, errors):
    if isinstance(data, str):
        if needle in data:
            errors.append(f"{path}: forbidden {needle!r}")
    elif isinstance(data, dict):
        for key, value in data.items():
            _check_forbidden(value, needle, f"{path}.{key}", errors)
    elif isinstance(data, list):
        for i, value in enumerate(data):
            _check_forbidden(value, needle, f"{path}[{i}]", errors)


def validate_file(root, rel, section, schema):
    """Error strings for one file (empty when valid)."""
    if section.get("type") == "env-keys":
        return check_env_keys(root)
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return [f"{rel}: file missing"]
    except ValueError as exc:
        return [f"{rel}: invalid JSON: {exc}"]
    errors = []
    _check(data, section, schema.get("$defs", {}), rel, errors)
    needle = section.get("forbidSubstring")
    if isinstance(needle, str) and needle:
        _check_forbidden(data, needle, rel, errors)
    return errors


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args and args[0] == "--root":
        if len(args) < 2:
            sys.stderr.write("usage: validate-config [--root DIR]\n")
            return 2
        root = args[1]
    try:
        with open(os.path.join(root, SCHEMA_REL), encoding="utf-8") as f:
            schema = json.load(f)
    except OSError:
        sys.stderr.write(f"{SCHEMA_REL}: schema missing\n")
        return 1
    failed = False
    for rel, section in schema.items():
        if rel.startswith("$"):
            continue
        errors = validate_file(root, rel, section, schema)
        if errors:
            failed = True
            for err in errors:
                print(f"FAIL {err}")
        else:
            print(f"ok {rel}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
