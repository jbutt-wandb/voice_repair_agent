"""Text-to-speech stage: a thin wrapper around Deepgram's live TTS WebSocket
(via the official SDK).

What travels over that socket:
  up:   {"type": "Speak", "text": "..."}  — queue text for synthesis
        {"type": "Flush"}                 — synthesize everything queued so far
        {"type": "Clear"}                 — discard queued/buffered audio (barge-in!)
  down: binary linear16 PCM audio chunks
        {"type": "Flushed", "sequence_id": n} — all audio for a Flush has been sent
        {"type": "Cleared"}               — a Clear was processed

Why a WebSocket instead of the simpler REST endpoint:
  1. The LLM streams text; we forward it sentence-by-sentence on one connection.
     With REST each sentence would be a new HTTPS request (TLS + queueing latency).
  2. `Clear` lets us cancel speech that is already synthesized but not yet sent —
     essential for barge-in. REST has no equivalent.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass

from deepgram import AsyncDeepgramClient
from deepgram.speak.v1.types import SpeakV1Text

from .config import TTS_ENCODING, TTS_MODEL, TTS_SAMPLE_RATE

log = logging.getLogger("tts")


@dataclass
class AudioChunk:
    data: bytes


@dataclass
class Flushed:
    sequence_id: int


@dataclass
class Cleared:
    pass


class DeepgramTTS:
    """One live TTS connection with lazy reconnect.

    Deepgram idles out a speak socket that has been quiet for a while — in a
    conversation there can easily be a minute between agent turns, so `speak()`
    transparently reopens the connection when needed and `events()` survives the
    reconnect. (A production system would do the same, or pre-warm connections.)
    """

    def __init__(self, api_key: str):
        self._client = AsyncDeepgramClient(api_key=api_key)
        self._stack: AsyncExitStack | None = None
        self._conn = None
        self._connected = asyncio.Event()
        self._closing = False

    async def __aenter__(self) -> "DeepgramTTS":
        await self._connect()
        return self

    async def __aexit__(self, *exc) -> None:
        self._closing = True
        self._connected.set()  # unblock events() so it can exit
        if self._conn is not None:
            try:
                await self._conn.send_close()
            except Exception:
                pass
        if self._stack is not None:
            await self._stack.aclose()

    async def _connect(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = AsyncExitStack()
        self._conn = await self._stack.enter_async_context(
            self._client.speak.v1.connect(
                model=TTS_MODEL,
                encoding=TTS_ENCODING,
                sample_rate=TTS_SAMPLE_RATE,
            )
        )
        self._connected.set()
        log.info("TTS connected (model=%s, %d Hz)", TTS_MODEL, TTS_SAMPLE_RATE)

    async def _ensure_connected(self) -> None:
        if self._conn is None or not self._connected.is_set():
            await self._connect()

    async def speak(self, text: str) -> None:
        """Queue a sentence for synthesis (audio arrives after the next flush())."""
        await self._ensure_connected()
        try:
            await self._conn.send_text(SpeakV1Text(type="Speak", text=text))
        except Exception:
            log.info("TTS socket was stale; reconnecting")
            self._connected.clear()
            await self._ensure_connected()
            await self._conn.send_text(SpeakV1Text(type="Speak", text=text))

    async def flush(self) -> None:
        await self._conn.send_flush()

    async def clear(self) -> None:
        """Barge-in: tell Deepgram to throw away any audio it hasn't sent yet."""
        if self._conn is not None:
            await self._conn.send_clear()

    async def events(self):
        """Async stream of AudioChunk / Flushed / Cleared events, across reconnects."""
        while not self._closing:
            await self._connected.wait()
            if self._closing:
                return
            conn = self._conn
            try:
                async for msg in conn:
                    if isinstance(msg, bytes):
                        yield AudioChunk(data=msg)
                        continue
                    mtype = getattr(msg, "type", None)
                    if mtype == "Flushed":
                        yield Flushed(sequence_id=int(msg.sequence_id or 0))
                    elif mtype == "Cleared":
                        yield Cleared()
                    elif mtype == "Warning":
                        log.warning("TTS warning: %s", msg)
            except Exception as exc:
                log.info("TTS connection ended (%s)", type(exc).__name__)
            # Connection dropped: wait for the next speak() to reconnect.
            if self._conn is conn:
                self._connected.clear()


async def _smoke_test():
    """Standalone check: synthesize one sentence, report bytes and TTFB.
    Run:  uv run python -m server.tts"""
    import os
    import time

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    async with DeepgramTTS(os.environ["DEEPGRAM_API_KEY"]) as tts:
        t0 = time.monotonic()
        await tts.speak("Your engineer visit is booked for Thursday morning. "
                        "The reference is E N G four two zero one.")
        await tts.flush()
        total, first = 0, None
        async for ev in tts.events():
            if isinstance(ev, AudioChunk):
                if first is None:
                    first = time.monotonic() - t0
                total += len(ev.data)
            elif isinstance(ev, Flushed):
                break
        secs = total / 2 / TTS_SAMPLE_RATE
        print(f"TTFB {first*1000:.0f} ms, {total} bytes = {secs:.1f}s of audio")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
