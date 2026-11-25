#!/usr/bin/env python3
"""Account router for Claude Code and Codex.

`omni_accounts.py PROVIDER [args...]` picks the configured account with the most
quota headroom and execs the real provider binary pinned to it. Management flags
(--usage, --accounts, --add-account, --remove-account, --rename-account,
--account, --account-help) are handled here; everything else passes through.

Claude accounts live in ~/.claude/accounts/NAME (the store of the earlier
standalone router, kept as is: each account's Keychain item name is derived from
its directory path). Codex accounts live in ~/.codex/accounts/NAME; each has its
own CODEX_HOME holding only its auth.json, every other entry links back to
~/.codex so config, hooks and sessions stay shared. The existing ~/.codex login
is registered in place as account "main".

The knowledge worker imports this module for the scheduler's account ops
(list_accounts, add_account, ...); those never block on a login and only
`refresh_accounts` touches the network.
"""

from __future__ import annotations

import base64
import concurrent.futures
import fcntl
import getpass
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROVIDERS = ("claude", "codex")
SWITCH_THRESHOLD = 80.0
PROFILE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHIM_MARKERS = (b"omni-accounts-shim", b"claude-account-router")
DEPTH_VARIABLE = "OMNI_ACCOUNTS_DEPTH"
HTTP_TIMEOUT = 8
USAGE_DEADLINE = 20
LOGIN_TIMEOUT = 900
FAILED_LOGIN_SHOWN = 3600
USER_AGENT = "omni-accounts/1"

# Seams for tests: every subprocess, HTTP request and exec goes through these.
_run = subprocess.run
_popen = subprocess.Popen
_urlopen = urllib.request.urlopen
_execve = os.execve
_JOBS: list = []


class AccountError(ValueError):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HOME") or os.path.expanduser("~"))


def user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def read_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def atomic_json(path: pathlib.Path, value, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def valid_name(value) -> str:
    name = normalize_name(value or "")
    if not name or not PROFILE_NAME.fullmatch(name):
        raise AccountError("account names may contain letters, numbers, '.', '_' and '-' only", "invalid_name")
    return name


def epoch(value) -> float | None:
    """A reset time as epoch seconds, from epoch numbers or ISO strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def score(usage: dict | None) -> float | None:
    if not usage:
        return None
    values = [window["utilization"] for window in usage.values()
              if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))]
    return max(values) if values else None


def expire_windows(usage: dict | None, now: float) -> dict | None:
    """Zero any window whose reset time has already passed."""
    if not isinstance(usage, dict):
        return None
    current = {}
    for key, window in usage.items():
        if not isinstance(window, dict):
            continue
        window = dict(window)
        reset = epoch(window.get("resets_at"))
        if reset is not None and reset <= now:
            window = {"utilization": 0.0, "resets_at": None}
        current[key] = window
    return current


def select_account(current: str, usage_by_name: dict, preferred=frozenset()) -> str:
    """Stay on current below the threshold, else move to the least-used account.

    Preferred accounts win while under the threshold; unknown usage counts as usable for them.
    """
    usable_preferred = [name for name in sorted(preferred) if name in usage_by_name and
                        (score(usage_by_name[name]) is None or score(usage_by_name[name]) < SWITCH_THRESHOLD)]
    if usable_preferred:
        if current in usable_preferred:
            return current
        return min(usable_preferred,
                   key=lambda name: (score(usage_by_name[name]) is None, score(usage_by_name[name]) or 0))
    current_score = score(usage_by_name.get(current))
    if current_score is not None and current_score >= SWITCH_THRESHOLD:
        candidates = [(score(usage), name) for name, usage in usage_by_name.items()
                      if score(usage) is not None and score(usage) < SWITCH_THRESHOLD]
        if candidates:
            return min(candidates)[1]
    return current


def format_percent(window) -> str:
    if not isinstance(window, dict) or not isinstance(window.get("utilization"), (int, float)):
        return "unknown"
    return f'{window["utilization"]:g}%'


def format_reset(window) -> str:
    reset = epoch(window.get("resets_at")) if isinstance(window, dict) else None
    if reset is None:
        return "-"
    seconds = max(0, int(reset - time.time()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {remainder // 60}m"
    return f"{remainder // 60}m"


def format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours < 24 else f"{hours // 24}d {hours % 24}h"


def is_shim(path: pathlib.Path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
    except OSError:
        return False
    return any(marker in head for marker in SHIM_MARKERS)


def resolve_real_binary(name: str) -> pathlib.Path | None:
    """The provider's real binary: an override, then known install places, then PATH; never a shim."""
    override = os.environ.get(f"{name.upper()}_REAL_BIN")
    if override:
        return pathlib.Path(override).expanduser()
    directories = [str(home() / ".local/bin")]
    directories += os.environ.get("PATH", "").split(":")
    directories += ["/opt/homebrew/bin", "/usr/local/bin", str(home() / ".npm-global/bin")]
    seen = set()
    for directory in directories:
        if not directory or directory in seen:
            continue
        seen.add(directory)
        candidate = pathlib.Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK) and not is_shim(candidate):
            return candidate
    return None


