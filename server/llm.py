"""LLM stage: OpenAI streaming, tool-call handling, and sentence chunking.

The contract with the orchestrator (session.py) is a flat async stream of events:

    TextDelta(text)                      — a few characters of the agent's reply
    ToolCalled(name, arguments, result)  — a booking was executed mid-turn
    Done()                               — the turn's text is complete

Why streaming matters here: a voice agent cannot wait for the full completion.
We forward text to TTS sentence-by-sentence as tokens arrive, so the user hears
the first sentence while the model is still writing the third.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from . import agent
from .config import LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE
from .weave_trace import suppress_autotrace


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCalled:
    name: str
    arguments: dict
    result: dict


@dataclass
class Done:
    # Token usage summed across the turn's LLM pass(es). None if the provider
    # didn't return it. Consumed by the observability layer (see weave_trace).
    usage: dict | None = None


async def agent_reply(client: AsyncOpenAI, messages: list[dict], *,
                      temperature: float = LLM_TEMPERATURE):
    """Run one agent turn. Mutates `messages` in place (appends the assistant
    reply, and tool-call/tool-result messages when a booking happens).

    This is the standard two-pass tool loop, written out explicitly:
      pass 1: model streams text and/or a tool call
      if it called a tool: execute it, append the result, and
      pass 2: model streams the spoken confirmation that references the result.
    The `while` handles the (rare) case of the model chaining more than one call.
    """
    # Accumulate token usage across every pass of the tool loop (one usage chunk
    # arrives per stream when include_usage is set) so the turn reports a single total.
    usage_total: dict | None = None
    while True:
        # suppress_autotrace: Weave records this LLM call via our own redacted span
        # (see weave_trace.finish_turn), so the client's auto-patch span is suppressed
        # here to avoid a duplicate that would also carry the raw image base64.
        with suppress_autotrace():
            stream = await client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=agent.TOOLS,
                temperature=temperature,
                max_tokens=LLM_MAX_TOKENS,
                stream=True,
                stream_options={"include_usage": True},
            )
            content_parts: list[str] = []
            # Tool-call arguments arrive as JSON *fragments* spread across many deltas,
            # keyed by index; they must be accumulated before they can be parsed.
            tool_calls: dict[int, dict] = {}
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        # Final (usage) chunk carries no choices; fold it in and move on.
                        usage_total = _add_usage(usage_total, chunk.usage)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content_parts.append(delta.content)
                        yield TextDelta(delta.content)
                    for tc in delta.tool_calls or []:
                        acc = tool_calls.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function and tc.function.name:
                            acc["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            acc["arguments"] += tc.function.arguments
            finally:
                # On barge-in the session cancels us mid-stream; closing the HTTP
                # stream stops OpenAI generating (and billing) further tokens. Guard the
                # close so a cleanup hiccup can't mask the real error that unwound us
                # here (e.g. a consumer exception thrown in at a yield point); on normal
                # completion the stream is already closed and close() returns None.
                closer = stream.close()
                if closer is not None:
                    await closer

        content = "".join(content_parts)
        if not tool_calls:
            messages.append({"role": "assistant", "content": content})
            break

        # Record the assistant turn that requested the tool(s)...
        messages.append({
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {"id": acc["id"], "type": "function",
                 "function": {"name": acc["name"], "arguments": acc["arguments"]}}
                for acc in tool_calls.values()
            ],
        })
        # ...execute each one and feed the result back for the second pass.
        for acc in tool_calls.values():
            result = agent.execute_tool(acc["name"], acc["arguments"])
            yield ToolCalled(name=acc["name"],
                             arguments=json.loads(acc["arguments"] or "{}"),
                             result=result)
            messages.append({
                "role": "tool",
                "tool_call_id": acc["id"],
                "content": json.dumps(result),
            })

    yield Done(usage=usage_total)


def _add_usage(total: dict | None, usage) -> dict:
    """Fold one OpenAI usage object into the running per-turn total."""
    total = total or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for field in total:
        total[field] += getattr(usage, field, 0) or 0
    return total


SUMMARIZER_PROMPT = """\
You compress the transcript of an in-progress appliance-repair support call into short
CALL NOTES for the agent handling the rest of the call. Preserve every fact that could
matter later; drop all chit-chat and wording. Always keep:
- the product and the reported symptoms
- every troubleshooting step already tried and its outcome (so nothing is repeated)
- anything the customer was asked to check and their answers (power, lights, error codes)
- anything shown in a photo the customer shared (these appear as "[Photo the customer
  shared — ...]" notes in the transcript); keep the described details verbatim enough that
  the agent still knows what the photo showed
- any safety refusals already given
- booking details if an engineer was booked (date, time window, reference)
- customer preferences, constraints, names, dates
Write terse bullet-like prose, max ~150 words. Output only the notes."""


def _content_text(content) -> str:
    """Flatten a message's content to text. Content is usually a string, but an
    image-bearing user turn is a list of parts ({type:text|image_url}); keep only
    the text so the transcript never serializes a raw list / base64 blob."""
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text").strip()
    return content or ""


async def summarize_history(client: AsyncOpenAI, old_messages: list[dict]) -> str:
    """Compress old conversation turns into compact call notes. Runs BETWEEN agent
    turns (never on the latency path) — see session.py::_maybe_summarize."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {_content_text(m.get('content')) or '[tool call]'}"
        for m in old_messages if m.get("role") in ("user", "assistant", "tool")
    )
    resp = await client.chat.completions.create(
        model=LLM_MODEL,
        temperature=0,
        max_tokens=250,
        messages=[{"role": "system", "content": SUMMARIZER_PROMPT},
                  {"role": "user", "content": transcript}],
    )
    return resp.choices[0].message.content.strip()


# Sentence boundary: ., !, or ? followed by whitespace or end-of-buffer.
# Deliberately naive — good enough for spoken prose; a production system would
# guard against abbreviations ("e.g.") and decimals ("1.5 litres").
_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


class SentenceChunker:
    """Buffers streamed text deltas and emits speakable sentences.

    Why sentences? Deepgram's TTS websocket synthesizes whatever text it has
    been sent when it receives a Flush. Flushing per token would sound robotic
    (prosody needs a full phrase); waiting for the whole reply wastes the
    streaming advantage. Sentence boundaries are the sweet spot.
    """

    def __init__(self, min_chars: int):
        self.min_chars = min_chars
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        """Add a delta; return any complete chunks ready to speak. A chunk ends at
        the earliest sentence boundary past min_chars — so a very short sentence
        ("Yes.") rides along with the one after it rather than being spoken alone."""
        self._buf += text
        out: list[str] = []
        while True:
            emit_at = next((m for m in _BOUNDARY.finditer(self._buf)
                            if m.start() >= self.min_chars), None)
            if emit_at is None:
                break
            out.append(self._buf[: emit_at.start()].strip())
            self._buf = self._buf[emit_at.end():]
        return out

    def flush(self) -> str | None:
        """Return whatever is left (called when the LLM stream finishes)."""
        rest, self._buf = self._buf.strip(), ""
        return rest or None
