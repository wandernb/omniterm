# Changelog

All notable changes to OmniTerm are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Tabs, Sublime Text style, above the window's content. Projects, folders, agents, runs, sessions, Inbox, Memory, Settings, Unblocker, search results and terminals all open as tabs. A plain click shows a page in an italic preview tab that changes in place until you click, scroll or type in it. Terminals and double-clicks open permanent tabs, and Command-click (or Command-Return) opens a tab in the background, wherever you click. Command-N opens a new empty terminal tab (New Agent moves to Option-Command-N). Command-W closes the tab: an agent's run tab only leaves the bar and the run keeps going. Shift-Command-T reopens the last closed tab. Command-1 to 9, Option-Command-Left/Right, Shift-Command-[ ] and Control-Tab (most recently used) switch tabs, Command-P opens the navigator, and tabs drag to reorder. Two tabs with the same name show where each comes from.
- Split the window into groups, as Sublime Text does, each with its own tabs. Command-D adds a column and Shift-Command-D a row, each opening a new terminal. Option-Command-1 to 5 and Option-Shift-Command-2/3 pick Single, Columns 2/3/4, Grid 4 or Rows 2/3. Control-1 to 4 focuses a group and Control-Shift-1 to 4 moves the tab there. Clicking in a group focuses it, and dividers drag. Terminals stay live in every group. Splitting a terminal into panes, as iTerm2 does, moves to Option-Command-D. View has Tabs, Layout, Groups, Focus Group and Move Tab to Group menus, and rows in the sidebar get Open in New Tab and Open in Group.
- Your tabs, groups and layout come back after a relaunch.
- Every tab and group key can be rebound in Settings › Shortcuts (Sublime Text's keymap format, `keymap` in settings), and the View menus show your keys. View › Tabs can hide the tab bar and the status bar.
- A status bar along the bottom of the window. It shows the sidebar toggle and, for the selected tab, the git branch with staged and unstaged counts, the folder, a run's agent and state, and on the right the branch with its change count, the project, the provider and model (⚠ for dangerous permissions), the terminal size, how many agents run and Inbox unread. The branch and project open the project, the run count opens the Dashboard and the unread count opens the Inbox.

### Changed

- A project folder reached through a symlink is now one project: when a checkout links into another project's folder (`app/@head` linking into `.dev/components/app`), the two merge into the project that holds the checkouts, with its name, and their sessions, agents and lookups all land on it.
- When a /goal or /loop session can only move with you (only your decision is left), or the Unblocker has found nothing to send three rounds in a row, it now ends that goal: it tells the session once to stop its loop and summarize what is done, what is blocked and the exact command you need, then OmniTerm's Stop hook ends the session's later turns, which pauses the goal until you type in that session. Released goals no longer reopen on Claude's idle notification, which had made the Unblocker re-send its release message every few minutes. A session waiting only on checks you would do by hand is no longer treated as blocked: the Unblocker tells it to finish the remaining work or report the goal complete, with those checks listed as not checked live. Once the session is idle, OmniTerm then types `/goal clear` into its iTerm2 or OmniTerm tab, which is the only way Claude Code clears a goal. macOS asks once to let OmniTerm control iTerm2. The Unblocker page shows ended goals as Released for you. Every Claude and Codex session on the machine now also starts with your standing rule that checks only you can do by hand never hold a goal: work keeps going, and with only those checks left the goal is complete.
- The project page's Code tab opens on what matters: the project's numbers at a glance, Start here questions (what breaks if you change its most-used definition, how two groups connect, what holds the largest group together, why two files import each other), surprising connections between groups of files, and a How do two parts connect? finder that shows the chain of calls, imports and uses step by step. Most-used definitions name the group they belong to.
- The Memory graph is easier to read: hubs grow with their links and fade when nothing links to them, only the most connected hubs are labelled until you zoom in, and labels no longer sit on nodes and stand out on a backdrop.
- The Memory graph's controls sit in three tidy rows that adapt to the window: the view switch and highlight field, the filters, then the link legend.
- Memory Search shows how to search, examples and your recent searches before you type, suggests what to try when nothing matches, and searches as you type.
- Page tabs (a project's, Settings') scroll sideways when they do not fit, and the chosen tab's bold label no longer shifts the others.
- The sidebar's top group has room above Dashboard and below Settings, and its project and folder trees look like an editor's file tree: compact one-line rows, small solid triangles to open and close folders, every name in the same weight, a project's short time (21h, 2w, 3mo) muted on the right, and a flat selection across the full width. Your project and folder icons and colours are unchanged.

### Fixed

- Typing in a Markdown editor (the Unblocker's policy, an agent's prompts) keeps the cursor where you type; every key used to send it to the end of the text. Typing in the Unblocker's policy and in Settings' fields works again. The page refreshed every two seconds and took the keyboard away; it now waits while you type, and Return and the arrow keys stay in the field.
- An agent run or resumed session starting no longer moves the window to it. It opens in the background and shows in the sidebar; the page you are on stays put, also when OmniTerm relaunches and picks its running agents back up.
- Command-N opens New Agent instead of an arbitrary terminal tab. New Tab and the sidebar's + still open a terminal.
- The Omni Unblocker reaches every waiting goal session again. A few old sessions it had already checked used up every pass, so newer ones were never looked at. Waiting-session lists now keep the newest when they are trimmed.

## [0.10.0] - 2026-11-27

### Added

- A Code tab on every project's page. Search the project's definitions and files, see the definitions most files use, the groups of files that depend on each other and any circular imports, then open a definition to follow its callers, what it calls, what a change would affect and the memories that mention it. Each step opens the next, and a breadcrumb leads back.
- The Memory graph's new Code switch shows the source files your memories name, and double-clicking one opens it on its project's Code tab.
- Agents get a link with each answer that opens the definition on the Code tab, so they can hand it to you. `omni graph open NAME` does the same from a terminal, and `omni graph query --kind class` keeps one kind of definition.

### Fixed

- The code graph no longer matches names from application code into test files, which made test helpers look like the most-used code. Groups of related files under the same folder now have distinct names.

## [0.9.1] - 2026-11-27

### Fixed

- Indexing a very large project no longer pauses OmniTerm's knowledge worker. Listing a repository with tens of thousands of files took most of a second and held searches and the Memory page back; it now runs in the background, and a question about that project waits at most a second for it.
- The code graph never opens SSH keys, signing keys or certificates (`id_ed25519`, `.p8`, `.jks`, `~/.ssh`, `.gnupg`), including files without an extension.

## [0.9.0] - 2026-11-27

### Added

- A built-in code graph. OmniTerm now maps the source files of every project it knows (forge's projects and the git repositories under `~/Projects`) into modules, classes and functions, with their imports, calls, inheritance and type references, and keeps that map current in the background. Agents ask it through the `code_graph` MCP tool, and you can ask it with `omni graph`: where something is defined, who calls or imports it, what a change affects, how two parts connect, which memories mention it, and a report of hubs, clusters and import cycles. Python is parsed exactly. Ruby, JavaScript/TypeScript, Swift, Objective-C, Go and Rust are matched by pattern, and those guessed links are marked `inferred`. Only names, paths and line numbers are stored, locally. Env files, secrets and privacy-guarded folders are never read.

## [0.8.3] - 2026-11-27

### Fixed

- OmniTerm no longer quits when the pointer moves onto a button. The pointing-hand cursor added in 0.8.0 answered AppKit under the wrong method names, so entering a button in the active window crashed the app.

## [0.8.2] - 2026-11-26

### Fixed

- Opening and closing tabs no longer leaves memory behind. Every closed terminal left a notification registration, and every run or Inbox page left its text system and three hidden views. All are now released, so twenty open/close cycles end where they started.
- An indexing batch that cannot be saved now pauses new indexing until it saves, instead of producing more writes behind it.

## [0.8.1] - 2026-11-26

### Fixed

- The knowledge worker uses about 1.1 GB less memory on a large history. An index that stores its vectors compactly (int8) now keeps them that way in memory, and search converts them a block at a time. Results are unchanged, snapshots load faster, and a semantic search takes about 30 ms longer.

## [0.8.0] - 2026-11-26

### Added

- Right-click works across OmniTerm. Agents offer Open, Run Now, Edit, Rename, Pause or Resume Schedule, Show Terminal, Stop and Delete. Runs offer their report, terminal, Stop or Resume. Sessions offer Open, Rename, Resume or Show Terminal, Search Memory and Copy ID. Tickets offer Open, Status, Priority, Copy and Delete. Files, projects, worktrees and branches offer Reveal in Finder, Open with Default App and Copy Path. Inbox items offer Mark Read or Unread, Resume, Reveal and Delete. Memories offer Pin, Reinforce, Show in Graph and Restore, the memory graph offers Open, Expand, Center, Fit and Overview, and folders (Knowledge ones included), search results and Unblocker decisions have menus too. Everything clickable now shows the pointing-hand cursor.
- Projects have a Tickets tab: a backlog and board of tickets with a title, live Markdown content, a status (open, in progress, done, discarded) and a priority (urgent, short-term, middle-term, long-term). Show them as a list or a board, grouped by status or priority, filter by status or priority, pick the board’s columns and the row properties under Display, and work from the keyboard with Linear-style keys (C new, S status, P priority, ⌘B list or board, G group, F filter, ⇧←→ move a card).

### Fixed

- Indexing a large history no longer competes with your work. Each five-second pass scans once, saves at most a megabyte and starts no new work after half a second; embedding, memory upkeep and space reclaiming take turns after the scan. Bigger bursts run only while the Mac sits idle on power, never as catch-up after a wake. Under memory pressure indexing pauses and the knowledge worker frees its rebuildable caches, while session messages are still delivered. The worker holds no more than about 128 MB of unsaved writes. Unchanged records are no longer rewritten. Semantic-search snapshots load about three times faster with half the peak memory, and save without holding a second copy of the vectors. `knowledge_status` reports each pass under `maintenance`.
- Project pages no longer rebuild and re-measure their README every two seconds when nothing changed.
- A missing shell-history resource, for example in an app bundle replaced while running, now turns shell history off instead of crashing OmniTerm.
- OmniTerm no longer comes to the front by itself. Runs, sessions, restarts and startup can no longer activate it, raise its window or switch Spaces while you are in another app; only the shortcut, the menu bar, the Dock icon, a notification click or an omniterm:// link bring it forward.
- Agents no longer pull OmniTerm forward when they wake its scheduler, and new notifications no longer bring the Inbox window to your Space.

## [0.7.0] - 2026-11-25

### Added

- Account routers for Claude and Codex, part of OmniTerm: `claude` and
  `codex` start each session on the account with the most room. `--usage`,
  `--accounts`, `--add-account`, `--remove-account`, `--rename-account` and
  `--account NAME` work the same for both. Existing Claude accounts carry
  over without signing in again; the current Codex login becomes `main`.
- Settings › Accounts lists every Claude and Codex account with its plan,
  usage and running sessions, and adds, signs in again, renames, edits the
  email of, or removes an account. Refresh Usage and Install Routers sit
  there too.
- Installing OmniTerm installs the routers (`--skip-routers` to leave them
  out); a replaced command is kept as a dated backup.

### Changed

- Settings is split into tabs, as on a project page: General,
  Notifications, Agents, Memory and Accounts.
- When Codex fails or hits its usage limit, the Unblocker's quick check and
  the memory agent fall back to Claude Haiku; the audit and the run say so.

## [0.6.2] - 2026-11-25

### Fixed

- On a narrow window the Inbox's Mark as Read, Reveal Workspace, Resume Run
  and Delete buttons wrap onto a second line instead of running off the edge.
- A Memory page test no longer fails when it runs first, while the app is
  still starting.

## [0.6.1] - 2026-11-25

### Fixed

- A pinned session older than the loaded part of the Sessions list now shows
  under Pinned; it is fetched on its own.

## [0.6.0] - 2026-11-25

### Added

- Pin sessions (right-click a session, Pin): pinned sessions sit at the top
  of Sessions mode, in the order pinned.
- Double-click a session in the sidebar to rename it in place; the name is
  saved the provider's own way, as Rename… on its page does.
- The view each list uses shows on its section header (By Project, My
  Folders, Live, Recent, All) and switches from there, in Sessions and in
  Agents, as well as from the options menu.
- Project, Knowledge and Files pages render Markdown as GitHub and Obsidian
  read it: front matter folds into a Properties block, fenced code sits on
  shaded blocks with its language, quotes and Obsidian callouts are styled,
  tables are real tables, lists indent and number properly, task items show
  checkboxes, and image badges and raw HTML show their text. Other files
  show as code.
- `omniterm://project/<root>?file=<path>` opens a project on one file.

### Changed

- By project or in folders, live sessions sit inside their project or
  folder, first among their neighbours, instead of in a separate Live
  section; the Live, Recent, All view keeps them apart. In Agents by project,
  each agent's runs sit under it in its project.
- In Agents with My Folders, runs are grouped by their agent's folder.
- The Unblocker spends far fewer tokens: a keyword check decides first with
  no model call, an unsure turn goes to a cheap model (Claude Haiku, then a
  fast Codex model, each named explicitly, never the default), and only a
  suspected block reaches the full decision. A failing link is skipped and
  the next tried. Each audit entry records which step decided.
- The Unblocker page draws at once and fills in as the rest arrives, and asks
  only for the sessions it can answer.

### Fixed

- The Command-K navigator's search icon no longer overlaps its text.
- A sidebar row no longer stays highlighted after the pointer has left it.
- The Inbox toolbar no longer piles up on a narrow window: the filters
  shrink, then the least used ones step aside.
- A long file name no longer pushes a file's line count out of its header.

## [0.5.0] - 2026-11-25

### Added

- The sidebar has a third mode, Projects, first in the switch before Sessions
  and Agents (⇧⌘1, ⇧⌘2, ⇧⌘3; each segment has an icon):
  every Forge project, arranged in folders you make like agent folders.
  Folders collapse, projects and folders move by drag and drop or the Move to
  menu, arrow keys walk the list, and a double-click or Return renames a
  project or folder. The arrangement is saved in OmniTerm's settings; a
  project's new name is OmniTerm's alone.
- A project's page has tabs: Home (facts, worktrees and branches at a glance,
  recent sessions, README), Branches, Sessions, Knowledge, Files and Settings.
  ⌘1 to ⌘6 switch tabs. Env files, secrets and keys are listed but never
  opened.
- Agent folders, projects and project folders take an icon and colour with
  the same picker as agents (Icon & Colour…, Command-I, or a double-click on
  the icon). A project's look shows on its page, the Dashboard, the projects
  list and its heading in Sessions mode.
- OmniTerm bundles Forge inside the app (Contents/Resources/Forge), so the
  Projects features work on a Mac without Forge installed; an installed Forge
  is used first. Forge stays its own repository.

### Changed

- The sidebar's top pages (Dashboard, Inbox, Memory, Unblocker, Settings) are
  the same in every mode and sit on a lighter rounded panel. Projects left that
  list: it is the switch's first mode. In a narrow sidebar the switch shows
  only the selected mode's name, the others as icons.
- One search for everything: the sidebar's search field finds projects,
  sessions (by title and folder, and inside transcripts), agents, runs,
  terminals and memories in any mode, each under its own heading. It filters
  as you type; Return or See All Results opens every result on a page
  (`omniterm://search?q=…`).
- Command-K opens a quick navigator over the window: the pages, recent
  projects and sessions, and a search over everything (arrows, Return,
  Command-Return for all results; `omniterm://quicknav`). Command-F goes to the
  search field and Command-, to OmniTerm's Settings; these take the place of
  iTerm2's Find, Clear Buffer and Settings keys in the terminal window.
- The Unblocker's audit entries open in full over the page (what happened,
  the goal, the facts) with an Open Session button; the list shows each
  outcome's icon and no longer shows a cost. `omniterm://unblocker?audit=N`
  opens entry N.
