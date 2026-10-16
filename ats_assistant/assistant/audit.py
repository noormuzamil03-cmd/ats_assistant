"""Audit log for the ATS Assistant: who asked what, when, which data tools were used and the answer.

DocType "ATS AI Log" (only System Manager can open it: /app/ats-ai-log).
site_config:
    ats_ai_audit       : 0 to switch logging off (default on)
    ats_ai_log_days    : delete logs older than this many days (default 90)
"""

import json
import time

import frappe
from frappe.utils import add_days, cint, cstr, nowdate

DOCTYPE = "ATS AI Log"


def log(question, answer, calls=None, provider=None, started=None, voice=None, error=None):
    """Save one question. Never breaks the chat if logging fails."""
    if str(frappe.conf.get("ats_ai_audit", 1)) == "0":
        return
    try:
        if not frappe.db.exists("DocType", DOCTYPE):
            return
        user = frappe.session.user
        calls = calls or []
        frappe.get_doc({
            "doctype": DOCTYPE,
            "user": user,
            "full_name": frappe.utils.get_fullname(user),
            "question": cstr(question)[:2000],
            "answer": cstr(answer)[:20000],
            "status": "Error" if error else "Success",
            "error": cstr(error)[:1000],
            "tools_used": ", ".join(c.get("tool") or "" for c in calls)[:1000],
            "tool_calls": json.dumps(calls, ensure_ascii=False, default=str, indent=1)[:20000],
            "tool_count": len(calls),
            "provider": cstr(provider),
            "seconds": round(time.time() - started, 1) if started else 0,
            "via_voice": 1 if cint(voice) else 0,
            "ip_address": cstr(getattr(frappe.local, "request_ip", "") or ""),
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(title="ATS Assistant audit log", message=frappe.get_traceback())


def clear_old_logs():
    """Daily job: delete logs older than ats_ai_log_days (default 90)."""
    if not frappe.db.exists("DocType", DOCTYPE):
        return
    days = cint(frappe.conf.get("ats_ai_log_days") or 90)
    frappe.db.delete(DOCTYPE, {"creation": ["<", add_days(nowdate(), -days)]})
    frappe.db.commit()
