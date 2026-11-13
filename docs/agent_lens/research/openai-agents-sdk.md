# OpenAI Agents SDK (`openai-agents`, Python) — research notes

Assessment of whether the per-turn LLM step in `server/llm.py::agent_reply` could be
replaced by the SDK's `Runner`, and what that buys / costs us.
Sources: https://openai.github.io/openai-agents-python/ (agents, running_agents,
streaming, sessions, tools, models, tracing, voice/pipeline, ref/result, usage),
https://docs.coreweave.com/weave/guides/integrations/openai_agents/,
https://docs.coreweave.com/products/agent-lens/integrations/openai-agents-sdk.

## 1. Core model

- `Agent(name, instructions, tools, model, model_settings)` — instructions = our system
  prompt; tools declared with `@function_tool` (JSON schema auto-generated from type
  hints + docstring via pydantic/griffe; sync or async; `failure_error_function` controls
  what the model sees on tool errors). Our `book_engineer_visit` / `end_call` map directly.
- `Runner.run(agent, input)` / `run_sync` / `run_streamed` execute the agent loop: call
  LLM -> if tool calls, execute tools, append results, loop -> until a plain text final
  output (or `MaxTurnsExceeded` at `max_turns`). This is exactly the two-pass tool loop
  we hand-wrote in `agent_reply`, including multi-call chaining.
- `RunResult`: `final_output`, `new_items`, `raw_responses`, and `to_input_list()` which
  returns the run as input items for the next turn — the "manual message list" mode.
  Token usage per run: `result.context_wrapper.usage` (requests/input/output/total).
- Sessions (`SQLiteSession`, `AsyncSQLiteSession`, `RedisSession`, `SQLAlchemySession`,
  `OpenAIConversationsSession`, custom via a 4-method `Session` protocol: `get_items` /
  `add_items` / `pop_item` / `clear_session`) auto-prepend history before a run and
  persist new items after. Alternative, not a requirement — `to_input_list()` or a raw
  item list works fine, which matters because our history is a mutable list that a
  background task rewrites (summarization). A custom `Session` over our list is possible
  but adds nothing.
- Models: the SDK defaults to the **Responses API** (`OpenAIResponsesModel`);
  `OpenAIChatCompletionsModel` / `set_default_openai_api("chat_completions")` keeps us on
  chat.completions. `ModelSettings(temperature=…, max_tokens=…)` covers our two knobs.
  Multimodal input is supported: user message content as items with
  `{"type": "input_image", "image_url": "data:image/jpeg;base64,…"}` (Responses-style
  item format, not chat.completions `image_url` parts — history format changes).

## 2. Streaming and cancellation

- `Runner.run_streamed()` returns `RunResultStreaming`; `stream_events()` yields:
  - `raw_response_event` wrapping raw model events — token deltas arrive as
    `ResponseTextDeltaEvent` (`event.data.delta`). Same granularity as our current
    chunk loop, so sentence-split TTS on first-sentence-complete still works; TTFT is
    one thin event-translation layer away from the raw stream.
  - `run_item_stream_event` — semantic items (`tool_called`, `tool_output`,
    `message_output_created`); maps to our `ToolCalled` event.
  - `agent_updated_stream_event` — handoffs only, irrelevant here.
- Cancellation: `result.cancel()` (immediate: stops tasks, clears queues) or
  `cancel(mode="after_turn")`. Barge-in maps to `cancel()` — but note our current code
  cancels the *asyncio task* and relies on `finally: stream.close()` to stop OpenAI
  billing; with the SDK we'd call `result.cancel()` instead and must verify it closes the
  underlying HTTP stream promptly (worth a test; not documented explicitly).
- Caveat: tool execution happens *inside* the run loop. Our `ToolCalled` yield today is
  emitted between execution and the second pass from the same generator; under the SDK
  we'd reconstruct it from `run_item_stream_event`s — ordering relative to text deltas is
  preserved in the event stream.

## 3. Tracing

- Built-in trace model: a `trace` (workflow) containing spans — `agent_span` per agent
  execution, `generation_span` per LLM call (inputs/outputs), `function_span` per tool
  call, `handoff_span`, `guardrail_span`, plus voice spans (`transcription_span`,
  `speech_span`, `speech_group_span`). Default export: OpenAI Traces dashboard via a
  background `BatchTraceProcessor` (off-hot-path batching).
