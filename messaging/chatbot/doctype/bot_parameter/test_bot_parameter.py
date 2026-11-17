import unittest
import frappe
from frappe.tests.utils import FrappeTestCase

from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	compile_expression,
	extract_expression_values,
	validate_parameter_value,
	validate_parameter_values,
)


class TestBotParameter(FrappeTestCase):
	"""Exercise Parameter validation with deliberate Spanish business message samples."""

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings
		configure_test_settings()

	def tearDown(self) -> None:
		frappe.db.rollback()
		super().tearDown()

	def _create_parameter(self, code: str, ptype: str = "String", mask: str | None = None, **kwargs):
		if frappe.db.exists("Bot Parameter", code):
			frappe.delete_doc("Bot Parameter", code, force=True)
		doc = frappe.get_doc(
			{
				"doctype": "Bot Parameter",
				"name": code,
				"label": kwargs.pop("label", f"Param {code}"),
				"parameter_type": ptype,
				"expression": mask,
				"enabled": kwargs.pop("enabled", 1),
				"request_phrases": [dict(language="es", request_phrase="Please provide the value.")],
				**kwargs,
			}
		)
		doc.insert()
		return doc

	def test_create_and_persist_supported_parameter_types(self) -> None:
		p_str = self._create_parameter("test_full_name", "String", min_length=2, max_length=80)
		p_int = self._create_parameter("test_year", "Int", min_value=2000, max_value=2100)
		p_fmt = self._create_parameter("test_ref_code", "Expression", mask="SC-#####")

		reloaded_str = frappe.get_doc("Bot Parameter", p_str.name)
		reloaded_int = frappe.get_doc("Bot Parameter", p_int.name)
		reloaded_fmt = frappe.get_doc("Bot Parameter", p_fmt.name)

		self.assertEqual(reloaded_str.name, "test_full_name")
		self.assertEqual(reloaded_str.label, "Param test_full_name")
		self.assertFalse(frappe.get_meta("Bot Parameter").has_field("parameter_code"))
		self.assertFalse(frappe.get_meta("Bot Parameter").has_field("parameter_name"))
		self.assertEqual(reloaded_str.parameter_type, "String")
		self.assertIsNone(reloaded_str.expression)
		self.assertEqual(reloaded_int.parameter_type, "Int")
		self.assertEqual(reloaded_fmt.parameter_type, "Expression")
		self.assertEqual(reloaded_fmt.expression, "SC-#####")

	def test_parameter_identity_is_immutable(self):
		parameter = self._create_parameter("test_immutable_parameter")
		with self.assertRaises(frappe.ValidationError):
			frappe.rename_doc("Bot Parameter", parameter.name, "test_changed_parameter", force=True)

	def test_directed_answer_targets_requested_overlapping_integer(self):
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import capture

		first = self._create_parameter("test_directed_first_integer", "Int", min_value=0, max_value=10)
		second = self._create_parameter("test_directed_second_integer", "Int", min_value=0, max_value=1)
		rows = [frappe._dict(parameter=parameter.name, required=1, capture_order=index)
			for index, parameter in enumerate((first, second), 1)]
		options = dict(accept_text=True, requested_parameter=first.name)
		values, missing, invalid = capture(rows, {}, dict(text="11"), **options)
		self.assertTrue(invalid)
		self.assertEqual(values, {})
		values, missing, invalid = capture(rows, {}, dict(text="0"), **options)
		self.assertEqual(values, {first.name: 0})
		self.assertEqual([row.parameter for row in missing], [second.name])
		self.assertFalse(invalid)
		values, missing, invalid = capture(rows, values, dict(text="1"), accept_text=True, requested_parameter=second.name)
		self.assertEqual(values, {first.name: 0, second.name: 1})
		self.assertEqual(missing, [])
		self.assertFalse(invalid)
		values, missing, invalid = capture(rows, {}, dict(text="Tengo 0 y 1"), **options)
		self.assertEqual(values, {})
		self.assertEqual(len(missing), 2)
		text = self._create_parameter("test_directed_text", "String")
		rows.insert(0, frappe._dict(parameter=text.name, required=1, capture_order=0))
		values, missing, invalid = capture(rows, {}, dict(text="Tengo 0 y 1"), **options)
		self.assertNotIn(text.name, values)
		date = self._create_parameter("test_directed_date", "Date")
		values, missing, invalid = capture(
			[frappe._dict(parameter=date.name, required=1, capture_order=1)], {},
			dict(text="2024-02-29"), accept_text=True, requested_parameter=date.name)
		self.assertEqual(values, {})
		self.assertTrue(invalid)

	def test_expression_type_requires_patterns_and_rejects_contradictory_types(self):
		for index, expression in enumerate((None, "", "\n\n", "   ", "#####\n\\q")):
			with self.subTest(expression=expression), self.assertRaises(frappe.ValidationError):
				self._create_parameter(f"test_required_expression_{index}", "Expression", mask=expression)
		for kind in ("String", "Int", "Format"):
			with self.subTest(kind=kind), self.assertRaises(frappe.ValidationError):
				self._create_parameter("test_contradictory_type", kind, mask="#####")
		parameter = self._create_parameter("test_explicit_transition", "Expression", mask="#####")
		parameter.parameter_type = "String"
		with self.assertRaises(frappe.ValidationError):
			parameter.save()
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "#####")
		parameter.reload()
		parameter.parameter_type = "String"
		parameter.expression = ""
		parameter.save()
		self.assertEqual(validate_parameter_value(parameter, "free text"), (True, "free text"))

	def test_three_types_keep_independent_value_contracts(self):
		text = self._create_parameter("test_free_text", "String", min_length=2, max_length=20)
		integer = self._create_parameter("test_integer", "Int", min_value=0, max_value=99999)
		expression = self._create_parameter("test_text_identity", "Expression", mask="#####\nSC-#####\nYY####")
		self.assertEqual(validate_parameter_value(text, " arbitrary text "), (True, "arbitrary text"))
		self.assertEqual(validate_parameter_value(integer, "00123"), (True, 123))
		self.assertFalse(validate_parameter_value(integer, "SC-00123")[0])
		for value in ("00123", "SC-12345", "YY1234"):
			self.assertEqual(validate_parameter_value(expression, value), (True, value))
		for value in (123, "123", "ABC1234", "123456"):
			self.assertFalse(validate_parameter_value(expression, value)[0])
		self.assertEqual(validate_parameter_values(expression, ["00123", "sc-12345", "SC-12345"], multiple=True),
			(True, ["00123", "sc-12345"]))
		# A stale in-memory field never adds expression semantics to String or Int.
		text.expression = integer.expression = "SC-#####"
		self.assertEqual(validate_parameter_value(text, "free text"), (True, "free text"))
		self.assertEqual(validate_parameter_value(integer, "00123"), (True, 123))

	def test_native_type_metadata(self):
		meta = frappe.get_meta("Bot Parameter")
		self.assertEqual(meta.get_field("parameter_type").options.splitlines(), ["String", "Int", "Date", "Name", "Expression", "Decimal", "Boolean"])
		field = meta.get_field("expression")
		self.assertEqual(field.fieldtype, "Small Text")
		self.assertEqual(field.depends_on, "eval:doc.parameter_type=='Expression'")
		self.assertEqual(field.mandatory_depends_on, field.depends_on)

	def test_date_name_and_assistance_persistence(self):
		for code, kind in (("test_native_date", "Date"), ("test_native_name", "Name")):
			parameter = self._create_parameter(code, kind, allow_ai_extraction=1)
			self.assertEqual(frappe.get_doc("Bot Parameter", code).parameter_type, kind)
			self.assertEqual(parameter.allow_ai_extraction, 1)
		date = frappe.get_doc("Bot Parameter", "test_native_date")
		for raw, expected in (("26/10/2026", "2026-10-26"), ("29/02/2024", "2024-02-29"), ("2024-02-29", "2024-02-29")):
			self.assertEqual(validate_parameter_value(date, raw), (True, expected))
		for raw in ("29/02/2023", "31/04/2026", "01/13/2026", "1/1/2026", "2026/10/26", "01/01/0000"):
			self.assertFalse(validate_parameter_value(date, raw)[0])
		name = frappe.get_doc("Bot Parameter", "test_native_name")
		for raw in ("Prince", "María Fernanda López García", "José de la Cruz", "Anne-Marie O’Neill", "محمد علي", "李小龍", "山田太郎", "अनुष्का शर्मा", "Jose\u0301 Pérez"):
			self.assertEqual(validate_parameter_value(name, raw), (True, raw))
		for raw in ("0801199012345", "Quiero consultar mi contrato", "Mi nombre es Juan", "Juan\nPérez", "Juan y Pedro"):
			self.assertFalse(validate_parameter_value(name, raw)[0])
		with self.assertRaises(frappe.ValidationError):
			self._create_parameter("test_expression_ai", "Expression", mask="#####", allow_ai_extraction=1)

	def test_string_formats_and_signed_integer(self):
		text = self._create_parameter("test_native_text", "String")
		for value in ("blue", "a descriptive sentence with several words"):
			self.assertEqual(validate_parameter_value(text, value), (True, value))
		text.string_mode = "Single Word"
		self.assertTrue(validate_parameter_value(text, "blue")[0])
		self.assertFalse(validate_parameter_value(text, "several words")[0])
		integer = self._create_parameter("test_signed_integer", "Int", min_value=-10, max_value=10)
		for raw, expected in (("+5", 5), ("-5", -5)):
			self.assertEqual(validate_parameter_value(integer, raw), (True, expected))
		for raw in ("1.5", "+", "-11", "11"):
			self.assertFalse(validate_parameter_value(integer, raw)[0])

	def test_central_registry_dispatch_and_installed_select_metadata(self):
		from unittest.mock import patch
		from messaging.chatbot.doctype.bot_parameter import parameters
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import capture_text, recognize, on_doctype_update
		expected = ("String", "Int", "Date", "Name", "Expression", "Decimal", "Boolean")
		self.assertEqual(parameters.get_supported_types(), expected)
		for kind, implementation in zip(expected, ("StringType", "IntegerType", "DateType", "NameType", "ExpressionType", "DecimalType", "BooleanType")):
			self.assertEqual(type(parameters.get_handler(kind)).__name__, implementation)
		self.assertEqual(frappe.get_meta("Bot Parameter").get_field("parameter_type").options.splitlines(), list(expected))
		with self.assertRaises(ValueError):
			parameters.get_handler("Unregistered")
		with self.assertRaises(frappe.ValidationError):
			self._create_parameter("test_unregistered_kind", "Unregistered")
		text = self._create_parameter("test_registry_text", "String")
		self.assertEqual(parameters.get_handler("String").extract(text, "El color de mi casa es verde.").status, "NOT_SUPPORTED")
		self.assertEqual(recognize("El color de mi casa es verde.", {text.name:text})[1], [])
		self.assertEqual(capture_text(text, "a general descriptive sentence"), (True, "a general descriptive sentence"))
		try:
			with patch.dict(parameters._HANDLERS, SyntheticText=("string", "StringType")):
				on_doctype_update()
				self.assertIn("SyntheticText", frappe.get_meta("Bot Parameter").get_field("parameter_type").options.splitlines())
				synthetic = self._create_parameter("test_registry_extension", "SyntheticText")
				self.assertEqual(validate_parameter_value(synthetic, "a native value"), (True, "a native value"))
		finally:
			on_doctype_update()
			parameters.get_handler.cache_clear()

	def test_parameter_name_validation(self) -> None:
		for invalid_code in ("123abc", "Invalid-Code", "UPPERCASE", "for", "a"):
			with self.assertRaises(frappe.ValidationError):
				self._create_parameter(invalid_code, "String")

	def test_expression_literals_alternatives_and_escapes(self):
		for expression, value in [("#####", "12345"), ("SC-#####", "sc-12345"),
			("@@-#####", "xy-12345"), ("A-#####", "a-12345"),
			(r"\#\@\\", "#@\\"), ("ABC", "abc")]:
			with self.subTest(expression=expression):
				self.assertTrue(compile_expression(expression)[0][0].fullmatch(value))
		self.assertFalse(compile_expression("A-#####")[0][0].fullmatch("B-12345"))
		self.assertFalse(compile_expression("@@-#####")[0][0].fullmatch("İa-12345"))
		self.assertEqual(len(compile_expression("\n#####\n\nSC-#####\n")), 2)
		self.assertEqual(compile_expression(""), [])

	def test_expression_rejects_invalid_syntax_with_line_number(self):
		for expression, line in [("#####\nSC-\\x", 2), ("ABC\\", 1),
			("SC-#####\nsc-#####", 2), ("#"*257, 1),
			("\n".join(str(i) for i in range(33)), 33), ("#\t#", 1),
			("###\r", 1), ("[0-9]+", 1), (" "*8193, 1), ("Aé", 1)]:
			with self.subTest(expression=expression[:50]):
				with self.assertRaisesRegex(frappe.ValidationError, str(line)):
					compile_expression(expression)

	def test_expression_extraction_boundaries_order_and_overlaps(self):
		expression = "#####\nSC-#####\n@@-#####"
		self.assertEqual(extract_expression_values(expression,
			"Consulta el saldo de SC-12345 y 67890."), ["SC-12345", "67890"])
		for text in ("123456", "X12345", "12345X", "ZZ-SC-12345", "é12345"):
			self.assertEqual(extract_expression_values(expression, text), [])
		self.assertEqual(extract_expression_values(expression,
			"sc-12345 67890 sc-12345"), ["sc-12345", "67890", "sc-12345"])

	def test_expression_save_and_runtime_share_compiler_and_list_bounds(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_parameter("test_invalid_expression", "Expression", mask="#####\n\\q")
		parameter = self._create_parameter("test_expression", "Expression", mask="#####\nSC-#####")
		self.assertEqual(validate_parameter_values(parameter, ["12345", "sc-12345", "SC-12345"], multiple=True),
			(True, ["12345", "sc-12345"]))
		self.assertFalse(validate_parameter_values(parameter, ["12345", "123"], multiple=True)[0])
		self.assertFalse(validate_parameter_values(parameter, ["12345"])[0])
		self.assertFalse(validate_parameter_values(parameter, ["12345"]*101, multiple=True)[0])
		self.assertFalse(validate_parameter_values(parameter, [["12345"]], multiple=True)[0])
		self.assertFalse(validate_parameter_value(parameter, "123")[0])

	def test_validate_parameter_values(self) -> None:
		p_int = self._create_parameter("test_qty", "Int", min_value=1, max_value=10)
		p_fmt = self._create_parameter("test_doc_id", "Expression", mask="####-####-#####")

		ok, val = validate_parameter_value(p_int, "5")
		self.assertTrue(ok)
		self.assertEqual(val, 5)

		self.assertFalse(validate_parameter_value(p_int, "15")[0])
		self.assertFalse(validate_parameter_value(p_int, True)[0])
		self.assertFalse(validate_parameter_value(p_int, "abc")[0])

		self.assertTrue(validate_parameter_value(p_fmt, "0801-1995-00123")[0])
		self.assertFalse(validate_parameter_value(p_fmt, "0801-1995-0012A")[0])

	def test_protection_of_parameter_referenced_by_flow(self) -> None:
		from messaging.communication.doctype.process_definition import validation as testing
		from messaging.registrations.processes import BotProcess
		from messaging.registry.processes import register_processes

		param = self._create_parameter("test_protected_param", "Expression", mask="SC-#####")
		frappe.session.sid = "protection-" + frappe.generate_hash(length=8)
		declaration = BotProcess(
			id="messaging.protection_" + frappe.generate_hash(length=6),
			label="Protection", definition=parameter_handler, handler=parameter_result,
		)
		_parameter_contracts[declaration.id] = [dict(name='code', type='string', multiple=False)]
		register_processes(app="messaging", processes=[declaration])
		used = {int(value) for value in frappe.get_all("Bot Flow", filters={"priority": [">", 0]}, pluck="priority")}
		priority = 8200
		while priority in used:
			priority += 1
		code = "test_wf_" + frappe.generate_hash(length=6)
		wf = frappe.new_doc("Bot Flow")
		wf.update(dict(
			workflow_code=code, workflow_name="Flow Using Protected Param",
			semantic_description="Checks referenced parameter protection.",
			priority=priority, enabled=0, process_definition=declaration.id,
			input_initial_message="Enter the code.", input_continuation_message="Enter the code.",
			outputs=[dict(status_code="success", output_mode="Message")],
		))
		wf.append("parameters", dict(
			parameter=param.name, required=0, capture_order=1, process_argument="code",
			allow_multiple_values=0, minimum_values=0, maximum_values=0,
		))
		from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		language = resolve_process_language()
		wf.append('translations', dict(language=language, menu_label='Protection', semantic_description=wf.semantic_description))
		wf.append('input_translations', dict(language=language, input_initial_message=wf.input_initial_message,
			input_continuation_message=wf.input_continuation_message))
		wf.insert()
		self.assertEqual(validate_process(wf.as_dict())['status'], 'Passed')
		wf.reload()

		try:
			param.reload()
			param.parameter_type = "Int"
			param.expression = ""
			with self.assertRaisesRegex(frappe.ValidationError, "already referenced"):
				param.save()

			param.reload()
			param.expression = "####-####-#####"
			with self.assertRaisesRegex(frappe.ValidationError, "already referenced"):
				param.save()

			param.reload()
			param.enabled = 0
			with self.assertRaisesRegex(frappe.ValidationError, "referenced"):
				param.save()

			param.reload()
			with self.assertRaises(frappe.ValidationError):
				param.delete()
		finally:
			frappe.delete_doc("Bot Flow", wf.name, force=True)
			frappe.delete_doc("Bot Parameter", param.name, force=True)

"""Synthetic focal cases for Unicode introductions, offsets and safe abstention."""

import unittest

import frappe

from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	captured_values,
	extract_name,
	recognize,
)


