"""Automations for the ATS Assistant.

A user tells the chat "roz 10 baje aaj ki absent list bhejo" or "kal 3 baje yaad dilana ke meeting hai".
The chat saves an "ATS AI Automation" record. Every 5 minutes the scheduler runs the ones that are due,
as that user (so the user's own ERP permissions apply), and delivers the answer:
    - into the chat window (popup + red count on the chat button, even if the chat is closed)
    - the ERP bell (Notification Log)
    - by email, if asked (needs an outgoing Email Account)

site_config:
    ats_ai_automation_limit : max automations per user (default 20)
"""

import calendar
import json
import re
import time
from datetime import datetime, timedelta

import frappe
from frappe.utils import add_days, cint, cstr, get_datetime, getdate, now_datetime

DOCTYPE = "ATS AI Automation"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
FREQUENCIES = ["Once", "Daily", "Working Days (Mon-Sat)", "Weekly", "Monthly"]

AUTOMATION_RULE = """
This question is running AUTOMATICALLY on a schedule (automation "{title}"). Nobody is waiting to reply:
answer directly with the latest data, never ask anything back, put the main numbers first and keep it short.
"""


# ---------------------------------------------------------------- schedule

def _seconds(value):
    if value is None or value == "":
        return None
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds())
    parts = [int(float(x)) for x in cstr(value).split(":")[:3]] + [0, 0]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_time(text):
    """'10', '10:30', '3 pm', '15:00', '9:15 AM' -> 'HH:MM:00'."""
    t = cstr(text).strip().lower().replace(".", ":")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm|baje)?$", t)
    if not m:
        raise ValueError(f"Time '{text}' not understood, use HH:MM")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        raise ValueError(f"Time '{text}' is not valid")
    return f"{h:02d}:{mi:02d}:00"


def normalise_frequency(value):
    v = cstr(value).strip().lower()
    if not v:
        return "Daily"
    for f in FREQUENCIES:
        if v == f.lower():
            return f
    if any(w in v for w in ("once", "one time", "aik dafa", "ek dafa")):
        return "Once"
    if any(w in v for w in ("working", "weekday", "mon-sat", "kaam")):
        return "Working Days (Mon-Sat)"
    if "week" in v or "hafta" in v:
        return "Weekly"
    if "month" in v or "mahin" in v:
        return "Monthly"
    return "Daily"


def compute_next(doc, after=None):
    """Next time this automation must run (None = never again)."""
    after = get_datetime(after or now_datetime())
    secs = _seconds(doc.get("run_time"))
    secs = 9 * 3600 if secs is None else secs

    def at(day):
        return datetime.combine(getdate(day), datetime.min.time()) + timedelta(seconds=secs)

    freq = doc.get("frequency") or "Daily"
    if freq == "Once":
        if not doc.get("run_date"):
            return None
        x = at(doc.get("run_date"))
        return x if x > after else None
    start = getdate(after)
    for i in range(0, 400):
        d = getdate(add_days(start, i))
        x = at(d)
        if x <= after:
            continue
        if freq == "Daily":
            return x
        if freq.startswith("Working") and d.weekday() != 6:
            return x
        if freq == "Weekly" and WEEKDAYS[d.weekday()] == (doc.get("weekday") or "Monday"):
            return x
        if freq == "Monthly":
            last = calendar.monthrange(d.year, d.month)[1]
            if d.day == min(max(cint(doc.get("day_of_month")) or 1, 1), last):
                return x
    return None


def describe(doc):
    secs = _seconds(doc.get("run_time")) or 0
    t = (datetime.min + timedelta(seconds=secs)).strftime("%I:%M %p")
    f = doc.get("frequency")
    when = {"Once": f"once on {doc.get('run_date')}", "Daily": "every day",
            "Working Days (Mon-Sat)": "every working day (Mon-Sat)",
            "Weekly": f"every {doc.get('weekday') or 'Monday'}",
            "Monthly": f"every month on day {cint(doc.get('day_of_month')) or 1}"}.get(f, f)
    return f"{when} at {t}"


# ---------------------------------------------------------------- scheduler

def run_due():
    """Scheduler (every 5 minutes): start the automations that are due."""
    if not frappe.db.exists("DocType", DOCTYPE):
        return
    now = now_datetime()
    for name in frappe.get_all(DOCTYPE, filters={"enabled": 1, "next_run": ["<=", now]},
                               pluck="name", order_by="next_run asc", limit_page_length=50):
        doc = frappe.get_doc(DOCTYPE, name)
        nxt = compute_next(doc, now)
        frappe.db.set_value(DOCTYPE, name, {"next_run": nxt, "enabled": 1 if nxt else 0}, update_modified=False)
        frappe.db.commit()  # saved before the run, so it never runs twice
        frappe.enqueue("ats_assistant.assistant.automation.run_one", queue="long", timeout=900, name=name)


