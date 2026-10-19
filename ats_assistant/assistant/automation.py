"""Automations for the ATS Assistant.

A user tells the chat "roz 10 baje aaj ki absent list bhejo" or "kal 3 baje yaad dilana ke meeting hai".
The chat saves an "ATS AI Automation" record. Every minute the scheduler runs the ones that are due,
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
answer directly with the latest data and never ask anything back. The answer is also EMAILED as a report, so:
start with one clear sentence with the main number(s), then a small markdown table with the useful breakdown
(e.g. by status, department or date - pick what fits the question, up to 15 rows), then at most one short
line of what stands out. No greetings, no questions, no "let me know".
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
    """Scheduler (every minute): start the automations that are due."""
    if not frappe.db.exists("DocType", DOCTYPE):
        return
    now = now_datetime()
    for name in frappe.get_all(DOCTYPE, filters={"enabled": 1, "next_run": ["<=", now]},
                               pluck="name", order_by="next_run asc", limit_page_length=50):
        doc = frappe.get_doc(DOCTYPE, name)
        nxt = compute_next(doc, now)
        frappe.db.set_value(DOCTYPE, name, {"next_run": nxt, "enabled": 1 if nxt else 0}, update_modified=False)
        frappe.db.commit()  # saved before the run, so it never runs twice
        _running(doc.user or doc.owner, True)
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
    _running(user, True)
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
        email_ok = deliver(doc, user, answer, error)
        frappe.db.set_value(DOCTYPE, name, {
            "last_run": now_datetime(),
            "last_status": "Error" if error else ("Email Failed" if email_ok is False else "Success"),
            "last_answer": (answer or error)[:60000], "run_count": cint(doc.run_count) + 1,
        }, update_modified=False)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title=f"ATS Assistant automation {name}", message=frappe.get_traceback())
    finally:
        _running(user, False)
        frappe.set_user(previous or "Administrator")


def _running(user, on):
    """Tells the chat window that a result is being prepared (so it checks every few seconds)."""
    try:
        key = f"ats_ai_running:{user}"
        if on:
            frappe.cache().set_value(key, 1, expires_in_sec=900)
        else:
            frappe.cache().delete_value(key)
    except Exception:
        pass


def deliver(doc, user, answer, error=""):
    icon = "⏰" if doc.kind == "Reminder" else "📊"
    text = answer if not error else f"Automation could not run: {error}"
    first = re.sub(r"[*_#|`>\[\]]", "", cstr(text).strip().splitlines()[0] if cstr(text).strip() else "")[:120]
    subject = f"{icon} {doc.title}" + (f": {first}" if first and doc.kind != "Reminder" else "")
    if doc.kind == "Reminder":
        subject = f"{icon} Reminder: {first or doc.title}"

    email_ok, email_note = None, ""
    if cint(doc.send_email):
        email_ok, email_note = _send_email(doc, user, subject, text)
        text = text + "\n\n_" + email_note + "_"

    log = frappe.get_doc({
        "doctype": "Notification Log", "subject": subject[:140], "for_user": user, "type": "Alert",
        "document_type": DOCTYPE, "document_name": doc.name, "from_user": "Administrator",
        "email_content": text,
    })
    log.insert(ignore_permissions=True)

    frappe.publish_realtime("ats_ai_delivery", {
        "id": log.name, "title": doc.title, "kind": doc.kind, "answer": text, "error": 1 if error else 0,
    }, user=user, after_commit=True)

    return email_ok


# ---------------------------------------------------------------- email

def email_ready():
    """Is an outgoing Email Account set up in the ERP?"""
    return bool(frappe.db.exists("Email Account", {"enable_outgoing": 1}))


def recipients(email_to, user):
    """Addresses to send to: email_to (comma separated) or the user's own ERP email."""
    out = []
    for part in re.split(r"[,;\s]+", cstr(email_to)):
        part = part.strip()
        if part:
            valid = frappe.utils.validate_email_address(part)
            if not valid:
                raise ValueError(f"'{part}' is not a valid email address")
            out.append(valid.lower())
    if not out:
        own = frappe.db.get_value("User", user, "email")
        if own:
            out.append(own.lower())
    return list(dict.fromkeys(out))[:10]


def check_recipients(emails, user):
    """Normal users may email only themselves or other ERP users; System Manager may email anyone."""
    if "System Manager" in frappe.get_roles(user):
        return
    own = cstr(frappe.db.get_value("User", user, "email")).lower()
    for e in emails:
        if e != own and not frappe.db.exists("User", {"email": e, "enabled": 1}):
            raise ValueError(f"{e} is not an ERP user. Only a System Manager can send to outside emails.")


