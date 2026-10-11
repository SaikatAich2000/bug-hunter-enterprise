# Security and privacy

How to protect your sign-in and what you can do with your data. Account settings are in the account menu at the top right, under **Account settings**.

## Two-factor authentication

Account settings, **Security**. Two-factor authentication (2FA) asks for a code from an authenticator app in addition to your password.

1. Click **Enable 2FA** and scan the QR code with an authenticator app. If you cannot scan, type the key shown under the code.
2. Enter the 6-digit code the app shows and click **Turn on**.
3. Save the **recovery codes** that appear. Each works once if you lose the app, and they are not shown again.

To turn 2FA off, or to get new recovery codes (the old ones stop working), enter your password first. If the server has 2FA switched off, the tab says so.

## Sessions

Every sign-in is a session on one device. Changing your password signs out every other device. Admins see all active sessions under **Sessions** in the top bar, with user, browser, address and last activity, and can **Revoke** one to sign that device out without touching the others. The session you are using cannot be revoked there; log out instead.

## Changing your email address

Account settings, **Profile**, **Email address**. Enter the new address and your current password, then click **Send code**. A 6-digit code goes to the new address; enter it and click **Confirm**. Until then nothing changes. The old address is told about the change, so an unexpected change gets noticed. An email address can belong to only one account on the server, whatever its capital letters.

## Downloading your data

Account settings, **Privacy**, **Download my data**. You get a JSON file with your profile, the items you reported or are assigned to, your comments, attachment details, sessions and audit entries, saved views, chat, notifications, devices, notification settings, invitations you sent, email-change requests and your custom-field values.

## Deleting your account

Account settings, **Privacy**, **Delete my account**: enter your password and confirm. It is permanent. Items you reported stay, without your name. The last admin of an organization cannot delete their account: promote another admin first, or delete the organization.

If you cannot sign in to the app, the public page `/delete-account.html` signs you in with your password (and 2FA code) and deletes the account.

## Deleting an organization

Admins only. Organization, **General**, **Delete organization**. This removes the organization and everything in it for good: people, projects, items, comments, attachments, webhooks, invitations, saved views and the audit trail.

1. Click **Show what will be deleted**. This is a dry run: it lists the counts and changes nothing.
2. Enter your **password** and type the organization's **exact name**.
3. Click **Delete organization forever**. You are signed out.

The deletion is written to the server's audit log. There is no undo, so download what you need first (see [Reports](#help/reports) and Download my data above).
