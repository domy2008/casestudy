# IntelliKnow KMS — Low-Level Design (LLD)

This document describes component-level interfaces and data models. Signatures
below match the implemented code under `app/`.

## 1. Core Data Models (`app/core/models.py`)

Behavior-free dataclasses that flow between components.

```python
@dataclass
class QueryContext:
    query_id: str            # uuid
    tool: str                # "telegram" | "teams"
    conversation_ref: dict   # tool-specific reply address
    text: str                # validated, 1..4000 chars
    received_at: datetime

@dataclass
class Classification:
    space_id: int            # assigned Intent_Space after threshold routing
    raw_space_id: int | None # what the model proposed (None on AI failure)
    confidence: float        # 0.0..100.0 (0.0 on AI failure)

@dataclass
class Passage:
    chunk_id: int
    document_id: int
    document_name: str
    text: str
    similarity: float        # cosine, 0..1

@dataclass
class GeneratedResponse:
    text: str
    citations: list[str] = field(default_factory=list)  # unique source doc names
    status: str = "success"                             # "success" | "no_match" | "failed"

@dataclass
class ConnectivityResult:
    tool: str
    ok: bool
    detail: str = ""
    timed_out: bool = False
    checked_at: datetime | None = None

@dataclass
class QueryLogEntry:
    ts: datetime
    query_text: str
    detected_space_id: int
    confidence: float
    response_status: str     # "Success" | "Failed"
    tool: str
    latency_ms: int | None = None
    verified_space_id: int | None = None
    id: int | None = None

@dataclass
class FieldError:
    field: str
    message: str

@dataclass
class ExtractedContent:
    text: str
    tables: list[list[list[str]]] = field(default_factory=list)
```

## 2. SQLite Schema (`app/db.py`, `/data/app.db`, WAL mode)

Nine tables. All statements use `IF NOT EXISTS`, so bootstrap is idempotent.

```sql
CREATE TABLE intent_spaces (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL COLLATE NOCASE UNIQUE,  -- case-insensitive uniqueness
    description   TEXT NOT NULL DEFAULT '',             -- <=500 chars (app-enforced)
    is_general    INTEGER NOT NULL DEFAULT 0,           -- General_Space undeletable
    is_default    INTEGER NOT NULL DEFAULT 0,           -- HR / Legal / Finance seeds
    created_at    TEXT NOT NULL
);

CREATE TABLE space_keywords (
    id            INTEGER PRIMARY KEY,
    space_id      INTEGER NOT NULL REFERENCES intent_spaces(id) ON DELETE CASCADE,
    keyword       TEXT NOT NULL                          -- 1..50 chars, <=50 per space
);

CREATE TABLE documents (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    format        TEXT NOT NULL,                         -- pdf|docx|xlsx|txt|md
    size_bytes    INTEGER NOT NULL,                      -- <= 52428800
    status        TEXT NOT NULL DEFAULT 'Pending',       -- Pending|Processed|Error
    space_id      INTEGER NOT NULL REFERENCES intent_spaces(id),
    file_path     TEXT NOT NULL,                         -- /data/uploads/{uuid}.{ext}
    error_message TEXT,
    uploaded_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE chunks (
    id            INTEGER PRIMARY KEY,                   -- doubles as FAISS vector id
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    text          TEXT NOT NULL,
    embedding     BLOB NOT NULL                          -- float32[dim]; source of truth
);

CREATE TABLE query_log (
    id                INTEGER PRIMARY KEY,
    ts                TEXT NOT NULL,
    query_text        TEXT NOT NULL,
    detected_space_id INTEGER NOT NULL REFERENCES intent_spaces(id),
    confidence        REAL NOT NULL,                     -- 0..100 (0 on AI failure)
    response_status   TEXT NOT NULL,                     -- Success|Failed
    latency_ms        INTEGER,
    tool              TEXT NOT NULL,                     -- telegram|teams
    verified_space_id INTEGER REFERENCES intent_spaces(id)
);

CREATE TABLE document_access (                            -- most-accessed docs
    id            INTEGER PRIMARY KEY,
    query_log_id  INTEGER NOT NULL REFERENCES query_log(id),
    document_id   INTEGER NOT NULL,
    ts            TEXT NOT NULL
);

CREATE TABLE integration_error_log (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    tool          TEXT NOT NULL,
    operation     TEXT NOT NULL,
    error_detail  TEXT NOT NULL
);

CREATE TABLE integrations (
    tool          TEXT PRIMARY KEY,                      -- telegram|teams
    status        TEXT NOT NULL DEFAULT 'Disconnected',  -- Connected|Error|Disconnected
    active        INTEGER NOT NULL DEFAULT 0,
    last_check_ts TEXT
);

CREATE TABLE settings (
    key           TEXT PRIMARY KEY,                      -- confidence_threshold (default '70'),
    value         TEXT NOT NULL                          -- latency_alarm_ms, error_rate_alarm_pct
);

CREATE INDEX idx_chunks_doc     ON chunks(document_id);
CREATE INDEX idx_qlog_ts        ON query_log(ts);
CREATE INDEX idx_qlog_space     ON query_log(detected_space_id);
CREATE INDEX idx_access_doc     ON document_access(document_id, ts);
CREATE INDEX idx_errlog_tool_ts ON integration_error_log(tool, ts);
```

