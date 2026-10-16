"""Read-only data tools for the ATS Assistant.

Every tool runs as the logged-in user, so Frappe's normal permissions
(roles, user permissions, permission_query_conditions) decide what the
assistant can see. Nothing here writes to the database.
"""

import json
import os
import re

import frappe
from frappe.utils import cint, cstr, flt

MAX_ROWS = 100         # rows sent back to the AI per call
MAX_SCAN = 100000      # rows scanned for counts / sums
MAX_RESULT_CHARS = 6000

# Never readable through the assistant (system / security data)
BLOCKED_DOCTYPES = {
    "User", "Access Log", "Activity Log", "Error Log", "Error Snapshot", "Version",
    "Deleted Document", "OAuth Bearer Token", "OAuth Client", "OAuth Authorization Code",
    "Session Default Settings", "Email Account", "Email Queue", "Integration Request",
    "Scheduled Job Log", "System Console", "Server Script", "Client Script",
    "Personal Data Download Request", "Personal Data Deletion Request", "Token Cache",
    "Social Login Key", "LDAP Settings", "Connected App", "User Social Login",
}
SENSITIVE = re.compile(r"(password|passwd|secret|token|api_key|private_key|otp)", re.I)
LAYOUT_TYPES = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Fold",
                "Heading", "Image", "HTML Editor"}
NUMERIC_TYPES = {"Int", "Float", "Currency", "Percent", "Duration"}
AGG_FUNCS = {"count", "sum", "avg", "min", "max"}

# ---- Sensitive HR / payroll / bank data: never sent to the AI ----
# Turn off only on purpose: bench --site SITE set-config ats_ai_allow_sensitive 1
SENSITIVE_DOCTYPES = {
    "Salary Slip", "Salary Detail", "Salary Slip Timesheet", "Salary Slip Leave", "Salary Slip Loan",
    "Salary Structure", "Salary Structure Assignment", "Payroll Entry", "Payroll Employee Detail",
    "Additional Salary", "Salary Withholding", "Retention Bonus", "Employee Incentive",
    "Employee Benefit Application", "Employee Benefit Claim", "Employee Tax Exemption Declaration",
    "Employee Tax Exemption Proof Submission", "Leave Encashment", "Gratuity", "Employee Advance",
    "Loan", "Loan Application", "Loan Repayment", "Loan Disbursement", "Bank Account", "Bank Transaction",
}
SENSITIVE_FIELD = re.compile(
    r"(ctc|salary|gross_pay|net_pay|base_gross|basic_pay|income_tax|bank_ac|bank_name|iban|"
    r"account_no|cnic|passport|pan_number|encashment_amount|loan|advance_amount)", re.I)
SENSITIVE_REPORT = re.compile(r"(salary|payroll|payslip|encashment|gratuity|loan|bank|income tax|ctc|advance)", re.I)


def _allow_sensitive():
    return bool(frappe.conf.get("ats_ai_allow_sensitive"))


def _blocked(doctype):
    doctype = cstr(doctype).strip()
    extra = set(frappe.conf.get("ats_ai_blocked_doctypes") or [])
    if doctype in BLOCKED_DOCTYPES or doctype in extra:
        return True
    return doctype in SENSITIVE_DOCTYPES and not _allow_sensitive()


def _sensitive(fieldname):
    fieldname = cstr(fieldname)
    if SENSITIVE.search(fieldname):
        return True
    return bool(SENSITIVE_FIELD.search(fieldname)) and not _allow_sensitive()


def _report_blocked(name):
    if _allow_sensitive():
        return False
    if SENSITIVE_REPORT.search(cstr(name)):
        return True
    ref = frappe.db.get_value("Report", name, "ref_doctype")
    return bool(ref and _blocked(ref))


class ToolError(Exception):
    pass


# ---------------- helpers ----------------

