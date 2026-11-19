# Development

## Task 12 continuation verification (offline)

### Owner decision and review fixes, 2026-11-19

The owner selected latest actual local DP140, not an epoch-long local history veto. The hub still seeds the latest same-epoch value and sequence without a receipt timestamp; current active or malformed local values deny, and local presence excludes the missing-DP140 exception. Cloud vetoes when local DP140 is absent and recovery-attempt vetoes remain unchanged. Cancel abandonment remains limited to absent local DP140.

Review A: pending → feeding → idle was incorrectly locked out until reconnect. The RED targeted run showed 7 expected failures (four admission/view cases and three dispatch cases), with 10 passing. The corrected targeted run, including unsafe-later and recovery checks, passed 25 cases. Feed idle selects the existing FEED_START command. Review B: the late recovery events lacked the monotonic receipt timestamp and failed during delivery instead of final validation. They now have the five-field report shape and assert the late unsafe value reaches the adapter; no mutation experiment was run.

Exact offline verification with pinned TinyTuya and account harness and `PETSAFE_PINNED_TUYA_LOCAL` unset: backend **589 OK**, HA **1500 passed, 1 skipped**. `git --no-pager diff --check` and strings/en `cmp` succeeded. The first HA full run exposed three outdated history-lockout assertions (1494 passed, 1 skipped); these were updated to the owner decision before the green rerun. No live HA or device testing was performed. Parent rerun (2026-11-19, same environment): backend **589 OK**, HA **1500 passed, 1 skipped**, diff-check and strings/en `cmp` zero. Independent review then corrected the README latest-local wording and removed the obsolete `cancel_observed` test plumbing; afterwards backend 589 OK, `test_write_adapter.py` 628 passed, diff-check zero, and a second independent review found no issues.

This continuation confirmed and fixed the reported-DP140 abandonment defect:
12 new schedule/settings cases (idle/ready/failed, baseline or fresh) failed on
incorrect abandonment before the fix; all 290 service tests then passed.
Abandonment now requires absent local DP140; reported values use the unchanged
strict operation-specific route. The first full HA run also exposed the existing
profile-lookup cancellation/exception password-cleanup tests (2 failures).
Submitted credentials are now removed before the cancellable lookup; the account
flow suite passed 87 tests with its existing isolation-only skip.

Final exact prescribed commands, with all three pins: backend **589 tests OK**
(33.774 s), HA **1503 passed, 1 skipped** (279.35 s). All exit statuses were zero, including
`git --no-pager diff --check` and strings/en `cmp`. With only
`PETSAFE_PINNED_TUYA_LOCAL` unset: backend **589 OK** (30.743 s), HA
**1503 passed, 1 skipped** (230.96 s), both exit zero. No missing-legacy skip.
HA skips the subprocess-isolation-only inner test in the outer run.
Compared with the supplied backend/HA 676/751 passing baseline, totals are -87/+752.
The retirement and port ledger below explains the removed journal (26), borrowed
binding/compatibility (100), and bridge (32 collected HA cases) coverage, plus
flow/migration retirement; subsequent account/cloud/recovery regressions add tests.
These counts do not independently prove every removed assertion was retained.

| Inherited finding | Resolution and evidence |
| --- | --- |
| Schedule state viewing does not invoke a non-polling sensor | Cached HA state reads intentionally do not perform I/O. Clients must explicitly refresh using `homeassistant.update_entity`. Existing schedule tests pass. No periodic reads added. |
| Reported DP140 could bypass missing schedule/settings readback through abandonment | Confirmed by 12 failing operation-specific tests; fixed by requiring no DP140 for abandonment. Strict recovery is unchanged. |
| 26 versus 27 registry rows | Baseline 3678602 sensor setup already adds disabled-by-default `quick_feed_state` beyond the 18 normal sensors. Current frozen fixture covers all 27 rows, not a new runtime entity. Upgrade tests compare row IDs/names/unique IDs/device bindings and retained records across reload/reconfigure; this is synthetic evidence, not a live inventory. |
| Whole-change independent review coverage | Still incomplete: the inherited reviewer did not line-review all changed/deleted files or reconcile every removed assertion. This continuation was explicitly forbidden to delegate and does not claim an independent review of its own fixes. Parent must complete that acceptance gate before release. |

All runs were offline. No staging, commit, live request, deployment, or real
credential build occurred. No background jobs were started by this continuation.
Logs are outside the checkout under `~/.cache/petsafe/task12-*.log`.
The app-dependence ledger below remains applicable: only pairing/firmware need
the app; Fully local unknown schedules cannot be replaced; cloud receipt is not
device age or acknowledgement, rejected/invalid restored sessions need HA reauth,
and an unreported pending quick feed can still result in an extra meal.

## Task 11 documentation and metadata verification

Independent review found cloud evidence still requires fresh local safety reports and
that active or malformed evidence can retain a veto; both READMEs now state those
limits. Review also identified obsolete challenge instructions in the translations;
both matching files now direct users to check their account instead. The local veto's
broader behavior remains the previously recorded parent decision below. No live operation occurred.

Offline checks: public docs 11 tests OK; distribution 26 tests OK; full backend 586 tests OK. Strings/en byte comparison and `git --no-pager diff --check` passed.
Root README 1235 whitespace words; integration README 1057. Historical changelog
entries remain unchanged. These checks do not establish publication or hardware behavior.

## Rollout and rollback (maintainer only)

Deployment and parity checks require separate authorization; none are performed by this guide. Before deployment, make a full secret Home Assistant backup and record the owned entry, entity and device inventory, observation store and complete retained-intent store including SQLite sidecars. Verify the existing owned installation before deploying. Never send a Feed command as an upgrade check.

