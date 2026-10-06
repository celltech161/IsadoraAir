/* iPortal capture worklet (2.22B): forwards raw float PCM blocks to the page
 * while capturing. Lossless -- no codec between the microphone and the take. */
class IPortalCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capturing = false;
    this.port.onmessage = (event) => {
      if (event.data === "start") this.capturing = true;
      else if (event.data === "pause" || event.data === "stop") this.capturing = false;
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (this.capturing && input && input.length) {
      const copies = input.map((channel) => new Float32Array(channel));
      this.port.postMessage(copies, copies.map((c) => c.buffer));
    }
    return true;
  }
}

registerProcessor("iportal-capture", IPortalCaptureProcessor);
