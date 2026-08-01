# Installation

## Release DMG

1. Download `OmniTerm-VERSION.dmg` and `SHA256SUMS` from the same GitHub
   release and verify the checksum.
2. Open the DMG.
3. Run **Install OmniTerm.command**. This copies the signed app to
   `/Applications`, installs `omni` and `omni-mcp` into `~/.local/bin`, starts
   OmniTerm in the background, and registers the `omni` MCP server with every
   installed supported provider.

Dragging the app to Applications is also supported. Run the installer command
afterward, or run `omni mcp install`, to create the launchers and provider
registrations.

Add `~/.local/bin` to `PATH` if your shell does not already include it.

## Verify

```sh
codesign --verify --deep --strict /Applications/OmniTerm.app
spctl --assess --type execute --verbose /Applications/OmniTerm.app
omni mcp doctor
claude mcp get omni
codex mcp get omni
```

Provider registration is conflict-safe: installation does not replace an
unrelated MCP server already named `omni`.

## Upgrade and uninstall

Running a newer installer replaces only an app whose bundle identifier is
`com.omniterm.app`; the prior app is retained as
`/Applications/OmniTerm.previous.app`. Agent definitions and history survive.

```sh
./scripts/uninstall.sh          # preserve data
./scripts/uninstall.sh --purge  # also remove agents, settings, and history
```