def _check_doctype(doctype, parent_doctype=None):
    doctype = cstr(doctype).strip()
    if not doctype or not frappe.db.exists("DocType", doctype):
        raise ToolError(f"DocType '{doctype}' not found. Use list_doctypes to find the right name.")
    if _blocked(doctype):
        raise ToolError(f"'{doctype}' is not available to the assistant.")
    meta = frappe.get_meta(doctype)
    if meta.istable:
        if not parent_doctype:
            raise ToolError(
                f"'{doctype}' is a child table. Pass parent_doctype (the main DocType it belongs to)."
            )
        _check_doctype(parent_doctype)
    elif not frappe.has_permission(doctype, "read"):
        raise ToolError(f"You do not have permission to read '{doctype}'.")
    return meta


def _safe_fields(meta, fields):
    allowed = {"name", "creation", "modified", "owner", "docstatus", "parent", "idx"}
    out = []
    for f in fields or []:
        f = cstr(f).strip()
        if not f or _sensitive(f):
            continue
        df = meta.get_field(f)
        if f in allowed or (df and df.fieldtype != "Password" and df.fieldtype not in LAYOUT_TYPES):
            out.append(f)
        else:
            raise ToolError(f"Field '{f}' does not exist in {meta.name}. Use describe_doctype first.")
    return out


def _filters(value):
    if not value:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            raise ToolError("filters must be a JSON object or a list like [[field, operator, value]].")
    return value


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


# ---------------- tools ----------------

def list_doctypes(search=""):
    search = cstr(search).strip()
    if len(search) < 2:
        raise ToolError("Give at least 2 letters to search, e.g. 'leave', 'stock', 'salary'.")
    fields = ["name", "module", "istable", "issingle"]
    rows = frappe.get_all("DocType", filters=[["name", "like", f"%{search}%"]],
                          fields=fields, limit_page_length=60)
    seen = {r.name for r in rows}
    rows += [r for r in frappe.get_all("DocType", filters=[["module", "like", f"%{search}%"]],
                                       fields=fields, limit_page_length=60) if r.name not in seen]
    out = []
    for r in rows:
        if _blocked(r.name):
            continue
        if r.istable or frappe.has_permission(r.name, "read"):
            out.append({"doctype": r.name, "module": r.module,
                        "child_table": bool(r.istable), "single": bool(r.issingle)})
        if len(out) >= 30:
            break
    return {"doctypes": out}


def describe_doctype(doctype):
    meta = frappe.get_meta(cstr(doctype).strip()) if frappe.db.exists("DocType", cstr(doctype).strip()) else None
    if not meta:
        raise ToolError(f"DocType '{doctype}' not found. Use list_doctypes.")
    if _blocked(meta.name):
        raise ToolError(f"'{meta.name}' is not available to the assistant.")
    fields = []
    for df in meta.fields:
        if df.fieldtype in LAYOUT_TYPES or df.fieldtype == "Password" or _sensitive(df.fieldname):
            continue
        item = [df.fieldname, df.label or "", df.fieldtype]
        if df.fieldtype in ("Link", "Table", "Table MultiSelect", "Dynamic Link"):
            item.append(df.options or "")
        elif df.fieldtype == "Select" and df.options:
            item.append(" | ".join(o for o in df.options.split("\n") if o)[:200])
        fields.append(item)
    return {
        "doctype": meta.name,
        "child_table": bool(meta.istable),
        "single": bool(meta.issingle),
        "submittable": bool(meta.is_submittable),
        "title_field": meta.title_field or "name",
        "note": "docstatus: 0=Draft, 1=Submitted, 2=Cancelled" if meta.is_submittable else "",
        "fields [fieldname, label, type, options]": fields,
    }


def query_records(doctype, filters=None, fields=None, order_by=None, limit=20, parent_doctype=None):
    meta = _check_doctype(doctype, parent_doctype)
    if meta.issingle:
        return get_document(meta.name, meta.name)
    fields = _safe_fields(meta, fields or ["name"]) or ["name"]
    filters = _filters(filters)
    limit = max(1, min(cint(limit) or 20, MAX_ROWS))
    kwargs = {"filters": filters}
    if parent_doctype:
        kwargs["parent_doctype"] = parent_doctype

    rows = frappe.get_list(meta.name, fields=fields, order_by=order_by or "modified desc",
                           limit_page_length=limit, **kwargs)
    total = len(frappe.get_list(meta.name, pluck="name", limit_page_length=MAX_SCAN, **kwargs))
    return {"doctype": meta.name, "total_matching": total, "returned": len(rows),
            "rows": _jsonable(rows)}


