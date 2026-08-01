# Architecture

OmniTerm is a native iTerm2 fork, not a wrapper around a separately installed
iTerm2. The AppKit process owns one terminal window, the global shortcut, tab
lifecycle, and scheduler. Tabs run the genuine interactive Claude Code and
Codex CLIs under PTYs.

The bundled `omni` Python client talks to the scheduler over a same-user Unix
socket. The `omni-mcp` stdio process translates MCP JSON-RPC into that same
socket protocol. Agent JSON is canonical under `~/.omni/agents`; global
settings are in `~/.omni/settings.json`; runtime history and IPC are under
`~/Library/Application Support/OmniTerm`.

OmniTerm intentionally shares iTerm2's preferences suite so terminal profiles,
fonts, and colors stay synchronized, but it uses a distinct bundle identity,
socket namespace, runtime state, and update path.

## Settings control plane

The native menu-bar Settings window owns OmniTerm behavior rather than exposing
iTerm2's inherited preferences UI. It configures the global shortcut, login
startup, window behavior, scheduling timezone, default run retention, and the
number of protected recent tabs. Changes apply live and are written to the
canonical `~/.omni/settings.json` file also read by the CLI, scheduler, and MCP
server.

Launch at login uses the macOS main-app service on supported systems, with a
per-user LaunchAgent fallback for macOS 12.

## Visibility lifecycle

OmniTerm treats process lifecycle and window visibility as separate concerns.
A fresh launch, login startup, or application-reopen event keeps every AppKit
window hidden, including the non-restorable Settings window. Only an explicit
menu-bar **Open OmniTerm** action or the configured global shortcut may reveal
the terminal. Closing or hiding the terminal therefore leaves the native app,
scheduler, MCP bridge, tabs, and PTYs running in the background.
