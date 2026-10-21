"""Per-model table of values the Aqara cloud serves but LANLink does not.

Keys are Aqara model strings. Each entry lists the legacy resource ids (and
the app's statistics keys) that carry a value the hub will not answer for over
LANLink, together with how the resulting sensor should present.

Adding a model means adding an entry here. Nothing else in the integration is
model-aware.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower


@dataclass(frozen=True, slots=True)
class CloudMetric:
    """One cloud-read value and how to present it.

    Attributes:
        key: Resource id ("0.17.85") or app statistics key
            ("1d_0_0.17.85"). Keys containing "_" are read with the
            statistics method; the rest with the present-value method.
        name: Entity name, appended to the device name by Home Assistant.
        device_class: Sensor device class.
        unit: Native unit of the value as the cloud returns it.
        state_class: Sensor state class.
    """

    key: str
    name: str
    device_class: SensorDeviceClass
    unit: str
    state_class: SensorStateClass

    @property
    def is_statistic(self) -> bool:
        """True when this key must be read with the statistics method."""
        return "_" in self.key


def _socket(index: int, energy_rid: str, power_rid: str) -> tuple[CloudMetric, ...]:
    """Build the four metrics a metered outlet exposes."""
    return (
        CloudMetric(
            key=power_rid,
            name=f"Socket {index} power",
            device_class=SensorDeviceClass.POWER,
            unit=UnitOfPower.WATT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        CloudMetric(
            key=energy_rid,
            name=f"Socket {index} energy",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        # Period totals as the Aqara app shows them. They reset at the start of
        # each period, which TOTAL_INCREASING treats as a new cycle.
        CloudMetric(
            key=f"1d_0_0.{energy_rid.removeprefix('0.')}",
            name=f"Socket {index} energy today",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        CloudMetric(
            key=f"month_0_0.{energy_rid.removeprefix('0.')}",
            name=f"Socket {index} energy this month",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
    )


#: model string -> metrics the cloud serves for it.
METRICS: dict[str, tuple[CloudMetric, ...]] = {
    # Wall Outlet H2 (EU). The hub serves only the device totals over LANLink;
    # per-socket figures answer on the cloud alone. Socket 1 shares endpoint 2
    # with the USB-C port, so its figures include USB draw.
    "lumi.plug.aeu002": (
        *_socket(1, energy_rid="0.17.85", power_rid="8.0.2293"),
        *_socket(2, energy_rid="0.18.85", power_rid="8.0.2304"),
    ),
}


def metrics_for(model: str) -> tuple[CloudMetric, ...]:
    """Return the cloud metrics for a model, or an empty tuple."""
    return METRICS.get(model, ())


def supported_models() -> frozenset[str]:
    """Return every model this integration can add sensors for."""
    return frozenset(METRICS)