def aggregate(doctype, function="count", field=None, filters=None, group_by=None,
              parent_doctype=None, top=25):
    meta = _check_doctype(doctype, parent_doctype)
    function = cstr(function).lower().strip()
    if function not in AGG_FUNCS:
        raise ToolError("function must be one of: count, sum, avg, min, max")
    if function != "count" and not field:
        raise ToolError(f"'{function}' needs a numeric field.")
    want = [f for f in (field, group_by) if f]
    want = _safe_fields(meta, want) if want else []
    fields = list(dict.fromkeys(["name"] + want))

    kwargs = {"filters": _filters(filters)}
    if parent_doctype:
        kwargs["parent_doctype"] = parent_doctype
    rows = frappe.get_list(meta.name, fields=fields, limit_page_length=MAX_SCAN, **kwargs)

    def calc(items):
        if function == "count":
            return len(items)
        vals = [flt(r.get(field)) for r in items if r.get(field) not in (None, "")]
        if not vals:
            return 0
        if function == "sum":
            return round(sum(vals), 2)
        if function == "avg":
            return round(sum(vals) / len(vals), 2)
        return min(vals) if function == "min" else max(vals)

    result = {"doctype": meta.name, "function": function, "field": field,
              "rows_scanned": len(rows), "value": calc(rows)}
    if len(rows) >= MAX_SCAN:
        result["warning"] = f"Only the first {MAX_SCAN} rows were scanned; add filters for an exact answer."
    if group_by:
        groups = {}
        for r in rows:
            groups.setdefault(cstr(r.get(group_by)) or "(not set)", []).append(r)
        ranked = sorted(((k, calc(v)) for k, v in groups.items()), key=lambda x: -flt(x[1]))
        top = max(1, min(cint(top) or 25, MAX_ROWS))
        result["group_by"] = group_by
        result["groups_total"] = len(ranked)
        result["groups"] = [{"group": k, "value": v} for k, v in ranked[:top]]
    return _jsonable(result)


def get_document(doctype, name):
    doctype = cstr(doctype).strip()
    if _blocked(doctype) or not frappe.db.exists("DocType", doctype):
        raise ToolError(f"DocType '{doctype}' not available.")
    if not frappe.db.exists(doctype, name):
        raise ToolError(f"{doctype} '{name}' not found. Use query_records to search by another field.")
    if not frappe.has_permission(doctype, "read", name):
        raise ToolError(f"You do not have permission to open {doctype} {name}.")
    doc = frappe.get_doc(doctype, name)
    meta = frappe.get_meta(doctype)
    skip = {"doctype", "_user_tags", "_comments", "_assign", "_liked_by", "_seen", "lft", "rgt",
            "old_parent", "amended_from"}
    out = {"doctype": doctype, "name": doc.name}
    for df in meta.fields:
        if df.fieldtype in LAYOUT_TYPES or df.fieldtype == "Password" or _sensitive(df.fieldname):
            continue
        if df.fieldname in skip:
            continue
        val = doc.get(df.fieldname)
        if val in (None, "", 0, []) and df.fieldtype not in NUMERIC_TYPES:
            continue
        if df.fieldtype in ("Table", "Table MultiSelect"):
            rows = []
            for child in (val or [])[:30]:
                rows.append({k: v for k, v in child.as_dict().items()
                             if v not in (None, "") and k not in (
                                 "name", "parent", "parentfield", "parenttype", "doctype",
                                 "owner", "creation", "modified", "modified_by", "docstatus")
                             and not _sensitive(k)})
            out[df.fieldname] = rows
            if len(val or []) > 30:
                out[df.fieldname + "_note"] = f"showing 30 of {len(val)} rows"
        else:
            out[df.fieldname] = val
    out["docstatus"] = doc.docstatus
    return _jsonable(out)


# ---------------- reports ----------------

