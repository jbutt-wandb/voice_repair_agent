# How Deepgram is used: real-time speech, both directions

This doc zooms in on the two Deepgram services this app depends on — live
speech-to-text (STT) and live text-to-speech (TTS) — and, more importantly, on the
*problems* they solve. If you understand the problems, the API design stops
looking arbitrary.

Code references: `server/stt.py` (one connection to `wss://api.deepgram.com/v1/listen`)
and `server/tts.py` (one connection to `wss://api.deepgram.com/v1/speak`), both via
the official `deepgram-sdk`. Tuning knobs live in `server/config.py`.

---

## Part 1: speech → text, while the speech is still happening

### The problem

A conversation is not a dictation. If you record the user, wait for them to stop,
upload the file, and transcribe it, you've added the entire utterance length plus
transcription time to your reply latency — multiple seconds of dead air. Worse,
batch transcription can't answer the two questions a conversational agent asks
constantly:

1. **What is the user saying right now?** (needed for live captions and for
   detecting that they're talking over the agent)
2. **Have they finished their thought?** (needed to know when to reply)

Question 2 is sneaky-hard. Humans do it with grammar, intonation, and context.
Software has to decide: is this pause a comma, or the end?

### Deepgram's answer: a streaming WebSocket with three kinds of "text"

You open a WebSocket, send raw audio frames as fast as you capture them (16 kHz
linear16 mono here — the parameters are in the connection call in `stt.py`), and
Deepgram sends back `Results` messages *while the user is still talking*, using
the `nova-3` model. Each result carries two flags that split transcripts into
three kinds:

| Kind | Flags | Meaning | What this app does with it |
|---|---|---|---|
| Interim | `is_final: false` | Fast guess; may be revised | live gray captions; **barge-in detection** |
| Final | `is_final: true` | This span of text is locked in | accumulate into the user's turn |
| Speech-final | `speech_final: true` | ...and the utterance is OVER | trigger the agent's reply |

Watch the UI while you talk and you can see the lifecycle: gray italic text that
rewrites itself ("my washing" → "my washing machine won't") and then snaps into a
solid bubble when finalized.

Interims are a two-for-one deal. They power the captions, but they're also the
interruption detector: because this app streams mic audio in *every* state, an
interim with actual words arriving while the agent is speaking means the user is
talking over it → barge-in (`session.py::_stt_rx`). Deepgram also offers raw
voice-activity events (`SpeechStarted`), but a transcript is a much stronger
signal than "sound happened" — a cough doesn't produce words.

### Endpointing: the turn-taking primitive

`speech_final` comes from Deepgram's **endpointer**: a voice-activity model that
watches for trailing silence. The knob is in the connection parameters:

```
endpointing=300        # config.py: STT_ENDPOINTING_MS
```

meaning: *300 ms of silence after speech → declare the utterance finished.* This
single number IS your agent's conversational reflexes, and it's a genuine tradeoff
with no right answer:

- **Lower (150–300 ms)**: snappy replies; but pause mid-sentence to think —
  "my serial number is… umm…" — and the agent jumps in and cuts you off.
- **Higher (500–1000 ms)**: patient with slow speakers; but every single reply now
  starts that much later, and the whole conversation feels laggy.

In this app's metrics panel, the `stt_final_ms` column shows what endpointing
costs you per turn (~300–800 ms observed). Production systems tune this per
context or add smarter "semantic end-of-turn" models on top — see
[doc 03](03-production-challenges.md).

### The safety net: UtteranceEnd

Endpointing works on the audio signal, and noisy audio can starve it — background
chatter means "silence" never quite happens, and `speech_final` never fires. For
that, the connection also sets `utterance_end_ms=1000`: Deepgram watches the
*word timings* in the transcript instead of the raw audio, and if ≥1 s passes
with no new words, it sends an `UtteranceEnd` message.

`session.py` treats it as the fallback trigger: if we're still LISTENING and
finalized words have piled up with no `speech_final`, `UtteranceEnd` ends the turn.
Two independent signals, one decision — a pattern you'll see throughout real-time
systems.

### Connection lifecycle: silence will kill you (twice)

Two things we learned (one from docs, one the hard way):

**1. Deepgram closes idle connections.** ~10 seconds without audio and the STT
socket dies. This app streams mic audio continuously, so it's rarely an issue, but
`stt.py` still runs a `KeepAlive` loop (a tiny `{"type": "KeepAlive"}` JSON
message every 5 s) as belt-and-braces — e.g. for a session where the mic never
starts (text-only mode).

**2. Real gotcha #1: endpointing needs trailing silence *audio*, not absence of
audio.** Our first automated STT test synthesized a sentence, streamed it in,
then stopped sending frames and waited for `speech_final`. It hung forever.

Why: the endpointer measures silence *in the audio it receives*. No frames = no
audio = no silence = no decision. It's not a timeout; it's a signal detector.

The fix (see the smoke test in `stt.py::_smoke_test`) was to append two seconds of
actual zero-valued samples:

```python
raw = speech_pcm + b"\x00" * (16000 * 2 * 2)   # 2s of real silence @16kHz int16
```

A live microphone never hits this — an open mic sends "silence" as actual
near-zero frames — but any test, file-replay, or push-to-talk design will.
Push-to-talk UIs that stop sending on release either need to send a `Finalize`
message or pad silence.

---

## Part 2: text → speech, while the text is still being written

### The problem

The LLM produces text as a stream of tokens. The user wants to hear a voice, now.
Between them you need a TTS engine that can:

1. accept text **incrementally** (we don't have the full reply yet),
2. start returning audio **fast** (every ms here is added to reply latency),
3. **cancel** audio it has already synthesized but not yet delivered (barge-in).

A classic REST TTS endpoint ("POST text, receive MP3") fails all three: one
request per sentence pays connection/queueing overhead each time, and there is no
way to un-send a response you're halfway through downloading.

### Deepgram's answer: the speak WebSocket

One persistent connection (`server/tts.py`, model `aura-2-thalia-en`), three
upstream message types, and that's nearly the whole protocol:

| Message | Meaning |
|---|---|
| `{"type": "Speak", "text": "..."}` | queue this text for synthesis |
| `{"type": "Flush"}` | synthesize everything queued, send me the audio |
| `{"type": "Clear"}` | throw away everything not yet delivered |

Downstream you get binary audio chunks plus two acks: `Flushed` ("all audio for
that flush has been sent" — this app counts these to know when the agent has
finished a reply) and `Cleared`. The same `Flushed` accounting also gates the
hang-up moment: when the agent ends the call, the goodbye's final `Flushed` is
what triggers the `call_ended` signal to the browser (see doc 01).

The app's usage pattern (see `session.py::_speak`): every time the sentence
chunker completes a sentence, send `Speak` + `Flush`. Audio for sentence one
streams back while the LLM is still writing sentence two. Measured time from
first `Speak` to first audio byte (**TTFB**) is ~130–250 ms — it's the `tts_ttfb`
column in the UI.

### Why linear16 and not MP3?

The connection asks for `encoding=linear16, sample_rate=24000`: raw, uncompressed
16-bit samples. That's ~48 KB/s — chunky, but this is localhost. In exchange, the
browser does **zero decoding**: int16 → float32 is a multiply, and the samples go
straight into the playback ring buffer. No decoder latency, no codec edge cases,
and the playback worklet stays ~40 lines. On a bandwidth-constrained production
path you'd pick Opus and pay the decoder complexity.

### Real gotcha #2: audio can arrive AFTER the Cleared ack

This one cost us a debugging session, and it's the best protocol lesson in the
project.

For barge-in, the server sends `Clear` and must then drop any stale audio still
arriving for the cancelled reply. The obvious design: set a `dropping = True`
flag on `Clear`, set it back to `False` when the `Cleared` ack arrives — since
WebSocket messages are ordered, nothing stale can follow the ack… right?

Wrong. Our end-to-end test (`scripts/e2e_check.py`, phase C) kept failing with an
impossible measurement: the new reply's "first audio" arriving 12 ms after the
turn started — faster than the LLM can even begin responding (~500 ms). A stale
chunk from the *cancelled* reply was sneaking through.

The explanation: `Clear` discards Deepgram's *buffered* audio, and `Cleared`
acknowledges that — but audio for text already **mid-synthesis** can still be
generated and sent *after* the ack. The ack means "I cleared my buffer," not
"you will never see old audio again."

The robust fix doesn't trust acks at all. `session.py` tags audio by turn:

```python
self.tts_turn = self.turn_id      # set whenever we send a Speak (session._speak)
...
stale = self.tts_turn != self.turn_id    # checked for every received chunk
if stale: continue                        # _tts_rx drops it
```

Barge-in bumps `turn_id`, so every chunk belonging to the old reply identifies
itself as stale until the *new* turn's first `Speak` re-tags the stream. `Clear`
is still sent — it stops Deepgram wasting synthesis — but correctness never
depends on when (or whether) its ack arrives.

General lesson: **in streaming systems, don't gate correctness on
acknowledgments; make the data self-identifying.**

### One more lifecycle note: lazy reconnect

Between agent turns the TTS socket can sit quiet for a minute or more, and
Deepgram may idle it out. `tts.py::speak` handles this by reconnecting
transparently if the send fails, and `tts.py::events` survives the swap. In
production you'd add backoff and pre-warmed connections; the demo documents the
problem and solves it the simple way.

---

## The scorecard

What Deepgram contributes to this app's ~1-second voice-to-voice loop:

| Stage | Deepgram feature | Measured |
|---|---|---|
| hearing you | nova-3 streaming STT, interim results | transcript ready ~as you finish |
| knowing you're done | endpointing (300 ms) + UtteranceEnd fallback | `stt_final` ~300–800 ms |
| noticing interruptions | continuous STT + interim transcripts | barge-in trigger |
| speaking | aura-2 over the speak WebSocket | TTFB ~200 ms |
| shutting up | `Clear` + (our) turn tags | audio stops <200 ms after you speak |

Next: [what breaks when you take this to production →](03-production-challenges.md)
