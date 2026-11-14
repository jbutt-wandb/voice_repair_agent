# Voice Repair Agent — a teaching POC for Deepgram voice agents

A browser-based voice agent that plays the role of an appliance-repair support line.
You talk; it diagnoses your washing machine / espresso machine / microwave / ice maker
using product manuals, suggests **only** manual-approved safe fixes, refuses anything
dangerous, and books a (simulated) engineer visit when a specialist is needed.

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
# WANDB_API_KEY     — OPTIONAL: https://wandb.ai/authorize — enables W&B Weave/Agent Lens;
#                     without it the app runs fine, observability just disables
uv run main.py            # serves http://127.0.0.1:8000  (PORT=8001 uv run main.py to change)
```

Open the URL in **Chrome**, click **Start call**, grant the mic, and **use headphones**
(echo cancellation matters — see doc 03 §5). Things to try:

- *"My washing machine won't drain."* → basics first, then the manual-approved safe fix.
- *"Tell me how to discharge the microwave capacitor."* → refusal, even if you insist.
- **Talk over the agent** mid-sentence → it stops instantly (barge-in).
- **Send a photo** of a display or error code → spoken filler masks the vision latency,
  then it diagnoses from the image — and *remembers* the photo on later turns.
- *"That's everything, I'm all set."* → it says goodbye and **hangs up itself**.

Verification gates and smoke tests:

```bash
uv run scripts/safety_check.py   # the safety evaluation gate (see below)
uv run scripts/e2e_check.py      # full-pipeline check over the real WebSocket (server must be up)
uv run python -m server.tts      # standalone TTS / STT smoke tests (also: -m server.stt)
```

### Replay a recorded call

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

## Configuration

Every knob lives in [`server/config.py`](server/config.py) with the tradeoff it
controls commented beside it, in three deliberate tiers: **secrets** (`.env` only —
see `.env.example`), **deploy tunables** (the env vars below), and **product
decisions** (plain constants you edit in the file). At startup the server fails fast
if a required key is missing and logs one `config:` line of the live values.
The tunables (booleans accept `0`/`false`/`no` as off):

| Area | Variable | Default | Meaning |
|---|---|---|---|
| Server | `PORT` | `8000` | HTTP/WebSocket port |
| STT / latency | `STT_ENDPOINTING_MS` * | `300` | silence (ms) that ends a user turn — THE latency/accuracy dial |
| Barge-in | `BARGE_IN_ON_VAD` | `1` | let a VAD speech onset pre-arm interruption (transcript still commits it) |
| Barge-in | `BARGE_IN_VAD_GRACE_MS` | `250` | ignore VAD onsets this long after the agent starts speaking |
| Images | `IMAGE_AUTOSEND_S` | `2.0` | a lone photo (no speech) auto-starts a turn after this many seconds |
| Idle timeout | `IDLE_PROMPT_S` / `IDLE_HANGUP_S` | `15` / `25` | silence before the nudge, then the goodbye |
| Weave | `WEAVE_ENABLED` / `WEAVE_CAPTURE_MEDIA` | `1` / `1` | tracing master switch; attach audio/photos to turns |
| Weave | `WANDB_ENTITY` / `WANDB_PROJECT` | *(your default entity)* / `voice-repair-agent` | where traces ship; set both to target a team project |
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
[Doc 06](docs/06-weights-and-biases.md) is the practical map (what's traced, where to
look, monitors vs the in-process acoustic judge);
[doc 07](docs/07-trace-shaping.md) is the field-by-field contract for both stores and
the from-scratch guide to shaping spans for an event-driven voice app.

## Safety design

The agent **fails** if it ever tells a customer to do something a manual forbids.
Three layers: **grounding** (the four manuals ride verbatim in the system prompt,
with explicit `Safe Troubleshooting` vs `DO NOT — Specialist Only` sections),
**hard rules** (refusals must hold under insistence; a SCOPE block pins the agent to
its four appliances with five refusal categories — it closed a real jailbreak), and
**evaluation** (`scripts/safety_check.py`: 16 scripted conversations through the real
prompt + LLM, graded by an LLM-judge that gets the manuals verbatim, plus mechanical
tool assertions; non-zero exit for CI). Honest limitation: runtime enforcement is
prompt-only — production should add an output-side guardrail screening each reply
before TTS. Details and war stories: [doc 03](docs/03-production-challenges.md) §7.

## What to measure

Per turn (`session.py::TurnMetrics`, shown live in the UI and attached to every trace):

| Metric | Meaning | Typical here |
|---|---|---|
| `stt_final_ms` | endpoint wait: last transcript → `speech_final` | ~300–800 ms |
| `llm_ttft_ms` | LLM request → first token | ~400–900 ms |
| `tts_ttfb_ms` | first `Speak` → first audio byte | ~150–250 ms |
| `v2v_server_ms` / `v2v_client_ms` | user stopped → first audio sent / *heard* | ~0.9–1.2 s |
| `barge_in` | turn was interrupted (the rate is a quality signal) | — |

**Voice-to-voice is the product metric** — track p50 *and* p95; the stage breakdown
exists so you know which stage to fix. All fields are filterable Weave attributes and
Agent Lens columns, so percentiles are a query, not a scraper. What production must
add on top (WER, task completion, cost/minute): doc 03 §8–10 and
[docs/evals/](https://github.com/jbutt-wandb/voice_repair_agent/tree/weave_evals)
(evaluation proposal, on the `weave_evals` branch).

## Production considerations

Each of these is a section in [doc 03](docs/03-production-challenges.md), with the
tradeoffs and the war stories; headlines only:

1. Endpointing is a product decision, not a constant.
2. Latency-budget thinking: v2v is a sum of line items — know it before adding a hop.
3. Context: rolling summarization between turns; photos collapse to ~20-token notes.
4. Interruption correctness: everything in flight must be cancellable or identifiably stale.
5. Echo cancellation vs self-barge-in (demo with headphones).
6. Connection hygiene: keep-alives, lazy reconnects.
7. Scaling: sessions are I/O-bound; one async process holds many.
8. Cost: ~$0.03–0.05/min at this model size — TTS dominates, not the LLM.
9. Observability: one log line per turn always; the full trace/Lens layer above it.

## Repository layout

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
the mental model. The full explainer series is in [docs/](docs/README.md).
