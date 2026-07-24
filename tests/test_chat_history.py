from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apps.common.assistant_tools import history_for_model

# Replaying a chat is not the same as replaying STATE.
#
# This system's data changes underneath a conversation: records arrive, norms
# firm up, situations open and close. An assistant turn is a snapshot of the
# moment it was written, but replayed unstamped it reads as present tense — and
# a model will repeat its own earlier "no records have arrived yet" over a tool
# result that says nine have. That happened to a real workspace, which is why
# these are pinned.


@dataclass
class _Msg:
    role: str
    content: str
    created_at: datetime | None = None


def test_the_agents_own_answers_carry_when_they_were_said() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    history = history_for_model(
        [_Msg("agent", "No records have arrived yet.", now - timedelta(hours=5))], now
    )
    assert history[0]["role"] == "assistant"
    assert "5 hours ago" in history[0]["content"]
    assert "No records have arrived yet." in history[0]["content"]


def test_a_users_question_is_replayed_untouched() -> None:
    """A question doesn't go stale — only an answer built from data does, and
    editing what someone asked would misquote them back to the model."""
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    history = history_for_model(
        [_Msg("user", "how many issues?", now - timedelta(days=3))], now
    )
    assert history == [{"role": "user", "content": "how many issues?"}]


def test_rows_without_usable_content_are_dropped() -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    history = history_for_model(
        [
            _Msg("user", "", now),
            _Msg("system", "internal note", now),
            _Msg("agent", "Nine issues.", now),
        ],
        now,
    )
    assert len(history) == 1
    assert history[0]["role"] == "assistant"


def test_a_turn_with_no_timestamp_is_still_marked_as_past() -> None:
    """Falling back to an unstamped turn would quietly reintroduce the bug for
    exactly the oldest messages, which are the most likely to be wrong."""
    history = history_for_model([_Msg("agent", "All quiet.", None)])
    assert history[0]["content"].startswith("[said earlier")
