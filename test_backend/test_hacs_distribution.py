"""Public packaging contract: exact paths, immutable inputs, and fail-closed IO."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/build-hacs.py'
COMPONENT = 'custom_components/petsafe_freshfeed/'
RUNTIME = '''__init__.py binary_sensor.py config_flow.py const.py critical_transport.py entity.py event.py hub.py lifecycle.py observed.py quick_feed.py schedule_codec.py schedule_state.py sensor.py services.py source_binding.py sqlite_witness.py store_bootstrap.py tuya_bridge.py tuya_compat.py write_adapter.py manifest.json strings.json services.yaml tuya_fingerprints.json translations/en.json'''.split()
COMMON = {'LICENSE', 'NOTICE', 'README.md', 'hacs.json', '.github/workflows/validate.yml', 'assets/brand/logo.svg', 'assets/brand/readme.png'}
PROFILE_SRC = 'config/tuya_local/petsafe_freshfeed_raw.yaml'
PROFILE_DEST = 'petsafe-freshfeed/examples/tuya_local/petsafe_freshfeed_raw.yaml'
EXPECTED = {
    'petsafe-freshfeed': COMMON | {COMPONENT + n for n in RUNTIME} | {COMPONENT + 'brand/icon.png'} | {'examples/tuya_local/petsafe_freshfeed_raw.yaml'},
    'pet-feeder-card': COMMON | {'dist/pet-feeder-card.js', 'dist/freshfeed-model.js'},
}
spec = importlib.util.spec_from_file_location('build_hacs', SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def contents(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}


class DistributionTest(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / '.cache/petsafe'
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def build(self, output):
        return subprocess.run([sys.executable, str(SCRIPT), '--output', str(output)], capture_output=True, text=True)

    def good_build(self, name='export'):
        out = self.base / name
        result = self.build(out)
        self.assertEqual(result.returncode, 0, result.stderr)
        return out

    def test_exact_paths_hashes_and_immutable_reproducibility(self):
        sources = set(exporter.INTEGRATION.values()) | set(exporter.CARD.values())
        before = {s: (ROOT / s).read_bytes() for s in sources if (ROOT / s).exists()}
        a, b = self.good_build('a'), self.good_build('b')
        self.assertEqual(contents(a), contents(b))
        self.assertEqual(before, {s: (ROOT / s).read_bytes() for s in before})
        expected = {f'{tree}/{p}' for tree, paths in EXPECTED.items() for p in paths}
        files = contents(a)
        self.assertEqual(set(files), expected | {'SHA256SUMS'})
        self.assertEqual(files['SHA256SUMS'].decode(), ''.join(
            f'{hashlib.sha256(files[p]).hexdigest()}  {p}\n' for p in sorted(expected)))
        for tree in EXPECTED:
            self.assertNotIn(b'holden@', (a / tree / 'NOTICE').read_bytes())

    def test_raw_profile_exported_sanitized_and_byte_identical(self):
        out = self.good_build()
        exported = out / PROFILE_DEST
        self.assertTrue(exported.is_file(), 'exported profile missing from distribution')
        self.assertEqual(exported.read_bytes(), (ROOT / PROFILE_SRC).read_bytes())
        data = yaml.safe_load(exported.read_text())
        self.assertEqual(data['name'], 'PetSafe FreshFeed raw observation')
        text = exported.read_text()
        for secret in ('device_id:', 'ip:', 'key:', 'protocol:'):
            self.assertNotIn(secret, text)

    def test_integration_readme_documents_pinned_compatibility_and_profile(self):
        readme = (self.good_build() / 'petsafe-freshfeed/README.md').read_text()
        for required in (
            'https://github.com/make-all/tuya-local/archive/027351717d3f6b8bef30c47a018944b430f5661e.tar.gz',
            '2026.10.0',
            '1.20.0',
            'examples/tuya_local/petsafe_freshfeed_raw.yaml',
            '/config/custom_components/tuya_local/devices/petsafe_freshfeed_raw.yaml',
            'restart',
            'petsafe_freshfeed_raw',
            'overwrite',
        ):
            self.assertIn(required, readme, f'readme missing: {required}')

    def test_existing_output_and_stale_extras_are_never_overwritten(self):
        out = self.base / 'stale'
        (out / 'pet-feeder-card').mkdir(parents=True)
        extra = out / 'pet-feeder-card/private.txt'
        extra.write_text('do not alter')
        self.assertNotEqual(self.build(out).returncode, 0)
        self.assertEqual(extra.read_text(), 'do not alter')
        self.assertNotEqual(self.build(self.good_build()).returncode, 0)

    def test_output_symlinks_and_inside_checkout_rejected(self):
        real = self.base / 'real'
        real.mkdir()
        link = self.base / 'link'
        link.symlink_to(real, target_is_directory=True)
        for out in (link, link / 'child', ROOT / 'forbidden-export'):
            self.assertNotEqual(self.build(out).returncode, 0)
        self.assertEqual(list(real.iterdir()), [])

    def test_source_symlinks_rejected_and_unknown_files_excluded(self):
        fixture = self.base / 'source'
        fixture.mkdir()
        (fixture / 'private.txt').write_text('private')
        (fixture / 'linked.py').symlink_to(fixture / 'private.txt')
        (fixture / 'linked_dir').symlink_to(fixture, target_is_directory=True)
        with patch.object(exporter, 'ROOT', fixture):
            for name in ('linked.py', 'linked_dir/private.txt'):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    exporter.source_bytes(name)
            with self.assertRaises(ValueError):
                exporter.source_bytes('../outside')
        approved = set(exporter.INTEGRATION.values()) | set(exporter.CARD.values())
        for forbidden in ('private.txt', 'deploy/cards.json', 'docs/whisker-reference.png', '.git/config', 'custom_components/petsafe_freshfeed/unknown.py'):
            self.assertNotIn(forbidden, approved)

    def test_raw_profile_source_and_test_inputs(self):
        self.assertTrue((ROOT / PROFILE_SRC).is_file())
        self.assertEqual(set(exporter.INTEGRATION.values()),
                         {'LICENSE', 'distribution/NOTICE-public', 'assets/brand/logo.svg', 'assets/brand/readme.png', PROFILE_SRC, 'assets/brand/icon.png', 'distribution/README-integration.md', 'distribution/hacs-integration.json', 'distribution/workflow-integration.yml'}
                         | {COMPONENT + n for n in RUNTIME})

    def test_manifests_png_and_import_closure(self):
        out = self.good_build()
        integration = out / 'petsafe-freshfeed'
        card = out / 'pet-feeder-card'
        manifest = json.loads((integration / COMPONENT / 'manifest.json').read_text())
        self.assertEqual(manifest['domain'], 'petsafe_freshfeed')
        self.assertEqual(manifest['version'], '0.2.0')
        self.assertEqual(manifest['codeowners'], ['@sudolulo'])
        self.assertEqual(manifest['issue_tracker'], 'https://github.com/sudolulo/petsafe-freshfeed/issues')
        self.assertEqual(manifest['after_dependencies'], ['tuya_local'])
        hc = json.loads((card / 'hacs.json').read_text())
        self.assertEqual(hc['filename'], 'pet-feeder-card.js')
        self.assertFalse(hc.get('zip_release', False))
        self.assertFalse(hc.get('content_in_root', False))
        self.assertNotIn('filename', json.loads((integration / 'hacs.json').read_text()))
        for name, size in ((COMPONENT + 'brand/icon.png', (256, 256)), ('assets/brand/readme.png', (512, 512))):
            png = (integration / name).read_bytes()
            self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', png[16:24]), size)
            self.assertEqual(png[25], 6)  # RGBA, transparent background
        for js in (card / 'dist').glob('*.js'):
            for dependency in re.findall(r'(?:from\s*|import\s*)[\'"]([^\'"]+)[\'"]', js.read_text()):
                self.assertTrue(dependency.startswith('./'), dependency)
                self.assertTrue((js.parent / dependency.split('?')[0]).is_file(), dependency)


if __name__ == '__main__':
    unittest.main()
