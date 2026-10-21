# Open-source voice agents: swapping Deepgram for Qwen & Kyutai

A *theoretical* design study. Today our speech edges are Deepgram (STT `nova-3`,
TTS `aura-2`). This doc asks: what would the same agent look like if those two
boxes were open-source models we host ourselves — Qwen (STT + TTS) and Kyutai
(CPU-capable TTS, plus Kyutai STT)? Nothing here is built yet; this is the map
for when we are ready.

> Specs below were verified against live sources in October 2026 (see
> **Sources** at the end). Model names/sizes/licenses move fast — re-check before
> committing.

---

## 1. Why this is easy for us

We built the pipeline as a **modular cascade we orchestrate ourselves**, not a
vendor's end-to-end bundle. Everything speech-vendor-specific lives behind two
thin seams:

- `server/stt.py` — emits `Transcript(text, is_final, speech_final)`,
  `UtteranceEnd`, `SpeechStarted`. Audio in = 16 kHz linear16 PCM.
- `server/tts.py` — accepts `speak(text)` / `flush()` / `clear()`, emits
  `AudioChunk(pcm)` / `Flushed` / `Cleared`. Audio out = 24 kHz linear16 PCM.

Everything else is vendor-agnostic and **stays unchanged**:

```
                 UNCHANGED                         SWAP TARGET
  +--------------------------------------+   +----------------------+
  | Browser: mic capture + ring-buffer   |   | stt.py backend       |
  | playback (AudioWorklets, 1 WebSocket) |   | tts.py backend       |
  | session.py: turn state machine,      |   +----------------------+
  |   barge-in, metrics                   |
  | llm.py: OpenAI stream, tools, chunker |   (orchestrator/front-end
  | agent.py / tools / safety prompts     |    talk only to the seams,
  +--------------------------------------+    never to Deepgram)
```

So "remove Deepgram" is really: write two new classes implementing the same
interface. The orchestrator, barge-in logic, metrics, tools, and frontend do not
care who synthesizes the audio — **as long as the new backends honor the same
event contract and PCM formats.** The caveats (endpointing, sample rate) are in §4.

---

## 2. Candidate models

| Model | Task | Streaming? | CPU-capable? | Approx size | License | Notes |
|---|---|---|---|---|---|---|
| `Qwen/Qwen3-ASR-0.6B` | STT | Yes (vLLM) | Weak — GPU expected | 0.6 B | Apache-2.0 | 52 langs; streaming mode drops batching + timestamps |
| `Qwen/Qwen3-ASR-1.7B` | STT | Yes (vLLM) | No (GPU) | 1.7 B | Apache-2.0 | SOTA among OSS ASR; strongest accuracy |
| `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | TTS | Yes (~97 ms e2e) | Possible, slow | 0.6 B | Apache-2.0 | Voice clone from 3 s sample; 10 langs |
| `Qwen/Qwen3-TTS-12Hz-1.7B-*` | TTS | Yes | No (GPU) | 1.7 B | Apache-2.0 | VoiceDesign / CustomVoice variants; top streaming TTFA |
| `kyutai/stt-1b-en_fr` | STT | Yes (~0.5 s delay) | Partial (MLX/Rust) | ~1 B | CC-BY-4.0 | **Built-in semantic VAD** (`--vad`); EN+FR |
| `kyutai/stt-2.6b-en` | STT | Yes (~2.5 s delay) | GPU | ~2.6 B | CC-BY-4.0 | Higher accuracy, English only, laggier |
| `kyutai/tts-1.6b-en_fr` | TTS | Yes (~220 ms) | GPU | ~1.8 B | CC-BY-4.0 | The voice in Unmute; Mimi codec @ 12.5 Hz |
| `kyutai/pocket-tts` | TTS | Yes (~200 ms TTFA) | **Yes — runs on CPU** | ~100 M | MIT | ~6× realtime on M4 using 2 cores; 7 langs; voice clone |

Reference stacks (not drop-ins, but worth knowing):

- **Mimi** — Kyutai's neural audio codec (12.5 Hz frame rate, 16/32 codebooks,
  ~220 ms latency). Both Kyutai STT and TTS are built on it; it is the
  tokenizer, not a product we call directly.
- **Moshi** — Kyutai's full-duplex speech-to-speech model. *Not* a cascade; it
  would replace our whole pipeline (STT+LLM+TTS), so it is out of scope here.
- **Unmute** — Kyutai's open, **modular** STT→LLM→TTS voice stack. This is a
  near-exact analogue of **our own architecture**: swappable LLM in the middle,
  optimized Kyutai STT/TTS on the edges, semantic-VAD turn detection. If we go
  the Kyutai route, Unmute is the reference implementation to crib from.

---

## 3. Proposed architectures

Three concrete options. In every one, `session.py`, `llm.py`, and the browser are
untouched; only the backends behind `stt.py` / `tts.py` change, plus new
local-model-serving glue.

### (a) All-Qwen, self-hosted on GPU

Best raw quality + widest language coverage; needs a GPU box.

```
  +-----------+   16kHz PCM   +---------------------------+
  | Browser   | ============> | stt.py  -> Qwen3-ASR-1.7B |
  | (mic/WS)  |               |   via local vLLM (GPU)    |
  +-----------+               +---------------------------+
        ^                                 | transcripts
        |  24kHz PCM                      v
        |                     +---------------------------+
        |                     | session.py (UNCHANGED)    |
        |                     |   + llm.py / OpenAI        |
        |                     +---------------------------+
        |                                 | sentences
        |                     +---------------------------+
        +==================== | tts.py  -> Qwen3-TTS-1.7B |
                              |   via local vLLM (GPU)     |
                              +---------------------------+
