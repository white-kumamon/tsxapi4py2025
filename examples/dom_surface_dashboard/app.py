"""FastAPI application that serves a DOM/price dashboard backed by tsxapipy."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from tsxapipy import DEFAULT_CONFIG_CONTRACT_ID

from .coordinator import DomSurfaceCoordinator


LOGGER = logging.getLogger(__name__)

app = FastAPI(title="TSX DOM Surface Dashboard", version="0.1.0")

_coordinator: Optional[DomSurfaceCoordinator] = None
_index_path = Path(__file__).with_name("index.html")


@app.on_event("startup")
async def startup_event() -> None:
    """Initialise the market data coordinator when the app starts."""

    global _coordinator
    loop = asyncio.get_running_loop()

    contract_id = os.getenv("TSXAPIPY_DASHBOARD_CONTRACT_ID", DEFAULT_CONFIG_CONTRACT_ID or "")
    demo_mode = False
    if not contract_id:
        contract_id = "CON.DEMO.DASHBOARD"
        demo_mode = True
        LOGGER.warning(
            "No contract configured. Starting DOM Surface dashboard in demo mode with synthetic data."
        )

    LOGGER.info(
        "Starting DOM Surface coordinator for %s (%s)",
        contract_id,
        "demo" if demo_mode else "live",
    )
    coordinator = DomSurfaceCoordinator(contract_id=contract_id, loop=loop, demo_mode=demo_mode)
    coordinator.start()
    _coordinator = coordinator


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Stop the coordinator gracefully on shutdown."""

    global _coordinator
    if _coordinator is None:
        return

    coordinator = _coordinator
    _coordinator = None
    await asyncio.get_running_loop().run_in_executor(None, coordinator.stop)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not _index_path.exists():
        return HTMLResponse("<h1>Dashboard assets not found</h1>", status_code=500)
    return HTMLResponse(_index_path.read_text(encoding="utf-8"))


@app.get("/api/snapshot", response_class=JSONResponse)
async def get_snapshot() -> JSONResponse:
    if _coordinator is None:
        return JSONResponse({"error": "Coordinator not ready"}, status_code=503)

    snapshot = _coordinator.latest_snapshot
    if snapshot is None:
        return JSONResponse({"status": "pending"})
    return JSONResponse(snapshot)


@app.get("/healthz", response_class=PlainTextResponse)
async def health_check() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if _coordinator is None:
        await websocket.close(code=1011)
        return

    await websocket.accept()
    queue = await _coordinator.register()

    try:
        while True:
            snapshot = await queue.get()
            await websocket.send_json(snapshot)
    except WebSocketDisconnect:
        pass
    finally:
        _coordinator.unregister(queue)


# ---------------------------------------------------------------------------
# Convenience property for tests / debugging
# ---------------------------------------------------------------------------

@property
def coordinator() -> Optional[DomSurfaceCoordinator]:
    return _coordinator


setattr(app, "coordinator", coordinator)

