# MCP integration

OmniTerm bundles a local stdio MCP adapter. It exposes scheduler operations to
Claude Code and Codex while the native app remains the sole scheduler and state
writer. There is no listening TCP service and no remote access.

The installer registers `~/.local/bin/omni-mcp` as the user-scoped Claude MCP
server and the global Codex MCP server, both named `omni`. Start a new provider
session after registration. Existing sessions do not dynamically acquire it.

The server offers tools to list agents and runs, inspect settings, preview
schedules, create/update/enable/delete agents, start/stop runs, and resume exact
provider sessions. It deliberately exposes no arbitrary shell, SQL, or raw file
write primitive.

```sh
omni mcp install
omni mcp status
omni mcp doctor
omni mcp uninstall
```
