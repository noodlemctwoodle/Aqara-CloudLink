<p align="center">
  <img src="docs/images/aqaralink-icon.png" alt="AqaraLink" width="160">
</p>

<h1 align="center">AqaraLink</h1>

<p align="center">
  A Home Assistant integration for Aqara hubs, their sub-devices and cameras -
  local where the hub allows it, cloud-backed where it does not.
</p>

---

## What this is

Aqara hubs speak a private protocol, **LANLink**, over the local network. Home
Assistant can talk it directly: state is pushed from the hub over a persistent
encrypted TCP connection, and commands are written back the same way, with no
round-trip through Aqara's servers.

That protocol was reverse-engineered by Dan Gibson in
[absent42/Aqara-LANLink](https://github.com/absent42/Aqara-LANLink). AqaraLink
is a fork of that project. **Every line of the LANLink and P2P protocol work is
his.** See [Relationship to Aqara-LANLink](#relationship-to-aqara-lanlink) below
for what is his, what is new here, and which project you should actually be
running.

What AqaraLink adds is a second source. Some values the Aqara Home app shows
simply are not served over LANLink at all - the hub does not answer for them in
any addressing form. AqaraLink reads those from Aqara's cloud on an interval, so
the entity exists and holds a value instead of being absent or permanently
unknown. Everything the hub does serve stays local and pushed, exactly as
before.

> **This replaces `aqara_lanlink`. Do not run both.** The hub allows one LANLink
> session per Aqara account, so the two integrations will fight over it and
> displace each other. Remove one before adding the other.

Aqara does not endorse or support this integration. The protocol was
reverse-engineered from the Aqara app and hub firmware, and a firmware update
may break it without warning.

**AqaraLink is early. Treat it as beta.**

---

## Relationship to Aqara-LANLink

This section exists so there is no confusion about who did what.

**Upstream's work, not ours.** The LANLink tunnel (ECDH key exchange, AES
encryption, framing, check-in, session lifecycle), the P2P camera protocol, the
device catalogue of ~379 models, the trait and override system, the config and
subentry flows, the camera, PTZ and go2rtc work, the Repairs integration, and
the documentation that explains all of it. AqaraLink inherits the lot
essentially unchanged. If something here works well locally, that is Dan's
doing.

**What this fork adds.** A `cloud_poll` flag on a trait, a cloud client method
for the app's `view/data/query/new` endpoint, and a polling loop that delivers
those values through the same path a LANLink push would take. Plus the model
overrides that use it. That is the whole delta.

**Why it is a fork and not a pull request.** It was offered upstream and
declined, correctly: Aqara-LANLink is local-push by design and says so in its
README and architecture docs. Adding a cloud poller would have changed what that
project is. Forking was the right answer, not a disagreement.

**Which one should you run?**

| | Aqara-LANLink | AqaraLink |
|---|---|---|
| Local push for everything the hub serves | yes | yes |
| Values the hub does not serve over LANLink | absent | read from the cloud on an interval |
| Cloud contact in steady state | none | one call per interval, and only if a model declares cloud-polled traits |
| Origin | the original | forked from it |

If local-only operation matters to you and you can live without the handful of
values the hub withholds, **run upstream**. It is the original, it is the better
fit for that goal, and it is actively maintained. Run AqaraLink if you
specifically want those extra values and accept a cloud dependency for them.

Issues with AqaraLink belong on
[this tracker](https://github.com/noodlemctwoodle/AqaraLink/issues), not
upstream's - please do not send our bugs to Dan. Fixes made here that are not
cloud-related are offered back upstream where they apply.

Licensed MIT, as upstream is. See [LICENSE](LICENSE); the original copyright
notice is retained.

---

## How it works

AqaraLink opens an encrypted tunnel from Home Assistant to an Aqara hub using
the LANLink protocol. Once that tunnel is up, sub-device state changes and
commands run over the LAN: the hub relays them to the sub-devices and pushes
state back, with no Aqara server in the path.

The cloud is still involved, and it is worth being precise about where:

- **At setup.** Adding a hub makes a small number of HTTPS calls to Aqara's
  regional API: a login that exchanges credentials for a session token,
  sub-device enumeration to build the device picker, and a light-effects fetch.
- **At every restart and reconnect.** The local push subscription is *armed
  through the cloud*, not over LANLink. Home Assistant re-arms it and seeds
  current values on each setup or reload. If Aqara's cloud is unreachable at
  that moment, devices come up without seeded state and pushes do not start
  until the next successful re-arm. Once armed, cloud availability has no
  further effect on state or control.
- **On the poll interval, for cloud-polled traits only.** Models that declare
  `cloud_poll` traits get one additional cloud call per interval (60 seconds by
  default). Models that declare none make no such calls at all.

Only a session token is stored in the config entry. Your password is not
retained.

For the protocol and session lifecycle in detail, see
[docs/architecture.md](docs/architecture.md).

---

## Features

- **~379 Aqara device models catalogued**, rendering with friendly entity names,
  labelled enum options and correct units on add.
- **Local push in steady state.** Device state arrives over the persistent
  encrypted LANLink tunnel and control is written back the same way, with no
  cloud round-trip once the subscription is armed.
- **Cloud-backed values where LANLink has none.** Traits marked `cloud_poll` in
  a model's overrides are read from Aqara's cloud on an interval and delivered
  through the same handler a LANLink push uses, so the entities behave normally.
  Nothing else is affected: a model with no such traits never triggers a poll.
- **Hub clusters and multiple regions.** Several hubs, including hubs bound to
  different Aqara regions, and Zigbee devices parented to a peer hub rather than
  the one Home Assistant connects to.
- **Eleven entity platforms** - sensor, binary sensor, switch, light, number,
  select, button, event, text, time and camera - derived from each device's
  catalogue entry.
- **Local device settings.** Child lock, indicator light, button mode,
  power-off memory, max power, find and restart, and similar, exposed as
  entities and written fully locally over LANLink by resource ID.
- **Cameras and doorbells.** RTSP streaming via go2rtc, doorbell ring events,
  motion, occupancy and gesture events, and two-way audio through the go2rtc
  backchannel.
- **Local camera PTZ.** Pan, tilt, zoom and saved-position presets as buttons, a
  preset selector and an `aqaralink.ptz` action.
- **Rich lights.** Brightness, colour temperature and full colour fused into one
  light entity, with Aqara and user-created dynamic and static effects in the
  effect picker.
- **Self-healing.** Automatic reconnect, push-liveness recovery, and Home
  Assistant Repairs that flag stalled hubs or newly discovered capabilities.
- **Contributor tooling.** Per-model overrides for labels, entity types and
  units, plus a `scan_device` action that exports both new traits and
  resource-ID pairs ready for a pull request.

### Worked example: Wall Outlet H2 per-socket metering

`lumi.plug.aeu002` meters each socket separately and the Aqara app shows it, but
the hub only serves the device totals over LANLink. Probing confirmed there is
no local path: the per-outlet resources are `EndpointLabel` metadata rather than
measurements, the obvious per-endpoint trait addresses do not exist, and the
legacy resource IDs the app uses get no reply - bare or instance-suffixed,
across every endpoint and instance index tried.

AqaraLink declares those legacy resource IDs as `cloud_poll` traits, giving
eight extra sensors per device: power, cumulative energy, energy today and
energy this month, for each of the two sockets. The same overrides drop
`Current voltage`, which this hardware does not measure - it reads 0 V under a
1.6 kW load and is never reported.

---

## Requirements

- Home Assistant **2025.4.0** or later.
- An Aqara hub supporting LAN Control. Tested on the **M3** (firmware
  `4.5.50_0019` / `4.5.60_0017`) and the **M200** as a cluster member; other
  `lumi1.` hubs may work but are unvalidated.
- **Hub Cluster** and **LAN Control** enabled on the hub, from your profile
  settings in the Aqara Home app. Without them the hub refuses the encrypted
  session.
- An Aqara account (email and password), or a user ID and session token obtained
  elsewhere. Only the resulting token is stored.
- go2rtc for camera streaming and two-way audio. Optional; declared as an
  `after_dependency`.

---

## Installation

### HACS (custom repository)

[![Open HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=noodlemctwoodle&repository=AqaraLink&category=Integration)

AqaraLink is not in the default HACS catalogue. Add it as a custom repository:

1. Click the badge above and confirm the prompt. Or, in HACS, open the
   three-dot menu, choose **Custom repositories**, paste
   `https://github.com/noodlemctwoodle/AqaraLink` and select **Integration** as
   the type.
2. Find **AqaraLink** in the list and click **Download**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/aqaralink/` into your Home Assistant configuration
directory and restart:

```
<config>/
  custom_components/
    aqaralink/
      __init__.py
      manifest.json
      ...
```

### Migrating from `aqara_lanlink`

Remove the `aqara_lanlink` config entry and delete its directory from
`custom_components/` before adding AqaraLink. The two cannot share the hub's
single LANLink session. Entity IDs are not carried over, so expect to re-add
your devices and fix up any automations that referenced the old entities.

---

## Configuration

Setup is two steps: add the hub, then add devices as sub-entries.

### 1. Add the hub

1. **Settings > Devices & Services > Add Integration**, then search for
   **AqaraLink**.
2. Click **Add hub**. Hubs on the same LAN appear in a discovered list. If
   yours does not, choose **Enter manually** and give its IP address and DID
   (shown in the Aqara app under hub settings; it starts with `lumi1.`).
3. On the credentials step, enter either your Aqara account email, password and
   region, or a user ID and token pasted directly to skip the cloud login.
4. Confirm the hub details.

### 2. Add devices

1. On the integration card, click **Add a device**.
2. AqaraLink queries the cloud for sub-devices paired to this hub and lists the
   supported ones. Pick one and submit. One device per run; rerun to add more.
3. Cameras prompt for RTSP credentials in a follow-up step.
4. Reload the integration to create the entities, or restart Home Assistant if
   you added a camera with a go2rtc stream.

If the stored token stops being accepted, Home Assistant raises a
re-authentication prompt on the integration card; the entry is updated and
reloaded automatically once you supply fresh credentials.

---

## Device support

Around 379 models are catalogued. Catalogued devices come up with friendly
names, labelled options and correct units straight away. Uncatalogued devices
may still work through auto-discovery, with entity names derived from the wire
trait ID and raw enum values instead of friendly labels.

The live list is at [docs/catalogue/index.md](docs/catalogue/index.md).

Most catalogued models are untested against real hardware and may produce
incorrect or non-functioning entities. The override mechanism exists to correct
them, and the catalogue improves through contributions.

Coverage is bounded by what Aqara's protocol exposes per model, so this does not
claim the full Aqara feature set. To add or improve a device, see
[docs/adding-device-support.md](docs/adding-device-support.md). If a device is
added but missing traits you expect, run the `scan_device` action rather than
removing and re-adding it.

---

## Actions

| Action | Purpose |
|---|---|
| `aqaralink.scan_device` | Re-scan a device for traits and resource IDs, and export what it finds |
| `aqaralink.export_overlay` | Export a model overlay ready for a pull request |
| `aqaralink.ptz` | Drive camera pan, tilt, zoom and presets from one call |
| `aqaralink.play_audio_file` | Play an audio file through a device that supports it |

See [docs/services.md](docs/services.md) for parameters.

---

## Documentation

- [docs/architecture.md](docs/architecture.md) - protocol and session lifecycle
- [docs/catalogue/index.md](docs/catalogue/index.md) - supported device list
- [docs/overrides.md](docs/overrides.md) - per-model overrides, including
  `cloud_poll`
- [docs/adding-device-support.md](docs/adding-device-support.md) - adding or
  improving a device
- [docs/services.md](docs/services.md) - available actions
- [docs/ptz.md](docs/ptz.md) - camera PTZ setup
- [docs/faq.md](docs/faq.md) - frequently asked questions
- [docs/troubleshooting.md](docs/troubleshooting.md) - troubleshooting and known
  limitations

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a device, author traits,
write tests and open a pull request.

Contributions that improve local behaviour are welcome here, and are worth
offering to [upstream](https://github.com/absent42/Aqara-LANLink) too, where
they will reach more people.

---

## License

MIT. See [LICENSE](LICENSE). Copyright 2026 Dan Gibson for the original work;
the fork's additions are copyright 2026 Toby Goulden.