def run_one(name, manual=False):
    """Run one automation as its owner and deliver the result."""
    if not frappe.db.exists(DOCTYPE, name):
        return
    doc = frappe.get_doc(DOCTYPE, name)
    user = doc.user or doc.owner
    if not cint(frappe.db.get_value("User", user, "enabled")):
        return
    previous = frappe.session.user
    frappe.set_user(user)
    answer, error = "", ""
    try:
        if doc.kind == "Reminder":
            answer = cstr(doc.question)
        else:
            from ats_assistant.assistant import api, audit

            try:
                api._allowed()
            except Exception:
                error = "This user may no longer use the assistant."
            if not error:
                result = {}
                for attempt in range(2):  # one retry if the free AI service is busy
                    frappe.local.ats_ai_datasets = []
                    calls, started = [], time.time()
                    result = api._answer(doc.question, None, calls, None,
                                         AUTOMATION_RULE.format(title=cstr(doc.title).replace('"', "'")))
                    audit.log(question=f"[Automation: {doc.title}] {doc.question}", answer=result.get("answer"),
                              calls=calls, provider=result.get("provider"), started=started,
                              error=result.get("error_text"))
                    if not result.get("error"):
                        break
                    if attempt == 0:
                        time.sleep(60)
                answer = cstr(result.get("answer"))
                if result.get("error"):
                    error = cstr(result.get("error_text") or answer)
        deliver(doc, user, answer, error)
        frappe.db.set_value(DOCTYPE, name, {
            "last_run": now_datetime(), "last_status": "Error" if error else "Success",
            "last_answer": (answer or error)[:60000], "run_count": cint(doc.run_count) + 1,
        }, update_modified=False)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title=f"ATS Assistant automation {name}", message=frappe.get_traceback())
    finally:
        frappe.set_user(previous or "Administrator")


def deliver(doc, user, answer, error=""):
    icon = "⏰" if doc.kind == "Reminder" else "📊"
    text = answer if not error else f"Automation could not run: {error}"
    first = re.sub(r"[*_#|`>\[\]]", "", cstr(text).strip().splitlines()[0] if cstr(text).strip() else "")[:120]
    subject = f"{icon} {doc.title}" + (f": {first}" if first and doc.kind != "Reminder" else "")
    if doc.kind == "Reminder":
        subject = f"{icon} Reminder: {first or doc.title}"

    log = frappe.get_doc({
        "doctype": "Notification Log", "subject": subject[:140], "for_user": user, "type": "Alert",
        "document_type": DOCTYPE, "document_name": doc.name, "from_user": "Administrator",
        "email_content": text,
    })
    log.insert(ignore_permissions=True)

    frappe.publish_realtime("ats_ai_delivery", {
        "id": log.name, "title": doc.title, "kind": doc.kind, "answer": text, "error": 1 if error else 0,
    }, user=user, after_commit=True)

    if cint(doc.send_email):
        email = frappe.db.get_value("User", user, "email")
        if email:
            try:
                html = frappe.utils.md_to_html(text) if hasattr(frappe.utils, "md_to_html") else f"<pre>{text}</pre>"
                frappe.sendmail(recipients=[email], subject=subject[:140],
                                message=html + "<p style='color:#888;font-size:12px'>Sent by ATS Assistant automation "
                                f"&ldquo;{frappe.utils.escape_html(doc.title)}&rdquo; ({describe(doc)}).</p>")
            except Exception:
                frappe.log_error(title="ATS Assistant automation email", message=frappe.get_traceback())


# ---------------------------------------------------------------- chat tool

def _own(name_or_title):
    """Automations of the current user matching an ID or (part of) a title."""
    user = frappe.session.user
    key = cstr(name_or_title).strip()
    fields = ["name", "title", "kind", "frequency", "run_time", "run_date", "weekday", "day_of_month",
              "enabled", "next_run", "last_run", "last_status", "question"]
    rows = frappe.get_all(DOCTYPE, filters={"user": user}, fields=fields, order_by="creation asc")
    if not key:
        return rows
    exact = [r for r in rows if r.name == key or cstr(r.title).lower() == key.lower()]
    return exact or [r for r in rows if key.lower() in cstr(r.title).lower() or key.lower() in cstr(r.question).lower()]


def _row(r):
    return {"id": r.name, "title": r.title, "type": r.kind, "schedule": describe(r),
            "status": "On" if cint(r.enabled) else "Off", "next_run": cstr(r.next_run or "")[:16],
            "last_run": cstr(r.last_run or "")[:16], "last_status": r.last_status or "",
            "question": cstr(r.question)[:200]}