The historical device identifier namespace `tuya_local` remains identity data, not a dependency; keep the original device leaf and all entity IDs. Owned setting suffix inventory is `-switch_stainless_inserts`, `-switch_meal_counter`, `-number_brightness`, `-sensor_quick_feed_state`; existing owned rows use entry ID and role. The observation store and `.storage/petsafe_freshfeed/<binding_id>/intent.sqlite` must never be deleted or reset to resolve uncertainty.

Only after a secret backup and confirmed owned setup, the release owner may optionally remove specific obsolete `.storage/petsafe_freshfeed_migrations/*.json` journal files manually. Runtime ignores them; this is not a prerequisite. For rollback, stop the integration and restore prior integration files and, only if needed, the matching secret backup. Older releases may consult those journals. Never replay commands or reset retained intent.

## Task 10 integrated cloud verification (offline)

### Final integrated review (Tasks 6-9 and schedule hook)

Three independent read-only reviewers covered: (1) all nine cloud-read
contracts, (2) session/flow, secrets/diagnostics and CloudRead lifecycle,
(3) the DP140 decision table across view, submit, final pre-send, dispatch and
feed command selection. Reviewer 1 reported its pass as partial; reviewers 2
and 3 together cover the remainder. Findings and outcomes:

| Severity | Finding | Outcome |
|---|---|---|
| Important | Fully local left a pending reauth flow open: it could still send the password after disable, and HA deduplication made re-enable reuse that stale flow, which then aborted. | Fixed: Fully local aborts this entry's reauth flows (`config_flow.py`, options step). `test_fully_local_fences_pending_reauth` failed on the open flow before the fix; it now also asserts zero `discover` calls and a fresh flow on re-enable. |
| Important | The adapter started with empty live state, so a DP140 the hub received this epoch before adapter construction was treated as never reported; fresh 4/142/145 then opened the missing-DP140 exception (feed allowed while a reported pending countdown existed). | Fixed: `hub.reported_dps(epoch)` exposes the latest same-epoch DP140 value and sequence; the adapter seeds it in the same synchronous turn as subscribing, with no receipt time; the latest local value selects the strict predicate. `test_local_dp140_reported_before_adapter_blocks_missing_dp140_exception` (3 cases) failed with Feed allowed before the fix. |
| Medium | Restoring a stored session loaded the contract file on the event loop during hub construction (HA blocking-call warning). | Fixed: construction performs only a structural check (`account.stored_session_shape_valid`); full `restore_session` runs in the executor on first need. `test_entry_reader_restores_contract_off_the_event_loop` failed before the fix. |
| Medium | A read completing between Fully local persisting and `close()` could publish evidence and a cloud veto. | Fixed: Fully local invalidates the reader before updating the entry. `test_fully_local_fences_read_completing_before_close` failed (listener called) before the fix. The listener-triggered reload also discarded such state. |
| Owner decision (2026-11-19) | Use the latest actual local DP140 for the strict L row; historical local pending/feeding/malformed reports do not veto a later safe value. | Implemented: current active or malformed local values still deny; a reported DP140 still excludes the missing-local exception. Recovery-attempt vetoes and the cloud veto for absent local DP140 remain separate. |
| Important (re-review) | A refused second drain in `hub.async_stop` left the cloud reader closed after `resume_after_failed_unload`. | Fixed: `async_stop` now calls `CloudRead.async_suspend()` (refuse demands, invalidate, drain actual I/O) and `close()` only after the second drain succeeds; `resume_after_failed_unload` calls `CloudRead.resume()`, which never revives a closed reader. `test_refused_second_shutdown_drain_resumes_cloud_reader` and `test_suspend_drains_and_refuses_then_resume_restores_reader` failed before the fix. |
| Superseded re-review | The earlier review requested historical local DP140 veto tracking and cancel release. | Removed under the owner decision; hub seeding retains only the latest value and sequence, without freshness time. |
| Minor observations retained (not fixed by this documentation pass) | Reauth success uses `async_update_reload_and_abort` while an update listener also reloads; a recovery-collection schedule refresh may add a cloud veto (conservative); negative cloud age (clock anomaly) is treated as expired; a listener exception in `CloudRead._run` propagates to waiters; 35/60 s literals in `source_operation_decision`. | These are residual review observations, not implemented corrections or failed tests. This ledger correction does not claim their resolution or new reviewer approval. |
| Important follow-up | Dismissing a reauth flow could leave later demand unable to request sign-in again. | Current regression `test_dismissed_reauth_is_requested_again_on_later_demand` verifies a later eligible demand starts a new flow, while demand with an active flow does not duplicate it. Included in the fresh full verification below. |
| Not confirmed | Recovery awaits cloud demand before the 35 s window: it does not consume or restart the window. Local abandonment is Task 5 scope, not cloud. | No change. |

C-row coverage follow-up:

| Severity | Finding | Outcome |
|---|---|---|
| Important | Adapter edge-case coverage used only absent cloud evidence, leaving the valid-cloud C rows untested for the same cases. | Extended `test_fresh_local_edge_cases_at_admission_and_dispatch` to absent/idle/ready/failed cloud evidence: bool DP142/145 feed denial, raw no_food denial for all four operations with valid cloud (versus the retained non-feed E allowance), feed counter6 denial and refilled counter1 denial. Added safe positive controls for feed/defrost/refilled/settings for each source and exact FEED_NOW versus FEED_START assertions. All 48 cases check admission and dispatch; 40 cases are new. No production change was needed. |

