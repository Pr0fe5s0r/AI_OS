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
from markvector.patterns import _identifiers, distinctive, extract, leaks, redact

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
    # The statement has to be one the fixture's passages actually support:
    # corroboration is counted from the text now, so an unsupported statement
    # is withheld and there would be nothing left to serialise.
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Activation is reviewed after two weeks", "groups": 2}]}'
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


# ------------------- filename words are not tenant names -------------------


def test_an_ordinary_word_in_a_filename_does_not_become_a_name():
    """Measured against real data: a correct pattern about how revenue is
    reported by region was withheld for "naming 'management'", because one
    workspace held `Project-Management-Sample-Data (1).xlsx` and the locator
    was split into words. An identifier that deletes true, general answers is
    not protecting anybody.
    """
    from markvector.patterns import _filename_words

    doc = type("D", (), {"title": "Project Management Data", "locator": "Project-Management-Sample-Data (1).xlsx"})
    words = _filename_words("acme-9da3b8", [doc])
    assert "management" in words, "it is still a candidate..."

    # ...and the candidate is dropped, because nothing ties it to one tenant.
    surviving = distinctive(
        [{"management"}, {"other"}],
        ["a table of regional revenue", "another table of regional revenue"],
        [{"management"}, set()],
    )
    assert "management" not in surviving


def test_a_tenant_name_still_survives_because_the_workspace_id_carries_it():
    """The other half: relaxing filenames must not disarm the defence.

    It does not, because tenant identity never depended on the filename.
    MarkVector derives the workspace id from the company name, so the name is
    in the strong set — vouched for by the server, not inferred.
    """
    surviving = distinctive(
        [{"globex-4f2a1c", "globex"}, {"other-91bc"}],
        ["reviews happen weekly", "activation is reviewed weekly"],
        [{"report"}, set()],
    )
    assert "globex" in surviving


def test_a_whole_title_or_locator_is_still_dropped():
    """Relaxing the SEGMENTS does not relax the whole. A statement echoing a
    document title or a path still names a document, which the contract
    forbids just as firmly as naming a client."""
    surviving = distinctive(
        [{"Acme onboarding audit", "acme/onboarding.md"}, {"other-91bc"}],
        ["a", "b"],
        [{"onboarding"}, set()],
    )
    assert "acme onboarding audit" in surviving
    assert "acme/onboarding.md" in surviving


def test_a_name_carried_by_the_workspace_id_is_never_weakened():
    """`acme` reached us from both the workspace id and a locator. Filename
    judgement applies only to what the workspace id does NOT already vouch
    for, or the strongest signal we have would be overridden by the weakest.
    """
    from markvector.patterns import _filename_words

    doc = type("D", (), {"title": "Onboarding", "locator": "acme/onboarding.md"})
    assert "acme" not in _filename_words("poc-acme", [doc])


# ---------------- corroboration is counted, not taken on trust ----------------


def test_support_is_measured_from_the_passages():
    from markvector.patterns import supported_by

    groups = [
        ["activation is reviewed on a two-week cycle by a named owner"],
        ["a named owner reviews activation every two weeks"],
        ["the canteen menu rotates monthly"],
    ]
    assert supported_by("Activation is reviewed by a named owner", groups) == 2


def test_a_model_cannot_inflate_its_way_past_corroboration():
    """The hole this closes. Corroboration is described at the top of the
    module as the defence that does not depend on the model behaving — but the
    gate compared MIN_WORKSPACES against a number the model wrote. Measured
    against real data, a statement true of two workspaces was reported as
    three, and a statement true of ONE would have passed the same way.
    """
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Every team runs a mandatory zebra husbandry drill", '
        '"groups": 2, "confidence": 0.99}]}'
    )
    assert report.patterns == [], "nothing in either workspace says this"
    assert any("only 0 workspace" in w or "only 1 workspace" in w for w in report.withheld)


def test_a_genuinely_shared_statement_still_passes():
    """The counter-test, so the fix above cannot be satisfied by refusing
    everything: both tenants really do describe a 14-day activation review."""
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Activation is reviewed after two weeks", '
        '"groups": 2, "confidence": 0.8}]}'
    )
    assert len(report.patterns) == 1
    assert report.patterns[0].workspaces == 2


def test_the_model_may_lower_the_count_but_never_raise_it():
    report = _two_workspace_run(
        '{"patterns": [{"statement": "Activation is reviewed after two weeks", "groups": 1}]}'
    )
    assert report.patterns == []


def test_support_survives_paraphrase():
    """Measured on real passages: a statement about "concurrency and timing
    constraints" is supported by a passage reading "50 concurrent callers with
    a budget of 840 milliseconds". Whole-word comparison scored that 0.23 and
    withheld a true pattern; a generaliser punished for generalising is no
    use."""
    from markvector.patterns import supported_by

    # Both groups as they actually arrived: several passages each, the
    # reranking rule alongside the conformance case.
    reranking = (
        "and MUST NOT be cached as complete. This applies to every reranking "
        "path without exception, including the degraded path in section 7.6."
    )
    conformance = (
        "Conformance is demonstrated by acceptance case A-761, exercising the "
        "boundary at 50 concurrent callers with a budget of 840 milliseconds."
    )
    groups = [[reranking, conformance], [conformance, reranking]]
    statement = (
        "Conformance to caching and reranking rules is verified through acceptance "
        "cases that test performance under specific concurrency and timing constraints."
    )
    assert supported_by(statement, groups) == 2
