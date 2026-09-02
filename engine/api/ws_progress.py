"""WebSocket: live progress for long-running work.

The UI shows a real progress rail with the current filename and an estimated
time remaining, so the reader needs a steady stream rather than one update at
the end.

Flooding is prevented by the bus, which coalesces progress events per run, and
by this reader, which wakes ten times a second. Endings are never coalesced,
so a rail cannot be left spinning after the work has stopped.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from engine.core.events import POLL_INTERVAL_SECONDS, progress_bus, shutdown_requested

router = APIRouter()


@router.websocket("/api/ws/progress")
async def progress_socket(websocket: WebSocket) -> None:
    """Stream `{stage, kind, current, total, current_item, elapsed, eta}`."""
    await websocket.accept()
    logger.debug("Progress socket connected")

    try:
        with progress_bus.subscribe() as subscriber:
            # Not `while True`: the engine must be able to shut down promptly
            # when the user closes the window.
            while not shutdown_requested.is_set():
                for event in subscriber.drain():
                    await websocket.send_json(event.as_dict())
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

            await websocket.close()
    except WebSocketDisconnect:
        logger.debug("Progress socket disconnected")
    except (asyncio.CancelledError, RuntimeError):
        # Server shutting down, or the socket closed under us. Not an error.
        raise
    finally:
        logger.debug("Progress socket closed")
