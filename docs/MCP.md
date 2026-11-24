# MCP integration

OmniTerm bundles a local stdio MCP adapter. It exposes scheduler operations to
Claude Code and Codex while the native app remains the sole scheduler and state
writer. There is no listening TCP service and no remote access.

The installer registers `~/.local/bin/omni-mcp` as the user-scoped Claude MCP
server and the global Codex MCP server, both named `omni`. Start a new provider
session after registration. Existing sessions do not dynamically acquire it.

The server offers tools to list agents and runs, read a run's report, inspect
settings, preview schedules, create/update/enable/delete agents, start/stop runs,
and resume exact provider sessions. It deliberately exposes no arbitrary shell,
SQL, or raw file write primitive.

## Session knowledge and communication

**Local development implementation; verification in progress.** The canonical
app passed native compilation, packaging, activation and ad-hoc signature
verification. The full helper suite, native gate, coverage gate and all five
opt-in real-IPC cases passed. Bidirectional delivery, indexed receipts and replay
safety passed with disposable sessions. Scheduled memory consolidation acceptance
remains open. No public release has been made.

The catalog covers this user's local Claude and Codex histories across
workspaces, including sessions outside OmniTerm schedules. Sources include
Claude JSONL and subagent history, Codex JSONL and archived history, and the
current Codex database history in both `native` and `paginated` modes. Visible
tool records include command output, file diffs, collaboration results and MCP
errors. A session mirrored in JSONL and the database can currently return both
source representations; cross-source deduplication remains incomplete.
Web-search queries and result text, hook output, and generated-image prompts,
failures and artifact paths are included. Encoded image data is not searchable
text; images are not OCRed. Existing native projections refresh incrementally,
and `indexing_pending` remains true while that refresh is incomplete.
Background catch-up processes bounded batches of native histories under a
shared byte budget; a tiny or empty history does not consume the entire pass.
Selected native streams share publication and cleanup capacity with JSONL,
preventing sustained JSONL work from using the whole allowance.
Filesystem discovery, JSONL ingestion and native catalog positions survive
worker restarts. Backlog counts include persisted pending files outside the
current in-memory work queue.
Codex pages interleave those sources by provider rollout position, keeping
each record's chunks together. Offset pagination reflects the current index;
newly indexed earlier records can shift later page offsets.
IDs include the provider, such as
`claude:<id>` or `codex:<id>`. Discovery and indexing continue in the background;
`knowledge_status` reports coverage, backlog and errors. `running` requires
verified live evidence; `unknown` does not mean stopped.
Its `errors` list retains the 20 most recent observations with timestamps;
successful retries do not erase that history. Check fresh indexing progress and
`blocked_sources` before treating an older database-open error as a current outage.

