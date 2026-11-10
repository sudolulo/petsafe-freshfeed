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
| `card/pet-feeder-card.js` | The card: one ES module, no build step, no runtime dependencies |
| `test/` | Unit tests for the pure helpers (`npm test`) |
| `harness/` | Mock-`hass` page plus Playwright checks and screenshots (`npm run shoot`, output in `harness/out/`) |
| `deploy/cards.json` | Reference/demo configurations; not a backup or source of truth for current live Home Assistant bindings |
| `tools/` | General Home Assistant websocket helpers, plus demo-only card installer and interactive screenshot checker |

## Deploy

The HA token comes from rbw at runtime (`tools/with-ha-token.sh`), never from a file.

```sh
npm install --no-fund --no-audit && npm test && npm run shoot
# After backing up the current module and confirming the live Lovelace resource:
scp card/pet-feeder-card.js ha-host:/config/www/pet-feeder-card/pet-feeder-card.js.new
ssh ha-host 'mv -f /config/www/pet-feeder-card/pet-feeder-card.js.new /config/www/pet-feeder-card/pet-feeder-card.js'
# Update only the existing resource URL's cache-buster, then:
tools/with-ha-token.sh node tools/ha-ws.mjs tools/cmds/update-resource.json
```

This updates the module file and its resource cache-buster only; it preserves existing
stored raw-entity bindings. After updating, refresh the browser. No Home Assistant core
restart is needed. Before running these commands against a live dashboard, back up its
current YAML, inspect the exact two existing card slots and resource ID, and verify the
resource URL/version; do not infer live configuration from `deploy/cards.json`. This
repository's command JSON currently names a fixed resource ID and cache-buster, so review
and update those deliberately for the verified live resource.

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
