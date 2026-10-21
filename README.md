<p align="center">
  <img src="docs/images/aqara-cloudlink-icon.png" alt="Aqara CloudLink" width="160">
</p>

<h1 align="center">Aqara CloudLink</h1>

<p align="center">
  Home Assistant sensors for Aqara values that only the cloud serves.
</p>

---

## What this is

Aqara hubs serve most of a device's state over the local network, and
[Aqara-LANLink](https://github.com/absent42/Aqara-LANLink) does an excellent job
of reading it. But some values the Aqara Home app displays are never served
locally at all — the hub does not answer for them in any addressing form.

Aqara CloudLink reads those from Aqara's cloud and exposes them as ordinary Home
Assistant sensors. That is all it does.

It is **not** a replacement for Aqara-LANLink and does not speak the LANLink
protocol. It makes no local connection to your hub, opens no tunnel, and holds
no LANLink session. Run it alongside Aqara-LANLink, or on its own — neither is
required by the other.

> **Early software.** Three models are covered so far. Adding more is a table
> entry; see [Adding a model](#adding-a-model).

---

## What you get

### Wall Outlet H2 (`lumi.plug.aeu002`) — ten sensors

Per-socket metering, which the app shows and the hub does not serve:

| | Socket 1 | Socket 2 |
|---|---|---|
| Power (W) | yes | yes |
| Energy, cumulative (Wh) | yes | yes |
| Energy today (Wh) | yes | yes |
| Energy this month (Wh) | yes | yes |

Plus the device's own **energy today** and **energy this month**. LANLink
serves the running total; these two period figures answer on the cloud alone.

The H2 meters each socket separately, but the hub serves only the device
totals. Probing confirmed there is no local path: the per-outlet resources are
`EndpointLabel` metadata rather than measurements, the obvious per-endpoint
trait addresses do not exist, and the legacy resource ids the app uses get no
reply — bare or instance-suffixed, across every endpoint and instance index
tried.

Socket 1 shares an endpoint with the USB-C port, so its figures include USB
draw. The two sockets sum to slightly under the device total; the difference is
the USB port plus the plug's own consumption.

### Camera G100 (`lumi.camera.agl005`) and Doorbell Camera G400 (`lumi.camera.agl013`) — three sensors

microSD card state, which no local entity reports:

| Sensor | Values |
|---|---|
| SD card | **OK** or **No card** |
| SD card capacity | MB |
| SD card free | MB |

Enough to alert on a card that has fallen out, failed, or filled up — the
failure mode where a camera keeps streaming and quietly records nothing.

Confirmed by inserting, formatting and removing a card while watching the
cloud: `sdstatus` reads `0` with a working card and `1` with an empty slot
(note the direction), free space tracks continuous recording minute to minute,
and the reported totals match the app's own figures. The G400 answers the same
resource id, so it gets the sensors whether or not a card is fitted.

---

## How it works

On setup, Aqara CloudLink logs in to Aqara's regional API, lists the devices on your
account, and keeps the ones whose model it has metrics for. Everything else is
ignored.

Every 60 seconds it reads those values with
`POST /app/v1.0/lumi/app/view/data/query/new` — the call the app's own device
pages make. Current values use the present-value method; today and this-month
totals use the statistics method, and are the cloud's own aggregates, the same
numbers the app shows.

Only the session token is stored. Your password is not retained.

**Device cards.** If Aqara-LANLink is installed and already owns a device,
Aqara CloudLink puts its sensors on that same device card rather than creating a
duplicate — the cloud readings sit alongside the local ones. If it is not
installed, Aqara CloudLink registers its own device. This is opportunistic only:
nothing requires Aqara-LANLink to be present, and if you later remove it the
device survives carrying the cloud sensors.

---

## Requirements

- Home Assistant 2025.4.0 or later.
- An Aqara account, and devices on it that this integration has metrics for.

No hub configuration, no LAN access, no go2rtc.

---

## Installation

### HACS (custom repository)

[![Open HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=noodlemctwoodle&repository=Aqara-CloudLink&category=Integration)

1. Click the badge above, or in HACS open the three-dot menu, choose **Custom
   repositories**, paste `https://github.com/noodlemctwoodle/Aqara-CloudLink` and
   select **Integration**.
2. Find **Aqara CloudLink** and click **Download**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/aqara_cloudlink/` into your configuration directory and
restart.

### Configure

**Settings > Devices & Services > Add Integration**, search for **Aqara CloudLink**,
and enter your Aqara email, password and region. Supported devices on the
account are added automatically.

---

## Adding a model

Everything model-specific lives in one table,
[`metrics.py`](custom_components/aqara_cloudlink/metrics.py). Nothing else in the
integration is model-aware.

To add a device, add its model string with the resource ids the cloud serves
for it:

```python
METRICS: dict[str, tuple[CloudMetric, ...]] = {
    "lumi.your.model": (
        CloudMetric(
            key="0.17.85",
            name="Socket 1 energy",
            device_class=SensorDeviceClass.ENERGY,
            unit=UnitOfEnergy.WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
        ),
    ),
}
```

Keys are legacy resource ids (`0.17.85`) or the app's statistics keys
(`1d_0_0.17.85` for today, `month_0_0.17.85` for this month). Keys containing
an underscore are read with the statistics method automatically.

Three optional fields cover the cases the plain form does not:

- `transport=Transport.RESOURCE_RID` for a resource id the device-page call
  will not answer for. Camera storage is one of these.
- `field="freesize"` when the value arrives as a JSON object and you want one
  entry from it. Several metrics can share a key this way; the resource is
  still fetched once per poll.
- `states=((0, "ok"), (1, "no_card"))` with `translation_key` for a value that
  is a state rather than a measurement. Add the names under `entity.sensor` in
  `strings.json`, or the dashboard shows the raw option strings. A number not
  in the list leaves the sensor unavailable rather than being guessed at.

Finding the resource ids for a device means watching what the Aqara app
requests when you open its page. Pull requests welcome, ideally with a note on
how you confirmed the values.

---

## Credits

The cloud client in [`cloud.py`](custom_components/aqara_cloudlink/cloud.py) is
derived from `hub/cloud_client.py` in
[absent42/Aqara-LANLink](https://github.com/absent42/Aqara-LANLink) by Dan
Gibson, MIT licensed. The protocol reverse engineering — request signing,
region handling, password encryption and endpoint semantics — is his work.
It is reduced here to the four calls this integration needs.

If you want local push for Aqara devices, that is the project to use. This one
only fills in what it cannot reach.

---

## License

MIT. See [LICENSE](LICENSE).