- Sessions mode has three views, chosen from its options button: By Project
  (your project folders, then each project with its sessions, the rest under
  Other by folder), My Folders (folders and subfolders you make; file sessions
  with Move to or drag and drop, the rest under Other; saved as
  `session_folders`), and Live, Recent, All.
- Agents mode can show agents By Project (under their workspace's project,
  in your project folders, the rest under Other) or in your own folders as
  before.
- The All Projects page groups projects by your folders and subfolders, each
  under a heading with its icon, colour and count.
- The search options sit in a button right of the search field, in every
  mode: provider and grouping for sessions, new agent or folder for agents,
  view all, new folder and refresh for projects.
- The Dashboard shows recent projects and recent sessions above the agent runs,
  with an All Projects button.
- The Projects header has View All (the page listing every project) and a +
  that adds a folder.
- A project's page reads like a repository host: its icon and colour in the
  heading, tabs with icons and an underline, the README in a bordered file box,
  and Files and Knowledge as bordered folder listings (folders first, a
  breadcrumb of links, sizes and dates), opened files in a box above them.
- Parsed Markdown sits on a lighter rounded card: READMEs and files, run
  summaries and final messages, the memory digest, and the agent prompt and
  Unblocker policy editors.
- Sessions show Claude's and Codex's own marks, in subtle grey and at the same
  size, instead of generic symbols. Each session row names its project and
  carries the project's colour as a bar at its leading edge, and chat replies
  sit on the Markdown card.

