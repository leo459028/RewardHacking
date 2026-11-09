"""OpenAPI compatibility contract (v1.6.0 D28).

Reduces an OpenAPI document to one entry per (path, method): parameter names, and digests of the
fully resolved request-body and 200-response schemas. A later release must keep every baseline entry
unchanged; new paths are allowed.

Regenerate the baseline only when a response shape is intentionally changed:
    python -m tests.openapi_contract tests/fixtures/v160/openapi_v1512.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_METHODS = ("get", "post", "put", "patch", "delete")


def _resolve(node: Any, schemas: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                return {"$cycle": name}
            return _resolve(schemas.get(name, {}), schemas, seen + (name,))
        return {key: _resolve(value, schemas, seen) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(item, schemas, seen) for item in node]
    return node


def _digest(node: Any) -> str:
    if node is None:
        return ""
    canonical = json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def contract(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    schemas = dict((spec.get("components") or {}).get("schemas") or {})
    result: dict[str, dict[str, Any]] = {}
    for path, item in sorted((spec.get("paths") or {}).items()):
        for method in _METHODS:
            operation = item.get(method)
            if not operation:
                continue
            body = (((operation.get("requestBody") or {}).get("content") or {}))
            body_schema = {ctype: _resolve(content.get("schema"), schemas) for ctype, content in sorted(body.items())}
            ok = (operation.get("responses") or {}).get("200") or {}
            ok_schema = {
                ctype: _resolve(content.get("schema"), schemas)
                for ctype, content in sorted((ok.get("content") or {}).items())
            }
            params = sorted(f"{p.get('in')}:{p.get('name')}" for p in operation.get("parameters") or [])
            result[f"{method.upper()} {path}"] = {
                "params": params,
                "request": _digest(body_schema or None),
                "response_200": _digest(ok_schema or None),
            }
    return result


def current_contract() -> dict[str, dict[str, Any]]:
    from app.main import app

    return contract(app.openapi())


def main(argv: list[str]) -> int:
    target = Path(argv[1] if len(argv) > 1 else "tests/fixtures/v160/openapi_v1512.json")
    data = current_contract()
    target.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(data)} operations to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
