# Joyful BLE Positioning

Joyful BLE Positioning is a diagnostic, admin-only Home Assistant custom integration for
reading bounded, current BLE observations from Home Assistant's public scanner cache. It is
designed as an observation bridge for an external indoor-positioning backend.

The integration is deliberately narrow:

- entity-free, with no sensors, devices, services, or event-bus events;
- current-state-only, with no Recorder or statistics writes;
- default-off, because installing the code does nothing until an administrator explicitly
  creates its singleton configuration entry; and
- read-only toward Bluetooth scanners: it does not initiate discovery, connect to beacons, or
  change scanner settings.

When its singleton configuration entry is loaded, the integration exposes the admin-only
`joyful_ble_positioning/subscribe_observations` WebSocket command. Each subscription accepts a
strictly bounded tracker and scanner-source allowlist and emits only minimized, freshness-bounded
RSSI observations. Unloading or reloading the entry immediately detaches the runtime and erases
subscription identity state.

## Safety

BLE signal strength is noisy and this component is diagnostic only. It is not suitable for
person safety, access control, emergency response, or automation-authoritative positioning.

## Development

The development environment requires Python 3.14.2 or later within the Python 3.14 series and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run pytest -q
```

## License

MIT
