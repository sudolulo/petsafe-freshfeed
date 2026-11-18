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
ACCOUNT = '''account/__init__.py account/boundary.py account/client.py account/profile.py account/exchange.py account/protocol_contract.json lan_locate.py source_lookup.py'''.split()
# Standalone onboarding no longer drives Tuya Local: its setup assist and the
# component profile copy it installed were removed from the source tree and
# are not shipped (the manual-install profile export stays for legacy users).
NOT_SHIPPED = '''tuya_local_assist.py profile/petsafe_freshfeed_raw.yaml'''.split()
# Neutral source contract and the PetSafe-owned TinyTuya source (standalone).
OWNED_SOURCE = '''local_source.py owned_source.py owned_compat.py owned_fingerprints.json'''.split()
# Durable migration journal and pure recovery reducer.
MIGRATION = '''migration_state.py migration_journal.py migration.py'''.split()
# Owned-backend setting entity platforms (stainless inserts, meal counter, brightness).
SETTINGS = '''switch.py number.py owned_setting.py'''.split()
RUNTIME = RUNTIME + ACCOUNT + OWNED_SOURCE + MIGRATION + SETTINGS + ['diagnostics.py', 'repairs.py', 'device_trigger.py']
APP_PROFILE = COMPONENT + 'account/app_profile.json'
APP_PROFILE_DEST = 'petsafe-freshfeed/' + APP_PROFILE
PROFILE_FILE = 'public-apk-profile.private.json'
ENVELOPE_FILE = 'public-apk-envelope.private.json'
PROFILE_KEYS = ('app_id', 'app_secret', 'cert_sha256_hex', 'app_key', 'package')
ENVELOPE_KEYS = ('app_version', 'ttid', 'sdk_version', 'device_core_version')
# Synthetic FAKE values only; each carries a sentinel that must never be echoed.
SENTINEL = 'SYNTHFAKE'
FAKE_PROFILE = {
    'app_id': 'SYNTHFAKEappid0001',
    'app_secret': 'SYNTHFAKEsecret0002',
    'cert_sha256_hex': 'abcdef0123456789' * 4,
    'app_key': 'SYNTHFAKEappkey0003',
    'package': 'com.example.SYNTHFAKE.pkg',
}
FAKE_ENVELOPE = {
    'app_version': 'SYNTHFAKE-1.2.3',
    'ttid': 'SYNTHFAKEttid0004',
    'sdk_version': 'SYNTHFAKE-sdk-5',
    'device_core_version': 'SYNTHFAKE-core-6',
}
CERT_SENTINEL = FAKE_PROFILE['cert_sha256_hex']
COMMON = {'LICENSE', 'NOTICE', 'README.md', 'hacs.json', '.github/workflows/validate.yml', 'assets/brand/logo.svg', 'assets/brand/readme.png'}
PROFILE_SRC = 'config/tuya_local/petsafe_freshfeed_raw.yaml'
PROFILE_DEST = 'petsafe-freshfeed/examples/tuya_local/petsafe_freshfeed_raw.yaml'
BLUEPRINTS = {f'blueprints/automation/petsafe_freshfeed/{name}.yaml' for name in (
    'low_estimated_meals', 'feeder_attention', 'refill_cleaning_reminder')}
