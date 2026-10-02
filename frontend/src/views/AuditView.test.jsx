/** Audit view: the CSV export carries the list's filters. */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.fn();
const apiBlobMock = vi.fn();
const toastErrorMock = vi.fn();

vi.mock("../lib/api", () => ({ api: (...a) => apiMock(...a), apiBlob: (...a) => apiBlobMock(...a) }));
vi.mock("../lib/toast", () => ({ toastError: (...a) => toastErrorMock(...a), toast: vi.fn() }));
vi.mock("../state/AppContext", () => ({
  DATA_POLL_MS: 60_000,
  useApp: () => ({ users: [{ id: 5, name: "Ada" }] }),
}));

import AuditView from "./AuditView";

let saved;

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockResolvedValue([]);
  apiBlobMock.mockReset();
  toastErrorMock.mockClear();
  URL.createObjectURL = vi.fn(() => "blob:audit");
  URL.revokeObjectURL = vi.fn();
  saved = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  saved.mockRestore();
});

describe("AuditView export", () => {
  it("downloads the CSV for the chosen filters", async () => {
    apiBlobMock.mockResolvedValue({ blob: new Blob(["id\n"]), filename: "audit.csv" });
    render(<AuditView />);
    await userEvent.selectOptions(screen.getByLabelText("Filter by entity"), "bug");
    await userEvent.selectOptions(screen.getByLabelText("Filter by actor"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() =>
      expect(apiBlobMock).toHaveBeenCalledWith("/audit/export.csv?entity_type=bug&actor_user_id=5"),
    );
    await waitFor(() => expect(saved).toHaveBeenCalled());
  });

  it("reports a failed export", async () => {
    apiBlobMock.mockRejectedValue(new Error("nope"));
    render(<AuditView />);
    await userEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(toastErrorMock).toHaveBeenCalled());
  });
});
