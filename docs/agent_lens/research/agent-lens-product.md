# CoreWeave Forge "Agent Lens" and the Weave Agents view

Research notes, 2026-11-13. Sources: docs.coreweave.com (mirrors docs.wandb.ai/weave), CoreWeave blog, and the installed `weave==0.53.12` SDK source (which ships the trace-server agent code and is authoritative on wire shapes).

## What Agent Lens is

- Announced 2026-09-30 as part of **CoreWeave Forge**: an agent-observability + continuous-improvement product that ingests agents' OpenTelemetry trace data at scale and layers automatic failure detection, intent clustering, custom LLM judges, and failure-to-test-case conversion on top ([blog](https://www.coreweave.com/blog/automate-agent-observability-and-improvement-with-coreweave-agent-lens), [Forge launch](https://www.coreweave.com/blog/coreweave-forge-turn-ai-iteration-into-compounding-improvement)).
- Public preview on hosted cloud; "Insights and online LLM judges are free until the end of 2026." Dedicated/on-prem later.
- Agent Lens docs ([docs.coreweave.com/products/agent-lens/](https://docs.coreweave.com/products/agent-lens/insights)) and the Weave "Agents view" docs ([docs.coreweave.com/products/wandb/weave/guides/tracking/view-agent-activity](https://docs.coreweave.com/products/wandb/weave/guides/tracking/view-agent-activity)) describe the **same surface**: a GenAI-span store separate from classic Weave calls, with agent/conversation/turn semantics. Internally the SDK calls it the "GenAI agent observability system" (`weave/shared/agents/`, `weave/trace_server/agents/`).

## The Agents view (UI)

- Five tabs: **Dashboard, Agents, Conversations, Spans, Signals** ([view-agent-activity](https://docs.coreweave.com/products/wandb/weave/guides/tracking/view-agent-activity)).
- **Conversations render as chat-style turn-by-turn transcripts**, not trace trees: a detail panel with **Turns** and **Events** sub-panels; per-turn counts of intermediate responses/tool calls and wall-clock duration; messages grouped by role; assistant bubbles show agent name + model; an Events timeline color-codes user/assistant/tool activity. Chat view caps at 50 turns (`MAX_CONVERSATION_CHAT_TURNS`) and 200 spans per conversation minimap (`weave/shared/agents/constants.py`).
- **Media in messages is supported**: "User messages show the message text and any attached media or content references." Server-side (`weave/trace_server/agents/chat_view.py`), message `content` may be a JSON **parts array**; parts of type `uri` / `blob` / `file` (`_MEDIA_PART_TYPES`) are resolved against the span's `weave.content_refs` (`weave:///entity/project/...` object refs) and rendered per message bubble — code comments explicitly handle "every turn's **audio**" staying on its own message. `gen_ai.output.type` admits `"speech"`. So audio attachments are a designed-for case, delivered as uri parts + uploaded content refs rather than inline base64.

## Ways to populate it

([agent-integration-quickstart](https://docs.coreweave.com/products/wandb/weave/agent-integration-quickstart))
1. **Harness plugins**: Claude Code (github.com/wandb/weave-claude-code), Codex, OpenClaw, Pi — emit GenAI OTel spans to the backend.
2. **Agent SDK autopatch**: OpenAI Agents SDK, Google ADK, Claude Agent SDK via `weave.init("team/project")`.
3. **Weave SDK manual instrumentation** ([trace-agents](https://docs.coreweave.com/products/wandb/weave/guides/tracking/trace-agents)): a `start_turn()` helper "creates a new `invoke_agent` span that becomes the root of a new OTel trace" (helper is in weave releases newer than the locally installed 0.53.12).
4. **Direct OTel/OTLP** ([trace-agents-otel](https://docs.coreweave.com/products/wandb/weave/guides/tracking/trace-agents-otel), [agent-lens/get-started/opentelemetry](https://docs.coreweave.com/products/agent-lens/get-started/opentelemetry)): POST protobuf to `https://trace.wandb.ai/agents/otel/v1/traces` (multi-tenant; a Forge/dedicated instance like forge.coreweave.com has its own trace host). Auth: `wandb-api-key: <key>` or Basic `api:<key>`. Project routing via resource attrs `wandb.entity` + `wandb.project` (or `project_id: TEAM/PROJECT` header). Note this is a **different endpoint** from classic Weave OTel ingest (`/otel/v1/traces`), which feeds the Traces view, not Agents.

## Data model (from SDK source + docs)

- Spans land in a dedicated ClickHouse `spans` table (`weave/trace_server/agents/schema.py`, `AgentSpanCHInsertable`). Attributes follow **OTel GenAI semantic conventions**, with each `gen_ai.*` key aliased to a canonical `weave.*` key (`weave/shared/agents/semconv.py`; `weave.*` wins if both present).
- Structural operation names (`gen_ai.operation.name`): `invoke_agent` = one turn (root span of its own trace), `chat` = one model call, `execute_tool` = tool execution; sub-agents-as-tools are `invoke_agent` children. **Conversation = no span at all**: turns are grouped purely by sharing `gen_ai.conversation.id` across separate traces.
- Key extracted columns: `gen_ai.agent.name/id/description/version`; `gen_ai.conversation.id` (+ Weave-only `weave.conversation.name`); `gen_ai.request.model`, `gen_ai.response.model/id`; `gen_ai.usage.input_tokens/output_tokens` (+ cache + reasoning tokens) for cost; `gen_ai.input.messages` / `gen_ai.output.messages` (JSON), `gen_ai.system_instructions`; `gen_ai.tool.name/type/call.id/definitions/call.arguments/call.result`; `gen_ai.response.finish_reasons`; request params; `gen_ai.output.type` (`text|json|image|speech`); `error.type`/status; Weave extensions `weave.content_refs`, `weave.artifact_refs`, `weave.object_refs`, `weave.compaction.*`, eval linkage `weave.eval.*`, and typed custom-attr maps (up to 1024 attrs/span, 256 KB per value).
- Messages normalize to `{role, content, finish_reason}` where `content` is plain text or a JSON parts array (OTel GenAI `role`+`parts` shape; OpenAI-style `{role, content}` also accepted). Only the trailing run of new user messages in a turn's `input_messages` is emitted as new bubbles — earlier history is deduped.

## Signals and monitors on agent turns

- **Signals** score **agent turns** ("Only `turn` is supported") and show per-scorer **tags** (labels like `user-frustration`) and **ratings** (0.0–1.0) with trend charts in the Signals tab ([view-agent-signals](https://docs.coreweave.com/products/wandb/weave/guides/tracking/view-agent-signals), [create-custom-signal](https://docs.coreweave.com/products/wandb/weave/guides/tracking/create-custom-signal)).
- Attachment mechanism: a Weave `Monitor` whose `op_names=["weave.genai.turn_ended"]` plus `sampling_rate` ([remote-scorer-signals](https://docs.coreweave.com/products/wandb/weave/guides/tracking/remote-scorer-signals)). The literal is `AgentSpanOpName = Literal["weave.genai.turn_ended"]` in both `weave/flow/monitor.py` and `weave/trace_server/agents/schema.py`.
- Server side: when a **parentless** agent span finishes, the trace server emits a `weave.genai.turn_ended` Kafka event (`ScoreAgentSpansEvent`; `weave/trace_server/agents/kafka_events.py`), which fans out to monitors/scorers. A sibling `EmbedAgentSpansEvent` feeds **Agent Insights** embeddings — it *requires* a `conversation_id` and skips evaluation traffic.
- Remote scorers receive `{schema_version: 2, scoring_target: {type: "agent_turn", payload: {messages: {system_instructions, input, output}, conversation metadata, agent details, timestamps, status}}}` and return `{result: {value: <tag≤36 chars | 0.0–1.0>, reason, confidence}}`.

## What data shape populates the Agents view (summary)

To appear there, send **GenAI-semconv OTel spans to the agents endpoint** (`/agents/otel/v1/traces`, protobuf, `wandb-api-key` auth, `wandb.entity`/`wandb.project` resource attrs) — or use a Weave SDK path that does this for you:
1. **Per turn**: one root (parentless) span with `gen_ai.operation.name="invoke_agent"` — this alone defines a turn; its completion fires `weave.genai.turn_ended` for Signals/Insights.
2. **Per conversation**: the same `gen_ai.conversation.id` on every span of every turn (no parent span across turns). Without it the turn buckets under `__NO_CONVERSATION__` and is excluded from Insights embedding.
3. **Identity**: `gen_ai.agent.name` (optional but drives the Agents tab grouping/display; `agent_id/description/version` also extracted).
4. **Content**: `gen_ai.input.messages` / `gen_ai.output.messages` as JSON `{role, content|parts}` arrays and `gen_ai.system_instructions` — these power the chat transcript; only top-level/new user messages become bubbles.
5. **Cost/usage**: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.model` on `chat` spans (required for cost math); cache/reasoning tokens optional.
6. **Tools**: `execute_tool` child spans with `gen_ai.tool.name`, `tool.call.id` matching the model span's tool-call output.
7. **Media (audio)**: message parts of type `uri`/`blob`/`file` whose uri digests match entries in the span's `weave.content_refs` (uploaded `weave:///` content objects); `gen_ai.output.type="speech"` marks audio output modality.

## Unknowns / to verify

- The exact OTLP trace host for the dedicated instance forge.coreweave.com (docs give `trace.wandb.ai` for multi-tenant only; dedicated deployments typically expose `trace.<instance-host>` — confirm empirically).
- The exact producer-side procedure for uploading media content and minting `weave.content_refs` / uri parts (observed only on the read path in `chat_view.py`; no public doc found — likely the SDK's content/file API or the newer SDK's turn helpers handle it). Whether raw inline base64 `blob` parts render without a content ref is unverified.
- Precise `start_turn()` signature/behavior (not present in the installed weave 0.53.12; docs describe it abstractly).
- Whether classic `weave.thread()` / `@weave.op` call traces ([threads doc](https://docs.coreweave.com/weave/guides/tracking/threads)) surface in the Agents view at all, or only genuine GenAI spans do (the separate spans table strongly suggests the latter).
- Minimum fields for an agent row in the **Agents tab** specifically (docs never state a minimum; empirically `invoke_agent` + `gen_ai.agent.name` should suffice).