### Fixed

- A project page opened just after launch no longer stays on “unknown
  project”: it asks again while the project list loads.
- OmniTerm no longer slows to a crawl after a few project actions: a long
  README was laid out in quadratic time on the main thread, using a full core
  and gigabytes of memory, and was laid out again on every page reload.
- The Projects list in the sidebar can no longer be hidden: nothing sits below
  it.
- The Projects list and sidebar show at once after a launch, from the last
  saved list, instead of waiting for the background worker.
- Sessions in Documents, Desktop or Downloads no longer make OmniTerm ask for
  access to those folders.
- A project's sessions are its own, not a filter of the newest sessions.
- Selecting a project folder in the sidebar no longer jumps back to the page
  shown, so the arrow keys and Return work on it.

- The Omni Unblocker no longer nudges a session that is waiting on its own
  running build, test, deploy or background task.
- The Omni Unblocker answers the way you would: it reads your own global
  agent instructions and a session's earlier replies, not only its last
  "still blocked" line, and settles a reversible choice inside the goal with
  the session's own recommendation instead of leaving it for you. What goes
  into a public repository or release always stays your call.

## [0.4.0] - 2026-11-25

### Fixed

- The Omni Unblocker now reaches sessions whose `/goal` or `/loop` hook keeps
  them going: those turns no longer restart the 45-second wait, so it can
  answer them, and its answer is no longer thrown away as out of date when
  the hook re-runs the turn. A session it left for you stays left for 30 minutes unless
  something new is refused, and it never tells a session to do what that
  project's own rules forbid. When the only next step is one it keeps for you, such as a
  permanent delete, it now points the session to a route that keeps the data
  instead of just stopping. A goal session it left for you, including one
  whose goal Claude Code paused, gets another look after 30 minutes, then
  after an hour, and so on.

### Added

- A Projects page in the sidebar lists every Forge project on the Mac, found
  by `forge projects`; a project's page shows its repository, worktrees,
  branches, recent sessions, knowledge folder, README and files. Sessions are
  grouped by project by default, with worktrees and `@head` checkouts counted
  as their project. Forge stays its own install; OmniTerm reads it through its
  command line.
- The Omni Unblocker reacts within seconds when a goal session's turn ends by
  asking for you ("Blocked", "needs you", "waiting for you", a `!` command).
  Other waiting turns get a fast check on a small model first, and the full
  decision runs only when that check sees someone is needed. Your own Claude
  hooks no longer run inside its calls.
- Pin the window (the pin beside the sidebar's mode switch, or **Pin Window**
  in the menu bar) to keep it on its own Space; it always reopens where you left
  it. Unpinned, the shortcut brings the window from another Space to yours.
  OmniTerm starts as a menu-bar app, so launching never flashes a Dock icon.
- Larger, cleaner Sessions search field in the sidebar.
- The Omni Unblocker now listens in the background instead of running as a
  scheduled agent, so it never shows up as an agent, run or tab, and a stuck
  call can no longer stop it for hours. It has its own **Unblocker** page in
  the sidebar: switch it on or off, pick its model, edit its policy and audit
  every decision and failure. When auto mode refused a command a goal needs,
  its go-ahead can pre-approve exactly that command, once, for 30 minutes,
  through a new `PreToolUse` hook (`omni hooks install` adds it).
- The Omni Unblocker only keeps goal-driven sessions moving: a `/goal`, a
  `/loop`, a goal command from your unblock policy, or a prompt that plainly
  asks for work to be finished. It leaves finished
  sessions, memos, proposals and new directions to you. On those sessions it
  can now allow permission prompts and answer `AskUserQuestion` dialogs that
  serve the goal, and it can authorize actions that auto mode denied. It never
  allows production, personal-file, secret or permanent-deletion actions.
- Messages from the unblocker and other sessions no longer show `&#x27;` and
  `&amp;` in Claude sessions.
- Sidebar modes. A switch at the top of the sidebar flips between Agents (the
  existing agents, runs and terminals) and Sessions, which lists every Claude
  Code and Codex session on the Mac, live or past, most recent first. Picking a
  session opens its transcript; Resume reopens it in an OmniTerm tab with the
  provider's own resume command, or shows its tab if it is already open there.
  Inbox, Memory and Settings appear in both modes. The window no longer starts
  the curses dashboard at launch and shows the sidebar's start page instead.
- Model fallback on usage limits. When a scheduled run's model or harness runs
  out of usage, the runner continues the task in the same tab on an equivalent
  model: the other harness first (Fable/Opus ↔ Astra, Sonnet ↔ Sol,
  Haiku ↔ Luna), then a same-tier model on the same harness when only one
  model family was limited. The run's reason names the fallback; a run with no
  equivalent left fails with the provider's limit message. A Codex turn refused
  for usage no longer counts as a finished turn.
- Settings page in the sidebar. It covers notifications, the global shortcut,
  login startup, window behaviour, defaults for new agents (provider, model and
  permission mode per provider, overlap), retention, time zone and run
  summaries. It replaces the separate OmniTerm Settings window, and the status
  menu's Settings item, `omniterm://settings`, now opens it. Changes save at
  once through the new `set_settings` scheduler operation, which validates
  every key and stores nothing if one is invalid.
- Configurable notifications. Mute OmniTerm for an hour, until tomorrow morning
  or until you unmute: from Settings, the status menu's Mute Notifications, or
  `omni mute [30m|2h|tomorrow]` / `omni unmute`. Choose, for each level (urgent,
  attention, info), whether it shows a banner, plays a sound and raises the
  menu-bar count. Each kind of notification can follow its level, stay in the
  Inbox only, or always banner; each agent can follow the rules, notify only
  when urgent, or stay in the Inbox. Everything still reaches the Inbox. While
  muted, the menu-bar icon is crossed out, and the terminal's own notifications
  (bell, idle and new-output alerts, triggers, and the notifications Claude and
  Codex ask for) are silenced too.
- Notifications record their kind: `agent`, `compaction`, `wrap_up`,
  `loop_continuation`, `storage` or `run_limit`. Existing notifications get
  one from their dedupe key. The Inbox shows the kind and can filter by it, and
  `notifications` takes a `kind` filter.
- `omni settings set KEY VALUE` changes one setting, using dots for nested
  keys (for example `notifications.levels.info.banner true`).
