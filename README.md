# Voice Repair Agent — a teaching POC for Deepgram voice agents

A browser-based voice agent that plays an appliance-repair support line. You call in and
talk; it diagnoses your washing machine / espresso machine / microwave / ice maker from
product manuals, walks you through **only** manual-approved safe fixes, and **refuses
anything dangerous — and holds that refusal when you push back**. Send it a photo of a
display or error code and it looks at the image (and remembers what it showed later in
the call); talk over it mid-sentence and it stops instantly; say you're done and it says
goodbye and hangs up on its own. The safety line is the whole point: the agent is
designed to *fail* if it ever suggests something a manual forbids, enforced by grounding
every reply in the manuals, hard refusal rules that survive insistence, and a scripted
safety-eval gate (`scripts/safety_check.py`). Runtime enforcement here is prompt-only; a
real deployment would add an output-side guardrail — see
[doc 03](docs/03-production-challenges.md) §7.

Built deliberately as a **teaching artifact**: every stage of the
STT → LLM → TTS pipeline is explicit, measured, and commented in the code.

## Getting started

Prerequisites: [uv](https://docs.astral.sh/uv/) and Chrome. Python 3.14 is pinned in
`.python-version`; uv installs it (and all dependencies) automatically on first run.

```bash
git clone <this-repo> && cd voice_repair_agent
cp .env.example .env      # fill in the required keys — the server refuses to start without them
# DEEPGRAM_API_KEY  — https://console.deepgram.com/
# OPENAI_API_KEY    — https://platform.openai.com/api-keys
# WANDB_API_KEY     — OPTIONAL: https://wandb.ai/authorize — enables W&B Weave/Agent Lens
uv run main.py            # serves http://127.0.0.1:8000  (PORT=8001 uv run main.py to change)
```

Open the URL in **Chrome**, click **Start call**, grant the mic, and **use headphones**
(echo cancellation matters).

**Where your traces go.** With a `WANDB_API_KEY` set, also set **`WANDB_ENTITY`** and
**`WANDB_PROJECT`** in `.env` so traces, Threads, and Agent Lens land in *your* own W&B
workspace. The default project is the maintainer's reference project — the one behind the
screenshots in these docs —
[`wandb-smle/jb_voice_deepgram_agent`](https://forge.coreweave.com/wandb/wandb-smle/jb_voice_deepgram_agent/weave);
your key can view it but not write to it, so point the two vars at your own entity to see
your own calls.

## Configuration

A *knob* here is any value that changes the agent's behavior — a latency threshold, a
feature toggle, a model name. Each is commented with the tradeoff it controls in
[`server/config.py`](server/config.py); the server fails fast on a missing required key
and logs one `config:` line of the live values at startup. Knobs come in three tiers:

- **Secrets** — API keys; `.env` only, never in code (see `.env.example`).
- **Deploy tunables** — env vars an operator flips per environment (the table below;
  booleans accept `0`/`false`/`no` as off).
- **Product decisions** — plain constants you edit in `config.py`, because changing them
  changes the product (e.g. `STT_ENDPOINTING_MS`).

| Area | Variable | Default | Meaning |
|---|---|---|---|
| Server | `PORT` | `8000` | HTTP/WebSocket port |
| STT / latency | `STT_ENDPOINTING_MS` * | `300` | silence (ms) that ends a user turn — THE latency/accuracy dial |
| Barge-in | `BARGE_IN_ON_VAD` | `1` | let a VAD speech onset pre-arm interruption (transcript still commits it) |
| Barge-in | `BARGE_IN_VAD_GRACE_MS` | `250` | ignore VAD onsets this long after the agent starts speaking |
| Images | `IMAGE_AUTOSEND_S` | `2.0` | a lone photo (no speech) auto-starts a turn after this many seconds |
| Idle timeout | `IDLE_PROMPT_S` / `IDLE_HANGUP_S` | `15` / `25` | silence before the nudge, then the goodbye |
| Weave | `WEAVE_ENABLED` / `WEAVE_CAPTURE_MEDIA` | `1` / `1` | tracing master switch; attach audio/photos to turns |
| Weave | `WANDB_ENTITY` / `WANDB_PROJECT` | *(your default entity)* / `voice-repair-agent` | where traces ship; set both to target a specific project |
| Vocal judge | `VOCAL_JUDGE_ENABLED` / `VOCAL_JUDGE_MODEL` | `1` / `gpt-audio-mini` | per-turn acoustic frustration/fear verdict |
| Agent Lens | `AGENT_LENS_ENABLED` | `1` | emit each turn to the Forge Agents view |
| Agent Lens | `AGENT_NAME` / `AGENT_VERSION` | `repair-agent` / `dev` | the agent entity + version label |

\* `STT_ENDPOINTING_MS` is a constant in `config.py` (edit the file), not an env var —
it's a product decision, not a deploy-time setting.

## Architecture

```
  BROWSER (static/)
  ┌────────────────────────────────────────────────┐
  │ mic ──▶ capture-worklet ──▶ 16 kHz PCM         │
  │ speakers ◀── playback-worklet ◀── 24 kHz PCM   │
  └────────────────────────┬───────────────────────┘
                           │  one WebSocket (audio both ways + JSON events)
  BACKEND (server/)
  ┌────────────────────────┴───────────────────────┐
  │ session.py  —  the orchestrator                │
  │ (turn state machine · barge-in · metrics)      │
  │   stt.py ──▶ Deepgram STT  (nova-3)            │
  │   llm.py ──▶ OpenAI gpt-4o-mini (+ manuals)    │
  │   tts.py ──▶ Deepgram TTS  (aura-2)            │
  └────────────────────────────────────────────────┘
```

One WebSocket carries everything; each connection gets its own `Session` owning the
three streaming stages and the conversation history (summarized in the background on
long calls). The governing rule everywhere: **nothing may regress voice-to-voice
latency** — streaming overlap on the hot path, everything else at the turn boundary.
The full walkthrough — stage by stage, the turn state machine, and why barge-in
cancellation needs four ordered kills — lives in
[doc 01](docs/01-how-this-app-works.md) (prose) and
[doc 05](docs/05-architecture.md) (diagrams).

## How Weave & Agent Lens are used

Every finished turn is handed to **one background task** (off the hot path) that fans
out to three destinations, all correlated by one id (`session_id`):

```
 turn ends ──▶ vocal judge (gpt-audio-mini: does the caller SOUND frustrated/fearful?)
                   │
                   ├──▶ Weave trace   — one voice_turn tree per turn (transcript, tool
                   │                    calls, latency/cost attributes, BOTH audio
                   │                    streams playable); Threads = one per call.
                   │                    The forensic view: debug a single call.
                   └──▶ Agent Lens    — one invoke_agent span per turn: Conversations
                                        (chat transcript with per-role audio bubbles +
                                        photos, "Play all" replays the call), Spans
                                        (per-turn STT/LLM/TTS waterfall, app.* metric
                                        columns), Signals (LLM-judge tags per turn).
                                        The product view: analytics across calls.
```

On top of the traces, a server-side **Monitor** (LLM-judge) tags every turn's
transcript for frustration/NSFW/jailbreaking, and a mirrored **Signal** does the same
on the Agents side. Everything degrades to a no-op without `WANDB_API_KEY`.
[Doc 06](docs/06-weights-and-biases.md) is the practical map, with screenshots (what's
traced, where to look, monitors vs the in-process acoustic judge);
[doc 07](docs/07-trace-shaping.md) is the field-by-field contract for both stores and
the from-scratch guide to shaping spans for an event-driven voice app.

## What we're measuring

Latency is the whole game in a voice agent: a caller expects a reply in well under a
second, so knowing *which stage* spends the budget is what makes the agent tunable at
all. Per turn (`session.py::TurnMetrics`, shown live in the UI and attached to every
trace):

| Metric | Meaning | Typical here |
|---|---|---|
| `stt_final_ms` | endpoint wait: last transcript → `speech_final` | ~300–800 ms |
| `llm_ttft_ms` | LLM request → first token | ~400–900 ms |
| `tts_ttfb_ms` | first `Speak` → first audio byte | ~150–250 ms |
| `v2v_server_ms` / `v2v_client_ms` | user stopped → first audio sent / *heard* | ~0.9–1.2 s |
| `barge_in` | turn was interrupted (the rate is a quality signal) | — |

**Voice-to-voice is the product metric** — track p50 *and* p95. All fields are
filterable Weave attributes and Agent Lens columns, so percentiles are a query, not a
scraper. What production must add on top (WER, task completion, cost/minute) is the
[evaluation proposal](https://github.com/jbutt-wandb/voice_repair_agent/tree/weave_evals)
on the `weave_evals` branch.

## Replay a recorded call

Two real conversations ship as local fixtures ([`tests/fixtures/`](tests/fixtures/)) —
re-run a complete call against the live pipeline, repeatably, with no re-recording:

```bash
uv run scripts/replay_session.py --scenario happy_path   # fault -> outlet photo -> unsafe idea (agent refuses) -> booking
uv run scripts/replay_session.py --scenario frustrated   # frustration + prompt-injection attempts -> agent holds the line
```

Each scenario streams the original caller's audio through the real mic path at
real-time pace and sends photos exactly as the browser does; `happy_path` exercises
vision twice (diagnosis from an outlet photo, refusal of scissors held to a power
cord). A `manifest.json` per scenario lists the events; `"autosend": true` on an image
means it is sent with no speech and gets its own turn. No arguments lists scenarios;
`--thread <id>` replays any traced call ad-hoc; `scripts/export_fixtures.py` turns a
recorded call into a new scenario.

Also available: `uv run scripts/safety_check.py` (safety eval gate, no server needed),
`uv run scripts/e2e_check.py` (full-pipeline check incl. a real barge-in, server up).

## Repository layout

> **For how any of this actually works, read [docs/](docs/README.md)** — a numbered
> explainer series: the pipeline stage by stage (01), how Deepgram solves the problem
> (02), production challenges and war stories (03), architecture diagrams (05), the
> W&B / Agent Lens integration with screenshots (06), and the trace-shaping contract (07).

```
main.py                     entrypoint (uvicorn)
server/config.py            every tuning knob, with the tradeoff each one controls
server/app.py               FastAPI app: /ws endpoint + static files, fail-fast startup
server/session.py           ★ orchestrator: state machine, barge-in, context, metrics, protocol
server/stt.py, tts.py       Deepgram live STT/TTS wrappers (each has a -m smoke test)
server/llm.py               OpenAI streaming, tool loop, sentence chunker, summarizer
server/agent.py             ★ safety surface: manuals → system prompt, scope rules, tools
server/weave_trace.py       Weave tracing: one span tree per turn, built off the hot path
server/vocal_judge.py       acoustic frustration/fear judge (forced strict tool call)
server/agent_lens.py        Agent Lens emission: one conversation turn per voice turn
manuals/*.md                4 simulated product manuals (safe vs. specialist-only)
static/                     UI, capture worklet (mic→16 kHz), ring-buffer playback
scripts/safety_check.py     ★ safety eval harness (LLM-as-judge + tool assertions)
scripts/e2e_check.py        full-pipeline integration check (incl. a real barge-in)
scripts/replay_session.py   ★ replay recorded calls / fixture scenarios through the live app
scripts/export_fixtures.py  turn any recorded call into a replay scenario
tests/fixtures/             the two shipped scenarios (trimmed audio + photos + manifests)
```

Start reading at `server/session.py` — the module docstring is the wire protocol and
the mental model.