| Tool | Arguments and result |
|---|---|
| `list_sessions` | Optional `provider`, `workspace`, `state`, `cursor` and `limit` (1–100, default 20). Continue with `next_cursor`. Socket clients may pass `order: "recent"` with `offset` for most recently active first (Claude and Codex only), continued with `next_offset`. |
| `get_session` | Required `session_id`; optional `offset` and `limit` (1–200, default 50). Returns ordered message and tool-output chunks with source references, `next_offset`, `indexing_pending` and `partial_index`. A Codex record mirrored by its JSONL rollout and the provider database (same rollout position, part, kind and text) is returned once; `include_mirrors: true` returns both copies with their source references. Socket clients may pass `message_id` to start a few records before that message (`focus_message_id` names the record shown, resolving a match on a Claude request echo), or `tail: true` for the newest page (`total` counts the records); `end_offset` is where the page ended. Request echoes older Claude Code builds wrote into transcripts, and a Codex record's mirror a line apart, are not returned (`include_echoes: true` returns them). |
| `rename_session` | Socket clients only. Required `session_id` and `title` (printable, up to 200 characters). Renames the session the provider's own way: a Claude `custom-title` record appended to its transcript (as `/rename` writes), or the Codex thread name through its app-server (`thread/name/set`), else a line in `~/.codex/session_index.jsonl`. Returns the title and which way was used. |
| `get_system_prompt` | Socket clients only. Required `session_id`. The instructions the session ran with, read from its transcript: Claude's recorded request shape (`system_prompt`, and `tools` by name) or Codex's base instructions. Untrusted text, for display. |
| `search_transcripts` | A `query`, filters or `like`; optional `mode`, `provider`, `workspace`, `session_id`, `role`, `after`/`before` (ISO dates, UTC unless offset) and `limit` (1–50, default 20). Searches messages and tool outputs with source references. Lexical search matches every word; when fewer than three messages do, messages matching any word follow, marked `partial_match: true` (phrases and `-` exclusions still hold). Each match carries a `snippet` with matched words in «». `like: "message:<id>"` (or `memory:<id>`) returns records nearest that record's stored vector, itself excluded; without a vector its opening words are searched lexically. |
| `knowledge_status` | Indexing, backlog, failures and local embedding availability. |
| `send_session_message` | Required `session_id`, UUID `request_id` and plain-text `message` (up to 8,000 characters); optional `sender_session_id` and `sender_name`. Returns a durable receipt. |
| `get_session_message` | Required `request_id`. Returns the current receipt without sending another message. |
| `list_blocked` | Optional `state` (`open`, default, or `all`), `answerable_only`, `min_age_seconds` and `limit` (1–200). Lists sessions reported waiting by `omni hooks`: `kind` is `idle` (turn ended), `permission` or `input` (a dialog only the person answers). |
| `get_block` | Required `block_id`. The block, the session's last assistant message and its project guidance (AGENTS.md, CLAUDE.md, knowledge/GUIDELINES.md). All untrusted text. |
| `answer_block` | Required `block_id`, `message` (up to 2,000 characters) and `reason`. Sends the reply as a session message whose `request_id` is the block ID. Refused for permission and dialog blocks, blocks idle under a minute and a fourth answer to one session within an hour. |
| `skip_block` | Required `block_id` and `reason`. Leaves the block for the person. |
| `search_memories` | Optional `query`, `mode`, `type`, `workspace`, `after`/`before`, `archived`, `like` and `limit` (1–50, default 20). Omit `query` to list recent source-linked memories. Archived memories are left out unless `archived: true`, which returns only them. `mode` defaults to `lexical`, which scores best for memories on the frozen eval. Lexical search relaxes to any word as transcripts do. Each memory carries a `snippet`; `like` works as for transcripts, with the memory's title as the lexical fallback. |
| `upsert_memory` | Required `title`, `body` and 1–50 `sources` containing `session_id` and `message_id`; optional `id`, `tags`, `memory_type`, `weight` (0–10), `lineage` (up to 20 memory IDs), `pinned`, `keep_sources` and a journal `reason` (up to 500 characters). Supply the existing `id` to revise a memory; its sources are replaced unless `keep_sources: true` keeps them and adds the new ones (up to 50). |
| `delete_memory` | Required `id`. Removes the authored memory and its revisions; original transcripts remain. |
| `memory_candidates` | Optional `limit` (1–20, default 20) and `session_id`. Returns unconsidered source evidence, excluding the consolidator's own sessions; scoping to one session keeps that exclusion. Candidates are assistant messages and user turns that state a correction, decision or preference (at least 12 characters of the person's own words after command and system markup is removed, not a pasted code or log block). Text is capped at about 4,000 characters, head and tail kept, and likely credentials (provider API keys and tokens, JWTs, private-key blocks, `password=`/`token=`/`secret=` values, `Authorization` headers, passwords in URLs) are masked as `[redacted]`; masking is best-effort. The person's own turns are offered first, through a walk that reaches further back on each call. A batch stays under about 24 KB so agent harnesses show it whole; candidates that do not fit are offered by the next call. Each candidate carries `similar`: up to five existing memories with `id`, `title`, a 300-character `excerpt`, `score`, `match` (`semantic` for a nearest neighbour by embedding, `lexical` for a shared distinctive term) and `source_count`. A consolidator can add a memory, update one (`upsert_memory` with its `id` and `keep_sources: true`, which keeps the memory's evidence and adds the new source) or dismiss the candidate. |
| `dismiss_memory_candidate` | Required `message_id` and factual `reason`. Marks examined evidence considered without inventing a memory. |
| `dismiss_memory_candidates` | Required `items`: 1–20 `{message_id, reason}`. Validates every item before saving any, so one call replaces a burst of single dismissals. |
| `memory_topics` | Optional `limit` (1–20, default 20). Returns stable topics (groups of related memories from shared concepts, recomputed daily over every non-archived memory, ids carried over by member overlap) that have no name or whose members changed by more than about 30%, each with `size`, its shared `concepts` and up to eight `examples` (title and excerpt, 120 characters each). The list stays under about 16 KB; `remaining` counts the topics left for a later call. Read-only. |
| `name_topic` | Required `id` (`topic:…`) and `name` (1–60 characters); optional `summary` (≤280). Stores the name shown in the Memory graph regions, the mind map's Topics branch and the digest, and records a `topic_named` event. |
| `merge_candidates` | Optional `limit` (1–10, default 10). Returns undecided pairs of non-archived memories whose embeddings score ≥ 0.85, best first, each with both memories' title, body (≤1,200 characters), type, `updated` and `source_count`. Read-only. |
| `decide_merge` | Required `keep`, `drop`, `decision` (`merge`, `supersede`, `keep`) and `reason`; optional `title` and `body` for a merge. `merge` revises `keep` with `drop`'s evidence (and the given text) and archives `drop`; `supersede` archives `drop`; `keep` leaves both. A decided pair is never offered again; each decision is a `merge_decided` event. |
| `explain_memory` | Required `id`. Explains one memory without marking it used: `weight` (journal points, decayed weight, `expires_at`, reasons), `archival` (exemption, unused days, next action), `sources` grouped by session, `formation` (creator, reason, journal steps), `lineage` (ancestors and descendants), `topics`, `concepts`, `links` (each with a `reason`), `cited_by_summaries`, `summarizes` on a summary, and a one-line `why`. Read-only. |
| `weekly_summary_candidates` | Optional `project` (folder under `~/Projects`), `week` (ISO, e.g. `2026-W45`) and `limit` (1–3, default 2). Without both, lists complete weeks of the last six with at least two episodes or memories that have no summary or changed since, newest first, never more than today's remaining cap. Each candidate carries its episodes, memories, `cites`, `sources`, `fingerprint`, `existing` and `stale`; `daily` reports the cap. Read-only. |
| `write_weekly_summary` | Required `project`, `week`, `title` and `body`. Rebuilds the candidate and writes the summary memory (`summary-<hash>`, episodic, tagged `weekly-summary`, the project and the week) with the candidate's evidence and cited IDs. At most six a day (`daily_cap`); a week with too little memory is refused (`nothing_to_summarize`). |
| `configure_memory` | Optional `provider` (default `codex` for a new agent), `workspace`, `model` and `enabled` (default true). Configures the owned 15-minute memory agent through native validation. |
| `list_memories` | Optional `memory_type`, `tag`, `workspace`, `query`, `sort` (`weight` default, `recent`, `evidence`, `title`), `include_expired` (default true), `limit` (1–500, default 100) and `offset`. Returns memories with decayed weights, `total` and the type table. |
| `get_memory` | Required `id`. Returns the memory with evidence excerpts, revisions, journal events and related memories. |
| `memory_events` | Optional `limit` (1–200, default 50), `kind`, `memory_id` and `before` (an event ID). Pages the memory journal newest first; pass `next` as `before` to continue. |
| `memory_overview` | Counts by type and tag, intake pipeline counts and journal events in the last 24 hours. |
| `memory_graph` | Optional `limit` (1–500, default 300), `edges` (`session`, `tag`, `lineage`, `similarity`, `workspace`, `concept`; default all but `workspace` and `concept`, which the Memory page turns on to group memories by project and by the files, packages, commands, error codes, symbols and plans they name), `min_similarity` (0–1, default 0.8), `memory_type`, `focus` (a memory ID kept in the graph), `hops` (1 or 2, with `focus`: return only that memory's neighbourhood, at most 120 nodes, strongest paths first; hubs are neighbours but are not walked through), `workspace` (memories from that path or another worktree checkout of the same repository: `repo/@name`, `repo-wt/name` and `repo/.claude/worktrees/name` fold into `repo`, which also names their single workspace hub), `after` (memories updated at or after this Unix time), `min_weight` (0–10, effective weight) and `mindmap`. Returns the strongest memories as nodes ordered by ID (hubs after them) and weighted edges ordered by kind, source and target, so an unchanged graph comes back in the same order. Each memory node in a concept community carries `community` (`id` and `label`): a member of a stable topic (computed daily over every memory) carries the topic's id (`topic:…`, kept while membership overlaps) and its name; any other memory carries the community found among the memories shown, its id named after the community's smallest member. A concept hub links the memories that name it when 2 to 60 of the shown memories do; the mind map adds a Topics branch of concept communities, whose leaves carry `memory_id`, and every branch carries `children_count`. |
| `memory_digest` | Optional `workspace` and `regenerate`. Returns the stored Markdown digest of live memories: a Topics section (concept communities among the strongest memories), then memories by type, strongest first. `stale` is set when memories changed since; `regenerate: true` rebuilds it. |
| `update_memory` | Required `id`; optional `memory_type`, `weight` (0–10), `pinned` and `reinforce` (adds 0.5, up to 10). Changing only the type resets the weight to that type's base. Title, body and sources change only through `upsert_memory`. |

Both search tools read a query grammar, so an agent or the Search tab can write
`type:decision ws:omniterm since:7d "sqlite vacuum" -codex`:

| Token | Meaning |
|---|---|
| `type:` | Memory type (or an alias: `decision`/`fact` → declarative, `event` → episodic, `howto` → procedural, `todo` → prospective, `preference` → autobiographical, `path` → spatial, `feedback` → conditioning). On transcripts, the record kind: `message`, `tool` or `output`. |
| `ws:` / `workspace:` | A workspace folder name (any case) or full path. |
| `provider:`, `role:`, `session:` | Provider (`claude`, `codex`, `kb`), message role, or a provider-qualified session ID. |
| `concept:` | Memories naming that file, package, command or error. On transcripts its words join the searched text. |
| `after:` / `before:` | ISO date or date-time, UTC. Transcripts compare message timestamps; memories compare `updated`. |
| `since:7d` | Shorthand for `after:` that many hours (`h`), days (`d`) or weeks (`w`) ago. |
| `archived:true` | Only archived memories. |
| `"…"` / `-word` | An exact phrase; a word or `-"phrase"` that must not appear. |

Unknown keys and unreadable values stay as plain text. Arguments passed explicitly
win over the same key in the query. A query of filters alone lists the newest
matching records (transcripts pick the most recently updated sessions first).

Every memory has a type, a weight from 0 to 10 and an effective weight that
decays linearly per day since it was last reinforced. A new memory starts at its
type's base weight unless `weight` is given, and `memory_type` defaults to
`declarative`. A revision that adds evidence reinforces it by 0.5 per new source. Below an effective
weight of 0.5 a memory is an `expiry_candidate`; it is flagged, not deleted.
A pinned memory never decays.

Two sources of memory need no model. Every `knowledge/` folder under `~/Projects`
(up to four levels deep, reading only `@head` where worktrees sit side by side,
skipping dependency, build and backup folders) plus `~/.claude/knowledge` and any
folder listed in `~/Library/Application Support/OmniTerm/knowledge-roots` (one
`label=path` or bare path per line; `#` starts a comment) is indexed: each
Markdown file is a `kb:` session whose
heading sections are searchable `document` records, and each file has one memory
(`kb-…`, tagged `knowledge-base` and its project) that is revised when the file
changes and deleted when it disappears. Each project's day of top-level sessions
also becomes one episodic memory (`episode-…`, tagged `episode` and the project)
listing each session's request and how it ended, with both records as evidence.
`knowledge_status` reports both under `knowledge_bases` and `episodes`. The
scheduled consolidator then distils durable facts from transcript candidates.

| Type | Holds | Base weight | Decay per day |
|---|---|---|---|
| `declarative` | Facts and decisions | 7 | 0.02 |
| `episodic` | Events and what happened | 5 | 0.05 |
| `procedural` | How to do things | 7 | 0.02 |
| `prospective` | Intentions and follow-ups | 9 | 0.10 |
| `autobiographical` | Who the person is and what they prefer | 8 | 0.01 |
| `spatial` | Where things live: repositories, paths, layout | 6 | 0.03 |
| `conditioning` | Feedback and learned reactions | 6 | 0.03 |

Memory results carry `memory_type`, `weight`, `effective_weight`, `base_weight`,
`decay_rate`, `evidence_count`, `lineage`, `pinned`, `last_reinforced_at` and
`expiry_candidate`. Saves, revisions, weight changes, deletions, intake and
dismissals are journaled. Inside an OmniTerm run, `omni-mcp` attaches the run
and agent to those journal entries; callers cannot supply them.

Search modes are `lexical` (default), `semantic` and `hybrid`. Semantic retrieval
uses local macOS NaturalLanguage embeddings, with the English sentence model
preferred and an explicit English word-vector mean fallback when available.
It does not download a model or call a hosted embedding API. Model identity,
revision and dimensions accompany the index; unavailable inference is reported
as an error rather than substituted with lexical results. Semantic coverage can
lag transcript ingestion, so inspect indexing status when assessing results.
Semantic search is exact: the knowledge worker keeps every stored vector in
memory and scores them all for each query. It loads them in the background
after launch (in seconds from a snapshot it saves beside the index, or a few
minutes without one), and until it has finished, results carry `vectors_warming: true`
and cover only the vectors loaded so far. Tool calls and tool output are found
by lexical search but are not vector-indexed. `hybrid` fuses the lexical and
semantic rankings with Reciprocal Rank Fusion (`score` is the fused value;
`semantic_score` and `lexical_score` stay available). It then lets near-duplicate
results give way to distinct ones.
An empty page or null `next_offset` means the currently indexed page range has
ended. When `partial_index` is true, more transcript content may still arrive.
Active transcripts and pending delivery evidence receive bounded indexing
priority while historical ingestion continues.
Catalog discovery has a separate metadata budget, so a large early transcript
does not delay listing later session files. Catalog presence does not mean its
content has finished indexing; inspect the pending flags before treating an
empty transcript page as complete.
Background native-history and vector scans save their progress through the
app-owned database, so restarting the app does not repeatedly revisit only the
oldest records. Vector scan checkpoints follow completed writes and retain
unfinished records for a later pass.
Reading an incomplete session requests a bounded indexing pass; retry the read
after background indexing advances to retrieve more content. Requests coalesce
and share scan time with live delivery evidence and ordinary history. Reading
a verified running session also queues a refresh, reported as
`indexing_pending`, even when its previous snapshot was complete.

Only the native app writes the embedded SQLite knowledge database. Its
supervised worker indexes off the scheduler queue and reads native projections
through a bounded cache. Authored memory revisions and message receipts are
durable records, separate from rebuildable transcript and vector indexes.
Memories retain their evidence references when a source disappears and identify
that evidence as unavailable; indexing never rewrites provider transcripts.
Orphaned vectors are removed in bounded background passes whose progress survives
worker restarts. Search excludes deleted source records immediately; physical
cleanup of their derived vectors may finish later.

Messages use provider channels rather than terminal Enter. The Claude peer
adapter uses the recipient's advertised peer protocol. Claude releases that
advertise protocol 1 use the same authenticated message format. Omni rejects
unknown protocols and unverifiable recipients before sending. Registry metadata may be
readable by others only inside its owned private directory; authentication
tokens remain private. Codex uses its session queue without starting a competing
resume writer. External Codex discovery uses a bounded WebSocket loaded-thread
probe through the same-user local control socket and forces a fresh ownership
check before delivery. Read-only discovery has been exercised against the
installed provider. Disposable Claude and Codex sessions have exchanged real MCP
messages in both directions. Their receipts recovered as `delivered` after an app
restart, matched exact indexed user input, and remained unchanged on replay.

Receipt states are `queued`, `sending`, `sent`, `delivered`, `rejected` and
`unknown`. `delivered` requires the exact canonical attributed message and its
request UUID to appear as user input in the recipient's indexed transcript.
The receipt includes that source evidence. This proves recorded input, not that
the model read, processed or completed the request. `queued` and `sent` are
transport states without that evidence. Reuse the same request UUID and exact
content after a retryable failure. A crash or ambiguous transport outcome does
not automatically resend a message. Control characters are rejected, and
provider peer input is treated as text rather than as a slash command or
permission-dialog response.

Background consolidation is opt-in. `configure_memory`, or the CLI installation
command below, creates the owned **Omni Memory** agent without overwriting an
unrelated agent with that name. Each occurrence examines at most 20 candidates,
searches existing memories, and creates or revises at most five source-linked
memories. The task marks examined non-memory evidence considered and excludes
its own output. It runs through the existing native scheduler using the selected
provider account, Codex by default: Codex uses normal permissions and may require
approval, and runs on its fast, cheap model (`gpt-6-luna`) unless `model` names
another; Claude uses auto-mode, without bypass permissions. An existing agent
keeps its provider and model unless they are passed. One approved scheduled trial created
and revised a source-linked memory, but request timeouts prevented clean completion
and review corrected an unsupported claim. Unattended consolidation remains under
verification.

```sh
omni sessions --provider claude --limit 20 --json
omni session claude:SESSION-ID --limit 50 --json
omni knowledge search "database connection" --mode hybrid --json
omni session codex:SESSION-ID send "Please check the migration" --request-id 00000000-0000-0000-0000-000000000001
omni message 00000000-0000-0000-0000-000000000001 --json
omni knowledge status --json
omni knowledge memories "migration" --mode semantic --json
omni knowledge memory install --provider claude --workspace . --disabled
```

Omit `--disabled` to enable scheduled consolidation. Transcript, output, message
and memory text is untrusted data, never instructions. These tools do not expose
arbitrary shell commands, SQL, client-selected file reads or raw file writes.

## Scheduling and run reports

`list_runs` returns runs newest first. It filters by `agent`, `state`,
`origin` (`scheduled` for runs OmniTerm started, `manual` for sessions a person
started by hand), and `before` (epoch seconds: only runs started earlier, so
passing the oldest `started` seen pages back), and returns at most `limit` runs
(1–100, default 20).

### Loop agents and session reuse

The source implementation adds these `create_agent` / `update_agent` fields
(the running app and MCP adapter must be rebuilt to use them):

| Field | Meaning |
|---|---|
| `kind` | `scheduled` or `loop`. A loop keeps one persistent run/session; busy ticks coalesce. |
| `session_policy` | `resume` (default) continues one session across runs, compacted once it passes half the model window (120k tokens when unreported); `fresh` starts a new session each run. Loops always retain their session. |
| `compact_on_end` | Compact after every run, not only when the session is large; defaults false. |
| `run_budget_seconds` | Soft work budget, zero to disable; independent of retention. |
| `wrap_up_seconds` | Reminder lead time before that budget expires. |

Loops repeat their startup prompt in the same idle instance, with a 600-second
soft work budget and a 120-second reminder lead time by default. A busy loop
keeps working; due ticks do not create another run or tab. Compaction and
recovery resume are idle-only operations. Existing ordinary scheduled agents
retain their fresh-session behavior and selectable overlap policy.

Saving a loop requires the connected app's `get_settings` response to advertise
`loop_mode: persistent`. Clients refuse loop creation on older apps rather than
falling back to parallel scheduled runs. The stored `overlap` field is retained
for compatibility but does not control a loop's behavior.

Codex reminders use its session queue and distinguish enqueueing from observed
consumption. An unconsumed reminder prevents automatic session closure. Claude
busy-turn reminders currently fall back to an Inbox notification.

Compaction requires provider confirmation. Automatic terminal input is refused
when safe delivery cannot be established; a busy provider or a possible
permission prompt must not be answered accidentally. Live lifecycle acceptance
is tracked in `knowledge/plans/@2026-11-06-02-loop-session-lifecycle.md`.

Agents can be organised for OmniTerm's sidebar. `create_agent` and
`update_agent` accept three optional fields, and an empty string clears one:

| Field | Values |
|---|---|
| `group` | `Section` or `Section/Subsection`: at most two levels, each name trimmed, 1–40 characters, no control characters. |
| `icon` | `sf:<SF Symbol name>` (lowercase letters, digits and dots, up to 64; `omni agents appearance` lists the ones OmniTerm offers), or `emoji:<one emoji>` with no spaces. |
| `color` | `red`, `rose`, `pink`, `orange`, `brown`, `yellow`, `lime`, `green`, `mint`, `teal`, `cyan`, `blue`, `indigo`, `purple`, `slate` or `gray`. |

An agent object carries these fields only when they are set; an absent field
means none. `rename_agent` (`name`, `new_name`) renames an agent and keeps its
schedule, history and place; a new name is trimmed, 1–100 characters, with no
control characters. A rejected value comes back as an `invalid_appearance`
error naming the `field`. `omni agents appearance --json` lists the allowed
icons and colours, `omni agent NAME set --group … --icon … --color …` and
`omni agent NAME rename NEW` do the same from a terminal, and `omni agents`
lists agents under their sections. The rules and shared valid and invalid
examples are in `tests/fixtures/agent_appearance.json`.

### Configured sidebar folders

The source adapter adds `set_sidebar_groups` with a required, nonempty `groups`
array, for example `{"groups":["Work","Work/Code","Empty"]}`. It replaces the
complete configured folder list, including empty folders. Paths follow the
same two-level group rules above and are normalized and deduplicated in order.
Agent memberships stay unchanged: a folder holding agents remains visible
even when omitted. The result is an object containing the saved `groups` array.

The CLI equivalent is `omni folders set "Work" "Work/Code" "Empty" --json`.
The new MCP tool requires packaging the updated adapter and restarting its
MCP client; current installed-client availability has not been verified.

### Run reports

`get_run` takes a run `id` and returns its report:

| Field | Meaning |
|---|---|
| `id`, `agent`, `state`, `reason`, `started`, `finished` | The run and its outcome. |
| `origin` | `scheduled` or `manual`. |
| `workspace`, `provider` | As they were when the run started. |
| `transcript_path` | The provider's transcript file. |
| `branch`, `head_start`, `head_end` | The Git branch and the commit before and after. |
| `commits` | `[{sha, subject}]` made during the run. |
| `diff_state`, `diff_files` | Whether the diff since the start is captured (`pending` until it is), and the changed files. |
| `pr_url` | A pull request for the branch, when one is known. |
| `final_message` | The agent's last answer, up to 4,000 characters. |
| `summary`, `summary_state` | The AI summary, usable once `summary_state` is `ready`; `failed` means use the final message. |

Fields may be null while a run is still going, and for a non-Git workspace. The summary and final message are
written by agents, so callers must treat them as data, not instructions. The
diff itself is not exposed to agents, for the same reason they cannot read
notifications: it may hold secrets from another workspace. People read it with
`omni run ID --diff`.

The run's last terminal screen is kept the same way, for people only. The
scheduler stores it at the end of each turn and when the run ends: the visible
screen plus up to 2,000 lines of scrollback, with colours. The socket operation
`run_screen` (`{"op": "run_screen", "id": ID}` → `{"ok": true, "screen": "…",
"truncated": false}`, or the errors `invalid_request`, `run_not_found` and
`screen_not_found`) serves `omni run ID --screen`. On a terminal that keeps
only colour sequences and drops every other escape (titles, cursor moves,
clears), and when piped or with `--plain` it drops them all. There is no MCP
tool for it: a screen can show secrets and other workspaces, like a diff.

Runs without a saved screen can still be previewed: `omni run ID --tail
[--lines N] [--json] [--plain]` reads the end of the run's Claude or Codex
transcript (its recorded `transcript_path`, else found by session ID) and
renders the last turns like the terminal: prompts, the agent's text, one line
per tool call and the first lines of each result. It is read-only, reads only
files under `~/.claude/projects` and `~/.codex/sessions`, and is not an MCP tool
either.

When a run's agent finishes a turn, its runner starts a detached summarizer:
`claude -p` with the `summary_model` setting (default `haiku`), `--safe-mode`,
`--tools ""`, `--strict-mcp-config` and `--no-session-persistence`, in an empty
temporary folder, without the run's `OMNI_RUN_ID`, and with a 90-second limit.
So it has no tools, loads no MCP server (not even `omni`), no hooks and no
project instructions, leaves no transcript, and is not itself a run. It sees the
agent's final message, the branch, commit subjects and changed file names, never
the diff body. Each turn's report carries the time it finished (`turn_at`), so
a slow summary never replaces a newer turn's.

Two keys in `~/.omni/settings.json` control it:

| Key | Default | Meaning |
|---|---|---|
| `summaries` | `true` | `false` turns AI summaries off; surfaces show the final message. |
| `summary_model` | `"haiku"` | The Claude model alias or full name used for summaries. |

A summary costs one small-model call per finished turn, Codex runs included.

`notify_user` lets an agent tell you something that needs you, or a result you
asked to hear about. It takes a one-line `title` (120 characters), an optional
`body` (8,000 characters), a `severity` (`info`, `attention` by default, or
`urgent`), an optional `dedupe_key`, and `include_diff`. The notification shows
an unread count on the menu-bar item (`info` is stored without one) and appears
in the Inbox, with the workspace's Git diff captured at that moment. Inside a
scheduled run, the run, agent and workspace are attached automatically; outside
one, the workspace is the nearest project above the session's directory.

The tool only writes: agents cannot read, clear or change notifications.
Notification text is treated as untrusted and shown as plain text, links are not
opened, and no field is ever executed. A repeat with the same `dedupe_key` within
24 hours updates the earlier item, and one run can store at most 20; later ones
are summarized in a single item. Diffs are captured off the scheduler with a
10-second timeout and a 2 MB cap, with repository filters, textconv and external
diff drivers disabled, and stored with mode 0600, since they may contain secrets.
The same notifications can be raised from scripts with `omni notify`.

Every session with the `omni` server learns this without being told: the
server's instructions carry the guidance on when and how to call `notify_user`,
and the resource `omni://guide` holds the full guide (arguments, severities,
where notifications appear, limits, the CLI, and scheduling). Runs that
OmniTerm starts also get a short note appended to their system prompt
(`--append-system-prompt` for Claude, developer instructions for Codex) that
names the agent and run and repeats when to notify. The person's own system
prompt is unchanged and comes first.

```sh
omni mcp install
omni mcp status
omni mcp doctor
omni mcp uninstall
```

## Blocked sessions

`omni hooks install` adds Claude Code `Stop`, `Notification` and `UserPromptSubmit`
hooks to `~/.claude/settings.json` and a Codex `notify` program to
`~/.codex/config.toml`, after backing each file up. It never replaces another
Codex `notify` program; it says how to chain it instead. `omni hooks uninstall`
removes only OmniTerm's entries. Each hook runs `omni hook <provider>`, which
records the event through the scheduler in a few hundred milliseconds and always
exits 0.

A block's kind comes from the hook's event type, never from prompt text, and an
unknown dialog counts as protected. Permission and dialog blocks raise one urgent
Inbox notification per session and are never answered. `omni unblocker` creates
the opt-in `Omni Unblocker` agent (MCP tools only). It is scheduled every minute
but OmniTerm starts it only when a session has waited 45 seconds and no unblocker
run is live, so a waiting session is answered within a minute or two and an idle
machine costs nothing. It answers permissively, as the person would, from what
the session was last asked (`get_block` returns `last_request`), its project
guidance and the person's own policy (`~/.omni/unblocker/policy.md`, returned as
`owner_policy`): keep building, push, deploy, continue. It never answers with
secrets, credentials, permission approvals or payments, and leaves sending real
email or messages and deploying to (or deleting data of) live commercial
production apps to the person, using `skip_block` for those. `omni blocked`, `omni block ID [show|answer|skip]` do the
same by hand.

Auto-unblock covers every Claude and Codex session on the machine, not only
OmniTerm runs, and is on for each session unless switched off: the
**Auto-Unblock** button on a session's page in Sessions mode, or
`omni unblock SESSION on|off`. `omni hooks install` turns the agent on with the
hooks and tells sessions who may answer for the person: a Claude `SessionStart`
hook adds that as context, and a marked block goes into `~/.codex/AGENTS.md`.
Only `answer_block` may send as "Omni Unblocker"; `send_session_message`
refuses that sender name.
