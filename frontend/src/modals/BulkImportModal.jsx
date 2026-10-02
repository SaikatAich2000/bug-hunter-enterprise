/**
 * "Bulk Upload" modal: pick a .xlsx/.csv built from the downloaded template,
 * POST it to /bugs/import, then show created/skipped counts plus per-row
 * errors and warnings. Refreshes the list on any successful create.
 */
import { useRef, useState } from "react";
import Modal from "../components/Modal";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
export default function BulkImportModal() {
  const { bulkImportOpen, setBulkImportOpen, refreshAll } = useApp();
  const [file, setFile] = useState(null);
  const [result, setResult] = useState(null);
  const inputRef = useRef(null);
  const busyRef = useRef(false);

  const close = () => {
    setBulkImportOpen(false);
    setFile(null);
    setResult(null);
  };

  const onFileChange = (e) => {
    setResult(null);
    setFile(e.target.files?.[0] ?? null);
  };

  const upload = async ()=> {
    if (!file || busyRef.current) return;
    busyRef.current = true;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await withLoader(
        () => api("/bugs/import", { method: "POST", body: fd }),
        "Importing…",
      );
      setResult(res);
      if (res.created > 0) {
        toast(res.message, res.failed ? "info" : "success");
        await refreshAll();
        // Imported items enter the project backlog; let open Sprints views re-fetch.
        window.dispatchEvent(new Event("agile:refresh"));
      } else {
        toast(res.message, res.failed ? "error" : "info");
      }
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch (err) {
      toastError(err);
    } finally {
      busyRef.current = false;
    }
  };

  return (
    <Modal
      id="modalBulkImport"
      open={bulkImportOpen}
      title="Bulk upload"
      subtitle="Upload a filled-in copy of the template — one row per bug, requirement, or task."
      onClose={close}
    >
      <div className="modal-body">
        <label className="field">
          <span>Spreadsheet (.xlsx or .csv)</span>
          <input
            ref={inputRef}
            type="file"
            accept=".xlsx,.csv"
            onChange={onFileChange}
          />
        </label>

        {result && (
          <div className="bulk-import-result">
            <p>
              <strong>{result.created}</strong> created
              {result.failed > 0 && (
                <>
                  {" · "}
                  <strong>{result.failed}</strong> skipped
                </>
              )}
            </p>
            {result.errors.length > 0 && (
              <div className="bulk-import-issues">
                <h4>Skipped rows</h4>
                <ul>
                  {result.errors.map((e) => (
                    <li key={`err-${e.row}`}>
                      Row {e.row}{e.title ? ` (“${e.title}”)` : ""}: {e.error}
                    </li>
                  ))}
                </ul>
                {result.errors_truncated && <p className="muted small">More rows were skipped than shown here.</p>}
              </div>
            )}
            {result.warnings.length > 0 && (
              <div className="bulk-import-issues">
                <h4>Created with a warning</h4>
                <ul>
                  {result.warnings.map((w) => (
                    <li key={`warn-${w.row}`}>
                      Row {w.row}{w.title ? ` (“${w.title}”)` : ""}: {w.warning}
                    </li>
                  ))}
                </ul>
                {result.warnings_truncated && <p className="muted small">More warnings were reported than shown here.</p>}
              </div>
            )}
          </div>
        )}

        <div className="modal-foot">
          <button type="button" className="btn ghost" data-close-modal onClick={close}>
            Close
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!file}
            onClick={() => { void upload(); }}
          >
            Upload
          </button>
        </div>
      </div>
    </Modal>
  );
}
