import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "omni_redact_tests", Path(__file__).parents[1] / "helpers/omni_redact.py")
REDACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REDACT)

# Built from parts so the fixtures never look like live credentials to a scanner.
BODY = "Ab3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dF3hJ5"
JWT = ("eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." +
       "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
PEM = ("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + BODY + "\n" + BODY +
       "\n-----END " + "RSA PRIVATE KEY-----")


class RedactionTests(unittest.TestCase):
    def test_credential_shapes_are_masked_and_their_context_kept(self):
        cases = (("key {} works", "sk-" + BODY),
                 ("use {}", "sk-ant-api03-" + BODY),
                 ("token {} for CI", "ghp_" + BODY),
                 ("pat {}", "github_pat_" + BODY + "_" + BODY),
                 ("slack {}", "xoxb-1234567890-" + BODY),
                 ("aws {} key", "AKIA" + "IOSFODNN7EXAMPLE"),
                 ("google {} key", "AIza" + "SyA" + BODY[:32]),
                 ("session {} expired", JWT),
                 ("key:\n{}\ndone", PEM),
                 ("DB_PASSWORD={}", "hunter2hunter"),
                 ('config {{"api_key": "{}"}}', "abc123def456"),
                 ("client_secret: {}", "s3cr3tvalue"),
                 ("AWS_SECRET_ACCESS_KEY={}", "wJalrXUtnFEMI"),
                 ("curl -H 'Authorization: Bearer {}'", "abc.def-ghi_jkl"),
                 ("Authorization: Basic {}", "dXNlcjpwYXNz"),
                 ("clone https://deploy:{}@git.example.com/repo.git", "Pa55word"))
        for template, secret in cases:
            with self.subTest(template=template):
                redacted = REDACT.redact(template.format(secret))
                self.assertNotIn(secret, redacted)
                self.assertIn(REDACT.MASK, redacted)
        self.assertEqual(REDACT.redact("export GITHUB_TOKEN=ghp_" + BODY), "export GITHUB_TOKEN=" + REDACT.MASK)
        self.assertEqual(REDACT.redact("clone https://deploy:Pa55word@git.example.com/repo.git"),
                         "clone https://" + REDACT.MASK + "@git.example.com/repo.git")
        self.assertEqual(REDACT.redact("key:\n" + PEM + "\ndone"), "key:\n" + REDACT.MASK + "\ndone")

    def test_look_alikes_are_left_alone(self):
        for text in ("Commit 3f9c2a1b7e4d5c6b8a9f0e1d2c3b4a5f6e7d8c9b fixed the token refresh.",
                     "Session 123e4567-e89b-12d3-a456-426614174000 ended.",
                     "The password reset flow and the secret santa page both need tests.",
                     "Use the tokenizer; skeleton tasks ask-for review; the task-runner is risky.",
                     "Bearer of bad news: max_tokens: 4000 and token_count = 12.",
                     "Compare password == expected before saving.",
                     "See https://example.com/docs/user:guide for details.",
                     "AKIA is a prefix; sk-short is not a key."):
            with self.subTest(text=text[:40]):
                self.assertEqual(REDACT.redact(text), text)

    def test_long_text_keeps_head_and_tail_within_the_cap(self):
        text = "HEAD " + "x" * 10000 + " TAIL"
        capped = REDACT.cap(text)
        self.assertLessEqual(len(capped), REDACT.MAX_CHARACTERS)
        self.assertTrue(capped.startswith("HEAD "))
        self.assertTrue(capped.endswith(" TAIL"))
        self.assertIn("characters omitted", capped)
        self.assertEqual(REDACT.cap("short"), "short")

    def test_clean_masks_before_capping_so_no_half_secret_survives(self):
        text = "a" * 2990 + " sk-" + BODY * 3 + " " + "b" * 2000
        cleaned = REDACT.clean(text, 3020)
        self.assertNotIn(BODY[:10], cleaned)
        self.assertEqual(REDACT.clean(None), "")


class UserCandidateTests(unittest.TestCase):
    def test_typed_corrections_and_preferences_qualify(self):
        for text in ("No, don't use tabs here; always indent with spaces.",
                     "From now on run the native gate before every release.",
                     "I prefer the dashboard sorted by weight.",
                     "<system-reminder>Context follows.</system-reminder>\nYou should never push to main directly.",
                     "Don't call it like this:\n```\nload(path, cache=True)\n```",
                     "Release rules:\n- always tag first,\n  - then push (main only),\n- never skip the gate,\n"
                     "- prefer the dist build (see RELEASING.md)"):
            with self.subTest(text=text[:40]):
                self.assertTrue(REDACT.is_user_candidate(text))

    def test_markup_short_cueless_and_pasted_turns_do_not_qualify(self):
        log = "\n".join("2026-11-01T00:00:%02dZ ERROR worker[%d]: no route {path=/a/b}" % (i, i) for i in range(50))
        code = ("def load(path):\n    # never cache this\n    with open(path) as handle:\n"
                "        return json.load(handle)\n")
        fenced = "```\n# always retry\nretry(3)\n```\nok"
        for text in ("<command-name>/clear</command-name>",
                     "No thanks",
                     "Persisted user turn without any cue at all",
                     "<local-command-stdout>never mind always</local-command-stdout>",
                     log, code, fenced, "", None):
            with self.subTest(text=(text or "")[:40]):
                self.assertFalse(REDACT.is_user_candidate(text))

    def test_harness_notices_and_pasted_documents_are_not_the_persons_words(self):
        for notice in ("Another Claude session sent a message:\n  This came from another session; never retry.",
                       "A session-scoped Stop hook is now active with condition: \"always finish\".",
                       "This session is being continued from a previous conversation. Never stop.",
                       "[Request interrupted by user] don't do that",
                       "<system-reminder>x</system-reminder>\nAnother Claude session sent a message while you were "
                       "working:\n never retry",
                       "Goal check-in: «always finish» is still active",
                       "[OmniTerm wrap-up 1:2] This occurrence should wrap up now."):
            self.assertFalse(REDACT.is_user_candidate(notice), notice)
        summary = "We should keep going. " + "Context about the earlier work. " * 80
        self.assertFalse(REDACT.is_user_candidate(summary), "a long pasted summary is not a typed request")
        self.assertTrue(REDACT.is_user_candidate("Well ok but Omni memory should not appear in the main run list"))
        relayed = "The user sent a new message while you were working: Always deploy from develop, never main."
        self.assertTrue(REDACT.is_user_candidate(relayed))
        self.assertEqual(REDACT.user_text(relayed), "Always deploy from develop, never main.")

    def test_slash_command_keeps_its_arguments_as_the_persons_words(self):
        text = "<command-message>plan</command-message><command-name>/plan</command-name>" \
               "<command-args>always keep the plan short</command-args>"
        self.assertEqual(REDACT.user_text(text), "/plan always keep the plan short")
        self.assertTrue(REDACT.is_user_candidate(text))


if __name__ == "__main__":
    unittest.main()
