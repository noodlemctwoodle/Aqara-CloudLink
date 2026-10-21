"""Per-model table of values the Aqara cloud serves but LANLink does not.

Keys are Aqara model strings. Each entry lists the legacy resource ids (and
the app's statistics keys) that carry a value the hub will not answer for over
LANLink, together with how the resulting sensor should present.

Adding a model means adding an entry here. Nothing else in the integration is
model-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfInformation, UnitOfPower


class Transport(StrEnum):
    """Which cloud call serves a metric.

    VIEW_DATA is the app's device-page read (`view/data/query/new`), keyed by
    legacy resource id. RESOURCE_RID is `res/query/by/resourceId`, which is
    what serves values the device-page call does not - camera storage, for one.
    """

    VIEW_DATA = "view_data"
    RESOURCE_RID = "resource_rid"


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
    device_class: SensorDeviceClass | None
    unit: str | None
    state_class: SensorStateClass | None
    transport: Transport = Transport.VIEW_DATA
    #: When the value is a JSON object, the field to take from it. Camera
    #: storage arrives as {"freesize":…,"totalsize":…,"sdstatus":…} under a
    #: single resource id, so several sensors share one key.
    field: str | None = None
    #: For a value that is a state rather than a measurement: the numbers the
    #: cloud reports, paired with the names to show. Presence of this makes
    #: the sensor an enum.
    states: tuple[tuple[int, str], ...] = ()
    #: Names the entry under `entity.sensor` in strings.json. Required for a
    #: metric with states, so the state names are displayed as written there
    #: rather than as the raw option strings.
    translation_key: str | None = None

    @property
    def is_statistic(self) -> bool:
        """True when this key must be read with the statistics method."""
        return self.transport is Transport.VIEW_DATA and "_" in self.key

    @property
    def unique_suffix(self) -> str:
        """Distinguishes sensors that share a resource id."""
        return f"{self.key}_{self.field}" if self.field else self.key

    @property
    def options(self) -> list[str] | None:
        """Enum option names, in the order given, or None for a number."""
        return [name for _, name in self.states] if self.states else None

    def label(self, value: float) -> str | None:
        """Map a reported number to its state name, or None if unrecognised."""
        for number, name in self.states:
            if number == value:
                return name
        return None


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


#: Camera SD card. The cloud serves the whole payload under one resource id,
#: as a JSON object: {"freesize": 60317, "othersize": 0, "sdstatus": 0,
#: "totalsize": 60318}. Sizes are MB - 60332 MB reads as 58.92 GB, which is
#: what the app shows for a nominal 64 GB card.
#:
#: sdstatus was confirmed by removing and reinserting a card: 0 with a working
#: card present, 1 with the slot empty. Note the direction - 0 is the healthy
#: state, not the fault.
#:
#: This resource id is not served by the device-page call; it answers only on
#: res/query/by/resourceId.
_CAMERA_STORAGE: tuple[CloudMetric, ...] = (
    CloudMetric(
        key="13.25.85",
        field="sdstatus",
        name="SD card",
        device_class=SensorDeviceClass.ENUM,
        unit=None,
        state_class=None,
        transport=Transport.RESOURCE_RID,
        states=((0, "ok"), (1, "no_card")),
        translation_key="sd_card_status",
    ),
    CloudMetric(
        key="13.25.85",
        field="totalsize",
        name="SD card capacity",
        device_class=SensorDeviceClass.DATA_SIZE,
        unit=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        transport=Transport.RESOURCE_RID,
    ),
    CloudMetric(
        key="13.25.85",
        field="freesize",
        name="SD card free",
        device_class=SensorDeviceClass.DATA_SIZE,
        unit=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        transport=Transport.RESOURCE_RID,
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
        # Device totals for the period. LANLink serves the running total
        # (0.13.85) but not these, and the app shows them on the device page.
        CloudMetric(
            key="1d_0_0.13.85",
            name="Energy today",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
        CloudMetric(
            key="month_0_0.13.85",
            name="Energy this month",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
    ),
    # Camera G100 and Doorbell Camera G400. Both answer for 13.25.85 - the
    # G400 reports sdstatus 1 with an empty slot - and neither serves card
    # state over LANLink, checked against a G100's 60 local entities, none of
    # which reports storage.
    "lumi.camera.agl005": _CAMERA_STORAGE,
    "lumi.camera.agl013": _CAMERA_STORAGE,
}


def metrics_for(model: str) -> tuple[CloudMetric, ...]:
    """Return the cloud metrics for a model, or an empty tuple."""
    return METRICS.get(model, ())


def supported_models() -> frozenset[str]:
    """Return every model this integration can add sensors for."""
    return frozenset(METRICS)
