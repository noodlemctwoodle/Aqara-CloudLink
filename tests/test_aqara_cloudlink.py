"""Tests for the Aqara CloudLink cloud integration."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aqara_cloudlink.config_flow import eligible_devices
from custom_components.aqara_cloudlink.cloud import AqaraCloudAuthError
from custom_components.aqara_cloudlink.const import DOMAIN, LANLINK_DOMAIN
from custom_components.aqara_cloudlink.coordinator import (
    AqaraCloudCoordinator,
    CloudDevice,
    _coerce,
)
from custom_components.aqara_cloudlink.metrics import metrics_for, supported_models
from custom_components.aqara_cloudlink.sensor import _device_info

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
# credential resolution and error classification
# --------------------------------------------------------------------------


async def test_pasted_token_skips_the_login(hass):
    """A user id and token together must not spend a login.

    Aqara rate-limits the login endpoint, so this path exists precisely to
    avoid calling it.
    """
    from unittest.mock import patch

    from custom_components.aqara_cloudlink.config_flow import _resolve_tokens

    with patch(
        "custom_components.aqara_cloudlink.config_flow.AqaraCloudClient"
    ) as client:
        tokens = await _resolve_tokens(
            hass, "EU", {"user_id": "u1", "token": "t1", "email": "e@x", "password": "p"}
        )

    assert (tokens.user_id, tokens.token) == ("u1", "t1")
    client.assert_not_called()


async def test_email_and_password_logs_in(hass):
    from unittest.mock import AsyncMock, patch

    from custom_components.aqara_cloudlink.cloud import AqaraTokens
    from custom_components.aqara_cloudlink.config_flow import _resolve_tokens

    login = AsyncMock(return_value=AqaraTokens("u2", "t2", {}))
    with patch(
        "custom_components.aqara_cloudlink.config_flow.AqaraCloudClient"
    ) as client:
        client.return_value.login = login
        tokens = await _resolve_tokens(hass, "EU", {"email": "e@x", "password": "p"})

    login.assert_awaited_once_with("e@x", "p")
    assert tokens.token == "t2"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "e@x"},                       # password missing
        {"user_id": "u1"},                      # token missing
        {"token": "t1"},                        # user id missing
        {"email": "  ", "password": "p"},       # whitespace is not a value
    ],
)
async def test_incomplete_credentials_rejected(hass, payload):
    from custom_components.aqara_cloudlink.config_flow import _resolve_tokens

    with pytest.raises(ValueError):
        await _resolve_tokens(hass, "EU", payload)


def test_error_classification():
    """A rate-limited login must not be reported as invalid credentials."""
    from custom_components.aqara_cloudlink.cloud import AqaraAuthError
    from custom_components.aqara_cloudlink.config_flow import _classify

    assert _classify(AqaraCloudAuthError("bad password")) == "invalid_auth"
    assert _classify(AqaraAuthError("code 106")) == "cannot_connect"
    assert _classify(RuntimeError("boom")) == "unknown"


# --------------------------------------------------------------------------
# attach-later: the ordering case that split the device card in practice
# --------------------------------------------------------------------------


def _our_device_with_sensor(hass, entry):
    """Register a device of our own plus one sensor on it, as setup would."""
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, DID)},
        name="Kitchen Power Socket",
    )
    ent = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{DID}_8.0.2293",
        config_entry=entry, device_id=dev.id,
    )
    return dev, ent


async def test_relink_moves_entities_when_lanlink_appears(hass):
    """LANLink arriving later must pull our sensors onto its card."""
    from custom_components.aqara_cloudlink.linker import async_relink_devices

    ours_entry = MockConfigEntry(domain=DOMAIN, data={})
    ours_entry.add_to_hass(hass)
    our_dev, our_ent = _our_device_with_sensor(hass, ours_entry)

    # Aqara-LANLink shows up afterwards.
    ll_entry = MockConfigEntry(domain=LANLINK_DOMAIN, data={})
    ll_entry.add_to_hass(hass)
    ll_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=ll_entry.entry_id,
        identifiers={(LANLINK_DOMAIN, DID)},
        name="Kitchen Power Socket",
    )

    assert async_relink_devices(hass, [DID]) == 1

    moved = er.async_get(hass).async_get(our_ent.entity_id)
    assert moved.device_id == ll_dev.id
    # The card we created in the meantime should be gone, not left empty.
    assert dr.async_get(hass).async_get(our_dev.id) is None


async def test_relink_is_a_no_op_without_lanlink(hass):
    """With Aqara-LANLink absent, our own device must be left alone."""
    from custom_components.aqara_cloudlink.linker import async_relink_devices

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    our_dev, our_ent = _our_device_with_sensor(hass, entry)

    assert async_relink_devices(hass, [DID]) == 0
    assert dr.async_get(hass).async_get(our_dev.id) is not None
    assert er.async_get(hass).async_get(our_ent.entity_id).device_id == our_dev.id


async def test_relink_leaves_other_integrations_entities(hass):
    """Only our own entities move; a third party's stay put."""
    from custom_components.aqara_cloudlink.linker import async_relink_devices

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    our_dev, our_ent = _our_device_with_sensor(hass, entry)

    other = MockConfigEntry(domain="someone_else", data={})
    other.add_to_hass(hass)
    foreign = er.async_get(hass).async_get_or_create(
        "sensor", "someone_else", "x1", config_entry=other, device_id=our_dev.id
    )

    ll_entry = MockConfigEntry(domain=LANLINK_DOMAIN, data={})
    ll_entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=ll_entry.entry_id, identifiers={(LANLINK_DOMAIN, DID)}
    )

    async_relink_devices(hass, [DID])

    assert er.async_get(hass).async_get(foreign.entity_id).device_id == our_dev.id
    # Something still lives there, so the device must survive.
    assert dr.async_get(hass).async_get(our_dev.id) is not None


