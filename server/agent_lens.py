"""Agent Lens emission: one conversation turn per voice turn, with audio bubbles.

The Forge Agents view (Agent Lens) reads a SEPARATE agent-spans store fed by an
OTLP pipeline — not the Weave calls store — so everything here is purely
additive: the voice_turn trace contract in weave_trace.py is untouched, and the
two views correlate because the Weave thread id (session_id) is passed as
gen_ai.conversation.id.

Each finished turn becomes one weave.log_turn() call (a parentless invoke_agent
span with chat/execute_tool children). Both sides of the exchange carry their
audio as UriPart message parts — caller mic on the user bubble, the agent's
spoken reply on the assistant bubble — so the conversation view's "Play all"
replays the entire call. Audio is published as weave:// content refs first;
publishing is synchronous network I/O, so the caller runs this whole module in
a worker thread (asyncio.to_thread) — never on the event loop.

Verified empirically (2026-11-13) on SDK 0.53.12: turns render as chat bubbles
with inline audio players; Signals monitors can score the emitted turns.
"""

from __future__ import annotations

import datetime
import logging

from .config import (AGENT_LENS_ENABLED, AGENT_NAME, AGENT_VERSION, LLM_MODEL,
                     STT_MODEL, TTS_MODEL)
from .llm import _content_text
from .weave_trace import _pcm16_to_wav

log = logging.getLogger("agent_lens")


def _audio_uri_part(pcm: bytes | None, sample_rate: int):
    """Publish one audio stream and return a UriPart for it, or None.

    Why not attach_media: the SDK serializer hardwires attachments onto the
    last USER message (_serialize_input_messages(media=...)), so the agent's
    reply audio would render in the caller's bubble. The chat view assigns
    media per-role by matching each message's uri parts against the span's
    weave.content_refs — so we publish the ref ourselves, place the UriPart
    in the correct-role message, and stamp content_refs via log_turn's
    attributes (see log_agent_turn). Returns None on failure: the turn then
    ships that bubble text-only."""
    if not pcm:
        return None
    return _media_uri_part(content=_pcm16_to_wav(pcm, sample_rate),
                           mime_type="audio/wav", modality="audio")


def _media_uri_part(*, content: bytes | str = "", uri: str = "",
                    mime_type: str = "", modality: str = ""):
    try:
        from weave.conversation.conversation import _publish_media_content
        from weave.conversation.types import UriPart
        ref = _publish_media_content(content=content, uri=uri, file_id="",
                                     mime_type=mime_type)
        return UriPart(uri=ref, mime_type=mime_type, modality=modality)
    except Exception:
        log.exception("media publish failed; emitting turn without this item")
        return None


