# pet-feeder-card

## Compatibility

| Component | Compatibility |
|---|---|
| FreshFeed integration 0.5.0 | Standalone: HA >= 2026.10.0, TinyTuya 1.20.0, protocol 3.5; Tuya Local is not required. |
| Legacy and migration | Fingerprint-compatible Tuya Local 2026.10.0; migration only HA 2026.10.x. |
| Card 0.4.0 | Aqara and raw modes are independent of the FreshFeed integration. |

The visual editor discovers entities where registry access permits; manual YAML remains
available. The five-minute near-meal warning is advisory only, not verified app
pre-defrost parity; saving remains allowed. Estimates derive from compartment position,
assuming a filled tray, not measured food or consumption. Observed-meal statistics
exclude offline gaps and unsaved increments, start at zero without backfill, and can
lose recent increments on abrupt power loss. Diagnostics omit identifying data;
Repairs only advise and do not mutate retained intent. Guarded services reject missing
or non-administrator contexts; automations are not automatically authorized.

HACS does not automatically install root blueprints. Import from the source links below
or manually copy the YAML into `/config/blueprints/automation/petsafe_freshfeed/`.
These blueprints notify or update helpers, not feeder commands; the links are source
locations, not a claim of publication or availability.

- Low estimated meals: [source](https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/blueprints/automation/petsafe_freshfeed/low_estimated_meals.yaml) / [import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsudolulo%2Fpetsafe-freshfeed%2Fmain%2Fblueprints%2Fautomation%2Fpetsafe_freshfeed%2Flow_estimated_meals.yaml)
- Feeder attention: [source](https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/blueprints/automation/petsafe_freshfeed/feeder_attention.yaml) / [import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsudolulo%2Fpetsafe-freshfeed%2Fmain%2Fblueprints%2Fautomation%2Fpetsafe_freshfeed%2Ffeeder_attention.yaml)
- Refill/cleaning: [source](https://raw.githubusercontent.com/sudolulo/petsafe-freshfeed/main/blueprints/automation/petsafe_freshfeed/refill_cleaning_reminder.yaml) / [import](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fsudolulo%2Fpetsafe-freshfeed%2Fmain%2Fblueprints%2Fautomation%2Fpetsafe_freshfeed%2Frefill_cleaning_reminder.yaml)

A Home Assistant Lovelace custom card for pet feeders, laid out like the Whisker
Litter-Robot card: status header, a large product illustration, round action buttons,
a chip, a food gauge and a metrics footer. Two models have artwork and defaults:

- `freshfeed` -- PetSafe FreshFeed refrigerated wet-food feeder (PFD00-18290)
- `aqara_c1` -- Aqara Smart Pet Feeder C1 (ZNCWWSQ01LM), via Zigbee2MQTT

On the FreshFeed a meal tray shows which of the 6 compartments are eaten, serving or
still full (`counter` or `meals_left` entity, `meal_slots` to change the count).
Feed actions need two taps within 4 seconds unless `confirm: false`. The full
configuration reference is in `docs/card-spec.md`; working examples are in
`deploy/cards.json`.

## Layout

| Path | What |
|------|------|
| `card/pet-feeder-card.js` | The card entry module; imports `card/freshfeed-model.js`, no build step or runtime dependencies |
| `test/` | Unit tests for the pure helpers (`npm test`) |
| `harness/` | Mock-`hass` page plus Playwright checks and screenshots (`npm run shoot`, output in `harness/out/`) |
| `deploy/cards.json` | Reference/demo configurations; not a backup or source of truth for current live Home Assistant bindings |
| `tools/` | General Home Assistant websocket helpers, plus demo-only card installer and interactive screenshot checker |

## Deploy

The HA token comes from rbw at runtime (`tools/with-ha-token.sh`), never from a file.

```sh
npm install --no-fund --no-audit && npm test && npm run shoot
# After backing up the current module and confirming the live Lovelace resource:
scp card/freshfeed-model.js ha-host:/config/www/pet-feeder-card/freshfeed-model.js.new
ssh ha-host 'mv -f /config/www/pet-feeder-card/freshfeed-model.js.new /config/www/pet-feeder-card/freshfeed-model.js'
scp card/freshfeed-editor-model.js ha-host:/config/www/pet-feeder-card/freshfeed-editor-model.js.new
ssh ha-host 'mv -f /config/www/pet-feeder-card/freshfeed-editor-model.js.new /config/www/pet-feeder-card/freshfeed-editor-model.js'
scp card/pet-feeder-card-editor.js ha-host:/config/www/pet-feeder-card/pet-feeder-card-editor.js.new
ssh ha-host 'mv -f /config/www/pet-feeder-card/pet-feeder-card-editor.js.new /config/www/pet-feeder-card/pet-feeder-card-editor.js'
scp card/pet-feeder-card.js ha-host:/config/www/pet-feeder-card/pet-feeder-card.js.new
ssh ha-host 'mv -f /config/www/pet-feeder-card/pet-feeder-card.js.new /config/www/pet-feeder-card/pet-feeder-card.js'
# Update only the existing resource URL's cache-buster, then:
tools/with-ha-token.sh node tools/ha-ws.mjs tools/cmds/update-resource.json
```

Install all four sibling modules together: `pet-feeder-card.js`, `freshfeed-model.js`,
`pet-feeder-card-editor.js` and `freshfeed-editor-model.js`.
When changing the model module, also bump its import query version in the entry module
(the Lovelace resource cache-buster alone does not invalidate imported modules).
This updates the module files and resource cache-buster only; it preserves existing
stored raw-entity bindings. After updating, refresh the browser. No Home Assistant core
restart is needed. Before running these commands against a live dashboard, back up its
current YAML, inspect the exact two existing card slots and resource ID, and verify the
resource URL/version; do not infer live configuration from `deploy/cards.json`. This
repository's command JSON currently names a fixed resource ID and cache-buster, so review
and update those deliberately for the verified live resource.

### FreshFeed parity card binding

`deploy/freshfeed-live-card.json` is a sanitized parity template, not a deployable
backup. Before any separately authorized deployment, the release owner must privately
replace `REPLACE_WITH_INTEGRATION_ENTRY_ID` with the existing integration entry ID;
the card rejects that sentinel until bound. Preserve the existing card positions
`/views/1/sections/1/cards/0` and `/views/9/cards/2`, raw entity IDs and Aqara config.
The template uses only `petsafe_freshfeed.command` for Feed, Defrost and Refilled;
the schedule editor and recovery use the same integration's guarded services.
Generic user-authored raw HA service calls outside this card are not intercepted.

### Demo setup only

**Warning:** `deploy/cards.json` still contains FreshFeed demo helper bindings and
actions. `add-cards.mjs` replaces matching cards by model and will overwrite a converted
raw card. Do not run it against a live FreshFeed dashboard when upgrading. It also writes
`deploy/lovelace-before.json` as a private Home Assistant configuration snapshot, even
with `--dry-run`; do not commit that file. Use only on a dedicated demo dashboard whose
scope you have confirmed. Start with `--dry-run` and restore demo configuration only
when you intentionally want to.

`shoot-live.mjs` is an interactive demo test, not read-only live verification: it presses
the FreshFeed Feed action. Do not run it against converted live cards.

```sh
tools/with-ha-token.sh node tools/add-cards.mjs deploy/cards.json --dry-run
tools/with-ha-token.sh node tools/add-cards.mjs deploy/cards.json
tools/with-ha-token.sh node tools/shoot-live.mjs
```

## FreshFeed integration 0.5.0

Integration 0.4.0 adds standalone entries: Tuya Local is not required. A standalone
entry owns exactly one local connection to the feeder through the pinned Python
requirement `tinytuya==1.20.0`, installed by Home Assistant with the integration.
Standalone setup requires Home Assistant Core `2026.10.0` or later (HACS metadata
`homeassistant: 2026.10.0`); there is no maximum-version gate, and later Core
releases need regression verification. Legacy entries (0.3.0 and earlier, or
**Use an existing Tuya Local entry (legacy)**) stay on their tuya_local source until
explicitly migrated; an upgrade never switches the backend. `tuya_local` remains only
an optional `after_dependencies` manifest entry for legacy start-up ordering, never a
required dependency.

TinyTuya coexistence: Home Assistant installs one TinyTuya shared by every
integration. Before connecting, the standalone source checks the loaded TinyTuya
version and files against `owned_fingerprints.json`; a mismatch fails closed with
`owned_source_incompatible`. Tuya Local 2026.10.0 also uses TinyTuya 1.20.0, but a
future Tuya Local (or other integration) pin change can conflict with this exact pin;
the standalone entry then stays unavailable rather than running unaudited code.

For a manual install, copy `custom_components/petsafe_freshfeed` to the Home Assistant
custom components folder and restart HA. In the integration options, explicitly
enable controls to provision durable storage. Ordinary startup never provisions
missing storage. Preserve the opaque store identity in the config entry and its SQLite
database together; do not delete, reset or recreate them to clear uncertainty.
Storage/identity faults block writes and cannot be waived by UI acknowledgement.

When the build bundles the app profile, adding **PetSafe FreshFeed** first shows a
menu: **Sign in with PetSafe** (standalone) or **Use an existing Tuya Local entry
(legacy)**. The legacy path behaves exactly as before, including the manual profile
copy. Builds without the bundled app profile show no menu and offer only the legacy
path.

### Sign in with PetSafe (standalone)

1. Install this integration and restart Home Assistant.
2. Add **PetSafe FreshFeed**, choose **Sign in with PetSafe**, and enter the region,
   email and password of the PetSafe app account.
3. Choose the feeder if there are several and confirm. The entry is created
   read-only; enable controls afterwards in its **Configure** options.

Sign-in supports four regions only, by country code: 1 (Americas), 44 (Europe),
91 (India) and 7 (Russia). Accounts in any other region need the legacy path.

The integration signs in to the PetSafe (Tuya platform) cloud read-only and takes the
feeder's device ID, local key and name. It listens passively on the LAN (UDP
6666/6667, up to 8 seconds, nothing sent) for the feeder's broadcast; a broadcast is
only a hint, because it can be forged. If none arrives it asks for a private IPv4
address (10/8, 172.16/12 or 192.168/16; 100.64/10 and IPv6-only LANs are not
supported). Before anything is saved, one authenticated read-only status check over
local protocol 3.5 must succeed with the account's local key; a wrong key, stale
address or forged broadcast fails setup with nothing saved and no command sent. Other
protocols are refused. An existing PetSafe entry for the feeder, or an enabled Tuya
Local entry holding its connection, stops setup. Sign-in is limited to one attempt
per 60 seconds, with no automatic retry; the limit is held in memory per Home
Assistant instance and resets when Home Assistant restarts.