def ps_table() -> str:
    try:
        result = _run(["/bin/ps", "-Eww", "-o", "pid=,command=", "-U", user()],
                      capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def http_json(request: urllib.request.Request, timeout: float = HTTP_TIMEOUT):
    with _urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class Provider:
    name = ""
    active_variable = ""

    def __init__(self):
        self.real_binary = resolve_real_binary(self.name)

    # --- store -----------------------------------------------------------
    @property
    def root(self) -> pathlib.Path:
        raise NotImplementedError

    @property
    def state_file(self) -> pathlib.Path:
        return self.root / "state.json"

    @property
    def cache_file(self) -> pathlib.Path:
        return self.root / "usage-cache.json"

    @property
    def jobs_dir(self) -> pathlib.Path:
        return self.root / ".logins"

    def profiles(self) -> dict:
        raise NotImplementedError

    def active(self, profiles: dict) -> str | None:
        active = read_json(self.state_file, {}).get("active")
        if active in profiles:
            return active
        return next(iter(profiles), None)

    def set_active(self, name: str) -> None:
        state = read_json(self.state_file, {})
        state = state if isinstance(state, dict) else {}
        state["active"] = name
        atomic_json(self.state_file, state)

    def load_cache(self) -> dict:
        cache = read_json(self.cache_file, {})
        return cache if isinstance(cache, dict) else {}

    def metadata(self, profile: pathlib.Path) -> dict:
        data = read_json(profile / "account.json", {})
        return data if isinstance(data, dict) else {}

    def update_metadata(self, profile: pathlib.Path, **values) -> None:
        data = self.metadata(profile)
        data.update(values)
        atomic_json(profile / "account.json", data)

    def email(self, profile: pathlib.Path) -> str | None:
        data = self.metadata(profile)
        email = data.get("email") or data.get("expected_email")
        return email if isinstance(email, str) and email else None

    def plan(self, profile: pathlib.Path) -> str | None:
        return None

    # --- usage -----------------------------------------------------------
    def query(self, item) -> tuple:
        """(name, usage or None, error or None) for one account."""
        raise NotImplementedError

    def usage(self, profiles: dict, live: bool) -> tuple:
        """Usage by account, errors, and when each reading was taken."""
        cache = self.load_cache()
        now = time.time()
        usage_by_name, errors, checked = {}, {}, {}
        if live and profiles:
            # One overall deadline: the scheduler gives a request 30s, and each account's check
            # may chain a Keychain read, a token refresh and two HTTP calls.
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(profiles)))
            futures = {pool.submit(self.query, item): item[0] for item in profiles.items()}
            done, _ = concurrent.futures.wait(futures, timeout=USAGE_DEADLINE)
            results = [future.result() if future in done else (name, None, "timed out")
                       for future, name in futures.items()]
            pool.shutdown(wait=False)
        else:
            results = [(name, None, None) for name in profiles]
        for name, usage, error in results:
            if usage is not None:
                usage_by_name[name] = usage
                cache[name] = {"fetched_at": now, "usage": usage}
                checked[name] = now
                continue
            cached = cache.get(name) if isinstance(cache.get(name), dict) else {}
            usage_by_name[name] = expire_windows(cached.get("usage"), now)
            if cached.get("fetched_at"):
                checked[name] = float(cached["fetched_at"])
            if error:
                errors[name] = error
        if live:
            atomic_json(self.cache_file, {name: entry for name, entry in cache.items() if name in profiles})
        return usage_by_name, errors, checked

    def choose(self, profiles: dict, preferred=frozenset()) -> tuple:
        current = self.active(profiles)
        usage_by_name, errors, checked = self.usage(profiles, live=True)
        selected = select_account(current, self.for_selection(usage_by_name), preferred)
        self.set_active(selected)
        return selected, usage_by_name, errors, checked

    def for_selection(self, usage_by_name: dict) -> dict:
        return usage_by_name

    def sessions(self) -> dict:
        """Running sessions per account, from `ps -E` (command followed by environment)."""
        pattern = re.compile(r"(?:^|\s)" + self.active_variable + r"=(\S+)")
        counts: dict = {}
        for line in ps_table().splitlines():
            words = line.split()
            if len(words) < 2 or pathlib.Path(words[1]).name != self.name:
                continue
            if self.skip_process(words):
                continue
            match = pattern.search(line)
            if match:
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        return counts

    def skip_process(self, words) -> bool:
        return False

    # --- management ------------------------------------------------------
    def require(self, name) -> tuple:
        name = valid_name(name)
        profiles = self.all_profiles()
        if name not in profiles:
            raise AccountError(f"unknown {self.name} account '{name}'", "unknown_account")
        return name, profiles[name]

    def all_profiles(self) -> dict:
        """Accounts including ones whose login has not completed."""
        return self.profiles()

    def busy(self, name: str) -> None:
        if self.sessions().get(name):
            raise AccountError(f"account '{name}' has running sessions; quit them first", "account_busy")

    def remove(self, name) -> None:
        name, profile = self.require(name)
        self.busy(name)
        self.forget_profile(name, profile)
        self.drop_state(name)

    def forget_profile(self, name: str, profile: pathlib.Path) -> None:
        shutil.rmtree(profile)

    def drop_state(self, name: str, new_name: str | None = None) -> None:
        state = read_json(self.state_file, {})
        if isinstance(state, dict) and state.get("active") == name:
            if new_name:
                state["active"] = new_name
            else:
                state.pop("active", None)
            atomic_json(self.state_file, state)
        cache = self.load_cache()
        if name in cache:
            entry = cache.pop(name)
            if new_name:
                cache[new_name] = entry
            atomic_json(self.cache_file, cache)

    def rename(self, name, new_name) -> None:
        name, profile = self.require(name)
        new_name = valid_name(new_name)
        if new_name == name:
            return
        if (self.root / new_name).exists():
            raise AccountError(f"account '{new_name}' already exists", "account_exists")
        self.busy(name)
        destination = self.root / new_name
        os.replace(profile, destination)
        try:
            self.after_rename(name, destination)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            os.replace(destination, profile)
            raise AccountError(f"could not move the login to '{new_name}': {error}", "keychain_failed")
        self.drop_state(name, new_name)

    def after_rename(self, old_name: str, profile: pathlib.Path) -> None:
        pass

    def set_email(self, name, email) -> None:
        name, profile = self.require(name)
        email = (email or "").strip().lower() or None
        if email is not None and "@" not in email:
            raise AccountError("email must be an address", "invalid_email")
        data = self.metadata(profile)
        if email:
            data["expected_email"] = email
        else:
            data.pop("expected_email", None)
        atomic_json(profile / "account.json", data)

    def login(self, name: str, email: str | None, adding: bool) -> int:
        raise NotImplementedError

    # --- background logins (scheduler ops) -------------------------------
    def start_login(self, name, email, adding: bool) -> None:
        name = valid_name(name)
        if adding and name in self.profiles():
            raise AccountError(f"account '{name}' already exists", "account_exists")
        if not adding:
            name, profile = self.require(name)
            email = email or self.metadata(profile).get("expected_email") or self.email(profile)
        if self.real_binary is None:
            raise AccountError(f"{self.name} is not installed", "not_installed")
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        status = self.jobs_dir / f"{name}.json"
        current = read_json(status, {})
        if current.get("state") == "running" and isinstance(current.get("pid"), int):
            # A retry replaces an abandoned login (e.g. a browser tab closed without finishing).
            try:
                os.killpg(current["pid"], 15)
            except OSError:
                pass
        log_path = self.jobs_dir / f"{name}.log"
        command = [sys.executable, str(pathlib.Path(__file__).resolve()), "--login-job", self.name, name,
                   "add" if adding else "login"] + ([email] if email else [])
        with open(log_path, "w") as log:
            job = _popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
        _JOBS.append(job)
        atomic_json(status, {"state": "running", "started": time.time(), "email": email,
                             "pid": job.pid if isinstance(getattr(job, "pid", None), int) else None})
        for job in list(_JOBS):
            if job.poll() is not None:
                _JOBS.remove(job)

    def login_jobs(self) -> dict:
        for job in list(_JOBS):
            if job.poll() is not None:
                _JOBS.remove(job)
        jobs = {}
        if self.jobs_dir.is_dir():
            for path in self.jobs_dir.glob("*.json"):
                status = read_json(path, {})
                if status.get("state") == "running" and time.time() - float(status.get("started", 0)) > LOGIN_TIMEOUT:
                    status = {"state": "failed", "error": "login timed out", "finished": time.time()}
                if status.get("state") == "failed" and \
                        time.time() - float(status.get("finished", 0)) > FAILED_LOGIN_SHOWN:
                    path.unlink(missing_ok=True)  # an old failure stops shadowing the account
                    continue
                jobs[path.stem] = status
        return jobs

    # --- launch ----------------------------------------------------------
    def launch(self, arguments: list) -> None:
        raise NotImplementedError

    def exec_real(self, arguments: list, environment: dict | None = None) -> None:
        if self.real_binary is None:
            print(f"{self.name}: the real {self.name} binary was not found", file=sys.stderr)
            raise SystemExit(127)
        if is_shim(self.real_binary):
            print(f"{self.name}: {self.real_binary} is a router shim, not the real binary; "
                  f"set {self.name.upper()}_REAL_BIN", file=sys.stderr)
            raise SystemExit(126)
        environment = dict(os.environ if environment is None else environment)
        environment.pop(DEPTH_VARIABLE, None)
        _execve(str(self.real_binary), [str(self.real_binary), *arguments], environment)

    def preferred(self, profiles: dict, arguments: list) -> set:
        return set()

    # --- CLI -------------------------------------------------------------
    def usage_overview(self) -> int:
        profiles = self.profiles()
        if not profiles:
            print("No accounts configured.")
            print(f"Add one with: {self.name} --add-account NAME [EMAIL]")
            return 1
        self.before_route(profiles)
        preferred = self.preferred(profiles, [])
        selected, usage_by_name, errors, checked = self.choose(profiles, preferred)
        sessions = self.sessions()
        header = (f'{"ACCOUNT":<12}   {"EMAIL":<28}   {"PLAN":<9}   {"SESSIONS":<8}   {"NEXT":<4}   '
                  f'{"5-HOUR":<15}   {"WEEKLY":<15}')
        print(header)
        print("─" * len(header))
        now = time.time()
        stale = {name: now - at for name, at in checked.items() if name in errors}
        for name, profile in profiles.items():
            usage = usage_by_name.get(name) or {}
            marker = "●" if name == selected else ""
            if name in preferred:
                marker = (marker + " ◆").strip()
            flag = "*" if name in stale else " "
            print(f"{name:<12}   {(self.email(profile) or 'unknown'):<28}   {(self.plan(profile) or 'unknown'):<9}   "
                  f"{str(sessions.get(name) or '–'):<8}   {marker:<4}   "
                  f"{format_percent(usage.get('five_hour')):>6} {format_reset(usage.get('five_hour')):>8}   "
                  f"{format_percent(usage.get('seven_day')):>6} {format_reset(usage.get('seven_day')):>8}{flag}")
        print(f"\nSESSIONS: running sessions on the account · NEXT ●: where a plain `{self.name}` goes")
        if preferred:
            print(f"◆ preferred account (Claude in Chrome signed in here) while under {SWITCH_THRESHOLD:g}%")
        if stale:
            print("\n* cached reading (" + ", ".join(f"{n} {format_age(a)} ago" for n, a in stale.items()) + ")")
        sys.stdout.flush()
        if errors:
            print("Unavailable checks: " + "; ".join(f"{n}: {e}" for n, e in errors.items()), file=sys.stderr)
        return 0

    def before_route(self, profiles: dict) -> None:
        pass

    def account_help(self) -> None:
        n = self.name
        print(f"{n.title()} account router options (OmniTerm):")
        print(f"  {n} --usage                       Show usage for every account")
        print(f"  {n} --accounts                    List accounts with current usage")
        print(f"  {n} --add-account NAME [EMAIL]    Authenticate another account")
        print(f"  {n} --remove-account NAME         Forget an account")
        print(f"  {n} --rename-account OLD NEW      Rename an account")
        print(f"  {n} --account NAME [args...]      Start on NAME, skipping quota routing")
        print(f"  {' ' * len(n)}                             (or set {n.upper()}_ACCOUNT=NAME)")
        print(f"  {n} --account-help                Show this help")
        print(f"\nAll other arguments are forwarded unchanged to {n}.")

    def management(self, arguments: list) -> int | None:
        if not arguments:
            return None
        option, rest = arguments[0], arguments[1:]
        try:
            if option in ("--usage", "--accounts", "--list-accounts"):
                if rest:
                    print(f"{self.name}: {option} takes no arguments", file=sys.stderr)
                    return 2
                return self.usage_overview()
            if option == "--add-account":
                if not rest or len(rest) > 2:
                    print(f"usage: {self.name} --add-account NAME [EMAIL]", file=sys.stderr)
                    return 2
                return self.login(valid_name(rest[0]), rest[1] if len(rest) == 2 else None, adding=True)
            if option == "--remove-account":
                if len(rest) != 1:
                    print(f"usage: {self.name} --remove-account NAME", file=sys.stderr)
                    return 2
                self.remove(rest[0])
                print(f"Removed {self.name} account '{normalize_name(rest[0])}'.")
                return 0
            if option == "--rename-account":
                if len(rest) != 2:
                    print(f"usage: {self.name} --rename-account OLD NEW", file=sys.stderr)
                    return 2
                self.rename(rest[0], rest[1])
                print(f"Renamed {self.name} account '{normalize_name(rest[0])}' to '{normalize_name(rest[1])}'.")
                return 0
            if option == "--account-help":
                self.account_help()
                return 0
            if option == "--router-check":
                profiles = self.profiles()
                print(json.dumps({"ok": self.real_binary is not None, "provider": self.name,
                                  "real": str(self.real_binary) if self.real_binary else None,
                                  "accounts": sorted(profiles)}))
                return 0 if self.real_binary is not None else 1
            if arguments in (["--help"], ["-h"]):
                self.account_help()
                print()
                sys.stdout.flush()
                if self.real_binary is None:
                    return 127
                return _run([str(self.real_binary), option]).returncode
            extra = self.extra_management(option, rest)
            if extra is not None:
                return extra
        except AccountError as error:
            print(f"{self.name}: {error}", file=sys.stderr)
            return 2 if error.code in ("invalid_name", "invalid_email", "unknown_account") else 1
        return None

    def extra_management(self, option: str, rest: list) -> int | None:
        return None

    def extract_forced(self, arguments: list) -> tuple:
        forced = os.environ.get(f"{self.name.upper()}_ACCOUNT") or None
        remaining: list = []
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == "--":
                remaining.extend(arguments[index:])
                break
            if argument == "--account":
                if index + 1 >= len(arguments):
                    print(f"usage: {self.name} --account NAME [{self.name} args...]", file=sys.stderr)
                    raise SystemExit(2)
                forced = arguments[index + 1]
                index += 2
                continue
            if argument.startswith("--account="):
                forced = argument.split("=", 1)[1]
                index += 1
                continue
            remaining.append(argument)
            index += 1
        return (normalize_name(forced) if forced else None), remaining

    def route(self, arguments: list) -> tuple:
        """The account to launch on and the remaining arguments; exits on an unknown account."""
        forced, arguments = self.extract_forced(arguments)
        profiles = self.profiles()
        if forced and forced not in profiles:
            print(f"{self.name}: unknown account '{forced}' (configured: {', '.join(profiles) or 'none'})",
                  file=sys.stderr)
            raise SystemExit(2)
        if not profiles:
            return None, profiles, arguments, set()
        if not forced and len(profiles) == 1:
            selected = next(iter(profiles))  # nothing to choose: skip the usage checks
            self.set_active(selected)
            return selected, profiles, arguments, set()
        quiet = os.environ.get(f"{self.name.upper()}_ACCOUNT_QUIET") == "1" or \
            os.environ.get("CLAUDE_ACCOUNT_QUIET") == "1"
        previous = read_json(self.state_file, {}).get("active")
        self.before_route(profiles)
        preferred = self.preferred(profiles, arguments)
        if forced:
            selected = forced
            self.set_active(selected)
            if previous != selected and not quiet:
                print(f"{self.name}: using account {selected} (forced)", file=sys.stderr)
        else:
            selected, usage_by_name, errors, _ = self.choose(profiles, preferred)
            selected_score = score(usage_by_name.get(selected))
            text = "unknown" if selected_score is None else f"{selected_score:g}% binding usage"
            if previous and previous != selected and not quiet:
                print(f"{self.name}: switched {previous} -> {selected} ({text})", file=sys.stderr)
            if errors and os.environ.get("OMNI_ACCOUNTS_DEBUG") == "1":
                print(f"{self.name}: usage checks: " + "; ".join(f"{n}: {e}" for n, e in errors.items()),
                      file=sys.stderr)
        return selected, profiles, arguments, preferred

    # --- scheduler view --------------------------------------------------
    def describe(self, live: bool) -> dict:
        profiles = self.all_profiles()
        usable = self.profiles()
        if live:
            self.before_route(usable)
        usage_by_name, errors, checked = self.usage(usable, live=live)
        active = self.active(usable)
        sessions = self.sessions()
        jobs = self.login_jobs()
        accounts = []
        for name, profile in profiles.items():
            usage = usage_by_name.get(name) or {}
            error = errors.get(name)
            job = jobs.get(name) or {}
            if name not in usable:
                error = error or "login not completed"
            if job.get("state") == "running":
                error = "login in progress"
            elif job.get("state") == "failed":
                error = job.get("error") or "login failed"
            accounts.append({
                "name": name, "email": self.email(profile), "plan": self.plan(profile),
                "active": name == active,
                "usage": {key: {"percent": float(window["utilization"]), "resets_at": epoch(window.get("resets_at"))}
                          for key, window in usage.items()
                          if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))},
                "sessions": int(sessions.get(name, 0)), "error": error, "checked_at": checked.get(name),
            })
        for name, job in jobs.items():
            if name not in profiles and job.get("state") in ("running", "failed"):
                accounts.append({"name": name, "email": job.get("email"), "plan": None, "active": False,
                                 "usage": {}, "sessions": 0, "checked_at": None,
                                 "error": "login in progress" if job.get("state") == "running"
                                 else job.get("error") or "login failed"})
        return {"installed": self.real_binary is not None, "router": router_installed(self.name),
                "accounts": accounts}


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
EXPIRY_BUFFER_MS = 5 * 60 * 1000
PROFILE_REFRESH_SECONDS = 15 * 60
SESSION_HELPER_MODES = {"--claude-in-chrome-mcp", "--chrome-native-host", "--computer-use-mcp"}
CHROME_EXTENSION_IDS = ("fcoeoabgfenejglbffodgkkbkcdhcgfn", "dihbgbndebgnbjfmelmegjepbnkhlgni",
                        "dngcpimnedloihjnnfngkgjoidhnaolf")
