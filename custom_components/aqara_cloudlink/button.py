"""Button platform for cloud writes LANLink has no local path for.

Everything here is a write, which is the opposite of what the rest of this
integration does. The bar for adding one is therefore high: a cloud write is
slower than a local one, needs internet, and for anything Aqara-LANLink
already exposes as a switch it is strictly worse. Only writes with no local
equivalent at all belong in `metrics.ACTIONS`.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AqaraCloudCoordinator, CloudDevice
from .entity import device_info
from .metrics import CloudAction

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one button per cloud action per device.

    As in `sensor`, entities are deliberately not registered against their
    subentry: these attach to a device owned by Aqara-LANLink's config entry,
    and claiming a subentry on another entry's device drops them entirely.
    """
    coordinator: AqaraCloudCoordinator = entry.runtime_data
    async_add_entities(
        AqaraCloudActionButton(coordinator, device, action)
        for device in coordinator.devices
        for action in device.actions
    )


class AqaraCloudActionButton(CoordinatorEntity[AqaraCloudCoordinator], ButtonEntity):
    """One cloud write, exposed as a button."""

    _attr_has_entity_name = True
    # Keeps these out of a device card's main controls. Nothing here is a
    # thing to press in passing.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: AqaraCloudCoordinator,
        device: CloudDevice,
        action: CloudAction,
    ) -> None:
        """Bind this entity to one device and one action."""
        super().__init__(coordinator)
        self._device = device
        self._action = action

        self._attr_unique_id = f"{DOMAIN}_{device.did}_action_{action.key}"
        self._attr_name = action.name
        self._attr_translation_key = action.translation_key
        self._attr_device_info = device_info(coordinator.hass, device)

    @property
    def available(self) -> bool:
        """Available only while the action's precondition holds.

        An action with no `available_when` is always offered. One that has it
        is offered only while the named metric reports the required value, so
        a write that would be meaningless - or worse than meaningless - is
        not reachable in the first place.
        """
        if not super().available:
            return False
        if self._action.available_when is None:
            return True
        key, required = self._action.available_when
        return self.coordinator.data.get(self._device.did, {}).get(key) == required

    async def async_press(self) -> None:
        """Send the write.

        Logged at warning level on purpose. These actions are the ones with
        no local equivalent and no undo, so the Home Assistant log should
        record that one was issued and by which entity.
        """
        _LOGGER.warning(
            "Writing %s=%s to %s (%s) via the Aqara cloud: %s",
            self._action.key, self._action.value,
            self._device.name, self._device.did, self._action.name,
        )
        await self.coordinator.async_write(
            self._device.did, {self._action.key: self._action.value}
        )
