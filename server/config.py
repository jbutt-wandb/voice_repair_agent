"""All tuning knobs in one place. These are the dials you would sweep when
tuning a production voice agent — the README explains the tradeoff behind each.

Three tiers of configuration, deliberately distinct:
  - SECRETS: live only in .env, never defaulted here (DEEPGRAM_API_KEY,
    OPENAI_API_KEY, optional WANDB_API_KEY). Copy .env.example to get started;
    the server refuses to boot without the required ones (check_required_keys).
  - DEPLOY TUNABLES: env-overridable via _env_bool/_env_float below — things an
    operator flips per environment without touching code.
  - PRODUCT DECISIONS: plain constants (e.g. STT_ENDPOINTING_MS) — changing them
    changes the product, so they are edited in this file, on purpose."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env as early as possible, from the project root, so EVERY entry point sees the
# keys — not just `uv run main.py`. The live server is launched as `uvicorn server.app:app`,
# which never runs main.py; without this, WANDB_API_KEY (and any env override below) would
# be missing in the uvicorn process and Weave tracing would silently no-op. config is the
# earliest common import (session.py and weave_trace.py both import it) and runs before
# init_weave() reads WANDB_API_KEY, so loading here covers the whole process. load_dotenv
# is idempotent and never overrides already-set vars, so main.py calling it too is harmless.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env_bool(name: str, default: bool) -> bool:
    """Boolean env override: "0"/"false"/"no" (any case) means off; unset = default."""
    raw = os.environ.get(name)
    return default if raw is None else raw.lower() not in ("0", "false", "no")


def _env_float(name: str, default: float) -> float:
    """Float env override; a malformed value fails LOUDLY at import, not mid-call."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"config: {name}={raw!r} is not a number") from None

# ---------------------------------------------------------------- speech-to-text
STT_MODEL = "nova-3"          # Deepgram's streaming STT model
STT_SAMPLE_RATE = 16000       # browser downsamples mic to this; 16 kHz is plenty for speech
# How long a silence (ms) before Deepgram declares the utterance finished and sets
# speech_final=true. THE key latency/accuracy dial: lower = snappier turn-taking but
# clips slow speakers mid-thought; higher = laggier agent but fewer false endpoints.
STT_ENDPOINTING_MS = 300
# Fallback end-of-turn signal: if no speech_final arrives (e.g. endpointing missed),
# Deepgram emits UtteranceEnd after this much silence between finalized words.
STT_UTTERANCE_END_MS = 1000

# ---------------------------------------------------------------- barge-in
# What interrupts the agent when the caller speaks over it. A barge-in is ALWAYS
# committed by a recognized, non-empty user TRANSCRIPT — never by a raw VAD onset
# alone. Deepgram's VAD SpeechStarted fires on speech ONSET (before any transcript),
# but it also fires on the agent's own greeting echoing back through the speakers or on
# room noise, so trusting it on its own made the agent interrupt ITSELF while the user
# was silent. Instead, when BARGE_IN_ON_VAD is on, a VAD onset only "pre-arms"
# (records that speech energy was seen) and the next real transcript commits the
# interrupt — echo cancellation (static/app.js) keeps the agent's own voice from
# producing confident transcript words, so silence can never self-interrupt. Set to 0
# to ignore VAD onsets entirely (pure transcript-triggered barge-in); either way the
# transcript is the trigger, so the DEFAULT is safe.
BARGE_IN_ON_VAD = _env_bool("BARGE_IN_ON_VAD", True)
# Grace window (ms) after the agent starts SPEAKING before a VAD onset may pre-arm a
# barge-in. Belt-and-braces: suppresses noisy pre-arming during the echo-cancellation
# ramp-up at speech onset. (Correctness no longer depends on this — a VAD onset can't
# interrupt on its own regardless — it only reduces spurious pre-arm bookkeeping.)
BARGE_IN_VAD_GRACE_MS = _env_float("BARGE_IN_VAD_GRACE_MS", 250)

