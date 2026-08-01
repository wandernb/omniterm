#!/bin/zsh
set -euo pipefail

purge=0
[[ "${1:-}" == --purge ]] && purge=1
app="/Applications/OmniTerm.app"

if [[ -x "$HOME/.local/bin/omni" ]]; then
  "$HOME/.local/bin/omni" mcp uninstall || true
fi
for launcher in "$HOME/.local/bin/omni" "$HOME/.local/bin/omni-mcp"; do
  [[ -L "$launcher" && "$(readlink "$launcher")" == *OmniTerm.app/* ]] && rm "$launcher"
done
if [[ -d "$app" ]]; then
  identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)
  [[ "$identifier" == com.omniterm.app ]] && rm -rf "$app"
fi
if (( purge )); then
  rm -rf "$HOME/.omni" "$HOME/Library/Application Support/OmniTerm"
  print "Removed OmniTerm and all local scheduler data."
else
  print "Removed OmniTerm. Preserved ~/.omni and scheduler history."
fi