**Seed data at first startup:** `General` (is_general=1), `HR`, `Legal`,
`Finance` (is_default=1); `confidence_threshold=70`, `latency_alarm_ms=3000`,
`error_rate_alarm_pct=5`. Seeding uses `INSERT OR IGNORE`, so it never
duplicates rows or overwrites Admin-modified values.

Connections are opened with `PRAGMA journal_mode = WAL` and
`PRAGMA foreign_keys = ON` (`app.db.connect`). `app.db.bootstrap()` is the
single startup entry point (ensure directories → create schema → seed).

## 3. FAISS Per-Space Index Layout (`app/kb/search.py`)

- One file per Intent_Space: `{settings.faiss_dir}/space_{space_id}.index`.
- Type `faiss.IndexIDMap2(faiss.IndexFlatIP(dim))`. Vectors are L2-normalized
  before add/search, so inner product equals cosine similarity.
- The FAISS vector id is the `chunks.id` value, giving a direct join back to
  SQLite.
- Only chunks of `Processed` documents are indexed; any stale vector id that no
  longer maps to a Processed chunk of the space is skipped at search time.
- Retrieval drops hits below `MIN_SIMILARITY` (default `0.30`), which drives the
  no-match path.
- Writes are atomic: build to a `.tmp` file then `os.replace`.
- An empty or missing index file yields an empty result set.

The embeddings in SQLite are the source of truth; index files are derived and
rebuilt from SQLite on delete/reassignment (`rebuild_space`).

## 4. Module / File Map under `app/`

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI assembly, startup/shutdown, background tasks |
| `app/config.py` | `Settings` + persistent-storage paths (`db_path`, `faiss_dir`, `uploads_dir`, `credentials_path`, `logbuffer_dir`) |
| `app/db.py` | Schema bootstrap, WAL connection, seed defaults |
| `app/core/models.py` | Domain dataclasses (§1) |
| `app/core/orchestrator.py` | `Orchestrator` |
| `app/bots/base.py` | `FrontendAdapter` protocol, `evaluate_inbound`, `GateDecision` |
| `app/bots/telegram.py` | `TelegramAdapter`, `format_telegram_message`, `build_sources_footer` |
| `app/bots/teams.py` | `TeamsAdapter` |
| `app/bots/dispatcher.py` | `QueryDispatcher` |
| `app/bots/monitor.py` | Background status monitor |
| `app/kb/loaders.py` | Per-format loaders, `load_document`, `ExtractedContent` |
| `app/kb/processor.py` | `DocumentProcessor`, `chunk_structured_text` |
| `app/kb/store.py` | Repositories (§5) + `embedding_to_blob` / `blob_to_embedding` |
| `app/kb/search.py` | `SearchIndex`, `MIN_SIMILARITY` |
| `app/ai/dashscope_client.py` | `DashScopeClient`, timeouts, retries |
| `app/ai/prompts.py` | `build_classification_messages`, `build_structuring_messages`, `build_rag_messages`, `IntentSpaceSpec`, `NO_MATCH_MESSAGE` |
| `app/rag/generator.py` | `ResponseGenerator`, `COULD_NOT_PROCESS_MESSAGE` |
| `app/security/credentials.py` | `CredentialStore`, `validate_credentials`, `mask_value`, `CREDENTIAL_SCHEMAS` |
| `app/analytics/service.py` | `AnalyticsService`, `Filters`, `ExportError`, `parse_exported_query_log` |
| `app/monitoring/publisher.py` | `CloudWatchPublisher`, `MetricSnapshot`, `MetricsSource` |
| `app/api/admin.py` | Admin REST endpoints |

