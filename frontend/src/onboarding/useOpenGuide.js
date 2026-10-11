/** Returns a function that opens the Help center, optionally on a given guide. */
import { useCallback } from "react";
import { useApp } from "../state/AppContext";
import { requestGuide } from "./onboardingBus";

export function useOpenGuide() {
  const { setView } = useApp();
  return useCallback((guideId) => {
    requestGuide(guideId ?? null);
    setView("help");
  }, [setView]);
}
