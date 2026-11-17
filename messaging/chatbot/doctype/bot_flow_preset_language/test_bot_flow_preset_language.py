"""Language rows cannot establish provenance through standalone document writes."""
import unittest
import frappe
from .bot_flow_preset_language import BotFlowPresetLanguage


class TestBotFlowPresetLanguage(unittest.TestCase):
    def test_ordinary_child_write_is_rejected(self):
        row = frappe.new_doc('Bot Flow Preset Language')
        self.assertIsInstance(row, BotFlowPresetLanguage)
        with self.assertRaises(frappe.PermissionError):
            row.before_validate()
