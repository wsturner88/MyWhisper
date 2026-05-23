import requests

from . import config


class LLMError(Exception):
    pass


def _active_provider():
    """Return (provider_name, api_key, model) from saved settings."""
    provider = config.get_llm_provider()
    info = config.LLM_PROVIDERS[provider]
    key = config.get_secret(info["key_name"])
    model = config.get_llm_model(provider)
    return provider, key, model


def chat(cfg, system, user, max_tokens=2048):
    provider, key, model = _active_provider()
    if not key:
        raise LLMError(
            f"No API key set for {config.LLM_PROVIDERS[provider]['label']}. "
            f"Set it in MyWhisper → Settings → LLM."
        )
    if provider == "openrouter":
        return _openrouter(key, model, system, user, max_tokens)
    if provider == "anthropic":
        return _anthropic(key, model, system, user, max_tokens)
    raise LLMError(f"Unknown LLM provider: {provider!r}")


def test_connection():
    """Send a tiny request to verify the API key and model work.

    Returns (True, provider_label) on success or (False, error_message) on
    failure.
    """
    provider, key, model = _active_provider()
    label = config.LLM_PROVIDERS[provider]["label"]
    if not key:
        return False, f"No API key set for {label}."
    try:
        reply = chat(
            {},  # cfg not used by chat() anymore
            "Reply with exactly: OK",
            "Say OK",
            max_tokens=8,
        )
        if reply:
            return True, label
        return False, "Got an empty response."
    except Exception as e:
        return False, str(e)


def _openrouter(key, model, system, user, max_tokens):
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _anthropic(key, model, system, user, max_tokens):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()
