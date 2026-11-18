# PetSafe FreshFeed

Owned connections expose stainless inserts and meal counter switches, a 0–10 brightness number, and a diagnostic raw quick-feed state. Setting writes require explicitly enabled controls and a subsequent matching device report; an unconfirmed write remains uncertain and is never retried automatically.

<img src="assets/brand/readme.png" width="192" alt="Original teal and navy six-compartment feeder mark">

Local Home Assistant integration, version **0.5.0**, for a compatible PetSafe
FreshFeed feeder. This is independent community software, not affiliated with or
endorsed by PetSafe. The logo is original artwork, not a manufacturer logo.

## Compatibility

| Component | Compatibility |
|---|---|
| Integration 0.5.0 | Standalone HA >= 2026.10.0, TinyTuya 1.20.0, protocol 3.5; Tuya Local is not required. |
| Legacy and migration | Fingerprint-compatible Tuya Local 2026.10.0; migration only HA 2026.10.x. |
| Card 0.4.0 | Aqara and raw modes are independent. |

The card's five-minute warning is advisory only; saving remains allowed, and app
pre-defrost parity is not established. Food estimates assume a filled tray and use
compartment position, not measured food or consumption. Observed-meal statistics
start at zero without backfill and exclude offline gaps and unsaved increments;
abrupt power loss can lose recent increments. Diagnostics omit identifying data;
Repairs advise without mutating retained intent. Guarded services reject missing or
non-administrator contexts; automations are not automatically authorized.

HACS does not automatically install root blueprints. Import these source URLs or
manually copy the YAML into `/config/blueprints/automation/petsafe_freshfeed/`.
These blueprints notify or update helpers only, not feeder commands. Source links do
not assert publication or availability.

- Low estimated meals: [source](https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/blueprints/automation/petsafe_freshfeed/low_estimated_meals.yaml) / [import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsudolulo%2Fpetsafe-freshfeed%2Fmain%2Fblueprints%2Fautomation%2Fpetsafe_freshfeed%2Flow_estimated_meals.yaml)
- Feeder attention: [source](https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/blueprints/automation/petsafe_freshfeed/feeder_attention.yaml) / [import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsudolulo%2Fpetsafe-freshfeed%2Fmain%2Fblueprints%2Fautomation%2Fpetsafe_freshfeed%2Ffeeder_attention.yaml)
- Refill/cleaning: [source](https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/blueprints/automation/petsafe_freshfeed/refill_cleaning_reminder.yaml) / [import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsudolulo%2Fpetsafe-freshfeed%2Fmain%2Fblueprints%2Fautomation%2Fpetsafe_freshfeed%2Frefill_cleaning_reminder.yaml)

## Two kinds of entries

- **Standalone (new in 0.4.0).** Tuya Local is not required. PetSafe FreshFeed owns
  exactly one local connection to the feeder through the Python requirement
  `tinytuya==1.20.0`, which Home Assistant installs with the integration. Requires
  Home Assistant Core `2026.10.0` or later (HACS minimum `homeassistant: 2026.10.0`);
  there is no maximum-version gate, but later Core releases have not been
  regression-tested.