## 5. Key Class / Function Signatures

### Orchestrator (`app/core/orchestrator.py`)

```python
SEARCH_TOP_K = 5
DEFAULT_CONFIDENCE_THRESHOLD = 70.0

class Orchestrator:
    def __init__(self, *, conn=None, ai_client=None, search_index=None,
                 generator=None, analytics=None, general_space_id: int | None = None,
                 top_k: int = SEARCH_TOP_K) -> None: ...

    async def handle_query(self, ctx: QueryContext) -> GeneratedResponse:
        """classify + embed concurrently → route → search(k=5) → generate →
        write exactly one Query_Log entry + document_access rows."""

    async def classify(self, text: str) -> Classification:
        """Qwen-Max JSON-mode prompt over ALL Intent_Spaces (names, descriptions,
        keywords). On AI error/timeout/malformed/unknown id → raw_space_id=None,
        confidence=0.0."""

    def route(self, classification: Classification, threshold: float) -> int:
        """Pure. Returns raw_space_id iff it is not None and confidence >=
        threshold; else the General_Space id."""
```

Constructed routing-only (`Orchestrator(general_space_id=1)`) or as the full
pipeline with all seams injected. The threshold is read from `settings` per
query via `SettingsRepository`.

### DocumentProcessor (`app/kb/processor.py`)

```python
PARSE_DEADLINE_S = 600.0      # 10-minute parse/structure/chunk deadline
CHUNK_TARGET_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100
EMBED_BATCH_SIZE = 16

class DocumentProcessor:
    def __init__(self, conn, client: ChatEmbedClient, *, settings=None,
                 search_index: SearchIndex | None = None,
                 parse_deadline_s: float = PARSE_DEADLINE_S,
                 target_tokens: int = CHUNK_TARGET_TOKENS,
                 overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
                 embed_batch_size: int = EMBED_BATCH_SIZE) -> None: ...

    def schedule(self, document_id: int) -> asyncio.Task[None]: ...

    async def process(self, document_id: int) -> None:
        """Pending → loader extract → AI structure → chunk → batched embed →
        persist + Processed + add to space index. Enforces the 10-min deadline;
        embed-only failure leaves Status Pending for Admin retry."""

def chunk_structured_text(structured: str, *, target_tokens=800,
                          overlap_tokens=100) -> list[str]:
    """~target_tokens windows with overlap; markdown tables kept whole."""
```

### SearchIndex (`app/kb/search.py`)

```python
MIN_SIMILARITY = 0.30

class SearchIndex:
    def __init__(self, conn, settings=None, *, min_similarity=MIN_SIMILARITY) -> None: ...
    def index_path(self, space_id: int) -> Path: ...
    def search(self, space_id: int, vector: np.ndarray, k: int) -> list[Passage]:
        """Top-k by cosine similarity from that space's index only; Processed
        chunks only; hits < min_similarity dropped; empty/missing index → []."""
    def add_document(self, document_id: int) -> None:
        """Add a Processed document's chunk vectors to its space index."""
    def rebuild_space(self, space_id: int) -> None:
        """Atomically rebuild the space index from SQLite; remove file if empty."""
```

### ResponseGenerator (`app/rag/generator.py`)

```python
COULD_NOT_PROCESS_MESSAGE = "Sorry, I couldn't process your request right now. ..."

class ResponseGenerator:
    def __init__(self, client: RagChatClient, *,
                 access_recorder: Callable[[list[int]], None] | None = None) -> None: ...

    async def generate(self, query: str, passages: Sequence[Passage]) -> GeneratedResponse:
        """Empty passages → no_match (no AI call). Else Qwen-Max RAG prompt;
        citations = unique document_name of used passages. Any AI/timeout
        failure → status 'failed'."""
```

### CredentialStore (`app/security/credentials.py`)

