# Privacy and security

OmniTerm does not add analytics, telemetry, accounts, or a network service.
Agent definitions, prompts, schedules, and run metadata are stored locally.
The MCP adapter uses stdio and a same-user Unix socket.

Claude Code and Codex are separate third-party command-line products. Runs use
their installed binaries and are subject to their providers' authentication,
privacy terms, network behavior, and configuration. OmniTerm does not proxy or
retain provider credentials.

The scheduler can execute configured prompts in configured workspaces. Treat
agent JSON files as executable automation configuration and review changes to
`~/.omni/agents` before enabling them.