- **Legacy entries** (created with 0.3.0 or earlier, or with **Use an existing Tuya
  Local entry (legacy)**) keep reading the feeder through their tuya_local source
  entry. Upgrading to 0.4.0 never switches a legacy entry to the standalone
  connection; it stays legacy until you explicitly migrate it (see "Migrating a
  legacy entry" below). The manifest lists `tuya_local` only as an optional
  `after_dependencies` entry for start-up ordering of legacy entries, never as a
  required dependency.

## TinyTuya pin and coexistence limitation

Before it connects, the standalone connection checks the loaded TinyTuya against the
version and SHA-256 values in `owned_fingerprints.json` (TinyTuya `1.20.0`). A
different loaded version or changed file fails closed with `owned_source_incompatible`;
there is no bypass. Home Assistant installs one TinyTuya per installation, shared by
every integration that requires it. Tuya Local 2026.10.0 uses TinyTuya `1.20.0` too,
so both coexist today, but a future Tuya Local (or another integration) that pins a
different TinyTuya version can conflict with this exact pin: Home Assistant may fail
to install one of the requirements, or load a version this integration refuses. The
standalone entry then stays unavailable rather than running unaudited code.

## Standalone setup (Sign in with PetSafe)

Available in builds that bundle the app profile; then adding **PetSafe FreshFeed**
shows a menu with **Sign in with PetSafe** and **Use an existing Tuya Local entry
(legacy)**. Builds without it show no menu and offer only the legacy path.

1. Install this integration (see Installation) and restart Home Assistant.
2. Add **PetSafe FreshFeed**, choose **Sign in with PetSafe**, and enter the region,
   email and password of your PetSafe app account.
3. Choose the feeder if the account has several and confirm. The entry is created
   **read-only**; enable controls afterwards in the entry's **Configure** options.

Sign-in supports four regions only, by country code: 1 (Americas), 44 (Europe),
91 (India) and 7 (Russia). Accounts in any other region need the legacy path.

The integration signs in to the PetSafe (Tuya platform) cloud read-only and finds your
FreshFeed with its device ID, local key and name. It listens passively on the LAN (UDP
6666/6667, up to 8 seconds, nothing sent) for the feeder's address; a broadcast is
only a hint, because broadcasts can be forged. If none arrives it asks for a private
IPv4 address (10/8, 172.16/12 or 192.168/16; 100.64/10 and IPv6-only LANs are not
supported). Before anything is saved, one authenticated read-only status check over
local protocol 3.5 must succeed with the account's local key; a wrong key, stale
address or forged broadcast fails setup without saving anything or sending a command.
Other local protocols are refused. A feeder that already has a PetSafe entry, or whose
enabled Tuya Local entry already holds its connection, stops setup. Sign-in is limited
to one attempt per 60 seconds, with no automatic retry; the limit is held in memory per
Home Assistant instance and resets when Home Assistant restarts.

- **Sent:** HTTPS to the PetSafe/Tuya mobile API hosts for the selected region, plus
  a server-directed API host only if it is on a closed allowlist of Tuya API domains,
  with fixed headers (`User-Agent: petsafe-freshfeed/<version>`); on the LAN, the
  read-only status check to the feeder. No feeder command is sent during setup.
- **Stored:** the password and the email are never stored. The device ID, local key,
  address and protocol are stored in the PetSafe FreshFeed config entry and are kept
  out of logs and error messages. Home Assistant backups contain that config entry,
  including the local key: treat every backup as secret.
- **Account session:** sign-in creates a new account session on the PetSafe/Tuya
  cloud. Whether this signs the PetSafe phone app out has not been checked.
- **Address change:** **Reconfigure** on a standalone entry changes only the address.
  It stops the entry, runs one read-only status check of the new address, and keeps
  the entry, device, entities, history and controls store.

## Migrating a legacy entry

Migration moves an existing legacy entry to the standalone connection in place. It
keeps the PetSafe entry ID, entity IDs, dashboard links, history, schedule state, the
existing device-registry entry and the controls (retained-intent) store and binding;
it never copies, resets or recreates the store. It is explicit and staged around a
deliberate Home Assistant restart, and requires Home Assistant Core `2026.10.x`: on any
other Core release migration is refused and nothing changes (new standalone setup is
not limited this way). No feeder command is sent.

Before you start, make a full Home Assistant backup and keep it secret: it contains
the local key. The local key is read once from the selected Tuya Local entry and saved
in the PetSafe entry; it is never shown.

1. Open the PetSafe entry's **Configure** options and choose **Connect directly
   (migrate away from Tuya Local)**. The preview lists the Tuya Local raw entities
   that will retire. Confirming stops PetSafe controls, waits for in-flight work and
   disables (does not delete) the Tuya Local entry. The feeder is unavailable from
   here until the migration completes.
2. Restart Home Assistant. This restart checkpoint is required: the migration
   continues only in a different Home Assistant process that observes the Tuya Local
   entry still disabled and unloaded. If it is not (for example the disabled flag did
   not persist after a power loss), the migration stays fenced and **Continue the
   migration** repeats the checkpoint; it never assumes the restart worked. If a power
   loss also lost a corrected address entered in step 1, the checkpoint cannot repeat
   and only cancelling remains. Do not edit the Tuya Local entry while a migration is
   open: a changed entry is refused.
3. Open the options again and choose **Continue the migration**. One authenticated
   read-only status check over local protocol 3.5 runs first, then the existing device
   moves from Tuya Local to PetSafe FreshFeed and the connection is checked again. If
   the Tuya Local entry uses a host name or automatic protocol, you are asked for the
   feeder's private IPv4 address and protocol 3.5; there is no automatic downgrade.

Retiring raw entities: when the device moves, Home Assistant removes the Tuya Local
raw entities on it (the ones listed in the preview). Raw Tuya Local entity IDs are not
preserved; update anything that used them. PetSafe FreshFeed entities are kept.

**Cancel the migration** is offered only before the device moves. It restores the
legacy configuration and re-enables the Tuya Local entry if it was enabled before.
The legacy entry then keeps working (also after later edits to the Tuya Local entry),
and a new migration can be started again from the options.
After the move, recovery is **forward-only**: the device is never moved back (Home
Assistant would delete the PetSafe entities). If the post-move check fails, controls
stay stopped; choose **Continue the migration** again, optionally with the feeder's
current address. The local key is re-read from the retained Tuya Local entry if needed
and only while that entry is unchanged; if it was deleted or changed after the move,
enter the feeder's local key in the same form. Once the migration is verified,
start-up no longer depends on the Tuya Local entry.

Controls stay fenced at every start-up while a migration is unfinished. A retained
uncertain command migrates as it is: it is never replayed, released from cached state
or erased, and still needs an explicit recovery afterwards.

**Uninstall eligibility.** After the migration completes and a later Home Assistant
restart has re-observed the standalone configuration and device ownership, the options
offer **Remove the retired Tuya Local entry**. It removes only that one Tuya Local
entry; the Tuya Local integration is not uninstalled. Before you uninstall Tuya Local
yourself, check that no other config entry (including disabled or ignored ones) still
uses it. Re-enabling a retired Tuya Local entry while the standalone connection is in
use is an unsupported ownership conflict: PetSafe FreshFeed stops its own connection,
but it cannot stop another integration's connection.

## Legacy Tuya Local entries

A legacy entry reads the feeder through a [tuya_local](https://github.com/make-all/tuya-local)
source entry that uses the `petsafe_freshfeed_raw` profile, and does not accept any
other profile. It uses that existing client, not a second LAN connection. Its bundled `tuya_fingerprints.json` allowlist checks the
loaded tuya_local source code before admitting transport/receive ownership; the
authoritative provenance is tuya_local revision
`027351717d3f6b8bef30c47a018944b430f5661e` (release `2026.10.0`) and TinyTuya `1.20.0`.
An unknown or changed runtime fails closed with `tuya_source_incompatible`.
Do not bypass the fingerprints, do not install an arbitrary tuya_local commit, and do
not assume every tuya_local release or FreshFeed device is supported. HACS installation
does **not** establish runtime compatibility. Local fixture tests are not hardware
certification.

## Pinned Tuya Local compatibility (legacy entries only)

The reviewed, supported tuya_local source is the pinned public archive
<https://github.com/make-all/tuya-local/archive/027351717d3f6b8bef30c47a018944b430f5661e.tar.gz>
(release `2026.10.0`, TinyTuya `1.20.0`). Install tuya_local from that archive using
the normal custom-integration install steps, and verify the shipped files against the
SHA-256 values in this integration's `tuya_fingerprints.json`; those hashes are
authoritative and there is no bypass. Do not guess or install an arbitrary tuya_local
commit; HACS arbitrary-commit UI support is not claimed here.

### FreshFeed raw profile (Use an existing Tuya Local entry)

This distribution ships the raw observation profile at
`examples/tuya_local/petsafe_freshfeed_raw.yaml`. Before creating this integration's
entry, copy that file exactly to the tuya_local device profile location:

```
/config/custom_components/tuya_local/devices/petsafe_freshfeed_raw.yaml
```

Then restart Home Assistant so tuya_local loads the profile, and in the tuya_local
config flow create the FreshFeed entry selecting the profile `petsafe_freshfeed_raw`
(or select the existing entry already created with that profile). The source profile is
maintained separately from this distribution: tuya_local has no external profile
loader, a HACS redownload or upgrade of tuya_local may overwrite the local profile file,
and on this path this integration does not install or preserve it. Keep a canonical
copy and restore and verify it before any tuya_local upgrade.

## Live acceptance scope for 0.4.0

Standalone setup and legacy migration are covered by automated tests against the
Home Assistant 2026.10 test harness with synthetic feeders. Live acceptance on a real
installation is recorded separately; it issues no physical feeder command, so physical
control through the standalone connection is not live-tested.

## Installation

In HACS, add `https://github.com/sudolulo/petsafe-freshfeed` as a custom repository,
category **Integration**, then download it. Restart Home Assistant, then add
**PetSafe FreshFeed** under Settings > Devices & services; choose **Sign in with
PetSafe** (standalone, when offered) or **Use an existing Tuya Local entry (legacy)**
and select the tuya_local source entry created with the `petsafe_freshfeed_raw`
profile.
Alternatively copy `custom_components/petsafe_freshfeed` from this distribution into
your Home Assistant `custom_components` directory, then restart and add it.
This repository is a clean distribution, not a default HACS catalog listing claim.

Controls are opt-in through the integration options. Explicitly enabling controls
provisions durable storage; ordinary startup never creates missing storage. Preserve
the opaque store identity and SQLite database together. Never delete or recreate
storage to clear uncertainty. Storage and integrity faults block writes.
On legacy entries, native raw tuya_local controls remain accessible to administrators
outside this adapter; they are not endorsed critical command paths. Standalone entries
expose no raw writable entities.

## Command safety and recovery

Feed, Cancel, Defrost and Refilled require confirmation and eligible live state.
The adapter blocks conflicting, uncertain, stale-state or faulty operations. Guarded
services `petsafe_freshfeed.command`, `petsafe_freshfeed.save_schedule` and
`petsafe_freshfeed.recover` require an authenticated HA administrator context;
automations without a user context cannot invoke them. Do not substitute raw
text/button services. Cancel is not an emergency stop.

Every new coordinator re-arms uncertainty after a prepared intent, even a previously
verified or recovered one; operations are never replayed or erased. Recovery explicitly
acknowledges the current operation/session before collecting new reports for up to
35 seconds. Missing evidence leaves the barrier in place; retry explicitly when
eligible. Recovery cannot repair storage/integrity faults and does not prove physical
success. Device observations do not establish command causality or physical feeding.

## Schedule and history limits

Schedule state is current only after an actual DP1 report on the uninterrupted
connection, last-known after restart/reload/disconnect, or unknown if never seen.
Last-known schedules remain editable with provenance visible. Saving confirms the
whole vector (maximum 10 rows), preserving order, duplicates, disabled rows and audible
flags. Revision checks cannot close the external-writer push-latency race: there is
no device compare-and-swap, demonstrated on-demand schedule read, or cross-app
exactly-once guarantee. A matching report verifies a local write, not physical feeding.
Next meal is a timezone-aware schedule projection, not a device promise.

History contains HA-observed DP14/DP150 reports and local DP14/DP145 transitions,
not cloud history. Events without source metadata are labeled HA-observed local
transitions with derivation inferred from event kind; this does not distinguish newly
recorded transitions from older persisted ones. **DP151 retrieval/decoding is unsupported.**

The optional [Pet Feeder Card](https://github.com/sudolulo/pet-feeder-card) is a
separate dashboard distribution. Report integration issues at
https://github.com/sudolulo/petsafe-freshfeed/issues.
Canonical development: https://git.arch.fyi/flan/pet-feeder-card.
Original work is MIT licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
