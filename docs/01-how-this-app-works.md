# How this app works: the life of one spoken turn

This doc walks a single utterance — *"my washing machine won't drain"* — through
every stage of the app, from the vibration of your voice hitting the microphone to
the agent's voice coming out of your speakers about one second later.

If you've never built a voice agent, read this first. It assumes you know roughly
what a WebSocket and an HTTP request are, and nothing else.

## The one-sentence version

> The browser streams your microphone to the server continuously; the server
> streams it to Deepgram, which streams back text; when Deepgram decides you've
> finished talking, the server streams that text to an LLM, streams the LLM's
> reply sentence-by-sentence into Deepgram's text-to-speech, and streams the
> resulting audio back to your browser — which plays it instantly and can also
> throw it all away instantly if you interrupt.

The word doing all the work in that sentence is **streams**. Nothing in this app
waits for anything else to finish. That's the whole trick, and the rest of this
doc is about why and how.

## Why streaming everywhere?

Imagine the naive design — a chain of ordinary request/response calls:

```
record whole utterance ──▶ upload file ──▶ STT request ──▶ full transcript
   ──▶ LLM request ──▶ full reply text ──▶ TTS request ──▶ full MP3 ──▶ play
```

Each arrow waits for the previous step to *completely finish*. Add it up: a
5-second utterance is uploaded after it ends (+5s of silence while you record),
STT on the whole file (~1s), the LLM writes its entire reply (~2–4s), TTS
synthesizes the entire reply (~1–2s), and only then does playback start. The user
sits in dead air for 4–8 seconds. In human conversation, a reply gap over ~1.5
seconds already feels broken; people start saying "hello? are you there?"

The streaming design overlaps everything:

- STT transcribes **while you're still talking** — by the time you stop, the
  transcript is already essentially done.
- The LLM **streams tokens** — we don't wait for its full reply.
- TTS synthesizes **sentence one while the LLM is still writing sentence three**.
- The browser plays **the first audio chunk while the rest is still being made**.

Measured on this app, the user hears the reply start about **1 second** after they
stop talking (we call this *voice-to-voice latency*). Every stage of that second is
measured and shown in the UI's metrics panel.

## The cast of characters

```
  BROWSER (static/)
  ┌────────────────────────────────────────────────┐
  │ mic ──▶ capture-worklet ──▶ 16 kHz PCM         │
  │ speakers ◀── playback-worklet ◀── 24 kHz PCM   │
  │ app.js: shows transcript, state, metrics       │
  └────────────────────────┬───────────────────────┘
                           │  one WebSocket
                           │  (audio both ways + JSON)
  SERVER (server/)
  ┌────────────────────────┴───────────────────────┐
  │ session.py  —  the orchestrator                │
  │ sequences three streaming stages:              │
  │                                                │
  │   stt.py  ──▶  Deepgram  (speech → text)       │
  │   llm.py  ──▶  OpenAI    (agent.py + manuals)  │
  │   tts.py  ──▶  Deepgram  (text → speech)       │
  └────────────────────────────────────────────────┘
```

- `static/capture-worklet.js` — turns your mic into a stream of small audio frames
- `static/playback-worklet.js` — turns a stream of audio chunks back into sound
- `static/app.js` — glues the worklets to the WebSocket and renders the UI
- `server/session.py` — **the orchestrator**: one instance per connected browser,
  owns the conversation state machine. If you read one file, read this one.
- `server/stt.py` / `server/tts.py` — thin wrappers around Deepgram's two
  streaming APIs (covered in depth in [doc 02](02-how-deepgram-solves-the-problem.md))
- `server/llm.py` — streams the OpenAI reply, handles the booking tool
- `server/agent.py` — builds the system prompt from the product manuals; this is
  where the safety rules live
- `server/config.py` — every tuning knob, each with a comment about its tradeoff

## Stage 1: the microphone (and why 16 kHz int16)

When you click **Start call**, `app.js` asks for the microphone:

```js
navigator.mediaDevices.getUserMedia({
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
});
```

(That `echoCancellation: true` is load-bearing — see the barge-in section.)

The browser delivers audio as float32 samples at the *hardware* rate, usually
48,000 samples per second. That's far more data than speech recognition needs:
human speech lives almost entirely below 8 kHz, and by the Nyquist rule a 16 kHz
sample rate captures everything up to 8 kHz. Speech models are trained on 16 kHz
audio; sending 48 kHz would just triple your bandwidth for zero accuracy.

So `capture-worklet.js` — a tiny program that runs on the browser's dedicated
real-time audio thread (an *AudioWorklet*) — does three things to each 128-sample
block the browser hands it:

1. **Downsample** 48 kHz → 16 kHz by linear interpolation. Because 48000/16000
   isn't always the ratio (some machines run at 44.1 kHz), the worklet keeps a
   fractional read position (`this.phase`) that carries across blocks, so there's
   never a seam between chunks.
