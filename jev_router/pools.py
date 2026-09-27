"""Build validated model-pool overrides from the current and bundled config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import validate_config


def set_pool(config: dict[str, Any], client: str, model_ids: list[str], fallback: str) -> dict[str, Any]:
    if client not in {"claude", "codex"}:
        raise ValueError("--client must be claude or codex")
    if not model_ids or len(model_ids) != len(set(model_ids)):
        raise ValueError("--models must list distinct model IDs")
    bundled = json.loads(Path(__file__).with_name("router-config.example.json").read_text(encoding="utf-8"))
    known = {item["id"]: item for source in (bundled, config)
             for item in source["clients"][client]["models"]}
    missing = [model_id for model_id in model_ids if model_id not in known]
    if missing:
        raise ValueError("Unknown model IDs: " + ", ".join(missing) + ". Use pool import for new model definitions")
    if fallback not in model_ids:
        raise ValueError("--fallback must be one of the selected models")
    revised = json.loads(json.dumps(config))
    revised["clients"][client] = {
        **revised["clients"][client],
        "models": [known[model_id] for model_id in model_ids],
        "fallback": fallback,
    }
    return validate_config(revised)