Fresh verification for this test-only follow-up, with the three prescribed pins:
`python3 -m pytest test_ha/test_write_adapter.py -q --asyncio-mode=auto -p no:cacheprovider`
reported **631 passed in 76.03 s**, no skips. The added coverage passed against
unchanged production code; no production RED/GREEN cycle is claimed.
`git --no-pager diff --check` also passed after the ledger edit. Full-suite
counts below are prior-run evidence, not a rerun for this follow-up.
`test_cloud_ready_feed_command_is_rechecked_at_dispatch` retains its explicit
cloud-ready expiry and local idle/pending replacement coverage.

Schedule hook properties are covered by `test_ha/test_schedule.py` (shared budget, Fully local no network/Repairs/reauth, local precedence).

Prior full-tree verification for the earlier ledger correction (supersedes the earlier
1428-pass run): backend **575 tests OK** (30.196 s); HA **1444 passed,
1 skipped** (234.37 s). That HA collection was 16 cases larger than the previously
recorded 1428-pass run; that documentation-only pass added or removed no tests.
The existing HA isolation-only subprocess fixture skip remains; no new skips. All four commands below passed with the
three prescribed pins, without disabling logging:

```bash
# Set PETSAFE_PINNED_TUYA_LOCAL, PETSAFE_PINNED_TINYTUYA_SITE and
# PETSAFE_ACCOUNT_HARNESS to your local test dependency paths as needed.
python3 -m unittest discover -s test_backend -q
python3 -m pytest test_ha -q --asyncio-mode=auto -p no:cacheprovider
git --no-pager diff --check
cmp custom_components/petsafe_freshfeed/strings.json custom_components/petsafe_freshfeed/translations/en.json
```

Diff-check and strings/en comparison were repeated after the ledger edit and
passed. That earlier ledger-only pass changed no production or test code; the
C-row follow-up above changes tests and this ledger only. No live requests,
credentials, device commands, commits or deployment were used. Owner-supplied
LAN/cloud evidence below remains supplied-only; passing offline tests do not
certify hardware behavior or resolve the residual observations above.

### Independent integrated review and follow-up

| Severity | Finding | Outcome |
|---|---|---|
| Important | A timed-out caller did not fence a coalesced request while another waiter remained. | Fixed: any caller timeout invalidates the shared generation; actual I/O still drains. Regression failed on published DP1 before the fix and passed afterwards. Cancellation of one of multiple waiters continues to preserve the other waiter. |
| Important | A read-only entry had no initial/reconnect DP140 demand because it has no write adapter. | Fixed with a nonblocking, LAN-first read-only connection demand using the same reader and budget; owned-hub regression failed waiting for the initial request before the fix. |
| Not confirmed | Pending, idle, then pending was claimed to prevent Cancel. | The veto condition applies only when the current status is idle, ready or failed; current pending does not satisfy it. Other gates still apply. |
| Residual limitation (not a verified server failure) | A syntactically valid but incorrect stored ecode cannot be detected by structural restore; a server response not recognized as a session rejection can leave cloud reads unavailable without reauth. | Recognized session rejection and structural restore failure are covered separately; unknown response/decryption failures remain fixed malformed/unavailable outcomes, not credential rejection. No live evidence establishes the server response to such corruption. This documentation pass does not claim this limitation was fixed. |

Fresh full offline verification with all three prescribed pins: backend 575 tests
OK (26.858 s); HA 1407 passed, 1 skipped (192.82 s). `git --no-pager diff --check` and strings/en
`cmp` passed. This earlier run does not establish live device behavior. No live requests or credentials were used.

### Takeover finding resolution and post-fix verification

| Severity | Review finding | Resolution and regression evidence |
|---|---|---|
| High | Timed-out cloud I/O kept command admission busy | Separate valid-result dispatch fencing from actual I/O lifetime. Timed-out/cancelled generations no longer block local fallback or rejoin a stale task; actual work remains tracked and drained. The fallback regression failed on `admitted` before the fix and passes afterwards. |
| Medium | Failed initial DP1 read had no later schedule-specific demand | Explicit schedule sensor refresh (`homeassistant.update_entity`) and schedule edit admission demand missing DP1 using the shared cooldown. Edit regression failed with zero fetches before the fix. A failed refresh followed by a later refresh at 60 seconds succeeds. |
| Medium | Connection proactively demanded DP1 | Removed hub connection DP1 demand. Connection DP140 demand remains in the adapter and may incidentally return DP1. Regression initially failed because schedule was already last-known before refresh. |

The sensor remains non-polling: reading cached HA state does not issue network
requests. Its explicit refresh hook supports schedule-view clients using
`homeassistant.update_entity`. Schedule edit demand is wired independently of
whether DP140 was already observed. Existing deferred-result/local-precedence
regressions now explicitly initiate their schedule read rather than relying on
the removed proactive connection request. No safety assertions were deleted.

Full commands with all three plan pins: backend 575 tests OK
(26.844 s); HA 1405 passed, 1 skipped (182.87 s). `git --no-pager diff --check` and strings/en `cmp` passed.
With only `PETSAFE_PINNED_TUYA_LOCAL` unset: backend 575 OK (25.831 s), HA
1405 passed, 1 skipped (193.08 s). No commits, live operations or credentials were used.
The supplied independent review was targeted, not exhaustive. Per takeover
instruction no additional reviewer was delegated; these corrections were
self-checked against the three findings and the full offline suites.

### Prior seat evidence (pre-fix, retained for audit)

Verification used the three pins in the implementation plan. The exact full
commands produced: backend `python3 -m unittest discover -s test_backend -q`:
575 tests OK (26.532 s); HA `python3 -m pytest
 test_ha -q --asyncio-mode=auto -p no:cacheprovider`: 1404 passed, 1 skipped
