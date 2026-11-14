# Trace shaping: one turn, two observability stores

Every finished voice turn is handed to a single background task —
`server/session.py::_judge_and_trace`, spawned fire-and-forget by
`_spawn_judge_and_trace` — which produces, in order: the acoustic verdict
(`vocal_judge.py`), the Weave `voice_turn` trace (`weave_trace.py`), and the Agent
Lens turn (`agent_lens.py`). Nothing on the voice path awaits any of it, so the
judge's latency only delays when the telemetry *appears*, never the call itself.

The two stores are genuinely independent backends: `voice_turn` goes to the Weave
**calls** store (Traces/Threads tabs), while the Agent Lens turn is an
`invoke_agent` OTel span tree shipped to a **separate agent-spans OTLP store**
(the Forge Agents view). They correlate by one shared key:
`session_id == weave.thread id == gen_ai.conversation.id`. This doc is the field
map for both shapes — what each field is, where it's set, and why it's shaped
that way.

## The Weave trace: `voice_turn`

The contract lives in `weave_trace.py`'s module docstring; `SessionTracer.finish_turn`
builds the whole tree at the turn boundary. A turn is spread across asyncio tasks, so
rather than wrap live spans, `TurnTrace` *collects* raw data as events happen and the
tree is emitted once, post-hoc — which also keeps image decode, WAV wrapping, and
network flushing off the hot path.

### Inputs

| Field | Where set | Why |
|---|---|---|
| `turn_id` | `session.py::_start_turn` → `weave_trace.py::SessionTracer.start_turn` | keys the span to the session's turn counter, matching the per-turn log lines |
| `user_text` | `SessionTracer.start_turn` | the finalized transcript (or typed text) that drove the turn |
| `image` | `TurnTrace.set_image` (raw data URL), decoded in `SessionTracer._decode_image` at finish | the customer's photo; decode deferred to the turn boundary so it never costs latency |
| `user_audio` | `TurnTrace.set_audio`, wrapped by `weave_trace.py::_pcm16_to_wav` as `weave.Audio` | caller mic for this turn, 16 kHz — you cannot debug a voice agent from text alone |
| `tts_audio` | `session.py::_finish_agent_turn` (`tt.set_tts_audio`) | the agent's spoken reply, 24 kHz. The two streams stay SEPARATE (never spliced) so each side can be scrubbed on its own in the UI — and they differ in sample rate anyway (16 vs 24 kHz) |

### Output

| Field | Where set | Why |
|---|---|---|
| `assistant_text` | `SessionTracer.finish_turn` | the full reply as spoken (on barge-in, only the sentences actually heard) |
| `tool_calls[]` | `TurnTrace.add_tool` → `finish_turn` | name/arguments/result summary alongside the `tool_*` child spans |
| `interrupted`, `ended` | `finish_turn` kwargs from `session.py` | turn outcome flags: barge-in cut the reply; the agent hung up |
| `vocal_frustrated`, `vocal_fearful` | verdict from `vocal_judge.py::classify_vocal`, folded in by `finish_turn` | `bool` or `null` (no mic audio, judge disabled, judge failed/timed out). It's part of the turn's *outcome*, produced after the turn ends — and emission is necessarily post-hoc because Weave **freezes a call's attributes at creation** while none of the turn's results exist until the end; `finish_turn` is exactly where everything is finally known |

### Attributes

All built in `SessionTracer._attributes`. Attributes rather than output because Weave
makes attributes **filterable and chartable across turns** — p95 TTFT or barge-in
rate becomes a query, not a scraper.

| Field | Where set | Why |
|---|---|---|
| `stt_final_ms`, `llm_ttft_ms`, `tts_ttfb_ms`, `v2v_server_ms`, `v2v_client_ms` | copied from `session.py::TurnMetrics` | the numbers we *measured*. Since the tree is emitted post-hoc, span wall-clock would be meaningless — so latency is recorded as the measured attributes, not inferred from span timing |
| `voice_to_voice_ms` | `_attributes` | the headline number the caller experienced: client-measured if the browser reported playback, else the server estimate |
| `barge_in`, `idle_timeout`, `filler_used`, `image_autosend` | `_attributes` (flags from `TurnTrace` / metrics) | per-turn behavior flags worth aggregating (barge-in rate is a quality signal) |
| `prompt_tokens`, `completion_tokens`, `total_tokens`, `est_cost_usd` | `_attributes`, from the LLM `usage` captured in `TurnTrace.llm_end` | cost-per-turn as a chartable column; `est_cost_usd` uses gpt-4o-mini list prices (constants at the top of `weave_trace.py`) |