- Processor API: `add_trace_processor(p)` adds alongside the OpenAI exporter;
  `set_trace_processors([p])` replaces it (use this to avoid shipping traces to OpenAI).
  Disable entirely with `OPENAI_AGENTS_DISABLE_TRACING=1` / `RunConfig.tracing_disabled`.
- Weave integration: `weave.init(...)` then
  `from weave.integrations.openai_agents.openai_agents import WeaveTracingProcessor`,
  `set_trace_processors([WeaveTracingProcessor()])`. Each `Runner.run` becomes its own
  Weave trace: an `invoke_agent` span with nested `chat` and `execute_tool` children,
  carrying input, model, output, token usage, tool results. A shared `group_id` in
  `RunConfig` groups turns into one conversation. Agent Lens reads these same traces
  (the Forge SDK does not support this framework yet, per CoreWeave docs).
- Collision with our contract: this produces a *second, parallel* trace shape — it is
  NOT our `voice_turn` tree, does not nest under it (our tree is emitted post-hoc at
  turn end, the SDK's spans are live), and would re-introduce the raw base64 image into
  trace inputs that `weave_trace.py` deliberately redacts. If adopted, SDK tracing would
  have to be disabled (or processor-filtered) and our `voice_turn` emission kept as-is.

## 4. Voice pipeline

- `VoicePipeline` (voice extra) = STT -> `SingleAgentVoiceWorkflow` -> TTS, with
  `AudioInput` (push-to-talk) or `StreamedAudioInput` (VAD turn detection). It is built
  around `OpenAIVoiceModelProvider` (OpenAI STT/TTS); `STTModel`/`TTSModel` are nominally
  pluggable interfaces but no third-party providers are documented, and crucially the
  docs state it "currently does not provide any built-in interruption handling" for
  streamed input. Our Deepgram nova-3/aura-2 duplex streams + barge-in state machine are
  strictly more capable. **Verdict: irrelevant to us** — if anything we'd use only the
  Runner layer and keep our own audio plumbing.

## Fit assessment

**Maps cleanly**
- The two tools -> `@function_tool`; system prompt -> `instructions`;
  temperature/max_tokens -> `ModelSettings`; model -> `OpenAIChatCompletionsModel` or
  `set_default_openai_api("chat_completions")` (stay off Responses if desired).
- `agent_reply`'s event contract can be preserved: wrap `run_streamed()` and translate
  `ResponseTextDeltaEvent` -> `TextDelta`, tool run-items -> `ToolCalled`, completion ->
  `Done(usage=context_wrapper.usage)`. session.py and the sentence chunker untouched.
- Barge-in -> `result.cancel()`.

**Risky / costs**
- Latency: extra layers (item building, event queueing, tracing hooks) sit between the
  model delta and our chunker. Probably small, but unmeasured — would need a TTFT A/B
  before merging (hard rule: no v2v regression).
- History format migration: `to_input_list()` items are Responses-style, not our plain
  chat.completions dicts — summarization (`_content_text`), image attachment, and the
  in-place `messages` mutation in `agent_reply` all assume the current shape.
- Tracing double-vision: `WeaveTracingProcessor` emits its own `invoke_agent` trace
  shape, unredacted, outside our `voice_turn` contract. We'd run with SDK tracing
  disabled, gaining *no* observability benefit from adoption — the main selling point.
- Cancellation semantics (does `cancel()` close the HTTP stream as fast as our
  `stream.close()`?) and streamed usage reporting on chat.completions
  (`ModelSettings(include_usage=True)` may be needed) are unverified.

**Open questions**
- Measured TTFT overhead of `run_streamed` vs raw `chat.completions.create(stream=True)`.
- Can `WeaveTracingProcessor` spans be re-parented/redacted to live *inside* our
  `voice_turn` tree, or filtered per-span? (Nothing in the docs suggests so.)
- Does `cancel()` immediately abort the in-flight HTTP response (billing stop)?

**Bottom line**: adoption is feasible behind the existing `agent_reply` contract, but
today it trades our ~90-line explicit tool loop for a dependency whose headline benefit
(standard agent spans) conflicts with the frozen `voice_turn` Weave contract. Revisit if
we add handoffs/guardrails/multi-agent, where the SDK starts paying for itself.
