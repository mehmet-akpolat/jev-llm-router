"""Load and validate the existing JSON router configuration."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("Config must be a JSON object")
    threshold = config.get("min_confidence", 0.7)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ConfigError("min_confidence must be a number from 0 to 1")
    timeout = config.get("timeout_ms", 5000)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 100 <= timeout <= 60000:
        raise ConfigError("timeout_ms must be an integer from 100 to 60000")
    jev_model = config.get("typesafe_model", "jev-latest")
    if not _nonempty(jev_model):
        raise ConfigError("typesafe_model must be a nonempty string")
    clients = config.get("clients")
    if not isinstance(clients, dict) or set(clients) != {"claude", "codex"}:
        raise ConfigError("clients must contain exactly claude and codex")
    for client, settings in clients.items():
        if client not in {"claude", "codex"}:
            raise ConfigError(f"Unsupported client: {client}")
        if not isinstance(settings, dict):
            raise ConfigError(f"clients.{client} must be an object")
        models = settings.get("models")
        if not isinstance(models, list) or not 1 <= len(models) <= 32:
            raise ConfigError(f"clients.{client}.models must contain 1 to 32 models")
        ids: set[str] = set()
        for model in models:
            if not isinstance(model, dict) or not _nonempty(model.get("id")) or not _nonempty(model.get("description")):
                raise ConfigError(f"Each {client} model needs an id and description")
            model_id = model["id"]
            if model_id in ids:
                raise ConfigError(f"Duplicate {client} model id: {model_id}")
            if len(model_id) > 120 or len(model["description"]) > 1000:
                raise ConfigError(f"A {client} model id or description is too long")
            weight = model.get("cost_weight")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight) or weight <= 0:
                raise ConfigError(f"{client} model {model_id} needs a positive cost_weight")
            for field in ("supports_tools", "supports_images", "supports_thinking"):
                if field in model and not isinstance(model[field], bool):
                    raise ConfigError(f"{client} model {model_id}.{field} must be a boolean")
            if "max_output_tokens" in model:
                limit = model["max_output_tokens"]
                if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                    raise ConfigError(f"{client} model {model_id}.max_output_tokens must be positive")
            ids.add(model_id)
        if settings.get("fallback") not in ids:
            raise ConfigError(f"clients.{client}.fallback must name a configured model")
    return {
        **config,
        "typesafe_model": jev_model,
        "min_confidence": float(threshold),
        "timeout_ms": timeout,
    }


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        path = os.environ.get("JEV_ROUTER_CONFIG") or None
    if path is None:
        from .paths import runtime_config_path
        runtime = runtime_config_path()
        local = Path("router-config.json")
        path = runtime if runtime.is_file() else local if local.is_file() else Path(__file__).with_name("router-config.example.json")
    path = Path(path).expanduser().resolve()
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