def _report_permitted(name):
    if _report_blocked(name):
        return False
    try:
        return frappe.get_doc("Report", name).is_permitted()
    except Exception:
        return False


def list_reports(search=""):
    search = cstr(search).strip()
    filters = {"disabled": 0, "report_type": ["in", ["Script Report", "Query Report"]]}
    if search:
        filters["name"] = ["like", f"%{search}%"]
    rows = frappe.get_all("Report", filters=filters,
                          fields=["name", "ref_doctype", "report_type", "module"],
                          order_by="name", limit_page_length=80)
    out = [r for r in rows if _report_permitted(r.name)][:30]
    return {"reports": _jsonable(out)}


FILTER_KEY = re.compile(r"""["']?(fieldname|label|fieldtype|options|default|reqd)["']?\s*:\s*(?:__\(\s*)?(["'`])((?:\\.|(?!\2).)*)\2""", re.S)
FILTER_REQD = re.compile(r"""["']?reqd["']?\s*:\s*(1|true)""")


def _filters_from_js(js):
    out = []
    for block in re.findall(r"\{[^{}]*?fieldname[^{}]*?\}", js or "", re.S):
        item = {}
        for key, _q, val in FILTER_KEY.findall(block):
            item[key] = val.replace("\\n", " | ")
        if FILTER_REQD.search(block):
            item["reqd"] = 1
        if item.get("fieldname"):
            out.append(item)
    return out


def describe_report(report_name):
    if not frappe.db.exists("Report", report_name):
        raise ToolError(f"Report '{report_name}' not found. Use list_reports.")
    if not _report_permitted(report_name):
        raise ToolError(f"You do not have permission for report '{report_name}'.")
    rep = frappe.get_doc("Report", report_name)
    filters = []
    if rep.is_standard == "Yes" and rep.report_type == "Script Report":
        try:
            folder = frappe.get_module_path(rep.module, "report", frappe.scrub(rep.name))
            path = os.path.join(folder, frappe.scrub(rep.name) + ".js")
            if os.path.exists(path):
                with open(path) as fh:
                    filters = _filters_from_js(fh.read())
        except Exception:
            pass
    if not filters and rep.get("javascript"):
        filters = _filters_from_js(rep.javascript)
    if not filters and rep.get("filters"):
        filters = [{"fieldname": f.fieldname, "label": f.label, "fieldtype": f.fieldtype,
                    "options": f.options, "default": f.default} for f in rep.filters]
    return {"report": rep.name, "type": rep.report_type, "ref_doctype": rep.ref_doctype,
            "filters": filters,
            "hint": "Dates as YYYY-MM-DD. Leave optional filters out. Use run_report next."}


def _col_info(col):
    if isinstance(col, dict):
        return (col.get("fieldname") or frappe.scrub(col.get("label") or ""),
                col.get("label") or col.get("fieldname"), col.get("fieldtype") or "Data")
    parts = cstr(col).split(":")
    label = parts[0]
    ftype = (parts[1].split("/")[0] if len(parts) > 1 and parts[1] else "Data")
    return frappe.scrub(label), label, ftype


def run_report(report_name, filters=None, limit=30):
    if not frappe.db.exists("Report", report_name):
        raise ToolError(f"Report '{report_name}' not found. Use list_reports.")
    if not _report_permitted(report_name):
        raise ToolError(f"You do not have permission for report '{report_name}'.")
    from frappe.desk.query_report import run

    filters = _filters(filters)
    try:
        res = run(report_name, filters=filters, ignore_prepared_report=True)
    except TypeError:
        res = run(report_name, filters=filters)
    if res.get("prepared_report") and not res.get("result"):
        raise ToolError("This is a background (prepared) report; it cannot be run from chat.")

    cols = [_col_info(c) for c in (res.get("columns") or [])]
    cols = [c for c in cols if not _sensitive(c[0]) and not _sensitive(c[1] or "")]
    data = res.get("result") or []
    rows = []
    for row in data:
        if isinstance(row, dict):
            rows.append({label: row.get(fn) for fn, label, _t in cols if row.get(fn) not in (None, "")})
        elif isinstance(row, (list, tuple)):
            rows.append({cols[i][1]: v for i, v in enumerate(row) if i < len(cols) and v not in (None, "")})
    totals = {}
    for fn, label, ftype in cols:
        if ftype in NUMERIC_TYPES:
            totals[label] = round(sum(flt(r.get(label)) for r in rows), 2)
    limit = max(1, min(cint(limit) or 30, MAX_ROWS))
    return _jsonable({
        "report": report_name,
        "columns": [label for _f, label, _t in cols],
        "total_rows": len(rows),
        "column_totals_all_rows": totals,
        "rows": rows[:limit],
        "note": f"showing {min(limit, len(rows))} of {len(rows)} rows" if len(rows) > limit else "",
    })


