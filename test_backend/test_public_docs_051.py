"""Standalone-only 0.5.1 public documentation contracts (root and integration READMEs)."""
import json
import re
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components/petsafe_freshfeed'
ROOT_README = 'README.md'
INTEGRATION_README = 'README.md'
READMES = (ROOT_README, INTEGRATION_README)
BLUEPRINTS = sorted(p.relative_to(ROOT).as_posix()
                    for p in (ROOT / 'blueprints/automation/petsafe_freshfeed').glob('*.yaml'))
BACKUP = 'Make a full Home Assistant backup and keep it secret; it contains the local key.'
EXTRA_MEAL = 'A pending quick feed the feeder never reported could result in an extra meal.'
GROUPS = ('Status', 'Estimates', 'Schedule', 'History/statistics', 'Connectivity',
          'Faults', 'Events', 'Controls', 'Settings')


def read(path):
    return (ROOT / path).read_text()


def flat(text):
    return ' '.join(text.split())


def sections(text):
    """Return (heading, body) pairs for every markdown heading level 2 or 3."""
    parts = re.split(r'^(#{2,3} .*)$', text, flags=re.M)
    return [(parts[i].lstrip('#').strip(), parts[i + 1]) for i in range(1, len(parts), 2)]


def entity_names():
    strings = json.loads((COMPONENT / 'strings.json').read_text())
    return sorted({row['name'] for platform in strings['entity'].values()
                   for row in platform.values()})


