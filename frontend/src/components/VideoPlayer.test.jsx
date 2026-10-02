import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import VideoPlayer from "./VideoPlayer.jsx";

let container;
let root;

function mount(props) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(VideoPlayer, { src: "/v/clip.mp4", ...props }));
  });
  return container;
}

function byLabel(label) {
  return container.querySelector(`[aria-label="${label}"]`);
}

beforeEach(() => {
  window.HTMLMediaElement.prototype.play = vi.fn(async () => undefined);
  window.HTMLMediaElement.prototype.pause = vi.fn();
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("VideoPlayer", () => {
  it("renders the accessible player chrome for its label", () => {
    mount({ label: "clip" });
    const wrap = container.querySelector(".vplayer");
    expect(wrap.getAttribute("role")).toBe("application");
    expect(wrap.getAttribute("aria-label")).toBe("Video: clip");
    expect(container.querySelector("video.vplayer-video")).not.toBeNull();
    expect(container.querySelector("source").getAttribute("src")).toBe("/v/clip.mp4");
    expect(byLabel("Play")).not.toBeNull();
    expect(byLabel("Mute")).not.toBeNull();
    expect(byLabel("Seek")).not.toBeNull();
    expect(byLabel("Volume")).not.toBeNull();
    expect(byLabel("Playback speed")).not.toBeNull();
    expect(byLabel("Fullscreen")).not.toBeNull();
  });

  it("falls back to a generic label when none is given", () => {
    mount({});
    expect(container.querySelector(".vplayer").getAttribute("aria-label")).toBe("Video player");
  });

  it("survives autoplay when play() resolves normally", () => {
    expect(() => mount({ autoPlay: true })).not.toThrow();
    expect(window.HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it("survives a non-playback environment where play() returns nothing", () => {
    window.HTMLMediaElement.prototype.play = vi.fn(() => undefined);
    expect(() => mount({ autoPlay: true })).not.toThrow();
  });

  it("survives a throwing play() implementation", () => {
    window.HTMLMediaElement.prototype.play = vi.fn(() => { throw new Error("denied"); });
    expect(() => mount({ autoPlay: true })).not.toThrow();
  });

  it("toggles playback from the bar play control and the keyboard", () => {
    mount({});
    act(() => {
      byLabel("Play").click();
    });
    expect(window.HTMLMediaElement.prototype.play).toHaveBeenCalled();

    act(() => {
      container.querySelector("video").dispatchEvent(
        new KeyboardEvent("keydown", { key: "k", bubbles: true }),
      );
    });
    expect(window.HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });

  it("toggles mute from its control and the keyboard shortcut", () => {
    mount({});
    act(() => {
      byLabel("Mute").click();
    });
    expect(byLabel("Unmute")).not.toBeNull();
    act(() => {
      byLabel("Unmute").click();
    });
    expect(byLabel("Mute")).not.toBeNull();

    act(() => {
      container.querySelector("video").dispatchEvent(
        new KeyboardEvent("keydown", { key: "m", bubbles: true }),
      );
    });
    expect(byLabel("Unmute")).not.toBeNull();
  });

  it("seeks with the arrow keys without crashing", () => {
    mount({});
    const video = container.querySelector("video");
    expect(() => {
      act(() => {
        video.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
        video.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
        video.dispatchEvent(new KeyboardEvent("keydown", { key: "f", bubbles: true }));
      });
    }).not.toThrow();
  });

  it("cycles the playback speed on each speed-control press", () => {
    mount({});
    const speed = byLabel("Playback speed");
    const first = speed.textContent;
    act(() => {
      speed.click();
    });
    expect(byLabel("Playback speed").textContent).not.toBe(first);
  });
});
