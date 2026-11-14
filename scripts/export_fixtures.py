"""Export recorded Weave calls into committed replay fixtures (tests/fixtures/).

For each scenario below, downloads every voice_turn's `user_audio` (the caller's
mic WAV on the trace) for one conversation thread, trims leading silence with the
same `server.session._trim_leading_silence` used live (these recordings predate /
underuse the adaptive trim), and writes clip_NN.wav (16 kHz mono s16) plus a
manifest.json with transcripts. The happy_path scenario also gets the demo photos
copied in, with one inline image event placed after the turn that mentions
sending a photo (falls back to the mid-call turn if no transcript mentions one).

Re-run when: the source threads change, the trim algorithm improves, or you add a
scenario (edit SCENARIOS). Requires WANDB/WEAVE credentials in .env. Output is
deterministic from the trace store, so re-running just refreshes the fixtures:

    uv run scripts/export_fixtures.py
"""
import datetime
import io
import json
import re
import shutil
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import weave

from server.config import WANDB_ENTITY, WANDB_PROJECT
from server.session import _trim_leading_silence
from server.weave_trace import _pcm16_to_wav

FIXTURES = ROOT / "tests" / "fixtures"
IMAGES_SRC = Path("/Users/jbutt/Downloads/voice_repair_agent_images")
INLINE_IMAGE = "IMG_3341.jpeg"
EXTRA_IMAGES = ["IMG_3340.jpeg", "IMG_3342.jpeg"]
PHOTO_RE = re.compile(r"\b(photo|picture|image)s?\b", re.IGNORECASE)

SCENARIOS = {
    "happy_path": {
        "thread": "89922bd19e5a41f5b7a878998dde6f00",
        "description": "Ice maker fault: basics walkthrough, photo of the unit, "
                       "engineer booking, polite goodbye.",
        "images": True,
    },
    "frustrated": {
        "thread": "039cc6810b89433fb64b24ea979ce6bc",
        "description": "Frustrated caller plus two jailbreak attempts; agent "
                       "holds the line to a goodbye.",
        "images": False,
    },
}


def fetch_turns(client, thread_id: str) -> list[tuple[str, bytes]]:
    """(user_text, wav_bytes) per voice_turn of the thread, oldest first.
    Turns without mic audio (typed / image-autosend) are skipped with a note."""
    calls = list(client.get_calls(
        filter={"op_names": [f"weave:///{WANDB_ENTITY}/{WANDB_PROJECT}/op/voice_turn:*"]},
        sort_by=[{"field": "started_at", "direction": "desc"}], limit=2000))
    picked = sorted((c for c in calls if c.thread_id == thread_id),
                    key=lambda c: c.started_at)
    out = []
    for call in picked:
        audio = call.inputs.get("user_audio")
        text = call.inputs.get("user_text") or ""
        if audio is None:
            print(f"  SKIP turn {call.inputs.get('turn_id')} (no audio): {text[:60]!r}")
            continue
        data = getattr(audio, "data", None)
        if data is None and hasattr(audio, "export"):
            buf = io.BytesIO()
            audio.export(buf)
            data = buf.getvalue()
        if data:
            out.append((text, bytes(data)))
    return out


def export(name: str, spec: dict, client) -> None:
    out_dir = FIXTURES / name
    out_dir.mkdir(parents=True, exist_ok=True)
    turns = fetch_turns(client, spec["thread"])
    if not turns:
        raise SystemExit(f"{name}: no clips found for thread {spec['thread']}")

    events = []
    for i, (text, wav_bytes) in enumerate(turns, 1):
        with wave.open(io.BytesIO(wav_bytes)) as w:
            fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
            assert fmt == (16000, 1, 2), f"unexpected source wav format {fmt}"
            pcm = w.readframes(w.getnframes())
        fname = f"clip_{i:02d}.wav"
        (out_dir / fname).write_bytes(_pcm16_to_wav(_trim_leading_silence(pcm)))
        events.append({"type": "audio", "file": fname, "transcript": text})

    manifest = {
        "name": name,
        "description": spec["description"],
        "source_thread": spec["thread"],
        "exported_at": datetime.date.today().isoformat(),
        "events": events,
    }
    if spec["images"]:
        for img in [INLINE_IMAGE, *EXTRA_IMAGES]:
            shutil.copy2(IMAGES_SRC / img, out_dir / img)
        # Insert the inline photo right after the FIRST turn whose transcript
        # mentions a photo (the offer to send one; later mentions just refer back
        # to it), so the image rides the next spoken turn. Fall back to after the
        # mid-call diagnostic turn if nothing mentions a photo.
        idx = next((i for i, e in enumerate(events)
                    if PHOTO_RE.search(e["transcript"])), len(events) // 2 - 1)
        events.insert(idx + 1, {"type": "image", "file": INLINE_IMAGE})
        manifest["extra_images"] = EXTRA_IMAGES
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{name}: {len(turns)} clips -> {out_dir}")


if __name__ == "__main__":
    client = weave.init(f"{WANDB_ENTITY}/{WANDB_PROJECT}")
    for name, spec in SCENARIOS.items():
        export(name, spec, client)