EXPECTED = {
    'petsafe-freshfeed': COMMON | {COMPONENT + n for n in RUNTIME} | {COMPONENT + 'brand/icon.png'} | {'examples/tuya_local/petsafe_freshfeed_raw.yaml'} | BLUEPRINTS,
    'pet-feeder-card': COMMON | {'dist/pet-feeder-card.js', 'dist/freshfeed-model.js', 'dist/pet-feeder-card-editor.js', 'dist/freshfeed-editor-model.js'},
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

    def build(self, output, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), '--output', str(output), *map(str, extra)],
                              capture_output=True, text=True)

    def profile_dir(self, profile=None, envelope=None, *, raw_profile=None, raw_envelope=None, mode=0o600):
        # Synthetic directory in the system temp dir; never a real private directory.
        temp = tempfile.TemporaryDirectory(prefix='petsafe-synth-profile-')
        self.addCleanup(temp.cleanup)
        d = Path(temp.name)
        for name, doc, raw in ((PROFILE_FILE, FAKE_PROFILE if profile is None else profile, raw_profile),
                               (ENVELOPE_FILE, FAKE_ENVELOPE if envelope is None else envelope, raw_envelope)):
            path = d / name
            path.write_bytes(raw if raw is not None else json.dumps(doc).encode())
            path.chmod(mode)
        return d

    def assert_no_values(self, result):
        for stream in (result.stdout, result.stderr):
            self.assertNotIn(SENTINEL, stream)
            self.assertNotIn(CERT_SENTINEL, stream)
            for value in (*FAKE_PROFILE.values(), *FAKE_ENVELOPE.values()):
                self.assertNotIn(value, stream)

    def test_blueprint_exports_are_byte_identical_root_files(self):
        out = self.good_build()
        for path in BLUEPRINTS:
            self.assertEqual((out / 'petsafe-freshfeed' / path).read_bytes(),
                             (ROOT / path).read_bytes())
            self.assertEqual(exporter.INTEGRATION[path], path)
            self.assertNotIn(path, exporter.RUNTIME)

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

    def test_card_sibling_module_imports_resolve(self):
        import re
        out = self.good_build() / 'pet-feeder-card' / 'dist'
        for module in out.glob('*.js'):
            for sibling in re.findall(r"['\"](\./[^'\"]+\.js)(?:\?[^'\"]*)?['\"]", module.read_text()):
                self.assertTrue((module.parent / sibling).is_file(), f'{module.name}: {sibling}')

    def test_notice_attributes_tuya_mobile_with_full_mit_text(self):
        out = self.good_build()
        for tree in EXPECTED:
            notice = (out / tree / 'NOTICE').read_text()
            for required in ('abovecolin/tuya-mobile',
                             'ade70eff248764757aa177acf9bdd10440ae7394',
                             'Copyright (c) 2026 tuya-mobile contributors',
                             'Permission is hereby granted'):
                self.assertIn(required, notice, f'{tree} NOTICE missing: {required}')
            self.assertNotIn('holden@', notice)
        root_notice = (ROOT / 'NOTICE').read_text()
        for required in ('abovecolin/tuya-mobile', 'ade70eff248764757aa177acf9bdd10440ae7394',
                         'Permission is hereby granted'):
            self.assertIn(required, root_notice, f'root NOTICE missing: {required}')

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

    def test_release_050_040_metadata(self):
        """Release versions retain the pinned compatibility contract."""
        out = self.good_build()
        integration = out / 'petsafe-freshfeed'
        hacs = json.loads((integration / 'hacs.json').read_text())
        self.assertEqual(hacs['homeassistant'], '2026.10.0')
        self.assertEqual(hacs['name'], 'PetSafe FreshFeed')
        self.assertFalse(hacs.get('content_in_root', False))
        manifest = json.loads((integration / COMPONENT / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], '0.5.0')
        self.assertEqual(manifest['after_dependencies'], ['tuya_local'])
        self.assertNotIn('dependencies', manifest)
        self.assertEqual(manifest['requirements'], ['tinytuya==1.20.0'])
        package = json.loads((ROOT / 'package.json').read_text())
        self.assertEqual(package['version'], '0.4.0')
        lock = json.loads((ROOT / 'package-lock.json').read_text())
        self.assertEqual(lock['version'], '0.4.0')
        self.assertEqual(lock['packages']['']['version'], '0.4.0')
        self.assertIn('**0.4.0**', (ROOT / 'distribution/README-card.md').read_text())
        changelog = (ROOT / 'CHANGELOG.md').read_text()
        self.assertIn('## Integration [0.5.0] - 2026-11-18', changelog)
        self.assertIn('## Card [0.4.0] - 2026-11-18', changelog)
        self.assertIn('## Integration [0.4.0] - 2026-11-18', changelog)
        self.assertLess(changelog.index('## Integration [0.4.0]'),
                        changelog.index('## Integration [0.3.0]'))

    def test_early_compatibility_and_safety_contract(self):
        for path in ('README.md', 'distribution/README-card.md', 'distribution/README-integration.md'):
            text = (ROOT / path).read_text()
            early = text[:2500]
            for value in ('0.5.0', '0.4.0', '2026.10.0', '2026.10.x', '1.20.0', '3.5'):
                self.assertIn(value, early, f'{path}: {value}')
            for value in ('administrator', 'blueprint', 'offline', 'advisory'):
                self.assertIn(value, text.lower(), f'{path}: {value}')

    def test_release_documentation_examples_and_install_closure(self):
        readme = (ROOT / 'README.md').read_text()
        deploy = readme.split('## Deploy', 1)[1].split('### FreshFeed', 1)[0]
        for module in ('pet-feeder-card.js', 'freshfeed-model.js',
                       'pet-feeder-card-editor.js', 'freshfeed-editor-model.js'):
            self.assertIn(f'scp card/{module}', deploy)
        spec_text = (ROOT / 'docs/card-spec.md').read_text()
        self.assertNotIn('No visual editor.', spec_text)
        self.assertNotIn('One deployable ES module:', spec_text)
        for phrase in ('0.4.0', 'getConfigElement', 'advisory', 'Estimated', 'aria-modal'):
            self.assertIn(phrase, spec_text)
        text = (ROOT / 'distribution/README-card.md').read_text()
        examples = [yaml.safe_load(block) for block in re.findall(r'```yaml\n(.*?)```', text, re.S)]
        semantic = [example for example in examples if 'freshfeed' in example]
        self.assertEqual(len(semantic), 1)
        config = semantic[0]
        self.assertEqual(set(config['freshfeed']), {'entry_id', 'schedule_entity',
                         'write_status_entity', 'quick_feed_entity', 'history_entity'})
        self.assertTrue(config['entities'])
        self.assertFalse(config.get('actions'))
        config['freshfeed']['entry_id'] = 'synthetic_release_entry_42'
        result = subprocess.run(['node', '--input-type=module', '-e',
            "import {validateConfig} from './card/pet-feeder-card.js'; "
            "validateConfig(JSON.parse(process.argv[1]));", json.dumps(config)],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_blueprint_documentation_has_full_source_and_import_urls(self):
        from urllib.parse import quote
        for path in ('README.md', 'distribution/README-card.md', 'distribution/README-integration.md'):
            text = (ROOT / path).read_text()
            for blueprint in BLUEPRINTS:
                raw = 'https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/' + blueprint
                self.assertIn(raw, text, path)
                self.assertIn('https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=' + quote(raw, safe=''), text, path)
            for phrase in ('does not automatically install', 'unsaved increments', 'administrator', 'without backfill'):
                self.assertIn(phrase, text, path)

    def test_readmes_document_standalone_and_migration(self):
        built = (self.good_build() / 'petsafe-freshfeed/README.md').read_text()
        root = (ROOT / 'README.md').read_text()
        for name, text in (('distribution', built), ('root', root)):
            readme = ' '.join(text.split())  # wrapped prose: compare whitespace-normalized
            for required in (
                '0.4.0',
                'tinytuya==1.20.0',
                'Tuya Local is not required',
                'owned_source_incompatible',
                '2026.10.0',
                '2026.10.x',
                'Legacy entries',
                'raw entities',
                'restart',
                'backup',
                'local key',
                'Connect directly (migrate away from Tuya Local)',
                'Continue the migration',
                'Cancel the migration',
                'forward-only',
                'Remove the retired Tuya Local entry',
                'not uninstalled',
                'protocol 3.5',
            ):
                self.assertIn(required, readme, f'{name} readme missing: {required}')
            self.assertNotIn('Tuya Local 2026.10.0 remains a required separately installed dependency', readme)

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
                         | {COMPONENT + n for n in RUNTIME} | BLUEPRINTS)

    def test_manifests_png_and_import_closure(self):
        out = self.good_build()
        integration = out / 'petsafe-freshfeed'
        card = out / 'pet-feeder-card'
        manifest = json.loads((integration / COMPONENT / 'manifest.json').read_text())
        self.assertEqual(manifest['domain'], 'petsafe_freshfeed')
        self.assertEqual(manifest['version'], '0.5.0')
        self.assertNotIn('tuya_local', manifest.get('dependencies', []))
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

    def test_runtime_python_import_closure(self):
        """Every intra-package import of a shipped module is itself shipped."""
        import ast
        shipped = set(exporter.RUNTIME)
        for name in shipped:
            if not name.endswith('.py'):
                continue
            path = ROOT / COMPONENT / name
            package = Path(name).parent.parts
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if not isinstance(node, ast.ImportFrom) or node.level == 0:
                    continue
                base = list(package[:len(package) - (node.level - 1)] if node.level > 1 else package)
                parts = base + (node.module.split('.') if node.module else [])
                targets = ['/'.join(parts) + '.py', '/'.join(parts + ['__init__.py'])]
                if node.module is None:
                    targets = ['/'.join(parts + [alias.name]) + '.py' for alias in node.names]
                self.assertTrue(any(target.lstrip('/') in shipped for target in targets), (name, targets))

    def test_owned_source_runtime_and_requirement(self):
        for name in OWNED_SOURCE:
            self.assertIn(name, exporter.RUNTIME)
        manifest = json.loads((ROOT / COMPONENT / 'manifest.json').read_text())
        self.assertEqual(manifest['requirements'], ['tinytuya==1.20.0'])
        files = contents(self.good_build())
        for name in OWNED_SOURCE:
            self.assertEqual(files['petsafe-freshfeed/' + COMPONENT + name], (ROOT / COMPONENT / name).read_bytes(), name)

    def test_runtime_includes_account_modules_and_profile_export(self):
        self.assertEqual(exporter.RUNTIME, RUNTIME)
        for name in ACCOUNT:
            self.assertIn(name, exporter.RUNTIME)
        self.assertNotIn('account/app_profile.json', exporter.RUNTIME)
        files = contents(self.good_build())
        for name in ACCOUNT:
            self.assertEqual(files['petsafe-freshfeed/' + COMPONENT + name], (ROOT / COMPONENT / name).read_bytes(), name)
        self.assertIn(PROFILE_DEST, files)  # manual-install export kept

    def test_tuya_local_assist_not_shipped(self):
        for name in NOT_SHIPPED:
            self.assertNotIn(name, exporter.RUNTIME)
        files = contents(self.good_build())
        for name in NOT_SHIPPED:
            self.assertNotIn('petsafe-freshfeed/' + COMPONENT + name, files)
        self.assertNotIn('tuya_local_assist', (ROOT / COMPONENT / 'config_flow.py').read_text())
        for name in NOT_SHIPPED:
            self.assertFalse((ROOT / COMPONENT / name).exists(), name)
        self.assertFalse((ROOT / 'test_ha/test_tuya_local_assist.py').exists())
        self.assertTrue((ROOT / PROFILE_SRC).is_file())
        self.assertEqual(files[PROFILE_DEST], (ROOT / PROFILE_SRC).read_bytes())

    def test_build_bundles_profile_from_dir(self):
        out = self.base / 'with-profile'
        result = self.build(out, '--profile-dir', self.profile_dir())
        self.assertEqual(result.returncode, 0, result.stderr)
        written = out / APP_PROFILE_DEST
        self.assertTrue(written.is_file())
        document = json.loads(written.read_text())
        self.assertEqual(document, {**FAKE_PROFILE, **FAKE_ENVELOPE})
        self.assertEqual(list(document), list(PROFILE_KEYS + ENVELOPE_KEYS))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from custom_components.petsafe_freshfeed.account import profile as account_profile
        loaded = account_profile.load_app_profile(path=written)
        self.assertIs(type(loaded), account_profile.AppProfile)
        self.assertEqual(loaded._mapping(), {**FAKE_PROFILE, **FAKE_ENVELOPE})
        for key in PROFILE_KEYS + ENVELOPE_KEYS:
            self.assertIn(key, result.stdout)
        self.assertIn(str(written), result.stdout)
        self.assertNotIn('account sign-in disabled', result.stdout)
        self.assertFalse((ROOT / APP_PROFILE).exists())
        # The release flag with a profile directory changes nothing else.
        release = self.base / 'release'
        result = self.build(release, '--release', '--profile-dir', self.profile_dir())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(contents(release), contents(out))

    def test_build_without_profile_dir_omits_profile(self):
        out = self.base / 'no-profile'
        result = self.build(out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ['account sign-in disabled: no app profile'])
        self.assertFalse((out / APP_PROFILE_DEST).exists())
        self.assertNotIn(APP_PROFILE_DEST, (out / 'SHA256SUMS').read_text())

    def test_release_requires_profile_dir(self):
        out = self.base / 'release-no-profile'
        result = self.build(out, '--release')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--profile-dir', result.stderr)
        self.assertFalse(out.exists())

    def test_build_output_never_echoes_values(self):
        ok = self.build(self.base / 'ok', '--profile-dir', self.profile_dir())
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assert_no_values(ok)
        cases = []
        for key in PROFILE_KEYS + ENVELOPE_KEYS:
            in_profile = key in PROFILE_KEYS
            base = dict(FAKE_PROFILE if in_profile else FAKE_ENVELOPE)
            for label, value in (('empty', ''), ('type', 12345), ('long', SENTINEL + 'x' * 300),
                                 ('control', SENTINEL + '\n\x01'), ('nonascii', SENTINEL + '\u00e9')):
                doc = {**base, key: value}
                cases.append((f'{key}-{label}', key, {'profile': doc} if in_profile else {'envelope': doc}))
            missing = {k: v for k, v in base.items() if k != key}
            cases.append((f'{key}-missing', key, {'profile': missing} if in_profile else {'envelope': missing}))
        cases.append(('cert-uppercase', 'cert_sha256_hex', {'profile': {**FAKE_PROFILE, 'cert_sha256_hex': CERT_SENTINEL.upper()}}))
        cases.append(('cert-short', 'cert_sha256_hex', {'profile': {**FAKE_PROFILE, 'cert_sha256_hex': CERT_SENTINEL[:62]}}))
        cases.append(('extra-key', None, {'profile': {**FAKE_PROFILE, SENTINEL + 'extra': SENTINEL}}))
        cases.append(('envelope-key-in-profile', None, {'profile': {**FAKE_PROFILE, 'ttid': FAKE_ENVELOPE['ttid']}}))
        dup = json.dumps(FAKE_PROFILE)[:-1] + ', "app_id": "' + SENTINEL + 'dup"}'
        cases.append(('duplicate', None, {'raw_profile': dup.encode()}))
        nan = json.dumps(FAKE_ENVELOPE)[:-1] + ', "ttid": NaN}'
        cases.append(('nan', None, {'raw_envelope': nan.encode()}))
        cases.append(('not-object', None, {'raw_profile': json.dumps(list(FAKE_PROFILE.values())).encode()}))
        cases.append(('bad-json', None, {'raw_profile': (SENTINEL + ' {').encode()}))
        cases.append(('bad-utf8', None, {'raw_profile': b'{"app_id": "' + SENTINEL.encode() + b'\xff"}'}))
        cases.append(('oversize', None, {'raw_profile': json.dumps({**FAKE_PROFILE, 'app_id': SENTINEL * 1000}).encode()}))
        cases.append(('group-readable', None, {'mode': 0o640}))
        cases.append(('world-readable', None, {'mode': 0o604}))
        for label, key, kwargs in cases:
            with self.subTest(label):
                out = self.base / f'bad-{label}'
                result = self.build(out, '--profile-dir', self.profile_dir(**kwargs))
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_values(result)
                self.assertTrue(result.stderr.startswith('profile error: '), result.stderr)
                if key is not None:
                    self.assertIn(f'key {key}', result.stderr)
                self.assertFalse(out.exists(), 'no output on a schema error')
        # A missing file, a symlinked file and a symlinked directory are refused too.
        d = self.profile_dir()
        (d / ENVELOPE_FILE).unlink()
        missing = self.build(self.base / 'bad-missing-file', '--profile-dir', d)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(ENVELOPE_FILE, missing.stderr)
        self.assert_no_values(missing)
        d = self.profile_dir()
        real = d / 'real.json'
        (d / PROFILE_FILE).rename(real)
        (d / PROFILE_FILE).symlink_to(real)
        linked = self.build(self.base / 'bad-symlink', '--profile-dir', d)
        self.assertNotEqual(linked.returncode, 0)
        self.assert_no_values(linked)
        link_dir = self.base / 'linked-dir'
        link_dir.symlink_to(self.profile_dir(), target_is_directory=True)
        linked = self.build(self.base / 'bad-symlink-dir', '--profile-dir', link_dir)
        self.assertNotEqual(linked.returncode, 0)
        self.assert_no_values(linked)
        self.assertFalse((self.base / 'bad-symlink-dir').exists())

    def test_profile_file_in_sha256sums(self):
        out = self.base / 'sums'
        result = self.build(out, '--profile-dir', self.profile_dir())
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {f'{tree}/{p}' for tree, paths in EXPECTED.items() for p in paths} | {APP_PROFILE_DEST}
        files = contents(out)
        self.assertEqual(set(files), expected | {'SHA256SUMS'})
        self.assertEqual(files['SHA256SUMS'].decode(), ''.join(
            f'{hashlib.sha256(files[p]).hexdigest()}  {p}\n' for p in sorted(expected)))

    def test_repo_tree_never_contains_app_profile(self):
        self.assertFalse((ROOT / APP_PROFILE).exists())
        self.assertEqual(list(ROOT.glob('custom_components/**/app_profile.json')), [])
        self.assertIn(APP_PROFILE, (ROOT / '.gitignore').read_text().splitlines())
        self.assertNotIn(APP_PROFILE, set(exporter.INTEGRATION.values()))
        tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--', '*app_profile.json', '*.private.json'],
                                 capture_output=True, text=True)
        if tracked.returncode == 0:
            self.assertEqual(tracked.stdout, '')
        ignored = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', APP_PROFILE])
        if ignored.returncode in (0, 1):
            self.assertEqual(ignored.returncode, 0, 'app_profile.json must be git-ignored')


if __name__ == '__main__':
    unittest.main()
