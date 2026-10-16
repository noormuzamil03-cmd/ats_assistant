"""AI provider layer (free APIs). Settings come from site_config.json, never from the database.

Main provider:
    ats_ai_provider : "gemini" (default) | "groq" | "openai"
    ats_ai_api_key  : API key
    ats_ai_model    : optional, a sensible default is used per provider
Backup provider (used automatically when the main one is busy / over its free limit):
    ats_ai_fallback_provider, ats_ai_fallback_api_key, ats_ai_fallback_model
Other:
    ats_ai_base_url : only for "openai" (any OpenAI-compatible API)
    ats_ai_timeout  : seconds (default 60)
"""

import json
import time

import frappe
import requests
from frappe.utils import cstr

PRESETS = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-3.8-flash"},
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": ""},
}


class LLMError(Exception):
    pass


class RetryableError(LLMError):
    """Rate limit / server busy: try the backup provider."""


def _profile(prefix):
    conf = frappe.conf
    provider = (conf.get(prefix + "provider") or ("gemini" if prefix == "ats_ai_" else "")).strip().lower()
    key = (conf.get(prefix + "api_key") or "").strip()
    if not provider or not key:
        return None
    preset = PRESETS.get(provider, PRESETS["openai"])
    model = (conf.get(prefix + "model") or preset["model"]).strip()
    base = (conf.get(prefix + "base_url") or conf.get("ats_ai_base_url") or preset["base_url"]).rstrip("/")
    if not model:
        raise LLMError(f"Set the model: bench --site SITE set-config {prefix}model \"...\"")
    return frappe._dict(provider=provider, key=key, model=model, base_url=base,
                        timeout=int(conf.get("ats_ai_timeout") or 60))


def _model_list(value):
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [cstr(m).strip() for m in value if cstr(m).strip()]


def profiles():
    """Main model, then extra models on the same key (each model has its own free limit), then the backup."""
    main = _profile("ats_ai_")
    if not main:
        raise LLMError('AI API key is not set. Run: bench --site SITE set-config ats_ai_api_key "YOUR-KEY"')
    out = [main]
    for model in _model_list(frappe.conf.get("ats_ai_extra_models")):
        if model not in [x.model for x in out]:
            out.append(frappe._dict(main, model=model))
    backup = _profile("ats_ai_fallback_")
    if backup:
        out.append(backup)
    return out


def _down_key(p):
    return f"ats_ai_down:{p.provider}:{p.model}"


def chat(system, messages, tools):
    """messages (neutral format):
        {"role": "user", "content": str}
        {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
        {"role": "tool", "tool_call_id": str, "name": str, "content": str}
    Returns {"text": str, "tool_calls": [...], "provider": str}.
    """
    errors = []
    plist = profiles()
    # Skip models that hit their free limit a moment ago; if all are resting, try them all anyway
    ready = [p for p in plist if not _cache_get(_down_key(p))] or plist
    for idx, p in enumerate(ready):
        last = idx == len(ready) - 1
        try:
            result = _openai_compatible(p, system, messages, tools, patient=last)
            result["provider"] = p.provider
            result["model"] = p.model
            return result
        except RetryableError as e:  # busy / free limit: rest this model and move to the next one now
            errors.append(str(e))
            _cache_set(_down_key(p), getattr(e, "wait", None) or 30)
        except LLMError as e:  # wrong key / model not found: rest it longer
            errors.append(str(e))
            _cache_set(_down_key(p), 300)
    raise LLMError(" | ".join(errors) or "No AI provider available.")


def _cache_get(key):
    try:
        return frappe.cache().get_value(key)
    except Exception:
        return None


def _cache_set(key, seconds=60):
    try:
        frappe.cache().set_value(key, 1, expires_in_sec=max(5, min(int(seconds), 600)))
    except Exception:
        pass


def _post(p, payload, patient=True):
    """patient=False: if the model is busy for more than a few seconds, give up at once so the
    next model can answer (much faster than waiting). The last model in the list is patient."""
    headers = {"Authorization": f"Bearer {p.key}", "Content-Type": "application/json"}
    attempts = 3 if patient else 2
    for attempt in range(attempts):
        r = _send(p, headers, payload)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
            wait = _wait_seconds(r, attempt)
            if not patient and wait > 3:
                break
            time.sleep(wait)
            continue
        break
    return _parse(p, r)


def _wait_seconds(r, attempt):
    """Seconds to wait before retrying: Retry-After header or 'try again in 1.5s / 210ms'."""
    import re
    wait = 0.0
    try:
        wait = float(r.headers.get("retry-after") or 0)
    except Exception:
        wait = 0.0
    m = re.search(r"try again in ([0-9.]+)\s*(ms|s)", r.text or "")
    if m:
        wait = max(wait, float(m.group(1)) / (1000.0 if m.group(2) == "ms" else 1.0))
    return min(max(wait + 0.5, 2.0 * (attempt + 1)), 20.0)


def _send(p, headers, payload):
    try:
        return requests.post(f"{p.base_url}/chat/completions", headers=headers, json=payload, timeout=p.timeout)
    except requests.Timeout:
        raise RetryableError(f"{p.provider} took too long to answer.")
    except requests.RequestException as e:
        raise RetryableError(f"Could not reach {p.provider}: {e}")


def _parse(p, r):
    if r.status_code >= 400:
        try:
            detail = r.json()
            if isinstance(detail, list):
                detail = detail[0] if detail else {}
            detail = detail.get("error", detail)
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
        except Exception:
            detail = r.text[:300]
        msg = f"{p.provider} ({p.model}) error {r.status_code}: {str(detail)[:300]}"
        if r.status_code in (408, 429, 500, 502, 503, 504):
            err = RetryableError(msg)
            err.wait = _wait_seconds(r, 0) if r.status_code == 429 else 20
            raise err
        raise LLMError(msg)
    return r.json()


def _openai_compatible(p, system, messages, tools, patient=True):
    out = [{"role": "system", "content": system}]
    # Older tool results are shortened (saves tokens = fewer free-limit waits); the latest stay complete
    last_assistant = max([i for i, m in enumerate(messages) if m["role"] == "assistant"] or [-1])
    for i, m in enumerate(messages):
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            item = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                item["tool_calls"] = []
                for tc in m["tool_calls"]:
                    call = {"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"] or {})}}
                    if tc.get("extra"):  # Gemini "thought_signature" must be sent back
                        call["extra_content"] = tc["extra"]
                    elif p.provider == "gemini":  # call made by the other provider
                        call["extra_content"] = {"google": {"thought_signature": "skip_thought_signature_validator"}}
                    item["tool_calls"].append(call)
            out.append(item)
        elif m["role"] == "tool":
            content = m["content"]
            if i < last_assistant and len(content) > 1500:
                content = content[:1500] + " ...[shortened]"
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "name": m.get("name"),
                        "content": content})

    payload = {
        "model": p.model,
        "temperature": 0,
        "messages": out,
    }
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
    effort = (frappe.conf.get("ats_ai_reasoning_effort") or "").strip()
    if effort:  # e.g. "low" / "none": faster answers on thinking models
        payload["reasoning_effort"] = effort
    data = _post(p, payload, patient)

    msg = ((data.get("choices") or [{}])[0]).get("message") or {}
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        calls.append({"id": tc.get("id") or f"call_{i}_{fn.get('name')}", "name": fn.get("name"), "args": args,
                      "extra": tc.get("extra_content")})
    return {"text": (msg.get("content") or "").strip(), "tool_calls": calls}
