// Logo, accent colour and the From address of this organization's emails (admins).
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { parseHex } from "../lib/orgBranding";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

// The server accepts ~150 KB of data URL; a file of 100 KB encodes to about 134 KB.
const MAX_LOGO_BYTES = 100 * 1024;
const LOGO_TYPES = ["image/png", "image/jpeg", "image/svg+xml", "image/gif", "image/webp"];

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : "");
    reader.onerror = () => reject(new Error("Could not read that file"));
    reader.readAsDataURL(file);
  });
}

export default function BrandingTab() {
  const { refreshMe } = useApp();
  const [logo, setLogo] = useState("");
  const [accent, setAccent] = useState("");
  const [sender, setSender] = useState("");
  const [loaded, setLoaded] = useState(false);
  const fileInput = useRef(null);

  useEffect(() => {
    api("/branding")
      .then((b) => {
        setLogo(b.logo_data_url || "");
        setAccent(b.accent_color || "");
        setSender(b.email_from_override || "");
        setLoaded(true);
      })
      .catch(toastError);
  }, []);

  async function pickLogo(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!LOGO_TYPES.includes(file.type)) {
      toast("Use a PNG, JPEG, SVG, GIF or WebP image", "error");
      return;
    }
    if (file.size > MAX_LOGO_BYTES) {
      toast("The logo must be 100 KB or smaller", "error");
      return;
    }
    try {
      setLogo(await readAsDataUrl(file));
    } catch (err) {
      toastError(err);
    }
  }

  async function save(e) {
    e.preventDefault();
    if (accent && !parseHex(accent)) {
      toast("Enter the accent colour as a hex value such as #6366f1", "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api("/branding", {
          method: "PUT",
          json: { logo_data_url: logo, accent_color: accent, email_from_override: sender },
        });
        await refreshMe();
      }, "Saving…");
      toast("Branding updated", "success");
    } catch (err) {
      toastError(err);
    }
  }

  const swatch = parseHex(accent) ? accent : "#6366f1";

  return (
    <form className="ent-section" noValidate onSubmit={save} aria-busy={!loaded}>
      <h3>Branding</h3>
      <fieldset className="ent-fieldset" disabled={!loaded}>
      <p className="hint">Shown to everyone in this organization. Leave a field empty to use the default.</p>

      <div className="field">
        <span>Logo</span>
        <div className="ent-logo-row">
          {logo ? <img className="ent-logo-preview" src={logo} alt="Current logo" /> : <span className="hint">No logo</span>}
          <button type="button" className="btn" onClick={() => fileInput.current?.click()}>
            Choose image…
          </button>
          {logo && (
            <button type="button" className="btn ghost" onClick={() => setLogo("")}>
              Remove
            </button>
          )}
          <input ref={fileInput} type="file" accept={LOGO_TYPES.join(",")} hidden onChange={pickLogo}
            aria-label="Logo file" />
        </div>
      </div>

      <label className="field" htmlFor="brandAccent">
        <span>Accent colour</span>
        <span className="ent-color-row">
          <input type="color" aria-label="Pick accent colour" value={swatch}
            onChange={(e) => setAccent(e.target.value)} />
          <input id="brandAccent" value={accent} onChange={(e) => setAccent(e.target.value)}
            placeholder="#6366f1" maxLength={9} />
        </span>
      </label>

      <label className="field">
        <span>Email From address</span>
        <input type="text" value={sender} onChange={(e) => setSender(e.target.value)}
          placeholder="Acme Bugs <bugs@acme.com>" maxLength={254} />
      </label>
      <p className="hint">
        Used as the sender of notification emails for this organization. Your mail server must be
        allowed to send as this address.
      </p>

      <div className="ent-actions">
        <button type="submit" className="btn primary">
          Save
        </button>
      </div>
      </fieldset>
    </form>
  );
}
