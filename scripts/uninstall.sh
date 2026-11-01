#!/bin/zsh
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--purge] [--destination DIR] [--bin-dir DIR]

Removes OmniTerm, its previous version, its launchers, and its MCP registrations.
Agents, settings, and run history are preserved unless --purge is given.
EOF
}

purge=0
destination="/Applications"
bin_dir="$HOME/.local/bin"

while (( $# )); do
  case "$1" in
    --purge) purge=1; shift ;;
    --destination) destination="${2:A}"; shift 2 ;;
    --bin-dir) bin_dir="${2:A}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) print -u2 "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
done

if [[ -x "$bin_dir/omni" ]]; then
  "$bin_dir/omni" mcp uninstall || true
fi
for launcher in "$bin_dir/omni" "$bin_dir/omni-mcp"; do
  if [[ -L "$launcher" && "$(readlink "$launcher")" == *OmniTerm.app/* ]]; then
    rm "$launcher"
  fi
done
for app in "$destination/OmniTerm.app" "$destination/OmniTerm.previous.app"; do
  [[ -d "$app" ]] || continue
  identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)
  if [[ "$identifier" == com.omniterm.app ]]; then
    rm -rf "$app"
  fi
done
if pgrep -x OmniTerm >/dev/null; then
  print "OmniTerm is still running. Quit it with Terminate OmniTerm Completely in the menu bar."
fi
if (( purge )); then
  rm -rf "$HOME/.omni" "$HOME/Library/Application Support/OmniTerm"
  print "Removed OmniTerm and all local scheduler data."
else
  print "Removed OmniTerm. Preserved ~/.omni and scheduler history."
fi
