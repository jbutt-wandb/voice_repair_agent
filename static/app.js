/**
 * Main-thread client: one WebSocket to the backend, two AudioWorklets.
 *
 *   mic ──capture-worklet──▶ ws.send(16k int16 PCM)          (upstream audio)
 *   ws.onmessage binary ──resample──▶ playback-worklet ──▶ speakers
 *   ws.onmessage JSON   ──▶ transcript / state / metrics UI  (downstream events)
 *
 * Everything latency-critical lives on the audio thread (the worklets); this
 * file only shovels buffers and renders UI.
 */

'use strict';

const TTS_RATE = 24000; // must match server config TTS_SAMPLE_RATE

// ---------------------------------------------------------------- state
let ws = null;
let audioCtx = null;
let micStream = null;
let playbackNode = null;
let currentTurnId = 0;        // latest turn id seen from the server
let lastPlaybackTurn = -1;    // dedupe playback_started per turn
let isPlaying = false;        // playback worklet currently has audio
let callEnded = false;        // agent hung up; tear down once audio drains
let micOff = false;           // mic unavailable this session (text-only mode)
let userBubble = null;        // current user utterance bubble (merges finals)
let interimBubble = null;     // floating gray italic interim line
const agentBubbles = new Map();   // turn_id -> element
const metricRows = new Map();     // turn_id -> <tr>

// ---------------------------------------------------------------- dom
const $ = (id) => document.getElementById(id);
const transcript = $('transcript');
const badge = $('state-badge');
const connectBtn = $('connect-btn');

// ---------------------------------------------------------------- connect / teardown

connectBtn.onclick = () => (ws ? endCall() : startCall());

async function startCall() {
  // Fresh session: new server-side Session, new conversation — reset the UI too.
  callEnded = false;
  isPlaying = false;
  micOff = false;
  currentTurnId = 0;
  lastPlaybackTurn = -1;
  userBubble = interimBubble = null;
  agentBubbles.clear();
  transcript.innerHTML = '';
  metricRows.clear();
  document.querySelector('#metrics-table tbody').innerHTML = '';
  $('metrics-avg').innerHTML = '<td>avg</td><td>–</td><td>–</td><td>–</td><td>–</td><td>–</td><td></td>';

  // AudioContext must be created inside a user gesture (autoplay policy).
  audioCtx = new AudioContext();
  await audioCtx.audioWorklet.addModule('capture-worklet.js');
  await audioCtx.audioWorklet.addModule('playback-worklet.js');

  // Echo cancellation is NOT optional: without it the mic picks up the agent's
  // own voice from the speakers and the agent barge-ins on itself.
  //
  // Acquiring the mic fails for several distinct reasons, and the app is useless
  // if it fails silently — the user just sees a call where nothing they say
  // registers. So diagnose precisely and SHOW it; fall back to text-only loudly.
  micStream = await acquireMic();

  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    connectBtn.textContent = 'End call';
    connectBtn.classList.add('live');

    // Mic -> capture worklet -> WebSocket (binary frames up).
    if (micStream) {
      const source = audioCtx.createMediaStreamSource(micStream);
      const capture = new AudioWorkletNode(audioCtx, 'capture-processor');
      source.connect(capture);
      capture.port.onmessage = (e) => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
      };
    }

    // Playback worklet -> speakers (binary frames down land here).
    playbackNode = new AudioWorkletNode(audioCtx, 'playback-processor');
    playbackNode.connect(audioCtx.destination);
    playbackNode.port.onmessage = (e) => {
      if (e.data.type === 'playing') {
        isPlaying = true;
        if (lastPlaybackTurn !== currentTurnId) {
          // First audible sample of this turn: lets the server compute the
          // voice-to-voice latency the user actually experienced.
          lastPlaybackTurn = currentTurnId;
          sendJson({ type: 'playback_started', turn_id: currentTurnId });
        }
      } else if (e.data.type === 'drained') {
        isPlaying = false;
        // The agent's goodbye has finished playing: NOW hang up.
        if (callEnded) hangUp();
      }
    };
  };

  const upsample = makeResampler(TTS_RATE, audioCtx.sampleRate);
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      // TTS audio: int16 @ 24 kHz -> float32 @ context rate -> ring buffer.
      const int16 = new Int16Array(e.data);
      const float32 = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
      playbackNode.port.postMessage(upsample(float32));
    } else {
      handleEvent(JSON.parse(e.data));
    }
  };

  ws.onclose = endCall;
  ws.onerror = endCall;
}