ACCOUNT_UUID_ENTRY = re.compile(
    rb'accountUuid(?:.{0,24}?"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")?', re.S)


def keychain_service(storage_dir: str | None = None) -> str:
    """Keychain item Claude Code uses; CLAUDE_SECURESTORAGE_CONFIG_DIR gets its own item."""
    if not storage_dir:
        return KEYCHAIN_SERVICE
    digest = hashlib.sha256(unicodedata.normalize("NFC", storage_dir).encode()).hexdigest()
    return f"{KEYCHAIN_SERVICE}-{digest[:8]}"


def read_keychain_item(service: str) -> dict | None:
    try:
        result = _run(["/usr/bin/security", "find-generic-password", "-a", user(), "-s", service, "-w"],
                      capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        item = json.loads(result.stdout)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def read_keychain_credentials(service: str = KEYCHAIN_SERVICE) -> dict | None:
    credentials = read_keychain_item(service)
    return credentials if credentials and credentials.get("claudeAiOauth") else None


def activate_keychain(credentials: dict, service: str = KEYCHAIN_SERVICE) -> None:
    """Install a Claude OAuth login in a Keychain item, preserving unrelated OAuth state."""
    current = read_keychain_item(service)
    if current is None and service != KEYCHAIN_SERVICE:
        # A new account item starts from the shared one, so MCP OAuth logins carry over.
        current = read_keychain_item(KEYCHAIN_SERVICE)
    current = current or {}
    current["claudeAiOauth"] = credentials["claudeAiOauth"]
    hex_value = json.dumps(current, separators=(",", ":")).encode().hex()
    command = f'add-generic-password -U -a "{user()}" -s "{service}" -X {hex_value}\n'
    write = _run(["/usr/bin/security", "-i"], input=command, capture_output=True, text=True, timeout=10)
    if write.returncode != 0:
        raise RuntimeError("failed to store the account login in macOS Keychain")


def delete_keychain_item(service: str) -> None:
    try:
        _run(["/usr/bin/security", "delete-generic-password", "-a", user(), "-s", service],
             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def oauth_expiry(credentials: dict | None) -> float:
    expires_at = (credentials or {}).get("claudeAiOauth", {}).get("expiresAt")
    return expires_at if isinstance(expires_at, (int, float)) else 0


def claude_profile_api(token: str) -> dict | None:
    request = urllib.request.Request("https://api.anthropic.com/api/oauth/profile",
                                     headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
    try:
        payload = http_json(request)
    except Exception:  # noqa: BLE001 - offline or rejected: the caller keeps what it has
        return None
    return payload if isinstance(payload, dict) else None


def oauth_email(credentials: dict) -> str | None:
    token = credentials.get("claudeAiOauth", {}).get("accessToken")
    payload = claude_profile_api(token) if token else None
    account = payload.get("account") if payload else None
    email = account.get("email") if isinstance(account, dict) else None
    return email.lower() if isinstance(email, str) else None


def extension_account_uuid(store: pathlib.Path) -> str | None:
    """Latest accountUuid in a Chrome extension's LevelDB store; a trailing entry without one is a sign-out."""
    try:
        files = [path for path in store.iterdir() if path.suffix in (".ldb", ".log") and path.stem.isdigit()]
    except OSError:
        return None
    files.sort(key=lambda path: (path.suffix == ".log", int(path.stem)))
    latest = None
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for match in ACCOUNT_UUID_ENTRY.finditer(data):
            latest = match.group(1).decode() if match.group(1) else None
    return latest


class Claude(Provider):
    name = "claude"
    active_variable = "CLAUDE_ACCOUNT_ACTIVE"

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get("CLAUDE_ACCOUNT_ROOT") or home() / ".claude/accounts").expanduser()

    def migrate_profile_names(self) -> None:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            normalized = normalize_name(path.name)
            if not normalized or normalized == path.name:
                continue
            destination = self.root / normalized
            if destination.exists():
                try:
                    same = os.path.samefile(path, destination)
                except OSError:
                    same = False
                if not same:
                    continue
            temporary = self.root / f".rename-{os.getpid()}-{normalized}"
            os.replace(path, temporary)
            os.replace(temporary, destination)
        active = read_json(self.state_file, {}).get("active")
        if isinstance(active, str) and normalize_name(active) != active:
            self.set_active(normalize_name(active))

    def profiles(self) -> dict:
        self.migrate_profile_names()
        if not self.root.is_dir():
            return {}
        return {path.name: path for path in sorted(self.root.iterdir())
                if path.is_dir() and not path.name.startswith(".") and (path / ".credentials.json").is_file()}

    def plan(self, profile: pathlib.Path) -> str | None:
        metadata = self.metadata(profile)
        oauth = read_json(profile / ".credentials.json", {}).get("claudeAiOauth", {})
        tier = str(metadata.get("rate_limit_tier") or oauth.get("rateLimitTier") or "").lower()
        subscription = str(metadata.get("organization_type") or oauth.get("subscriptionType") or "").lower()
        match = re.search(r"max[_-](\d+)x", tier)
        if match:
            return f"Max {match.group(1)}x"
        if subscription == "max":
            return "Max"
        return subscription.title() if subscription else None

    def account_service(self, profile: pathlib.Path) -> str:
        return keychain_service(str(profile))

    # --- credentials -----------------------------------------------------
    def load_credentials(self, profile: pathlib.Path) -> dict:
        """The account's freshest login: its file, or its Keychain item if a session rotated it."""
        stored = read_json(profile / ".credentials.json", {})
        session = read_keychain_credentials(self.account_service(profile))
        if not session or oauth_expiry(session) <= oauth_expiry(stored):
            return stored
        # `/login` inside a session writes this item too; never adopt a different account's login.
        expected = self.metadata(profile).get("email")
        actual = oauth_email(session) if expected else None
        if actual and actual != expected.lower():
            print(f"claude: account '{profile.name}' Keychain login belongs to another email; ignored",
                  file=sys.stderr)
            return stored
        stored["claudeAiOauth"] = session["claudeAiOauth"]
        atomic_json(profile / ".credentials.json", stored)
        return stored

    def publish_credentials(self, profile: pathlib.Path, credentials: dict) -> None:
        activate_keychain(credentials, self.account_service(profile))

    def refresh_if_needed(self, profile: pathlib.Path, credentials: dict) -> dict:
        oauth = credentials.get("claudeAiOauth")
        if not isinstance(oauth, dict) or not isinstance(oauth.get("expiresAt"), (int, float)):
            return credentials
        if int(time.time() * 1000) + EXPIRY_BUFFER_MS < oauth["expiresAt"] or not oauth.get("refreshToken"):
            return credentials
        lock_path = profile / ".credentials.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            latest = self.load_credentials(profile) or credentials
            latest_oauth = latest.get("claudeAiOauth", {})
            if int(time.time() * 1000) + EXPIRY_BUFFER_MS < oauth_expiry(latest):
                return latest
            request = urllib.request.Request(
                os.environ.get("CLAUDE_ACCOUNT_TOKEN_URL", "https://platform.claude.com/v1/oauth/token"),
                data=json.dumps({"grant_type": "refresh_token", "refresh_token": latest_oauth.get("refreshToken"),
                                 "client_id": CLAUDE_CLIENT_ID}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
            try:
                payload = http_json(request)
            except urllib.error.HTTPError as error:
                body = error.read().decode(errors="replace")
                if error.code in (400, 401, 403) and ("invalid_grant" in body or "invalid_client" in body):
                    raise RuntimeError("reauthentication required") from error
                raise
            latest_oauth["accessToken"] = payload["access_token"]
            latest_oauth["expiresAt"] = int(time.time() * 1000) + int(payload["expires_in"]) * 1000
            if payload.get("refresh_token"):
                latest_oauth["refreshToken"] = payload["refresh_token"]
            if payload.get("scope"):
                latest_oauth["scopes"] = payload["scope"].split()
            latest["claudeAiOauth"] = latest_oauth
            atomic_json(profile / ".credentials.json", latest)
            # Refresh tokens rotate: running sessions must get the new one or they lose the login.
            try:
                self.publish_credentials(profile, latest)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                pass
            return latest

    def refresh_account_metadata(self, profile: pathlib.Path, token: str) -> dict:
        metadata = self.metadata(profile)
        if time.time() - float(metadata.get("fetched_at", 0)) < PROFILE_REFRESH_SECONDS:
            return metadata
        payload = claude_profile_api(token)
        if not payload:
            return metadata
        account = payload.get("account") or {}
        organization = payload.get("organization") or {}
        refreshed = {"email": account.get("email"), "account_id": account.get("uuid"),
                     "organization_id": organization.get("uuid"),
                     "organization_type": organization.get("organization_type"),
                     "rate_limit_tier": organization.get("rate_limit_tier"),
                     "subscription_status": organization.get("subscription_status"), "fetched_at": time.time()}
        if metadata.get("expected_email"):
            refreshed["expected_email"] = metadata["expected_email"]
        refreshed = {key: value for key, value in refreshed.items() if value is not None}
        atomic_json(profile / "account.json", refreshed)
        return refreshed

    def query(self, item) -> tuple:
        name, profile = item
        try:
            credentials = self.refresh_if_needed(profile, self.load_credentials(profile))
            token = credentials.get("claudeAiOauth", {}).get("accessToken")
            if not token:
                return name, None, "no OAuth access token"
            self.refresh_account_metadata(profile, token)
            request = urllib.request.Request(
                os.environ.get("CLAUDE_ACCOUNT_USAGE_URL", "https://api.anthropic.com/api/oauth/usage"),
                headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                         "User-Agent": USER_AGENT})
            raw = http_json(request)
            usage = {key: {"utilization": float(raw[key]["utilization"]), "resets_at": raw[key].get("resets_at")}
                     for key in ("five_hour", "seven_day")
                     if isinstance(raw.get(key), dict) and raw[key].get("utilization") is not None}
            return name, usage or None, None
        except urllib.error.HTTPError as error:
            return name, None, f"HTTP {error.code}"
        except RuntimeError as error:
            return name, None, str(error)
        except Exception as error:  # noqa: BLE001 - one account's failure never stops routing
            return name, None, type(error).__name__

    def sync_active_profile_from_keychain(self, name, profile) -> None:
        """Pull Claude's rotated credential back into the last active profile."""
        if not name or profile is None:
            return
        stored = read_json(profile / ".credentials.json", {})
        keychain = read_keychain_credentials()
        if not keychain:
            return
        if stored.get("claudeAiOauth", {}).get("accessToken") == keychain["claudeAiOauth"].get("accessToken"):
            return
        if oauth_expiry(keychain) <= oauth_expiry(stored):
            return
        expected = self.metadata(profile).get("email")
        actual = oauth_email(keychain)
        if expected and actual != expected:
            return
        stored["claudeAiOauth"] = keychain["claudeAiOauth"]
        atomic_json(profile / ".credentials.json", stored)
        if actual:
            self.update_metadata(profile, email=actual)

    def before_route(self, profiles: dict) -> None:
        active = read_json(self.state_file, {}).get("active")
        self.sync_active_profile_from_keychain(active, profiles.get(active))

    def preferred(self, profiles: dict, arguments: list) -> set:
        """Profiles a Claude in Chrome extension is signed in to, when Chrome is in use.

        CLAUDE_ACCOUNT_CHROME=NAME[,NAME] overrides detection; CLAUDE_ACCOUNT_CHROME=off disables it.
        """
        override = os.environ.get("CLAUDE_ACCOUNT_CHROME")
        if override is not None:
            return {normalize_name(name) for name in override.split(",")} & set(profiles)
        if "--no-chrome" in arguments:
            return set()
        if "--chrome" not in arguments and read_json(home() / ".claude.json", {}).get(
                "claudeInChromeDefaultEnabled") is not True:
            return set()
        chrome = pathlib.Path(os.environ.get("CLAUDE_ACCOUNT_CHROME_DATA")
                              or home() / "Library/Application Support/Google/Chrome")
        uuids = set()
        for extension_id in CHROME_EXTENSION_IDS:
            for store in chrome.glob(f"*/Local Extension Settings/{extension_id}"):
                uuid = extension_account_uuid(store)
                if uuid:
                    uuids.add(uuid)
        return {name for name, profile in profiles.items() if self.metadata(profile).get("account_id") in uuids}

    def skip_process(self, words) -> bool:
        return len(words) > 2 and words[2] in SESSION_HELPER_MODES

    # --- management ------------------------------------------------------
    def all_profiles(self) -> dict:
        self.migrate_profile_names()
        if not self.root.is_dir():
            return {}
        return {path.name: path for path in sorted(self.root.iterdir())
                if path.is_dir() and not path.name.startswith(".") and
                ((path / ".credentials.json").is_file() or (path / "account.json").is_file())}

    def capture_active_account(self, name: str) -> int:
        credentials = read_keychain_credentials()
        if not credentials or not credentials.get("claudeAiOauth", {}).get("refreshToken"):
            print("claude: no active Claude OAuth credential was found in Keychain", file=sys.stderr)
            return 1
        profile = self.root / name
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        atomic_json(profile / ".credentials.json", credentials)
        try:
            self.publish_credentials(profile, credentials)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            print(f"claude: could not update account '{name}' in Keychain", file=sys.stderr)
        email = oauth_email(credentials)
        if email:
            self.update_metadata(profile, email=email, fetched_at=0)
        print(f"Captured active Claude login as account '{name}'.")
        return 0

    def login(self, name: str, email: str | None, adding: bool) -> int:
        if self.real_binary is None:
            print("claude: the real claude binary was not found", file=sys.stderr)
            return 127
        profile = self.root / name
        credentials_path = profile / ".credentials.json"
        previous = read_json(credentials_path, None)
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        if email:
            self.update_metadata(profile, expected_email=email.lower())
        command = [str(self.real_binary), "auth", "login", "--claudeai"] + (["--email", email] if email else [])
        environment = dict(os.environ)
        environment.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        result = _run(command, env=environment)
        if result.returncode != 0:
            return result.returncode
        code = self.capture_active_account(name)
        if code != 0:
            return code
        if email:
            actual = oauth_email(read_json(credentials_path, {}))
            if actual != email.lower():
                if previous is not None:
                    atomic_json(credentials_path, previous)
                else:
                    credentials_path.unlink(missing_ok=True)
                print(f"claude: browser authenticated {actual or 'unknown'}, expected {email.lower()}; "
                      "account was not changed", file=sys.stderr)
                return 1
        print(f"Claude account '{name}' is configured.")
        return 0

    def forget_profile(self, name: str, profile: pathlib.Path) -> None:
        delete_keychain_item(self.account_service(profile))
        shutil.rmtree(profile)

    def after_rename(self, old_name: str, profile: pathlib.Path) -> None:
        # The Keychain item name follows the directory path: republish under the new one.
        old_service = self.account_service(profile.with_name(old_name))
        credentials = read_keychain_credentials(old_service) or read_json(profile / ".credentials.json", {})
        if credentials.get("claudeAiOauth"):
            if oauth_expiry(credentials) > oauth_expiry(read_json(profile / ".credentials.json", {})):
                stored = read_json(profile / ".credentials.json", {})
                stored["claudeAiOauth"] = credentials["claudeAiOauth"]
                atomic_json(profile / ".credentials.json", stored)
            self.publish_credentials(profile, credentials)
            delete_keychain_item(old_service)

    def extra_management(self, option: str, rest: list) -> int | None:
        if option == "--capture-account":
            if len(rest) != 1:
                print("usage: claude --capture-account NAME", file=sys.stderr)
                return 2
            return self.capture_active_account(valid_name(rest[0]))
        return None

    def account_help(self) -> None:
        super().account_help()
        print("  claude --capture-account NAME        Save the active Keychain login as NAME")
        print("\nWith Claude in Chrome enabled, the account the extension is signed in to is preferred")
        print("while under the switch threshold (CLAUDE_ACCOUNT_CHROME=NAME overrides, =off disables).")

    def launch(self, arguments: list) -> None:
        selected, profiles, arguments, preferred = self.route(arguments)
        if selected is None:
            self.exec_real(arguments)
            return
        quiet = os.environ.get("CLAUDE_ACCOUNT_QUIET") == "1"
        if preferred and selected not in preferred and not quiet:
            print(f"claude: Chrome extension is signed in to {', '.join(sorted(preferred))}; "
                  f"browser tools won't connect on {selected}", file=sys.stderr)
        profile = profiles[selected]
        try:
            credentials = self.refresh_if_needed(profile, self.load_credentials(profile))
        except (RuntimeError, urllib.error.URLError) as error:
            print(f"claude: account '{selected}' needs reauthentication (run: claude --add-account {selected})",
                  file=sys.stderr)
            if os.environ.get("OMNI_ACCOUNTS_DEBUG") == "1":
                print(f"claude: refresh detail: {error}", file=sys.stderr)
            raise SystemExit(1)
        if not credentials.get("claudeAiOauth", {}).get("accessToken"):
            print(f"claude: account '{selected}' has no usable OAuth token", file=sys.stderr)
            raise SystemExit(1)
        try:
            self.publish_credentials(profile, credentials)
        except RuntimeError as error:
            print(f"claude: {error}", file=sys.stderr)
            raise SystemExit(1)
        environment = dict(os.environ)
        environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        environment.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
        environment["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(profile)
        environment["CLAUDE_ACCOUNT_ACTIVE"] = selected
        self.exec_real(arguments, environment)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

CODEX_PRIVATE = frozenset(("auth.json", "auth.json.lock", "auth.lock", "accounts"))
CODEX_SCAN_FILES = 400
CODEX_USAGE_TTL = 30  # list_accounts polls: reuse a scan this fresh
_CODEX_USAGE: dict = {}
CODEX_SCAN_DAYS = 8
CODEX_TAIL_BYTES = 512 * 1024
RATE_LIMITS = re.compile(rb'"rate_limits":\s*\{')


def jwt_claims(token) -> dict:
    try:
        payload = str(token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def codex_windows(limits) -> dict | None:
    """Map Codex rate-limit windows to five_hour / seven_day by their length, not by slot."""
    if not isinstance(limits, dict):
        return None
    usage = {}
    for slot in ("primary", "secondary"):
        window = limits.get(slot)
        if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
            continue
        minutes = window.get("window_minutes")
        key = "five_hour" if isinstance(minutes, (int, float)) and minutes <= 6 * 60 else "seven_day"
        usage[key] = {"utilization": float(window["used_percent"]), "resets_at": epoch(window.get("resets_at"))}
    return usage or None


def last_rate_limits(path: pathlib.Path) -> tuple:
    """(rate limits, event epoch) of the last token_count event with Codex windows in a rollout."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            for start in ((max(0, size - CODEX_TAIL_BYTES), 0) if size > CODEX_TAIL_BYTES else (0,)):
                handle.seek(start)
                data = handle.read(size - start if start else 32 * 1024 * 1024)
                for line in reversed(data.splitlines()):
                    if not RATE_LIMITS.search(line) or b'"used_percent"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    limits = (event.get("payload") or {}).get("rate_limits")
                    if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
                        continue
                    usage = codex_windows(limits)
                    if usage:
                        return usage, epoch(event.get("timestamp")) or path.stat().st_mtime
    except OSError:
        pass
    return None, None


def rollout_account(path: pathlib.Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            meta = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    payload = meta.get("payload") if isinstance(meta, dict) else None
    account = payload.get("creator_account_id") if isinstance(payload, dict) else None
    return account if isinstance(account, str) else None


class Codex(Provider):
    name = "codex"
    active_variable = "CODEX_ACCOUNT_ACTIVE"

    @property
    def base_home(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get("OMNI_CODEX_BASE_HOME") or home() / ".codex").expanduser()

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get("CODEX_ACCOUNT_ROOT") or self.base_home / "accounts").expanduser()

    def codex_home(self, profile: pathlib.Path) -> pathlib.Path:
        adopted = self.metadata(profile).get("home")
        return pathlib.Path(adopted) if adopted else profile / "home"

    def adopt_existing_login(self) -> None:
        """Register the existing ~/.codex login in place as an account (never copied: its refresh token rotates).

        Done once: removing the adopted account records that it should stay unregistered.
        """
        if not (self.base_home / "auth.json").is_file() or read_json(self.state_file, {}).get("adopt") is False:
            return
        records = [path for path in (self.root.iterdir() if self.root.is_dir() else [])
                   if path.is_dir() and not path.name.startswith(".") and (path / "account.json").is_file()]
        if any(pathlib.Path(self.metadata(path).get("home") or "") == self.base_home for path in records):
            return
        name = "main" if not (self.root / "main").exists() else "default"
        profile = self.root / name
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(profile / "account.json", {"home": str(self.base_home), "adopted": True})

    def all_profiles(self) -> dict:
        self.adopt_existing_login()
        if not self.root.is_dir():
            return {}
        return {path.name: path for path in sorted(self.root.iterdir())
                if path.is_dir() and not path.name.startswith(".") and (path / "account.json").is_file()}

    def profiles(self) -> dict:
        return {name: profile for name, profile in self.all_profiles().items()
                if (self.codex_home(profile) / "auth.json").is_file()}

    def auth(self, profile: pathlib.Path) -> dict:
        auth = read_json(self.codex_home(profile) / "auth.json", {})
        return auth if isinstance(auth, dict) else {}

    def identity(self, profile: pathlib.Path) -> dict:
        auth = self.auth(profile)
        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        claims = jwt_claims(tokens.get("id_token"))
        details = claims.get("https://api.openai.com/auth") if isinstance(
            claims.get("https://api.openai.com/auth"), dict) else {}
        email = claims.get("email")
        plan = details.get("chatgpt_plan_type")
        if not tokens and auth.get("OPENAI_API_KEY"):
            plan = "api key"
        return {"email": email.lower() if isinstance(email, str) else None,
                "account_id": tokens.get("account_id") or details.get("chatgpt_account_id"),
                "plan": plan}

    def email(self, profile: pathlib.Path) -> str | None:
        return self.identity(profile)["email"] or super().email(profile)

    def plan(self, profile: pathlib.Path) -> str | None:
        plan = self.identity(profile)["plan"]
        return str(plan).replace("_", " ").title() if plan else None

    def prepare_home(self, profile: pathlib.Path) -> pathlib.Path:
        """Link every shared ~/.codex entry into the account's home; only auth stays private."""
        codex_home = self.codex_home(profile)
        if codex_home == self.base_home:
            return codex_home
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.base_home.is_dir():
            for entry in self.base_home.iterdir():
                if entry.name in CODEX_PRIVATE:
                    continue
                link = codex_home / entry.name
                if not link.exists() and not link.is_symlink():
                    link.symlink_to(entry)
        return codex_home

    def rollouts(self) -> list:
        cutoff = time.time() - CODEX_SCAN_DAYS * 86400
        files = []
        for folder in ("sessions", "archived_sessions"):
            base = self.base_home / folder
            if not base.is_dir():
                continue
            for path in base.rglob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    files.append((mtime, path))
        files.sort(reverse=True)
        return files[:CODEX_SCAN_FILES]

    def usage(self, profiles: dict, live: bool) -> tuple:
        """Codex reports rate limits in each session's token_count events: read the newest per account.

        Rollouts name their account (session_meta creator_account_id); with a single account every
        rollout counts. Files go newest first and the scan stops once no older file can be fresher.
        """
        identities = {name: self.identity(profile)["account_id"] for name, profile in profiles.items()}
        key_set = (str(self.base_home), tuple(sorted(identities.items())))
        cached = getattr(self, "_usage_cache", None) or _CODEX_USAGE.get("last")
        if not live and cached and cached[1] == key_set and time.time() - cached[0] < CODEX_USAGE_TTL:
            return cached[2]
        single = len(profiles) == 1
        keys = {"*"} if single else {account for account in identities.values() if account}
        found: dict = {}
        for mtime, path in self.rollouts():
            if keys and all(key in found and found[key][1] >= mtime for key in keys):
                break
            key = "*" if single else rollout_account(path)
            if key not in keys or (key in found and found[key][1] >= mtime):
                continue
            usage, at = last_rate_limits(path)
            at = min(at, mtime) if at is not None else mtime  # an event is never newer than its file
            if usage is not None and (key not in found or at > found[key][1]):
                found[key] = (usage, at)
        now = time.time()
        usage_by_name, errors, checked = {}, {}, {}
        for name in profiles:
            entry = found.get("*" if single else identities[name])
            if entry:
                usage_by_name[name] = expire_windows(entry[0], now)
                checked[name] = entry[1]
            else:
                usage_by_name[name] = None  # no session on this account yet: unknown, not an error
        _CODEX_USAGE["last"] = self._usage_cache = (time.time(), key_set, (usage_by_name, errors, checked))
        return usage_by_name, errors, checked

    def for_selection(self, usage_by_name: dict) -> dict:
        # An account with no reading yet (e.g. just added) is fresh: it can take the next session.
        return {name: usage if usage is not None else {"seven_day": {"utilization": 0.0, "resets_at": None}}
                for name, usage in usage_by_name.items()}

    def login(self, name: str, email: str | None, adding: bool) -> int:
        if self.real_binary is None:
            print("codex: the real codex binary was not found", file=sys.stderr)
            return 127
        profile = self.root / name
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (profile / "account.json").is_file():
            atomic_json(profile / "account.json", {})
        if email:
            self.update_metadata(profile, expected_email=email.lower())
        codex_home = self.prepare_home(profile)
        auth_path = codex_home / "auth.json"
        previous = auth_path.read_bytes() if auth_path.is_file() else None
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(codex_home)
        result = _run([str(self.real_binary), "login"], env=environment)
        if result.returncode != 0:
            return result.returncode
        actual = self.identity(profile)["email"]
        if email and actual != email.lower():
            if previous is not None:
                auth_path.write_bytes(previous)
            else:
                auth_path.unlink(missing_ok=True)
            print(f"codex: browser authenticated {actual or 'unknown'}, expected {email.lower()}; "
                  "account was not changed", file=sys.stderr)
            return 1
        print(f"Codex account '{name}' is configured.")
        return 0

    def forget_profile(self, name: str, profile: pathlib.Path) -> None:
        # An adopted home is the user's own ~/.codex: only the account record goes, and it stays gone.
        if self.metadata(profile).get("adopted"):
            state = read_json(self.state_file, {})
            state = state if isinstance(state, dict) else {}
            state["adopt"] = False
            atomic_json(self.state_file, state)
        shutil.rmtree(profile)

    def launch(self, arguments: list) -> None:
        selected, profiles, arguments, _ = self.route(arguments)
        if selected is None:
            self.exec_real(arguments)
            return
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.prepare_home(profiles[selected]))
        environment["CODEX_ACCOUNT_ACTIVE"] = selected
        self.exec_real(arguments, environment)


BACKENDS = {"claude": Claude, "codex": Codex}


def backend(name) -> Provider:
    if name not in BACKENDS:
        raise AccountError("provider must be claude or codex", "invalid_provider")
    return BACKENDS[name]()


# ---------------------------------------------------------------------------
# Router shims
# ---------------------------------------------------------------------------

def shim_source(provider: str) -> pathlib.Path:
    return pathlib.Path(__file__).resolve().with_name("shims") / provider


def router_bin() -> pathlib.Path:
    return home() / ".omni/bin"


def legacy_bin() -> pathlib.Path:
    return home() / ".claude/bin"


def uses_legacy_bin() -> bool:
    """~/.claude/bin already holds a claude router (and is on PATH ahead of the real binary)."""
    return (legacy_bin() / "claude").exists() or (legacy_bin() / "claude").is_symlink()


def rendered_shim(provider: str) -> str:
    """The bundle's shim with this router's location baked in.

    The installed copy is a real file, not a link into the app: while the app is rebuilt or
    moved the shim stays and falls back to the real binary instead of vanishing from PATH.
    """
    text = shim_source(provider).read_text()
    router = str(pathlib.Path(__file__).resolve())
    return text.replace('router=""', "router='" + router.replace("'", "'\\''") + "'", 1)


def router_installed(provider: str) -> bool:
    try:
        installed = router_bin() / provider
        if not installed.is_file() or installed.read_text() != rendered_shim(provider):
            return False
        return not uses_legacy_bin() or os.path.realpath(legacy_bin() / provider) == os.path.realpath(installed)
    except OSError:
        return False


ZSHRC_BEGIN = "# >>> OmniTerm account router >>>"
ZSHRC_END = "# <<< OmniTerm account router <<<"


def install_routers(arguments=None) -> dict:
    """Install the claude/codex shims ahead of the real binaries; replace an old router only once verified."""
    paths, backups = {}, []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for provider in PROVIDERS:
        if not shim_source(provider).is_file():
            raise AccountError(f"router shim for {provider} is missing from the app", "shim_missing")
        installed = router_bin() / provider
        installed.parent.mkdir(parents=True, exist_ok=True)
        if (installed.exists() or installed.is_symlink()) and not is_shim(installed):
            backup = installed.with_name(f"{provider}.pre-omni-{stamp}")
            os.replace(installed, backup)
            backups.append(str(backup))
        fd, temporary = tempfile.mkstemp(prefix=f".{provider}.", dir=installed.parent)
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered_shim(provider))
        os.chmod(temporary, 0o755)
        os.replace(temporary, installed)
        check = _run([str(installed), "--router-check"], capture_output=True, text=True, timeout=30,
                     env={**os.environ, DEPTH_VARIABLE: "0"})
        try:
            report = json.loads(check.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            report = {}
        if report.get("provider") != provider:
            raise AccountError(f"the {provider} router failed its self-check: "
                               f"{(check.stderr or check.stdout).strip()[-300:]}", "router_check_failed")
        real = report.get("real")
        if not real:
            paths[provider] = None  # provider not installed: nothing to route yet
            continue
        if check.returncode != 0 or os.path.realpath(real) == os.path.realpath(installed) or \
                is_shim(pathlib.Path(real)):
            raise AccountError(f"the {provider} router resolved itself instead of the real binary",
                               "router_check_failed")
        if provider == "claude" and report.get("accounts") != sorted(Claude().profiles()):
            raise AccountError("the claude router does not see the existing accounts", "router_check_failed")
        paths[provider] = str(installed)
    if uses_legacy_bin():
        for provider in PROVIDERS:
            if paths.get(provider) is None:
                continue
            target = legacy_bin() / provider
            if os.path.realpath(target) != os.path.realpath(router_bin() / provider):
                if target.exists() or target.is_symlink():
                    backup = target.with_name(f"{provider}.pre-omni-{stamp}")
                    os.replace(target, backup)
                    backups.append(str(backup))
                target.symlink_to(router_bin() / provider)
            paths[provider] = str(target)
    else:
        zshrc = home() / ".zshrc"
        text = zshrc.read_text() if zshrc.is_file() else ""
        if ZSHRC_BEGIN not in text:
            with open(zshrc, "a") as handle:
                handle.write(f"\n{ZSHRC_BEGIN}\n# Routes claude and codex through OmniTerm's account router "
                             f"(keep last).\nexport PATH=\"$HOME/.omni/bin:$PATH\"\n{ZSHRC_END}\n")
    return {"claude": paths.get("claude"), "codex": paths.get("codex"), "backups": backups}


# ---------------------------------------------------------------------------
# Scheduler ops (called in-process by the knowledge worker)
# ---------------------------------------------------------------------------

def _providers(arguments) -> list:
    provider = (arguments or {}).get("provider")
    if provider in (None, ""):
        return list(PROVIDERS)
    if provider not in PROVIDERS:
        raise AccountError("provider must be claude or codex", "invalid_provider")
    return [provider]


def list_accounts(arguments=None, live=False) -> dict:
    return {"providers": {name: backend(name).describe(live) for name in _providers(arguments)}}


def refresh_accounts(arguments=None) -> dict:
    return list_accounts(arguments, live=True)


def _one(arguments) -> Provider:
    provider = (arguments or {}).get("provider")
    if not provider:
        raise AccountError("provider is required", "invalid_provider")
    return backend(provider)


def add_account(arguments) -> dict:
    _one(arguments).start_login(arguments.get("name"), arguments.get("email") or None, adding=True)
    return {"started": True}


def login_account(arguments) -> dict:
    _one(arguments).start_login(arguments.get("name"), None, adding=False)
    return {"started": True}


def remove_account(arguments) -> dict:
    _one(arguments).remove(arguments.get("name"))
    return {}


def rename_account(arguments) -> dict:
    _one(arguments).rename(arguments.get("name"), arguments.get("new_name"))
    return {}


def set_account_email(arguments) -> dict:
    _one(arguments).set_email(arguments.get("name"), arguments.get("email"))
    return {}


OPS = {"list_accounts": list_accounts, "refresh_accounts": refresh_accounts, "add_account": add_account,
       "login_account": login_account, "remove_account": remove_account, "rename_account": rename_account,
       "set_account_email": set_account_email, "install_routers": install_routers}


def login_job(provider: str, name: str, mode: str, email: str | None) -> int:
    """Background login started by add_account / login_account; records its outcome for list_accounts."""
    instance = backend(provider)
    status = instance.jobs_dir / f"{name}.json"
    try:
        code = instance.login(name, email, adding=mode == "add")
    except Exception as error:  # noqa: BLE001 - the outcome is reported through the status file
        print(f"{provider}: {error}", file=sys.stderr)
        code = 1
    if code == 0:
        status.unlink(missing_ok=True)
    else:
        log = instance.jobs_dir / f"{name}.log"
        try:
            lines = [line for line in log.read_text(errors="replace").splitlines() if line.strip()]
        except OSError:
            lines = []
        atomic_json(status, {"state": "failed", "finished": time.time(), "email": email,
                             "error": (lines[-1] if lines else f"login exited with {code}")[:300]})
    return code


def main(argv: list) -> int:
    if argv[:1] == ["--install-routers"]:
        # The installer's step: shims first on PATH; a failed check leaves things as they were.
        result = install_routers({})
        print(json.dumps(result))
        return 0 if result.get("ok", True) else 1
    if len(argv) >= 4 and argv[0] == "--login-job":
        return login_job(argv[1], argv[2], argv[3], argv[4] if len(argv) > 4 else None)
    if not argv or argv[0] not in PROVIDERS:
        print("usage: omni_accounts.py claude|codex [args...]", file=sys.stderr)
        return 2
    if int(os.environ.get(DEPTH_VARIABLE, "0") or 0) > 3:
        print(f"{argv[0]}: account router loop detected; set {argv[0].upper()}_REAL_BIN", file=sys.stderr)
        return 126
    instance = backend(argv[0])
    result = instance.management(argv[1:])
    if result is not None:
        return result
    instance.launch(argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
