/** Owns onboarding state for the signed-in person: loads their saved preferences, shows the welcome
 * screen on first login (worded for their role), runs the resumable tour and, for an organization's
 * first admin, the resumable setup wizard. Everything is stored on the server
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
import { onboardingRole } from "./roles";
import SetupWizard from "./SetupWizard";
import { useOpenGuide } from "./useOpenGuide";

export default function OnboardingHost() {
  const { currentUser, roleRank, setView, projects = [], setAccountOpen } = useApp();
  const openGuide = useOpenGuide();
  const [prefs, setPrefs] = useState(null);
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const prefsRef = useRef(null);
  const role = onboardingRole(currentUser, projects);
  const isAdminUser = currentUser.role === "admin";

  const steps = useMemo(
    () => stepsFor((view) => roleRank(currentUser.role) >= roleRank(VIEW_MIN_ROLE[view] ?? "user"), role),
    [currentUser.role, roleRank, role],
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
        // An admin who left the wizard halfway finds it where they stopped.
        else if (p.setup_status === "running" && isAdminUser) setSetupOpen(true);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [isAdminUser]);

  // One request: two concurrent first writes for the same user would race on creating the row.
  const startTour = useCallback((step = 0, extra = {}) => {
    setWelcomeOpen(false);
    setView("list");
    void save({ ...extra, tour_status: "running", tour_step: step });
  }, [save, setView]);

  const openSetup = useCallback((extra = {}) => {
    setWelcomeOpen(false);
    const p = prefsRef.current;
    const resume = p?.setup_status === "running";
    setSetupOpen(true);
    void save({ ...extra, setup_status: "running", ...(resume ? {} : { setup_step: 0 }) });
  }, [save]);

  useEffect(() => onOnboarding(({ action }) => {
    if (action === "welcome") setWelcomeOpen(true);
    else if (action === "tour") startTour(0);
    else if (action === "setup") openSetup();
    else if (action === "tour-resume") {
      const p = prefsRef.current;
      const paused = p?.tour_status === "dismissed" || p?.tour_status === "running";
      startTour(paused ? p.tour_step : 0);
    }
  }), [startTour, openSetup]);

  const seen = (dontShow) => ({ welcome_seen: true, ...(dontShow ? { welcome_hidden: true } : {}) });

  const closeSetup = (patch) => {
    setSetupOpen(false);
    if (patch) void save(patch);
  };
  const leaveSetupFor = (go) => {
    setSetupOpen(false);
    go();
  };

  const index = Math.min(prefs?.tour_step ?? 0, Math.max(steps.length - 1, 0));
  const touring = prefs?.tour_status === "running" && !welcomeOpen && steps.length > 0;

  return (
    <>
      <WelcomeModal
        open={welcomeOpen}
        name={currentUser.name}
        role={role}
        onTour={(dontShow) => startTour(0, seen(dontShow))}
        onSetup={role === "admin" ? (dontShow) => openSetup(seen(dontShow)) : undefined}
        onHelp={(dontShow) => { setWelcomeOpen(false); void save(seen(dontShow)); openGuide(null); }}
        onLater={(dontShow) => {
          setWelcomeOpen(false);
          if (dontShow) void save(seen(true));
        }}
      />
      {role === "admin" && (
        <SetupWizard
          open={setupOpen && !welcomeOpen}
          step={prefs?.setup_step ?? 0}
          onStep={(i) => void save({ setup_step: i, setup_status: "running" })}
          onClose={() => setSetupOpen(false)}
          onSkip={() => closeSetup({ setup_status: "skipped", setup_step: 0 })}
          onFinish={() => closeSetup({ setup_status: "done", setup_step: 0 })}
          onTour={() => { closeSetup({ setup_status: "done", setup_step: 0 }); startTour(0); }}
          onOpenAccount={() => leaveSetupFor(() => setAccountOpen?.(true))}
          onOpenWebhooks={() => leaveSetupFor(() => setView("organization"))}
          onOpenGuide={(id) => leaveSetupFor(() => openGuide(id))}
        />
      )}
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
