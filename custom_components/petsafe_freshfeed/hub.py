"""Home Assistant hub for FreshFeed raw entities and the optional write coordinator.

Observation is always read-only. Controls exist only when the entry carries the
IDs persisted by an explicit provisioning flow; startup then only OPENS that
store (never provisions, repairs or clears it) and constructs one write adapter
(coordinator) per attached bridge. Each new coordinator re-arms any retained
intent with a new session.

Retirement (source unload/replacement, setup failure, entry unload) first takes
the coordinator out of service synchronously, then quiesces the adapter, then
fences transport before witness via ``LifecycleFence``, and only after a
successful drain detaches the bridge. A failed drain keeps the retired group
parked (bridge attached, barrier and witness untouched) for a later retry.

Only ``connection_backend`` ``owned`` is admitted. No Tuya Local entry,
raw entity, wrapper or raw-state listener is used. The source's actual reports
are observed directly: RAW_DPS values are
mapped to the HA-style strings the pure codecs expect, the first report of
each connection epoch only seeds values, and disconnected values are
unavailable rather than last-known.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_DEVICE_ID, CONF_HOST,
    CONF_INTENT_BINDING_ID, CONF_INTENT_STORE_ID, CONF_LOCAL_KEY, CONF_MIGRATED_FROM,
    CONF_PROTOCOL_VERSION, CONF_REGISTRY_DEVICE_ID,
    CONF_SOURCE_ENTRY_ID, DOMAIN, SOURCE_DOMAIN,
    COORDINATOR_READY_TIMEOUT, INTENT_STORE_ROOT, LIFECYCLE_TIMEOUT,
)
from . import observed
from .lifecycle import LifecycleFence
from .schedule_state import ScheduleState
from .schedule_codec import next_occurrence
from .store_bootstrap import open_store
from .owned_source import FAILURE as OWNED_FAILURE, LocalConnection, OwnedSource
from .write_adapter import async_create_adapter
from .services import acknowledgement, recovery_allowed
from .quick_feed import decode_quick_feed
from .source_lookup import owned_leaf_valid

# Fixed setup-refusal codes (never a configured value).
UNSUPPORTED_BACKEND = "unsupported_backend"
SOURCE_CONFLICT = "source_conflict"
LEAF_INVALID = "registry_leaf_invalid"


def _ha_state(value):
    """Owned DP value -> the HA-style state string the pure codecs expect."""
    if type(value) is bool:
        return "on" if value else "off"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return value
    return None


class FreshFeedHub:
    """Observe the owned feeder reports and persist events."""

    # Seconds between owned admission attempts after a failed start.
    OWNED_RETRY = 30.0

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        from .cloud_read import create_entry_reader
        self.cloud_read = create_entry_reader(hass, entry)
        self._deferred_cloud_schedule_epoch = None
        self._backend = entry.data.get(CONF_CONNECTION_BACKEND)
        self._owned = True  # compatibility flag for owned entity consumers
        self._connection = None
        # Owned path: RAW_DPS -> HA-style value observed in the current epoch.
        self._observed = {}
        self._owned_retry_at = 0.0
        self.device_identifiers = set()
        self.device_id = None
        self.snapshot = observed.snapshot({})
        self.events = []
        self.observed_meals_dispensed_total = 0
        self.counting_since = dt_util.utcnow()
        self.statistics_reason = None
        self._invalid_statistics_slice = None
        self._unsupported_store = None
        self._report_values = {}
        self._report_epoch = None
        self._dp_sequences = {}
        self.schedule = ScheduleState()
        self.schedule_entity_id = None
        self.quick_feed_entity_id = None
        self.quick_feed = decode_quick_feed(None)
        self.bridge = None
        # Write path: the witness opened (never provisioned) from the persisted
        # IDs and the adapter built over this bridge. Absent means read-only.
        self.witness = None
        self.adapter = None
        self._adapter_unsubscribe = None
        self._controls_reason = None
        # Retired [bridge, adapter, fence, witness] groups awaiting a successful drain.
        self._parked = []
        self._drain_lock = asyncio.Lock()
        self._store_loaded = False
        self._binding_generation = 0
        self._binding_valid = True
        self._stopped = False
        self._attach_task = None
        self._coordinator_task = None
        self._coordinator_bridge = None
        self._retry_task = None
        self._bridge_unsubscribe = None
        self.last_fed = None
        self.last_report = None
        self._listeners = []
        self._event_listeners = []
        self._unsubscribers = []
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")

    async def async_start(self):
        if self._backend != BACKEND_OWNED:
            raise ConfigEntryNotReady(UNSUPPORTED_BACKEND)
        self.cloud_read.subscribe(self._cloud_read_completed)
        await self._async_start_owned()

    async def _async_load_store(self):
        stored = await self._store.async_load()
        self._store_loaded = True
        if stored is not None and not isinstance(stored, list) and not (
            isinstance(stored, dict) and type(stored.get("version")) is int
            and stored["version"] in (2, 3)
        ):
            # Unknown schemas are opaque: even unrelated saves must not destroy them.
            self._unsupported_store = deepcopy(stored)
            self.observed_meals_dispensed_total = None
            self.counting_since = None
            self.statistics_reason = "invalid_statistics_payload"
            return
        if isinstance(stored, dict):
            if stored["version"] == 3:
                total = stored.get("observed_meals_dispensed_total")
                since_raw = stored.get("counting_since")
                try:
                    since = dt_util.parse_datetime(since_raw) if isinstance(since_raw, str) else None
                except ValueError:
                    # Well-formed ISO strings can still contain impossible dates.
                    since = None
                if type(total) is int and total >= 0 and since is not None and since.utcoffset() is not None:
                    self.observed_meals_dispensed_total = total
                    self.counting_since = since
                else:
                    self.observed_meals_dispensed_total = None
                    self.counting_since = None
                    self.statistics_reason = "invalid_statistics_payload"
                    self._invalid_statistics_slice = deepcopy({
                        key: stored[key] for key in ("observed_meals_dispensed_total", "counting_since") if key in stored
                    })
            self.schedule = ScheduleState.from_dict(stored.get("schedule"))
            stored = stored.get("events")
        if isinstance(stored, list):
            self.events = [event for item in stored
                           if (event := observed.FeedEvent.from_dict(item)) is not None]
        fed = observed.last_of(self.events, {"dispensed"})
        if fed:
            self.restore_last_fed(fed.at)
        self._prune_history(dt_util.utcnow())

    # ----- owned backend -----------------------------------------------------

    async def _async_start_owned(self):
        """Owned setup: conflict, connection and leaf checks, then one source.

        Never discovers raw entities, listens to the entity registry, or touches
        a Tuya Local wrapper. An existing historical source is watched only for
        conflicts; that source need not exist.
        """
        data = self.entry.data
        if self._source_conflict():
            raise ConfigEntryNotReady(SOURCE_CONFLICT)
        try:
            self._connection = LocalConnection(
                data.get(CONF_DEVICE_ID), data.get(CONF_HOST), data.get(CONF_LOCAL_KEY),
                data.get(CONF_PROTOCOL_VERSION))
        except ValueError as error:
            code = str(error)  # fixed LocalConnectionError code, never a value
        else:
            code = None
        if code is not None:
            raise ConfigEntryNotReady(code)
        device = self._resolve_owned_leaf()
        if device is None:
            raise ConfigEntryNotReady(LEAF_INVALID)
        # Assigned before platforms forward: entities use this explicit leaf.
        self.device_identifiers = set(device.identifiers)
        self.device_id = device.id
        await self._async_load_store()
        self._refresh_states()
        self._unsubscribers = [
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=60)),
            async_track_time_interval(self.hass, self._check_bridge, timedelta(seconds=1)),
        ]
        provenance = data.get(CONF_SOURCE_ENTRY_ID)
        watched = (self.hass.config_entries.async_get_entry(provenance)
                   if type(provenance) is str else None)
        if watched is not None:
            self._unsubscribers.append(watched.async_on_state_change(self._fence_conflict))
        await self._async_attach_owned()

    @callback
    def _fence_conflict(self):
        """Fence and stop the owned source while a conflicting Tuya Local entry is enabled.

        Best effort inside HA only: this cannot stop another integration's own
        connection, so it is not a universal cross-integration lock.
        """
        if self._binding_valid and not self._stopped and self._source_conflict():
            self._controls_reason = SOURCE_CONFLICT
            self.hass.async_create_task(self._source_unloaded(invalidate=True), eager_start=True)

    def _source_conflict(self) -> bool:
        """An enabled Tuya Local entry for this feeder (or the provenance source)."""
        physical = self.entry.data.get(CONF_DEVICE_ID)
        provenance = self.entry.data.get(CONF_SOURCE_ENTRY_ID)
        for other in self.hass.config_entries.async_entries(
                SOURCE_DOMAIN, include_ignore=False, include_disabled=False):
            if (other.entry_id == provenance or other.data.get("device_id") == physical
                    or other.options.get("device_id") == physical):
                return True
        return False

    def _resolve_owned_leaf(self):
        """The single PetSafe-owned leaf; never a second leaf, never another owner's."""
        registry = dr.async_get(self.hass)
        data, entry_id = self.entry.data, self.entry.entry_id
        if CONF_REGISTRY_DEVICE_ID in data:
            persisted = data[CONF_REGISTRY_DEVICE_ID]
            device = registry.async_get(persisted) if type(persisted) is str else None
            if device is None or device.id != persisted:
                return None
        elif CONF_MIGRATED_FROM in data:
            return None  # a migrated entry reuses its persisted leaf only
        else:
            devices = dr.async_entries_for_config_entry(registry, entry_id)
            if len(devices) > 1:
                return None
            device = devices[0] if devices else registry.async_get_or_create(
                config_entry_id=entry_id, identifiers={(DOMAIN, self._connection.device_id)},
                manufacturer="PetSafe", model="FreshFeed",
                name=self.entry.title or "PetSafe FreshFeed")
        return device if self._owned_leaf_valid(device) else None

    def _owned_leaf_valid(self, device) -> bool:
        return owned_leaf_valid(device, self.entry.entry_id, self._connection.device_id)

    async def _async_attach_owned(self, *, notify=False):
        """Start one owned source; a failed start retries after OWNED_RETRY."""
        generation = self._binding_generation
        async with self._drain_lock:
            if (self._parked or generation != self._binding_generation or self._stopped
                    or not self._binding_valid or self.bridge is not None):
                return
            source = OwnedSource(self.hass, self._connection)
            self.bridge = source
            # Subscribed before start: the admission status report is the first
            # actual observation (it seeds values; it never classifies).
            self._bridge_unsubscribe = source.subscribe(self._device_report, self._connection_changed)
        try:
            await source.async_start()
        except ValueError:
            self._owned_retry_at = self.hass.loop.time() + self.OWNED_RETRY
            if self.bridge is source:
                if self._bridge_unsubscribe is not None:
                    self._bridge_unsubscribe()
                    self._bridge_unsubscribe = None
                self.bridge = None
                self._observed.clear()
                self._refresh_states()
            if generation == self._binding_generation:
                self.schedule = replace(self.schedule.disconnected(), error=OWNED_FAILURE)
                if notify:
                    self._notify()
            return
        if generation != self._binding_generation or self.bridge is not source:
            return  # retired while starting: its parked group stops it
        if self.schedule.error == OWNED_FAILURE:
            self.schedule = replace(self.schedule, error=None)
        self._notify()
        self._start_coordinator(source, generation)

    @callback
    def _check_owned(self):
        """Periodic owned check: current-state, controls retry, drain, reattach."""
        self._fence_conflict()
        bridge = self.bridge
        available = self.snapshot.available
        self._refresh_states()
        if self.snapshot.available != available:
            self._notify()
        if (self.adapter is None and bridge is not None
                and self._controls_reason == "source_incompatible"
                and self._binding_valid and not self._stopped and self._is_current(bridge)
                and (self._coordinator_task is None or self._coordinator_task.done())):
            self._start_coordinator(bridge, self._binding_generation)
        elif (self._parked and not self._stopped and not self._drain_lock.locked()
              and (self._retry_task is None or self._retry_task.done())):
            self._retry_task = self.hass.async_create_task(
                self._async_retry_drain(), eager_start=True)
        elif (bridge is None and not self._parked and not self._stopped and self._binding_valid
              and (self._attach_task is None or self._attach_task.done())
              and (self._retry_task is None or self._retry_task.done())
              and self.hass.loop.time() >= self._owned_retry_at):
            self._attach_task = self.hass.async_create_task(
                self._async_attach_owned(notify=True), eager_start=True)

    @staticmethod
    def _is_current(bridge) -> bool:
        try:
            return bridge is not None and bridge.is_current() is True
        except Exception:  # noqa: BLE001 - unknown shape is not current
            return False

    def _store_ids(self):
        """Persisted (store_id, binding_id); None for a read-only entry."""
        data = self.entry.data
        if CONF_INTENT_STORE_ID not in data and CONF_INTENT_BINDING_ID not in data:
            return None
        return data.get(CONF_INTENT_STORE_ID), data.get(CONF_INTENT_BINDING_ID)

    def _superseded(self, bridge, generation):
        return (generation != self._binding_generation or self.bridge is not bridge
                or self._stopped)

    def _start_coordinator(self, bridge, generation):
        """Start one tracked construction of a new coordinator over this bridge."""
        ids = self._store_ids()
        if ids is None:
            return  # read-only entry: receive ownership is never taken
        store_id, binding_id = ids
        # The store belongs to the source-owned registry leaf it was provisioned for.
        if type(store_id) is not str or type(binding_id) is not str or binding_id != self.device_id:
            self._controls_reason = "binding_mismatch"
            self._notify()
            return
        previous = self._coordinator_task
        if previous is not None and not previous.done() and self._coordinator_bridge is bridge:
            return  # one construction per bridge at a time
        self._controls_reason = "source_not_ready"
        self._coordinator_bridge = bridge
        self._coordinator_task = self.hass.async_create_task(
            self._async_run_coordinator(previous, bridge, generation, store_id, binding_id),
            eager_start=True)

    async def _async_run_coordinator(self, previous, bridge, generation, store_id, binding_id):
        """Construct; return an adapter that was superseded before installation, else None.

        Never drains or detaches anything itself: a superseded result belongs to
        the retired group that captured this task, whose drain awaits it.
        """
        try:
            # A construction for a superseded bridge settles first.
            if previous is not None and not previous.done():
                await asyncio.wait({previous})
            return await self._async_construct(bridge, generation, store_id, binding_id)
        finally:
            self._notify()

    async def _async_construct(self, bridge, generation, store_id, binding_id):
        """Open (never provision) the store and construct the coordinator."""
        deadline = asyncio.get_running_loop().time() + COORDINATOR_READY_TIMEOUT
        # Every previous coordinator must be fully drained before a new one admits.
        while self._parked:
            if self._superseded(bridge, generation):
                return None
            if asyncio.get_running_loop().time() >= deadline:
                self._controls_reason = "coordinator_draining"
                return None
            await asyncio.sleep(0.05)
        if self._superseded(bridge, generation):
            return None
        # Receive ownership is claimed only on a current connection. Claiming
        # costs one ordinary reconnect; the transport is enabled only on the
        # owned connection once it is current again. A refused claim is retried
        # (bounded); a claim refusal happens before or is rolled back from any
        # mutation, and nothing is ever sent here.
        while True:
            if not await self._wait_current(bridge, generation, deadline):
                return None
            try:
                await bridge.async_claim_receive()
                break
            except ValueError:
                if self._superseded(bridge, generation):
                    return None
                if asyncio.get_running_loop().time() >= deadline:
                    self._controls_reason = "source_incompatible"
                    return None
                await asyncio.sleep(0.25)
        if not await self._wait_current(bridge, generation, deadline):
            return None
        try:
            witness = open_store(self.hass.config.path(INTENT_STORE_ROOT),
                                 binding_id=binding_id, store_id=store_id)
        except ValueError:
            self._controls_reason = "binding_mismatch"
            return None
        # Runtime data supplies the opened witness to the adapter construction.
        self.witness = witness
        try:
            adapter = await async_create_adapter(
                self.hass, self.entry, bridge, ack_predicate=acknowledgement,
                recovery_predicate=recovery_allowed)
        except ValueError:
            self._controls_reason = "source_incompatible"
            return None
        if self._superseded(bridge, generation):
            # Superseded while constructing: it never admits; the retired group
            # that captured this task quiesces and fences it before detaching.
            if self.witness is witness:
                self.witness = None
            return adapter
        self.adapter = adapter
        self._controls_reason = None
        self._adapter_unsubscribe = adapter.add_listener(self._notify)
        return None

    async def _wait_current(self, bridge, generation, deadline) -> bool:
        loop = asyncio.get_running_loop()
        while not bridge.is_current():
            if self._superseded(bridge, generation):
                return False
            if loop.time() >= deadline:
                self._controls_reason = "source_not_ready"
                return False
            await asyncio.sleep(0.05)
        return not self._superseded(bridge, generation)

    def _park(self):
        """Synchronously take the bridge and coordinator out of service.

        An in-flight construction is part of the retired group: its drain
        settles that task (and fences any adapter it produced) before detaching.
        """
        bridge, adapter, witness = self.bridge, self.adapter, self.witness
        task = self._coordinator_task
        if task is not None and (task.done() or self._coordinator_bridge is not bridge):
            task = None  # settled, or owned by an earlier group
        self.bridge = self.adapter = self.witness = None
        # Epochs are local to each bridge, so a replacement may reuse one.
        self._report_values.clear()
        self._report_epoch = None
        self._dp_sequences.clear()
        self.cloud_read.invalidate()
        if self._owned:
            # A retired owned source's values are not current observations.
            self._observed.clear()
            self._refresh_states()
        if self._bridge_unsubscribe is not None:
            self._bridge_unsubscribe()
            self._bridge_unsubscribe = None
        if self._adapter_unsubscribe is not None:
            self._adapter_unsubscribe()
            self._adapter_unsubscribe = None
        if bridge is not None or adapter is not None or witness is not None or task is not None:
            self._parked.append([bridge, adapter, None, witness, task])

    async def _async_drain(self) -> bool:
        """Settle construction, quiesce, fence transport then witness, then detach.

        False retains the group (bridge attached, barrier and witness untouched).
        """
        async with self._drain_lock:
            while self._parked:
                group = self._parked[0]
                task = group[4]
                if task is not None:
                    # Bounded: construction observes supersession promptly.
                    await asyncio.wait({task}, timeout=LIFECYCLE_TIMEOUT)
                    if not task.done():
                        return False
                    group[4] = None
                    orphan = (None if task.cancelled() or task.exception() is not None
                              else task.result())
                    if orphan is not None:
                        if group[1] is not None:
                            # Never expected; retire the extra coordinator separately.
                            self._parked.insert(1, [None, orphan, None, None, None])
                        else:
                            group[1] = orphan
                    if group[3] is None and group[1] is not None:
                        group[3] = group[1].witness
                bridge, adapter, fence, witness, _ = group
                if adapter is not None:
                    # Quiesce admission and invalidate recovery evidence first.
                    if not await adapter.async_stop(LIFECYCLE_TIMEOUT):
                        return False
                    if fence is None:
                        fence = group[2] = LifecycleFence(adapter.transport, adapter.witness)
                    if not await fence.async_stop(LIFECYCLE_TIMEOUT):
                        return False
                if (witness is not None and (adapter is None or witness is not adapter.witness)
                        and not await witness.async_wait_idle(LIFECYCLE_TIMEOUT)):
                    return False
                if bridge is not None:
                    # Only after a successful drain: detach waits for the shared
                    # API lock and restores upstream receive ownership. A
                    # refused detach keeps the parked group: never pop a live
                    # source.
                    if await bridge.async_detach() is False:
                        return False
                self._parked.pop(0)
            return True

    async def _source_unloaded(self, *, notify=True, invalidate=False) -> bool:
        if invalidate:
            self._binding_valid = False
        self._binding_generation += 1
        self._park()
        self.schedule = self.schedule.disconnected()
        self.quick_feed = decode_quick_feed(None)
        if notify:
            self._notify()
        return await self._async_drain()

    async def async_drain_controls(self) -> bool:
        """Unload step 1: stop admitting and drain/detach, keeping HA listeners.

        False means actual work is still running; everything is retained.
        """
        self._stopped = True
        return await self._source_unloaded(notify=False)

    def resume_after_failed_unload(self):
        """Resume only after retired groups drain; never revive a retired adapter."""
        self._stopped = False
        # A refused shutdown suspended (never closed) the reader: reads resume.
        self.cloud_read.resume()
        self._notify()
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = self.hass.async_create_task(
                self._async_retry_drain(), eager_start=True)

    async def async_stop(self) -> bool:
        """Fenced shutdown; False refuses (listeners, parked group and barrier retained)."""
        if not await self.async_drain_controls():
            return False
        # Refuse new cloud reads and drain actual I/O; close only once the
        # shutdown can no longer be refused (resume_after_failed_unload).
        await self.cloud_read.async_suspend()
        # Actual attach/construction work drains; superseded results retire fenced.
        for name in ("_retry_task", "_attach_task", "_coordinator_task"):
            task = getattr(self, name)
            if task is not None and not task.done():
                await task
            setattr(self, name, None)
        if not await self._async_drain():
            return False
        await self.cloud_read.close()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._store_loaded:
            await self._store.async_save(self._serialize())
        return True

    def reported_dps(self, epoch):
        """Latest local DP140 already delivered in ``epoch``, for adapter seeding.

        Only presence/value and sequence: never a receipt time or freshness.
        """
        bridge = self.bridge
        if (bridge is None or self._report_epoch != epoch or bridge.epoch != epoch
                or '140' not in self._dp_sequences):
            return {}
        snapshot = bridge.snapshot_dps()
        if '140' not in snapshot:
            return {}
        return {'140': (snapshot['140'], self._dp_sequences['140'])}

    @callback
    def _device_report(self, report):
        if report.epoch != self._report_epoch:
            self._report_epoch = report.epoch
            self._report_values.clear()
            # A new epoch's first values only seed; never compared across epochs.
            self._observed.clear()
            self._dp_sequences.clear()
        self._dp_sequences.update({key: report.sequence for key in report.dps})
        if self._owned:
            self._observe_report(report)
        for dp in (14, 150):
            if str(dp) in report.dps:
                value = report.dps[str(dp)]
                value = value if type(value) is int else None
                event = observed.classify_report(dp, self._report_values.get(dp), value, at=report.at)
                self._report_values[dp] = value
                if event:
                    self._record_event(event)
        if '140' in report.dps:
            if self._owned:
                self._observed[140] = _ha_state(report.dps['140'])
            self.quick_feed = decode_quick_feed(report.dps['140'])
            self._notify()
        if "1" in report.dps:
            self.schedule = self.schedule.with_report(report.dps["1"], report.at)
            if self.bridge is not None and (report.epoch != self.bridge.epoch or not self.bridge.is_current()):
                self.schedule = self.schedule.disconnected()
            self._store.async_delay_save(self._serialize, 10)
            self._notify()
        if self._owned:
            self._notify()

    def _observe_report(self, report):
        """Owned: direct RAW_DPS observation from one actual report.

        The state classifier sees old/new values of the same epoch only, at
        the report's own timestamp; DP14/150 report events stay with the
        report classifier in ``_device_report`` (each records exactly once).
        """
        if self.last_report is None or report.at > self.last_report:
            self.last_report = report.at
        events = []
        for dp in observed.RAW_DPS:
            key = str(dp)
            if key not in report.dps:
                continue
            old, new = self._observed.get(dp), _ha_state(report.dps[key])
            self._observed[dp] = new
            if (feed := observed.classify(dp, old, new, at=report.at)) is not None:
                events.append(feed)
        self._refresh_states()
        for feed in events:
            self._record_event(feed)

    @callback
    def _connection_changed(self, epoch, connected):
        self.quick_feed = decode_quick_feed(None)
        self._report_values.clear()
        self._report_epoch = None
        self._dp_sequences.clear()
        self.cloud_read.invalidate()
        self._deferred_cloud_schedule_epoch = None
        if self._owned:
            self._observed.clear()
            self._refresh_states()
            if connected:
                self._notify()
                if self._store_ids() is None and self.bridge is not None:
                    self.hass.async_create_task(self._demand_read_only_cloud(
                        epoch, self.bridge, self._binding_generation))
        if not connected:
            self.schedule = self.schedule.disconnected()
            self._store.async_delay_save(self._serialize, 10)
            self._notify()
        elif (self.adapter is None and self.bridge is not None
              and self._controls_reason == "source_not_ready"):
            # Construction timed out before the owned connection was current.
            self._start_coordinator(self.bridge, self._binding_generation)

    async def _demand_read_only_cloud(self, epoch, bridge, generation):
        # Let the admission report establish LAN precedence before demanding DP140.
        await asyncio.sleep(0)
        if (self._superseded(bridge, generation) or not self._is_current(bridge)
                or bridge.epoch != epoch or '140' in self._dp_sequences
                or self._store_ids() is not None):
            return
        await self.cloud_read.demand(epoch=epoch, sequences=dict(self._dp_sequences),
                                     allowed=self._cloud_schedule_allowed())

    async def _demand_cloud_schedule(self, epoch, bridge, generation):
        # Yield so the initial LAN status can supply DP1 first.
        await asyncio.sleep(0)
        if (self._superseded(bridge, generation) or not self._is_current(bridge)
                or bridge.epoch != epoch or '1' in self._dp_sequences):
            return
        await self.cloud_read.demand(epoch=epoch, sequences=dict(self._dp_sequences),
                                     allowed=self._cloud_schedule_allowed())

    def _cloud_schedule_allowed(self):
        adapter = self.adapter
        return adapter is None or (adapter._submitting is None and adapter._ack_wait is None
                                   and adapter._collector is None)

    @callback
    def _cloud_read_completed(self, epoch):
        # Every successful demand can supply DP1, not only the epoch-start need.
        if not self._cloud_schedule_allowed():
            self._deferred_cloud_schedule_epoch = epoch
            return
        self._consume_cloud_schedule(epoch, self.bridge, self._binding_generation)

    def _consume_cloud_schedule(self, epoch, bridge, generation):
        if (self._superseded(bridge, generation) or not self._is_current(bridge)
                or bridge.epoch != epoch or not self._cloud_schedule_allowed()):
            return
        evidence = self.cloud_read.evidence('1', sequences=self._dp_sequences)
        if evidence is None or evidence.epoch != epoch:
            return
        self.schedule = self.schedule.with_cloud_report(evidence.value, evidence.received_wall_at)
        self._store.async_delay_save(self._serialize, 10)
        self._notify()

    def _serialize(self):
        if self._unsupported_store is not None:
            return deepcopy(self._unsupported_store)
        data = {"version": 3, "events": [event.to_dict() for event in self.events],
                "schedule": self.schedule.to_dict()}
        if self._invalid_statistics_slice is not None:
            data.update(deepcopy(self._invalid_statistics_slice))
        else:
            data.update(observed_meals_dispensed_total=self.observed_meals_dispensed_total,
                        counting_since=self.counting_since.isoformat())
        return data

    def _refresh_states(self):
        # Only actual current-epoch reports supply observations.
        current = self._is_current(self.bridge)
        self.snapshot = observed.snapshot(dict(self._observed) if current else {})

    def _record_event(self, feed):
        self.events = observed.prune([*self.events, feed], now=dt_util.utcnow())
        if feed.kind == "dispensed":
            if self.observed_meals_dispensed_total is not None:
                self.observed_meals_dispensed_total += 1
            self.restore_last_fed(feed.at)
        self._store.async_delay_save(self._serialize, 10)
        for listener in tuple(self._event_listeners):
            listener(feed)
        self._notify()

    @callback
    def _check_bridge(self, now):
        self._check_owned()

    async def _async_retry_drain(self):
        """Drain parked groups; then restore observation/controls for the live source."""
        if not await self._async_drain() or self._stopped or not self._binding_valid:
            return
        if self.bridge is None:
            if self._attach_task is None or self._attach_task.done():
                self._attach_task = self.hass.async_create_task(
                    self._async_attach_owned(notify=True), eager_start=True)
        elif self.adapter is None and self._controls_reason == "coordinator_draining":
            self._start_coordinator(self.bridge, self._binding_generation)

    def _prune_history(self, now):
        events = observed.prune(self.events, now=now)
        if events != self.events:
            self.events = events
            self._store.async_delay_save(self._serialize, 10)

    @callback
    def _tick(self, now):
        self._check_bridge(now)
        self._prune_history(now)
        self._refresh_states()
        self._notify()

    def _notify(self):
        if self._deferred_cloud_schedule_epoch is not None and self._cloud_schedule_allowed():
            epoch = self._deferred_cloud_schedule_epoch
            self._deferred_cloud_schedule_epoch = None
            self._consume_cloud_schedule(epoch, self.bridge, self._binding_generation)
        for listener in tuple(self._listeners):
            listener()

    def async_add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def async_add_event_listener(self, cb):
        self._event_listeners.append(cb)
        return lambda: self._event_listeners.remove(cb)

    def restore_last_fed(self, value: datetime):
        if self.last_fed is None or value > self.last_fed:
            self.last_fed = value

    @property
    def write_view(self) -> dict:
        """Write status; without an adapter the write path is unavailable."""
        if self.adapter is not None:
            return self.adapter.view
        return {"state": "blocked", "operation_id": None, "operation": None, "session_id": "",
                "reason": self._controls_reason or "controls_unavailable", "observed_failure": None, "storage_fault": False,
                "integrity_fault": False, "recovery_can_start": False,
                "recovery_phase": "idle", "recovery_resolution": None, "can_feed": False, "can_defrost": False,
                "can_refilled": False, "can_cancel": False,
                "cancel_reason": self._controls_reason or "controls_unavailable",
                "blocked_reason": self._controls_reason or "controls_unavailable"}

    @property
    def last_event(self):
        return observed.last_of(self.events, {"dispensed", "refilled", "skipped_empty", "skipped_fault"})

    @property
    def meals_today(self):
        return observed.meals_today(self.events, now=dt_util.utcnow(), tz=dt_util.get_default_time_zone())

    @property
    def meal_times(self):
        return observed.infer_meal_times(self.events, now=dt_util.utcnow(), tz=dt_util.get_default_time_zone())

    @property
    def next_meal(self):
        if self.snapshot.meals_left == 0:
            return None
        if self.schedule.entries is None:
            return None
        return next_occurrence(self.schedule.entries, now=dt_util.utcnow(), tz=dt_util.get_default_time_zone())