What is sent where: HTTPS to the PetSafe/Tuya mobile API hosts for the selected
region, plus a server-directed API host only if it is on a closed allowlist of Tuya
API domains; outgoing headers are fixed (`User-Agent: petsafe-freshfeed/<version>`).
On the LAN, the read-only status check. No feeder command is sent during setup.

What is stored: the password and the email are never stored. The device ID, local
key, address and protocol are stored in the PetSafe FreshFeed config entry and kept
out of logs and error messages. Home Assistant backups contain that entry, including
the local key, so treat every backup as secret. Sign-in creates a new account session
on the PetSafe/Tuya cloud; whether this signs the phone app out has not been checked.
**Reconfigure** on a standalone entry changes only its address after one read-only
check, keeping the entry, device, entities, history and controls store.

### Migrating a legacy entry

Migration moves a legacy entry to the standalone connection in place, keeping the
PetSafe entry ID, entity IDs, dashboard links, history, schedule state, the existing
device-registry entry and the retained-intent store and binding IDs; the store is
never copied, reset or recreated. It requires Home Assistant Core `2026.10.x` and is
refused on any other Core release. No feeder command is sent. Make a full backup first
(it contains the local key); the key is read once from the selected Tuya Local entry
into the PetSafe entry and never shown.

1. Options: **Connect directly (migrate away from Tuya Local)**. The preview lists
   the Tuya Local raw entities that will retire. Confirming stops PetSafe controls,
   waits for in-flight work and disables (not deletes) the Tuya Local entry.
