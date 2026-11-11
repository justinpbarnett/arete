# Dashboard UX research

Justin finds the dashboard confusing: "things aren't where I would guess that they'd be or there is too many button presses and scrolling to get to something."
This note covers what users of comparable tools like best, what design research says, an audit of the dashboard as it stands, and prioritized changes that keep the Pattern house style.
Every outside claim links to the page it came from; all pages were read on 2026-11-09.

## Summary

- Developer and ops dashboards moved from flat rows of tabs to a persistent, grouped left sidebar, and users pushed back hard every time a vendor hid primary destinations behind a menu ([Grafana discussion 58910](https://github.com/grafana/grafana/discussions/58910), [Hacker News on GitHub's 2023 nav](https://news.ycombinator.com/item?id=35465869)).
- Agent and workflow tools organize around one list of work items with status filters, and they collect everything that waits on a person into one queue with a visible count ([Devin release notes 2026](https://docs.devin.ai/release-notes/2026), [Astronomer on Airflow Required Actions](https://www.astronomer.io/docs/learn/airflow-human-in-the-loop), [Linear Inbox](https://linear.app/docs/inbox)).
- Detail opens beside the list, with its own address and a way back, and users complain whenever opening a detail loses their place ([Trigger.dev changelog](https://trigger.dev/changelog/move-between-adjacent-runs), [Airflow issue 67472](https://github.com/apache/airflow/issues/67472)).
- Design systems agree that a segmented control switches between closely related views of the same content, and that separate sections need real navigation ([Apple HIG, Segmented controls](https://developer.apple.com/design/human-interface-guidelines/segmented-controls), [Primer, SegmentedControl](https://primer.style/product/components/segmented-control/guidelines/)).
- Arete's dashboard draws its nine views as one segmented control that scrolls away, spreads what needs Justin over five views, gives one run three viewers and no address, and opens details far from the row that was clicked (see the audit).
- The changes with the largest effect are a "Needs you" section at the top of the landing view, a sticky grouped sidebar of real links with counts, and one addressable run page that opens beside the list.

## What users of comparable tools like best

The patterns below recur across vendor changelogs, product docs and public user feedback.
Each one is followed by the pushback users gave when a product got it wrong.

### 1. A grouped left sidebar that stays visible

- Vercel "Moved horizontal tabs to a resizable sidebar that can be hidden when not needed" and "Reordered navigation items to prioritize the most common developer workflows" ([Vercel changelog, January 2026](https://vercel.com/changelog/new-dashboard-navigation-available)).
- Railway: "The navigation has moved to the left." ([Railway changelog, March 2026](https://railway.com/changelog/2026-03-06-domains-ui-layout)).
- Datadog orders its sidebar "both by their usage and relationship to each other" ([Datadog blog](https://www.datadoghq.com/blog/datadog-navigation-redesign/)).
- Inngest redesigned after "Users were having difficulty navigating and finding specific parts of the Cloud Application, such as Webhooks", ran a card sort, and ended with two sidebar groups, Monitor and Manage ([Inngest blog](https://www.inngest.com/blog/reimagining-information-architecture)).
- Sentry regrouped its sidebar because it had "started to feel a bit cramped, especially on smaller screens" ([Sentry changelog](https://sentry.io/changelog/new-nav-issue-views-ga)).
- The strongest negative signal in this survey is about hiding that sidebar.
  When Grafana 10 put its navigation behind a menu button, a user wrote "I don't want it to be hidden behind a hamburger menu", and Grafana staff conceded "the left hand menu being clunky is by far the most common piece of feedback we've had" and "We acknowledge that moving between some of the pages now takes more steps" ([Grafana discussion 58910](https://github.com/grafana/grafana/discussions/58910)).
  Grafana answered with a design "that lets users pin the menu open" (same discussion).
- GitHub's 2023 nav moved links "to a hidden menu behind the GitHub logo", and a commenter answered "All it does is require more clicks for no benefit to the user" ([Hacker News](https://news.ycombinator.com/item?id=35465869)).
  GitHub then put "Links to issues and pull requests at the upper level of the navigation" ([GitHub changelog](https://github.blog/changelog/2023-06-15-redesigned-navigation-enabled-for-all-users-beta/)).
- A related complaint from the same redesign: "Links should be links to places. If your link is an option menu, it's not a link." ([Hacker News](https://news.ycombinator.com/item?id=36418525)).
- Users also push back when a wide sidebar cannot be made smaller: "the visible by default this huge sidebar is absolutely contrary to what is called "put content first"" ([Grafana community](https://community.grafana.com/t/how-to-disable-grafana-navigation-docked-localstorage-variable-by-default/116791)).

### 2. One "needs you" queue, and a count you can see from anywhere

- Linear's Inbox has a Priority tab that "separates notifications that need your attention from other updates", opened with G then I from any page ([Linear docs](https://linear.app/docs/inbox)).
- Devin's sessions sidebar groups by "Working / Ready / Blocked / Inactive", and "The sidebar now names the approval a session is waiting on" ([Devin release notes 2026](https://docs.devin.ai/release-notes/2026)).
- Airflow's human-in-the-loop approvals started as a tab on each task, then gained a global Browse > Required Actions page listing "all required actions (pending and resolved)", then a sidebar icon in 3.3 ([Astronomer docs](https://www.astronomer.io/docs/learn/airflow-human-in-the-loop)).
- GitHub Actions has no approval queue across repos, and users ask for one: "At the moment I would need to iterate over every repo" ([GitHub discussion 49451](https://github.com/orgs/community/discussions/49451)).
- Dagster+ built a homepage around "what needs attention right now" and quotes a customer: "The new homepage is the first thing I check every morning." ([Dagster blog](https://dagster.io/blog/introducing-the-new-dagster-plus-ui)).
- A queue on its own page is not enough; a Linear user said "it is easy to miss stuff unless you are constantly checking their inbox" ([Hacker News](https://news.ycombinator.com/item?id=38340106)).

### 3. One list of work items, narrowed by filters

- Devin folded a separate view back into the main list: "The dedicated PR-only view has been consolidated with the "All Sessions" page for a unified view." ([Devin release notes 2025](https://docs.devin.ai/release-notes/2025)).
- A Copilot coding agent user: "I'm not sure I found any page or view where I can see the status of the coding agent in one shot." ([GitHub discussion 168291](https://github.com/orgs/community/discussions/168291)).
- Temporal's workflow list filters by status, type and time, keeps up to 20 saved views that can be shared as a URL, and ships a predefined "Task Failures" view ([Temporal docs](https://docs.temporal.io/web-ui)).
- Trigger.dev shows only root runs by default and added ID and date filters to "narrow down to the runs you want" ([Trigger.dev changelog](https://trigger.dev/changelog/new-run-filters)).
- Without filters, finding one run is slow: "It takes me about 10 minutes just to find the right execution" ([n8n community](https://community.n8n.io/t/the-executions-tab-user-experience-is-terrible/146569)).

### 4. Detail beside the list, with an address and a way back

- Langfuse's peek view lets you "preview trace details directly from the table view without having to navigate away" ([Langfuse changelog](https://langfuse.com/changelog/2025-03-21-table-peek-view)), and in LangSmith you "Click into any row to open the side panel" ([LangSmith docs](https://docs.langchain.com/langsmith/view-traces)).
- Trigger.dev: "Now you can easily navigate to the previous or next run directly from the run details page", with the `[` and `]` keys, and "The run details page now remembers your table filters" ([Trigger.dev changelog](https://trigger.dev/changelog/move-between-adjacent-runs)).
- Buildkite's job log drawer is "available on every page" ([Buildkite changelog 266](https://buildkite.com/resources/changelog/266-introducing-the-new-build-page-engineered-for-scale-and-flexibility/)).
- Losing your place is a recurring complaint.
  On GitHub Actions: "the horrible click on a failed job and never be able to come back reliably" ([Hacker News](https://news.ycombinator.com/item?id=46914114)).
  On Airflow: "It does not work well if you debug by jumping between run history, dependencies, and logs on the same page." ([Airflow issue 67472](https://github.com/apache/airflow/issues/67472)).
  On Dagster: "When I'm on the "Jobs" page and then I click on one job, suddenly I'm in the "Settings" part of Dagster." ([Dagster discussion 21370](https://github.com/dagster-io/dagster/discussions/21370)).
- Actions on a run must stay in reach: "Every failed CI investigation starts with a manual scroll-up just to reach the Rerun button." ([GitHub discussion 195854](https://github.com/orgs/community/discussions/195854)).

### 5. Live lists that do not move under the pointer

- On Trigger.dev, "When new runs come in, a banner appears at the top with the count, and you click it to refresh." ([Trigger.dev changelog](https://trigger.dev/changelog/live-runs-page)).
- n8n's executions list re-sorts live, and a user reported "the order has re-sorted during my click and the wrong execution gets stopped." ([n8n community](https://community.n8n.io/t/difficult-to-stop-processes-on-executions-tab-due-to-constant-resorting/300850)).

### 6. A command palette and go-to keys on top of the sidebar, never instead of it

- Devin: "The ⌘K palette lists the same navigation pages as the sidebar" ([Devin release notes 2026](https://docs.devin.ai/release-notes/2026)).
- Grafana's palette lets you "move through the platform without even taking your hands off the keyboard" ([Grafana blog](https://grafana.com/blog/2023/03/09/new-in-grafana-cloud-key-improvements-to-the-command-palette-and-navigation-experience)), and Datadog's Quick Nav opens with Cmd/Ctrl+K from any screen ([Datadog blog](https://www.datadoghq.com/blog/datadog-quick-nav-menu/)).
- Linear users praise that it is fully keyboard navigable, with shortcuts for nearly everything ([Hacker News](https://news.ycombinator.com/item?id=45826122)).
- A Grafana user explains why a palette cannot replace one-click paths: "I know that I can use Ctrl+K but some times I just have my hand on the mouse and want to do it this way." ([Grafana discussion 58910](https://github.com/grafana/grafana/discussions/58910#discussioncomment-5744257)).

### 7. What users push back on

- Moving something they watch all the time one level deeper.
  After v0 moved its credit balance out of the profile menu: "We literally monitor that every prompt." ([Vercel community](https://community.vercel.com/t/credit-balance-removed-from-profile-dropdown/31157)).
  Sentry restored live updates after "without real time updates I would have to keep refreshing the feed every second like a crazy person." ([Sentry issue 92205](https://github.com/getsentry/sentry/issues/92205)).
- Low density: "The list items have excessively large paddings, I'd prefer a (much) more compact view." ([GitHub discussion 177902](https://github.com/orgs/community/discussions/177902#discussioncomment-14806984)).
- Losing the choice of landing view: "I'd like an option for the Feed to be my default view." ([GitHub discussion 177902](https://github.com/orgs/community/discussions/177902#discussioncomment-14807322)).
- Extra clicks after a regroup: "Extra clicks to navigate vs fast hover-pop-up flow." ([Grafana discussion 58910](https://github.com/grafana/grafana/discussions/58910#discussioncomment-5542364)).

## Principles from design research

These come from NN/g, Apple's Human Interface Guidelines, IBM Carbon, GitHub Primer, GOV.UK and Atlassian.
Material Design's pages render in the browser and returned no text to the fetcher, so this note makes no claim from Material.
Some NN/g articles are old, and their year is given where it matters.

### Pick the control for the job

- Apple: "Consider a segmented control to switch between closely related subviews." and "For switching between completely separate sections of an app, use a tab bar instead."; "Aim for no more than about five to seven segments in a wide interface" ([Apple HIG, Segmented controls](https://developer.apple.com/design/human-interface-guidelines/segmented-controls)).
- Primer: a SegmentedControl picks "from a linear set of closely related choices", and "Don't use a SegmentedControl for more than 5 options." ([Primer, SegmentedControl](https://primer.style/product/components/segmented-control/guidelines/)).
- NN/g: "Mixing in-page and navigation tabs within one tab control will disorient users." ([NN/g, Tabs, Used Right](https://www.nngroup.com/articles/tabs-used-right/)).
- NN/g lists the left side of the screen as the expected place for an application's primary menu, and says "Failing to indicate the current location is probably the single most common mistake we see on website menus." ([NN/g, Menu-Design Checklist](https://www.nngroup.com/articles/menu-design/)).
- "visual search in a list is more efficient if the list is vertical than if it is horizontal", and vertical navigation suits products whose navigation "is likely to grow in the future" ([NN/g, Left-Side Vertical Navigation](https://www.nngroup.com/articles/vertical-nav/)).
- Carbon: "Use the left panel if there are more than five secondary navigation items, or if you expect a user to switch between secondary items frequently." ([Carbon, UI shell left panel](https://carbondesignsystem.com/components/UI-shell-left-panel/usage/)).
- Apple: title each group in a sidebar, and "In general, show no more than two levels of hierarchy in a sidebar." ([Apple HIG, Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars)).
- Apple: "Use a tab bar to support navigation, not to provide actions." ([Apple HIG, Tab bars](https://developer.apple.com/design/human-interface-guidelines/tab-bars)).
- Primer's NavList "indicates which view they're currently on", uses groups for long lists, and gives "a count of unread notifications" as its example of a trailing visual ([Primer, NavList](https://primer.style/product/components/nav-list/)).
- Carbon reserves the header for "universal, system-level functions such as profile, search, notifications" ([Carbon, UI shell header](https://carbondesignsystem.com/components/UI-shell-header/usage/)).

### Labels, groups and order

- A link's label is its information scent, and "link names should be clear and self-explanatory" ([NN/g, Information Scent](https://www.nngroup.com/articles/information-scent/)).
- "Each of your categories should have a unique identity so users don't get confused between two similar options." ([NN/g, Confusing Category Names](https://www.nngroup.com/articles/category-names-suck/)).
- The number of clicks matters less than whether each click has strong scent ([NN/g, The 3-Click Rule for Navigation Is False](https://www.nngroup.com/articles/3-click-rule/)).
- "Prioritizing the most frequently accessed items means that most people won't have to read through the entire menu." ([NN/g, Top 3 IA Questions about Navigation Menus](https://www.nngroup.com/articles/ia-questions-navigation-menus/)).
- GOV.UK: "Navigation is not a site map and does not need to list every part of your service." ([GOV.UK, Navigate a service](https://design-system.service.gov.uk/patterns/navigate-a-service/)).
- Card sorting finds the groups people expect, and tree testing checks that a proposed structure lets them find key features ([NN/g, Card Sorting](https://www.nngroup.com/articles/card-sorting-definition/), [NN/g, Tree Testing](https://www.nngroup.com/articles/tree-testing/)).

### Long pages and what goes at the top

- Users spent "about 57% of their page-viewing time above the fold"; "Reserve the top of the page for high-priority content" ([NN/g, Scrolling and Attention, 2018](https://www.nngroup.com/articles/scrolling-and-attention/)).
- "Operational dashboards aim to impart critical information quickly to users" at a glance ([NN/g, Dashboards, 2017](https://www.nngroup.com/articles/dashboards-preattentive/)).
- Carbon: "Place the most important at the top of the page" and "Non-essential information should be provided as needed." ([Carbon, Dashboards](https://carbondesignsystem.com/data-visualization/dashboards/)).
- "designs that go beyond 2 disclosure levels typically have low usability" ([NN/g, Progressive Disclosure, 2006](https://www.nngroup.com/articles/progressive-disclosure/)).
- "When a user needs to access information from most or all accordions, it can become tedious to have to expand each of them." ([NN/g, Accordions on Desktop](https://www.nngroup.com/articles/accordions-on-desktop/)).
- "Offering direct access to specific sections on the page minimizes scrolling" ([NN/g, In-Page Links](https://www.nngroup.com/articles/in-page-links-content-navigation/)), but in testing "many users failed to notice the sticky table of contents" ([NN/g, Table of Contents](https://www.nngroup.com/articles/table-of-contents/)).
- Clicking and scrolling are both interaction cost, and "Usable sites minimize the interaction cost required to attain a variety of user goals." ([NN/g, Interaction Cost](https://www.nngroup.com/articles/interaction-cost-definition/)).

### Detail, addresses and Back

- Apple: a split view shows "multiple levels of your app's hierarchy at once", and you should "persistently highlight the current selection in each pane that leads to the detail view." ([Apple HIG, Split views](https://developer.apple.com/design/human-interface-guidelines/split-views)).
- Complex applications should let users "access and view supplemental information without leaving the primary screen" ([NN/g, Complex Applications](https://www.nngroup.com/articles/complex-application-design/)).
- Primer: "Encode meaningful state, like the active tab, selection, or highlighted line, in the URL so a link or a reload restores it." ([Primer, View](https://primer.style/product/scenario-patterns/view/)).
- NN/g: "ensure the Back button has the same effect as a Close link" ([NN/g, User Control and Freedom](https://www.nngroup.com/articles/user-control-and-freedom/)); GOV.UK: take users back "in the state they last saw it" ([GOV.UK, Back link](https://design-system.service.gov.uk/components/back-link/)).
- "Pogo sticking often signals insufficient helpful details on the listing page." ([NN/g, Anatomy of a List Entry](https://www.nngroup.com/articles/list-entries/)).
- Carbon: "If there is too much information for a user to consume in a dialog context, consider using a full page instead." ([Carbon, Dialogs](https://carbondesignsystem.com/patterns/dialog-pattern/)).

### Things that need action

- "Action-required notifications alert the user of an event that requires a user action", while a badge is a typical passive notification and is easy to overlook ([NN/g, Indicators, Validations, and Notifications](https://www.nngroup.com/articles/indicators-validations-notifications/)).
- Carbon: "Notification panels ensure that users can access and read all notifications." ([Carbon, Notifications](https://carbondesignsystem.com/patterns/notification-pattern/)).
- Atlassian: "Never use auto dismiss flags for any critical warning or error messages" ([Atlassian, Flag](https://atlassian.design/components/flag/usage)).
- Carbon: "if a user needs to repeatably perform a task, consider making the task completable on the main page." ([Carbon, Modal](https://carbondesignsystem.com/components/modal/usage/)).
- "Do not use confirmation dialogs for routine actions."; label responses with what will happen, and prefer undo ([NN/g, Confirmation Dialogs](https://www.nngroup.com/articles/confirmation-dialog/)).

### Accelerators

- Accelerators "should be readily available, yet easy to ignore" ([NN/g, UI Accelerators](https://www.nngroup.com/articles/ui-accelerators/)).
- "Recognition is easier than recall", so a palette should list what it can do as you type ([NN/g, Recognition and Recall](https://www.nngroup.com/articles/recognition-and-recall/)).
- Apple: "Aim to make your app's content searchable through a single location." and "Define custom keyboard shortcuts for only the most frequently used app-specific commands." ([Apple HIG, Searching](https://developer.apple.com/design/human-interface-guidelines/searching), [Apple HIG, Keyboards](https://developer.apple.com/design/human-interface-guidelines/keyboards)).

## Audit of the current dashboard

The audit reads `arete/web/index.html`, `arete/web/app.js`, `arete/web/app.css`, `arete/web/pattern.css` and `arete/dashboard.py` at commit `acd1ee6`; these files are unchanged through `55e482f`.
Page heights and positions were measured in Chrome against the running dashboard on 2026-11-09, at a 1854 x 997 CSS px viewport, with that day's real data: 10 loops, 25 recent runs, 2 pushes waiting for approval, 43 triage tickets, 4 sessions and 1 device.
On a 1440 x 900 laptop the viewport is about 780 px tall, so each 1,000 px of scrolling below is about 1.3 screens there.

### The frame around every view

- The header (kicker, a 76 px `h1`, a four line description and the status line) takes the first 367 px, so the view row starts at y = 367 and the first section heading at y = 482.
- `nav.views` is `position: static`, so it scrolls away; after reading to the bottom of Live (6,430 px) the only way to another view is to scroll back up.
- The nine views are `<button>` elements inside `.seg role="group"` with `aria-pressed`, not links, so a view cannot be opened in a new tab and assistive tech hears a toggle group, not navigation.
- No view label carries a count, so nothing outside Loops says that two pushes wait for approval.
- Only the view is in the URL (`#live`, `#loops`...); an open log, feed, loop detail, session or fold is lost on reload and cannot be linked.
- Each `h2` carries 76 px of top margin plus an 18 px rule gap from `pattern.css`, so an empty section ("Nothing is running.") still costs about 170 px.

### View inventory

| View | What it holds, top to bottom | Height |
|------|------------------------------|--------|
| Live (default) | 01 Running now (slot readout; every running run of any kind with Log and Stop); the log panel opens here; 02 Skipped (Run anyway); 03 By hand (harness fixes and memory facts, Mark done); 04 Review feedback; 05 Recent runs (the last 25 of every kind, Log) | 6,430 px |
| Loops | 01 Approvals (Approve or Try again, Reject, Show the body); 02 Loops (serve line, then each loop with Open, Start, Try, Turn on or off); the loop feed opens here; 03 Draft a loop (form); the draft feed opens here; 04 the loop detail opens here (facts, runs, findings, sources, metrics, digests, memory, prompt) | 2,898 px, 3,875 px with a detail open |
| Launch | 01 Launch a session (form); the session feed and its message box open here; 02 Sessions (Open) | 1,819 px |
| Fleet | 01 Serve (state, pid, beat, ticks, sources that fail to poll, loops that do not load); 02 Devices (Probe) | 1,258 px |
| Health | 01 Checks (Run the checks now, 20 checks); 02 Loops (loops that report on their own); 03 Probes; 04 Alerts | 2,801 px |
| Triage | Refresh triage now; 01 Ready to build; 02 Sent to the pipeline; 03 Research; 04 With questions; 05 Needs a person; 06 Not triaged; 75 folds and 33 buttons in all | 8,478 px |
| Trends | 01 Cost per day; 02 Outcomes per day; 03 By repo; 04 Evals; 05 Where it went (by model, by runner, by loop) | 3,670 px |
| Settings | Runs at once, runs per repo, a model per role | 1,386 px |
| Chat | Console messages, the drafted issue (File this issue, Drop), the message box | 1,445 px |

### Top tasks today

Counts start on Live at the top of the page, which is where the dashboard opens.
A click is a pointer press; typing is not counted.

| # | Task | Path today | Clicks | Scrolling |
|---|------|-----------|--------|-----------|
| 1 | See what is running now | Live, 01 Running now | 0 | none; it sits at y = 482 |
| 2 | See what waits for my approval | Loops, 01 Approvals; nothing on Live points there | 1, if you know to look in Loops | none in Loops |
| 3 | See everything that waits on me | Loops (approvals), Live (Skipped, By hand), Triage (Ready to build, With questions, Research reports), Health (Alerts), Chat (a drafted issue) | 4 to 5 view switches, plus folds | about 6,000 px: Triage's With questions starts at y = 4,998, Health's Alerts at y = 2,485 |
| 4 | Open the live feed of a running loop | Loops, Feed in the Last run cell | 2 | 0 to 1,200 px to find the row |
| 5 | Open the log of a past pipeline run | Live, scroll to the row in Recent runs, Log | 1 | up to 5,500 px down; the click then jumps the page back to the log at y = 642, so the list position is lost |
| 6 | Open the feed of a past loop run | Loops, Open, then Feed in the detail's Runs table | 3 | Open renders the detail at y = 2,753, about 1,650 px below the row and under the Draft a loop form; Feed then opens the feed back above the form |
| 7 | Stop a run | Live, Stop in Running now | 1 | none |
| 8 | Try a loop, then turn it on | Loops, Try, read the feed, Turn on | 3 | the feed opens under the loop table, so you scroll back up to the row to turn it on |
| 9 | Launch a session | Launch, fill the form, Launch | 2 | none |
| 10 | Return to an earlier session | Launch, scroll to 02 Sessions, Open | 2 | about 1,100 px down; the feed then opens above the list at y = 1,138 and pushes the list down |
| 11 | Check what this week cost and where it went | Trends, scroll to 05 Where it went | 1 | about 1,700 px to the breakdown, 2,500 px to By loop |
| 12 | Read a research report | Triage, scroll to 03 Research, find the ticket, Show the report | 2 | about 1,400 px, then the report reads in a 420 px high box inside a table cell |
| 13 | Answer a ticket's questions and send it | Triage, scroll to 04 With questions, Answer the questions, Send to the pipeline | 3 | about 4,500 px |
| 14 | Check that serve and the machine are healthy | Fleet (serve), Health (checks, alerts), or the serve line in Loops | 1 to 2 | Alerts sit at the bottom of Health, y = 2,485 |
| 15 | Draft a new loop | Loops, scroll to 03 Draft a loop, Draft it | 2 | about 1,800 px |

### Where things are not where a user would guess

1. **What needs you is in five places.**
   Approvals are section 01 of Loops, but the dashboard opens on Live, which says nothing about them; on the day measured two pushes to arete's main waited there unseen.
   Skipped tickets and By hand fixes are on Live, questions and ready tickets on Triage, alerts at the bottom of Health, and a drafted issue in Chat.
2. **One run has three viewers and no address.**
   Live's Log opens the raw `run.log`; Loops' Feed opens the structured feed (steps, tool calls, output) of the same kind of run; Launch's Open opens a session feed with a message box.
   The server's `/api/feed/<id>` serves any run, but no URL shows one.
3. **Detail opens far from the row you clicked.**
   Log on the 25th recent run (y = 5,997) opens the log at y = 642 and scrolls the page there; Open on a loop renders the detail at y = 2,753, below an unrelated form; Close the log and Close the detail hide the panel but do not take you back to the row you came from.
4. **Labels do not match the jobs.**
   Serve's state shows in Loops and in Fleet, and "Fleet" does not suggest "serve".
   "Loops" is both a view and a section of Health.
   Costs show in Live, Loops, Launch, Chat and at the bottom of Trends, and no view is called Costs.
   Launch starts sessions only, while starting other work is spread over Loops (Start, Try, Draft it), Triage (Send to the pipeline, Research it now, Refresh triage now), Live (Run anyway) and Health (Run the checks now).
   Sessions are listed under Launch, below the form, and Chat is a second, separate agent conversation.
5. **Long pages with no way to narrow them.**
   Recent runs is a fixed list of 25 with no filter by status, kind or repo, even though `state.runs()` in `arete/state.py` already filters by kind, repo and loop.
   It is newest first and redrawn every 5 seconds, so a run that starts while you read pushes every row down.
   Triage is 8,478 px with 75 folds, and its groups are stacked sections, not filters.
6. **Empty sections cost a full section.**
   On the day measured, Skipped, By hand and Review feedback were all empty and still took the 520 px between y = 704 and y = 1,221, pushing Recent runs below the first screen.
7. **Nine equal buttons, no grouping.**
   The row mixes objects (Loops, Fleet), an action (Launch), a tool (Chat), a report (Trends) and configuration (Settings) at the same weight, in an order that is neither by frequency nor by kind.
8. **Approve and Stop act on one click with no confirmation.**
   That matches the advice for routine actions, but the Approve button does not say what it will do (for example, push to arete's main), and the result message appears in Loops' result line, not next to the row.

A side note from the same session: Live polls `/api/state` every 5 seconds, and that answer was 175 KB, most of it the whole triage board, which Live does not show.

## Recommendations

They are ordered by how much each one cuts clicks and scrolling across the top tasks above.
Each one reuses Pattern parts that already exist: `a.btn[aria-current]` for the current place, `.seg` for "one of several", `.tag` for neutral labels, `.state` for the only colored words, tables and `details` folds.
What is new is layout, not look.

### 1. Open on a "Needs you" section that holds everything waiting on Justin

**Change.**
In `drawLive` (`arete/web/app.js`), replace 02 Skipped, 03 By hand and 04 Review feedback with one section, 01 Needs you, above Running now.
It is one table with one row per waiting item and its one-click action: pushes waiting for approval and failed deliveries (Approve or Try again, Reject; moved from Loops' 01 Approvals), open alerts (from Health's 04 Alerts), tickets with questions (Answer), tickets ready to build (Send to the pipeline), research reports (Read), skipped tickets (Run anyway), By hand fixes (Mark done), review feedback, and a drafted issue from Chat (File this issue, Drop).
A `.tag` names each row's kind, and a failed delivery keeps its `.state.fault` word.
Triage kinds show their five oldest rows, then a link such as "All 21 with questions" into the filtered Triage view from recommendation 6.
When nothing waits, the section is the single line "Nothing needs you."

**Why.**
Action-required events need a prominent, persistent place, not a passive badge alone ([NN/g, Indicators, Validations, and Notifications](https://www.nngroup.com/articles/indicators-validations-notifications/)).
Linear, Devin and Airflow each ended up with one queue of what waits on a person ([Linear Inbox](https://linear.app/docs/inbox), [Devin release notes 2026](https://docs.devin.ai/release-notes/2026), [Astronomer on Airflow](https://www.astronomer.io/docs/learn/airflow-human-in-the-loop)), and GitHub Actions users ask for one because approvals are scattered across repos ([GitHub discussion 49451](https://github.com/orgs/community/discussions/49451)).
The top of the page gets most of the attention ([NN/g, Scrolling and Attention](https://www.nngroup.com/articles/scrolling-and-attention/)).
Repeated tasks belong on the main page, not in a modal ([Carbon, Modal](https://carbondesignsystem.com/components/modal/usage/)).

**Effect.**
"See everything that waits on me" goes from 4 to 5 view switches and about 6,000 px of scrolling to 0 clicks.
On the day measured the section would hold about 17 rows (2 approvals and 5 each of questions, people and research), roughly one screen; this is an estimate.
"See what waits for my approval" goes from 1 click, if you know to look in Loops, to 0.
Live sheds the 520 px of empty sections.

### 2. Replace the view row with a sticky, grouped sidebar of links with counts

**Change.**
In `index.html`, replace `nav.actions.views > .seg` (nine `<button data-view>` with `aria-pressed`) with a `<nav aria-label="Views">` of `a.btn` links (`href="#live"` and so on) that carry `aria-current="page"`, stacked in a left column with `position: sticky; top: 0`.
Group the links under short titles, for example Work (Live, Runs, Sessions, Loops, Triage, Chat), Watch (Costs, Health) and Settings, and order each group by how often Justin uses it.
Show counts at the end of a link: the Needs you count on Live as a `.state.caution` word when it is above zero, the questions count on Triage and the running count on Runs as `.tag`, and a `.state.fault` word on Health when an alert is open or serve is down.
Move the kicker, the 76 px `h1` and the four-line description out of the app frame; the product name sits at the top of the sidebar, and the status line sits at its foot as the one place serve's state shows (replacing the serve line in Loops and Fleet's 01 Serve).
Where the sidebar and the 1,180 px `.wrap` no longer fit side by side, the same links become a sticky row at the top that wraps to a second line; never a hamburger menu.

**Why.**
Applications put primary navigation on the left and must show the current location ([NN/g, Menu-Design Checklist](https://www.nngroup.com/articles/menu-design/)); vertical lists scan faster and scale ([NN/g, Vertical Navigation](https://www.nngroup.com/articles/vertical-nav/)); more than five items or frequent switching calls for a left panel ([Carbon, UI shell left panel](https://carbondesignsystem.com/components/UI-shell-left-panel/usage/)).
Nine separate sections are the wrong job for a segmented control ([Apple HIG, Segmented controls](https://developer.apple.com/design/human-interface-guidelines/segmented-controls), [Primer, SegmentedControl](https://primer.style/product/components/segmented-control/guidelines/)), and an action like Launch does not belong in navigation ([Apple HIG, Tab bars](https://developer.apple.com/design/human-interface-guidelines/tab-bars)).
Titled groups, at most two levels ([Apple HIG, Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars)), and counts as trailing visuals ([Primer, NavList](https://primer.style/product/components/nav-list/)) follow the design systems; ordering by frequency follows [NN/g](https://www.nngroup.com/articles/ia-questions-navigation-menus/).
Users rejected hiding this navigation in Grafana and GitHub ([Grafana discussion 58910](https://github.com/grafana/grafana/discussions/58910), [Hacker News](https://news.ycombinator.com/item?id=35465869)), and they want links that are links ([Hacker News](https://news.ycombinator.com/item?id=36418525)).

**Effect.**
Moving to any view is 1 click and 0 px of scrolling from anywhere; today it can mean scrolling up 6,430 px from the bottom of Live or 8,478 px from the bottom of Triage first.
The first heading of every view moves up by most of the 367 px header, an estimated 300 px.
Views can be opened in a new tab, and the current one is announced as the current page.
`a.btn[aria-current]` already draws filled ink in Pattern, and `.seg` goes back to the in-view filters it was made for.

### 3. Give every run one page with an address, opened beside the list

**Change.**
Add a route `#run/<id>` that shows the run's kind (`.tag`), repo, loop or ticket and status (`.state`); its facts (start, duration, cost, model); a header row of its actions that stays at the top of the panel (Stop while running, Approve or Reject when an emit waits, a link to the pull request when there is one); and a `.seg` of Feed and Log that switches between `/api/feed/<id>` and `run.log`.
A session gets the same page with its message box under the feed.
Every Log button in Live, Feed in Loops' Last run cell and the loop detail's Runs table, and Open in Launch's Sessions becomes a link to `#run/<id>`.
On a wide screen the page opens as a panel to the right of the list, which keeps its scroll position and highlights the selected row; on a narrow screen it replaces the list.
Back and Escape close it and return to the row, and previous and next buttons step through the list as it is filtered.
Loops get the same treatment at `#loop/<name>`, and tickets at `#ticket/<repo>/<number>` (recommendation 6).

**Why.**
A split view shows a list and its detail at once and keeps the selection highlighted ([Apple HIG, Split views](https://developer.apple.com/design/human-interface-guidelines/split-views)), and supplemental information should not take users away from the primary screen ([NN/g, Complex Applications](https://www.nngroup.com/articles/complex-application-design/)).
State belongs in the URL so a link or reload restores it ([Primer, View](https://primer.style/product/scenario-patterns/view/)), and Back should act as Close and return users to the page as they left it ([NN/g, User Control and Freedom](https://www.nngroup.com/articles/user-control-and-freedom/), [GOV.UK, Back link](https://design-system.service.gov.uk/components/back-link/)).
Langfuse, LangSmith, Buildkite and Trigger.dev all open run detail beside the list or step between runs from it ([Langfuse](https://langfuse.com/changelog/2025-03-21-table-peek-view), [LangSmith](https://docs.langchain.com/langsmith/view-traces), [Buildkite](https://buildkite.com/resources/changelog/266-introducing-the-new-build-page-engineered-for-scale-and-flexibility/), [Trigger.dev](https://trigger.dev/changelog/move-between-adjacent-runs)), and users of GitHub Actions and Airflow complain when detail loses their place or buries the action ([Hacker News](https://news.ycombinator.com/item?id=46914114), [Airflow issue 67472](https://github.com/apache/airflow/issues/67472), [GitHub discussion 195854](https://github.com/orgs/community/discussions/195854)).

**Effect.**
Opening a past run's log no longer jumps the page from y = 5,997 to y = 642, and closing it puts you back on the row.
Opening a past loop run's feed goes from 3 clicks with a 1,650 px jump to 2 clicks (the loop, then the run) with no scrolling.
Returning to an earlier session goes from 2 clicks and about 1,100 px to 2 clicks and 0 px.
Any run, loop or ticket can be linked, bookmarked and reloaded, and the three viewers of a run become one.

### 4. Add one Runs view with filters, and keep Live short

**Change.**
Add a `#runs` view with every run of every kind, newest first, running runs pinned to the top, and "Show 25 more" at the bottom.
A `.seg` filters by status (Running, Failed, All) and another by kind; pickers narrow by repo and loop; the filters live in the URL, for example `#runs?kind=loop&status=failed`.
`state.runs()` in `arete/state.py` already takes `kind`, `repo` and `loop`.
Each row shows status, kind, repo, loop or ticket, age, duration and cost, so most runs need no opening.
Runs that start while the list is open gather behind one line, "3 new runs, show them", instead of pushing rows down under the pointer.
Live's 05 Recent runs moves here, so Live holds only 01 Needs you and 02 Running now.

**Why.**
Devin folded its separate PR view back into one list of sessions ([Devin release notes 2025](https://docs.devin.ai/release-notes/2025)), and a Copilot user could not find "the status of the coding agent in one shot" ([GitHub discussion 168291](https://github.com/orgs/community/discussions/168291)).
Temporal keeps filters in the URL and ships a saved "Task Failures" view ([Temporal docs](https://docs.temporal.io/web-ui)), and Trigger.dev added filters to narrow runs ([Trigger.dev changelog](https://trigger.dev/changelog/new-run-filters)).
Filtering one set of content is the job of a segmented control ([Carbon, Content switcher](https://carbondesignsystem.com/components/content-switcher/usage/)), and rows rich enough to read avoid pogo sticking ([NN/g, List Entries](https://www.nngroup.com/articles/list-entries/)).
A banner for new runs is Trigger.dev's answer to lists that move under the pointer ([Trigger.dev changelog](https://trigger.dev/changelog/live-runs-page)), which made an n8n user stop the wrong run ([n8n community](https://community.n8n.io/t/difficult-to-stop-processes-on-executions-tab-due-to-constant-resorting/300850)).

**Effect.**
Finding a past run by kind, repo or status goes from scrolling a fixed list of 25 (up to 5,500 px) to 1 or 2 filter clicks, and runs older than the last 25 become reachable.
Live drops from 6,430 px to the height of its two sections, which on the day measured is about one screen (an estimate).

### 5. Name views after jobs, and put each view's main object first

**Change.**
- Launch becomes Sessions: the list of sessions is the page, with a "New session" button at its top that opens today's form above the list.
- Fleet merges into Health, and Health opens with open alerts and anything failing (today 04 Alerts at y = 2,485), then serve, devices, checks and probes.
- Health's "02 Loops" heading becomes "Self-reporting loops", so "Loops" names one place.
- Trends becomes Costs, with "Where it went" (by model, runner and loop, today 05 at y = 2,169) first and cost per day, outcomes and evals after it.
- In Loops, "03 Draft a loop" becomes a "New loop" button at the top of the view that opens the form there, and Open shows the loop beside the list at `#loop/<name>` (recommendation 3) instead of at y = 2,753 under the form.

**Why.**
A label has to predict what is behind it ([NN/g, Information Scent](https://www.nngroup.com/articles/information-scent/)), and each category needs a unique identity ([NN/g, Confusing Category Names](https://www.nngroup.com/articles/category-names-suck/)).
Navigation names places, and actions live on buttons ([Apple HIG, Tab bars](https://developer.apple.com/design/human-interface-guidelines/tab-bars)).
System-level status belongs in the app frame, not in a peer view ([Carbon, UI shell header](https://carbondesignsystem.com/components/UI-shell-header/usage/)), and users object when a number they watch all the time moves deeper ([Vercel community](https://community.vercel.com/t/credit-balance-removed-from-profile-dropdown/31157)).
The most important content goes at the top ([Carbon, Dashboards](https://carbondesignsystem.com/data-visualization/dashboards/)).

**Effect.**
Returning to a session: about 1,100 px to 0.
Seeing where the money went: about 1,700 px to 0.
Checking health: alerts move from y = 2,485 to the top of Health, and serve's state is in the sidebar on every view, so 0 clicks.
Drafting a loop: about 1,800 px to 0.
Opening a loop: the detail appears beside the row instead of 1,650 px below it.

### 6. Turn Triage's six stacked groups into one filtered list with ticket pages

**Change.**
In `triageParts`, the six stacked `h2` groups become one `.seg` with counts (for example "With questions 21", "Needs a person 11", "Research 11", "Sent 8"), opening on the first group with something to act on, with the choice kept in the URL (`#triage?group=questions`).
Each ticket row links to `#ticket/<repo>/<number>`, a full-width page with the research report, the questions form and Send to the pipeline.
That replaces the 420 px report box inside a table cell and most of the 75 folds.

**Why.**
Distinct groupings of the same content suit a switcher, and a single long page suits content without them ([NN/g, Tabs, Used Right](https://www.nngroup.com/articles/tabs-used-right/), [Carbon, Content switcher](https://carbondesignsystem.com/components/content-switcher/usage/)); six segments is inside Apple's limit for a wide interface ([Apple HIG, Segmented controls](https://developer.apple.com/design/human-interface-guidelines/segmented-controls)).
More than two levels of disclosure hurts usability ([NN/g, Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/)), expanding fold after fold is tedious ([NN/g, Accordions on Desktop](https://www.nngroup.com/articles/accordions-on-desktop/)), and content too big for a small container deserves a full page ([Carbon, Dialogs](https://carbondesignsystem.com/patterns/dialog-pattern/)).

**Effect.**
Triage drops from 8,478 px to the length of one group.
Reading a research report goes from 2 clicks, about 1,400 px and a 420 px box to 2 or 3 clicks, 0 px and the full width.
Answering a ticket's questions goes from 3 clicks and about 4,500 px to 3 clicks and 0 px from Triage, or 2 clicks from Needs you.

### 7. Drop empty sections and add in-page links to long views

**Change.**
A section with nothing to show renders as one line under the section before it ("No alerts."), not as a numbered `h2` with its 94 px of spacing.
Views still longer than about two screens (Health, Costs, the loop detail) get a one-line list of links to their sections under the first heading.

**Why.**
Attention falls off below the first screen ([NN/g, Scrolling and Attention](https://www.nngroup.com/articles/scrolling-and-attention/)), in-page links cut scrolling ([NN/g, In-Page Links](https://www.nngroup.com/articles/in-page-links-content-navigation/)), and a contents list has to be visible to be used ([NN/g, Table of Contents](https://www.nngroup.com/articles/table-of-contents/)).
Users of GitHub's new home objected to padding and space spent on little ([GitHub discussion 177902](https://github.com/orgs/community/discussions/177902#discussioncomment-14806984)).

**Effect.**
About 170 px saved per empty section, 520 px on Live on the day measured, and any section of a long view is 1 click away.

### 8. Add a Cmd+K palette and go-to keys after the sidebar exists

**Change.**
Cmd+K (Ctrl+K) opens a box that matches, by name, the sidebar's views with the same labels, loops, recent runs, sessions and tickets, and actions such as New session, New loop, Run the checks now and Refresh triage now.
Add a few two-key go-to shortcuts for the most used views, such as `g l` for Live and `g r` for Runs.

**Why.**
Accelerators help a daily expert user and "should be readily available, yet easy to ignore" ([NN/g, UI Accelerators](https://www.nngroup.com/articles/ui-accelerators/)); a palette that lists matches relies on recognition, not recall ([NN/g, Recognition and Recall](https://www.nngroup.com/articles/recognition-and-recall/)); keep custom shortcuts to the most used commands ([Apple HIG, Keyboards](https://developer.apple.com/design/human-interface-guidelines/keyboards)).
Devin's palette mirrors its sidebar ([Devin release notes 2026](https://docs.devin.ai/release-notes/2026)) and Linear's go-to keys open its Inbox from anywhere ([Linear Inbox](https://linear.app/docs/inbox)), but users with a hand on the mouse still need one-click paths ([Grafana discussion 58910](https://github.com/grafana/grafana/discussions/58910#discussioncomment-5744257)).

**Effect.**
Any view or named object is 2 to 4 keystrokes away with no scrolling.
It does nothing for a phone or a mouse, which is why it comes last.

### Expected effect on the top tasks

The "after" column is an estimate for a design not yet built, measured from Live with recommendations 1 to 7 in place.

| Task | Today | After |
|------|-------|-------|
| See what waits for my approval | 1 click, if you know to look in Loops | 0 clicks; top of the landing view, count in the sidebar |
| See everything that waits on me | 4 to 5 view switches, about 6,000 px | 0 clicks, about one screen |
| Switch views from the bottom of a long page | scroll back up, up to 8,478 px | 1 click, 0 px |
| Open the log of a past pipeline run | 1 click, up to 5,500 px, list position lost | 2 clicks (Runs, the row), a filter instead of scrolling, list keeps its place |
| Open the feed of a past loop run | 3 clicks, a 1,650 px jump | 2 clicks, 0 px |
| Return to an earlier session | 2 clicks, about 1,100 px | 2 clicks, 0 px |
| Check what this week cost and where it went | 1 click, about 1,700 px | 1 click, 0 px |
| Read a research report | 2 clicks, about 1,400 px, a 420 px box | 1 click from Needs you or 3 from Triage, 0 px, full width |
| Answer a ticket's questions and send it | 3 clicks, about 4,500 px | 2 clicks from Needs you, 0 px |
| Check that serve and the machine are healthy | 1 to 2 clicks, alerts at y = 2,485 | 0 clicks for the status line, 1 click with alerts on top |
| Draft a new loop | 2 clicks, about 1,800 px | 2 clicks, 0 px |

## Open questions

Only Justin can answer these, and the answers change the order or shape of the recommendations above.

1. Where do you use the dashboard most: a large screen, a laptop or a phone?
   This decides how the sidebar collapses and whether the command palette is worth building.
2. Which five of the 15 tasks in the audit do you do most often?
   The sidebar and each view should be ordered by that ([NN/g](https://www.nngroup.com/articles/ia-questions-navigation-menus/)).
3. Should the landing view put Needs you or Running now first, and do you want to choose your landing view?
   GitHub users asked to pick their own default view ([GitHub discussion 177902](https://github.com/orgs/community/discussions/177902#discussioncomment-14807322)).
4. Which items count as "needs you"?
   In particular: tickets that Needs a person, research reports, and review feedback.
5. Should Chat be a view, or a panel you can open from any view, as Railway did with its agent panel ([Railway changelog](https://railway.com/changelog/2026-03-06-domains-ui-layout))?
6. Do the group names Work, Watch and Settings match how you think about Arete?
   Sorting the features into groups yourself, then finding each of the 15 tasks in the proposed sidebar, would check it before building ([NN/g, Card Sorting](https://www.nngroup.com/articles/card-sorting-definition/), [NN/g, Tree Testing](https://www.nngroup.com/articles/tree-testing/)).
7. Have you ever approved a push or stopped a run by mistake?
   If so, the buttons should name their outcome (for example "Push to main") and offer undo where possible; if not, keep them as one click ([NN/g, Confirmation Dialogs](https://www.nngroup.com/articles/confirmation-dialog/)).
8. Do you read Trends for outcomes and evals, or mostly for cost?
   This decides whether the view is renamed Costs or keeps its name with costs moved to the top.
