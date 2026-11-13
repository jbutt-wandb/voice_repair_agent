# ElevenLabs on evaluating conversational voice agents

Research notes, 2026-11-13. Sources: elevenlabs.io docs (Agents Platform / "ElevenAgents") and blog.

## What they say

### 1. Per-conversation LLM-judge "success evaluation"

- Every conversation transcript can be scored against up to **30 custom "evaluation criteria"** per agent. Each criterion is a *goal prompt*: a name plus a free-text description that is "passed to an LLM along with the conversation transcript to verify if a specific goal was met." ([Success evaluation docs](https://elevenlabs.io/docs/eleven-agents/customization/agent-analysis/success-evaluation))
- Each criterion returns a three-valued result — **`success` / `failure` / `unknown`** — plus a **rationale** explaining why. `unknown` is expected for incomplete/ambiguous calls rather than forcing a binary. ([same](https://elevenlabs.io/docs/eleven-agents/customization/agent-analysis/success-evaluation))
- Prompt-writing guidance: be specific about what constitutes success vs failure, and include edge cases and examples in the criterion prompt. ([same](https://elevenlabs.io/docs/eleven-agents/customization/agent-analysis/success-evaluation))
- Results flow to dashboards and **post-call webhooks** for external aggregation. ([Conversation analysis overview](https://elevenlabs.io/docs/eleven-agents/customization/agent-analysis))

Their own taxonomy of criteria (used on their docs/support agent): **Interaction** (was it a valid, relevant conversation — a hard gate: fail this and the call fails), **Positive interaction** (user satisfied vs frustrated/confused), **Understanding root cause**, **Problem resolution** (solved or offered alternatives), and **Hallucination** (fabricated info absent from the knowledge base). ([Testing blog](https://elevenlabs.io/blog/testing-conversational-ai-agents))

### 2. Data collection (structured extraction per call)

- Separate from pass/fail judging: LLM extraction of typed fields from each transcript — **string / boolean / integer / number** — defined by a description prompt ("The description field is passed to the LLM and should be as specific as possible about what to extract and how to format it"). Limit 25-50 items per agent depending on plan. ([Data collection docs](https://elevenlabs.io/docs/eleven-agents/customization/agent-analysis/data-collection))
- Example uses: contact details, issue category, satisfaction rating, and behavioral booleans like whether "the customer expressed anger or frustration during the call." ([same](https://elevenlabs.io/docs/eleven-agents/customization/agent-analysis/data-collection))

### 3. Tests: authored test suites with simulated users

Three test types ([Agent testing docs](https://elevenlabs.io/docs/eleven-agents/customization/agent-testing)):

- **Simulation tests** — "end-to-end, multi-turn conversations with a simulated user." You describe the simulated user's context, intent, and behavior in natural language; a success-condition prompt (up to 30 per test, merged) judges the resulting transcript. Supports **tool mocking** (mock none / all / selected, with fallback = call real tool or finish with error) and **dynamic variables** to inject test-specific values (names, order IDs).
- **Next-reply (LLM scenario) tests** — unit-level: given a fixed chat history, "validates the agent's next response against success criteria," with **success and failure examples** supplied "to help the evaluator understand the nuances."
- **Tool-call tests** — "ensures the agent calls the right tool with the right parameters"; parameter validation by **exact match, regex, or LLM** semantic check.

Authoring and running:

- Tests can be **created from real conversations**: open a call in history → "Create test from this conversation" → review prefilled context → define expected behavior. They also mention auto-generating tests from past customer conversations. ([Tests blog](https://elevenlabs.io/blog/tests-for-elevenlabs-agents))
- Run from dashboard (single / batch / "Run All Tests"), CLI (`elevenlabs agents test <agent_id>`), or API — designed to sit in **CI/CD so "every pull request can be validated against all your test scenarios."** ([Agent testing docs](https://elevenlabs.io/docs/eleven-agents/customization/agent-testing), [Tests blog](https://elevenlabs.io/blog/tests-for-elevenlabs-agents))
- **Probabilistic testing**: run the same test N times (`repeat_count` 2-20) and report a pass rate — green 100%, amber ≥80%, red <80% — with failures **clustered by failure reason**. ([Agent testing docs](https://elevenlabs.io/docs/eleven-agents/customization/agent-testing))
- Methodology note from their blog: use **full simulations** for end-to-end flows and **partial simulations** that "start mid-conversation to validate decision points or sub-flows" — their unit-test analogue for fast iteration. Production and tests share the *same* evaluation criteria, closing "a feedback loop that connects real usage with structured evaluation, targeted testing, and automated validation." ([Testing blog](https://elevenlabs.io/blog/testing-conversational-ai-agents))
- The older [simulate-conversation API](https://elevenlabs.io/docs/api-reference/agents/simulate-conversation) (deprecated Oct 2026 in favor of the test endpoints) shows the shape: `simulated_user_config` (first_message, language), `extra_evaluation_criteria`, `tool_mock_config`, `partial_conversation_history`, `new_turns_limit`; response = full simulated transcript (with tool_calls/results and `time_in_call_secs`) + `analysis` (`call_successful` success/failure/unknown, `transcript_summary`, per-criterion results).

### 4. Experiments (online A/B)

- Controlled A/B tests over any config dimension (prompt, voice, tools, workflow), traffic split deterministic by conversation ID, judged on "CSAT, containment rate, conversion, average handling time, median agent response latency, cost per resolution." Explicit prerequisite: "Configure success evaluation criteria before running experiments." Change one variable at a time; start at 5-10% traffic. ([Experiments docs](https://elevenlabs.io/docs/eleven-agents/operate/experiments))

### 5. TTS / acoustic quality evaluation

- No dedicated docs page on MOS or side-by-side TTS eval was found. The closest published guidance is a blog, [Voice agent evaluation framework: metrics that matter](https://elevenlabs.io/blog/voice-agent-evaluation-framework-6-pillars-explained), with six pillars: **TTS voice quality** (MOS 1-5 by human listeners, target 4.3-4.5; automated proxies UTMOS/NISQA at scale), **conversation quality** (intent accuracy 85-96%; "time-to-first audio of under 500ms, with sub-300ms being superior"; STT WER), **tool usage / task completion** (Task Success Rate target >85%, slot-fill accuracy, regression testing), **intelligence** (hallucination, out-of-scope handling, context retention), **compliance/safety** (guardrails, logging, post-hoc review), **reliability** (uptime, load at 2x peak). Includes human-vs-AI benchmark targets (e.g. FCR 70% human vs 75% AI target, CSAT 85%).

## Applicability to our app

- **Adopt the three-valued verdict.** Our safety_check and Weave Monitor judges should return success/failure/**unknown + rationale**, not a bare boolean — ElevenLabs treats "unknown" as the honest answer for truncated/ambiguous calls, which matches our idle-timeout hang-ups.
- **Split judging from extraction.** We already have the split implicitly (Monitor = judge, acoustic judge = typed booleans). Formalize it: a small set of goal-prompt criteria (per call) + typed data-collection fields (per call) written as Weave scorers — e.g. `issue_category: string`, `visit_booked: boolean`, `appliance_identified: boolean`, `caller_frustrated: boolean` (the last aligns with our existing vocal_frustrated stamp).
- **Steal their criteria taxonomy almost verbatim** for the repair domain: valid interaction (gate), positive interaction, root-cause identified (correct appliance fault diagnosis), problem resolved *or* engineer visit booked, no hallucinated repair advice outside the knowledge/prompt. Run them over whole-call transcripts off the hot path (post-call, like their webhook model) — zero latency impact.
- **Three test types map cleanly onto our assets:** simulation tests ≈ scripts/safety_check.py (16 scripted conversations); next-reply tests = a cheaper new layer we lack (fixed history → judge only the next assistant turn, with success *and failure* examples in the judge prompt); tool-call tests = assert `book_engineer_visit`/`end_call` fire with correct params via exact/regex/LLM param matching — currently untested directly.
- **Tool mocking with explicit fallback** (mock selected, else error) — safety_check should mock `book_engineer_visit` rather than let the judge see real side effects.
- **Create tests from real conversations**: our Weave-trace REPLAY harness is the audio analogue of their "Create test from this conversation" — add a step that snapshots a replayed call's transcript into a scripted test case, so production failures become permanent regression tests.
- **Probabilistic runs**: LLM agents are nondeterministic; run each safety_check scenario `repeat_count`≈3-5 and gate CI on pass *rate* (their green/amber/red at 100/80%) instead of a single sample; cluster failures by reason in the report.
- **Partial simulations**: start scripted tests mid-conversation (seeded history incl. a photo-memory note) to unit-test decision points — e.g. "after two failed fixes, does it offer a visit?" — much faster than full-call simulation.
- **Same criteria online and offline**: whatever criteria the CI gate uses should be the exact prompts the Weave Monitor runs on production calls — their core loop (production monitoring → generate tests → CI gate) is directly reproducible with Weave Monitors + safety_check.
- **TTS quality**: nothing worth copying beyond targets — if we ever score aura-2 output, use automated MOS proxies (UTMOS/NISQA) offline on recorded TTS audio from Weave traces; never in-session.
