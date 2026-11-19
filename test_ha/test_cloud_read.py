"""Demand-driven cloud read lifetime tests; all I/O is synthetic."""
import asyncio
import importlib.util
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_cloud_read_coalesces_and_limits_attempts():
    name = "custom_components.petsafe_freshfeed.cloud_read"
    assert importlib.util.find_spec(name) is not None, "cloud read helper must be shipped"
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead

    clock = [100.0]
    release = asyncio.Event()
    calls = []

    async def fetch():
        calls.append(1)
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    helper = CloudRead(fetch, clock=lambda: clock[0])
    first = asyncio.create_task(helper.demand(epoch=1, sequences={"1": 2, "140": 3}))
    await asyncio.sleep(0)
    second = asyncio.create_task(helper.demand(epoch=1, sequences={"1": 2, "140": 3}))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 1
    assert helper.evidence("1") is None  # Local DP1 was already reported.
    await helper.demand(epoch=2, sequences={})
    assert len(calls) == 1
    clock[0] += 60
    await helper.demand(epoch=2, sequences={})
    assert len(calls) == 2
    await helper.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["1", "140"])
@pytest.mark.parametrize("timing", ["before", "during", "after", "never"])
async def test_local_report_always_supersedes_cloud(key, timing):
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    entered, release = asyncio.Event(), asyncio.Event()
    sequences = {key: 0} if timing == "before" else {}

    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140="idle", dp140_present=True)

    helper = CloudRead(fetch)
    caller = asyncio.create_task(helper.demand(epoch=1, sequences=sequences))
    await entered.wait()
    if timing == "during":
        sequences[key] = 1
    release.set()
    await caller
    if timing == "after":
        sequences[key] = 1
    if timing == "before":
        assert helper.evidence(key) is None, "pre-existing local evidence must win"
    else:
        evidence = helper.evidence(key, sequences=sequences)
        assert (evidence is not None) == (timing == "never")
    other = "140" if key == "1" else "1"
    assert helper.evidence(other) is not None
    await helper.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_fence_other_waiter():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    helper = CloudRead(fetch)
    first = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await entered.wait()
    second = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second
    assert helper.evidence("1").value == "plan"
    await helper.close()


