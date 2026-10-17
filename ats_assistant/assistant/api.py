import json
import time

import frappe
from frappe.utils import add_days, cstr, getdate, now_datetime, nowdate

from ats_assistant.assistant import audit, llm, tools

MAX_STEPS = 12
MAX_HISTORY = 6
PER_MINUTE = 12   # questions per user per minute (protects the free API limit and the server)
MAX_SECONDS = 75  # stop looking for more data after this (the web request must finish in time)

SYSTEM_PROMPT = """You are "ATS Assistant", the ERP assistant of {company} (Frappe / ERPNext / HRMS).
Today is {today} ({weekday}). Yesterday was {yesterday}. Current time {time}. Currency {currency}.
You are talking to {user_name} (user {user}{employee}).

How to work:
- ALWAYS get facts from the tools. Never invent names, numbers, dates or records.
- For "how many / total / sum / average" questions use the aggregate tool (exact), not by counting rows yourself.
- If you are not sure of a DocType name call list_doctypes; if you are not sure of field names call describe_doctype. Do this before guessing.
- If a ready report fits the question (e.g. stock balance, leave, salary, attendance reports) use list_reports -> describe_report -> run_report.
- Dates in filters must be YYYY-MM-DD. "aaj"=today, "kal"=yesterday (for past events), "is mahine"=this month.
- Employee counts or lists ("IT mein kitne employees", "HR ke log kon hain", "kitne helpers hain"): ALWAYS use
  employee_stats. Count only Active employees unless the user asks for left / all. Never use a 'like' filter
  with short words such as IT or HR (they also match QUALITY, SECURITY, ...).
- General questions about the world (who is the current president / prime minister / CEO of a famous company,
  countries, cities, history, definitions, sports, science): use web_lookup and answer from it, because your
  own knowledge can be out of date. Say the answer comes from Wikipedia. Never send company data to it.
  For simple chit-chat (hi, how are you, salam, kaise ho, thanks) just answer politely without tools.
- When the user names a person, card/attendance ID, item, machine, customer or document, call search_records
  first to get the exact record and ID, then query with that ID.
- For questions that need joins, per-day / per-month grouping or calculations across tables use run_sql
  (if it is in your tool list); otherwise combine query_records / aggregate / run_report.
- Never say you cannot reach the database or the data. Always try the tools first, and try another way
  (another DocType, field, report or search) before saying the data was not found.
- Use as few tool calls as possible. If a tool returns an error, fix the call and try again.
- Double-check every number in your answer against the tool results.
- If permission is denied, say politely that this user does not have access.
- Salary, payroll, bank, loan, advance, CNIC and income-tax data is restricted for the assistant. If asked,
  say politely that this information is not available in chat and the user should open it in ERPNext.

Useful DocTypes: Employee (status, department, designation, user_id), Employee Checkin (employee, time, log_type IN/OUT),
Attendance (employee, attendance_date, status, docstatus=1), Leave Application, Leave Allocation, Salary Slip (docstatus=1),
Salary Structure Assignment, Leave Encashment, Additional Salary, Item, Bin (item_code, warehouse, actual_qty, stock_value),
Stock Ledger Entry, Warehouse, Purchase Order, Purchase Receipt, Purchase Invoice, Supplier, Sales Order, Sales Invoice,
Customer, Payment Entry, Journal Entry, Asset, Work Order Slip, Work Order Completion (workflow_state).

Company terms (always use these, they override general ERPNext meaning):
- "work order", "WO", "slip", "maintenance order", "machine kharab" = DocType "Work Order Slip"
  (NOT ERPNext manufacturing "Work Order"). Fields: name, machine_name, defect_description, priority,
  date, requesting_department, assigned_department, work_status (Not Started / Pending / In Progress /
  Pending Approval / Rework Required / Completed), docstatus.
  "open work orders" = Work Order Slip with work_status != "Completed" and docstatus != 2.
- "completion", "approval", "pending approval", "rework" = DocType "Work Order Completion",
  field workflow_state ("Pending Approval", "Rework Required", "Approved").
- Attendance of a day ("present today", "aaj kon aya", "kaun late aya", "kaun absent hai", "department-wise
  attendance", any date) = attendance_summary tool. Leave balance of an employee = leave_balance tool.
- Only use the manufacturing "Work Order" DocType if the user clearly says production / manufacturing.
- "CEO / GM / director / manager / head / incharge kon hai" = an Employee by designation. Use search_records
  with the title (it also matches designation and department) and also try related titles, e.g. CEO ->
  'Chief', 'Director', 'Managing'; GM -> 'General Manager'. Only say nobody was found after these tries.

How to answer:
- Language: always reply in the SAME language as the user's latest message.
  English message (e.g. hi, hello, how are you, how many employees) -> reply in English.
  Roman Urdu message (e.g. kaise ho, aaj kitne log present hain) -> reply in Roman Urdu.
  Urdu script message -> reply in Urdu script. English ERP words inside a Roman Urdu sentence keep it Roman Urdu.
- Talk like a helpful colleague, not a database: do not mention DocType names, field names, workflow_state,
  filters or tool names in the answer, unless the user asks how it was calculated.
- Questions may come from voice (speech to text), so they can have spelling mistakes, Urdu script or
  mixed words. Understand the meaning; if it is really unclear, ask one short question.
- Be short and direct. Put the main number / answer first.
- For lists use a markdown table. If the user asks for all / sab / poori list / full list, show EVERY row you
  received (call query_records with limit 100 and only the 2-3 fields needed). Otherwise show up to 20 rows
  and say how many more there are.
- Charts: when the user asks for a chart / graph / "...-wise" comparison, give the numbers as a markdown table
  (first column = label, then one or more number columns). The chat window draws the bar chart and offers an
  Excel download by itself, so never say you cannot draw charts or make Excel files.
- Link records like [EMP-0001](/app/employee/EMP-0001) (doctype in lower case with - instead of spaces).
- If data is not found, say so clearly and suggest what to check.
"""


