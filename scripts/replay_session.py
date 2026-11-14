"""Replay a recorded call's caller audio through the live app — no re-recording.

    uv run main.py &                          # server must be up
    uv run scripts/replay_session.py          # replay the newest traced call
    uv run scripts/replay_session.py --thread <thread_id>   # or a specific one

Downloads each voice_turn's `user_audio` (the caller's mic, stored as playable
media on the Weave trace) for one conversation thread, then streams the clips
over the real WebSocket mic path exactly as a browser would: 1280-byte PCM
chunks every 40 ms (real-time pacing — Deepgram's endpointing is time-based, so
faster-than-realtime would change turn segmentation), a 2 s silence tail per
clip so the turn closes, waiting out the agent's reply between clips.

The replayed session produces brand-new voice_turn traces carrying the SAME
caller audio — which is what makes this the dataset source for audio-path
evaluations (docs/evals/proposal.md): monitors, scorers, judge prompts, or
pipeline changes can be re-tested against identical input without anyone
re-acting the conversation. Requires WEAVE/WANDB to be configured (the clips
come from the trace store).
"""
import argparse
import asyncio
import io
import json
import os
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import weave
import websockets

from server.config import WANDB_ENTITY, WANDB_PROJECT

URL = os.environ.get("VRA_WS_URL", "ws://127.0.0.1:8000/ws")


def fetch_clips(thread_id: str | None) -> list[tuple[int, bytes]]:
    """The (turn_id, wav_bytes) clips of one thread, oldest first. Defaults to the
    newest thread that has voice_turn calls with recorded caller audio."""
    client = weave.init(f"{WANDB_ENTITY}/{WANDB_PROJECT}")
    # Filter by op SERVER-side: a recency-window fetch silently returns zero
    # clips for older threads once the project holds many calls (each turn also
    # logs judge/scorer/child calls that crowd out the window).
    calls = list(client.get_calls(
        filter={"op_names": [f"weave:///{WANDB_ENTITY}/{WANDB_PROJECT}/op/voice_turn:*"]},
        sort_by=[{"field": "started_at", "direction": "desc"}], limit=2000))
    turns = [c for c in calls if "voice_turn" in str(c.op_name)]
    if thread_id is None:
        if not turns:
            raise SystemExit("no voice_turn calls found in the project")
        thread_id = turns[0].thread_id
    picked = sorted((c for c in turns if c.thread_id == thread_id),
                    key=lambda c: c.started_at)
    clips = []
    for call in picked:
        audio = call.inputs.get("user_audio")
        if audio is None:
            continue  # typed / image-autosend turn: no mic audio
        data = getattr(audio, "data", None)
        if data is None and hasattr(audio, "export"):
            buf = io.BytesIO()
            audio.export(buf)
            data = buf.getvalue()
        if data:
            clips.append((call.inputs.get("turn_id"), bytes(data)))
    print(f"thread {thread_id}: {len(clips)} clips with caller audio")
    return clips


def wav_to_pcm16k(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        assert fmt == (16000, 1, 2), f"unexpected wav format {fmt}"
        return w.readframes(w.getnframes())


async def replay(clips: list[tuple[int, bytes]]) -> None:
    async with websockets.connect(URL, max_size=None) as ws:
        events: list[dict] = []

        async def pump_until(pred, timeout=120):
            async with asyncio.timeout(timeout):
                while True:
                    msg = await ws.recv()
                    if isinstance(msg, bytes):
                        continue  # agent TTS audio: a replay client just drops it
                    ev = json.loads(msg)
                    events.append(ev)
                    if pred(ev):
                        return ev

        # The agent greets first; wait until it's listening.
        await pump_until(lambda e: e["type"] == "state" and e["state"] == "listening")
        print(f"greeting done, replaying {len(clips)} clips")

        for turn_id, wav in clips:
            pcm = wav_to_pcm16k(wav) + b"\x00" * 16000 * 2 * 2  # 2s tail -> endpoint
            print(f"  clip (orig turn {turn_id}): {len(pcm) / 32000:.1f}s ... ",
                  end="", flush=True)

            async def mic_pump():
                for i in range(0, len(pcm), 1280):   # 1280 B = 40 ms @ 16 kHz mono s16
                    await ws.send(pcm[i:i + 1280])
                    await asyncio.sleep(0.04)

            pump = asyncio.create_task(mic_pump())
            try:
                ev = await pump_until(
                    lambda e: (e["type"] == "state" and e["state"] == "listening")
                    or e["type"] == "call_ended")
            finally:
                pump.cancel()
            if ev["type"] == "call_ended":
                print("call ended by agent")
                return
            finals = [e for e in events if e["type"] == "final_transcript"]
            print("ok", repr(finals[-1]["text"][:60]) if finals else "(no transcript)")
    print("replay complete")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--thread", help="Weave thread id to replay (default: newest)")
    args = ap.parse_args()
    clips = fetch_clips(args.thread)
    if not clips:
        raise SystemExit("no clips to replay")
    asyncio.run(replay(clips))
