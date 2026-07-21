# Joyful BLE Positioning

Joyful BLE Positioning is a diagnostic, admin-only Home Assistant custom integration that exposes
bounded current BLE observations from Home Assistant's public scanner cache. It is an observation
bridge for an external indoor-positioning backend, not a positioning engine by itself.

## Install with HACS

This repository is a HACS custom repository; it is not listed in the default HACS store.

1. In HACS, open **Integrations**, choose **Custom repositories**, and add
   `https://github.com/joyfulhouse/joyful-ble-positioning` with category **Integration**.
2. Install release **v0.1.0**.
3. Restart Home Assistant. The restart is mandatory after installing or replacing custom
   integration files.
4. Open **Settings → Devices & services → Add integration**, search for
   **Joyful BLE Positioning**, and create its singleton configuration entry.

Installing the files alone is inert. The integration does not start a runtime until its one config
entry loads, and it does not sample until an administrator creates a WebSocket subscription.

## WebSocket API

The command type is `joyful_ble_positioning/subscribe_observations`. The caller must be a Home
Assistant administrator. This synthetic request observes one static-address tracker through one
scanner source:

```json
{
  "id": 1,
  "type": "joyful_ble_positioning/subscribe_observations",
  "trackers": [
    {
      "trackerId": "11111111-1111-4111-8111-111111111111",
      "kind": "static-mac",
      "identity": "02:00:00:00:00:01"
    }
  ],
  "scannerSources": ["synthetic-scanner-source"]
}
```

After the normal success result, events contain only the stream ID, caller-supplied tracker ID,
scanner source, sequence, state, age, and—while observed—RSSI, optional TX power, and receive time:

```json
{
  "id": 1,
  "type": "event",
  "event": {
    "streamId": "22222222-2222-4222-8222-222222222222",
    "state": "observed",
    "trackerId": "11111111-1111-4111-8111-111111111111",
    "scannerSource": "synthetic-scanner-source",
    "sequence": 1,
    "rssiDbm": -61,
    "txPowerDbm": null,
    "ageMs": 125,
    "receivedAt": "2026-01-01T00:00:00.000Z"
  }
}
```

Unsubscribe with Home Assistant's standard `unsubscribe_events` command, using the original
subscription message ID.

### Limits and freshness

- Up to 8 trackers and 64 scanner sources per subscription.
- Up to 4 simultaneous subscriptions process-wide.
- One shared sampler runs at most 4 Hz regardless of subscriber count.
- An observation becomes stale after 10 seconds without a newer scanner-cache timestamp. Scanner
  source disappearance emits `stale` immediately, so `state` is authoritative and a stale
  event's `ageMs` can be less than 10,000.
- Supported identities are strict static MAC, iBeacon UUID/major/minor, and resolved-address
  forms. Request values are canonicalized and held only in memory for the subscription lifetime.

## Privacy and lifecycle

The bridge reads only Home Assistant's public, current scanner cache. It does not initiate scans,
connect to beacons, use scanner history, call a network client, create entities or devices, register
services, fire event-bus events, or write Recorder/statistics data. Raw tracker identities are never
included in bridge responses or integration logs. Scanner-source names appear only in successful
observation events requested by the administrator.

Unloading or reloading the config entry detaches the runtime before cleanup and erases retained
identity, callback, pairing, and task state. The WebSocket command remains registered but returns a
fixed unavailable error until a compatible entry is loaded again.

The integration is pinned to Home Assistant 2026.7's public Bluetooth scanner-cache contract. If
that capability changes, setup fails closed and Home Assistant creates a nonpersistent warning
repair. Updating to a compatible integration or Home Assistant version and reloading the entry
removes the repair automatically.

## Safety

BLE signal strength is noisy and this component is diagnostic only. Do not use it as the sole input
for person safety, access control, emergency response, or other automation-authoritative decisions.

## Development

Development requires Python 3.14.2 or later within the Python 3.14 series and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy custom_components/joyful_ble_positioning
uv run pytest -q
```

## License

MIT
