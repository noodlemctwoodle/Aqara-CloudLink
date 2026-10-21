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
    _values_for,
)
from custom_components.aqara_cloudlink.metrics import (
    CloudMetric,
    Transport,
    metrics_for,
    supported_models,
)
from custom_components.aqara_cloudlink.entity import device_info as _device_info

PLUG = "lumi.plug.aeu002"
CAMERA = "lumi.camera.agl013"
DID = "lumi.54ef4410014f4b36"
HUB = "lumi3.8333b4a6a7b34e2b"


def _device(did: str = DID, model: str = PLUG) -> CloudDevice:
    return CloudDevice(did=did, model=model, name="Kitchen Power Socket")


class _FakeClient:
    """Records calls and replays canned responses keyed by read method."""

    def __init__(self, responses=None, raises=None, write_raises=None):
        self.responses = responses or {}
        self.raises = raises
        self.write_raises = write_raises
        self.calls: list[tuple[str, tuple[str, ...], str]] = []
        self.writes: list[tuple[str, dict]] = []

    async def query_view_data(self, token, did, read_keys, process_method):
        self.calls.append((did, tuple(read_keys), process_method))
        if self.raises is not None:
            raise self.raises
        return self.responses.get(process_method, {})

    async def query_resources_by_rid(self, token, did, rids):
        self.calls.append((did, tuple(rids), "resourceId"))
        if self.raises is not None:
            raise self.raises
        return self.responses.get("resourceId", {})

    async def write_resources(self, token, did, values):
        self.writes.append((did, dict(values)))
        if self.write_raises is not None:
            raise self.write_raises


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
        # Device totals for the period; LANLink serves only the running total.
        "1d_0_0.13.85", "month_0_0.13.85",
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


_PLAIN = CloudMetric(
    key="a", name="A", device_class=None, unit=None, state_class=None
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"a": "1.5"}, {"a": 1.5}),
        ({"a": ""}, {}),            # never-taken reading
        ({"a": None}, {}),
        ({"a": "abc"}, {}),         # unparseable
        ({}, {}),                   # key absent from this cycle
        ({"a": "1", "b": "9"}, {"a": 1.0}),   # unasked-for keys ignored
    ],
)
def test_values_keeps_only_numbers(raw, expected):
    assert _values_for((_PLAIN,), raw) == expected


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
    assert len(states) == 10


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
    assert len(ours) == 10, "the sensors must exist"
    assert {e.config_subentry_id for e in ours} == {None}


def test_entry_title_names_the_account():
    """Several accounts must be tellable apart in the UI."""
    from homeassistant.const import CONF_EMAIL

    from custom_components.aqara_cloudlink.config_flow import _entry_title

    assert _entry_title("EU", {CONF_EMAIL: "someone@example.com"}) == (
        "someone@example.com (EU)"
    )
    # A pasted user id and token give us no human-meaningful identifier.
    assert _entry_title("EU", {}) == "Aqara cloud (EU)"
    assert _entry_title("US", {CONF_EMAIL: "   "}) == "Aqara cloud (US)"


def test_subentry_strings_declare_only_implemented_flows():
    """initiate_flow must name only flows the handler implements.

    Declaring one that does not exist stops Home Assistant using the block,
    and the add button renders as a bare "+" with no label.
    """
    import json
    import pathlib

    from custom_components.aqara_cloudlink.config_flow import (
        DeviceSubentryFlowHandler,
    )

    root = pathlib.Path(__file__).parent.parent / "custom_components/aqara_cloudlink"
    for name in ["strings.json", "translations/en.json"]:
        data = json.loads((root / name).read_text())
        device = data["config_subentries"]["device"]
        declared = set(device["initiate_flow"])
        implemented = {
            attr.removeprefix("async_step_")
            for attr in dir(DeviceSubentryFlowHandler)
            if attr.startswith("async_step_")
        }
        assert declared <= implemented, f"{name}: {declared - implemented}"
        assert declared, f"{name}: no flow declared, button would be unlabelled"


# --------------------------------------------------------------------------
# camera storage - one resource id, several sensors
# --------------------------------------------------------------------------

#: Exactly what the cloud returned for a healthy 64 GB card, as a JSON string.
SD_HEALTHY = '{"freesize":60317,"othersize":0,"sdstatus":0,"totalsize":60318}'
#: And with the slot empty.
SD_ABSENT = '{"freesize":0,"othersize":0,"sdstatus":1,"totalsize":0}'


def test_camera_metrics_share_one_resource_id():
    metrics = metrics_for(CAMERA)
    assert {m.key for m in metrics} == {"13.25.85"}
    assert {m.field for m in metrics} == {"sdstatus", "totalsize", "freesize"}
    assert all(m.transport is Transport.RESOURCE_RID for m in metrics)
    # None of them is a statistics read, despite the underscore rule.
    assert not any(m.is_statistic for m in metrics)


def test_shared_key_metrics_get_distinct_ids():
    suffixes = {m.unique_suffix for m in metrics_for(CAMERA)}
    assert suffixes == {
        "13.25.85_sdstatus", "13.25.85_totalsize", "13.25.85_freesize"
    }


