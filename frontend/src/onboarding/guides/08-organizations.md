# Organizations

Everything in your workspace belongs to one organization: its people, projects, items, webhooks and settings. Nothing is shared with other organizations. Open **Organization** in the top bar. Admins see four tabs (General, Branding, Invitations, Webhooks); managers see only Invitations.

## General

Admins only. Change the organization's **name** (at least two characters) and its **description**, then click **Save**. The name appears in the top bar and in the account menu, and every change is recorded in the audit trail. At the bottom of the tab, **Delete organization** removes the whole organization. See [Security and privacy](#help/security).

## Branding

Admins only. Branding is shown to everyone in the organization.

- **Logo**: a PNG, JPEG, SVG, GIF or WebP image of 100 KB or less.
- **Accent colour**: a hex value such as `#6366f1`, or pick it with the colour box. Leave it empty for the default.
- **Email From address**: the sender of notification emails, for example `Acme Bugs <bugs@acme.com>`. Your mail server must be allowed to send as that address.

Leave a field empty to go back to the default. The first-time **Organization setup** (account menu) asks for the same details.

## Invitations

Admins and managers invite people by email. An invitation is a link that works for 7 days and can be used once.

1. Enter the **email**, choose a **role** (only admins can invite another admin) and, optionally, the **projects** the person joins. Managers can only pick projects they lead. Tick **Join as project lead** to make them a lead of those projects.
2. Click **Send invitation**. The list shows each invitation as pending, accepted, expired or revoked.
3. **Revoke** a pending invitation and its link stops working.

To protect the organization, invitations are limited: a burst limit per minute and a daily ceiling. When you reach one you see a message and can try again later. Someone who is already a member cannot be invited again.

## Members and project leads

Each project has members, and each member is either a **lead** or a **member**. Open a project from the sidebar (click its name) to see **Members**. Admins and the project's leads can add people, change a role and remove people. Everyone else sees the list read-only.

**A project always keeps at least one lead.** If you try to demote or remove the last lead, the server refuses with "Cannot demote the last project lead. Promote another member first." Promote someone else to lead, then change the first person.

People only see the projects they belong to; admins see all of them. See [Roles and permissions](#help/roles).

## Webhooks

Admins only. A webhook sends a signed `POST` to your URL when something happens.

- **Create**: a name, a URL and the events. Use `*` for everything, or names such as `bug.created`, `bug.*` or `comment.added`.
- **Secret**: shown once when the webhook is created or rotated. Copy it then. Check each delivery with the `X-BugHunter-Signature` header (HMAC-SHA256 of the body).
- **Addresses**: the URL must be reachable on the public internet. Private, loopback, link-local and cloud-metadata addresses are refused, and redirects are not followed.
- **Test** queues a ping so you can check your listener. **Pause** and **Resume** stop and restart deliveries. **Rotate secret** issues a new one (listeners with the old secret then fail verification). **Delete** removes it.
- After ten failed deliveries in a row a webhook is suspended; fix the listener and click **Resume**. The list shows the last status and error.

## Custom fields

Admins and a project's leads define extra fields for the project's items. Open the project (click its name in the sidebar) and use **Custom fields**.

- Field types are **text**, **number**, **date** and **select** (a list of options).
- Mark a field **required** and the server refuses to save an item without it, including bulk imports.
- Everyone who can edit an item fills the values in on the item form, under **Custom fields**. People who cannot edit an item (for example a plain user on a Task) see its values but cannot change them. Changes are audited and fire the same webhook events as other edits.

## Saved views

A saved view stores the current filters of the Work Items list. In the filter bar, click **Save view**, name it and save; pick it later from **Saved views** to apply it. You can delete your own views. Admins and managers can tick **Share with everyone** so the whole organization sees the view; only its creator can change or delete it.