def system_info():
    """Installed apps with versions, site companies and basic counts."""
    apps = []
    for app in frappe.get_installed_apps():
        try:
            version = frappe.get_attr(app + ".__version__")
        except Exception:
            version = ""
        apps.append({"app": app, "version": version})
    return {
        "installed_apps": apps,
        "installed_apps_count": len(apps),
        "companies": frappe.get_all("Company", pluck="name"),
        "active_employees": frappe.db.count("Employee", {"status": "Active"})
        if frappe.has_permission("Employee", "read") else "no permission",
    }


# ---------------- search & SQL ----------------

def _existing_fields(meta, fields):
    std = {"name", "creation", "modified", "owner", "docstatus"}
    return [f for f in fields if f in std or meta.get_field(f)]


def search_records(text, doctype=None, limit=15):
    """Find records by a name / word: employees first, then Frappe global search."""
    text = cstr(text).strip()
    if len(text) < 2:
        raise ToolError("Give at least 2 letters to search.")
    limit = max(1, min(cint(limit) or 15, 50))
    like = f"%{text}%"
    doctype = cstr(doctype).strip() or None
    out = []

    # 1) Employees by name / ID / attendance (card) ID
    if (not doctype or doctype == "Employee") and not _blocked("Employee") \
            and frappe.has_permission("Employee", "read"):
        meta = frappe.get_meta("Employee")
        fields = _existing_fields(meta, ["name", "employee_name", "attendance_device_id", "department",
                                         "designation", "branch", "employment_type", "status", "user_id"])
        or_filters = [["employee_name", "like", like], ["name", "like", like]]
        for extra in ("attendance_device_id", "designation", "department"):
            if meta.get_field(extra):
                or_filters.append([extra, "like", like])
        for r in frappe.get_list("Employee", fields=fields, or_filters=or_filters,
                                 order_by="status asc, employee_name asc", limit_page_length=limit):
            out.append(dict(r, doctype="Employee"))

    # 2) A specific DocType: match name or title field
    if doctype and doctype != "Employee":
        meta = _check_doctype(doctype)
        title = meta.title_field if meta.title_field and meta.get_field(meta.title_field) else None
        or_filters = [["name", "like", like]] + ([[title, "like", like]] if title else [])
        fields = ["name"] + ([title] if title else [])
        for r in frappe.get_list(meta.name, fields=fields, or_filters=or_filters,
                                 order_by="modified desc", limit_page_length=limit):
            out.append(dict(r, doctype=meta.name))

    # 3) Global search over every searchable DocType
    if not doctype and len(out) < limit:
        try:
            from frappe.utils.global_search import search as global_search
            hits = global_search(text, start=0, limit=limit * 2)
        except Exception:
            hits = []
        for h in hits:
            dt, name = h.get("doctype"), h.get("name")
            if not dt or dt == "Employee" or _blocked(dt):
                continue
            try:
                if not frappe.has_permission(dt, "read", name):
                    continue
            except Exception:
                continue
            out.append({"doctype": dt, "name": name, "content": cstr(h.get("content"))[:160]})
            if len(out) >= limit:
                break

    if not out:
        return {"matches": [], "hint": "Nothing found. Try another spelling, a shorter word, "
                                       "or query_records with a 'like' filter on a specific DocType."}
    return {"matches": _jsonable(out[:limit])}


