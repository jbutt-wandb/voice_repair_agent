"""W&B Weave tracing: one strict, reviewable trace tree per conversation turn.

WHY THIS SHAPE
==============
A voice turn is event-driven and spread across asyncio tasks (STT finalizes in one
task, the LLM runs in another, TTS audio completes in a third). Rather than fight
that with live span wrapping, we COLLECT a turn's data as it happens and emit the
whole span tree ONCE, at the turn boundary (see session.py::_finish_agent_turn).

Two consequences make this the right call for a live agent:
  * It never touches the voice hot path. All serialization (image decode, WAV wrap)
    and all network flushing happen after the caller has already heard the reply.
  * Latency is recorded as call ATTRIBUTES (the numbers we measured), not inferred
    from span wall-clock — because we emit post-hoc, wall-clock would be meaningless.
    Weave freezes a call's attributes at creation, and the metrics aren't known until
    the turn ends, which is exactly why the tree is built at the end.

THE TRACE CONTRACT (strict — this is the whole point)
=====================================================
Session           weave.thread(session_id)         all turns of one call grouped
└─ voice_turn     root op, one per model-driven turn
   │  inputs      {turn_id, user_text, image, user_audio, tts_audio}  (media = weave | None)
   │                 user_audio = the caller's mic for this turn (16 kHz)
   │                 tts_audio  = the agent's SPOKEN REPLY for this turn (24 kHz)
   │  output      {assistant_text, tool_calls[], interrupted, ended,
   │               vocal_frustrated, vocal_fearful}   (bool | null — see vocal_judge.py)
   │  attributes  latency (ms), barge_in, idle_timeout, filler_used, image_autosend,
   │              token usage, cost
   ├─ llm_completion   inputs {messages}  — base64 image data REDACTED
   │                   output {assistant_text, usage}
   ├─ tool_<name>      inputs = parsed arguments     output = tool result
   └─ speak            inputs {text}                 one per spoken sentence of the reply

Both audio streams are kept SEPARATE (not spliced) so each side can be scrubbed on its
own in the Weave UI; the two also differ in sample rate (mic 16 kHz vs TTS 24 kHz).

Canned/system speech (greeting, image filler, idle nudge/goodbye) is NOT a voice_turn
— it is a separate root op `system_utterance` {text, kind} carrying its own `audio`
(the synthesized canned speech), so model-generated speech is never confused with
scripted speech yet ALL agent audio is still captured.

OpenAI auto-patch is DISABLED on purpose: we record the LLM call ourselves (with the
image redacted and token usage folded in) so no raw base64 is ever shipped and so the
usage lands in the turn's attributes.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import datetime
import io
import logging
import wave

from .config import (STT_SAMPLE_RATE, TTS_SAMPLE_RATE, WEAVE_CAPTURE_MEDIA,
                     WEAVE_ENABLED, WEAVE_TARGET)

log = logging.getLogger("weave_trace")

# gpt-4o-mini list price (USD per token), for a rough per-turn cost attribute.
_COST_PER_PROMPT_TOKEN = 0.15 / 1_000_000
_COST_PER_COMPLETION_TOKEN = 0.60 / 1_000_000

_client = None  # weave WeaveClient once init_weave() succeeds; None = tracing off


def init_weave() -> None:
    """Initialise Weave once at server startup. Safe to call when disabled or when
    no API key is present — tracing simply stays a no-op and the app runs normally."""
    global _client
    if not WEAVE_ENABLED:
        log.info("weave tracing disabled (WEAVE_ENABLED=0)")
        return
    import os
    if not os.environ.get("WANDB_API_KEY"):
        log.info("weave tracing disabled (no WANDB_API_KEY)")
        return
    try:
        import weave
        _client = weave.init(WEAVE_TARGET)
        # Weave auto-patches the OpenAI client on init. We don't want that duplicate
        # (unredacted) span — we record the LLM call ourselves in finish_turn. The call
        # site wraps the live request in suppress_autotrace() to stop the auto span.
        log.info("weave tracing ON -> %s/%s", _client.entity, _client.project)
    except Exception:
        log.exception("weave.init failed; continuing with tracing disabled")
        _client = None


def suppress_autotrace():
    """Stop Weave auto-tracing any third-party call (e.g. the OpenAI client) made
    inside this context. We record the LLM call ourselves, post-hoc, with the image
    redacted — so the live call must NOT also create its own (unredacted) span. This
    is version-robust: the deprecated autopatch_settings disable is ignored by the SDK,
    but tracing_disabled() reliably suppresses the auto-patch span."""
    if _client is None:
        return contextlib.nullcontext()
    from weave.trace.context import call_context
    return call_context.tracing_disabled()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _redact(obj):
    """Deep-copy-safe walk that replaces any base64 data URL with a short placeholder,
    so the recorded LLM request never carries the raw image bytes."""
    if isinstance(obj, str):
        if obj.startswith("data:") and "base64," in obj:
            return f"[image omitted: {len(obj)} base64 chars]"
        return obj
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _pcm16_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16 kHz mono int16 mic PCM as a WAV container for weave.Audio."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class TurnTrace:
    """Accumulates one turn's data as events happen. Holds raw bytes/strings only;
    all heavy encoding is deferred to SessionTracer.finish_turn (the turn boundary)."""

    def __init__(self, turn_id: int, user_text: str):
        self.turn_id = turn_id
        self.user_text = user_text
        self.image_data_url: str | None = None   # raw data URL; decoded at finish
        self.audio_pcm: bytes | None = None       # caller mic: raw 16k mono PCM
        self.tts_pcm: bytes | None = None         # agent reply: raw 24k mono PCM
        self.req_messages: list | None = None     # redacted snapshot of the LLM request
        self.usage: dict | None = None
        self._reply: list[str] = []
        self.tools: list[tuple] = []              # (name, args, result, started, ended)
        self.speaks: list[tuple] = []             # (text, started, ended)
        self.filler_used = False
        self.image_autosend = False               # turn auto-started by a lone photo
        self.started_at = _now()
        self.llm_started: datetime.datetime | None = None
        self.llm_ended: datetime.datetime | None = None

    # -- hooks called from session.py as the turn unfolds --
    def set_image(self, data_url: str) -> None:
        self.image_data_url = data_url

    def set_audio(self, pcm: bytes | None) -> None:
        self.audio_pcm = pcm

    def set_tts_audio(self, pcm: bytes | None) -> None:
        self.tts_pcm = pcm

    def set_request(self, messages: list) -> None:
        """Snapshot the exact messages sent to the LLM, with base64 stripped."""
        self.req_messages = _redact(copy.deepcopy(messages))

    def llm_start(self) -> None:
        self.llm_started = _now()

    def llm_end(self, usage: dict | None) -> None:
        self.llm_ended = _now()
        self.usage = usage

    def add_reply(self, text: str) -> None:
        self._reply.append(text)

    def add_tool(self, name: str, arguments: dict, result: dict) -> None:
        t = _now()
        self.tools.append((name, arguments, result, t, t))

    def add_speak(self, text: str) -> None:
        t = _now()
        self.speaks.append((text, t, t))

    @property
    def reply_text(self) -> str:
        return "".join(self._reply).strip()


class SessionTracer:
    """One per Session. Groups the session's calls under a Weave thread and emits a
    full span tree per turn. Every method is a guarded no-op when tracing is off, so
    session.py can call them unconditionally."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.client = _client
        self.enabled = _client is not None
        self.capture_media = WEAVE_CAPTURE_MEDIA and self.enabled

    def start_turn(self, turn_id: int, user_text: str) -> TurnTrace | None:
        return TurnTrace(turn_id, user_text) if self.enabled else None

    def finish_turn(self, tt: TurnTrace | None, *, assistant_text: str,
                    interrupted: bool, ended: bool, metrics=None,
                    vocal: dict | None = None) -> None:
        """Build and flush the whole voice_turn tree. Called at the turn boundary."""
        if not (self.enabled and tt is not None):
            return
        try:
            import weave
            with weave.thread(self.session_id):
                image_val = None
                audio_val = None
                tts_val = None
                if self.capture_media and tt.image_data_url:
                    image_val = self._decode_image(tt.image_data_url)
                if self.capture_media and tt.audio_pcm:
                    audio_val = weave.Audio(
                        _pcm16_to_wav(tt.audio_pcm, STT_SAMPLE_RATE), format="wav")
                if self.capture_media and tt.tts_pcm:
                    tts_val = weave.Audio(
                        _pcm16_to_wav(tt.tts_pcm, TTS_SAMPLE_RATE), format="wav")

                parent = self.client.create_call(
                    "voice_turn",
                    inputs={"turn_id": tt.turn_id, "user_text": tt.user_text,
                            "image": image_val, "user_audio": audio_val,
                            "tts_audio": tts_val},
                    attributes=self._attributes(metrics, tt.usage, tt.filler_used,
                                                tt.image_autosend),
                    use_stack=False,
                    started_at=tt.started_at,
                )
                # LLM child: the (redacted) request in, the reply + usage out.
                if tt.req_messages is not None:
                    lc = self.client.create_call(
                        "llm_completion", inputs={"messages": tt.req_messages},
                        parent=parent, use_stack=False,
                        started_at=tt.llm_started or tt.started_at)
                    self.client.finish_call(
                        lc, output={"assistant_text": assistant_text, "usage": tt.usage},
                        ended_at=tt.llm_ended or _now())
                # Tool children.
                for name, args, result, st, en in tt.tools:
                    tc = self.client.create_call(
                        f"tool_{name}", inputs=args if isinstance(args, dict) else {"arguments": args},
                        parent=parent, use_stack=False, started_at=st)
                    self.client.finish_call(tc, output=result, ended_at=en)
                # Speak children: one per spoken sentence of the real reply.
                for text, st, en in tt.speaks:
                    sc = self.client.create_call(
                        "speak", inputs={"text": text}, parent=parent,
                        use_stack=False, started_at=st)
                    self.client.finish_call(sc, output={"synthesized": True}, ended_at=en)

                self.client.finish_call(parent, output={
                    "assistant_text": assistant_text,
                    "tool_calls": [{"name": n, "arguments": a, "result": r}
                                   for n, a, r, _, _ in tt.tools],
                    "interrupted": interrupted,
                    "ended": ended,
                    # Acoustic verdict on the caller's mic audio (vocal_judge.py);
                    # null = no mic audio this turn, judge disabled, or judge failed.
                    "vocal_frustrated": vocal.get("frustrated") if vocal else None,
                    "vocal_fearful": vocal.get("fearful") if vocal else None,
                }, ended_at=_now())
        except Exception:
            log.exception("weave finish_turn failed (turn %s); ignoring", tt.turn_id)

    def system_utterance(self, text: str, kind: str, audio: bytes | None = None) -> None:
        """A canned, non-model utterance (greeting / filler / idle_prompt / idle_goodbye),
        recorded as its own root op so it is never mistaken for a model turn. `audio` is
        the synthesized canned speech (24 kHz mono PCM) so the trace holds ALL agent audio."""
        if not self.enabled:
            return
        try:
            import weave
            audio_val = None
            if self.capture_media and audio:
                audio_val = weave.Audio(
                    _pcm16_to_wav(audio, TTS_SAMPLE_RATE), format="wav")
            with weave.thread(self.session_id):
                c = self.client.create_call(
                    "system_utterance",
                    inputs={"text": text, "kind": kind, "audio": audio_val},
                    use_stack=False)
                self.client.finish_call(c, output={"synthesized": True})
        except Exception:
            log.exception("weave system_utterance failed; ignoring")

    # ------------------------------------------------------------------ helpers
    def _decode_image(self, data_url: str):
        import PIL.Image
        _, _, b64 = data_url.partition(",")
        raw = base64.b64decode(b64)
        return PIL.Image.open(io.BytesIO(raw)).convert("RGB")

    def _attributes(self, metrics, usage: dict | None, filler_used: bool,
                    image_autosend: bool = False) -> dict:
        attrs: dict = {"idle_timeout": False, "filler_used": filler_used,
                       "image_autosend": image_autosend}
        if metrics is not None:
            for field in ("stt_final_ms", "llm_ttft_ms", "tts_ttfb_ms",
                          "v2v_server_ms", "v2v_client_ms"):
                value = getattr(metrics, field, None)
                if value is not None:
                    attrs[field] = value
            # The headline number the caller actually experienced (client-measured if
            # the browser reported playback, else the server-side estimate).
            v2v = metrics.v2v_client_ms if metrics.v2v_client_ms is not None else metrics.v2v_server_ms
            if v2v is not None:
                attrs["voice_to_voice_ms"] = v2v
            attrs["barge_in"] = metrics.barge_in
        if usage:
            attrs["prompt_tokens"] = usage.get("prompt_tokens")
            attrs["completion_tokens"] = usage.get("completion_tokens")
            attrs["total_tokens"] = usage.get("total_tokens")
            attrs["est_cost_usd"] = round(
                (usage.get("prompt_tokens", 0) or 0) * _COST_PER_PROMPT_TOKEN
                + (usage.get("completion_tokens", 0) or 0) * _COST_PER_COMPLETION_TOKEN, 6)
        return attrs
