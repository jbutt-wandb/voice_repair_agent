"""FastAPI app: serves the static frontend and owns the /ws voice endpoint.

Each WebSocket connection gets its own Session (see session.py) with its own
Deepgram STT + TTS connections and its own conversation history. Scaling this
pattern = scaling the number of concurrent Sessions a host can hold (see README).
"""

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from .session import Session

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)-8s %(levelname)-7s %(message)s")
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Voice Repair Agent")


@app.websocket("/ws")
async def voice_ws(ws: WebSocket) -> None:
    await ws.accept()
    log.info("browser connected")
    try:
        await Session(ws).run()
    except WebSocketDisconnect:
        pass
    finally:
        log.info("browser disconnected")


class NoCacheStaticFiles(StaticFiles):
    """Serve the frontend with caching disabled. This is a teaching/dev app whose
    HTML/JS/worklets change often; browser caching of a stale app.js silently hides
    edits (reload shows old behaviour) — a confusing trap during development. In a
    real deployment you'd cache aggressively with content-hashed filenames instead."""

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


# Mounted last so /ws is matched first. html=True serves index.html at /.
app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")
