"""Acoustic emotion judge: does the caller SOUND frustrated or fearful this turn?

One chat-completions call per turn sends the turn's mic audio (16 kHz WAV) to an
OpenAI audio model and asks for a strict-JSON verdict on vocal delivery only —
tone, pace, volume, strain — never the meaning of the words (the transcript
monitor already covers content). Called from a background task at the turn
boundary (session.py::_judge_and_trace), so it can never add voice-to-voice
latency; any failure degrades to None and the turn's trace ships without flags.

Server-side Weave monitors were tried first and cannot do this: their audio
scoring only reads audio inlined in OpenAI-style `messages` inputs, not the
weave.Audio objects our traces carry.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from openai import AsyncOpenAI

from .config import VOCAL_JUDGE_MODEL, VOCAL_JUDGE_TIMEOUT_S
from .weave_trace import _pcm16_to_wav, suppress_autotrace

log = logging.getLogger("vocal_judge")

_PROMPT = (
    "You will hear a short audio clip of a customer speaking on a support call. "
    "Judge ONLY the vocal delivery — tone, pace, volume, pitch, sighs, strain. "
    "Ignore the meaning of the words entirely; judge how the speaker SOUNDS, "
    "not what they say. Report your verdict by calling report_vocal_verdict.\n"
    "frustrated: the voice sounds agitated, exasperated, irritated or angry.\n"
    "fearful: the voice sounds anxious, panicked, shaky or afraid.\n"
    "When in doubt, answer false rather than refusing."
)

# Forced strict tool call = schema-guaranteed output. The audio models reject
# response_format (both json_schema and json_object — verified empirically), and
# with a plain text reply they sometimes soft-refuse ("I can't determine...");
# tool_choice removes the refusal path entirely.
_VERDICT_TOOL = {"type": "function", "function": {
    "name": "report_vocal_verdict",
    "description": "Report the acoustic emotion verdict for the clip.",
    "strict": True,
    "parameters": {"type": "object",
                   "properties": {"frustrated": {"type": "boolean"},
                                  "fearful": {"type": "boolean"}},
                   "required": ["frustrated", "fearful"],
                   "additionalProperties": False}}}


async def classify_vocal(client: AsyncOpenAI, pcm16k: bytes) -> dict | None:
    """Classify one turn's mic audio. Returns {"frustrated": bool, "fearful": bool},
    or None on any failure (timeout, API error, unparseable reply)."""
    b64 = base64.b64encode(_pcm16_to_wav(pcm16k)).decode()
    last = ""
    # One retry for transient API errors (bounded, still off the hot path).
    for attempt in (1, 2):
        try:
            with suppress_autotrace():  # we surface the verdict, not a judge span
                async with asyncio.timeout(VOCAL_JUDGE_TIMEOUT_S):
                    resp = await client.chat.completions.create(
                        model=VOCAL_JUDGE_MODEL,
                        modalities=["text"],
                        temperature=0,
                        tools=[_VERDICT_TOOL],
                        tool_choice={"type": "function",
                                     "function": {"name": "report_vocal_verdict"}},
                        messages=[{"role": "user", "content": [
                            {"type": "text", "text": _PROMPT},
                            {"type": "input_audio",
                             "input_audio": {"data": b64, "format": "wav"}},
                        ]}],
                    )
            msg = resp.choices[0].message
            last = msg.tool_calls[0].function.arguments if msg.tool_calls \
                else (msg.content or "")
            verdict = json.loads(last)
            return {"frustrated": bool(verdict["frustrated"]),
                    "fearful": bool(verdict["fearful"])}
        except Exception:
            if attempt == 2:
                log.exception("vocal judge failed twice (last reply %r); "
                              "emitting turn without vocal flags", last[:120])
    return None