(213.98 s); `git --no-pager diff --check` and strings/en `cmp` both passed. The HA skip is
an isolation-only fixture run in a subprocess. No new skip was introduced by this verification. With
`PETSAFE_PINNED_TUYA_LOCAL` unset and the other two pins retained, the same full
backend command passed 575 tests (25.980 s) and the same HA command passed 1404
with 1 skipped (209.00 s).

No live feeder, HA host, credentials, cloud account or command was used.

Owner-supplied observations, not reproduced here: seven read-only LAN probes
returned no DP1/140; UPDATEDPS yielded empty acknowledgements without a push.
The owner-run cloud `s.m.dev.dp.get` v1.0 returned DP1 and DP140 with identical
selected-device gwId/devId; no recognized per-DP observation timestamp was
exposed. Receipt time does not prove device freshness or strict recovery idle.
These offline tests do not certify hardware behavior.

## Task 6 account seam handoff (offline, pending independent parent review)

Public interfaces for Tasks 7–9, all from `petsafe_freshfeed.account`:

- `discover(SignIn, profile=...) -> Discovery`: existing discovery labels and
  feeders, plus `Discovery.session: SessionHandoff` on success.
  Password is cleared in the existing finally block; uid/email/password are
  not retained in the handoff. Discovery/SafeResult formatting carries no secrets.
- `SessionHandoff.storage_data()` explicitly exports only `{sid, ecode}` for
  HA storage; `storage_route()` exports country_code, region, canonical url and
  api_domain. These mappings must never be logged. HA storage is plain JSON.
- `restore_session(stored, route) -> SessionHandoff | None`: None means signed
  out. Requires both bounded printable secret strings and exact canonical
  boundary-approved route metadata; no request is sent. Structural corruption
  (missing/type/empty/control characters/oversize) is locally detectable; a
  syntactically valid wrong secret cannot be detected offline and must be
  rejected by the server. No invented cryptographic validity test or TTL.
- `read_dps(session, device_id, profile=..., deadline_s=10) -> DPRead`: caller
  passes its selected entry's device_id, used identically as gwId/devId; one
  closed DP_GET, no discovery/retry/write. Result has fixed `outcome` (`ok`,
  `session_rejected`, `unavailable`, `malformed`), `dp1` (optional string,
  maximum 4096 characters), decoded `dp140` and `dp140_present`. Present invalid
  DP140 remains a decoded unknown plus presence flag, so the helper must latch
  a malformed safety veto rather than treating it as absent. Other malformed
  responses fail closed. No raw payload or reported cloud timestamp is exposed.
  Only a recognized session rejection requires reauth; other server errors
  are labelled `malformed`. Error details are not logged or returned.

The caller owns selected-device binding, session deletion/reauth, cooldown,
coalescing, executor drain and epoch/source provenance. These are Tasks 7–10,
not account functionality. DPRead is never a LAN report or acknowledgement.
Synthetic encrypted restart tests use the actual AccountSession derive/seal/
decode path with a fake server, not real credentials or endpoints. Supplied
owner LAN/cloud observations in the design remain supplied-only evidence.

RED reproduced the missing restore_session assertion (3 tests, 1 failure).
A second RED caught session rejection being labelled malformed; GREEN maps
recognized session rejection to the fixed reauth outcome. Existing
boundary/profile/contract DP_GET edits from the previous seat were retained.
Added synthetic server operation, malformed/redaction/route/secret-shape/final
wire tests and actual-profile login-to-handoff-to-restart test. No tests removed.
Final verification with the plan's three pinned environment exports:
`python3 -m unittest discover -s test_backend -p 'test_account_dp_get.py' -v`
11 tests OK (0.124s);
`python3 -m unittest discover -s test_backend -p 'test_account_*.py' -q`
285 tests OK (10.438s);
`python3 -m unittest discover -s test_backend -q`
573 tests OK (24.787s), no skips. A targeted discovery validation regression
also passed (1 test, 0.065s); its pre-fix run failed on all six invalid-secret
cases.
`git --no-pager diff --check` and strings/en `cmp` passed.
The HA suite was not rerun for this account-only task. Independent review
was not dispatched: the takeover explicitly prohibits delegation. Parent must
perform that separate review; this is not a claim of independent certification.

## Task 6 partial account-boundary verification (not release acceptance)

The continuation reproduced the pinned account suite failure: 274 tests,
1 failure in `test_contract_matches_pinned_source_anchors`. The shipped
contract deliberately adds DP_GET, so byte identity with the older pinned
harness is no longer the contract. The test now compares the entire parsed
harness document plus exactly three approved changes: the closed DP_GET row,
the exact device_id value-kind description, and removal of DP_GET alone from
the unsupported-operation prose. Source hashes/line anchors and the deliberate
vendor-source mutation rejection remain checked. No external harness was
modified and no source-anchor assertion was removed.

With the plan's pinned environment, targeted DP_GET: 2 tests OK (0.002s);
account suite: 274 tests OK (9.877s); full backend: 560 tests OK (22.971s).
The first HA invocation timed out at 120 seconds; the complete rerun finished
745 passed, 1 skipped (171.07s). Diff whitespace and strings/en comparison
passed. These are current partial-tree regression results, not Task 6
acceptance: session persistence/restoration, runtime DP read orchestration,
Fully local/reauth, cloud schedule provenance and the source-aware admission
decision table remain unimplemented. The two DP_GET tests do not constitute
wire/session lifecycle coverage. No independent review or live operation was
performed by this continuation; no commit was made.