RESTRICTED_RULE = """- Salary, payroll, bank, loan, advance, CNIC and income-tax data is restricted for the assistant. If asked,
  say politely that this information is not available in chat and the user should open it in ERPNext.
"""
SALARY_OPEN_RULE = """- Salary slips, salary register, payroll, income tax and salary structures CAN be answered: use the
  Salary Slip DocType (docstatus=1 for final slips; fields such as gross_pay, net_pay, rounded_total,
  total_deduction, payment_days, start_date, end_date) or the salary reports. The user only sees what
  their own ERP permissions allow. Bank account, CNIC, loan and advance data is still restricted.
"""


def _system_prompt():
    user = frappe.session.user
    today = getdate(nowdate())
    emp = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"},
                              ["name", "employee_name", "department"], as_dict=True)
    employee = f", employee {emp.name}, department {emp.department}" if emp else ""
    company = (frappe.defaults.get_user_default("Company")
               or frappe.db.get_single_value("Global Defaults", "default_company") or "the company")
    currency = frappe.db.get_value("Company", company, "default_currency") if company else ""
    prompt = SYSTEM_PROMPT
    if tools._allow_sensitive():
        prompt = prompt.replace(RESTRICTED_RULE, "")
    elif tools._allow_salary():
        prompt = prompt.replace(RESTRICTED_RULE, SALARY_OPEN_RULE)
    return prompt.format(
        company=company,
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        yesterday=add_days(today, -1),
        time=now_datetime().strftime("%H:%M"),
        currency=currency or "PKR",
        user_name=frappe.utils.get_fullname(user),
        user=user,
        employee=employee,
    )


def _history(history):
    if isinstance(history, str):
        try:
            history = json.loads(history or "[]")
        except Exception:
            history = []
    out = []
    for h in (history or [])[-MAX_HISTORY:]:
        role = h.get("role") if isinstance(h, dict) else None
        if role in ("user", "assistant") and h.get("content"):
            out.append({"role": role, "content": cstr(h["content"])[:800]})
    # The conversation must start with the user
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _rate_limit():
    key = f"ats_ai_rate:{frappe.session.user}:{now_datetime().strftime('%Y%m%d%H%M')}"
    count = frappe.cache().incr(key)
    frappe.cache().expire(key, 70)
    if count > PER_MINUTE:
        frappe.throw("Too many questions at once. Please try again in a minute.", title="Please wait")


def _allowed():
    if frappe.session.user == "Guest":
        frappe.throw("Please log in.", frappe.PermissionError)
    # Only these roles may use the assistant (change with: bench set-config --parse ats_ai_roles '[...]')
    roles = frappe.conf.get("ats_ai_roles") or ["AI Assistant User"]
    if not (set(roles) & set(frappe.get_roles())) and frappe.session.user != "Administrator":
        frappe.throw("You are not allowed to use the assistant.", frappe.PermissionError)


