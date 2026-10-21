"""Maintainer overrides for this model. Preserved across catalogue regen.
Add entries here for V3 spec corrections, custom device_class, etc.
"""
# NOTE: do NOT run generate_catalogue.py on this model before v2 - it is not yet settings-aware and would wipe the data.json `settings` block.
from custom_components.aqaralink.device.settings import SettingSpec
from custom_components.aqaralink.device.traits import TraitSpec

OVERRIDES: dict[str, TraitSpec | None] = {
    # "wire_path": TraitSpec(...)  # replace generated entry
    # "wire_path": None            # drop generated entry
    # No voltage sensor on this hardware: the V3 spec lists CurrentVoltage, but the
    # plug reads 0 V under load and never reports it (Z2M's converter likewise
    # builds its meter with voltage: false).
    "1.140.32944": None,
    # Per-outlet metering. The hub serves none of these over LANLink -- not as
    # trait paths, not as rids -- so they are read from the Aqara cloud by
    # legacy resource id, as the Aqara Home app does.
    # Endpoint 2 is socket 1 (4.1.85), endpoint 3 is socket 2 (4.2.85).
    "8.0.2293": TraitSpec(id="8.0.2293", name="Socket 1 power", wire_path="8.0.2293", data_type="float", endpoint_id=2, platform="sensor", device_class="power", state_class="measurement", unit_of_measurement="W", cloud_poll=True),
    "0.17.85": TraitSpec(id="0.17.85", name="Socket 1 energy", wire_path="0.17.85", data_type="float", endpoint_id=2, platform="sensor", device_class="energy", state_class="total_increasing", unit_of_measurement="Wh", cloud_poll=True),
    "8.0.2304": TraitSpec(id="8.0.2304", name="Socket 2 power", wire_path="8.0.2304", data_type="float", endpoint_id=3, platform="sensor", device_class="power", state_class="measurement", unit_of_measurement="W", cloud_poll=True),
    "0.18.85": TraitSpec(id="0.18.85", name="Socket 2 energy", wire_path="0.18.85", data_type="float", endpoint_id=3, platform="sensor", device_class="energy", state_class="total_increasing", unit_of_measurement="Wh", cloud_poll=True),
    # Period totals, as shown in the Aqara Home app: the app's statistics keys
    # for today ("1d_0_0.<rid>") and this month ("month_0_0.<rid>"). They reset
    # at the start of each period, which total_increasing treats as a new cycle.
    "1d_0_0.17.85": TraitSpec(id="1d_0_0.17.85", name="Socket 1 energy today", wire_path="1d_0_0.17.85", data_type="float", endpoint_id=2, platform="sensor", device_class="energy", state_class="total_increasing", unit_of_measurement="Wh", cloud_poll=True),
    "month_0_0.17.85": TraitSpec(id="month_0_0.17.85", name="Socket 1 energy this month", wire_path="month_0_0.17.85", data_type="float", endpoint_id=2, platform="sensor", device_class="energy", state_class="total_increasing", unit_of_measurement="Wh", cloud_poll=True),
    "1d_0_0.18.85": TraitSpec(id="1d_0_0.18.85", name="Socket 2 energy today", wire_path="1d_0_0.18.85", data_type="float", endpoint_id=3, platform="sensor", device_class="energy", state_class="total_increasing", unit_of_measurement="Wh", cloud_poll=True),
    "month_0_0.18.85": TraitSpec(id="month_0_0.18.85", name="Socket 2 energy this month", wire_path="month_0_0.18.85", data_type="float", endpoint_id=3, platform="sensor", device_class="energy", state_class="total_increasing", unit_of_measurement="Wh", cloud_poll=True),
}

# Tuning layer for the rid-keyed settings authored in data.json. Entries here
# are merged onto the data.json `settings` block at load time. Empty by default;
# data.json is the end-goal home for confirmed settings.
SETTINGS_OVERRIDES: dict[str, SettingSpec | None] = {
    # "8.0.2042": SettingSpec(rid="8.0.2042", name="Max power", platform="number", min=100, max=4000, unit="W")  # replace data.json entry
    # "8.0.2096": None  # drop a data.json entry
    # "9.9.99": SettingSpec(rid="9.9.99", name="New setting", platform="switch")  # add a new rid not in data.json
}