2. Restart Home Assistant. The restart checkpoint is required: only a different HA
   process that observes the Tuya Local entry still disabled and unloaded may
   continue; otherwise the migration stays fenced and **Continue the migration**
   repeats the checkpoint (re-staging from the unchanged Tuya Local entry if a power
   loss lost the staged copy; a lost host override cannot be guessed, so then only
   cancel remains). Do not edit the Tuya Local entry while a migration is open.
3. Options: **Continue the migration**. An authenticated read-only protocol 3.5
   status check runs first, then the same device moves to PetSafe FreshFeed and the
   connection is checked again. A host name or automatic protocol requires an
   explicit private IPv4 address and protocol 3.5; there is no automatic downgrade.

When the device moves, HA removes the Tuya Local raw entities on it; raw entity IDs
are outside preservation scope. **Cancel the migration** is offered only before the
move and re-enables the Tuya Local entry only if it was enabled before; the entry
then starts as legacy again (also after later edits to the Tuya Local entry) and a new
migration can be started from the options. If
a power loss leaves the journal ahead of HA's delayed registry save, Continue redoes
the same move forward; it is never cancellable once the move began. After the move
recovery is forward-only: the device is never moved back (that would delete PetSafe
entities); a failed check keeps controls stopped and **Continue the migration** can
be repeated, optionally with a corrected address. If the Tuya Local entry was deleted
or changed after the move and the standalone fields were lost, Continue also accepts
the feeder's local key (it is never re-read from a changed or deleted entry). Once
verified, start-up no longer depends on the Tuya Local entry, which may be edited or
deleted. Controls stay fenced at start-up
while a migration is unfinished; a retained uncertain command is never replayed,
released from cached state or erased.

