"""Core domain data models shared across the IntelliKnow KMS backend.

This module defines the plain-data structures that flow between the frontend
adapters, the orchestrator, the knowledge base, the response generator, the
security layer, and the analytics module. They are intentionally behavior-free
dataclasses so they can be constructed, compared, and serialized freely and so
they act as stable seams between components (see the design's "Core Data
Types" and "Components and Interfaces" sections).

Field definitions for :class:`QueryContext`, :class:`Classification`,
:class:`Passage`, and :class:`GeneratedResponse` match the design document
exactly. The remaining types (:class:`ConnectivityResult`,
:class:`QueryLogEntry`, :class:`FieldError`, :class:`ExtractedContent`) are
designed to be consistent with how the component interfaces consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "QueryContext",
    "Classification",
    "Passage",
    "GeneratedResponse",
    "ConnectivityResult",
    "QueryLogEntry",
    "FieldError",
    "ExtractedContent",
]


@dataclass
class QueryContext:
    """An inbound End_User query normalized for the orchestrator.

    Produced by a frontend adapter once a message has passed the inbound
    validation gate (1-4,000 characters of text) and handed to the
    Orchestrator to classify, route, retrieve, and answer.

    Attributes:
        query_id: Unique identifier for this query (a UUID string).
        tool: Originating Frontend_Tool, either ``"telegram"`` or ``"teams"``.
        conversation_ref: Tool-specific reply address used to deliver the
            response (e.g. a Telegram ``chat_id`` or a Teams conversation
            reference).
        text: The validated user message text (1-4,000 characters).
        received_at: Timestamp when the message was received by the adapter.
    """

    query_id: str
    tool: str
    conversation_ref: dict
    text: str
    received_at: datetime


@dataclass
class Classification:
    """The outcome of intent classification and threshold routing.

    Returned by the Orchestrator's ``classify`` step and refined by ``route``.
    ``space_id`` is the Intent_Space the query is ultimately routed to after
    applying the Confidence_Threshold, while ``raw_space_id`` records what the
    AI model actually proposed before routing.

    Attributes:
        space_id: The assigned Intent_Space id after threshold routing. On a
            low-confidence or failed classification this is the General_Space
            id.
        raw_space_id: The Intent_Space id proposed by the model, or ``None``
            when the AI call errored or timed out.
        confidence: Model confidence on a 0.0-100.0 scale. Set to ``0.0`` when
            the AI call errored or timed out.
    """

    space_id: int
    raw_space_id: int | None
    confidence: float


@dataclass
class Passage:
    """A single retrieved chunk of a document with its similarity score.

    Returned by the FAISS-backed semantic search for a given Intent_Space and
    consumed by the Response Generator to ground its answer and build
    citations.

    Attributes:
        chunk_id: Identifier of the source chunk (also the FAISS vector id).
        document_id: Identifier of the document the chunk belongs to.
        document_name: Human-readable name of the source document, used for
            citations.
        text: The chunk text used as grounding context for generation.
        similarity: Cosine similarity to the query vector, in the range 0..1.
    """

    chunk_id: int
    document_id: int
    document_name: str
    text: str
    similarity: float


@dataclass
class GeneratedResponse:
    """The final answer produced by the Response Generator.

    Carries the answer text, the citations for any source documents used, and
    a status describing the generation outcome. This is what a frontend adapter
    formats and delivers back to the End_User.

    Attributes:
        text: The answer text to deliver to the End_User.
        citations: Unique source document names referenced in the answer.
            Empty when there are no cited documents.
        status: Generation outcome, one of ``"success"``, ``"no_match"``, or
            ``"failed"``.
        query_log_id: The Query_Log row id for this query, set by the
            Orchestrator after logging so delivery can attach feedback
            controls (👍/👎) that reference it. ``None`` when logging failed
            or has not happened.
    """

    text: str
    citations: list[str] = field(default_factory=list)
    status: str = "success"
    query_log_id: int | None = None


@dataclass
class ConnectivityResult:
    """The result of an end-to-end connectivity check for an integration.

    Produced by a frontend adapter's ``check_connectivity`` (Req 3.2) and by
    the background status monitor. Used to update the stored integration status
    (Connected/Error/Disconnected) and to surface test results to the Admin.

    Attributes:
        tool: The Frontend_Tool checked, e.g. ``"telegram"`` or ``"teams"``.
        ok: ``True`` when the check succeeded end to end, otherwise ``False``.
        detail: Human-readable description of the outcome (success detail or
            the failure/error reason).
        timed_out: ``True`` when the check exceeded its time cap (e.g. the 30s
            connectivity-test deadline) and was terminated.
        checked_at: Timestamp when the check completed.
    """

    tool: str
    ok: bool
    detail: str = ""
    timed_out: bool = False
    checked_at: datetime | None = None


@dataclass
class QueryLogEntry:
    """A single Query_Log record describing one processed query.

    Written exactly once per query by the Orchestrator/Analytics module and
    read back by the analytics history, metrics, and export features. Mirrors
    the ``query_log`` table in the design schema.

    Attributes:
        ts: Timestamp when the query was processed.
        query_text: The End_User query text.
        detected_space_id: Intent_Space the query was routed to.
        confidence: Classification confidence, 0..100 (``0.0`` on AI failure).
        response_status: Delivered response status, e.g. ``"Success"`` or
            ``"Failed"``.
        tool: Originating Frontend_Tool (``"telegram"`` or ``"teams"``).
        latency_ms: End-to-end processing latency in milliseconds, or ``None``
            if not recorded.
        verified_space_id: Admin-verified correct Intent_Space id used for
            accuracy computation, or ``None`` when the query has not been
            verified.
        id: Database identifier, or ``None`` before the entry is persisted.
    """

    ts: datetime
    query_text: str
    detected_space_id: int
    confidence: float
    response_status: str
    tool: str
    latency_ms: int | None = None
    verified_space_id: int | None = None
    id: int | None = None


@dataclass
class FieldError:
    """A single validation error for one credential/input field.

    Returned (one per offending field) by ``validate_credentials`` and other
    validation routines so the UI can show per-field messages. An empty list of
    ``FieldError`` means the submission is valid (Req 1.4/1.6).

    Attributes:
        field: Name of the field that failed validation.
        message: Human-readable reason the field is invalid (e.g. missing,
            empty, or format-invalid).
    """

    field: str
    message: str


@dataclass
class ExtractedContent:
    """Raw content extracted from an uploaded document by a format loader.

    Produced by the per-format loaders (PDF, DOCX, XLSX, TXT, Markdown) and
    consumed by the document processor's AI-structuring step. Tables are kept
    separate from body text so their row/column structure is preserved before
    being rendered as markdown and chunked (Req 5.2).

    Attributes:
        text: The extracted body text of the document.
        tables: Extracted tables, each represented as a list of rows where each
            row is a list of cell strings. Empty when the document has no
            tables.
    """

    text: str
    tables: list[list[list[str]]] = field(default_factory=list)
