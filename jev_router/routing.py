"""TypeSafe Jev transport and selection policy."""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from .credentials import read_typesafe_key


JEV_URL = "https://api.typesafe.ai/v1/systemone"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/cert.pem")


@dataclass(frozen=True)
class Selection:
    model: str
    reason: str
    confidence: float | None = None


def fallback(settings: dict[str, Any], reason: str) -> Selection:
    return Selection(settings["fallback"], reason)


def required_features(body: dict[str, Any]) -> set[str]:
    features: set[str] = set()
    if body.get("tools"):
        features.add("tools")
    thinking = body.get("thinking")
    if (isinstance(thinking, dict) and thinking.get("type") not in (None, "disabled")) or body.get("reasoning"):
        features.add("thinking")
    if _contains_image(body.get("messages", body.get("input"))):
        features.add("images")
    return features


def _contains_image(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"image", "input_image", "localImage"}:
            return True
        return any(_contains_image(child) for child in value.values())
    return False


def eligible_models(settings: dict[str, Any], body: dict[str, Any]) -> list[dict[str, Any]]:
    features = required_features(body)
    max_tokens = body.get("max_tokens", body.get("max_output_tokens", 0))
    output_limit = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else 0
    return [model for model in settings["models"] if
            all(model.get(f"supports_{feature}", True) for feature in features) and
            (not output_limit or not model.get("max_output_tokens") or model["max_output_tokens"] >= output_limit)]


def build_jev_request(prompt: str, models: list[dict[str, Any]], typesafe_model: str) -> dict[str, Any]:
    criteria = {
        f"option_{index + 1}": f"{model['id']}: {model['description']}. Relative cost weight: {model['cost_weight']}."
        for index, model in enumerate(models)
    }
    return {
        "model": typesafe_model,
        "state": {"task": prompt[:20000]},
        "questions": {"selected_model": {
            "type": "choice",
            "instructions": "Choose the model likely to complete this coding task well with the lowest total expected usage. Weights are rough relative cost proxies for similar work, not measured cost per completed task. Consider likely reasoning and output tokens, tool-loop length, and retries; a stronger model can use fewer total tokens by finishing more reliably. Use the descriptions to judge capability.",
            "criteria": criteria,
        }},
    }


def interpret_answer(data: Any, models: list[dict[str, Any]], settings: dict[str, Any], threshold: float) -> Selection:
    answers = data.get("answers") if isinstance(data, dict) else None
    answer = answers.get("selected_model") if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return fallback(settings, "invalid_answer")
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    if not isinstance(choice, str) or not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        return fallback(settings, "invalid_answer")
    choices = {f"option_{i + 1}": model["id"] for i, model in enumerate(models)}
    if choice not in choices:
        return Selection(settings["fallback"], "invalid_choice", float(confidence))
    if confidence < threshold:
        return Selection(settings["fallback"], "low_confidence", float(confidence))
    return Selection(choices[choice], "jev", float(confidence))


def choose_model(prompt: str | None, client: str, config: dict[str, Any], *, api_key: str | None = None, opener=None) -> Selection:
    settings = config["clients"][client]
    if not prompt or not prompt.strip():
        return fallback(settings, "no_text_prompt")
    # Coding workers need tool access; the MCP call does not expose the full
    # client request or attachments, so other capability flags are advisory.
    models = eligible_models(settings, {"tools": [{"name": "worker"}]})
    if not models:
        return fallback(settings, "no_compatible_model")
    if len(models) == 1:
        return Selection(models[0]["id"], "only_compatible_model")
    key = api_key if api_key is not None else read_typesafe_key()
    if not key:
        return fallback(settings, "jev_unavailable")
    payload = json.dumps(build_jev_request(prompt, models, config["typesafe_model"])).encode("utf-8")
    req = request.Request(
        os.environ.get("JEV_ROUTER_TYPESAFE_URL", JEV_URL),
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout = config["timeout_ms"] / 1000
        if opener is not None:
            response_context = opener(req, timeout=timeout)
        elif ssl.get_default_verify_paths().cafile is None and SYSTEM_CA_BUNDLE.is_file():
            # Some Python installations on macOS lack OpenSSL's expected CA
            # file, while the system CA bundle is available at this path.
            response_context = request.urlopen(req, timeout=timeout, context=ssl.create_default_context(cafile=str(SYSTEM_CA_BUNDLE)))
        else:
            response_context = request.urlopen(req, timeout=timeout)
        with response_context as response:
            data = json.load(response)
        return interpret_answer(data, models, settings, config["min_confidence"])
    except (error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return fallback(settings, "jev_unavailable")