@frappe.whitelist()
def ask(question: str, history: str | None = None, voice: str | int | None = None, rid: str | None = None):
    _allowed()
    question = cstr(question).strip()[:2000]
    if not question:
        frappe.throw("Please type a question.")
    _rate_limit()

    started = time.time()
    calls = []
    result = _answer(question, history, calls, cstr(rid)[:40])
    audit.log(question=question, answer=result.get("answer"), calls=calls,
              provider=result.get("provider"), started=started, voice=voice,
              error=result.get("error_text"))
    result.pop("error_text", None)
    return result


TOOL_LABELS = {
    "search_records": "Searching records", "employee_stats": "Counting employees",
    "attendance_summary": "Checking attendance", "leave_balance": "Checking leave balance",
    "query_records": "Reading records", "aggregate": "Calculating totals", "get_document": "Opening the record",
    "list_reports": "Looking for a report", "describe_report": "Reading report filters", "run_report": "Running the report",
    "run_sql": "Querying the database", "web_lookup": "Searching Wikipedia", "list_doctypes": "Looking up tables",
    "describe_doctype": "Reading fields", "system_info": "Reading system info",
}


def _progress(rid, text):
    """Live status in the chat bubble (needs the realtime / socket.io service; silently skipped otherwise)."""
    if not rid:
        return
    try:
        frappe.publish_realtime("ats_ai_progress", {"rid": rid, "text": text}, user=frappe.session.user)
    except Exception:
        pass


def _answer(question, history, calls, rid=None):
    messages = _history(history) + [{"role": "user", "content": question}]
    system = _system_prompt()
    used = []
    provider = ""
    started = time.time()
    try:
        for _step in range(MAX_STEPS):
            if time.time() - started > MAX_SECONDS:
                return {"answer": "The AI service is busy right now (free limit). Please try again in a minute.",
                        "tools": used, "provider": provider, "error": 1, "error_text": "Time limit reached"}
            _progress(rid, "Thinking")
            reply = llm.chat(system, messages, tools.get_schemas())
            provider = (reply.get("provider", "") + " / " + reply.get("model", "")) if reply.get("provider") else provider
            if not reply["tool_calls"]:
                return {"answer": reply["text"] or "Sorry, I could not find an answer.",
                        "tools": used, "provider": provider}
            messages.append({"role": "assistant", "content": reply["text"], "tool_calls": reply["tool_calls"]})
            for tc in reply["tool_calls"]:
                used.append(tc["name"])
                label = TOOL_LABELS.get(tc["name"], "Working")
                detail = (tc["args"] or {}).get("doctype") or (tc["args"] or {}).get("report_name") \
                    or (tc["args"] or {}).get("department") or ""
                _progress(rid, f"{label} {detail}".strip())
                output = tools.run_tool(tc["name"], tc["args"])
                calls.append({"tool": tc["name"], "args": tc["args"],
                              "error": output[:300] if output.startswith('{"error"') else ""})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": output})
        return {"answer": "This took too many steps. Please ask the question in a simpler way.",
                "tools": used, "provider": provider, "error_text": "Too many steps"}
    except llm.LLMError as e:
        frappe.log_error(title="ATS Assistant", message=cstr(e))
        return {"answer": f"The AI service did not answer: {e}", "tools": used, "error": 1,
                "provider": provider, "error_text": cstr(e)}


@frappe.whitelist()
def status():
    """Quick check from the browser console: frappe.call('ats_assistant.assistant.api.status')"""
    _allowed()
    try:
        p = llm.profiles()
        return {"ok": 1, "providers": [{"provider": x.provider, "model": x.model} for x in p]}
    except llm.LLMError as e:
        return {"ok": 0, "error": cstr(e)}


@frappe.whitelist()
def export_table(data: str, title: str | None = None):
    """A table from the chat (JSON list of rows, first row = headings) -> Excel file download."""
    _allowed()
    try:
        rows = json.loads(data or "[]")
    except Exception:
        frappe.throw("Invalid table data.")
    if not isinstance(rows, list) or not rows:
        frappe.throw("The table is empty.")

    def cell(v):
        text = cstr(v).strip()
        num = text.replace(",", "")
        try:
            return int(num) if num.lstrip("-").isdigit() else float(num) if num.count(".") == 1 and \
                num.replace(".", "", 1).lstrip("-").isdigit() else text
        except Exception:
            return text

    rows = [[cell(v) for v in (r if isinstance(r, list) else [r])][:50] for r in rows[:5000]]
    from frappe.utils.xlsxutils import make_xlsx

    sheet = (cstr(title) or "ATS Assistant")[:30]
    xlsx = make_xlsx(rows, sheet)
    frappe.response["filename"] = frappe.scrub(sheet) + ".xlsx"
    frappe.response["filecontent"] = xlsx.getvalue()
    frappe.response["type"] = "binary"
