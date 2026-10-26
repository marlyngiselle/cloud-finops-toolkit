"""``finops tags``: evaluate resources against a required-tags policy.

Tags are what turn a cloud bill into something an owner can act on: without
``owner`` and ``cost-center`` a finding has nobody to send it to. The policy is a
small YAML file (see ``examples/tag-policy.yaml``)::

    required_tags:
      - name: owner
        pattern: '^[^@\\s]+@example\\.com$'
      - name: environment
        allowed: [dev, test, prod]
    case_sensitive_keys: false
    inherit_from_resource_group: true
    exclude_types:
      - microsoft.network/networkwatchers
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from finops.resources import IGNORE_TAG, Row, is_ignored, tags_of, text

RESOURCE_GROUP_TYPE = "microsoft.resources/subscriptions/resourcegroups"


class PolicyError(ValueError):
    """The tag policy file is malformed."""


class ViolationKind(StrEnum):
    MISSING = "missing"
    EMPTY = "empty"
    NOT_ALLOWED = "not-allowed"
    PATTERN = "pattern-mismatch"
    KEY_CASE = "key-case"


@dataclass(frozen=True)
class TagRule:
    name: str
    allowed: tuple[str, ...] = ()
    pattern: str | None = None
    case_sensitive_values: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PolicyError("tag rule needs a non-empty name")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise PolicyError(f"invalid pattern for tag {self.name!r}: {exc}") from exc


@dataclass(frozen=True)
class Violation:
    tag: str
    kind: ViolationKind
    message: str

    def __str__(self) -> str:
        return f"{self.tag}: {self.message}"


@dataclass(frozen=True)
class TagPolicy:
    rules: tuple[TagRule, ...]
    case_sensitive_keys: bool = False
    inherit_from_resource_group: bool = False
    include_types: frozenset[str] = frozenset()
    exclude_types: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TagPolicy:
        raw_rules = data.get("required_tags")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise PolicyError("policy must define a non-empty 'required_tags' list")
        rules: list[TagRule] = []
        for item in raw_rules:
            if isinstance(item, str):
                rules.append(TagRule(name=item))
            elif isinstance(item, Mapping) and "name" in item:
                allowed = item.get("allowed") or []
                if not isinstance(allowed, list):
                    raise PolicyError(f"'allowed' for tag {item['name']!r} must be a list")
                rules.append(
                    TagRule(
                        name=str(item["name"]),
                        allowed=tuple(str(v) for v in allowed),
                        pattern=None if item.get("pattern") is None else str(item["pattern"]),
                        case_sensitive_values=bool(item.get("case_sensitive_values", False)),
                    )
                )
            else:
                raise PolicyError(f"invalid tag rule: {item!r}")
        names = [r.name.lower() for r in rules]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise PolicyError(f"duplicate tag rules: {', '.join(duplicates)}")
        return cls(
            rules=tuple(rules),
            case_sensitive_keys=bool(data.get("case_sensitive_keys", False)),
            inherit_from_resource_group=bool(data.get("inherit_from_resource_group", False)),
            include_types=frozenset(str(t).lower() for t in data.get("include_types") or []),
            exclude_types=frozenset(str(t).lower() for t in data.get("exclude_types") or []),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> TagPolicy:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PolicyError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(data, Mapping):
            raise PolicyError(f"{path}: policy must be a YAML mapping")
        return cls.from_dict(data)

    def applies_to(self, resource_type: str) -> bool:
        rtype = resource_type.lower()
        if self.include_types and rtype not in self.include_types:
            return False
        return rtype not in self.exclude_types

    def evaluate(self, tags: Mapping[str, str]) -> list[Violation]:
        violations: list[Violation] = []
        for rule in self.rules:
            violation = self._check_rule(rule, tags)
            if violation:
                violations.append(violation)
        return violations

    def _check_rule(self, rule: TagRule, tags: Mapping[str, str]) -> Violation | None:
        if rule.name in tags:
            value = tags[rule.name]
        else:
            matches = [k for k in tags if k.lower() == rule.name.lower()]
            if not matches:
                return Violation(rule.name, ViolationKind.MISSING, "missing")
            if self.case_sensitive_keys:
                return Violation(
                    rule.name, ViolationKind.KEY_CASE, f"key is spelled {matches[0]!r}"
                )
            value = tags[matches[0]]

        if not value.strip():
            return Violation(rule.name, ViolationKind.EMPTY, "empty value")
        if rule.allowed:
            if rule.case_sensitive_values:
                ok = value in rule.allowed
            else:
                ok = value.lower() in {a.lower() for a in rule.allowed}
            if not ok:
                return Violation(
                    rule.name,
                    ViolationKind.NOT_ALLOWED,
                    f"{value!r} not in [{', '.join(rule.allowed)}]",
                )
        if rule.pattern and not re.fullmatch(rule.pattern, value):
            return Violation(
                rule.name, ViolationKind.PATTERN, f"{value!r} does not match {rule.pattern}"
            )
        return None


@dataclass(frozen=True)
class ResourceCompliance:
    name: str
    type: str
    resource_group: str
    subscription_id: str
    resource_id: str
    violations: tuple[Violation, ...]

    @property
    def compliant(self) -> bool:
        return not self.violations

    def to_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "resource_group": self.resource_group,
            "status": "compliant" if self.compliant else "non-compliant",
            "violations": "; ".join(str(v) for v in self.violations),
            "subscription_id": self.subscription_id,
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True)
class TagReport:
    results: tuple[ResourceCompliance, ...]
    violations_by_tag: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def compliant(self) -> int:
        return sum(1 for r in self.results if r.compliant)

    @property
    def compliance_pct(self) -> float:
        return 100.0 if not self.results else round(100.0 * self.compliant / self.total, 1)

    def summary(self) -> dict[str, Any]:
        return {
            "evaluated": self.total,
            "compliant": self.compliant,
            "non_compliant": self.total - self.compliant,
            "compliance_pct": self.compliance_pct,
            "violations_by_tag": dict(sorted(self.violations_by_tag.items())),
        }


def evaluate_resources(
    policy: TagPolicy, rows: Iterable[Row], *, ignore_tag: str = IGNORE_TAG
) -> TagReport:
    """Evaluate Resource Graph rows (resources and resource groups) against ``policy``."""
    rows = list(rows)
    group_tags: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if text(row, "type").lower() == RESOURCE_GROUP_TYPE:
            key = (text(row, "subscriptionId").lower(), text(row, "name").lower())
            group_tags[key] = tags_of(row)

    results: list[ResourceCompliance] = []
    counter: Counter[str] = Counter()
    for row in rows:
        rtype = text(row, "type")
        if not policy.applies_to(rtype) or is_ignored(row, ignore_tag):
            continue
        tags = tags_of(row)
        if policy.inherit_from_resource_group and rtype.lower() != RESOURCE_GROUP_TYPE:
            key = (text(row, "subscriptionId").lower(), text(row, "resourceGroup").lower())
            inherited = group_tags.get(key, {})
            own_keys = {k.lower() for k in tags}
            tags = {**{k: v for k, v in inherited.items() if k.lower() not in own_keys}, **tags}
        violations = tuple(policy.evaluate(tags))
        counter.update(v.tag for v in violations)
        results.append(
            ResourceCompliance(
                name=text(row, "name"),
                type=rtype.lower(),
                resource_group=text(row, "resourceGroup"),
                subscription_id=text(row, "subscriptionId"),
                resource_id=text(row, "id"),
                violations=violations,
            )
        )
    results.sort(key=lambda r: (r.compliant, r.resource_group.lower(), r.name.lower()))
    return TagReport(results=tuple(results), violations_by_tag=dict(counter))
