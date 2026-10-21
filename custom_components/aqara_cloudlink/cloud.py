"""Aqara Legacy RPC cloud client.

Talks to the same regional endpoints the Aqara Home mobile app uses:

- :meth:`AqaraCloudClient.login` - email/password login
  (``/app/v1.0/lumi/user/login``). Returns a userId + token pair.
- :meth:`AqaraCloudClient.query_device_list` - device inventory across all
  homes (``/app/v1.0/lumi/app/position/device/query``). GET; the sign source
  is the alphabetically-sorted query string.
- :meth:`AqaraCloudClient.query_device_detail` - extended per-device metadata
  (mac, firmwareVersion, parentDeviceId, homeId)
  (``/app/v1.0/lumi/app/dev/query/detail``). GET; the sign source is the
  sorted query string with the JSON-encoded ``dids`` list, NOT
  percent-encoded.
- :meth:`AqaraCloudClient.query_view_data` - the call the app's device pages
  make (``/app/v1.0/lumi/app/view/data/query/new``). POST; the sign source is
  the JSON body verbatim. Reaches values addressed by legacy resource id,
  which is how per-outlet metering is read.

Requests are signed with an appid/appkey pair that varies by region; a
code-106 ("invalid sign") reply triggers an auto-probe of the known
combinations. The password is RSA-encrypted with a public key extracted from
the Android app.

Provenance
----------
This module is derived from ``hub/cloud_client.py`` in
`absent42/Aqara-LANLink <https://github.com/absent42/Aqara-LANLink>`_ by Dan
Gibson, MIT licensed. The protocol reverse engineering - signing, region
handling, password encryption and endpoint semantics - is his work.

Reduced here to the four calls a cloud-only integration needs. Removed from
the original: local trait reads, resource-id queries, light effects and
scenes, collection panels, and camera P2P and PTZ.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding


_LOGGER = logging.getLogger(__name__)


# =============================================================================
# Region map -- server + app credentials per region.
# Mirrors sdavides/AqaraPOST-tokenGenerator.py verbatim.
# =============================================================================

@dataclass(frozen=True)
class _AreaConfig:
    server: str
    appid: str
    appkey: str


AREAS: dict[str, _AreaConfig] = {
    "CN": _AreaConfig(
        server="https://aiot-rpc.aqara.cn",
        appid="94549908487478b220992a70",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "EU": _AreaConfig(
        server="https://rpc-ger.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "RU": _AreaConfig(
        server="https://rpc-ru.aqara.com",
        appid="94549908487478b220992a70",
        appkey="euGhPe2rcmxwculATNj45eEtnd50zp0I",
    ),
    "KR": _AreaConfig(
        server="https://rpc-kr.aqara.com",
        appid="94549908487478b220992a70",
        appkey="euGhPe2rcmxwculATNj45eEtnd50zp0I",
    ),
    "JP": _AreaConfig(
        server="https://rpc-kr.aqara.com",
        appid="94549908487478b220992a70",
        appkey="euGhPe2rcmxwculATNj45eEtnd50zp0I",
    ),
    "AF": _AreaConfig(
        server="https://rpc-ger.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "US": _AreaConfig(
        server="https://aiot-rpc-usa.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "HMT": _AreaConfig(
        server="https://aiot-rpc-usa.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "OTHER": _AreaConfig(
        server="https://aiot-rpc-usa.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "AU": _AreaConfig(
        server="https://rpc-au.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
    "ME": _AreaConfig(
        server="https://rpc-au.aqara.com",
        appid="7be1984f0556276133336839",
        appkey="Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3",
    ),
}


# The 3 distinct (appid, appkey) pairs across the region table. The per-region
# assignment in AREAS is the best first guess; AqaraCloudClient auto-probes
# these on a code-106 ("invalid sign") reply. Order = probe order.
_SIGNING_COMBOS: tuple[tuple[str, str], ...] = (
    ("7be1984f0556276133336839", "Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3"),
    ("94549908487478b220992a70", "euGhPe2rcmxwculATNj45eEtnd50zp0I"),
    ("94549908487478b220992a70", "Jddz01kIORDYrBzqGYgpUXKBnIHfW8E3"),
)


# Hardcoded RSA public key used to encrypt the password hash. Extracted from
# the Android app; present across all current cross-platform Aqara builds.
_AQARA_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCG46slB57013JJs4Vvj5cVyMpR
9b+B2F+YJU6qhBEYbiEmIdWpFPpOuBikDs2FcPS19MiWq1IrmxJtkICGurqImRUt
4lP688IWlEmqHfSxSRf2+aH0cH8VWZ2OaZn5DWSIHIPBF2kxM71q8stmoYiV0oZs
rZzBHsMuBwA4LQdxBwIDAQAB
-----END PUBLIC KEY-----"""


