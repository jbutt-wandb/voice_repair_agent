# Building this app with a coding agent: a collaboration case study

This project wasn't written by a human alone, or by an agent alone. It was built
through a conversation between an engineer and a coding agent. This doc is the
*process* story — not how the code works (the other docs cover that), but how the
**collaboration** worked, so you can learn to drive an agent on a project of your
own.

The honest summary up front: the agent did most of the typing, but the human made
every decision that mattered, corrected the agent when it guessed wrong, and kept
raising the bar. That division of labour is the whole lesson.

---

## The arc at a glance

```
  BRIEF ──▶ PLAN ──▶ DE-RISK ──▶ BUILD ──▶ VERIFY ──▶ DEEPEN ──▶ DEBUG
   │         │         │           │          │          │          │
  rich    clarify   scary       safety     real       analyse    diagnose
  prompt  decisions parts       before     browser +  before     before
          first     first       features   eval gate  coding     fixing
```

Each arrow was a human decision point. The agent proposed; the human disposed.

---

## 1. The opening brief, and *not* diving into code

The engineer opened with a rich brief (preserved verbatim in
[00-original-prompt.md](00-original-prompt.md)): a voice agent for appliance
repair, Deepgram for speech, safety as the top priority, a simulated engineer
booking, and — crucially — a requirement that the project *teach* how voice agents
work.

A weaker way to use an agent would be to say "ok, start coding." Instead the first
move was **planning, not typing**. The agent explored the empty repository,
confirmed the API keys were present, and then asked the human to settle the
architectural forks that would shape everything downstream:

- **Explicit `STT → LLM → TTS` pipeline, or Deepgram's all-in-one Agent API?**
  The explicit pipeline was chosen *because the goal was teaching* — every stage
  needed to be visible and measurable.
- **Which LLM?** (OpenAI, given the available key.)
- **Vanilla HTML/JS frontend, or a React build?** (Vanilla — one process, no Node
  toolchain, readable audio code.)
- **How many product manuals, and which?** The agent proposed three; the human
  added a fourth — an **ice maker** — specifically because its sealed refrigerant
  system is a great example of a "specialist-only, never DIY" repair.

### The correction that changed the architecture

The agent's initial plan proposed talking to Deepgram over **raw WebSockets**,
reasoning it was more transparent for teaching. The human pushed back with a
single sharp question — in spirit, *"Doesn't Deepgram have an official SDK? How
will we use their models?"*

That one question flipped a foundational decision: the project switched to the
**official `deepgram-sdk`**. The lesson here cuts both ways. The agent should
surface its reasoning (it did), but the human should interrogate assumptions that
feel off (they did), *early*, before any code is built on top of them. A wrong
foundational choice is cheap to change in a plan and expensive to change in code.

> **Takeaway:** On non-trivial work, ask for a plan before code. Treat the
> clarifying questions as the most valuable part of the exchange — that's where
> you steer the architecture while steering is still free.

---

## 2. Build order driven by de-risking

Once the plan was approved, the agent didn't build the app in "user-visible"
order (pretty frontend first). It built in **risk order** — surfacing the things
most likely to fail or to matter most, earliest:

1. **Dependencies + a Python 3.14 wheel check.** Before writing a line of app
   logic: would the libraries even install on this Python version? (They did —
   but finding out *first* is the point.)
2. **The product manuals + the safety system prompt.** The heart of the
   requirement.
3. **The LLM layer + a safety-evaluation harness** — and the rule that the harness
   had to pass *before any audio code existed at all*. If the agent gives unsafe
   advice in text, it will give it in speech; so prove the text is safe first.
4. **Deepgram STT and TTS wrappers**, each with a standalone smoke test.
5. **The orchestrator** (the turn-taking state machine).
6. **The frontend** — last, because by then everything it talked to was proven.

The sequencing is itself a technique you can ask for: *"build this so the riskiest
assumptions fail fast, and so safety is provable before features exist."*

> **Takeaway:** Direct the *order* of work, not just the work. "Scary parts first,
> safety before features" catches showstoppers while they're cheap.