def log_agent_turn(*, session_id: str, tt, assistant_text: str, interrupted: bool,
                   ended: bool, metrics, system_prompt: str,
                   stt_rate: int, tts_rate: int, vocal: dict | None = None) -> None:
    """Emit one voice turn to Agent Lens. Sync (network I/O) — call via
    asyncio.to_thread from the post-turn background task."""
    if not AGENT_LENS_ENABLED:
        return
    try:
        import weave
        from weave import Message
        from weave.conversation.conversation import LLM, Tool
        from weave.conversation.types import TextPart, Usage

        user_audio = _audio_uri_part(tt.audio_pcm, stt_rate)
        tts_audio = _audio_uri_part(tt.tts_pcm, tts_rate)
        # The customer's photo (image turns), published from its data URL.
        photo = _media_uri_part(uri=tt.image_data_url, modality="image") \
            if tt.image_data_url else None
        content_refs = [p.uri for p in (user_audio, tts_audio, photo)
                        if p is not None]

        def msg(role: str, text: str, *media) -> Message:
            parts = [TextPart(content=text)] + [m for m in media if m is not None]
            if len(parts) == 1:
                return Message(role=role, content=text)
            return Message(role=role, parts=parts)

        user_msg = msg("user", tt.user_text or "(no speech)", user_audio, photo)
        out_msg = msg("assistant", assistant_text, tts_audio)

        usage = tt.usage or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        # The chat span carries the FULL (already-redacted) request context the
        # model actually saw this turn — the standard agent-span shape; the chat
        # view dedupes previously-seen history so only new bubbles render. The
        # current exchange (user_msg/out_msg, with media) rides on both the turn
        # and the chat output so the bubbles keep their audio/photo.
        history = [Message(role=m["role"], content=_content_text(m.get("content")))
                   for m in (tt.req_messages or [])
                   if m.get("role") != "system" and _content_text(m.get("content"))]
        llm = LLM(
            model=LLM_MODEL,
            provider_name="openai",
            input_messages=(history[:-1] + [user_msg]) if history else [user_msg],
            output_messages=[out_msg],
            usage=Usage(input_tokens=usage.get("prompt_tokens") or 0,
                        output_tokens=usage.get("completion_tokens") or 0,
                        total_tokens=usage.get("total_tokens")),
            started_at=tt.llm_started or tt.started_at,
            ended_at=tt.llm_ended or now,
        )
        spans: list = [llm]
        # STT and TTS as their own chat-spans: the per-turn pipeline waterfall.
        # Timestamps are the honest boundaries we have — STT spans caller speech
        # through transcription (turn start -> LLM start); TTS spans first
        # sentence sent through audio fully flushed (first speak -> turn end).
        if tt.llm_started is not None:
            spans.append(LLM(model=STT_MODEL, provider_name="deepgram",
                             output_type="text", started_at=tt.started_at,
                             ended_at=tt.llm_started))
        if tt.speaks:
            spans.append(LLM(model=TTS_MODEL, provider_name="deepgram",
                             output_type="speech", started_at=tt.speaks[0][1],
                             ended_at=now))
        for name, args, result, started, ended_ts in tt.tools:
            spans.append(Tool(name=name, arguments=args, result=result,
                              started_at=started, ended_at=ended_ts))

        # Custom (non-semconv) attributes: the per-turn facts an Agent Lens user
        # would filter on. Mirrors — not replaces — the voice_turn trace fields.
        attrs = {"app.interrupted": interrupted, "app.call_ended": ended,
                 "app.turn_id": tt.turn_id, "app.filler_used": tt.filler_used,
                 "app.image_autosend": tt.image_autosend}
        if content_refs:
            # Authorizes the UI to resolve the UriParts above; per-role media
            # assignment comes from which message carries which part. No span
            # here sets this itself (we don't use attach_media), so stamping it
            # via attributes is collision-free.
            attrs["weave.content_refs"] = content_refs
        if vocal is not None:
            attrs["app.vocal_frustrated"] = bool(vocal.get("frustrated"))
            attrs["app.vocal_fearful"] = bool(vocal.get("fearful"))
        if metrics is not None:
            attrs["app.barge_in"] = metrics.barge_in
            for field in ("stt_final_ms", "llm_ttft_ms", "tts_ttfb_ms"):
                value = getattr(metrics, field, None)
                if value is not None:
                    attrs[f"app.{field}"] = value
            v2v = metrics.v2v_client_ms if metrics.v2v_client_ms is not None \
                else metrics.v2v_server_ms
            if v2v is not None:
                attrs["app.voice_to_voice_ms"] = v2v

        weave.log_turn(
            conversation_id=session_id,
            conversation_name=f"call {session_id[:8]}",
            agent_name=AGENT_NAME,
            agent_version=AGENT_VERSION,
            agent_description="HomeFix appliance repair voice support agent",
            model=LLM_MODEL,
            system_instructions=[system_prompt],
            messages=[user_msg],
            output_messages=[out_msg],
            spans=spans,
            started_at=tt.started_at,
            ended_at=now,
            attributes=attrs,
        )
    except Exception:
        log.exception("agent lens emission failed (turn %s); ignoring", tt.turn_id)