class TestNameRecognition(unittest.TestCase):
	def parameters(self):
		return {
			"person": frappe._dict(
				name="person",
				label="Person name",
				enabled=1,
				parameter_type="Name",
				allow_contextual_name_extraction=1,
			),
			"identity": frappe._dict(
				name="identity",
				label="Identity",
				enabled=1,
				parameter_type="Expression",
				expression="#############\n#########",
			),
			"contract": frappe._dict(
				name="contract",
				label="Contract",
				enabled=1,
				parameter_type="Expression",
				expression="CTR-@#####",
			),
		}

	def test_direct_names_and_conversational_requests(self):
		from messaging.chatbot.doctype.bot_parameter.parameters.name import valid_name
		for value in ("María José de la Cruz", "Anne-Marie O’Neill", "D'Arcy Smith",
				"Jose\u0301 Pérez", "李明", "J. R. Smith", "Luz", "Will", "Hope", "Consulta",
				"May Water", "Dolores del Río"):
			with self.subTest(value=value):
				self.assertTrue(valid_name(value))
		for value in ("Quisiera consultar el saldo de mi servicio de agua.",
				"I would like to know my water service balance.",
				"Quisiera consultar el saldo", "Podría saber el estado del servicio",
				"Could you check my balance", "Please check the water service",
				"Cómo puedo pagar", "What is my balance", "Ana Pérez?",
				"My name is I would like to know my balance"):
			with self.subTest(value=value):
				self.assertFalse(valid_name(value))
		# A deterministic structural validator cannot resolve every plausible phrase.
		self.assertTrue(valid_name("River Water"))
		self.assertNotEqual(extract_name("My name is I would like to know my balance").status, "EXTRACTED")

	def test_introductions_and_original_masked_offsets(self):
		cases = [
			("Me llamo Juan Pérez y mi número de identidad es 0801199012345.", "Juan Pérez"),
			("Mi nombre es María Fernanda López García.", "María Fernanda López García"),
			("Soy José de la Cruz y quiero consultar mi contrato.", "José de la Cruz"),
			("My name is John Smith and my ID is 123456789.", "John Smith"),
			("Me llamo Ana López CTR-G12345.", "Ana López"),
			("Contrato CTR-G12345. Me llamo Jose\u0301 Pérez.", "Jose\u0301 Pérez"),
		]
		for text, expected in cases:
			with self.subTest(text=text):
				original = text[:]
				transformed, occurrences = recognize(text, self.parameters())
				name = next(item for item in occurrences if item.parameter == "person")
				self.assertEqual(name.value, expected)
				self.assertEqual(text[name.start : name.end], expected)
				for item in occurrences:
					self.assertEqual(transformed[item.masked_start : item.masked_end], item.marker)
					self.assertEqual(text[item.start : item.end], item.value)
				self.assertEqual(text, original)
				self.assertNotIn(expected, transformed)

	def test_uncertainty_missing_marker_and_disabled_recognition(self):
		for text, expected in (
			("Me llamo Ana López y Pedro Pérez.", "AMBIGUOUS"),
			("Me llamo Ana. Mi esposo se llama Pedro.", "AMBIGUOUS"),
			("Mi esposa se llama María y su identidad es 0801199012345.", "AMBIGUOUS"),
			("No tengo el dato.", "NOT_FOUND"),
			("Juan Pérez", "NOT_FOUND"),
			("Quiero consultar mi contrato.", "NOT_FOUND"),
			("Me llamo Ana López CTR-G12345.", "AMBIGUOUS"),
		):
			with self.subTest(text=text):
				self.assertEqual(extract_name(text).status, expected)
		parameters = self.parameters()
		parameters["person"].allow_contextual_name_extraction = 0
		self.assertFalse(
			any(item.parameter == "person" for item in recognize("Me llamo Juan Pérez.", parameters)[1])
		)

	def test_no_arbitrary_assignment_to_equivalent_parameters(self):
		parameters = self.parameters()
		parameters["second_person"] = frappe._dict(
			parameters["person"], name="second_person", label="Other person"
		)
		_text, occurrences = recognize("Me llamo Juan Pérez.", parameters)
		values, _ambiguous = captured_values(occurrences, parameters, set())
		self.assertNotIn("person", values)
		self.assertNotIn("second_person", values)


