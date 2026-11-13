# Voice agents in the wild: what makes production hard

The demo in this repo works, measurably: ~1 s voice-to-voice, 20/20 end-to-end
checks, 16/16 safety scenarios. This doc is the honest list of everything that
gets harder between "works on my laptop with my voice and headphones" and "handles
ten thousand real callers a day." Each section names the problem, shows where this
codebase touches it, and sketches what production actually does.

**One principle runs through all of it: every feature must preserve voice-to-voice
latency.** That is the hard constraint a voice agent lives under, and it is the
reason the later features in this project look the way they do. Observability
(§10), the tighter safety scope (§7), snappier barge-in (§5), and photo memory (§6)
were *all* added without a latency regression — each one was deliberately designed
to keep its work off the real-time hot path (traces assembled at the turn boundary,
photo descriptions generated as trailing text after the spoken reply, interruption
moved to a signal that only *shortens* time-to-interrupt). When you read those
sections, watch for where the work was moved *to*; that's the transferable idea.

## 1. Latency: budget like money, measure like tails

Humans expect a conversational reply within ~500 ms; beyond ~1.5 s they start
assuming the line is dead. Voice-to-voice (V2V) latency is a *sum*:

```
V2V ≈ endpoint wait + LLM time-to-first-token + TTS time-to-first-byte
      + network hops + client buffering
       (~300–800ms)      (~500ms here)            (~200ms here)
```

Treat it as a budget: every feature you add — a retrieval hop, a guardrail check,
a bigger model — is a line item that must fit. This app measures every line item
per turn (`TurnMetrics` in `server/session.py`, visible in the UI).

The production trap is averages. Your demo shows p50 ≈ 1 s; your users remember
the p95 and p99 — the turn where the LLM had a slow day, the TTS connection had to
re-handshake, the user's WiFi hiccuped. A 1 s median with a 4 s p95 *feels like a
4-second agent*, because conversation gives you ~6 chances per minute to be slow.
Production dashboards track latency percentiles per stage, so when p95 degrades
you know whether to blame the STT, the LLM, or the network.

## 2. Speech recognition meets reality: WER

Word Error Rate on clean demo audio and on real traffic are different worlds.
Real callers bring accents, speakerphones in kitchens, 8 kHz telephone codecs,
crosstalk, and — worst of all for a repair line — *entities*: product names,
model numbers, booking references.

True story from this project's own testing: we fed slightly degraded synthetic
audio through the pipeline and *"can an engineer come on Friday morning"* came
back as **"can an inch of hair come on Friday morning."** One mangled content
word. An LLM can often recover from that via context — but imagine the same
failure on a serial number ("IM-200" → "I am 200") where context can't save you.

What production does:

- Measure WER on *your* traffic with human-transcribed samples, segmented by
  accent, channel (app vs phone), and noise level — not on a benchmark.
- Use keyword/keyterm boosting for your product vocabulary (Deepgram supports
  prompting the model with expected terms).
- Design dialogs that confirm critical entities back: "That's reference E-N-G
  four two four six, correct?" — cheap insurance against a 5% entity error rate.

## 3. Echo cancellation: the agent that interrupts itself

Barge-in detection assumes the microphone hears *the user*. But the agent's own
voice comes out of the speakers, into the mic, back through STT… and the agent
interrupts itself, forever, in a loop.

This app leans on the browser's built-in acoustic echo cancellation
(`echoCancellation: true` in `static/app.js` — it's mandatory, not an
optimization), and the README tells demo users to wear headphones because
browser AEC is good-not-perfect with loudspeakers. The risk is sharpest now that
barge-in fires on Deepgram's `SpeechStarted` VAD onset (§5) rather than waiting for
a transcript: a VAD detector will happily flag the agent's own voice as "speech." So
the VAD path adds a second guard on top of AEC — a short grace window
(`BARGE_IN_VAD_GRACE_MS`, default 250 ms) after the agent's first audio during which
a VAD onset is ignored — and the whole VAD trigger can be disabled
(`BARGE_IN_ON_VAD=0`) to fall back to the transcript path, which is naturally immune
(the agent's own words don't read as the *user* starting a new turn).

