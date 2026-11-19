import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

class RootLayoutTest(unittest.TestCase):
    def test_manifest_and_unfiltered_workflow(self):
        tree = {"name": "PetSafe FreshFeed", "content_in_root": False, "homeassistant": "2026.10.0", "zip_release": False}
        release = {**tree, "zip_release": True, "filename": "petsafe_freshfeed.zip"}
        self.assertIn(json.loads((ROOT / "hacs.json").read_text()), (tree, release))
        workflow = (ROOT / ".github/workflows/validate.yml").read_text()
        self.assertRegex(workflow, r"(?m)^on:\n  push:\n  pull_request:\n  workflow_dispatch:\n(?=\S)")
