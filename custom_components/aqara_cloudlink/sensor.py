"""Sensor platform for cloud-served Aqara values."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AqaraCloudCoordinator, CloudDevice
from .entity import device_info
from .metrics import CloudMetric


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per metric per device.

    Entities are deliberately NOT registered against their subentry. Doing so
    (`async_add_entities(..., config_subentry_id=...)`) is the documented way
    to group them in the UI, but these entities attach to a device owned by
    Aqara-LANLink's config entry, and claiming a subentry on another entry's
    device drops them entirely - verified on a live instance, where all eight
    sensors vanished from the registry.

    The cost is that the subentry row renders without its entities. The
    merged device card is worth more than that grouping.
    """
    coordinator: AqaraCloudCoordinator = entry.runtime_data
    async_add_entities(
        AqaraCloudSensor(coordinator, device, metric)
        for device in coordinator.devices
        for metric in device.metrics
    )


class AqaraCloudSensor(CoordinatorEntity[AqaraCloudCoordinator], SensorEntity):
    """One cloud-read value."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AqaraCloudCoordinator,
        device: CloudDevice,
        metric: CloudMetric,
    ) -> None:
        """Bind this entity to one device and one metric."""
        super().__init__(coordinator)
        self._device = device
        self._metric = metric

        # `unique_suffix` is the metric key unless several metrics share one
        # resource id, so ids already in the registry are unaffected.
        self._attr_unique_id = f"{DOMAIN}_{device.did}_{metric.unique_suffix}"
        self._attr_name = metric.name
        self._attr_device_class = metric.device_class
        self._attr_native_unit_of_measurement = metric.unit
        self._attr_state_class = metric.state_class
        self._attr_options = metric.options
        self._attr_translation_key = metric.translation_key
        self._attr_device_info = device_info(coordinator.hass, device)

    @property
    def native_value(self) -> float | str | None:
        """Latest polled value, or None if this cycle did not return one.

        A metric with states reports the state name. An unrecognised number
        reads as None rather than being passed through, since Home Assistant
        rejects an enum value that is not among the declared options.
        """
        value = self.coordinator.data.get(self._device.did, {}).get(
            self._metric.unique_suffix
        )
        if value is None or not self._metric.states:
            return value
        return self._metric.label(value)

    @property
    def available(self) -> bool:
        """Available only while the last poll produced this value."""
        return super().available and self.native_value is not None
