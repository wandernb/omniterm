"""Claude/Codex account router: routing by headroom, the account store, scheduler ops and shim install.

Every subprocess, HTTP call, Keychain access and exec is stubbed; HOME is a temp directory.
"""
import base64
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("omni_accounts_test", ROOT / "helpers/omni_accounts.py")
ACCOUNTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCOUNTS)


def jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def codex_auth(email, account_id, plan="pro"):
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}),
                       "access_token": "a", "refresh_token": "r", "account_id": account_id}}


def oauth(token, expires_in=3600):
    return {"claudeAiOauth": {"accessToken": token, "refreshToken": "r-" + token,
                              "expiresAt": int((time.time() + expires_in) * 1000)}}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Fakes:
    """Keychain, ps, provider logins and the Anthropic endpoints."""

    def __init__(self, home):
        self.home = home
        self.keychain = {}
        self.usage = {}  # access token -> usage payload
        self.emails = {}  # access token -> email
        self.ps = ""
        self.commands = []
        self.on_login = None

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "/usr/bin/security":
            if command[1] == "find-generic-password":
                service = command[command.index("-s") + 1]
                value = self.keychain.get(service)
                return subprocess.CompletedProcess(command, 0 if value else 44, value or "", "")
            if command[1] == "-i":
                text = kwargs["input"]
                service = re.search(r'-s "([^"]+)"', text).group(1)
                self.keychain[service] = bytes.fromhex(re.search(r"-X ([0-9a-f]+)", text).group(1)).decode()
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "delete-generic-password":
                self.keychain.pop(command[command.index("-s") + 1], None)
                return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "/bin/ps":
            return subprocess.CompletedProcess(command, 0, self.ps, "")
        if self.on_login:
            self.on_login(command, kwargs.get("env") or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    def urlopen(self, request, timeout=None):
        token = (request.get_header("Authorization") or "").replace("Bearer ", "")
        if request.full_url.endswith("/oauth/usage"):
            if token not in self.usage:
                raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, io.BytesIO(b""))
            return Response(json.dumps(self.usage[token]).encode())
        if request.full_url.endswith("/oauth/profile"):
            return Response(json.dumps({"account": {"email": self.emails.get(token), "uuid": "u-" + token},
                                        "organization": {"organization_type": "max",
                                                         "rate_limit_tier": "default_max_20x"}}).encode())
        raise AssertionError("unexpected URL " + request.full_url)


class AccountsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".local/bin").mkdir(parents=True)
        for name in ("claude", "codex"):
            binary = self.home / ".local/bin" / name
            binary.write_text("#!/bin/sh\necho real-" + name + " \"$@\"\n")
            binary.chmod(0o755)
        environment = {"HOME": str(self.home), "USER": "tester", "PATH": "/usr/bin:/bin"}
        self.env = mock.patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.fakes = Fakes(self.home)
        self.execs = []
        self.patches = [mock.patch.object(ACCOUNTS, "_run", self.fakes.run),
                        mock.patch.object(ACCOUNTS, "_urlopen", self.fakes.urlopen),
                        mock.patch.object(ACCOUNTS, "_execve", lambda *call: self.execs.append(call))]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def claude_profile(self, name, token, email, five_hour):
        profile = self.home / ".claude/accounts" / name
        ACCOUNTS.atomic_json(profile / ".credentials.json", oauth(token))
        ACCOUNTS.atomic_json(profile / "account.json", {"email": email, "fetched_at": time.time(),
                                                         "rate_limit_tier": "default_max_20x"})
        reset = "2099-01-01T00:00:00+00:00"
        self.fakes.usage[token] = {"five_hour": {"utilization": five_hour, "resets_at": reset},
                                   "seven_day": {"utilization": 10, "resets_at": reset}}
        self.fakes.emails[token] = email
        return profile