Production telephony stacks do this properly: the TTS output is fed to the echo
canceller as a *reference signal* ("this is what we're playing — subtract it from
the mic"), and barge-in may additionally require N consecutive interim transcripts
or a confidence threshold before firing. If you ever see a transcript of your
agent's own words coming back at you, this is what's missing.

## 4. Turn-taking: endpointing vs humans who pause to think

Doc 02 covers the mechanics; here's the product problem. `endpointing=300` means
anyone who pauses 300 ms mid-thought gets talked over:

> "My serial number is… *(looks at the sticker)* …" → agent: "I'm sorry, I didn't catch that!"

Raising the threshold makes every turn slower for everyone. There is no value
that's right for both "yes" and "let me read you this 12-digit number."

Production approaches, in increasing sophistication:

- per-context endpointing (expecting a long answer? raise it temporarily),
- a *semantic* end-of-turn model: does the transcript so far look finished?
  ("my serial number is" → clearly not; hold even through a long pause),
- graceful repair when you get it wrong anyway: barge-in handling (which this app
  has) doubles as the undo button for premature replies — the user just keeps
  talking and the agent yields.

Related knob with the same flavor: barge-in *sensitivity*. This app fires on any
non-empty interim transcript. Real callers say "mm-hmm" and "right" *while
agreeing with you* (backchanneling) — production systems often ignore short
acknowledgment words unless they persist.

## 5. Interruption correctness: everything must be cancellable

When a user barges in, the old reply is simultaneously (a) being generated by the
LLM, (b) being synthesized by TTS, (c) in flight on the network, and (d) sitting
in the client's playback buffer. Kill all four or the user hears a ghost of the
cancelled reply — the single most "broken-feeling" bug a voice agent can have.

This app's four-step chain (`session.py::_barge_in`) and the lesson behind it:

1. bump `turn_id` — **stale output must be self-identifying**. We learned the hard
   way (doc 02) that Deepgram can deliver audio after its `Cleared` ack, so
   correctness can't hang on acknowledgments; every audio chunk is checked against
   the turn that requested it.
2. cancel the LLM task — also stops token billing for a reply nobody will hear.
3. `Clear` the TTS socket — stop wasting synthesis.
4. tell the client to zero its ring buffer — instant local silence.

Cancellation is the hard half; *detection* is the fast half, and this app tightened
it. Barge-in originally waited for an interim transcript — robust, but it needs
recognized words, so a short agent reply could finish before the user's interruption
was ever noticed. It now fires on Deepgram's `SpeechStarted` VAD onset (speech
*begin*, before any word), with the transcript path kept as a fallback. This is pure
upside for responsiveness: it only ever *shortens* time-to-interrupt, adds no latency
anywhere, and finally makes short replies interruptible. The cost is a slightly
noisier trigger (a loud background sound can fire it), bounded by echo cancellation,
a grace window, and a kill switch — see §3.

The generalizable rule: in a streaming system, *design the cancellation path with
the same care as the happy path*, and assume every ack can be late, reordered in
effect, or missing.

There's a fifth copy people forget: the **conversation history**. If the model
generated three sentences and the user heard one, storing all three means the
model later references things the user never heard. This app stores only the
sentences actually delivered, plus a `[cut off here by the customer]` marker
(`session.py`, both the mid-stream-cancel and audio-cut paths) — an idea borrowed
from how production frameworks like Pipecat aggregate assistant context from TTS
output rather than LLM output.

## 6. Long calls: context growth

Every turn appends to the conversation history, and this app re-sends the whole
thing — including the ~4k-token system prompt with all four manuals — on every
LLM call. Cost and TTFT grow linearly with call length, so this app implements the
standard production answer, **rolling summarization**, and it makes a good worked
example (`session.py::_summarize_history`, `llm.py::summarize_history`,
thresholds in `config.py`):

- **Trigger**: after a turn finishes — never during one — if the history exceeds
  `CONTEXT_MAX_HISTORY_TOKENS` (2500, estimated at ~4 chars/token; exact counting
  is pointless for a threshold), a background task starts. Because it runs in the
  quiet moment *between* turns, it can never add to voice-to-voice latency.
