#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
fork_dir="$project_dir/Vendor/iTerm2"
patch_file="$project_dir/patches/iterm2-omniterm.patch"

git -C "$project_dir" submodule update --init --recursive
if git -C "$fork_dir" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
  print "OmniTerm's iTerm2 patch is already applied."
elif git -C "$fork_dir" apply --check "$patch_file"; then
  git -C "$fork_dir" apply "$patch_file"
  print "Applied OmniTerm's native iTerm2 changes."
else
  print -u2 "The iTerm2 checkout is neither clean nor already patched."
  print -u2 "Preserve your work, restore the pinned submodule commit, and retry."
  exit 1
fi

print "Bootstrap complete. Install native dependencies when needed with:"
print "  (cd Vendor/iTerm2 && make paranoid-deps)"
