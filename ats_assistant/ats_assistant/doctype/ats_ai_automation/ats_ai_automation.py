import frappe
from frappe.model.document import Document
from frappe.utils import cint


class ATSAIAutomation(Document):
    def validate(self):
        from ats_assistant.assistant import automation

        # a normal user can only make automations for himself; System Manager may choose the user
        if not self.user or "System Manager" not in frappe.get_roles():
            if self.is_new() or not self.user:
                self.user = frappe.session.user
        self.frequency = automation.normalise_frequency(self.frequency)
        if self.frequency == "Once" and not self.run_date:
            frappe.throw("Please set the date for a one-time automation.")
        if self.frequency == "Monthly":
            self.day_of_month = min(max(cint(self.day_of_month) or 1, 1), 31)
        if self.frequency == "Weekly" and not self.weekday:
            self.weekday = "Monday"
        self.next_run = automation.compute_next(self) if cint(self.enabled) else None
