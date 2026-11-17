"""Atomic registration of application-owned scenarios and their localized cases."""

import frappe
from frappe import _

from messaging.security.permissions import require_registration_owner
from .ai_evaluation_case_template import _registry_write, metadata, refresh_cases_revision, reject

OPTIONAL = {'description', 'applicable_application', 'provider', 'group', 'available_flows', 'greeting_available', 'human_attention_available',
    'previously_recognized_flows', 'parameters'}


def _declarations(app, evaluations):
    """Validate owner identities on the public contract's transient native projection."""
    from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import default_case_identity
    seen, case_names = set(), set()
    for raw in evaluations:
        identifier = raw['template_id']
        if not identifier.startswith(app + '.') or identifier in seen:
            reject('Registered template IDs must be unique and use the owning application namespace.')
        seen.add(identifier)
        for case in raw['cases']:
            name = default_case_identity(identifier, case['language'])
            if name in case_names:
                reject('Default evaluation case identities collide after normalization.')
            case_names.add(name)
        yield raw


def _register_evaluations(*, app, evaluations, caller):
    require_registration_owner(app, caller)
    from messaging.common.locking import lock_document_types
    from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import sync_default_cases
    declarations = tuple(_declarations(app, evaluations))
    lock_document_types('AI Evaluation Case Template', 'AI Evaluation Case')
    savepoint = 'templates_' + frappe.generate_hash(length=12)
    frappe.db.savepoint(savepoint)
    summary = dict(created=0, updated=0, unchanged=0, cases_created=0, cases_updated=0, cases_unchanged=0)
    try:
        with _registry_write():
            for raw in declarations:
                identifier = raw['template_id']
                exists = frappe.db.exists('AI Evaluation Case Template', identifier)
                doc = frappe.get_doc('AI Evaluation Case Template', identifier, for_update=True) if exists else frappe.new_doc('AI Evaluation Case Template')
                if exists and (doc.origin != 'Registered' or doc.application != app):
                    raise frappe.PermissionError(_('A template cannot replace another owner or a custom definition.'))
                before = (doc.definition_revision, doc.metadata_revision, doc.cases_revision)
                original_metadata = metadata(doc) if exists else None
                for key in OPTIONAL:
                    field = doc.meta.get_field(key)
                    doc.set(key, [] if field.fieldtype == 'Table' else int(field.default or 0) if field.fieldtype in ('Int', 'Check') else field.default)
                doc.update({key: value for key, value in raw.items() if key != 'cases'})
                doc.update(dict(origin='Registered', application=app, mandatory=int(raw['mandatory'])))
                doc.before_validate();doc.validate()
                if not exists or before[0] != doc.definition_revision or original_metadata != metadata(doc):
                    doc.save()
                cases_summary = sync_default_cases(doc, app=app, cases=raw['cases'])
                for key, count in cases_summary.items():
                    summary[key] += count
                from .queries import coverage_status
                status = coverage_status(identifier)
                if any(r['available'] == 0 or r['required'] and not r['sufficient'] for r in status):
                    reject('Registered required coverage exceeds its available localized case bank.')
                refresh_cases_revision(identifier)
                doc.reload()
                current = (doc.definition_revision, doc.metadata_revision, doc.cases_revision)
                summary['created' if not exists else 'unchanged' if current == before else 'updated'] += 1
                from messaging.chatbot.doctype.ai_evaluation_run.dependencies import dependencies_changed
                dependencies_changed(doc)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return summary


def register_evaluations(*, app, evaluations):
    """Synchronize typed evaluation documents in the verified owner's transaction."""
    import inspect
    from .configuration import validate_definitions
    frame = inspect.currentframe()
    try:
        validate_definitions(evaluations)
        return _register_evaluations(app=app, evaluations=tuple(d.initial_values() for d in evaluations), caller=frame.f_back)
    finally:
        del frame