class ClaudeTest(AccountsTestCase):
    def test_existing_store_routes_off_an_exhausted_account_and_pins_the_session(self):
        self.claude_profile("main", "t-main", "a@example.com", 95)
        work = self.claude_profile("work", "t-work", "b@example.com", 20)
        ACCOUNTS.atomic_json(self.home / ".claude/accounts/state.json", {"active": "main"})

        listed = ACCOUNTS.refresh_accounts({"provider": "claude"})["providers"]["claude"]
        self.assertEqual([a["name"] for a in listed["accounts"]], ["main", "work"])
        main = listed["accounts"][0]
        self.assertEqual(main["email"], "a@example.com")
        self.assertEqual(main["plan"], "Max 20x")
        self.assertEqual(main["usage"]["five_hour"]["percent"], 95.0)
        self.assertEqual(main["usage"]["five_hour"]["resets_at"], 4070908800.0)
        self.assertTrue(listed["installed"])

        self.assertEqual(ACCOUNTS.main(["claude", "-p", "hi"]), 0)
        binary, argv, environment = self.execs[-1]
        self.assertEqual(argv[1:], ["-p", "hi"])
        self.assertEqual(Path(binary), self.home / ".local/bin/claude")
        self.assertEqual(environment["CLAUDE_SECURESTORAGE_CONFIG_DIR"], str(work))
        self.assertEqual(environment["CLAUDE_ACCOUNT_ACTIVE"], "work")
        service = ACCOUNTS.keychain_service(str(work))
        self.assertEqual(json.loads(self.fakes.keychain[service])["claudeAiOauth"]["accessToken"], "t-work")

        # The cached (offline) listing now marks work active and keeps the readings.
        cached = ACCOUNTS.list_accounts({})["providers"]["claude"]["accounts"]
        self.assertEqual([a["active"] for a in cached], [False, True])
        self.assertEqual(cached[1]["usage"]["five_hour"]["percent"], 20.0)

    def test_forced_account_skips_routing_and_unknown_account_fails(self):
        main = self.claude_profile("main", "t-main", "a@example.com", 95)
        self.claude_profile("work", "t-work", "b@example.com", 5)
        ACCOUNTS.main(["claude", "--account", "main", "--resume"])
        _, argv, environment = self.execs[-1]
        self.assertEqual(argv[1:], ["--resume"])
        self.assertEqual(environment["CLAUDE_SECURESTORAGE_CONFIG_DIR"], str(main))
        with self.assertRaises(SystemExit) as raised:
            ACCOUNTS.main(["claude", "--account", "nobody"])
        self.assertEqual(raised.exception.code, 2)

    def test_rename_moves_the_login_to_the_new_keychain_item_and_refuses_busy_accounts(self):
        old = self.claude_profile("main", "t-main", "a@example.com", 5)
        self.fakes.keychain[ACCOUNTS.keychain_service(str(old))] = json.dumps(oauth("t-main"))
        ACCOUNTS.atomic_json(self.home / ".claude/accounts/state.json", {"active": "main"})
        self.fakes.ps = "1 /x/claude --resume CLAUDE_ACCOUNT_ACTIVE=main\n"
        with self.assertRaises(ACCOUNTS.AccountError) as raised:
            ACCOUNTS.rename_account({"provider": "claude", "name": "main", "new_name": "home"})
        self.assertEqual(raised.exception.code, "account_busy")

        self.fakes.ps = ""
        ACCOUNTS.rename_account({"provider": "claude", "name": "main", "new_name": "Home"})
        new = self.home / ".claude/accounts/home"
        self.assertTrue((new / ".credentials.json").is_file())
        self.assertIn(ACCOUNTS.keychain_service(str(new)), self.fakes.keychain)
        self.assertNotIn(ACCOUNTS.keychain_service(str(old)), self.fakes.keychain)
        self.assertEqual(json.loads((self.home / ".claude/accounts/state.json").read_text())["active"], "home")

        ACCOUNTS.set_account_email({"provider": "claude", "name": "home", "email": "New@Example.com"})
        self.assertEqual(json.loads((new / "account.json").read_text())["expected_email"], "new@example.com")
        ACCOUNTS.remove_account({"provider": "claude", "name": "home"})
        self.assertFalse(new.exists())
        self.assertNotIn(ACCOUNTS.keychain_service(str(new)), self.fakes.keychain)


