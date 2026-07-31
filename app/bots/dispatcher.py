"""Query dispatcher: bind an inbound query to a delivered response.

The :class:`QueryDispatcher` is the shared piece of the Frontend_Integration
Module that ties a validated inbound query to a response delivered back to the
originating conversation. It sits between an adapter's intake (which produces a
:class:`~app.core.models.QueryContext`) and that same adapter's ``send``. It
owns four responsibilities from the design's "Frontend Integration Module →
Shared dispatch" section:

1. **A 30-second processing deadline** around the Orchestrator's
   ``handle_query``. If processing does not finish within
   :data:`QUERY_DEADLINE_SECONDS`, the pending work is cancelled and a
   could-not-process message is delivered to the originating conversation
   (Req 2.5).

2. **Delivery with retries.** The formatted response (or the could-not-process
   message) is delivered via ``adapter.send``; a failed delivery is retried up
   to :data:`DELIVERY_MAX_RETRIES` *additional* times (three total attempts)
   (Req 2.6).

3. **Error logging on final failure.** When delivery still fails after every
   attempt, an ``integration_error_log`` entry is recorded carrying the
   timestamp, Frontend_Tool identifier, and failure reason (Req 2.7).

4. **Swallow-and-continue logging guards.** Any failure of the error-log writer
   itself is caught and swallowed so it can never interrupt query processing
   (Req 2.8).

Every collaborator is injected as a seam so the dispatcher can be exercised
without a network or a database:

* ``orchestrator`` — anything exposing ``async handle_query(ctx) -> response``
  (see :class:`app.core.orchestrator.Orchestrator`).
* ``adapter`` — a :class:`app.bots.base.FrontendAdapter` (uses ``format`` and
  ``send``).
* ``error_log`` — anything exposing
  ``insert(tool, operation, error_detail)`` (see
  :class:`app.kb.store.IntegrationErrorLogRepository`); optional.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

from app.bots.base import FrontendAdapter
from app.core.models import GeneratedResponse, QueryContext

__all__ = [
    "QUERY_DEADLINE_SECONDS",
    "DELIVERY_MAX_RETRIES",
    "COULD_NOT_PROCESS_MESSAGE",
    "SEND_OPERATION",
    "OrchestratorSeam",
    "ErrorLogSeam",
    "QueryDispatcher",
]

logger = logging.getLogger(__name__)

# Hard deadline for producing a response to a forwarded query (Req 2.5). If the
# Orchestrator does not return within this window the query is abandoned and the
# End_User receives the could-not-process message.
QUERY_DEADLINE_SECONDS = 30

# Additional delivery attempts after the first one fails (Req 2.6: "up to 2
# additional attempts" → three attempts in total).
DELIVERY_MAX_RETRIES = 2

# Delivered to the originating conversation when a query cannot be processed —
# either because the 30s deadline elapsed or because the pipeline failed
# outright (Req 2.5).
COULD_NOT_PROCESS_MESSAGE = (
    "Sorry, your query could not be processed right now. Please try again later."
)

# Operation label recorded in the integration error log for a failed delivery
# (Req 2.7).
SEND_OPERATION = "send"


@runtime_checkable
class OrchestratorSeam(Protocol):
    """Minimal orchestrator seam consumed by the dispatcher.

    Matches :meth:`app.core.orchestrator.Orchestrator.handle_query`.
    """

    async def handle_query(self, ctx: QueryContext) -> GeneratedResponse:
        """Produce the response for a validated inbound query context."""
        ...


@runtime_checkable
class ErrorLogSeam(Protocol):
    """Minimal error-log seam consumed by the dispatcher (Req 2.7).

    Matches :meth:`app.kb.store.IntegrationErrorLogRepository.insert`.
    """

    def insert(self, tool: str, operation: str, error_detail: str) -> Any:
        """Record one integration error entry (ts, tool, operation, reason)."""
        ...


class QueryDispatcher:
    """Run one query end to end: process under a deadline, then deliver.

    Args:
        orchestrator: The query pipeline (:class:`OrchestratorSeam`); its
            ``handle_query`` is run under the processing deadline.
        adapter: The Frontend_Tool adapter used to ``format`` the response and
            ``send`` it to the originating conversation.
        error_log: Optional integration error-log sink. When present, a delivery
            that fails after every retry is recorded with the timestamp, tool,
            and failure reason (Req 2.7). When ``None``, the failure is only
            logged to the module logger.
        deadline_s: Processing deadline in seconds (Req 2.5; default
            :data:`QUERY_DEADLINE_SECONDS`).
        max_retries: Additional delivery attempts after the first failure
            (Req 2.6; default :data:`DELIVERY_MAX_RETRIES`).
    """

    def __init__(
        self,
        orchestrator: OrchestratorSeam,
        adapter: FrontendAdapter,
        *,
        error_log: ErrorLogSeam | None = None,
        deadline_s: float = QUERY_DEADLINE_SECONDS,
        max_retries: int = DELIVERY_MAX_RETRIES,
    ) -> None:
        self._orchestrator = orchestrator
        self._adapter = adapter
        self._error_log = error_log
        self._deadline_s = deadline_s
        self._max_retries = max_retries

    async def dispatch(self, ctx: QueryContext) -> None:
        """Process ``ctx`` under the deadline and deliver the result.

        When the adapter supports streaming delivery (``supports_streaming``,
        e.g. Telegram's send-then-edit loop) and the orchestrator exposes
        ``handle_query_stream``, the answer is delivered incrementally so text
        appears within about a second; otherwise the classic
        process-then-deliver path runs. Both paths share the same deadline,
        could-not-process fallback, and never-raise guarantees (Req 2.5/2.8).

        Args:
            ctx: The validated inbound query context to process and answer.
        """
        if getattr(self._adapter, "supports_streaming", False) and hasattr(
            self._orchestrator, "handle_query_stream"
        ):
            await self._dispatch_streaming(ctx)
            return

        response = await self._process(ctx)
        if response is not None:
            text = self._safe_format(response)
        else:
            # Deadline expired or the pipeline failed: tell the End_User the
            # query could not be processed (Req 2.5).
            text = COULD_NOT_PROCESS_MESSAGE
        await self._deliver(ctx, text)

    async def _dispatch_streaming(self, ctx: QueryContext) -> None:
        """Stream the answer to the adapter under the processing deadline.

        Drives ``orchestrator.handle_query_stream`` into the adapter's
        ``send_stream`` (send-then-edit delivery). On deadline expiry or any
        failure the could-not-process message is delivered through the classic
        retried path instead, and nothing ever raises back to the intake loop
        (Req 2.5/2.8). Note that on a mid-stream failure the End_User may see
        a partial message followed by the could-not-process message.

        Args:
            ctx: The validated inbound query context to process and answer.
        """
        try:
            await asyncio.wait_for(
                self._adapter.send_stream(
                    ctx.conversation_ref,
                    self._orchestrator.handle_query_stream(ctx),
                ),
                timeout=self._deadline_s,
            )
            return
        except asyncio.TimeoutError:
            logger.warning(
                "Streaming query %s exceeded the %gs deadline; "
                "delivering could-not-process message",
                ctx.query_id,
                self._deadline_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stream failure => could-not-process
            logger.exception(
                "Streaming query %s failed; delivering could-not-process message",
                ctx.query_id,
            )
        await self._deliver(ctx, COULD_NOT_PROCESS_MESSAGE)

    async def _process(self, ctx: QueryContext) -> GeneratedResponse | None:
        """Run the pipeline under the 30s deadline (Req 2.5).

        Args:
            ctx: The inbound query context.

        Returns:
            The generated response, or ``None`` when the deadline elapsed or the
            pipeline raised — both of which map to the could-not-process
            message.
        """
        try:
            return await asyncio.wait_for(
                self._orchestrator.handle_query(ctx), timeout=self._deadline_s
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Query %s exceeded the %gs processing deadline; "
                "delivering could-not-process message",
                ctx.query_id,
                self._deadline_s,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - pipeline failure => could-not-process
            logger.exception(
                "Query %s processing failed; delivering could-not-process message",
                ctx.query_id,
            )
            return None

    def _safe_format(self, response: GeneratedResponse) -> str:
        """Format ``response`` for the tool, falling back on formatter failure.

        A formatting error must not sink the whole delivery; if it occurs the
        End_User still receives the could-not-process message (Req 2.5).

        Args:
            response: The generated response to format.

        Returns:
            The tool-formatted message text, or
            :data:`COULD_NOT_PROCESS_MESSAGE` if formatting raised.
        """
        try:
            return self._adapter.format(response)
        except Exception:  # noqa: BLE001 - never let formatting break delivery
            logger.exception(
                "Formatting response for %s failed; sending could-not-process",
                self._adapter.tool_name,
            )
            return COULD_NOT_PROCESS_MESSAGE

    async def _deliver(self, ctx: QueryContext, text: str) -> None:
        """Deliver ``text`` with retries, logging a final failure (Req 2.6/2.7).

        The message is sent via ``adapter.send``; a failed send is retried up to
        :attr:`_max_retries` additional times (three attempts total). If every
        attempt fails, the failure is recorded in the integration error log with
        timestamp, tool, and reason (Req 2.7), and the error-log write itself is
        guarded so a logging failure never propagates (Req 2.8).

        Args:
            ctx: The originating query context (provides the reply address).
            text: The message body to deliver.
        """
        max_attempts = self._max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await self._adapter.send(ctx.conversation_ref, text)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry then log (Req 2.6/2.7)
                last_exc = exc
                logger.warning(
                    "Delivery to %s failed (attempt %d/%d): %s",
                    self._adapter.tool_name,
                    attempt,
                    max_attempts,
                    exc,
                )
        # Every attempt failed: log the final failure (Req 2.7).
        self._log_delivery_failure(last_exc)

    def _log_delivery_failure(self, exc: Exception | None) -> None:
        """Record a final delivery failure, swallowing any logging error.

        Writes one ``integration_error_log`` entry with the tool, the ``send``
        operation, and the failure reason (Req 2.7). Any exception raised by the
        error-log writer is caught and swallowed so logging can never interrupt
        query processing (Req 2.8).

        Args:
            exc: The last delivery exception, used as the failure reason.
        """
        reason = str(exc) if exc is not None else "delivery failed"
        logger.error(
            "Delivery to %s failed after all retries: %s",
            self._adapter.tool_name,
            reason,
        )
        if self._error_log is None:
            return
        try:
            self._error_log.insert(self._adapter.tool_name, SEND_OPERATION, reason)
        except Exception:  # noqa: BLE001 - logging must never break processing
            logger.exception(
                "Failed to record delivery error-log entry for %s",
                self._adapter.tool_name,
            )