---

## 3. Verification as a first-class habit

The agent was expected to **exercise its own work**, not just declare it done.
Two durable test artifacts came out of this and still live in the repo:

- `scripts/safety_check.py` — an **LLM-as-judge** harness that runs scripted
  conversations (safe fixes, forbidden requests, an insistent user who won't take
  no, out-of-scope products, a full booking) and grades the transcripts. It's a
  gate: red means stop.
- `scripts/e2e_check.py` — an end-to-end check that drives the real WebSocket
  server the way the browser would.

Beyond scripts, the agent **drove the actual app in a browser** to watch it work.

### Bugs became teaching material

Two real bugs surfaced *through verification* and were turned into permanent
lessons (see [02-how-deepgram-solves-the-problem.md](02-how-deepgram-solves-the-problem.md)):

- **Endpointing needs trailing *silence audio*, not an absence of audio.** An early
  STT test streamed a sentence then stopped sending frames and waited forever for
  the "you're done talking" signal. Deepgram's endpointer detects silence *in the
  audio stream* — no frames means no silence means no decision.
- **TTS audio can arrive *after* the "cleared" acknowledgement.** When the user
  barges in, the server tells Deepgram to discard buffered speech. But audio
  already mid-synthesis can still arrive after the ack. The fix: tag each audio
  chunk with the turn that requested it and drop stale tags, rather than trusting
  the ack's timing.

Neither bug would have been caught by "looks right to me." They were caught by
running the thing and watching what actually happened.

> **Takeaway:** Demand that the agent verify by *exercising* the code and show you
> the real output. "Tests pass" with the output attached beats "done."

---

## 4. Iterative deepening: the agent as a thinking partner

After the core app worked, the engineer kept raising the ceiling with follow-up
requests. The valuable pattern here is that **not every request became code
immediately** — some were deliberately answered with *analysis first*.

- **"Research Pipecat and see how it might help."** This was delegated to a
  research subagent that read current docs and reported back a structured
  comparison and a recommendation (keep the hand-rolled pipeline as the teaching
  artifact; consider a framework only for a production track). Delegating research
  keeps the main thread focused and the findings concise.
- **"Write beginner explainer docs."** Delegated to another subagent, which
  produced the `docs/` explainers you're reading.
- **A genuine design debate about context management.** The engineer asked a
  pointed, narrowing question — in spirit, *"Could Pipecat help us with **just**
  context management while we keep everything else?"* The right response was
  **reasoning, not code**: the agent explained why that framework's context
  handling isn't cleanly separable from its pipeline, and that a focused
  hand-rolled solution (rolling summarization) was the better fit. Only *after*
  that shared conclusion did implementation follow.

When implementation did come, it bundled several related improvements: **rolling
summarization** of long conversations, a **"remember only what the customer
actually heard"** rule on interruptions (an idea borrowed conceptually from the
Pipecat research), a **natural end-of-call** so the agent can hang up when you're
done, and a **"check the basics first"** prompt change so the agent asks "is it
plugged in? any lights? an error code?" before ever suggesting an engineer visit.

> **Takeaway:** Use the agent as a thinking partner, not a code vending machine.
> A sharp, narrowing question ("could we do *just* X?") often deserves an honest
> "here's why that's the wrong cut" before any code. Push back, and expect
> push-back.

---

## 5. Debugging together: diagnose before you fix

Late in the project: *"the app is not picking up microphone input, diagnose."*

Note the word **diagnose** — the engineer asked for a diagnosis, not a guess-fix.
What followed is a model for debugging with an agent:

1. **Form a hypothesis.** The agent suspected the microphone capture node wasn't
   wired into the audio graph correctly, which can stop it from running.
2. **Test the hypothesis in isolation — and *disprove* it.** Rather than "fix" the
   suspected wiring, the agent ran a controlled experiment (rendering the capture
   worklet offline) and found the node *was* producing audio frames correctly. The
   hypothesis was wrong. **It did not "fix" something that wasn't broken.**
