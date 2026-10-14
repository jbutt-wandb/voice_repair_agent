"""All tuning knobs in one place. These are the dials you would sweep when
tuning a production voice agent — the README explains the tradeoff behind each."""

import os

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

SERVER_HOST = os.environ.get("HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("PORT", "8000"))
