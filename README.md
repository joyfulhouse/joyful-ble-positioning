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

This repository currently contains the inert integration and singleton config-flow scaffold.
The bounded admin observation subscription will be added in a later implementation task.

## Safety

BLE signal strength is noisy and this component is diagnostic only. It is not suitable for
person safety, access control, emergency response, or automation-authoritative positioning.

## Development

The development environment requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run pytest -q
```

## License

MIT
