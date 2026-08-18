from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Markvector

# ---------------------------------------------------------------------------
# CROSS-WORKSPACE PATTERNS.
#
# One credential can read several tenants. What comes back out must not be
# about any of them.
#
# This is the only place in the SDK that reads more than one workspace, and it
# is deliberately not a search: it returns GENERALISATIONS — "activation is
# reviewed on a two-week cycle, and credential issuing is the common blocker" —
# with no client named, no document identified, and no passage quoted. A
# caller wanting one tenant's material keeps using search() and answer(),
# which are untouched by any of this.
#
# Three independent defences, because a single one is a promise rather than a
# property:
#
#   1. CORROBORATION. A pattern is emitted only if at least MIN_WORKSPACES
#      distinct workspaces support it, where support is COUNTED from their
#      passages (see supported_by) rather than taken from the model's reply.
#      This is the strongest of the three and the only one that does not
#      depend on the model behaving: a fact true of exactly one client cannot
#      survive a rule that requires two, and a model claiming otherwise does
#      not get a vote.
#
#   2. REDACTION BEFORE THE MODEL. Passages are stripped of identifiers — the
#      tenant's own name, document titles and locators, emails, URLs, money,
#      dates, ids — before any of it reaches the LLM, so the model cannot
#      repeat what it was never shown.
#
#   3. INSPECTION AFTER THE MODEL. Whatever it writes is checked against the
#      identifiers we know about, and anything carrying one is DROPPED rather
#      than cleaned up. A sentence that leaked once will leak again in a form
#      the cleaner does not recognise.
#
# The output carries no document ids and no verbatim passages, by construction:
# a Pattern has room for a statement and a count, and nowhere to put them.
# ---------------------------------------------------------------------------

# How many distinct workspaces must show something before it is a pattern
# rather than a client's business. Two is the minimum that means anything;
# raise it for a stronger claim and fewer results.
MIN_WORKSPACES = 2

# Passages read per workspace. Enough to see a repeated shape, small enough
# that one tenant cannot dominate the sample.
PER_WORKSPACE = 6

# Trimmed hard: a long excerpt is a passage wearing a disguise, and the model
# needs the shape of what is said, not its wording.
MAX_EXCERPT_CHARS = 320

# How much of a statement's substance must appear in a workspace's own passages
# before that workspace counts as SUPPORTING it. Corroboration used to be taken
# on trust: the model reported how many groups backed each statement and the
# gate below compared that number against MIN_WORKSPACES. A model that writes
# "groups": 2 for a fact true of one tenant therefore walked straight through
# the defence described at the top of this file as the one that does not depend
# on the model behaving. Measured against real data, a statement true of two
# workspaces was reported as three.
#
# So support is now counted here, from the redacted passages, and the model's
# own number is advisory at most.
SUPPORT_RATIO = 0.25

# How much of a composed SENTENCE must already be present in the findings.
# Higher than SUPPORT_RATIO because this is a different comparison: support
# matches a short statement against long passages, whereas the answer is meant
# to be a rewording of the findings and nothing else, so it should share most
# of their vocabulary. Measured on a real run: a faithful sentence scored 1.00
# against the finding it cited and a fabricated one — "after launch they track
# performance metrics", which no surviving finding said, cited [P1] anyway —
# scored 0.36.
# A ratio alone punishes length: a longer, better-written statement needs
# proportionally more matches to clear the same bar, and once the prompt asked
# for answers rather than terse generalisations, true statements grew to twenty
# stems and started failing. Measured on real runs — "a key driver for
# launching a product is responding to competitive pricing", supported by two
# workspaces whose material says "competitor pricing moved in January", shared
# 4 stems of 18 and scored 0.22. An absolute floor of three distinctive stems
# admits it while still refusing an invented statement, which shared at most
# two anywhere.
MIN_SHARED_STEMS = 3

