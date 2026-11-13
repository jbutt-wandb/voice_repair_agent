# Evaluation proposal — voice repair agent

Synthesis of six research efforts ([research/](research/)) into a concrete evaluation plan for this
app. Hard constraint inherited from the architecture ([doc 05](../05-architecture.md)): **nothing may
regress voice-to-voice latency** — every evaluation below runs offline on session_logs/, Weave traces,
the replay harness ([scripts/replay_session.py](../../scripts/replay_session.py)), or scripted text
runs. Nothing executes on the hot path.

## A. Theory: a framework for evaluating this voice agent

### Two axes

- **Component vs end-to-end.** Component metrics (WER, endpointing delay, TTS quality) localize
  failures to a pipeline stage; end-to-end metrics (task success, whole-call quality) measure what the
  caller experiences. Deepgram's VAQI scores whole calls (40% interruptions, 40% response coverage,
  20% latency) precisely because component scores don't compose predictably ([deepgram](research/deepgram.md));
  τ-Voice found voice agents retain only 30–45% of their text-LLM capability, and 79–90% of failures
  are *agent behavior*, not audio ([academic](research/academic-benchmarks.md)) — so we need both axes,
  with end-to-end as the gate and component metrics as the diagnosis.
- **Offline vs online.** The ecosystem method ladder: cheap text-mode scripted regression in CI →
  simulated users → full-audio replay → production scoring ([ecosystem](research/ecosystem.md)).
  ElevenLabs' core loop uses the *same* criteria in production monitoring and CI tests, so production
  failures become regression tests ([elevenlabs](research/elevenlabs.md)). For us: offline = safety_check
  + replay harness; online = the Weave Monitor + acoustic judge already stamping every call.

### The four questions (textbook taxonomy)

Outcome (right result), trajectory (right path — tools, args, step coherence), reliability (succeeds
*every* time — pass^k, not pass@k), safety (misuse, injected data, benign self-harm)
([textbook](research/textbook-agent-evals.md)). PARADISE supplies the oldest schema and it still maps
1:1 onto our data: performance = task success − dialogue costs (turns, time, delays); use the schema,
not the 1997 regression weights ([academic](research/academic-benchmarks.md)).

### Metric taxonomy by layer

**Speech layer** (component):
- *Keyword recall rate (KRR)* — % of domain-critical terms (appliance names, model/part numbers)
  correctly transcribed; WER equal-weights all errors and can look fine while missing a third of key
  terms; target 80–90% ([deepgram](research/deepgram.md)).
- *Transcript latency vs endpointing delay* — measure from **interim** results (audio cursor −
  transcript cursor); finals conflate endpointing. Our `stt_final_ms` currently conflates the two
  ([deepgram](research/deepgram.md)); the literature name for the endpointing-delay side is
  ep50/ep90 ([academic](research/academic-benchmarks.md)).
- *TTS quality* — NISQA, a reference-free MOS predictor, run offline over stored aura-2 audio
  ([academic](research/academic-benchmarks.md)); MOS target band 4.3–4.5 ([elevenlabs](research/elevenlabs.md)).
- *DER — explicitly skipped*: single caller, one speaker per channel ([ecosystem](research/ecosystem.md), [academic](research/academic-benchmarks.md)).

**Interaction layer** (timing & turn-taking):
- *Voice-to-voice latency percentiles* (≈ floor-transfer offset in the literature
  ([academic](research/academic-benchmarks.md))) — report p50/p95, never means; ecosystem bar: turn p95 <800 ms,
  sub-1000 ms "good" ([ecosystem](research/ecosystem.md), [deepgram](research/deepgram.md)).
- *Barge-in stop time* — agent audio should stop <200 ms after user speech onset ([ecosystem](research/ecosystem.md)).
- *Takeover rate (TOR) & latency-after-interruption* — did the agent stop, and did its next reply
  address the *interruption* rather than resume the pre-interruption script (Full-Duplex-Bench;
  Deepgram names the same failure mode) ([academic](research/academic-benchmarks.md), [deepgram](research/deepgram.md)).