## Task 5 recovery protocol and app-dependence ledger

Recovery never sends a command or guesses elapsed-time safety. The existing
owned monitor uses empty-object DP_QUERY_NEW (16) every 30 seconds and heartbeat
(9) every 5 seconds, within the 35-second collection window. No early query,
UPDATEDPS, second client, retry or witness schema change was added. A heartbeat
alone cannot satisfy the fresh safety triple. The unsigned reported minutes
byte represents 0..255, not a proven firmware maximum. The durable witness has
only operation_id/operation/state, no timestamp; external age of case c3f18021
cannot prove execution or idle.

Strict recovery can combine a frozen actual same-epoch safety baseline with
at least one fresh report. Schedule DP1 and known setting echoes remain fresh
requirements. Explicit local abandonment instead needs fresh raw 4/142/145;
missing DP140 stays unknown, and any reported DP140 retains the operation's
strict predicate. Every active/invalid safety report vetoes the entire attempt,
even if followed by safe status. Both outcomes retain durable intent and use
the existing final flush/reconcile gates. Only successful reconciliation emits
the exact operation/session resolution; abandonment is execution unknown, not
an acknowledgement. New command gates are unchanged in Task 5.

| Path | Offline decision |
| --- | --- |
| Feed/cancel/defrost/refilled recovery | Recovery-only raw no_food is accepted. Explicit abandonment can remove local uncertainty without pretending a never-reported DP140 is idle. |
| Schedule recovery | Missing DP1 may use abandonment; no installed-plan claim, no read-model mutation. |
| Settings acknowledgement | Exact eligible local target-value echo remains required; abandonment is not setting verification. |
| New commands with unknown DP140 | Still denied in Task 5; Task 6 owns the separately approved fresh-local exception and its extra-meal warning. |
| Never-observed schedule viewing/editing | Unknown remains unknown; Task 6 owns labelled cloud reads. No replacement workflow. |
| Setup/address repair | Integration account discovery, passive locate and authenticated read-only probe need no running official app after pairing. |
| Official app | Only first Wi-Fi pairing and firmware updates are accepted dependencies. |

Owner-supplied evidence, not repeated here: seven LAN reads (three DP_QUERY_NEW
variants and four UPDATEDPS variants) did not return DP1/140; command18 gave
empty acknowledgements without push. Owner-run cloud s.m.dev.dp.get v1.0 with
exact gwId/devId succeeded and included DP1/140, unlike DEVICES. No per-DP
observation timestamp was exposed; receipt time cannot prove fresh recovery
idle. Task 6 alone implements that read boundary and session lifecycle.

Task 5 full verification rerun after the test-only refinement, with the plan's
pinned environment: backend 558 OK (24.641s); HA 734 passed, 1 skipped
(154.99s). The refinement uses
the fake device's full-status monitor for all six abandonment cases rather than
injecting reports directly. Earlier targeted Python recovery/upgrade/settings/
cancel run: 340 passed (74.52s).
Diff whitespace and strings/en byte comparison passed on this rerun. Initial
RED proved no_food denial, unsafe-then-safe erasure,
missing baseline/abandonment;
a later RED proved duplicate/reordered reports were silently ignored during
recovery. Full-suite failures exposed two exact-view fixtures needing the new
nullable field and a test-invoked expiry timer that needed explicit cancellation;
all were corrected without weakening existing assertions.

No Task 5 test was removed or weakened. Existing exact-view fixtures gain only
the nullable resolution field. Independent review must be supplied by the
parent: this delegated continuation explicitly forbids subagent dispatch.

## Retired profile protocol evidence (Task 4)

The removed raw profile declared DP4 only as a string and DP140 as optional;
absence of DP140 is unknown, never idle. The partial, coverage-capped schema
captured 2026-10-24 listed DP4 standby/feeding/done/last_meal and unresolved
`enum_4` through `enum_10`; these unresolved values are not safety evidence.
The owner separately reported raw `no_food`; synthesized empty status is not
that report. DP140 raw bytes encode command/status/unsigned minutes, but 255
representable minutes is not a firmware execution-duration bound. DP1 is raw
meal-plan data. Unknown DP1 is not an empty schedule. DP142 and DP145 schema
integer ranges do not prove their operational safety domains.

Owner-run 2026-11-18 protocol-3.5 observations (not repeated here): status and
experimental queries returned DPs 4,14,110,111,120,125,130,131,142,145,150 only.
DP140 and DP1 were absent; UPDATEDPS returned empty acknowledgements without
push. Production continues to use full empty-object status (16) and heartbeat
(9), not targeted queries or UPDATEDPS. Cloud DP_GET evidence is separate:
receipt time is not device observation time. No private payload is retained.

Task 4 retires `test_source_binding.py`, `test_tuya_compat.py` and
`test_tuya_bridge.py`: borrowed registry/client/provenance behavior only after
the shared safety ports listed below. Pure signature tests remain in
`test_owned_code_signature.py`; transport and socket/drain tests use owned
fixtures. The unused `legacy_test_support.py` and `loaded_source` fixture are
retired; cutoff tests do not require a live borrowed client.

### Task 4 verification and retirement audit

- Distribution RED: 26 tests, 5 failures (retired output/source allowlists).
- Standalone RED: 3 tests, 9 failures (retired files/imports/constructor).
- Initial full backend: 556 tests, 4 errors exposed an unported
  `RestartTests.engine` constructor in `test_sqlite_witness_restart.py`; ported
  to public owned api/lock/current contract without changing restart assertions.