# ---------------------------------------------------------------- language model
LLM_MODEL = "gpt-4o-mini"     # low-latency, cheap; voice agents live and die on TTFT
LLM_TEMPERATURE = 0.3         # low temperature: consistency matters more than flair here
LLM_MAX_TOKENS = 300          # spoken replies are short; also caps worst-case TTS cost

# ---------------------------------------------------------------- text-to-speech
TTS_MODEL = "aura-2-thalia-en"  # Deepgram Aura-2 voice
TTS_SAMPLE_RATE = 24000         # linear16 PCM out; browser plays it with zero decoding
TTS_ENCODING = "linear16"

# ---------------------------------------------------------------- context management
# When the conversation history (excluding the system prompt) exceeds this many
# estimated tokens, old turns are compressed into a short "call notes" summary by a
# background LLM call BETWEEN turns — so it never adds to voice-to-voice latency.
CONTEXT_MAX_HISTORY_TOKENS = 2500
# How many of the most recent messages always stay verbatim (the model needs exact
# recent wording; older turns only need their facts).
CONTEXT_KEEP_RECENT_MESSAGES = 8

# ---------------------------------------------------------------- sentence chunking
# Minimum characters before we treat a sentence boundary as "enough to speak".
# Too low: TTS gets choppy fragments ("Yes." ... "So,"). Too high: TTFB suffers.
MIN_SENTENCE_CHARS = 20

# ---------------------------------------------------------------- image-turn filler
# Spoken (via TTS) to mask the ~1.8s vision-model latency when the customer sends a
# photo. These are NEVER sent to the LLM — zero extra tokens; audio only.
IMAGE_FILLER_PHRASES = (
    "Let me take a look.",
    "One moment while I look at that.",
    "Okay, let me see that.",
)

# ---------------------------------------------------------------- image auto-send
# A photo normally rides on the NEXT user turn (the caller sends a picture, then
# says "what's this?"). But if the caller just sends a photo and stays silent, we
# shouldn't wait forever: after this many seconds with a photo pending and no speech,
# auto-start a turn that feeds the image to the LLM on its own. The LLM message is the
# image followed by DEFAULT_IMAGE_PROMPT (image FIRST — the ordering that makes the
# vision model reliably look before it answers). Env-overridable for fast tests.
IMAGE_AUTOSEND_S = _env_float("IMAGE_AUTOSEND_S", 2.0)
DEFAULT_IMAGE_PROMPT = os.environ.get(
    "DEFAULT_IMAGE_PROMPT", "(The customer shared this photo — take a look.)")
# Attached as an extra text part on the image message (same multimodal vision call — no
# extra LLM round-trip) to reliably elicit the silent photo note. gpt-4o-mini follows a
# reminder adjacent to the image far more consistently than the system prompt alone. It is
# model INPUT only (never spoken) and is dropped when the image message is collapsed
# (collapse keeps only the first text part — the customer's words).
IMAGE_NOTE_REMINDER = (
    "(System reminder: a photo is attached to THIS turn. After your short spoken reply, you "
    "MUST append the silent photo note as the very last thing, in exactly this form: "
    "[[PHOTO: one-line factual description of what is visible]]. Never skip it on a photo "
    "turn and never speak it aloud.)")

# ---------------------------------------------------------------- inactivity timeout
# A silent (abandoned) call shouldn't stay open forever burning STT/TTS minutes.
# After IDLE_PROMPT_S of silence the agent nudges once ("are you still there?"); if
# still no response IDLE_HANGUP_S later it says goodbye and hangs up. Canned phrases,
# spoken via TTS — never sent to the LLM. Env-overridable (handy for fast tests).
IDLE_PROMPT_S = _env_float("IDLE_PROMPT_S", 15)
IDLE_HANGUP_S = _env_float("IDLE_HANGUP_S", 25)
IDLE_PROMPT_PHRASE = "Are you still there?"
IDLE_GOODBYE_PHRASE = ("I'll let you go for now. Call back any time you need a hand. "
                       "Goodbye.")

