"""Polling coordinator for cloud-served device values."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable
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
from .metrics import CloudMetric, Transport, metrics_for

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
        """Poll every device once, grouping metrics by how they are read.

        Three reads at most per device: present values and statistics from the
        device-page call, and anything the device-page call will not answer for
        from the rid query. Each is skipped when no metric needs it.

        An auth failure is raised so Home Assistant can start its re-auth
        flow. Any other per-device error is logged and that device is left
        out of this cycle's data; one unhealthy device does not take the
        others down with it.
        """
        result: dict[str, dict[str, float]] = {}

        for device in self.devices:
            raw = await self._read_device(device)
            values = _values_for(device.metrics, raw)
            if values:
                result[device.did] = values

        if not result and self.devices:
            raise UpdateFailed("No device returned a usable value this cycle")
        return result

    async def _read_device(self, device: CloudDevice) -> dict[str, Any]:
        """Return every raw value this device's metrics need, keyed by key.

        A failed read contributes nothing and is logged; the metrics it would
        have served are simply absent, which leaves their entities
        unavailable rather than stale.
        """
        view_present: list[str] = []
        view_statistics: list[str] = []
        rids: list[str] = []
        for metric in device.metrics:
            if metric.transport is Transport.RESOURCE_RID:
                target = rids
            elif metric.is_statistic:
                target = view_statistics
            else:
                target = view_present
            if metric.key not in target:
                # Metrics sharing a resource id are read once, not once each.
                target.append(metric.key)

        raw: dict[str, Any] = {}

        async def _read(label: str, call: Awaitable[dict[str, Any]]) -> None:
            try:
                raw.update(await call)
            except AqaraCloudAuthError as err:
                raise ConfigEntryAuthFailed(
                    "Aqara cloud rejected the stored token"
                ) from err
            except Exception as err:  # noqa: BLE001 - one read must not break the rest
                _LOGGER.warning(
                    "Cloud read failed for %s (%s) using %s: %s",
                    device.name, device.did, label, err,
                )

        if view_present:
            await _read(
                PRESENT_VALUE_METHOD,
                self._client.query_view_data(
                    self._token, device.did, view_present,
                    process_method=PRESENT_VALUE_METHOD,
                ),
            )
        if view_statistics:
            await _read(
                STATISTICS_METHOD,
                self._client.query_view_data(
                    self._token, device.did, view_statistics,
                    process_method=STATISTICS_METHOD,
                ),
            )
        if rids:
            await _read(
                "resourceId",
                self._client.query_resources_by_rid(self._token, device.did, rids),
            )

        return raw


def _values_for(
    metrics: tuple[CloudMetric, ...], raw: dict[str, Any]
) -> dict[str, float]:
    """Resolve each metric to a number, keyed by its unique suffix.

    Most metrics read their key straight out of `raw`. A metric with a `field`
    takes one entry from a JSON object served under that key, which is how
    camera storage arrives: several sensors, one resource id.

    A metric that cannot be resolved is left out, so its entity reads as
    unavailable rather than being handed something it cannot represent.
    """
    out: dict[str, float] = {}
    parsed: dict[str, Any] = {}

    for metric in metrics:
        if metric.key not in raw:
            continue
        value = raw[metric.key]

        if metric.field is not None:
            if metric.key not in parsed:
                parsed[metric.key] = _parse_object(metric.key, value)
            obj = parsed[metric.key]
            if not isinstance(obj, dict) or metric.field not in obj:
                continue
            value = obj[metric.field]

        number = _coerce_one(metric.unique_suffix, value)
        if number is not None:
            out[metric.unique_suffix] = number

    return out


def _parse_object(key: str, value: Any) -> Any:
    """Return `value` as a mapping, decoding it from JSON if need be."""
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        _LOGGER.debug("Value for %s is not a JSON object: %r", key, value)
        return None


def _coerce_one(key: str, value: Any) -> float | None:
    """Return `value` as a float, or None when it is not a number.

    The cloud returns values as strings, and an empty string for a reading
    that has never been taken.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        _LOGGER.debug("Ignoring non-numeric value for %s: %r", key, value)
        return None
