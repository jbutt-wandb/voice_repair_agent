# How it all links together

[Doc 01](01-how-this-app-works.md) walks one utterance through the pipeline in prose;
this doc is the same system drawn as diagrams, with the observability layer — which
doc 01 predates — in the picture. Two diagrams: the static map of who talks to whom,
then one voice turn as a timeline.

One rule governs every box below, and it's worth stating before the pictures:
**nothing may regress voice-to-voice latency** (the hard rule running through
[doc 03](03-production-challenges.md)). Everything on the path from your voice to the
agent's voice streams; everything else — tracing, the vocal judge, context
summarization — happens at or after the turn boundary, once the caller has already
heard the reply.

## The system map

```mermaid
flowchart LR
    subgraph Browser["Browser (static/)"]
        MIC["mic → capture-worklet<br/>16 kHz PCM"]
        SPK["speakers ← playback-worklet<br/>24 kHz ring buffer"]
        CAM["photo upload"]
    end

    subgraph Server["Backend (server/)"]
        S["session.py — orchestrator<br/>state machine · barge-in · metrics"]
        subgraph Obs["observability — OFF the hot path"]
            VJ["vocal_judge.py<br/>acoustic emotion verdict"]
            WT["weave_trace.py<br/>builds the voice_turn span tree"]
            AL["agent_lens.py<br/>invoke_agent turn + audio bubbles"]
        end
    end

    STT["Deepgram STT<br/>nova-3, streaming"]
    LLM["OpenAI LLM<br/>gpt-4o-mini, streaming + tools"]
    TTS["Deepgram TTS<br/>aura-2, streaming"]
    WB[("W&amp;B Weave<br/>calls + agent spans")]

    MIC -- "one WebSocket:<br/>audio both ways + JSON events" --> S
    CAM --> S
    S --> SPK
    S <--> STT
    S <--> LLM
    S <--> TTS
    S -. "at the turn boundary" .-> VJ
    VJ -. "verdict" .-> WT
    VJ -. "verdict" .-> AL
    WT -. "calls store" .-> WB
    AL -. "agent-spans store (OTLP)" .-> WB
```

Reading the map:

- **One WebSocket** carries everything between browser and backend: binary mic audio
  up, binary TTS audio down, JSON events both ways. Each connection gets its own
  `Session` — the orchestrator that owns the other three connections (STT socket, TTS
  socket, in-flight LLM stream) and all conversation state.
- **Solid arrows are the hot path.** All three provider links stream: STT transcribes
  while you're still talking, the LLM streams tokens, TTS synthesizes sentence one
  while the model writes sentence three.
- **Dotted arrows are the side-channel.** One post-turn background task
  (`session.py::_judge_and_trace`) fans out: vocal judge → Weave `voice_turn` trace →
  Agent Lens turn (`agent_lens.py`), the last two landing in two *independent* stores
  (call traces vs agent spans) correlated by `session_id`. Unplug them (no
  `WANDB_API_KEY`) and the solid arrows don't change at all. Field-level detail:
  [doc 07](07-trace-shaping.md).

## One voice turn, as a timeline

```mermaid
sequenceDiagram
    participant B as Browser
    participant S as session.py
    participant STT as Deepgram STT
    participant LLM as OpenAI LLM
    participant TTS as Deepgram TTS
    participant J as Vocal judge
    participant W as W&B Weave

    B->>S: mic PCM frames (continuous, never paused)
    S->>STT: forward frames
    STT-->>S: interim transcripts (captions, barge-in fallback)
    STT-->>S: final transcript + speech_final (turn over)
    S->>LLM: history + user text, stream=True
    LLM-->>S: tokens…
    Note over S: SentenceChunker cuts at<br/>sentence boundaries
    S->>TTS: Speak + Flush, sentence by sentence
    TTS-->>S: 24 kHz PCM chunks (turn-tagged)
    S-->>B: audio chunks
    Note over B: playback starts — caller hears the reply<br/>(~1 s after they stopped talking)

    Note over S,W: AFTER the turn — background task, zero added latency
    S->>J: this turn's mic audio (16 kHz WAV)
    J-->>S: {frustrated, fearful} — gpt-audio-mini, forced tool call
    S->>W: voice_turn span tree: both audio streams,<br/>latency/token/cost attributes,<br/>vocal_frustrated + vocal_fearful in output
```

Reading the timeline:

- **The first half is pure overlap.** No stage waits for the previous one to finish;
  that's why voice-to-voice lands around a second instead of five (doc 01 does the
  arithmetic).
- **The turn boundary is where the deferred work goes.** A turn is spread across
  asyncio tasks, so rather than wrap live spans around it, `session.py` *collects* the
  turn's data as it happens and hands the finished bundle to `weave_trace.py` once the
  reply has been heard. The vocal judge runs first (one audio-model call on the
  caller's mic clip, bounded by a timeout), then the whole `voice_turn` tree is
  emitted with the verdict folded into its output. Judge fails or times out? The
  trace ships anyway, with `null` flags.
- **Not drawn, same spirit:** barge-in (the caller talking over the agent cancels the
  LLM stream, clears TTS, and zeroes the browser's ring buffer — doc 01 §barge-in),
  and canned speech (greeting, image filler, idle nudge), which bypasses the LLM
  entirely and is traced as `system_utterance`, not `voice_turn`.

Where to go next: [doc 06](06-weights-and-biases.md) for what the traces look like in
W&B and the lessons learned wiring it; `server/session.py`'s docstring for the wire
protocol the diagrams abbreviate.
