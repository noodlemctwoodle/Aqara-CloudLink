"""Tests for the AqaraCloudLink cloud integration."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aqaracloudlink import _supported_devices
from custom_components.aqaracloudlink.cloud import AqaraCloudAuthError
from custom_components.aqaracloudlink.const import DOMAIN, LANLINK_DOMAIN
from custom_components.aqaracloudlink.coordinator import (
    AqaraCloudCoordinator,
    CloudDevice,
    _coerce,
)
from custom_components.aqaracloudlink.metrics import metrics_for, supported_models
from custom_components.aqaracloudlink.sensor import _device_info

PLUG = "lumi.plug.aeu002"
DID = "lumi.54ef4410014f4b36"
HUB = "lumi3.8333b4a6a7b34e2b"


def _device(did: str = DID, model: str = PLUG) -> CloudDevice:
    return CloudDevice(did=did, model=model, name="Kitchen Power Socket")


class _FakeClient:
    """Records calls and replays canned responses keyed by read method."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    async def query_view_data(self, token, did, read_keys, process_method):
        self.calls.append((did, tuple(read_keys), process_method))
        if self.raises is not None:
            raise self.raises
        return self.responses.get(process_method, {})


async def _coordinator(hass, client, devices=None):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    return AqaraCloudCoordinator(
        hass, entry, client, "tok", devices if devices is not None else [_device()]
    )


# --------------------------------------------------------------------------
# metrics table
# --------------------------------------------------------------------------


def test_metrics_cover_both_sockets():
    """The eight keys must match what was verified against real hardware."""
    keys = {m.key for m in metrics_for(PLUG)}
    assert keys == {
        "8.0.2293", "0.17.85", "1d_0_0.17.85", "month_0_0.17.85",
        "8.0.2304", "0.18.85", "1d_0_0.18.85", "month_0_0.18.85",
    }


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("8.0.2293", False),
        ("0.17.85", False),
        ("1d_0_0.17.85", True),
        ("month_0_0.18.85", True),
    ],
)
def test_statistic_keys_are_identified(key, expected):
    """Period keys go to statisticsMethod; plain resource ids do not."""
    metric = next(m for m in metrics_for(PLUG) if m.key == key)
    assert metric.is_statistic is expected


def test_unknown_model_has_no_metrics():
    assert metrics_for("lumi.sensor_magnet.aq2") == ()
    assert "lumi.sensor_magnet.aq2" not in supported_models()


# --------------------------------------------------------------------------
# device filtering
# --------------------------------------------------------------------------


def test_only_models_with_metrics_are_kept():
    devices = _supported_devices(
        [
            {"did": DID, "model": PLUG, "deviceName": "Socket", "parentDeviceId": HUB},
            {"did": "lumi.x", "model": "lumi.sensor_magnet.aq2", "deviceName": "Window"},
            {"did": "lumi.y"},                      # no model
            {"model": PLUG, "deviceName": "No did"},  # no did
        ]
    )
    assert [d.did for d in devices] == [DID]


def test_name_falls_back_to_model():
    devices = _supported_devices([{"did": DID, "model": PLUG}])
    assert devices[0].name == PLUG


# --------------------------------------------------------------------------
# coordinator
# --------------------------------------------------------------------------


async def test_poll_splits_keys_by_method(hass):
    """Plain rids and statistics keys go in separate calls."""
    client = _FakeClient(
        {
            "presentValueMethod": {"8.0.2293": "1612.5", "0.17.85": "113704.5"},
            "statisticsMethod": {"1d_0_0.17.85": "42.0"},
        }
    )
    coordinator = await _coordinator(hass, client)
    data = await coordinator._async_update_data()

    methods = {method for _, _, method in client.calls}
    assert methods == {"presentValueMethod", "statisticsMethod"}

    present = next(keys for _, keys, m in client.calls if m == "presentValueMethod")
    stats = next(keys for _, keys, m in client.calls if m == "statisticsMethod")
    assert set(present) == {"8.0.2293", "0.17.85", "8.0.2304", "0.18.85"}
    assert all("_" in k for k in stats)

    assert data[DID]["8.0.2293"] == 1612.5
    assert data[DID]["1d_0_0.17.85"] == 42.0


