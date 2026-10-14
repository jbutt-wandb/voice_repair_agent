/**
 * TTS playback worklet: a ring buffer player.
 *
 * Why a ring buffer instead of queueing AudioBuffers with source.start()?
 *   1. Queued buffers click at chunk seams unless you schedule sample-exactly.
 *   2. Barge-in needs playback to stop INSTANTLY — you can't un-schedule a
 *      half-played AudioBuffer cleanly, but zeroing two pointers is immediate.
 *
 * Main thread posts float32 audio (already resampled to the context rate);
 * `process()` drains it 128 samples at a time, outputting silence when empty.
 *
 * Messages in:  Float32Array audio, or {cmd: "clear"}
 * Messages out: {type: "playing"}  — first sample of a new burst is being played
 *               {type: "drained"}  — buffer just ran empty (agent finished a burst)
 */

const CAPACITY = 24000 * 2 * 30; // ~30s of context-rate audio; plenty for any reply

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(CAPACITY);
    this.readPos = 0;
    this.writePos = 0;
    this.wasPlaying = false;
    this.port.onmessage = (e) => {
      if (e.data && e.data.cmd === 'clear') {
        // Barge-in: discard everything not yet played. This one line is the
        // entire client half of interruption handling.
        this.readPos = this.writePos = 0;
        this.wasPlaying = false;
        return;
      }
      const audio = e.data; // Float32Array
      for (let i = 0; i < audio.length; i++) {
        this.buf[this.writePos % CAPACITY] = audio[i];
        this.writePos++;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    const available = this.writePos - this.readPos;
    const n = Math.min(out.length, available);
    for (let i = 0; i < n; i++) {
      out[i] = this.buf[this.readPos % CAPACITY];
      this.readPos++;
    }
    for (let i = n; i < out.length; i++) out[i] = 0; // silence when starved

    // Edge detection for the main thread: started playing / ran dry.
    if (n > 0 && !this.wasPlaying) {
      this.wasPlaying = true;
      this.port.postMessage({ type: 'playing' });
    } else if (n === 0 && this.wasPlaying) {
      this.wasPlaying = false;
      this.port.postMessage({ type: 'drained' });
    }
    return true;
  }
}

registerProcessor('playback-processor', PlaybackProcessor);
