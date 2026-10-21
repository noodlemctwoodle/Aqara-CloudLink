"""lumi.plug.aeu002 overrides.

The hub serves no per-outlet power or energy over LANLink, so the four meters
are authored as cloud-polled traits (TraitSpec.cloud_poll) keyed by the legacy
resource ids the Aqara Home app reads. The spec's CurrentVoltage trait is
dropped: the hardware has no voltage sensor.
"""
from __future__ import annotations

import pytest

from custom_components.aqaralink.device import catalog
from custom_components.aqaralink.device.build_descriptors import build_descriptors
from custom_components.aqaralink.device.descriptors import SensorDescriptor
from custom_components.aqaralink.device.overlay import Overlay

MODEL = "lumi.plug.aeu002"

# rid -> (name, endpoint, device_class, state_class, unit)
METERS = {
    "8.0.2293": ("Socket 1 power", 2, "power", "measurement", "W"),
    "0.17.85": ("Socket 1 energy", 2, "energy", "total_increasing", "Wh"),
    "8.0.2304": ("Socket 2 power", 3, "power", "measurement", "W"),
    "0.18.85": ("Socket 2 energy", 3, "energy", "total_increasing", "Wh"),
    "1d_0_0.17.85": ("Socket 1 energy today", 2, "energy", "total_increasing", "Wh"),
    "month_0_0.17.85": ("Socket 1 energy this month", 2, "energy", "total_increasing", "Wh"),
    "1d_0_0.18.85": ("Socket 2 energy today", 3, "energy", "total_increasing", "Wh"),
    "month_0_0.18.85": ("Socket 2 energy this month", 3, "energy", "total_increasing", "Wh"),
}


@pytest.fixture(autouse=True)
def _reset_catalog():
    catalog.reset_for_tests()
    yield
    catalog.reset_for_tests()


@pytest.mark.parametrize("rid", sorted(METERS))
def test_meter_is_catalogued_as_cloud_polled_sensor(rid: str) -> None:
    name, endpoint, device_class, state_class, unit = METERS[rid]
    spec = catalog.get_trait(MODEL, rid)

    assert spec is not None
    assert spec.cloud_poll is True
    assert spec.id == spec.wire_path == rid
    assert spec.name == name
    assert spec.endpoint_id == endpoint
    assert spec.platform == "sensor"
    assert spec.device_class == device_class
    assert spec.state_class == state_class
    assert spec.unit_of_measurement == unit


def test_only_the_meters_are_cloud_polled() -> None:
    polled = {
        rid for rid, spec in catalog.all_traits_for_model(MODEL).items()
        if spec.cloud_poll
    }
    assert polled == set(METERS)


@pytest.mark.parametrize("rid", sorted(METERS))
def test_meter_builds_sensor_descriptor(rid: str) -> None:
    name, _endpoint, device_class, state_class, unit = METERS[rid]
    key = f"auto_{rid.replace('.', '_')}"
    by_key = {
        d.key: d for d in build_descriptors(MODEL, Overlay())
        if isinstance(d, SensorDescriptor)
    }

    desc = by_key[key]
    assert desc.name == name
    assert desc.trait.id == rid
    assert desc.device_class == device_class
    assert desc.state_class == state_class
    assert desc.native_unit_of_measurement == unit


def test_unsupported_voltage_is_dropped() -> None:
    assert catalog.get_trait(MODEL, "1.140.32944") is None
    # The device-level power and energy on the same cluster stay.
    assert catalog.get_trait(MODEL, "1.140.32945") is not None
    assert catalog.get_trait(MODEL, "1.140.32946") is not None


def test_no_voltage_descriptor_is_built() -> None:
    keys = {d.key for d in build_descriptors(MODEL, Overlay())}
    assert "auto_1_140_32944" not in keys
    assert "auto_1_140_32945" in keys




def test_display_metadata_unchanged() -> None:
    meta = catalog.get_display_metadata(MODEL)
    assert meta is not None
    assert meta.manufacturer == "Aqara"
    assert meta.display_name == "Wall Outlet H2"