class PublicDocs051(unittest.TestCase):
    # Short failure messages: the haystacks are whole READMEs.
    def assertIn(self, member, container, msg=None):
        if member not in container:
            self.fail(f'{msg}: missing {member!r}')

    def assertNotIn(self, member, container, msg=None):
        if member in container:
            self.fail(f'{msg}: unexpected {member!r}')

    def test_integration_readme_is_under_1200_words_and_root_is_concise(self):
        self.assertLess(len(read(INTEGRATION_README).split()), 1200)
        self.assertLess(len(read(ROOT_README).split()), 1500)

    def test_versions_and_pins(self):
        for path in READMES:
            text = flat(read(path))
            for value in ('0.6.0', '2026.10.0', 'tinytuya==1.20.0', 'protocol 3.5'):
                self.assertIn(value, text, f'{path}: {value}')
            self.assertNotIn('0.5.0', text, path)

    def test_grouped_entity_table_uses_translated_names(self):
        names = entity_names()
        self.assertGreaterEqual(len(names), 26)
        for path in READMES:
            text = read(path)
            rows = {m.group(1).strip(): m.group(2) for m in
                    re.finditer(r'^\| *\**([A-Za-z/]+)\** *\|(.*)\|$', text, re.M)}
            for group in GROUPS:
                self.assertIn(group, rows, f'{path}: table group {group}')
            table = ' '.join(rows[group] for group in GROUPS)
            for name in names:
                self.assertIn(name, table, f'{path}: entity name {name}')
            low = flat(table).lower()
            for phrase in ('unknown means not reported', 'not measured', 'not a device promise',
                           'not an empty plan', 'no backfill', 'not a guarantee',
                           'not proof of consumption', 'read from the petsafe cloud'):
                self.assertIn(phrase, low, f'{path}: {phrase}')

    def test_backup_privacy_and_cloud_session_facts(self):
        for path in READMES:
            text = flat(read(path))
            low = text.lower()
            self.assertIn(BACKUP, text, path)
            for phrase in ('read-only', 'on demand', 'every command is local',
                           'new setups', 'fully local', 'deletes the stored cloud session',
                           'after a restart', 'sign in again', 'password is never stored',
                           'not an acknowledgement', 'changes only the address',
                           '1 (americas)', '44 (europe)', '91 (india)', '7 (russia)',
                           'no fallback', 'shared'):
                self.assertIn(phrase, low, f'{path}: {phrase}')

    def test_official_app_only_for_pairing_and_firmware(self):
        for path in READMES:
            text = flat(read(path))
            sentences = [s for s in re.split(r'(?<=[.!?])\s+', text) if 'official' in s]
            self.assertTrue(any('first Wi-Fi pairing' in s and 'firmware update' in s
                                for s in sentences), path)
            for bad in (r'\bopen (the )?(official |PetSafe )?(phone )?app',
                        r'(refresh|recover\w*|retry) (it )?(in|with|from|using) the (official |PetSafe )?app',
                        r'Sign in with the PetSafe app first'):
                self.assertIsNone(re.search(bad, text, re.I), f'{path}: {bad}')

    def test_controls_warnings_and_recovery_outcomes(self):
        for path in READMES:
            text = read(path)
            control = [body for heading, body in sections(text) if 'control' in heading.lower()]
            self.assertTrue(control, f'{path}: controls section')
            body = flat(' '.join(control))
            self.assertIn(EXTRA_MEAL, body, f'{path}: extra-meal warning beside controls')
            low = flat(text).lower()
            for phrase in ('never-observed schedule', 'administrator', 'not an emergency stop',
                           'execution unknown', 'never sends a command', 'empty tray',
                           'never reported', 'cancel'):
                self.assertIn(phrase, low, f'{path}: {phrase}')

    def test_controls_distinguish_confirmation_from_admin_service_access(self):
        for path in READMES:
            control = ' '.join(body for heading, body in sections(read(path))
                               if 'control' in heading.lower())
            text = flat(control)
            for phrase in (
                'Feed, Cancel, Defrost and Refilled require explicit confirmation.',
                'Command, recovery and schedule-save services require an authenticated administrator;',
                'automations without a user context cannot call those services.',
                'Schedule saves and setting changes do not require explicit confirmation.',
                'Settings use normal Home Assistant entity access.',
            ):
                self.assertIn(phrase, text, path)
            self.assertNotIn('settings and schedule saves need confirmation', text, path)

    def test_schedule_revision_warns_about_external_writer_push_latency(self):
        for path in READMES:
            text = flat(read(path)).lower()
            for phrase in ('changes the integration has observed', 'external writer',
                           'before its update arrives', 'overwritten'):
                self.assertIn(phrase, text, f'{path}: schedule race')
            self.assertNotIn('refused if it changed since loading', text, path)

    def test_troubleshooting(self):
        for path in READMES:
            match = [body for heading, body in sections(read(path))
                     if 'troubleshooting' in heading.lower()]
            self.assertTrue(match, f'{path}: troubleshooting section')
            low = flat(match[0]).lower()
            for phrase in ('settings > system > repairs', 'diagnostics', 'unavailable',
                           'reconfigure', 'recovery', 'do not delete', 'reset'):
                self.assertIn(phrase, low, f'{path}: troubleshooting {phrase}')

    def test_root_leads_with_install_and_has_no_maintainer_commands(self):
        text = read(ROOT_README)
        headings = [heading.lower() for heading, _ in sections(text) if heading]
        first = headings[:3]
        self.assertTrue(any('install' in h for h in first), headings)
        self.assertTrue(any('set' in h or 'sign' in h for h in first), headings)
        self.assertIn('HACS', text.split('\n## ', 2)[1])
        for banned in ('ha-host', 'scp ', 'ssh ', 'with-ha-token', 'rbw', 'add-cards',
                       'shoot-live', 'update-resource', 'REPLACE_WITH_INTEGRATION_ENTRY_ID'):
            self.assertNotIn(banned, text, banned)
        self.assertIn('docs/DEVELOPMENT.md', text)

    def test_no_legacy_setup_or_migration_copy(self):
        for path in READMES:
            text = flat(read(path))
            for banned in ('Use an existing Tuya Local entry', 'Connect directly',
                           'Continue the migration', 'Cancel the migration',
                           'Remove the retired Tuya Local entry', 'petsafe_freshfeed_raw',
                           'Legacy entries', 'legacy path', 'tuya_fingerprints',
                           'after_dependencies', 'Tuya Local is not required'):
                self.assertNotIn(banned, text, f'{path}: {banned}')
            self.assertIsNone(re.search(r'migrat', text, re.I), path)

    def test_internal_jargon_moved_to_developer_guide(self):
        for path in READMES:
            text = flat(read(path))
            self.assertIsNone(re.search(r'\bDP ?\d+', text), path)
            for banned in (r'\bCAS\b', 'compare-and-swap', 'watermark', 'durable witness',
                           'coordinator', r'\bvector\b'):
                self.assertIsNone(re.search(banned, text, re.I), f'{path}: {banned}')

    def test_services_plain_language_and_machine_schema(self):
        import yaml

        services = yaml.safe_load((COMPONENT / 'services.yaml').read_text())
        field = lambda kind: {'required': True, 'selector': {kind: None}}
        expected = {
            'save_schedule': {'entry_id': field('text'), 'entries': field('object'),
                              'base_revision': field('text')},
            'command': {'entry_id': field('text'),
                        'operation': {'required': True, 'selector': {'select': {
                            'options': ['feed', 'cancel', 'defrost', 'refilled']}}},
                        'confirmed': field('boolean')},
            'recover': {'entry_id': field('text'), 'operation_id': field('text'),
                        'session_id': field('text'), 'confirmed': field('boolean')},
        }
        self.assertEqual(set(services), set(expected))
        for name, schema in expected.items():
            service = services[name]
            self.assertEqual(set(service), {'name', 'description', 'fields'})
            self.assertEqual(service['fields'], schema)
            for copy in (service['name'], service['description']):
                self.assertIsNone(re.search(
                    r'\bDP\s*\d+\b|\bCAS\b|compare-and-swap|watermark|'
                    r'durable witness|coordinator|\bvector\b', copy, re.I), name)

    def test_blueprints_linked_once_with_full_urls(self):
        self.assertEqual(len(BLUEPRINTS), 3)
        for path in READMES:
            text = read(path)
            for blueprint in BLUEPRINTS:
                raw = 'https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/trunk/' + blueprint
                imp = ('https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url='
                       + quote(raw, safe=''))
                self.assertEqual(text.count(raw), 1, f'{path}: {raw}')
                self.assertEqual(text.count(imp), 1, f'{path}: {imp}')
            low = flat(text).lower()
            for phrase in ('does not automatically install', 'not feeder commands'):
                self.assertIn(phrase, low, f'{path}: {phrase}')


if __name__ == '__main__':
    unittest.main()