```python
CREDENTIAL_SCHEMAS = {
    "telegram": {"bot_token": r"^\d+:[A-Za-z0-9_-]{30,}$"},
    "teams":    {"app_id": r"^[0-9a-fA-F-]{36}$", "app_password": r"^\S{8,}$"},
    "dashscope":{"api_key": r"^sk-[A-Za-z0-9]{16,}$"},
}

def validate_credentials(integration: str, fields: dict) -> list[FieldError]:
    """Pure. One FieldError per missing/empty/format-invalid required field;
    empty list means valid. No I/O, so it runs before any write."""

def mask_value(value: str) -> str:
    """Reveal at most the last 4 chars; strings <=4 chars fully masked."""

class CredentialStore:
    def __init__(self, settings=None) -> None: ...      # requires CREDENTIAL_MASTER_KEY
    def save(self, integration: str, fields: dict[str, str]) -> None:
        """Validate first; on success re-encrypt whole doc + atomic os.replace."""
    def load(self, integration: str) -> dict[str, str] | None: ...
    def masked(self, integration: str) -> dict[str, str]: ...
```

Storage: single Fernet-encrypted JSON at `/data/credentials/credentials.enc`;
atomic writes via temp file + `os.replace`.

### AnalyticsService (`app/analytics/service.py`)

```python
class AnalyticsService:
    def __init__(self, conn: sqlite3.Connection) -> None: ...
    def log_query(self, entry: QueryLogEntry) -> int | None:      # never raises
    def history(self, start=None, end=None, space_ids=None, tool=None,
                limit: int = 50) -> list[QueryLogEntry]: ...
    def error_history(self, tool=None, limit: int = 50) -> list[dict]: ...
    def top_documents(self, start=None, end=None, n: int = 10) -> list[tuple[str, int]]: ...
    def top_spaces(self, start=None, end=None, n: int = 10) -> list[tuple[str, int]]: ...
    def accuracy_by_space(self) -> dict[int, float | None]: ...   # None = N/A
    def verify_query(self, query_log_id: int, verified_space_id: int) -> None: ...
    def export_csv(self, filters: Filters) -> bytes:              # ExportError on failure
```

### DashScopeClient (`app/ai/dashscope_client.py`)

```python
CHAT_MODEL = "qwen-max"
EMBEDDING_MODEL = "text-embedding-v3"
CLASSIFICATION_TIMEOUT_S = 5.0
EMBEDDING_TIMEOUT_S = 5.0
RAG_TIMEOUT_S = 10.0

class DashScopeClient:
    def __init__(self, *, settings=None, credential_store=None, http_client=None,
                 base_url=DEFAULT_BASE_URL, max_retries=2, backoff_base_s=0.5) -> None: ...
    async def chat_completion(self, messages, *, model=CHAT_MODEL, timeout=RAG_TIMEOUT_S,
                              json_mode=False, max_tokens=None, temperature=None, **extra) -> str: ...
    async def classify(self, messages, *, timeout=CLASSIFICATION_TIMEOUT_S,
                        json_mode=True, max_tokens=None, **extra) -> str: ...
    async def generate(self, messages, *, timeout=RAG_TIMEOUT_S, json_mode=False,
                       max_tokens=None, **extra) -> str: ...
    async def embed(self, texts, *, model=EMBEDDING_MODEL,
                    timeout=EMBEDDING_TIMEOUT_S) -> list[np.ndarray]: ...
```

API key resolution: Credential_Store first (`dashscope` → `api_key`), else
`settings.dashscope_api_key`; the key is never logged or placed in an exception.
Transient failures (connection errors, timeouts, 429/5xx) are retried with
exponential backoff.

### SQLite Repositories (`app/kb/store.py`)

- `DocumentRepository` — `create`, `get`, `list(name/format/space_id/date filters)`,
  `set_status`, `set_space`/`update_space`, `reassign_space_documents`, `delete`.
- `ChunkRepository` — `insert`, `insert_many`, `fetch_by_document`,
  `fetch_for_space(space_id, processed_only=True)`.
- `IntentSpaceRepository` — `create`, `get`, `get_by_name` (NOCASE),
  `get_general`, `list`, `update`, `delete`, `get_keywords`, `set_keywords`.
