// Shared bootstrap of the signed-out pages. CSP blocks inline scripts, so the saved theme is applied here.
import { createRoot } from "react-dom/client";
import "../styles/styles.css";
import "../styles/enterprise.css";

export default function mountPublicPage(Page) {
  document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";
  const container = document.getElementById("root");
  if (!container) throw new Error("page: #root container not found");
  // display:contents keeps .auth-body flex centering; CSSOM avoids the style-src CSP.
  container.style.display = "contents";
  createRoot(container).render(<Page />);
}
