#!/bin/zsh
# Maintains patches/iterm2-omniterm.patch, the tracked record of OmniTerm's native
# changes to the Vendor/iTerm2 checkout.
#
#   iterm2-fork.sh latest           Print the newest upstream nightly tag.
#   iterm2-fork.sh current          Print the tag the checkout is pinned to.
#   iterm2-fork.sh export           Rewrite the patch from the checkout.
#   iterm2-fork.sh update [TAG]     Move a clean, unpatched checkout to TAG (default: newest),
#                                   reapply the patch, and rewrite it. Exits 3 on conflicts,
#                                   leaving the conflicted checkout for manual resolution.
set -euo pipefail

project_dir="${0:A:h:h}"
fork_dir="${OMNITERM_FORK_DIR:-$project_dir/Vendor/iTerm2}"
patch_file="${OMNITERM_PATCH_FILE:-$project_dir/patches/iterm2-omniterm.patch}"

# Generated dependency output is dirty by design and must never enter the patch.
patch_paths=(
  sources
  plists
  docs/notes-3.7.txt
  iTerm2.xcodeproj/project.pbxproj
  ModernTests/OmniSchedulerTests.m
)

latest_tag() {
  git -C "$fork_dir" ls-remote --tags --refs origin 'refs/tags/v*-nightly' |
    sed 's#.*refs/tags/##' |
    sort -V |
    tail -n 1
}

current_tag() {
  git -C "$fork_dir" describe --tags --exact-match HEAD
}

export_patch() {
  git -C "$fork_dir" diff --binary --full-index HEAD -- "${patch_paths[@]}" >"$patch_file.tmp"
  [[ -s "$patch_file.tmp" ]] || {
    rm -f "$patch_file.tmp"
    print -u2 "Refusing to write an empty patch: OmniTerm's changes are not applied to $fork_dir."
    exit 1
  }
  mv "$patch_file.tmp" "$patch_file"
}

update_fork() {
  local tag="$1"
  [[ -z "$(git -C "$fork_dir" status --porcelain --untracked-files=no)" ]] || {
    print -u2 "$fork_dir has local changes. Update from a clean, unpatched checkout."
    exit 1
  }
  if [[ "$tag" == "$(current_tag 2>/dev/null || true)" ]]; then
    print "Already on $tag."
    return
  fi
  git -C "$fork_dir" fetch --quiet --depth=1 origin tag "$tag"
  git -C "$fork_dir" checkout --quiet --detach "$tag"
  if ! git -C "$fork_dir" apply --3way "$patch_file"; then
    print -u2 "The patch needs manual resolution on $tag:"
    git -C "$fork_dir" diff --name-only --diff-filter=U >&2
    exit 3
  fi
  export_patch
  print "Updated to $tag."
}

case "${1:-}" in
  latest) latest_tag ;;
  current) current_tag ;;
  export) export_patch ;;
  update) update_fork "${2:-$(latest_tag)}" ;;
  *)
    print -u2 "Usage: $0 latest|current|export|update [TAG]"
    exit 2
    ;;
esac
