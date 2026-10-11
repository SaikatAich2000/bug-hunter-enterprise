import{n as e}from"./rolldown-runtime-CbXtAM7H.js";import{v as t,x as n}from"./vendor-CFDLG235.js";import{i as r}from"./rich-editor-sbPULaiK.js";import{S as i,b as a,x as o}from"./index-DQXYs5Z-.js";var s=e(n(),1),c={"&":`&amp;`,"<":`&lt;`,">":`&gt;`,'"':`&quot;`,"'":`&#39;`},l=e=>e.replace(/[&<>"']/g,e=>c[e]);function u(e){return e.toLowerCase().replace(/[^a-z0-9]+/g,`-`).replace(/^-+|-+$/g,``)}function d(e){let t=e.trim();return/^(https?:\/\/|mailto:|#)/i.test(t)?t:``}function f(e){let t=[],n=e.replace(/`([^`]+)`/g,(e,n)=>(t.push(n),`\uE000${t.length-1}\uE000`));return n=n.replace(/\*\*([^*]+)\*\*/g,`<strong>$1</strong>`).replace(/(^|[^*])\*([^*\s][^*]*)\*/g,`$1<em>$2</em>`).replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(e,t,n)=>{let r=d(n.replace(/&amp;/g,`&`));if(!r)return t;let i=/^https?:/i.test(r);return`<a href="${l(r)}"${i?` target="_blank" rel="noopener noreferrer"`:``}>${t}</a>`}),n.replace(/\uE000(\d+)\uE000/g,(e,n)=>`<code>${t[Number(n)]}</code>`)}var p=e=>String(e??``).replace(/\uE000/g,``).replace(/\r\n?/g,`
`);function m(e){return e.trim().replace(/^\|/,``).replace(/\|$/,``).split(`|`).map(e=>e.trim())}var h=e=>/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(e)&&e.includes(`-`),g=e=>/^(#{1,4}\s|>\s?|\s*[-*]\s|\s*\d+\.\s|```|---+\s*$)/.test(e);function _(e){let t=p(e).split(`
`),n=[],r=0;for(;r<t.length;){let e=t[r];if(!e.trim()){r++;continue}if(e.startsWith("```")){let e=[];for(r++;r<t.length&&!t[r].startsWith("```");)e.push(t[r++]);r++,n.push(`<pre><code>${l(e.join(`
`))}</code></pre>`);continue}let i=/^(#{1,4})\s+(.*)$/.exec(e);if(i){let e=i[1].length;n.push(`<h${e} id="g-${u(i[2])}">${f(l(i[2]))}</h${e}>`),r++;continue}if(/^---+\s*$/.test(e)){n.push(`<hr>`),r++;continue}if(e.startsWith(`>`)){let e=[];for(;r<t.length&&t[r].startsWith(`>`);)e.push(t[r++].replace(/^>\s?/,``));n.push(`<blockquote>${f(l(e.join(` `)))}</blockquote>`);continue}if(e.includes(`|`)&&r+1<t.length&&h(t[r+1])){let i=m(e);r+=2;let a=[];for(;r<t.length&&t[r].includes(`|`)&&t[r].trim();)a.push(m(t[r++]));let o=(e,t)=>`<${e}>${f(l(t))}</${e}>`;n.push(`<table><thead><tr>${i.map(e=>o(`th`,e)).join(``)}</tr></thead><tbody>`+a.map(e=>`<tr>${e.map(e=>o(`td`,e)).join(``)}</tr>`).join(``)+`</tbody></table>`);continue}let a=/^\s*([-*]|\d+\.)\s+/.exec(e);if(a){let e=/\d/.test(a[1]),i=[];for(;r<t.length&&/^\s*([-*]|\d+\.)\s+/.test(t[r]);){let e=t[r++].replace(/^\s*([-*]|\d+\.)\s+/,``);for(;r<t.length&&t[r].trim()&&/^\s{2,}\S/.test(t[r])&&!/^\s*([-*]|\d+\.)\s+/.test(t[r]);)e+=` ${t[r++].trim()}`;i.push(`<li>${f(l(e))}</li>`)}n.push(`<${e?`ol`:`ul`}>${i.join(``)}</${e?`ol`:`ul`}>`);continue}let o=[e.trim()];for(r++;r<t.length&&t[r].trim()&&!g(t[r]);)o.push(t[r++].trim());n.push(`<p>${f(l(o.join(` `)))}</p>`)}return v(n.join(`
`))}function v(e){return r.sanitize(e,{ALLOWED_TAGS:[`h1`,`h2`,`h3`,`h4`,`p`,`ul`,`ol`,`li`,`strong`,`em`,`code`,`pre`,`a`,`blockquote`,`hr`,`table`,`thead`,`tbody`,`tr`,`th`,`td`,`br`],ALLOWED_ATTR:[`href`,`rel`,`id`],ADD_ATTR:[`target`],ALLOW_DATA_ATTR:!1})}function y(e){let t=[],n=!1;for(let r of p(e).split(`
`)){r.startsWith("```")&&(n=!n);let e=!n&&/^(#{2,3})\s+(.*)$/.exec(r);e&&t.push({level:e[1].length,text:e[2],id:`g-${u(e[2])}`})}return t}var b=Object.assign({"./guides/01-items.md":`# Creating and tracking items\r
\r
Everything you track is an **item**. Open **Work Items** in the top bar to see them all in one table.\r
\r
## Item types\r
\r
- **Bug**, **Requirement** and **Task** are the classic types.\r
- **Epic**, **Story** and **Sub-task** are the agile types. They work with [Sprints](#help/sprints).\r
- The tab strip above the table filters the list, the KPI tiles and Analytics by type.\r
\r
All types share one \`#N\` counter, so an item number is unique across the app.\r
\r
## Create an item\r
\r
1. Click **+ New Item** in the page header.\r
2. Give it a title and pick a **Project**. Both are required.\r
3. Set the **Status**, **Priority**, **Environment** (DEV, UAT or PROD, used by bugs), **Reporter** and one or more **Assignees**.\r
4. Optionally attach it to an **Event**, and for agile items an **Epic** or **Sprint**.\r
5. Save. The item appears in the list straight away.\r
\r
Add comments, attachments and links to other items from the same window once it exists.\r
\r
## Find and filter\r
\r
- Type in the search box to match a title, a description or an \`#id\`.\r
- Click a **KPI tile** to filter by that group; click it again to clear.\r
- Use the filter bar for project, status, priority, environment and assignee. **Clear** resets everything.\r
\r
## Update many items at once\r
\r
Tick the checkboxes at the start of rows. A bulk bar appears with the number selected and lets you change status, priority or environment, or delete. Items you are not allowed to change are skipped by the server.\r
\r
## Bring items in from a spreadsheet\r
\r
Managers and admins see **Download Template** and **Bulk Upload** in the header. Download the Excel template, fill in one item per row, then upload the \`.xlsx\` or \`.csv\`. Every row is checked on its own, and you get a count of created and skipped rows with the reason for each skipped one.\r
\r
## Group items into an event\r
\r
**Events** collect items for a standup, a review or a release. Managers and admins create one with **+ New Event**, then add existing items or create new ones inside it. Open an event to see all its items together.\r
\r
## Keep track of changes\r
\r
Open an item to see its activity: who changed what and when. Notifications for items you are involved in arrive in the bell at the top right.\r
`,"./guides/02-sprints.md":`# Sprints, end to end\r
\r
Sprints follow the usual Scrum workflow. Open **Sprints** in the top bar and choose a project.\r
\r
## Turn Scrum on\r
\r
Agile is off for every project until a manager or admin turns it on. Open **Sprints**, pick the project and click **Set up Scrum board**. This creates the board (To Do, In Progress, Testing, Done) and a backlog ranked from the project's open issues. Nothing is deleted or changed. If you are not a manager, ask one to do this.\r
\r
## The three levels\r
\r
- **Epic**: a large body of work. Epics are never put into a sprint.\r
- **Story, Task, Bug and Requirement**: the standard issues. Each can belong to one Epic and is planned into sprints.\r
- **Sub-task**: always belongs to a standard issue and follows its sprint and Epic.\r
\r
## Plan\r
\r
1. Open the **Backlog** tab. Future and active sprints sit above the ranked backlog.\r
2. Click **Create sprint** and give it a name, a goal and dates.\r
3. Drag issues to rank them or move them into the sprint. The ⋯ menu on each row does the same without dragging, and the keyboard works too.\r
4. Check the **Capacity** tab to compare each member's available hours with the committed estimate.\r
\r
## Run\r
\r
1. Click **Start sprint**. It needs at least one issue and opens the board.\r
2. On the **Active sprint** tab, drag a card to another column to change its status. An issue is done when its status is in the right-most column.\r
3. Use quick filters, swimlanes and flags to focus. Columns can have minimum and maximum limits that warn when the work in progress is too high.\r
\r
## Finish\r
\r
Click **Complete sprint**. Finished issues stay with the closed sprint. For each unfinished issue you choose where it goes: the backlog, a future sprint or a new sprint.\r
\r
## Review\r
\r
The **Reports** tab has burndown, burnup, sprint report, velocity, cumulative flow, control chart, Epic report, daily summary and workload. Each can be exported to CSV. See [Reports and exports](#help/reports). The **Epics** tab shows each Epic with its issues and progress, and **Board settings** controls columns, limits, the estimation statistic and working days.\r
\r
## Who can do what\r
\r
Everyone with access can move cards on the board. Starting, completing and configuring sprints needs a manager or admin. See [Roles and permissions](#help/roles).\r
`,"./guides/03-reports.md":`# Reports and exports\r
\r
## The Reports page\r
\r
**Reports** is available to managers and admins. Pick a report type, set the filters and click **Run** to preview it in the page. Then click **Download XLSX** to get the full data as an Excel file.\r
\r
## Report types\r
\r
- **Item Detail Export**: one row per item.\r
- **Resolution Throughput** and **Created vs Resolved Timeline**: how fast work is closed over time.\r
- **Pending Items Snapshot** and **Aging Report**: what is still open and for how long.\r
- **Status Distribution**, **Priority Distribution** and **Project Breakdown**: how work is spread out.\r
- **Time to Resolution**: how long items take from creation to resolved.\r
\r
Each type starts with a sensible date range. Change **From** and **To**, or clear them for all time. The optional run label is added to the exported file so you can tell exports apart.\r
\r
The preview shows only the first rows. The downloaded file always has the complete result.\r
\r
## Analytics\r
\r
**Analytics** shows charts of status, priority, environment, project and assignee. It follows the type tabs and the KPI filters.\r
\r
## Sprint reports\r
\r
Agile reports live inside **Sprints**, on the **Reports** tab: burndown, burnup, sprint report, velocity, cumulative flow, control chart, Epic report, daily summary and workload. They are built from the change log, so a sprint's commitment is what it held when it started. Scope added later is shown separately. Each one can be exported to CSV.\r
\r
## Ask Sleuth\r
\r
You can also ask Sleuth, for example "export all bugs in apollo to excel", and it returns a real Excel file. Open it with the **Ask Sleuth** button in the top bar.\r
`,"./guides/04-notifications.md":`# Notifications and push\r
\r
## The bell\r
\r
The bell at the top right lists what happened on items and events you are involved in. A badge shows how many are unread. Click a notification to jump straight to the item, or use **Mark all read**. The list refreshes by itself while the bell is open.\r
\r
## Email\r
\r
Whether email arrives depends on how your administrator set the server up. It can send a message per event, or one **daily digest** that batches everything for the day. If you do not receive any, ask your administrator which email backend is configured.\r
\r
## Browser push\r
\r
Push notifications show up even when the app is in a background tab. They are optional and switched on by your administrator.\r
\r
1. When push is enabled on the server, your browser asks for permission the first time you open the app. Choose **Allow**.\r
2. That is all. The app registers your browser by itself each time it opens.\r
\r
If you chose **Block**, change the notification permission for this site in your browser's site settings, then reload. Push needs a secure (https) address, except on localhost. On networks that block Google's push servers it may never arrive, and the bell still works.\r
\r
Logging out removes your browser's push registration, so a shared computer does not keep receiving your alerts.\r
\r
## Reduce the noise\r
\r
Notifications follow your involvement, so being removed as an assignee or reporter stops them for that item. Admins control the email schedule and retention.\r
`,"./guides/05-git.md":`# Git branches for stories\r
\r
Bug Hunter can create a feature branch in a GitHub repository for a **User Story**, and remove exactly that branch again. This is optional and off until an administrator sets it up. Pull requests, merges and tags are not part of it.\r
\r
## One-time setup (admin or manager)\r
\r
1. Edit the project (click it in the sidebar) and open **Git integration**.\r
2. Enter the API base URL, the organization and the default base branch.\r
3. Paste a project token. Use a fine-grained token with Metadata read and Contents read and write. It is stored encrypted and never shown again.\r
4. Click **Save Git settings**, then **Test Connection**, and **Load repositories** to check the token can see them.\r
\r
## Create a branch\r
\r
1. Open a **Story**. The Git branches section is only on Stories; other types are refused.\r
2. Choose the repository and the base branch.\r
3. Click **Create Feature Branch**.\r
\r
The name is generated for you as \`feature_<story number>_<title slug>\`, where the slug comes from the first 25 characters of the title. Asking twice does not create a second branch.\r
\r
## Remove a branch\r
\r
Any member of the story's project can remove a branch that Bug Hunter created, if the administrator enabled deletion. Protected names such as \`main\`, \`master\`, \`dev\`, \`develop\` and \`release\`, and the project's base branch, are always refused. If the branch is already gone at the provider it counts as removed. Removed branches stay visible as history.\r
\r
## If something fails\r
\r
Authentication, TLS, timeout and rate-limit errors leave the branch record active, so you can retry. The message tells you which kind it was.\r
`,"./guides/06-admin.md":`# Admin setup\r
\r
This guide is for admins and managers setting up a new workspace.\r
\r
## 1. Create projects\r
\r
In the sidebar, click **+** next to **Projects**. Give the project a name, a colour and a description. Tick **Enable Agile** if the team will use [Sprints](#help/sprints). You can also turn it on later from **Sprints**.\r
\r
## 2. Add people\r
\r
Click **+** next to **Team** and enter a name, an email, a role and a password. The password must meet the password policy. Under **Projects**, tag the projects this person can see. People only see the projects they belong to; admins see all of them. Untick **Active** to stop someone logging in without deleting their history.\r
\r
## 3. Choose roles\r
\r
See [Roles and permissions](#help/roles). Give people the least role that lets them work.\r
\r
## 4. Check the basics\r
\r
- **Audit Trail** lists every create, update, delete and login for incident review. It is open to managers and admins.\r
- **Sessions** (admins) lists devices that are signed in. Revoke one to log that device out without touching the others.\r
- Light and dark themes are in the account menu at the top right, along with **Change password**.\r
\r
## 5. Optional features\r
\r
These are switched on in the server's environment settings, not in the app:\r
\r
- **Email**: choose the backend and, if you like, the daily digest.\r
- **Push**: needs a Firebase project and an https address. See [Notifications and push](#help/notifications).\r
- **Git branches**: per project, see [Git branches for stories](#help/git).\r
- **Sleuth** assistant: runs locally by default. A cloud model is optional.\r
\r
## 6. Bring existing work in\r
\r
Use **Download Template** and **Bulk Upload** on the Work Items page to import items from Excel or CSV. See [Creating and tracking items](#help/items).\r
`,"./guides/07-roles.md":`# Roles and permissions\r
\r
There are three roles. Your role is shown in the account menu at the top right. The server checks it on every action, so a hidden button is never the only protection.\r
\r
| Capability | User | Manager | Admin |\r
|---|---|---|---|\r
| See projects they belong to | Yes | Yes | All projects |\r
| Create and edit Bugs, Stories and Sub-tasks | Yes | Yes | Yes |\r
| Edit Tasks and Requirements | No | Yes | Yes |\r
| Edit any item or event | No | Yes | Yes |\r
| Move cards on a sprint board, view agile reports | Yes | Yes | Yes |\r
| Enable Agile, manage sprints, boards, releases, labels | No | Yes | Yes |\r
| Create projects and users | No | Yes | Yes |\r
| Use Reports and Audit Trail | No | Yes | Yes |\r
| Delete items, edit or delete comments, delete attachments | No | No | Yes |\r
| Grant the admin role, edit admins | No | No | Yes |\r
| View and revoke Sessions | No | No | Yes |\r
\r
## Project access\r
\r
Non-admins only see items in projects they are a member of. If something you expect is missing, ask a manager to add you to the project.\r
\r
## Why a button might be missing\r
\r
Sections such as Reports, Audit Trail and Sessions only appear for roles that may use them. If you need access, ask an admin to change your role.\r
\r
See also [Admin setup](#help/admin).\r
`});function x(e){for(let t of e.split(/\n\s*\n/)){let e=t.trim();if(e&&!e.startsWith(`#`))return e.replace(/\s+/g,` `)}return``}var S=Object.entries(b).sort(([e],[t])=>e.localeCompare(t)).map(([e,t])=>{let n=e.replace(/^.*\/\d*-?/,``).replace(/\.md$/,``);return{id:n,title:/^#\s+(.+)$/m.exec(t)?.[1]??n,summary:x(t),markdown:t}});function C(e){return S.find(t=>t.id===e)??null}function w(e){let t=String(e??``).toLowerCase().split(/\s+/).filter(Boolean);return t.length?S.filter(e=>{let n=`${e.title}\n${e.markdown}`.toLowerCase();return t.every(e=>n.includes(e))}):S}var T=t();function E(){let[e,t]=(0,s.useState)(``),[n,r]=(0,s.useState)(()=>i()??S[0]?.id),c=(0,s.useRef)(null),l=(0,s.useRef)(!1);(0,s.useEffect)(()=>o(()=>{let e=i();e&&C(e)&&(l.current=!0,r(e),t(``))}),[]);let u=(0,s.useMemo)(()=>w(e),[e]),d=C(n)??u[0]??S[0],f=(0,s.useMemo)(()=>d?_(d.markdown):``,[d]),p=(0,s.useMemo)(()=>d?y(d.markdown):[],[d]);(0,s.useEffect)(()=>{l.current&&(l.current=!1,c.current?.focus({preventScroll:!0}))},[d?.id]);let m=e=>{l.current=!0,r(e)};return(0,T.jsxs)(`section`,{className:`help-center`,id:`viewHelp`,"aria-label":`Help center`,children:[(0,T.jsxs)(`aside`,{className:`help-side`,children:[(0,T.jsxs)(`div`,{className:`field help-search`,children:[(0,T.jsx)(`label`,{htmlFor:`helpSearch`,className:`sr-only`,children:`Search the guides`}),(0,T.jsx)(`input`,{id:`helpSearch`,type:`search`,placeholder:`Search the guides…`,autoComplete:`off`,value:e,onChange:e=>t(e.target.value)})]}),(0,T.jsxs)(`nav`,{"aria-label":`Guides`,children:[(0,T.jsx)(`ul`,{className:`help-list`,children:u.map(e=>(0,T.jsx)(`li`,{children:(0,T.jsx)(`button`,{type:`button`,className:`help-list-item${e.id===d?.id?` active`:``}`,"aria-current":e.id===d?.id?`page`:void 0,onClick:()=>m(e.id),children:e.title})},e.id))}),u.length===0&&(0,T.jsxs)(`p`,{className:`help-none muted`,role:`status`,children:[`No guide matches “`,e,`”.`]})]}),(0,T.jsx)(`div`,{className:`help-side-actions`,children:(0,T.jsx)(`button`,{type:`button`,className:`btn ghost`,onClick:()=>a(`tour`),children:`Take the product tour`})})]}),(0,T.jsxs)(`article`,{className:`help-article`,ref:c,tabIndex:-1,"aria-label":d?.title,children:[p.length>1&&(0,T.jsx)(`nav`,{className:`help-toc`,"aria-label":`On this page`,children:p.filter(e=>e.level===2).map(e=>(0,T.jsx)(`button`,{type:`button`,className:`help-toc-link`,onClick:()=>document.getElementById(e.id)?.scrollIntoView?.({block:`start`}),children:e.text},e.id))}),(0,T.jsx)(`div`,{className:`help-md`,onClick:e=>{let t=e.target.closest?.(`a[href]`);if(!t)return;let n=t.getAttribute(`href`)||``;if(n.startsWith(`#help/`)){e.preventDefault();let t=n.slice(6);C(t)&&m(t)}else n.startsWith(`#`)&&(e.preventDefault(),document.getElementById(n.slice(1))?.scrollIntoView?.({block:`start`}))},role:`presentation`,dangerouslySetInnerHTML:{__html:f}})]},d?.id)]})}export{E as default};