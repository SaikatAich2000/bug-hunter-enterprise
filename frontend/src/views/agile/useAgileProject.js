/** Loads a project's agile settings and board configuration, and reloads
 * them when agile data changes anywhere in the app. */
import { useCallback, useEffect, useRef, useState } from "react";
import { toastError } from "../../lib/toast";
import { agileApi, onAgileChanged } from "./agileApi";

export function useAgileProject(projectId) {
  const [state, setState] = useState({ loading: true, settings: null, board: null });
  const requestRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!projectId) {
      setState({ loading: false, settings: null, board: null });
      return;
    }
    const request = ++requestRef.current;
    try {
      const settings = await agileApi.settings(projectId);
      const board = settings.agile_enabled && settings.board_id ? await agileApi.board(settings.board_id) : null;
      // A newer request (project switch) wins; an older answer is dropped.
      if (request === requestRef.current) setState({ loading: false, settings, board });
    } catch (err) {
      if (request === requestRef.current) setState((prev) => ({ ...prev, loading: false }));
      toastError(err);
    }
  }, [projectId]);

  useEffect(() => {
    setState({ loading: true, settings: null, board: null });
    void refresh();
  }, [refresh]);

  useEffect(() => onAgileChanged(() => { void refresh(); }), [refresh]);

  return { ...state, refresh };
}

/**
 * Runs ``load`` whenever its dependencies change, keeping only the latest
 * answer (a slow response for a previous sprint/project never overwrites a
 * newer one). Also reloads on "agile:refresh".
 */
export function useLatestLoad(load, deps) {
  const key = JSON.stringify(deps);
  // Data is kept with the inputs it was loaded for and only handed out while
  // they are still the current inputs, so a view never renders one frame of
  // another project's, sprint's or report's data.
  const [state, setState] = useState({ key: null, value: null });
  const [loading, setLoading] = useState(false);
  const counter = useRef(0);
  const loadRef = useRef(load);
  const keyRef = useRef(key);
  useEffect(() => {
    loadRef.current = load;
    keyRef.current = key;
  });
  const run = useCallback(async () => {
    const request = ++counter.current;
    const forKey = keyRef.current;
    setLoading(true);
    try {
      const result = await loadRef.current();
      if (request === counter.current) setState({ key: forKey, value: result });
    } catch (err) {
      // A failed refresh keeps what is on screen; the error is reported.
      toastError(err);
    } finally {
      if (request === counter.current) setLoading(false);
    }
  }, []);
  useEffect(() => { void run(); }, [run, key]);
  useEffect(() => onAgileChanged(() => { void run(); }), [run]);
  const setData = useCallback((value) => setState((prev) => ({
    key: prev.key, value: typeof value === "function" ? value(prev.value) : value,
  })), []);
  return { data: state.key === key ? state.value : null, loading, reload: run, setData };
}