# Words too common to be evidence of anything. Deliberately short: this list
# exists to stop "the", "with" and the vocabulary of the question itself from
# making every group look like it supports every statement, not to do topic
# modelling.
_TOO_COMMON = frozenset(
    """
    about above across after against all also and any are because been before
    being between both but can does each from had has have how into its more
    most not now off only other out over own same should some such than that
    the their them then there these they this those through under until upon
    use used using very was were what when where which while who why will with
    within without would you your
    """.split()
)


def _content_words(text: str) -> set[str]:
    """The words in a piece of text that could carry meaning."""
    return {
        word
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text.lower())
        if word not in _TOO_COMMON
    }


def _stems(text: str) -> set[str]:
    """Content words cut to a common stem.

    Comparing whole words punished exactly the behaviour we want: a good
    generalisation paraphrases, so the passage says "50 concurrent callers"
    and the statement says "concurrency", and the two shared nothing. Measured,
    a statement genuinely supported by two workspaces scored 0.23 against a
    0.25 threshold and was withheld. Six characters is enough to join
    concurrent/concurrency, rerank/reranking, accept/acceptance.
    """
    return {word[:6] for word in _content_words(text)}


def supported_by(statement: str, groups: list[list[str]]) -> int:
    """How many workspaces' passages actually carry this statement's substance.

    Counted, not asked. A group supports a statement when it shares at least
    SUPPORT_RATIO of the statement's content words — enough that the statement
    is plausibly about that group's material, rather than a generalisation the
    model reached from one group and attributed to several.

    This is deliberately a blunt overlap and not a semantic judgement: a
    corroboration rule enforced by a second model call would be back to
    trusting a model with the boundary.
    """
    words = _stems(statement)
    if not words:
        return 0
    count = 0
    for excerpts in groups:
        shared = words & _stems(" ".join(excerpts))
        if len(shared) >= MIN_SHARED_STEMS or len(shared) / len(words) >= SUPPORT_RATIO:
            count += 1
    return count


@dataclass(slots=True)
class Pattern:
    """Something true across several tenants, said in a way that is about none.

    There is nowhere here to put a document id, a workspace, or a quotation —
    not because they are stripped out late, but because the type has no field
    for them.
    """

    statement: str
    # How many distinct workspaces showed it. Never WHICH: the count is the
    # evidence, the identity is not.
    workspaces: int
    confidence: float = 0.0
    # What the answer cites instead of a document: "P1", "P2". A citation has
    # to point at something, and the only thing safe to point at is the
    # sanitised finding itself — never the passage it came from.
    id: str = ""

    def __str__(self) -> str:
        label = f"{self.id}: " if self.id else ""
        return f"{label}{self.statement}  (seen in {self.workspaces} workspaces)"


@dataclass(slots=True)
class PatternReport:
    """What a cross-workspace run produced, and what it refused to produce."""

    question: str
    # The sanitised prose answering the question, citing [P1], [P2]. This is
    # the point of the whole exercise: the caller asked something and gets an
    # answer, rather than raw passages that would carry another client's data
    # across the boundary.
    answer: str = ""
    patterns: list[Pattern] = field(default_factory=list)
    workspaces_searched: int = 0
    passages_considered: int = 0
    # Statements the model wrote that were thrown away, and why. Kept because a
    # sanitiser whose rejections are invisible cannot be audited — and because
    # a run that drops everything should look different from one that found
    # nothing.
    withheld: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "patterns": [
                {
                    "id": p.id,
                    "statement": p.statement,
                    "workspaces": p.workspaces,
                    "confidence": p.confidence,
                }
                for p in self.patterns
            ],
            "workspaces_searched": self.workspaces_searched,
            "passages_considered": self.passages_considered,
            "withheld": self.withheld,
        }


# ------------------------------- redaction -------------------------------

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
_MONEY = re.compile(r"[$£€]\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:USD|EUR|GBP|INR)\b", re.IGNORECASE)
_LONG_NUMBER = re.compile(r"\b\d[\d,]{3,}\b")
_HEX_ID = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_DATE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4})\b",
    re.IGNORECASE,
)


