from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import SUB, resource
from finops.resources import Row
from finops.tags import (
    RESOURCE_GROUP_TYPE,
    PolicyError,
    TagPolicy,
    TagRule,
    ViolationKind,
    evaluate_resources,
)

POLICY: dict[str, Any] = {
    "required_tags": [
        {"name": "owner", "pattern": r"^[^@\s]+@example\.com$"},
        {"name": "environment", "allowed": ["dev", "test", "prod"]},
        "cost-center",
    ]
}


def _rg(name: str, tags: dict[str, str]) -> Row:
    return {
        "id": f"/subscriptions/{SUB}/resourceGroups/{name}",
        "name": name,
        "type": RESOURCE_GROUP_TYPE,
        "resourceGroup": name,
        "subscriptionId": SUB,
        "tags": tags,
    }


def kinds(policy: TagPolicy, tags: dict[str, str]) -> dict[str, ViolationKind]:
    return {v.tag: v.kind for v in policy.evaluate(tags)}


class TestPolicyLoading:
    def test_from_dict_accepts_strings_and_mappings(self) -> None:
        policy = TagPolicy.from_dict(POLICY)
        assert [r.name for r in policy.rules] == ["owner", "environment", "cost-center"]
        assert policy.rules[1].allowed == ("dev", "test", "prod")

    def test_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.yaml"
        path.write_text(
            "required_tags:\n  - owner\ninherit_from_resource_group: true\n"
            "exclude_types: [Microsoft.Network/NetworkWatchers]\n"
        )
        policy = TagPolicy.from_yaml(path)
        assert policy.inherit_from_resource_group
        assert "microsoft.network/networkwatchers" in policy.exclude_types

    def test_example_policy_in_repo_is_valid(self) -> None:
        root = Path(__file__).resolve().parents[1]
        policy = TagPolicy.from_yaml(root / "examples" / "tag-policy.yaml")
        assert {r.name for r in policy.rules} >= {"owner", "cost-center", "environment"}

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({}, "non-empty 'required_tags'"),
            ({"required_tags": []}, "non-empty 'required_tags'"),
            ({"required_tags": [42]}, "invalid tag rule"),
            ({"required_tags": [{"name": "a", "allowed": "x"}]}, "must be a list"),
            ({"required_tags": ["a", "A"]}, "duplicate tag rules: a"),
            ({"required_tags": [{"name": "a", "pattern": "("}]}, "invalid pattern"),
            ({"required_tags": [{"name": " "}]}, "non-empty name"),
        ],
    )
    def test_invalid_policies(self, data: dict[str, Any], message: str) -> None:
        with pytest.raises(PolicyError, match=message):
            TagPolicy.from_dict(data)

    def test_yaml_errors(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("required_tags: [unclosed")
        with pytest.raises(PolicyError, match="invalid YAML"):
            TagPolicy.from_yaml(bad)
        scalar = tmp_path / "scalar.yaml"
        scalar.write_text("just a string")
        with pytest.raises(PolicyError, match="must be a YAML mapping"):
            TagPolicy.from_yaml(scalar)


class TestRuleEvaluation:
    policy = TagPolicy.from_dict(POLICY)

    def test_compliant(self) -> None:
        tags = {"owner": "team@example.com", "environment": "prod", "cost-center": "CC-1"}
        assert self.policy.evaluate(tags) == []

    def test_missing_and_empty(self) -> None:
        result = kinds(self.policy, {"owner": "  "})
        assert result == {
            "owner": ViolationKind.EMPTY,
            "environment": ViolationKind.MISSING,
            "cost-center": ViolationKind.MISSING,
        }

    def test_allowed_values_case_insensitive_by_default(self) -> None:
        result = kinds(self.policy, {"environment": "PROD"})
        assert "environment" not in result
        result = kinds(self.policy, {"environment": "production"})
        assert result["environment"] is ViolationKind.NOT_ALLOWED

    def test_allowed_values_case_sensitive(self) -> None:
        policy = TagPolicy(rules=(TagRule("env", allowed=("prod",), case_sensitive_values=True),))
        assert kinds(policy, {"env": "PROD"}) == {"env": ViolationKind.NOT_ALLOWED}

    def test_pattern_must_match_whole_value(self) -> None:
        result = kinds(self.policy, {"owner": "team@example.com.evil"})
        assert result["owner"] is ViolationKind.PATTERN

    def test_key_case(self) -> None:
        tags = {"Owner": "team@example.com", "environment": "dev", "cost-center": "x"}
        assert self.policy.evaluate(tags) == []
        strict = TagPolicy(rules=self.policy.rules, case_sensitive_keys=True)
        violations = strict.evaluate(tags)
        assert [(v.kind, str(v)) for v in violations] == [
            (ViolationKind.KEY_CASE, "owner: key is spelled 'Owner'")
        ]

    def test_scope(self) -> None:
        policy = TagPolicy(
            rules=(TagRule("a"),),
            include_types=frozenset({"microsoft.compute/virtualmachines", "x/y"}),
            exclude_types=frozenset({"x/y"}),
        )
        assert policy.applies_to("Microsoft.Compute/virtualMachines")
        assert not policy.applies_to("x/y")
        assert not policy.applies_to("microsoft.storage/storageaccounts")


class TestReport:
    def test_inheritance_from_resource_group(self) -> None:
        policy = TagPolicy(
            rules=(TagRule("owner"), TagRule("environment")), inherit_from_resource_group=True
        )
        rows = [
            _rg("rg-a", {"owner": "a@example.com", "environment": "dev"}),
            resource("vm-1", rg="RG-A", tags={"environment": "prod"}),
            resource("vm-2", rg="rg-b"),
        ]
        report = evaluate_resources(policy, rows)
        by_name = {r.name: r for r in report.results}
        assert by_name["vm-1"].compliant
        assert not by_name["vm-2"].compliant
        assert report.summary() == {
            "evaluated": 3,
            "compliant": 2,
            "non_compliant": 1,
            "compliance_pct": 66.7,
            "violations_by_tag": {"environment": 1, "owner": 1},
        }

    def test_without_inheritance_group_tags_do_not_count(self) -> None:
        policy = TagPolicy(rules=(TagRule("owner"),))
        rows = [_rg("rg-a", {"owner": "x"}), resource("vm-1", rg="rg-a")]
        report = evaluate_resources(policy, rows)
        assert report.compliant == 1
        assert report.results[0].name == "vm-1"  # non-compliant sorted first

    def test_ignored_and_out_of_scope_resources_are_skipped(self) -> None:
        policy = TagPolicy(rules=(TagRule("owner"),), exclude_types=frozenset({"x/watcher"}))
        rows = [
            resource("w", "x/watcher"),
            resource("vm", tags={"finops-ignore": "yes"}),
        ]
        report = evaluate_resources(policy, rows)
        assert report.total == 0
        assert report.compliance_pct == 100.0

    def test_to_row(self) -> None:
        policy = TagPolicy(rules=(TagRule("owner"),))
        row = evaluate_resources(policy, [resource("vm")]).results[0].to_row()
        assert row["status"] == "non-compliant"
        assert row["violations"] == "owner: missing"
