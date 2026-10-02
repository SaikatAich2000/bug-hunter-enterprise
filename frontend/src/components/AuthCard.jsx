// Card shell of the signed-out pages: brand, heading, optional inline alert.
import { getAppName, getStaticAssetUrl } from "../lib/branding";

export default function AuthCard({ title, tagline, alert, children }) {
  const appName = getAppName();
  return (
    <main className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-logo">
            <img src={getStaticAssetUrl("icon.png")} alt={appName} />
          </div>
          <h1>{title}</h1>
          {tagline && <p className="auth-tagline">{tagline}</p>}
        </div>
        <div className={alert ? `auth-alert ${alert.kind}` : "auth-alert"} hidden={!alert} role="alert">
          {alert?.msg}
        </div>
        {children}
      </div>
    </main>
  );
}
