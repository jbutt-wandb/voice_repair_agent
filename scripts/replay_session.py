"""Replay a recorded call's caller audio through the live app — no re-recording.

    uv run main.py &                          # server must be up
    uv run scripts/replay_session.py          # list the available scenarios
    uv run scripts/replay_session.py --scenario frustrated   # replay a local fixture
    uv run scripts/replay_session.py --thread <thread_id>    # ad-hoc: fetch from Weave

The primary mode is --scenario: it replays a LOCAL fixture checked into
tests/fixtures/<scenario>/ (wav clips + photos listed in manifest.json, produced
by scripts/export_fixtures.py) — no Weave access needed to fetch anything.
--thread is the ad-hoc fallback: it downloads each voice_turn's `user_audio`
(the caller's mic, stored as playable media on the Weave trace) for one
conversation thread. Either way the events are streamed over the real WebSocket
mic path exactly as a browser would: 1280-byte PCM chunks every 40 ms
(real-time pacing — Deepgram's endpointing is time-based, so faster-than-
realtime would change turn segmentation), a 2 s silence tail per clip so the
turn closes, waiting out the agent's reply between clips; photos go up as the
same {type: "image", data: <data URL>} JSON the browser sends.

The replayed session produces brand-new voice_turn traces carrying the SAME
caller audio — which is what makes this the dataset source for audio-path
evaluations (docs/evals/proposal.md): monitors, scorers, judge prompts, or
pipeline changes can be re-tested against identical input without anyone
re-acting the conversation. Only --thread requires WEAVE/WANDB to be configured
(the clips come from the trace store); --scenario reads everything from disk.
"""
import argparse
import asyncio
import base64
import io
import json
import mimetypes
import os
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import websockets

from server.config import WANDB_ENTITY, WANDB_PROJECT

URL = os.environ.get("VRA_WS_URL", "ws://127.0.0.1:8000/ws")
FIXTURES = ROOT / "tests" / "fixtures"


def list_scenarios() -> list[str]:
    """The local fixture scenarios available under tests/fixtures/."""
    return sorted(p.parent.name for p in FIXTURES.glob("*/manifest.json"))


def load_scenario(name: str) -> list[dict]:
    """The events of one local fixture scenario, files loaded from disk:
    audio -> {"type": "audio", "wav": bytes, "label": transcript} and
    image -> {"type": "image", "data_url": str, "label": filename}."""
    folder = FIXTURES / name
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        avail = ", ".join(list_scenarios()) or "none (run scripts/export_fixtures.py)"
        raise SystemExit(f"no manifest at {manifest_path}; available scenarios: {avail}")
    manifest = json.loads(manifest_path.read_text())
    events = []
    for ev in manifest["events"]:
        blob = (folder / ev["file"]).read_bytes()
        if ev["type"] == "audio":
            events.append({"type": "audio", "wav": blob,
                           "label": ev.get("transcript") or ev["file"]})
        elif ev["type"] == "image":
            # Encode exactly as the browser does (static/app.js): a base64 data URL.
            mime = mimetypes.guess_type(ev["file"])[0] or "image/jpeg"
            events.append({"type": "image", "label": ev["file"],
                           "data_url": f"data:{mime};base64,{base64.b64encode(blob).decode()}"})
        else:
            raise SystemExit(f"unknown event type {ev['type']!r} in {manifest_path}")
    print(f"scenario {name}: {len(events)} events — {manifest.get('description', '')}")
    return events


def fetch_clips(thread_id: str | None) -> list[tuple[int, bytes]]:
    """The (turn_id, wav_bytes) clips of one thread, oldest first. Defaults to the
    newest thread that has voice_turn calls with recorded caller audio."""
    import weave  # deferred: only --thread mode talks to the trace store

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


async def replay(items: list[dict]) -> None:
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

        def turn_over(e):
            return (e["type"] == "state" and e["state"] == "listening") \
                or e["type"] == "call_ended"

        # The agent greets first; wait until it's listening.
        await pump_until(lambda e: e["type"] == "state" and e["state"] == "listening")
        print(f"greeting done, replaying {len(items)} events")

        for n, item in enumerate(items):
            if item["type"] == "image":
                # Same JSON the browser sends from the photo picker (static/app.js).
                await ws.send(json.dumps({"type": "image", "data": item["data_url"]}))
                print(f"  image {item['label']} sent", end="", flush=True)
                nxt = items[n + 1] if n + 1 < len(items) else None
                if nxt is None or nxt["type"] == "image":
                    # No speech rides with this photo: the server auto-sends a lone
                    # image after IMAGE_AUTOSEND_S (2 s) of silence — wait that out,
                    # then wait for the agent's reply to finish.
                    print(" ... waiting for image auto-send turn", end="", flush=True)
                    await asyncio.sleep(3.5)
                    ev = await pump_until(turn_over)
                    if ev["type"] == "call_ended":
                        print(" call ended by agent")
                        return
                print()
                continue

            pcm = wav_to_pcm16k(item["wav"]) + b"\x00" * 16000 * 2 * 2  # 2s tail -> endpoint
            print(f"  clip {item['label'][:48]!r}: {len(pcm) / 32000:.1f}s ... ",
                  end="", flush=True)

            async def mic_pump():
                for i in range(0, len(pcm), 1280):   # 1280 B = 40 ms @ 16 kHz mono s16
                    await ws.send(pcm[i:i + 1280])
                    await asyncio.sleep(0.04)

            pump = asyncio.create_task(mic_pump())
            try:
                ev = await pump_until(turn_over)
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
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--scenario", metavar="NAME",
                      help="local fixture scenario under tests/fixtures/ (primary mode)")
    mode.add_argument("--thread", metavar="ID",
                      help="Weave thread id to fetch and replay (ad-hoc fallback)")
    args = ap.parse_args()
    if args.scenario:
        items = load_scenario(args.scenario)
    elif args.thread:
        items = [{"type": "audio", "wav": wav, "label": f"orig turn {turn_id}"}
                 for turn_id, wav in fetch_clips(args.thread)]
    else:
        ap.print_help()
        names = list_scenarios()
        print("\navailable scenarios:" if names else
              "\nno scenarios under tests/fixtures/ (produce them with scripts/export_fixtures.py)")
        for name in names:
            print(f"  --scenario {name}")
        sys.exit(0)
    if not items:
        raise SystemExit("no events to replay")
    asyncio.run(replay(items))
