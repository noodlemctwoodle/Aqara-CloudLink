"""Aqara CloudLink: Aqara values the cloud serves but LANLink does not."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .cloud import AqaraCloudClient
from .const import (
    CONF_DID,
    CONF_MODEL,
    CONF_NAME,
    CONF_REGION,
    CONF_TOKEN,
    DEFAULT_REGION,
    SUBENTRY_TYPE_DEVICE,
)
from .coordinator import AqaraCloudCoordinator, CloudDevice
from .linker import async_track_lanlink_devices

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type AqaraCloudLinkConfigEntry = ConfigEntry[AqaraCloudCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AqaraCloudLinkConfigEntry) -> bool:
    """Start polling the devices added as subentries."""
    client = AqaraCloudClient(entry.data.get(CONF_REGION, DEFAULT_REGION), hass=hass)
    token: str = entry.data[CONF_TOKEN]

    devices = _subentry_devices(entry)
    if not devices:
        # Nothing picked yet. Setting up with no entities beats failing: the
        # user adds devices from the integration page and the entry reloads.
        _LOGGER.info("No devices added yet; add one from the integration page")

    coordinator = AqaraCloudCoordinator(hass, entry, client, token, devices)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Our sensors belong on Aqara-LANLink's device card when it has one for the
    # same DID. Which integration was configured first decides whether that
    # device exists by the time our entities are created, so keep watching
    # rather than deciding once.
    entry.async_on_unload(
        async_track_lanlink_devices(hass, [device.did for device in devices])
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: AqaraCloudLinkConfigEntry) -> None:
    """Reload when a device subentry is added or removed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AqaraCloudLinkConfigEntry) -> bool:
    """Tear down the platforms; the coordinator stops with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _subentry_devices(entry: AqaraCloudLinkConfigEntry) -> list[CloudDevice]:
    """Build the device list from the entry's device subentries."""
    devices: list[CloudDevice] = []
    for sub in entry.subentries.values():
        if sub.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        did, model = sub.data.get(CONF_DID), sub.data.get(CONF_MODEL)
        if not did or not model:
            continue
        devices.append(
            CloudDevice(did=did, model=model, name=sub.data.get(CONF_NAME) or model)
        )
    return devices