def redact(text: str, names: set[str]) -> str:
    """Remove what identifies a tenant, before a model ever sees it.

    `names` are the identifiers we know about for this workspace — its id, its
    collections, its document titles and locators, and the words those are
    made of. They are replaced first, because they are the ones that actually
    matter; the generic patterns below catch the rest.

    Deliberately blunt. A redactor that tries to preserve readability keeps
    exactly the details worth keeping out.
    """
    # Emails and links go FIRST, before the tenant's own words are replaced.
    # Measured the other way round: redacting "Acme" first turned
    # bob@acme.com into bob@[client].com, which no longer matches an email
    # pattern and so kept a real address in the text. Redacting a name can
    # destroy the shape a later rule was going to recognise.
    out = _EMAIL.sub("[email]", text)
    out = _URL.sub("[link]", out)
    for name in sorted(names, key=len, reverse=True):
        if len(name) < 3:
            continue
        out = re.sub(rf"\b{re.escape(name)}\b", "[client]", out, flags=re.IGNORECASE)
    out = _HEX_ID.sub("[id]", out)
    out = _MONEY.sub("[amount]", out)
    out = _DATE.sub("[date]", out)
    out = _LONG_NUMBER.sub("[number]", out)
    return out


def _identifiers(workspace: str, documents: list[Any]) -> set[str]:
    """Every string that could point back at ONE tenant.

    The workspace id and its parts, each document's whole title and locator,
    and the SEGMENTS of the locator — "acme/onboarding" gives up "acme", which
    is exactly the sort of name worth catching.

    A title is taken whole and never split. Titles here are prose: MarkVector
    derives one from the document's first line when the caller does not supply
    it, so splitting them made candidates out of "account", "during" and every
    other ordinary word in a first sentence — and the run then withheld every
    pattern it produced for naming them.

    These are candidates, not verdicts. Which of them actually identify anyone
    is decided by `distinctive()` once every workspace has been read.
    """
    names: set[str] = {workspace}
    names.update(part for part in re.split(r"[-_]", workspace) if len(part) > 2)
    for doc in documents:
        title = str(getattr(doc, "title", "") or "")
        if title:
            names.add(title)
        locator = str(getattr(doc, "locator", "") or "")
        if locator:
            names.add(locator)
            names.update(w for w in re.split(r"[/\_\-.]+", locator) if len(w) > 2)
    return {n for n in names if n}


def _filename_words(workspace: str, documents: list[Any]) -> set[str]:
    """The bare words a LOCATOR was split into, minus anything the workspace id
    already vouches for.

    These are the weakest candidates we produce, and the only ones that can be
    ordinary English. A tenant called Acme filing `acme/onboarding.md` gives up
    "acme" — worth catching — but a spreadsheet called
    `Project-Management-Sample-Data.xlsx` gives up "management", which names
    nobody. Measured: a correct pattern about how revenue is reported was
    withheld for "naming 'management'", because a filename happened to contain
    the word.

    A segment that also appears in the workspace id is NOT returned here: it is
    a real name and is judged by the ordinary rules.
    """
    strong = {workspace.lower()}
    strong.update(part.lower() for part in re.split(r"[-_]", workspace) if len(part) > 2)
    words: set[str] = set()
    for doc in documents:
        locator = str(getattr(doc, "locator", "") or "")
        if not locator:
            continue
        words.update(
            w.lower() for w in re.split(r"[/\_\-.]+", locator) if len(w) > 2
        )
    return words - strong


