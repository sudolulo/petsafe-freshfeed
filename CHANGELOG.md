# Changelog

## Integration [0.4.0] - Unreleased (draft)

### Added
- Standalone entries: Tuya Local is not required. A PetSafe-owned local source keeps
  exactly one connection, monitor and lock per feeder through the pinned Python
  requirement `tinytuya==1.20.0`; the loaded TinyTuya is checked against
  `owned_fingerprints.json` and any other version fails closed with
  `owned_source_incompatible`. Critical dispatch keeps the one-attempt, no-resend,
  no-replay guarantees.
- Sign in with PetSafe now creates a standalone entry: one authenticated read-only
  protocol 3.5 status check must succeed before anything is saved; LAN broadcasts are
  advisory. New entries start read-only; controls are a separate options step.
  Reconfigure changes only the address of a standalone entry.
- Explicit, staged migration of a legacy entry (options: Connect directly, Continue,
  Cancel, Remove the retired Tuya Local entry) with a durable private journal and a
  required Home Assistant restart checkpoint. Preserves the entry, entity IDs,
  history, schedule state, device-registry entry and retained-intent store/binding.
  Cancellation only before the registry move (a cancelled entry may migrate again
  later); forward-only afterwards, with an explicit host/local-key forward repair if
  the Tuya Local entry was deleted or changed after the move. Tuya Local raw
  entities on the device retire at the move. Migration is limited to Home Assistant
  Core 2026.10.x.
- Owned settings entities for standalone entries: stainless inserts and meal counter
  switches, a brightness number (0-10) and a quick feed state sensor. They keep the
  entity IDs of the retired Tuya Local raw entities so existing dashboards continue
  to resolve. Writes require a complete, fresh safe status, go through the same
  admission gates as other writes, validate input strictly and fail closed.

### Changed
- HACS metadata minimum `homeassistant` is `2026.10.0`.
- Legacy entries are unchanged and never switch backend on upgrade; `tuya_local`
  stays an optional `after_dependencies` entry only.
- The Tuya Local setup assist and its bundled component profile copy were removed
  (no longer shipped); the manual-install profile example remains for legacy entries.
- Deleting an entry also removes its private migration journal.

### Known limitations
- TinyTuya is shared per Home Assistant installation; a future Tuya Local (or other
  integration) pin of a different TinyTuya version can conflict with this exact pin.
- No physical feeder command has been issued through the standalone connection.

## Integration [0.3.0] - 2026-11-17

### Added
- "Sign in with PetSafe" setup path: a read-only account client signs in once with
  the PetSafe app email and password (one attempt per 60 seconds, no automatic retry,
  closed host allowlist, fixed headers) and returns supported FreshFeed devices with
  device ID, local key and name. The password and email are not stored. Sign-in
  supports four regions only, by country code: 1 (Americas), 44 (Europe), 91 (India)
  and 7 (Russia). The 60-second limit is held in memory per Home Assistant instance
  and resets on restart.
- Prerequisite for the sign-in path: Tuya Local release 2026.10.0 exactly (HACS
  installs the latest release by default; choose 2026.10.0). With another Tuya Local
  version, use "Use an existing Tuya Local entry".
- LAN locator that listens passively on UDP 6666/6667 for the feeder's broadcast to
  learn its address and protocol version; otherwise the flow asks for a private IPv4
  address. It sends nothing.
- Tuya Local setup assist: checks the pinned Tuya Local release on disk and as loaded,
  installs the bundled `petsafe_freshfeed_raw` profile when absent (never overwriting a
  different file), and drives Tuya Local's own config flow to create its entry, which
  alone stores the local key. No feeder command is sent during setup.
- NOTICE attributes the account package's port of algorithms from
  abovecolin/tuya-mobile (revision ade70eff248764757aa177acf9bdd10440ae7394) and
  includes its MIT license text.
- `tools/build-hacs.py --profile-dir` bundles the app profile into the built package
  only; `--release` requires it. Builds without it hide the sign-in path.

### Changed
- With a bundled app profile, setup starts with a menu; "Use an existing Tuya Local
  entry" is the previous path, unchanged, including the manual profile copy.

## Distribution preparation - 2026-11-15

### Added
- Original MIT-licensed feeder branding and deterministic, allowlisted integration
  and card exports for separate public HACS custom repositories. Existing component
  versions remain 0.2.0 and 0.3.0 for their first public distribution.
- Public installation documentation, compatible Tuya source profile example, and
  HACS/hassfest validation workflows. Packaging does not broaden runtime compatibility.

### Fixed
- Freeze the legacy history migration test clock so fixed fixture dates do not age
  outside the unchanged seven-day retention window.

## Integration [0.2.0] - 2026-11-14

### Added
- Reported quick-feed minutes without a local countdown and confirmed pending-only
  Cancel through the durable adapter. One guarded live Start/Cancel test produced
  cancel/idle reports with an unchanged counter beyond the original countdown.
  Reports are observations, not proof of physical success; uncertain writes are not replayed.
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

### Fixed
- Confirmed Feed selects delayed Start from reported idle/failed and Feed Now from
  reported ready; a changed state at dispatch refuses the selected command. One
  guarded live ready-state test produced now/feeding reports and a counter advance.
  Device reports remain observations, not proof of causality or physical feeding.
- Decode the observed four-byte quick-feed reports with a zero trailing byte as
  well as the original three-byte form; unsupported report formats remain unknown.

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
