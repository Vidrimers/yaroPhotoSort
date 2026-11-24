"""Провайдеры моделей: LM Studio (локально) и облако."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
STATS_PATH = ROOT / "results" / "usage_stats.json"


# ────────────────────────────── .env ──────────────────────────────

def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ────────────────────────────── цены (USD за 1M токенов, ориентир) ──────────────────────────────

PRICING: dict[str, dict[str, float]] = {
    # provider/model : input, output
    "lmstudio": {},
    "ollama": {},
    "openai": {
        "gpt-4o": {"in": 2.5, "out": 10.0},
        "gpt-4o-mini": {"in": 0.15, "out": 0.6},
        "gpt-4.1": {"in": 2.0, "out": 8.0},
        "gpt-4.1-mini": {"in": 0.4, "out": 1.6},
    },
    "anthropic": {
        "claude-sonnet-4": {"in": 3.0, "out": 15.0},
        "claude-3-5-sonnet-latest": {"in": 3.0, "out": 15.0},
        "claude-3-5-haiku-latest": {"in": 0.8, "out": 4.0},
    },
    "gemini": {
        "gemini-2.0-flash": {"in": 0.1, "out": 0.4},
        "gemini-1.5-pro": {"in": 1.25, "out": 5.0},
        "gemini-1.5-flash": {"in": 0.075, "out": 0.3},
    },
    "qwen": {
        "qwen-vl-max": {"in": 0.8, "out": 3.2},
        "qwen-vl-plus": {"in": 0.15, "out": 0.6},
    },
    "mimo": {
        "mimo-v2.6-pro": {"in": 0.0, "out": 0.0},
        "mimo-v2.6-flash": {"in": 0.0, "out": 0.0},
        "mimo-v2.6-pro-ultraspeed": {"in": 0.0, "out": 0.0},
    },
    "colab": {},
    "nvidia": {
        "meta/llama-3.2-11b-vision-instruct": {"in": 0.0, "out": 0.0},
        "meta/llama-3.2-90b-vision-instruct": {"in": 0.0, "out": 0.0},
        "google/gemma-3-27b-it": {"in": 0.0, "out": 0.0},
    },
    "openrouter": {},
}

DEFAULT_MODELS: dict[str, str] = {
    "lmstudio": "google/gemma-3-12b",
    "ollama": "llava",
    "colab": "google/gemma-3-12b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-2.0-flash",
    "qwen": "qwen-vl-plus",
    "mimo": "mimo-v2.6-pro",
    "nvidia": "meta/llama-3.2-11b-vision-instruct",
    "openrouter": "google/gemma-3-12b",
}

DEFAULT_BASES: dict[str, str] = {
    "lmstudio": "http://127.0.0.1:1234",
    "ollama": "http://127.0.0.1:11434",
    "colab": "",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode",
    # token-plan key works here; api.xiaomimimo.com/v1 401s for tp- keys
    "mimo": "https://token-plan-sgp.xiaomimimo.com",
    "nvidia": "https://integrate.api.nvidia.com",
    "openrouter": "https://openrouter.ai/api",
}

ENV_KEYS: dict[str, str] = {
    "lmstudio": "",
    "ollama": "",
    "colab": "COLAB_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "qwen": "QWEN_API_KEY",
    "mimo": "MIMO_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

PROVIDER_LABELS: dict[str, str] = {
    "lmstudio": "LM Studio",
    "ollama": "Ollama",
    "colab": "Google Colab",
    "openai": "ChatGPT",
    "anthropic": "Claude",
    "gemini": "Gemini",
    "qwen": "Qwen",
    "mimo": "MiMo",
    "nvidia": "NVIDIA",
    "openrouter": "OpenRouter",
}

LOCAL_PROVIDERS = ("lmstudio", "ollama")


@dataclass
class ModelReply:
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    sec: float = 0.0
    error: str = ""


# ────────────────────────────── статистика ──────────────────────────────

def load_stats() -> dict[str, Any]:
    if STATS_PATH.exists():
        try:
            return json.loads(STATS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "by_provider": {},
    }


def save_stats(stats: dict[str, Any]) -> None:
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def record_usage(provider: str, model: str, tin: int, tout: int) -> float:
    stats = load_stats()
    price = (PRICING.get(provider) or {}).get(model) or {}
    cost = (tin / 1_000_000) * price.get("in", 0.0) + (tout / 1_000_000) * price.get("out", 0.0)
    stats["requests"] = int(stats.get("requests", 0)) + 1
    stats["input_tokens"] = int(stats.get("input_tokens", 0)) + tin
    stats["output_tokens"] = int(stats.get("output_tokens", 0)) + tout
    stats["cost_usd"] = round(float(stats.get("cost_usd", 0.0)) + cost, 6)
    bp = stats.setdefault("by_provider", {})
    row = bp.setdefault(provider, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    row["requests"] = int(row.get("requests", 0)) + 1
    row["input_tokens"] = int(row.get("input_tokens", 0)) + tin
    row["output_tokens"] = int(row.get("output_tokens", 0)) + tout
    row["cost_usd"] = round(float(row.get("cost_usd", 0.0)) + cost, 6)
    save_stats(stats)
    return cost


def reset_stats() -> dict[str, Any]:
    stats = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "by_provider": {},
    }
    save_stats(stats)
    return stats


# ────────────────────────────── ключи / база ──────────────────────────────

def api_key_for(provider: str) -> str:
    env = load_env()
    env_name = ENV_KEYS.get(provider, "")
    key = env.get(env_name, "") if env_name else ""
    if key:
        return key
    return os.environ.get(env_name, "") if env_name else ""


def save_api_key(provider: str, key: str) -> str:
    """Пишет ключ в .env (создаёт/обновляет строку). Возвращает имя переменной."""
    env_name = ENV_KEYS.get(provider, "")
    if not env_name:
        raise ValueError(f"No env key name for provider {provider}")
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        if line.strip().startswith(env_name + "="):
            out.append(f"{env_name}={key}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{env_name}={key}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_name


def save_base_url(provider: str, url: str) -> str:
    """Пишет base URL в .env, например COLAB_BASE_URL=..."""
    env_name = f"{provider.upper()}_BASE_URL"
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        if line.strip().startswith(env_name + "="):
            out.append(f"{env_name}={url}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{env_name}={url}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_name


def base_for(provider: str, override: str | None = None) -> str:
    env = load_env()
    name = f"{provider.upper()}_BASE_URL"
    alt = f"{provider.upper()}_URL"
    return (
        override
        or env.get(name)
        or env.get(alt)
        or DEFAULT_BASES.get(provider)
        or ""
    ).rstrip("/")


# ────────────────────────────── запросы ──────────────────────────────

def _openai_like(
    base: str,
    api_key: str,
    model: str,
    prompt: str,
    data_urls: list[str],
    max_tokens: int,
    extra_headers: dict[str, str] | None = None,
) -> ModelReply:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    content: list[dict[str, Any]] = []
    for url in data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    t0 = time.time()
    r = requests.post(f"{base}/v1/chat/completions", json=payload, headers=headers, timeout=120)
    dt = time.time() - t0
    if r.status_code != 200:
        return ModelReply(error=f"HTTP {r.status_code}: {r.text[:200]}", sec=dt)
    data = r.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage") or {}
    return ModelReply(
        text=text or "",
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        sec=round(dt, 2),
    )


def _anthropic(
    base: str,
    api_key: str,
    model: str,
    prompt: str,
    data_urls: list[str],
    max_tokens: int,
) -> ModelReply:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    content: list[dict[str, Any]] = []
    for data_url in data_urls:
        m = re.match(r"data:([^;]+);base64,(.*)", data_url, re.S)
        media = m.group(1) if m else "image/jpeg"
        b64 = m.group(2) if m else data_url
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": b64},
            }
        )
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    t0 = time.time()
    r = requests.post(f"{base}/v1/messages", json=payload, headers=headers, timeout=120)
    dt = time.time() - t0
    if r.status_code != 200:
        return ModelReply(error=f"HTTP {r.status_code}: {r.text[:200]}", sec=dt)
    data = r.json()
    text = ""
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")
    usage = data.get("usage") or {}
    return ModelReply(
        text=text,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        sec=round(dt, 2),
    )


def _gemini(
    base: str,
    api_key: str,
    model: str,
    prompt: str,
    data_urls: list[str],
    max_tokens: int,
) -> ModelReply:
    parts: list[dict[str, Any]] = []
    for data_url in data_urls:
        m = re.match(r"data:([^;]+);base64,(.*)", data_url, re.S)
        b64 = m.group(2) if m else data_url
        mime = m.group(1) if m else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    parts.append({"text": prompt})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
    }
    t0 = time.time()
    r = requests.post(
        f"{base}/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        json=payload,
        timeout=120,
    )
    dt = time.time() - t0
    if r.status_code != 200:
        return ModelReply(error=f"HTTP {r.status_code}: {r.text[:200]}", sec=dt)
    data = r.json()
    text = ""
    try:
        for part in data["candidates"][0]["content"]["parts"]:
            text += part.get("text", "")
    except Exception:
        text = json.dumps(data)[:300]
    usage = data.get("usageMetadata") or {}
    return ModelReply(
        text=text,
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0),
        sec=round(dt, 2),
    )


def ask_model(
    provider: str,
    model: str,
    prompt: str,
    data_url: str,
    max_tokens: int = 220,
    base_override: str | None = None,
) -> ModelReply:
    return ask_model_multi(provider, model, prompt, [data_url], max_tokens, base_override)


def ask_model_multi(
    provider: str,
    model: str,
    prompt: str,
    data_urls: list[str],
    max_tokens: int = 220,
    base_override: str | None = None,
) -> ModelReply:
    base = base_for(provider, base_override)
    key = api_key_for(provider)
    if provider == "lmstudio":
        reply = _openai_like(base, key, model, prompt, data_urls, max_tokens)
    elif provider == "anthropic":
        if not key:
            return ModelReply(error="ANTHROPIC_API_KEY is empty")
        reply = _anthropic(base, key, model, prompt, data_urls, max_tokens)
    elif provider == "gemini":
        if not key:
            return ModelReply(error="GEMINI_API_KEY is empty")
        reply = _gemini(base, key, model, prompt, data_urls, max_tokens)
    else:
        # openai / qwen / mimo / openrouter / colab
        if not key and provider not in ("colab",):
            env_name = ENV_KEYS.get(provider, "API_KEY")
            return ModelReply(error=f"{env_name} is empty")
        extra = {}
        if provider == "openrouter":
            extra["HTTP-Referer"] = "https://localhost"
            extra["X-Title"] = "yaroPhotoSort"
        reply = _openai_like(base, key, model, prompt, data_urls, max_tokens, extra)

    if not reply.error and (reply.input_tokens or reply.output_tokens):
        record_usage(provider, model, reply.input_tokens, reply.output_tokens)
    return reply


def provider_status(provider: str, model: str | None = None) -> dict[str, Any]:
    model = model or DEFAULT_MODELS.get(provider, "")
    key = api_key_for(provider)
    out: dict[str, Any] = {
        "provider": provider,
        "label": PROVIDER_LABELS.get(provider, provider),
        "model": model,
        "has_key": bool(key),
        "base": base_for(provider),
        "local": provider in LOCAL_PROVIDERS,
        "ok": False,
        "detail": "",
        "usage": load_stats(),
    }
    if provider in LOCAL_PROVIDERS or provider == "colab":
        if provider == "colab" and not base_for(provider):
            out["detail"] = "Base URL is empty"
            return out
        try:
            r = requests.get(f"{out['base']}/v1/models", timeout=3)
            out["ok"] = r.status_code == 200
            out["detail"] = "up" if out["ok"] else f"HTTP {r.status_code}"
        except Exception as e:
            out["detail"] = str(e)
        return out

    if not key:
        out["detail"] = "API key missing"
        return out
    out["ok"] = True
    out["detail"] = "key loaded"
    return out


def test_connection(provider: str, model: str | None = None) -> dict[str, Any]:
    """Реальный запрос /v1/models (или ping) с текущим ключом."""
    model = model or DEFAULT_MODELS.get(provider, "")
    base = base_for(provider)
    key = api_key_for(provider)
    out: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "base": base,
        "ok": False,
        "status": 0,
        "detail": "",
        "models": [],
    }
    if provider == "colab" and not base:
        out["detail"] = "Base URL is empty - paste the tunnel URL first"
        return out
    try:
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider == "anthropic":
            # у Anthropic нет публичного /v1/models с тем же ключом на старых ключах,
            # проверяем минимальный messages-запрос
            if not key:
                out["detail"] = "ANTHROPIC_API_KEY is empty"
                return out
            r = requests.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=30,
            )
            out["status"] = r.status_code
            out["ok"] = r.status_code == 200
            out["detail"] = r.text[:160] if not out["ok"] else "anthropic ok"
            return out

        r = requests.get(f"{base}/v1/models", headers=headers, timeout=15)
        out["status"] = r.status_code
        if r.status_code == 200:
            data = r.json()
            ids = []
            for m in data.get("data") or []:
                mid = m.get("id")
                if mid:
                    ids.append(mid)
            out["models"] = ids[:30]
            out["ok"] = True
            out["detail"] = f"{len(ids)} models"
        else:
            out["detail"] = r.text[:160]
    except Exception as e:
        out["detail"] = str(e)
    return out
