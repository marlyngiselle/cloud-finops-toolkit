from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finops.queries import available_queries, load_query
from finops.resources import (
    ResourceId,
    age_in_days,
    get_tag,
    is_ignored,
    is_truthy,
    parse_timestamp,
    tags_of,
    text,
)

BASE_COLUMNS = ("id", "name", "type", "resourceGroup", "subscriptionId", "location", "tags")


class TestResourceId:
    def test_parses_resource_id(self) -> None:
        rid = ResourceId.parse(
            "/subscriptions/abc/resourceGroups/rg-app/providers/Microsoft.Compute/"
            "virtualMachines/vm-01"
        )
        assert rid == ResourceId("abc", "rg-app", "vm-01")

    def test_case_insensitive_segments(self) -> None:
        rid = ResourceId.parse("/SUBSCRIPTIONS/abc/RESOURCEGROUPS/rg/providers/x/y/z")
        assert rid.resource_group == "rg"

    @pytest.mark.parametrize("value", ["", "/subscriptions/abc", "/foo/abc/bar/rg/x"])
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValueError, match="not an ARM resource ID"):
            ResourceId.parse(value)


class TestTags:
    def test_tags_of_handles_missing_and_none_values(self) -> None:
        assert tags_of({}) == {}
        assert tags_of({"tags": None}) == {}
        assert tags_of({"tags": "oops"}) == {}
        assert tags_of({"tags": {"a": None, "b": 1}}) == {"a": "", "b": "1"}

    def test_get_tag_is_case_insensitive(self) -> None:
        assert get_tag({"Owner": "x"}, "owner") == "x"
        assert get_tag({"Owner": "x"}, "team") is None

    @pytest.mark.parametrize(("value", "expected"), [("true", True), (" YES ", True),
                                                      ("1", True), ("false", False),
                                                      ("", False), (None, False)])  # fmt: skip
    def test_is_truthy(self, value: str | None, expected: bool) -> None:
        assert is_truthy(value) is expected

    def test_is_ignored(self) -> None:
        assert is_ignored({"tags": {"FinOps-Ignore": "true"}})
        assert not is_ignored({"tags": {"finops-ignore": "no"}})

    def test_text(self) -> None:
        assert text({"a": None}, "a", "dflt") == "dflt"
        assert text({"a": 3}, "a") == "3"


class TestTimestamps:
    def test_parse_z_suffix(self) -> None:
        assert parse_timestamp("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def test_naive_is_utc(self) -> None:
        parsed = parse_timestamp(datetime(2026, 1, 1))
        assert parsed is not None
        assert parsed.tzinfo is UTC

    @pytest.mark.parametrize("value", [None, "", "yesterday", 42])
    def test_unparseable(self, value: object) -> None:
        assert parse_timestamp(value) is None

    def test_age_in_days(self) -> None:
        now = datetime(2026, 3, 11, tzinfo=UTC)
        assert age_in_days("2026-03-01T00:00:00Z", now) == 10
        assert age_in_days("2026-04-01T00:00:00Z", now) == 0  # clock skew never negative
        assert age_in_days(None, now) is None


class TestQueries:
    def test_every_query_loads_and_has_base_columns(self) -> None:
        names = available_queries()
        assert len(names) >= 11
        for name in names:
            query = load_query(name)
            assert "//" not in query.text, f"{name}: comments must be stripped"
            assert query.text.startswith("resources"), name
            for column in BASE_COLUMNS:
                assert column in query.text, f"{name} does not project {column}"

    @pytest.mark.parametrize(
        ("name", "fragment"),
        [
            ("idle/unattached-disks", "'Unattached'"),
            ("idle/stopped-vms", "'PowerState/stopped'"),
            ("idle/empty-app-service-plans", "numberOfSites"),
            ("rightsize/running-vms", "'PowerState/running'"),
            ("hybrid-benefit/sql-vms", "sqlServerLicenseType"),
        ],
    )
    def test_key_filters_present(self, name: str, fragment: str) -> None:
        assert fragment in load_query(name).text

    def test_unknown_query(self) -> None:
        with pytest.raises(KeyError):
            load_query("does/not-exist")
