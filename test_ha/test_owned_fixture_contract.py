"""Surviving synthetic helpers must not carry borrowed-client adapters."""
import ast
from pathlib import Path


def test_neutral_helpers_have_no_borrowed_api():
    tree = ast.parse(Path('test_ha/owned_test_support.py').read_text())
    names = {node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    assert 'Api' not in names, 'borrowed Api must remain in legacy-only fixture provider'
    assert {'FakeSocket', 'Owner', '_frame', 'until'} <= names
