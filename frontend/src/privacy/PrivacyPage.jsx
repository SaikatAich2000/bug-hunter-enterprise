// Public privacy notice. It states what this software stores and what the operator
// configured (audit retention, contact address), both read from /api/meta.
import { useEffect, useState } from "react";
import { getAppName } from "../lib/branding";

export default function PrivacyPage() {
  const appName = getAppName();
  const [meta, setMeta] = useState(null);

  useEffect(() => {
    fetch("/api/meta")
      .then((r) => (r.ok ? r.json() : null))
      .then(setMeta)
      .catch(() => {
        /* the static text below is still correct */
      });
  }, []);

  const retention = meta?.audit_retention_days;
  const contact = meta?.privacy_contact_email;

  return (
    <main className="policy-shell">
      <h1>Privacy policy</h1>
      <p className="policy-meta">{appName}</p>

      <p>
        {appName} is a project and bug tracker. This page explains what it stores about you, why,
        who can see it and how to have it deleted. The organization that runs this installation
        (the operator) decides where it is hosted and who its users are.
      </p>

      <h2>What is stored</h2>
      <ul>
        <li>
          <strong>Account</strong>: your name, email address and a one-way hash of your password.
          With two-factor sign-in on, the authenticator secret and hashed one-time recovery codes.
        </li>
        <li>
          <strong>Content you create</strong>: projects, work items, comments, attachments, custom
          field values and saved views.
        </li>
        <li>
          <strong>Sessions</strong>: the IP address and browser or device name of each sign-in, so
          you and your admins can see and revoke devices.
        </li>
        <li>
          <strong>Audit log</strong>: what was done, by whom and when, so administrators of your
          organization can review activity.
          {retention > 0 && <> Audit entries are deleted after {retention} days.</>}
          {retention === 0 && <> Audit entries are kept for as long as the installation exists.</>}
        </li>
        <li>
          <strong>Push notification token</strong>: if you allow notifications in the browser or
          mobile app, a Firebase Cloud Messaging token; it is removed when you sign out.
        </li>
      </ul>

      <h2>Why</h2>
      <p>
        To run the application, to send the notifications you opted in to, and to keep it secure
        (rate limiting, session integrity, audit trail). Data is not sold and is not used for
        advertising or to train models.
      </p>

      <h2>Who can see it</h2>
      <p>
        Only members of your own organization, according to the roles and project memberships its
        admins set. Another organization on the same installation cannot see your data. Firebase
        Cloud Messaging (Google) receives the notification text and device token when push is
        enabled; the operator hosts everything else.
      </p>

      <h2>Your rights</h2>
      <ul>
        <li>
          <strong>Export</strong>: Account settings, Privacy, <em>Download my data</em>, gives you a
          JSON copy of your profile, items, comments, attachment metadata, sessions and audit
          entries.
        </li>
        <li>
          <strong>Delete</strong>: Account settings, Privacy, <em>Delete my account</em>, or{" "}
          <a href="/delete-account">the web page for it</a>. Deletion is immediate and permanent.
          Items you reported keep their content with your name removed. The last admin of an
          organization must promote another admin first.
        </li>
        <li>
          <strong>Correct</strong>: change your name and email in Account settings.
        </li>
      </ul>

      <h2>Security</h2>
      <p>
        Passwords are hashed with bcrypt, sessions use signed HttpOnly cookies, requests from other
        sites are rejected, and sign-in is rate limited with optional two-factor authentication.
      </p>

      <h2>Children</h2>
      <p>This software is meant for adults in a professional setting.</p>

      <h2>Contact</h2>
      <p>
        {contact ? (
          <>
            Privacy questions: <a href={`mailto:${contact}`}>{contact}</a>.
          </>
        ) : (
          <>Ask the administrator of your organization.</>
        )}
      </p>

      <p className="policy-back">
        <a href="/">&larr; Back to {appName}</a>
      </p>
    </main>
  );
}
