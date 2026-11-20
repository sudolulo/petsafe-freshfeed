import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

class RootLayoutTest(unittest.TestCase):
    def assert_manifest(self, manifest):
        tree = {"name": "PetSafe FreshFeed", "content_in_root": False, "homeassistant": "2026.10.0", "zip_release": False}
        release = {**tree, "zip_release": True, "filename": "petsafe_freshfeed.zip"}
        self.assertIn(set(manifest), (set(tree), set(release)))
        for key, value in manifest.items():
            self.assertIs(type(value), bool if key in ("content_in_root", "zip_release") else str)
        self.assertIn(manifest, (tree, release))

    def test_numeric_booleans_rejected(self):
        for key in ("content_in_root", "zip_release"):
            for value in (0, 1):
                with self.subTest(key=key, value=value), self.assertRaises(AssertionError):
                    self.assert_manifest({"name": "PetSafe FreshFeed", "content_in_root": False, "homeassistant": "2026.10.0", "zip_release": False, key: value})

    def test_only_exact_tree_and_release_manifests_accepted(self):
        tree = {"name": "PetSafe FreshFeed", "content_in_root": False,
                "homeassistant": "2026.10.0", "zip_release": False}
        release = {**tree, "zip_release": True, "filename": "petsafe_freshfeed.zip"}
        for manifest in (tree, release):
            self.assert_manifest(manifest)
        for manifest in ({**tree, "extra": "no"}, {**release, "filename": "wrong.zip"},
                         {**tree, "filename": "petsafe_freshfeed.zip"},
                         {**tree, "zip_release": True}, {**tree, "name": 1}):
            with self.subTest(manifest=manifest), self.assertRaises(AssertionError):
                self.assert_manifest(manifest)

    def test_manifest_and_unfiltered_workflow(self):
        self.assert_manifest(json.loads((ROOT / "hacs.json").read_text()))
        workflow = (ROOT / ".github/workflows/validate.yml").read_text()
        self.assertRegex(workflow, r"(?m)^on:\n  push:\n  pull_request:\n  workflow_dispatch:\n(?=\S)")


class ManifestOrderTest(unittest.TestCase):
    def test_manifest_keys_follow_hassfest_order(self):
        # hassfest requires domain, name, then the remaining keys alphabetically.
        manifest = json.loads((ROOT / 'custom_components/petsafe_freshfeed/manifest.json').read_text())
        keys = list(manifest)
        self.assertEqual(keys[:2], ['domain', 'name'])
        self.assertEqual(keys[2:], sorted(keys[2:]))
