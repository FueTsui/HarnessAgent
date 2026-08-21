"""Codex ChatGPT-account model discovery and legacy-name compatibility."""
from __future__ import annotations

import json
from pathlib import Path


DEFAULT_CHATGPT_CODEX_MODEL = "gpt-5.6-sol"
FALLBACK_CHATGPT_CODEX_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
)
LEGACY_CHATGPT_CODEX_MODELS = frozenset({
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5.1",
    "gpt-5.1-codex",
})


def codex_home() -> Path:
    return Path.home() / ".codex"


def available_chatgpt_codex_models(home: Path | None = None) -> list[str]:
    """Return the models exposed by this machine's Codex model catalog.

    The ChatGPT Codex transport has no standard ``/v1/models`` endpoint.  Codex
    already maintains an account-scoped cache, so use that catalog and retain a
    current fallback for first-run or temporarily unreadable cache files.
    """
    cache_path = (home or codex_home()) / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return list(FALLBACK_CHATGPT_CODEX_MODELS)

    result: list[str] = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict) or item.get("supported_in_api") is False:
            continue
        slug = str(item.get("slug") or "").strip()
        # Internal routing/review aliases are not general chat models.
        if not slug.startswith("gpt-") or slug.endswith("-wm") or slug in result:
            continue
        result.append(slug)
    return result or list(FALLBACK_CHATGPT_CODEX_MODELS)


def preferred_chatgpt_codex_model(home: Path | None = None) -> str:
    """Choose the user's configured Codex model when it exists in the catalog."""
    root = home or codex_home()
    models = available_chatgpt_codex_models(root)
    try:
        import tomllib

        config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
        configured = str(config.get("model") or "").strip()
        if configured in models:
            return configured
    except (OSError, ValueError, TypeError):
        pass
    if DEFAULT_CHATGPT_CODEX_MODEL in models:
        return DEFAULT_CHATGPT_CODEX_MODEL
    return models[0]


def normalize_chatgpt_codex_model(model: str | None, home: Path | None = None) -> str:
    """Migrate only empty or known-incompatible legacy ChatGPT model names."""
    value = str(model or "").strip()
    if not value or value in LEGACY_CHATGPT_CODEX_MODELS:
        return preferred_chatgpt_codex_model(home)
    return value
