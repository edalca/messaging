"""Bot Flow argument capture and functional output mapping over the neutral executor."""
import frappe
from frappe import _

from messaging.chatbot.doctype.bot_flow.bot_flow import (
	get_flow_effective_internal_error_message,
	process_arguments,
	resolve_flow_output,
	validate_runtime_arguments,
)
from messaging.common.locking import shared_document
from messaging.communication.doctype.process_definition.execution import execute_process
from messaging.communication.doctype.process_execution_log.process_execution_log import require_execution_receipt
from messaging.registrations.processes import validate_captured_process_argument
from messaging.communication.doctype.process_definition.validation import fingerprint


def invoke(flow, values, *, session, turn, execution_id):
	"""Adapt Bot Flow parameters and outputs to the shared executor."""
	flow = shared_document("Bot Flow", flow.name)

	def prepare(arguments):
		arguments.update(process_arguments(flow, values))

	output = {}

	def render(result):
		output.update(resolve_flow_output(flow, result))
		return output

	result = execute_process(
		process_method=flow.process_method,
		arguments={},
		source_type="Bot Flow",
		source_name=flow.name,
		workflow=flow,
		session=session.name,
		turn=turn.name,
		execution_id=execution_id,
		invocation_mode=flow.process_invocation_mode,
		prepare=prepare,
		render_result=render,
		public_error=lambda: get_flow_effective_internal_error_message(flow),
	)
	if result["functional"]:
		result.update(
			data=output["data"],
			flow_action=output.get("flow_action", "Complete Flow"),
			retry_parameter=output.get("retry_parameter"),
		)
	return result


def require_execution_result(flow, result, *, session, turn, execution_id):
	"""Bind consumer side effects to the executor's exact current protected receipt."""
	if not result.get("functional") or not result.get("execution_log"):
		raise frappe.ValidationError(_("A verified functional Process result is required."))
	log = require_execution_receipt(result['execution_log'], source_type='Bot Flow', source_name=flow.name,
		session=session, turn=turn, execution_id=execution_id, status=result.get('status'))
	if (log.process_definition, log.configuration_fingerprint) != (flow.process_definition, fingerprint(flow)):
		raise frappe.ValidationError(_("The Process receipt does not match the current Flow execution."))
	return log
