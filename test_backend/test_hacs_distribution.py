"""Public packaging contract: exact paths, immutable inputs, and fail-closed IO."""
import builtins
import contextlib
import io
import os
import zipfile
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
SCRIPT = ROOT / 'tools/build-release.py'
COMPONENT = 'custom_components/petsafe_freshfeed/'
RUNTIME = '''__init__.py cloud_read.py binary_sensor.py config_flow.py const.py critical_transport.py entity.py event.py hub.py lifecycle.py observed.py quick_feed.py schedule_codec.py schedule_state.py sensor.py services.py sqlite_witness.py store_bootstrap.py write_adapter.py manifest.json strings.json services.yaml translations/en.json'''.split()
ACCOUNT = '''account/__init__.py account/boundary.py account/client.py account/profile.py account/exchange.py account/protocol_contract.json lan_locate.py source_lookup.py'''.split()
# Retired setup assist and profile are never shipped.
NOT_SHIPPED = '''tuya_local_assist.py profile/petsafe_freshfeed_raw.yaml'''.split()
# Neutral source contract and the PetSafe-owned TinyTuya source (standalone).
OWNED_SOURCE = '''local_source.py owned_source.py owned_compat.py owned_fingerprints.json'''.split()
# Owned-backend setting entity platforms (stainless inserts, meal counter, brightness).
SETTINGS = '''switch.py number.py owned_setting.py'''.split()
RUNTIME = RUNTIME + ACCOUNT + OWNED_SOURCE + SETTINGS + ['diagnostics.py', 'repairs.py', 'device_trigger.py']
APP_PROFILE = COMPONENT + 'account/app_profile.json'
APP_PROFILE_DEST = 'account/app_profile.json'
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
EXPECTED = set(RUNTIME) | {'brand/icon.png'}
spec = importlib.util.spec_from_file_location('build_hacs', SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def contents(path):
    with zipfile.ZipFile(path / 'petsafe_freshfeed.zip') as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def artifact_contents(path):
    return {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}


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
        temp = tempfile.TemporaryDirectory(prefix='petsafe-synth-profile-', dir=self.base)
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
        for path in BLUEPRINTS:
            self.assertTrue((ROOT / path).is_file())
            self.assertNotIn(path, exporter.RUNTIME)

    def good_build(self, name='export'):
        out = self.base / name
        result = self.build(out)
        self.assertEqual(result.returncode, 0, result.stderr)
        return out

    def test_exact_paths_hashes_and_immutable_reproducibility(self):
        sources = set(exporter.INTEGRATION.values())
        before = {s: (ROOT / s).read_bytes() for s in sources if (ROOT / s).exists()}
        a, b = self.good_build('a'), self.good_build('b')
        self.assertEqual(contents(a), contents(b))
        self.assertEqual(before, {s: (ROOT / s).read_bytes() for s in before})
        self.assertEqual(set(contents(a)), EXPECTED)
        files = artifact_contents(a)
        self.assertEqual(set(files), {'petsafe_freshfeed.zip', 'SHA256SUMS'})
        self.assertEqual(files['SHA256SUMS'].decode(),
                         hashlib.sha256(files['petsafe_freshfeed.zip']).hexdigest() + '  petsafe_freshfeed.zip\n')

    def test_notice_attributes_tuya_mobile_with_full_mit_text(self):
        root_notice = (ROOT / 'NOTICE').read_text()
        for required in ('abovecolin/tuya-mobile', 'ade70eff248764757aa177acf9bdd10440ae7394',
                         'Permission is hereby granted'):
            self.assertIn(required, root_notice, f'root NOTICE missing: {required}')

    def test_retired_runtime_and_raw_profile_not_exported(self):
        out = self.good_build()
        self.assertNotIn(PROFILE_DEST, contents(out))
        retired = {'tuya_bridge.py', 'tuya_compat.py', 'source_binding.py',
                   'tuya_fingerprints.json', 'migration.py', 'migration_state.py',
                   'migration_journal.py', 'petsafe_freshfeed_raw.yaml'}
        self.assertFalse(retired & {Path(name).name for name in contents(out)})

    def test_integration_readme_documents_pinned_compatibility(self):
        readme = (ROOT / 'README.md').read_text()
        for required in (
            '2026.10.0',
            '1.20.0',
        ):
            self.assertIn(required, readme, f'readme missing: {required}')

    def test_release_051_041_metadata(self):
        """Release versions retain the pinned compatibility contract."""
        out = self.good_build()
        hacs = json.loads((ROOT / 'hacs.json').read_text())
        self.assertEqual(hacs['homeassistant'], '2026.10.0')
        self.assertEqual(hacs['name'], 'PetSafe FreshFeed')
        self.assertFalse(hacs.get('content_in_root', False))
        manifest = json.loads(contents(out)['manifest.json'])
        self.assertEqual(manifest['version'], '0.5.2')
        self.assertNotIn('after_dependencies', manifest)
        self.assertNotIn('dependencies', manifest)
        self.assertEqual(manifest['requirements'], ['tinytuya==1.20.0'])
        changelog = (ROOT / 'CHANGELOG.md').read_text()
        self.assertIn('## [0.5.0] - 2026-11-18', changelog)
        self.assertIn('## [0.4.0] - 2026-11-18', changelog)
        self.assertLess(changelog.index('## [0.4.0]'),
                        changelog.index('## [0.3.0]'))

    def test_early_compatibility_and_safety_contract(self):
        for path in ('README.md',):
            text = (ROOT / path).read_text()
            for value in ('0.5.2', '2026.10.0', '1.20.0', '3.5'):
                self.assertIn(value, text, f'{path}: {value}')
            for value in ('administrator', 'offline', 'advisory'):
                self.assertIn(value, text.lower(), f'{path}: {value}')

    def test_blueprint_documentation_has_full_source_and_import_urls(self):
        from urllib.parse import quote
        for path in ('README.md',):
            text = (ROOT / path).read_text()
            for blueprint in BLUEPRINTS:
                raw = 'https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/trunk/' + blueprint
                self.assertIn(raw, text, path)
                self.assertIn('https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=' + quote(raw, safe=''), text, path)
            for phrase in ('does not automatically install', 'unsaved increments', 'administrator', 'without backfill'):
                self.assertIn(phrase, text, path)

    def test_readmes_document_standalone(self):
        built = (ROOT / 'README.md').read_text()
        root = (ROOT / 'README.md').read_text()
        for name, text in (('distribution', built), ('root', root)):
            readme = ' '.join(text.split())
            for required in ('tinytuya==1.20.0', '2026.10.0', 'protocol 3.5', 'local key'):
                self.assertIn(required, readme, f'{name} readme missing: {required}')
            for retired in ('Continue the migration', 'Cancel the migration', 'profile copy'):
                self.assertNotIn(retired, readme)

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
        approved = set(exporter.INTEGRATION.values())
        for forbidden in ('private.txt', 'deploy/cards.json', 'docs/whisker-reference.png', '.git/config', 'custom_components/petsafe_freshfeed/unknown.py'):
            self.assertNotIn(forbidden, approved)

    def test_exact_source_inputs_without_raw_profile(self):
        self.assertFalse((ROOT / PROFILE_SRC).exists())
        self.assertEqual(set(exporter.INTEGRATION.values()),
                         {COMPONENT + n for n in RUNTIME} | {'assets/brand/icon.png'})

    def test_manifests_png_and_import_closure(self):
        out = self.good_build()
        manifest = json.loads(contents(out)['manifest.json'])
        self.assertEqual(manifest['domain'], 'petsafe_freshfeed')
        self.assertEqual(manifest['version'], '0.5.2')
        self.assertNotIn('after_dependencies', manifest)
        self.assertNotIn('tuya_local', manifest.get('dependencies', []))
        self.assertEqual(manifest['codeowners'], ['@sudolulo'])
        self.assertEqual(manifest['issue_tracker'], 'https://github.com/sudolulo/petsafe-freshfeed/issues')
        for name, size in ((COMPONENT + 'brand/icon.png', (256, 256)), ('assets/brand/readme.png', (512, 512))):
            png = contents(out)['brand/icon.png'] if name.startswith(COMPONENT) else (ROOT / name).read_bytes()
            self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', png[16:24]), size)
            self.assertEqual(png[25], 6)  # RGBA, transparent background
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
            self.assertEqual(files[name], (ROOT / COMPONENT / name).read_bytes(), name)

    def test_runtime_includes_account_modules_and_profile_export(self):
        self.assertEqual(exporter.RUNTIME, RUNTIME)
        for name in ACCOUNT:
            self.assertIn(name, exporter.RUNTIME)
        self.assertNotIn('account/app_profile.json', exporter.RUNTIME)
        files = contents(self.good_build())
        for name in ACCOUNT:
            self.assertEqual(files[name], (ROOT / COMPONENT / name).read_bytes(), name)
        self.assertNotIn(APP_PROFILE_DEST, files)
        self.assertNotIn(PROFILE_DEST, files)

    def test_tuya_local_assist_not_shipped(self):
        for name in NOT_SHIPPED:
            self.assertNotIn(name, exporter.RUNTIME)
        files = contents(self.good_build())
        for name in NOT_SHIPPED:
            self.assertNotIn(name, files)
        self.assertNotIn('tuya_local_assist', (ROOT / COMPONENT / 'config_flow.py').read_text())
        for name in NOT_SHIPPED:
            self.assertFalse((ROOT / COMPONENT / name).exists(), name)
        self.assertFalse((ROOT / 'test_ha/test_tuya_local_assist.py').exists())
        self.assertFalse((ROOT / PROFILE_SRC).exists())
        self.assertNotIn(PROFILE_DEST, files)

    def test_build_bundles_profile_from_dir(self):
        out = self.base / 'with-profile'
        result = self.build(out, '--profile-dir', self.profile_dir())
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(contents(out)[APP_PROFILE_DEST])
        self.assertEqual(document, {**FAKE_PROFILE, **FAKE_ENVELOPE})
        self.assertEqual(list(document), list(PROFILE_KEYS + ENVELOPE_KEYS))
        written = self.base / 'loaded-profile.json'
        written.write_bytes(contents(out)[APP_PROFILE_DEST])
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from custom_components.petsafe_freshfeed.account import profile as account_profile
        loaded = account_profile.load_app_profile(path=written)
        self.assertIs(type(loaded), account_profile.AppProfile)
        self.assertEqual(loaded._mapping(), {**FAKE_PROFILE, **FAKE_ENVELOPE})
        for key in PROFILE_KEYS + ENVELOPE_KEYS:
            self.assertIn(key, result.stdout)
        self.assertIn(APP_PROFILE_DEST, result.stdout)
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
        self.assertNotIn(APP_PROFILE_DEST, contents(out))
        self.assertNotIn(APP_PROFILE_DEST, (out / 'SHA256SUMS').read_text())

    def test_release_requires_profile_dir(self):
        out = self.base / 'release-no-profile'
        result = self.build(out, '--release')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--profile-dir', result.stderr)
        self.assertFalse(out.exists())
        help_result = subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertNotIn('private', help_result.stdout.lower())
        self.assertIn('directory holding the two app profile input files (values embedded in the public app; never committed)', ' '.join(help_result.stdout.split()))

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
        self.assertEqual(set(contents(out)), EXPECTED | {APP_PROFILE_DEST})
        files = artifact_contents(out)
        self.assertEqual(set(files), {'petsafe_freshfeed.zip', 'SHA256SUMS'})
        self.assertEqual(files['SHA256SUMS'].decode(), hashlib.sha256(files['petsafe_freshfeed.zip']).hexdigest() + '  petsafe_freshfeed.zip\n')

    def test_release_preparation_zip_manifest(self):
        before = (ROOT / 'hacs.json').read_bytes()
        # Explicit tree input: CI also runs with the ZIP manifest checked out.
        tree = {'name': 'PetSafe FreshFeed', 'content_in_root': False,
                'homeassistant': '2026.10.0', 'zip_release': False}
        rendered = exporter.release_manifest(json.dumps(tree).encode())
        manifest = json.loads(rendered)
        self.assertEqual(manifest, {'name': 'PetSafe FreshFeed', 'content_in_root': False,
                                   'homeassistant': '2026.10.0', 'zip_release': True,
                                   'filename': 'petsafe_freshfeed.zip'})
        preparation = self.base / 'tag-tree'
        preparation.mkdir()
        (preparation / 'hacs.json').write_bytes(rendered)
        self.assertTrue(json.loads((preparation / 'hacs.json').read_bytes())['zip_release'])
        self.assertEqual((ROOT / 'hacs.json').read_bytes(), before)
        self.assertEqual(exporter.release_manifest(rendered), rendered)

    def test_deterministic_zip_without_profile(self):
        created = []
        real_mkdir, real_open, real_io_open, real_builtin_open = os.mkdir, os.open, io.open, builtins.open
        current = None

        def record(path):
            path = Path(path).absolute()
            self.assertTrue(path == current or current in path.parents, str(path))
            created.append(path)

        def mkdir(path, *args, **kwargs):
            record(path)
            return real_mkdir(path, *args, **kwargs)

        def os_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                record(path)
            return real_open(path, flags, *args, **kwargs)

        def opener(original):
            def wrapped(path, mode='r', *args, **kwargs):
                if not isinstance(path, int) and any(flag in mode for flag in 'wax+'):
                    record(path)
                return original(path, mode, *args, **kwargs)
            return wrapped

        outputs = []
        for name in ('zip-a', 'zip-b'):
            current = self.base / name
            with contextlib.ExitStack() as stack:
                for function in ('mkdtemp', 'mkstemp', 'NamedTemporaryFile', 'TemporaryDirectory', 'gettempdir'):
                    stack.enter_context(patch.object(tempfile, function, side_effect=AssertionError('tempfile forbidden')))
                stack.enter_context(patch('os.mkdir', side_effect=mkdir))
                stack.enter_context(patch('os.open', side_effect=os_open))
                stack.enter_context(patch('io.open', side_effect=opener(real_io_open)))
                stack.enter_context(patch('builtins.open', side_effect=opener(real_builtin_open)))
                stack.enter_context(patch.object(sys, 'argv', [str(SCRIPT), '--output', str(current)]))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                previous_umask = os.umask(0o022)
                try:
                    exporter.main()
                    actual_umask = os.umask(0o022)
                    self.assertEqual(actual_umask, 0o022, 'builder must restore caller umask')
                finally:
                    os.umask(previous_umask)
            self.assertEqual(current.stat().st_mode & 0o777, 0o700)
            self.assertEqual({p.name for p in current.iterdir()}, {'petsafe_freshfeed.zip', 'SHA256SUMS'})
            for path in current.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            outputs.append(artifact_contents(current))
        self.assertTrue(created)
        self.assertEqual(outputs[0], outputs[1])
        with zipfile.ZipFile(current / 'petsafe_freshfeed.zip') as archive:
            self.assertEqual(archive.namelist(), sorted(EXPECTED))
            self.assertNotIn(APP_PROFILE_DEST, archive.namelist())
            for entry in archive.infolist():
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual((entry.external_attr >> 16) & 0o777, 0o644)
                source = 'assets/brand/icon.png' if entry.filename == 'brand/icon.png' else COMPONENT + entry.filename
                self.assertEqual(archive.read(entry), (ROOT / source).read_bytes())

    def test_synthetic_profile_zip(self):
        import zipfile
        out = self.base / 'zip-profile'
        result = self.build(out, '--release', '--profile-dir', self.profile_dir())
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(out / 'petsafe_freshfeed.zip') as z:
            self.assertEqual(json.loads(z.read('account/app_profile.json')), {**FAKE_PROFILE, **FAKE_ENVELOPE})
        self.assert_no_values(result)

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
