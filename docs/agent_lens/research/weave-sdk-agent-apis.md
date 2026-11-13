# Weave SDK: Agent / Conversation-Turn Logging APIs

Researched against local install `weave==0.53.12` (`.venv/lib/python3.14/site-packages/weave/`) and GitHub `wandb/weave` master. Latest release: **0.54.1** (2026-11-05) — 0.54.x adds **no new conversation APIs**; it removes `weave.shared` / `weave.trace_server` aliases (breaking, #8061) and fixes OpenAI-Agents tool call-id recording (#8080). 0.53.13 had one relevant fix: "emit current OTel GenAI attribute names" (#8045). Everything below exists in our 0.53.12.

## The Conversation SDK (`weave.conversation`, re-exported at top-level `weave.*`)

Source: `weave/conversation/conversation.py` (1879 lines), attribute builders in `conversation_otel.py`, types in `types.py`. Two usage styles, byte-identical output:

**Streaming (context managers / contextvars):**
- `weave.start_conversation(*, agent_name="", model="", conversation_id="", conversation_name="", include_content=True, continue_parent_trace=False, attributes=None) -> Conversation`
- `conversation.start_turn(*, user_message="", model="", agent_name="", agent_id="", agent_description="", agent_version="", system_instructions=None) -> Turn` (auto-ends previous turn)
- `weave.start_llm(*, model="", provider_name="", system_instructions=None) -> LLM`; `weave.start_tool(*, name, arguments="", tool_call_id="") -> Tool`; `weave.start_subagent(*, name, model="", system_instructions=None) -> SubAgent`
- `end_turn() / end_llm() / end_conversation()`, `get_current_conversation() / get_current_turn() / get_current_llm()`; all span classes are also `with`-able context managers. `LLM.record(...)` takes output messages, `Usage`, finish_reasons, request params; `attach_media` publishes media as `weave://` Content refs.

**Batch / imperative (for callbacks, queue workers — our voice-agent case):**
```python
weave.log_turn(*, conversation_id: str, agent_name="", conversation_name="", model="",
    agent_id="", agent_description="", agent_version="",
    messages: list[Message] | None = None,          # turn input
    output_messages: list[Message] | None = None,   # terminal agent response
    system_instructions: list[str] | None = None,
    spans: list[LLM | Tool | SubAgent] | None = None,   # children with started_at/ended_at
    started_at: datetime | None = None, ended_at: datetime | None = None,
    include_content=True, continue_parent_trace=False, attributes=None) -> LogResult
```
```python
weave.log_conversation(*, turns: list[Turn], conversation_id="", conversation_name="",
    agent_name="", model="", agent_id="", agent_description="", agent_version="",
    include_content=True, continue_parent_trace=False, attributes=None) -> LogResult
```
- Timestamps: explicit `started_at/ended_at` win; else min/max of child spans; else `now()`. Child OTel span times come from each child's `started_at/ended_at` (`conversation.py:1628,1745`).
- `LogResult` carries `conversation_id`, W3C `trace_ids` / `root_span_ids`, `span_count`.
- `Message(role, content)` flat form, or `parts=[TextPart, ToolCallPart, ToolCallResponsePart, ReasoningPart, BlobPart, UriPart, FilePart]` (import from `weave.conversation`). `Usage(input_tokens, output_tokens, total_tokens, reasoning_tokens, cache_*_input_tokens)`.
- Deprecated aliases: `start_session/log_session/end_session`, `Session` (`conversation/_deprecated.py`).
- Verified against GitHub test `tests/conversation/test_conversation_otel.py::TestLogTurn`: one `invoke_agent <agent_name>` root span; `chat <model>` and `execute_tool <name>` children share its trace_id/parent.

## OTEL gen_ai attributes stamped (`conversation/conversation_otel.py`)

- Turn (`invoke_agent` span): `gen_ai.operation.name="invoke_agent"`, `gen_ai.agent.name/.id/.description/.version`, `gen_ai.conversation.id`, `gen_ai.conversation.name`, `gen_ai.request.model`, `gen_ai.system_instructions` (JSON TextPart array), `gen_ai.input.messages` / `gen_ai.output.messages` (JSON, semconv parts model `{role, parts:[...]}`).
- LLM (`chat` span): adds `gen_ai.provider.name`, `gen_ai.response.id/.model`, `gen_ai.response.finish_reasons`, `gen_ai.request.temperature/max_tokens/top_p/...`, `gen_ai.usage.input_tokens/output_tokens/total_tokens/reasoning_tokens/cache_*`, `weave.content_refs` for media.
- Tool (`execute_tool` span): `gen_ai.tool.name/.call.id/.call.arguments/.call.result/.type/.description/.definitions`.

## Transport and what populates the Agents view

- `weave.init()` calls `_setup_conversation_tracing()` (`trace/weave_init.py:177-283`): a dedicated OTel `TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter)` pointed at **`<trace-server>/agents/otel/v1/traces`** (`trace/urls.py: otel_traces_endpoint`). This is a separate ingestion path from calls: server-side `genai_otel_export` (`trace_server/clickhouse_trace_server_batched.py:7407`) writes a dedicated agent-spans ClickHouse table (`trace_server/agents/clickhouse.py: AgentWriteHandler.insert_otel_spans`) that backs the **Agents view** (`agent_spans_query`, chat view, stats, insights).
- **`weave.genai.turn_ended` is not emitted by the SDK.** It is a server-side synthetic op name: on ingest, any finished agent span with **no parent_span_id** (i.e., a turn root) produces a `"weave.genai.turn_ended"` Kafka event (`trace_server/agents/kafka_events.py:50-73`). `weave.Monitor(op_names=["weave.genai.turn_ended"], ...)` (`flow/monitor.py:22`) is how you attach scorers to turns. So: emit turns → server mints turn_ended events → monitors can score them.
- Relation to threads: `weave.thread()` stamps `thread_id`/`turn_id` on **calls** (Traces/Threads view) — a *parallel* store. The Agents view does not read calls. The generic OTLP→calls endpoint does map `gen_ai.conversation.id` → call `thread_id` (`trace_server/opentelemetry/constants.py:169-172`), but the conversation SDK exports to the agents endpoint only. To correlate the two, reuse the same ID: pass our Weave `thread_id` as `conversation_id` to `log_turn`/`start_conversation`.

## `weave/integrations/openai_agents/` (confirmed present locally)

- Two processors (`patcher.py`): legacy `WeaveTracingProcessor` (`openai_agents.py`) creates **Weave calls**; `WeaveOtelTracingProcessor` (`otel_processor.py`) emits **OTel GenAI spans via the Conversation SDK's attribute builders** — this is what populates the Agents view. Selected by `WEAVE_USE_OTEL_V2`, **default True** (`trace/settings.py:308`).
- Span tree it produces: `invoke_agent` for AgentSpan (one agent_start event per real agent, so handoffs/multi-agent show correctly); `chat` for ResponseSpan/GenerationSpan; `execute_tool` for FunctionSpan; TaskSpan (Runner.run) and TurnSpan (loop iteration) become *structural* spans with no `gen_ai.operation.name`; Handoff/Guardrail/Transcription/Speech/MCPListTools/Custom get descriptive names + `weave.openai_agents.*` attrs. One conversation per OpenAI trace (`gen_ai.conversation.id`).
- Siblings using the same pattern: `integrations/claude_agent_sdk/otel_integration.py` (one `invoke_agent` turn per query/response) and `integrations/openai_realtime/otel_state_exporter.py` (speech-to-speech: `invoke_agent` session root, one `chat` per `response.done`, `execute_tool` per function call — directly analogous to our voice-agent shape).
- `weave.conversation.agent_name_override(name)` context manager relabels `invoke_agent` spans emitted by integrations.

## Paths to populate the Agents view from custom code (ranked)

1. **`weave.log_turn(...)` per voice turn** — best fit. Off the hot path (build `LLM`/`Tool` child objects with recorded timestamps after the turn completes; BatchSpanProcessor exports in background). Pass our existing `thread_id` as `conversation_id` so Threads and Agents views correlate. No contextvar discipline required in async callback code.
2. **Streaming `start_conversation()` / `conversation.start_turn()` / `start_llm()`** — richer live view (in-progress spans, media via `attach_media`), but requires contextvar/context-manager hygiene across the session's async tasks; `end()` on the hot path does PII redaction + attribute serialization, so measure before adopting.
3. **`weave.log_conversation(turns=[...])`** — batch-replay an entire session at hang-up; simplest, but no per-turn live visibility.
4. **Raw OTel spans to `<trace-server>/agents/otel/v1/traces`** with hand-stamped `gen_ai.*` attributes — works (server extracts by semconv), but reimplements what `log_turn` already does; use only from non-Python components.
5. **Adopt an auto-instrumented SDK** (OpenAI Agents / Realtime, Claude Agent SDK, Google ADK) — zero code, but requires moving off the current Deepgram/cascade architecture; not applicable here.
