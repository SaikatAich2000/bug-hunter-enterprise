// Entry point for /login.html. CSP blocks inline scripts, so theme bootstrap happens here.
import { createRoot } from "react-dom/client";
import LoginPage from "./LoginPage";
import "../styles/styles.css";

// Apply saved theme before render to avoid a flash on load.
document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

const container = document.getElementById("root");
if (!container) {
  throw new Error("login: #root container not found");
}
// display:contents keeps .auth-body flex centering; CSSOM avoids the style-src CSP.
container.style.display = "contents";

createRoot(container).render(<LoginPage />);