from unittest.mock import patch

from messaging.registrations.processes import BotProcess, BotRequest, BotResponse
from messaging.registry.processes import register_processes
from messaging.communication.doctype.process_definition import validation as testing
from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures
from messaging.chatbot.doctype.bot_flow.bot_flow import BotFlow
from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	apply_referenced_update,
	capture,
	capture_context,
	preview_referenced_update,
	validate_input_value,
)


_parameter_contracts = {}


def parameter_handler(*, params, outcomes):
    for item in _parameter_contracts[params._context.process_id]:
        params.register(item['name'], type=item['type'], multiple=item['multiple'], required=item.get('required', False))
    outcomes.register('success', fields={}, mode=['message', 'template'], action='complete_flow')


def parameter_result(request: BotRequest) -> BotResponse:
    return BotResponse('success', message='Saved result')


class TestReferencedParameterUpdate(ProcessFixtures, unittest.TestCase):
	"""Confirmed edits of a shared parameter. Direct saves stay rejected."""

	def definition_id(self, inputs=None):
		if inputs is None and getattr(self, '_definition_id', None):
			return self._definition_id
		identifier = 'messaging.' + self.prefix + '_definition_' + str(len(self.definitions))
		_parameter_contracts[identifier] = inputs or []
		declaration = BotProcess(id=identifier, label='Referenced update', definition=parameter_handler, handler=parameter_result)
		self.definitions.append(declaration)
		register_processes(app='messaging', processes=self.definitions)
		self._definition_id = identifier
		return identifier

	def take_priority(self):
		used = {int(value) for value in frappe.get_all("Bot Flow", filters={"priority": [">", 0]}, pluck="priority")}
		used.update(getattr(self, "_priorities", set()))
		priority = 8100
		while priority in used:
			priority += 1
		self._priorities = used | {priority}
		return priority

	def parameter(self, suffix, **fields):
		values = dict(label="Shared value", parameter_type="String", enabled=1,
			request_phrases=[dict(language="es", request_phrase="Please provide the shared value.")])
		values.update(fields)
		return frappe.get_doc(dict(doctype="Bot Parameter", name=f"{self.prefix}_{suffix}", **values)).insert()

	def add_flow(self, parameter, *, enabled=0, required=1, multiple=0, minimum=0, maximum=0,
		argument=None, priority=None, extra=None):
		code = f"{self.prefix}_f{len(getattr(self, '_flows', []))}"
		self._flows = getattr(self, "_flows", []) + [code]
		flow = frappe.new_doc("Bot Flow")
		flow.update(dict(
			workflow_code=code, workflow_name=code, semantic_description="Shared parameter consumer",
			priority=self.take_priority() if priority is None else priority, status='Disabled',
			process_definition=self.definition_id(inputs=[dict(name=argument or parameter.name,
			type={'Int': 'integer', 'Date': 'date'}.get(parameter.parameter_type, 'string'), multiple=bool(multiple), required=bool(required))]
			+ [dict(name=row.get('process_argument') or row['parameter'],
				type={'Int': 'integer', 'Date': 'date'}.get(frappe.get_doc('Bot Parameter', row['parameter']).parameter_type, 'string'),
				multiple=bool(row.get('allow_multiple_values')), required=bool(row.get('required'))) for row in extra or []]),
			input_initial_message="Enter the value", input_continuation_message="Enter the value",
			outputs=[dict(status_code="success", output_mode="Message")],
		))
		flow.append("parameters", dict(
			parameter=parameter.name, process_argument=argument or parameter.name, capture_order=1,
			required=required, allow_multiple_values=multiple,
			minimum_values=minimum if multiple else 0, maximum_values=maximum if multiple else 0,
		))
		for row in extra or []:
			flow.append("parameters", row)
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		language = resolve_process_language()
		flow.append('translations', dict(language=language, menu_label='Technical option', semantic_description=flow.semantic_description))
		flow.append('input_translations', dict(language=language, input_initial_message=flow.input_initial_message,
			input_continuation_message=flow.input_continuation_message))
		result = self.approve(flow)
		self.assertEqual(result['status'], 'Passed', result)
		if enabled:
			self.activate(flow)
		self.assertEqual(testing.test_state(flow), 'Passed')
		return flow

	def approval_sample(self, parameter):
		kind = parameter.parameter_type
		if kind == "Expression":
			lines = [line.strip() for line in (parameter.expression or "").splitlines() if line.strip()]
			if "#####" in lines:
				return "12345"
			if "SC-#####" in lines:
				return "SC-12345"
			return "12345"
		if kind == "Name":
			return "Ana"
		if kind == "Int":
			return 1
		if kind == "Date":
			return "01/01/2026"
		return "alpha"

	def relation(self, flow, parameter):
		return frappe.db.get_value(
			"Flow Parameter", {"parent": flow.name, "parameter": parameter},
			("process_argument", "required", "allow_multiple_values", "minimum_values",
				"maximum_values", "capture_order"),
			as_dict=True,
		)

	def test_confirmed_technical_and_phrase_edit_save_together(self):
		parameter = self.parameter('combined')
		flow = self.add_flow(parameter)
		values = dict(min_length=2, request_phrases=[dict(language='es', request_phrase='Updated reusable question.')])
		preview = preview_referenced_update(parameter.name, values)
		result = apply_referenced_update(parameter.name, values, preview['dependency_token'])
		self.assertTrue(result['applied'])
		parameter.reload()
		self.assertEqual(parameter.min_length, 2)
		self.assertEqual(parameter.request_phrases[0].request_phrase, 'Updated reusable question.')
		self.assertEqual(testing.test_state(frappe.get_doc('Bot Flow', flow.name)), 'Untested')

	def test_unreferenced_parameter_saves_without_confirmation(self):
		parameter = self.parameter("plain")
		preview = preview_referenced_update(parameter.name, {"label": "Renamed"})
		self.assertFalse(preview["confirmation_required"])
		self.assertIsNone(preview["dependency_token"])
		parameter.label = "Renamed"
		parameter.save()
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "label"), "Renamed")

	def test_preview_lists_every_consumer_without_writing(self):
		parameter = self.parameter("shared", parameter_type="Expression", expression="SC-#####")
		disabled = self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		enabled = self.add_flow(parameter, enabled=1, multiple=1, minimum=1, maximum=3)
		before = frappe.db.get_value("Bot Parameter", parameter.name, ("expression", "modified"), as_dict=True)
		preview = preview_referenced_update(parameter.name, {"expression": "SC-#####\n#####"})
		self.assertTrue(preview["confirmation_required"])
		self.assertEqual(preview["changes"], ["expression"])
		self.assertEqual(
			[row["name"] for row in preview["consumers"]],
			sorted((disabled.name, enabled.name)),
		)
		self.assertEqual(len(preview["consumers"]), 2)
		after = frappe.db.get_value("Bot Parameter", parameter.name, ("expression", "modified"), as_dict=True)
		self.assertEqual(after, before)

	def test_confirmed_expression_change_invalidates_only_affected_approvals(self):
		parameter = self.parameter("contracts", parameter_type="Expression", expression="SC-#####", label="Número de contrato")
		affected = self.add_flow(parameter, enabled=1, multiple=1, minimum=1, maximum=3, argument="contracts")
		other = frappe.new_doc("Bot Flow")
		other.update(dict(
			workflow_code=self.prefix + "_bare", workflow_name="Bare", semantic_description="Untouched",
			priority=0, status='Disabled', process_definition=self.definition_id(inputs=[]),
			outputs=[dict(status_code="success", output_mode="Message")],
		))
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		other.append('translations', dict(language=resolve_process_language(), menu_label='Bare option', semantic_description=other.semantic_description))
		self.assertEqual(self.approve(other)['status'], 'Passed')
		self.activate(other)
		relation = self.relation(affected, parameter.name)
		definition = affected.process_definition
		fingerprint = testing.fingerprint(affected)
		other_fingerprint = testing.fingerprint(other)
		revision = testing.runtime_revision()
		preview = preview_referenced_update(parameter.name, {"expression": "SC-#####\n#####"})
		result = apply_referenced_update(parameter.name, {"expression": "SC-#####\n#####"}, preview["dependency_token"])
		self.assertTrue(result["applied"])
		self.assertFalse(result["unchanged"])
		self.assertEqual(result["affected"], 1)
		self.assertEqual(result["needs_correction"], [affected.name])
		self.assertEqual(result["validated"], [])
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####\n#####")
		affected.reload()
		other.reload()
		self.assertEqual(testing.test_state(affected), "Untested")
		self.assertEqual(affected.process_test_status, "Passed")
		self.assertEqual(affected.process_test_fingerprint, fingerprint)
		self.assertNotEqual(testing.fingerprint(affected), fingerprint)
		self.assertEqual(testing.test_state(other), "Passed")
		self.assertEqual(testing.fingerprint(other), other_fingerprint)
		self.assertEqual(testing.runtime_revision(), revision)
		self.assertEqual(affected.process_definition, definition)
		self.assertEqual(self.relation(affected, parameter.name).expression, relation.expression)
		self.assertEqual(self.relation(affected, parameter.name).process_argument, "contracts")
		self.assertEqual(int(self.relation(affected, parameter.name).allow_multiple_values), 1)
		self.assertNotIn(affected.name, __import__(
			"messaging.chatbot.doctype.bot_settings.interpretation", fromlist=["catalog"]
		).catalog())
		self.assertIn(other.name, __import__(
			"messaging.chatbot.doctype.bot_settings.interpretation", fromlist=["catalog"]
		).catalog())

	def test_non_contract_change_does_not_invalidate_or_require_confirmation(self):
		parameter = self.parameter("text")
		flow = self.add_flow(parameter, enabled=1)
		fingerprint = testing.fingerprint(flow)
		preview = preview_referenced_update(parameter.name, {"allow_ai_extraction": 1})
		self.assertFalse(preview["confirmation_required"])
		result = apply_referenced_update(parameter.name, {"allow_ai_extraction": 1})
		self.assertTrue(result["applied"])
		flow.reload()
		self.assertEqual(testing.test_state(flow), "Untested")
		self.assertNotEqual(testing.fingerprint(flow), fingerprint)
		self.assertEqual(int(frappe.db.get_value("Bot Parameter", parameter.name, "allow_ai_extraction")), 1)

	def test_label_change_is_confirmed_and_invalidates_the_fingerprint(self):
		parameter = self.parameter("labelled", parameter_type="Expression", expression="#####")
		flow = self.add_flow(parameter, enabled=1)
		preview = preview_referenced_update(parameter.name, {"label": "Public label"})
		self.assertTrue(preview["confirmation_required"])
		self.assertEqual(preview["changes"], ["label"])
		result = apply_referenced_update(parameter.name, {"label": "Public label"}, preview["dependency_token"])
		self.assertTrue(result["applied"])
		self.assertEqual(result["validated"], [flow.name])
		self.assertEqual(result["needs_correction"], [])
		self.assertEqual(testing.test_state(flow), "Passed")
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "label"), "Public label")

	def test_direct_save_and_missing_confirmation_are_rejected(self):
		parameter = self.parameter("guarded", parameter_type="Expression", expression="SC-#####")
		self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		parameter.expression = "#####"
		with self.assertRaisesRegex(frappe.ValidationError, "already referenced"):
			parameter.save()
		parameter.reload()
		parameter.min_length = 2
		with self.assertRaisesRegex(frappe.ValidationError, "already referenced"):
			parameter.save()
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####")
		with self.assertRaisesRegex(frappe.ValidationError, "confirm it again"):
			apply_referenced_update(parameter.name, {"expression": "#####"})
		with self.assertRaisesRegex(frappe.ValidationError, "confirm it again"):
			apply_referenced_update(parameter.name, {"expression": "#####"}, "0" * 64)
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####")

	def test_token_cannot_be_reused_for_a_different_change(self):
		parameter = self.parameter("token", parameter_type="Expression", expression="SC-#####")
		self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		preview = preview_referenced_update(parameter.name, {"expression": "#####"})
		with self.assertRaisesRegex(frappe.ValidationError, "confirm it again"):
			apply_referenced_update(parameter.name, {"expression": "@@-#####"}, preview["dependency_token"])
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####")

	def test_concurrent_consumer_change_requires_a_new_confirmation(self):
		parameter = self.parameter("concurrent", parameter_type="Expression", expression="SC-#####")
		flow = self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		preview = preview_referenced_update(parameter.name, {"expression": "#####"})
		flow.workflow_name = "Changed after confirmation"
		flow.save()
		with self.assertRaisesRegex(frappe.ValidationError, "confirm it again"):
			apply_referenced_update(parameter.name, {"expression": "#####"}, preview["dependency_token"])
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####")
		self.assertEqual(testing.test_state(flow), "Passed")

	def test_repeating_an_applied_update_does_not_write_again(self):
		parameter = self.parameter("repeat", parameter_type="Expression", expression="SC-#####")
		flow = self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		preview = preview_referenced_update(parameter.name, {"expression": "#####"})
		first = apply_referenced_update(parameter.name, {"expression": "#####"}, preview["dependency_token"])
		self.assertTrue(first["applied"])
		modified = str(frappe.db.get_value("Bot Parameter", parameter.name, "modified"))
		second = apply_referenced_update(parameter.name, {"expression": "#####"}, preview["dependency_token"])
		self.assertTrue(second["unchanged"])
		self.assertEqual(str(frappe.db.get_value("Bot Parameter", parameter.name, "modified")), modified)
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "#####")
		self.assertEqual(testing.test_state(flow), "Untested")

	def test_transactional_failure_rolls_back_the_parameter(self):
		parameter = self.parameter("atomic", parameter_type="Expression", expression="SC-#####")
		self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		preview = preview_referenced_update(parameter.name, {"expression": "#####"})
		with patch.object(BotFlow, "validate_configuration", side_effect=RuntimeError("assessment failed")):
			with self.assertRaises(RuntimeError):
				apply_referenced_update(parameter.name, {"expression": "#####"}, preview["dependency_token"])
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####")

	def test_overlapping_parameters_remain_valid_and_invalidate_approval_on_change(self):
		first = self.parameter("left", parameter_type="Expression", expression="#####")
		second = self.parameter("right", parameter_type="Expression", expression="SC-#####")
		flow = self.add_flow(first, argument="left", extra=[dict(parameter=second.name,
			process_argument="right", capture_order=2, required=1)])
		preview = preview_referenced_update(second.name, {"expression": "#####"})
		result = apply_referenced_update(second.name, {"expression": "#####"}, preview["dependency_token"])
		self.assertTrue(result["applied"])
		self.assertEqual(frappe.db.get_value("Bot Parameter", second.name, "expression"), "#####")
		self.assertEqual(testing.test_state(frappe.get_doc('Bot Flow', flow.name)), "Untested")

	def test_disabling_a_referenced_parameter_rolls_back(self):
		parameter = self.parameter("active", parameter_type="Expression", expression="#####")
		flow = self.add_flow(parameter)
		preview = preview_referenced_update(parameter.name, {"enabled": 0})
		result = apply_referenced_update(parameter.name, {"enabled": 0}, preview["dependency_token"])
		self.assertFalse(result["applied"])
		self.assertEqual(int(frappe.db.get_value("Bot Parameter", parameter.name, "enabled")), 1)
		self.assertTrue(frappe.db.exists("Flow Parameter", {"parent": flow.name, "parameter": parameter.name}))
		self.assertEqual(testing.test_state(flow), "Passed")

	def test_missing_evidence_is_not_replaced_with_a_passed_approval(self):
		parameter = self.parameter("pending", parameter_type="Expression", expression="SC-#####")
		flow = self.add_flow(parameter, enabled=1, multiple=1, minimum=1, maximum=3)
		stored = flow.process_test_fingerprint
		preview = preview_referenced_update(parameter.name, {"expression": "#####"})
		apply_referenced_update(parameter.name, {"expression": "#####"}, preview["dependency_token"])
		promoted = {"promoted": testing.test_state(frappe.get_doc("Bot Flow", flow.name)) == "Passed", "approval": testing.test_state(frappe.get_doc("Bot Flow", flow.name))}
		self.assertFalse(promoted["promoted"])
		self.assertEqual(promoted["approval"], "Untested")
		flow.reload()
		self.assertEqual(testing.test_state(flow), "Untested")
		self.assertEqual(flow.process_test_fingerprint, stored)
		self.assertEqual(flow.process_test_status, "Passed")
		self.assertNotEqual(testing.fingerprint(flow), stored)

	def test_real_output_evidence_can_be_promoted_from_the_parameter_operation(self):
		parameter = self.parameter("renewed", parameter_type="Expression", expression="SC-#####")
		flow = self.add_flow(parameter, enabled=1, multiple=1, minimum=1, maximum=3, argument="contracts")
		preview = preview_referenced_update(parameter.name, {"expression": "SC-#####\n#####"})
		apply_referenced_update(parameter.name, {"expression": "SC-#####\n#####"}, preview["dependency_token"])
		self.assertEqual(testing.test_state(flow), "Untested")
		described = testing.describe(frappe.get_doc("Bot Flow", flow.name).as_dict())
		self.assertFalse(described["configuration_error"])
		self.assertEqual(described["outputs"][0]["status"], "Untested")
		tested = self.approve(flow)
		self.assertEqual(tested["status"], "Passed", tested)
		promoted = {"promoted": testing.test_state(frappe.get_doc("Bot Flow", flow.name)) == "Passed", "approval": testing.test_state(frappe.get_doc("Bot Flow", flow.name))}
		self.assertTrue(promoted["promoted"])
		self.assertEqual(promoted["approval"], "Passed")
		flow.reload()
		self.assertEqual(testing.test_state(flow), "Passed")
		self.assertEqual(flow.process_test_fingerprint, testing.fingerprint(flow))
		parameter.reload()
		self.assertEqual(parameter.expression, "SC-#####\n#####")
		self.assertEqual(flow.process_definition, self.definition_id())
		again = {"promoted": testing.test_state(flow) == "Passed", "unchanged": True}
		self.assertTrue(again["unchanged"])

	def test_guest_and_parameter_only_writer_are_rejected(self):
		parameter = self.parameter("secret", parameter_type="Expression", expression="SC-#####")
		self.add_flow(parameter, multiple=1, minimum=1, maximum=3)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			preview_referenced_update(parameter.name, {"expression": "#####"})
		frappe.set_user("Administrator")
		role = "param_writer_" + self.prefix
		frappe.get_doc(dict(doctype="Role", role_name=role, desk_access=1)).insert()
		from frappe.permissions import add_permission, update_permission_property
		add_permission("Bot Parameter", role, 0, "read")
		update_permission_property("Bot Parameter", role, 0, "write", 1)
		email = role + "@example.com"
		frappe.get_doc(dict(
			doctype="User", email=email, first_name="Limited", enabled=1, user_type="System User",
			send_welcome_email=0, roles=[dict(role=role)],
		)).insert()
		frappe.clear_cache(doctype="Bot Parameter")
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			apply_referenced_update(parameter.name, {"expression": "#####"}, "f" * 64)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Bot Parameter", parameter.name, "expression"), "SC-#####")


	def test_contextual_name_change_confirms_without_dropping_current_approval(self):
		parameter = self.parameter("person", parameter_type="Name", allow_contextual_name_extraction=1)
		flow = self.add_flow(parameter, enabled=1)
		preview = preview_referenced_update(parameter.name, {"allow_contextual_name_extraction": 0})
		self.assertTrue(preview["confirmation_required"])
		result = apply_referenced_update(
			parameter.name, {"allow_contextual_name_extraction": 0}, preview["dependency_token"]
		)
		self.assertTrue(result["applied"])
		self.assertEqual(result["validated"], [])
		self.assertEqual(result["needs_correction"], [flow.name])
		self.assertEqual(testing.test_state(flow), "Untested")
		self.assertEqual(int(frappe.db.get_value("Bot Parameter", parameter.name, "allow_contextual_name_extraction")), 0)


