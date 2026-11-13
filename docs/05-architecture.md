# How it all links together

[Doc 01](01-how-this-app-works.md) walks one utterance through the pipeline in prose;
this doc is the same system drawn as diagrams. Two diagrams: the static map of who
talks to whom, then one voice turn as a timeline.

One rule governs every box below, and it's worth stating before the pictures:
**nothing may regress voice-to-voice latency** (the hard rule running through
[doc 03](03-production-challenges.md)). Everything on the path from your voice to the
agent's voice streams; everything else — context summarization, for example — happens
at or after the turn boundary, once the caller has already heard the reply.

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
    end

    STT["Deepgram STT<br/>nova-3, streaming"]
    LLM["OpenAI LLM<br/>gpt-4o-mini, streaming + tools"]
    TTS["Deepgram TTS<br/>aura-2, streaming"]

    MIC -- "one WebSocket:<br/>audio both ways + JSON events" --> S
    CAM --> S
    S --> SPK
    S <--> STT
    S <--> LLM
    S <--> TTS
```

Reading the map:

- **One WebSocket** carries everything between browser and backend: binary mic audio
  up, binary TTS audio down, JSON events both ways. Each connection gets its own
  `Session` — the orchestrator that owns the other three connections (STT socket, TTS
  socket, in-flight LLM stream) and all conversation state.
- **Solid arrows are the hot path.** All three provider links stream: STT transcribes
  while you're still talking, the LLM streams tokens, TTS synthesizes sentence one
  while the model writes sentence three.

## One voice turn, as a timeline

```mermaid
sequenceDiagram
    participant B as Browser
    participant S as session.py
    participant STT as Deepgram STT
    participant LLM as OpenAI LLM
    participant TTS as Deepgram TTS

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
```

Reading the timeline:

- **It's pure overlap.** No stage waits for the previous one to finish;
  that's why voice-to-voice lands around a second instead of five (doc 01 does the
  arithmetic).
- **Not drawn, same spirit:** barge-in (the caller talking over the agent cancels the
  LLM stream, clears TTS, and zeroes the browser's ring buffer — doc 01 §barge-in),
  and canned speech (greeting, image filler, idle nudge), which bypasses the LLM
  entirely.

Where to go next: `server/session.py`'s docstring for the wire protocol the diagrams
abbreviate.
