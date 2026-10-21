"""Config flow for AqaraLink."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .cloud import AREAS, AqaraAuthError, AqaraCloudAuthError, AqaraCloudClient
from .const import CONF_REGION, CONF_TOKEN, CONF_USER_ID, DEFAULT_REGION, DOMAIN

_LOGGER = logging.getLogger(__name__)

_REGION_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=sorted(AREAS), mode=SelectSelectorMode.DROPDOWN)
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): _REGION_SELECTOR,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)


class AqaraLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Exchange Aqara credentials for a token and store only the token."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with no reauth context."""
        self._reauth_entry_data: Mapping[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]
            try:
                tokens = await _login(
                    self.hass, region, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except AqaraCloudAuthError:
                errors["base"] = "invalid_auth"
            except AqaraAuthError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surfaced to the user as "unknown"
                _LOGGER.exception("Unexpected error during Aqara login")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(tokens.user_id)
                self._abort_if_unique_id_configured()
                # The password is deliberately not stored; only the token the
                # cloud handed back is persisted.
                return self.async_create_entry(
                    title=f"Aqara cloud ({region})",
                    data={
                        CONF_USER_ID: tokens.user_id,
                        CONF_TOKEN: tokens.token,
                        CONF_REGION: region,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Begin re-auth after the stored token stopped being accepted."""
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Swap fresh credentials for a new token, keeping the same region."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        region = entry.data.get(CONF_REGION, DEFAULT_REGION)

        if user_input is not None:
            try:
                tokens = await _login(
                    self.hass, region, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except AqaraCloudAuthError:
                errors["base"] = "invalid_auth"
            except AqaraAuthError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Aqara re-auth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USER_ID: tokens.user_id,
                        CONF_TOKEN: tokens.token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"region": region},
        )


async def _login(hass, region: str, email: str, password: str):
    """Exchange credentials for a token."""
    return await AqaraCloudClient(region, hass=hass).login(email, password)