async def test_auth_failure_triggers_reauth(hass):
    """An auth error must reach HA as ConfigEntryAuthFailed, not UpdateFailed."""
    coordinator = await _coordinator(hass, _FakeClient(raises=AqaraCloudAuthError("nope")))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_one_bad_device_does_not_sink_the_rest(hass):
    """A device that errors is omitted; the healthy one still reports."""

    class _Selective(_FakeClient):
        async def query_view_data(self, token, did, read_keys, process_method):
            if did == "lumi.bad":
                raise RuntimeError("boom")
            return {"8.0.2293": "5.0"}

    devices = [_device(), _device(did="lumi.bad")]
    coordinator = await _coordinator(hass, _Selective(), devices)
    data = await coordinator._async_update_data()

    assert DID in data
    assert "lumi.bad" not in data


async def test_total_failure_raises_update_failed(hass):
    coordinator = await _coordinator(hass, _FakeClient(raises=RuntimeError("down")))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_no_devices_is_not_a_failure(hass):
    """An account with nothing to poll yields empty data, not an error."""
    coordinator = await _coordinator(hass, _FakeClient(), devices=[])
    assert await coordinator._async_update_data() == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"a": "1.5"}, {"a": 1.5}),
        ({"a": ""}, {}),            # never-taken reading
        ({"a": None}, {}),
        ({"a": "abc"}, {}),         # unparseable
        ({"a": "1", "b": ""}, {"a": 1.0}),
    ],
)
def test_coerce_keeps_only_numbers(raw, expected):
    assert _coerce(raw) == expected


# --------------------------------------------------------------------------
# device attachment - the behaviour proven by the spike
# --------------------------------------------------------------------------


async def test_attaches_to_existing_lanlink_device(hass):
    """When Aqara-LANLink owns this DID, reuse its identifiers."""
    entry = MockConfigEntry(domain=LANLINK_DOMAIN, data={})
    entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(LANLINK_DOMAIN, DID)},
        name="Kitchen Power Socket",
    )

    info = _device_info(hass, _device())
    assert info["identifiers"] == {(LANLINK_DOMAIN, DID)}
    # Nothing else is set: the owning integration names and models the device.
    assert "name" not in info


async def test_registers_own_device_without_lanlink(hass):
    """With no LANLink device present, stand up our own."""
    info = _device_info(hass, _device())
    assert info["identifiers"] == {(DOMAIN, DID)}
    assert info["name"] == "Kitchen Power Socket"
    assert info["model"] == PLUG
    # Deliberately absent: this integration never registers the hub, so a
    # via_device would always dangle. HA stops allowing that in 2025.12.
    assert "via_device" not in info


# --------------------------------------------------------------------------
# end to end: a real config entry setup
# --------------------------------------------------------------------------


async def test_entry_sets_up_and_creates_sensors(hass):
    """Set the entry up with a stubbed cloud and assert the sensors appear."""
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.aqaracloudlink.const import CONF_REGION, CONF_TOKEN, CONF_USER_ID

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def query_device_list(self, token):
            return [
                {"did": DID, "model": PLUG, "deviceName": "Kitchen Power Socket",
                 "parentDeviceId": HUB},
                {"did": "lumi.z", "model": "lumi.sensor_magnet.aq2",
                 "deviceName": "Window"},
            ]

        async def query_view_data(self, token, did, read_keys, process_method):
            if process_method == "presentValueMethod":
                return {"8.0.2293": "1612.5", "0.17.85": "113704.5",
                        "8.0.2304": "0.0", "0.18.85": "9976.8"}
            return {"1d_0_0.17.85": "42.0", "month_0_0.17.85": "1386.7",
                    "1d_0_0.18.85": "0.0", "month_0_0.18.85": "25.6"}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USER_ID: "u1", CONF_TOKEN: "tok", CONF_REGION: "EU"},
        unique_id="u1",
    )
    entry.add_to_hass(hass)

    with patch("custom_components.aqaracloudlink.AqaraCloudClient", _Client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    states = {
        s.entity_id: s.state
        for s in hass.states.async_all("sensor")
        if s.entity_id.startswith("sensor.kitchen_power_socket")
    }
    print("\n  entities created:")
    for eid, value in sorted(states.items()):
        print(f"    {eid} = {value}")

    # Eight sensors for the plug; the magnet sensor has no metrics so is skipped.
    assert len(states) == 8
    assert "1612.5" in states.values()

    # And it unloads cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
