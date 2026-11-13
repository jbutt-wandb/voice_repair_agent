# Ecosystem research: voice-agent evaluation (non-Deepgram/ElevenLabs)

Scope: pyannote.audio / pyannoteAI, voice-agent platforms (Vapi, Retell, LiveKit, Pipecat, Bland), and
dedicated eval startups (Coval, Hamming, Cekura). Researched 2026-11-13.

## What they say

### Acoustic/component metrics (pyannote)

- [pyannote.metrics](https://www.isca-archive.org/interspeech_2017/bredin17_interspeech.pdf) defines the
  standard acoustic eval vocabulary: **Diarization Error Rate** = (missed detection + false alarm +
  speaker confusion) / total speech time; **VAD detection error rate** = (false alarm + miss) / total,
  plus detection precision/recall and segmentation metrics. pyannote.audio pipelines are
  hyperparameter-tuned directly against these metrics.
- Reference DER numbers for pyannote/speaker-diarization-3.1: ~12-14% AMI, ~9-11% VoxConverse
  ([framework comparison](https://lajavaness.medium.com/comparing-state-of-the-art-speaker-diarization-frameworks-pyannote-vs-nemo-31a191c6300)).
- The company [pyannoteAI](https://www.pyannote.ai/solutions/ai-voice-agents) now sells "speaker
  intelligence" for voice agents: **turn-taking metrics, speaker consistency, interaction-quality
  measurement for production agent fleets**, overlapping-speech detection ("tag simultaneous speech and
  crosstalk so the agent acts on the right turn"), and end-of-thought detection ("avoid interruptions
  and dead-air delays"). Voice-agent-eval vendor Bluejay is a
  [customer](https://www.pyannote.ai/customer-story/bluejay) — diarization is becoming eval-infra.

### Platform eval features

- **Vapi** ([simulations overview](https://docs.vapi.ai/observability/simulations-overview),
  [voice testing](https://docs.vapi.ai/test/voice-testing)): a simulation = **scenario** (AI tester's
  intent + success criteria) x **personality** (model/transcriber/voice). Two modes: **chat** (fast,
  cheap, no audio) and **voice** (full pipeline, also checks **latency limits** per run). Grading via
  reusable structured-output schemas. Known gap: test suites are transcript-graded and can't measure
  WER, TTS naturalness, or latency percentiles.
- **Retell AI** ([simulation testing](https://docs.retellai.com/test/llm-simulation-testing),
  [test overview](https://docs.retellai.com/test/test-overview)): text-only LLM-simulated user, each run
  pass/fail with written explanation; saved cases become a **regression suite rerun after every prompt
  change**. Explicit caveats: text sims can't test background noise or interruption handling, and
  **unmocked tools fire real endpoints** (can create real bookings).
- **LiveKit Agents** ([testing docs](https://docs.livekit.io/agents/start/testing/),
  [test framework](https://docs.livekit.io/agents/start/testing/test-framework/),
  [evals API](https://docs.livekit.io/reference/python/livekit/agents/evals/index.html)): pytest/Vitest
  helpers on `AgentSession.run()` with assertions on messages, **tool calls + arguments**, handoffs.
  **Eight built-in LLM judges** (accuracy, coherence, conciseness, handoff, relevancy, safety, task
  completion, tool use) run concurrently via `JudgeGroup`; a grounding judge requires responses be
  supported by function-call outputs. Text-mode unit tests first, audio Agent Simulations for the full
  STT-LLM-TTS pipeline, plus `lk perf agent-load-test` for load.
- **Pipecat/Daily** ([evals overview](https://docs.pipecat.ai/pipecat/evals/overview)): ships "Pipecat
  Evals" CLI with **scripted scenarios** (script user turns, assert agent behavior) and **simulated
  scenarios** (LLM user with persona + goal), run against real agent code. Rationale: "voice agents are
  probabilistic systems"; text-mode tests skip audio and run in seconds; semantic (judge) assertions
  over exact strings; per-event **latency budgets** and interruption tests. Recommends third parties
  (Bluejay, Cekura, Coval, Roark, Arize) for production, with OpenTelemetry spans carrying
  voice-specific attributes.
- **Bland AI** ([docs index](https://docs.bland.ai/llms.txt),
  [Standards](https://docs.bland.ai/tutorials/standards)): layered stack — Testbed (node prompts),
  **Standards** (node regression: run each scenario **10x against success criteria, pass if above
  threshold** — explicitly treating nondeterminism statistically), Agent Testing (simulated
  conversations with persona + success criteria, batch API), Evals (grade real calls with **versioned
  LLM judges**).

### Dedicated eval startups

- **Coval** ([metrics docs](https://docs.coval.ai/concepts/metrics/overview),
  [products](https://www.coval.ai/products/)): five metric classes — **deterministic** (regex, field
  lookups, end-reason, agent-fails-to-respond), **statistical** (latency, interruption rate, speaking-time
  %, pause analysis, audio artifacts/clipping, pitch/syllable rate), **ML model** (audio sentiment,
  timbre drift, transcription-error detection), **LLM judge** (binary/categorical/numeric, composite),
  and **trace** (computed from your **OpenTelemetry spans**: STT/LLM/TTS latency, tokens, tool-call
  counts). Turn-taking trio: interruption rate, agent-fails-to-respond, repeated turns.
- **Hamming AI** ([metrics guide](https://hamming.ai/resources/voice-agent-evaluation-metrics-guide),
  [production eval](https://hamming.ai/blog/how-to-evaluate-ai-voice-agents-performance-in-production)):
  metric layers = system performance, conversation mechanics, task completion, sentiment. Concrete
  targets: WER <5%, task success rate >85%, first-call resolution >75%, hallucination rate <1%, TTFB
  <400ms (critical >800ms), turn-latency p95 <800ms vs industry median 1.4-1.7s, barge-in: **agent
  stops within 200ms of user speech**, MOS 4.3+, intent accuracy >95%. Voice adds dimensions text
  evals lack: latency, audio quality, turn-taking dynamics, acoustic robustness (noise, accents).
- **Cekura (ex-Vocera)** ([testing methods](https://www.cekura.ai/blogs/how-to-test-voice-ai-agents),
  [persona QA](https://www.cekura.ai/blogs/persona-based-voice-ai-qa)): calls your agent as a simulated
  caller; **50+ personalities** each owning voice/accent, language, speaking speed, **interruption
  timing, idle timing, background noise, volume, network conditions**. Scores via Boolean/numeric/
  rating/**custom Python** metrics in three eval types: binary workflow adherence (hard fail), binary
  qualitative, continuous qualitative (1-5). Auto-generates test scenarios from the agent's knowledge
  base; runs thousands of personas pre-launch and monitors production.

### Common taxonomy across vendors

Nearly every vendor converges on the same structure:

1. **Dimensions measured**: task completion / workflow adherence; instruction-following; **tool-call
   correctness (name + args)**; latency (TTFB/TTFW, turn p50/p95); **turn-taking** (interruption/barge-in
   stop time, agent-fails-to-respond, repeated turns, dead air); audio quality (WER, MOS, artifacts);
   hallucination/grounding; safety; sentiment/frustration; correct call termination.
2. **Method ladder**: cheap text-mode scripted regression (CI) → LLM-persona simulated user (text) →
   full-audio simulated calls with personas/noise/interruptions → load testing → production scoring +
   alerting. Everyone grades with LLM judges on structured rubrics but prefers **deterministic checks
   wherever possible**, and repeated runs + thresholds to handle nondeterminism (Bland's 10x Standards).
3. **Trace-first observability**: OTel-style spans with voice-specific attributes become the substrate
   for both production monitoring and offline metric computation (Coval trace metrics, Pipecat).

Not useful: the Vapi marketing "test" pages and Bland's starter guide add nothing beyond the docs above.

## Applicability to our app

- **We already match the ecosystem baseline**: `scripts/safety_check.py` = Retell/Pipecat-style scripted
  text regression; the replay harness = voice-mode simulation but with *real recorded* caller audio —
  stronger than synthetic personas for regression; Weave spans = the trace substrate Coval computes
  metrics from. The gap is scoring, not plumbing.
- **Score the replay harness**: today it replays but doesn't grade. Attach (a) latency assertions per
  turn (voice-to-voice p95 budget, Hamming's <800ms target as aspiration, our own baseline as the
  regression gate) and (b) an LLM-judge pass per replayed call. This makes replays a CI-able regression
  suite — all offline, zero hot-path cost.
- **Adopt Coval's five-class metric split** as our organizing scheme. We have deterministic (latency
  JSON), LLM judge (Weave Monitor), ML/audio (gpt-audio-mini acoustic judge). Missing: **statistical
  turn-taking metrics** (interruption rate, barge-in stop time, agent-fails-to-respond, repeated turns,
  dead-air gaps) and **trace-derived metrics** computed post-hoc from Weave spans.
- **Measure barge-in stop time** (ecosystem norm: agent audio should stop <200ms after user speech
  onset). We have VAD-assisted barge-in but no metric; compute offline from session timestamps/replay
  audio.
- **Add LiveKit's judge rubric set** (accuracy, coherence, conciseness, relevancy, safety, task
  completion, tool use, grounding-in-tool-outputs) as a Weave Evaluation scorer bundle over transcripts;
  run several repetitions per scenario with a pass threshold, per Bland's Standards pattern.
- **Deterministic tool-call assertions**: every vendor checks tool name + arguments. Extend
  safety_check scenarios to assert `book_engineer_visit` args and `end_call` firing/non-firing
  deterministically, not only via judge. Mock `book_engineer_visit` in all sims (Retell's real-booking
  warning applies to us verbatim).
- **Persona-based simulated callers** (Cekura/Vapi pattern): upgrade safety_check's scripted users to
  LLM personas with goals (confused, impatient, non-native, interrupts mid-sentence); in voice mode,
  overlay background noise / speed-perturb the replay audio for acoustic robustness.
- **pyannote.audio locally, not the API**: we're single-speaker-per-channel, so DER is irrelevant; but
  its VAD/overlap detection, run offline on replayed turn audio, gives ground truth for barge-in and
  end-of-turn timing metrics. pyannoteAI's paid platform is overkill here.
- **Everything above runs offline** on session_logs/ and Weave traces — consistent with the hard rule
  that nothing touches voice-to-voice latency.