- Initial HA: 638 passed, 7 failed, 1 skipped, 1 teardown error. Fixed stale
  attach method names, absent raw-discovery attribute expectations, unknown
  unsupported-backend diagnostics and fingerprint comparison to a retired file.
  Fingerprint assertions now freeze all 14 original owned rows exactly.
- Extended AST test initially mistook HA recorder's own `migration` import for
  our retired module; scoped the import check to this integration.
- Final distribution command: 26 tests OK (7.782 seconds).
- Final standalone command: 5 tests OK (0.776 seconds), including AST inventory
  of surviving tests/tools and runtime import closure.
- Final full backend: 558 tests OK (25.062 seconds).
- Full HA after runtime/fixture fixes: 645 passed, 1 pre-existing skip
  (154.37 seconds). Subsequent changes were backend contract tests/docs only.
- `git diff --check` and strings/en byte comparison passed.

Backend count: Task 3 653 minus 100 exclusively retired tests plus 5 standalone
contract tests = 558. HA count: 677 minus 32 borrowed-bridge collected cases =
645; no new skips. Retired source-binding classes: BindAcceptTests,
BindRejectEntryTests, BindRejectSourceTests, BindRejectLeafTests,
IsCurrentDriftTests, LoopOwnershipTests, ModuleSurfaceTests. Retired compatibility
classes: SyntheticSource, ExecutableProvenance, PinnedWrapperAcceptance,
OwnedHooks, ControlFlow, Logging (pure helper assertions already ported).
All of `test_tuya_bridge.py` is retired; its shared socket/receive/dispatch
assertions have the owned replacements in the preceding coverage ledger.

Self-review: retained report/store/coordinator/drain logic, exact owned leaf
helpers and historical namespace, enabled competing-source checks, unchanged
owned fingerprints and NOTICE mobile attribution. Build remains an explicit
allowlist. Independent review is not claimed: this delegation explicitly
prohibits dispatching another agent; the parent must perform that gate.
No staging, commits, live operations or real-credential builds occurred.

## Task 3 standalone setup and flow retirement

| Retired coverage | Replacement or reason for retirement |
| --- | --- |
| `test_ha/test_migration.py` (entire file, 46 test functions with parameterized cases) | Exclusively conversion/preflight/prepare/resume/cancel/source-removal, takeover and journal admission. Removed with the feature. Already-owned identity is covered by `test_owned_upgrade_051.py` and `test_owned_hub.py`; malformed/unreadable leftovers and absent/changed historical sources are covered by `test_standalone_only.py`. |
| `test_backend/test_migration_journal.py` (entire file, 26 tests) | Exclusively journal persistence/reducer behavior; no owned runtime depends on it. Retained-intent SQLite coverage remains in the witness/bootstrap/transport suites. |
| `test_config_flow.py` borrowed picker, source profile/identity and user-time provisioning cases | Legacy setup no longer exists. Rewritten as owned options provisioning tests: decline is read-only, explicit provisioning once, failure preserves data, partial directory never adopted, ineligible leaf refuses, cancellation drains filesystem work without persisting IDs and later attempts refuse the reservation. Missing profile now aborts without fallback. Package lazy-import assertion retained. |
| `test_config_flow_account.py` existing-source menu cases | Direct account form despite existing sources, absent profile abort, profile caching/executor and subprocess isolation assertions retained. All sign-in/privacy/probe/cooldown assertions retained. |
| `test_config_flow_owned.py` legacy picker duplicate case | Account duplicate rejection with competing source; owned provisioning/host transition/unload-failure checks retained. Legacy reconfigure now expects permanent cutoff. |
| `test_repairs.py` migration-priority cases | Non-owned cutoff precedes protocol issue changes, with and without existing issues. Owned failed retiring drain, protocol issue, failed setup and timer lifecycle assertions retained. Removed only migration-admission parameter of failed-start test. |
| `test_owned_hub.py` journal-fenced historical entry and legacy default-start | Historical fields no longer fence owned setup; same leaf/entity identities survive. Non-owned setup is permanent failure with no hub construction. Leaf/conflict checks remain. |
| `test_owned_settings.py` legacy setup without writable entities | Cutoff denies setup and creates no setting entities. All owned setting safety tests unchanged. |

No surviving Python test imports either deleted provider (recursive source grep).
Task 2's neutral owned fixtures and transport/provenance ports were inspected before retirement.
The prior Task 2 baseline was backend 679 and HA 755 passed / 1 skipped;
Task 3 final counts are backend 653 (26 journal tests retired) and HA 677 passed /
1 skipped. The HA net decrease of 78 includes migration/picker retirement and new
cutoff/owned provisioning regressions; no owned safety test was disabled or skipped.
The existing account subprocess-isolation-only skip is unchanged.

Verification with all three plan environment pins:
- Initial exact targeted command: 21 failed, 118 passed, 1 skipped (obsolete menu and migration mocks).
- Extended targeted port run: 5 failed, 160 passed, 1 skipped; corrected fixtures to patch the persisted-leaf lookup path and use the actual `country` form key.
- Initial full HA: 1 failed, 676 passed, 1 skipped (last legacy-settings setup expectation).
- Final exact targeted command: 139 passed, 1 skipped, 25.89 seconds.
- Final `python3 -m unittest discover -s test_backend -q`: 653 tests OK, 24.619 seconds.
- Final exact full HA: 677 passed, 1 skipped, 153.91 seconds.
- Strings/en byte comparison and `git diff --check`: passed.