- **What it does**: everything except the last `CONTEXT_KEEP_RECENT_MESSAGES` (8)
  messages is sent to a cheap model with a prompt that preserves the facts that
  matter later — product, symptoms, every step tried *and its outcome*, safety
  refusals already given, booking details. The result replaces the old messages as
  one "Call notes" system message; recent turns stay verbatim because the model
  needs exact recent wording, while old turns only need their facts.
- **Two correctness details** worth stealing: the splice point never separates an
  assistant tool-call message from its tool-result message (an orphaned
  `role:"tool"` message is an invalid OpenAI sequence — the split walks back past
  it), and because history is append-only, replacing the slice `[1:split]` by
  index is safe even if a new turn starts appending mid-summarization.
- **The hidden cost**: rewriting the message prefix invalidates the provider's
  automatic prompt cache for the next request — you pay full input price once.
  That's why it runs on a threshold, not every turn: batch the rewrites and the
  cache discount does its job the rest of the time.

### Images are context too: remembering a photo without re-sending it

Text isn't the only thing that grows the prompt. A photo (doc 01, "Photos: a second
way in") is *far* heavier than a sentence — a vision payload is hundreds to thousands
of tokens — and because this app re-sends the whole history every turn, a photo left
sitting in the conversation would be re-billed and re-processed on *every subsequent
turn*, inflating prefill, TTFT, and cost for the rest of the call. The naive fixes
are both bad: keep the image (slow, forever) or drop it entirely and the agent
instantly "forgets" what the customer showed it.