Uninstall eligibility: after completion and a later HA restart that re-observes the
standalone configuration and device ownership, the options offer **Remove the retired
Tuya Local entry**. It removes only that entry; the Tuya Local integration is not
uninstalled. Check that no other entry (including disabled or ignored ones) uses Tuya
Local before uninstalling it. Re-enabling a retired Tuya Local entry is an unsupported
ownership conflict: PetSafe stops its own connection but cannot stop another
integration's.

### Legacy Tuya Local entries

A legacy entry uses the existing authenticated Tuya Local client, not a second LAN connection.
A public-source compatibility allowlist gates receive ownership and transport;
unrecognized loaded code fails closed. Local fixture tests do not establish that
a production runtime is compatible. Native raw controls remain accessible to HA
administrators outside this adapter and are not endorsed critical command paths.
Standalone entries expose no raw writable entities.

### Controls, schedule and history (both entry kinds)

Schedule freshness is `current` only after an actual DP1 report on the uninterrupted
connection, `last_known` after restart/reload/disconnect, or `unknown` if never seen.
Unknown shows an unavailable panel, not an editable empty plan. Last-known schedules
remain editable without an extra confirmation; the card displays last-seen time.
Invalid reports preserve the usable cache and expose an error. Save confirms the
whole vector, preserving order, duplicate/disabled rows and audible flags (10 rows
maximum). A matching eligible device report decoded after the dispatch watermark
verifies the local write, not physical feeding. Uncorrelated exchange responses remain
observations but cannot acknowledge a write. Ordinary asynchronous reports can still
have been buffered before dispatch; sequence ordering does not establish command
causality. The revision check rejects stale drafts at admission but cannot close the
external-writer push-latency race (approximately 1 second in the observed experiment,
not a maximum delay guarantee): there is no device CAS, demonstrated on-demand
schedule read, or cross-app exactly-once guarantee.

Feed, Cancel, Defrost and Refilled require confirmation. Feed is blocked for pending/feeding
DP140, exhausted counter, missing live status, unavailable source, pending or uncertain
intent and transport/storage/integrity faults. From reported idle or failed, Feed sends
the delayed Start command; from reported ready, it sends Feed Now. The adapter refuses
to send if the reported state no longer matches its selected command at dispatch.
A single guarded live ready-state test produced now/feeding reports and a counter
advance. Reports are observations, not proof of command causality or physical feeding.
Defrost also rejects quick-feed pending and active defrost. Missing flags default to
denied in the browser. Raw Cancel and Feed Now commands are not exposed. Every new coordinator re-arms uncertainty after any prepared intent,
even one previously verified or recovered. Nothing is replayed or erased.

