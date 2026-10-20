# Changelog

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
