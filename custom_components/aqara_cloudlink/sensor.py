"""Sensor platform for cloud-served Aqara values."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LANLINK_DOMAIN
from .coordinator import AqaraCloudCoordinator, CloudDevice
from .metrics import CloudMetric


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per metric per device.

    Entities are added per subentry so Home Assistant files them under the
    subentry that added the device. Without `config_subentry_id` the device
    is listed as belonging to no sub-entry and the subentry row shows empty,
    which reads as a failed add.
    """
    coordinator: AqaraCloudCoordinator = entry.runtime_data
    for device in coordinator.devices:
        entities = [
            AqaraCloudSensor(coordinator, device, metric)
            for metric in device.metrics
        ]
        if not entities:
            continue
        if device.subentry_id is None:
            async_add_entities(entities)
        else:
            async_add_entities(entities, config_subentry_id=device.subentry_id)


def _device_info(hass: HomeAssistant, device: CloudDevice) -> DeviceInfo:
    """Attach to the Aqara-LANLink device for this DID when one exists.

    Reusing that integration's identifiers puts these sensors on the same
    device card as the locally-pushed ones, rather than creating a second
    card for the same socket. When Aqara-LANLink is not installed, or has
    not added this device, we register our own device instead.

    This is opportunistic only: nothing here requires Aqara-LANLink to be
    present, and if its entry is later removed the device survives carrying
    just our entities.
    """
    existing = dr.async_get(hass).async_get_device(
        identifiers={(LANLINK_DOMAIN, device.did)}
    )
    if existing is not None:
        return DeviceInfo(identifiers={(LANLINK_DOMAIN, device.did)})

    # No via_device: this integration is cloud-only and never registers the
    # hub a device sits behind, so pointing at one would always dangle. Home
    # Assistant warns on a non-existent via_device and stops allowing it in
    # 2025.12. When Aqara-LANLink is installed it owns the device and models
    # the hub relationship itself, which is the branch above.
    return DeviceInfo(
        identifiers={(DOMAIN, device.did)},
        name=device.name,
        model=device.model,
        manufacturer="Aqara",
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

        self._attr_unique_id = f"{DOMAIN}_{device.did}_{metric.key}"
        self._attr_name = metric.name
        self._attr_device_class = metric.device_class
        self._attr_native_unit_of_measurement = metric.unit
        self._attr_state_class = metric.state_class
        self._attr_device_info = _device_info(coordinator.hass, device)

    @property
    def native_value(self) -> float | None:
        """Latest polled value, or None if this cycle did not return one."""
        return self.coordinator.data.get(self._device.did, {}).get(self._metric.key)

    @property
    def available(self) -> bool:
        """Available only while the last poll produced this value."""
        return super().available and self.native_value is not None
