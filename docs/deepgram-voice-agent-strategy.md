# Deepgram Voice Agent API — Strategy & Learning Notes

*What changes if we use Deepgram's managed Voice Agent product instead of
wiring STT + LLM + TTS ourselves — and the best practices worth adopting
either way.*

> All latency, WER, and pricing figures below are **Deepgram-reported**
> (vendor / promotional) and version-dependent. See Caveats before building.

---

## 1. TL;DR

- The managed **Voice Agent API** would absorb most of what we hand-built in
  `session.py`: turn-taking, barge-in, STT->LLM->TTS routing, and function
  execution — all over **one WebSocket** we currently orchestrate ourselves.
- **Flux** (GA Oct 2025) is the biggest single shift: it fuses transcription
  **and** semantic end-of-turn detection into one model, replacing our
  `speech_final` / `UtteranceEnd` endpointing logic.
- We keep what matters to us: our **OpenAI LLM**, our **system prompt**, and
  our **tools** (`book_engineer_visit`, `end_call`) — the API supports BYO-LLM
  and client-side functions, so none of that is thrown away.
- The trade is **less code for less control/observability**. Deepgram itself
  says the **cascade is the production default** (text at every stage =
  debuggable + auditable; HIPAA/SOC2-friendly); speech-to-speech is not yet
  production-ready for most.
- For our **teaching POC**, keep the explicit cascade — it is the pedagogical
  point. But adopt the portable best practices now (keyterm prompting, a
  9-scenario eval, confidence thresholds) and treat managed + Flux as the
  recommended **production** path.

---

## 2. Deepgram's current stack