class TestContractExpressionRecognition(FrappeTestCase):
	"""Both contract spellings stay on one Expression parameter. Recognition stays local."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.parameter = frappe.get_doc(dict(
			doctype="Bot Parameter", name="test_contract_spellings", label="Número de contrato",
			parameter_type="Expression", expression="SC-#####\n#####", enabled=1,
		)).insert()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def row(self, **fields):
		values = dict(parameter=self.parameter.name, required=1, capture_order=1,
			allow_multiple_values=1, minimum_values=1, maximum_values=3)
		values.update(fields)
		return frappe._dict(values)

	def test_prefixed_plain_leading_zero_duplicate_and_invalid_values(self):
		for value in ("SC-12345", "sc-12345", "12345", "00123"):
			self.assertEqual(validate_parameter_value(self.parameter, value), (True, value))
		for value in ("ABC", "1234", "123456", "SC-1234", "SC12345"):
			self.assertFalse(validate_parameter_value(self.parameter, value)[0])
		self.assertEqual(extract_expression_values(self.parameter.expression, "SC-12345"), ["SC-12345"])
		self.assertEqual(
			validate_parameter_values(self.parameter, ["SC-12345", "12345", "00123"], multiple=True),
			(True, ["SC-12345", "12345", "00123"]),
		)
		self.assertEqual(
			validate_parameter_values(self.parameter, ["12345", "12345", "sc-12345", "SC-12345"], multiple=True),
			(True, ["12345", "sc-12345"]),
		)
		self.assertFalse(validate_input_value(
			self.row(), self.parameter, ["12345", "12346", "12347", "12348"]
		)[0])

	def test_directed_capture_accepts_a_plain_contract_without_calling_interpretation(self):
		with patch("messaging.chatbot.doctype.bot_settings.interpretation.interpret",
			side_effect=AssertionError("interpretation must not run")):
			values, missing, invalid = capture(
				[self.row()], {}, dict(text="12345"),
				accept_text=True,
			)
		self.assertEqual(values, {self.parameter.name: ["12345"]})
		self.assertEqual(missing, [])
		self.assertFalse(invalid)
		values, missing, invalid = capture(
			[self.row()], {}, dict(text="SC-12345 y 12346"),
			accept_text=True,
		)
		self.assertEqual(values[self.parameter.name], ["SC-12345", "12346"])
		self.assertFalse(invalid)

	def test_free_text_keeps_every_matching_number_and_a_singular_value_stays_ambiguous(self):
		text = "Quiero consultar el saldo del contrato 12345 porque debo 12000 lempiras."
		masked, occurrences = recognize(text, {self.parameter.name: self.parameter})
		self.assertEqual([item.value for item in occurrences], ["12345", "12000"])
		self.assertIn("[numero_de_contrato]", masked)
		multiple, ambiguous = captured_values(occurrences, {self.parameter.name: self.parameter}, {self.parameter.name})
		self.assertEqual(multiple[self.parameter.name], ["12345", "12000"])
		self.assertFalse(ambiguous)
		single, ambiguous = captured_values(occurrences, {self.parameter.name: self.parameter}, set())
		self.assertNotIn(self.parameter.name, single)
		self.assertTrue(ambiguous)
		context, rejected, _occurrences = capture_context(
			text, {self.parameter.name: self.parameter}, multiple={self.parameter.name},
		)
		self.assertEqual(context[self.parameter.name], ["12345", "12000"])
		self.assertFalse(rejected)

	def test_optional_contract_is_not_required_and_a_single_contract_is_captured(self):
		values, missing, invalid = capture(
			[self.row(required=0)], {}, dict(text=""),
			accept_text=True,
		)
		self.assertEqual(values, {})
		self.assertEqual(missing, [])
		self.assertFalse(invalid)
		sentence = "La tubería del contrato 12345 está dañada."
		_masked, occurrences = recognize(sentence, {self.parameter.name: self.parameter})
		captured, ambiguous = captured_values(
			occurrences, {self.parameter.name: self.parameter}, {self.parameter.name}
		)
		self.assertEqual(captured[self.parameter.name], ["12345"])
		self.assertFalse(ambiguous)
		empty, _ambiguous = captured_values(
			recognize("La tubería está dañada.", {self.parameter.name: self.parameter})[1],
			{self.parameter.name: self.parameter}, {self.parameter.name},
		)
		self.assertEqual(empty, {})


class TestRequestPhrases(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user('Administrator')
		from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings
		self.settings = configure_test_settings()
		self.settings.append('chatbot_languages', dict(language='en', enabled=1, is_default=0))
		self.settings.save()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def parameter(self, phrases=None):
		return frappe.get_doc(dict(doctype='Bot Parameter', name='phrase_' + frappe.generate_hash(length=8),
			label='Contract', parameter_type='Expression', expression='SC-#####', enabled=1,
			request_phrases=phrases if phrases is not None else [dict(language='es', request_phrase='Indique su contrato.')])).insert()

	def test_unique_enabled_languages_and_native_persistence(self):
		parameter = self.parameter()
		parameter.append('request_phrases', dict(language='en', request_phrase='Please provide your contract.'))
		parameter.save(); parameter.reload()
		self.assertEqual(len(parameter.request_phrases), 2)
		parameter.append('request_phrases', dict(language='en', request_phrase='Duplicate'))
		with self.assertRaisesRegex(frappe.ValidationError, 'at most one'): parameter.save()
		parameter.reload()
		parameter.set('request_phrases', [dict(language='fr', request_phrase='Foreign')])
		with self.assertRaisesRegex(frappe.ValidationError, 'enabled Chatbot Languages'): parameter.save()
		meta = frappe.get_meta('Bot Parameter Translation')
		self.assertEqual([f.fieldname for f in meta.fields], ['language', 'request_phrase'])
		self.assertTrue(meta.istable)
		self.assertEqual(meta.get_field('language').options, 'Language')

	def test_flow_uses_shared_phrase_and_default_fallback(self):
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_request_phrase
		from messaging.chatbot.doctype.bot_settings.language import presentation_language
		from messaging.conversation.doctype.chat_session.chat_session import prompt
		parameter = self.parameter()
		parameter.append('request_phrases', dict(language='en', request_phrase='Please provide your contract.'));parameter.save()
		row = frappe._dict(parameter=parameter.name, required=1, capture_order=1)
		with presentation_language('en'):
			self.assertIn('Please provide your contract.', prompt([row], 'Unused Flow input'))
		self.assertEqual(get_request_phrase(parameter, 'fr'), 'Indique su contrato.')
		parameter.request_phrases[1].request_phrase='';parameter.save()
		self.assertEqual(get_request_phrase(parameter.name,'en'),'Indique su contrato.')

	def test_missing_default_blocks_parameter_use(self):
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_request_phrase
		parameter = self.parameter([dict(language='en', request_phrase='English only')])
		with self.assertRaisesRegex(frappe.ValidationError, 'default language es'):
			get_request_phrase(parameter, 'en')

	def test_language_selector_searches_native_code_and_excludes_disabled_languages(self):
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import language_options
		self.assertIn('es', [row[0] for row in language_options('Language', 'es', 'name', 0, 20, {})])
		self.settings.chatbot_languages[1].enabled = 0;self.settings.save()
		self.assertNotIn('en', [row[0] for row in language_options('Language', '', 'name', 0, 20, {})])



class TestDeterministicCapture(unittest.TestCase):
	"""The common engine needs neither a configured site profile nor an AI provider."""

	def setUp(self):
		self.docs = {
			'contract': frappe._dict(name='contract', label='Contract', enabled=1, parameter_type='Expression', expression='SC-#####'),
			'count': frappe._dict(name='count', label='Count', enabled=1, parameter_type='Int', min_value=0, max_value=99),
			'date': frappe._dict(name='date', label='Date', enabled=1, parameter_type='Date'),
			'text': frappe._dict(name='text', label='Description', enabled=1, parameter_type='String'),
		}
		self.lookup = patch('frappe.get_doc', side_effect=lambda dt, name: self.docs[name])
		self.lookup.start(); self.addCleanup(self.lookup.stop)
		self.provider = patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('AI extraction forbidden'))
		self.provider.start(); self.addCleanup(self.provider.stop)

	def rows(self, *keys, **options):
		return [frappe._dict(parameter=key, required=1, capture_order=index, **options) for index, key in enumerate(keys, 1)]

	def test_distinguishable_candidates_are_captured_together_and_missing_is_ordered(self):
		rows = self.rows('text', 'contract', 'count', 'date')
		values, missing, invalid = capture(rows, {}, dict(text='Contrato SC-00125, cantidad 4, fecha 29/02/2024.'), accept_text=True)
		self.assertEqual(values, {'contract': 'SC-00125', 'count': 4, 'date': '2024-02-29'})
		self.assertEqual([row.parameter for row in missing], ['text'])
		self.assertFalse(invalid)
		values, missing, invalid = capture(rows, values, dict(text='Hay una fuga.'), accept_text=True, requested_parameter='text')
		self.assertEqual(values['text'], 'Hay una fuga.'); self.assertFalse(missing); self.assertFalse(invalid)

	def test_overlap_has_no_arbitrary_assignment_but_a_directed_answer_can_resolve_it(self):
		self.docs['other'] = frappe._dict(self.docs['contract'], name='other', label='Other contract')
		rows = self.rows('contract', 'other')
		values, missing, invalid = capture(rows, {}, dict(text='Mi contrato es SC-00125.'), accept_text=True)
		self.assertEqual(values, {}); self.assertEqual(len(missing), 2)
		values, missing, invalid = capture(rows, {}, dict(text='SC-00125'), accept_text=True, requested_parameter='contract')
		self.assertEqual(values, {'contract': 'SC-00125'}); self.assertEqual([r.parameter for r in missing], ['other'])

	def test_same_span_across_types_is_ambiguous(self):
		self.docs['contract'].expression = '#####'
		self.docs['count'].max_value = 99999
		values, missing, invalid = capture(self.rows('contract', 'count'), {}, dict(text='12345'), accept_text=True)
		self.assertEqual(values, {}); self.assertEqual(len(missing), 2)

	def test_multiple_values_cardinality_and_atomic_invalid_set(self):
		rows = self.rows('contract', allow_multiple_values=1, minimum_values=1, maximum_values=2)
		for text in ('SC-00125 y SC-00126 y SC-00127', 'SC-00125 y SC-1234'):
			with self.subTest(text=text):
				values, missing, invalid = capture(rows, {}, dict(text=text), accept_text=True, requested_parameter='contract')
				self.assertEqual(values, {}); self.assertTrue(missing); self.assertTrue(invalid)
		values, missing, invalid = capture(rows, {}, dict(text='SC-00125, SC-00126'), accept_text=True)
		self.assertEqual(values, {'contract': ['SC-00125', 'SC-00126']}); self.assertFalse(missing)

	def test_valid_values_survive_invalid_replacements_and_retries(self):
		rows = self.rows('contract', 'date')
		values, missing, invalid = capture(rows, {'contract': 'SC-00125'}, dict(parameters={'contract': 'bad'}, text='31/02/2024'),
			accept_text=True, requested_parameter='date')
		self.assertEqual(values, {'contract': 'SC-00125'}); self.assertTrue(invalid)
		values, missing, invalid = capture(rows, values, dict(text='29/02/2024'), accept_text=True, requested_parameter='date')
		self.assertEqual(values['date'], '2024-02-29'); self.assertFalse(missing)

	def test_initial_free_text_waits_for_a_question_and_optional_candidates_are_accepted(self):
		rows = self.rows('text', 'contract'); rows[1].required = 0
		values, missing, invalid = capture(rows, {}, dict(text='Una descripción libre.'), accept_text=True)
		self.assertEqual(values, {}); self.assertEqual([row.parameter for row in missing], ['text'])
		values, missing, invalid = capture(rows, {}, dict(text='Contrato SC-00125 con fuga.'), accept_text=True, requested_parameter='text')
		self.assertEqual(values, {'contract': 'SC-00125'}); self.assertEqual([row.parameter for row in missing], ['text'])


	def test_invalid_explicit_field_does_not_discard_other_safe_candidates(self):
		rows = self.rows('contract', 'date')
		values, missing, invalid = capture(rows, {}, dict(parameters={'contract': 'bad'}, text='29/02/2024'), accept_text=True)
		self.assertEqual(values, {'date': '2024-02-29'})
		self.assertEqual([row.parameter for row in missing], ['contract']); self.assertTrue(invalid)


class TestDecimalBooleanCapture(unittest.TestCase):
    def parameter(self, kind):
        return frappe._dict(name='synthetic_value', label='Value', parameter_type=kind, enabled=1)

    def test_directed_decimal_and_boolean_without_contextual_boolean_recognition(self):
        from decimal import Decimal
        from messaging.chatbot.doctype.bot_parameter.bot_parameter import capture_text, capture, recognize
        from messaging.chatbot.doctype.bot_settings.language import presentation_language
        settings = frappe._dict(chatbot_languages=[frappe._dict(language='es',enabled=1),frappe._dict(language='en',enabled=1)])
        boolean, decimal = self.parameter('Boolean'), self.parameter('Decimal')
        with patch('messaging.chatbot.doctype.bot_settings.language.get_bot_settings', return_value=settings):
            for text, expected in [('Sí', True),('si',True),('No',False),('Yes',True),('no',False)]:
                self.assertEqual(capture_text(boolean,text), (True, expected))
                self.assertEqual(recognize(text,{boolean.name:boolean})[1], [])
            for text in ('No sé','Tal vez','not sure','yes please','nobody','yesterday'):
                self.assertEqual(capture_text(boolean,text), (False,None))
            row=frappe._dict(parameter=boolean.name, required=1, capture_order=1)
            with patch('frappe.get_doc', return_value=boolean):
                self.assertEqual(capture([row],{},dict(text='No'),accept_text=True)[0], {})
                values, missing, invalid=capture([row],{},dict(text='No'),accept_text=True,requested_parameter=boolean.name)
                self.assertEqual(values,{boolean.name:False})
                self.assertEqual(missing,[])
                self.assertFalse(invalid)
            settings.chatbot_languages[0].enabled=0
            self.assertEqual(capture_text(boolean,'Sí'), (False,None))
            self.assertEqual(capture_text(boolean,'Yes'), (True,True))
        with presentation_language('es'):
            for text, expected in [('2.50','2.50'),('2,50','2.50'),('100','100'),('-15.75','-15.75'),('0.125','0.125')]:
                valid,value=capture_text(decimal,text)
                self.assertTrue(valid)
                self.assertIs(type(value),Decimal)
                self.assertEqual(value.as_tuple(),Decimal(expected).as_tuple())
            for text in ('1,250','1.234,56','1,234.56','NaN','Infinity','2.5.0'):
                self.assertEqual(capture_text(decimal,text),(False,None))
            self.assertEqual(capture_text(decimal,'2.50; 0.125',True), (True,[Decimal('2.50'),Decimal('0.125')]))
        with presentation_language('en'):
            self.assertEqual(capture_text(decimal,'2,50'),(False,None))

    def test_typed_values_and_state_roundtrip_preserve_false_and_precision(self):
        from decimal import Decimal
        from messaging.chatbot.doctype.bot_parameter.bot_parameter import validate_parameter_values
        from messaging.conversation.doctype.chat_session.chat_session import write_state,read_state
        decimal,boolean=self.parameter('Decimal'),self.parameter('Boolean')
        amount=Decimal('12345678901234567890.123456789012345678901')
        state=frappe._dict()
        state.set=lambda key,value: state.__setitem__(key,value)
        write_state(state,'dialogue_state',dict(values={'amount':[amount], 'accepted':False}))
        restored=read_state(state,'dialogue_state')['values']
        self.assertEqual(validate_parameter_values(decimal,restored['amount'],multiple=True),(True,[amount]))
        self.assertEqual(validate_parameter_values(boolean,restored['accepted']),(True,False))
        self.assertEqual(validate_parameter_values(decimal,True),(False,None))
        self.assertEqual(validate_parameter_values(boolean,'false'),(False,None))
