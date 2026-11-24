#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
version="${1:-}"
[[ "$version" == <->.<->.<-> ]] || { print -u2 "Usage: $0 MAJOR.MINOR.PATCH"; exit 2; }
release_dir="$project_dir/dist/release"
app="$project_dir/dist/OmniTerm.app"
release_build_dir="${OMNITERM_RELEASE_BUILD_DIR:-$project_dir/.release-build}"

# Release binaries must not name this machine's folders: compiled-in file names
# are mapped at build time and the debug map is stripped before signing.
path_config="$release_build_dir/release-paths.xcconfig"
"$project_dir/scripts/release-paths.sh" xcconfig "$project_dir" "$path_config"
if [[ -n "${OMNITERM_UNIVERSAL:-}" ]]; then
  XCODE_XCCONFIG_FILE="$path_config" UNIVERSAL=1 OMNITERM_BUILD_DIR="$release_build_dir" OMNITERM_CONFIGURATION=Deployment OMNITERM_SKIP_SIGNING=1 "$project_dir/scripts/build-app.sh"
else
  XCODE_XCCONFIG_FILE="$path_config" OMNITERM_BUILD_DIR="$release_build_dir" OMNITERM_CONFIGURATION=Deployment OMNITERM_SKIP_SIGNING=1 "$project_dir/scripts/build-app.sh"
fi
"$project_dir/scripts/release-paths.sh" strip "$app"

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

work=$(mktemp -d "${TMPDIR:-/tmp}/omniterm-release.XXXXXX")
candidate=""
retain_candidate=0
cleanup_release() {
  local original_exit=$?
  rm -rf "$work"
  if [[ -n "$candidate" ]] && (( ! retain_candidate )); then
    rm -rf "$candidate"
  fi
  return "$original_exit"
}
trap cleanup_release EXIT

identity="${OMNITERM_SIGN_IDENTITY:--}"
mkdir -p "$work/signing"
entitlements="$work/signing/OmniTerm.entitlements"
"$project_dir/scripts/signing-entitlements.sh" \
  "$project_dir/Vendor/iTerm2/iTerm2.entitlements" "$entitlements" "$identity"
sign_args=(--force --sign "$identity")
if [[ "$identity" != - ]]; then
  sign_args+=(--options runtime --timestamp)
fi
# Sign nested code from the inside out. Apple explicitly discourages using
# --deep for distribution signing. The app's own executable is signed last, with
# the bundle: signing it here would fail on any nested code not yet reached.
while IFS= read -r -d '' item; do
  if file -b "$item" | grep -q 'Mach-O'; then
    codesign "${sign_args[@]}" "$item"
  fi
done < <(find "$app/Contents" -type f -perm -111 ! -path "$app/Contents/MacOS/OmniTerm" -print0)
while IFS= read -r item; do
  codesign "${sign_args[@]}" "$item"
done < <(find "$app/Contents" -depth \
  \( -name '*.framework' -o -name '*.dylib' -o -name '*.xpc' -o -name '*.app' \) \
  ! -path "$app" -print)
# A widget must be sandboxed, so it is signed with its own entitlements.
while IFS= read -r item; do
  codesign "${sign_args[@]}" --entitlements "$project_dir/widget/OmniInboxWidget.entitlements" "$item"
done < <(find "$app/Contents/PlugIns" -depth -name '*.appex' -print 2>/dev/null)
codesign "${sign_args[@]}" --entitlements "$entitlements" "$app"
codesign --verify --deep --strict "$app"

mkdir -p "$release_dir"
rm -rf "$work/signing"
ditto "$app" "$work/OmniTerm.app"
ln -s /Applications "$work/Applications"
install -m 0755 "$project_dir/scripts/install.sh" "$work/Install OmniTerm.command"
install -m 0644 "$project_dir/README.md" "$work/README.md"
install -m 0644 "$project_dir/CHANGELOG.md" "$work/CHANGELOG.md"
install -m 0644 "$project_dir/LICENSE" "$work/LICENSE"
install -m 0644 "$project_dir/LICENSES/GPL-3.0.txt" "$work/GPL-3.0.txt"
install -m 0644 "$project_dir/THIRD_PARTY_NOTICES.md" "$work/THIRD_PARTY_NOTICES.md"

candidate=$(mktemp -d "$release_dir/.omniterm-release.XXXXXX")
dmg="$candidate/OmniTerm-$version.dmg"
zip="$candidate/OmniTerm-$version.zip"
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
(cd "$candidate" && shasum -a 256 "${dmg:t}" "${zip:t}" > SHA256SUMS)
artifacts=("${dmg:t}" "${zip:t}" SHA256SUMS)
mkdir "$candidate/previous"
for artifact in "${artifacts[@]}"; do
  [[ -s "$candidate/$artifact" ]] || { print -u2 "Empty release artifact: $artifact"; exit 1; }
  if [[ -d "$release_dir/$artifact" ]]; then
    print -u2 "Release destination is a directory: $release_dir/$artifact"
    exit 1
  fi
  if [[ -e "$release_dir/$artifact" || -L "$release_dir/$artifact" ]]; then
    cp -pP "$release_dir/$artifact" "$candidate/previous/$artifact"
  fi
done

# Keep the complete previous set until every publication rename succeeds.
for artifact in "${artifacts[@]}"; do
  if mv -f "$candidate/$artifact" "$release_dir/$artifact"; then
    continue
  else
    publication_exit=$?
    for restore_artifact in "${artifacts[@]}"; do
      previous="$candidate/previous/$restore_artifact"
      if [[ -e "$previous" || -L "$previous" ]]; then
        if cp -pP "$previous" "$candidate/$restore_artifact" && \
            mv -f "$candidate/$restore_artifact" "$release_dir/$restore_artifact"; then
          continue
        fi
      elif rm -f "$release_dir/$restore_artifact"; then
        continue
      fi
      retain_candidate=1
    done
    if (( retain_candidate )); then
      print -u2 "Release rollback incomplete; recovery copies retained at $candidate/previous"
    fi
    exit "$publication_exit"
  fi
done
print "$release_dir/${dmg:t}"
print "$release_dir/${zip:t}"