2. **Convert** float32 (range −1.0..1.0) → int16 (range −32768..32767). Halves the
   size again; this format is called *linear16 PCM* and it's what Deepgram expects.
3. **Batch** into 640-sample frames = **40 ms of audio = 1280 bytes**, and post
   each frame to the main thread, which sends it on the WebSocket.

Why 40 ms? It's a classic streaming sweet spot: small enough that latency added by
batching is negligible, large enough that you're not drowning in per-message
overhead (25 messages/second instead of 375).

## Stage 2: one WebSocket, two kinds of traffic

The browser holds exactly **one** WebSocket to the server (`/ws`). WebSocket
frames are either *binary* or *text*, and the app exploits that to avoid inventing
a framing protocol:

- **Binary, browser → server**: mic PCM frames (16 kHz int16)
- **Binary, server → browser**: TTS audio chunks (24 kHz int16)
- **Text (JSON), both ways**: everything else — transcripts, agent text, state
  changes, metrics, the barge-in signal, the `call_ended` hang-up signal, and a
  `text_input` message that lets you type instead of speak (same pipeline minus
  STT; invaluable for testing).

The full message catalog is documented in the docstring at the top of
`server/session.py`. Since binary frames only ever mean one thing per direction,
no header bytes are needed — `isinstance(msg, bytes)` is the entire parser.

On the server, each connection gets its own `Session` object with its own Deepgram
connections and its own conversation history. Three async tasks run for the whole
call:

- `_browser_rx` — receives from the browser: binary → forward to STT; JSON → handle
- `_stt_rx` — receives transcription events from Deepgram
- `_tts_rx` — receives synthesized audio from Deepgram, forwards it to the browser

A crucial, easy-to-miss design decision: **mic audio is forwarded to Deepgram in
every state, even while the agent is talking.** The STT stream never pauses.
That's what makes interruption detection possible (below), and it also keeps the
STT connection alive (Deepgram closes connections that go silent).

## Stage 3: Deepgram turns sound into text — and decides when you're done

Deepgram streams back `Results` messages as you speak. Three flags matter
(see `server/stt.py` and doc 02 for the full story):

- **interim results** (`is_final: false`) — fast, revisable guesses. You see these
  as the gray italic text in the UI that updates as you talk.
- **`is_final: true`** — this piece of transcript is locked in; the session
  accumulates these into `utterance_parts`.
- **`speech_final: true`** — the *endpointer* heard 300 ms of silence after your
  speech (the `STT_ENDPOINTING_MS` knob in `config.py`) and declares: *the user is
  done talking.* This is the starting gun for everything that follows.

There's also a fallback: if `speech_final` never arrives (noisy room), Deepgram's
`UtteranceEnd` message triggers end-of-turn from the finalized words we already
have. Both paths funnel into `session.py::_end_of_user_turn`.

This moment — "the user is done" — is timestamped as `t_turn_end`, and every
latency number you see in the UI is measured from it.

## Stage 4: the state machine

`session.py` models the conversation as three states and a turn counter:

```
            speech_final                first TTS audio
 LISTENING ──────────────▶ THINKING ──────────────────▶ SPEAKING
     ▲                                                     │
     └──────────────── reply finished ────────────────────┘
          (all sentences flushed & the LLM is done)

 BARGE-IN:  user speaks during THINKING or SPEAKING
            ──▶ cancel everything, return to LISTENING
```

(There's one more way out of this loop: when the customer says they're done, the
agent hangs up — see "Hanging up" below. And between turns, if the conversation
history has grown large, it gets compressed in the background — doc 03 §6.)

Why so explicit? Because a voice conversation is full of *races*. Audio is in
flight in four places at once (browser buffer, network, Deepgram's buffer, the
LLM mid-sentence), and the state machine plus an incrementing `turn_id` is how the
code always knows which turn any given byte belongs to. Plain request/response
code has no such problem — and no such capability.

## Stage 5: the LLM thinks (with the manuals in its pocket)

On end-of-turn, the user's text is appended to the conversation and sent to OpenAI
(`gpt-4o-mini`) with `stream=True`. The system prompt — assembled once per session
in `server/agent.py` — contains:

1. The agent's role and *spoken-style rules* (1–3 sentences, one question at a
   time, no markdown — this text will be read aloud!)
2. Hard safety rules: only suggest actions from a manual's "Safe Troubleshooting"
   section; refuse anything in "DO NOT — Specialist Only"; hold the refusal if the
   user insists; offer an engineer visit instead.
3. A diagnose-basics-first rule: before any fix, ask the universal counter-questions
   one at a time — plugged in? socket working? lights or display on? error code? did
   anything change recently? — and never offer an engineer in the first reply to a
   new problem. Booking is the *last* resort, not the first reflex.
