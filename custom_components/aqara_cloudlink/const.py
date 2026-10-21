"""Constants for the Aqara CloudLink cloud integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "aqara_cloudlink"

# The integration whose devices we attach to when it is present. Aqara-LANLink
# registers devices as `identifiers={("aqara_lanlink", did)}`, so reusing that
# tuple puts our sensors on the same device card instead of a duplicate one.
# Purely opportunistic: when no such device exists we register our own.
LANLINK_DOMAIN: Final = "aqara_lanlink"

CONF_REGION: Final = "region"
CONF_USER_ID: Final = "user_id"
CONF_TOKEN: Final = "token"
CONF_ATTACH_TO_LANLINK: Final = "attach_to_lanlink"

DEFAULT_REGION: Final = "EU"
DEFAULT_SCAN_INTERVAL_SECONDS: Final = 60.0

# Statistics keys ("1d_0_0.<rid>", "month_0_0.<rid>") go to the cloud's
# statisticsMethod; plain resource ids go to presentValueMethod.
PRESENT_VALUE_METHOD: Final = "presentValueMethod"
STATISTICS_METHOD: Final = "statisticsMethod"
