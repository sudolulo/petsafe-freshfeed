#!/usr/bin/env python3
"""Export explicitly approved public files, never source directory trees."""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = 'custom_components/petsafe_freshfeed/'
RUNTIME = '''__init__.py binary_sensor.py config_flow.py const.py critical_transport.py entity.py event.py hub.py lifecycle.py observed.py quick_feed.py schedule_codec.py schedule_state.py sensor.py services.py source_binding.py sqlite_witness.py store_bootstrap.py tuya_bridge.py tuya_compat.py write_adapter.py manifest.json strings.json services.yaml tuya_fingerprints.json translations/en.json account/__init__.py account/boundary.py account/client.py account/profile.py account/exchange.py account/protocol_contract.json lan_locate.py tuya_local_assist.py source_lookup.py profile/petsafe_freshfeed_raw.yaml'''.split()
COMMON = {'LICENSE': 'LICENSE', 'NOTICE': 'distribution/NOTICE-public', 'assets/brand/logo.svg': 'assets/brand/logo.svg', 'assets/brand/readme.png': 'assets/brand/readme.png'}
PROFILE = 'examples/tuya_local/petsafe_freshfeed_raw.yaml'
INTEGRATION = {**COMMON, **{COMPONENT + n: COMPONENT + n for n in RUNTIME}, COMPONENT + 'brand/icon.png': 'assets/brand/icon.png', 'README.md': 'distribution/README-integration.md', 'hacs.json': 'distribution/hacs-integration.json', '.github/workflows/validate.yml': 'distribution/workflow-integration.yml', PROFILE: 'config/tuya_local/petsafe_freshfeed_raw.yaml'}
CARD = {**COMMON, 'dist/pet-feeder-card.js': 'card/pet-feeder-card.js', 'dist/freshfeed-model.js': 'card/freshfeed-model.js', 'README.md': 'distribution/README-card.md', 'hacs.json': 'distribution/hacs-card.json', '.github/workflows/validate.yml': 'distribution/workflow-card.yml'}

# App profile (plan Global Constraints): written only into the build output,
# never into the source checkout. Values are never printed; messages name keys only.
APP_PROFILE_DEST = 'petsafe-freshfeed/' + COMPONENT + 'account/app_profile.json'
PROFILE_INPUTS = (
    ('public-apk-profile.private.json', ('app_id', 'app_secret', 'cert_sha256_hex', 'app_key', 'package')),
    ('public-apk-envelope.private.json', ('app_version', 'ttid', 'sdk_version', 'device_core_version')),
)
MAX_PROFILE_INPUT_BYTES = 4096
MAX_PROFILE_VALUE_CHARS = 256
MAX_APP_PROFILE_BYTES = 4096  # account/profile.py MAX_FILE_BYTES
_PRINTABLE = re.compile(r'\A[\x20-\x7e]+\Z')
_CERT_HEX = re.compile(r'\A[0-9a-f]{64}\Z')


class ProfileError(Exception):
    """Fixed message built only from constant file and key names."""


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError()
        out[key] = value
    return out


def _bad_constant(_name):
    raise ValueError()


def _read_private(directory, name):
    """O_NOFOLLOW, regular file, owned by this user, owner-only mode, bounded."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(directory / name, flags)
    except OSError:
        raise ProfileError(f'{name}: cannot open (missing or symlink)') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProfileError(f'{name}: not a regular file')
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProfileError(f'{name}: must be owned by this user with owner-only mode')
        if info.st_size > MAX_PROFILE_INPUT_BYTES:
            raise ProfileError(f'{name}: too large')
        chunks, total = [], 0
        while True:
            try:
                chunk = os.read(fd, MAX_PROFILE_INPUT_BYTES + 1 - total)
            except OSError:
                raise ProfileError(f'{name}: read failed') from None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PROFILE_INPUT_BYTES:
                raise ProfileError(f'{name}: too large')
    finally:
        os.close(fd)
    failed = False
    try:
        document = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=_no_duplicates,
                              parse_constant=_bad_constant)
    except (ValueError, RecursionError):
        failed = True
    if failed:
        raise ProfileError(f'{name}: not strict JSON (syntax, encoding, duplicate key or non-finite number)')
    return document


def load_profile_dir(directory):
    """Return the app_profile.json bytes and its key names. Raises ProfileError."""
    try:
        info = os.lstat(directory)
    except OSError:
        raise ProfileError('profile directory: missing') from None
    if not stat.S_ISDIR(info.st_mode):
        raise ProfileError('profile directory: not a directory (symlinks refused)')
    values = {}
    for name, keys in PROFILE_INPUTS:
        document = _read_private(directory, name)
        if type(document) is not dict:
            raise ProfileError(f'{name}: not a JSON object')
        for key in keys:
            if key not in document:
                raise ProfileError(f'key {key}: missing from {name}')
        if len(document) != len(keys):
            raise ProfileError(f'{name}: unexpected key (expected exactly: {", ".join(keys)})')
        for key in keys:
            value = document[key]
            if type(value) is not str or not 1 <= len(value) <= MAX_PROFILE_VALUE_CHARS or not _PRINTABLE.match(value):
                raise ProfileError(f'key {key}: must be a non-empty string of at most {MAX_PROFILE_VALUE_CHARS} printable ASCII characters')
            if key == 'cert_sha256_hex' and not _CERT_HEX.match(value):
                raise ProfileError(f'key {key}: must be 64 lowercase hex characters')
            values[key] = value
    data = json.dumps(values, indent=2).encode('ascii') + b'\n'
    if len(data) > MAX_APP_PROFILE_BYTES:
        raise ProfileError('app_profile.json: too large')
    return data, tuple(values)


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
    parser.add_argument('--profile-dir', type=Path, help='directory holding the two private app profile files')
    parser.add_argument('--release', action='store_true', help='public release build; requires --profile-dir')
    args = parser.parse_args()
    if args.release and args.profile_dir is None:
        parser.error('--release requires --profile-dir (a release must not ship with account sign-in disabled)')
    out = args.output.absolute()
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
    profile_keys = None
    if args.profile_dir is not None:
        try:
            files[APP_PROFILE_DEST], profile_keys = load_profile_dir(args.profile_dir.absolute())
        except ProfileError as error:
            print(f'profile error: {error}', file=sys.stderr)
            sys.exit(1)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(files.items()):
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / 'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in sorted(files.items())), encoding='utf-8')
    if profile_keys is None:
        print('account sign-in disabled: no app profile')
    else:
        print(f'app profile keys: {", ".join(profile_keys)}')
        print(f'app profile written: {out / APP_PROFILE_DEST}')

if __name__ == '__main__':
    main()
