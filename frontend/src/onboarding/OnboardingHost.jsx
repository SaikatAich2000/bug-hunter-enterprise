/** Owns onboarding state for the signed-in person: loads their saved preferences, shows the welcome
 * screen on first login and runs the resumable tour. Everything is stored on the server
 * (/api/me/preferences) so it follows them across browsers; a failed load simply shows nothing. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { toast } from "../lib/toast";
import { VIEW_MIN_ROLE } from "../types";
import Tour from "./Tour";
import WelcomeModal from "./WelcomeModal";
import { onOnboarding } from "./onboardingBus";
import { stepsFor } from "./tourSteps";
import { useOpenGuide } from "./useOpenGuide";

export default function OnboardingHost() {
  const { currentUser, roleRank, setView } = useApp();
  const openGuide = useOpenGuide();
  const [prefs, setPrefs] = useState(null);
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const prefsRef = useRef(null);

  const steps = useMemo(
    () => stepsFor((view) => roleRank(currentUser.role) >= roleRank(VIEW_MIN_ROLE[view] ?? "user")),
    [currentUser.role, roleRank],
  );

  const save = useCallback((patch) => {
    setPrefs((p) => {
      const next = { ...(p ?? {}), ...patch };
      prefsRef.current = next;
      return next;
    });
    // Best effort: the on-screen state already changed, and a failed save only means it is asked again.
    return api("/me/preferences", { method: "PUT", json: patch }).catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    api("/me/preferences")
      .then((p) => {
        if (cancelled || !p) return;
        prefsRef.current = p;
        setPrefs(p);
        if (!p.welcome_seen && !p.welcome_hidden) setWelcomeOpen(true);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // One request: two concurrent first writes for the same user would race on creating the row.
  const startTour = useCallback((step = 0, extra = {}) => {
    setWelcomeOpen(false);
    setView("list");
    void save({ ...extra, tour_status: "running", tour_step: step });
  }, [save, setView]);

  useEffect(() => onOnboarding(({ action }) => {
    if (action === "welcome") setWelcomeOpen(true);
    else if (action === "tour") startTour(0);
    else if (action === "tour-resume") {
      const p = prefsRef.current;
      const paused = p?.tour_status === "dismissed" || p?.tour_status === "running";
      startTour(paused ? p.tour_step : 0);
    }
  }), [startTour]);

  const seen = (dontShow) => ({ welcome_seen: true, ...(dontShow ? { welcome_hidden: true } : {}) });

  const index = Math.min(prefs?.tour_step ?? 0, Math.max(steps.length - 1, 0));
  const touring = prefs?.tour_status === "running" && !welcomeOpen && steps.length > 0;

  return (
    <>
      <WelcomeModal
        open={welcomeOpen}
        name={currentUser.name}
        onTour={(dontShow) => startTour(0, seen(dontShow))}
        onHelp={(dontShow) => { setWelcomeOpen(false); void save(seen(dontShow)); openGuide(null); }}
        onLater={(dontShow) => {
          setWelcomeOpen(false);
          if (dontShow) void save(seen(true));
        }}
      />
      {touring && (
        <Tour
          steps={steps}
          index={index}
          onIndex={(i) => void save({ tour_step: i })}
          onPause={() => {
            void save({ tour_status: "dismissed", tour_step: index });
            toast("Tour paused. Resume it from the account menu.");
          }}
          onFinish={() => void save({ tour_status: "done", tour_step: 0 })}
        />
      )}
    </>
  );
}
