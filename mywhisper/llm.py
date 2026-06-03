import json
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


def chat(cfg, system, user, max_tokens=2048, on_token=None):
    """Run an LLM chat. If on_token is provided, stream tokens to it as
    they arrive (called with each text chunk). Always returns the full
    response text at the end.

    For older non-streaming callers, just omit on_token.
    """
    provider, key, model = _active_provider()
    info = config.LLM_PROVIDERS[provider]
    if info.get("key_name") and not info.get("key_optional") and not key:
        raise LLMError(
            f"No API key set for {info['label']}. "
            f"Set it in MyWhisper → Dashboard → Settings → LLM."
        )
    if provider == "openrouter":
        return _openrouter(key, model, system, user, max_tokens, on_token)
    if provider == "anthropic":
        return _anthropic(key, model, system, user, max_tokens, on_token)
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
        return _custom_chat(url, model, system, user, max_tokens,
                            auth_token=key, on_token=on_token)
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

    Returns (True, message) on success or (False, error_message) on
    failure.
    """
    provider, key, model = _active_provider()
    info = config.LLM_PROVIDERS[provider]
    label = info["label"]
    if info.get("key_name") and not info.get("key_optional") and not key:
        return False, f"No API key set for {label}."
    if info.get("needs_url") and not config.get_custom_llm_url():
        return False, f"No server URL set for {label}."
    if not model:
        return False, "No model selected — pick one from the dropdown."

    # For Custom LLM we already know the URL responds (we just listed
    # models). Verify the chosen model actually exists on the server.
    if provider == "custom":
        try:
            url = config.get_custom_llm_url()
            available = _custom_models(url, auth_token=key)
            ids = {m["id"] for m in available}
            if model not in ids:
                return False, (
                    f"Model '{model}' isn't on the server. Refresh the "
                    f"model list and pick a different one."
                )
        except Exception as e:
            return False, str(e)

    # Do an actual completion — bigger token budget and a plain prompt so
    # we get past any "thinking" preamble small models add.
    try:
        reply = chat(
            {},
            "You are a helpful assistant. Reply briefly.",
            "Say the word: hello",
            max_tokens=128,
        )
        if reply.strip():
            preview = reply.strip()[:60]
            return True, f"{label} — '{preview}'"
        # No error, but no text either. The link is good — model probably
        # warmed up but emitted nothing visible this round.
        return True, (
            f"{label} connected. The model returned no text (it may be "
            f"warming up — try a real meeting and it should work)."
        )
    except requests.exceptions.Timeout:
        return False, (
            "Timed out. Local models can take 10–30 seconds the very "
            "first time they're loaded — try Test Connection again."
        )
    except Exception as e:
        return False, str(e)


def _openrouter(key, model, system, user, max_tokens, on_token=None):
    if on_token is None:
        # Non-streaming fast path
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
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    # Streaming via OpenAI-style SSE
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
            "stream": True,
        },
        stream=True,
        timeout=600,
    )
    resp.raise_for_status()
    return _consume_openai_sse(resp, on_token)


def _anthropic(key, model, system, user, max_tokens, on_token=None):
    if on_token is None:
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
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    # Streaming via Anthropic SSE
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
            "stream": True,
        },
        stream=True,
        timeout=600,
    )
    resp.raise_for_status()
    return _consume_anthropic_sse(resp, on_token)


def _consume_openai_sse(resp, on_token):
    """OpenAI-style SSE: 'data: {json}\\n\\n' lines with content deltas."""
    full = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        try:
            delta = obj["choices"][0]["delta"].get("content") or ""
        except (KeyError, IndexError):
            delta = ""
        if delta:
            full.append(delta)
            try:
                on_token(delta)
            except Exception:
                pass
    return "".join(full).strip()


def _consume_anthropic_sse(resp, on_token):
    """Anthropic SSE: 'event: ...' and 'data: {json}' lines.
    Text deltas live in `content_block_delta` events under .delta.text.
    """
    full = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if obj.get("type") == "content_block_delta":
            delta = (obj.get("delta") or {}).get("text") or ""
            if delta:
                full.append(delta)
                try:
                    on_token(delta)
                except Exception:
                    pass
        elif obj.get("type") == "message_stop":
            break
    return "".join(full).strip()


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


def _custom_chat(url, model, system, user, max_tokens, auth_token=None,
                 on_token=None):
    base = _custom_base(url)
    style = _detect_custom_api(base, auth_token=auth_token)
    headers = _auth_headers(auth_token)
    if style == "ollama":
        if on_token is None:
            r = requests.post(
                f"{base}/api/chat", headers=headers,
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
        # Streaming Ollama — line-delimited JSON, each line has .message.content
        r = requests.post(
            f"{base}/api/chat", headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": True,
                "options": {"num_predict": max_tokens},
            },
            stream=True,
            timeout=600,
        )
        r.raise_for_status()
        return _consume_ollama_stream(r, on_token)
    # openai-compatible
    if on_token is None:
        r = requests.post(
            f"{base}/v1/chat/completions", headers=headers,
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
    # Streaming openai-compat (LM Studio, vLLM, etc.)
    r = requests.post(
        f"{base}/v1/chat/completions", headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "stream": True,
        },
        stream=True,
        timeout=600,
    )
    r.raise_for_status()
    return _consume_openai_sse(r, on_token)


def _consume_ollama_stream(resp, on_token):
    """Ollama streams newline-delimited JSON. Each line has
    {'message': {'content': '...'}} until done==true."""
    full = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        delta = (obj.get("message") or {}).get("content") or ""
        if delta:
            full.append(delta)
            try:
                on_token(delta)
            except Exception:
                pass
        if obj.get("done"):
            break
    return "".join(full).strip()