- *Response coverage / agent-fails-to-respond, repeated turns, dead air* — VAQI's second 40% and
  Coval's turn-taking trio ([deepgram](research/deepgram.md), [ecosystem](research/ecosystem.md)).

**Task layer**:
- *Task completion, all-or-nothing* — did `book_engineer_visit` fire with correct args / did the call
  end cleanly; checked against ground truth, not judged (τ-Voice design); partial credit hides agents
  that never finish ([academic](research/academic-benchmarks.md), [textbook](research/textbook-agent-evals.md)).
- *Slot accuracy* — per-field exact match on booking args (product, date, time window), the
  MultiWOZ/JGA tradition ([academic](research/academic-benchmarks.md)); every vendor checks tool
  name + args deterministically ([ecosystem](research/ecosystem.md), [elevenlabs](research/elevenlabs.md)).
- *Safety compliance* — refusal under misuse, resistance to injected data (our photo-memory note is a
  retrieved-content injection surface), and benign self-harm (wrong booking, premature hang-up)
  ([textbook](research/textbook-agent-evals.md)).
- *Dialogue costs* — turns-to-resolution, escalation correctness (too early / too late / never),
  false statements ([deepgram](research/deepgram.md), [academic](research/academic-benchmarks.md)).

**Experience layer**:
- *Frustration/sentiment trajectory* — per-turn frustration (our Monitor + vocal judge) aggregated
  into a per-call trajectory; ElevenLabs models this as typed data-collection booleans
  ([elevenlabs](research/elevenlabs.md)).