def manage_automation(action, title=None, question=None, kind="Question", frequency="Daily", time=None,
                      weekday=None, day_of_month=None, date=None, send_email=False, automation=None):
    from ats_assistant.assistant.tools import ToolError

    if not frappe.db.exists("DocType", DOCTYPE):
        raise ToolError("Automations are not installed yet (run bench migrate).")
    action = cstr(action).strip().lower()

    if action == "list":
        rows = [_row(r) for r in _own(None)]
        return {"automations": rows, "count": len(rows),
                "note": "" if rows else "No automations yet.", "manage_link": "/app/ats-ai-automation"}

    if action == "create":
        question = cstr(question).strip()
        if not question:
            raise ToolError("Give the question (or the reminder text) to run.")
        limit = cint(frappe.conf.get("ats_ai_automation_limit") or 20)
        if frappe.db.count(DOCTYPE, {"user": frappe.session.user}) >= limit:
            raise ToolError(f"You already have {limit} automations. Delete one first.")
        kind = "Reminder" if cstr(kind).lower().startswith("rem") else "Question"
        freq = normalise_frequency(frequency)
        try:
            run_time = parse_time(time or "09:00")
        except ValueError as e:
            raise ToolError(str(e))
        doc = frappe.get_doc({
            "doctype": DOCTYPE, "enabled": 1, "title": (cstr(title).strip() or question)[:120],
            "kind": kind, "frequency": freq, "run_time": run_time,
            "run_date": getdate(date) if (freq == "Once" and date) else (getdate() if freq == "Once" else None),
            "weekday": next((w for w in WEEKDAYS if cstr(weekday).lower()[:3] == w.lower()[:3]), "Monday")
            if freq == "Weekly" else None,
            "day_of_month": min(max(cint(day_of_month) or 1, 1), 31) if freq == "Monthly" else 0,
            "question": question[:2000], "send_email": 1 if (send_email in (True, 1, "1", "true", "yes")) else 0,
            "user": frappe.session.user,
        })
        if not compute_next(doc):
            raise ToolError("That time is already over. Give a future date / time.")
        doc.insert(ignore_permissions=True)
        return {"created": _row(doc), "delivery": "chat popup + ERP bell" + (" + email" if doc.send_email else ""),
                "link": f"/app/ats-ai-automation/{doc.name}"}

    if action in ("delete", "pause", "resume", "run_now"):
        found = _own(automation or title)
        if not found:
            return {"error": "No automation of yours matches that.", "yours": [_row(r) for r in _own(None)][:20]}
        if len(found) > 1:
            return {"error": "More than one automation matches. Say which one (id or full title).",
                    "candidates": [_row(r) for r in found][:20]}
        r = found[0]
        if action == "delete":
            frappe.delete_doc(DOCTYPE, r.name, ignore_permissions=True)
            return {"deleted": r.title}
        if action == "run_now":
            frappe.enqueue("ats_assistant.assistant.automation.run_one", queue="long", timeout=900,
                           name=r.name, manual=True, enqueue_after_commit=True)
            return {"started": r.title, "note": "The result will arrive in this chat in a minute."}
        doc = frappe.get_doc(DOCTYPE, r.name)
        doc.enabled = 0 if action == "pause" else 1
        doc.save(ignore_permissions=True)
        return {"updated": _row(doc)}

    raise ToolError("action must be list, create, delete, pause, resume or run_now")


# ---------------------------------------------------------------- chat inbox

@frappe.whitelist()
def get_inbox():
    """Automation results the user has not seen in the chat yet."""
    from ats_assistant.assistant.api import _allowed

    _allowed()
    if not frappe.db.exists("DocType", DOCTYPE):
        return []
    rows = frappe.get_all("Notification Log",
                          filters={"for_user": frappe.session.user, "document_type": DOCTYPE, "read": 0},
                          fields=["name", "subject", "email_content", "document_name", "creation"],
                          order_by="creation asc", limit_page_length=20)
    titles = {}
    out = []
    for r in rows:
        if r.document_name not in titles:
            titles[r.document_name] = frappe.db.get_value(DOCTYPE, r.document_name, ["title", "kind"], as_dict=True) or {}
        t = titles[r.document_name]
        out.append({"id": r.name, "title": t.get("title") or r.subject, "kind": t.get("kind") or "Question",
                    "answer": cstr(r.email_content), "time": cstr(r.creation)[:16]})
    return out


@frappe.whitelist()
def mark_read(ids: str | None = None):
    try:
        names = json.loads(ids or "[]")
    except Exception:
        names = []
    for n in names[:50]:
        if frappe.db.get_value("Notification Log", n, "for_user") == frappe.session.user:
            frappe.db.set_value("Notification Log", n, "read", 1, update_modified=False)
    return {"ok": 1}
