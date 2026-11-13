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

from .config import AGENT_LENS_ENABLED, AGENT_NAME, AGENT_VERSION, LLM_MODEL
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
    try:
        from weave.conversation.conversation import _publish_media_content
        from weave.conversation.types import UriPart
        ref = _publish_media_content(
            content=_pcm16_to_wav(pcm, sample_rate), uri="", file_id="",
            mime_type="audio/wav")
        return UriPart(uri=ref, mime_type="audio/wav", modality="audio")
    except Exception:
        log.exception("audio publish failed; emitting turn without this clip")
        return None


def log_agent_turn(*, session_id: str, tt, assistant_text: str, interrupted: bool,
                   ended: bool, metrics, system_prompt: str,
                   stt_rate: int, tts_rate: int) -> None:
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
        content_refs = [p.uri for p in (user_audio, tts_audio) if p is not None]

        def msg(role: str, text: str, audio) -> Message:
            if audio is None:
                return Message(role=role, content=text)
            return Message(role=role, parts=[TextPart(content=text), audio])

        user_msg = msg("user", tt.user_text or "(no speech)", user_audio)
        out_msg = msg("assistant", assistant_text, tts_audio)

        usage = tt.usage or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        # The chat span carries the SAME parts-based messages as the turn: the
        # chat view dedupes bubbles by (text, media) signature, so identical
        # messages collapse into one user and one assistant bubble, each with
        # its own audio, while the chat span keeps model/usage/cost metadata.
        llm = LLM(
            model=LLM_MODEL,
            provider_name="openai",
            input_messages=[user_msg],
            output_messages=[out_msg],
            usage=Usage(input_tokens=usage.get("prompt_tokens") or 0,
                        output_tokens=usage.get("completion_tokens") or 0,
                        total_tokens=usage.get("total_tokens")),
            started_at=tt.llm_started or tt.started_at,
            ended_at=tt.llm_ended or now,
        )
        spans: list = [llm]
        for name, args, result, started, ended_ts in tt.tools:
            spans.append(Tool(name=name, arguments=str(args),
                              started_at=started, ended_at=ended_ts))

        # Custom (non-semconv) attributes: the per-turn facts an Agent Lens user
        # would filter on. Mirrors — not replaces — the voice_turn trace fields.
        attrs = {"app.interrupted": interrupted, "app.call_ended": ended,
                 "app.turn_id": tt.turn_id}
        if content_refs:
            # Authorizes the UI to resolve the UriParts above; per-role media
            # assignment comes from which message carries which part. No span
            # here sets this itself (we don't use attach_media), so stamping it
            # via attributes is collision-free.
            attrs["weave.content_refs"] = content_refs
        if metrics is not None:
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
