"""Polling coordinator for cloud-served device values."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud import AqaraCloudAuthError, AqaraCloudClient
from .const import (
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    PRESENT_VALUE_METHOD,
    STATISTICS_METHOD,
)
from .metrics import CloudMetric, metrics_for

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CloudDevice:
    """A device this integration will create sensors for."""

    did: str
    model: str
    name: str
    #: The subentry this device was added under. Entities are registered
    #: against it so Home Assistant groups them under that subentry in the
    #: UI, rather than listing the device as belonging to no sub-entry.
    subentry_id: str | None = None

    @property
    def metrics(self) -> tuple[CloudMetric, ...]:
        """Cloud metrics available for this device's model."""
        return metrics_for(self.model)


class AqaraCloudCoordinator(DataUpdateCoordinator[dict[str, dict[str, float]]]):
    """Polls the Aqara cloud for values LANLink does not serve.

    `data` is keyed by device id, then by metric key:
    ``{"lumi.54ef...": {"8.0.2293": 1612.0, ...}}``. A device whose poll
    failed is absent from `data`, which leaves its entities unavailable
    rather than showing a stale reading.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AqaraCloudClient,
        token: str,
        devices: list[CloudDevice],
    ) -> None:
        """Set up the coordinator for a fixed set of devices."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self._client = client
        self._token = token
        self.devices = devices

    async def _async_update_data(self) -> dict[str, dict[str, float]]:
        """Poll every device once, splitting keys by read method.

        An auth failure is raised so Home Assistant can start its re-auth
        flow. Any other per-device error is logged and that device is left
        out of this cycle's data; one unhealthy device does not take the
        others down with it.
        """
        result: dict[str, dict[str, float]] = {}

        for device in self.devices:
            present = [m.key for m in device.metrics if not m.is_statistic]
            statistics = [m.key for m in device.metrics if m.is_statistic]
            values: dict[str, float] = {}

            for keys, method in (
                (present, PRESENT_VALUE_METHOD),
                (statistics, STATISTICS_METHOD),
            ):
                if not keys:
                    continue
                try:
                    raw = await self._client.query_view_data(
                        self._token, device.did, keys, process_method=method
                    )
                except AqaraCloudAuthError as err:
                    raise ConfigEntryAuthFailed(
                        "Aqara cloud rejected the stored token"
                    ) from err
                except Exception as err:  # noqa: BLE001 - one device must not break the rest
                    _LOGGER.warning(
                        "Cloud read failed for %s (%s) using %s: %s",
                        device.name, device.did, method, err,
                    )
                    continue
                values.update(_coerce(raw))

            if values:
                result[device.did] = values

        if not result and self.devices:
            raise UpdateFailed("No device returned a usable value this cycle")
        return result


def _coerce(raw: dict[str, Any]) -> dict[str, float]:
    """Return only the entries that parse as a number.

    The cloud returns values as strings, and an empty string for a reading
    that has never been taken. Those are skipped so a numeric sensor is left
    unknown rather than being handed something it cannot represent.
    """
    out: dict[str, float] = {}
    for key, value in raw.items():
        if value is None or value == "":
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring non-numeric value for %s: %r", key, value)
    return out
