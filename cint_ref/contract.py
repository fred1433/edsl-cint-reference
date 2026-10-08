"""Validate a request body against a schema extracted from Cint's pinned spec
(validation keywords as published; descriptions and examples dropped).

OpenAPI 3.0 semantics (nullable, oneOf, nested types) via openapi-schema-validator.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from openapi_schema_validator import OAS30Validator

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


@lru_cache(maxsize=None)
def _validator(name: str) -> OAS30Validator:
    return OAS30Validator(json.loads((CONTRACTS / f"{name}.json").read_text()))


def contract_errors(name: str, body: Any) -> list[str]:
    """Every violation, as 'path: message'. Empty when the body is valid."""
    errors = sorted(_validator(name).iter_errors(body), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors:
        where = "/".join(map(str, e.absolute_path)) or "<root>"
        what = e.message if len(e.message) <= 160 else f"fails '{e.validator}' at {'/'.join(map(str, e.schema_path))}"
        out.append(f"{where}: {what}")
    return out
