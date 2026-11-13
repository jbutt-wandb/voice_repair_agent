# Evals — research and proposal

Research effort (2026-11-13) into how to evaluate this voice repair agent, distilled into one
proposal. Six research docs in [research/](research/), each with a "what they say" and an
"applicability to our app" section:

- [research/deepgram.md](research/deepgram.md) — VAQI whole-call scoring (interruptions/coverage/latency), the 9-scenario pre-production suite, real-audio-over-synthetic, KRR/KER over WER, and how to measure STT latency without conflating endpointing.
- [research/elevenlabs.md](research/elevenlabs.md) — per-call LLM-judge criteria with success/failure/**unknown** verdicts, typed data extraction, three test types (simulation / next-reply / tool-call), tool mocking, repeat-count pass-rate gating, production→test→CI loop.
- [research/ecosystem.md](research/ecosystem.md) — Vapi/Retell/LiveKit/Pipecat/Bland/Coval/Hamming/Cekura: the common metric taxonomy, the text→sim→audio→production method ladder, concrete targets (barge-in stop <200 ms, turn p95 <800 ms), pyannote for offline VAD ground truth.
- [research/academic-benchmarks.md](research/academic-benchmarks.md) — PARADISE, τ-Voice, VoiceBench, Full-Duplex-Bench barge-in metrics (TOR), slot accuracy, NISQA, FTO/ep50/ep90, and audio-judge validity caveats.
- [research/weave-evaluations.md](research/weave-evaluations.md) — weave.Evaluation vs EvaluationLogger, client-side scorers (audio-capable, unlike Monitors), trials, media support, leaderboards/compare UI; verified against SDK 0.53.12.
- [research/textbook-agent-evals.md](research/textbook-agent-evals.md) — the four questions (outcome/trajectory/reliability/safety), pass@k vs pass^k, rubric judging, judge-bias mitigations, benchmark skepticism.

**[proposal.md](proposal.md)** is the deliverable: a theory section (component-vs-end-to-end and
offline-vs-online axes; speech/interaction/task/experience metric layers), the concrete Weave mapping,
twelve prioritized proposals (each with dataset source, scorer design, and S/M/L effort), and a
three-phase seed plan that starts by migrating `scripts/safety_check.py` into a gated weave.Evaluation
and ends with a graded, perturbation-capable replay benchmark. Everything runs off the hot path — the
no-latency-regression rule is itself promoted to a gated eval metric.

Reading order: proposal.md first; dip into research docs for sources and detail. If you read only two
research docs, pick weave-evaluations.md (what the tooling can do) and deepgram.md (what to measure).
