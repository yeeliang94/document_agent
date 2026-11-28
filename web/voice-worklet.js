// Turns the microphone's audio into what the live transcription model takes: 24 kHz mono 16-bit samples, posted in
// pieces of about 100 ms. The browser records at its own rate (often 48 kHz), so the samples are resampled here.
// Sent 'flush', it posts what it holds and then { end: true }.

const RATE = 24000;
const PIECE = 2400; // 100 ms at 24 kHz

class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / RATE; // input samples per output sample
    this.at = 0; // position of the next output sample in the incoming stream
    this.last = 0; // the previous block's last sample, for the step across blocks
    this.out = new Int16Array(PIECE);
    this.filled = 0;
    this.port.onmessage = (e) => {
      if (e.data !== 'flush') return;
      this.post();
      this.port.postMessage({ end: true });
    };
  }

  post() {
    if (!this.filled) return;
    const piece = this.out.slice(0, this.filled);
    this.port.postMessage(piece.buffer, [piece.buffer]);
    this.filled = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    // Linear interpolation between neighbouring samples; -1 stands for the last sample of the previous block.
    while (this.at < input.length - 1) {
      const i = Math.floor(this.at);
      const left = i < 0 ? this.last : input[i];
      const right = input[i + 1];
      const value = left + (right - left) * (this.at - i);
      this.out[this.filled++] = Math.max(-1, Math.min(1, value)) * 0x7fff;
      if (this.filled === PIECE) this.post();
      this.at += this.step;
    }
    this.at -= input.length;
    this.last = input[input.length - 1];
    return true;
  }
}

registerProcessor('pcm16', Pcm16);
