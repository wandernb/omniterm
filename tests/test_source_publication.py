import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/package-source.sh"


@unittest.skipUnless(shutil.which("zsh"), "source publication requires zsh")
class SourcePublicationTests(unittest.TestCase):
    def test_failed_rename_preserves_previous_archive_and_cleans_temporary_output(self):
        with tempfile.TemporaryDirectory(prefix="omniterm-source-publication-") as directory:
            root = Path(directory).resolve()
            scratch = root / "scratch"
            scratch.mkdir()
            environment = {name: value for name, value in os.environ.items()
                           if not name.startswith("GIT_")}
            environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                               TMPDIR=str(scratch))
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(SCRIPT, scripts / "package-source.sh")

            def git(repository, *arguments, input_text=""):
                return subprocess.run(
                    [shutil.which("git"), "-C", str(repository), *arguments],
                    env=environment, input=input_text, check=True, capture_output=True,
                    text=True, timeout=10,
                ).stdout.strip()

            def snapshot(repository, files, links=None):
                repository.mkdir(parents=True, exist_ok=True)
                git(repository, "init", "--quiet")
                entries = {}
                for name, content in files.items():
                    parent = entries
                    components = name.split("/")
                    for component in components[:-1]:
                        parent = parent.setdefault(component, {})
                    revision = git(repository, "hash-object", "-w", "--stdin",
                                   input_text=content)
                    parent[components[-1]] = ("100644", "blob", revision)
                for name, revision in (links or {}).items():
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

                identity = "Fixture <fixture@example.invalid> 1600000000 +0000"
                return git(repository, "hash-object", "-w", "-t", "commit", "--stdin",
                           input_text=f"tree {write_tree(entries)}\nauthor {identity}\n"
                                      f"committer {identity}\n\nPublication fixture\n")

            upstream_revision = snapshot(root / "Vendor/iTerm2",
                                         {"upstream.txt": "Pinned upstream\n"})
            patch = ("diff --git a/omni.txt b/omni.txt\n"
                     "new file mode 100644\n--- /dev/null\n+++ b/omni.txt\n"
                     "@@ -0,0 +1 @@\n+OmniTerm patch\n")
            release_revision = snapshot(root, {
                "README.md": "Tagged project\n",
                "LICENSE": "Fixture license\n",
                "patches/iterm2-omniterm.patch": patch,
            }, {"Vendor/iTerm2": upstream_revision})
            git(root, "update-ref", "refs/tags/v1.2.3", release_revision)

            def package():
                return subprocess.run(
                    ["zsh", str(scripts / "package-source.sh"), "1.2.3"],
                    env=environment, capture_output=True, text=True, timeout=20,
                )

            archive = root / "dist/release/OmniTerm-1.2.3-source.tar.gz"
            baseline = package()
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            self.assertEqual(baseline.stdout.strip(), str(archive))
            previous_archive = archive.read_bytes()
            scratch_baseline = set(scratch.iterdir())
            self.assertFalse(any(path.name.startswith("omniterm-source.")
                                 for path in scratch_baseline))

            boundary = root / "publication-bin"
            boundary.mkdir()
            rename = boundary / "mv"
            rename.write_text("#!/bin/sh\necho 'injected rename failure' >&2\nexit 24\n")
            rename.chmod(0o755)
            original_path = environment["PATH"]
            environment["PATH"] = str(boundary) + os.pathsep + original_path

            failed = package()
            self.assertEqual(failed.returncode, 24, failed.stderr)
            self.assertIn("injected rename failure", failed.stderr)
            self.assertEqual(failed.stdout, "")
            self.assertEqual(archive.read_bytes(), previous_archive)
            self.assertEqual(list(archive.parent.iterdir()), [archive])
            self.assertEqual(set(scratch.iterdir()), scratch_baseline)

            environment["PATH"] = original_path
            recovered = package()
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(recovered.stdout.strip(), str(archive))
            with tarfile.open(archive) as contents:
                self.assertEqual(contents.extractfile(
                    "OmniTerm-1.2.3-source/README.md").read(), b"Tagged project\n")
                self.assertEqual(contents.extractfile(
                    "OmniTerm-1.2.3-source/Vendor/iTerm2/upstream.txt").read(),
                    b"Pinned upstream\n")
            self.assertEqual(list(archive.parent.iterdir()), [archive])
            self.assertEqual(set(scratch.iterdir()), scratch_baseline)


if __name__ == "__main__":
    unittest.main()