- `explain_memory` (MCP): why a memory exists and weighs what it does — its
  weight history from the journal with the expiry projection and the archival
  step due, evidence grouped by session, who formed it and why, UPDATE lineage
  both ways, stable topics and concepts, linked memories with a reason per link,
  and the weekly summaries that cite it. Read-only: explaining is not a use.
- Weekly project summaries, first slice: `weekly_summary_candidates` lists
  project weeks due a summary (that week's daily episodes and memories, with the
  memory IDs and evidence a summary cites), and `write_weekly_summary` stores
  the summary as an episodic memory tagged `weekly-summary`, with store-chosen
  evidence and a cap of six a day. The Omni Memory agent does not call them yet.
- Memory graph exploration: search a memory to focus its one- or two-hop
  neighbourhood (`memory_graph` `focus` and `hops`, at most 120 nodes), expand a
  node on double-click, a filter bar (type, workspace, time, minimum weight),
  tinted community regions in the overview, and a collapsible mind map whose
  open branches survive refreshes.
- Search query grammar for transcripts and memories: `type:`, `ws:`,
  `provider:`, `role:`, `concept:`, `session:`, `after:`/`before:`/`since:`,
  `archived:`, quoted phrases and `-exclusions`, with «»-marked snippets and
  "more like this" (`like`). The Search tab shows filters as removable chips
  and keeps recent searches.
- Semantic coverage: the embedder walks the person's own turns first, then the
  last 14 days, then the rest; it takes a larger budget when the Mac is idle on
  power. `knowledge_status` reports coverage, counted on a thread of its own.
- Memory archival and a storage report: memories fading and unused for 30 days
  are archived (restorable), and deleted 90 days later unless pinned or backed
  by three or more sources. A daily report sizes the knowledge database against
  a 30 GB budget (`configure_storage`); over budget, vectors of old, uncited
  assistant messages are dropped. Transcript text is never deleted.
- Knowledge compaction, owner-run: `scripts/compact-knowledge.py` migrates the
  database to schema v2 (one copy of message text, vectors as float32 blobs,
  incremental vacuum) after a backup is confirmed; `scripts/compact-rehearsal.py`
  rehearses it on a copy with integrity, row-count and search-parity checks.
  It also merges the full-text index segments and clears the filter sidecar of
  rows for deleted messages, and is safe to rerun on a version-2 index (14.7 GB
  to 12.7 GB on a live index). The app now sweeps those stale sidecar rows
  itself, so the sidecar no longer grows past the index.
- Knowledge schema v3 (same script): each message is stored once, in
  `messages`, with no `records` row kept beside it; a removed message travels
  as a `{"_deleted": true}` marker the worker sends only to a v3 index. A live
  index went from 12.0 GB to 10.0 GB (10.8 GB with the filter sidecar, from
  14.7 GB before the compaction work).
- Background topic naming and merges: stable memory topics, recomputed daily
  with ids carried over by overlap, and near-duplicate memory pairs (cosine
  ≥ 0.85) offered to the Omni Memory agent through `memory_topics`,
  `name_topic`, `merge_candidates` and `decide_merge`; a merge keeps both
  memories' evidence and archives the dropped one. Consolidation prompt v8.
- Memory eval abilities: `scripts/memory-eval.py` labels each golden query with
  a LongMemEval ability and reports nDCG@10 and recall@20 per ability, abstention
  precision and recall both in-sample and held out (the threshold is fitted on one
  half of the queries, split by a stable hash of the query id, and scored on the
  other), the judged share of each top 10 and
  failure tags (coverage, ranking, stemming, synonym). Headline quality averages
  answerable queries only; results that quote two or more golden queries (the
  eval's own output, once indexed) and sessions in `exclude_sessions` are left
  out before judging and when re-scoring. `--frozen` scores against
  stored grades only; `--rescore` re-scores earlier reports against today's grades.

- Brainstorm memo: semantic data layers and long-term memory research applied to Omni memory
  (knowledge/brainstorms/@2026-11-11-02-brainstorm-semantic-layers-and-long-memory.md)
- Brainstorm memo: explorable memory graph, richer querying, background LLM and compact storage
  (knowledge/brainstorms/@2026-11-11-01-brainstorm-memory-graph-query-llm-storage.md)
- Brainstorm memo: semantic search and memory quality
  (knowledge/brainstorms/@2026-11-10-01-brainstorm-semantic-search-and-memory-quality.md)
- Memory retrieval eval: `scripts/memory-eval.py` grades search results against
  a private golden set with a cheap Codex judge (cached), and reports nDCG@10,
  pooled recall@20, MRR and latency per corpus and mode.
- Memory graph concept map: memories link through shared concepts (files,
  packages, commands, error codes, symbols, plan slugs), shown as diamond hubs
  with a Concepts toggle. The mind map gains a Topics branch built from concept
  communities.
- The memory consolidator now receives the owner's corrections, decisions and
  preferences as candidates. Secrets are masked and text capped. Each candidate
  carries its most similar existing memories (semantic neighbours first), and the
  consolidator answers it with ADD, UPDATE or NOOP. An UPDATE keeps the memory's
  evidence (`upsert_memory` `keep_sources`), so repeated statements reinforce it.
- The memory digest opens with a Topics section built from concept communities.
- Knowledge-base memory: every project's `knowledge/` folder (all of
  `~/Projects`, `@head` worktrees only, plus the shared Claude knowledge
  and any folders listed in `knowledge-roots`) is indexed as `kb:` sessions of searchable heading sections, and
  each file becomes one source-linked memory tagged with its project, revised
  on change and deleted with the file. Live: 30 knowledge bases, 2,436 files.

- Daily session episodes: each project's day of sessions becomes one episodic
  memory listing every session's request and outcome, linked to both records.

- The Memory page has an Ingest tab listing the background memory agent's
  runs (result, summary, schedule) with Open Run and Run Now. Those runs no
  longer appear in the sidebar's Runs, its Terminals or the Overview; the
  agent itself stays under Agents.

- The Memory page's graph groups memories by project: workspace hubs are on by
  default and a worktree checkout is named after its repository.

- Stable-identity deduplication for Codex histories indexed from both the
  JSONL rollout and the provider database: `get_session`, lexical and
  semantic search and memory candidates show a record once, keyed by session,
  rollout position, part, kind and text; `get_session` takes
  `include_mirrors: true` for both copies' provenance. An additive
  `message_positions` index serves the lookup; no data is rewritten.

- `knowledge_status` reports corpus identity: `mirrored_sessions` (sessions
  with both a JSONL and a provider-database source), `stale_native_sources`
  (native projections waiting to be revisited) and `orphan_threads` (provider
  history with no catalog row, never resurrected; counted read-only, cached
  for half an hour, retried after a minute on failure). The Memory page's
  Process tab shows them as an Identity stage. Memory candidates skip a
  message mirrored by another source of the same session, so the consolidator
  never sees one conclusion twice.

- A Memory page in the sidebar, modelled on a hippocampus. Browse lists
  durable memories by type and decayed weight with their evidence excerpts,
  revisions, related memories and journal, and can pin, reinforce or retype
  one; Search spans memories and transcripts (lexical, semantic, hybrid);
  Process shows the transcript → embedding → candidate → memory pipeline with
  live counts and the consolidation journal; Digest renders a per-workspace
  mid-term summary; Graph draws memories as a force-directed network (links
  by shared session, tag, lineage, similarity, workspace) or a radial mind
  map (type → tag → memory), highlighting what consolidation just touched.
- Hippocampus memory model: seven memory types with base weights and daily
  decay, evidence counts, lineage and pinning; weight decays from the last
  reinforcement and memories below 0.5 become expiry candidates, never silent
  deletions. A bounded journal (`metadata:memory_event:`) records candidates
  offered, memories created, revised, adjusted or deleted, dismissals, digest
  regenerations and ten-minute ingestion snapshots, with the run and agent
  that made each change. New knowledge operations and MCP tools:
  `list_memories`, `get_memory`, `memory_events`, `memory_overview`,
  `memory_graph`, `memory_digest`, `update_memory` and
  `dismiss_memory_candidates`; the all-workspaces digest regenerates itself at
  most hourly when memories change; `upsert_memory` accepts `memory_type`,
  `weight`, `lineage`, `pinned` and `reason`.

