import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { QrCode, qrMatrix } from "./qr";

describe("qrMatrix", () => {
  it("returns a square matrix with the finder pattern in the top-left corner", () => {
    const m = qrMatrix("otpauth://totp/Test:a@b.co?secret=ABC&issuer=Test");
    expect(m.length).toBeGreaterThanOrEqual(21);
    expect(m.every((row) => row.length === m.length)).toBe(true);
    expect(m[0].slice(0, 7)).toEqual(Array(7).fill(true));
  });
});

describe("QrCode", () => {
  it("renders an accessible svg", () => {
    render(<QrCode text="hello" label="Scan me" />);
    expect(screen.getByRole("img", { name: "Scan me" })).toBeTruthy();
  });
});
