"""Extract the request schema of create_target_group from Cint's pinned OpenAPI spec.

    python scripts/extract_contract.py [path/to/demand_openapi_v1_2025-12-18.yaml]

Without a path, downloads the spec. Refuses any file whose SHA-256 differs from the
pinned one, then writes contracts/create_draft_target_group.json: the request schema
plus every component it references. Only the non-normative "description" and
"example" keys are dropped; every validation keyword is kept as published.
"""

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import yaml

SPEC_URL = "https://developer.cint.com/demand/specs/demand_openapi_v1_2025-12-18.yaml"
SPEC_SHA256 = "008c54ee13d77341026cb9d5cdd06d2493350da66923b243e6d70f79ee904940"
ROOT = "CreateDraftTargetGroupRequest"
OUT = Path(__file__).resolve().parent.parent / "contracts" / "create_draft_target_group.json"


def refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                yield v
            else:
                yield from refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs(v)


def strip_docs(node):
    if isinstance(node, dict):
        return {k: strip_docs(v) for k, v in node.items() if k not in ("description", "example")}
    if isinstance(node, list):
        return [strip_docs(v) for v in node]
    return node


def main() -> None:
    raw = Path(sys.argv[1]).read_bytes() if len(sys.argv) > 1 else urllib.request.urlopen(SPEC_URL).read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SPEC_SHA256:
        sys.exit(f"spec SHA-256 {digest} differs from pinned {SPEC_SHA256}")
    schemas = yaml.safe_load(raw)["components"]["schemas"]
    keep, todo = {}, [ROOT]
    while todo:
        name = todo.pop()
        if name in keep:
            continue
        keep[name] = schemas[name]
        for ref in refs(schemas[name]):
            assert ref.startswith("#/components/schemas/"), ref
            todo.append(ref.rsplit("/", 1)[-1])
    doc = {
        "x-source": {"url": SPEC_URL, "sha256": SPEC_SHA256, "root": ROOT},
        "$ref": f"#/components/schemas/{ROOT}",
        "components": {"schemas": strip_docs(dict(sorted(keep.items())))},
    }
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(keep)} schemas)")


if __name__ == "__main__":
    main()