Self-review checked cutoff ordering, retained failed-setup drain, host-transition-only
reload suppression, data merges, and removal without journal access. Independent
review remains the parent's responsibility because this continuation prohibits
subagent delegation. No production changes were needed in this continuation;
prior workers' setup/flow changes were exercised, not restored to satisfy old tests.
No staging, commits, live operations or credentials were used.

## Test coverage ledger for standalone transition

The borrowed transport and migration tests are not substitutes for owned-path safety coverage. Before retiring their fixtures, record the old assertion and its owned replacement here; assertions unique to removed behavior must be identified separately. The shared ports below are complete. Legacy-only providers remain until their callers are retired in Tasks 3–4; parent independent review is still required before deletion.

| Original coverage | Standalone disposition |
| --- | --- |
| `test_backend/test_tuya_compat.py::ExecutableProvenance::test_signed_zero_and_compound_constants_are_distinct` | `test_backend/test_owned_code_signature.py::OwnedProvenanceOwnershipTest::test_exact_constant_and_code_semantics` checks signed zero and compound constants, plus nested code and flags. |
| `test_ha/test_executable_compat.py::test_slice_constants_are_attested_recursively` | Same assertion now imports the owned provenance implementation. |
| `test_backend/test_critical_transport.py` TransportTests, DispatchHookTests, witness-model tests, ShutdownTests | All constructor calls now use `async_create_owned` and public `Owner.api`/`Owner.lock`; 89 tests retain send, persistence, drain, cancellation, byte-bound and logger assertions. The borrowed constructor signature/re-export assertions are replaced by owned signature and neutral report immutability contracts. |
| `test_ha/test_write_adapter.py` (49 tests) | Real OwnedSource and pinned TinyTuya over FakeDevice protocol 3.5. Decoded CONTROL frames replace synthetic byte counters; socket restoration faults replace vendor-method monkeypatches (which correctly fail provenance). Cancellation is injected at the real dispatch boundary. |
| `test_ha/test_write_lifecycle.py` (10 tests) | Owned registry leaf, real source, protocol-3.5 device and unchanged SQLite provision/open contract. Foreign borrowed API replacement becomes owned retirement/reconstruction with a new API; retained-intent and no-replay assertions remain. |
| `test_ha/test_cancel_safety.py` | Uses the owned adapter fixture, public provenance method and persistent fake disconnect; command/service latch assertions remain. |
| `test_ha/test_attach_retry.py`, `test_claim_retry.py`, `test_unload_refusal.py` | Owned source start/claim/stop seams replace borrowed attachment. Start retry uses a shortened test-only OWNED_RETRY; stale-start gate is after real admission so parked drain exercises a live owned client. |
| `test_ha/test_parity_acceptance.py` | Real protocol CONTROL payloads and post-send reports, retained SQLite records across reloads, observation store and stale revision/session assertions remain. |
| `test_ha/test_owned_source.py::test_socket_hook_parity_with_legacy` | Replaced by `test_owned_socket_hook_contract`: explicit ordered report/connection sequence, six epoch advances, DP allowlist, ack eligibility and hook cleanup. No legacy implementation comparison. |
| `test_ha/test_feeding_statistics.py` | Real owned hub/device fixtures now exercise all persistence, corrupt/unsupported-store preservation and recorder assertions. Removed only the `legacy` parameter of the duplicate new-dispense test; the owned path no longer toggles `_owned` on a borrowed hub. 61 tests pass. |
| `test_ha/test_entity_hygiene.py` | Owned setup preserves pre-existing registry names/IDs, nonpolling and nondeprecated lookup assertions. Exact inventory increases from borrowed 23 to owned 27 (existing runtime inventory); three tests pass. |

### Remaining shared ports (Task 2 continuation)

