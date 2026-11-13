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


def _attach_audio(llm, pcm: bytes | None, sample_rate: int) -> None:
    """Attach one audio stream to the chat span via the SDK's attach_media —
    the ONLY path the conversation UI renders (it also stamps the span's
    weave.content_refs attribute, which authorizes the media fetch; UriPart
    message parts alone do NOT render — verified empirically). Failures are
    attach_media's problem: a failed upload is dropped before span emission."""
    if pcm:
        llm.attach_media(content=_pcm16_to_wav(pcm, sample_rate),
                         mime_type="audio/wav", modality="audio")


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
        from weave.conversation.types import Usage

        user_msg = Message(role="user", content=tt.user_text or "(no speech)")
        out_msg = Message(role="assistant", content=assistant_text)

        usage = tt.usage or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        llm = LLM(
            model=LLM_MODEL,
            provider_name="openai",
            input_messages=[Message(role="user", content=tt.user_text or "")],
            output_messages=[Message(role="assistant", content=assistant_text)],
            usage=Usage(input_tokens=usage.get("prompt_tokens") or 0,
                        output_tokens=usage.get("completion_tokens") or 0,
                        total_tokens=usage.get("total_tokens")),
            started_at=tt.llm_started or tt.started_at,
            ended_at=tt.llm_ended or now,
        )
        # Caller mic first, agent reply second: "Play all" then replays the
        # exchange in natural order.
        _attach_audio(llm, tt.audio_pcm, stt_rate)
        _attach_audio(llm, tt.tts_pcm, tts_rate)
        spans: list = [llm]
        for name, args, result, started, ended_ts in tt.tools:
            spans.append(Tool(name=name, arguments=str(args),
                              started_at=started, ended_at=ended_ts))

        # Custom (non-semconv) attributes: the per-turn facts an Agent Lens user
        # would filter on. Mirrors — not replaces — the voice_turn trace fields.
        attrs = {"app.interrupted": interrupted, "app.call_ended": ended,
                 "app.turn_id": tt.turn_id}
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
