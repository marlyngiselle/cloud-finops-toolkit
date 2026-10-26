"""Small helpers shared by every command: resource IDs, tags and timestamps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

Row = dict[str, Any]
"""One record as returned by Azure Resource Graph (``objectArray`` format)."""

IGNORE_TAG = "finops-ignore"
"""Resources carrying this tag with a truthy value are left out of every report."""

_TRUTHY = {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class ResourceId:
    """The parts of an ARM resource ID that reports and actions need."""

    subscription_id: str
    resource_group: str
    name: str

    @classmethod
    def parse(cls, value: str) -> ResourceId:
        parts = [p for p in value.strip().split("/") if p]
        lowered = [p.lower() for p in parts]
        if len(parts) < 4 or lowered[0] != "subscriptions" or lowered[2] != "resourcegroups":
            raise ValueError(f"not an ARM resource ID: {value!r}")
        return cls(subscription_id=parts[1], resource_group=parts[3], name=parts[-1])


def tags_of(row: Mapping[str, Any]) -> dict[str, str]:
    """Return the row's tags as ``str -> str``; missing or malformed tags become ``{}``."""
    raw = row.get("tags")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def get_tag(tags: Mapping[str, str], key: str) -> str | None:
    """Case-insensitive tag lookup (ARM tag names are case-insensitive)."""
    wanted = key.lower()
    for k, v in tags.items():
        if k.lower() == wanted:
            return v
    return None


def is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def is_ignored(row: Mapping[str, Any], ignore_tag: str = IGNORE_TAG) -> bool:
    return is_truthy(get_tag(tags_of(row), ignore_tag))


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp as returned by ARM. Naive values are treated as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age_in_days(value: object, now: datetime) -> int | None:
    ts = parse_timestamp(value)
    if ts is None:
        return None
    return max(0, (now - ts).days)


def text(row: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read a column as a string, mapping ``None`` to ``default``."""
    value = row.get(key)
    return default if value is None else str(value)
