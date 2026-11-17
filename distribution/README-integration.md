# PetSafe FreshFeed community integration

<img src="assets/brand/readme.png" width="192" alt="Original teal and navy six-compartment feeder mark">

Local Home Assistant integration, version **0.3.0**, for a compatible PetSafe
FreshFeed source. This is independent community software, not affiliated with or
endorsed by PetSafe. The logo is original artwork, not a manufacturer logo.

## Prerequisites and compatibility

This integration reads the feeder through a [tuya_local](https://github.com/make-all/tuya-local)
source entry that uses the `petsafe_freshfeed_raw` profile, and does not accept any
other profile. Either create that entry yourself before adding this integration (see
"Use an existing Tuya Local entry" below), or let this integration create it through
Tuya Local's own setup flow (see "Sign in with PetSafe" below). This integration uses that existing client, not
a second LAN connection. Its bundled `tuya_fingerprints.json` allowlist checks the
loaded tuya_local source code before admitting transport/receive ownership; the
authoritative provenance is tuya_local revision
`027351717d3f6b8bef30c47a018944b430f5661e` (release `2026.10.0`) and TinyTuya `1.20.0`.
An unknown or changed runtime fails closed with `tuya_source_incompatible`.
Do not bypass the fingerprints, do not install an arbitrary tuya_local commit, and do
not assume every tuya_local release or FreshFeed device is supported. HACS installation
does **not** establish runtime compatibility. Local fixture tests are not hardware
certification.

## Pinned compatibility

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

## Sign in with PetSafe

Available only in builds that bundle the app profile; then adding **PetSafe FreshFeed**
shows a menu with **Sign in with PetSafe** and **Use an existing Tuya Local entry**.
Builds without it show no menu and offer only the existing-entry path.

1. Install tuya_local release `2026.10.0` exactly (the pinned revision above) from
   HACS and restart Home Assistant. HACS installs the latest release by default, so
   choose `2026.10.0` explicitly, for example with HACS **Redownload** and its version
   selector. With any other tuya_local version, set the feeder up in tuya_local
   yourself and use **Use an existing Tuya Local entry**.
2. Install this integration (see Installation) and restart.
3. Add **PetSafe FreshFeed**, choose **Sign in with PetSafe**, and enter the region,
   email and password of your PetSafe app account.

Sign-in supports four regions only, by country code: 1 (Americas), 44 (Europe),
91 (India) and 7 (Russia). Accounts in any other region need the existing-entry path.

The integration signs in to the PetSafe (Tuya platform) cloud read-only, finds your
FreshFeed with its device ID, local key and name (you choose if there are several),
and listens passively on the LAN (UDP 6666/6667, up to 8 seconds, nothing sent) for
its address; if none arrives it asks for a private IPv4 address (10/8, 172.16/12 or
192.168/16; 100.64/10 and IPv6-only LANs are not supported). It checks that the loaded
tuya_local is the pinned release, installs the `petsafe_freshfeed_raw` profile into
tuya_local's `devices/` folder if it is absent (never overwriting a different file),
drives tuya_local's own setup flow to create the tuya_local entry, then creates the
PetSafe FreshFeed entry bound to it. A loaded tuya_local entry for the same feeder with
that profile is reused; any other tuya_local entry for it stops setup. Sign-in is
limited to one attempt per 60 seconds, with no automatic retry; the limit is held in
memory per Home Assistant instance and resets when Home Assistant restarts.

- **Sent:** HTTPS to the PetSafe/Tuya mobile API hosts for the selected region, plus
  a server-directed API host only if it is on a closed allowlist of Tuya API domains,
  with fixed headers (`User-Agent: petsafe-freshfeed/<version>`); on the LAN,
  tuya_local's connection test sends read-only status queries to the feeder. No
  feeder command is sent during setup; setup contacts nothing else.
- **Never stored:** the password and the email. The local key and device ID are
  stored only in the tuya_local entry, exactly as when you create it by hand. Home
  Assistant backups contain that tuya_local entry, including the local key and
  device ID.
- **Account session:** sign-in creates a new account session on the PetSafe/Tuya
  cloud. Whether this signs the PetSafe phone app out has not been checked.
- **Caveats:** tuya_local logs the feeder's data points (DPS) at warning level during
  its setup. A tuya_local update can remove the installed profile file; this
  integration reinstalls it on the next sign-in setup. Accounts that need MFA or
  another challenge, or are locked, are refused with a fixed message; use the
  existing-entry path. If setup stops after the tuya_local entry was created, that
  entry is kept and the message says how to finish with **Use an existing Tuya Local
  entry**.

## Live acceptance scope for 0.3.0

On 2026-11-17, sign-in through Home Assistant discovered the existing FreshFeed
and stopped as already configured. Entry counts stayed at one PetSafe entry and
one Tuya Local entry. No feeder command was issued. Creating a new Tuya Local
entry on a fresh installation is covered by automated tests, but has not been
live-tested. Tuya Local 2026.10.0 remains a required separately installed dependency.

## Installation

In HACS, add `https://github.com/sudolulo/petsafe-freshfeed` as a custom repository,
category **Integration**, then download it. Restart Home Assistant, then add
**PetSafe FreshFeed** under Settings > Devices & services; choose **Sign in with
PetSafe** (when offered) or select the existing tuya_local source entry created with
the `petsafe_freshfeed_raw` profile.
Alternatively copy `custom_components/petsafe_freshfeed` from this distribution into
your Home Assistant `custom_components` directory, then restart and add it.
This repository is a clean distribution, not a default HACS catalog listing claim.

Controls are opt-in through the integration options. Explicitly enabling controls
provisions durable storage; ordinary startup never creates missing storage. Preserve
the opaque store identity and SQLite database together. Never delete or recreate
storage to clear uncertainty. Storage and integrity faults block writes.
Native raw tuya_local controls remain accessible to administrators outside this
adapter; they are not endorsed critical command paths.

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
