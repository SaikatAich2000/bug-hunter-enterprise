/** Modal exit animation: it waits for the animation, and not at all under reduced motion. */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Modal from "./Modal.jsx";
import { MOTION_MS } from "../lib/motion.js";

const original = window.matchMedia;

function mockMedia(reduce) {
  window.matchMedia = vi.fn((query) => ({ matches: reduce && query.includes("reduce"), media: query }));
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.matchMedia = original;
});

const modal = () => document.querySelector(".modal");

describe("Modal motion", () => {
  it("plays the exit animation, then hides", () => {
    mockMedia(false);
    const { rerender } = render(<Modal id="m" open title="T" onClose={() => {}}>x</Modal>);
    rerender(<Modal id="m" open={false} title="T" onClose={() => {}}>x</Modal>);
    expect(modal().hidden).toBe(false);
    expect(modal()).toHaveAttribute("data-closing");
    act(() => vi.advanceTimersByTime(MOTION_MS + 5));
    expect(modal().hidden).toBe(true);
    expect(modal()).not.toHaveAttribute("data-closing");
  });

  it("closes immediately when the system asks for reduced motion", () => {
    mockMedia(true);
    const { rerender } = render(<Modal id="m" open title="T" onClose={() => {}}>x</Modal>);
    rerender(<Modal id="m" open={false} title="T" onClose={() => {}}>x</Modal>);
    expect(modal().hidden).toBe(true);
    expect(modal()).not.toHaveAttribute("data-closing");
  });

  it("reopening during the exit cancels it", () => {
    mockMedia(false);
    const { rerender } = render(<Modal id="m" open title="T" onClose={() => {}}>x</Modal>);
    rerender(<Modal id="m" open={false} title="T" onClose={() => {}}>x</Modal>);
    rerender(<Modal id="m" open title="T" onClose={() => {}}>x</Modal>);
    act(() => vi.advanceTimersByTime(MOTION_MS + 5));
    expect(modal().hidden).toBe(false);
    expect(modal()).not.toHaveAttribute("data-closing");
  });
});
