/**
 * Mic capture worklet: runs on the audio rendering thread.
 *
 * The browser gives us float32 audio at the AudioContext rate (usually 48000 Hz,
 * sometimes 44100). Deepgram wants 16000 Hz linear16 (int16) mono. This worklet:
 *   1. downsamples to 16 kHz with linear interpolation (handles non-integer
 *      ratios like 44100/16000, with phase carried across 128-sample blocks),
 *   2. converts float32 [-1,1] -> int16,
 *   3. batches into 40 ms frames (640 samples = 1280 bytes) and posts them to
 *      the main thread, which forwards them over the WebSocket.
 *
 * 40 ms is a good streaming granularity: small enough for low latency, large
 * enough that per-message overhead stays negligible.
 */

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 640; // 40 ms at 16 kHz

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE; // input samples per output sample
    this.phase = 0;                        // fractional read position in `carry+input`
    this.carry = new Float32Array(0);      // unconsumed input from the previous block
    this.frame = new Int16Array(FRAME_SAMPLES);
    this.frameFill = 0;
  }

  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;

    // Join leftover input with the new block so interpolation never sees a seam.
    const buf = new Float32Array(this.carry.length + input.length);
    buf.set(this.carry);
    buf.set(input, this.carry.length);

    let pos = this.phase;
    while (pos + 1 < buf.length) {
      const i0 = Math.floor(pos);
      const frac = pos - i0;
      const sample = buf[i0] + (buf[i0 + 1] - buf[i0]) * frac;
      // float32 -> int16 with clamping
      this.frame[this.frameFill++] = Math.max(-32768, Math.min(32767, sample * 32768)) | 0;
      if (this.frameFill === FRAME_SAMPLES) {
        // Transfer the buffer (zero-copy) to the main thread.
        const out = this.frame.buffer.slice(0);
        this.port.postMessage(out, [out]);
        this.frameFill = 0;
      }
      pos += this.ratio;
    }

    // Keep the tail we haven't consumed; carry the fractional phase with it.
    const keepFrom = Math.floor(pos);
    this.carry = buf.slice(keepFrom);
    this.phase = pos - keepFrom;
    return true; // keep processor alive
  }
}

registerProcessor('capture-processor', CaptureProcessor);