function endCall() {
  if (ws) { ws.onclose = null; ws.onerror = null; ws.close(); ws = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  playbackNode = null;
  isPlaying = false;
  connectBtn.textContent = 'Start call';
  connectBtn.classList.remove('live');
  setBadge('idle', callEnded ? 'call ended' : 'disconnected');
  setStages([]);
}

function hangUp() {
  // Natural end of call (vs. the user clicking "End call").
  const note = document.createElement('div');
  note.className = 'bubble interim';
  note.textContent = '— call ended — press "Start call" to begin a new session —';
  transcript.appendChild(note);
  scrollDown();
  endCall();
}

function sendJson(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// ---------------------------------------------------------------- microphone

async function acquireMic() {
  // getUserMedia only exists in a SECURE context. localhost/127.0.0.1 count as
  // secure even over http, but a LAN IP (e.g. http://192.168.1.5:8001, or opening
  // the demo from a phone) does NOT — mediaDevices is undefined there. This is the
  // single most common "the mic doesn't work" cause, so check it explicitly.
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    micWarning(`Microphone blocked: this page is not a secure context ` +
      `(${location.origin}). Browsers only allow mic access over https or on ` +
      `localhost/127.0.0.1 — not a LAN IP address. Open the app at ` +
      `http://localhost:${location.port || 80} instead.`);
    return null;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    // Permission can be granted but the track still dead on arrival (OS-muted,
    // device unplugged mid-grant). Surface that too rather than go silently deaf.
    const track = stream.getAudioTracks()[0];
    if (!track || track.readyState === 'ended') {
      micWarning('Microphone permission was granted but no live audio track is ' +
                 'available — check the device is connected and not in use elsewhere.');
      stream.getTracks().forEach((t) => t.stop());
      return null;
    }
    return stream;
  } catch (err) {
    const msg = {
      NotAllowedError: 'Microphone permission was denied. Click the camera/mic icon ' +
        'in the address bar, allow access for this site, and press Start call again.',
      NotFoundError: 'No microphone was found. Plug one in (or check OS input ' +
        'settings), then press Start call again.',
      NotReadableError: 'The microphone is in use by another app and could not be ' +
        'opened. Close the other app and try again.',
      OverconstrainedError: 'No microphone matched the requested audio settings.',
    }[err.name] || `Could not access the microphone (${err.name}: ${err.message}).`;
    micWarning(msg);
    return null;
  }
}

function micWarning(text) {
  console.warn('[mic]', text);
  const note = document.createElement('div');
  note.className = 'bubble syswarn';
  note.textContent = '⚠ ' + text + ' — you can still type in the box below.';
  transcript.appendChild(note);
  scrollDown();
  micOff = true;
}

// ---------------------------------------------------------------- server events

function handleEvent(ev) {
  if (ev.turn_id !== undefined) currentTurnId = Math.max(currentTurnId, ev.turn_id);
  switch (ev.type) {
    case 'state': {
      // In text-only mode "listening" is misleading — nothing is being heard.
      setBadge(ev.state, ev.state === 'listening' && micOff ? 'type to reply' : ev.state);
      const stages = { listening: ['mic', 'stt'], thinking: ['llm'],
                       speaking: ['tts', 'spk'] }[ev.state] || [];
      setStages(micOff ? stages.filter((s) => s !== 'mic' && s !== 'stt') : stages);
      if (ev.state === 'thinking') userBubble = null; // user turn is closed
      break;
    }
    case 'interim_transcript': {
      if (!interimBubble) interimBubble = addBubble('interim');
      interimBubble.textContent = ev.text;
      scrollDown();
      break;
    }
    case 'final_transcript': {
      if (interimBubble) { interimBubble.remove(); interimBubble = null; }
      // Start a fresh bubble when the turn changes (a new user utterance), so
      // separate turns don't concatenate into one bubble.
      if (!userBubble || userBubble.dataset.turn !== String(ev.turn_id)) {
        userBubble = addBubble('user');
        userBubble.dataset.turn = String(ev.turn_id);
      }
      userBubble.textContent = (userBubble.textContent + ' ' + ev.text).trim();
      scrollDown();
      break;
    }
    case 'agent_text_delta': {
      let b = agentBubbles.get(ev.turn_id);
      if (!b) { b = addBubble('agent'); agentBubbles.set(ev.turn_id, b); }
      b.textContent += ev.text;
      scrollDown();
      break;
    }
    case 'agent_text_done': {
      if (ev.interrupted) markInterrupted(ev.turn_id);
      break;
    }
    case 'tool_call': {
      if (ev.name !== 'book_engineer_visit') break; // e.g. end_call has no card
      const card = document.createElement('div');
      card.className = 'booking-card';
      const r = ev.result;
      card.innerHTML = `<strong>Engineer booked</strong><br>` +
        `${esc(r.engineer)} · ${esc(r.date)} · ${esc(r.time_window)}<br>` +
        `ref <strong>${esc(r.booking_ref)}</strong> — ${esc(r.issue_summary)}`;
      transcript.appendChild(card);
      scrollDown();
      break;
    }
    case 'call_ended': {
      // Natural hang-up: the agent asked "anything else?", the user was done, the
      // model called its end_call tool. Wait for the goodbye audio to finish, then
      // tear down. "Start call" begins a completely fresh session.
      callEnded = true;
      setBadge('idle', 'call ended');
      if (!isPlaying) setTimeout(() => { if (callEnded) hangUp(); }, 800);
      break;
    }
    case 'barge_in': {
      // Stop sound NOW; the server has already cancelled LLM + TTS upstream.
      if (playbackNode) playbackNode.port.postMessage({ cmd: 'clear' });
      // Mark the cut-off reply even when its text had already fully streamed
      // (barge-in can land while only the AUDIO is still playing).
      markInterrupted(ev.turn_id);
      break;
    }
    case 'turn_metrics': upsertMetrics(ev); break;
    case 'error': console.error('server error:', ev.message); break;
  }
}

// ---------------------------------------------------------------- UI helpers

function addBubble(cls) {
  const div = document.createElement('div');
  div.className = `bubble ${cls}`;
  transcript.appendChild(div);
  return div;
}

function scrollDown() { transcript.scrollTop = transcript.scrollHeight; }

function markInterrupted(turnId) {
  const b = agentBubbles.get(turnId);
  if (b && !b.querySelector('.interrupted')) {
    const tag = document.createElement('div');
    tag.className = 'interrupted';
    tag.textContent = '— interrupted —';
    b.appendChild(tag);
  }
}

function setBadge(cls, text) {
  badge.className = `badge ${cls}`;
  badge.textContent = text;
}

function setStages(active) {
  document.querySelectorAll('.stage').forEach((el) =>
    el.classList.toggle('active', active.includes(el.dataset.stage)));
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

// ---------------------------------------------------------------- metrics table

const COLS = ['stt_final_ms', 'llm_ttft_ms', 'tts_ttfb_ms', 'v2v_server_ms', 'v2v_client_ms'];

function upsertMetrics(m) {
  let row = metricRows.get(m.turn_id);
  if (!row) {
    row = document.createElement('tr');
    metricRows.set(m.turn_id, row);
    document.querySelector('#metrics-table tbody').appendChild(row);
  }
  const cells = COLS.map((c) => `<td>${m[c] ?? '–'}</td>`).join('');
  row.innerHTML = `<td>${m.turn_id}</td>${cells}` +
                  `<td class="int">${m.barge_in ? '✂' : ''}</td>`;
  row.dataset.metrics = JSON.stringify(m);

  // Running averages across all completed turns.
  const all = [...metricRows.values()].map((r) => JSON.parse(r.dataset.metrics));
  const avg = (col) => {
    const xs = all.map((x) => x[col]).filter((v) => v != null);
    return xs.length ? Math.round(xs.reduce((a, b) => a + b) / xs.length) : '–';
  };
  $('metrics-avg').innerHTML =
    `<td>avg</td>${COLS.map((c) => `<td>${avg(c)}</td>`).join('')}<td></td>`;
}

// ---------------------------------------------------------------- text input (no-mic path)

$('text-form').onsubmit = (e) => {
  e.preventDefault();
  const input = $('text-input');
  const text = input.value.trim();
  if (text && ws) {
    sendJson({ type: 'text_input', text });
    input.value = '';
  }
};

// ---------------------------------------------------------------- resampling

/**
 * Streaming linear-interpolation resampler with phase continuity across chunks
 * (chunk-local interpolation would click at every chunk boundary).
 */
function makeResampler(fromRate, toRate) {
  const step = fromRate / toRate;
  let last = 0;      // final sample of the previous chunk
  let phase = 0;     // fractional position, relative to -1 = `last`
  return (input) => {
    const out = [];
    let pos = phase - 1; // -1 means "interpolate from `last` to input[0]"
    while (pos + 1 < input.length) {
      const i0 = Math.floor(pos);
      const frac = pos - i0;
      const s0 = i0 < 0 ? last : input[i0];
      const s1 = input[i0 + 1];
      out.push(s0 + (s1 - s0) * frac);
      pos += step;
    }
    phase = pos - input.length + 1;
    last = input[input.length - 1];
    return Float32Array.from(out);
  };
}
