#!/bin/zsh
# Fails when an app bundle holds resources that shadow its Base.lproj ones.
#
# NSBundle looks in Contents/Resources before any .lproj folder, so a nib left
# at the root by an older build silently replaces the current one. Xcode only
# adds to an existing product, so these files survive incremental builds.
set -euo pipefail

app_dir="${1:?usage: check-bundle.sh <path to .app>}"
resources="$app_dir/Contents/Resources"

if [[ ! -d "$resources/Base.lproj" ]]; then
    print -u2 "No Base.lproj in $app_dir"
    exit 1
fi

shadowed=()
for base_file in "$resources"/Base.lproj/*(N); do
    name="${base_file:t}"
    if [[ -e "$resources/$name" ]]; then
        shadowed+=("$name")
    fi
done

if (( ${#shadowed} > 0 )); then
    print -u2 "${#shadowed} resources shadow their Base.lproj copy in $app_dir:"
    print -u2 -l -- "  ${^shadowed[1,10]}"
    print -u2 "Delete the build product and rebuild."
    exit 1
fi