```

- **Runs where:** one CUDA GPU hosting both models (vLLM serving).
- **Plug-in:** `stt.py` opens a streaming request to the local vLLM ASR endpoint
  instead of Deepgram's WS; `tts.py` streams tokens→PCM from the local TTS server.
- **Latency:** TTS e2e as low as ~97 ms; ASR streaming adds model-dependent lag.
- **New glue:** stand up vLLM, a streaming adapter that maps Qwen token output to
  our `AudioChunk`/`Transcript` events, **and our own endpointer** (Qwen ASR
  ships no `speech_final`; see §4).

### (b) Kyutai STT + Kyutai Pocket-TTS (cheap / edge, no GPU for TTS)

The low-cost, on-prem story. TTS genuinely on CPU; STT small.

```
  +-----------+   PCM (Mimi)  +---------------------------+
  | Browser   | ============> | stt.py -> kyutai/stt-1b   |
  | (mic/WS)  |               |   Rust/MLX, semantic VAD  |
  +-----------+               +---------------------------+
        ^                                 | transcripts + VAD
        |  PCM                            v
        |                     +---------------------------+
        |                     | session.py (UNCHANGED)    |
        |                     |   + llm.py / OpenAI        |
        |                     +---------------------------+
        |                                 | sentences
        |                     +---------------------------+
        +==================== | tts.py -> kyutai/pocket-tts|
                              |   CPU, ~6x realtime (M4)   |
                              +---------------------------+
```

- **Runs where:** Pocket-TTS on **CPU** (2 cores, ~6× realtime on an M4);
  `stt-1b` on a small GPU or Apple-silicon MLX / Rust.
- **Plug-in:** Kyutai's Rust server exposes a streaming WS close in spirit to
  ours — `tts.py` feeds text and reads PCM frames; `stt.py` consumes transcript +
  the **semantic-VAD** signal and maps it to our `speech_final`.
- **Latency:** TTS ~200 ms TTFA; STT ~0.5 s delay (1b model).
- **New glue:** run Kyutai Rust/MLX servers; a resampler (Mimi/24 kHz ↔ our
  rates); map semantic VAD → `speech_final`/`UtteranceEnd`.

### (c) Hybrid — Qwen ASR + Kyutai TTS (best-of)

Qwen's broad-language, SOTA ASR up front; Kyutai's purpose-built streaming TTS
(or Pocket-TTS on CPU) out back.

```
  +-----------+   +--------------------+   +------------------------+
  | Browser   |-->| stt.py -> Qwen3-ASR |   | tts.py -> kyutai TTS   |
  | (mic/WS)  |   |   (GPU, 52 langs)   |   |  1.6b (GPU) OR pocket   |
  +-----------+   +--------------------+   |  (CPU) for cheap edge   |
        ^                 |                +------------------------+
        |                 v                            ^
        |        +--------------------------+          |
        +--------| session.py + llm.py (as-is)|---------+
                 +--------------------------+
