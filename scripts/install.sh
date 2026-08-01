#!/bin/zsh
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install.sh [--app PATH] [--destination DIR] [--bin-dir DIR]
                  [--skip-launch] [--skip-mcp]

Installs OmniTerm, its `omni` CLI, and its `omni-mcp` adapter. Existing agent
configuration and run history are preserved.
EOF
}

script_dir="${0:A:h}"
if [[ -d "$script_dir/OmniTerm.app" ]]; then
  default_app="$script_dir/OmniTerm.app"
else
  default_app="$script_dir/../dist/OmniTerm.app"
fi
source_app="${OMNITERM_APP_PATH:-$default_app}"
destination="/Applications"
bin_dir="$HOME/.local/bin"
launch=1
install_mcp=1

while (( $# )); do
  case "$1" in
    --app) source_app="${2:A}"; shift 2 ;;
    --destination) destination="${2:A}"; shift 2 ;;
    --bin-dir) bin_dir="${2:A}"; shift 2 ;;
    --skip-launch) launch=0; shift ;;
    --skip-mcp) install_mcp=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) print -u2 "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
done

[[ -d "$source_app" ]] || { print -u2 "OmniTerm.app not found: $source_app"; exit 1; }
identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$source_app/Contents/Info.plist")
[[ "$identifier" == com.omniterm.app ]] || { print -u2 "Refusing unexpected bundle: $identifier"; exit 1; }
codesign --verify --deep --strict "$source_app"

mkdir -p "$destination" "$bin_dir"
installed_app="$destination/OmniTerm.app"
staged_app="$destination/.OmniTerm.installing.$$.app"
trap '[[ -e "$staged_app" ]] && rm -rf "$staged_app"' EXIT
ditto "$source_app" "$staged_app"
codesign --verify --deep --strict "$staged_app"

if [[ -e "$installed_app" ]]; then
  installed_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$installed_app/Contents/Info.plist" 2>/dev/null || true)
  [[ "$installed_id" == com.omniterm.app ]] || {
    print -u2 "Refusing to replace non-OmniTerm app at $installed_app"
    exit 1
  }
  backup="$destination/OmniTerm.previous.app"
  [[ -e "$backup" ]] && rm -rf "$backup"
  mv "$installed_app" "$backup"
fi
mv "$staged_app" "$installed_app"

ln -sfn "$installed_app/Contents/Resources/Helpers/omni" "$bin_dir/omni"
ln -sfn "$installed_app/Contents/Resources/Helpers/omni-mcp" "$bin_dir/omni-mcp"

if (( launch )); then
  open -g "$installed_app"
fi
if (( install_mcp )); then
  "$bin_dir/omni" mcp install
fi

print "Installed OmniTerm at $installed_app"
print "Installed CLI at $bin_dir/omni"
print "Your agents and settings remain in $HOME/.omni"
