# Contributing

Read the vendored iTerm2 contribution guides before editing the native fork.
Build only with `./scripts/build-app.sh`; preserve unrelated changes in the
intentionally patched fork checkout.

Run the fast helper and script checks from the project root:

```sh
python3 -m unittest discover -s tests -v
python3 tests/coverage_report.py --fail-under 65
/bin/zsh -n scripts/*.sh
```

Run focused native tests from `Vendor/iTerm2` with the upstream harness, for
example:

```sh
tools/run_tests.expect ModernTests/OmniSchedulerTests
```

Contributions must be compatible with GPL-3.0-or-later and retain upstream
copyright and license notices. Document user-visible changes in
`Vendor/iTerm2/docs/notes-3.7.txt`.
