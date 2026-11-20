# Development

## Layout

- `custom_components/petsafe_freshfeed/`: the integration.
  - `owned_source.py`, `local_source.py`, `lan_locate.py`: the local protocol 3.5
    connection, using the pinned `tinytuya==1.20.0`.
  - `hub.py`, `entity.py` and the platform modules: Home Assistant entities.
  - `write_adapter.py`, `critical_transport.py`, `sqlite_witness.py`,
    `store_bootstrap.py`: guarded commands. Every command is recorded durably
    before it is sent, so an interrupted operation is never retried blindly.
  - `observed.py`, `quick_feed.py`, `schedule_codec.py`, `schedule_state.py`: pure
    decoding of feeder reports, tested without Home Assistant.
  - `account/`, `cloud_read.py`: PetSafe sign-in and optional read-only cloud data.
- `blueprints/`: optional notification blueprints.
- `test_backend/`: standard-library `unittest` tests that do not need Home Assistant.
- `test_ha/`: pytest tests that run against Home Assistant.
- `tools/build-release.py`: builds the release ZIP.

## Tests

Use Python 3.14 with the same pins as CI:

```sh
python -m pip install pytest-homeassistant-custom-component==0.13.375 tinytuya==1.20.0
python -m unittest discover -s test_backend -q
python -m pytest test_ha -q --asyncio-mode=auto -p no:cacheprovider
```

Run the backend tests with a umask of 022. Two store tests check that loosely created
directories are refused, and a stricter umask makes those directories private already.

Tests use synthetic data and fake devices only. They never contact a real feeder,
PetSafe account or cloud endpoint, and no test may send a real Feed command.

`custom_components/petsafe_freshfeed/strings.json` and `translations/en.json` must
stay identical.

## Release builds

Build the runtime ZIP with `python3 tools/build-release.py --output /existing-parent/out`.
The output directory holds exactly `petsafe_freshfeed.zip` and `SHA256SUMS`. Public
release builds add `--release --profile-dir <dir>`, which embeds the app profile only
in the ZIP; never commit profile values. Tag a ZIP-manifest preparation commit, then
restore the tree manifest in a forward commit before publishing trunk. Never publish
branch-named release assets.
