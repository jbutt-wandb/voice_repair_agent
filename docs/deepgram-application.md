# Deepgram application — "Why Deepgram?" answers

*Drafts to review and personalize before pasting into the form. The technical and
safety figures below are real — measured from the build of this project. The
business-impact figures (deflection %, cost per visit) are **illustrative** of the
opportunity this prototype demonstrates, not results I personally achieved — sanity-
check them or swap in your own before submitting.*

---

## What excites you about Deepgram?

Voice is the most natural interface humans have, and voice AI has just crossed a real
inflection point. For years the real-time cascade of speech-to-text → LLM →
text-to-speech was a demo that fell apart the moment a customer actually spoke to it —
too slow, too error-prone. That's no longer true. The latency and accuracy are finally
good enough to hold real conversations, and that changes which markets are addressable
almost overnight.

Deepgram sits exactly where the value concentrates: the speech layer everything else
depends on. Fast, accurate, affordable STT and TTS is the pick-and-shovel position in
the voice-AI boom — whoever owns it, wins regardless of which model or app layer comes
out on top. The business promise is enormous: 24/7 voice agents that handle genuine
support, sales, and service calls rewrite the unit economics of contact centres, field
service, and healthcare intake.

EMEA is the part that excites me most commercially — large, multi-language, and
under-penetrated, which is a GTM opportunity for someone who can build a region
deliberately. And I don't come to it cold: I've built on Deepgram myself (see below),
so I can sell this with real conviction rather than a pitch deck.

---

## What is the most impressive thing you've personally built or automated with AI? Describe exactly what you did, how it worked, and the measurable outcome.

I built a real-time, browser-based voice agent that acts as an expert appliance-repair
support line. You call it and talk; it listens, diagnoses by asking the right
counter-questions ("Is it plugged in? Any lights or error codes?"), walks you through
only the fixes its product manuals mark as safe, refuses anything dangerous, books a
(simulated) engineer when a specialist is genuinely needed, and hangs up naturally once
you're done.

**How it works.** An explicit streaming pipeline — Deepgram speech-to-text (nova-3) → an
LLM → Deepgram text-to-speech (aura-2) — orchestrated to feel like a real phone call: it
streams at every stage so you hear a reply almost immediately, and you can interrupt it
mid-sentence and it stops instantly (barge-in).

**Safeguards.** The agent's answers aren't open-ended. The LLM is grounded strictly in
the supplied product manuals, which define exactly which fixes a customer may attempt and
which are specialist-only — it can only recommend what the manuals permit and must refuse
everything else, enforced by an automated "LLM-as-judge" suite that red-teams it on every
change. Memory is managed too: long calls are summarised on the fly to keep latency and
cost flat, and the agent retains only what the caller actually heard — so its context
stays faithful to the real conversation.

**Measurable outcome (real):** ~1-second voice-to-voice latency, ~500 ms LLM
time-to-first-token, ~200 ms TTS time-to-first-audio. On safety, 12/12 scenarios and
20/20 end-to-end checks pass with **zero** unsafe recommendations — even when a simulated
user insisted on dangerous actions (discharging a microwave capacitor, regassing a
refrigerant line). I built the whole thing — pipeline, barge-in, tool-calling, memory
management, and the test harness — in about a day, without a framework.

**Why it matters commercially (illustrative):** in field service, a large share of
call-outs are resolved by basic steps. Safely deflecting even ~30–40% avoids engineer
visits that run ~£80–150 each, while adding 24/7 availability, instant scaling, and
auditable safety compliance.

Building it end-to-end gave me first-hand conviction in the exact product I'd be selling,
and a concrete grasp of the ROI a Deepgram buyer cares about.