Recovery acknowledges the current operation/session **before** collecting new reports
for up to 35 seconds; there is no second confirmation. Old-session requests are rejected.
Missing evidence times out with the barrier retained and allows an explicit retry when
eligible. A fresh DP1 report may not arrive until a genuine subsequent push. Recovery
cannot repair storage/integrity faults, and local clearance is not physical success.

Next meal is a schedule projection in HA's timezone: first strictly future UTC occurrence,
including DST folds and excluding nonexistent gap times. Empty trays suppress it with
`empty_tray`; last-known schedule provenance remains visible. History is HA-observed
DP14/DP150 device reports and HA-observed local DP14/DP145 transitions, not cloud history. Events without explicit source metadata are labeled as HA-observed local transitions, with their DP14/DP145 derivation inferred from the event kind. This label does not distinguish newly recorded transitions from older persisted ones. DP151 retrieval/decoding is unsupported.

All three guarded services require an authenticated Home Assistant administrator
context. Missing, unknown or non-admin users are rejected with `admin_required`;
automations without a user context cannot invoke them.

Synthetic service payloads (use your integration entry ID, never raw text/button services):

```yaml
service: petsafe_freshfeed.save_schedule
data:
  entry_id: synthetic_entry
  entries: [{weekmask: 127, hour: 8, minute: 0, enabled: true, audible: true}]
  base_revision: synthetic_revision_from_schedule_sensor
```

```yaml
service: petsafe_freshfeed.command
data: {entry_id: synthetic_entry, operation: feed, confirmed: true}
# operation is feed, defrost, refilled, or cancel
```

```yaml
service: petsafe_freshfeed.recover
data: {entry_id: synthetic_entry, operation_id: synthetic_operation, session_id: synthetic_session, confirmed: true}
```

The local browser harness allocates a unique `harness/out/parity-*` directory and
preserves existing output. Local B/H/F/V checks are software evidence only: no loaded
production-runtime admission, hardware verification, acoustic-effect check, deployment,
or physical-ingestion success is claimed. Integration version 0.5.0 and card/package
version 0.4.0 are independently versioned.

## Read-only raw device data

`mode: raw` is an opt-in view for selected Home Assistant sensor states whose
meanings have not been verified. Select the `aqara_c1` model for Aqara C1 devices;
`model: aqara_c1` with `mode: raw` is rejected. Raw data uses only the Home Assistant
entities you select and infers no FreshFeed semantics. It preserves the selected model's artwork but
does not infer feeder health, food level, meal counts, temperature, or camera status.
It never renders actions or controls. The card updates when one of the selected entity
states changes; `unknown`, `unavailable`, and a missing entity remain distinct literal
states.

```yaml
type: custom:pet-feeder-card
model: freshfeed
mode: raw
raw_entities:
  - sensor.example_dp_4
  - binary_sensor.example_dp_110
```

`raw_entities` accepts 1–32 unique `sensor.*` or `binary_sensor.*` ids. The card makes
no claim about whether a selected entity is locally or cloud sourced; select verified
local bindings yourself if local provenance matters. Expand the native **Device data** panel to
see literal entity names and states; selecting a row opens Home Assistant's read-only
more-info view. Raw mode intentionally cannot decode data points.
**Data available** means Home Assistant states are present and neither `unknown` nor
`unavailable`; it does not prove recent communication or changed readings. **Partial data**
and **Data unavailable** remain distinct states.

The raw-data header shows the text labels “PetSafe” and “FreshFeed” beside an original
six-compartment feeder-card mark. The mark is original project artwork, not a manufacturer
logo; PetSafe and FreshFeed are trademarks of their respective owners, and the project is
not affiliated with either trademark owner.

### FreshFeed countdown and Cancel

The controls dialog shows the latest reported quick-feed minutes; it does not run a local countdown. HA state updated is a Home Assistant timestamp, not proof of device-report freshness. Cancel requires two taps and is available only while pending and eligible according to the integration. It is not an emergency stop. A submitted command is not proof of physical success: only corresponding device status is shown as observed, and uncertain durable writes are not replayed. One guarded live HA Start followed by one guarded HA Cancel produced cancel/idle reports, with the counter unchanged beyond the reported countdown plus 45 seconds. This is report-based acceptance, not proof of physical motion, ingestion or command causality.
