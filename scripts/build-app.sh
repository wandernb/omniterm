#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
fork_dir="$project_dir/Vendor/iTerm2"
app_dir="$project_dir/dist/OmniTerm.app"
configuration="${OMNITERM_CONFIGURATION:-Development}"
build_dir="${OMNITERM_BUILD_DIR:-}"

"$project_dir/scripts/iterm2-fork.sh" export

cd "$fork_dir"
if [[ "$configuration" == Deployment ]]; then
    cp plists/release-iTerm2.plist plists/iTerm2.plist
fi

# Xcode never removes files from an existing product, so start from an empty
# one. Compiled objects stay in Intermediates and are still reused.
if [[ -n "$build_dir" ]]; then
    previous_products=("$build_dir/$configuration/OmniTerm.app"(N))
else
    previous_products=("$HOME"/Library/Developer/Xcode/DerivedData/iTerm2-*/Build/Products/$configuration/OmniTerm.app(N))
fi
if (( ${#previous_products} > 0 )); then
    rm -rf "${previous_products[@]}"
fi

if [[ -n "$build_dir" ]]; then
    BUILD_DIR="$build_dir" tools/build.sh "$configuration"
else
    tools/build.sh "$configuration"
fi

if [[ -n "$build_dir" ]]; then
    products=("$build_dir/$configuration/OmniTerm.app"(Nom))
else
    products=("$HOME"/Library/Developer/Xcode/DerivedData/iTerm2-*/Build/Products/$configuration/OmniTerm.app(Nom))
fi
if (( ${#products} == 0 )); then
    print -u2 "Could not locate the native OmniTerm.app build product."
    exit 1
fi

rm -rf "$app_dir"
ditto "$products[1]" "$app_dir"
"$project_dir/scripts/check-bundle.sh" "$app_dir"
mkdir -p "$app_dir/Contents/Resources/Helpers"
install -m 0755 "$project_dir/helpers/omni" "$app_dir/Contents/Resources/Helpers/omni"
install -m 0755 "$project_dir/helpers/omni-runner" "$app_dir/Contents/Resources/Helpers/omni-runner"
install -m 0755 "$project_dir/helpers/omni-mcp" "$app_dir/Contents/Resources/Helpers/omni-mcp"
install -m 0644 "$project_dir/assets/OmniTerm.icns" "$app_dir/Contents/Resources/OmniTerm.icns"
/usr/libexec/PlistBuddy -c 'Set :CFBundleIconFile OmniTerm.icns' "$app_dir/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Delete :CFBundleIconName' "$app_dir/Contents/Info.plist" 2>/dev/null || true
mkdir -p "$HOME/.local/bin"
ln -sfn "$app_dir/Contents/Resources/Helpers/omni" "$HOME/.local/bin/omni"
ln -sfn "$app_dir/Contents/Resources/Helpers/omni-mcp" "$HOME/.local/bin/omni-mcp"
if [[ -z "${OMNITERM_SKIP_SIGNING:-}" ]]; then
    codesign --force --deep --sign - "$app_dir"
    codesign --verify --deep --strict "$app_dir"
fi

echo "$app_dir"
