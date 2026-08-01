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
