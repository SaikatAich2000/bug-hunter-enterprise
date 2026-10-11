/** Skeleton placeholder: announced once, decorative bars hidden from assistive tech. */
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import Skeleton from "./Skeleton.jsx";

afterEach(cleanup);

describe("Skeleton", () => {
  it("is a busy status region with a single readable label", () => {
    render(<Skeleton label="Loading sprints" />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent("Loading sprints");
  });

  it("draws the requested number of bars, all hidden from screen readers", () => {
    const { container } = render(<Skeleton lines={5} />);
    const bars = container.querySelectorAll(".skeleton");
    expect(bars).toHaveLength(5);
    bars.forEach((b) => expect(b).toHaveAttribute("aria-hidden", "true"));
  });

  it("keeps the count within sensible bounds", () => {
    expect(render(<Skeleton lines={0} />).container.querySelectorAll(".skeleton")).toHaveLength(1);
    cleanup();
    expect(render(<Skeleton lines={99} />).container.querySelectorAll(".skeleton")).toHaveLength(12);
  });
});