- `omni folders set PATH… [--json]` replaces the configured sidebar folder
  list, including empty folders, while preserving agent memberships.
- An offline outcome-review HTML prototype with three explicitly synthetic
  outcomes. It has no native integration, execution or persistence; browser,
  mobile keyboard and assistive-technology acceptance remain unverified.
- Source implementation of unified local Claude/Codex session discovery,
  paginated transcript and tool-output retrieval, lexical/semantic/hybrid
  search, provider-channel messaging with idempotent durable receipts, and
  revisioned memories with validated source references. Current native Codex
  database history (`native` and `paginated` modes), JSONL, subagents and archived
  sessions are indexed. Database-backed file diffs, collaboration results and
  MCP errors are included; mirrored JSONL/database records retain their distinct
  source references pending stable-identity deduplication. Codex transcript
  pages interleave those sources by rollout position without dropping repeated
  messages or separating a record's chunks.
  Native web-search results and hook output are searchable. Generated images
  retain their revised prompt, failure text and artifact path while encoded
  image bytes stay out of transcript and vector indexes. Existing native
  projections refresh incrementally without changing provider histories.
  Delivery evidence requires the exact attributed request and UUID recorded as
  user input in the recipient's transcript; it does not claim model processing.
- An app-supervised knowledge worker with native-only SQLite persistence,
  continuous ingestion, read-only persistent projections and local macOS
  embeddings with an explicit English word-vector fallback. Model identity
  and inference availability are reported; no model download is introduced.
  Bounded streaming supports records above 8 MiB across restarts, and source
  audits reconcile interior rewrites and deleted provider items. Active
  transcripts and pending message receipts receive bounded indexing priority
  while older history continues; transcript pages flag incomplete indexing.
  Metadata discovery advances independently of transcript parsing, so large
  earlier files no longer delay cataloging later sessions.
  JSONL discovery, normal ingestion and native catalog traversal also keep
  durable positions. Worker restarts no longer put later histories behind
  the same earlier files; queued append work survives and backlog counts
  include pending histories outside the bounded in-memory queue.
  Native background catch-up visits multiple histories within shared byte,
  item and source limits, so tiny or empty histories do not consume whole
  scan cycles. Requested-session priority and restart checkpoints remain bounded.
  Native streams reserve publication and cleanup capacity so sustained JSONL
  work cannot consume their entire allowance. Status counts use identity indexes
  while preserving pending additions, replacements and deletions.
  Native-history and vector scan positions survive worker restarts. Vector
  catch-up inspects already indexed records separately from its inference
  budget, while unfinished records remain eligible for processing.
  Reading an incomplete session requests a bounded indexing pass. Requested
  history, live delivery evidence and ordinary history share fair scan slots;
  database-only recipients also receive delivery-evidence priority.
- Opt-in memory consolidation through the owned 15-minute **Omni Memory**
  agent, configured with MCP `configure_memory` or
  `omni knowledge memory install`. Intake is bounded, source-linked, deduplicated
  and excludes the consolidator's own sessions. The canonical local app has
  passed build, packaging, activation and ad-hoc signature verification;
  helper coverage and all five opt-in IPC cases passed. Disposable provider
  exchanges, durable receipt recovery and replay safety passed. Local synthetic
  memory revision, retrieval and deletion passed; scheduled memory inference
  remains under verification.

- Loop agent configuration for one persistent run/session, idle prompt
  repetition, coalesced busy ticks, soft work budgets, and idle-only compaction.
  Native editor, CLI and MCP controls and provider-channel wrap-up reminders
  are implemented. Human follow-ups remain visible after compaction, and
  Claude compaction output does not interrupt loop continuation. Live lifecycle
  verification remains partial, including Codex compaction compatibility.

- A sidebar on the left of the terminal window replaces the tab bar. It lists
  the dashboard, your agents, live runs by agent and every other terminal tab;
  click a row to show its terminal, or `+` to open a new terminal.

- Agent notifications. Agents call the new `notify_user` MCP tool, and scripts
  run `omni notify`, to raise a notification. The menu-bar item shows the number
  of unread notifications that need attention and lists them, and a new Inbox
  window shows every notification with the changed files and diff captured when
  it was sent. `omni notifications` and `omni notification` list and manage
  them from the terminal. Urgent notifications also show a macOS banner.
- An OmniTerm Inbox widget for Notification Center, in small and medium sizes,
  showing how many notifications need you and the newest ones. Clicking it
  opens the Inbox.
- The Omni dashboard has an Inbox button (`i`), shows the unread total, and
  counts notifications per agent and per run.
- The MCP server's instructions and a new `omni://guide` resource document
  notifications for every session, and runs started by OmniTerm get a short
  note naming the agent and run and saying when to notify.
- Runs export `OMNI_RUN_ID` and `OMNI_AGENT` to their agent.
- An `interrupted` run outcome, for a run whose runner disappeared before the
  agent finished its turn, such as during a restart.
- Agents can be renamed (`omni agent NAME rename NEW`, MCP `rename_agent`; the
  agent file, run history and notifications move with it), grouped into
  sections and subsections, and given an icon (an SF Symbol or one emoji) and
  one of sixteen named colours: red, rose, pink, orange, brown, yellow, lime,
  green, mint, teal, cyan, blue, indigo, purple, slate and gray
  (`omni agent NAME set --group/--icon/--color`, MCP
  `create_agent`/`update_agent`). `omni agents` lists them by section, in the
  sidebar's order (`omni agents order NAME…`).
- Sidebar folders open a folder page listing their subfolders and agents, with
  Rename, New Subfolder, New Agent Here and Delete Folder. The sidebar works
  from the keyboard: Control-Command-S focuses it, the arrow keys move through it
  and open and close folders, Return renames, Command-I edits the icon and
  colour, Option-Command-Up/Down reorders, Command-N and Shift-Command-N create
  an agent or a folder, and Tab returns to the page or terminal.
- `omni run RUN-ID --screen [--plain]` prints the run's last terminal screen and
  up to 2,000 lines of scrollback, saved with colours when a turn finishes and
  when the run's session ends (`run-screens/`, kept 30 days and for the 200
  newest runs). `omni run RUN-ID --tail [--lines N] [--json]` renders the end
  of the run's Claude or Codex session transcript the same way, for runs without
  a saved screen.
- Run reports. `omni run RUN-ID [--diff] [--json]` and the read-only MCP tool
  `get_run` show a run's AI summary, the agent's final message, branch, commits,
  changed files and pull request. When an agent finishes a turn, OmniTerm writes
  a short summary with a small Claude model (`summary_model`, default `haiku`)
  that has no tools and no MCP servers; `"summaries": false` in
  `~/.omni/settings.json` turns it off and shows the final message instead.
  The scheduler records each run's workspace, provider and starting commit, and
  when a turn finishes captures the commits made since, and one diff of
  everything that changed since the run started, committed or not. Run diffs
  are private files kept for 30 days and the 200 newest runs. `list_runs`
  filters by `agent`, `origin`, `limit` and `before`.

- Keyboard navigation through the main window and the Inbox. → or Tab in the
  sidebar goes into the page. There:
  - ↑ and ↓ move through a list, and the run report follows the selection;
  - ← and → move between buttons;
  - Return opens a row or presses a button;
  - Tab moves between the parts of the page;
  - Escape returns to the sidebar.
- Every panel can be resized and collapsed: the sidebar, an agent page's run
  list, the terminal band and a run's diff. Control-Command-1 to 4 show and hide
  them. A narrow window stacks the report under the run list.
- Panel sizes and the window's frame are kept in `~/.omni/settings.json` under
  `layout`, so the window reopens as it was left.
