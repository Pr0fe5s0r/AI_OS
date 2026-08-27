# MarkVector v2 — What We Built, In Plain Language

*A briefing document for the sales conversation. Written so a non-technical founder
can explain the product confidently, understand what makes it different, and answer
the "but how does it actually work?" question without hand-waving.*

---

## 1. The one-sentence version

**MarkVector is a knowledge engine that reads your company's documents the way a
careful analyst would — and every answer it gives points back to the exact
sentence, on the exact page, it came from.**

If you remember nothing else, remember this: most "AI search" tools guess and hope.
MarkVector **reads, cites, and can prove it**. That difference is the whole product.

---

## 2. The problem we solve

Every company is sitting on a pile of documents — PDFs, contracts, reports, specs,
research, meeting notes, scanned pages. The knowledge is *in there*, but getting it
out means someone remembering which file, opening it, and scrolling.

The last two years produced a wave of "chat with your documents" tools. They have a
well-known, trust-destroying flaw: **they make things up.** They return a confident,
fluent answer that sounds right and is sometimes wrong — and you have no way to tell
which is which. For anything that matters (a legal clause, a financial figure, a
compliance rule), "sometimes wrong with no warning" is worse than useless.

**MarkVector is built from the ground up to refuse to do that.** Its central design
principle is: *never state anything a real document doesn't say, and always show the
receipt.*

---

## 3. What happens when you add a document (Ingestion)

Think of ingestion as **onboarding a new document into the system's memory.** Here is
the journey, in order:

**a. Anything goes in.** PDFs, Word documents, Markdown, plain text, and — importantly
— **scanned documents and images that have no readable text at all.** When a PDF is
just pictures of pages (a scan, a photographed contract), the system *looks at each
page with vision*, the way a person reads a photocopy, and turns it into text it can
work with. Competitors typically choke on these and silently drop them.

**b. Everything becomes one clean format.** No matter what came in, it's converted to a
single tidy internal format so the rest of the system treats a Word doc, a PDF, and a
scan identically. The **original file is also kept**, untouched, so we can always show
the customer the real document exactly as they uploaded it.

**c. The document is split into "passages."** A 40-page report isn't one blob — it's cut
into meaningful sections (a passage), because when you ask a question, you want *the
paragraph that answers it*, not "somewhere in this 40-page file." The passage is the
unit we can point a citation at.

**d. Each passage gets a mathematical "meaning fingerprint" (an embedding).** This is
what lets the system find content by *meaning*, not just by matching words. Ask "why
did sales drop?" and it finds the paragraph about "the quarterly revenue decline" even
though it shares no words with your question.

**e. We only do the expensive work when something actually changed.** Re-uploading the
same document, or re-syncing a folder that hasn't moved, costs essentially nothing —
the system notices the content is identical and skips the work. **This is a real cost
advantage** and a concrete thing to mention: we don't re-bill the customer for
re-reading documents they already have.

> **Sales line:** *"You can throw anything at it — including scanned and photographed
> documents most tools can't read — and it never charges you twice for the same file."*

---

## 4. How answering a question works (Retrieval)

This is the heart of the product, and where we're genuinely differentiated. Most tools
do one thing: grab a few paragraphs that look similar to the question and hand them to
an AI. We do something closer to **how a knowledgeable person actually finds an answer.**

We offer two modes:

### Mode 1: "Agentic" — the system reads its way to the answer (our flagship)

Instead of blindly grabbing text, the system behaves like a researcher who *knows where
to look*:

1. **It's handed the table of contents of every document** — titles and section
   headings, plus a short summary of what each document and section contains.
2. **It decides which sections are worth opening**, and reads them — the actual full
   text, not a preview.
3. **If the first section isn't enough, it reads another.** It can course-correct. This
   is the crucial difference: older tools had to commit to an answer *before* reading
   anything, and when they guessed wrong, they'd wrongly report "nothing here."
4. **It can search across everything** for a specific number, name, or code that no
   heading would advertise ("find SKU-4471").
5. **It can follow the thread.** Having found one relevant passage, it can hop to the
   passages sitting *next to it in meaning* — related material that's one step away.
6. **When the answer lives in a picture** — a chart, a diagram, a table whose columns
   got scrambled — it *looks at the actual page image* rather than guessing from the
   mangled text. It will even tell you *which quarter's bar is tallest* by reading the
   chart visually.

### Mode 2: "Hybrid" — fast, direct search

For customers and developers who want speed and predictability, a classic fast search
that combines two techniques:
- **Meaning-based search** (finds "revenue decline" for "why did sales drop")
- **Exact-word search** (finds precise codes, names, and identifiers that meaning-search
  misses)

Neither is enough alone; fusing them is what makes it reliable. This mode is
instant, repeatable, and is what other software can build on top of.

> **Sales line:** *"Other tools skim. Ours actually opens the documents, reads the
> relevant sections, follows the trail, and looks at the charts — then answers."*

---

## 5. The thing that builds trust: grounding and citations

This is the feature to lead with for any serious buyer (legal, finance, compliance,
research). Three hard rules are enforced in the software — not suggestions, not
"usually":

