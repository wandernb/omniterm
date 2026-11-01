#!/bin/zsh
# Build the corresponding-source archive for a release: the tagged tree, unmodified
# iTerm2, and every nested submodule at its pinned revision. Binaries stay out.
set -euo pipefail

project_dir="${0:A:h:h}"
version="${1:-}"
[[ "$version" == <->.<->.<-> ]] || { print -u2 "Usage: $0 MAJOR.MINOR.PATCH"; exit 2; }
tag="v$version"
fork_dir="$project_dir/Vendor/iTerm2"
name="OmniTerm-$version-source"
archive="$project_dir/dist/release/$name.tar.gz"

git -C "$project_dir" rev-parse -q --verify "refs/tags/$tag" >/dev/null || {
  print -u2 "Tag $tag does not exist."
  exit 1
}
work=$(mktemp -d "${TMPDIR:-/tmp}/omniterm-source.XXXXXX")
trap 'rm -rf "$work"' EXIT
source_dir="$work/$name"
mkdir -p "$source_dir/Vendor/iTerm2"

git -C "$project_dir" archive "$tag" -- . ':(exclude)releases' | tar -x -C "$source_dir"
git -C "$fork_dir" archive HEAD | tar -x -C "$source_dir/Vendor/iTerm2"

git -C "$fork_dir" submodule status --recursive | while read -r line; do
  relative="${${line[2,-1]#* }%% *}"
  if [[ "$line[1]" == - ]]; then
    print -u2 "Submodule is not initialized: $relative"
    exit 1
  fi
  mkdir -p "$source_dir/Vendor/iTerm2/$relative"
  git -C "$fork_dir/$relative" archive HEAD | tar -x -C "$source_dir/Vendor/iTerm2/$relative"
done

cat > "$source_dir/SOURCE.md" <<NOTES
# OmniTerm $version corresponding source

This is the tree of tag $tag without the binaries in releases/, plus iTerm2 at
$(git -C "$fork_dir" rev-parse --short=12 HEAD) and every nested submodule at its pinned
revision. Vendor/iTerm2 is unmodified upstream source. Apply OmniTerm's changes,
then build:

    git -C Vendor/iTerm2 init -q
    git -C Vendor/iTerm2 apply ../../patches/iterm2-omniterm.patch
    (cd Vendor/iTerm2 && make paranoid-deps)
    ./scripts/package-release.sh $version
NOTES

(cd "$source_dir/Vendor/iTerm2" && git init -q && git apply --check ../../patches/iterm2-omniterm.patch && rm -rf .git)
mkdir -p "${archive:h}"
tar -czf "$archive" -C "$work" "$name"
print "$archive"
