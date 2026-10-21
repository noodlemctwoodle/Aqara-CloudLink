"""Shared device attachment for every platform this integration provides."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, LANLINK_DOMAIN
from .coordinator import CloudDevice


def device_info(hass: HomeAssistant, device: CloudDevice) -> DeviceInfo:
    """Attach to the Aqara-LANLink device for this DID when one exists.

    Reusing that integration's identifiers puts our entities on the same
    device card as the locally-pushed ones, rather than creating a second
    card for the same hardware. When Aqara-LANLink is not installed, or has
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
