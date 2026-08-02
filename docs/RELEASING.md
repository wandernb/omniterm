# Release process

OmniTerm should be distributed outside the Mac App Store as a Developer
ID-signed and Apple-notarized DMG. A GitHub Release is the canonical download
location and must contain binaries and corresponding source for the same tag.

## One-time setup

- Join the Apple Developer Program and create a **Developer ID Application**
  certificate.
- Store notarization credentials with `xcrun notarytool store-credentials`.
- Publish the pinned iTerm2 submodule, `patches/iterm2-omniterm.patch`, and all
  scripts needed to reconstruct the exact modified source with
  `scripts/bootstrap.sh`.
- Keep the original OmniTerm artwork in `assets/`; do not restore or publish
  the inherited iTerm2 application icon.

## Build a candidate

```sh
export OMNITERM_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)'
export OMNITERM_NOTARY_PROFILE='omniterm-notary'
./scripts/package-release.sh 0.2.0
```

The default artifact targets the build machine's architecture. For a universal
release, first rebuild native dependencies for both architectures with
`UNIVERSAL=1 make paranoid-deps` in `Vendor/iTerm2`, then export
`OMNITERM_UNIVERSAL=1` before packaging.

This produces a DMG, ZIP, and `SHA256SUMS` under `dist/release`. It invokes the
fork's official Release build, adds notices, signs the final app, submits the
DMG with `notarytool`, staples the ticket, and generates checksums. Without the
two environment variables it produces an ad-hoc-signed local test package.

## Validate and publish

Test on a clean supported Mac. Verify signature, Gatekeeper assessment,
installation, background launch, CLI launchers, MCP registration, one-window
behavior, provider runs, upgrade, and uninstall. Then:

1. Update release notes and version metadata.
2. Commit every source and submodule revision used by the binary.
3. Create a signed tag such as `v0.1.0` from that commit.
4. Attach DMG, ZIP, checksums, and a complete corresponding-source archive to
   the GitHub Release.
5. Keep that source downloadable for as long as the binary is offered.

Never publish an ad-hoc-signed artifact, a build containing an upstream signing
identity, a binary without matching source, or a tag created from a different
tree than the released binary.

## Versioning and changelog

OmniTerm follows [Semantic Versioning](https://semver.org/): increment MAJOR
for incompatible behavior or configuration changes, MINOR for backward-
compatible features, and PATCH for backward-compatible fixes. During the
initial `0.y.z` series, MINOR may still contain substantial product changes,
but breaking changes must be called out explicitly.

Maintain `CHANGELOG.md` in Keep a Changelog format. Move entries from
`Unreleased` into a dated version section before building, keep comparison
links current, and ensure the app metadata, artifact filenames, release
directory, tag, README, and website all use the same version.
