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
                          {type: "image", data}                 a photo (base64 data URL);
                                                                attached to the NEXT user turn
                          {type: "playback_started", turn_id}   first TTS sample audible
  JSON  server->client  : {type: "state", state, turn_id}
                          {type: "interim_transcript", text}
                          {type: "final_transcript", text, turn_id}
                          {type: "agent_text_delta", text, turn_id}
                          {type: "agent_text_done", turn_id, interrupted}
                          {type: "tool_call", name, args, result, turn_id}
                          {type: "barge_in", turn_id}           clear your playback buffer NOW
                          {type: "turn_metrics", ...}           see TurnMetrics
                          {type: "image_received"}              ack: a photo is attached
                          {type: "call_ended", reason}          agent ended the call; hang up
                                                                after the goodbye finishes playing
                          {type: "error", message}
"""

from __future__ import annotations

import asyncio
import array
import contextlib
import datetime
import enum
import json
import logging
import os
import random
import re
import statistics
import time
import uuid
from dataclasses import asdict, dataclass

from fastapi import WebSocket
from openai import AsyncOpenAI

from . import llm
from .agent import build_system_prompt
from .config import (BARGE_IN_ON_VAD, BARGE_IN_VAD_GRACE_MS,
                     CONTEXT_KEEP_RECENT_MESSAGES, CONTEXT_MAX_HISTORY_TOKENS,
                     DEFAULT_IMAGE_PROMPT, IDLE_GOODBYE_PHRASE, IDLE_HANGUP_S,
                     IDLE_PROMPT_PHRASE, IDLE_PROMPT_S, IMAGE_AUTOSEND_S,
                     IMAGE_FILLER_PHRASES, IMAGE_NOTE_REMINDER, LATENCY_LOG_DIR,
                     MIN_SENTENCE_CHARS, VOCAL_JUDGE_ENABLED)
from .stt import DeepgramSTT, SpeechStarted, Transcript, UtteranceEnd
from .tts import AudioChunk, Cleared, DeepgramTTS, Flushed
from .vocal_judge import classify_vocal
from .weave_trace import SessionTracer

log = logging.getLogger("session")

# Metric fields we aggregate across a session, in display order.
_METRIC_FIELDS = ("stt_final_ms", "llm_ttft_ms", "tts_ttfb_ms",
                  "v2v_server_ms", "v2v_client_ms")


def _p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile (no numpy): the value at ceil(0.95*n)."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * 95 // 100))  # ceil(0.95*n)
    return ordered[rank - 1]


# Leading-silence trim for the per-turn mic snapshot (observability only — the live
# STT stream is never touched). The mic rolls continuously between turn boundaries,
# so an untrimmed clip is mostly the agent's own reply playing out (near-silence
# after browser echo cancellation) plus the caller's thinking pause.
_TRIM_FRAME_SAMPLES = 320            # 20 ms frames at 16 kHz
_TRIM_THRESHOLD = 500                # int16 peak floor ~1.5% full scale
_TRIM_PEAK_DIVISOR = 6               # ...raised to clip_peak/6: live-call boundary blips
                                     # (the agent's own audio tail / echo ramp, measured
                                     # peaks ~1-2k) sit far below real speech (9-32k), so
                                     # the speech threshold must scale with THIS clip
_TRIM_PREROLL_BYTES = 16000 * 2      # keep 1 s before the first voiced frame: barge-in
                                     # context survives and a late threshold can't clip speech


def _trim_leading_silence(pcm: bytes) -> bytes:
    """Drop everything before the caller's first genuinely-voiced 20 ms frame,
    keeping a 1 s pre-roll. "Voiced" is relative to the clip's own loudness
    (max of a fixed floor and clip_peak/6), so a short boundary blip at second
    zero can't defeat the trim. If nothing qualifies, return the clip untouched."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return pcm
    clip_peak = max(max(samples), -min(samples))
    threshold = max(_TRIM_THRESHOLD, clip_peak // _TRIM_PEAK_DIVISOR)
    for i in range(0, len(samples), _TRIM_FRAME_SAMPLES):
        frame = samples[i:i + _TRIM_FRAME_SAMPLES]
        if frame and max(max(frame), -min(frame)) >= threshold:
            return pcm[max(0, i * 2 - _TRIM_PREROLL_BYTES):]
    return pcm


GREETING = ("Hi, you're through to HomeFix Appliances repair support, this is Alex. "
            "Which appliance can I help you with today?")

# ---------------------------------------------------------------- photo-memory sentinel
# On an image turn the model appends a silent, content-labelled description of the photo
# wrapped in this marker AFTER its spoken reply (see agent.build_system_prompt). The
# description is kept in history as cheap text (so the base64 is never resent) while the
# marker itself is NEVER spoken. The sentinel is a token sequence the model won't utter
# naturally, so detecting it is unambiguous.
_PHOTO_OPEN = "[["
# Pull "<description>" out of a complete [[PHOTO: <description>]] marker...
_PHOTO_NOTE_RE = re.compile(r"\[\[\s*PHOTO\s*:\s*(.*?)\s*\]\]", re.IGNORECASE | re.DOTALL)
# ...or out of an unterminated one (model hit max_tokens before closing ]]).
_PHOTO_NOTE_OPEN_RE = re.compile(r"\[\[\s*PHOTO\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)
# Hard guard: strip any complete [[...]] marker, then any dangling [[... tail, so nothing
# containing "[[" can ever reach TTS even if the model formats the note oddly.
_SENTINEL_STRIP_RE = re.compile(r"\[\[.*?\]\]", re.DOTALL)
_SENTINEL_TAIL_RE = re.compile(r"\[\[.*$", re.DOTALL)


def _strip_sentinel(text: str) -> str:
    """Remove any photo-note sentinel (complete or dangling) from text bound for TTS.
    Belt-and-braces: the streaming loop already stops feeding at the sentinel, but this
    guarantees no "[[" ever reaches the speaker or spoken_sentences."""
    text = _SENTINEL_STRIP_RE.sub("", text)
    text = _SENTINEL_TAIL_RE.sub("", text)
    return text


def _speakable_prefix_len(buf: str) -> int:
    """Index up to which `buf` is safe to speak: everything before the first sentinel
    marker. If no marker has appeared yet, hold back a lone trailing '[' (it could be
    the first half of a '[[' arriving split across deltas)."""
    idx = buf.find(_PHOTO_OPEN)
    if idx != -1:
        return idx
    if buf.endswith("["):
        return len(buf) - 1
    return len(buf)


def _extract_photo_note(buf: str) -> str | None:
    """Pull the content-labelled description out of a [[PHOTO: ...]] marker in `buf`
    (tolerating an unterminated marker). Returns None if no marker is present."""
    m = _PHOTO_NOTE_RE.search(buf) or _PHOTO_NOTE_OPEN_RE.search(buf)
    if not m:
        return None
    note = m.group(1).strip()
    return note or None


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
        # A photo the user sent, waiting to ride on the next user turn (then cleared).
        self.pending_image: str | None = None
        # Content-labelled descriptions captured from the model's [[PHOTO: ...]] note on an
        # image turn, keyed by id() of the image-bearing user message. Consumed (popped) by
        # _collapse_image_messages so the collapsed note carries what the photo showed —
        # the photo's information survives in cheap text without resending the base64.
        self._image_descriptions: dict[int, str] = {}
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
        self.t_first_speak: float | None = None  # first REAL speak; reset per turn
        self.has_filler = False                  # image-turn filler spoken this turn?
        self.total_barge_ins = 0
        # VAD barge-in is TRANSCRIPT-confirmed: a SpeechStarted (VAD onset) only "pre-arms"
        # (records that speech energy was seen); it never interrupts on its own. The actual
        # interrupt is committed by a recognized, non-empty transcript. This guarantees the
        # agent can NEVER barge-in on its own greeting/echo or room noise while the user is
        # silent. See _stt_rx (SpeechStarted branch) and _vad_barge_in_allowed.
        self._vad_pre_armed = False
        self._t_vad_pre_arm = 0.0

        # Per-session latency record, flushed to a JSON file on teardown.
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        self.turn_metrics: dict[int, dict] = {}   # turn_id -> merged metric fields

        # Inactivity watchdog: nudge once, then hang up a silent (abandoned) call.
        self.t_last_activity = time.monotonic()
        self.idle_stage = 0          # 0 normal, 1 nudged ("are you still there?"), 2 ending
        self.t_nudge = 0.0
        self.idle_turn = False       # the current scripted turn is a nudge/goodbye

        # Weave tracing: one thread per session, one span tree per turn (see
        # weave_trace.py). All no-ops when tracing is disabled.
        self.session_id = uuid.uuid4().hex
        self.trace = SessionTracer(self.session_id)
        self.tt = None                      # current turn's trace accumulator (or None)
        # In-flight judge-then-trace tasks (one per finished turn). Teardown AWAITS
        # these (briefly) instead of cancelling, or the last turn's trace would be lost.
        self._judge_tasks: set[asyncio.Task] = set()
        # Rolling buffer of this turn's mic PCM, snapshotted as the turn's user audio.
        self._mic_buf = bytearray()
        # Agent-audio capture for the trace, filled from the TTS output in _tts_rx and
        # split into two streams: the model reply (-> voice_turn.tts_audio) and any canned
        # speech (greeting / filler / idle -> that system_utterance's audio).
        self._tts_buf = bytearray()         # current turn's REAL reply audio (24 kHz PCM)
        self._sys_tts_buf = bytearray()     # current canned-utterance audio (24 kHz PCM)
        # A canned utterance whose audio we're capturing, emitted (with its audio) at the
        # turn boundary rather than up-front, so the system_utterance op carries sound.
        self._pending_sys_utt: tuple[str, str] | None = None

        # Auto-send a lone photo after IMAGE_AUTOSEND_S of silence (see _arm_image_autosend).
        self._image_autosend_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        key = os.environ["DEEPGRAM_API_KEY"]
        async with DeepgramSTT(key) as stt, DeepgramTTS(key) as tts:
            self.stt, self.tts = stt, tts
            tasks = [
                asyncio.create_task(self._browser_rx(), name="browser_rx"),
                asyncio.create_task(self._stt_rx(), name="stt_rx"),
                asyncio.create_task(self._tts_rx(), name="tts_rx"),
                asyncio.create_task(self._idle_watcher(), name="idle_watcher"),
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
                # Let in-flight judge-then-trace tasks finish (bounded) BEFORE the
                # cancel sweep — cancelling them would drop the final turns' traces.
                if self._judge_tasks:
                    with contextlib.suppress(Exception):
                        await asyncio.wait(self._judge_tasks, timeout=10)
                for extra in (self.agent_task, self.summarize_task, self.close_task,
                              self._image_autosend_task, *self._judge_tasks):
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
                if self.trace.capture_media:
                    # Retain the raw PCM so the turn's utterance can be attached to its
                    # trace; snapshotted and cleared at end-of-turn (see _snapshot_mic).
                    self._mic_buf.extend(msg["bytes"])
                await self.stt.send_audio(msg["bytes"])
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data["type"] == "text_input":
                    await self._on_text_input(data["text"])
                elif data["type"] == "image":
                    # A photo. Normally it rides on the NEXT user turn, sent to the
                    # (multimodal) LLM alongside the user's words. But if the caller stays
                    # silent, _arm_image_autosend fires it on its own after a short wait.
                    self._mark_activity()
                    self.pending_image = data["data"]
                    await self._send({"type": "image_received"})
                    self._arm_image_autosend()
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
                self._mark_activity()  # real speech resets the inactivity watchdog
                # The caller is speaking: a pending photo will ride WITH their words on
                # this turn, so cancel the "lone photo" auto-send countdown.
                self._cancel_image_autosend()
                # ANY real, recognized speech while the agent is thinking or speaking =
                # barge-in. THIS is the only thing that commits an interrupt — a non-empty
                # transcript proves it's the user, not the agent's own echo or room noise
                # (which may still have pre-armed VAD above but can never fire on its own).
                if self.state is not State.LISTENING:
                    if self._vad_pre_armed:
                        log.info("barge-in confirmed by transcript (VAD pre-armed %d ms earlier)",
                                 round((time.monotonic() - self._t_vad_pre_arm) * 1000))
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
                # VAD onset is the EARLIEST possible barge-in cue, but it fires on ANY
                # sound energy — the agent's OWN greeting echoing back through the
                # speakers (no headphones), or room noise — so it can NEVER be trusted to
                # interrupt on its own. Doing so was the regression that made the agent
                # barge-in on its own greeting with the user completely silent.
                #
                # So VAD only PRE-ARMS here; it never cancels anything. The interrupt is
                # COMMITTED only by an actual recognized, non-empty user transcript (the
                # Transcript branch above). Echo cancellation (static/app.js) suppresses
                # the agent's own voice enough that STT won't produce confident words from
                # it even while VAD energy fires — so "a real transcript appeared" is the
                # discriminator that makes "silence can never self-interrupt" a hard
                # guarantee. This costs the raw VAD-onset earliness (see README / report):
                # we deliberately trade a few ms of snappiness for never self-interrupting.
                if (self.state is not State.LISTENING
                        and self._vad_barge_in_allowed()):
                    self._vad_pre_armed = True
                    self._t_vad_pre_arm = time.monotonic()

    async def _tts_rx(self) -> None:
        """Deepgram TTS output: audio to the browser + turn completion tracking."""
        async for event in self.tts.events():
            stale = self.tts_turn != self.turn_id  # audio from a barged-in turn
            if isinstance(event, AudioChunk):
                if stale:
                    continue
                # The agent genuinely starts speaking on the FIRST audio chunk (filler
                # included) — flip the UI state here.
                if self.state is State.THINKING:
                    await self._set_state(State.SPEAKING)
                # But the latency metrics must measure the REAL reply, not the filler,
                # so they stay consistent across image and non-image turns. The filler
                # is a single Speak+Flush, so the real reply's audio is the first chunk
                # that arrives AFTER the filler's Flushed ack (flushed_received >= 1).
                # Capture once, and only once t_first_speak (first real speak) is set.
                if (self.metrics and self.metrics.tts_ttfb_ms is None
                        and self.t_first_speak is not None
                        and (not self.has_filler or self.flushed_received >= 1)):
                    now = time.monotonic()
                    self.metrics.tts_ttfb_ms = round((now - self.t_first_speak) * 1000)
                    self.metrics.v2v_server_ms = round((now - self.t_turn_end) * 1000)
                # Capture the agent's audio for the trace, split into two streams. The
                # filler (image turns) is spoken first and its Flushed ack lands before
                # the real reply's first chunk — the same gate the TTFB metric uses — so
                # flushed_received separates filler audio from the real reply. A scripted
                # turn (greeting / idle) has no self.tt: all of its audio is canned.
                if self.trace.capture_media:
                    if self.has_filler and self.flushed_received == 0:
                        self._sys_tts_buf.extend(event.data)         # image-turn filler
                    elif self.tt is None and self._pending_sys_utt is not None:
                        self._sys_tts_buf.extend(event.data)         # greeting / idle
                    else:
                        self._tts_buf.extend(event.data)             # real model reply
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
        self._mark_activity()
        self._cancel_image_autosend()  # typed turn will consume any pending photo
        if self.state is not State.LISTENING:
            await self._barge_in()
        self.t_last_transcript = time.monotonic()  # typed "speech" ends now
        self.utterance_parts = [text]
        await self._send({"type": "final_transcript", "text": text, "turn_id": self.turn_id})
        await self._end_of_user_turn(from_voice=False)

    async def _end_of_user_turn(self, from_voice: bool = True) -> None:
        # Defensive: if a prior image turn was cancelled by barge-in before it could
        # collapse, strip the lingering base64 now so it isn't resent.
        self._collapse_image_messages()
        user_text = " ".join(self.utterance_parts).strip()
        self.utterance_parts = []
        # Always snapshot (and clear) the mic buffer so it can't grow unbounded; keep
        # the audio only for spoken turns (a typed turn has no meaningful mic input).
        audio = self._snapshot_mic()
        if not user_text:
            return
        now = time.monotonic()
        self.t_turn_end = now
        self._begin_agent_turn(user_text=user_text,
                               audio=audio if from_voice else None)
        # How long the endpointer made us wait after the last word (approximate:
        # true STT lag needs audio-sample timestamps, see README).
        self.metrics.stt_final_ms = round((now - self.t_last_transcript) * 1000)

    def _begin_agent_turn(self, user_text: str | None = None,
                          scripted: str | None = None, *,
                          audio: bytes | None = None, kind: str = "greeting",
                          image_autosend: bool = False) -> None:
        """Common setup for a new agent turn (LLM-driven or scripted greeting)."""
        # A turn is starting: any pending "lone photo" countdown is now moot.
        self._cancel_image_autosend()
        self.turn_id += 1
        self.metrics = TurnMetrics(turn_id=self.turn_id)
        self.speaks_sent = self.flushes_sent = self.flushed_received = 0
        self.llm_done = False
        self.spoken_sentences = []
        self.has_filler = False           # set True if an image-turn filler is spoken
        self.t_first_speak = None         # set on the first REAL (recorded) speak
        self._vad_pre_armed = False       # fresh turn: no VAD onset seen yet
        # Fresh agent-audio capture for this turn (see _tts_rx).
        self._tts_buf.clear()
        self._sys_tts_buf.clear()
        self._pending_sys_utt = None
        if scripted:
            # Canned speech is not a model turn; no voice_turn trace. Its system_utterance
            # op is emitted (with captured audio) at the turn boundary via _finish_agent_turn.
            self.tt = None
            coro = self._scripted_turn(scripted, self.turn_id, kind)
        else:
            # Open this turn's trace accumulator and attach its media (the photo is
            # still pending here; it's consumed inside _agent_turn).
            self.tt = self.trace.start_turn(self.turn_id, user_text or "")
            if self.tt:
                if self.pending_image:
                    self.tt.set_image(self.pending_image)
                self.tt.set_audio(audio)
                self.tt.image_autosend = image_autosend
            coro = self._agent_turn(user_text or "", self.turn_id)
        self.agent_task = asyncio.create_task(coro, name=f"agent_turn_{self.turn_id}")
        self.agent_task.add_done_callback(self._log_turn_failure)

    @staticmethod
    def _log_turn_failure(task: asyncio.Task) -> None:
        """Surface a crashed agent turn (its exception is otherwise never awaited)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("agent turn %s crashed", task.get_name(), exc_info=exc)

    async def _agent_turn(self, user_text: str, turn_id: int) -> None:
        """One LLM-driven reply: stream tokens, chunk into sentences, feed TTS."""
        await self._set_state(State.THINKING)
        had_image = self.pending_image is not None  # capture before it's consumed below
        image_msg: dict | None = None  # the image-bearing user message, for its photo note
        # If a photo is pending, send it to the multimodal LLM alongside the words as
        # a list of content parts; otherwise a plain string. Cleared once consumed —
        # and collapsed back to text after the turn (see _collapse_image_messages) so
        # the base64 isn't resent on every later turn.
        if self.pending_image:
            # Image part FIRST, then text: gpt-4o-mini reliably "looks" before it
            # answers this way. Text-first lets it reply from the conversation persona
            # ("I'm on a voice call, I can't see images") and ignore the attachment.
            # Parts order: image FIRST (so the vision model looks before answering), then the
            # customer's words, then a silent reminder to append the photo note. The reminder
            # is model INPUT only and is dropped at collapse (which keeps the FIRST text part).
            content = [{"type": "image_url", "image_url": {"url": self.pending_image}},
                       {"type": "text", "text": user_text or DEFAULT_IMAGE_PROMPT},
                       {"type": "text", "text": IMAGE_NOTE_REMINDER}]
            self.pending_image = None
            image_msg = {"role": "user", "content": content}
            self.messages.append(image_msg)
        else:
            self.messages.append({"role": "user", "content": user_text})
        # Image turns take ~1.8s for the vision model; speak a canned filler NOW to
        # cover the silence while the real reply is generated concurrently below. It is
        # TTS-only — never appended to self.messages — so it costs ZERO LLM tokens.
        # record=False keeps it out of spoken_sentences (barge-in "what was heard" memory).
        if had_image:
            self.has_filler = True
            if self.tt:
                self.tt.filler_used = True
            filler = random.choice(IMAGE_FILLER_PHRASES)
            # Emit the filler's system_utterance at the turn boundary (with its captured
            # audio), not now — its audio hasn't been synthesized yet. See _tts_rx routing.
            self._pending_sys_utt = (filler, "filler")
            await self._speak(filler, record=False)
        # Snapshot the exact request the model sees (base64 redacted inside the tracer)
        # for the turn's llm_completion child span.
        if self.tt:
            self.tt.set_request(self.messages)
            self.tt.llm_start()
        chunker = llm.SentenceChunker(MIN_SENTENCE_CHARS)
        t_llm_start = time.monotonic()
        # Raw model text accumulated across the turn, and how much of it has already been
        # released to TTS/UI. Only the portion BEFORE the [[PHOTO: ...]] sentinel is ever
        # released; the sentinel + description are captured silently as trailing generation
        # (they arrive AFTER the full spoken reply, so first-sentence TTFT is unaffected).
        raw_text = ""
        released = 0
        try:
            async for event in llm.agent_reply(self.openai, self.messages):
                if isinstance(event, llm.TextDelta):
                    if self.metrics.llm_ttft_ms is None:
                        self.metrics.llm_ttft_ms = round(
                            (time.monotonic() - t_llm_start) * 1000)
                    raw_text += event.text
                    safe_to = _speakable_prefix_len(raw_text)
                    if safe_to > released:
                        new_text = raw_text[released:safe_to]
                        released = safe_to
                        if self.tt:
                            self.tt.add_reply(new_text)
                        await self._send({"type": "agent_text_delta", "text": new_text,
                                          "turn_id": turn_id})
                        for sentence in chunker.feed(new_text):
                            await self._speak(sentence)
                    # Once the sentinel has appeared, everything after it is the silent
                    # photo note — stop releasing text to TTS entirely.
                elif isinstance(event, llm.ToolCalled):
                    if event.name == "end_call":
                        # The model decided the call is over; after the goodbye
                        # finishes flushing, _finish_agent_turn hangs up.
                        self.ending = True
                    if self.tt:
                        self.tt.add_tool(event.name, event.arguments, event.result)
                    await self._send({"type": "tool_call", "name": event.name,
                                      "args": event.arguments, "result": event.result,
                                      "turn_id": turn_id})
                elif isinstance(event, llm.Done):
                    if self.tt:
                        self.tt.llm_end(event.usage)
                    # Release any remaining non-sentinel text (recovers a legitimate lone
                    # trailing '[' that we conservatively held back and that never became
                    # a sentinel). Never releases anything at/after a real sentinel.
                    if _PHOTO_OPEN not in raw_text and released < len(raw_text):
                        tail = raw_text[released:]
                        released = len(raw_text)
                        if self.tt:
                            self.tt.add_reply(tail)
                        await self._send({"type": "agent_text_delta", "text": tail,
                                          "turn_id": turn_id})
                        for sentence in chunker.feed(tail):
                            await self._speak(sentence)
                    remainder = chunker.flush()
                    if remainder:
                        await self._speak(remainder)
            await self._send({"type": "agent_text_done", "turn_id": turn_id,
                              "interrupted": False})
            # Capture the silent photo note (if the model emitted one) and scrub the
            # sentinel from the stored assistant message so the raw marker never lingers
            # in history. The description is associated with THIS turn's image message and
            # consumed when that message is collapsed to a cheap text note.
            self._capture_photo_note(raw_text, image_msg)
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

    async def _scripted_turn(self, text: str, turn_id: int, kind: str = "greeting") -> None:
        """Speak fixed text (the greeting) through the normal TTS path."""
        await self._set_state(State.THINKING)
        self.t_turn_end = time.monotonic()  # baseline so greeting metrics aren't nonsense
        # Canned speech is traced as a system_utterance (distinct from model turns), emitted
        # with its captured audio at the turn boundary (_finish_agent_turn / _barge_in).
        self._pending_sys_utt = (text, kind)
        self.messages.append({"role": "assistant", "content": text})
        await self._send({"type": "agent_text_delta", "text": text, "turn_id": turn_id})
        await self._speak(text)
        await self._send({"type": "agent_text_done", "turn_id": turn_id, "interrupted": False})
        self.llm_done = True
        await self._maybe_finish_agent_turn()

    async def _speak(self, sentence: str, record: bool = True) -> None:
        """Send one sentence to TTS. We Flush after every sentence: deterministic
        audio delivery and per-sentence Flushed acks to track completion. (The
        tradeoff: flushing very often can clip prosody; sentence-level is fine.)

        record=False is for the image-turn filler: it still plays and still counts
        toward flush accounting, but it's excluded from spoken_sentences so an
        interrupted turn doesn't "remember" the filler as something the model said."""
        # HARD GUARD: never let a photo-note sentinel reach TTS or spoken_sentences, even
        # if the model formatted it oddly. The streaming loop already withholds the
        # sentinel; this is the last line of defense. If stripping leaves nothing speakable
        # (the chunk was pure sentinel), drop it entirely — no Speak/Flush is sent.
        sentence = _strip_sentinel(sentence)
        if not sentence.strip():
            return
        # t_first_speak marks the first REAL (recorded) sentence, so TTS TTFB measures
        # the real reply even when a non-recorded filler was spoken first.
        if record and self.t_first_speak is None:
            self.t_first_speak = time.monotonic()
        self.tts_turn = self.turn_id  # tag the socket's output as ours
        if record:
            self.spoken_sentences.append(sentence)
            # A spoken sentence of the real reply becomes a speak child of this turn.
            # (The filler is record=False; scripted turns have no self.tt.)
            if self.tt:
                self.tt.add_speak(sentence)
        self.speaks_sent += 1
        self.flushes_sent += 1
        await self.tts.speak(sentence)
        await self.tts.flush()

    async def _maybe_finish_agent_turn(self) -> None:
        """The turn is over when the LLM finished AND every flush was acknowledged."""
        if self.llm_done and self.flushed_received >= self.flushes_sent:
            await self._finish_agent_turn()

    async def _finish_agent_turn(self) -> None:
        # Idle nudge/goodbye turns aren't real conversation turns: don't log their
        # (near-empty) metrics, and don't let them reset the inactivity clock — the
        # clock must keep running so stage 1 (nudged) can progress to hang-up.
        was_idle = self.idle_turn
        self.idle_turn = False
        if not was_idle:
            await self._send_metrics()
            log.info("turn %d metrics: %s", self.metrics.turn_id, asdict(self.metrics))
        # The model has now seen any image this turn; drop the base64 from history so
        # it isn't resent on subsequent turns (keeps context — and cost — flat).
        self._collapse_image_messages()
        # Emit any canned utterance (greeting / filler / idle) now, with its audio.
        self._flush_system_utterance()
        # Emit this turn's full Weave trace tree at the boundary (off the hot path),
        # attaching the agent's spoken-reply audio captured in _tts_rx.
        if self.tt:
            self.tt.set_tts_audio(bytes(self._tts_buf))
            self._spawn_judge_and_trace(self.tt, assistant_text=self.tt.reply_text,
                                        interrupted=False, ended=self.ending,
                                        metrics=self.metrics)
            self.tt = None
        if self.ending:
            # The agent said goodbye and all of its audio is flushed. Tell the client
            # to hang up once playback drains; close ourselves as a fallback in case
            # the client never does (tab crashed, etc.).
            await self._send({"type": "call_ended", "reason": "agent_ended"})
            log.info("call ended by agent")
            self.close_task = asyncio.create_task(self._close_after(15))
            return
        await self._set_state(State.LISTENING)
        # A photo that arrived while the agent was busy (so its countdown never armed)
        # now gets its chance: start the countdown now that we're idle again.
        if self.pending_image:
            self._arm_image_autosend()
        if not was_idle:
            # A real reply (or the greeting) just finished — restart the inactivity
            # window so the caller gets the full quiet period before being nudged.
            self.t_last_activity = time.monotonic()
            self.idle_stage = 0
        # Quiet moment between turns: compress old history if it has grown too big.
        # Never runs during a turn, so it cannot add to voice-to-voice latency.
        if (self.summarize_task is None or self.summarize_task.done()) \
                and self._history_tokens() > CONTEXT_MAX_HISTORY_TOKENS:
            self.summarize_task = asyncio.create_task(self._summarize_history())

    def _spawn_judge_and_trace(self, tt, *, assistant_text: str, interrupted: bool,
                               ended: bool, metrics) -> None:
        """Hand a finished turn to a background task that (1) classifies the caller's
        vocal emotion and (2) emits the turn's Weave trace with the verdict in its
        output. Fire-and-forget: nothing on the voice path awaits it, so the judge's
        latency only delays when the trace APPEARS, never the call itself. Safe to
        detach because tt is handed off (self.tt is nulled by the caller) and a fresh
        TurnMetrics is created per turn."""
        task = asyncio.create_task(
            self._judge_and_trace(tt, assistant_text, interrupted, ended, metrics),
            name=f"judge_turn_{tt.turn_id}")
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)

    async def _judge_and_trace(self, tt, assistant_text: str, interrupted: bool,
                               ended: bool, metrics) -> None:
        vocal = None
        # Only turns with real mic audio get judged (typed/image-autosend turns have
        # none), and only when the trace will actually be emitted.
        if VOCAL_JUDGE_ENABLED and self.trace.enabled and tt.audio_pcm:
            vocal = await classify_vocal(self.openai, tt.audio_pcm)
        self.trace.finish_turn(tt, assistant_text=assistant_text,
                               interrupted=interrupted, ended=ended,
                               metrics=metrics, vocal=vocal)

    def _mark_activity(self) -> None:
        """Any real user input (speech, typing, a photo) resets the inactivity
        watchdog back to normal."""
        self.t_last_activity = time.monotonic()
        self.idle_stage = 0
        self.idle_turn = False

    def _vad_barge_in_allowed(self) -> bool:
        """Guard for whether a VAD (SpeechStarted) onset may PRE-ARM a barge-in.

        Note: a VAD onset never interrupts on its own anymore (that was the
        self-interrupt regression) — it only pre-arms, and a real transcript commits
        the interrupt. This guard therefore just suppresses noisy pre-arming during the
        echo-cancellation ramp-up at the very start of a reply; it has no effect on
        correctness (silence can't self-interrupt regardless, because no transcript
        confirms it). The primary defense remains browser echo cancellation
        (static/app.js getUserMedia). While THINKING (no agent audio yet) there is
        nothing to echo, so onset pre-arms immediately."""
        if not BARGE_IN_ON_VAD:
            return False
        if self.state is State.SPEAKING and self.t_first_speak is not None:
            elapsed_ms = (time.monotonic() - self.t_first_speak) * 1000
            return elapsed_ms >= BARGE_IN_VAD_GRACE_MS
        return True

    def _flush_system_utterance(self) -> None:
        """Emit the turn's pending canned utterance (greeting / filler / idle) as a
        system_utterance op, carrying the audio captured for it in _tts_rx. Called at
        the turn boundary (once the audio exists), so the op holds the sound."""
        if self._pending_sys_utt is None:
            return
        text, kind = self._pending_sys_utt
        self._pending_sys_utt = None
        audio = bytes(self._sys_tts_buf) if self._sys_tts_buf else None
        self._sys_tts_buf.clear()
        self.trace.system_utterance(text, kind, audio=audio)

    # ------------------------------------------------------------------ image auto-send

    def _arm_image_autosend(self) -> None:
        """(Re)start the countdown that auto-sends a lone pending photo to the LLM after
        IMAGE_AUTOSEND_S of silence. Only arms while idle; if the caller speaks (or types)
        first, the photo rides with their words instead and the timer is cancelled. The
        timer re-checks every condition before it actually fires."""
        self._cancel_image_autosend()
        if self.state is not State.LISTENING:
            return  # agent is busy; the photo waits for the next real user turn
        self._image_autosend_task = asyncio.create_task(
            self._image_autosend_timer(), name="image_autosend")

    def _cancel_image_autosend(self) -> None:
        if self._image_autosend_task and not self._image_autosend_task.done():
            self._image_autosend_task.cancel()
        self._image_autosend_task = None

    async def _image_autosend_timer(self) -> None:
        try:
            await asyncio.sleep(IMAGE_AUTOSEND_S)
        except asyncio.CancelledError:
            return
        # Clear our own handle BEFORE starting the turn so _begin_agent_turn's cancel is
        # a harmless no-op (never cancels the task it's running inside).
        self._image_autosend_task = None
        # Re-verify: the photo is still waiting, nobody is speaking, no turn in flight.
        if not self.pending_image or self.state is not State.LISTENING:
            return
        if self.agent_task and not self.agent_task.done():
            return
        log.info("photo idle %.1fs with no speech -> auto-sending it to the LLM",
                 IMAGE_AUTOSEND_S)
        self.t_turn_end = time.monotonic()  # baseline for this turn's v2v metric
        self._begin_agent_turn(user_text="", image_autosend=True)

    def _snapshot_mic(self) -> bytes | None:
        """Return (and clear) the mic PCM buffered for the turn just ended. Always
        clears so the buffer can't grow without bound; returns None when empty or
        when media capture is off. Leading silence is trimmed (keeping a 1s
        pre-roll) — the buffer rolls from the PREVIOUS turn boundary, so untrimmed
        it spans the agent's whole reply + the caller's thinking pause before any
        speech. This snapshot is observability-only (trace, Agent Lens, vocal
        judge all consume it); the live STT stream is untouched."""
        if not self._mic_buf:
            return None
        data = _trim_leading_silence(bytes(self._mic_buf))
        self._mic_buf.clear()
        return data if self.trace.capture_media else None

    async def _idle_watcher(self) -> None:
        """Hang up a call the caller has abandoned. After IDLE_PROMPT_S of silence
        (while idle), nudge once — "Are you still there?"; if still no response
        IDLE_HANGUP_S later, speak a goodbye and end the call via the same path the
        end_call tool uses. Both lines are canned TTS — zero LLM tokens."""
        while True:
            await asyncio.sleep(1)
            if self.ending or self.idle_stage == 2:
                continue                        # already ending / hung up
            if self.state is not State.LISTENING:
                continue                        # agent busy; not the caller's turn
            if self.agent_task and not self.agent_task.done():
                continue                        # a turn (incl. a nudge) is in flight
            now = time.monotonic()
            if self.idle_stage == 0 and now - self.t_last_activity >= IDLE_PROMPT_S:
                self.idle_stage = 1
                self.t_nudge = now
                self.idle_turn = True
                log.info("idle %.0fs -> nudging the caller", now - self.t_last_activity)
                self._begin_agent_turn(scripted=IDLE_PROMPT_PHRASE, kind="idle_prompt")
            elif self.idle_stage == 1 and now - self.t_nudge >= IDLE_HANGUP_S:
                self.idle_stage = 2
                self.ending = True
                self.idle_turn = True
                log.info("idle hang-up: no response after the nudge")
                self._begin_agent_turn(scripted=IDLE_GOODBYE_PHRASE, kind="idle_goodbye")

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

    def _capture_photo_note(self, raw_text: str, image_msg: dict | None) -> None:
        """After an image turn, pull the model's silent [[PHOTO: ...]] description out of
        the raw reply and associate it with this turn's image message, so the image can be
        collapsed to a content-labelled text note (see _collapse_image_messages).

        We deliberately LEAVE the raw [[PHOTO: ...]] marker on the assistant message that
        llm.agent_reply appended. It is cheap text (never the base64), it is never spoken
        (the streaming loop withholds it and _speak strips it), and keeping it gives the
        model a consistent few-shot example of its own prior output — without it, gpt-4o-mini
        reliably STOPS emitting the note from the second photo turn onward.

        No-op when the turn had no image or the model omitted the note (we then fall back
        to the generic placeholder in _collapse_image_messages)."""
        if image_msg is None:
            return
        note = _extract_photo_note(raw_text)
        if note:
            self._image_descriptions[id(image_msg)] = note

    def _collapse_image_messages(self) -> None:
        """Replace any image-bearing user message (list content) with a short text
        note. The photo is sent to the model only on the turn it arrived; keeping the
        base64 in history would resend it — and its tokens — on every later turn.

        If a content-labelled description was captured from the model's photo note
        (see _capture_photo_note), the collapsed note carries WHAT the photo showed so
        the agent remembers it as cheap text; otherwise it falls back to a generic
        placeholder so history is never left holding base64."""
        for m in self.messages:
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, list):
                text = next((p.get("text", "") for p in content
                             if isinstance(p, dict) and p.get("type") == "text"), "")
                desc = self._image_descriptions.pop(id(m), None)
                if desc:
                    m["content"] = f"[Photo the customer shared — {desc}] {text}".strip()
                else:
                    m["content"] = (text + " [customer shared a photo]").strip()

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
        self._vad_pre_armed = False  # consumed: this interrupt is now committing
        if self.metrics:
            self.metrics.barge_in = True
            await self._send_metrics()  # record the interrupted turn's partial metrics
        # Emit the interrupted turn's trace now (its task is about to be cancelled, so
        # _finish_agent_turn won't run). What was actually spoken is its assistant_text,
        # and whatever reply audio was played is attached. A canned utterance interrupted
        # mid-flight (e.g. the greeting) is still emitted with its partial audio.
        self._flush_system_utterance()
        if self.tt:
            self.tt.set_tts_audio(bytes(self._tts_buf))
            self._spawn_judge_and_trace(
                self.tt, assistant_text=" ".join(self.spoken_sentences).strip(),
                interrupted=True, ended=False, metrics=self.metrics)
            self.tt = None
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
