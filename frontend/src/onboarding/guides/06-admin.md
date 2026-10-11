# Admin setup

This guide is for admins and managers setting up a new workspace.

## 1. Create projects

In the sidebar, click **+** next to **Projects**. Give the project a name, a colour and a description. Tick **Enable Agile** if the team will use [Sprints](#help/sprints). You can also turn it on later from **Sprints**.

## 2. Add people

Click **+** next to **Team** and enter a name, an email, a role and a password. The password must meet the password policy. Under **Projects**, tag the projects this person can see. People only see the projects they belong to; admins see all of them. Untick **Active** to stop someone logging in without deleting their history.

## 3. Choose roles

See [Roles and permissions](#help/roles). Give people the least role that lets them work.

## 4. Check the basics

- **Audit Trail** lists every create, update, delete and login for incident review. It is open to managers and admins.
- **Sessions** (admins) lists devices that are signed in. Revoke one to log that device out without touching the others.
- Light and dark themes are in the account menu at the top right, along with **Change password**.

## 5. Optional features

These are switched on in the server's environment settings, not in the app:

- **Email**: choose the backend and, if you like, the daily digest.
- **Push**: needs a Firebase project and an https address. See [Notifications and push](#help/notifications).
- **Git branches**: per project, see [Git branches for stories](#help/git).
- **Sleuth** assistant: runs locally by default. A cloud model is optional.

## 6. Bring existing work in

Use **Download Template** and **Bulk Upload** on the Work Items page to import items from Excel or CSV. See [Creating and tracking items](#help/items).