- `SettingsRepository` — `get(key, default)`, `set(key, value)` (upsert).
- `IntegrationRepository` — `get`, `list`, `set_status`, `set_active`.
- `QueryLogRepository` — `insert`, `get`, `list(filters)`, `set_verified_space_id`.
- `DocumentAccessRepository` — `insert`, `counts`.
- `IntegrationErrorLogRepository` — `insert`, `list_recent`.

Helpers `embedding_to_blob` / `blob_to_embedding` serialize embeddings as
little-endian `float32` bytes for the `chunks.embedding` column.

### Frontend Integration (`app/bots/`)

```python
# base.py
MIN_QUERY_LENGTH = 1
MAX_QUERY_LENGTH = 4000

def evaluate_inbound(text: str | None) -> GateDecision:
    """Forward iff text length is 1..4000; else a rejection decision."""

class FrontendAdapter(Protocol):
    tool_name: str
    async def send(self, conversation_ref: dict, text: str) -> None: ...
    def format(self, response: GeneratedResponse) -> str: ...
    async def check_connectivity(self) -> ConnectivityResult: ...

# telegram.py
def format_telegram_message(response: GeneratedResponse) -> str: ...  # <=4096, citations preserved
class TelegramAdapter:  # run_polling, send, format, check_connectivity, proxy retries

# dispatcher.py
class QueryDispatcher:
    async def dispatch(self, ctx: QueryContext) -> None:
        """Owns the deadline, delivery retries (x2), and error logging."""
```

### CloudWatchPublisher (`app/monitoring/publisher.py`)

```python
class CloudWatchPublisher:
    def build_metric_data(self, snapshot: MetricSnapshot) -> list[dict]: ...
    def publish_cycle(self, source: MetricsSource) -> bool:
        """put_metric_data; on failure buffer to /data/logbuffer and re-send later."""
    async def run_forever(self, source: MetricsSource) -> None: ...
```

Metrics: `QueryLatencyMs`, `ErrorRatePercent`, per-tool `IntegrationHealthy{tool}`.

## 6. Correctness Properties Summary

The design defines 25 correctness properties (one Hypothesis test each, ≥100
examples). They map to these components:

| # | Property | Component |
|---|---|---|
| 1 | Credential validation gates storage | `credentials.validate_credentials` |
| 2 | Store holds the last valid save | `CredentialStore` |
| 3 | Message gate forwards exactly the valid messages | `bots.base.evaluate_inbound` |
| 4 | Logging failures never alter the response | `Orchestrator` / `AnalyticsService.log_query` |
| 5 | Log listings respect filter, order, limit | `AnalyticsService.history` / `error_history` |
| 6 | Upload validation accepts exactly the supported envelope | upload path / `DocumentRepository` |
| 7 | Document list filtering returns exactly the matches | `DocumentRepository.list` |
| 8 | Deletion removes every trace of a document | `DocumentRepository.delete` + `SearchIndex.rebuild_space` |
| 9 | Semantic search invariant | `SearchIndex.search` |
| 10 | Intent_Space configuration validation | Intent_Space create/update |
| 11 | Space name uniqueness is case-insensitive | `intent_spaces.name COLLATE NOCASE` |
| 12 | Space deletion reassigns all documents to General | `reassign_space_documents` |
| 13 | Classification accuracy computation | `AnalyticsService.accuracy_by_space` |
| 14 | Threshold routing is total and correct | `Orchestrator.route` |
| 15 | Threshold configuration validation | settings threshold update |
| 16 | Keywords appear in the classification context | `prompts.build_classification_messages` |
| 17 | Exactly one complete Query_Log entry per query | `Orchestrator._log_query` |
| 18 | Citations name exactly the source documents | `ResponseGenerator._unique_document_names` |
| 19 | Empty retrieval yields a no-match success | `ResponseGenerator.generate` |
| 20 | Telegram formatting respects length + preserves citations | `format_telegram_message` |
| 21 | Any generation-path failure → error message + Failed log | `ResponseGenerator` / `Orchestrator` |
| 22 | Usage metrics equal the reference computation | `top_documents` / `top_spaces` |
| 23 | Export round trip preserves the filtered history | `export_csv` / `parse_exported_query_log` |
| 24 | Masking reveals at most the last four characters | `credentials.mask_value` |
| 25 | Loaded credential values never appear in log output | `RedactingFilter` |