| Stage | Model(s)              | Notes (Deepgram-reported)                          |
|-------|-----------------------|----------------------------------------------------|
| STT   | **Nova-3** (streaming)| median WER 6.84%; needs separate VAD/endpointing   |
| STT   | **Flux** (GA Oct '25) | fuses transcription + end-of-turn in ONE model     |
| TTS   | **Aura-2** (default)  | sub-200ms time-to-first-byte                       |
| TTS   | **Flux TTS** (`flux-kit-en`) | newer, adds expressivity control            |

Flux specifics: model `flux-general-en` (multilingual `flux-general-multi`
covers EN/ES/FR/DE/HI/RU/PT/JA/IT/NL); runs on `/v2/listen`, pin `version: v2`;
recommends **80ms** audio chunks; `smart_format` is **not** available; EOT
latency ~260ms.

Sources: Nova-3 <https://deepgram.com/learn/introducing-nova-3-speech-to-text-api> ·
Flux <https://deepgram.com/learn/introducing-flux-conversational-speech-recognition>

---

## 3. Three ways to build (Deepgram's own framing)

```
  ASSEMBLED (us)          BUNDLED (managed)         SPEECH-TO-SPEECH
  ------------            -----------------         ----------------
  mic                     mic                       mic
   |                       |                         |
  [STT]  <- our keys      [ Voice Agent API ]       [ single S2S model ]
   |                       |  STT->LLM->TTS          |
  [LLM]  <- our orch.      |  managed for you        | (audio in, audio out)
   |                       |                         |
  [TTS]  <- our code      [ one WebSocket ]          |
   |                       |                         |
  speaker                 speaker                   speaker
```

| Dimension      | Assembled (us)        | Bundled (managed API)      | Speech-to-speech       |
|----------------|-----------------------|----------------------------|------------------------|
| Control        | Maximum               | High (BYO-LLM/TTS escape)  | Low                    |
| Effort         | High (3 of everything)| Lowest                     | Low                    |
| Latency        | ~755ms TTFA*          | sub-200ms TTFB* (TTS)      | ~4-8s task completion  |
| Observability  | Full (text each stage)| Good (LatencyReport, text) | Opaque / hard to audit |
| Custom ASR     | Yes (on-prem too)     | No (Deepgram STT only)     | No                     |
| Cost model     | Per-component         | Flat ~$4.50/hr (Standard)  | Token cost ~up to 4x** |
| When to use    | Need custom/on-prem ASR, max control | Below ~10k min/mo, no custom ASR needed | Rarely (not prod-ready) |

\* Deepgram-reported: well-tuned streaming cascade ~755ms time-to-first-audio.
\*\* Token cost grows non-linearly (~up to 4x) with conversation length.

"Three API keys, three billing dashboards, three rate-limit policies, three
error-handling paths" is Deepgram's shorthand for the assembled burden — which
is exactly the middleware we wrote. Pricing: Standard ~$4.50/hr,
BYO-TTS ~$0.065/min, Advanced ~$9.75/hr.

Source: <https://deepgram.com/learn/voice-agent-api-architecture-bundled-vs-assembled> ·
<https://deepgram.com/learn/speech-to-speech-vs-cascade-voice-agent-architecture>

---

## 4. What the Voice Agent API would absorb

The managed agent runs over one socket:
`wss://agent.deepgram.com/v1/agent/converse` (EU variant exists; auth
`Authorization: Token <key>`). Flow: open socket -> send one `Settings`
message -> stream `Media` up, receive `Audio` + events down.

| Our component (today)              | Voice Agent API equivalent                         |
|------------------------------------|----------------------------------------------------|
| `session.py` turn state machine (LISTENING/THINKING/SPEAKING) | Managed orchestration + turn-taking |
| `stt.py` endpointing (`speech_final`/`UtteranceEnd`) | **Flux** semantic EOT (built in) |
| Barge-in: cancel LLM + TTS `Clear` + turn-tagging | Native barge-in ("avoids costly stream resets") + `UserStartedSpeaking` / `FunctionCallCancelled` |
| `llm.py` sentence chunker            | Internal chunking (agent handles TTS feeding)     |
| Metrics (voice-to-voice, TTFT, TTFB) | `LatencyReport` (per-turn STT/LLM/TTS breakdown)  |
| Our tools (`book_engineer_visit`, `end_call`) | `agent.think.functions[]` (JSON schema, client-side by default) |
| Rolling-summary context mgmt         | `agent.think.context_length` + `agent.context.messages` seed |
| Our OpenAI LLM + system prompt       | `agent.think` (BYO-LLM; `prompt`, live `UpdatePrompt`) |

**`Settings` configures:** `audio.input/output` (encoding, sample_rate);
`agent.listen.provider` (STT: flux-general-en / nova-3, keyterms, turn params);
`agent.think` (LLM provider+model, `prompt`, `functions[]`, `context_length`,
optional `endpoint` for BYO/self-hosted); `agent.speak.provider` (TTS:
deepgram/elevenlabs/cartesia/openai/aws_polly or custom); `agent.greeting`;
`agent.context.messages`.

**Client messages:** Settings, UpdatePrompt, UpdateSpeak, UpdateThink,
InjectUserMessage, InjectAgentMessage, ForceEndTurn, Media,
SendFunctionCallResponse, KeepAlive.
**Server events:** Welcome, SettingsApplied, UserStartedSpeaking (= barge-in),
ConversationText, AgentThinking, AgentStartedSpeaking, AgentAudioDone, Audio,
FunctionCallRequest, FunctionCallCancelled, LatencyReport, History, Warning,
Error.

**BYO matrix:** LLM **yes** (open_ai, anthropic, google, groq, aws_bedrock,
nvidia, or OpenAI-compatible `endpoint.url` + headers; fallback chains).
TTS **yes** (ElevenLabs/Cartesia/OpenAI/Polly/custom; discounted BYO-TTS tier).
STT **no** — locked to Deepgram (Nova-3/Flux).

Source: <https://developers.deepgram.com/docs/voice-agent> ·
config <https://developers.deepgram.com/docs/configure-voice-agent> ·
outputs <https://developers.deepgram.com/docs/voice-agent-outputs>

---

## 5. Flux — the biggest shift

Today our `stt.py` + `session.py` do turn detection by watching Deepgram's
`speech_final` and `UtteranceEnd` events and running our own state logic. Flux
collapses that: **transcription and semantic end-of-turn live in one model**,
so there is no separate VAD/endpointing layer to tune.

**The four turn events:**

```
  StartOfTurn     -> user began speaking  (drives barge-in)
  EagerEndOfTurn  -> speculative end       (start LLM early, off by default)
  TurnResumed     -> user kept talking     (cancel the speculative draft)
  EndOfTurn       -> confirmed end         (commit the turn)
```

**Tuning knobs:** `eot_threshold` (default 0.7), `eager_eot_threshold` (off by
default), `eot_timeout_ms` (default 5000).

**The eager-EOT trade:** firing the LLM on `EagerEndOfTurn` saves ~150-250ms,
but produces ~50-70% more LLM calls (drafts that may be cancelled by
`TurnResumed`). A latency-vs-cost dial, not a free win.

**Flux-reported gains** vs a classic STT+VAD setup: 200-600ms lower latency,
~30% fewer false interruptions, p90 ~1s / p95 ~1.5s.

**Not a drop-in:** Flux runs on the **v2** endpoint (`/v2/listen`, pin
`version: v2`), **`smart_format` is unavailable**, and it wants **80ms** audio
chunks — different from our current Nova-3 streaming setup. Adopting Flux means
reworking the STT client even if we keep our own orchestration.

Source: <https://developers.deepgram.com/docs/flux/quickstart>

---

## 6. Best practices to adopt now (even while staying cascaded)

Legend:  **[HAVE]** already do it · **[GAP]** worth closing

### Latency
- **[HAVE]** Stream every stage; never batch.
- **[HAVE]** Persistent WebSockets; per-turn metrics (we match `LatencyReport`).
- Target sub-300ms voice-to-voice / ~150ms TTS first-audio; colocate stages;
  Opus codec; right-size the LLM; prompt caching; minimize tokens.

### Turn-taking
- Prefer **semantic** end-of-turn (Flux) over silence/VAD; tune `eot_threshold`.
- **[GAP]** Eager-EOT as a concept — know the latency vs +50-70% LLM-calls dial.
- **[HAVE]** On barge-in, stop audio immediately **and** tell the LLM exactly
  what text was actually spoken — our "spoken-text-only memory" already does this.

### STT / TTS quality
- **[GAP]** **Keyterm prompting** for domain vocab — our appliance / part names.
  Up to 100 terms (20-50 recommended); audit real transcripts for OOV density.
- Pre-process audio: noise suppression, echo cancellation, VAD to strip silence.
- Expect production WER > benchmark (telephone ~17%+; stressed speech 25-35% higher).

### Prompt / spoken output
- Structured system prompt: role, personality (2-3 adjectives), scope, speaking
  style, function-calling rules, boundaries/escalation, and a "win condition."
- Plain text for TTS (no markdown/lists; spell out numbers and dates).
- Responses 1-2 sentences (~<120 chars), ideally ending in a question.
- Action-oriented tool instructions; never narrate before a function call.
- **[HAVE]** Prompt sets policy but **server code enforces** critical behavior —
  matches our safety stance.

### Evaluation
- **[GAP]** A **9-scenario** suite with written expected outcomes:
  happy path · mid-sentence corrections · agent-interruption · silence timeouts ·
  out-of-scope · background noise / false barge-in · accents · stressed speech ·
  wrong-number.
- Score **4 pass/fail metrics**: Task Completion, Turns to Resolution,
  Escalation Rate, False Statements. Keep LLM judges **binary** per question.
- **Re-run the whole suite on every prompt edit** — single-word changes swung
  results +/-28-46%. Test **per transport** (WebRTC / SIP).

### Deployment / cost
- Bundled flat per-hour vs assembled per-component billing.
- Managed is deployable cloud / VPC / on-prem.
- **[GAP]** Confidence-score thresholds to flag uncertain responses for review.

Sources: low-latency <https://deepgram.com/learn/low-latency-voice-ai> ·
prompting <https://developers.deepgram.com/docs/prompting-voice-agents> ·
testing <https://deepgram.com/learn/voice-agent-testing-evaluate-before-real-calls>

---

## 7. Trade-offs: gain vs give up

| We GAIN (managed)                         | We GIVE UP (managed)                       |
|-------------------------------------------|--------------------------------------------|
| Less orchestration code to own/maintain   | Fine-grained control of the turn loop      |
| One socket, one bill, one error path      | STT choice (locked to Deepgram)            |
| Native barge-in w/o stream resets         | Visibility into intermediate glue logic    |
| Flux semantic EOT out of the box          | The pedagogical "wire it yourself" value   |
| `LatencyReport` per-turn metrics for free | Freedom to colocate/self-host every stage  |
| Live prompt/voice updates mid-call        | Custom chunking / cancellation behavior    |
| Keep OpenAI LLM + prompt + tools (BYO)    | Some determinism in failure handling       |

---

## 8. Recommendation

For our **teaching POC**, keep the explicit cascade. Wiring STT + LLM + TTS
ourselves is the whole point — it gives full control and observability and is
what the docs are meant to teach. But:

1. **Adopt the portable best practices now.** They are path-independent wins:
   keyterm prompting for appliance/part names, a 9-scenario transport-aware eval
   with the 4 pass/fail metrics, and confidence thresholds for human review.
   We already do several (spoken-text-only memory, server-enforced safety,
   streaming every stage, per-turn metrics) — document those as deliberate.
2. **Treat managed Voice Agent API + Flux as the recommended production path**
   if/when this ships. Below ~10k min/month with no custom-ASR need, the flat
   per-hour model and absorbed orchestration are the pragmatic choice.
3. **Possible middle step:** swap our hand-rolled endpointing for **Flux** while
   keeping our own orchestration. It lets us feel the semantic turn-detection
   upgrade without surrendering the cascade — at the cost of a v2-endpoint
   rewrite (no `smart_format`, 80ms chunks).

Honest summary: managed removes real toil and Deepgram's own guidance favors the
cascade for production. The explicit build stays the right call **for learning**;
managed + Flux is the right call **for shipping**.

---

## 9. Caveats

- Every latency, WER, and pricing number here is **Deepgram-reported**
  (vendor / promotional) and **version-dependent**. Flux is **< 1 year old**
  (GA Oct 2025) and still maturing.
- **Re-verify model IDs and prices before building** — `flux-general-en`,
  `flux-general-multi`, `flux-kit-en`, Nova-3, Aura-2, and the ~$4.50/hr /
  ~$0.065/min / ~$9.75/hr tiers can all change.
- Deepgram's dedicated best-practices page returned **404** during research;
  the guidance above is stitched from several pages (prompting, low-latency,
  testing, architecture posts), so treat it as a reconstruction, not a single
  canonical doc.
- Pricing reference: <https://deepgram.com/pricing> ·
  GA post: <https://deepgram.com/learn/voice-agent-api-generally-available>
