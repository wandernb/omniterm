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

installed_app="$destination/OmniTerm.app"
backup="$destination/OmniTerm.previous.app"

# Refuse conflicts before staging, rotating apps, or replacing either launcher.
# -e alone misses dangling symlinks, which are still occupied destinations.
for bundle_path in "$installed_app" "$backup"; do
  if [[ -e "$bundle_path" || -L "$bundle_path" ]]; then
    bundle_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$bundle_path/Contents/Info.plist" 2>/dev/null || true)
    if [[ -L "$bundle_path" || ! -d "$bundle_path" || "$bundle_id" != com.omniterm.app ]]; then
      print -u2 "Refusing to replace non-OmniTerm app at $bundle_path"
      exit 1
    fi
  fi
done
for helper in omni omni-mcp; do
  launcher_path="$bin_dir/$helper"
  expected_target="$installed_app/Contents/Resources/Helpers/$helper"
  if [[ -e "$launcher_path" || -L "$launcher_path" ]]; then
    if [[ ! -L "$launcher_path" || "$(readlink "$launcher_path")" != "$expected_target" ]]; then
      print -u2 "Refusing to replace unrelated launcher at $launcher_path"
      exit 1
    fi
  fi
done

mkdir -p "$destination" "$bin_dir"
staged_app="$destination/.OmniTerm.installing.$$.app"
rollback_dir=""
saved_previous=0
rotated_active=0
published=0
cleanup_install() {
  local original_exit=$?
  local recovery_failed=0
  if (( ! published )); then
    if (( rotated_active )); then
      if mv "$backup" "$installed_app"; then
        rotated_active=0
      else
        recovery_failed=1
        print -u2 "Could not restore the active app; recoverable copy retained at $backup"
      fi
    fi
    if (( saved_previous )); then
      if (( ! recovery_failed )) && mv "$rollback_dir/previous.app" "$backup"; then
        saved_previous=0
      else
        recovery_failed=1
        print -u2 "Previous app retained for recovery at $rollback_dir/previous.app"
      fi
    fi
  fi
  [[ -e "$staged_app" ]] && rm -rf "$staged_app"
  if [[ -n "$rollback_dir" ]] && (( ! recovery_failed )); then
    rm -rf "$rollback_dir"
  fi
  return "$original_exit"
}
trap cleanup_install EXIT
ditto "$source_app" "$staged_app"
codesign --verify --deep --strict "$staged_app"

if [[ -e "$installed_app" ]]; then
  if [[ -e "$backup" ]]; then
    rollback_dir=$(mktemp -d "$destination/.OmniTerm.rollback.XXXXXX")
    mv "$backup" "$rollback_dir/previous.app"
    saved_previous=1
  fi
  mv "$installed_app" "$backup"
  rotated_active=1
fi
mv "$staged_app" "$installed_app"
published=1

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
