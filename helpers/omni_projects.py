"""Forge projects for OmniTerm: the knowledge worker's view of `forge projects`.

Forge owns what a project is (its own repository, a separate install). OmniTerm depends on it through
its command line only: it finds the `forge` launcher and a Python new enough to run it, calls
`forge projects … --json`, and checks the output's `schema`. Without forge the Projects page says so and
sessions fall back to grouping by folder.

The worker runs with launchd's minimal PATH and the system Python, so nothing here imports forge.
Listing and path resolution are cached and refreshed on a background thread, so `list_sessions` never
waits for forge; detail calls run forge directly with a timeout, and only for a project forge listed.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

SCHEMA = 1
LIST_TTL = 120.0
DETAIL_TTL = 30.0
RESOLVE_TTL = 600.0
TIMEOUT = 25.0
LAUNCHERS = ("~/.local/bin/forge", "/usr/local/bin/forge", "/opt/homebrew/bin/forge", "~/bin/forge")
PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3",
           *("/opt/homebrew/bin/python3.%d" % minor for minor in range(20, 10, -1)),
           *("/usr/local/bin/python3.%d" % minor for minor in range(20, 10, -1)),
           "/usr/bin/python3")


# Folders macOS guards with a privacy prompt (TCC). Looking inside one from the worker would ask the
# person to grant OmniTerm access, so sessions there are never sent to forge to be placed.
PROTECTED_FOLDERS = ("~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mobile Documents", "~/Pictures",
                     "~/Movies", "~/Music", "/Volumes")


def _protected(path):
    for folder in PROTECTED_FOLDERS:
        base = os.path.expanduser(folder)
        if path == base or path.startswith(base + "/"):
            return True
    return False


class ForgeUnavailable(Exception):
    pass


def _python_ok(python):
    try:
        result = subprocess.run([python, "-c", "import sys;print(sys.version_info >= (3, 11))"],
                                capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        return result.returncode == 0 and result.stdout.strip() == "True"
    except (OSError, subprocess.SubprocessError):
        return False


def find_forge(environ=None):
    """(python, launcher) able to run forge, or raises ForgeUnavailable with the reason."""
    environ = os.environ if environ is None else environ
    explicit = environ.get("OMNI_FORGE")
    launchers = [explicit] if explicit else [os.path.expanduser(path) for path in LAUNCHERS]
    launcher = next((path for path in launchers if path and os.path.isfile(path)), None)
    if launcher is None:
        raise ForgeUnavailable("forge is not installed (no forge launcher on this Mac)")
    python = next((path for path in PYTHONS if os.path.isfile(path) and _python_ok(path)), None)
    if python is None:
        raise ForgeUnavailable("forge needs Python 3.11 or newer, and none was found")
    return python, os.path.realpath(launcher)


CACHE = "~/Library/Application Support/OmniTerm/forge-projects.json"


class Projects:
    def __init__(self, runner=None, clock=time.monotonic, cache=CACHE):
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._forge = None
        self._error = None
        self._projects = None
        self._listed_at = None
        self._resolved = {}
        self._resolved_at = {}
        self._pending = set()
        self._refreshing = False
        self._details = {}
        # The last answer survives relaunches, so the page and the session groups show at once and
        # forge only refreshes them in the background.
        self._cache = os.path.expanduser(cache) if cache else None
        self._load_cache()

    def _load_cache(self):
        if not self._cache:
            return
        try:
            with open(self._cache) as handle:
                data = json.load(handle)
            projects = data.get("projects")
            if isinstance(projects, list):
                self._projects = [p for p in projects if isinstance(p, dict) and isinstance(p.get("root"), str)]
            resolved = data.get("resolved")
            if isinstance(resolved, dict):
                self._resolved = {k: v for k, v in resolved.items()
                                  if isinstance(k, str) and (v is None or isinstance(v, str))}
                self._resolved_at = {k: self._clock() for k in self._resolved}
        except (OSError, ValueError, AttributeError):
            pass

    def _save_cache(self):
        if not self._cache:
            return
        with self._lock:
            data = {"saved": time.time(), "projects": self._projects or [], "resolved": dict(self._resolved)}
        try:
            os.makedirs(os.path.dirname(self._cache), exist_ok=True)
            temporary = self._cache + ".tmp"
            with open(temporary, "w") as handle:
                json.dump(data, handle)
            os.replace(temporary, self._cache)
        except OSError:
            pass

    # Running forge.

    def _run(self, arguments, stdin=None):
        payload = self._runner(arguments, stdin) if self._runner is not None else self._run_forge(arguments, stdin)
        if not isinstance(payload, dict) or not isinstance(payload.get("schema"), int) or payload["schema"] < SCHEMA:
            raise ForgeUnavailable("this forge is too old for OmniTerm: update forge")
        return payload

    def _run_forge(self, arguments, stdin):
        if self._forge is None:
            self._forge = find_forge()
        python, launcher = self._forge
        environment = {"HOME": os.path.expanduser("~"), "LC_ALL": "en_US.UTF-8",
                       "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
        result = subprocess.run([python, launcher, "projects", *arguments, "--json"], input=stdin,
                                capture_output=True, text=True, timeout=TIMEOUT, env=environment,
                                stdin=None if stdin is not None else subprocess.DEVNULL)
        if result.returncode != 0 and not result.stdout.strip():
            raise ForgeUnavailable((result.stderr.strip().splitlines() or ["forge projects failed"])[-1][:300])
        return json.loads(result.stdout)

    def _status(self):
        if self._error:
            return {"available": False, "error": self._error}
        return {"available": self._projects is not None, "error": None}

    # Cached listing, refreshed off the request path.

    def _refresh(self, list_again=True):
        try:
            if list_again:
                projects = self._run(["list"]).get("projects") or []
                projects = [p for p in projects if isinstance(p, dict) and isinstance(p.get("root"), str)]
                for project in projects:
                    # A checkout may be a symlink (`@head` -> elsewhere); sessions can record either path.
                    bases = [project["root"]] + [c.get("path") for c in project.get("checkouts") or []
                                                 if isinstance(c, dict) and isinstance(c.get("path"), str)]
                    project["_bases"] = sorted({os.path.realpath(b) for b in bases} | set(bases))
                with self._lock:
                    self._projects = projects
                    self._listed_at = self._clock()
                    self._error = None
            self._resolve_pending()
            self._save_cache()
        except (ForgeUnavailable, OSError, ValueError, subprocess.SubprocessError) as exc:
            with self._lock:
                self._error = str(exc) or type(exc).__name__
                self._listed_at = self._clock()
        finally:
            with self._lock:
                self._refreshing = False

    def _resolve_pending(self):
        with self._lock:
            pending = sorted(self._pending)[:500]
        if not pending:
            return
        resolved = self._run(["resolve", "--stdin"], stdin="\n".join(pending) + "\n").get("resolved") or {}
        now = self._clock()
        with self._lock:
            for path in pending:
                root = resolved.get(path)
                self._resolved[path] = root if isinstance(root, str) else None
                self._resolved_at[path] = now
                self._pending.discard(path)

    def _kick(self, wait=False):
        with self._lock:
            stale = self._listed_at is None or self._clock() - self._listed_at >= LIST_TTL
            start = (stale or self._pending) and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            if wait:
                self._refresh(stale)
            else:
                threading.Thread(target=self._refresh, args=(stale,), name="omni-projects", daemon=True).start()

    def list_projects(self, arguments):
        refresh = arguments.get("refresh") is True
        if refresh:
            with self._lock:
                self._listed_at = None
        # Waits only the first time (or on an explicit refresh); otherwise the cache answers and a
        # background refresh catches up.
        self._kick(wait=refresh or (self._projects is None and self._error is None))
        with self._lock:
            return {"forge": self._status(),
                    "projects": [{k: v for k, v in p.items() if not k.startswith("_")} for p in self._projects or []]}

    def _known(self, identifier):
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("project is required")
        self._kick(wait=self._projects is None)
        with self._lock:
            for project in self._projects or []:
                if project.get("id") == identifier or project.get("root") == identifier:
                    return project
        raise ValueError("unknown project")

    def get_project(self, arguments):
        """One project's page data; kept briefly, since it costs several git calls on the worker thread."""
        project = self._known(arguments.get("project"))
        root = project["root"]
        cached = self._details.get(root)
        if cached and arguments.get("refresh") is not True and self._clock() - cached[0] < DETAIL_TTL:
            return {"project": cached[1]}
        detail = self._run(["show", root]).get("project")
        self._details[root] = (self._clock(), detail)
        return {"project": detail}

    def list_project_folder(self, arguments):
        project = self._known(arguments.get("project"))
        relative = arguments.get("path") or ""
        if not isinstance(relative, str) or relative.startswith("/") or "\0" in relative:
            raise ValueError("path must be relative to the project")
        arguments_list = ["files", project["root"]] + ([relative] if relative else [])
        return {"folder": self._run(arguments_list).get("folder")}

    def project_of(self, arguments):
        """{path: project id or None} for workspaces such as agents', from cached data only."""
        paths = arguments.get("paths")
        if not isinstance(paths, list):
            raise ValueError("paths must be a list")
        rows = [{"workspace": p} for p in paths[:500] if isinstance(p, str)]
        self.annotate(rows)
        return {"projects": {row["workspace"]: (row.get("project") or {}).get("id") for row in rows}}

    def session_filter(self, identifier, column="s.workspace"):
        """SQL (condition, bindings) matching sessions whose workspace lies in a project: its root, checkouts
        (real paths too) and workspaces forge resolved to it. Prefixes compare with substr, never LIKE."""
        project = self._known(identifier)
        bases = project.get("_bases") or [project["root"]]
        with self._lock:
            exact = [path for path, root in self._resolved.items() if root == project["root"]][:200]
        parts, bindings = [], []
        for base in bases[:50]:
            prefix = base.rstrip("/") + "/"
            parts.append("(" + column + "=? OR substr(" + column + ",1,?)=?)")
            bindings.extend([base, len(prefix), prefix])
        if exact:
            parts.append(column + " IN (" + ",".join("?" * len(exact)) + ")")
            bindings.extend(exact)
        return "(" + " OR ".join(parts) + ")", bindings

    def get_project_file(self, arguments):
        project = self._known(arguments.get("project"))
        relative = arguments.get("path")
        if not isinstance(relative, str) or not relative or relative.startswith("/") or "\0" in relative:
            raise ValueError("path must be relative to the project")
        return {"file": self._run(["file", project["root"], relative]).get("file")}

    # Sessions: each row gets the project its workspace belongs to.

    def _match(self, workspace, projects):
        best = None
        for project in projects:
            for base in project.get("_bases") or [project["root"]]:
                if isinstance(base, str) and (workspace == base or workspace.startswith(base.rstrip("/") + "/")):
                    if best is None or len(base) > best[0]:
                        best = (len(base), project)
        return best[1] if best else None

    def annotate(self, sessions):
        """Adds `project` ({id, name}) to each session from cached data only; never waits for forge."""
        self._kick()
        with self._lock:
            projects = list(self._projects or [])
            by_root = {p["root"]: p for p in projects}
            now = self._clock()
            for session in sessions:
                workspace = session.get("workspace")
                if not isinstance(workspace, str) or not workspace.startswith("/"):
                    continue
                workspace = os.path.normpath(workspace)
                project = self._match(workspace, projects)
                if project is None:
                    if workspace in self._resolved and now - self._resolved_at.get(workspace, 0) < RESOLVE_TTL:
                        project = by_root.get(self._resolved[workspace] or "")
                    elif not _protected(workspace):
                        self._pending.add(workspace)
                if project is not None:
                    session["project"] = {"id": project["id"], "name": project.get("display_name") or project["name"],
                                          "root": project["root"]}
        if self._pending:
            self._kick()
        return sessions