- The Inbox also opens as a page in the main window's sidebar.
- A keyboard map: Command-/ anywhere in the terminal window, or `?` from the
  sidebar or a page. In the OmniTerm window, Command-/ no longer finds the cursor.
- An agent's page shows its startup and system prompts in full, formatted as
  Markdown, with **Show more** for long ones.
- The icon and colour picker (Command-I, or a double-click on a sidebar icon)
  offers about 340 SF Symbols in categories and an Emoji tab of common emoji.
  Typing in its search field filters both as you go, by name and keywords;
  for example, "bug" finds the ant and the ladybugs. ↓ moves into the grid,
  the arrows move through it, and Return picks. `omni agents appearance --json`
  lists each icon with its category and keywords.
- The agent editor's prompt fields are Markdown editors that format as you type:
  - the syntax stays visible, dimmed;
  - Command-B, Command-I and Command-K add bold, italic and links;
  - Return continues a list;
  - Tab indents list items.

### Changed

- The knowledge index stores one copy of transcript text and vectors as binary,
  and returns freed space as it goes. Vectors are stored as int8. A live 26 GB
  index became 9.6 GB. Tool output is kept as its first 1,500 and last 500
  characters, for new transcripts and, through a background pass, for ones
  already indexed; the full output stays in the transcript file.
- Each turn a loop agent finishes now appears as its own run in history, with
  its own final message, summary, diff, screen and notifications. The loop
  still keeps one live run, tab and provider session. Its agent page lists
  every turn; the sidebar, dashboard and folder pages show its two newest.
- Memory, search and other knowledge reads no longer wait for indexing to save
  its writes: they go straight to the knowledge worker and leave the writes
  for the indexing tick, which also stops between batches when a request
  waits. `knowledge_status` uses counts kept warm in steps instead of counting
  millions of messages and vectors while every other request waits, and a scan
  looks for pending transcripts only once its last batch is used up and the
  last scan counted some. The vector snapshot is saved every four hours rather
  than every 30 minutes, since each save rewrites hundreds of megabytes, and is
  restored in stages after a launch. Scans stop early when a request is
  waiting. On a 26 GB
  index a Memory request went from about 6 s to under 100 ms.
- Pages ask the scheduler for every live run plus recent foreground runs
  (`list_runs` takes `include_live`, `foreground` and `loop_turns`), and the Settings page
  reads login-startup status off the main thread, once per refresh. Live-run
  queries use a new index on the run state.
- New agents start from the Settings defaults everywhere: the app's editor,
  `omni agent add` and MCP `create_agent`. MCP `create_agent` no longer needs a
  `provider`. The CLI's fallback provider changes from Codex to the default,
  which is Claude unless you change it in Settings.
- The menu bar, widget and sidebar count the notifications your settings
  count (by default attention and urgent, as before). Banners follow the same
  rules: by default only urgent ones, as before. An urgent notification beyond
  the newest eight is no longer missed.
- Meaning-based (semantic) search ranks better without changing the embedding model.
  A memory's title gets a vector of its own: the local sentence model ranks short
  titles far better than long bodies. Memories embedded earlier are re-embedded once,
  and the graph's similarity and the merge sweep still average the body only. Memory
  scores are mean-centred against the corpus, so text that resembles everything
  stops outranking the specific match; intake's duplicate check keeps raw cosine.
  Turns the harness writes (Codex approval reviews and their verdicts, system
  reminders, hook feedback, skill and command wrappers) are no longer embedded.
  They were a third of all transcript vectors, and their near-identical short chunks
  topped every semantic search. They stay lexically searchable, and vectors embedded
  before the rule are skipped at query time and pruned in the background.
- Release builds keep the build machine's folders out of the binaries.
  `scripts/release-paths.sh` maps the project and home folders out of compiled-in
  file names (C/Objective-C `__FILE__`, Swift 5 `#file`, debug info), builds Metal
  shaders without embedded sources, and strips the debug map before signing.
  Releases up to 0.3.1 still name the builder's home folder.
- Extra knowledge folders are listed in
  `~/Library/Application Support/OmniTerm/knowledge-roots` (`label=path` per
  line) instead of a built-in path; only `~/Projects` and `~/.claude/knowledge`
  are read by default.
- The contact address is now `dev at wnbx dot net`, and the documentation
  screenshots no longer show a real home folder or carry editing metadata.
- The memory consolidator agent no longer has a sidebar row and is not
  counted in folders or on the Dashboard. The Memory page's Ingest tab lists
  its runs, and its new Open Agent button opens the agent's page to pause or
  edit it. Reordering the sidebar keeps its saved place. `omni agents` and MCP
  `list_agents` still list it.
- The keyboard map (`?` or Command-/) lists what Return does on each kind
  of sidebar row: rename an agent or folder, focus a run's or a terminal's
  terminal (a finished run's page instead), and go into the Dashboard, Inbox
  or Memory page.
- Lexical search ranks inside the full-text index and reads only the ranked
  rows, widening the window when filters reject rows, and marks snippets from
  the returned rows' text instead of FTS5 `snippet()` (0.3 s saved per search). When fewer than three messages or memories match
  every word, matches of any word follow, marked `partial_match`.
- The graph re-lays itself out only when its memories, links or half-point
  weights change; a refresh warm-starts from the previous positions, and large
  graphs use Barnes–Hut repulsion, culled edges and cached edge drawing.
- Memory search keeps lexical as its default mode, now judged by the frozen eval:
  with any-word relaxation, lexical scores nDCG@10 0.642 on answerable memory
  queries against hybrid's 0.595 (0.232 against 0.313 before relaxation, on the
  same grades). Stopwords (English and French) are left out of searches whenever
  another word remains, and the any-word pass stops after 150 ms.
- Filtered transcript search tests ranked messages on `knowledge-filters.sqlite`,
  a derived copy of each message's filter columns beside the index (about 350 MB
  for 1.6M messages), filled 4,000 rows per tick and rebuilt when the index is
  replaced; the rows returned are checked against the index again. A cold
  `provider:` filter step went from 1.5 s to 0.04 s.
- Ranked rows are read by rowid in a fixed join order, so a `provider:` filter
  no longer walks the scope index over every message of that provider (2 s cold
  before, 0.1 s after). Semantic transcript search rejects filtered candidates on
  the sidecar instead of reading each message, and `type:` with a memory type
  (`procedural`, `decision`…) on transcripts answers at once with no matches.
- The idle embedding burst runs as 0.25 s slices with a check for waiting
  searches between them (`embed` `slice`/`followup`, `slices` in its answer), so
  a search waits for at most one slice (0.5–0.85 s measured) instead of the
  whole burst (about 2 s or more).
- Scoped memory search (`ws:`, `provider:`, `role:`) judges scope on a copy of
  cited messages' scope columns, warmed a few batches per tick after launch:
  30 s for `provider:codex` before, about 0.2 s after.
- Worktree checkouts of one repository (`repo/@head`, `repo-wt/<name>`,
  `repo/.claude/worktrees/<name>`) share one workspace hub in the memory graph,
  and the graph's workspace filter matches all of them.
- Memory candidate batches stay under about 24 KB and topic lists under about
  16 KB, so agent harnesses show them whole; the rest wait for the next call.

- Semantic and hybrid search are exact and fast. The knowledge worker holds
  every stored vector in memory, scores them all with Accelerate, and loads them
  in the background after launch (results say `vectors_warming` until done).
  Searches no longer embed on the query path. The loaded vectors are saved as a
  snapshot beside the index, so a relaunch warms in seconds.
- Hybrid search fuses lexical and semantic rankings with Reciprocal Rank Fusion
  and lets near-duplicate results give way (MMR).
- Tool calls and tool output are no longer vector-indexed; lexical search still
  finds them, and existing vectors for them are pruned in bounded sweeps.
- Repeated chunks embed once, and background embedding runs for a time budget
  per tick, memories first, instead of one chunk per tick.
