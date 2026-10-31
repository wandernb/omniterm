# Changelog

All notable changes to OmniTerm are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/wandernb/omniterm/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wandernb/omniterm/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wandernb/omniterm/releases/tag/v0.1.0
