import assert from "node:assert/strict";
import test from "node:test";

import { ChipPlayer, DEFAULT_VOLUME } from "../web/chip-player.js";


class AudioParamStub {
  constructor(value = 0) { this.value = value; }
}

class AudioNodeStub {
  constructor() { this.connectedTo = null; }
  connect(target) { this.connectedTo = target; return target; }
  disconnect() { this.connectedTo = null; }
}

class GainStub extends AudioNodeStub {
  constructor() { super(); this.gain = new AudioParamStub(1); }
}

class CompressorStub extends AudioNodeStub {
  constructor() {
    super();
    this.threshold = new AudioParamStub();
    this.knee = new AudioParamStub();
    this.ratio = new AudioParamStub();
    this.attack = new AudioParamStub();
    this.release = new AudioParamStub();
  }
}

class AudioContextStub {
  constructor() {
    this.currentTime = 0;
    this.destination = new AudioNodeStub();
    this.compressor = null;
  }
  async resume() {}
  createGain() { return new GainStub(); }
  createDynamicsCompressor() {
    this.compressor = new CompressorStub();
    return this.compressor;
  }
}


test("the generic player uses an audible default gain and a peak limiter", async () => {
  globalThis.AudioContext = AudioContextStub;
  globalThis.window = globalThis;
  const player = new ChipPlayer();

  assert.equal(DEFAULT_VOLUME, 0.6);
  assert.equal(player.volume, DEFAULT_VOLUME);
  await player.play({ notes: [], duration: 0 });

  assert.equal(player.master.gain.value, DEFAULT_VOLUME);
  assert.ok(player.context.compressor);
  assert.equal(player.master.connectedTo, player.context.compressor);
  assert.equal(player.context.compressor.connectedTo, player.context.destination);
  player.stop();
});


test("setVolume clamps values and updates active playback", async () => {
  globalThis.AudioContext = AudioContextStub;
  globalThis.window = globalThis;
  const player = new ChipPlayer({ volume: 0.4 });
  await player.play({ notes: [], duration: 0 });

  assert.equal(player.setVolume(2), 1);
  assert.equal(player.volume, 1);
  assert.equal(player.master.gain.value, 1);
  assert.equal(player.setVolume(-1), 0);
  assert.equal(player.master.gain.value, 0);
  player.stop();
});
