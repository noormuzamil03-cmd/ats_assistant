"""Voice for the ATS Assistant.

Speech to text : Groq Whisper (free tier) - uses the Groq key already set for the chat.
Text to speech : the answer is rewritten as a short spoken Urdu text by the chat AI,
                 then read by Microsoft Edge's free neural Urdu voice (python package edge-tts).
                 If that is not available, the browser's own voice is used.

Optional site_config settings:
    ats_ai_stt_api_key   : Groq key only for voice (default: the chat Groq key)
    ats_ai_stt_model     : default "whisper-large-v3" (most accurate for Urdu)
    ats_ai_stt_language  : default "ur" (Urdu, so it never writes Hindi script); "auto" = detect
    ats_ai_stt_roman     : 0 to keep the Urdu script text (default: converted to Roman Urdu)
    ats_ai_tts_engine    : "edge" (default) or "browser"
    ats_ai_tts_voice     : default "ur-PK-UzmaNeural" (male: "ur-PK-AsadNeural")
    ats_ai_tts_rate      : e.g. "+10%" (default "+0%")
    ats_ai_tts_proxy     : http proxy for edge-tts if the server needs one
"""

import base64
import hashlib
import json
import re

import frappe
import requests
from frappe.utils import cstr

from ats_assistant.assistant import llm
from ats_assistant.assistant.api import _allowed, _rate_limit

STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_AUDIO_BYTES = 15 * 1024 * 1024
STT_PROMPT = ("آج کتنے لوگ حاضر ہیں؟ آئی ٹی ڈیپارٹمنٹ میں کتنے ایمپلائز ہیں؟ ورک آرڈر، اٹینڈنس، اوور ٹائم، "
              "سیلری سلپ، چھٹی، چیک ان، اسٹاک، پرچیز آرڈر، اے ٹی ایس سنتھیٹک۔")

ROMAN_PROMPT = """Convert the user's text (Urdu or Hindi script, from speech recognition) into natural Roman Urdu,
the way Pakistanis type it on WhatsApp. Keep English words in English (present, work order, department,
employee, IT, HR, salary slip, overtime). Keep the meaning exactly; do not answer the question.
Return only the converted text."""

SPEECH_PROMPT = """You turn an ERP assistant's written answer into what should be SPOKEN to the user.
Rules:
- Write in natural Pakistani Urdu, in Urdu script.
- Keep it short: at most 3 or 4 sentences.
- Give the main answer first. For tables or long lists, say how many items there are and mention only
  the first few (2-3) by name.
- Write numbers as digits. Write dates like 15 October.
- Keep common English ERP words (work order, attendance, salary slip, department, overtime) but write
  them in Urdu script.
- No markdown, no links, no record IDs unless they are the answer, no emojis.
Return ONLY a JSON object with two keys:
  "ur": the text in Urdu script,
  "hi": exactly the same spoken words written in Devanagari (Hindustani pronunciation), used as a backup voice.
No other text."""


def _conf(key, default=None):
    value = frappe.conf.get(key)
    return value if value not in (None, "") else default


def _groq_key():
    key = _conf("ats_ai_stt_api_key")
    if key:
        return cstr(key).strip()
    for prefix in ("ats_ai_", "ats_ai_fallback_"):
        if cstr(_conf(prefix + "provider")).strip().lower() == "groq" and _conf(prefix + "api_key"):
            return cstr(_conf(prefix + "api_key")).strip()
    return None


