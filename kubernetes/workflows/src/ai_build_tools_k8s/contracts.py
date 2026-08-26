from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    pass


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def require_sha256(value: str, field: str) -> None:
    require(
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{field} must be a lowercase sha256 digest",
    )