| Original coverage | Owned replacement / exclusively retired behavior |
| --- | --- |
| `test_hub.py` observation, dispense/refill/skip, unavailable, restart, schedule cache, list/v2 history loading, missing cache, tick, unload, no service calls, last-fed restore, forward failure, remove and reload tests | Same assertions on `owned_hub` and protocol-3.5 status frames. Owned receive also produces the DP14 `meal_report`, asserted explicitly. Disconnect replaces raw `unavailable`; no report-driven history is invented on reconnect. |
| `test_hub.py::test_missing_required_raw_not_ready`, `test_entry_setup_retry_when_required_raw_missing` | Exclusively retired raw registry admission (2 tests removed). Owned missing-DP availability remains in `test_owned_hub.py::test_owned_missing_required_dps_unavailable`; raw registry absence is normal for owned. |
| `test_entities.py` all 6 tests | Owned setup/status frames/disconnect; same values, history, statistics authority and identity assertions. Borrowed 23-row inventory becomes existing owned 27-row registry inventory (26 enabled plus disabled quick-feed diagnostic). `test_no_writable_platforms` becomes explicit three owned settings identities, unavailable command path without witness and unchanged three-service inventory. |
| `test_history.py` all 10 collected cases | Owned setup, retained store and direct observation classification; reconnect baseline replaces borrowed client replacement. Startup already reports DP14, so explicit new-epoch baseline uses actual epoch+1 rather than hardcoded epoch2. DST, retention500, expiry, schema, no-double-count and empty/unknown/disabled plan assertions retained. |
| `test_schedule.py::test_loaded_receive_not_optimistic_state`, `test_schedule_only_reports_promote_and_disconnect_demotes` | Same sensor and schedule assertions via owned protocol receive. |
| `test_schedule.py::test_queued_report_survives_reconnect_as_last_known`, `test_nonpersistent_close_before_cache_retains_last_known` | Owned hook queue ordering tests `test_queued_report_survives_disconnect_as_last_known`, `test_close_before_cache_retains_last_known`; actual queued cache/close hooks, not invented sensor state. |
| `test_schedule.py::test_api_replacement_demotes_without_report`, `test_source_unload_clears_quick_feed`, `test_source_reload_reattaches_new_client` | Owned socket reconnect demotes schedule, disconnect clears quick-feed, restart rejects old-client reports and removes hooks. |
| `test_schedule.py::test_registry_removal_invalidates_before_lock_released` | `test_unload_invalidates_before_lock_released`: public owned lock held while stop invalidates owner; queued report cannot replace schedule and drain completes after release. Raw schedule registry removal itself is retired. |
| `test_schedule.py::test_unload_during_attachment_detaches_result`, `test_unload_during_reattachment_detaches_result`, `test_source_unload_detaches` | Borrowed attachment triggers retired; owned equivalents already ported in `test_attach_retry.py::test_pending_stale_attach_after_refused_unload_eventually_recovers`, `test_unload_refusal.py` drain/detach refusal, and `test_owned_hub.py::test_owned_detach_refusal_keeps_group_parked`. No additional duplicate cases retained. |
| `test_schedule.py::test_schedule_registry_binds_profile_suffix_on_selected_device`, `test_raw_rename_keeps_state_listener`, `test_wrong_device_schedule_not_bound`, `test_invalidated_binding_not_reattached_on_reload` | Exclusively borrowed raw registry/attachment behavior removed (4 cases). Owned identity/conflict admission remains in owned-hub and frozen upgrade suites. Total schedule collection decreases 15 to 8. |
| `test_write_adapter.py::test_hub_without_adapter_is_unavailable_and_stops_adapter_before_detach` | Last borrowed setup now real owned hub; exact adapter/transport/witness/source stop ordering remains. |
| `test_repairs.py` | Default synthetic entry now explicitly owned/3.5. Unit Repairs timer/issue tests require no borrowed setup. Migration admission cases intentionally remain for Task 3 retirement. |
| `owned_test_support.py`, `test_owned_source.py` | Neutral frame/wait helpers, public `Owner(api, lock, is_current)` and extracted protocol `FakeSocket`. Condition timeout now raises on an actual timed-out wait even under frozen monotonic time, avoiding an infinite executor recv in freezer tests. No vendor/provenance behavior changed. |

`legacy_test_support.Api` is isolated only for `conftest.loaded_source` and
exclusively migration tests; delete it with those callers in Tasks 3–4.
No surviving port imports `test_tuya_bridge` or `test_tuya_compat`.
No exact-payload write assertion was reduced to a count.

Count reconciliation versus the handed-off tree: hub -2, schedule -7,
new neutral-helper contract +1 = HA -8 (762 to 754 passing). No new skips.
Independent review was not delegated because this continuation explicitly
prohibits subagents; parent must supply that review before fixture deletion.

### Task 2 continuation verification

With the plan's three environment pins, the exact quiet full backend run passed
679 tests; the exact quiet full HA run passed 762 tests with 1 pre-existing skip
in 344.62 seconds. The eight-file required targeted HA command passed 243 tests.
The additional verbose full HA diagnostic run failed 49 tests (713 passed,
1 skipped): DEBUG logging made transport refuse `unsafe_logging`; this is not
a passing run and logging guards were not weakened. Two earlier quiet runs
were cut off at 120/300-second command deadlines; the 600-second quiet run
completed. After the statistics/hygiene ports, a fresh quiet full HA run passed
762 tests with 1 skipped in 334.76 seconds, the required eight-file targeted
command passed 242 tests, and the full backend run passed 679 tests. These
results do not satisfy the remaining fixture retirement gate above. No live
operations, staging or commits were performed.

### Final Task 2 continuation checks

- New helper contract RED: 1 failed on `borrowed Api must remain in legacy-only fixture provider`, before extracting the helper.
- Provenance exact `-v` command: 3 tests OK; transport exact `-v` command: 89 tests OK.
- Full backend exact quiet command rerun after the latest cancellation-test amendment: 679 tests OK (30.490 seconds).
- Required eight-file quiet HA command: 242 passed (77.21 seconds).
- Full exact quiet HA command rerun after the latest cancellation-test amendment: 755 passed, 1 skipped (376.10 seconds). This includes both cancellation-at-dispatch and in-flight socket-send drain tests in `test_write_adapter.py`.
- Final helper/owned-source check after using public Owner: 23 passed (5.02 seconds).
- `git diff --check` and strings/en `cmp`: passed.

A first newly ported run timed out in a frozen-clock socket wait; the neutral
socket now raises on real condition timeout. Subsequent port failures identified
fixture assumptions (27 registry rows versus 26 enabled, startup DP14 baseline,
brightness_setting identity, unavailable controls without a witness, and a stop
wrapper dropping its bool return); each was corrected without production changes
or weakened payload assertions. The full gate passed after those corrections.
The earlier 754-pass full HA gate preceded the latest cancellation-test amendment;
both fresh full-suite reruns above include that amendment and the public Owner
fixture. The additional in-flight socket-send cancellation case accounts for the
increase to 755 passing HA tests; the pre-existing skip is unchanged.
Parent independent review remains
outstanding under the explicit no-delegation instruction.

## Release builds

Build the runtime ZIP with `python3 tools/build-release.py --output /existing-parent/out`.
The output directory holds exactly `petsafe_freshfeed.zip` and `SHA256SUMS`. Public
release builds add `--release --profile-dir <dir>`, which embeds the app profile only
in the ZIP; never commit profile values. Tag a ZIP-manifest preparation commit, then
restore the tree manifest in a forward commit before publishing trunk. Never publish
branch-named release assets.