- The Omni Memory consolidator runs on Codex `gpt-6-luna` at low reasoning
  effort by default (`configure_memory` takes `model`) and works through up to
  three batches of 20 candidates per run.

- Memory reads reuse enriched memories and re-read only those that changed:
  with 2,600 memories, list, graph and overview answer in 15–75 ms warm
  instead of 0.5–0.8 s, and the background tick keeps them warm.

- Transcript indexing takes up to 4 MB per scan but stops after about 0.8 s
  and embeds at most 4 chunks; while a backlog remains and no request waits,
  a tick runs up to four such scans. A request waits for at most one scan, and
  a multi-gigabyte backlog drains at about 70 MB a minute instead of 12–19.
  Stream cleanup removes up to 4,000 small staging documents per scan instead
  of 200, which had left a few hundred reindexing transcripts stalled.
  Records up to 1 MB are parsed in place instead of going through the
  incremental stream parser above 64 KB; a 1 GB transcript of ~120 KB tool
  results had advanced about 60 KB per scan.
- The memory consolidator's candidates are the newest unconsidered records,
  walked backwards by row instead of sorted by timestamp (313 s → 0.09 s on a
  million-message index), and the pending count stops at 500 ("500+").
- The worker's startup probe and the Memory page no longer count the whole
  corpus on the request path. Display counts (messages, vectors, pending and
  considered candidates) are counted exactly in bounded index steps by the
  background tick and refresh every 30 minutes; the page shows them as warming
  right after a launch.
- Memories store the workspaces of their evidence, so the Memory page's cache
  builds from the memory rows alone (0.65 s cold on 2,600 memories, was up to
  13 s); evidence availability is checked in the detail view. The graph keeps
  memory embeddings and pairwise similarity in memory, loads embeddings in
  slices off the request path and re-reads only changed memories.
- Embedding runs as its own worker request, and each Memory background step
  is bounded, so a page request waits for at most one short call. Calls over
  0.5 s are listed in `knowledge_status.slow_calls`. Live, 2,600 memories:
  overview, list, graph, mind map and journal p90 ≤ 0.1 s; first page load
  after a launch ≤ 0.6 s.

- The helper-test workflow selects upstream-update workflow changes on both push
  and pull request. Release-host documentation records the system Python
  prerequisite used by ad-hoc entitlement processing.
- OmniTerm shows in the Dock and Command-Tab while one of its windows is up.
  Command-Q and Command-W still only hide it.
- "Overview" is now "Dashboard", with an icon. Dashboard and Inbox sit at the
  top of the sidebar.
- The Inbox window has the main window's look: the same sidebar, cards, status
  chips and colours. A hairline divides the title bar from the content in both
  windows.
- Command-W closes the Inbox, and Control-Option-Space shows only the terminal,
  putting the Inbox away.
- The dashboard shows `completed` runs in green and `interrupted` runs in peach.
- `omni run NAME` is now `omni run RUN-ID`, the run report; an argument that is
  not a run ID still starts the named agent, as `omni agent NAME run` does.

### Fixed

- Session handoffs to Claude keep working across Claude Code updates that
  advertise the same peer protocol.
- Idle runs no longer show as Running. Live runs dropped out of the sidebar's
  short list of recent runs once the memory consolidator had started a few
  runs, so their tabs fell back to Running. A Claude session compacted while
  idle also looked as though a new turn had started. The agent page, `omni
  runs` and the terminal dashboard now say idle too.
- Runs whose agent had finished before retention closed the idle session are
  recorded as completed, not evicted. Earlier history is corrected once, for
  runs that recorded a finished turn. Runs from before turns were tracked show
  as Ended, since whether they finished was never recorded; only runs known to
  be mid-turn show as Evicted.
- The window no longer jumps to the Omni Memory run every time the consolidator
  starts. Background agents' run tabs now open without being selected, and when
  one is selected incidentally (a neighbouring tab closed) the window shows the
  Dashboard rather than that run's page.
- Running an agent whose advanced arguments end in a multi-value Claude flag
  (`--add-dir a --add-dir b`) started an empty session that did nothing: the flag
  took the startup prompt as one more directory. The runner now ends options with
  `--` before the prompt, for Claude and Codex, which also keeps a prompt that
  starts with `-` from being read as a flag.
- Memory searches scoped by `provider:` or `ws:` found nothing once the any-word
  fallback was needed. Every pass re-parsed all memories' cited sources (about 0.2 s
  with ~2,900 memories), which used up the fallback's 150 ms budget. The parsed
  sources and scoped memory sets are now cached until a memory changes.
- Semantic transcript searches limited by `before:`, `after:` or `since:` scored
  every record in turn and rejected the ones outside the window one lookup at a time
  (over 4 s for an early window). Each loaded vector now carries its message's time,
  so only records inside the window are scored.
- A transcript keyword search for a very common word ranked every match (142,000 for
  one project name, 0.6–1.6 s). It now ranks the newest 20,000 matches. Searches with
  fewer matches are ranked exactly as before.
- Hybrid transcript search fuses the top 30 keyword matches instead of 50. The eval
  scored 20, 30, 40 and 50 the same, and the slowest warm searches (about 0.4 s)
  lose the time spent reading rows that never reached the results.

- Codex loops and session-reuse runs can compact again. OmniTerm typed
  `/compact` and Enter in one write, and Codex’s TUI takes a carriage return
  that arrives in the same burst as typed text for a pasted newline, so the
  command sat unsubmitted in the composer. Codex now gets the command, then
  Enter as a separate write half a second later; each write is refused if a
  person has typed in the session, and a refused Enter reports compaction as
  rejected instead of delivered. Claude is unchanged.
- Codex runs start with `check_for_update_on_startup=false`. Codex's update
  prompt could take the Enter OmniTerm types for `/compact` or a loop tick and
  install an update.
- Omni Memory runs end once their agent finishes its turn and are recorded
  completed, instead of idling at the prompt as "Idle · waiting for you" until
  the next occurrence replaced them. Only OmniTerm's own unattended agents close
  this way; ordinary runs stay open for a reply, loops are unchanged, and a
  memory session reopened from history stays open.

- Graph community regions show again in the overview: memories outside the
  day's stable topics keep the community found among the memories shown.

- The memory graph's similarity edges include memories embedded by the knowledge
  helper itself (float32 vectors were skipped).

- Opening the knowledge index no longer scans every record: the one-time
  backfill guard now seeks the primary key, so a cold launch stops reading the
  whole 22 GB file before the worker starts.

- A transcript whose stream job record had gone missing restarted from zero,
  so a 1 GB transcript kept re-reading itself; it now resumes at its offset.
- Memory candidates' Codex-mirror check looks up the record's rollout
  position instead of comparing text across the whole session (3.8 s → 0.02 s
  for a pending-count window).

- Codex provider databases closed by their last writer (no `-wal`/`-shm`
  files) are read immutably instead of failing with "unable to open database
  file" on every scan.

- The Memory page loads promptly: it refreshes in the background every 15
  seconds instead of every 2, never repeats a request still in flight, and
  reuses corpus counts for five minutes, refreshed in the background by the
  indexing tick, so tab switches and clicks no longer queue behind its own
  polling or a multi-second recount of the message index. Its list column has a source-list design
  (count, type chips, weight bars) and can be resized by dragging its edge;
  the width is remembered with the window layout. A memory's evidence is
  grouped under each session's title, without command markup.

- Memory page and MCP knowledge requests no longer wait behind indexing: the
  app's indexing tick yields to queued requests between its worker calls,
  taking typical reads from 45–56 seconds to under 2 seconds while a large
  backlog drains, and each five-second indexing tick now reads up to 1 MB of
  transcript instead of 256 KB. `knowledge_status` reports `last_scan_timings`
  per phase.

- `omni agents order` now reorders agents in the sidebar itself: the
  scheduler rewrites the agent slots of the sidebar's canonical order within
  each folder (named agents first, in the order given, the rest unchanged)
  and leaves folder positions and memberships alone.

