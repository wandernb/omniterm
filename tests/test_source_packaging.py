import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("zsh"), "source packaging requires zsh")
class SourcePackagingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="omniterm-source-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/package-source.sh", self.root / "scripts")
        self.upstream = "Vendor/iTerm2"
        self.dependency = self.upstream + "/deps/module"
        self.nested = self.dependency + "/nested"
        self.root_revision = "1" * 40
        self.upstream_revision = "2" * 40
        self.dependency_revision = "3" * 40
        self.nested_revision = "4" * 40
        patch = ("diff --git a/omni.txt b/omni.txt\n"
                 "new file mode 100644\n--- /dev/null\n+++ b/omni.txt\n"
                 "@@ -0,0 +1 @@\n+OmniTerm patch\n")
        self.repositories = {
            ".": {"head": self.root_revision, "revisions": {
                self.root_revision: {"files": {
                    "README.md": "Tagged project\n", "LICENSE": "License\n",
                    "THIRD_PARTY_NOTICES.md": "Notices\n",
                    "patches/iterm2-omniterm.patch": patch,
                }, "links": {self.upstream: self.upstream_revision}},
            }},
            self.upstream: {"head": self.upstream_revision, "revisions": {
                self.upstream_revision: {"files": {"upstream.txt": "Pinned upstream\n"},
                    "links": {"deps/module": self.dependency_revision}},
            }},
            self.dependency: {"head": self.dependency_revision, "revisions": {
                self.dependency_revision: {"files": {"dependency.txt": "Pinned dependency\n"},
                    "links": {"nested": self.nested_revision}},
            }},
            self.nested: {"head": self.nested_revision, "revisions": {
                self.nested_revision: {"files": {"nested.txt": "Pinned nested\n"},
                    "links": {}},
            }},
        }
        for relative in self.repositories:
            (self.root / relative / ".git").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        shim = self.bin / "git"
        # Stub only Git's repository/object boundary: run the actual packager,
        # tar/gzip and patch applicability check. No commits, network or real
        # checkout mutations are needed to represent release/checkout drift.
        shim.write_text(f"#!{sys.executable}\n" + '''import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

arguments = sys.argv[1:]
if not arguments or arguments[0] != "-C":
    os.execv(os.environ["REAL_GIT"], [os.environ["REAL_GIT"], *arguments])
root = Path(os.environ["SOURCE_FIXTURE"]).resolve()
repository = Path(arguments[1]).resolve()
relative = str(repository.relative_to(root))
arguments = arguments[2:]
repositories = json.loads((root / "repositories.json").read_text())
data = repositories[relative]
command = arguments[0]

def resolve_revision(value):
    if value == "HEAD":
        return data["head"]
    if relative == "." and value in ("v1.2.3", "refs/tags/v1.2.3",
                                    "refs/tags/v1.2.3^{commit}", "v1.2.3^{commit}"):
        return repositories["."]["head"]
    return value.removesuffix("^{commit}")

if command == "rev-parse":
    if arguments[-1] == "--show-toplevel":
        print(repository)
    elif ":Vendor/iTerm2" in arguments[-1]:
        print(repositories["."]["revisions"][repositories["."]["head"]]["links"]["Vendor/iTerm2"])
    else:
        revision = resolve_revision(arguments[-1])
        if revision not in data["revisions"]:
            sys.exit(1)
        print(revision[:12] if "--short=12" in arguments else revision)
elif command == "cat-file":
    sys.exit(0 if resolve_revision(arguments[-1]) in data["revisions"] else 1)
elif command == "archive":
    revision = resolve_revision(arguments[1])
    if revision not in data["revisions"]:
        sys.exit("Missing revision " + revision)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, content in data["revisions"][revision]["files"].items():
            payload = content.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
elif command == "ls-tree":
    revision = resolve_revision(arguments[-1])
    for name, pinned in data["revisions"][revision]["links"].items():
        sys.stdout.buffer.write(("160000 commit " + pinned + "\\t" + name + "\\0").encode())
elif command == "submodule" and arguments[1:] == ["status", "--recursive"]:
    for name, dependency in repositories.items():
        if name.startswith(relative + "/"):
            print(" " + dependency["head"] + " " + name[len(relative) + 1:])
else:
    sys.exit("Unexpected Git fixture operation: " + repr(arguments))
''')
        shim.chmod(0o755)

    def package(self):
        (self.root / "repositories.json").write_text(json.dumps(self.repositories))
        return subprocess.run(
            ["zsh", str(self.root / "scripts/package-source.sh"), "1.2.3"],
            env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                 "REAL_GIT": shutil.which("git"), "SOURCE_FIXTURE": str(self.root)},
            capture_output=True, text=True, timeout=20,
        )

    def contents(self):
        archive_path = self.root / "dist/release/OmniTerm-1.2.3-source.tar.gz"
        with tarfile.open(archive_path) as archive:
            return {member.name.removeprefix("OmniTerm-1.2.3-source/"):
                    archive.extractfile(member).read().decode()
                    for member in archive if member.isfile()}

    def drift(self, relative):
        repository = self.repositories[relative]
        revision = "f" * 40
        repository["revisions"][revision] = {
            "files": {"checkout-only.txt": "Wrong checkout revision\n"}, "links": {},
        }
        repository["head"] = revision

    def assert_pinned_source(self):
        contents = self.contents()
        self.assertEqual(contents["README.md"], "Tagged project\n")
        self.assertEqual(contents[self.upstream + "/upstream.txt"], "Pinned upstream\n")
        self.assertEqual(contents[self.dependency + "/dependency.txt"], "Pinned dependency\n")
        self.assertEqual(contents[self.nested + "/nested.txt"], "Pinned nested\n")
        self.assertFalse(any(name.endswith("checkout-only.txt") for name in contents))
        self.assertIn(self.upstream_revision[:12], contents["SOURCE.md"])
        self.assertIn("LICENSE", contents)
        self.assertIn("THIRD_PARTY_NOTICES.md", contents)
        self.assertFalse(any("/.git/" in name for name in contents))

    def test_aligned_release_includes_all_recursive_sources_and_notices(self):
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_pinned_source()

    def prepare_real_git(self, annotated_tag=True):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

        def git(repository, *arguments, input_text=""):
            return subprocess.run(
                [shutil.which("git"), "-C", str(repository), *arguments],
                env=environment, input=input_text, check=True, capture_output=True,
                text=True, timeout=10,
            ).stdout.strip()

        def snapshot(repository, files, links):
            entries = {}
            for name, content in files.items():
                parent = entries
                components = name.split("/")
                for component in components[:-1]:
                    parent = parent.setdefault(component, {})
                blob = git(repository, "hash-object", "-w", "--stdin", input_text=content)
                parent[components[-1]] = ("100644", "blob", blob)
            for name, revision in links.items():
                parent = entries
                components = name.split("/")
                for component in components[:-1]:
                    parent = parent.setdefault(component, {})
                parent[components[-1]] = ("160000", "commit", revision)

            def write_tree(children):
                lines = []
                for name, entry in sorted(children.items()):
                    if isinstance(entry, dict):
                        entry = ("040000", "tree", write_tree(entry))
                    mode, kind, revision = entry
                    lines.append(f"{mode} {kind} {revision}\t{name}\n")
                return git(repository, "mktree", input_text="".join(lines))

            # Raw objects keep fixture creation independent of user identity,
            # signing, hooks and the workspace's refs, index and history.
            identity = "Fixture <fixture@example.invalid> 1600000000 +0000"
            return git(repository, "hash-object", "-w", "-t", "commit", "--stdin",
                       input_text=f"tree {write_tree(entries)}\nauthor {identity}\n"
                                  f"committer {identity}\n\nSource fixture\n")

        revisions = {}
        checkout_revisions = {}
        for relative, data in reversed(list(self.repositories.items())):
            repository = self.root / relative
            git(repository, "init", "--quiet")
            old_revision = data["head"]
            release = data["revisions"][old_revision]
            links = {name: revisions[pinned] for name, pinned in release["links"].items()}
            revisions[old_revision] = snapshot(repository, release["files"], links)
            checkout_files = {**release["files"], "checkout-only.txt": "Wrong checkout\n"}
            if relative == ".":
                checkout_files["README.md"] = "Checkout project\n"
            checkout_links = {name: checkout_revisions[str(Path(relative) / name)]
                              for name in release["links"]}
            checkout_revision = snapshot(repository, checkout_files, checkout_links)
            checkout_revisions[relative] = checkout_revision
            git(repository, "update-ref", "HEAD", checkout_revision)

        self.root_revision = revisions[self.root_revision]
        self.upstream_revision = revisions[self.upstream_revision]
        self.dependency_revision = revisions[self.dependency_revision]
        self.nested_revision = revisions[self.nested_revision]
        if annotated_tag:
            tag = git(self.root, "hash-object", "-w", "-t", "tag", "--stdin",
                      input_text=f"object {self.root_revision}\ntype commit\ntag v1.2.3\n"
                                 "tagger Fixture <fixture@example.invalid> 1600000000 +0000\n"
                                 "\nAnnotated release\n")
            git(self.root, "update-ref", "refs/tags/v1.2.3", tag)
        git(self.root, "update-ref", "refs/heads/v1.2.3", "HEAD")
        return environment

    def test_annotated_tag_wins_over_root_and_recursive_checkout_drift(self):
        environment = self.prepare_real_git()
        result = subprocess.run(
            ["zsh", str(self.root / "scripts/package-source.sh"), "1.2.3"],
            env=environment, capture_output=True, text=True, timeout=20,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_pinned_source()

    def test_branch_without_release_tag_does_not_replace_existing_archive(self):
        environment = self.prepare_real_git(annotated_tag=False)
        archive = self.root / "dist/release/OmniTerm-1.2.3-source.tar.gz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"previous source archive")

        result = subprocess.run(
            ["zsh", str(self.root / "scripts/package-source.sh"), "1.2.3"],
            env=environment, capture_output=True, text=True, timeout=20,
        )

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(archive.read_bytes(), b"previous source archive")

    def test_failed_compression_preserves_previous_archive_and_cleans_temporary_output(self):
        environment = self.prepare_real_git()
        scratch = self.root / "scratch"
        scratch.mkdir()
        environment.update(TMPDIR=str(scratch))
        # Apple's Git launcher initializes its platform cache inside TMPDIR.
        subprocess.run(
            [shutil.which("git"), "--version"], env=environment,
            check=True, capture_output=True, text=True, timeout=10,
        )
        scratch_baseline = set(scratch.iterdir())

        def package():
            return subprocess.run(
                ["zsh", str(self.root / "scripts/package-source.sh"), "1.2.3"],
                env=environment, capture_output=True, text=True, timeout=20,
            )

        baseline = package()
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assert_pinned_source()
        self.assertEqual(set(scratch.iterdir()), scratch_baseline)
        archive = self.root / "dist/release/OmniTerm-1.2.3-source.tar.gz"
        previous_archive = archive.read_bytes()
        compressor = self.bin / "tar"
        compressor.write_text(f"#!{sys.executable}\n" + '''import os
import sys
from pathlib import Path

if sys.argv[1] == "-czf":
    if os.environ["COMPRESSION_FAILURE"] == "after-open":
        Path(sys.argv[2]).write_bytes(b"incomplete compressed source")
    print("injected compression failure", file=sys.stderr)
    sys.exit(23)
os.execv(os.environ["REAL_TAR"], [os.environ["REAL_TAR"], *sys.argv[1:]])
''')
        compressor.chmod(0o755)
        # Keep real Git and tar extraction; inject only the final compressor.
        boundary = self.root / "compression-bin"
        boundary.mkdir()
        (boundary / "tar").symlink_to(compressor)
        environment.update(
            PATH=str(boundary) + os.pathsep + environment["PATH"],
            REAL_TAR=shutil.which("tar"),
        )

        for phase in ("before-open", "after-open"):
            with self.subTest(phase=phase):
                environment["COMPRESSION_FAILURE"] = phase
                result = package()
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn("injected compression failure", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(archive.read_bytes(), previous_archive)
                self.assertEqual(list(archive.parent.iterdir()), [archive])
                self.assertEqual(set(scratch.iterdir()), scratch_baseline)

        environment["PATH"] = os.environ["PATH"]
        recovered = package()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(recovered.stdout.strip(), str(archive))
        self.assert_pinned_source()
        self.assertEqual(list(archive.parent.iterdir()), [archive])
        self.assertEqual(set(scratch.iterdir()), scratch_baseline)

    def test_directory_at_archive_destination_is_rejected_without_writing_inside_it(self):
        archive = self.root / "dist/release/OmniTerm-1.2.3-source.tar.gz"
        archive.mkdir(parents=True)
        sentinel = archive / "keep.txt"
        sentinel.write_text("unrelated directory content")

        result = self.package()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertEqual(sentinel.read_text(), "unrelated directory content")
        self.assertEqual(list(archive.iterdir()), [sentinel])
        self.assertEqual(list(archive.parent.iterdir()), [archive])

    def test_release_pin_wins_over_upstream_and_recursive_checkout_heads(self):
        for relative in (self.upstream, self.dependency, self.nested):
            self.drift(relative)
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_pinned_source()

    def test_nested_pin_wins_even_when_parent_checkout_is_aligned(self):
        self.drift(self.nested)
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_pinned_source()

    def test_recursive_gitlink_path_preserves_spaces(self):
        spaced_dependency = self.upstream + "/deps/module with spaces"
        spaced_nested = spaced_dependency + "/nested"
        self.repositories[self.upstream]["revisions"][self.upstream_revision]["links"] = {
            "deps/module with spaces": self.dependency_revision,
        }
        self.repositories[spaced_dependency] = self.repositories.pop(self.dependency)
        self.repositories[spaced_nested] = self.repositories.pop(self.nested)
        (self.root / self.dependency).rename(self.root / spaced_dependency)
        self.dependency, self.nested = spaced_dependency, spaced_nested
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_pinned_source()

    def test_uninitialized_submodule_does_not_fall_back_to_parent_repository(self):
        (self.root / self.nested / ".git").rmdir()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.nested_revision, result.stderr)
        self.assertFalse((self.root / "dist/release/OmniTerm-1.2.3-source.tar.gz").exists())

    def test_missing_pinned_object_fails_without_replacing_existing_archive(self):
        self.drift(self.nested)
        del self.repositories[self.nested]["revisions"][self.nested_revision]
        archive = self.root / "dist/release/OmniTerm-1.2.3-source.tar.gz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"previous source archive")
        result = self.package()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(self.nested_revision, result.stderr)
        self.assertEqual(archive.read_bytes(), b"previous source archive")


if __name__ == "__main__":
    unittest.main()
