"""Speech-to-text stage: a thin wrapper around Deepgram's live-transcription
WebSocket (via the official SDK).

What travels over that socket:
  up:   raw linear16 PCM audio frames (binary), continuously, plus the occasional
        {"type": "KeepAlive"} so Deepgram doesn't drop an idle connection
  down: {"type": "Results"} JSON — interim and finalized transcripts
        {"type": "UtteranceEnd"} — fallback "the speaker stopped" signal
        {"type": "SpeechStarted"} — VAD onset events

The two flags that make real-time turn-taking possible:
  is_final      — this chunk of transcript will not change any more
  speech_final  — Deepgram's endpointer heard enough trailing silence to declare
                  the whole utterance over. THIS is our end-of-turn trigger.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass

from deepgram import AsyncDeepgramClient

from .config import STT_ENDPOINTING_MS, STT_MODEL, STT_SAMPLE_RATE, STT_UTTERANCE_END_MS

log = logging.getLogger("stt")

KEEPALIVE_INTERVAL_S = 5  # Deepgram closes the socket after ~10s of total silence


@dataclass
class Transcript:
    text: str
    is_final: bool
    speech_final: bool


@dataclass
class UtteranceEnd:
    pass


@dataclass
class SpeechStarted:
    pass


class DeepgramSTT:
    """One live-transcription connection. Usage:

        async with DeepgramSTT(api_key) as stt:
            asyncio.create_task(pump_mic_audio(stt))
            async for event in stt.events():
                ...
    """

    def __init__(self, api_key: str):
        self._client = AsyncDeepgramClient(api_key=api_key)
        self._stack = AsyncExitStack()
        self._conn = None
        self._keepalive_task: asyncio.Task | None = None

    async def __aenter__(self) -> "DeepgramSTT":
        self._conn = await self._stack.enter_async_context(
            self._client.listen.v1.connect(
                model=STT_MODEL,
                encoding="linear16",
                sample_rate=STT_SAMPLE_RATE,
                channels=1,
                interim_results=True,   # stream partial transcripts (needed for barge-in)
                smart_format=True,      # "twenty three" -> "23", punctuation etc.
                vad_events=True,        # SpeechStarted events
                endpointing=STT_ENDPOINTING_MS,
                utterance_end_ms=str(STT_UTTERANCE_END_MS),
            )
        )
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        log.info("STT connected (model=%s, %d Hz)", STT_MODEL, STT_SAMPLE_RATE)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
        try:
            await self._conn.send_close_stream()
        except Exception:
            pass  # already closed
        await self._stack.aclose()

    async def send_audio(self, pcm: bytes) -> None:
        """Forward one mic frame. Called for every frame the browser sends,
        regardless of conversation state — continuous audio is what lets Deepgram
        detect the user speaking over the agent (barge-in)."""
        await self._conn.send_media(pcm)

    async def events(self):
        """Async stream of Transcript / UtteranceEnd / SpeechStarted events."""
        async for msg in self._conn:
            mtype = getattr(msg, "type", None)
            if mtype == "Results":
                alts = msg.channel.alternatives if msg.channel else []
                text = alts[0].transcript if alts else ""
                yield Transcript(text=text,
                                 is_final=bool(msg.is_final),
                                 speech_final=bool(msg.speech_final))
            elif mtype == "UtteranceEnd":
                yield UtteranceEnd()
            elif mtype == "SpeechStarted":
                yield SpeechStarted()
            # Metadata / Error messages are logged and skipped
            elif mtype == "Error":
                log.error("STT error: %s", msg)

    async def _keepalive_loop(self):
        """Belt and braces: the mic streams continuously, but if the browser tab is
        muted or audio hasn't started yet, this keeps the socket alive anyway."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            try:
                await self._conn.send_keep_alive()
            except Exception:
                return  # socket gone; session teardown will handle it


async def _smoke_test():
    """Standalone check: synthesize a known sentence with Deepgram TTS, stream it
    back through STT, and print every event. Run:  uv run python -m server.stt"""
    import os

    from dotenv import load_dotenv

    from .tts import DeepgramTTS

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    key = os.environ["DEEPGRAM_API_KEY"]

    # Get some speech audio without needing a microphone: TTS it.
    async with DeepgramTTS(key) as tts:
        await tts.speak("Hello, my washing machine will not drain, can you help me?")
        await tts.flush()
        audio = bytearray()
        async for ev in tts.events():
            if ev.__class__.__name__ == "AudioChunk":
                audio.extend(ev.data)
            else:  # Flushed — all audio received
                break
    print(f"TTS produced {len(audio)} bytes of 24 kHz PCM")

    # Naive 24k -> 16k resample (take 2 of every 3 samples) just for this test.
    import array
    samples = array.array("h", bytes(audio))
    resampled = array.array("h", (samples[i] for i in range(len(samples)) if i % 3 != 2))

    async with DeepgramSTT(key) as stt:
        async def pump():
            chunk = 1280  # 40ms @ 16kHz int16
            # Endpointing detects end-of-speech from trailing SILENCE IN THE AUDIO —
            # if we just stop sending frames, speech_final never fires. So append
            # two seconds of real silence, exactly like an open mic would deliver.
            raw = resampled.tobytes() + b"\x00" * (16000 * 2 * 2)
            for i in range(0, len(raw), chunk):
                await stt.send_audio(raw[i:i + chunk])
                await asyncio.sleep(0.04)  # real-time pacing

        pump_task = asyncio.create_task(pump())
        async for event in stt.events():
            print(event)
            if isinstance(event, Transcript) and event.speech_final:
                break
        pump_task.cancel()


if __name__ == "__main__":
    asyncio.run(_smoke_test())