def test_plain_metrics_keep_their_key_as_suffix():
    """Guards the registry: ids already in use must not change."""
    assert all(m.unique_suffix == m.key for m in metrics_for(PLUG))


def test_json_payload_is_split_across_metrics():
    values = _values_for(metrics_for(CAMERA), {"13.25.85": SD_HEALTHY})
    assert values == {
        "13.25.85_sdstatus": 0.0,
        "13.25.85_totalsize": 60318.0,
        "13.25.85_freesize": 60317.0,
    }


def test_sd_status_direction_is_zero_for_healthy():
    """0 means a working card, 1 means none. Confirmed by pulling the card.

    The intuitive reading is the opposite, so this is pinned deliberately.
    """
    status = next(m for m in metrics_for(CAMERA) if m.field == "sdstatus")
    assert status.label(0) == "ok"
    assert status.label(1) == "no_card"
    assert status.label(7) is None       # unknown code, not guessed at
    assert status.options == ["ok", "no_card"]


def test_absent_card_reports_no_card():
    values = _values_for(metrics_for(CAMERA), {"13.25.85": SD_ABSENT})
    assert values["13.25.85_sdstatus"] == 1.0
    assert values["13.25.85_totalsize"] == 0.0


def test_malformed_payload_yields_nothing():
    """A non-JSON body leaves the sensors unavailable rather than wrong."""
    assert _values_for(metrics_for(CAMERA), {"13.25.85": "not json"}) == {}
    assert _values_for(metrics_for(CAMERA), {"13.25.85": ""}) == {}


def test_missing_field_is_skipped():
    """A payload without one field still serves the others."""
    values = _values_for(metrics_for(CAMERA), {"13.25.85": '{"sdstatus":0}'})
    assert values == {"13.25.85_sdstatus": 0.0}


async def test_camera_poll_uses_the_rid_call_once(hass):
    """The shared resource id is requested once, not once per sensor."""
    client = _FakeClient({"resourceId": {"13.25.85": SD_HEALTHY}})
    device = _device(did="lumi3.b35942fcbb88606d", model=CAMERA)
    coordinator = await _coordinator(hass, client, [device])
    data = await coordinator._async_update_data()

    assert len(client.calls) == 1
    _, rids, method = client.calls[0]
    assert method == "resourceId"
    assert rids == ("13.25.85",)
    assert data[device.did]["13.25.85_freesize"] == 60317.0


async def test_camera_makes_no_view_data_call(hass):
    """Nothing the camera needs is served by the device-page call."""
    client = _FakeClient({"resourceId": {"13.25.85": SD_HEALTHY}})
    device = _device(did="lumi3.b35942fcbb88606d", model=CAMERA)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator._async_update_data()

    assert all(method == "resourceId" for _, _, method in client.calls)


async def test_sd_status_sensor_reports_a_state_name(hass):
    """End to end: the enum sensor shows "ok", not 0.0."""
    from custom_components.aqara_cloudlink.sensor import AqaraCloudSensor

    client = _FakeClient({"resourceId": {"13.25.85": SD_HEALTHY}})
    device = _device(did="lumi3.b35942fcbb88606d", model=CAMERA)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    status, capacity = (
        next(m for m in metrics_for(CAMERA) if m.field == field)
        for field in ("sdstatus", "totalsize")
    )

    assert AqaraCloudSensor(coordinator, device, status).native_value == "ok"
    assert AqaraCloudSensor(coordinator, device, capacity).native_value == 60318.0


async def test_unknown_status_code_leaves_the_sensor_unavailable(hass):
    """An unmapped number must not be handed to an enum sensor."""
    from custom_components.aqara_cloudlink.sensor import AqaraCloudSensor

    client = _FakeClient({"resourceId": {"13.25.85": '{"sdstatus":9}'}})
    device = _device(did="lumi3.b35942fcbb88606d", model=CAMERA)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    status = next(m for m in metrics_for(CAMERA) if m.field == "sdstatus")
    sensor = AqaraCloudSensor(coordinator, device, status)
    assert sensor.native_value is None
    assert sensor.available is False


def test_every_enum_metric_has_its_states_translated():
    """Guard for the next model added: an enum with no strings shows raw keys.

    Home Assistant falls back to the option string itself when a state has no
    translation, so "no_card" would reach the dashboard verbatim. This checks
    strings.json covers every state of every enum metric.
    """
    import json
    from pathlib import Path

    from custom_components.aqara_cloudlink.metrics import METRICS

    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components/aqara_cloudlink/strings.json"
        ).read_text()
    )
    sensors = strings.get("entity", {}).get("sensor", {})

    for model, metrics in METRICS.items():
        for metric in metrics:
            if not metric.states:
                assert metric.translation_key is None, (
                    f"{model} {metric.key}: translation_key without states"
                )
                continue
            key = metric.translation_key
            assert key, f"{model} {metric.key}: enum metric needs a translation_key"
            assert key in sensors, f"strings.json has no entity.sensor.{key}"
            translated = sensors[key].get("state", {})
            missing = {name for _, name in metric.states} - set(translated)
            assert not missing, f"{key} is missing state strings for {missing}"


