import requests

from . import config


class LLMError(Exception):
    pass


def _active_provider():
    """Return (provider_name, api_key, model) from saved settings."""
    provider = config.get_llm_provider()
    info = config.LLM_PROVIDERS[provider]
    key = config.get_secret(info["key_name"]) if info.get("key_name") else None
    model = config.get_llm_model(provider)
    return provider, key, model


def chat(cfg, system, user, max_tokens=2048):
    provider, key, model = _active_provider()
    info = config.LLM_PROVIDERS[provider]
    # Only require the key if it's *not* marked optional.
    if info.get("key_name") and not info.get("key_optional") and not key:
        raise LLMError(
            f"No API key set for {info['label']}. "
            f"Set it in MyWhisper → Dashboard → Settings → LLM."
        )
    if provider == "openrouter":
        return _openrouter(key, model, system, user, max_tokens)
    if provider == "anthropic":
        return _anthropic(key, model, system, user, max_tokens)
    if provider == "custom":
        url = config.get_custom_llm_url()
        if not url:
            raise LLMError(
                "Custom LLM URL not set. Add it in Dashboard → Settings → LLM."
            )
        if not model:
            raise LLMError(
                "No model selected. Pick one from the dropdown after the "
                "server connects."
            )
        return _custom_chat(url, model, system, user, max_tokens, auth_token=key)
    raise LLMError(f"Unknown LLM provider: {provider!r}")


def list_models():
    """Fetch the list of available models from the active provider.

    Returns a list of {"id": str, "label": str} dicts, or raises LLMError.
    """
    provider, key, _ = _active_provider()
    if provider == "openrouter":
        return _openrouter_models()
    if provider == "anthropic":
        if not key:
            raise LLMError(
                "Anthropic needs an API key to list models. Set it above first."
            )
        return _anthropic_models(key)
    if provider == "custom":
        url = config.get_custom_llm_url()
        if not url:
            raise LLMError(
                "Set the Custom LLM URL above first (e.g. http://llm.local:11434)."
            )
        return _custom_models(url, auth_token=key)
    raise LLMError(f"Unknown LLM provider: {provider!r}")


def _openrouter_models():
    resp = requests.get(
        "https://openrouter.ai/api/v1/models",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", []) or []
    models = []
    for m in data:
        mid = m.get("id")
        if not mid:
            continue
        models.append({"id": mid, "label": m.get("name") or mid})
    models.sort(key=lambda m: m["label"].lower())
    return models


def _anthropic_models(key):
    resp = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", []) or []
    models = []
    for m in data:
        mid = m.get("id")
        if not mid:
            continue
        models.append({"id": mid, "label": m.get("display_name") or mid})
    # Newest first — Anthropic returns these created_at-sorted descending.
    return models


def test_connection():
    """Send a tiny request to verify the connection works.

    Returns (True, provider_label) on success or (False, error_message) on
    failure.
    """
    provider, key, model = _active_provider()
    info = config.LLM_PROVIDERS[provider]
    label = info["label"]
    if info.get("key_name") and not key:
        return False, f"No API key set for {label}."
    if info.get("needs_url") and not config.get_custom_llm_url():
        return False, f"No server URL set for {label}."
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


# -- Custom LLM (Ollama / OpenAI-compatible self-hosted) -------------------

# Remembers what flavor of API a given base URL speaks, so we only probe
# once per session.
_custom_api_cache = {}


def _custom_base(url):
    """Normalize the base URL — strip trailing slash and /v1 suffix."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u


def _auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


def _detect_custom_api(base, auth_token=None):
    """Probe the server and figure out whether it speaks Ollama or
    OpenAI-compatible. Returns 'ollama' or 'openai'. Cached per base URL."""
    if base in _custom_api_cache:
        return _custom_api_cache[base]
    headers = _auth_headers(auth_token)
    # Ollama: /api/tags returns {"models": [...]}
    try:
        r = requests.get(f"{base}/api/tags", headers=headers, timeout=4)
        if r.ok and "models" in r.json():
            _custom_api_cache[base] = "ollama"
            return "ollama"
    except Exception:
        pass
    # OpenAI-compatible: /v1/models returns {"data": [...]}
    try:
        r = requests.get(f"{base}/v1/models", headers=headers, timeout=4)
        if r.ok and "data" in r.json():
            _custom_api_cache[base] = "openai"
            return "openai"
    except Exception:
        pass
    raise LLMError(
        f"Couldn't reach an LLM API at {base}. Make sure the server is "
        f"running and the URL is correct (e.g. http://llm.local:11434 for "
        f"Ollama, or http://localhost:1234 for LM Studio)."
    )


def _custom_models(url, auth_token=None):
    base = _custom_base(url)
    style = _detect_custom_api(base, auth_token=auth_token)
    headers = _auth_headers(auth_token)
    if style == "ollama":
        r = requests.get(f"{base}/api/tags", headers=headers, timeout=8)
        r.raise_for_status()
        data = r.json().get("models", []) or []
        models = []
        for m in data:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            size_gb = (m.get("size") or 0) / 1e9
            label = f"{name}" if size_gb < 0.1 else f"{name}  ({size_gb:.1f} GB)"
            models.append({"id": name, "label": label})
        models.sort(key=lambda m: m["label"].lower())
        return models
    # openai-compatible
    r = requests.get(f"{base}/v1/models", headers=headers, timeout=8)
    r.raise_for_status()
    data = r.json().get("data", []) or []
    models = []
    for m in data:
        mid = m.get("id")
        if not mid:
            continue
        models.append({"id": mid, "label": mid})
    models.sort(key=lambda m: m["label"].lower())
    return models


def _custom_chat(url, model, system, user, max_tokens, auth_token=None):
    base = _custom_base(url)
    style = _detect_custom_api(base, auth_token=auth_token)
    headers = _auth_headers(auth_token)
    if style == "ollama":
        r = requests.post(
            f"{base}/api/chat",
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
            timeout=600,
        )
        r.raise_for_status()
        return (r.json().get("message", {}).get("content") or "").strip()
    # openai-compatible
    r = requests.post(
        f"{base}/v1/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        },
        timeout=600,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()