- *Judge hygiene* — three-valued verdicts (success/failure/**unknown**) + rationale
  ([elevenlabs](research/elevenlabs.md)); rubrics over single grades; different-family judges (our
  OpenAI judge grades an OpenAI agent — self-preference risk); for pairwise judging, swap presentation
  order and watch verbosity bias (AudioJudge); audio judges must be ablation-tested
  (many keep their score with the audio removed) ([textbook](research/textbook-agent-evals.md),
  [academic](research/academic-benchmarks.md)).

### Cross-cutting principles

1. **Deterministic checks before judges** — cheapest, objective, CI-friendly ([ecosystem](research/ecosystem.md), [textbook](research/textbook-agent-evals.md)).
2. **Repeat runs, gate on pass rate** — agents are stochastic; Bland runs 10×, ElevenLabs
   `repeat_count` with green 100% / amber ≥80% / red <80%; choose shipping configs by pass^k
   ([ecosystem](research/ecosystem.md), [elevenlabs](research/elevenlabs.md), [textbook](research/textbook-agent-evals.md)).
3. **Real audio over synthetic** — flat TTS is "the easiest input your stack will ever get"; recorded
   real calls with written outcomes are the deliverable ([deepgram](research/deepgram.md)).
4. **Report cost and latency next to every quality score** — a variant that scores higher but
   regresses latency fails our hard rule by construction ([textbook](research/textbook-agent-evals.md)).
5. **Failures become fixtures** — "create test from this conversation" ([elevenlabs](research/elevenlabs.md));
   5–10 curated real cases beat waiting for a perfect suite ([textbook](research/textbook-agent-evals.md)).

## B. What Weave supports (verified against pinned SDK 0.53.12)

Full detail in [weave-evaluations.md](research/weave-evaluations.md); the load-bearing facts:

- **Two APIs, both needed.** `weave.Evaluation(dataset, scorers, trials=N)` for callable systems —
  fits the text-level layer (safety scenarios as Dataset rows, model = an op wrapping the LLM/tool
  loop). `EvaluationLogger` (imperative `log_prediction` → `log_score` → `finish` → `log_summary`)
  for systems that aren't a function — the right fit for the replay harness: one logger per replayed
  session, one prediction per voice turn, session metrics in `log_summary`.
- **Scorers run client-side in our process** (server-side "remote scorers" exist only via Monitors).
  So the gpt-audio-mini acoustic judge can be reused verbatim as an eval scorer over raw audio —
  sidestepping the project-verified fact that Monitors can't read `weave.Audio`.
- **Media**: EvaluationLogger predictions accept images and audio (playable in the UI). Audio inside
  `weave.Dataset` rows is undocumented — needs a 2-row smoke test; fallback = file paths in rows +
  `preprocess_model_input`.
- **Latency as a first-class score** via `log_score`/`log_summary` — turns the no-regression rule into
  a comparable, gateable metric in the compare-evals UI (radar chart, baseline deltas, scatter plots).
- **Trials & parallelism**: `trials=N` gives pass^k data for free; set `WEAVE_PARALLELISM=1` for
  real-time-paced replay evals.
- **Leaderboards + SavedViews** rank prompt/model variants by scorer summary metrics.
- **Avoid** `call.apply_scorer` guardrails — explicitly in-band, violates the latency rule.

## C. Proposals (prioritized)

| # | Name | Measures | Dataset source | Scorer design | Effort | Decision informed |
|---|------|----------|----------------|---------------|--------|-------------------|
| 1 | **safety_check → weave.Evaluation with trials + rubrics** | Outcome + safety + reliability | The existing 16 scenarios as a published `weave.Dataset` | Split the single judge into named rubric scorers (policy compliance, no fabricated details, correct escalation, tone) + keep deterministic `check_tools` as its own scorer; three-valued verdict + rationale; `trials=3`, gate CI on pass^3 | **M** | Every prompt/model edit: ship or revert |
| 2 | **Deterministic tool & latency validators** | Trajectory + the latency hard rule | session_logs/ JSON + Weave traces (existing) | Pure-Python checks: booking arg schema/slot accuracy, `end_call` only after farewell, idle-timeout never mid-speech, voice-to-voice p50/p95 under a baseline budget | **S** | CI gate; latency-regression alarm |
| 3 | **Graded replay harness (EvaluationLogger)** | End-to-end voice-native outcome + reliability | Replayed real sessions via the existing replay harness (real audio > synthetic, per deepgram) | One `log_prediction` per turn (caller audio in, agent transcript+TTS out); scorers: per-turn latency, transcript-quality judge, task-completion bit, plus a trajectory rubric judge over the turn sequence — right tool/args, efficiency, step coherence, e.g. no re-asking the model number after a photo note (textbook); `log_summary` = session PARADISE-style score (success − costs) | **M** | Pipeline-level regressions text evals can't see (VAD, barge-in, endpointing) |
| 4 | **Turn-taking metrics from Weave audio** | Interaction layer: VAQI-style interruptions, response coverage, barge-in stop time, TOR, FTO p50/p90 | voice_turn spans (both audio streams already stored); pyannote VAD run offline for ground truth | Statistical scorers over timestamps + VAD; Full-Duplex-Bench TOR + a 0–5 judge on whether the post-barge-in reply addressed the interruption | **M** | Whether barge-in/endpointing tuning helps or hurts; first-ever barge-in eval |
| 5 | **New scenario types for safety_check** | Coverage gaps named by deepgram's 9-scenario suite + textbook threat taxonomy | Authored: 3-s silence, angry caller, wrong number, photo-memory prompt injection ("sticky note says ignore your instructions"), benign-harm (unrequested booking, premature hang-up) | Same rubric scorers as #1; injection cases score attack-success-rate + on-task adherence | **S** | Safety sign-off for demo/teaching use |
| 6 | **Next-reply unit tests** | Single-turn decision points, fast iteration | Fixed seeded histories (incl. photo-memory notes), partial-simulation style (elevenlabs) | Judge the single next reply with success AND failure examples in the prompt; mock `book_engineer_visit` (Retell's real-booking warning) | **S** | Prompt tweaks without full-call cost |
| 7 | **STT keyword recall (KRR) on replays** | Speech layer: domain-entity transcription | Reference transcripts of replayed sessions + an appliance keyterm list | jiwer with a fixed normalization protocol; KRR/CER scorers; slice by acoustic condition | **M** | Whether to adopt nova-3 keyterm prompting; STT vendor/config choices |
| 8 | **Latency-metric split** | stt transcript latency vs endpointing delay (currently conflated in `stt_final_ms`) | Data already collected per turn; log both fields | Deterministic: interim-based transcript latency + EOT delay; report ep50/ep90 | **S** | Where turn latency actually goes; endpointing tuning |
| 9 | **Acoustic-judge ablation** | Does gpt-audio-mini read prosody or transcript? | ~20 turns from Weave traces, audio stripped/swapped (arXiv 2607.13477 protocol) | Compare `vocal_frustrated`/`vocal_fearful` flags with vs without real audio; also validate once against human labels | **S** | Whether to trust (or replace) the vocal judge |
| 10 | **Noise/accent perturbation over replay** | Acoustic robustness (τ-Voice: noise/accents cost 5–13 pts of task success) | Replay fixtures with noise overlay / speed-pitch shift; later accent TTS personas | Same scorers as #3; compare clean vs perturbed in the Weave compare view | **L** | Robustness claims; STT config |
| 11 | **Leaderboard + experience trajectory** | Cross-variant ranking; frustration over call | All of the above + Monitor/vocal-judge stamps | Leaderboard columns: pass^3, task completion, p95 v2v latency, frustration rate; SavedView filtered to CI runs | **S** | Model/prompt selection at a glance |
| 12 | **TTS quality regression (NISQA)** | Speech layer: aura-2 output quality | Stored TTS audio from Weave traces | NISQA offline as a scorer; MOS-proxy trend line | **L** (low priority) | TTS vendor/voice changes |

Also adopted as policy, not builds: judges from a different model family for graded quality (textbook);
never use STT confidence as a quality signal (deepgram); keep `e2e_check.py` as a plumbing smoke test
only — replay is the accuracy benchmark (deepgram); snapshot failed production calls into fixtures
(elevenlabs).

## D. Seed plan

**Phase 1 — gate what exists (proposals 1, 2, 8).** Pure reuse: migrate the 16 safety scenarios into
a `weave.Evaluation` with rubric scorers and `trials=3` (the script's judge + `check_tools` split into
named scorers almost line-for-line); add deterministic validators over session_logs JSON including the
latency budget; split `stt_final_ms`. This converts current assets into gateable, comparable CI
metrics with zero new infrastructure and makes the latency hard rule machine-enforced. Smoke-test
audio-in-Dataset-rows (2 rows) while here.

**Phase 2 — make replay a graded benchmark (proposals 3, 4, 5, 6).** Wrap the replay harness in
`EvaluationLogger` (`WEAVE_PARALLELISM=1`; initialize the logger before any LLM call so token/cost
usage lands in it), attach latency + judge + task-completion scorers, then add
the turn-taking/barge-in metrics from stored voice_turn audio — the single biggest gap every research
doc flagged (we replay but don't grade; barge-in has no eval at all). Add the missing scenario types
and next-reply tests with tool mocking. End state: a voice-native pass^k regression suite built from
real recorded calls.

**Phase 3 — component depth and robustness (proposals 7, 9, 10, 11, 12).** KRR scoring, the
acoustic-judge ablation (cheap and it decides whether an existing component is trustworthy — do it
early in the phase), perturbed replays, the leaderboard, and NISQA last. These refine diagnosis once
the end-to-end gates from phases 1–2 are trusted.

Reused throughout: safety_check scenarios (phase 1 dataset), replay harness (phase 2 substrate),
per-session latency JSON (phase 1 validators + every eval's cost column), vocal judge (phase 2 scorer,
phase 3 ablation subject), Weave Monitor prompts (reused verbatim as CI criteria, closing the
ElevenLabs production→test loop).