3. **Find the real cause.** Instrumenting the live browser revealed two things: the
   microphone-permission request was failing *silently* (the code swallowed the
   error and quietly fell back to text-only mode, so the user got no explanation),
   and — a red herring that briefly muddied the water — the browser was serving a
   **stale cached copy** of the frontend, hiding the very fix being tested.
4. **Fix the real problems.** Surface microphone failures loudly and specifically
   (denied permission vs. no device vs. an insecure-origin page served over a LAN
   IP), and disable caching of the dev frontend so stale assets can't mislead.

The discipline on display: *a hypothesis is a question, not a conclusion.* The
agent's willingness to prove itself wrong — and to say so — is exactly what you
want.

> **Takeaway:** Ask the agent to **diagnose before fixing**. A good debugging
> session states a hypothesis, tests it, and is willing to discard it. Be wary of
> any agent that jumps straight to a patch.

---

## 6. The tools behind the collaboration

A few workflow mechanics made the above smoother, worth knowing if you drive an
agent yourself:

- **Plan mode.** The early architecture questions happened in a dedicated planning
  phase that produced a written plan for approval *before* any files were created.
- **Clarifying questions as multiple-choice.** Big decisions were offered as a few
  concrete options with trade-offs, not open-ended "what do you want?" prompts —
  faster for the human to steer.
- **A visible task list.** Multi-step work was tracked as todo items, so progress
  was legible at a glance.
- **Parallel subagents.** Research (Pipecat) and documentation were handed to
  separate subagents that worked in the background while the main thread continued
  — useful when a task is self-contained and shouldn't clog the main conversation.

---

## The transferable playbook

If you take nothing else, take these — each one actually happened above:

1. **Give a rich brief, then expect questions.** The opening prompt was detailed
   *and* the agent still had good clarifying questions. Both are features.
2. **Settle architecture before code.** The raw-WebSocket-vs-SDK fork was free to
   change in the plan; it would have been costly after implementation.
3. **Correct wrong assumptions early and specifically.** One pointed question
   ("doesn't it have an SDK?") redirected a foundational choice.
4. **Direct the order of work.** "Riskiest first, safety before features" is a
   request you can make, and it pays off.
5. **Make verification non-negotiable.** Insist the agent run the thing and show
   real output. Let safety/eval gates *block* progress.
6. **Use the agent to think, not just to type.** Some requests deserve analysis or
   push-back before code. A narrowing question often deserves an honest "that's the
   wrong cut, here's why."
7. **Debug by hypothesis.** Ask for a diagnosis; reward disproving a hypothesis;
   distrust straight-to-patch.
8. **Delegate self-contained work.** Research and docs went to subagents, keeping
   the main thread focused on decisions.

The throughline: **the human stayed in the driver's seat on every decision that
mattered, and the agent made that cheap to do** — by planning out loud, asking
before assuming, verifying its own work, and being willing to be wrong.

---

## Appendix: a sanitized transcript

Below is a condensed, lightly-edited record of the actual conversation that built
this project, so you can see the *rhythm* of it — not a verbatim log. Each **human**
turn is quoted (typos cleaned, long messages trimmed with `[…]`); each **agent**
turn is summarized in a line or two rather than reproduced in full. Secrets, paths,
and tooling mechanics are omitted.

### 1 — Human
> "I want to build a POC voice agent project with Deepgram. A frontend that captures
> the user's voice and responds with an AI voice; an expert electronics-repair agent
> that asks questions and gives safe advice; simulated product manuals defining safe
> vs. specialist-only actions; safety is paramount; if a specialist is needed, book a
> human engineer (simulated). Structure it to teach an engineer how Deepgram STT →
> LLM → TTS voice agents work and what to measure in production. […] Work with me to
> tighten things up."

**Agent:** Entered plan mode. Explored the empty repo, confirmed the API keys were
present, and asked four multiple-choice architecture questions: explicit pipeline vs.
Deepgram's all-in-one Agent API; which LLM; vanilla JS vs. React; and how many
manuals. (The human picked the explicit pipeline, OpenAI, vanilla JS, and *added* an
ice maker as a fourth product.)

