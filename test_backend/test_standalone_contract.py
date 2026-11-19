"""Standalone distribution must not retain the borrowed runtime."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components/petsafe_freshfeed'
RETIRED = {'tuya_bridge', 'source_binding', 'tuya_compat', 'migration',
           'migration_state', 'migration_journal'}


class StandaloneContract(unittest.TestCase):
    def test_retired_modules_absent(self):
        for name in RETIRED:
            with self.subTest(name=name):
                self.assertFalse((COMPONENT / (name + '.py')).exists())
        self.assertFalse((COMPONENT / 'tuya_fingerprints.json').exists())

    def test_runtime_never_imports_retired_modules(self):
        for path in COMPONENT.rglob('*.py'):
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = (node.module or '').split('.') + [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [part for a in node.names for part in a.name.split('.')]
                else:
                    continue
                self.assertFalse(RETIRED.intersection(names), (path, node.lineno, names))

    def test_surviving_tests_have_no_retired_imports(self):
        for directory in ('test_backend', 'test_ha', 'tools'):
            for path in (ROOT / directory).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(), str(path))):
                    if isinstance(node, ast.ImportFrom):
                        module = node.module or ''
                        if not module.startswith('custom_components.petsafe_freshfeed'):
                            continue
                        names = module.split('.') + [a.name for a in node.names]
                    elif isinstance(node, ast.Import):
                        names = [part for a in node.names
                                 if a.name.startswith('custom_components.petsafe_freshfeed')
                                 for part in a.name.split('.')]
                    else:
                        continue
                    self.assertFalse(RETIRED.intersection(names), (path, node.lineno, names))

    def test_no_raw_discovery_or_journal_reader(self):
        text = (COMPONENT / 'hub.py').read_text()
        for retired in ('async_track_state_change_event', '_async_attach_bridge',
                        '_registry_changed', 'raw_entity_ids', 'migration_journal'):
            self.assertNotIn(retired, text)
        for path in COMPONENT.rglob('*.py'):
            self.assertNotIn('petsafe_freshfeed_migrations', path.read_text(), str(path))
        for name in ('petsafe_freshfeed_raw.yaml', 'petsafe_freshfeed_schema.json'):
            self.assertFalse((ROOT / 'config/tuya_local' / name).exists())

    def test_no_borrowed_transport_constructor(self):
        tree = ast.parse((COMPONENT / 'critical_transport.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'CriticalTransport':
                self.assertNotIn('async_create', {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
        self.assertNotIn('_LegacyBinding', {n.name for n in tree.body if isinstance(n, ast.ClassDef)})


if __name__ == '__main__':
    unittest.main()
