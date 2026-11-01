#!/bin/zsh
# Write the entitlements for one signing identity. iTerm2's file carries Xcode
# build variables that only Xcode expands, and restricted entitlements that macOS
# refuses to launch on ad-hoc code.
set -euo pipefail

source_file="${1:-}"
output_file="${2:-}"
identity="${3:-}"
[[ -n "$source_file" && -n "$output_file" && -n "$identity" ]] || {
  print -u2 "Usage: $0 SOURCE OUTPUT IDENTITY  (identity - means ad-hoc)"
  exit 2
}

if [[ "$identity" == - ]]; then
  cp "$source_file" "$output_file"
  /usr/libexec/PlistBuddy -c 'Delete :keychain-access-groups' "$output_file"
  /usr/libexec/PlistBuddy -c 'Delete :com.apple.security.application-groups' "$output_file"
else
  team="${OMNITERM_TEAM_ID:-}"
  if [[ -z "$team" && "$identity" =~ '\(([A-Z0-9]{10})\)$' ]]; then
    team="$match[1]"
  fi
  [[ "$team" =~ '^[A-Z0-9]{10}$' ]] || {
    print -u2 "Cannot find a team ID in '$identity'. Use the full identity name or set OMNITERM_TEAM_ID."
    exit 1
  }
  sed "s/\$(TeamIdentifierPrefix)/$team./g" "$source_file" > "$output_file"
fi

if grep -q '\$(' "$output_file"; then
  print -u2 "Unexpanded build variable in entitlements: $(grep '\$(' "$output_file" | head -1)"
  exit 1
fi
plutil -lint -s "$output_file"