class CodexTest(AccountsTestCase):
    def rollout(self, account_id, percent, minutes, age):
        folder = self.home / ".codex/sessions/2099/01/01"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-{account_id}-{percent}.jsonl"
        events = [{"type": "session_meta", "payload": {"creator_account_id": account_id}},
                  {"timestamp": "2099-01-01T00:00:00Z", "type": "event_msg",
                   "payload": {"type": "token_count", "rate_limits": {
                       "limit_id": "codex", "primary": {"used_percent": percent, "window_minutes": minutes,
                                                        "resets_at": 4102444800}, "secondary": None}}},
                  {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                      "limit_id": "premium", "primary": None, "secondary": None}}}]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        os.utime(path, (time.time() - age, time.time() - age))

    def test_existing_login_is_adopted_in_place_and_routing_uses_its_own_session_usage(self):
        base = self.home / ".codex"
        ACCOUNTS.atomic_json(base / "auth.json", codex_auth("a@example.com", "acct-a"))
        (base / "config.toml").write_text("model = 'x'\n")
        work = self.home / ".codex/accounts/work"
        ACCOUNTS.atomic_json(work / "account.json", {})
        ACCOUNTS.atomic_json(work / "home/auth.json", codex_auth("b@example.com", "acct-b", "plus"))
        self.rollout("acct-a", 92.0, 10080, age=10)
        self.rollout("acct-b", 15.0, 300, age=20)

        listed = ACCOUNTS.list_accounts({"provider": "codex"})["providers"]["codex"]["accounts"]
        by_name = {account["name"]: account for account in listed}
        self.assertEqual(set(by_name), {"main", "work"})
        self.assertEqual(by_name["main"]["email"], "a@example.com")
        self.assertEqual(by_name["main"]["usage"], {"seven_day": {"percent": 92.0, "resets_at": 4102444800.0}})
        self.assertEqual(by_name["work"]["usage"]["five_hour"]["percent"], 15.0)
        self.assertEqual(by_name["work"]["plan"], "Plus")

        ACCOUNTS.atomic_json(base / "accounts/state.json", {"active": "main"})
        ACCOUNTS.main(["codex", "exec", "hi"])
        binary, argv, environment = self.execs[-1]
        self.assertEqual(Path(binary), self.home / ".local/bin/codex")
        self.assertEqual(argv[1:], ["exec", "hi"])
        self.assertEqual(environment["CODEX_ACCOUNT_ACTIVE"], "work")
        codex_home = Path(environment["CODEX_HOME"])
        self.assertEqual(codex_home, work / "home")
        self.assertTrue((codex_home / "config.toml").is_symlink())
        self.assertFalse((codex_home / "auth.json").is_symlink())
        self.assertFalse((codex_home / "accounts").exists())

        ACCOUNTS.main(["codex", "--account", "main"])
        self.assertEqual(self.execs[-1][2]["CODEX_HOME"], str(base))

        # Removing the adopted account forgets it but never touches ~/.codex.
        ACCOUNTS.remove_account({"provider": "codex", "name": "main"})
        self.assertTrue((base / "auth.json").is_file())
        remaining = ACCOUNTS.list_accounts({"provider": "codex"})["providers"]["codex"]["accounts"]
        self.assertEqual([a["name"] for a in remaining], ["work"])  # not re-adopted
        ACCOUNTS.remove_account({"provider": "codex", "name": "work"})
        self.assertTrue((base / "config.toml").is_file())

    def test_a_new_account_without_sessions_takes_over_from_an_exhausted_one(self):
        ACCOUNTS.atomic_json(self.home / ".codex/auth.json", codex_auth("a@example.com", "acct-a"))
        fresh = self.home / ".codex/accounts/fresh"
        ACCOUNTS.atomic_json(fresh / "account.json", {})
        ACCOUNTS.atomic_json(fresh / "home/auth.json", codex_auth("f@example.com", "acct-f"))
        self.rollout("acct-a", 95.0, 10080, age=5)
        listed = ACCOUNTS.list_accounts({"provider": "codex"})["providers"]["codex"]["accounts"]
        self.assertEqual({a["name"]: (a["usage"], a["error"]) for a in listed}["fresh"], ({}, None))
        ACCOUNTS.atomic_json(self.home / ".codex/accounts/state.json", {"active": "main"})
        with mock.patch("sys.stderr", io.StringIO()):
            ACCOUNTS.main(["codex"])
        self.assertEqual(self.execs[-1][2]["CODEX_ACCOUNT_ACTIVE"], "fresh")

    def test_add_account_logs_in_in_the_background_with_its_own_codex_home(self):
        started, killed = [], []
        popen = lambda command, **kw: started.append(command) or mock.Mock(poll=lambda: None, pid=4242)  # noqa: E731
        with mock.patch.object(ACCOUNTS, "_popen", popen), \
                mock.patch.object(ACCOUNTS.os, "killpg", lambda pid, signal: killed.append(pid)):
            result = ACCOUNTS.add_account({"provider": "codex", "name": "Side Project", "email": "c@example.com"})
            self.assertEqual(result, {"started": True})
            self.assertEqual(started[0][-4:], ["codex", "side-project", "add", "c@example.com"])
            pending = ACCOUNTS.list_accounts({"provider": "codex"})["providers"]["codex"]["accounts"]
            self.assertEqual([(a["name"], a["error"]) for a in pending], [("side-project", "login in progress")])
            # Retrying replaces the abandoned login rather than being refused.
            ACCOUNTS.add_account({"provider": "codex", "name": "side-project", "email": "c@example.com"})
            self.assertEqual(killed, [4242])

        def login(command, environment):
            self.assertEqual(command[1:], ["login"])
            ACCOUNTS.atomic_json(Path(environment["CODEX_HOME"]) / "auth.json",
                                 codex_auth("c@example.com", "acct-c"))
        self.fakes.on_login = login
        self.assertEqual(ACCOUNTS.main(["--login-job", "codex", "side-project", "add", "c@example.com"]), 0)
        done = ACCOUNTS.list_accounts({"provider": "codex"})["providers"]["codex"]["accounts"]
        self.assertEqual([(a["name"], a["email"], a["error"]) for a in done], [("side-project", "c@example.com", None)])

    def test_login_for_the_wrong_email_is_rolled_back_and_reported(self):
        def login(command, environment):
            ACCOUNTS.atomic_json(Path(environment["CODEX_HOME"]) / "auth.json", codex_auth("x@example.com", "x"))
        self.fakes.on_login = login
        log = self.home / ".codex/accounts/.logins"
        log.mkdir(parents=True)
        (log / "pro.log").write_text("codex: browser authenticated x@example.com, expected p@example.com\n")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(ACCOUNTS.main(["--login-job", "codex", "pro", "add", "p@example.com"]), 1)
        self.assertFalse((self.home / ".codex/accounts/pro/home/auth.json").exists())
        listed = ACCOUNTS.list_accounts({"provider": "codex"})["providers"]["codex"]["accounts"]
        self.assertIn("expected p@example.com", listed[0]["error"])