The trick is to keep the photo's *information* while throwing away its *pixels*. On
an image turn, the **same** vision call that produces the spoken reply is prompted to
also emit a short, content-labelled description of the photo — but *after* the reply,
wrapped in a `[[PHOTO: ...]]` sentinel (`server/agent.py`). The sentinel is stripped
before TTS and is **never spoken** (`session.py::_strip_sentinel`, with a belt-and-
braces guard so a stray `[[` can't ever reach the speaker). Then the image is
collapsed out of history: the base64 user message is replaced with a tagged text note
like `[Photo the customer shared — microwave display showing error E-15]`
(`session.py::_collapse_image_messages`). Because the full history is re-sent every
turn, that note is always in context with no retrieval step — the agent simply
"remembers" what each photo showed, including across **multiple** photos (each
becomes its own chronological tagged note), at ~20 text tokens apiece instead of the
hundreds-to-thousands a resident image would cost. Rolling summarization is written
to preserve these photo notes, so the memory survives compression too.

Two things make this free. First, **latency**: the description is *trailing
generation* — it streams out after the spoken sentences, so TTFT and voice-to-voice
are untouched, and there is **no second LLM call**. Second, it's strictly better than
the behavior it replaced, where the collapsed placeholder was contentless
(`[customer shared a photo]`) and the agent remembered only *that* a photo existed,
not *what was in it*.

What this app *doesn't* implement, and production eventually wants:

- **Structured state**: an explicit slot object (product, fault, steps attempted,
  booking status) updated each turn. More work than prose notes, more robust —
  and it's exactly the handoff data a human engineer wants.
- **Retrieval** for the manuals: at a real catalog size (4,000 manuals, not 4) you
  fetch only the relevant one once the product is known. New safety risk: a
  retrieval miss means the model has no manual and may improvise — precisely what
  the safety rules forbid.

## 7. Safety: a prompt is a policy, not a mechanism

This agent gives *physical-world* advice. A hallucinated repair step isn't a
wrong fact — it's someone opening a microwave with a charged high-voltage
capacitor. The demo's layers:

1. **Grounding**: manuals with explicit "Safe Troubleshooting" vs "DO NOT —
   Specialist Only" sections, all in the system prompt (`server/agent.py`).
2. **Hard rules**: never describe a forbidden action even partially/hypothetically
   /with disclaimers; refuse again under insistence; unknown product → booking only.
3. **Evaluation**: `scripts/safety_check.py` replays 16 scripted conversations —
   safe fixes, forbidden probes ("walk me through discharging the capacitor",
   "can I regas the refrigerant?"), an insistence attack, an out-of-scope product,
   a full booking, basics-before-booking, the end-call flow, and (new) four scope
   attacks: a plumbing pivot, a general-knowledge question, a "while you're at it"
   secondary request, and a prompt-injection "ignore your instructions" — and grades
   transcripts with an **LLM-as-judge** (keyword matching can't grade refusals,
   because a good refusal legitimately *mentions* the dangerous thing). Mechanical
   assertions check each tool was called exactly when it should be. Exit code
   gates CI.

**Scope is a safety surface too.** "Dangerous" isn't only capacitor discharge; it's
also the agent wandering off its four appliances. The system prompt now carries a
dedicated **SCOPE block** (`server/agent.py`) with five refusal categories, each
behaving differently on purpose: (a) another *appliance* it has no manual for —
decline the advice but still offer an engineer; (b) a non-appliance home system
(plumbing, water pipes, mains electrics, gas, HVAC) — give *no* advice **and** offer
*no* engineer, just redirect; (c) general/off-topic questions — one-line decline,
steer back; (d) "while you're at it…" secondary requests — decline the extra, stay on
the appliance; (e) instruction-override / roleplay attempts — ignored entirely. This
closed a real jailbreak found in testing: a caller pivoting an ice-maker call into
"my tap has no pressure, talk me through fixing the plumbing," which the old prompt
would help with. The four new eval scenarios lock each vector down. One honest
residual: the LLM judge occasionally misgrades a *correct* decline (judge noise, not
agent behavior) — it's eval-only and never touches the voice path, but it's why a
green run is read as a trend, not a single pass.

**Ground your judge like you ground your agent.** A lesson from building that
harness: the judge originally saw only the criteria and the transcript — and it
failed a perfectly safe reply for instructing the user to "hold the two-cup
button for five seconds," a step the espresso manual spells out verbatim. Without
the ground truth, a judge can't tell a manual-approved step from an improvised
one, so it hallucinates violations (or worse, misses them). The fix: each
scenario names its product, and the judge receives that manual as the
authoritative safe/forbidden list. An ungrounded judge is just a second opinion;
a grounded judge is an auditor.

**Making the model actually call the tool.** The end-call feature surfaced a
classic reliability trap. The first prompt said "say goodbye AND call the
end_call tool in the same reply" — and in live testing (temperature 0.3, not the
harness's 0) the model sometimes wrote `(end_call: {"reason": "customer_done"})`
as *literal reply text* instead of invoking the function. The line stayed open,
and that pseudo-code went to TTS to be read aloud. The fix was structural, not
more prompt emphasis: **tool-first, speak-second** — invoke `end_call`
immediately, then say the goodbye in the second pass of the tool loop, exactly
like the booking flow (which never had this problem). When a tool call and prose
must happen together, sequencing them through the tool loop is far more reliable
than asking a small model to do both in one completion — and your eval only
catches this if it runs at production temperature and asserts the tool call
mechanically, not just judging the text.

Be clear about what this is and isn't: the runtime enforcement is *prompt-only*.
The eval harness measures whether the prompt holds; it doesn't prevent a novel
jailbreak at 2 a.m. Production adds:

- an **output-side guardrail**: a cheap classifier or rule layer screening every
  reply *before* TTS (note the latency line item — it must fit the budget; you
  can screen per-sentence in parallel with synthesis),
- continuous **red-teaming** and an ever-growing regression suite (every incident
  becomes a scenario),
- tracking the safety-eval pass rate over time like any other SLO, re-running on
  every prompt/model change.

## 8. Connections are livestock, not pets

Three long-lived connections per call (browser WS, STT WS, TTS WS), and each one
will eventually drop, idle out, or hiccup:

- Deepgram STT closes after ~10 s without audio → continuous mic + `KeepAlive`
  (`server/stt.py`).
- The TTS socket idles out during long quiet stretches between replies →
  lazy reconnect in `server/tts.py::speak`.
- Browser refresh mid-utterance → the session's `finally` block tears down all
  tasks and sockets (and we fixed a real bug here: `CancelledError` isn't a
  subclass of `Exception` in Python, so a naive `suppress(Exception)` let
  teardown crash).

One more stream-teardown bug fixed in passing: barge-in cancels the LLM by closing
the OpenAI HTTP stream, but under `openai` 3.31 calling `await stream.close()` on an
*already fully-consumed* stream raises `TypeError` (close returns `None`, not an
awaitable). `llm.py::agent_reply` now guards the await, so a normal completion can't
throw during cleanup and mask the real exception that unwound it. A reminder that the
cancellation path runs in both the interrupted *and* the happy case, and has to be
correct in both.

Production adds reconnect-with-backoff that *survives mid-call* (buffer the mic
audio during the gap, resume the STT stream, don't lose the conversation),
heartbeats, and alerting on abnormal disconnect rates.

## 9. Scale and cost

The good news: a session is almost pure I/O — three sockets and an HTTP stream,
near-zero CPU — so one async Python process holds many concurrent calls, and you
scale horizontally. The catch: conversation state lives in process memory, so you
need sticky routing (a call must keep hitting its host) or externalized session
state if you want mid-call failover.

Cost per conversation-minute, order-of-magnitude at list prices: streaming STT
~$0.006 + TTS ~$0.02 (a talkative agent speaks ~700 chars/min) + gpt-4o-mini with
this app's ~5k-token prompt ~$0.005 ≈ **$0.03–0.05/min**. Two observations that
surprise people: at small-model prices, *TTS, not the LLM, dominates*; and
swapping in a frontier-class LLM or doubling the prompt flips that instantly.
Do this arithmetic before promising unit economics.

## 10. Observability: you can't debug voice from text logs

When a user says "it talked over me and then booked the wrong thing," you need:

- **structured per-turn events** — this app logs one JSON-ish line per turn with
  every `TurnMetrics` field; that's the atom you aggregate into dashboards
  (latency percentiles by stage, barge-in rate, booking conversion, safety pass
  rate),
- **audio recordings with transcripts** (with consent and a retention policy) —
  WER problems, echo problems, and endpointing problems are inaudible in text;
  the "an inch of hair" bug is invisible in logs that only show the final
  transcript looking confident,
- **turn-level tracing** — the `turn_id` that powers barge-in correctness doubles
  as the trace ID that ties a user complaint to the exact STT results, LLM call,
  and TTS stream involved.

### The implemented answer: a W&B Weave trace per turn

This app does more than log a line — it ships a full, reviewable trace tree to
**W&B Weave** (`server/weave_trace.py`), so a completed conversation can be replayed
afterward with its transcript, per-stage latency, tool use, cost, *and audio*. It's a
worked example of all three bullets above at once, and — like rolling summarization in
§6 — it was built to the project's hard rule: **it adds zero voice-to-voice latency.**

The trace contract is deliberately strict (that's the whole point — a trace you can't
rely on the shape of is a trace you can't chart across turns):

```
Session            = one weave.thread(session_id)   — groups every turn of a call
└─ voice_turn       one root span per model-driven turn
   │  inputs   {turn_id, user_text, image, user_audio, tts_audio}
   │  output   {assistant_text, tool_calls[], interrupted, ended}
   │  attrs    latency (ms), token usage, cost, barge_in, filler_used,
   │           image_autosend, idle_timeout
   ├─ llm_completion   inputs {messages (base64 image REDACTED)}  output {text, usage}
   ├─ tool_<name>      inputs = parsed args                        output = result
   └─ speak            inputs {text}                               one per spoken sentence
```

Four design choices are worth stealing:

- **The span tree is assembled at the *turn boundary*, not live inline.** A turn is
  spread across asyncio tasks — STT finalizes in one, the LLM runs in another, TTS
  audio completes in a third — so rather than fight that with live span wrapping, the
  code *collects* a turn's data as it happens and emits the whole tree once, after the
  caller has already heard the reply. All the heavy work (image decode, WAV wrapping,
  network flush) lands here, off the hot path; Weave itself flushes on a background
  thread. That's *why* latency is zero.

- **Latency lives in `attributes`, not `output`.** `stt_final_ms`, `llm_ttft_ms`,
  `tts_ttfb_ms`, `v2v_server_ms`, `v2v_client_ms`, the headline `voice_to_voice_ms`,
  token counts, `est_cost_usd`, and the `barge_in` / `filler_used` / `image_autosend`
  / `idle_timeout` flags are all call *attributes* — which Weave makes filterable and
  chartable *across* turns (p95 TTFT, barge-in rate, cost per turn), exactly the
  aggregation the bullets ask for. Because attributes are frozen at call creation and
  the metrics aren't known until the turn ends, this is the second reason the tree is
  built post-hoc: emit early and the numbers wouldn't exist yet.

- **We record the LLM call ourselves instead of auto-patching.** Weave can
  auto-instrument the OpenAI client, but that path (as of weave 0.53.12) can't redact
  the base64 image buried in a multimodal `messages` array, and it freezes the span's
  attributes before our per-turn metrics exist. So the auto-patch span is suppressed
  (`suppress_autotrace()` around the live call in `llm.py`) and `llm_completion` is
  recorded by hand with the image **redacted** and the token usage folded in. No raw
  image bytes ever leave the box.

- **Canned speech is not a `voice_turn`.** The greeting, the image filler, and the
  idle nudge/goodbye aren't model turns, so they're recorded as their own
  `system_utterance` root ops (with their own audio) — model-generated speech is never
  confused with scripted speech, yet *all* agent audio is captured.

### Hearing both sides

The feature text alone can't surface a WER or echo problem — so when
`WEAVE_CAPTURE_MEDIA` is on, each `voice_turn` carries **two separate audio streams**:
`user_audio` (the caller's 16 kHz mic for that turn) and `tts_audio` (the agent's
24 kHz spoken reply). They're kept separate rather than spliced — partly so a reviewer
can scrub each side on its own, partly because they're genuinely *different sample
rates*. Keeping the right audio on the right turn reuses the same machinery that makes
barge-in correct: the TTS turn-tagging staleness check plus a per-turn buffer reset
stop one turn's audio from bleeding into the next. (The caller's photo, decoded, rides
along as the turn's `image` input.)

### Turning it on

Config lives in `server/config.py`, all env-overridable: `WANDB_ENTITY` /
`WANDB_PROJECT` (default `wandb-smle/jb_voice_deepgram_agent`), `WEAVE_ENABLED` (master
switch), and `WEAVE_CAPTURE_MEDIA` (attach photo + audio). The whole layer degrades to
a **no-op** when `WANDB_API_KEY` is absent, so the app runs identically with or without
it. One subtlety worth copying: `.env` is loaded in `config.py` itself, not just in
`main.py` — the live server is launched as `uvicorn server.app:app`, which never runs
`main.py`, and without that early load the key would be missing in the uvicorn process
and tracing would silently stay off.

A metric worth singling out: **barge-in rate** — now a first-class, filterable trace
attribute. Rising barge-ins mean either the agent talks too much (prompt problem) or
it misheard and answered the wrong question (STT problem). Read (or now, *listen to*)
the traces before deciding which — it's a symptom with two diseases, and this is
exactly the kind of question the per-turn trace tree exists to answer.

## The one-table summary

| Challenge | Demo answer (this repo) | Production answer |
|---|---|---|
| Latency | per-stage metrics, ~1 s V2V | budgets + p95/p99 dashboards |
| WER | nova-3, confirm-back in dialog | per-segment WER tracking, keyword boosting |
| Echo | browser AEC + headphones + VAD grace window | reference-signal AEC, debounced barge-in |
| Turn-taking | 300 ms endpointing + UtteranceEnd | adaptive/semantic end-of-turn |
| Interruption | VAD-onset trigger + turn tags + 4-step cancel | same idea, everywhere, tested hard |
| Long calls | rolling summarization, spoken-only history, photo→text memory | + structured state, RAG for manuals |
| Safety | prompt rules + scope block + manual-grounded LLM-judge (16 scenarios) | + output guardrail, red-teaming, SLO |
| Connections | KeepAlive, lazy reconnect, clean teardown | mid-call recovery with backoff |
| Scale/cost | 1 async process, ~$0.03–0.05/min math | sticky routing, cost per segment |
| Observability | per-turn Weave trace tree (transcript, latency, audio, cost) | + percentiles, retention, alerting |

If you remember one thing: **a voice agent is a real-time distributed system that
happens to contain an LLM.** The LLM is the easy part.