@frappe.whitelist()
def transcribe():
    """Audio file (field 'audio') -> text."""
    _allowed()
    _rate_limit()
    files = getattr(frappe.request, "files", None) if frappe.request else None
    audio = files.get("audio") if files else None
    if not audio:
        frappe.throw("No audio received.")
    data = audio.stream.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        frappe.throw("Recording is too long. Please speak a shorter question.")
    if len(data) < 800:
        return {"text": ""}

    key = _groq_key()
    if not key:
        frappe.throw("Voice is not set up: add a Groq key with "
                     "bench --site SITE set-config ats_ai_stt_api_key \"YOUR-GROQ-KEY\"")

    form = {
        "model": _conf("ats_ai_stt_model", "whisper-large-v3"),
        "response_format": "json",
        "temperature": "0",
        "prompt": STT_PROMPT,
    }
    language = cstr(_conf("ats_ai_stt_language", "ur")).strip()
    if language and language != "auto":
        form["language"] = language
    filename = audio.filename or "voice.webm"
    mimetype = audio.mimetype or "audio/webm"

    try:
        r = requests.post(_conf("ats_ai_stt_url", STT_URL), headers={"Authorization": f"Bearer {key}"},
                          data=form, files={"file": (filename, data, mimetype)}, timeout=60)
    except requests.RequestException as e:
        frappe.throw(f"Voice service could not be reached: {cstr(e)[:200]}")
    if r.status_code >= 400:
        frappe.log_error(title="ATS Assistant voice", message=r.text[:2000])
        frappe.throw(f"Voice to text failed ({r.status_code}). Please try again.")
    text = cstr((r.json() or {}).get("text")).strip()
    return {"text": _to_roman(text), "original": text}


def _to_roman(text):
    """Urdu / Hindi script -> Roman Urdu (what the users type)."""
    if not text or not re.search(r"[\u0600-\u06FF\u0900-\u097F]", text) or str(_conf("ats_ai_stt_roman", 1)) == "0":
        return text
    try:
        reply = llm.chat(ROMAN_PROMPT, [{"role": "user", "content": text}], [])
        roman = cstr(reply.get("text")).strip().strip('"')
        return roman or text
    except Exception as e:
        frappe.log_error(title="ATS Assistant voice (roman)", message=cstr(e))
        return text


def _plain(text):
    """Markdown answer -> plain text (used when the AI rewrite is not available)."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*\|", line):
            continue
        line = re.sub(r"[*_`#>|]", "", line).strip(" -•")
        if line:
            lines.append(line)
    return " ".join(lines)[:600]


def _spoken_text(text):
    """-> (Urdu script text, Devanagari text). Falls back to the plain answer."""
    try:
        reply = llm.chat(SPEECH_PROMPT, [{"role": "user", "content": text}], [])
        raw = cstr(reply.get("text")).strip()
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            data = json.loads(match.group(0))
            ur, hi = cstr(data.get("ur")).strip(), cstr(data.get("hi")).strip()
            if ur:
                return ur[:1500], hi[:1500]
        if raw:
            return raw[:1500], ""
    except Exception as e:
        frappe.log_error(title="ATS Assistant speech text", message=cstr(e))
    plain = _plain(text)
    return plain, ""


def _edge_tts(text):
    import edge_tts

    kwargs = {"rate": _conf("ats_ai_tts_rate", "+0%")}
    if _conf("ats_ai_tts_proxy"):
        kwargs["proxy"] = _conf("ats_ai_tts_proxy")
    communicate = edge_tts.Communicate(text, _conf("ats_ai_tts_voice", "ur-PK-UzmaNeural"), **kwargs)
    audio = bytearray()
    for chunk in communicate.stream_sync():
        if chunk.get("type") == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


@frappe.whitelist()
def speak(text: str):
    """Answer text -> {spoken: Urdu text, audio: mp3 data URL or None}."""
    _allowed()
    text = cstr(text).strip()[:4000]
    if not text:
        return {}

    cache_key = "ats_ai_tts:" + hashlib.md5(
        (text + cstr(_conf("ats_ai_tts_voice"))).encode()).hexdigest()
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    _rate_limit()
    spoken, spoken_hi = _spoken_text(text)
    audio = None
    engine = "browser"
    if _conf("ats_ai_tts_engine", "edge") == "edge":
        try:
            mp3 = _edge_tts(spoken)
            if mp3:
                audio = "data:audio/mpeg;base64," + base64.b64encode(mp3).decode()
                engine = "edge"
        except Exception as e:
            frappe.log_error(title="ATS Assistant voice (edge-tts)", message=cstr(e))

    result = {"spoken": spoken, "spoken_hi": spoken_hi, "audio": audio, "engine": engine, "lang": "ur-PK"}
    if audio:
        frappe.cache().set_value(cache_key, result, expires_in_sec=3600)
    return result