def email_html(doc, text):
    """A clean report-style email (inline styles, works in Gmail / Outlook)."""
    esc = frappe.utils.escape_html
    body = frappe.utils.md_to_html(text) if hasattr(frappe.utils, "md_to_html") else f"<pre>{esc(text)}</pre>"
    body = cstr(body)
    body = re.sub(r"<table[^>]*>", '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
                  'width:100%;margin:12px 0;font-size:13px">', body)
    body = re.sub(r"<th(\s[^>]*)?>", '<th style="background:#f3f4f6;border:1px solid #d9dce1;padding:7px 10px;'
                  'text-align:left;font-weight:600">', body)
    body = re.sub(r"<td(\s[^>]*)?>", '<td style="border:1px solid #e3e5e8;padding:7px 10px">', body)
    body = re.sub(r"<p>", '<p style="margin:0 0 10px">', body)
    try:
        company = (frappe.defaults.get_user_default("Company")
                   or frappe.db.get_single_value("Global Defaults", "default_company") or "")
    except Exception:
        company = ""
    try:
        link = frappe.utils.get_url(f"/app/ats-ai-automation/{doc.name}")
    except Exception:
        link = ""
    when = now_datetime().strftime("%d %b %Y, %I:%M %p")
    is_reminder = doc.kind == "Reminder"
    heading = "Reminder" if is_reminder else esc(cstr(doc.title))
    question = "" if is_reminder else (
        '<div style="font-size:12px;color:#6b7280;margin:0 0 14px">Question: '
        f'<i>{esc(cstr(doc.question))}</i></div>')
    button = (f'<a href="{link}" style="display:inline-block;background:#d23a2e;color:#fff;text-decoration:none;'
              'padding:8px 16px;border-radius:6px;font-size:13px">Open in ERP</a>') if link else ""
    return f"""<div style="background:#f3f4f6;padding:24px 12px;font-family:Arial,Helvetica,sans-serif;color:#1f272e">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e3e5e8">
<div style="background:#d23a2e;color:#fff;padding:16px 22px">
<div style="font-size:12px;opacity:.85">{esc(company) or 'ATS'} &middot; ATS Assistant</div>
<div style="font-size:19px;font-weight:bold;margin-top:2px">{'&#9200; ' if is_reminder else '&#128202; '}{heading}</div>
</div>
<div style="padding:20px 22px;font-size:14px;line-height:1.55">
{question}{body}
<div style="margin-top:18px">{button}</div>
</div>
<div style="padding:12px 22px;background:#fafafa;border-top:1px solid #eee;font-size:11px;color:#888">
Sent automatically on {when} &middot; {esc(describe(doc))}.<br>
To stop it, tell the ATS Assistant "is automation ko band karo" or open My Automations in the ERP.
</div></div></div>"""


def _send_email(doc, user, subject, text):
    try:
        to = recipients(doc.get("email_to"), user)
    except ValueError as e:
        return False, f"Email not sent: {e}"
    if not to:
        return False, "Email not sent: no email address."
    if not email_ready():
        return False, "Email not sent: no outgoing Email Account is set up in the ERP."
    try:
        queued = frappe.sendmail(recipients=to, subject=subject[:140], now=True, message=email_html(doc, text))
        # check what really happened (Email Queue status)
        qname = getattr(queued, "name", None) if queued else None
        status = frappe.db.get_value("Email Queue", qname, ["status", "error"], as_dict=True) if qname else None
        if status and status.status in ("Error", "Expired"):
            return False, f"Email to {', '.join(to)} failed: {cstr(status.error).strip().splitlines()[-1][:150] if status.error else status.status}"
        if status and status.status != "Sent":
            return True, f"Email to {', '.join(to)} is in the mail queue (it arrives in 1-2 minutes)"
        return True, "Email sent to " + ", ".join(to)
    except Exception as e:
        frappe.log_error(title="ATS Assistant automation email", message=frappe.get_traceback())
        return False, f"Email to {', '.join(to)} failed: {cstr(e)[:150]}"


# ---------------------------------------------------------------- health check

RUN_DUE = "ats_assistant.assistant.automation.run_due"