4. Rules for two tools: `book_engineer_visit` (a simulated scheduling backend — it
   always confirms) and `end_call` (hangs up once the customer says they're done).
5. **All four product manuals, verbatim** (~4k tokens). No retrieval system
   needed at this scale, and the model always sees the complete safety contract.
6. Today's date, so "tomorrow" means something.

When the model wants to book an engineer, it emits a *tool call* instead of text.
`llm.py::agent_reply` executes the booking, feeds the confirmation back, and runs
a second streamed completion — that's the text the agent speaks ("Your engineer
visit is confirmed for tomorrow morning, the reference is ENG-4246"). This
two-pass tool loop is written out explicitly in the code rather than hidden in a
framework, because seeing it is the point.

## Stage 6: sentence chunking — the bridge between LLM and TTS

Here's the subtle part. The LLM emits tokens — a few characters at a time. TTS
wants *phrases*: synthesize word-by-word and the prosody (the melody of speech)
sounds robotic; wait for the whole reply and you've thrown away the streaming
advantage.

`llm.py::SentenceChunker` buffers tokens just until a sentence boundary (`.`, `!`,
`?` followed by whitespace, with a ~20-character minimum so "Yes." rides along
with the next sentence instead of being spoken alone). Each complete sentence is
immediately sent to Deepgram TTS with a `Speak` message followed by a `Flush`
("synthesize what you have now").

So the timeline of a reply looks like:

```
LLM:  [Sure, let's check th][e drain pump filter.][ First, unplug the ][machine...
TTS:                        ▲ sentence 1 sent ──▶ audio streaming back
                                                 ▲ sentence 2 sent (while s1 plays)
```

The user hears sentence one while sentence two is still being written.

## Stage 7: playback — a ring buffer, not a queue

TTS audio arrives at the browser as raw 24 kHz int16 chunks (no decoding needed —
we deliberately chose an uncompressed format). `app.js` converts each chunk to
float32, resamples up to the hardware rate, and posts it to
`playback-worklet.js`, which writes it into a **ring buffer** — one big circular
array with a write pointer and a read pointer. The audio thread's `process()`
callback just reads 128 samples per tick, or outputs silence if the buffer is
empty.

Why not the "obvious" approach of queueing each chunk as an `AudioBuffer` and
scheduling it with `source.start()`? Two reasons:

1. Unless your scheduling is sample-accurate, you get audible clicks at every
   chunk seam.
2. You can't *un-schedule* a half-played buffer cleanly. With a ring buffer,
   stopping playback instantly is one line: set the read pointer equal to the
   write pointer. Which brings us to…

## Barge-in: interrupting the agent

Try it: ask the agent something, and while it's answering, just start talking.
The agent's voice cuts off within a fraction of a second and it listens to you.
This is called **barge-in**, and it's the single hardest behavior in any voice
agent, because at the moment you interrupt, the "old" reply exists in four places
simultaneously:

1. the LLM is still generating tokens for it,
2. Deepgram TTS has text queued and audio mid-synthesis,
3. audio bytes are in flight over two networks,
4. the browser's ring buffer holds seconds of not-yet-played sound.

**Detection** first: remember that mic audio streams to Deepgram continuously,
even while the agent speaks. So when you talk over the agent, Deepgram produces an
interim transcript for your new speech. The rule in `session.py::_stt_rx` is: *a
non-empty interim transcript arriving while the state is THINKING or SPEAKING = a
barge-in.* We use transcripts rather than raw "voice activity" events because
transcripts require actual words — a cough or a door slam doesn't produce one.

**Cancellation** then has to kill all four in-flight copies. From
`session.py::_barge_in`, in order:

```
1. turn_id += 1          every TTS chunk is implicitly tagged with the turn that
                         requested it (self.tts_turn); after the bump, in-flight
                         chunks identify themselves as stale and _tts_rx drops them
2. cancel the LLM task   closes the OpenAI stream (also stops paying for tokens)
3. send Clear to TTS     Deepgram discards synthesized-but-unsent audio
4. send barge_in JSON    the browser zeroes the ring buffer: read_pos = write_pos
   to the browser        — instant silence
```

Then the state returns to LISTENING, and your interrupting speech becomes the next
turn's input. The UI marks the cut-off reply with "— interrupted —".

(Why not trust Deepgram's `Cleared` acknowledgment to know when the stale audio
has stopped? Because we tested it, and audio can still arrive *after* the ack.
That war story is in [doc 02](02-how-deepgram-solves-the-problem.md).)

**What the agent remembers after you interrupt.** There's a subtle fifth cleanup:
the conversation *history*. If the LLM generated three sentences but you cut it off
after one, what should the transcript say the agent said? This app records only
what was actually delivered — the model's memory of the call matches the
customer's. Two paths in `session.py`: if the LLM was cancelled mid-stream, the
history gets just the sentences already sent to TTS plus a
`[cut off here by the customer]` marker; if the LLM had finished but the *audio*
was cut mid-playback, the stored reply is truncated to the sentences whose audio
was fully flushed. Without this, the model later "remembers" saying things the
user never heard — and conversations go subtly, confusingly wrong.

One dependency to know about: detection assumes the mic hears *you*, not the
agent's own voice coming out of your speakers. The browser's echo cancellation
(`echoCancellation: true`) handles this most of the time, but with loudspeakers
and a sensitive mic the agent can occasionally interrupt itself. Headphones make
the demo bulletproof. In production telephony this is a whole engineering topic —
see [doc 03](03-production-challenges.md).

## Hanging up: the natural end of a call

A phone agent that never hangs up is always listening — creepy and wasteful. This
app gives the *model* the off switch: an `end_call` tool next to the booking tool.
When the problem is fixed or booked, the agent asks "anything else I can help you
with?"; if you say you're all set, the model invokes `end_call`, and the goodbye is
spoken by the second pass of the same two-pass tool loop as the booking flow.

Then the teardown has its own ordering problem: hang up too early and you clip the
goodbye mid-word. The sequence (`session.py` + `app.js`):

```
model invokes end_call ──▶ session sets ending=True
goodbye sentences ──▶ TTS ──▶ last Flushed ack received
                              │
                              ▼
server sends {type:"call_ended"} ──▶ client waits for the playback worklet's
(+ schedules a 15s fallback close)   'drained' signal (goodbye finished PLAYING)
                                      │
                                      ▼
                        client hangs up: closes WS, stops mic, shows "call ended"
```

Note what the server *can't* know: its last audio byte being sent doesn't mean the
user has heard it — the client's ring buffer may hold seconds of goodbye. So the
client owns the final hang-up moment, and the server's delayed close is only a
safety net for a vanished tab. Pressing **Start call** afterwards begins a
completely fresh session: new `Session` object, new history, reset UI.

Two details worth noticing: barge-in during the goodbye *un-ends* the call ("wait,
actually—" works right up to the last second), and the goodbye is generated
*after* the tool call, not alongside it. We first prompted "say goodbye AND call
the tool in the same reply" — and at live temperature the model sometimes wrote
`(end_call: {...})` as literal text instead of invoking the function. Tool-first,
speak-second fixed it (the full lesson is in
[doc 03](03-production-challenges.md)).

## Stage 8: measuring everything

Every turn produces a `TurnMetrics` record (`session.py`), shown live in the UI
and logged as one structured line:

| Metric | What it measures | Typical here |
|---|---|---|
| `stt_final_ms` | how long the endpointer waited after your last word | ~300–800 ms |
| `llm_ttft_ms` | LLM request → first token | ~500 ms |
| `tts_ttfb_ms` | first `Speak` → first audio byte back | ~200 ms |
| `v2v_server_ms` | you stopped talking → first audio byte sent | ~1 s |
| `v2v_client_ms` | you stopped talking → first audio you actually *heard* | +~50 ms |
| `barge_in` | was this turn interrupted? | — |

Notice the stages roughly add up: endpoint wait + TTFT + TTFB ≈ voice-to-voice.
That's the point of per-stage metrics — when the total is too slow, you know
*which* stage to attack.

## Recap: the full journey in 12 steps

1. Mic captures 48 kHz float32 → worklet downsamples to 16 kHz int16, 40 ms frames
2. Frames stream over the WebSocket to the server, continuously, in every state
3. Server forwards every frame to Deepgram live STT
4. Deepgram streams back interims (live captions) and finals (locked-in text)
5. 300 ms of silence → `speech_final` → the turn is over (`t_turn_end`)
6. Session enters THINKING, sends conversation + manuals to OpenAI, streaming
7. Tokens stream back; the SentenceChunker cuts them at sentence boundaries
8. Each sentence → `Speak` + `Flush` on the Deepgram TTS WebSocket
9. Audio chunks stream back, get tagged by turn, and are forwarded to the browser
10. First chunk forwarded → state SPEAKING; browser ring buffer plays it
11. All sentences flushed + LLM done → back to LISTENING
12. At any point during 6–11, user speech triggers barge-in: bump turn_id, cancel
    LLM, Clear TTS, zero the ring buffer, store only the spoken sentences in
    history, go to LISTENING
13. Between turns, if history has grown past the threshold, old turns are
    compressed into "call notes" in the background (doc 03 §6)
14. When the user says they're done, the model invokes `end_call`, says goodbye,
    and the client hangs up after the goodbye finishes playing

Next: [how Deepgram specifically solves the hard parts →](02-how-deepgram-solves-the-problem.md)
