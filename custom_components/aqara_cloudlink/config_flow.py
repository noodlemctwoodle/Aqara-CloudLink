"""Config flow for Aqara CloudLink."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .cloud import (
    AREAS,
    AqaraAuthError,
    AqaraCloudAuthError,
    AqaraCloudClient,
    AqaraTokens,
)
from .const import CONF_REGION, CONF_TOKEN, CONF_USER_ID, DEFAULT_REGION, DOMAIN

_LOGGER = logging.getLogger(__name__)

_REGION_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=sorted(AREAS), mode=SelectSelectorMode.DROPDOWN)
)

# Email and password are optional because a user id and token can be given
# instead. Aqara's login endpoint rate-limits, so anyone who already holds a
# token - from another tool, or from an Aqara-LANLink entry on the same box -
# can skip the login rather than spending another one.
_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EMAIL): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): _REGION_SELECTOR,
        vol.Optional(CONF_USER_ID): str,
        vol.Optional(CONF_TOKEN): str,
    }
)

_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EMAIL): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Optional(CONF_USER_ID): str,
        vol.Optional(CONF_TOKEN): str,
    }
)


async def _resolve_tokens(
    hass: HomeAssistant, region: str, user_input: Mapping[str, Any]
) -> AqaraTokens:
    """Return tokens from the input, logging in only when necessary.

    A user id and token given together are used as-is. Otherwise an email and
    password are exchanged for a token. Raises ValueError when neither
    complete pair was supplied, which the caller turns into a form error.
    """
    user_id = (user_input.get(CONF_USER_ID) or "").strip()
    token = (user_input.get(CONF_TOKEN) or "").strip()
    if user_id and token:
        return AqaraTokens(user_id=user_id, token=token, raw_result={})

    email = (user_input.get(CONF_EMAIL) or "").strip()
    password = user_input.get(CONF_PASSWORD) or ""
    if not email or not password:
        raise ValueError("need either email and password, or user id and token")

    return await AqaraCloudClient(region=region, hass=hass).login(email, password)


def _classify(err: Exception) -> str:
    """Map a failure to a form error key, logging what actually went wrong.

    The cloud returns a non-zero code for a bad signature, a rate-limited
    login and a genuine outage alike, and all of them surface as
    AqaraAuthError. Without this log the user only ever sees "could not
    reach", which is misleading for most of those cases.
    """
    if isinstance(err, AqaraCloudAuthError):
        _LOGGER.debug("Aqara rejected the credentials: %s", err)
        return "invalid_auth"
    if isinstance(err, AqaraAuthError):
        _LOGGER.warning(
            "Aqara cloud call failed. This covers rate-limited logins and "
            "server-side rejections as well as genuine connectivity problems. "
            "If you already hold a user id and token, enter those instead to "
            "skip the login. Detail: %s",
            err,
        )
        return "cannot_connect"
    _LOGGER.error("Unexpected error talking to the Aqara cloud: %s", err, exc_info=err)
    return "unknown"


class AqaraCloudLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect credentials and store only the resulting token."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in, or accept a supplied token, and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]
            try:
                tokens = await _resolve_tokens(self.hass, region, user_input)
            except ValueError:
                errors["base"] = "missing_credentials"
            except Exception as err:  # noqa: BLE001 - classified and logged
                errors["base"] = _classify(err)
            else:
                await self.async_set_unique_id(tokens.user_id)
                self._abort_if_unique_id_configured()
                # Only the token is persisted; the password is never stored.
                return self.async_create_entry(
                    title=f"Aqara cloud ({region})",
                    data={
                        CONF_USER_ID: tokens.user_id,
                        CONF_TOKEN: tokens.token,
                        CONF_REGION: region,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_CREDENTIALS_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Begin re-auth after the stored token stopped being accepted."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Get a fresh token, keeping the entry's existing region."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        region = entry.data.get(CONF_REGION, DEFAULT_REGION)

        if user_input is not None:
            try:
                tokens = await _resolve_tokens(self.hass, region, user_input)
            except ValueError:
                errors["base"] = "missing_credentials"
            except Exception as err:  # noqa: BLE001 - classified and logged
                errors["base"] = _classify(err)
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
            data_schema=_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"region": region},
        )
