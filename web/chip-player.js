/** Browser player for the compact CHP1 files produced by midi_to_chip.py. */

const MAGIC = [0x43, 0x48, 0x50, 0x31]; // "CHP1"
export const DEFAULT_VOLUME = 0.6;

function clampVolume(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) throw new TypeError("volume must be a finite number");
  return Math.max(0, Math.min(1, numeric));
}

function readVarint(bytes, cursor) {
  let value = 0;
  for (let count = 0; count < 5; count += 1) {
    if (cursor.offset >= bytes.length) throw new Error("Truncated .chip varint");
    const byte = bytes[cursor.offset++];
    value = value * 128 + (byte & 0x7f);
    if ((byte & 0x80) === 0) return value;
  }
  throw new Error("Invalid .chip varint");
}

export function decodeChip(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  if (bytes.length < 7 || !MAGIC.every((value, index) => bytes[index] === value)) {
    throw new Error("Not a supported CHP1 chiptune file");
  }
  const quantumMs = bytes[4];
  const maxPolyphony = bytes[5];
  const cursor = { offset: 6 };
  const eventCount = readVarint(bytes, cursor);
  const notes = [];
  let startUnits = 0;

  for (let index = 0; index < eventCount; index += 1) {
    startUnits += readVarint(bytes, cursor);
    const durationUnits = readVarint(bytes, cursor);
    if (cursor.offset + 2 > bytes.length) throw new Error("Truncated .chip note");
    const midiNote = bytes[cursor.offset++];
    const packed = bytes[cursor.offset++];
    notes.push({
      start: (startUnits * quantumMs) / 1000,
      duration: (durationUnits * quantumMs) / 1000,
      midiNote,
      velocity: packed >> 2,
      waveform: packed & 0x03,
    });
  }
  if (cursor.offset !== bytes.length) throw new Error("Unexpected data at end of .chip file");
  const duration = notes.reduce((end, note) => Math.max(end, note.start + note.duration), 0);
  return { quantumMs, maxPolyphony, notes, duration };
}

export class ChipPlayer {
  constructor({ volume = DEFAULT_VOLUME } = {}) {
    this.context = null;
    this.master = null;
    this.limiter = null;
    this.volume = clampVolume(volume);
    this.sources = new Set();
    this.noiseBuffer = null;
    this.stopTimer = null;
  }

  async load(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Could not load ${url}: HTTP ${response.status}`);
    return decodeChip(await response.arrayBuffer());
  }

  async play(songOrBuffer) {
    const song = songOrBuffer instanceof ArrayBuffer ? decodeChip(songOrBuffer) : songOrBuffer;
    if (!song?.notes) throw new TypeError("play() expects a decoded song or ArrayBuffer");
    this.stop();
    this.context ??= new AudioContext();
    await this.context.resume();

    this.master = this.context.createGain();
    this.master.gain.value = this.volume;
    this.limiter = this.context.createDynamicsCompressor();
    this.limiter.threshold.value = -10;
    this.limiter.knee.value = 12;
    this.limiter.ratio.value = 8;
    this.limiter.attack.value = 0.003;
    this.limiter.release.value = 0.18;
    this.master.connect(this.limiter).connect(this.context.destination);
    const beginsAt = this.context.currentTime + 0.04;
    for (const note of song.notes) this.#scheduleNote(note, beginsAt);
    this.stopTimer = window.setTimeout(() => this.stop(), (song.duration + 0.2) * 1000);
    return song.duration;
  }

  setVolume(value) {
    this.volume = clampVolume(value);
    if (this.master) this.master.gain.value = this.volume;
    return this.volume;
  }

  stop() {
    if (this.stopTimer !== null) window.clearTimeout(this.stopTimer);
    this.stopTimer = null;
    for (const source of this.sources) {
      try { source.stop(); } catch { /* It may already have stopped. */ }
    }
    this.sources.clear();
    this.master?.disconnect();
    this.limiter?.disconnect();
    this.master = null;
    this.limiter = null;
  }

  #scheduleNote(note, origin) {
    const startsAt = origin + note.start;
    const endsAt = startsAt + note.duration;
    const amplitude = (note.velocity / 31) ** 1.4;
    const envelope = this.context.createGain();
    const attackEnd = Math.min(startsAt + 0.008, endsAt);
    const releaseStart = Math.max(attackEnd, endsAt - 0.025);
    envelope.gain.setValueAtTime(0, startsAt);
    envelope.gain.linearRampToValueAtTime(amplitude, attackEnd);
    envelope.gain.setValueAtTime(amplitude * 0.82, releaseStart);
    envelope.gain.linearRampToValueAtTime(0, endsAt);
    envelope.connect(this.master);

    if (note.waveform === 3) {
      const source = this.context.createBufferSource();
      source.buffer = this.#getNoiseBuffer();
      source.loop = true;
      const filter = this.context.createBiquadFilter();
      filter.type = "bandpass";
      filter.frequency.value = Math.min(12000, 80 * 2 ** (note.midiNote / 12));
      filter.Q.value = 0.8;
      source.connect(filter).connect(envelope);
      this.#startAndTrack(source, startsAt, endsAt);
      return;
    }

    const oscillator = this.context.createOscillator();
    oscillator.type = ["square", "triangle", "sawtooth"][note.waveform];
    oscillator.frequency.value = 440 * 2 ** ((note.midiNote - 69) / 12);
    oscillator.connect(envelope);
    this.#startAndTrack(oscillator, startsAt, endsAt);
  }

  #startAndTrack(source, startsAt, endsAt) {
    this.sources.add(source);
    source.addEventListener("ended", () => this.sources.delete(source), { once: true });
    source.start(startsAt);
    source.stop(endsAt);
  }

  #getNoiseBuffer() {
    if (this.noiseBuffer) return this.noiseBuffer;
    const length = this.context.sampleRate;
    const buffer = this.context.createBuffer(1, length, this.context.sampleRate);
    const samples = buffer.getChannelData(0);
    for (let index = 0; index < length; index += 1) samples[index] = Math.random() * 2 - 1;
    this.noiseBuffer = buffer;
    return buffer;
  }
}
