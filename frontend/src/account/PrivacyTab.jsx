// Data-subject rights: download everything stored about me, delete my account.
import { useState } from "react";
import PasswordInput from "../components/PasswordInput";
import { confirmDialog } from "../components/ConfirmHost";
import { api, apiBlob } from "../lib/api";
import { saveBlob } from "../lib/download";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";

async function download() {
  try {
    const { blob } = await withLoader(() => apiBlob("/auth/data-export"), "Preparing your data…");
    saveBlob(blob, "my-data.json");
    toast("Download started", "success");
  } catch (err) {
    toastError(err);
  }
}

export default function PrivacyTab() {
  const [password, setPassword] = useState("");

  async function deleteAccount(e) {
    e.preventDefault();
    const ok = await confirmDialog(
      "Delete your account permanently? This cannot be undone. Items you reported stay, without your name.",
      { title: "Delete account", okLabel: "Delete my account" },
    );
    if (!ok) return;
    try {
      await withLoader(() => api("/auth/account", { method: "DELETE", json: { password } }), "Deleting…");
      location.replace("/login.html");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <>
      <div className="ent-section">
        <h3>Download my data</h3>
        <p className="hint">
          A JSON file with your profile, the items you reported or are assigned to, your comments,
          attachment details, sessions and audit entries.
        </p>
        <div className="ent-actions">
          <button type="button" className="btn" onClick={() => void download()}>
            Download my data
          </button>
        </div>
      </div>

      <form className="ent-section" noValidate onSubmit={deleteAccount}>
        <h3>Delete my account</h3>
        <p className="hint">
          Permanent. The last admin of an organization must promote another admin first.{" "}
          <a href="/privacy.html" target="_blank" rel="noreferrer">
            Privacy policy
          </a>
        </p>
        <label className="field" htmlFor="deleteAccountPassword">
          <span>
            Your password <em>*</em>
          </span>
          <PasswordInput id="deleteAccountPassword" value={password}
            onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>
        <div className="ent-actions">
          <button type="submit" className="btn danger" disabled={!password}>
            Delete my account
          </button>
        </div>
      </form>
    </>
  );
}
