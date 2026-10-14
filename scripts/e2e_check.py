"""End-to-end pipeline check — run with the server up:

    uv run main.py &                      # or PORT=8001 uv run main.py
    uv run scripts/e2e_check.py           # VRA_WS_URL overrides the target

Simulates the browser over the real WebSocket, no mic or speakers needed:
  A greeting      — expect agent text + TTS audio + state listening
  B text turn     — text_input through LLM+TTS, expect metrics
  C barge-in      — interrupt while SPEAKING, expect barge_in + clean next turn
  D voice turn    — synthesized speech PCM through the real mic path (STT)
  E natural end   — "I'm all set" → end_call tool → call_ended → teardown
"""
import asyncio, json, sys, array, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import websockets

URL = os.environ.get("VRA_WS_URL", "ws://127.0.0.1:8000/ws")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}: {name} {detail}")


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.events = []       # JSON events
        self.audio_bytes = 0

    async def pump_until(self, pred, timeout=45, collect_audio=True):
        """Read messages until pred(event) is true; returns matching event."""
        async with asyncio.timeout(timeout):
            while True:
                msg = await self.ws.recv()
                if isinstance(msg, bytes):
                    self.audio_bytes += len(msg)
                    continue
                ev = json.loads(msg)
                self.events.append(ev)
                if pred(ev):
                    return ev

    def got(self, type_, **fields):
        return any(e["type"] == type_ and all(e.get(k) == v for k, v in fields.items())
                   for e in self.events)


async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        c = Client(ws)

        print("Phase A: greeting")
        await c.pump_until(lambda e: e["type"] == "state" and e["state"] == "listening")
        check("greeting text streamed", c.got("agent_text_delta"))
        check("greeting audio received", c.audio_bytes > 10000, f"({c.audio_bytes} bytes)")
        check("greeting metrics emitted", c.got("turn_metrics"))

        print("Phase B: text turn through LLM + TTS")
        c.events.clear(); c.audio_bytes = 0
        await ws.send(json.dumps({"type": "text_input",
                                  "text": "The plate in my microwave stopped spinning."}))
        await c.pump_until(lambda e: e["type"] == "state" and e["state"] == "listening")
        check("final_transcript echoed", c.got("final_transcript"))
        check("agent replied", c.got("agent_text_done", interrupted=False))
        check("reply audio received", c.audio_bytes > 10000, f"({c.audio_bytes} bytes)")
        m = [e for e in c.events if e["type"] == "turn_metrics"][-1]
        check("llm_ttft measured", m.get("llm_ttft_ms") is not None, f"({m.get('llm_ttft_ms')} ms)")
        check("tts_ttfb measured", m.get("tts_ttfb_ms") is not None, f"({m.get('tts_ttfb_ms')} ms)")
        check("v2v_server measured", m.get("v2v_server_ms") is not None, f"({m.get('v2v_server_ms')} ms)")

        print("Phase C: barge-in while agent is speaking")
        c.events.clear(); c.audio_bytes = 0
        await ws.send(json.dumps({"type": "text_input",
                                  "text": "Tell me everything about cleaning the drive coupling in detail."}))
        await c.pump_until(lambda e: e["type"] == "state" and e["state"] == "speaking")
        await ws.send(json.dumps({"type": "text_input",
                                  "text": "Actually never mind, it works now, thanks!"}))
        bi = await c.pump_until(lambda e: e["type"] == "barge_in", timeout=10)
        check("barge_in emitted", True, f"(interrupted turn {bi['turn_id']})")
        new_turn = bi["turn_id"] + 2  # +1 invalidation bump, +1 new turn
        # wait for the NEW turn to fully complete (not the listening sent at barge-in)
        await c.pump_until(lambda e: e["type"] == "state" and e["state"] == "listening"
                           and e["turn_id"] >= new_turn)
        check("interrupted turn metrics flagged",
              any(e["type"] == "turn_metrics" and e["turn_id"] == bi["turn_id"]
                  and e["barge_in"] for e in c.events))
        check("next turn completed after barge-in",
              c.got("agent_text_done", interrupted=False, turn_id=new_turn))
        # no stale audio: new turn's v2v must be plausible (>= its TTFT)
        m = [e for e in c.events if e["type"] == "turn_metrics"
             and e["turn_id"] == new_turn][-1]
        check("no stale audio after barge-in",
              m["v2v_server_ms"] is not None and m["v2v_server_ms"] >= m["llm_ttft_ms"],
              f"(v2v {m['v2v_server_ms']} ms vs ttft {m['llm_ttft_ms']} ms)")

        print("Phase D: voice turn — synthesized speech through the mic path")
        # Make speech audio with Deepgram TTS, downsample 24k -> 16k, pace like a mic.
        from server.tts import DeepgramTTS, AudioChunk, Flushed
        audio = bytearray()
        async with DeepgramTTS(os.environ["DEEPGRAM_API_KEY"]) as tts:
            await tts.speak("My ice maker is not cooling at all. Can an engineer come on Friday morning?")
            await tts.flush()
            async for ev in tts.events():
                if isinstance(ev, AudioChunk):
                    audio.extend(ev.data)
                elif isinstance(ev, Flushed):
                    break
        samples = array.array("h", bytes(audio))
        pcm16k = array.array("h", (samples[i] for i in range(len(samples)) if i % 3 != 2)).tobytes()
        pcm16k += b"\x00" * 16000 * 2 * 2  # 2s trailing silence so endpointing fires

        c.events.clear(); c.audio_bytes = 0
        voice_turn = new_turn + 1
        async def mic_pump():
            for i in range(0, len(pcm16k), 1280):
                await ws.send(pcm16k[i:i + 1280])
                await asyncio.sleep(0.04)
        pump = asyncio.create_task(mic_pump())
        ft = await c.pump_until(lambda e: e["type"] == "final_transcript"
                                and e["turn_id"] >= voice_turn - 1, timeout=40)
        check("STT transcribed the voice",
              "cool" in ft["text"].lower() or "ice" in ft["text"].lower(), repr(ft["text"]))
        await c.pump_until(lambda e: e["type"] == "state" and e["state"] == "listening"
                           and e["turn_id"] >= voice_turn, timeout=60)
        pump.cancel()
        m = [e for e in c.events if e["type"] == "turn_metrics"
             and e["turn_id"] >= voice_turn][-1]
        check("voice turn: agent replied with audio", c.audio_bytes > 10000, f"({c.audio_bytes} bytes)")
        check("voice turn: stt_final measured", m.get("stt_final_ms") is not None, f"({m.get('stt_final_ms')} ms)")
        check("voice turn: full v2v measured", m.get("v2v_server_ms") is not None, f"({m.get('v2v_server_ms')} ms)")
        print("last voice-turn metrics:", {k: v for k, v in m.items() if k != 'type'})

        print("Phase E: natural end of call")
        c.events.clear(); c.audio_bytes = 0
        await ws.send(json.dumps({"type": "text_input",
                                  "text": "Actually that's everything, I'm all set. "
                                          "Thanks for your help, goodbye!"}))
        ended = await c.pump_until(lambda e: e["type"] == "call_ended", timeout=45)
        check("call_ended emitted", True, f"(reason {ended.get('reason')})")
        check("end_call tool was called",
              any(e["type"] == "tool_call" and e["name"] == "end_call" for e in c.events))
        check("goodbye was spoken", c.audio_bytes > 5000, f"({c.audio_bytes} bytes)")
        # a real client hangs up after playback drains; simulate that
        await ws.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


raise SystemExit(asyncio.run(main()))
