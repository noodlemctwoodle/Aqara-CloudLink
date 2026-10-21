"""Config flow for Aqara CloudLink."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
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
from .const import (
    CONF_DID,
    CONF_MODEL,
    CONF_NAME,
    CONF_REGION,
    CONF_TOKEN,
    CONF_USER_ID,
    DEFAULT_REGION,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
)
from .metrics import metrics_for

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


def added_dids(entry: ConfigEntry) -> set[str]:
    """DIDs already added to this entry as device subentries."""
    return {
        sub.data[CONF_DID]
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_DEVICE and CONF_DID in sub.data
    }


def eligible_devices(
    records: list[dict], already: set[str]
) -> list[tuple[str, str, str]]:
    """Return (did, model, name) for devices that can still be added.

    A device is eligible when `metrics` knows cloud-only values for its model
    and it is not already a subentry. Sorted by name so the picker is stable.
    """
    out: list[tuple[str, str, str]] = []
    for record in records:
        did, model = record.get("did"), record.get("model")
        if not did or not model or did in already or not metrics_for(model):
            continue
        out.append((did, model, record.get("deviceName") or model))
    return sorted(out, key=lambda item: item[2].lower())


class AqaraCloudLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect credentials and store only the resulting token."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Devices are added one at a time, as subentries."""
        return {SUBENTRY_TYPE_DEVICE: DeviceSubentryFlowHandler}

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


class DeviceSubentryFlowHandler(ConfigSubentryFlow):
    """Pick one device to read cloud-only values for.

    One device per run, matching how Aqara-LANLink adds devices, so the list
    is always current and an unreachable cloud fails one add rather than all
    of them.
    """

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the devices this integration can read, and add the chosen one."""
        entry = self._get_entry()
        client = AqaraCloudClient(
            region=entry.data.get(CONF_REGION, DEFAULT_REGION), hass=self.hass
        )

        try:
            records = await client.query_device_list(entry.data[CONF_TOKEN])
        except Exception as err:  # noqa: BLE001 - classified and logged
            return self.async_abort(reason=_classify(err))

        choices = eligible_devices(records, added_dids(entry))
        if not choices:
            # Either everything eligible is already added, or this account has
            # nothing `metrics` knows about. Both are dead ends for this form.
            return self.async_abort(reason="no_devices")

        by_did = {did: (model, name) for did, model, name in choices}

        if user_input is not None:
            did = user_input[CONF_DID]
            model, name = by_did[did]
            return self.async_create_entry(
                title=name,
                data={CONF_DID: did, CONF_MODEL: model, CONF_NAME: name},
                unique_id=did,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_DID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=did, label=f"{name} ({model})")
                            for did, model, name in choices
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