SQL_BAD = re.compile(
    r"\b(insert|update|delete|replace|merge|drop|alter|create|truncate|rename|grant|revoke|lock|unlock|"
    r"handler|call|do|load_file|sleep|benchmark|outfile|dumpfile|set|use|kill|shutdown|flush|analyze|"
    r"optimize|repair|prepare|execute|deallocate|load|into|procedure)\b|@@|;|--|/\*|#", re.I)
SQL_SYSTEM = re.compile(r"(information_schema|performance_schema|\bmysql\s*\.|\bsys\s*\.|`?__)", re.I)
SQL_TABLES = re.compile(r"`(tab[^`]+)`|\b(tab[A-Za-z0-9_]+)\b")


def _sql_allowed():
    if frappe.conf.get("ats_ai_disable_sql"):
        return False
    if frappe.session.user == "Administrator":
        return True
    roles = set(frappe.conf.get("ats_ai_sql_roles") or ["System Manager"])
    return bool(roles & set(frappe.get_roles()))


def run_sql(query, limit=50):
    """Read-only SELECT on the ERP database (MariaDB). Only for System Managers."""
    if not _sql_allowed():
        raise ToolError("run_sql is not allowed for this user. Use query_records / aggregate / run_report.")
    q = cstr(query).strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", q, re.I):
        raise ToolError("Only a single SELECT query is allowed.")
    if SQL_BAD.search(q) or SQL_SYSTEM.search(q):
        raise ToolError("Query not allowed: use one plain SELECT (no ;, comments, variables or write words).")
    for a, b in SQL_TABLES.findall(q):
        table = a or b
        if _blocked(table[3:]):
            raise ToolError(f"Table '{table}' is not available to the assistant.")
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", q):
        if _sensitive(word):
            raise ToolError(f"Column '{word}' is restricted for the assistant.")
    limit = max(1, min(cint(limit) or 50, MAX_ROWS))
    if not re.search(r"\blimit\s+\d+(\s*(,|offset)\s*\d+)?\s*$", q, re.I):
        q = f"{q} LIMIT {limit}"
    timeout = max(1, cint(frappe.conf.get("ats_ai_sql_timeout") or 15))

    frappe.db.rollback()
    try:
        frappe.db.sql("SET TRANSACTION READ ONLY")
        rows = frappe.db.sql(f"SET STATEMENT max_statement_time={timeout} FOR {q}", as_dict=True)
    except Exception as e:
        raise ToolError(f"SQL error: {cstr(e)[:300]}")
    finally:
        frappe.db.rollback()

    rows = [{k: v for k, v in r.items() if not _sensitive(k)} for r in rows]
    return _jsonable({"returned": len(rows), "rows": rows[:limit],
                      "note": "" if len(rows) <= limit else f"showing {limit} rows"})


# ---------------- registry ----------------

FUNCTIONS = {
    "list_doctypes": list_doctypes,
    "describe_doctype": describe_doctype,
    "query_records": query_records,
    "aggregate": aggregate,
    "get_document": get_document,
    "list_reports": list_reports,
    "describe_report": describe_report,
    "run_report": run_report,
    "system_info": system_info,
    "search_records": search_records,
    "run_sql": run_sql,
}

_FILTERS_SCHEMA = {
    "description": "Frappe filters. Object like {\"status\": \"Active\", \"posting_date\": [\">=\", \"2026-10-01\"]} "
                   "or list like [[\"department\", \"like\", \"%HR%\"]]. Operators: =, !=, >, <, >=, <=, like, "
                   "not like, in, not in, between, is (\"set\"/\"not set\").",
}

