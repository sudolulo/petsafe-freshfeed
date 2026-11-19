"""The owned provenance implementation must not depend on the retired bridge."""
import ast
from pathlib import Path
import unittest

from custom_components.petsafe_freshfeed import owned_compat as compat


class OwnedProvenanceOwnershipTest(unittest.TestCase):
    def test_owned_provenance_does_not_import_legacy_compat(self):
        path = Path(__file__).resolve().parents[1] / "custom_components/petsafe_freshfeed/owned_compat.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("tuya_compat", imports, "owned provenance still imports legacy tuya_compat")

    def test_exact_constant_and_code_semantics(self):
        signature = compat._constant_signature
        for left, right in [(True, 1), (0.0, -0.0), (0j, complex(-0.0, 0.0)),
                            ((0.0,), (-0.0,)), (frozenset({0.0}), frozenset({-0.0})),
                            (slice(True), slice(1))]:
            self.assertNotEqual(signature(left), signature(right))
        with self.assertRaises(ValueError):
            signature([])
        code = (lambda: (lambda: 1)).__code__
        self.assertNotEqual(compat._code_signature(code),
                            compat._code_signature(code.replace(co_flags=code.co_flags ^ 1)))
        self.assertNotEqual(compat._code_signature(code),
                            compat._code_signature(code.replace(co_exceptiontable=b'\x00')))
        nested = next(item for item in code.co_consts if hasattr(item, 'co_code'))
        changed = nested.replace(co_consts=(None, 2))
        self.assertNotEqual(compat._code_signature(code), compat._code_signature(
            code.replace(co_consts=tuple(changed if item is nested else item for item in code.co_consts))))

    def test_source_class_and_method_matching(self):
        source = 'class Owner:\n def method(self): return 1\n def other(self): return 2\n'
        root = compile(source, '<synthetic>', 'exec')
        namespace = {}
        exec(root, namespace)
        owner = namespace['Owner']
        self.assertTrue(compat._source_code_matches(owner.method, owner, root))
        altered = compile(source.replace('return 1', 'return 3'), '<synthetic>', 'exec')
        self.assertFalse(compat._source_code_matches(owner.method, owner, altered))
        self.assertFalse(compat._source_code_matches(owner.method, owner, compile('class Other: pass', '<synthetic>', 'exec')))
        self.assertFalse(compat._source_code_matches(owner.method, owner, compile('class Owner: pass', '<synthetic>', 'exec')))