# --------------------------------------------------------------------------
# cloud actions - the only writes this integration makes
# --------------------------------------------------------------------------

CAMERA_DID = "lumi3.b35942fcbb88606d"


def _camera_coordinator_args(sd_payload: str):
    client = _FakeClient({"resourceId": {"13.25.85": sd_payload}})
    return client, _device(did=CAMERA_DID, model=CAMERA)


def test_only_camera_models_have_actions():
    from custom_components.aqara_cloudlink.metrics import actions_for

    assert actions_for(PLUG) == ()
    for model in ("lumi.camera.agl005", "lumi.camera.agl013"):
        actions = actions_for(model)
        assert len(actions) == 1
        assert actions[0].key == "14.34.85"
        assert actions[0].value == 1


def test_a_model_with_only_actions_is_still_offered():
    """Eligibility must not depend on a model having readable metrics."""
    from custom_components.aqara_cloudlink import metrics as metrics_mod

    records = [{"did": "d1", "model": "lumi.fake.actiononly", "deviceName": "Thing"}]
    assert eligible_devices(records, set()) == []

    metrics_mod.ACTIONS["lumi.fake.actiononly"] = metrics_mod._CAMERA_ACTIONS
    try:
        assert eligible_devices(records, set()) == [
            ("d1", "lumi.fake.actiononly", "Thing")
        ]
    finally:
        del metrics_mod.ACTIONS["lumi.fake.actiononly"]


async def test_format_button_is_unavailable_without_a_card(hass):
    """The guard that keeps a destructive write out of reach when it is moot."""
    from custom_components.aqara_cloudlink.button import AqaraCloudActionButton

    client, device = _camera_coordinator_args(SD_ABSENT)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    button = AqaraCloudActionButton(coordinator, device, device.actions[0])
    assert button.available is False


async def test_format_button_is_available_with_a_healthy_card(hass):
    from custom_components.aqara_cloudlink.button import AqaraCloudActionButton

    client, device = _camera_coordinator_args(SD_HEALTHY)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    button = AqaraCloudActionButton(coordinator, device, device.actions[0])
    assert button.available is True


async def test_pressing_writes_the_captured_payload(hass):
    """The body must match what the Aqara app sends: rid -> integer."""
    from custom_components.aqara_cloudlink.button import AqaraCloudActionButton

    client, device = _camera_coordinator_args(SD_HEALTHY)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    await AqaraCloudActionButton(coordinator, device, device.actions[0]).async_press()

    assert client.writes == [(CAMERA_DID, {"14.34.85": 1})]

    # The press requests a debounced refresh; stop it leaving a pending timer.
    await coordinator.async_shutdown()


async def test_write_failure_surfaces_in_the_ui(hass):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.aqara_cloudlink.button import AqaraCloudActionButton

    client = _FakeClient(
        {"resourceId": {"13.25.85": SD_HEALTHY}}, write_raises=RuntimeError("nope")
    )
    device = _device(did=CAMERA_DID, model=CAMERA)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    with pytest.raises(HomeAssistantError):
        await AqaraCloudActionButton(
            coordinator, device, device.actions[0]
        ).async_press()


async def test_write_auth_failure_triggers_reauth(hass):
    from custom_components.aqara_cloudlink.button import AqaraCloudActionButton

    client = _FakeClient(
        {"resourceId": {"13.25.85": SD_HEALTHY}},
        write_raises=AqaraCloudAuthError("rejected"),
    )
    device = _device(did=CAMERA_DID, model=CAMERA)
    coordinator = await _coordinator(hass, client, [device])
    await coordinator.async_refresh()

    with pytest.raises(ConfigEntryAuthFailed):
        await AqaraCloudActionButton(
            coordinator, device, device.actions[0]
        ).async_press()


def test_every_action_has_its_name_translated():
    """Same guard as for enum sensors: an untranslated key reaches the UI raw."""
    import json
    from pathlib import Path

    from custom_components.aqara_cloudlink.metrics import ACTIONS

    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components/aqara_cloudlink/strings.json"
        ).read_text()
    )
    buttons = strings.get("entity", {}).get("button", {})
    for model, actions in ACTIONS.items():
        for action in actions:
            key = action.translation_key
            assert key, f"{model} {action.key}: action needs a translation_key"
            assert key in buttons, f"strings.json has no entity.button.{key}"
            assert buttons[key].get("name")


def test_action_preconditions_name_a_real_metric():
    """A typo in available_when would silently disable the button forever."""
    from custom_components.aqara_cloudlink.metrics import ACTIONS, metrics_for

    for model, actions in ACTIONS.items():
        suffixes = {m.unique_suffix for m in metrics_for(model)}
        for action in actions:
            if action.available_when is None:
                continue
            key, _ = action.available_when
            assert key in suffixes, (
                f"{model}: available_when names {key}, which is not a metric"
            )
