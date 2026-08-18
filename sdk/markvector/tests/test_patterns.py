"""Cross-workspace patterns: what may be said about several tenants at once.

The rule being tested is narrow and absolute — what comes out must be about
none of them. Three independent defences, and the tests are organised by which
one they exercise, because if any single one were doing all the work this
would be a promise rather than a property.
"""

from __future__ import annotations

import httpx
import pytest
from markvector import Markvector, Pattern, PatternReport
from markvector.patterns import _identifiers, extract, leaks, redact

BASE = "http://localhost:8000"


class FakeLLM:
    """An OpenAI-compatible stub that says whatever the test needs it to."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.chat = self  # completions.create is reached as llm.chat.completions.create
        self.completions = self

    def create(self, *, model, messages, temperature=0.0):  # noqa: ARG002
        self.prompts.append(messages[-1]["content"])
        message = type("M", (), {"content": self.reply})
        return type("R", (), {"choices": [type("C", (), {"message": message})]})


def store(handler) -> Markvector:
    return Markvector(api_key="test", base_url=BASE, transport=httpx.MockTransport(handler))


# ------------------------- 1. redaction, before the model -------------------------


def test_a_tenants_own_name_is_removed():
    assert "acme" not in redact("Acme renews annually", {"Acme"}).lower()


def test_emails_and_links_survive_the_name_pass():
    """Order matters, and the wrong order was measured: redacting "Acme" first
    turned bob@acme.com into bob@[client].com, which no longer matches an email
    pattern — so a real address stayed in the text."""
    out = redact("mail bob@acme.com or see https://acme.com/x", {"Acme"})
    assert "bob@" not in out and "acme.com" not in out
    assert "[email]" in out and "[link]" in out


def test_amounts_dates_and_ids_go():
    out = redact("On 2026-03-04 they paid $40,000 under ref a1b2c3d4e5f60718", set())
    for gone in ("2026-03-04", "40,000", "a1b2c3d4e5f60718"):
        assert gone not in out


def test_redaction_respects_word_boundaries():
    """Blunt, but not so blunt it mangles unrelated words — a redactor whose
    output is nonsense produces patterns about nonsense."""
    assert redact("Acmeson lives here", {"Acme"}) == "Acmeson lives here"


def test_identifiers_take_a_title_whole_and_split_only_the_locator():
    """Titles are prose — MarkVector derives one from the first line when the
    caller supplies none — so splitting them made candidates out of "account",
    "during" and every other ordinary word in an opening sentence, and the run
    then withheld every pattern it produced for naming them. A locator is
    structured, so its segments are fair game and "acme/onboarding" gives up
    the name worth catching.
    """
    doc = type(
        "D",
        (),
        {
            "title": "Acme onboarding audit. Every new account gets a review during setup.",
            "locator": "acme/onboarding",
        },
    )
    names = {n.lower() for n in _identifiers("poc-acme", [doc])}
    assert "acme" in names
    assert "acme/onboarding" in names
    assert not {"account", "during", "every"} & names


# ------------------------- 2. inspection, after the model -------------------------


def test_a_statement_naming_a_tenant_is_refused():
    assert leaks("Acme reviews activation fortnightly", {"acme"})


def test_a_general_statement_passes():
    assert leaks("Activation is reviewed on a two-week cycle", {"acme"}) is None


def test_shapes_that_can_only_be_specifics_are_refused():
    for specific in (
        "Teams pay $40,000 for onboarding",
        "Reviews happen on 2026-03-04",
        "See https://example.com/policy",
        "Contact ops@example.com",
        "Document a1b2c3d4e5f60718 covers it",
    ):
        assert leaks(specific, set()), specific


def test_group_numbers_are_refused():
    """The prompt labels tenants as groups. A statement that says "group 2"
    re-identifies by position, which is identity with extra steps."""
    assert leaks("Group 2 escalates faster than the others", set())


# ------------------------- 3. corroboration, structural -------------------------


def test_one_workspace_alone_produces_nothing():
    """The defence that does not depend on getting redaction right: a fact true
    of exactly one client cannot survive a rule that requires two."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(200, json={"workspace_id": "a", "workspaces": ["a"]})
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "i", "title": "Acme audit", "excerpt": "Acme reviews activation.",
                     "score": 1.0, "source": {"source": "s", "locator": "acme/x"}}
                ],
                "trace_id": "t",
            },
        )

    llm = FakeLLM('{"patterns": [{"statement": "Activation is reviewed", "groups": 1}]}')
    report = extract(store(handler), "how is activation handled?", llm=llm, model="m")
    assert report.patterns == []
    assert report.withheld, "a run that refuses should say so"
    assert llm.prompts == [], "the model should not even have been asked"


