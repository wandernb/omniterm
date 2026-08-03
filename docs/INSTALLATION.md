# Installation

## Release DMG

1. Download the latest [OmniTerm 0.2.0 DMG](https://github.com/wandernb/omniterm/releases/download/v0.2.0/OmniTerm-0.2.0.dmg)
   and [SHA256SUMS](https://github.com/wandernb/omniterm/releases/download/v0.2.0/SHA256SUMS), then verify the checksum.
2. Open the DMG.
3. Run **Install OmniTerm.command**. This copies the signed app to
   `/Applications`, installs `omni` and `omni-mcp` into `~/.local/bin`, starts
   OmniTerm in the background, and registers the `omni` MCP server with every
   installed supported provider.

Dragging the app to Applications is also supported. Run the installer command
afterward, or run `omni mcp install`, to create the launchers and provider
registrations.

Add `~/.local/bin` to `PATH` if your shell does not already include it.

The current build is ad-hoc signed but not Apple-notarized. If Gatekeeper
blocks the first launch, Control-click OmniTerm in Applications, choose
**Open**, and confirm once. Future Developer ID releases will remove this step.

## Background startup and settings

Installation and ordinary app launches start OmniTerm as a menu-bar app without
showing a terminal or Settings window. Use **Open OmniTerm** from the menu-bar
icon or press Control-Option-Space, the default configurable shortcut, when you
want the terminal.

Choose **OmniTerm Settings…** from the menu bar to change the shortcut, enable
launch at login, configure active-screen centering and double-Escape behavior,
or set scheduler timezone and retention defaults. Changes apply immediately and
are stored in `~/.omni/settings.json`.

When launch at login is enabled, macOS may ask for confirmation under **System
Settings → General → Login Items**. Login startup remains background-only and
does not open a terminal window.

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
