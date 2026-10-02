// Tell React this is a test environment that wraps updates in act(); without
// it every render logs "The current testing environment is not configured to
// support act(...)".
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no media pipeline: HTMLMediaElement.play()/pause() throw "Not
// implemented". Resolve/no-op them so video components behave like a browser
// that simply isn't playing anything.
if (typeof window !== "undefined" && window.HTMLMediaElement) {
  window.HTMLMediaElement.prototype.play = function play() { return Promise.resolve(); };
  window.HTMLMediaElement.prototype.pause = function pause() {};
}
