# AI Usage Reflection — IntelliKnow KMS

This document describes how AI was used in IntelliKnow KMS: where it sits inside the
product itself, how it accelerated the build, and the adjustments that were made to keep
AI outputs trustworthy. It satisfies the delivery requirement to document the key moments
AI was used, the iteration speedups achieved, and the adjustments made to AI outputs
(Requirement 15.3).

The single AI vendor across the whole system is **Aliyun Tongyi Qwen-Max via the DashScope
API**, chosen because it is reachable from the AWS China deployment region and covers every
AI task the product needs (parsing, classification, generation) with one credential and one
client. Embeddings use DashScope `text-embedding-v3` from the same vendor.

---

## 1. Where AI Lives in the Product

AI is not a bolt-on feature in IntelliKnow KMS — it is on the critical path of both the
ingestion pipeline and the live query pipeline. There are three distinct moments where the
AI_Model does work that would otherwise require brittle heuristics or manual effort.

### 1.1 Document Parsing and Structuring (ingestion path)

When an Admin uploads a document, format-specific loaders (pdfplumber, openpyxl,
python-docx, plain text/markdown readers) do the deterministic first pass: they recover raw
text and pull table cell grids out of the source file. That raw extraction is rarely clean —
reading order is scrambled, section boundaries are implicit, and tables arrive as loose
cell arrays.

Qwen-Max is then used to **structure and normalize** that extracted content:

- Repairing reading order and labeling sections so chunks carry meaningful context.
- Rendering each extracted table as a GitHub-flavored markdown table so that row/column
  relationships survive chunking and retrieval. This is what lets an embedded **HR salary
  grid** remain answerable — the numeric structure is preserved as markdown rather than
  flattened into an unsearchable blob of numbers.
- Producing content that the chunker can split on ~800-token windows (100-token overlap)
  while keeping each table whole in a single chunk so it is never cut in half.

The design intent here is to make **numerical and structured knowledge searchable**, which
directly reduces the manual data-entry burden that a KMS would otherwise push onto an admin.

### 1.2 Query Intent Classification (query path)

Every incoming End_User question is classified by Qwen-Max into one of the configured
Intent_Spaces (HR, Legal, Finance, or others the Admin defines), with a **confidence score
(0–100)**. Two deliberate design choices shape this:

- **Keyword-guided prompting**: the Admin's per-space keywords and descriptions are injected
  into the classification prompt, so classification improves as the Admin curates spaces —
  closing the "improve the bot" feedback loop rather than baking behavior into code.
- **Confidence + General fallback**: routing is a pure function — if confidence is at or
  above the configurable threshold (default 70) and the model returned a valid space, the
  query is scoped to that space; otherwise it falls back to the **General_Space**. On any AI
  error or timeout the classification returns confidence 0, which safely routes to General.
  This keeps a low-confidence or failed classification from silently sending a user to the
  wrong knowledge base.

### 1.3 RAG Response Generation with Citations (query path)

After the query is scoped and embedded, FAISS returns the top-5 passages from the correct
Intent_Space's index. Qwen-Max then generates the answer under a strict
**"answer ONLY from the passages below"** instruction. The generator:

- Grounds the answer strictly in retrieved passages to avoid hallucination.
- Attaches **citations** — the unique set of source document names for the passages actually
  used — so every answer is traceable back to an admin-managed document.
- Emits a clean **no-match** response (rather than an invented answer) when no passage clears
  the minimum-similarity floor.

### 1.4 Per-Frontend Formatting

The same generated response is adapted to each Frontend_Tool's native format constraints
before delivery. This is described in more detail as scenario (2) below.

---

## 2. The Two Highlighted AI Usage Scenarios

The source brief asked for two concrete scenarios that show AI doing meaningful,
product-differentiating work. These are the two we chose to highlight.

### Scenario 1 — Document Parsing: making structured/tabular knowledge searchable

**Problem.** Organizational knowledge frequently lives inside tables embedded in PDFs and
spreadsheets — salary bands, benefit tiers, fee schedules. A naive text extractor turns a
salary grid into a scrambled run of numbers with no row/column meaning, so a question like
"what is the band-4 midpoint salary?" cannot be answered from the retrieved text.

**How AI is used.** After deterministic extraction pulls the table cells out of the PDF,
Qwen-Max reconstructs the table as a markdown grid and normalizes surrounding prose. The
structured markdown is kept as a single chunk and embedded, so the numeric structure is
retrievable and the RAG step can read straight down a column.

