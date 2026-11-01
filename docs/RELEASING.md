# Release process

OmniTerm is distributed outside the Mac App Store. A GitHub Release is the
canonical download location and must contain binaries and corresponding source
for the same tag.

The goal is a Developer ID-signed and Apple-notarized DMG. No Developer ID
certificate exists yet, so current releases are ad-hoc signed and not notarized.
Ad-hoc releases are acceptable only while that is true, and each must say so in
its release notes and `releases/<version>/README.md`. Publishing one is a
decision the maintainer makes for that release.

## One-time setup

- Join the Apple Developer Program and create a **Developer ID Application**
  certificate.
- Store notarization credentials with `xcrun notarytool store-credentials`.
- Install SF Symbols with `brew install --cask sf-symbols`. `make paranoid-deps`
  reads its metadata from `/Applications/SF Symbols.app`.
- Publish the pinned iTerm2 submodule, `patches/iterm2-omniterm.patch`, and all
  scripts needed to reconstruct the exact modified source with
  `scripts/bootstrap.sh`.
- Keep the original OmniTerm artwork in `assets/`; do not restore or publish
  the inherited iTerm2 application icon.

## Build a candidate

Rebuild native dependencies first. `make paranoid-deps` in `Vendor/iTerm2`
refuses to run on dirty submodules, and earlier dependency builds leave generated
output in them, so restore those first. Inspect anything that is not generated.

```sh
export OMNITERM_SIGN_IDENTITY='Developer ID Application: Example (TEAMID)'
export OMNITERM_NOTARY_PROFILE='omniterm-notary'
./scripts/package-release.sh 0.3.1
```

`scripts/signing-entitlements.sh` prepares the entitlements. For a Developer ID
it expands `$(TeamIdentifierPrefix)`, taking the team from the identity name or
`OMNITERM_TEAM_ID`, and fails if any build variable remains. For an ad-hoc build
it removes `keychain-access-groups` and `application-groups`, because macOS kills
ad-hoc code that claims them. The Developer ID path has not yet been used with a
real certificate.

The default artifact targets the build machine's architecture. For a universal
release, first rebuild native dependencies for both architectures with
`UNIVERSAL=1 make paranoid-deps` in `Vendor/iTerm2`, then export
`OMNITERM_UNIVERSAL=1` before packaging.

This produces a DMG, ZIP, and `SHA256SUMS` under `dist/release`. It invokes the
fork's official Release build, adds notices, signs the final app, submits the
DMG with `notarytool`, staples the ticket, and generates checksums. Without the
two environment variables it produces an ad-hoc-signed package.

## Validate and publish

A signature check does not prove the app starts. Launch the packaged build and
judge it by a scheduler reply and a real provider run.

Test on a clean supported Mac. Verify signature, Gatekeeper assessment,
installation, background launch, CLI launchers, MCP registration, one-window
behavior, provider runs, upgrade, and uninstall. A Tart VM such as
`ghcr.io/cirruslabs/macos-tahoe-vanilla` works for this. Then:

1. Update release notes and version metadata.
2. Commit every source and submodule revision used by the binary.
3. Create a tag such as `v0.1.0` from that commit, signed when a signing key is
   available.
4. Run `scripts/package-source.sh VERSION` to build the corresponding source.
5. Push the tag and create the GitHub Release with the DMG, ZIP, checksums, and
   source archive. Push `main` last: that deploys the website, which must not
   link to a download that does not exist yet.
6. Keep that source downloadable for as long as the binary is offered.

Never publish a build containing an upstream signing identity, a binary without
matching source, or a tag created from a different tree than the released binary.

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
