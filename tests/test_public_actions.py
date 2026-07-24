from __future__ import annotations

from packages.connectors.base import moves_for
from packages.core.act import needs_approval

# `public` means this action writes where teammates or customers read it.
#
# It was declared on five moves and read by nothing, which is worse than not
# declaring it: the registry described a risk the system did not take. An
# operator could allowlist `comment_on_issue` for autonomy and the agent would
# post into a customer-visible thread with nobody in the loop, while the
# capability entry said "public: True" the whole time.
#
# It is capability, not policy — a fact about what the GitHub API call does —
# so the connector keeps declaring it and the GATE decides what to do about it.

COMMENT = {"kind": "http", "public": True}
LABEL = {"kind": "http", "reversible": True}


def test_a_public_action_asks_a_human_by_default() -> None:
    assert needs_approval(COMMENT, {}) is True


def test_a_private_action_still_follows_the_ordinary_policy() -> None:
    """Nothing else changes shape: a non-public move under a policy that does
    not require approval still runs."""
    assert needs_approval(LABEL, {"default_require_approval": False}) is False


def test_public_outranks_a_policy_that_would_otherwise_let_it_run() -> None:
    """The failure this exists to prevent: `default_require_approval: False`
    turning "post to a customer thread" into an unattended action."""
    assert needs_approval(COMMENT, {"default_require_approval": False}) is True


def test_allowlisting_a_public_move_for_autonomy_is_not_enough_on_its_own() -> None:
    """Autonomy reaches the gate with force_approval already resolved; being on
    the allowlist must not smuggle a public post past it."""
    assert needs_approval(COMMENT, {"default_require_approval": False, "force_approval": False}) is True


def test_an_operator_can_say_yes_out_loud() -> None:
    """Not a locked door — a decision someone has to make deliberately."""
    assert needs_approval(
        COMMENT, {"default_require_approval": False, "allow_public_actions": True}
    ) is False


def test_a_human_clicking_the_button_is_still_the_approval() -> None:
    """pre_approved means a person chose THIS action. Asking them to approve
    their own click adds a step and no safety — and would make the Feed's
    comment button take two presses."""
    assert needs_approval(COMMENT, {"pre_approved": True}) is False


def test_the_moves_that_reach_other_people_are_the_ones_marked_public() -> None:
    """Pins the intent against a future connector edit: anything that writes
    something a teammate or customer will see, and nothing that doesn't."""
    registry = moves_for("github")
    public = {name for name, spec in registry.items() if spec.get("public")}
    assert public == {
        "comment_on_issue",
        "create_issue",
        "approve_pull_request",
        "request_changes_on_pull_request",
    }
    assert not registry["apply_label"].get("public")