def test_a_pattern_needs_support_from_two_workspaces():
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Activation is reviewed on a two-week cycle", '
        '"groups": 2, "confidence": 0.8}]}'
    )
    assert [p.statement for p in report.patterns] == [
        "Activation is reviewed on a two-week cycle"
    ]
    assert report.patterns[0].workspaces == 2


def test_a_statement_the_model_claims_for_one_group_is_dropped():
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Someone runs a 14-day review", "groups": 1}]}'
    )
    assert report.patterns == []
    assert any("only 1" in w for w in report.withheld)


def test_a_leaking_statement_is_dropped_whole_not_cleaned():
    """Editing it would leave a sentence that leaked once and now looks safe."""
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Acme and the others review activation", "groups": 2}]}'
    )
    assert report.patterns == []
    assert any("withheld" in w for w in report.withheld)


def test_the_excerpts_reaching_the_model_are_already_redacted():
    report = _two_workspace_run('{"patterns": []}')
    assert report.workspaces_searched == 2
    prompt = _LAST_LLM.prompts[0]
    for identifying in ("Acme", "Globex", "acme/", "globex/"):
        assert identifying not in prompt, prompt


def test_the_model_is_never_told_which_tenant_a_group_is():
    _two_workspace_run('{"patterns": []}')
    prompt = _LAST_LLM.prompts[0]
    assert "GROUP 1" in prompt and "GROUP 2" in prompt
    assert "poc-" not in prompt


# ------------------------------ what comes back ------------------------------


def test_a_pattern_has_nowhere_to_put_an_identifier():
    """Not stripped late — the type has no field for a document id, a
    workspace, or a quotation."""
    fields = set(Pattern.__dataclass_fields__)
    assert fields == {"statement", "workspaces", "confidence"}


def test_the_report_is_serialisable_without_leaking_structure():
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Credentials are the common blocker", "groups": 2}]}'
    )
    payload = report.as_dict()
    assert set(payload) == {
        "question",
        "patterns",
        "workspaces_searched",
        "passages_considered",
        "withheld",
    }
    assert set(payload["patterns"][0]) == {"statement", "workspaces", "confidence"}


def test_a_report_says_what_it_refused():
    """A sanitiser whose rejections are invisible cannot be audited."""
    report = _two_workspace_run('{"patterns": [{"statement": "Acme does X", "groups": 2}]}')
    assert isinstance(report, PatternReport)
    assert report.withheld


# --------------------------- single-workspace is untouched ---------------------------


def test_ordinary_search_is_not_redacted():
    """Single-workspace work keeps its verbatim passages: there is no
    cross-tenant boundary to protect, and sanitising there would degrade the
    product for no gain."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "i", "title": "Acme audit", "excerpt": "Acme reviews activation.",
                     "score": 1.0, "source": {"source": "s", "locator": "acme/x"}}
                ],
                "trace_id": "t",
            },
        )

    hits = store(handler).collection("c").search("activation")
    assert "Acme" in hits.matches[0].excerpt


def test_a_collection_handle_names_its_workspace_only_when_asked():
    sent: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(dict(request.headers))
        return httpx.Response(200, json={"results": [], "trace_id": "t"})

    mv = store(handler)
    mv.collection("c").search("q")
    mv.collection("c", workspace="poc-globex").search("q")
    assert "x-workspace" not in sent[0]
    assert sent[1]["x-workspace"] == "poc-globex"


# --------------------------------- helpers ---------------------------------

_LAST_LLM: FakeLLM


def _two_workspace_run(reply: str) -> PatternReport:
    """Two tenants, one document each, saying much the same thing."""
    global _LAST_LLM

    bodies = {
        "poc-acme": ("Acme audit", "acme/onboarding", "Acme runs a 14-day activation review."),
        "poc-globex": ("Globex audit", "globex/onboarding", "Globex reviews activation after two weeks."),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/whoami":
            return httpx.Response(
                200,
                json={"workspace_id": "poc-acme", "workspaces": list(bodies)},
            )
        workspace = request.headers.get("x-workspace", "poc-acme")
        title, locator, text = bodies[workspace]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "item-1",
                        "title": title,
                        "excerpt": text,
                        "score": 0.9,
                        "source": {"source": "seed", "locator": locator},
                    }
                ],
                "trace_id": "t",
            },
        )

    _LAST_LLM = FakeLLM(reply)
    return extract(store(handler), "how is onboarding handled?", llm=_LAST_LLM, model="m")


@pytest.fixture(autouse=True)
def _reset():
    yield
