"""Keep cloud sensors on Aqara-LANLink's device card when one exists.

`sensor._device_info` attaches to an existing Aqara-LANLink device when it
finds one, but that lookup runs when entities are created. If this
integration is set up before Aqara-LANLink has registered a device for the
same DID - a fresh install, or simply the order the two were configured in -
the lookup finds nothing and we register our own device instead.

Home Assistant then links the two rather than folding one into the other, so
the sensors end up on a second card next to the socket instead of on it.

This module closes that gap: it moves our entities onto the Aqara-LANLink
device whenever one turns up, and removes the device we created in the
meantime. It runs once at setup and then on every device registry change, so
it does not matter which integration is configured first.

Nothing here makes Aqara-LANLink a requirement. With it absent, our own
device stays exactly as it is.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN, LANLINK_DOMAIN

_LOGGER = logging.getLogger(__name__)


@callback
def async_relink_devices(hass: HomeAssistant, dids: Iterable[str]) -> int:
    """Move our entities onto the Aqara-LANLink device for each DID.

    Works from our own entities rather than from the device we registered,
    so it also repairs an installation that already ended up split: a reload
    can leave our entities on a second device that carries Aqara-LANLink's
    identifiers without being Aqara-LANLink's device.

    Returns the number of DIDs whose entities were moved. A DID is skipped
    when Aqara-LANLink has no device for it, or when our entities are already
    on it.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    relinked = 0

    for did in dids:
        target = dev_reg.async_get_device(identifiers={(LANLINK_DOMAIN, did)})
        if target is None:
            continue

        # Our unique_ids are "<domain>_<did>_<metric key>" (see sensor.py).
        prefix = f"{DOMAIN}_{did}_"
        stale: set[str] = set()
        moved = 0
        for entity in list(ent_reg.entities.values()):
            if entity.platform != DOMAIN or not entity.unique_id.startswith(prefix):
                continue
            if entity.device_id in (None, target.id):
                continue
            stale.add(entity.device_id)
            ent_reg.async_update_entity(entity.entity_id, device_id=target.id)
            moved += 1

        if not moved:
            continue

        # Drop the devices we left behind, but only once nothing is on them.
        # Anything still attached belongs to another integration.
        for device_id in stale:
            if not er.async_entries_for_device(
                ent_reg, device_id, include_disabled_entities=True
            ):
                dev_reg.async_remove_device(device_id)

        relinked += 1
        _LOGGER.debug(
            "Moved %d entities for %s onto the Aqara-LANLink device", moved, did
        )

    return relinked


@callback
def async_track_lanlink_devices(
    hass: HomeAssistant, dids: Iterable[str]
) -> CALLBACK_TYPE:
    """Relink now, and again whenever a device registry entry changes.

    Returns an unsubscribe callback for the caller to register with
    `entry.async_on_unload`.
    """
    wanted = set(dids)
    async_relink_devices(hass, wanted)

    @callback
    def _on_device_registry_updated(event: Event) -> None:
        # Cheap guard: only look when something was added or changed, and only
        # for a device carrying one of our DIDs under Aqara-LANLink's domain.
        if event.data.get("action") not in ("create", "update"):
            return
        device = dr.async_get(hass).async_get(event.data["device_id"])
        if device is None:
            return
        touched = {
            did
            for domain, did in device.identifiers
            if domain == LANLINK_DOMAIN and did in wanted
        }
        if touched:
            async_relink_devices(hass, touched)

    return hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED, _on_device_registry_updated
    )