def distinctive(
    per_workspace_names: list[set[str]],
    per_workspace_text: list[str] | None = None,
    per_workspace_filename_words: list[set[str]] | None = None,
) -> set[str]:
    """The candidate names that belong to exactly one tenant.

    The corroboration rule again, in a different place: a word every tenant
    uses cannot identify any of them. "Onboarding" is the subject of the
    question and appears in all three tenants' material; "Acme" appears in one.

    Two passes, and the second was learned the hard way. Judging only by
    TITLES withheld every pattern the model produced — for containing
    "onboarding" and "account" — because those words happened to sit in one
    workspace's matching filenames and not in another's. Judging by the
    PASSAGE TEXT as well is what makes the distinction hold: a word that shows
    up in two tenants' prose is vocabulary, wherever their filenames put it.

    A sanitiser that removes the vocabulary of the question answers nothing,
    which is its own kind of failure.
    """
    seen: Counter[str] = Counter()
    for names in per_workspace_names:
        for name in {n.lower() for n in names}:
            seen[name] += 1

    shared_in_prose: Counter[str] = Counter()
    for text in per_workspace_text or []:
        for word in {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)}:
            shared_in_prose[word] += 1

    unique = {
        name
        for name, count in seen.items()
        if count == 1 and shared_in_prose[name] < 2
    }
    # A filename word is NOT a name. Splitting locators was meant to catch
    # "acme" in `acme/onboarding.md`, but filenames are also where ordinary
    # words live, and a bare word promoted to "tenant name" deletes every true
    # pattern that happens to use it. Measured against real data: a correct,
    # fully general statement about how revenue is reported by region was
    # withheld for "naming 'management'", because one workspace held
    # `Project-Management-Sample-Data (1).xlsx`.
    #
    # Tenant identity does not have to be guessed at. It arrives as trusted
    # metadata — MarkVector derives every workspace id from the company name
    # (`acme-7a3d9c`, `temprl-21a2b3`), so the tenant's name is already in the
    # strong set, vouched for by the server rather than inferred from a
    # filename. Anything a locator segment could add beyond that is a guess,
    # and a guess that is wrong in the direction of silence.
    #
    # What is NOT relaxed: the workspace id and its parts, whole titles and
    # whole locators all remain identifiers, so a statement echoing a document
    # or a path is still dropped whole — and corroboration across two or more
    # workspaces, the defence that does not depend on naming anything
    # correctly, is untouched.
    if per_workspace_filename_words is not None:
        for words in per_workspace_filename_words:
            unique -= words

    # A workspace id or a whole title identifies however common its parts are.
    for names in per_workspace_names:
        unique |= {n.lower() for n in names if " " in n or "/" in n or "-" in n}
    return unique


# ------------------------------- the prompt -------------------------------

_SYSTEM = """You ANSWER A QUESTION using material from several different
companies, and your answer must identify none of them.

This is not a summarising exercise and not a search. The caller may not see
the material itself — it belongs to other people — so your job is to say what
it establishes, in your own words, with nothing in it that could point back at
whose material it was.

You will be given short, already-redacted excerpts. Each is labelled only with
an anonymous group number. You never learn which company any group is.

Write FINDINGS that answer the question. A finding is a statement of substance
— a practice, a rule, a constraint, a recurring problem. Prefer what holds
across more than one group, but a finding that genuinely answers the question
is worth stating even when fewer groups show it: the caller asked something,
and an empty answer to an answerable question helps nobody.

Rules, all of them absolute:
  * Never name a company, product, person, place, or document.
  * Never name a region, market, site, team or system taken from the material.
    Say "several regions" or "each region", never which ones. Naming them is
    the single most common way one of these answers goes wrong: nobody is
    called "Iberia", so it feels safe, but which markets a client trades in is
    that client's business and not a practice worth reporting.
  * Never quote an excerpt. Say what it establishes, in your own words.
  * Never mention group numbers, ids, dates, or amounts.
  * Never say "one group" or "another group".
  * If the material does not answer the question, return an empty list. An
    empty answer is correct far more often than an invented one.

Reply with JSON only:
{"patterns": [{"statement": "...", "groups": 2, "confidence": 0.0-1.0}]}"""


