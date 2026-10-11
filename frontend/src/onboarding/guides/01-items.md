# Creating and tracking items

Everything you track is an **item**. Open **Work Items** in the top bar to see them all in one table.

## Item types

- **Bug**, **Requirement** and **Task** are the classic types.
- **Epic**, **Story** and **Sub-task** are the agile types. They work with [Sprints](#help/sprints).
- The tab strip above the table filters the list, the KPI tiles and Analytics by type.

All types share one `#N` counter, so an item number is unique across the app.

## Create an item

1. Click **+ New Item** in the page header.
2. Give it a title and pick a **Project**. Both are required.
3. Set the **Status**, **Priority**, **Environment** (DEV, UAT or PROD, used by bugs), **Reporter** and one or more **Assignees**.
4. Optionally attach it to an **Event**, and for agile items an **Epic** or **Sprint**.
5. Save. The item appears in the list straight away.

Add comments, attachments and links to other items from the same window once it exists.

## Find and filter

- Type in the search box to match a title, a description or an `#id`.
- Click a **KPI tile** to filter by that group; click it again to clear.
- Use the filter bar for project, status, priority, environment and assignee. **Clear** resets everything.

## Update many items at once

Tick the checkboxes at the start of rows. A bulk bar appears with the number selected and lets you change status, priority or environment, or delete. Items you are not allowed to change are skipped by the server.

## Bring items in from a spreadsheet

Managers and admins see **Download Template** and **Bulk Upload** in the header. Download the Excel template, fill in one item per row, then upload the `.xlsx` or `.csv`. Every row is checked on its own, and you get a count of created and skipped rows with the reason for each skipped one.

## Group items into an event

**Events** collect items for a standup, a review or a release. Managers and admins create one with **+ New Event**, then add existing items or create new ones inside it. Open an event to see all its items together.

## Keep track of changes

Open an item to see its activity: who changed what and when. Notifications for items you are involved in arrive in the bell at the top right.