_LOGIN_PATH = "/app/v1.0/lumi/user/login"
_DEVICE_LIST_PATH = "/app/v1.0/lumi/app/position/device/query"
_TRAIT_READ_PATH = "/app/v1.0/lumi/app/qlink/trait/read"
_RES_QUERY_BY_RID_PATH = "/app/v1.0/lumi/res/query/by/resourceId"
_VIEW_DATA_QUERY_PATH = "/app/v1.0/lumi/app/view/data/query/new"
_DEV_QUERY_DETAIL_PATH = "/app/v1.0/lumi/app/dev/query/detail"
_CUSTOM_ACTION_QUERY_PATH = "/app/v1.0/lumi/app/customaction/query"
_SEQUENCE_RUN_PATH = "/app/v1.0/lumi/app/sequence/run"
_COLLECTION_PANELS_PATH = "/app/v1.0/lumi/app/layout/collection/panels"
_CAMERA_P2P_INFO_PATH = "/app/v1.0/lumi/devex/camera/p2p/info"
_CAMERA_P2P_SIGN_PATH = "/app/v1.0/lumi/devex/camera/p2p/sign"
_CAMERA_PTZ_POSITION_LIST_PATH = "/app/v1.0/lumi/devex/camera/ptz/position/list"



_UNIVERSAL_PATHS: tuple[str, ...] = (
    "0.128.32901", "0.129.32906", "0.129.32907", "0.129.32909",
    "0.129.32912", "0.129.33013", "0.130.32913", "0.130.32914",
    "0.130.33016", "1.147.32969",
)



# =============================================================================
# Errors + return type
# =============================================================================


class AqaraAuthError(RuntimeError):
    """Raised on any auth-related failure (bad creds, network, server error)."""


class AqaraCloudAuthError(AqaraAuthError):
    """Raised when the cloud explicitly rejects the stored token / session.

    Subclass of :class:`AqaraAuthError` so existing broad catches keep
    working; new catch sites can match this narrower type and trigger
    HA's re-auth flow via ``entry.async_start_reauth()``.

    Distinguished from generic AqaraAuthError by matching one of the
    documented auth-failure codes (see ``_AUTH_FAILURE_CODES``).
    """


# Aqara Open Cloud "Account Authorization Mode" error codes that indicate
# the stored token is no longer valid and the user must re-authenticate.
# Source: https://opendoc.aqara.cn/en/docs/developmanual/apiIntroduction/errorCode.html
#
#   108  Token has expired           -- the canonical case (token TTL elapsed)
#   109  Token is absence            -- missing/blank token (config corruption)
#   802  Account not login           -- server-side session ended (e.g. user
#                                       changed account password elsewhere)
#   804  Token failed                -- generic token validation failure
#
# Codes intentionally NOT in this set:
#   803  User permission denied      -- scope error, not auth-state
#   810  Password incorrect          -- only fires on the login step itself
#   2xxx (RefreshToken / AccessToken codes) -- Developer App authorization mode
#                                              which we don't use
_AUTH_FAILURE_CODES: frozenset[int] = frozenset({108, 109, 802, 804})