```

- **Runs where:** ASR on GPU; TTS either GPU (`tts-1.6b`) or CPU (`pocket-tts`).
- **Why:** maximize ASR language coverage/accuracy while keeping TTS cheap or
  CPU-deployable. Two different serving stacks is the cost.
- **New glue:** both adapters from (a) and (b); two model servers to operate.

---

## 4. What changes in our code

The seams are designed for exactly this, but open models shift three assumptions:

**New backends behind the same interface.** Add e.g. `QwenSTT` / `KyutaiSTT`
classes and `QwenTTS` / `KyutaiTTS` / `PocketTTS` classes implementing the exact
methods and dataclasses in `stt.py`/`tts.py`. A config switch picks the backend;
`session.py` never changes.

**Endpointing / turn-taking — the real work.** Today Deepgram hands us
`speech_final` (tuned via `STT_ENDPOINTING_MS=300`) and `UtteranceEnd` as a
fallback. That is what drives end-of-turn.

- **Qwen3-ASR ships no endpointing/VAD.** We would have to **add our own VAD**
  (e.g. Silero/WebRTC VAD on the incoming PCM) and synthesize `speech_final` from
  trailing silence — re-implementing what Deepgram gave us for free. This is the
  biggest hidden cost of the Qwen route.
- **Kyutai STT ships a semantic VAD** (the `stt-1b` model, `--vad` flag) that
  predicts *finished speaking* vs *just pausing*. That maps cleanly onto our
  `speech_final`/`UtteranceEnd` and is arguably better than silence-timeout
  endpointing. Favor Kyutai STT if turn-taking quality matters.

**Barge-in.** `session.py` cancels the LLM and calls `tts.clear()` to flush
in-flight audio. Deepgram's `Clear` is a server-side discard. For local TTS we
get this cheaply: the generator loop lives in our process, so `clear()` just stops
pulling frames and drops the buffer — arguably simpler than the remote case.

**Audio format / sample rate.** Deepgram gave us 16 kHz in / 24 kHz out linear16.
Kyutai is built on the **Mimi codec @ 12.5 Hz frame rate** and its own PCM sample
rate; Qwen TTS tokenizer runs at 12 Hz. We will need **resampling adapters** on
both edges so the browser worklets (which assume 16 kHz capture / 24 kHz playback)
keep working untouched. Keep the resample in the backend, not the orchestrator.

**Serving glue (brand new).** None of these are SaaS WebSockets. We must stand up
local serving — vLLM for Qwen, Kyutai's Rust/MLX/PyTorch servers — plus streaming
adapters that translate each model's native token/PCM stream into our event
dataclasses. This is the bulk of the build.

---

## 5. Trade-offs vs Deepgram

| Dimension | Deepgram (today) | Qwen / Kyutai self-host |
|---|---|---|
| Latency | Very low, tuned SaaS | Qwen TTS ~97 ms, Kyutai TTS ~220 ms, Pocket ~200 ms; ASR adds 0.5–2.5 s depending on model |
| Quality / WER | Strong, production-hardened | Qwen3-ASR-1.7B SOTA among OSS, near commercial; smaller models weaker |
| Languages | Nova-3: broad multilingual | Qwen ASR 52 langs (best OSS coverage); Kyutai STT EN/FR only; Qwen TTS 10 langs, Pocket 7 |
| Endpointing | `speech_final` built in | Kyutai = semantic VAD (good); Qwen = **none, build our own** |
| Cost | Per-minute SaaS, no hardware | No per-minute fee; pay for GPU (a/c) or just CPU (b). Fixed infra cost, cheaper at scale |
| Ops burden | Near zero (vendor) | **High** — host/scale models, GPUs, serving stack, our own endpointer |
| Privacy / on-prem | Audio leaves our network | **Fully on-prem / air-gappable** — a real differentiator |
| Maturity | Battle-tested | Newer; permissive licenses (Apache-2.0 Qwen, MIT Pocket-TTS, CC-BY-4.0 Kyutai weights) but less production mileage |

Net: we trade a zero-ops SaaS bill for infrastructure + engineering, and buy
on-prem privacy, no per-minute cost at scale, and full control of the models.

---

## 6. Recommendation & open questions

**Recommended POC starting point:** architecture **(b)** — **Kyutai STT (`stt-1b-en_fr`)
+ Kyutai Pocket-TTS**, with Unmute as the reference. Rationale:

- Pocket-TTS (MIT, ~100 M) genuinely runs **on CPU** — we can prototype the whole
  TTS swap with no GPU, the explicit ask.
- Kyutai STT's **semantic VAD** means we are not forced to rebuild endpointing on
  day one — it maps onto our existing `speech_final` contract.
- Unmute proves the exact STT→LLM→TTS cascade works and gives us a blueprint.

Then, if language coverage or accuracy is short, graduate STT to **Qwen3-ASR-1.7B**
(architecture (c) hybrid) once we have a GPU and have built our own endpointer.

**Open questions to validate when we build:**

1. **Endpointing fidelity** — does Kyutai semantic VAD match the snappiness of our
   tuned `STT_ENDPOINTING_MS=300`? How do we expose/tune its threshold?
2. **Language scope** — Kyutai STT is EN/FR only. Do we need more? If yes, Qwen
   ASR up front from the start.
3. **CPU headroom** — Pocket-TTS is ~6× realtime on an M4; what is the concurrent
   per-server stream count on our actual (x86 server) CPUs?
4. **Resampling cost/quality** — Mimi/12.5 Hz and 12 Hz Qwen tokenizers vs our
   16 k/24 k worklet assumptions; verify a clean resample adapter adds negligible
   latency.
5. **Streaming adapter shape** — how closely do vLLM (Qwen) and Kyutai Rust WS
   streams map onto our `Transcript`/`AudioChunk` events, and where is backpressure?
6. **Barge-in + quality** — confirm local `clear()` stops audio within one frame
   and metrics still read cleanly; and does Pocket-TTS hit Aura-2 parity for our
   domain, or do we need the 1.6B Kyutai / 1.7B Qwen voices?

---

## Sources

- Qwen3-ASR: <https://github.com/QwenLM/Qwen3-ASR> ·
  <https://huggingface.co/Qwen/Qwen3-ASR-1.7B> ·
  <https://huggingface.co/Qwen/Qwen3-ASR-0.6B>
- Qwen3-TTS: <https://github.com/QwenLM/Qwen3-TTS> ·
  <https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign> ·
  <https://qwen.ai/blog?id=qwen3tts-0115>
- Kyutai STT/TTS (Delayed Streams Modeling):
  <https://github.com/kyutai-labs/delayed-streams-modeling> ·
  <https://huggingface.co/kyutai/stt-1b-en_fr> ·
  <https://huggingface.co/kyutai/stt-2.6b-en> ·
  <https://huggingface.co/kyutai/tts-1.6b-en_fr> · <https://kyutai.org/tts/>
- Kyutai Pocket-TTS: <https://github.com/kyutai-labs/pocket-tts>
- Kyutai Unmute: <https://kyutai.org/unmute/>
- Deepgram (current baseline): Nova-3 STT, Aura-2 TTS — <https://deepgram.com>
