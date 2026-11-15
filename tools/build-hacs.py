#!/usr/bin/env python3
"""Export explicitly approved public files, never source directory trees."""
import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = 'custom_components/petsafe_freshfeed/'
RUNTIME = '''__init__.py binary_sensor.py config_flow.py const.py critical_transport.py entity.py event.py hub.py lifecycle.py observed.py quick_feed.py schedule_codec.py schedule_state.py sensor.py services.py source_binding.py sqlite_witness.py store_bootstrap.py tuya_bridge.py tuya_compat.py write_adapter.py manifest.json strings.json services.yaml tuya_fingerprints.json translations/en.json'''.split()
COMMON = {'LICENSE': 'LICENSE', 'NOTICE': 'distribution/NOTICE-public', 'assets/brand/logo.svg': 'assets/brand/logo.svg', 'assets/brand/readme.png': 'assets/brand/readme.png'}
PROFILE = 'examples/tuya_local/petsafe_freshfeed_raw.yaml'
INTEGRATION = {**COMMON, **{COMPONENT + n: COMPONENT + n for n in RUNTIME}, COMPONENT + 'brand/icon.png': 'assets/brand/icon.png', 'README.md': 'distribution/README-integration.md', 'hacs.json': 'distribution/hacs-integration.json', '.github/workflows/validate.yml': 'distribution/workflow-integration.yml', PROFILE: 'config/tuya_local/petsafe_freshfeed_raw.yaml'}
CARD = {**COMMON, 'dist/pet-feeder-card.js': 'card/pet-feeder-card.js', 'dist/freshfeed-model.js': 'card/freshfeed-model.js', 'README.md': 'distribution/README-card.md', 'hacs.json': 'distribution/hacs-card.json', '.github/workflows/validate.yml': 'distribution/workflow-card.yml'}

def source_bytes(name):
    if Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError(f'unsafe source path: {name}')
    p = ROOT
    for part in Path(name).parts:
        p = p / part
        if p.is_symlink():
            raise ValueError(f'symlink forbidden: {p}')
    if not p.is_file():
        raise ValueError(f'missing source: {p}')
    return p.read_bytes()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    out = parser.parse_args().output.absolute()
    if '..' in out.parts:
        parser.error('output path must not contain parent traversal')
    if out == ROOT or ROOT in out.parents:
        parser.error('output must be outside source checkout')
    if out.exists() and (out.is_symlink() or any(out.iterdir())):
        parser.error('output must be absent or empty')
    for parent in [out, *out.parents]:
        if parent.is_symlink():
            parser.error('symlink output path forbidden')
    trees = {'petsafe-freshfeed': INTEGRATION, 'pet-feeder-card': CARD}
    files = {f'{tree}/{dest}': source_bytes(src) for tree, mapping in trees.items() for dest, src in mapping.items()}
    out.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(files.items()):
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(files.items())), encoding='utf-8')

if __name__ == '__main__':
    main()
