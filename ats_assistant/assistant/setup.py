import frappe

ROLE = "AI Assistant User"


def ensure_role():
    """Create the role that is allowed to use the ATS Assistant chat."""
    if not frappe.db.exists("Role", ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(ignore_permissions=True)
        frappe.db.commit()
    print(f"Role ready: {ROLE}")
