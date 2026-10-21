"""The orchestrator: one Session per browser connection, wiring the three stages
(Deepgram STT, OpenAI LLM, Deepgram TTS) into a conversation with interruptions.

THE TURN STATE MACHINE
======================
    LISTENING --(speech_final, or UtteranceEnd fallback)--> THINKING
    THINKING  --(first TTS audio chunk forwarded)---------> SPEAKING
    SPEAKING  --(every sentence flushed & LLM done)-------> LISTENING
    THINKING or SPEAKING --(user speaks: BARGE-IN)--------> cancel --> LISTENING

Mic audio flows to Deepgram continuously in EVERY state. That is the trick that
makes barge-in possible: the STT stream never stops, so the moment the user talks
over the agent we get an interim transcript and can cancel everything in flight.

BROWSER <-> SERVER PROTOCOL (one WebSocket)
===========================================
  binary client->server : 16 kHz mono int16 PCM mic frames
  binary server->client : 24 kHz mono int16 PCM TTS audio
  JSON  client->server  : {type: "text_input", text}            typed input, bypasses STT
                          {type: "playback_started", turn_id}   first TTS sample audible
  JSON  server->client  : {type: "state", state, turn_id}
                          {type: "interim_transcript", text}
                          {type: "final_transcript", text, turn_id}
                          {type: "agent_text_delta", text, turn_id}
                          {type: "agent_text_done", turn_id, interrupted}
                          {type: "tool_call", name, args, result, turn_id}
                          {type: "barge_in", turn_id}           clear your playback buffer NOW
                          {type: "turn_metrics", ...}           see TurnMetrics
                          {type: "call_ended", reason}          agent ended the call; hang up
                                                                after the goodbye finishes playing
                          {type: "error", message}
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import enum
import json
import logging
import os
import statistics
import time
from dataclasses import asdict, dataclass

from fastapi import WebSocket
from openai import AsyncOpenAI

from . import llm
from .agent import build_system_prompt
from .config import (CONTEXT_KEEP_RECENT_MESSAGES, CONTEXT_MAX_HISTORY_TOKENS,
                     LATENCY_LOG_DIR, MIN_SENTENCE_CHARS)
from .stt import DeepgramSTT, SpeechStarted, Transcript, UtteranceEnd
from .tts import AudioChunk, Cleared, DeepgramTTS, Flushed

log = logging.getLogger("session")

# Metric fields we aggregate across a session, in display order.
_METRIC_FIELDS = ("stt_final_ms", "llm_ttft_ms", "tts_ttfb_ms",
                  "v2v_server_ms", "v2v_client_ms")


def _p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile (no numpy): the value at ceil(0.95*n)."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * 95 // 100))  # ceil(0.95*n)
    return ordered[rank - 1]

GREETING = ("Hi, you're through to HomeFix Appliances repair support, this is Alex. "
            "Which appliance can I help you with today?")


class State(enum.Enum):
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass
class TurnMetrics:
    """Everything we measure about one agent turn. All times in milliseconds.
    One of these is sent to the UI (and logged) per turn — this IS the
    observability story of the demo."""
    turn_id: int
    stt_final_ms: float | None = None    # endpoint wait: speech_final minus last interim
    llm_ttft_ms: float | None = None     # LLM time-to-first-token
    tts_ttfb_ms: float | None = None     # TTS time-to-first-audio-byte
    v2v_server_ms: float | None = None   # user stopped speaking -> first audio byte sent
    v2v_client_ms: float | None = None   # user stopped speaking -> first audio byte PLAYED
    barge_in: bool = False               # was this turn interrupted by the user?


class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.state = State.LISTENING
        self.turn_id = 0                     # bumped on every new turn AND every barge-in
        self.openai = AsyncOpenAI()
        self.messages: list[dict] = [{"role": "system", "content": build_system_prompt()}]

        self.utterance_parts: list[str] = []  # finalized STT fragments of the current user turn
        self.agent_task: asyncio.Task | None = None
        self.summarize_task: asyncio.Task | None = None  # background context compression
        self.close_task: asyncio.Task | None = None      # delayed hang-up after end_call
        self.ending = False                   # agent called the end_call tool this turn
        # Sentences actually sent to TTS this turn. On barge-in, the history records
        # THESE — what the user plausibly heard — not the full LLM output, so the
        # model's memory of the call matches the customer's.
        self.spoken_sentences: list[str] = []

        # TTS bookkeeping for the current turn
        self.speaks_sent = 0        # Speak messages sent
        self.flushes_sent = 0       # Flush messages sent (one per sentence)
        self.flushed_received = 0   # Flushed acks received
        self.llm_done = False
        # The turn whose audio the TTS socket is currently producing (set on every
        # Speak). TTS events are forwarded only while tts_turn == turn_id — stale
        # audio from a barged-in turn identifies itself by its old tag. We cannot
        # rely on Deepgram's Cleared ack for this: audio already mid-synthesis can
        # still arrive AFTER the Cleared message (observed in testing).
        self.tts_turn = 0

        # Metrics timestamps (time.monotonic())
        self.metrics: TurnMetrics | None = None
        self.t_last_transcript = 0.0
        self.t_turn_end = 0.0
        self.t_first_speak = 0.0
        self.total_barge_ins = 0

        # Per-session latency record, flushed to a JSON file on teardown.
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        self.turn_metrics: dict[int, dict] = {}   # turn_id -> merged metric fields

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        key = os.environ["DEEPGRAM_API_KEY"]
        async with DeepgramSTT(key) as stt, DeepgramTTS(key) as tts:
            self.stt, self.tts = stt, tts
            tasks = [
                asyncio.create_task(self._browser_rx(), name="browser_rx"),
                asyncio.create_task(self._stt_rx(), name="stt_rx"),
                asyncio.create_task(self._tts_rx(), name="tts_rx"),
            ]
            # The agent speaks first: a scripted greeting through the real TTS path,
            # so the user hears the voice (and we exercise the pipeline) immediately.
            self._begin_agent_turn(scripted=GREETING)
            try:
                # First task to exit (browser disconnect or stage failure) ends the session.
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception():
                        log.error("task %s failed", t.get_name(), exc_info=t.exception())
            finally:
                for extra in (self.agent_task, self.summarize_task, self.close_task):
                    if extra:
                        extra.cancel()
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    # NB: CancelledError is a BaseException — suppress(Exception)
                    # would NOT swallow it and teardown would raise.
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
                log.info("session closed (%d turns, %d barge-ins)",
                         self.turn_id, self.total_barge_ins)
                # Flush this session's latency record. Never let a logging failure
                # propagate out of teardown.
                try:
                    self._write_latency_log()
                except Exception:
                    log.exception("failed to write latency log")

    # ------------------------------------------------------------------ receive loops

    async def _browser_rx(self) -> None:
        """Everything arriving from the browser: mic audio and JSON control."""
        while True:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                # Mic frame. Forwarded unconditionally — see module docstring.
                await self.stt.send_audio(msg["bytes"])
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data["type"] == "text_input":
                    await self._on_text_input(data["text"])
                elif data["type"] == "playback_started":
                    # First TTS sample is audible. Refine voice-to-voice with the
                    # real number the user experienced (localhost: network ~0).
                    if self.metrics and data.get("turn_id") == self.metrics.turn_id:
                        self.metrics.v2v_client_ms = round(
                            (time.monotonic() - self.t_turn_end) * 1000)
                        await self._send_metrics()

    async def _stt_rx(self) -> None:
        """Deepgram transcription events drive both transcription UI and turn-taking."""
        async for event in self.stt.events():
            if isinstance(event, Transcript):
                if not event.text.strip():
                    continue  # silence / empty results
                # ANY real speech while the agent is thinking or speaking = barge-in.
                if self.state is not State.LISTENING:
                    await self._barge_in()
                if not event.speech_final:
                    # Timestamp of the last transcript BEFORE the endpoint decision,
                    # so stt_final_ms below measures the endpoint wait itself.
                    self.t_last_transcript = time.monotonic()
                if event.is_final:
                    self.utterance_parts.append(event.text)
                    await self._send({"type": "final_transcript", "text": event.text,
                                      "turn_id": self.turn_id})
                else:
                    await self._send({"type": "interim_transcript", "text": event.text})
                if event.speech_final:
                    # Primary end-of-turn signal: the endpointer heard enough silence.
                    await self._end_of_user_turn()
            elif isinstance(event, UtteranceEnd):
                # Fallback end-of-turn: fires when finalized words exist but no
                # speech_final arrived (e.g. endpointing missed a noisy pause).
                if self.state is State.LISTENING and self.utterance_parts:
                    log.info("end-of-turn via UtteranceEnd fallback")
                    await self._end_of_user_turn()
            elif isinstance(event, SpeechStarted):
                pass  # VAD onset; too noise-prone to act on, but useful to log/plot

    async def _tts_rx(self) -> None:
        """Deepgram TTS output: audio to the browser + turn completion tracking."""
        async for event in self.tts.events():
            stale = self.tts_turn != self.turn_id  # audio from a barged-in turn
            if isinstance(event, AudioChunk):
                if stale:
                    continue
                if self.state is State.THINKING:
                    # First audible evidence of the reply: THINKING -> SPEAKING.
                    now = time.monotonic()
                    if self.metrics:
                        self.metrics.tts_ttfb_ms = round((now - self.t_first_speak) * 1000)
                        self.metrics.v2v_server_ms = round((now - self.t_turn_end) * 1000)
                    await self._set_state(State.SPEAKING)
                await self.ws.send_bytes(event.data)
            elif isinstance(event, Flushed):
                if stale:
                    continue  # ack for a cancelled turn
                self.flushed_received += 1
                await self._maybe_finish_agent_turn()
            elif isinstance(event, Cleared):
                pass  # informational only — see tts_turn comment above

    # ------------------------------------------------------------------ turn handling

    async def _on_text_input(self, text: str) -> None:
        """Typed input from the UI's text box: bypasses STT but exercises the whole
        rest of the pipeline (LLM -> TTS -> playback). Invaluable for testing."""
        if self.state is not State.LISTENING:
            await self._barge_in()
        self.t_last_transcript = time.monotonic()  # typed "speech" ends now
        self.utterance_parts = [text]
        await self._send({"type": "final_transcript", "text": text, "turn_id": self.turn_id})
        await self._end_of_user_turn()

    async def _end_of_user_turn(self) -> None:
        user_text = " ".join(self.utterance_parts).strip()
        self.utterance_parts = []
        if not user_text:
            return
        now = time.monotonic()
        self.t_turn_end = now
        self._begin_agent_turn(user_text=user_text)
        # How long the endpointer made us wait after the last word (approximate:
        # true STT lag needs audio-sample timestamps, see README).
        self.metrics.stt_final_ms = round((now - self.t_last_transcript) * 1000)

    def _begin_agent_turn(self, user_text: str | None = None,
                          scripted: str | None = None) -> None:
        """Common setup for a new agent turn (LLM-driven or scripted greeting)."""
        self.turn_id += 1
        self.metrics = TurnMetrics(turn_id=self.turn_id)
        self.speaks_sent = self.flushes_sent = self.flushed_received = 0
        self.llm_done = False
        self.spoken_sentences = []
        coro = (self._scripted_turn(scripted, self.turn_id) if scripted
                else self._agent_turn(user_text, self.turn_id))
        self.agent_task = asyncio.create_task(coro, name=f"agent_turn_{self.turn_id}")

    async def _agent_turn(self, user_text: str, turn_id: int) -> None:
        """One LLM-driven reply: stream tokens, chunk into sentences, feed TTS."""
        await self._set_state(State.THINKING)
        self.messages.append({"role": "user", "content": user_text})
        chunker = llm.SentenceChunker(MIN_SENTENCE_CHARS)
        t_llm_start = time.monotonic()
        try:
            async for event in llm.agent_reply(self.openai, self.messages):
                if isinstance(event, llm.TextDelta):
                    if self.metrics.llm_ttft_ms is None:
                        self.metrics.llm_ttft_ms = round(
                            (time.monotonic() - t_llm_start) * 1000)
                    await self._send({"type": "agent_text_delta", "text": event.text,
                                      "turn_id": turn_id})
                    for sentence in chunker.feed(event.text):
                        await self._speak(sentence)
                elif isinstance(event, llm.ToolCalled):
                    if event.name == "end_call":
                        # The model decided the call is over; after the goodbye
                        # finishes flushing, _finish_agent_turn hangs up.
                        self.ending = True
                    await self._send({"type": "tool_call", "name": event.name,
                                      "args": event.arguments, "result": event.result,
                                      "turn_id": turn_id})
                elif isinstance(event, llm.Done):
                    remainder = chunker.flush()
                    if remainder:
                        await self._speak(remainder)
            await self._send({"type": "agent_text_done", "turn_id": turn_id,
                              "interrupted": False})
            self.llm_done = True
            if self.speaks_sent == 0:
                # Model produced no speakable text (shouldn't happen, but never
                # leave the state machine stuck in THINKING).
                await self._finish_agent_turn()
            else:
                await self._maybe_finish_agent_turn()
        except asyncio.CancelledError:
            # Barge-in or session teardown cancelled us mid-stream. agent_reply never
            # got to append its assistant message, so record one ourselves containing
            # only what was actually SENT TO TTS — the model's memory of the call
            # should match what the customer heard, not what was silently generated.
            spoken = " ".join(self.spoken_sentences).strip()
            if not (self.messages and self.messages[-1].get("role") == "assistant"):
                # (if the last message is already an assistant one, agent_reply
                # finished its append just before we were cancelled — don't duplicate)
                self.messages.append({
                    "role": "assistant",
                    "content": (spoken + " [cut off here by the customer]") if spoken
                               else "[reply interrupted by the customer before speaking]",
                })
            with contextlib.suppress(Exception):
                await self._send({"type": "agent_text_done", "turn_id": turn_id,
                                  "interrupted": True})
            raise

    async def _scripted_turn(self, text: str, turn_id: int) -> None:
        """Speak fixed text (the greeting) through the normal TTS path."""
        await self._set_state(State.THINKING)
        self.t_turn_end = time.monotonic()  # baseline so greeting metrics aren't nonsense
        self.messages.append({"role": "assistant", "content": text})
        await self._send({"type": "agent_text_delta", "text": text, "turn_id": turn_id})
        await self._speak(text)
        await self._send({"type": "agent_text_done", "turn_id": turn_id, "interrupted": False})
        self.llm_done = True
        await self._maybe_finish_agent_turn()

    async def _speak(self, sentence: str) -> None:
        """Send one sentence to TTS. We Flush after every sentence: deterministic
        audio delivery and per-sentence Flushed acks to track completion. (The
        tradeoff: flushing very often can clip prosody; sentence-level is fine.)"""
        if self.speaks_sent == 0:
            self.t_first_speak = time.monotonic()
        self.tts_turn = self.turn_id  # tag the socket's output as ours
        self.spoken_sentences.append(sentence)
        self.speaks_sent += 1
        self.flushes_sent += 1
        await self.tts.speak(sentence)
        await self.tts.flush()

    async def _maybe_finish_agent_turn(self) -> None:
        """The turn is over when the LLM finished AND every flush was acknowledged."""
        if self.llm_done and self.flushed_received >= self.flushes_sent:
            await self._finish_agent_turn()

    async def _finish_agent_turn(self) -> None:
        await self._send_metrics()
        log.info("turn %d metrics: %s", self.metrics.turn_id, asdict(self.metrics))
        if self.ending:
            # The agent said goodbye and all of its audio is flushed. Tell the client
            # to hang up once playback drains; close ourselves as a fallback in case
            # the client never does (tab crashed, etc.).
            await self._send({"type": "call_ended", "reason": "agent_ended"})
            log.info("call ended by agent")
            self.close_task = asyncio.create_task(self._close_after(15))
            return
        await self._set_state(State.LISTENING)
        # Quiet moment between turns: compress old history if it has grown too big.
        # Never runs during a turn, so it cannot add to voice-to-voice latency.
        if (self.summarize_task is None or self.summarize_task.done()) \
                and self._history_tokens() > CONTEXT_MAX_HISTORY_TOKENS:
            self.summarize_task = asyncio.create_task(self._summarize_history())

    # ------------------------------------------------------------------ context management

    def _history_tokens(self) -> int:
        """Cheap token estimate (~4 chars/token) of history excluding the system
        prompt. Good enough to decide WHEN to summarize; exact counts don't matter."""
        return sum(len(str(m.get("content") or "")) for m in self.messages[1:]) // 4

    async def _summarize_history(self) -> None:
        """Rolling summarization: replace old turns with compact call notes, keeping
        the most recent messages verbatim. History is append-only, so replacing the
        slice [1:split] by index is safe even if a new turn appends meanwhile."""
        split = len(self.messages) - CONTEXT_KEEP_RECENT_MESSAGES
        # Never split between an assistant tool-call message and its tool result —
        # an orphaned role:"tool" message is an invalid OpenAI sequence.
        while split > 1 and self.messages[split].get("role") == "tool":
            split -= 1
        if split <= 1:
            return
        old = self.messages[1:split]
        try:
            notes = await llm.summarize_history(self.openai, old)
        except Exception:
            log.exception("history summarization failed; keeping full history")
            return
        summary_msg = {"role": "system",
                       "content": f"Call notes from the conversation so far:\n{notes}"}
        before = self._history_tokens()
        self.messages[1:split] = [summary_msg]
        log.info("history summarized: %d msgs -> call notes (%d -> %d est. tokens)",
                 len(old), before, self._history_tokens())

    async def _close_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        with contextlib.suppress(Exception):
            await self.ws.close()

    # ------------------------------------------------------------------ barge-in

    async def _barge_in(self) -> None:
        """The user spoke while the agent was thinking/speaking. Kill everything
        in flight, in this order (each step is one place stale output could leak):
          1. bump turn_id              -> stale events become identifiable
          2. cancel the LLM task       -> no more tokens, no more Speak messages
          3. Clear on the TTS socket   -> Deepgram discards synthesized-but-unsent audio
          4. drop in-flight TTS bytes  -> dropping_tts until Deepgram acks Cleared
          5. tell the browser          -> it zeroes its playback ring buffer instantly
        """
        self.total_barge_ins += 1
        if self.metrics:
            self.metrics.barge_in = True
            await self._send_metrics()  # record the interrupted turn's partial metrics
        interrupted_turn = self.turn_id
        self.turn_id += 1
        self.ending = False  # "...wait, actually" during the goodbye un-ends the call
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.agent_task
        elif (self.spoken_sentences and self.messages
              and self.messages[-1].get("role") == "assistant"):
            # The LLM had already finished (full reply in history) but the AUDIO was
            # cut mid-playback. Truncate the stored reply to the sentences whose audio
            # was fully delivered — the model's memory should match what the customer
            # heard, not what was generated. (Best server-side proxy; true playback
            # position lives in the client's ring buffer.)
            heard = " ".join(self.spoken_sentences[:max(1, self.flushed_received)])
            self.messages[-1]["content"] = heard + " [cut off here by the customer]"
        if self.speaks_sent:
            # Stop Deepgram synthesizing/sending the rest; whatever is already in
            # flight gets dropped by the tts_turn tag check in _tts_rx.
            await self.tts.clear()
        await self._send({"type": "barge_in", "turn_id": interrupted_turn})
        log.info("barge-in: turn %d interrupted", interrupted_turn)
        await self._set_state(State.LISTENING)

    # ------------------------------------------------------------------ helpers

    async def _set_state(self, state: State) -> None:
        self.state = state
        await self._send({"type": "state", "state": state.value, "turn_id": self.turn_id})

    async def _send_metrics(self) -> None:
        if self.metrics:
            payload = {"type": "turn_metrics", **asdict(self.metrics)}
            self._record_metrics(payload)   # also accumulate for the session log
            await self._send(payload)

    def _record_metrics(self, payload: dict) -> None:
        """Merge a turn_metrics payload's non-None metric fields into the per-session
        record, keyed by turn_id (the same payload is also sent to the browser)."""
        turn_id = payload.get("turn_id")
        if turn_id is None:
            return
        row = self.turn_metrics.setdefault(turn_id, {})
        for field in _METRIC_FIELDS:
            value = payload.get(field)
            if value is not None:
                row[field] = value

    def _write_latency_log(self) -> None:
        """Write this session's per-turn + aggregate latency to a timestamped JSON
        file. Called once on teardown. No-op for sessions with no recorded turns."""
        turns = [tid for tid, m in self.turn_metrics.items() if m]
        if not turns:
            log.debug("no turn metrics recorded; skipping latency log")
            return

        per_turn = [{"turn_id": tid, **self.turn_metrics[tid]} for tid in sorted(turns)]

        overall = {}
        for field in _METRIC_FIELDS:
            values = [m[field] for m in self.turn_metrics.values() if m.get(field) is not None]
            if not values:
                overall[field] = {"count": 0}
                continue
            overall[field] = {
                "count": len(values),
                "mean": round(statistics.fmean(values)),
                "min": min(values),
                "max": max(values),
                "median": round(statistics.median(values)),
                "p95": round(_p95(values)),
            }

        ended_at = datetime.datetime.now(datetime.timezone.utc)
        report = {
            "session": {
                "started_at_utc": self.started_at.isoformat(),
                "ended_at_utc": ended_at.isoformat(),
                "duration_seconds": round((ended_at - self.started_at).total_seconds(), 1),
                "turns": len(turns),
                "barge_ins": self.total_barge_ins,
            },
            "overall": overall,
            "per_turn": per_turn,
        }

        LATENCY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        path = LATENCY_LOG_DIR / f"session_{stamp}.json"
        suffix = 2
        while path.exists():  # same-second starts don't clobber each other
            path = LATENCY_LOG_DIR / f"session_{stamp}_{suffix}.json"
            suffix += 1
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        log.info("wrote latency log %s (%d turns)", path.name, len(turns))

    async def _send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))
