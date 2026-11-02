"""Non-blocking async event handlers.

`schedule_event` hands an event to `asyncio.create_task` so the HTTP request can
return `202 Accepted` immediately. The abstraction (`schedule_event` /
`execute_event`) is deliberately narrow so the dispatcher could be swapped for
Celery/Redis later without touching the API or the engine.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog
from app.schemas.flight_event import DisruptionEvent
from app.services import catering_engine
from app.services.catering_engine import RecalcResult

logger = logging.getLogger("skyflow.worker")

SessionFactory = async_sessionmaker[AsyncSession]

# asyncio only keeps weak references to tasks; hold strong ones until they finish.
_pending: set[asyncio.Task] = set()
# One lock per flight serialises read-modify-write cycles on the same flight so
# concurrent events cannot overwrite each other. Weak values: unused locks vanish.
_flight_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _lock_for(flight_id: str) -> asyncio.Lock:
    lock = _flight_locks.get(flight_id)
    if lock is None:
        lock = asyncio.Lock()
        _flight_locks[flight_id] = lock
    return lock


async def execute_event(session_factory: SessionFactory, event: DisruptionEvent) -> RecalcResult:
    """Apply an event atomically. Any exception rolls the whole transaction back."""
    async with _lock_for(event.flight_id):
        async with session_factory() as session:
            async with session.begin():  # commits on success, rolls back on error
                return await catering_engine.apply_event(session, event)


async def process_event(
    session_factory: SessionFactory, event: DisruptionEvent
) -> Optional[RecalcResult]:
    """Background wrapper: never raises; failures are logged and audited."""
    try:
        result = await execute_event(session_factory, event)
        logger.info(
            "Recalculated %s after %s: %d -> %d meals",
            result.flight_id, result.event_type, result.previous_total, result.new_total,
        )
        return result
    except Exception as exc:
        logger.exception("Failed to process %s for %s", event.event_type.value, event.flight_id)
        await _record_failure(session_factory, event, exc)
        return None


async def _record_failure(
    session_factory: SessionFactory, event: DisruptionEvent, exc: Exception
) -> None:
    """Persist a failure marker in its own transaction (the original one rolled back)."""
    try:
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    AuditLog(
                        flight_id=event.flight_id,
                        event_type="RECALC_FAILED",
                        description=(
                            f"{event.event_type.value} could not be applied and was rolled "
                            f"back: {type(exc).__name__}: {exc}"
                        )[:1000],
                    )
                )
    except Exception:
        logger.exception("Could not write failure audit log for %s", event.flight_id)


def schedule_event(session_factory: SessionFactory, event: DisruptionEvent) -> asyncio.Task:
    """Fire-and-forget: run `process_event` on the event loop without blocking the caller."""
    task = asyncio.create_task(
        process_event(session_factory, event), name=f"recalc-{event.flight_id}"
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def pending_count() -> int:
    return len(_pending)


async def wait_for_pending(timeout: Optional[float] = None) -> None:
    """Wait for in-flight tasks (used on shutdown and by tests)."""
    if not _pending:
        return
    done, pending = await asyncio.wait(set(_pending), timeout=timeout)
    for task in pending:
        task.cancel()
