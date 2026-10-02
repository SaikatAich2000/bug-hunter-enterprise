// App root — global chrome (loader, toast, confirm, Sleuth) + shell + Escape handler.
import { lazy, Suspense, useEffect } from "react";
import Shell from "./shell/Shell";
import ConfirmHost from "./components/ConfirmHost";
// Sleuth is shown only behind the chat FAB, so keep it out of the main bundle.
const SleuthPanel = lazy(() => import("./sleuth/SleuthPanel"));

export default function App() {
  // Close the topmost open modal on Escape, but not when focus is in a field.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      const t = e.target;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      ) {
        return;
      }
      const open = document.querySelectorAll(".modal:not([hidden])");
      const top = open[open.length - 1];
      if (!top) return;
      const close = top.querySelector(".modal-close");
      close?.click();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      {/* Blocking loader — shown imperatively during async ops */}
      <div
        id="globalLoader"
        className="global-loader"
        hidden
        aria-hidden="true"
        role="alert"
      >
        <div className="global-loader-card">
          <div className="global-loader-spinner" aria-hidden="true"></div>
          <div className="global-loader-text" id="globalLoaderText">
            Working…
          </div>
        </div>
      </div>

      <Shell />

      {/* Toast — one instance, controlled imperatively */}
      <div id="toast" className="toast" hidden></div>

      <ConfirmHost />
      <Suspense fallback={null}>
        <SleuthPanel />
      </Suspense>
    </>
  );
}
