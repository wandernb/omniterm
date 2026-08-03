# Changelog

All notable changes to OmniTerm are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- GitHub Release assets for measurable DMG and ZIP download counts.
- Live aggregate release-download counts on the documentation website.

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