### Children, and the separate root op

- **`llm_completion`** — inputs are the exact request messages, snapshotted by
  `TurnTrace.set_request` through `weave_trace.py::_redact`, which replaces any base64
  data URL with `[image omitted: N chars]`: no raw image bytes ever ship. This is also
  why the live OpenAI call runs inside `weave_trace.py::suppress_autotrace` — Weave's
  auto-patch would otherwise create a duplicate, *unredacted* span.
- **`tool_<name>`** — parsed arguments in, tool result out, one per tool call.
- **`speak`** — one per spoken sentence of the reply (`TurnTrace.add_speak`), so you
  can see exactly how the reply was chunked for TTS.
- **`system_utterance`** — canned speech (greeting, image filler, idle nudge/goodbye)
  is *not* a `voice_turn`: it bypasses the LLM, so it's a separate root op
  `{text, kind, audio}` (`SessionTracer.system_utterance`, flushed by
  `session.py::_flush_system_utterance`). Model speech is never confused with
  scripted speech, yet ALL agent audio is still captured.

## The Agent Lens turn: `invoke_agent`

One `weave.log_turn()` call per finished turn — a parentless `invoke_agent` span with
`chat` / `execute_tool` children — built in `server/agent_lens.py::log_agent_turn`.
Purely additive: it reads the same `TurnTrace`, touches nothing in the voice_turn
contract. (Background on the span model: `docs/agent_lens/research/agent-lens-product.md`
and `weave-sdk-agent-apis.md`.)

| Field | Where set | Why |
|---|---|---|
| `conversation_id` | `log_agent_turn` → `weave.log_turn(conversation_id=session_id)` | a conversation is *no span at all* — turns group purely by sharing `gen_ai.conversation.id`. Reusing the session id makes Threads (calls store) and Conversations (agent-spans store) two views of the same call |
| `agent_name` / `agent_version` / `agent_description` | `config.py` `AGENT_NAME` / `AGENT_VERSION`, passed in `log_agent_turn` | `gen_ai.agent.name` is what makes the agent *exist* as an entity in the Agents tab; version lets you segment traffic across deploys |
| user message | `log_agent_turn::msg("user", ...)` — `TextPart` + `UriPart` mic audio + `UriPart` photo | the caller's bubble, with playable audio. Each stream is published as a `weave://` content ref by `_audio_uri_part` / `_media_uri_part`, placed as a `UriPart` in the correct-role message, and authorized via `weave.content_refs` in the span attributes. `attach_media` could **not** be used: the SDK serializer hardwires attachments onto the last *user* message, so the agent's reply audio would render in the caller's bubble |
| assistant message | `msg("assistant", assistant_text, tts_audio)` | the agent's bubble with its spoken reply — with both sides carrying audio, the Conversations view's "Play all" replays the entire call |
| photo | `_media_uri_part(uri=tt.image_data_url, modality="image")` | the customer's photo as an image content ref on the user bubble |
| `chat` span | `log_agent_turn` — `LLM(model, provider_name="openai", input_messages=history[:-1] + [user_msg], ...)` | carries the FULL (already-redacted, via `TurnTrace.req_messages`) request context the model actually saw — the standard agent-span shape; the chat view dedupes previously-seen history so only new bubbles render. `Usage(input/output/total_tokens)` feeds the native token and cost columns |
| STT stage span | `LLM(model=STT_MODEL ("nova-3"), provider_name="deepgram", output_type="text")`, `tt.started_at → tt.llm_started` | the pipeline waterfall. These are the honest boundaries we have — but note the caveat: the span covers caller speech through transcription, so its duration includes the caller *speaking*, not pure STT latency (the real number is `app.stt_final_ms`) |
| TTS stage span | `LLM(model=TTS_MODEL ("aura-2-thalia-en"), provider_name="deepgram", output_type="speech")`, first `speak` → turn end | `output_type="speech"` marks the audio modality; the span runs from the first sentence sent until the audio is fully flushed |
| Tool spans | `Tool(name=..., arguments=..., result=...)` per `tt.tools` entry | become `execute_tool` spans; arguments/result land JSON-encoded in `gen_ai.tool.call.arguments` / `.result` |
| `app.*` attributes | the `attrs` dict in `log_agent_turn` | `app.turn_id`, `app.interrupted`, `app.call_ended`, `app.filler_used`, `app.image_autosend`, `app.barge_in`, `app.stt_final_ms`, `app.llm_ttft_ms`, `app.tts_ttfb_ms`, `app.voice_to_voice_ms`, `app.vocal_frustrated`, `app.vocal_fearful`. Custom attrs because the GenAI semconv has no slot for voice-pipeline latencies; they arrive typed and show up as filterable columns in the Spans tab. They *mirror* — never replace — the voice_turn fields |