class InstallTest(AccountsTestCase):
    def real_run(self):
        return mock.patch.object(ACCOUNTS, "_run", subprocess.run)

    def test_install_replaces_the_old_router_after_the_new_one_checks_out(self):
        self.claude_profile("main", "t-main", "a@example.com", 5)
        legacy = self.home / ".claude/bin/claude"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("#!/bin/zsh\nexec /usr/bin/python3 \"$HOME/.claude/bin/claude-account-router.py\" \"$@\"\n")
        legacy.chmod(0o755)
        os.environ["PATH"] = f"{legacy.parent}:/usr/bin:/bin"

        with self.real_run():
            result = ACCOUNTS.install_routers({})
            self.assertEqual(result["claude"], str(legacy))
            self.assertEqual(result["codex"], str(self.home / ".claude/bin/codex"))
            backups = list(legacy.parent.glob("claude.pre-omni-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("claude-account-router", backups[0].read_text())
            self.assertTrue(ACCOUNTS.router_installed("claude") and ACCOUNTS.router_installed("codex"))
            installed = self.home / ".omni/bin/claude"
            self.assertFalse(installed.is_symlink())  # survives an app rebuild
            self.assertIn(str(ROOT / "helpers/omni_accounts.py"), installed.read_text())
            # The installed codex shim finds the real binary, not itself (no accounts: plain pass-through).
            ran = subprocess.run([str(self.home / ".claude/bin/codex"), "--version"], capture_output=True,
                                 text=True, timeout=30, env=dict(os.environ))
            self.assertEqual(ran.stdout.strip(), "real-codex --version")
            # Idempotent: a second install keeps the links and makes no new backup.
            ACCOUNTS.install_routers({})
            self.assertEqual(len(list(legacy.parent.glob("claude.pre-omni-*"))), 1)
        self.assertFalse((self.home / ".zshrc").exists())

    def test_without_an_old_router_the_path_goes_into_zshrc_once(self):
        with self.real_run():
            ACCOUNTS.install_routers({})
            ACCOUNTS.install_routers({})
        text = (self.home / ".zshrc").read_text()
        self.assertEqual(text.count(ACCOUNTS.ZSHRC_BEGIN), 1)
        self.assertIn('$HOME/.omni/bin:$PATH', text)

    def test_a_failing_self_check_leaves_the_old_router_in_place(self):
        legacy = self.home / ".claude/bin/claude"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("old router claude-account-router\n")
        failing = lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "boom")  # noqa: E731
        with mock.patch.object(ACCOUNTS, "_run", failing):
            with self.assertRaises(ACCOUNTS.AccountError) as raised:
                ACCOUNTS.install_routers({})
        self.assertEqual(raised.exception.code, "router_check_failed")
        self.assertEqual(legacy.read_text(), "old router claude-account-router\n")
        self.assertFalse(list(legacy.parent.glob("claude.pre-omni-*")))

    def test_shim_fallback_skips_router_shims_on_path(self):
        # A copy of the shim with no router next to it must reach the real binary, not loop.
        lonely = self.home / "lonely/shims"
        lonely.mkdir(parents=True)
        shim = lonely / "claude"
        shim.write_text((ROOT / "helpers/shims/claude").read_text())
        shim.chmod(0o755)
        environment = dict(os.environ, PATH=f"{lonely}:/usr/bin:/bin")
        ran = subprocess.run([str(shim), "-v"], capture_output=True, text=True, timeout=30, env=environment)
        self.assertEqual(ran.stdout.strip(), "real-claude -v")


def native_scheduler_source():
    """OmniScheduler.m from the fork checkout, or from the tracked patch (CI has no
    patched checkout; the file is new in the fork, so the patch holds all of it)."""
    checkout = ROOT / "Vendor/iTerm2/sources/OmniTerm/OmniScheduler.m"
    if checkout.exists():
        return checkout.read_text()
    lines, inside = [], False
    for line in (ROOT / "patches/iterm2-omniterm.patch").read_text().splitlines():
        if line.startswith("diff --git "):
            inside = line.endswith(" b/sources/OmniTerm/OmniScheduler.m")
        elif inside and line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return "\n".join(lines)


class WiringTest(unittest.TestCase):
    def test_every_account_op_is_public_in_the_worker_and_allowed_natively(self):
        source = (ROOT / "helpers/omni_knowledge.py").read_text()
        native = native_scheduler_source()
        for op in ACCOUNTS.OPS:
            self.assertIn(f'"{op}"', source)
            self.assertEqual(native.count(f'@"{op}"'), 2, op)  # allowlist and no-drain read set

    def test_errors_carry_codes_for_the_worker(self):
        with self.assertRaises(ACCOUNTS.AccountError) as raised:
            ACCOUNTS.list_accounts({"provider": "gemini"})
        self.assertEqual(raised.exception.code, "invalid_provider")
        self.assertIsInstance(raised.exception, ValueError)


if __name__ == "__main__":
    unittest.main()
