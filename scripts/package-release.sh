#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
version="${1:-}"
[[ "$version" == <->.<->.<-> ]] || { print -u2 "Usage: $0 MAJOR.MINOR.PATCH"; exit 2; }
release_dir="$project_dir/dist/release"
app="$project_dir/dist/OmniTerm.app"
release_build_dir="${OMNITERM_RELEASE_BUILD_DIR:-$project_dir/.release-build}"

if [[ -n "${OMNITERM_UNIVERSAL:-}" ]]; then
  UNIVERSAL=1 OMNITERM_BUILD_DIR="$release_build_dir" OMNITERM_CONFIGURATION=Deployment OMNITERM_SKIP_SIGNING=1 "$project_dir/scripts/build-app.sh"
else
  OMNITERM_BUILD_DIR="$release_build_dir" OMNITERM_CONFIGURATION=Deployment OMNITERM_SKIP_SIGNING=1 "$project_dir/scripts/build-app.sh"
fi

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $version" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :NSHumanReadableCopyright Copyright © 2026 Paul REBOH. Based on iTerm2; GPL-3.0-or-later." "$app/Contents/Info.plist"
mkdir -p "$app/Contents/Resources/Licenses"
install -m 0644 "$project_dir/LICENSE" "$app/Contents/Resources/LICENSE"
install -m 0644 "$project_dir/LICENSES/GPL-3.0.txt" "$app/Contents/Resources/Licenses/GPL-3.0.txt"
install -m 0644 "$project_dir/THIRD_PARTY_NOTICES.md" "$app/Contents/Resources/THIRD_PARTY_NOTICES.md"
install -m 0644 "$project_dir/Vendor/iTerm2/LICENSE" "$app/Contents/Resources/Licenses/iTerm2-LICENSE"
install -m 0644 "$project_dir/Vendor/iTerm2/COPYING" "$app/Contents/Resources/Licenses/iTerm2-COPYING"
while IFS= read -r license; do
  relative="${license#$project_dir/Vendor/iTerm2/}"
  target="$app/Contents/Resources/Licenses/${relative:h}"
  mkdir -p "$target"
  install -m 0644 "$license" "$target/${relative:t}"
done < <(find "$project_dir/Vendor/iTerm2/ThirdParty" "$project_dir/Vendor/iTerm2/submodules" \
  -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) -print)

identity="${OMNITERM_SIGN_IDENTITY:--}"
sign_args=(--force --sign "$identity")
if [[ "$identity" != - ]]; then
  sign_args+=(--options runtime --timestamp)
fi
# Sign nested code from the inside out. Apple explicitly discourages using
# --deep for distribution signing.
while IFS= read -r -d '' item; do
  if file -b "$item" | grep -q 'Mach-O'; then
    codesign "${sign_args[@]}" "$item"
  fi
done < <(find "$app/Contents" -type f -perm -111 -print0)
while IFS= read -r item; do
  codesign "${sign_args[@]}" "$item"
done < <(find "$app/Contents" -depth \
  \( -name '*.framework' -o -name '*.dylib' -o -name '*.xpc' -o -name '*.app' \) \
  ! -path "$app" -print)
codesign "${sign_args[@]}" --entitlements "$project_dir/Vendor/iTerm2/iTerm2.entitlements" "$app"
codesign --verify --deep --strict "$app"

mkdir -p "$release_dir"
work=$(mktemp -d "${TMPDIR:-/tmp}/omniterm-release.XXXXXX")
trap 'rm -rf "$work"' EXIT
ditto "$app" "$work/OmniTerm.app"
ln -s /Applications "$work/Applications"
install -m 0755 "$project_dir/scripts/install.sh" "$work/Install OmniTerm.command"
install -m 0644 "$project_dir/README.md" "$work/README.md"
install -m 0644 "$project_dir/CHANGELOG.md" "$work/CHANGELOG.md"
install -m 0644 "$project_dir/LICENSE" "$work/LICENSE"
install -m 0644 "$project_dir/LICENSES/GPL-3.0.txt" "$work/GPL-3.0.txt"
install -m 0644 "$project_dir/THIRD_PARTY_NOTICES.md" "$work/THIRD_PARTY_NOTICES.md"

dmg="$release_dir/OmniTerm-$version.dmg"
zip="$release_dir/OmniTerm-$version.zip"
rm -f "$dmg" "$zip" "$release_dir/SHA256SUMS"
hdiutil create -quiet -volname "OmniTerm $version" -srcfolder "$work" -format UDZO "$dmg"
ditto -c -k --sequesterRsrc --keepParent "$app" "$zip"

dmg_sign_args=(--force --sign "$identity")
if [[ "$identity" != - ]]; then
  dmg_sign_args+=(--timestamp)
fi
codesign "${dmg_sign_args[@]}" "$dmg"
codesign --verify --strict "$dmg"

if [[ -n "${OMNITERM_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$dmg" --keychain-profile "$OMNITERM_NOTARY_PROFILE" --wait
  xcrun stapler staple "$dmg"
fi
(cd "$release_dir" && shasum -a 256 "${dmg:t}" "${zip:t}" > SHA256SUMS)
print "$dmg"
print "$zip"
