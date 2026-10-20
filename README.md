# pet-feeder-card

A Home Assistant Lovelace custom card for pet feeders, laid out like the Whisker
Litter-Robot card: status header, a large product illustration, round action buttons,
a chip, a food gauge and a metrics footer. Two models have artwork and defaults:

- `freshfeed` -- PetSafe FreshFeed refrigerated wet-food feeder (PFD00-18290)
- `aqara_c1` -- Aqara Smart Pet Feeder C1 (ZNCWWSQ01LM), via Zigbee2MQTT

Feed actions need two taps within 4 seconds unless `confirm: false`. The full
configuration reference is in `docs/card-spec.md`; working examples are in
`deploy/cards.json`.

## Layout

| Path | What |
|------|------|
| `card/pet-feeder-card.js` | The card: one ES module, no build step, no runtime dependencies |
| `test/` | Unit tests for the pure helpers (`npm test`) |
| `harness/` | Mock-`hass` page plus Playwright checks and screenshots (`npm run shoot`, output in `harness/out/`) |
| `deploy/cards.json` | The two cards as configured on the cat view |
| `tools/` | HA websocket helpers: `ha-ws.mjs` runs a JSON list of commands, `add-cards.mjs` installs or replaces the cards, `shoot-live.mjs` checks the live dashboard |

## Deploy

The HA token comes from rbw at runtime (`tools/with-ha-token.sh`), never from a file.

```sh
npm install --no-fund --no-audit && npm test && npm run shoot
ssh ha-host 'cat > /config/www/pet-feeder-card/pet-feeder-card.js' < card/pet-feeder-card.js
# bump the ?v= cache-buster in tools/cmds/update-resource.json, then:
tools/with-ha-token.sh node tools/ha-ws.mjs tools/cmds/update-resource.json
tools/with-ha-token.sh node tools/add-cards.mjs deploy/cards.json   # --dry-run first
tools/with-ha-token.sh node tools/shoot-live.mjs
```

`shoot-live.mjs` only ever presses actions on the FreshFeed demo card, and refuses any
action that does not target a `*_demo_*` helper or a notification.
