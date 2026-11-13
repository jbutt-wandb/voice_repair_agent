# Academic & benchmark literature on evaluating spoken dialogue systems / voice agents

Researched 2026-11-13. Primary sources preferred; each metric defined in one line.

## What they say

### 1. PARADISE: the classic frame (1997, still the right mental model)
[PARADISE (Walker et al., ACL 1997)](https://arxiv.org/abs/cmp-lg/9704004) defines dialogue-system performance as a weighted linear combination of **task success** minus **dialogue costs**, with weights fit by regressing user satisfaction surveys against the measures ([ACL PDF](https://aclanthology.org/P97-1035.pdf)).
- *Task success*: kappa agreement between the attribute-value matrix the user wanted and what the system achieved (chance-corrected).
- *Dialogue costs*: **efficiency costs** (number of turns, elapsed time) and **quality costs** (ASR rejections, system response delay, repair turns).
A later critique ([Hajdinjak & Mihelič, CL 2006](https://aclanthology.org/J06-2004.pdf)) found the regression weights generalize poorly across systems/user populations — treat PARADISE as a schema (success + cost + satisfaction), not a portable formula. Broad context: [Survey on Evaluation Methods for Dialogue Systems (arXiv 2019)](https://arxiv.org/abs/1905.04071).

### 2. Modern benchmarks for voice/audio LLMs
- [VoiceBench (arXiv 2410.17196, TACL'26)](https://arxiv.org/abs/2410.17196): spoken-instruction benchmark for LLM-based voice assistants covering knowledge, instruction-following, and **safety**, with deliberate perturbations — speaker styles (age, accent), environmental noise/far-field, and disfluent content ([code](https://github.com/MatthewCYM/VoiceBench)). Relevant insight: robustness to *speech realism* is tested separately from capability.
- [AudioBench (arXiv 2406.16020, NAACL'25)](https://arxiv.org/abs/2406.16020): 8 tasks / 26 datasets across speech understanding, audio-scene understanding, and **paralinguistics** (emotion, accent, gender); no model wins everywhere.
- [Big Bench Audio (Artificial Analysis / HF)](https://github.com/huggingface/blog/blob/main/big-bench-audio-release.md): 1,000 Big-Bench-Hard reasoning questions rendered to audio; scores reasoning accuracy plus first-packet latency; cascaded STT→LLM→TTS pipelines historically beat native speech-to-speech on reasoning ([methodology](https://artificialanalysis.ai/methodology/speech-to-speech-benchmarking)).
- [τ-Voice (arXiv 2603.13686, ICML'26)](https://arxiv.org/abs/2603.13686): extends τ²-bench to full-duplex voice agents — 278 verifiable customer-service tasks (retail/airline/telecom) checked against a real database, with a controllable voice user simulator (accents, noise). Headline: text GPT-5 solves 85%; best voice agents 31–51% clean, 26–38% noisy — voice agents retain only 30–45% of text capability, and 79–90% of failures are agent behavior, not audio ([Sierra blog](https://sierra.ai/blog/tau-voice-benchmarking-real-time-voice-agents-on-real-world-tasks)).
- [VoiceAgentBench (arXiv 2510.07978)](https://arxiv.org/abs/2510.07978): agentic/tool-calling eval for voice — reports tool-selection accuracy and structural correctness of invocations, but uses static TTS queries and skips conversational quality.
- Also-rans worth knowing exist: [VocalBench](https://arxiv.org/pdf/2505.15727) (vocal conversational ability), [MTalk-Bench](https://arxiv.org/pdf/2508.18240) (multi-turn S2S, arena + rubric protocols), [EVA-Bench](https://arxiv.org/html/2605.13841) (end-to-end voice-agent framework).

### 3. Component metrics (definitions + failure modes)
- **WER** — word-level edit distance (S+D+I)/N vs a reference transcript. Failure modes: all errors weighted equally regardless of meaning ("no" vs "snow"), surface-level only, punishes harmless normalization variants, blind to named-entity/intent-critical errors ([Semantic-WER, arXiv 2106.02016](https://arxiv.org/abs/2106.02016); [critical-error paradigm, arXiv 2605.03671](https://arxiv.org/abs/2605.03671)).
- **Semantic alternatives** — **SemDist** (embedding distance between reference and hypothesis, [arXiv 2104.02138](https://arxiv.org/abs/2104.02138)); sentence-embedding/BERTScore metrics agree with human judgment better than WER ([arXiv 2609.21663](https://arxiv.org/pdf/2609.21663)); Semantic-WER reweights errors by semantic importance.
- **MOS** — mean opinion score, 1–5 human listening rating of speech quality (ITU-T P.800/P.808 protocols). **NISQA** ([arXiv 2104.09494](https://arxiv.org/abs/2104.09494)) is a CNN–self-attention model predicting MOS plus four dimensions (noisiness, coloration, discontinuity, loudness) without a reference signal — the standard automated TTS/channel-quality proxy.
- **DER** — diarization error rate = (missed speech + false-alarm speech + speaker confusion) / total reference speech time, NIST RT lineage, usually with a 0.25 s collar ([review, arXiv 2101.09624](https://arxiv.org/pdf/2101.09624)).
- **Endpointing latency** — **ep50/ep90**: median/90th-percentile delay between true turn end and detected endpoint ([streaming endpointer, arXiv 2506.07081](https://arxiv.org/html/2506.07081v1)).
- **Floor-transfer offset (FTO) / response offset** — gap (positive) or overlap (negative) between user turn end and system speech start; humans average ~200 ms and often go negative ([TurnBench, arXiv 2608.25218](https://arxiv.org/pdf/2608.25218)); a 2025 corpus study measured Alexa at ~1,366 ms average FTO ([ACM](https://dl.acm.org/doi/10.1145/3772318.3790947)).

### 4. Task-oriented dialogue metrics
From the MultiWOZ tradition ([MultiWOZ 2.4, arXiv 2104.00773](https://arxiv.org/abs/2104.00773); [SimpleTOD, arXiv 2005.00796](https://arxiv.org/abs/2005.00796)):
- **Inform rate** — fraction of dialogues where the system provided a correct/appropriate entity.
- **Success rate** — fraction where it additionally answered all user-requested attributes (task success is binary per dialogue, checkable against a database — exactly τ-Voice's design).
- **Joint goal accuracy (JGA)** — fraction of turns where *every* slot value in the predicted dialogue state exactly matches ground truth; SOTA on MultiWOZ 2.1 is only ~60%, i.e. slot-filling is still hard.
- **Slot accuracy / slot F1** — per-slot correctness, a softer per-field counterpart to JGA.

### 5. LLM-as-judge on audio: it works, with caveats
- [AudioJudge (arXiv 2507.12705)](https://arxiv.org/abs/2507.12705): large audio models can judge speech directly; audio concatenation + in-context audio examples helps most; a multi-aspect ensemble (lexical / quality / paralinguistic judges) hits 0.91 Spearman with human system rankings; but **verbosity bias** (prefer longer answers) and **positional bias** (prefer first/second clip) are significant and need mitigation (position swapping, reference-guided scoring).
- [Protocol-shortcut audit (arXiv 2607.13477)](https://arxiv.org/abs/2607.13477): many audio-LLM judges keep much of their score **with the audio removed**, and will copy a wrong emotion label from the text prompt even when audio contradicts it — i.e. "acoustic" judgments can silently become text judgments. Validate that your acoustic judge actually uses the audio.
- Audio LLMs can also produce descriptive quality assessments approaching MOS-predictor utility ([arXiv 2501.17202](https://arxiv.org/pdf/2501.17202)).

### 6. Interruption / barge-in metrics
[Full-Duplex-Bench (arXiv 2503.04721)](https://arxiv.org/abs/2503.04721) is the primary source; it defines per-scenario metrics:
- **Takeover rate (TOR)** — fraction of events where the system starts (or wrongly starts) speaking; should be ~0 during user pauses/backchannels and ~1 after a genuine user interruption.
- **Latency after interruption** — time from user barge-in to the system's (stopped-then-)new response.
- **Response latency** — user turn end → system speech start (smooth turn-taking).
- **Backchannel frequency + JSD** — rate and timing-distribution match of backchannels vs human ground truth.
- **GPT-4o judge score (0–5)** — coherence/relevance/adaptability of the post-interruption response.
Follow-ups extend this to multilingual/multi-turn ([M3-DuplexBench, arXiv 2607.29125](https://arxiv.org/abs/2607.29125)) and overlap strategy choice — continue vs adapt vs yield ([arXiv 2609.13117](https://arxiv.org/pdf/2609.13117)). Artificial Analysis now folds Full-Duplex-Bench and τ-Voice into a composite [Speech-to-Speech Index](https://artificialanalysis.ai/speech-to-speech).

## Applicability to our app

- **PARADISE maps 1:1 onto our data**: task success = did `book_engineer_visit`/`end_call` fire with correct args (binary, checkable like τ-Voice's DB check); costs = turns-to-resolution + our existing latency JSON. Compute a per-session score offline from Weave traces — zero hot-path cost.
- **Adopt inform/success/JGA-style slot checks** for the booking tool: appliance type, fault, address/time slot correctness per replayed session — exact-match slot accuracy is cheap and objective, unlike judge scores.
- **Our replay harness is the right substrate for a τ-Voice-style eval**: re-feed recorded calls, then add VoiceBench-style perturbations (noise overlay, speed/pitch shift, accent TTS personas) to the replayed audio to measure robustness — τ-Voice shows noisy/accented audio costs 5–13 points of task success even for frontier agents.
- **Measure FTO and ep50/ep90 from data we already log**: voice-to-voice ≈ floor-transfer offset; report p50/p90 per session and alarm when p90 drifts — matches the "nothing regresses latency" rule with a literature-standard name.
- **Add Full-Duplex-Bench barge-in metrics to replay**: TOR after a scripted mid-TTS interruption (did we stop? did we respond?), latency-after-interruption, and an LLM-judge 0–5 on whether the post-barge-in reply addressed the interruption. Our VAD barge-in currently has no eval at all.
- **Audit the acoustic judge (gpt-audio-mini)**: run the arXiv 2607.13477 ablation — strip/replace audio and check whether `vocal_frustrated`/`vocal_fearful` flags change; if they don't, the judge is reading transcript context, not prosody.
- **Judge-bias hygiene for safety_check.py and the Weave Monitor**: add reference-guided rubrics and (for any pairwise comparisons) position swapping; watch verbosity bias — longer agent answers will score better for no good reason (AudioJudge).
- **WER is the wrong gate for our STT**: for a repair domain, score entity/slot-critical errors (model numbers, appliance names, addresses) or SemDist against reference transcripts from replays, not raw WER.
- **TTS quality**: run NISQA (open-source, offline) over stored aura-2 output from Weave traces as a cheap regression check — no humans, no hot path.
- **Don't bother with DER**: single-caller telephone-style sessions have one speaker per channel; DER only becomes relevant if speakerphone/multi-party support appears.
