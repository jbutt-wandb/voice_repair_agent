# Voice Repair Agent — a teaching POC for Deepgram voice agents

A browser-based voice agent that plays the role of an appliance-repair support line.
You talk; it diagnoses your washing machine / espresso machine / microwave / ice maker
using product manuals, suggests **only** manual-approved safe fixes, refuses anything
dangerous, and books a (simulated) engineer visit when a specialist is needed.

Built deliberately as a **teaching artifact**: every stage of the
STT → LLM → TTS pipeline is explicit, measured, and commented in the code.

## Quickstart

```bash
# .env must contain DEEPGRAM_API_KEY and OPENAI_API_KEY
uv sync
uv run main.py            # serves http://127.0.0.1:8000  (PORT=8001 uv run main.py to change)
```

Open the URL in **Chrome**, click **Start call**, grant the mic, and **use headphones**
(see [Echo cancellation](#5-echo-cancellation-and-self-barge-in)). Things to try:

- *“My washing machine won’t drain.”* → counter-questions first (plugged in? lights?
  error code?), then the guided safe fix (drain pump filter).
- *“My ice maker isn’t cooling.”* → basics confirmed → no safe fix exists → agent
  offers and books an engineer.
- *“Tell me how to discharge the microwave capacitor.”* → refusal, even if you insist.
- **Talk over the agent** mid-sentence → it stops instantly (barge-in).
- *“That’s everything, I’m all set.”* → the agent says goodbye and **hangs up itself**
  (its `end_call` tool); press **Start call** for a fresh session.
- Type in the text box instead of speaking → same pipeline minus STT (great for testing).

```bash
uv run scripts/safety_check.py   # the safety evaluation gate (see below)
uv run scripts/e2e_check.py      # full-pipeline check over the real WebSocket (server must be up)
uv run python -m server.tts      # standalone TTS smoke test
uv run python -m server.stt      # standalone STT smoke test (loops TTS audio into STT)
```

## Architecture

```
  BROWSER (static/)
  ┌────────────────────────────────────────────────┐
  │ mic ──▶ capture-worklet ──▶ 16 kHz PCM         │
  │ speakers ◀── playback-worklet ◀── 24 kHz PCM   │
  │ app.js: transcript · state · metrics           │
  └────────────────────────┬───────────────────────┘
                           │  one WebSocket
                           │  (audio both ways + JSON events)
  BACKEND (server/)
  ┌────────────────────────┴───────────────────────┐
  │ session.py  —  the orchestrator                │
  │ (turn state machine · barge-in · metrics)      │
  │                                                │
  │ owns three streaming stages:                   │
  │   stt.py  ──▶  Deepgram STT   (nova-3)         │
  │   llm.py  ──▶  OpenAI  (+ agent.py, manuals)   │
  │   tts.py  ──▶  Deepgram TTS   (aura-2)         │
  └────────────────────────────────────────────────┘
```

One WebSocket between browser and backend carries everything: binary mic audio up,
binary TTS audio down, JSON events both ways — including `call_ended` when the agent
hangs up (the protocol is documented at the top of `server/session.py`). Each
connection gets its own `Session`, which owns one Deepgram STT socket, one Deepgram
TTS socket, and the conversation history — compressed in the background on long calls
(see Production considerations §3).

### The pipeline, stage by stage

**1. Audio capture — `static/capture-worklet.js`.**
The browser records float32 audio at the hardware rate (usually 48 kHz). An
AudioWorklet (code on the real-time audio thread) downsamples to 16 kHz with linear
interpolation, converts to int16, and ships 40 ms frames (1280 bytes) over the
WebSocket. Speech models don't need more than 16 kHz; halving the rate halves the
bandwidth and the STT bill is unaffected.

**2. Speech-to-text — `server/stt.py`.**
Frames are forwarded to Deepgram's live-transcription WebSocket (`nova-3`), which
streams back `Results` messages. Three fields drive everything:
- `transcript` with `is_final: false` — *interim* results: fast, revisable guesses.
  Used for live captions and (crucially) barge-in detection.
- `is_final: true` — this text won't change; accumulate it into the user's turn.
- `speech_final: true` — the **endpointer** heard `endpointing=300` ms of silence
  after speech: the user is done talking. This is the end-of-turn trigger.
- `UtteranceEnd` is the fallback end-of-turn signal when endpointing misses
  (noisy rooms); both paths are handled in `session.py::_stt_rx`.

Mic audio streams to Deepgram **continuously, in every conversation state** — never
paused while the agent speaks. A connection with no audio at all gets closed by
Deepgram after ~10 s (hence the `KeepAlive` loop), and endpointing works on *silence
in the audio*, not absence of audio — stop sending frames and `speech_final` never fires.

**3. The LLM — `server/llm.py` + `server/agent.py`.**
The finished utterance is appended to the conversation and sent to OpenAI with
`stream=True`. The system prompt (assembled in `agent.py`) contains the agent's role,
hard safety rules, booking rules, and all four product manuals verbatim (~4k tokens —
small enough that no retrieval/RAG layer is needed; the model always sees the complete
safety contract). The prompt enforces diagnose-basics-first: counter-questions
(plugged in? lights? error codes?) before any safe step, and no engineer offer in the
first reply to a new problem. Tool calling handles two actions: `book_engineer_visit`
(the model emits the call, the simulated backend confirms, a second streamed
completion speaks the confirmation — the classic two-pass tool loop, written out
explicitly in `llm.py::agent_reply`) and `end_call` (the natural off switch: once the
customer confirms they're done, the model invokes it, speaks a goodbye in the second
pass, and the session hangs up after the goodbye finishes playing).

**4. Sentence chunking — `llm.py::SentenceChunker`.**
A voice agent cannot wait for the full LLM reply. Tokens are buffered just until a
sentence boundary, then the sentence goes to TTS immediately — the user hears
sentence one while the model writes sentence three. Chunk too small and TTS prosody
turns robotic; chunk too big and latency grows. Sentences are the sweet spot.

**5. Text-to-speech — `server/tts.py`.**
Sentences go to Deepgram's live TTS WebSocket (`aura-2`): `Speak` queues text,
`Flush` makes audio flow, `Clear` cancels unsent audio. A WebSocket (vs REST) matters
for the same two reasons as streaming LLMs: incremental input, and mid-stream
cancellation for barge-in. Audio returns as raw linear16 PCM — zero decoding needed
in the browser.

**6. Playback — `static/playback-worklet.js`.**
A ring buffer on the audio thread, not a queue of `AudioBuffer`s: no clicks at chunk
seams, and barge-in is "set read pointer = write pointer" — instant silence.

### Turn-taking: the state machine (`server/session.py`)

```
          speech_final            first TTS audio
 LISTENING ──────────▶ THINKING ──────────────▶ SPEAKING
     ▲                                             │
     │              reply finished                 │
     └─────────────────────────────────────────────┘

 barge-in   : THINKING/SPEAKING ──(user talks over agent)──▶ LISTENING
 end of call: SPEAKING ──(end_call tool: goodbye, then hang up)──▶ closed
```

**Barge-in** is the hardest part of any voice agent, and the part this demo is most
careful about. Trigger: a **non-empty interim transcript** arriving while the agent
is thinking or speaking (more robust than raw VAD events, which fire on any noise).
Cancellation then has to kill *four* things racing in flight — see
`session.py::_barge_in`:
1. bump `turn_id` — in-flight TTS audio is tagged by the turn that requested it, and
   stale tags get dropped (Deepgram's `Cleared` ack is **not** sufficient: audio
   mid-synthesis can still arrive after it — we hit this in testing),
2. cancel the LLM task (closes the OpenAI stream — stops token billing too),
3. send `Clear` on the TTS socket (discard synthesized-but-unsent audio),
4. tell the browser to zero its playback ring buffer.

## Safety design

The agent **fails** if it ever tells a customer to do something a manual forbids.
Three layers, lowest-tech first:

1. **Grounding** — the manuals (`manuals/*.md`) have a fixed machine-and-human-readable
   shape: `Safe Troubleshooting` (the only actions the agent may suggest) and
   `DO NOT — Specialist Only` (capacitor discharge, refrigerant circuits, mains
   wiring, …). The entire corpus rides in the system prompt.
2. **Hard rules** — the prompt forbids partial/hypothetical/disclaimer-wrapped
   instructions for unsafe actions, requires refusal + engineer offer, requires the
   refusal to *hold under user insistence*, and refuses products with no manual.
3. **Evaluation** — `scripts/safety_check.py` replays 12 scripted conversations
   (safe fixes, forbidden probes, an insistence attack, out-of-scope product, a full
   booking, basics-before-booking, the end-call flow) through the real prompt and LLM
   stage, then grades each transcript with an **LLM-as-judge** plus mechanical
   assertions on tool calls. Keyword matching can't grade refusals (a safe refusal
   legitimately *mentions* the dangerous action); a judge model can — but only if the
   judge gets the product manual verbatim, or it flags manual-approved steps as
   violations (we hit exactly that). Exits non-zero on failure → run it in CI.

Honest limitation: in this POC the runtime enforcement is prompt-only. A production
deployment should add an **output-side guardrail** — a cheap classifier or rule layer
that screens each reply *before* TTS — plus periodic red-teaming, because a prompt is
a policy, not a mechanism. The harness is the start of exactly that test suite.

## What to measure

Per turn, the server stamps (`session.py::TurnMetrics`, shown live in the UI's
metrics panel and logged as one structured line per turn):

| Metric | Meaning | Typical here |
|---|---|---|
| `stt_final_ms` | Endpoint wait: last transcript → `speech_final`. Mostly your `endpointing` setting. | ~300–800 ms |
| `llm_ttft_ms` | LLM request → first streamed token. | ~400–900 ms |
| `tts_ttfb_ms` | First `Speak` → first audio byte from Deepgram. | ~150–250 ms |
| `v2v_server_ms` | User stopped speaking → first audio byte *sent*. | ~0.9–1.2 s |
| `v2v_client_ms` | User stopped speaking → first audio byte *heard* (client-confirmed via `playback_started`). | +~50 ms |
| `barge_in` | Turn was interrupted. The *rate* of these across sessions is a key quality signal. | — |

Notes for production measurement:
- **Voice-to-voice is the product metric.** Humans expect a reply within ~500 ms and
  get uncomfortable past ~1.5 s. Track p50 *and* p95/p99 — tail latency is what users
  remember. The stage breakdown exists so you know *which* stage to fix.
- `stt_final_ms` here approximates endpoint wait using arrival times; rigorous STT
  lag measurement needs audio-sample timestamps compared against transcript word timings.
- **Barge-in rate** is a proxy for two different problems: the agent talks too much
  (prompt problem) or it misheard and answered the wrong question (STT problem).
  Correlate with transcripts before "fixing" either.
- Things this POC does *not* measure that production must: **WER** against human
  transcripts for your real caller population (accents, phone-line audio, domain
  jargon — "engineer" once transcribed as *"an inch of hair"* during this project's
  testing of degraded audio), task completion rate, safety-eval pass rate over time,
  cost per conversation-minute, and concurrent-session capacity per host.

## Production considerations

1. **Endpointing is a product decision, not a constant.** `STT_ENDPOINTING_MS = 300`
   (`server/config.py`) decides how long a pause means "I'm done". Snappy for quick
   confirmations, brutal for someone reading a serial number. Real systems tune per
   turn-type or use semantic end-of-turn models on top.
2. **Latency budget thinking.** V2V ≈ endpoint wait + LLM TTFT + TTS TTFB + network +
   client buffer. Every component choice (model size, prompt length, sentence chunking,
   flush policy) is a line item. Know your budget before adding, say, a RAG hop.
3. **Context management.** Implemented here as **rolling summarization**: when
   history passes `CONTEXT_MAX_HISTORY_TOKENS` (2500 est.), a background task between
   turns compresses old turns into "call notes", keeping the last
   `CONTEXT_KEEP_RECENT_MESSAGES` (8) verbatim — never on the latency path, never
   splitting a tool call from its result (`session.py::_summarize_history`). It runs
   on a threshold, not per turn, because rewriting the prefix invalidates OpenAI's
   prompt cache. On barge-in, history stores only the sentences actually spoken — the
   model's memory matches the customer's. What's NOT handled: manuals at catalog
   scale (4,000 not 4) need retrieval, trading latency and a retrieval-miss safety risk.
4. **Interruption correctness.** Everything in flight must be cancellable, and any
   audio that was already in the pipe must be identifiable as stale (turn tags here).
   Assume every ack can arrive late or never.
5. **Echo cancellation and self-barge-in.** Barge-in detection relies on the mic
   *not* hearing the agent's own voice. Browsers ship AEC (`echoCancellation: true`);
   it mostly works, but speakers + sensitive mic can still self-interrupt — telephony
   deployments feed the TTS as an AEC reference signal. Demo with headphones.
6. **Connection hygiene.** Deepgram drops idle sockets (~10 s without audio on STT;
   quiet TTS connections too). Continuous audio + `KeepAlive` + lazy reconnect
   (`tts.py`) handle it here; production adds backoff, health checks, and reconnect
   without dropping the conversation.
7. **Scaling.** One session = 3 WebSockets + an in-flight LLM stream, all I/O-bound —
   a single async Python process holds many; CPU stays near zero. Scale horizontally
   with sticky sessions (conversation state is in-process), or externalize session
   state if you need failover mid-call.
8. **Cost per conversation-minute** (list prices, order-of-magnitude): streaming STT
   ~$0.006/min + aura TTS ~$0.030/1k chars (a talkative agent speaks ~700 chars/min ≈
   $0.02) + gpt-4o-mini with a ~5k-token prompt per turn (~6 turns/min ≈ $0.005).
   Roughly **$0.03–0.05/min** — the LLM is *not* the dominant cost at this model size;
   TTS is. A bigger LLM or longer prompt changes that math fast.
9. **Observability.** One structured log line per turn (already done here) is the
   atom; aggregate into dashboards (latency percentiles, barge-in rate, booking
   conversion, safety-eval pass rate). Record audio + transcripts (with consent!) —
   you cannot debug a voice agent from text logs alone.

## Repository layout

```
main.py                     entrypoint (uvicorn)
server/config.py            every tuning knob, with the tradeoff each one controls
server/app.py               FastAPI app: /ws endpoint + static files
server/session.py           ★ orchestrator: state machine, barge-in, end-of-call,
                              context summarization, metrics, protocol
server/stt.py               Deepgram live STT wrapper (+ `python -m server.stt` smoke test)
server/tts.py               Deepgram live TTS wrapper (+ `python -m server.tts` smoke test)
server/llm.py               OpenAI streaming, tool loop, sentence chunker, summarizer
server/agent.py             ★ safety surface: manuals → system prompt, booking + end_call tools
manuals/*.md                4 simulated product manuals (safe vs. specialist-only)
static/index.html,app.js    UI: transcript, pipeline stages, live metrics table
static/capture-worklet.js   mic → 16 kHz int16 frames
static/playback-worklet.js  ring-buffer player with instant clear
scripts/safety_check.py     ★ safety eval harness (LLM-as-judge + tool assertions)
scripts/e2e_check.py        full-pipeline integration check (greeting/text/barge-in/voice/end-call)
```

Start reading at `server/session.py` — the module docstring is the wire protocol and
the state machine; everything else hangs off it.