async def test_listener_relinks_on_device_registry_event(hass):
    """The tracker must react to LANLink's device being created."""
    from custom_components.aqara_cloudlink.linker import async_track_lanlink_devices

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    _, our_ent = _our_device_with_sensor(hass, entry)

    unsub = async_track_lanlink_devices(hass, [DID])

    ll_entry = MockConfigEntry(domain=LANLINK_DOMAIN, data={})
    ll_entry.add_to_hass(hass)
    ll_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=ll_entry.entry_id, identifiers={(LANLINK_DOMAIN, DID)}
    )
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(our_ent.entity_id).device_id == ll_dev.id
    unsub()


async def test_relink_noop_when_already_on_the_lanlink_device(hass):
    """The happy path - LANLink first - must not be disturbed."""
    from custom_components.aqara_cloudlink.linker import async_relink_devices

    ll_entry = MockConfigEntry(domain=LANLINK_DOMAIN, data={})
    ll_entry.add_to_hass(hass)
    ll_dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=ll_entry.entry_id, identifiers={(LANLINK_DOMAIN, DID)}
    )
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    ent = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{DID}_8.0.2293", config_entry=entry, device_id=ll_dev.id
    )

    assert async_relink_devices(hass, [DID]) == 0
    assert er.async_get(hass).async_get(ent.entity_id).device_id == ll_dev.id


async def test_relink_repairs_an_already_split_install(hass):
    """Repair the state a reload can leave behind.

    Our entities sit on a second device that carries Aqara-LANLink's
    identifiers without being Aqara-LANLink's device - which is how this
    installation actually ended up, so the fix has to cope with it rather
    than only preventing it.
    """
    from custom_components.aqara_cloudlink.linker import async_relink_devices

    ll_entry = MockConfigEntry(domain=LANLINK_DOMAIN, data={})
    ll_entry.add_to_hass(hass)
    real = dr.async_get(hass).async_get_or_create(
        config_entry_id=ll_entry.entry_id,
        identifiers={(LANLINK_DOMAIN, DID)},
        name="Kitchen Power Socket",
    )

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    stray = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{DID}-stray")},
        name="Aqara cloud (EU)",
    )
    ent = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{DID}_8.0.2293",
        config_entry=entry, device_id=stray.id,
    )

    assert async_relink_devices(hass, [DID]) == 1
    assert er.async_get(hass).async_get(ent.entity_id).device_id == real.id
    assert dr.async_get(hass).async_get(stray.id) is None


# --------------------------------------------------------------------------
# device picker
# --------------------------------------------------------------------------

_RECORDS = [
    {"did": DID, "model": PLUG, "deviceName": "Kitchen Power Socket"},
    {"did": "lumi.zzz", "model": "lumi.sensor_magnet.aq2", "deviceName": "Window"},
    {"did": "lumi.aaa", "model": PLUG, "deviceName": "Another Socket"},
    {"did": "lumi.nomodel", "deviceName": "Broken"},
]


def test_picker_offers_only_models_with_metrics():
    """A device we have no cloud metrics for must not be offerable."""
    got = eligible_devices(_RECORDS, already=set())
    assert [d[0] for d in got] == ["lumi.aaa", DID]  # sorted by name
    assert all(d[1] == PLUG for d in got)


