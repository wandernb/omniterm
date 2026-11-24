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

release_revision=$(git -C "$project_dir" rev-parse -q --verify "refs/tags/$tag^{commit}") || {
  print -u2 "Tag $tag does not exist."
  exit 1
}
fork_revision=$(git -C "$project_dir" rev-parse "$release_revision:Vendor/iTerm2")
work=$(mktemp -d "${TMPDIR:-/tmp}/omniterm-source.XXXXXX")
output_work=""
trap 'rm -rf "$work"; if [[ -n "$output_work" ]]; then rm -rf "$output_work"; fi' EXIT
source_dir="$work/$name"
mkdir -p "$source_dir/Vendor/iTerm2"

# Read gitlinks from each selected commit, never from checkout status. The
# checkout supplies the object database only; its HEAD may be a different release.
archive_repository() {
  local repository="$1" revision="$2" destination="$3"
  local entry metadata pinned relative
  if [[ ! -e "$repository/.git" ]] || ! git -C "$repository" cat-file -e "$revision^{commit}"; then
    print -u2 "Pinned revision $revision is unavailable in submodule: $repository"
    exit 1
  fi
  mkdir -p "$destination"
  git -C "$repository" archive "$revision" | tar -x -C "$destination"
  git -C "$repository" ls-tree -r -z "$revision" | while IFS= read -r -d $'\0' entry; do
    metadata="${entry%%$'\t'*}"
    [[ "$metadata" == '160000 commit '* ]] || continue
    pinned="${metadata##* }"
    relative="${entry#*$'\t'}"
    archive_repository "$repository/$relative" "$pinned" "$destination/$relative"
  done
}

git -C "$project_dir" archive "$release_revision" -- . ':(exclude)releases' | tar -x -C "$source_dir"
archive_repository "$fork_dir" "$fork_revision" "$source_dir/Vendor/iTerm2"

cat > "$source_dir/SOURCE.md" <<NOTES
# OmniTerm $version corresponding source

This is the tree of tag $tag without the binaries in releases/, plus iTerm2 at
${fork_revision[1,12]} and every nested submodule at its pinned
revision. Vendor/iTerm2 is unmodified upstream source. Apply OmniTerm's changes,
then build:

    git -C Vendor/iTerm2 init -q
    git -C Vendor/iTerm2 apply ../../patches/iterm2-omniterm.patch
    (cd Vendor/iTerm2 && make paranoid-deps)
    ./scripts/package-release.sh $version
NOTES

(cd "$source_dir/Vendor/iTerm2" && git init -q && git apply --check ../../patches/iterm2-omniterm.patch && rm -rf .git)
mkdir -p "${archive:h}"
if [[ -d "$archive" ]]; then
  print -u2 "Archive destination is a directory: $archive"
  exit 1
fi
# Compress beside the destination so a failed rebuild preserves the last archive
# and the completed candidate can replace it with a same-filesystem rename.
output_work=$(mktemp -d "$archive.tmp.XXXXXX")
tar -czf "$output_work/$name.tar.gz" -C "$work" "$name"
mv -f "$output_work/$name.tar.gz" "$archive"
print "$archive"