### 2 — Human
> "Does Deepgram not have an API/SDK? How will we use their models?"

**Agent:** Good catch — surfaced the trade-off and flipped the plan from raw
WebSockets to the **official `deepgram-sdk`**. Updated the plan and presented it.

### 3 — Human
> *(approved the plan)*

**Agent:** Built the whole app in risk order: dependency/Python-3.14 check → manuals
+ safety prompt → LLM + a safety-eval harness that had to pass *before* any audio →
Deepgram STT/TTS wrappers with smoke tests → the orchestrator → the frontend. Ran
the safety harness (10/10) and an end-to-end check (17/17), drove the app in a
browser, and reported results — including two real bugs found and fixed along the way.

### 4 — Human
> "Tear down the app for now."

**Agent:** Stopped the server, confirmed nothing was left listening, left the
unrelated project on the other port untouched.

### 5 — Human
> "How are we dealing with the growing memory as the conversation gets longer? Do we
> do any context management?"

**Agent:** Answered honestly: no — history grew unbounded (a deliberate POC
simplification). Explained what grew, the cost, and the options (sliding window,
rolling summarization, structured state), and asked whether to implement — rather
than silently building it.

### 6 — Human
> "Launch a research subagent to look into Pipecat and how it might help. Launch
> another subagent to make explainer docs on how this app works […] put them in docs."

**Agent:** Dispatched a research subagent (Pipecat) and a docs subagent in parallel,
then relayed the Pipecat findings and reviewed the docs when each returned.

### 7 — Human
> "Could Pipecat help us with context management but keep everything else the same?
> Don't write code, just tell me."

**Agent:** Reasoned it through *without coding*: Pipecat's context management isn't
cleanly separable from its pipeline, the useful idea is cheap to build ourselves, and
recommended a focused hand-rolled summarizer — plus flagged one genuinely clever
Pipecat detail worth borrowing (remember only what the user actually heard).

### 8 — Human
> "Implement the methods you described plus the clever detail. Add a natural off
> switch so it's not always listening — the agent asks if you need anything, and ends
> the call if you're done; a button starts a fresh session. Have it ask counter-
> questions and check basics (plugged in? blinking lights?) before jumping to booking
> an engineer. Use subagents where appropriate. Update docs."

**Agent:** Implemented rolling summarization, spoken-text-only interruption memory,
an `end_call` tool with graceful teardown, and basics-first prompting. Grew the test
gates to 12/12 safety and 20/20 end-to-end, verified the hang-up in a browser, then
delegated the docs update to a subagent. (A live-only bug surfaced: the model
sometimes *wrote* the tool call as text — fixed with a tool-first prompt.)

### 9 — Human
> "The app is not picking up microphone input, diagnose."

**Agent:** Diagnosed before fixing. Formed a hypothesis (capture-worklet wiring),
**disproved it** with a controlled offline-audio test, then found the real issues:
microphone failures were swallowed silently, and a stale browser cache was masking
the fix. Fixed both — loud, specific mic errors and no-cache headers for the dev
frontend — and verified the happy path still streamed audio frames.

### 10 — Human
> "Finish any code changes and restart the app. I'll test manually."

**Agent:** Finished the edits and restarted the server on the latest code.

### 11 — Human
> "What is my initial prompt to you? Put it in a markdown file in docs."

**Agent:** Saved the opening brief verbatim to `docs/00-original-prompt.md`.

### 12 — Human
> "Using a subagent, record a sanitized version of our conversation to build this app
> […] so a reader can learn how to work with an agent. Save in docs."

**Agent:** Delegated to a subagent that wrote this very document — the narrative
above.

### 13 — Human
> "Using a subagent, add a sanitized transcript to document 4. Add an env example.
> Improve the diagrams — they look too cluttered."

**Agent:** Added `.env.example`, redrew the cluttered architecture and state-machine
diagrams as clean aligned boxes, and appended this transcript — which is why you're
reading a conversation that includes the request to write it down. A fitting place to
stop.