**Impact.** Numerical and structured knowledge becomes searchable through the same natural
language interface as free text, and the admin is spared the manual data entry of
re-keying tables into a structured store. This is the clearest example of AI reducing
human effort in the product.

### Scenario 2 — Frontend Integration: adapting responses to each tool's format

**Problem.** Telegram and Microsoft Teams have different, incompatible presentation
constraints. A single generated answer cannot be sent verbatim to both without either
breaking a hard limit or looking unformatted.

**How AI is used.** The generation and formatting layers adapt each response to the
originating tool's native constraints:

- **Telegram**: responses are held under the 4096-character hard limit, with truncation
  applied to the answer body only so that the `Sources:` citation footer always survives.
- **Teams**: responses are emitted as Teams-friendly markdown (bullet lists, bold citations)
  within the Bot Framework's formatting limits.

**Impact.** The same grounded, cited answer arrives correctly rendered on each platform,
so the AI's usefulness is not lost at the last mile of delivery.

---

## 3. How AI Accelerated Development and Iteration

Beyond running inside the product, using AI strategically during the build shortened
several development loops that would otherwise have been slow and manual.

- **Streamlining multi-format parsing.** Rather than hand-writing bespoke cleanup logic for
  every quirk of every file format (PDF reading-order, DOCX table layout, XLSX sheets),
  offloading structuring to the AI let one prompt handle normalization across formats. The
  loaders stay thin and deterministic; the AI absorbs the messy variability. That kept the
  ingestion module small enough for a one-person MVP workload.

- **Tuning classification thresholds and prompts.** The classification behavior was iterated
  by adjusting the prompt (how spaces and keywords are presented, JSON output shape) and the
  confidence threshold, then observing routing outcomes — rather than rewriting classifier
  code each time. Because the threshold is read from settings per query and keywords are
  admin-editable, most tuning happens through configuration, not redeployment.

- **Rapid prompt iteration for RAG grounding.** The grounding instruction and citation
  behavior were refined by iterating on the RAG prompt wording and passage budget (5 × ~500
  tokens, `max_tokens ≈ 500`), which is far faster than restructuring retrieval code.

- **Scaffolding and repetitive code.** AI assistance accelerated the repetitive,
  boilerplate-heavy parts of the build — the five Streamlit admin screens, per-format
  loaders behind a common interface, REST endpoint stubs, and the test scaffolding — freeing
  attention for the parts that actually needed judgment (latency budgeting, the
  proxy/long-polling decision, the embeddings-as-source-of-truth data model).

---

## 4. Adjustments Made to AI Outputs

AI output was never accepted blindly. The following adjustments were deliberate and are
reflected in the design.

- **Refining parsed content.** AI structuring output was reviewed and constrained so tables
  render as valid markdown and are never split across chunks. The deterministic loaders run
  first so the AI is normalizing real extracted content rather than inventing it, and the
  document status pipeline (Pending → Processed / Error) surfaces parse failures to the Admin
  instead of storing garbage silently.

- **Calibrating confidence thresholds.** The default confidence threshold (70) was chosen as
  a starting point and made runtime-configurable so it can be recalibrated against observed
  misclassifications. The Analytics screen lets the Admin verify or correct a query's
  detected space, which produces the accuracy signal used to decide whether the threshold or
  keywords need adjustment.

- **Grounding RAG strictly in retrieved passages.** The RAG prompt is explicitly constrained
  to answer only from the supplied passages, and citations are computed from the passages
  actually used. When retrieval returns nothing above the similarity floor, the system
  returns an honest no-match message rather than letting the model improvise. This is the
  primary guard against hallucination.

- **Handling AI failure explicitly.** Every AI call has an explicit timeout (classification
  5s, embedding 5s, RAG 10s) and a defined failure behavior — classification failure routes
  to General with confidence 0; generation failure returns a `failed` status rather than a
  fabricated answer. The AI is treated as a fallible external dependency, not an oracle.

---

## 5. Strategic Usage Summary

The strategy was to concentrate AI where it removes genuine effort or brittleness —
turning messy documents into searchable structured knowledge, routing questions to the
right knowledge base with a graceful fallback, and producing cited answers grounded in
real content — while wrapping every AI interaction in deterministic guards: format
validation before parsing, thresholded routing, strict grounding, per-tool formatting, and
explicit timeouts and failure paths. During the build, AI accelerated the repetitive and
multi-format-heavy work and enabled fast prompt/threshold iteration, while human judgment
stayed on the architecture, latency budget, and correctness decisions. The result is a
system where AI does the heavy lifting it is good at, and the surrounding engineering keeps
its output trustworthy.