@pytest.mark.asyncio
async def test_entry_cooldown_survives_reader_reload(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader

    entry = MockConfigEntry(domain="petsafe_freshfeed", data={}, options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    with patch.object(type(entry), "async_start_reauth") as reauth:
        first = create_entry_reader(hass, entry)
        await first.demand(epoch=1, sequences={})
        await first.close()
        second = create_entry_reader(hass, entry)
        await second.demand(epoch=1, sequences={})
        assert reauth.call_count == 1
        assert second.last_outcome == "unknown"
        await second.close()


@pytest.mark.asyncio
async def test_cloud_failure_is_fixed_and_does_not_escape():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead

    async def fetch():
        raise RuntimeError("synthetic-private-response")

    helper = CloudRead(fetch)
    await helper.demand(epoch=1, sequences={})
    assert helper.last_outcome == "unavailable"
    await helper.close()


@pytest.mark.asyncio
async def test_cloud_epoch_change_invalidates_running_result_and_evidence():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    release = asyncio.Event()

    async def fetch():
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    helper = CloudRead(fetch)
    caller = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await asyncio.sleep(0)
    helper.invalidate()
    release.set()
    await caller
    assert helper.evidence("1") is None
    await helper.close()


@pytest.mark.asyncio
async def test_cancellation_before_fetch_preserves_credentials_and_auth(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from homeassistant.helpers import issue_registry as ir
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader

    original = {"cloud_session": {"sid": "synthetic"}, "cloud_route": {}}
    entry = MockConfigEntry(domain="petsafe_freshfeed", data=original,
                            options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    helper = create_entry_reader(hass, entry)
    entered, release = asyncio.Event(), asyncio.Event()
    run = helper._run

    async def delayed_run(*args):
        entered.set()
        await release.wait()
        await run(*args)

    with patch.object(helper, "_run", side_effect=delayed_run), patch.object(
        type(entry), "async_start_reauth") as reauth:
        caller = asyncio.create_task(helper.demand(epoch=1, sequences={}))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await helper._task
        assert dict(entry.data) == original
        reauth.assert_not_called()
        assert ir.async_get(hass).async_get_issue(
            "petsafe_freshfeed", entry.entry_id + "_cloud_auth") is None
        assert helper.last_outcome == "unknown"
    await helper.close()


@pytest.mark.asyncio
async def test_entry_helper_invalid_restore_reauth_only_on_demand(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed import cloud_read
    entry = MockConfigEntry(domain="petsafe_freshfeed", data={
        "device_id": "synthetic", "cloud_session": {"sid": "synthetic"}},
        options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    assert hasattr(cloud_read, "create_entry_reader"), "entry reader wiring required"
    with patch.object(type(entry), "async_start_reauth") as reauth:
        helper = cloud_read.create_entry_reader(hass, entry)
        reauth.assert_not_called()
        await helper.demand(epoch=1, sequences={})
        await helper.demand(epoch=2, sequences={})
        reauth.assert_called_once()
        assert helper.last_outcome == "session_rejected"
        assert "cloud_session" not in entry.data
        await helper.close()


@pytest.mark.asyncio
async def test_session_rejection_does_not_reload_lan(hass):
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed import _async_entry_updated
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader
    entry = MockConfigEntry(domain='petsafe_freshfeed', data={'cloud_session': {'sid': 'bad'}},
                            options={'cloud_read_enabled': True})
    entry.add_to_hass(hass)
    unsubscribe = entry.add_update_listener(_async_entry_updated)
    with patch.object(type(entry), 'async_start_reauth'), \
         patch.object(hass.config_entries, 'async_reload', new=AsyncMock()) as reload:
        helper = create_entry_reader(hass, entry)
        await helper.demand(epoch=1, sequences={})
        await hass.async_block_till_done()
        reload.assert_not_awaited()
        await helper.close()
    unsubscribe()


@pytest.mark.asyncio
async def test_late_rejection_after_disable_does_not_start_reauth(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed import account
    from custom_components.petsafe_freshfeed.account import boundary
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader
    import threading
    started, release = threading.Event(), threading.Event()
    route = boundary.select_route('1', boundary.Contract.load())
    entry = MockConfigEntry(domain='petsafe_freshfeed', data={
        'device_id': 'synthetic', 'cloud_session': {'sid': 'synthetic', 'ecode': 'synthetic'},
        'cloud_route': {'country_code': '1', 'region': route.region, 'url': route.url,
                        'api_domain': route.api_domain.value}}, options={'cloud_read_enabled': True})
    entry.add_to_hass(hass)
    def read(*args, **kwargs):
        started.set()
        release.wait(5)
        return account.DPRead('session_rejected')
    with patch.object(account, 'load_app_profile', return_value=object()), \
         patch.object(account, 'read_dps', side_effect=read), \
         patch.object(type(entry), 'async_start_reauth') as reauth:
        helper = create_entry_reader(hass, entry)
        caller = asyncio.create_task(helper.demand(epoch=1, sequences={}))
        await hass.async_add_executor_job(started.wait, 5)
        closing = asyncio.create_task(helper.close())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(caller, closing)
        reauth.assert_not_called()


@pytest.mark.asyncio
async def test_disable_during_rejection_listener_yield_does_not_start_reauth(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from homeassistant.helpers import issue_registry as ir
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader

    entry = MockConfigEntry(domain="petsafe_freshfeed", data={"cloud_session": {"sid": "bad"}},
                            options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    helper = create_entry_reader(hass, entry)
    update = hass.config_entries.async_update_entry

    def update_then_disable(entry, **kwargs):
        update(entry, **kwargs)
        def disable():
            update(entry, options={"cloud_read_enabled": False})
            helper.invalidate()
        hass.loop.call_soon(disable)

    with patch.object(hass.config_entries, "async_update_entry", side_effect=update_then_disable), \
         patch.object(type(entry), "async_start_reauth") as reauth:
        await helper.demand(epoch=1, sequences={})
        reauth.assert_not_called()
        assert ir.async_get(hass).async_get_issue("petsafe_freshfeed", entry.entry_id + "_cloud_auth") is None
    await helper.close()


@pytest.mark.asyncio
async def test_restored_session_reads_and_rejection_is_deduplicated(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed import account
    from custom_components.petsafe_freshfeed.account import boundary
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader
    from homeassistant.helpers import issue_registry as ir
    route = boundary.select_route('1', boundary.Contract.load())
    stored = {'sid': 'synthetic-sid', 'ecode': 'synthetic-ecode'}
    route_data = {'country_code': '1', 'region': route.region, 'url': route.url,
                  'api_domain': route.api_domain.value}
    entry = MockConfigEntry(domain='petsafe_freshfeed', data={
        'device_id': 'synthetic', 'cloud_session': stored, 'cloud_route': route_data},
        options={'cloud_read_enabled': True})
    entry.add_to_hass(hass)
    with patch.object(account, 'load_app_profile', return_value=object()), \
         patch.object(account, 'read_dps', return_value=account.DPRead('session_rejected')) as read, \
         patch.object(type(entry), 'async_start_reauth') as reauth:
        helper = create_entry_reader(hass, entry)
        await helper.demand(epoch=1, sequences={})
        await helper.demand(epoch=2, sequences={})
        read.assert_called_once()
        assert read.call_args.args[0].storage_data() == stored
        reauth.assert_called_once()
        assert ir.async_get(hass).async_get_issue('petsafe_freshfeed', entry.entry_id + '_cloud_auth')
        assert helper.auth_state == 'signed_out'
        await helper.close()


@pytest.mark.asyncio
async def test_dispatch_window_refuses_cloud_attempt():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    calls = []
    async def fetch():
        calls.append(1)
        return SimpleNamespace(outcome="ok", dp1=None, dp140=None, dp140_present=False)
    helper = CloudRead(fetch)
    await helper.demand(epoch=1, sequences={}, allowed=False)
    assert calls == []
    await helper.close()


@pytest.mark.asyncio
async def test_epoch_demand_drops_old_evidence_even_inside_cooldown():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    async def fetch():
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)
    helper = CloudRead(fetch)
    await helper.demand(epoch=1, sequences={})
    assert helper.evidence('1') is not None
    await helper.demand(epoch=2, sequences={})
    assert helper.evidence('1') is None
    await helper.close()


@pytest.mark.asyncio
async def test_disabled_entry_never_reads_or_prompts(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader
    entry = MockConfigEntry(domain="petsafe_freshfeed", data={}, options={})
    entry.add_to_hass(hass)
    with patch.object(type(entry), "async_start_reauth") as reauth:
        helper = create_entry_reader(hass, entry)
        await helper.demand(epoch=1, sequences={})
        reauth.assert_not_called()
        await helper.close()


@pytest.mark.asyncio
async def test_cancellation_fences_but_drains_fetch():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    release, entered = asyncio.Event(), asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    helper = CloudRead(fetch)
    task = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close = asyncio.create_task(helper.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    assert helper.evidence("1") is None
    assert helper._fetch is None  # Drop the closure holding restored session secrets.


@pytest.mark.asyncio
async def test_first_coalesced_caller_timeout_invalidates_shared_result():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    release, entered = asyncio.Event(), asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    helper = CloudRead(fetch, deadline=0.05)
    first = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await entered.wait()
    helper._deadline = 1
    second = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await first
    release.set()
    await second
    assert helper.evidence("1") is None
    await helper.close()


@pytest.mark.asyncio
async def test_cloud_timeout_and_close_drain_actual_work():
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    release = asyncio.Event()
    entered = asyncio.Event()

    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    helper = CloudRead(fetch, deadline=0.01)
    await helper.demand(epoch=1, sequences={})
    assert entered.is_set()
    closing = asyncio.create_task(helper.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert helper.evidence("1") is None


async def test_suspend_drains_and_refuses_then_resume_restores_reader():
    """A refused shutdown resumes the same reader: fetch, listeners and auth survive."""
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    release = asyncio.Event()
    calls, seen = [], []

    async def fetch():
        calls.append(True)
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1="plan", dp140=None, dp140_present=False)

    now = [0.0]
    helper = CloudRead(fetch, clock=lambda: now[0], deadline=0.01)
    helper.auth_state = "signed_in"
    helper.subscribe(seen.append)
    await helper.demand(epoch=1, sequences={})
    suspending = asyncio.create_task(helper.async_suspend())
    await asyncio.sleep(0)
    assert not suspending.done()  # actual I/O drains before suspension completes
    release.set()
    await suspending
    assert not helper.in_flight and helper.evidence("1") is None and seen == []
    now[0] = 120.0
    await helper.demand(epoch=1, sequences={})
    assert len(calls) == 1  # suspended: no request
    helper.resume()
    assert helper.auth_state == "signed_in"
    await helper.demand(epoch=1, sequences={})
    await asyncio.sleep(0)
    assert len(calls) == 2
    assert helper.evidence("1").value == "plan" and seen == [1]
    await helper.close()
    helper.resume()  # a final close is never revived
    now[0] = 240.0
    await helper.demand(epoch=1, sequences={})
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_dismissed_reauth_is_requested_again_on_later_demand(hass, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from homeassistant.config_entries import SOURCE_REAUTH
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader

    from custom_components.petsafe_freshfeed.const import BACKEND_OWNED, CONF_CONNECTION_BACKEND

    entry = MockConfigEntry(domain="petsafe_freshfeed", data={CONF_CONNECTION_BACKEND: BACKEND_OWNED},
                            options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)

    def reauth_flows():
        return [flow for flow in hass.config_entries.flow.async_progress_by_handler("petsafe_freshfeed")
                if flow["context"].get("source") == SOURCE_REAUTH]

    from unittest.mock import AsyncMock

    # The reauth flow shows the sign-in form; no account request is made here.
    monkeypatch.setattr("custom_components.petsafe_freshfeed.config_flow._async_app_profile",
                        AsyncMock(return_value=object()))
    reader = create_entry_reader(hass, entry)
    await reader.demand(epoch=1, sequences={})
    await hass.async_block_till_done()
    flows = reauth_flows()
    assert len(flows) == 1
    hass.config_entries.flow.async_abort(flows[0]["flow_id"])
    await hass.async_block_till_done()
    assert reauth_flows() == []

    # A later eligible demand, after the per-entry cooldown, must ask again.
    hass.data["petsafe_freshfeed"]["cloud_read_budgets"][entry.entry_id].pop("last_attempt")
    await reader.demand(epoch=1, sequences={})
    await hass.async_block_till_done()
    assert len(reauth_flows()) == 1

    # A demand while a reauth flow is active does not start a duplicate.
    hass.data["petsafe_freshfeed"]["cloud_read_budgets"][entry.entry_id].pop("last_attempt")
    await reader.demand(epoch=1, sequences={})
    await hass.async_block_till_done()
    assert len(reauth_flows()) == 1
    await reader.close()


@pytest.mark.asyncio
async def test_entry_reader_restores_contract_off_the_event_loop(hass):
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.petsafe_freshfeed import account
    from custom_components.petsafe_freshfeed.account import boundary
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader
    route = boundary.select_route('1', boundary.Contract.load())
    stored = {'sid': 'synthetic-sid', 'ecode': 'synthetic-ecode'}
    entry = MockConfigEntry(domain='petsafe_freshfeed', data={
        'device_id': 'synthetic', 'cloud_session': stored, 'cloud_route': {
            'country_code': '1', 'region': route.region, 'url': route.url,
            'api_domain': route.api_domain.value}}, options={'cloud_read_enabled': True})
    entry.add_to_hass(hass)
    original = boundary.Contract.load.__func__
    on_loop = []

    def load(cls, *args, **kwargs):
        # Contract loading is blocking file I/O: never on the event loop thread.
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            pass
        return original(cls, *args, **kwargs)

    with patch.object(boundary.Contract, 'load', classmethod(load)), \
         patch.object(account, 'load_app_profile', return_value=object()), \
         patch.object(account, 'read_dps', return_value=account.DPRead('ok')) as read:
        helper = create_entry_reader(hass, entry)
        assert helper.auth_state == 'signed_in'
        await helper.demand(epoch=1, sequences={})
        read.assert_called_once()
        assert read.call_args.args[0].storage_data() == stored
        await helper.close()
    assert on_loop == []
