"""AqaraCloudLink: Aqara values the cloud serves but LANLink does not."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .cloud import AqaraAuthError, AqaraCloudAuthError, AqaraCloudClient
from .const import CONF_REGION, CONF_TOKEN, DEFAULT_REGION
from .coordinator import AqaraCloudCoordinator, CloudDevice
from .metrics import metrics_for

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type AqaraCloudLinkConfigEntry = ConfigEntry[AqaraCloudCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AqaraCloudLinkConfigEntry) -> bool:
    """Enumerate the account's devices and start polling the supported ones."""
    client = AqaraCloudClient(entry.data.get(CONF_REGION, DEFAULT_REGION), hass=hass)
    token: str = entry.data[CONF_TOKEN]

    try:
        records = await client.query_device_list(token)
    except AqaraCloudAuthError as err:
        raise ConfigEntryAuthFailed("Aqara cloud rejected the stored token") from err
    except AqaraAuthError as err:
        raise ConfigEntryNotReady(f"Could not reach the Aqara cloud: {err}") from err

    devices = _supported_devices(records)
    if not devices:
        # Not an error: the account simply holds nothing this integration can
        # add sensors for. Setting up with no entities beats failing, so the
        # entry stays and picks up devices added later on the next reload.
        _LOGGER.info(
            "No devices in this Aqara account have cloud-only values to read; "
            "set up with no entities"
        )

    coordinator = AqaraCloudCoordinator(hass, entry, client, token, devices)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AqaraCloudLinkConfigEntry) -> bool:
    """Tear down the platforms; the coordinator stops with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _supported_devices(records: list[dict]) -> list[CloudDevice]:
    """Keep only the account's devices that have cloud-only values.

    The cloud returns every device across every home. Anything whose model
    has no entry in `metrics` is skipped, so adding a model to that table is
    all it takes to start reading it.
    """
    devices: list[CloudDevice] = []
    for record in records:
        did, model = record.get("did"), record.get("model")
        if not did or not model or not metrics_for(model):
            continue
        devices.append(
            CloudDevice(
                did=did,
                model=model,
                name=record.get("deviceName") or model,
            )
        )
    return devices
