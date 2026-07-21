"""Validated in-memory request models for BLE observation subscriptions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from .const import MAX_SCANNER_SOURCES, MAX_TRACKERS, WS_TYPE

type TrackerKind = Literal["static-mac", "ibeacon", "resolved-address"]

_TRACKER_KEYS = frozenset({"trackerId", "kind", "identity"})
_SUBSCRIPTION_KEYS = frozenset({"trackers", "scannerSources"})
_WEBSOCKET_KEYS = frozenset({"id", "type"})
_TRACKER_KINDS = frozenset({"static-mac", "ibeacon", "resolved-address"})

_MAX_TRACKER_FIELD_LENGTH = 64
_MAX_IDENTITY_LENGTH = 64
_MAX_SCANNER_SOURCE_LENGTH = 255

_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MAC_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
_IBEACON_PATTERN = re.compile(
    r"^(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
    r"(?P<major>0|[1-9][0-9]{0,4})/(?P<minor>0|[1-9][0-9]{0,4})$"
)


class SubscriptionValidationError(ValueError):
    """Raised when a subscription request fails closed validation."""


@dataclass(frozen=True, slots=True)
class TrackerSpec:
    """One canonical tracker identity held only in subscription memory."""

    tracker_id: UUID
    kind: TrackerKind
    identity: str


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    """A bounded tracker and scanner-source allowlist."""

    trackers: tuple[TrackerSpec, ...]
    scanner_sources: tuple[str, ...]


def _invalid(message: str) -> SubscriptionValidationError:
    """Build a validation error whose message never contains request values."""
    return SubscriptionValidationError(message)


def _parse_tracker_id(value: object) -> UUID:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_TRACKER_FIELD_LENGTH
        or _UUID_PATTERN.fullmatch(value) is None
    ):
        raise _invalid("invalid tracker id")
    return UUID(value)


def _canonical_mac(value: str) -> str:
    if _MAC_PATTERN.fullmatch(value) is None:
        raise _invalid("invalid tracker identity")
    return value.upper()


def _canonical_ibeacon(value: str) -> str:
    match = _IBEACON_PATTERN.fullmatch(value)
    if match is None:
        raise _invalid("invalid tracker identity")

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    if major > 65535 or minor > 65535:
        raise _invalid("invalid tracker identity")

    ibeacon_uuid = UUID(match.group("uuid"))
    return f"{ibeacon_uuid}/{major}/{minor}"


def _parse_tracker(value: object) -> TrackerSpec:
    if not isinstance(value, dict) or set(value) != _TRACKER_KEYS:
        raise _invalid("invalid tracker shape")

    tracker_id = _parse_tracker_id(value["trackerId"])
    kind_value = value["kind"]
    identity_value = value["identity"]
    if (
        not isinstance(kind_value, str)
        or len(kind_value) > _MAX_TRACKER_FIELD_LENGTH
        or kind_value not in _TRACKER_KINDS
    ):
        raise _invalid("invalid tracker kind")
    if not isinstance(identity_value, str) or len(identity_value) > _MAX_IDENTITY_LENGTH:
        raise _invalid("invalid tracker identity")

    kind = cast(TrackerKind, kind_value)
    identity = (
        _canonical_ibeacon(identity_value) if kind == "ibeacon" else _canonical_mac(identity_value)
    )
    return TrackerSpec(tracker_id=tracker_id, kind=kind, identity=identity)


def _parse_source(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SCANNER_SOURCE_LENGTH
        or value != value.strip()
        or not value.isprintable()
    ):
        raise _invalid("invalid scanner source")
    return value


def _validate_envelope(message: Mapping[object, object]) -> None:
    has_id = "id" in message
    has_type = "type" in message
    if has_id != has_type:
        raise _invalid("invalid subscription shape")
    if not has_id:
        return

    message_id = message["id"]
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 0:
        raise _invalid("invalid subscription shape")
    if message["type"] != WS_TYPE:
        raise _invalid("invalid subscription shape")


def parse_subscription_spec(message: object) -> SubscriptionSpec:
    """Validate and canonicalize one bounded WebSocket subscription request."""
    if not isinstance(message, dict):
        raise _invalid("invalid subscription shape")

    keys = set(message)
    if not _SUBSCRIPTION_KEYS.issubset(keys) or not keys.issubset(
        _SUBSCRIPTION_KEYS | _WEBSOCKET_KEYS
    ):
        raise _invalid("invalid subscription shape")
    _validate_envelope(message)

    tracker_values = message["trackers"]
    source_values = message["scannerSources"]
    if not isinstance(tracker_values, list) or not 1 <= len(tracker_values) <= MAX_TRACKERS:
        raise _invalid("invalid tracker count")
    if not isinstance(source_values, list) or not 1 <= len(source_values) <= MAX_SCANNER_SOURCES:
        raise _invalid("invalid source count")

    trackers = tuple(_parse_tracker(value) for value in tracker_values)
    sources = tuple(_parse_source(value) for value in source_values)
    if len({tracker.tracker_id for tracker in trackers}) != len(trackers):
        raise _invalid("duplicate tracker id")
    if len(set(sources)) != len(sources):
        raise _invalid("duplicate scanner source")

    return SubscriptionSpec(trackers=trackers, scanner_sources=sources)
