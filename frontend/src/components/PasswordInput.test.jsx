/** Behavioral tests for the real `PasswordInput` component (`./PasswordInput.jsx`).
 *
 * `name` must pass through unchanged (Playwright selectors and form submission
 * depend on it) and the toggle must switch masking without losing the value.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import PasswordInput from "./PasswordInput.jsx";

let container;
let root;

function mount(props) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(React.createElement(PasswordInput, props));
  });
  return container;
}

function input() {
  return container.querySelector("input");
}

function toggle() {
  return container.querySelector("button.pw-toggle");
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
});

describe("PasswordInput", () => {
  it("masks by default and forwards props to the input", () => {
    mount({ name: "password", id: "pw", placeholder: "Password" });
    expect(input().type).toBe("password");
    expect(input().name).toBe("password");
    expect(input().id).toBe("pw");
    expect(input().getAttribute("placeholder")).toBe("Password");
    expect(toggle().getAttribute("aria-label")).toBe("Show password");
  });

  it("reveals the value on toggle and masks it again", () => {
    mount({ name: "password", defaultValue: "s3cret" });
    act(() => {
      toggle().click();
    });
    expect(input().type).toBe("text");
    expect(input().value).toBe("s3cret");
    expect(toggle().getAttribute("aria-label")).toBe("Hide password");

    act(() => {
      toggle().click();
    });
    expect(input().type).toBe("password");
    expect(input().value).toBe("s3cret");
  });

  it("forwards a ref to the underlying input", () => {
    const ref = React.createRef();
    mount({ name: "password", ref });
    expect(ref.current).toBe(input());
    expect(ref.current.tagName).toBe("INPUT");
  });

  it("keeps the toggle out of form submission", () => {
    mount({ name: "password" });
    expect(toggle().type).toBe("button");
  });

  it("reports typing through onChange without altering the value", () => {
    const onChange = vi.fn();
    mount({ name: "password", value: "", onChange });
    const el = input();
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      ).set;
      setter.call(el, "abc");
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(onChange).toHaveBeenCalled();
  });
});