SCHEMAS = [
    {"name": "list_doctypes",
     "description": "Find DocTypes (tables) by part of their name or module, e.g. 'leave', 'salary', 'stock', 'work order'.",
     "parameters": {"type": "object", "properties": {"search": {"type": "string"}}, "required": ["search"]}},
    {"name": "describe_doctype",
     "description": "Get the fields of a DocType (fieldname, label, type, options). Call before filtering on fields you are not sure about.",
     "parameters": {"type": "object", "properties": {"doctype": {"type": "string"}}, "required": ["doctype"]}},
    {"name": "query_records",
     "description": "List records of a DocType with chosen fields, filters and order. Returns total_matching (exact count) and up to 50 rows.",
     "parameters": {"type": "object", "properties": {
         "doctype": {"type": "string"},
         "filters": _FILTERS_SCHEMA,
         "fields": {"type": "array", "items": {"type": "string"}, "description": "fieldnames to return"},
         "order_by": {"type": "string", "description": "e.g. 'posting_date desc'"},
         "limit": {"type": "integer", "description": "max rows (1-100)"},
         "parent_doctype": {"type": "string", "description": "only for child tables"}},
         "required": ["doctype"]}},
    {"name": "aggregate",
     "description": "Exact count / sum / avg / min / max over a DocType, optionally grouped by a field. Use this for every 'how many' / 'total' question.",
     "parameters": {"type": "object", "properties": {
         "doctype": {"type": "string"},
         "function": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
         "field": {"type": "string", "description": "numeric field (not needed for count)"},
         "filters": _FILTERS_SCHEMA,
         "group_by": {"type": "string", "description": "field to group by, e.g. department"},
         "parent_doctype": {"type": "string"},
         "top": {"type": "integer"}},
         "required": ["doctype", "function"]}},
    {"name": "get_document",
     "description": "Open one record by its name/ID with all fields and child tables.",
     "parameters": {"type": "object", "properties": {"doctype": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["doctype", "name"]}},
    {"name": "list_reports",
     "description": "Find reports the user can run (Script / Query reports), optionally by part of the name.",
     "parameters": {"type": "object", "properties": {"search": {"type": "string"}}}},
    {"name": "describe_report",
     "description": "Get the filters a report needs (fieldname, label, type, options, default, reqd).",
     "parameters": {"type": "object", "properties": {"report_name": {"type": "string"}}, "required": ["report_name"]}},
    {"name": "run_report",
     "description": "Run a report with filters. Returns columns, total_rows, column totals over all rows and up to 50 rows.",
     "parameters": {"type": "object", "properties": {
         "report_name": {"type": "string"},
         "filters": {"type": "object", "description": "report filters by fieldname, dates as YYYY-MM-DD"},
         "limit": {"type": "integer"}},
         "required": ["report_name"]}},
    {"name": "system_info",
     "description": "Installed apps (with versions), companies and number of active employees on this ERP site.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "search_records",
     "description": "Find records by a word or name the user mentioned (a person, card/attendance ID, item, "
                    "customer, machine, document number...). Searches employees first, then all searchable "
                    "DocTypes. Use it before other tools whenever the user names something.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "description": "name / word / number to look for"},
         "doctype": {"type": "string", "description": "optional: only search this DocType"},
         "limit": {"type": "integer"}},
         "required": ["text"]}},
    {"name": "run_sql",
     "description": "Run ONE read-only MariaDB SELECT for questions that need joins, date grouping or "
                    "calculations across tables. Tables are named `tabDocType Name` (with backticks), child "
                    "tables join on parent = main.name. Use describe_doctype first for exact column names. "
                    "Select only the columns you need.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "a single SELECT ... statement, no semicolon"},
         "limit": {"type": "integer", "description": "max rows (1-100)"}},
         "required": ["query"]}},
]


def get_schemas():
    """Tools this user may use (run_sql only for allowed roles)."""
    return [s for s in SCHEMAS if s["name"] != "run_sql" or _sql_allowed()]


def run_tool(name, args):
    fn = FUNCTIONS.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool {name}"})
    try:
        result = fn(**(args or {}))
    except ToolError as e:
        return json.dumps({"error": str(e)})
    except TypeError as e:
        return json.dumps({"error": f"Wrong arguments for {name}: {e}"})
    except frappe.PermissionError:
        return json.dumps({"error": "Permission denied for this data."})
    except Exception as e:
        frappe.db.rollback()
        return json.dumps({"error": f"{type(e).__name__}: {cstr(e)[:300]}"})
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + ' ... [truncated: ask for fewer rows/fields or use aggregate]'
    return text