def scheduler_problem():
    """Plain reason why automations will not run on time, or '' if all looks fine."""
    try:
        from frappe.utils.scheduler import is_scheduler_inactive

        if is_scheduler_inactive():
            return ("The ERP scheduler is OFF, so automations do not run by themselves (only 'run now' works). "
                    "An admin must run: bench --site SITE enable-scheduler")
    except Exception:
        pass
    try:
        job = frappe.db.get_value("Scheduled Job Type", {"method": RUN_DUE}, ["last_execution", "stopped"], as_dict=True)
    except Exception:
        return ""
    if not job:
        return ("The automation timer is not registered yet. An admin must run: bench --site SITE migrate, "
                "then restart the ERP (bench start / supervisorctl restart all).")
    if cint(job.stopped):
        return "The automation timer is stopped in Scheduled Job Type. An admin must untick 'Stopped'."
    if not job.last_execution or get_datetime(job.last_execution) < now_datetime() - timedelta(minutes=5):
        return ("The automation timer has not run in the last 5 minutes: the ERP scheduler / workers are not "
                "running. An admin must check: bench --site SITE scheduler status, and restart the ERP.")
    return ""


# ---------------------------------------------------------------- chat tool

def _own(name_or_title):
    """Automations of the current user matching an ID or (part of) a title."""
    user = frappe.session.user
    key = cstr(name_or_title).strip()
    fields = ["name", "title", "kind", "frequency", "run_time", "run_date", "weekday", "day_of_month",
              "enabled", "next_run", "last_run", "last_status", "question", "send_email", "email_to"]
    rows = frappe.get_all(DOCTYPE, filters={"user": user}, fields=fields, order_by="creation asc")
    if not key:
        return rows
    exact = [r for r in rows if r.name == key or cstr(r.title).lower() == key.lower()]
    return exact or [r for r in rows if key.lower() in cstr(r.title).lower() or key.lower() in cstr(r.question).lower()]


def _row(r):
    return {"id": r.name, "title": r.title, "type": r.kind, "schedule": describe(r),
            "status": "On" if cint(r.enabled) else "Off", "next_run": cstr(r.next_run or "")[:16],
            "last_run": cstr(r.last_run or "")[:16] or "never ran yet", "last_status": r.last_status or "",
            "email": (", ".join(recipients(r.get("email_to"), r.get("user") or frappe.session.user))
                      if cint(r.get("send_email")) else "no"),
            "question": cstr(r.question)[:200]}


def manage_automation(action, title=None, question=None, kind="Question", frequency="Daily", time=None,
                      weekday=None, day_of_month=None, date=None, send_email=False, automation=None, email_to=None):
    from ats_assistant.assistant.tools import ToolError

    if not frappe.db.exists("DocType", DOCTYPE):
        raise ToolError("Automations are not installed yet (run bench migrate).")
    action = cstr(action).strip().lower()

    if action == "list":
        rows = [_row(r) for r in _own(None)]
        out = {"automations": rows, "count": len(rows), "now": cstr(now_datetime())[:16],
               "note": "" if rows else "No automations yet.", "manage_link": "/app/ats-ai-automation"}
        problem = scheduler_problem()
        if problem:
            out["scheduler_warning"] = problem
        if not email_ready():
            out["email_warning"] = "No outgoing Email Account is set up in the ERP, so no email can be sent."
        return out

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
            "question": question[:2000],
            "send_email": 1 if (email_to or send_email in (True, 1, "1", "true", "yes")) else 0,
            "email_to": cstr(email_to).strip()[:500],
            "user": frappe.session.user,
        })
        if not compute_next(doc):
            raise ToolError("That time is already over. Give a future date / time.")
        if doc.send_email:
            try:
                check_recipients(recipients(doc.email_to, frappe.session.user), frappe.session.user)
            except ValueError as e:
                raise ToolError(str(e))
        doc.insert(ignore_permissions=True)
        out = {"created": _row(doc), "delivery": "chat popup + ERP bell" + (" + email" if doc.send_email else ""),
               "link": f"/app/ats-ai-automation/{doc.name}",
               "checks_every": "1 minute (it can arrive up to 1 minute after the set time)"}
        problem = scheduler_problem()
        if problem:
            out["scheduler_warning"] = problem
        if doc.send_email and not email_ready():
            out["email_warning"] = ("No outgoing Email Account is set up in the ERP, so the email will NOT be sent "
                                    "until an admin sets one up (Email Account with 'Enable Outgoing'). "
                                    "The chat popup and bell still work.")
        return out

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
        return {"items": []}
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
    # when the next automation of this user is due, so the chat checks right then (no refresh needed)
    nxt = (frappe.get_all(DOCTYPE, filters={"user": frappe.session.user, "enabled": 1, "next_run": ["is", "set"]},
                          pluck="next_run", order_by="next_run asc", limit_page_length=1) or [None])[0]
    next_in = int((get_datetime(nxt) - now_datetime()).total_seconds()) if nxt else None
    running = bool(frappe.cache().get_value(f"ats_ai_running:{frappe.session.user}"))
    return {"items": out, "next_in": next_in, "running": running}


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
