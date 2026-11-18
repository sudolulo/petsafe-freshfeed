"""Public presentation and refusal translation contracts."""
import ast
import json
from pathlib import Path
import unittest

from custom_components.petsafe_freshfeed.const import EVENT_KINDS

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'petsafe_freshfeed'
REASONS = set('invalid_request confirmation_required invalid_operation admin_required unknown_entry source_unavailable recovery_rejected stopped storage_fault integrity_fault busy recovery_in_progress intent_uncertain source_incompatible schedule_unknown stale_schedule quick_feed_active defrost_active feeding_active status_unavailable cancel_not_pending empty_tray already_refilled controls_disabled source_not_ready schedule_conflict'.split())

class TranslationContract(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / 'strings.json').read_text())

    def test_exception_keys_complete(self):
        self.assertTrue(REASONS | {'request_refused', 'setting_refused', 'setting_uncertain', 'invalid_setting'} <= self.data.get('exceptions', {}).keys())
        self.assertEqual((ROOT / 'strings.json').read_bytes(), (ROOT / 'translations/en.json').read_bytes())

    def test_exception_copy_contract(self):
        generic = 'Check the feeder connection, controls and write status before trying again.'
        expected = dict.fromkeys(REASONS, generic)
        expected.update(dict.fromkeys({'stale_schedule', 'schedule_conflict'},
                                      'Reload the schedule before saving again.'))
        expected.update(dict.fromkeys({'storage_fault', 'integrity_fault'},
                                      'Check storage and write status; do not delete retained intent.'))
        expected.update(dict.fromkeys({'intent_uncertain', 'recovery_in_progress', 'recovery_rejected'},
                                      'Inspect the feeder and use the existing recovery controls.'))
        expected.update({
            'admin_required': 'Use an administrator account.',
            'request_refused': 'The request could not be confirmed. Check device and write status before trying again.',
            'setting_refused': 'Setting refused: {reason}',
            'setting_uncertain': 'The setting could not be confirmed. Inspect the feeder and write status.',
            'invalid_setting': 'The setting value is invalid.',
        })
        for key, message in expected.items():
            with self.subTest(key=key):
                self.assertEqual(self.data['exceptions'][key]['message'], message)

    def test_public_refusal_inventory(self):
        """Inventory admission literals, excluding private transport receipts."""
        services = ast.parse((ROOT / 'services.py').read_text())
        adapter = ast.parse((ROOT / 'write_adapter.py').read_text())
        emitted = set()
        for tree, names in ((services, {'operation_reason', 'unsafe_reason'}),
                            (adapter, {'_request_reason', '_gate_reason', '_revision_reason'})):
            for function in ast.walk(tree):
                if isinstance(function, ast.FunctionDef) and function.name in names:
                    for node in ast.walk(function):
                        if isinstance(node, ast.Return) and node.value is not None:
                            emitted.update(value.value for value in ast.walk(node.value)
                                           if isinstance(value, ast.Constant) and isinstance(value.value, str))
        for node in ast.walk(services):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'service_error' and node.args
                    and isinstance(node.args[0], ast.Constant)):
                emitted.add(node.args[0].value)
        # The sole conditional return includes its comparison literal as well.
        emitted.discard('pending')
        compatibility = {'controls_disabled', 'source_not_ready', 'schedule_conflict'}
        self.assertEqual(emitted | compatibility, REASONS)
        self.assertTrue(emitted <= self.data['exceptions'].keys())

    def test_event_translation_keys_cover_all_kinds(self):
        states = self.data['entity']['event']['feeding']['state_attributes']['event_type']['state']
        self.assertEqual(set(states), set(EVENT_KINDS))

    def test_estimate_labels(self):
        sensors = self.data['entity']['sensor']
        for key, name in {'meals_left': 'Estimated meals remaining', 'food_level': 'Estimated food level', 'firmware_a': 'Firmware version', 'firmware_b': 'Hardware version', 'quick_feed_state': 'Quick feed state'}.items():
            self.assertEqual(sensors[key]['name'], name)