- The managed memory consolidator now works one tool call at a time and
  dismisses examined candidates in a single batch, so its requests no longer
  queue behind indexing until responses are lost; its run budget is 600
  seconds. Its prompt routes memories into types and treats an assistant's
  report as a claim unless the evidence shows the result. Existing v1 agents
  are recognised and upgraded in place. Knowledge requests from MCP wait up
  to 90 seconds, and lost dismissal responses are retried safely.

- Failed release packaging preserves the prior DMG, ZIP and checksums. Failed
  publication restores the prior files, or retains backups with their location
  if restoration is incomplete, while preserving the original failure status.
- Uninstall propagates MCP removal failure before deleting the app, launchers
  or user data, including with `--purge`, so the installation remains available
  for retry. Provider registrations already removed are not rolled back.
- `omni mcp uninstall` exits 1 if an installed provider's removal fails, after
  attempting removal from both installed providers. Existing ownership checks,
  provider command scopes and printed status output are preserved.
- Ad-hoc entitlement processing accepts either or both optional group
  entitlement keys being absent while preserving the source plist.
- Upstream-update discovery preserves lookup failures, rejects empty references
  and publishes metadata only after both references have been validated.
- Embedding cleanup bounds inspected vector keys and retains its position across
  worker restarts. Healthy indexes no longer require a full orphan scan before
  each maintenance pass or semantic search; deleted sources remain excluded from
  search while their derived vectors are removed incrementally.
- Message outbox scans use the indexed receipt key range, so unrelated transcript
  records no longer add a full record scan to each delivery pass.
- Provider database connections are explicitly closed if read-only initialization
  fails, while successful connections remain owned by their calling importer.
- Rebuilding OmniTerm with `scripts/build-app.sh` deleted and recreated the app,
  which removed the Inbox widget from Notification Center. The build now
  updates `dist/OmniTerm.app` in place, and `OMNITERM_SIGN_IDENTITY` signs it
  with a stable certificate (`scripts/local-signing-identity.sh` makes one).
- Command-W on the dashboard tab closed it and, as the only tab, the whole
  terminal window, so the next hotkey opened a plain shell with no dashboard.
  Command-W and Option-Command-W on the dashboard or the only tab now hide the
  window instead.
- Runs were marked `failed` after finishing their work. Claude and Codex stay
  open at their prompts when they finish, so a run that finished was only ever
  ended by a reboot, a closed tab or the next schedule. OmniTerm now follows the
  provider's transcript, and a run whose agent finished its turn is `completed`
  however its idle session ends.
- A run being evicted whose runner disappeared was marked `failed` instead of
  `evicted`.

## [0.3.1] - 2026-11-01

### Added

- A failed run now shows the provider's last error, and its output is kept in
  `run-logs`, because the tab closes with the run. The 50 newest logs are kept.
- `stopped` and `closed` run outcomes.

### Changed

- Native dependencies are rebuilt from source, including `librailroad_dsl`.
- Provider starts are spaced two seconds apart across runs.
- Uninstalling also removes `OmniTerm.previous.app`, says if OmniTerm is still
  running, and accepts `--destination` and `--bin-dir`.

### Fixed

- Runs starting at the same moment could fail within seconds with exit 1, because
  Claude could not store its login in the macOS Keychain.
- Closing a run's tab was recorded as `runner exited without reporting`, a
  failure. It is now recorded as `closed`, and the provider is ended cleanly.
- Stopping a run was recorded as a failure with `exit 143`.
- Developer ID builds would have carried a literal `$(TeamIdentifierPrefix)` in
  their entitlements.

## [0.3.0] - 2026-11-01

### Added

- GitHub Release assets for measurable DMG and ZIP download counts.
- Live aggregate release-download counts on the documentation website.
- Dashboard toggle (`g` or the **By repo** button) that groups agents under
  their repository, folding `@branch` worktrees into one entry. The choice is
  remembered.
- Per-agent dangerous permissions: `bypassPermissions` for Claude and a new
  `dangerous` mode for Codex, marked with ⚠ on the dashboard. They can be set in
  the editor or agent file, but not through MCP.
- Weekly automated rebase onto the newest iTerm2 nightly, delivered as a pull
  request, with `scripts/iterm2-fork.sh` for doing the same locally.

### Changed

- Agent tabs now close as soon as their run ends, for any reason. The outcome
  remains in run history.
- Retention now applies only to active runs, because finished runs no longer
  have tabs.
- Updated the embedded iTerm2 to `v20261031-nightly`.
- The scheduler now runs on its own queue, so busy terminal tabs cannot delay it
  and scheduler work cannot slow down drawing.

### Fixed

- Scheduled runs no longer stall on the folder trust prompt. The runner records
  trust for Claude's repository before launch and passes it to Codex for that run.
- MCP now rejects permission modes that do not belong to the agent's provider.
- OmniTerm no longer crashes when a scheduler request contains a null or
  malformed field, such as `omni runs` or the MCP `list_runs` tool with no
  agent filter.
- Agents launched at login could not find `claude` or `codex`. The runner now
  uses the login shell's `PATH` and records a start failure as a failed run
  instead of crashing with a traceback.
- Runs left behind by a dead runner no longer stay in `starting` forever.
- An agent file with an invalid schedule no longer crashes OmniTerm at launch.
- A client that stalls mid-request can no longer hold a scheduler thread.
- Ad-hoc-signed release builds could be refused by macOS at launch, because they
  claimed entitlements that require a Developer ID. Packaging now removes them
  from ad-hoc builds, and signs the app's executable after its nested code.
- Builds could ship interface files from an older build, which crashed OmniTerm
  at launch. Each build now starts from an empty product, and `build-app.sh`
  fails if a stale resource remains.
- Release builds could stop before starting the scheduler, waiting on a
  move-to-Applications prompt, the inherited iTerm2 crash reporter, or a
  second-copy warning. OmniTerm no longer shows these at launch: a second copy
  exits quietly, and iTerm2's crash reporter is gone.
- MCP relaunches the app its helpers belong to, not another build with the same
  bundle ID.
- Runs no longer inherit a parent agent session when OmniTerm was started from
  one, which turned off Claude's transcripts and so broke resuming.
- `patches/iterm2-omniterm.patch` had drifted from the fork; it is now rewritten
  on every build.
- The helper test suite and `omni-mcp` load on Python 3.15.

## [0.2.0] - 2026-08-02

### Added

- Calendar-style agent recurrence controls for minute, hourly, daily, weekly,
  and monthly schedules.
- Aligned recurring schedules such as every two hours at minute 15, every two
  weeks on Monday, and monthly runs on a chosen day and time.
- Live schedule previews and context-sensitive controls in the inline agent
  editor.
- Explicit dashboard focus flow between the agent list and **Run now** action.

### Changed

- Agent creation and editing now stays in one resize-aware inline form with
  mouse support, directory completion, and multiline prompt editing.
- Monthly schedules clamp days 29–31 to short months without losing the
  originally requested day for later months.
- The public website and README now identify a single latest distribution.

## [0.1.0] - 2026-08-02

### Added

- Initial public OmniTerm release with the native single-window terminal,
  scheduler, interactive Claude Code and Codex tabs, CLI, and MCP server.
- Menu-bar-only background lifecycle, configurable global shortcut, retained
  sessions, agent history, installer, and repository-hosted macOS packages.

[Unreleased]: https://github.com/wandernb/omniterm/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/wandernb/omniterm/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/wandernb/omniterm/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/wandernb/omniterm/compare/v0.8.3...v0.9.0
[0.8.3]: https://github.com/wandernb/omniterm/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/wandernb/omniterm/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/wandernb/omniterm/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/wandernb/omniterm/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/wandernb/omniterm/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/wandernb/omniterm/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/wandernb/omniterm/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/wandernb/omniterm/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/wandernb/omniterm/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/wandernb/omniterm/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/wandernb/omniterm/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/wandernb/omniterm/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/wandernb/omniterm/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wandernb/omniterm/releases/tag/v0.1.0
