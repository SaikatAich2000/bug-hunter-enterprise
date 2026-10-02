// Entry point for /reset.html. CSP blocks inline scripts, so theme bootstrap happens here.
import { createRoot } from "react-dom/client";
import ResetPage from "./ResetPage";
import "../styles/styles.css";

// Apply saved theme before first paint.
document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

const container = document.getElementById("root");
if (!container) {
  throw new Error("reset: #root container not found");
}
// display:contents keeps .auth-body flex centering; CSSOM avoids the style-src CSP.
container.style.display = "contents";

createRoot(container).render(<ResetPage />);
