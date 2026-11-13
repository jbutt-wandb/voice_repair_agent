# Explainer docs

Beginner-friendly companions to the [main README](../README.md) (which stays the
quick reference). Read in order:

0. **[00-original-prompt.md](00-original-prompt.md)** — the verbatim brief that
   kicked off the project, so you can see where it all started.
1. **[01-how-this-app-works.md](01-how-this-app-works.md)** — the life of one
   spoken turn: mic → STT → state machine → LLM → sentence chunking → TTS →
   ring-buffer playback, why everything streams, how barge-in works (now on VAD
   onset), how photos ride the pipeline, and how the agent hangs up when you're done.
2. **[02-how-deepgram-solves-the-problem.md](02-how-deepgram-solves-the-problem.md)** —
   the real-time STT/TTS problems and how Deepgram's APIs solve them: endpointing,
   interim results, Speak/Flush/Clear — plus the two gotchas we hit in testing.
3. **[03-production-challenges.md](03-production-challenges.md)** — what gets hard
   in the wild: latency tails, WER, echo, turn-taking, cancellation correctness,
   context growth (incl. photo→text memory), safety guardrails and the scope block,
   scaling, cost, and observability — all added under one rule: never regress
   voice-to-voice latency.
4. **[04-building-this-with-an-agent.md](04-building-this-with-an-agent.md)** — the
   *process* story: how this app was built in collaboration with a coding agent,
   and a transferable playbook for driving one yourself.
5. **[05-architecture.md](05-architecture.md)** — how it all links together, in
   diagrams: the system map (browser ↔ session ↔ STT/LLM/TTS) and one voice turn
   as a timeline.

## Strategy & research notes

Forward-looking design studies (not part of the beginner walkthrough above):

- **[deepgram-voice-agent-strategy.md](deepgram-voice-agent-strategy.md)** — what
  changes if we use Deepgram's managed Voice Agent API (and Flux) instead of wiring
  the cascade ourselves, plus best practices worth adopting either way.
- **[open-source-voice-agents.md](open-source-voice-agents.md)** — a theoretical
  swap of Deepgram for self-hosted open-source models (Qwen STT/TTS, Kyutai
  CPU-capable TTS + STT), and what our modular architecture would look like.

Reading the code afterwards? Start at `server/session.py` — its docstring is the
wire protocol and the state machine; everything else hangs off it.
