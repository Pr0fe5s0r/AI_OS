from __future__ import annotations

from apps.common.discovery_flow import _merge_new_source
from packages.core.profile import Profile

# Connecting a SECOND source must ADD it, not replace the profile. This once
# wiped a whole GitHub setup the moment Slack was connected.


def test_merging_a_new_source_keeps_the_existing_ones():
    base = Profile(
        company_id="x",
        sources=[{"source": "github", "mapping": {"id": {}}}],
        things={"status_field": "state"},
        moves={"registry": {"create_issue": {"kind": "http"}}},
        reviewers=[{"key": "pull_request_review"}],
        rhythms=[{"name": "issue_resolution_hours"}],
    )
    induced = Profile(
        company_id="x",
        sources=[{"source": "slack", "mapping": {"id": {}}}],
        things={"status_field": "status"},  # a different, conflicting status field
        rhythms=[{"name": "reply_latency"}],
    )
    merged = _merge_new_source(base, induced, "slack")

    assert [s["source"] for s in merged.sources] == ["github", "slack"], "github must survive"
    assert merged.things["status_field"] == "state", "existing things are not clobbered"
    assert "create_issue" in merged.moves["registry"], "moves preserved"
    assert merged.reviewers == [{"key": "pull_request_review"}], "reviewers preserved"
    assert {r["name"] for r in merged.rhythms} == {"issue_resolution_hours", "reply_latency"}


def test_reconnecting_the_same_source_replaces_only_its_own_entry():
    base = Profile(company_id="x", sources=[{"source": "slack", "mapping": {"v": 1}}])
    induced = Profile(company_id="x", sources=[{"source": "slack", "mapping": {"v": 2}}])
    merged = _merge_new_source(base, induced, "slack")
    assert len(merged.sources) == 1 and merged.sources[0]["mapping"]["v"] == 2