def test_picker_hides_devices_already_added():
    got = eligible_devices(_RECORDS, already={DID})
    assert [d[0] for d in got] == ["lumi.aaa"]


def test_picker_falls_back_to_model_for_a_nameless_device():
    got = eligible_devices([{"did": DID, "model": PLUG}], already=set())
    assert got[0][2] == PLUG


def test_added_dids_reads_device_subentries(hass):
    """Only device subentries count, and only those carrying a DID."""
    from homeassistant.config_entries import ConfigSubentryData

    from custom_components.aqara_cloudlink.config_flow import added_dids
    from custom_components.aqara_cloudlink.const import SUBENTRY_TYPE_DEVICE

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        subentries_data=[
            ConfigSubentryData(
                data={"did": DID, "model": PLUG, "name": "Socket"},
                subentry_type=SUBENTRY_TYPE_DEVICE,
                title="Socket",
                unique_id=DID,
            ),
            ConfigSubentryData(
                data={"something": "else"},
                subentry_type="not_a_device",
                title="Other",
                unique_id="x",
            ),
        ],
    )
    entry.add_to_hass(hass)
    assert added_dids(entry) == {DID}


async def test_setup_builds_devices_from_subentries(hass):
    """Entities come from what was picked, not from everything on the account."""
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData

    from custom_components.aqara_cloudlink.const import (
        CONF_REGION, CONF_TOKEN, CONF_USER_ID, SUBENTRY_TYPE_DEVICE,
    )

    class _Client:
        def __init__(self, *a, **k): pass
        async def query_view_data(self, token, did, read_keys, process_method):
            if process_method == "presentValueMethod":
                return {"8.0.2293": "12.5", "0.17.85": "1.0",
                        "8.0.2304": "0.0", "0.18.85": "2.0"}
            return {"1d_0_0.17.85": "3.0", "month_0_0.17.85": "4.0",
                    "1d_0_0.18.85": "0.0", "month_0_0.18.85": "5.0"}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USER_ID: "u1", CONF_TOKEN: "tok", CONF_REGION: "EU"},
        unique_id="u1",
        subentries_data=[
            ConfigSubentryData(
                data={"did": DID, "model": PLUG, "name": "Kitchen Power Socket"},
                subentry_type=SUBENTRY_TYPE_DEVICE,
                title="Kitchen Power Socket",
                unique_id=DID,
            )
        ],
    )
    entry.add_to_hass(hass)

    with patch("custom_components.aqara_cloudlink.AqaraCloudClient", _Client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    states = [s for s in hass.states.async_all("sensor")
              if s.entity_id.startswith("sensor.kitchen_power_socket")]
    assert len(states) == 8


async def test_setup_with_no_subentries_loads_empty(hass):
    """Before anything is picked the entry must still load, with no entities."""
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.aqara_cloudlink.const import (
        CONF_REGION, CONF_TOKEN, CONF_USER_ID,
    )

    class _Client:
        def __init__(self, *a, **k): pass

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USER_ID: "u2", CONF_TOKEN: "tok", CONF_REGION: "EU"},
        unique_id="u2",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.aqara_cloudlink.AqaraCloudClient", _Client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert not [s for s in hass.states.async_all("sensor")
                if s.entity_id.startswith("sensor.kitchen")]

async def test_entities_are_not_claimed_for_the_subentry(hass):
    """Entities must NOT claim a subentry.

    They attach to a device owned by Aqara-LANLink's config entry, and
    claiming a subentry on another entry's device drops them entirely -
    all eight disappeared from the registry on a live instance.
    """
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigSubentryData

    from custom_components.aqara_cloudlink.const import (
        CONF_REGION, CONF_TOKEN, CONF_USER_ID, SUBENTRY_TYPE_DEVICE,
    )

    class _Client:
        def __init__(self, *a, **k): pass
        async def query_view_data(self, token, did, read_keys, process_method):
            return {k: "1.0" for k in read_keys}

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USER_ID: "u3", CONF_TOKEN: "tok", CONF_REGION: "EU"},
        unique_id="u3",
        subentries_data=[
            ConfigSubentryData(
                data={"did": DID, "model": PLUG, "name": "Kitchen Power Socket"},
                subentry_type=SUBENTRY_TYPE_DEVICE,
                title="Kitchen Power Socket",
                unique_id=DID,
            )
        ],
    )
    entry.add_to_hass(hass)

    with patch("custom_components.aqara_cloudlink.AqaraCloudClient", _Client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ours = [e for e in er.async_get(hass).entities.values() if e.platform == DOMAIN]
    assert len(ours) == 8, "the sensors must exist"
    assert {e.config_subentry_id for e in ours} == {None}
