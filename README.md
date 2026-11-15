# pet-feeder-card

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
scp card/pet-feeder-card.js ha-host:/config/www/pet-feeder-card/pet-feeder-card.js.new
ssh ha-host 'mv -f /config/www/pet-feeder-card/pet-feeder-card.js.new /config/www/pet-feeder-card/pet-feeder-card.js'
# Update only the existing resource URL's cache-buster, then:
tools/with-ha-token.sh node tools/ha-ws.mjs tools/cmds/update-resource.json
```

Install both module files together; the entry module imports its sibling model module.
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

## FreshFeed integration 0.2.0 (local release draft)

Copy `custom_components/petsafe_freshfeed` to the Home Assistant custom components
folder, restart HA, then add **PetSafe FreshFeed** and select the existing tuya_local
source. In the integration options, explicitly enable controls to provision durable
storage. Ordinary startup never provisions missing storage. Preserve the opaque
store identity in the config entry and its SQLite database together; do not delete,
reset or recreate them to clear uncertainty. Storage/identity faults block writes
and cannot be waived by UI acknowledgement.

The adapter uses the existing authenticated client, not a second LAN connection.
A public-source compatibility allowlist gates receive ownership and transport;
unrecognized loaded code fails closed. Local fixture tests do not establish that
a production runtime is compatible. Native raw controls remain accessible to HA
administrators outside this adapter and are not endorsed critical command paths.

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

Feed, Defrost and Refilled require confirmation. Feed is blocked for pending/feeding
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
# operation is feed, defrost, or refilled
```

```yaml
service: petsafe_freshfeed.recover
data: {entry_id: synthetic_entry, operation_id: synthetic_operation, session_id: synthetic_session, confirmed: true}
```

The local browser harness allocates a unique `harness/out/parity-*` directory and
preserves existing output. Local B/H/F/V checks are software evidence only: no loaded
production-runtime admission, hardware verification, acoustic-effect check, deployment,
or physical-ingestion success is claimed. Integration version 0.2.0 is independent of
the npm package 0.3.0 and card internal 0.2.0 versions.

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
