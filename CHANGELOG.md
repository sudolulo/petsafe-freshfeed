# Changelog

## Integration [0.2.0] - 2026-11-14

### Added
- Local FreshFeed schedule editor and connection-scoped actual-report cache, with
  strict wire validation, revision admission and eligible post-watermark reports.
  Uncorrelated exchange responses cannot acknowledge writes; sequence ordering alone
  does not establish command causality.
- Explicitly provisioned durable retained-intent controls using the existing client;
  fail-closed compatibility/storage gates, restart uncertainty, no replay, and
  current-session recovery collecting fresh reports for a bounded 35 seconds.
- HA-observed DP14/DP150 history with persisted local-transition preservation, planned next meal
  in HA timezone, guarded Feed/Defrost/Refilled controls, and responsive editor.
- Non-destructive isolated local browser output and synthetic parity acceptance.

### Limits
- Local software checks do not establish loaded production compatibility or physical
  success. DP151 remains unsupported; the external-writer push-latency race remains.
  This is the integration release, not a change to npm/card versions.

## [0.3.0] - 2026-10-30

### Added
- Opt-in, read-only FreshFeed mode for displaying selected Home Assistant entity
  names and literal states without assigning data-point meanings or offering controls.
- Raw observations panel with distinct missing, unknown, unavailable, and available states,
  plus responsive visual coverage and live-resource-only deployment guidance.

### Changed
- Existing FreshFeed standard cards now display `Unknown` for absent or unrecognized
  statuses rather than implying readiness. Offline and fault precedence is unchanged.
- FreshFeed `last_fed` now requires a valid, explicitly zoned timestamp at or before the
  current time. Non-timestamp states no longer fall back to `last_changed` and display
  `Unknown` instead. Operators upgrading existing cards must bind a verified last-feed
  timestamp to retain this display; Aqara's `last_changed` fallback is unchanged.
- Raw mode does not verify whether selected entities are local or cloud sourced; operators
  must verify their bindings when local provenance matters.

## [0.2.0] — 2026-10-20

### Added
- Meal tray row for the FreshFeed: one tile per compartment showing which meals are
  eaten, which one is in the serving window and which are still full, with "4 of 6 left" /
  "Last meal" / "Refill needed" and the next meal time under the next compartment. It
  replaces the food gauge when a `counter` or `meals_left` entity is configured.
- `meal_slots` option (1-12, default 6 for `freshfeed`) and the `meals_left` entity key.
- Pure `mealSlots()` helper with unit tests; harness checks for the tray, including
  tile fit inside a narrow card.

## [0.1.0] — 2026-10-20

### Added
- `custom:pet-feeder-card` with SVG artwork for the PetSafe FreshFeed and Aqara C1,
  state-driven LED, food and kibble animations, light/dark theme support and
  reduced-motion handling.
- Two-tap confirmation for feed actions, keyboard focus kept on the armed button.
- In-card controls overlay built from Home Assistant entity rows.
- Pure helpers with unit tests: status and food-level normalisation, Aqara schedule
  parsing, next-meal calculation, relative time.
- Playwright harness with a mock `hass`, behaviour checks and screenshots.
- Websocket tooling to install the cards and verify the live dashboard.

### Fixed
- Controls overlay no longer closes when the masonry view re-parents cards.
