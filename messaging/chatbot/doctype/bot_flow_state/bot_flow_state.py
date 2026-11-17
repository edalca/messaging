"""Read-only Link identities for native Table MultiSelect, subordinate to Bot Flow."""
import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.primitives import identity_key
from messaging.common.write_guards import InternalWriteGuard

_state_write = InternalWriteGuard('messaging_flow_state')


class BotFlowState(Document):
    def validate(self):
        if not _state_write.active():
            raise frappe.PermissionError(_('Functional states are managed by Bot Flow.'))
        self.name = state_name(self.bot_flow, self.status_code)

    def on_trash(self):
        if not _state_write.active():
            raise frappe.PermissionError(_('Functional states are managed by Bot Flow.'))

    def before_rename(self, *args, **kwargs):
        raise frappe.PermissionError(_('Functional states are managed by Bot Flow.'))


def state_name(flow, status):
    return identity_key(flow, status)


def synchronize(flow):
    """Persist only Link identities; actions and fields remain owned by Flow Output."""
    available = {state_name(flow.name, row.status_code): row.status_code
        for row in flow.outputs if row.flow_action == 'Complete Flow'}
    with _state_write():
        for name, status in available.items():
            if not frappe.db.exists('Bot Flow State', name):
                frappe.get_doc(dict(doctype='Bot Flow State', name=name,
                    bot_flow=flow.name, status_code=status)).insert(ignore_permissions=True)
        # Keep referenced identities so stale settings can be opened and corrected.
        # Every query/save/execution rechecks the actual Flow Output, never this projection.
        for name in frappe.get_all('Bot Flow State', filters={'bot_flow': flow.name}, pluck='name'):
            if name not in available and not frappe.db.exists('Flow Consumer State', {'state': name}):
                frappe.delete_doc('Bot Flow State', name, ignore_permissions=True)


def remove(flow):
    with _state_write():
        for name in frappe.get_all('Bot Flow State', filters={'bot_flow': flow.name}, pluck='name'):
            frappe.delete_doc('Bot Flow State', name, ignore_permissions=True)