@dataclass
class AqaraTokens:
    """Result of a successful login.

    The Legacy RPC API doesn't return a refresh token or expiry, so we store
    only what the server actually gave us. Tokens appear stable for months
    in practice; the caller should re-login if a request is later rejected.
    """

    user_id: str
    token: str
    raw_result: dict[str, Any]


# =============================================================================
# Public client
# =============================================================================


class AqaraCloudClient:
    """Aqara Legacy RPC cloud client.

    Provides username/password login plus a small set of authenticated
    cloud queries used by the integration during enrollment / discovery:

    - :meth:`login` - exchange email + password for an (user_id, token)
      pair.
    - :meth:`query_device_list` - fetch the user's full device inventory
      across all homes.
    - :meth:`query_device_traits` - fetch per-device per-trait metadata
      and current values for one device.
    - :meth:`query_device_detail` - fetch extended per-device metadata
      (mac, firmwareVersion, parentDeviceId, homeId, etc.) for one
      device.
    - :meth:`query_custom_actions` - fetch the cloud's `result` dict for
      a named-scene action_id query, used at setup to fetch effect lists
      per Light. Note the action_id semantics gotcha (verified empirically
      against a live T2 RGB CCT capture): `scene_mode` returns DYNAMIC
      scenes, `new_dynamic_scene_mode` returns STATIC scenes -- the names
      are REVERSED from what they suggest.
    - :meth:`run_sequence` - activate a dynamic scene by seq_id.
    - :meth:`query_collection_panels` - fetch per-endpoint path lists
      for one device from the layout/collection/panels endpoint. Returns
      a dict[int, list[str]] keyed by endpointId.
    """

    #: Client headers aligned with a captured Android-app request, so our
    #: traffic looks like the real app's.
    _CLIENT_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": "okhttp/4.12.0",
        "App-Version": "6.1.6",
        "Sys-Type": "1",          # 0 = iOS, 1 = Android
        "Lang": "en",
        "Phone-Model": "Home Assistant##Server",
    }

    def __init__(
        self,
        region: str = "EU",
        *,
        session: aiohttp.ClientSession | None = None,
        hass: Any = None,
        phone_id: str | None = None,
    ) -> None:
        if region not in AREAS:
            raise ValueError(f"unknown Aqara region {region!r}")
        self._region = region
        self._area = AREAS[region]
        # Active signing credential, seeded from the region's best-guess pair.
        # Swapped in place by the code-106 auto-probe (see _signed_request).
        self._appid = self._area.appid
        self._appkey = self._area.appkey
        self._signing_probed = False
        self._session = session
        # When no explicit session is given but a HomeAssistant instance is,
        # _request lazily borrows HA's shared aiohttp session (pooled TLS,
        # reused across this client's calls) instead of opening and tearing
        # down a fresh session per request.
        self._hass = hass
        # Stable per-instance PhoneId. Aqara's cloud namespaces push-subscription
        # state by (user, PhoneId); regenerating it per request orphans every
        # subscription on the hub and accumulates dead policy_resset entries
        # across reloads. A caller-provided value (persisted in ConfigEntry.data)
        # wins; the UUID4 fallback at least stays stable for this instance.
        self._phone_id = phone_id or str(uuid.uuid4()).upper()

    # -------------------------------------------------------------------------
    # Password encryption (RSA-PKCS1v15 over the hex MD5)
    # -------------------------------------------------------------------------

    @staticmethod
    def _encrypt_password(password: str) -> str:
        """Return `base64(RSA-PKCS1v15(md5(password).hexdigest().encode()))`.

        The server expects `encryptType: 2` alongside this value -- it tells
        the server the password has been pre-hashed+encrypted by the client.
        """
        pubkey = serialization.load_pem_public_key(_AQARA_PUBLIC_KEY_PEM)
        md5_hex = hashlib.md5(password.encode()).hexdigest().encode()
        ciphertext = pubkey.encrypt(md5_hex, padding.PKCS1v15())
        return base64.b64encode(ciphertext).decode()

    # -------------------------------------------------------------------------
    # Header + signing
    # -------------------------------------------------------------------------

    def _sign(
        self,
        nonce: str,
        ts_ms: str,
        body_str: str,
        token: str | None = None,
    ) -> str:
        """MD5 sign of the canonical header+body+appkey string.

        SECURITY: the ``source`` string below concatenates the cloud Token and
        the region appkey (a shared signing secret). Never log ``source``, the
        request headers, or this function's inputs - doing so leaks both the
        session token and the appkey.
        """
        if token:
            source = (
                f"Appid={self._appid}&Nonce={nonce}&Time={ts_ms}"
                f"&Token={token}&{body_str}&{self._appkey}"
            )
        else:
            source = (
                f"Appid={self._appid}&Nonce={nonce}&Time={ts_ms}"
                f"&{body_str}&{self._appkey}"
            )
        return hashlib.md5(source.encode()).hexdigest()

    def _build_headers(self, body_str: str, token: str | None = None) -> dict[str, str]:
        nonce = hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()
        ts_ms = str(round(time.time() * 1000))
        headers = {
            **self._CLIENT_HEADERS,
            "PhoneId": self._phone_id,
            "Area": self._region,
            "Appid": self._appid,
            "Nonce": nonce,
            "Time": ts_ms,
            "Content-Type": "application/json",
            "Sign": self._sign(nonce, ts_ms, body_str, token),
        }
        if token:
            headers["Token"] = token
        return headers

    async def _signed_request(
        self,
        method: str,
        url: str,
        *,
        sign_source: str,
        token: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """Sign + issue a request, auto-probing alternate app credentials on a 106.

        ``sign_source`` is the per-endpoint sign component (JSON body for POST,
        sorted query string for GET). ``body`` is the POST body to send (None for GET).

        On a ``code == 106`` ("invalid sign") reply - and only once per client
        instance - re-sign with each other pair in ``_SIGNING_COMBOS`` and retry,
        keeping the first that signs (it stays on ``self._appid/_appkey`` for later
        calls). Probes at most once whether it succeeds or exhausts; any non-106 code
        (including 108) is returned untouched.

        The in-place probe is NOT safe under concurrent calls on the same
        instance; that is acceptable because the probe window is a single
        one-shot login round-trip.
        """
        headers = self._build_headers(sign_source, token)
        payload = await self._request(method, url, headers, data=body)
        if str(payload.get("code")) != "106" or self._signing_probed:
            return payload

        self._signing_probed = True
        seed = (self._appid, self._appkey)
        try:
            for appid, appkey in _SIGNING_COMBOS:
                if (appid, appkey) == seed:
                    continue  # the seed already failed on the original call
                self._appid, self._appkey = appid, appkey
                headers = self._build_headers(sign_source, token)
                retry = await self._request(method, url, headers, data=body)
                if str(retry.get("code")) != "106":
                    _LOGGER.info(
                        "Aqara signing fallback for region %s: switched to appid %s.. "
                        "after the configured credential was rejected (code 106)",
                        self._region, appid[:10],
                    )
                    return retry
        except BaseException:
            # A probe retry raised (HTTP / non-JSON / ClientError). Never leave
            # the instance stranded on an alternate pair: restore the seed before
            # propagating, so the active pair changes permanently ONLY on a
            # probe success.
            self._appid, self._appkey = seed
            raise

        # No combo signed: restore the seed pair, surface a clear 106 while
        # PRESERVING the server's original message (existing 106 tests match on it,
        # and we should not discard upstream detail).
        self._appid, self._appkey = seed
        clarified = dict(payload)
        original = (clarified.get("message") or "").strip()
        suffix = (
            f"all known app credentials were rejected for region {self._region}; "
            f"the app may use an undocumented appid"
        )
        clarified["message"] = f"{original} ({suffix})".strip() if original else suffix
        return clarified

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def login(self, email: str, password: str) -> AqaraTokens:
        """Log in and return a usable (user_id, token) pair.

        Raises :class:`AqaraAuthError` on credential, network, or server
        errors.
        """
        body_dict = {
            "account": email,
            "encryptType": 2,
            "password": self._encrypt_password(password),
        }
        # Server expects the body to be signed with default json.dumps spacing
        # (space after `,` and `:`). Using separators=(",", ":") breaks the sign.
        body_str = json.dumps(body_dict)

        url = f"{self._area.server}{_LOGIN_PATH}"
        payload = await self._signed_request(
            "POST", url, sign_source=body_str, body=body_str,
        )
        self._raise_if_error(payload)
        result = payload.get("result") or {}
        user_id = result.get("userId")
        token = result.get("token")
        if not user_id or not token:
            raise AqaraAuthError(
                f"Aqara login returned no userId/token (keys: {sorted(result)})"
            )
        return AqaraTokens(user_id=user_id, token=token, raw_result=result)

    async def query_device_list(self, token: str) -> list[dict[str, Any]]:
        """Return the user's full device inventory across all homes.

        Calls ``GET /app/v1.0/lumi/app/position/device/query?size=300&startIndex=0``.

        The sign source is the alphabetically-sorted query string
        (``size=300&startIndex=0``), passed verbatim into ``_sign`` as the
        body component. The query string is sent on the URL unchanged
        (no URL-percent-encoding of values is required for these
        parameters).

        Returns the raw ``result.devices`` list. Each entry is the cloud's
        per-device record (``did``, ``model``, ``deviceName``,
        ``parentDeviceId``, ``positionId``, ``state``,
        ``firmwareVersion``, etc.).

        Raises :class:`AqaraAuthError` on auth, HTTP, or server-error
        (``code != 0``) responses.
        """
        # Alphabetically-sorted query string. Order matters: the server
        # validates the sign against this exact byte sequence. Keep the
        # parameters in alpha order and serialise without URL encoding.
        query = "size=300&startIndex=0"
        url = f"{self._area.server}{_DEVICE_LIST_PATH}?{query}"
        payload = await self._signed_request("GET", url, sign_source=query, token=token)
        self._raise_if_error(payload)
        result = payload.get("result") or {}
        devices = result.get("devices")
        if not isinstance(devices, list):
            raise AqaraAuthError(
                f"Aqara device-list response missing 'devices' "
                f"(keys: {sorted(result)})"
            )
        return devices



    async def query_view_data(
        self,
        token: str,
        did: str,
        read_keys: Iterable[str],
        process_method: str = "presentValueMethod",
    ) -> dict[str, str]:
        """Return current values keyed by data key for one device.

        Calls ``POST /app/v1.0/lumi/app/view/data/query/new`` -- the call the
        Aqara Home app's device pages make -- with body
        ``{"dataInfoBeans": [{"processMethod": <method>, "readKeys": [<keys>]}],
        "subjectId": <did>}``. With ``presentValueMethod`` the keys are legacy
        resource ids (e.g. "0.17.85"), which reaches values the hub never
        exposes over LANLink, such as per-outlet metering.

        Args:
            token: Authentication token from login.
            did: Device ID (subjectId).
            read_keys: Resource ids to read.
            process_method: ``presentValueMethod`` for current values;
                ``statisticsMethod`` takes keys like ``"1d_0_0.17.85"``.

        Parses ``result.data`` into ``{dataKey: value}``. Items lacking either
        key are skipped; a partial or empty result simply yields fewer (or no)
        entries.

        Raises :class:`AqaraAuthError` on auth, HTTP, or server-error
        responses.
        """
        body_dict = {
            "dataInfoBeans": [
                {"processMethod": process_method, "readKeys": list(read_keys)},
            ],
            "subjectId": did,
        }
        # Use default json.dumps spacing - matches the byte sequence the
        # server expects when computing the sign.
        body_str = json.dumps(body_dict)
        url = f"{self._area.server}{_VIEW_DATA_QUERY_PATH}"

        _LOGGER.debug(
            "query_view_data: POST %s body=%s",
            _VIEW_DATA_QUERY_PATH, body_str,
        )
        payload = await self._signed_request(
            "POST", url, sign_source=body_str, token=token, body=body_str,
        )
        self._raise_if_error(payload)
        result = payload.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        values: dict[str, str] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = item.get("dataKey")
                value = item.get("value")
                if key is None or value is None:
                    continue
                values[key] = value
        return values

    async def query_device_detail(
        self, token: str, did: str,
    ) -> dict[str, Any]:
        """Return extended metadata for a single device.

        Calls ``GET /app/v1.0/lumi/app/dev/query/detail?area=<region>&dids=["<did>"]``.

        The ``area`` query parameter is taken from the client's configured
        region (``self._region``) - no need for the caller to repeat it.

        Sign source is the alphabetically-sorted query string with the
        JSON-encoded ``dids`` list (NOT URL-percent-encoded in the sign
        source). The same string is sent on the URL unencoded so the
        wire bytes match the sign source exactly.

        Returns the raw ``result[0]`` dict for the single requested
        device. Common fields: ``mac``, ``model``, ``manualUrl``,
        ``parentDeviceId``, ``parentDeviceName``, ``parentModel``,
        ``homeId``, ``positionId``, ``positionName``,
        ``firmwareVersion``, ``timeZoneId``.

        Raises :class:`AqaraAuthError` on auth, HTTP, or server-error
        responses.
        """
        # JSON-encode the dids list with default spacing. This is the
        # exact byte sequence used both in the sign source and on the
        # URL. Aqara's sign convention here does NOT use URL percent
        # encoding, so the wire URL must carry the raw JSON brackets.
        dids_json = json.dumps([did])
        # Alphabetical order: 'area' before 'dids'.
        query = f"area={self._region}&dids={dids_json}"
        url = f"{self._area.server}{_DEV_QUERY_DETAIL_PATH}?{query}"
        payload = await self._signed_request("GET", url, sign_source=query, token=token)
        self._raise_if_error(payload)
        result = payload.get("result")
        if not isinstance(result, list) or not result:
            raise AqaraAuthError(
                f"Aqara dev/query/detail response missing 'result' list (got {type(result).__name__})"
            )
        return result[0]








    @staticmethod
    def _raise_if_error(payload: dict[str, Any]) -> None:
        """Raise on a non-zero code; pick the right exception type.

        Auth-failure codes (see ``_AUTH_FAILURE_CODES``) raise the narrow
        :class:`AqaraCloudAuthError` so callers can trigger HA's re-auth
        flow specifically. Every other non-zero code raises the generic
        :class:`AqaraAuthError`.
        """
        code = payload.get("code")
        if str(code) == "0":
            return
        msg = (
            f"Aqara cloud rejected request (code={code} "
            f"msg={payload.get('message')!r})"
        )
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        if code_int is not None and code_int in _AUTH_FAILURE_CODES:
            raise AqaraCloudAuthError(msg)
        raise AqaraAuthError(msg)

    async def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        data: str | None = None,
    ) -> dict[str, Any]:
        """Issue an HTTP request and return the parsed JSON response.

        ``method`` is ``"GET"`` or ``"POST"``. For POST, pass the body
        as ``data``. For GET, leave ``data=None`` (any query string
        belongs in ``url``). Raises :class:`AqaraAuthError` on HTTP
        errors or non-JSON responses.
        """
        close_session = False
        session = self._session
        if session is None and self._hass is not None:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self._hass)
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        try:
            async with session.request(method, url, data=data, headers=headers) as resp:
                text = await resp.text()
        except aiohttp.ClientError as exc:
            raise AqaraAuthError(f"Aqara HTTP error: {exc}") from exc
        finally:
            if close_session:
                await session.close()

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AqaraAuthError(
                f"Aqara non-JSON response: {text[:200]!r}"
            ) from exc