# The second call sees ONLY the findings that already survived redaction and
# inspection — never a passage. That is what makes it structurally safe: it
# cannot leak what it was never shown, so the worst it can do is phrase badly.
_COMPOSE = """You write a short answer to a question, using ONLY the numbered
findings you are given.

Cite the findings you use as [P1], [P2] — those labels are the whole citation
system here. There is no document to cite: the findings came from material the
reader is not permitted to see, and naming its source would defeat the point.

Rules:
  * Use only what the findings say. Add no fact, number, name or example.
  * Cite every claim you make, as [P1] or [P1][P2].
  * Write ONE sentence per finding, in order, each citing its own label.
    Do not add a summary sentence, a conclusion, or anything the findings do
    not already say. Plain prose, no preamble, no bullet list.
  * Never mention companies, groups, workspaces, documents or excerpts.
  * Never name a region, market, site or system, even if a finding does. Write
    "several regions" instead. If a finding cannot be stated without naming
    one, leave that finding out.
  * If the findings do not answer the question, say so in one sentence."""


def _clean_json(raw: str) -> dict[str, Any]:
    """The model's reply, tolerating the wrappers models add."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ------------------------------ the inspection ------------------------------

_BANNED_SHAPES = (_EMAIL, _URL, _HEX_ID, _MONEY, _DATE, _LONG_NUMBER)


def specifics(passages: list[str]) -> set[str]:
    """Proper nouns lifted from the tenants' own material.

    Not identities — nobody is called "Iberia" — but client-specific FACTS,
    which the contract forbids just as firmly: which markets a client trades
    in is its business, not a practice worth reporting. Measured, an otherwise
    clean answer read "revenue is reported by region ... such as Iberia,
    Nordics, DACH and Benelux", every one of them lifted from one client's
    segment table.

    Title-Case words only, and never ALL-CAPS. Acronyms are overwhelmingly
    public vocabulary in this material — OSI, GBP, HTTP — and refusing them
    would cost real answers to protect nothing.
    """
    found: set[str] = set()
    for text in passages:
        for word in re.findall(r"\b[A-Z][a-z]{3,}\b", text):
            if word.lower() not in _TOO_COMMON:
                found.add(word)
    return found


def leaks(statement: str, identifiers: set[str]) -> str | None:
    """Why this statement may not be published, or None if it may.

    Checked AFTER the model has written, against the identifiers of every
    workspace read. Anything that matches is dropped whole — not edited —
    because a sentence that leaked once will leak again in a form the cleaner
    does not recognise.
    """
    lowered = statement.lower()
    for name in identifiers:
        if len(name) < 4:
            continue
        if re.search(rf"\b{re.escape(name.lower())}\b", lowered):
            return f"names {name!r}"
    for shape in _BANNED_SHAPES:
        found = shape.search(statement)
        if found:
            return f"contains {found.group(0)!r}"
    if re.search(r"\bgroup\s*\d", lowered):
        return "refers to a group number"
    return None


def names_a_specific(statement: str, known: set[str]) -> str | None:
    """Whether a statement carries a proper noun taken from the material.

    Sentence-opening words are skipped: "Revenue is reported…" must not be
    refused because "Revenue" happens to be capitalised there.
    """
    for sentence in _SENTENCE.split(statement.strip()):
        tokens = re.findall(r"\b[A-Z][a-z]{3,}\b", sentence)
        if tokens and _opens_with_capital(sentence):
            tokens = tokens[1:]
        for token in tokens:
            if token in known:
                return f"names {token!r}, which is one client's own detail"
    return None


def _opens_with_capital(sentence: str) -> bool:
    first = re.match(r"\s*([A-Za-z']+)", sentence)
    return bool(first and first.group(1)[:1].isupper())


# -------------------------------- the run --------------------------------


async def _noop() -> None:  # pragma: no cover - placeholder for symmetry
    return None


def extract(
    mv: Markvector,
    question: str,
    *,
    llm: Any,
    model: str,
    collection: str = "default",
    workspaces: list[str] | None = None,
    per_workspace: int = PER_WORKSPACE,
    min_workspaces: int = MIN_WORKSPACES,
) -> PatternReport:
    """Read every authorised workspace, and return only what generalises.

    The retrieval is one ordinary search per workspace — each one enforced by
    the server against the key's grant, so this cannot reach further than it
    was given. What is different is everything after: the excerpts are
    redacted, grouped anonymously, generalised by the model, and then inspected
    before anything is returned.

    `llm` is your own OpenAI-compatible client. As with the agent, the model
    call happens in your process and MarkVector never sees that key.
    """
    reachable = workspaces or mv.workspaces()
    report = PatternReport(question=question, workspaces_searched=0)

    # PASS ONE: read every authorised workspace, keeping the raw excerpts and
    # each workspace's candidate identifiers. Nothing is redacted yet, because
    # what counts as an identifier cannot be known until every workspace has
    # been seen — a word one tenant uses is a name, a word they all use is the
    # subject of the question.
    raw: list[list[str]] = []
    candidates: list[set[str]] = []
    filename_words: list[set[str]] = []

    for workspace in reachable:
        docs = mv.collection(collection, workspace=workspace)
        try:
            hits = docs.search(question, limit=per_workspace)
        except Exception as exc:  # noqa: BLE001 - one tenant must not end the run
            # Recorded, not swallowed. A workspace that could not be read is
            # not the same as one with nothing to say, and a report that cannot
            # tell them apart sent me hunting for a bug in the wrong place: the
            # run said "0 workspaces" and looked like a filter problem when it
            # was a request failing.
            report.withheld.append(f"a workspace could not be read: {type(exc).__name__}")
            continue
        if not hits.matches:
            continue

        names = _identifiers(workspace, [h.source for h in hits.matches])
        names |= _identifiers(workspace, hits.matches)
        weak = _filename_words(workspace, [h.source for h in hits.matches])
        weak |= _filename_words(workspace, hits.matches)
        # Every passage each document matched on, not only the winning one.
        # Taking just the headline excerpt left a question that answer() had
        # resolved from five passages arriving here as one, and the model was
        # then asked to answer from a fifth of the evidence — which it
        # correctly declined to do.
        excerpts: list[str] = []
        for hit in hits.matches:
            texts = hit.passages or [hit.clean_excerpt or hit.excerpt or ""]
            excerpts.extend(text[:MAX_EXCERPT_CHARS] for text in texts if text.strip())
        excerpts = [e for e in excerpts if e.strip()]
        if not excerpts:
            continue
        raw.append(excerpts)
        candidates.append(names)
        filename_words.append(weak)
        report.workspaces_searched += 1
        report.passages_considered += len(excerpts)

    # Corroboration is checked BEFORE the model is called, not after: with one
    # group there is nothing that can be a pattern, and asking anyway invites a
    # confident summary of a single client's business.
    if len(raw) < min_workspaces:
        report.withheld.append(
            f"{len(raw)} workspace(s) had anything to say; "
            f"{min_workspaces} are needed before a pattern can be claimed."
        )
        return report

    # PASS TWO: every tenant has now been seen, so the names that identify ONE
    # of them are known — and those are what gets removed, from the excerpts
    # here and from whatever the model writes later.
    identifiers = distinctive(
        candidates, [" ".join(e) for e in raw], filename_words
    )
    groups = [[redact(e, identifiers) for e in excerpts] for excerpts in raw]
    # Proper nouns belonging to the material itself — regions, sites, product
    # names. Not identities, but one client's own detail, which the contract
    # forbids exposing just as firmly as its name.
    own_detail = specifics([excerpt for group in groups for excerpt in group])
    prompt = "\n\n".join(
        f"GROUP {n}:\n" + "\n".join(f"- {e}" for e in excerpts)
        for n, excerpts in enumerate(groups, start=1)
    )
    reply = llm.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"QUESTION: {question}\n\n{prompt}"},
        ],
    )
    parsed = _clean_json(reply.choices[0].message.content or "")

    seen: Counter[str] = Counter()
    for row in parsed.get("patterns") or []:
        if not isinstance(row, dict):
            continue
        statement = str(row.get("statement") or "").strip()
        if not statement:
            continue
        # Counted from the passages, not taken from the model's reply. The
        # model's own figure is kept only as a ceiling: it may claim fewer
        # than the overlap suggests (it saw the text and we did not), but it
        # can never talk a statement past the corroboration rule.
        measured = supported_by(statement, groups)
        claimed = int(row.get("groups") or 0)
        supported = min(measured, claimed) if claimed else measured
        if supported < min_workspaces:
            report.withheld.append(
                f"only {supported} workspace(s) supported: {statement[:60]}…"
            )
            continue
        why = leaks(statement, identifiers) or names_a_specific(statement, own_detail)
        if why:
            report.withheld.append(f"withheld ({why})")
            continue
        if seen[statement.lower()]:
            continue
        seen[statement.lower()] += 1
        report.patterns.append(
            Pattern(
                statement=statement,
                workspaces=min(supported, report.workspaces_searched),
                confidence=round(float(row.get("confidence") or 0.0), 2),
                id=f"P{len(report.patterns) + 1}",
            )
        )

    # SECOND CALL: the prose answer. It is composed from the surviving
    # findings ALONE — every one of which has already been redacted and
    # inspected — so this step cannot reveal a passage it never received.
    # Splitting it in two is the point: one call reads the tenants' material
    # and may only emit sanitised findings; the other writes for the caller and
    # never sees the material at all.
    if report.patterns:
        report.answer = _compose(
            question, report.patterns, llm, model, identifiers, own_detail, report
        )
    return report


def _compose(
    question: str,
    patterns: list[Pattern],
    llm: Any,
    model: str,
    identifiers: set[str],
    own_detail: set[str],
    report: PatternReport,
) -> str:
    """Turn the surviving findings into an answer that cites them as [P1]."""
    numbered = "\n".join(f"{p.id}: {p.statement}" for p in patterns)
    try:
        reply = llm.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _COMPOSE},
                {"role": "user", "content": f"QUESTION: {question}\n\nFINDINGS:\n{numbered}"},
            ],
        )
        text = (reply.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - the findings still stand without prose
        report.withheld.append(f"the answer could not be composed: {type(exc).__name__}")
        return ""

    # Inspected like everything else. It should be incapable of leaking, having
    # seen only sanitised text — but "should be incapable" is exactly the kind
    # of reasoning this module refuses to rely on anywhere else.
    why = leaks(text, identifiers) or names_a_specific(text, own_detail)
    if why:
        report.withheld.append(f"the composed answer was withheld ({why})")
        return ""
    return _faithful(text, patterns, report)


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _faithful(text: str, patterns: list[Pattern], report: PatternReport) -> str:
    """Keep the answer within the findings it was given, structurally.

    An earlier version scored each sentence's word overlap against the
    findings and dropped the low scorers. That is unsound, and measuring it
    said so: two faithful sentences that paraphrased well ("labelled as
    partial" written as "clearly marked as incomplete") scored 0.29 and 0.33,
    while a sentence that invented a claim about post-launch tracking scored
    0.36. Overlap separates rewording from invention not at all, and the cost
    of guessing was an empty answer sitting above a perfectly good finding.

    So the bound is structural instead: the composer is asked for one sentence
    per finding, and gets no more than that. An extra sentence is where a
    fabricated claim appears — there is no finding left for it to be about —
    and dropping it costs nothing, because every finding has already been
    stated by the time it arrives.

    This is not a grounding check and does not pretend to be one. The privacy
    guarantee does not rest on it: the composer only ever sees findings that
    were already redacted and inspected, so the worst an over-reaching
    sentence can do is be wrong, never disclose.
    """
    sentences = [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]
    if len(sentences) <= len(patterns):
        return " ".join(sentences)
    for extra in sentences[len(patterns):]:
        report.withheld.append(
            f"a sentence beyond the findings was dropped: {extra[:60]}…"
        )
    return " ".join(sentences[: len(patterns)])


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MIN_WORKSPACES",
    "PER_WORKSPACE",
    "Pattern",
    "PatternReport",
    "extract",
    "leaks",
    "redact",
]