1. **If we found nothing relevant, the AI is never even asked the question.** An empty
   context is exactly when AI models invent most confidently, so we simply don't let it.
   The customer gets an honest *"nothing here covers that"* instead of a confident
   fabrication.

2. **Every claim must cite the passage it came from** — a numbered reference `[1]`,
   `[2]` — and those passages stay **on screen beneath the answer** so the customer can
   read the source themselves.

3. **Citations are verified, not trusted.** The system checks that each reference
   actually points at real supplied text. If the AI invents a reference to something
   that wasn't there, it's stripped out. For picture-based answers, the customer is even
   shown *the page, with the exact row or cell highlighted.*

And critically: **the system tells you when it's not sure.** Every answer is stamped
"grounded" or "not grounded." A degraded answer never gets to masquerade as a healthy
one. That honesty *is* the product.

> **Sales line:** *"Every answer comes with its receipts. If it can't show you where an
> answer came from, it tells you it doesn't know — it will never bluff."*

---

## 6. The "smart memory" that improves itself (Self-organising memory)

This is an advanced differentiator worth mentioning to a technical or sophisticated
buyer. Between questions, quietly in the background, the system **tidies and organises
its own memory** — much like a brain consolidating during sleep:

- **De-duplication:** identical passages collapse into one.
- **Merging:** several passages that all say the same thing become a single, richer
  summary — *while keeping a record of exactly which originals it came from*, so it can
  always be traced back.
- **Forgetting:** material that's superseded or never used gradually fades and is
  eventually dropped, so the store stays sharp instead of bloated.
- **Reinforcement:** what people actually use gets *stronger* and sticks around.

The result: the knowledge base gets **better organised and cheaper to search over
time**, and — again — nothing is ever merged or summarised without a traceable link
back to the real source documents. The original files are never altered.

> **Sales line:** *"It's not a static filing cabinet. It organises itself over time —
> the more it's used, the sharper it gets — without ever losing the paper trail."*

---

## 7. It's a product *and* a building block (the SDK)

MarkVector isn't only an app with a nice interface. It ships with a **developer kit
(SDK)** in both Python and JavaScript, so other companies can embed our engine inside
*their* software in a few lines of code:

```python
docs = mv.collection("client-research")
docs.add("Q2 paid conversions fell 18 percent.", locator="notes/q2")

answer = docs.answer("summarise Q2 performance")
print(answer.grounded)   # <- you can check honesty programmatically
```

This matters commercially because it opens **two revenue paths at once**:
- **End customers** who use our polished app.
- **Developers and companies** who pay to build MarkVector's retrieval into their own
  products (a platform / infrastructure play).

Everything is organised into **workspaces and collections** with scoped API keys, so
one account can cleanly separate clients, projects, or departments — each walled off
from the others.

---

## 8. Why we win — the short list to memorise

| What buyers fear | What MarkVector does |
|---|---|
| "AI makes things up" | Refuses to answer with no evidence; cites every claim; flags when unsure |
| "I can't trust it for anything important" | Shows the exact source passage, page, and highlighted region |
| "It can't read our scanned/old documents" | Reads scans and images with vision, page by page |
| "It gets expensive fast" | Never re-processes unchanged content; a fast deterministic mode for scale |
| "It's a black box" | Every answer records *why* it returned what it did — fully inspectable |
| "We'd have to rebuild our stack" | Drop-in SDK for Python and JavaScript |

---

## 9. Talking points, distilled to plain sentences

Use these verbatim if helpful:

- *"It reads your documents and answers questions — but unlike everything else out
  there, it shows you exactly where every answer came from, and it refuses to guess."*

- *"Think of it as a tireless analyst who has read every document you own, never
  forgets, and always cites the page."*

- *"It even reads scanned contracts and charts by *looking* at the page, the way a
  person would — not just the text a computer can copy-paste."*

- *"The system gets smarter and cheaper to run the more it's used, because it organises
  its own memory in the background."*

- *"And it's not just an app — developers can plug our engine into their own products,
  so we sell to end-users and to builders at the same time."*

---

## 10. One honest caveat (so you're never caught out)

If a buyer's questioner is sharp, they may ask: *"So the AI still writes the final
answer — how is that different from the tools that hallucinate?"*

The honest, strong answer: **"Yes, an AI writes the sentence — but it's only allowed to
use text we physically put in front of it from real documents, every sentence is checked
back against those documents, any claim we can't trace is removed, and if there's
nothing to draw from, it's not allowed to answer at all. We don't ask the AI to *know*
things. We ask it to *read* and *report* — and then we check its work."**

That's the difference between a tool that *sounds* trustworthy and one that's *built* to
be.

---

*Prepared from the MarkVector v2 engine (the "Knowledge-base" build). Technical
reference for anyone who wants to go deeper: ingestion pipeline
(`packages/core/pipeline.py`), the reading agent (`packages/core/navigator.py`),
grounded answering (`packages/core/answer.py`), self-organising memory
(`packages/core/consolidate.py`), and the developer kit (`sdk/markvector`).*