## Rules learned the hard way

Empirically verified on weave 0.53.12 (docstrings in `agent_lens.py` record the facts):

- **A `UriPart` alone does not render.** The chat view resolves a message's uri parts
  against the span's `weave.content_refs`; publish the ref AND stamp it via `log_turn`
  attributes (`agent_lens.py::_audio_uri_part`, the `content_refs` stamping in
  `log_agent_turn`). Since no span sets `weave.content_refs` itself (we skip
  `attach_media`), stamping it through attributes is collision-free.
- **`attach_media` is input-side only** — it pins media to the last user message, so
  per-role audio (reply audio on the assistant bubble) is impossible through it.
- **`_publish_media_content` requires all its kwargs** — `content`, `uri`, `file_id`,
  `mime_type` — even the empty ones (`agent_lens.py::_media_uri_part`).
- **Failures degrade, never break.** A failed media publish returns `None` and the
  bubble ships text-only; the whole emission is wrapped so a failed turn logs and is
  dropped (`log_agent_turn`'s outer `try/except`). Same spirit as the vocal judge's
  `null` flags: observability must never cost a turn.
- **Everything runs via `asyncio.to_thread`** (`session.py::_judge_and_trace`):
  content-ref publishing is synchronous network I/O, so it must never run on the
  event loop that is carrying live calls.

## Starting from scratch: how to shape spans for an app like this

If you're instrumenting a new event-driven voice/realtime agent, the generalized
version of what this codebase learned:

1. **Collect, then emit at the turn boundary.** STT finalizes in one task, the LLM
   streams in another, TTS acks in a third — don't fight that with live span
   wrapping. Accumulate the turn's data in a plain object
   (`weave_trace.py::TurnTrace`) and build the whole tree post-hoc. The consequence:
   wall-clock on your spans is meaningless unless you carry your own timestamps — so
   carry them (`tt.llm_started`/`llm_ended`, the per-`speak` timestamps).
2. **Nothing on the hot path.** One fire-and-forget background task per turn does
   judge → trace → agent-span emission (`session.py::_judge_and_trace`); sync network
   I/O goes through `asyncio.to_thread`. And teardown must *await* in-flight
   emissions before the cancel sweep, or the final turn's telemetry is lost
   (`session.py::run`'s `finally` waits on `_judge_tasks`, bounded).
3. **Pick your correlation id on day one.** Here one `session_id` serves as the Weave
   thread, the Agent Lens `conversation_id`, and the latency-log filename.
   Retrofitting correlation across stores later is painful.
4. **Scalars you'll want to chart go in attributes; narrative goes in
   inputs/outputs.** Latencies and flags as (custom) attributes
   (`SessionTracer._attributes`, the `app.*` dict in `log_agent_turn`) because both
   stores make attributes filterable and aggregable; transcripts and media as message
   content, because that's what the UIs render.
5. **Media as published refs, never inline blobs** — and learn your renderer's
   resolution rule *before* deciding where media lives. The `weave.content_refs`
   lesson above cost a day of invisible audio.
6. **Redact at the source, not the sink.** Strip base64 before anything is emitted
   (`weave_trace.py::_redact` + `suppress_autotrace`), so no store ever holds the raw
   bytes and there's nothing to scrub later.
7. **Observability must degrade, never break the product.** Every emission path is
   wrapped so a failure costs a log line and a `null`, not a turn
   (`vocal_judge.py::classify_vocal`, `agent_lens.py::log_agent_turn`).
8. **Give the two stores different jobs.** The call trace is the forensic per-turn
   tree for debugging one call; agent spans are product analytics and chat-style
   review across calls. Don't contort one store into doing both jobs.
9. **Name and version the agent from the start** (`AGENT_NAME`/`AGENT_VERSION` in
   `config.py`) — the Agents tab entity and any cross-version comparison key off
   them, and backfilling identity onto old spans isn't possible.

Where to look in the UI: [doc 06](06-weights-and-biases.md) covers both the
Traces/Threads tabs (calls store) and the Agents view tabs (agent-spans store).