SERVER_HOST = os.environ.get("HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("PORT", "8000"))

# ---------------------------------------------------------------- latency logging
# Each session's per-turn + aggregate latency is written here as a timestamped JSON
# on teardown (see server/session.py::_write_latency_log). Gitignored.
LATENCY_LOG_DIR = Path(__file__).resolve().parent.parent / "session_logs"

# ---------------------------------------------------------------- Weave tracing
# Rich per-turn traces (transcript, latency, tool use, media) shipped to W&B Weave
# for reviewing completed conversations. See server/weave_trace.py. Tracing is
# built ENTIRELY at the turn boundary (off the voice hot path) and degrades to a
# no-op when WANDB_API_KEY is missing, so it never affects the live call.
WANDB_ENTITY = os.environ.get("WANDB_ENTITY", "wandb-smle")
WANDB_PROJECT = os.environ.get("WANDB_PROJECT", "jb_voice_deepgram_agent")
# Master switch (env "0"/"false" to disable even when a key is present).
WEAVE_ENABLED = _env_bool("WEAVE_ENABLED", True)
# Whether to attach the caller's photo and recorded utterance audio to each turn.
WEAVE_CAPTURE_MEDIA = _env_bool("WEAVE_CAPTURE_MEDIA", True)

# ---------------------------------------------------------------- vocal judge
# Acoustic frustration/fear classification of the caller's per-turn mic audio by an
# OpenAI audio model. Runs in a background task AFTER the turn ends (never on the
# voice hot path); its verdict lands in voice_turn.output as vocal_frustrated /
# vocal_fearful (null when there is no mic audio or the judge fails/times out).
# Server-side Weave monitors can't read weave.Audio inputs, hence this in-process
# path (see docs/03-production-challenges.md).
VOCAL_JUDGE_ENABLED = _env_bool("VOCAL_JUDGE_ENABLED", True)
VOCAL_JUDGE_MODEL = os.environ.get("VOCAL_JUDGE_MODEL", "gpt-audio-mini")
VOCAL_JUDGE_TIMEOUT_S = _env_float("VOCAL_JUDGE_TIMEOUT_S", 8.0)

# ---------------------------------------------------------------- Agent Lens
# Emit each finished voice turn to the Forge Agents view (Agent Lens) as a
# conversation turn with audio bubbles — a SEPARATE store from the Weave call
# traces, correlated by conversation_id == session_id. See server/agent_lens.py.
# Runs in a worker thread from the post-turn background task; off the hot path.
AGENT_LENS_ENABLED = _env_bool("AGENT_LENS_ENABLED", True)
AGENT_NAME = os.environ.get("AGENT_NAME", "repair-agent")
AGENT_VERSION = os.environ.get("AGENT_VERSION", "dev")

# ---------------------------------------------------------------- startup checks
def check_required_keys() -> None:
    """Fail fast with a fix-it message when a required secret is missing. Called
    once at process start (server/app.py) so a bad .env dies at boot with a clear
    error — not minutes later, mid-call, with a provider stack trace."""
    missing = [k for k in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required key(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in — see README, Getting started.")


def effective_config_line() -> str:
    """One greppable line of the live, non-secret config, logged at startup —
    answers "which knobs are actually on right now?" without reading code."""
    return (f"port={SERVER_PORT} stt={STT_MODEL} llm={LLM_MODEL} tts={TTS_MODEL} "
            f"barge_in_vad={'on' if BARGE_IN_ON_VAD else 'off'} "
            f"weave={'on' if WEAVE_ENABLED else 'off'} "
            f"vocal_judge={VOCAL_JUDGE_MODEL if VOCAL_JUDGE_ENABLED else 'off'} "
            f"agent_lens={'on' if AGENT_LENS_ENABLED else 'off'} "
            f"agent={AGENT_NAME}@{AGENT_VERSION}")
