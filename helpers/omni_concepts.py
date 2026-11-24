"""Deterministic concepts in memory text: the files, packages, commands, error
codes, symbols and plans a memory names, so the graph can link memories that
talk about the same thing across sessions and projects.

Extraction is pattern matching over a memory's title, tags and body; nothing
calls a model. Concepts are keys of the form `<kind>:<value>`. Topics are
communities found by label propagation over the memory–concept bipartite
graph, in a fixed order with ties broken by key, so the same memories always
give the same topics.

Memory text is untrusted data; it is only matched, never interpreted.
"""

import os
import re


MAX_CONCEPTS = 12
HUB_MINIMUM = 2
HUB_MAXIMUM = 60
TOPIC_LIMIT = 24
PROPAGATION_ROUNDS = 20
KIND_ORDER = ("plan", "error", "file", "package", "command", "symbol")

EXTENSIONS = frozenset((
    "py", "rb", "swift", "js", "mjs", "cjs", "ts", "tsx", "jsx", "json", "yml", "yaml", "toml", "md", "sh",
    "zsh", "bash", "sql", "go", "rs", "c", "h", "m", "mm", "cpp", "hpp", "java", "kt", "plist", "entitlements",
    "xcconfig", "lock", "txt", "cfg", "ini", "css", "scss", "html", "erb", "rake", "gemspec", "patch", "expect",
    "xib", "storyboard", "pbxproj", "log", "csv", "env", "conf"))
# Conventional documents every project has: a relative path to one names a
# different file in each project, so it would join unrelated memories.
CONVENTIONAL_DOCUMENTS = frozenset(name.lower() for name in (
    "README.md", "CHANGELOG.md", "MASTER.md", "STATUS.md", "INDEX.md", "GUIDELINES.md", "AGENTS.md",
    "CLAUDE.md", "FEATURES.md", "VISION.md", "NEXT.md", "COVERAGE.md", "TODO.md", "FAKEIT.md",
    "CONTRIBUTING.md", "LICENSE.md", "SECURITY.md", "package.json", "Gemfile.lock", "pnpm-lock.yaml"))
BARE_FILES = frozenset(("Makefile", "Gemfile", "Dockerfile", "Podfile", "Rakefile", "Procfile", "Brewfile"))
ERRNO = frozenset((
    "E2BIG", "EACCES", "EADDRINUSE", "EAGAIN", "EBADF", "EBUSY", "ECHILD", "ECONNABORTED", "ECONNREFUSED",
    "ECONNRESET", "EDEADLK", "EEXIST", "EHOSTUNREACH", "EINTR", "EINVAL", "EIO", "EISDIR", "EMFILE",
    "ENAMETOOLONG", "ENETUNREACH", "ENOENT", "ENOMEM", "ENOSPC", "ENOTDIR", "ENOTEMPTY", "ENOTFOUND", "ENOTSUP",
    "EPERM", "EPIPE", "EROFS", "ESRCH", "ETIMEDOUT", "EXDEV"))
SIGNALS = frozenset((
    "SIGABRT", "SIGBUS", "SIGCHLD", "SIGHUP", "SIGILL", "SIGINT", "SIGKILL", "SIGPIPE", "SIGQUIT", "SIGSEGV",
    "SIGSTOP", "SIGTERM", "SIGTRAP", "SIGUSR1", "SIGUSR2"))
# First words of backticked prose or code that are not commands.
NOT_COMMANDS = frozenset((
    "a", "an", "and", "as", "async", "await", "case", "class", "const", "def", "elif", "else", "enum", "for",
    "func", "function", "if", "import", "in", "is", "let", "not", "or", "private", "public", "return", "self",
    "static", "struct", "the", "then", "this", "to", "try", "var", "while", "with", "from", "true", "false",
    "none", "null", "nil"))
INSTALLERS = frozenset(("pip", "pip3", "uv", "pipx", "npm", "yarn", "pnpm", "bun", "gem", "bundle", "brew"))
TEMPORARY = ("/tmp/", "/private/", "/var/folders/", "/dev/", "/proc/")

URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
BACKTICK = re.compile(r"`([^`\n]{1,200})`")
PATH = re.compile(r"(?<![\w.:/~@$-])((?:~|\.{1,2})?/?(?:[\w.@+-]+/)+[\w.@+-]*)")
BARE_FILE = re.compile(r"(?<![\w./~@-])([\w-][\w.-]*\.([A-Za-z0-9]{1,12}))(?![\w/-])")
BARE_NAME = re.compile(r"(?<![\w./~@-])(" + "|".join(sorted(BARE_FILES)) + r")(?![\w/-]|\.\w)")
PLAN = re.compile(r"(?<![\w-])@?(\d{4}-\d{2}-\d{2}(?:-\d{2})?-[a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![\w-])")
PLAN_NAME = re.compile(r"^@?\d{4}-\d{2}-\d{2}(?:-\d{2})?-[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.md)?$")
EXCEPTION = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning|Interrupt))\b")
ERROR_CODE = re.compile(r"\b((?:SQLITE|EXC|ERR)_[A-Z0-9_]*[A-Z0-9]|errSec[A-Z]\w*|E[A-Z0-9]{2,14}|SIG[A-Z0-9]{2,6})\b")
INSTALL = re.compile(r"\b(?:pip3?\s+install|uv\s+(?:pip\s+install|add)|pipx\s+install|npm\s+(?:install|i|add)|"
                     r"yarn\s+add|pnpm\s+(?:add|install|i)|bun\s+add|gem\s+install|bundle\s+add|brew\s+install)"
                     r"(?=([^\n`;&|]*))")
PYTHON_IMPORT = re.compile(r"(?m)^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import\b|import\s+([A-Za-z_][\w.]*))")
INLINE_FROM = re.compile(r"\bfrom\s+([A-Za-z_][\w.]*)\s+import\s+[\w(*]")
REQUIRE = re.compile(r"\brequire(?:\(\s*|\s+)['\"]([^'\"\s]+)['\"]|\bfrom\s+['\"]([^'\"\s]+)['\"]")
PROMPT = re.compile(r"(?m)(?:^|[\s(])\$\s+([^\n`]+)")
WORD = re.compile(r"^[a-z][a-z0-9._+-]*$")
SUBCOMMAND = re.compile(r"^[a-z][a-z0-9:_-]*$")
SYMBOL = re.compile(r"^[A-Za-z_]\w*(?:(?:\.|::|#)[A-Za-z_]\w*)*(?:\(\))?$")
CAMEL = re.compile(r"[a-z0-9][A-Z]")
PACKAGE = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*$")


def label(key):
    return key.split(":", 1)[1] if ":" in key else key


def kind_of(key):
    return key.split(":", 1)[0]


class _Collector:
    def __init__(self):
        self.found = {kind: [] for kind in KIND_ORDER}

    def add(self, kind, value):
        if value and value not in self.found[kind]:
            self.found[kind].append(value)

    def keys(self, limit=MAX_CONCEPTS):
        # Round-robin across kinds, so a body listing twenty files still names
        # its error and its command.
        result, depth = [], 0
        while len(result) < limit and any(depth < len(values) for values in self.found.values()):
            for kind in KIND_ORDER:
                values = self.found[kind]
                if depth < len(values) and len(result) < limit:
                    result.append(kind + ":" + values[depth])
            depth += 1
        return result


def _roots(workspaces):
    """Workspace roots, longest first; a worktree checkout (`repo/@head`) also
    folds paths under its repository."""
    roots = set()
    for workspace in workspaces or ():
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            continue
        parts = [part for part in workspace.rstrip("/").split("/") if part]
        roots.add("/" + "/".join(parts))
        while len(parts) > 1 and parts[-1].startswith("@"):
            parts.pop()
            roots.add("/" + "/".join(parts))
    return sorted(roots, key=lambda root: (-len(root), root))


def normalise_path(path, roots=(), home=None):
    """A file concept's value: line numbers and trailing punctuation stripped,
    paths inside a workspace made repo-relative, the home folder folded to `~`.
    Returns None for temporary files and for the workspace itself."""
    path = re.sub(r"#L\d+(?:-L?\d+)?$", "", path).rstrip(".,;:)]}'\"")
    if not path:
        return None
    home = (home if home is not None else os.path.expanduser("~")).rstrip("/")
    if path.startswith("~/") and home:
        path = home + path[1:]
    if path.startswith("/"):
        if any(path.startswith(prefix) for prefix in TEMPORARY):
            return None
        conventional = path.rstrip("/").rsplit("/", 1)[-1].lower() in CONVENTIONAL_DOCUMENTS
        for root in roots if not conventional else ():  # a conventional document keeps its project in the path
            if path.startswith(root + "/"):
                parts = [part for part in path[len(root) + 1:].split("/") if part]
                while parts and parts[0].startswith("@"):
                    parts.pop(0)
                return "/".join(parts) or None
        if home and path.startswith(home + "/"):
            path = "~" + path[len(home):]
        if len([part for part in path.split("/") if part and part != "~"]) < 2:
            return None
        return path.rstrip("/") or None
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("../"):
        # A markdown link relative to its own file: keep the part that names
        # something ("personas/carl.md"), drop bare "../MASTER.md"-style links,
        # which every project has and would join unrelated memories.
        while path.startswith("../"):
            path = path[3:]
        if path.count("/") < 1:
            return None
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if "." not in last or last.rsplit(".", 1)[1].lower() not in EXTENSIONS:
        return None  # a relative path names a file; "and/or" does not
    if last.lower() in CONVENTIONAL_DOCUMENTS:
        return None
    return path


def _file(collector, path, roots, home):
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if PLAN_NAME.match(base):
        collector.add("plan", base.lstrip("@")[:-3] if base.endswith(".md") else base.lstrip("@"))
        return
    value = normalise_path(path, roots, home)
    if value:
        collector.add("file", value)


def _packages(collector, text):
    for match in INSTALL.finditer(text):
        for token in match.group(1).split()[:6]:
            if token.startswith("-"):
                continue
            if token in ("2>&1", ">", ">>") or token in INSTALLERS:
                break
            last = token.endswith((".", ","))
            name = re.split(r"[<>=!~\[;]", token.rstrip(".,"), maxsplit=1)[0].lower().strip("'\"")
            if name.startswith("@"):
                name = "@" + name[1:].split("@", 1)[0]
            else:
                name = name.split("@", 1)[0]
            if PACKAGE.match(name):
                collector.add("package", name)
            if last:
                break  # the sentence goes on in prose
    for match in list(PYTHON_IMPORT.finditer(text)) + list(INLINE_FROM.finditer(text)):
        module = next(group for group in match.groups() if group)
        collector.add("package", module.split(".", 1)[0].lower())
    for match in REQUIRE.finditer(text):
        name = match.group(1) or match.group(2)
        if name.startswith((".", "/")):
            continue
        parts = name.split("/")
        name = "/".join(parts[:2]) if name.startswith("@") else parts[0]
        if PACKAGE.match(name.lower()):
            collector.add("package", name.lower())


def _command(collector, text):
    tokens = text.strip().split()
    while tokens and (tokens[0] == "sudo" or re.match(r"^[A-Z_][A-Z0-9_]*=", tokens[0])):
        tokens.pop(0)
    if not tokens:
        return False
    first = tokens[0].rsplit("/", 1)[-1]
    if not WORD.match(first) or first in NOT_COMMANDS:
        return False
    parts = [first]
    if len(tokens) > 2 and tokens[1] == "-m" and re.match(r"^[A-Za-z_][\w.]*$", tokens[2]):
        parts += ["-m", tokens[2]]
    elif len(tokens) > 1 and SUBCOMMAND.match(tokens[1]):
        parts.append(tokens[1])
    collector.add("command", " ".join(parts))
    return True


def _span(collector, span, roots, home):
    """A backticked span (or a tag) is a command, a file, an error or a symbol."""
    span = span.strip()
    if span.startswith("$ "):
        _command(collector, span[2:])
        return
    if " " in span:
        if not re.search(r"[=(){};]", span.split()[0]):
            _command(collector, span)
        return
    if "/" in span:
        _file(collector, span, roots, home)
        return
    bare = re.sub(r"(?::\d+)+$", "", span)
    if bare in BARE_FILES or "." in bare and bare.rsplit(".", 1)[1].lower() in EXTENSIONS and \
            re.match(r"^[\w-][\w.-]*$", bare):
        _file(collector, bare, roots, home)
        return
    if PLAN_NAME.match(span):
        collector.add("plan", span.lstrip("@").rsplit(".md", 1)[0])
        return
    if _error(collector, span):
        return
    if SYMBOL.match(span) and len(span) >= 3 and ("_" in span or "." in span or "::" in span or "#" in span or
                                                  span.endswith("()") or CAMEL.search(span)):
        symbol = span[:-2] if span.endswith("()") else span
        for prefix in ("self.", "this."):
            if symbol.startswith(prefix) and len(symbol) > len(prefix):
                symbol = symbol[len(prefix):]
        collector.add("symbol", symbol)


def _is_error(name):
    if EXCEPTION.fullmatch(name):
        return True
    if name in ERRNO or name in SIGNALS:
        return True
    return bool(re.fullmatch(r"(?:SQLITE|EXC|ERR)_[A-Z0-9_]*[A-Z0-9]|errSec[A-Z]\w*", name))


def _error(collector, span):
    name = span.rsplit(".", 1)[-1] if "." in span else span
    if _is_error(name):
        collector.add("error", name)
        return True
    return False


def extract(title, body, tags=(), workspaces=(), home=None, limit=MAX_CONCEPTS):
    """The concept keys of a memory, at most `limit`, title first."""
    roots = _roots(workspaces)
    collector = _Collector()
    texts = [str(title or ""), str(body or "")]
    for text in texts[:1]:
        _scan(collector, text, roots, home)
    for tag in tags or ():
        if isinstance(tag, str) and tag.strip():
            _span(collector, tag, roots, home)
    _scan(collector, texts[1], roots, home)
    return collector.keys(limit)


def _scan(collector, text, roots, home):
    text = URL.sub(" ", text.replace("${HOME}", "~").replace("$HOME", "~"))
    for match in PLAN.finditer(text):
        collector.add("plan", match.group(1))
    for match in EXCEPTION.finditer(text):
        collector.add("error", match.group(1))
    for match in ERROR_CODE.finditer(text):
        if _is_error(match.group(1)):
            collector.add("error", match.group(1))
    spans = [match.group(1) for match in BACKTICK.finditer(text)]
    prose = BACKTICK.sub(" ", text)
    for match in PATH.finditer(prose):
        _file(collector, match.group(1), roots, home)
    for match in BARE_FILE.finditer(prose):
        if match.group(2).lower() in EXTENSIONS:
            _file(collector, match.group(1), roots, home)
    for match in BARE_NAME.finditer(prose):
        collector.add("file", match.group(1))
    _packages(collector, text)
    for span in spans:
        _packages(collector, span)
    for match in PROMPT.finditer(prose):
        _command(collector, match.group(1))
    for span in spans:
        _span(collector, span, roots, home)


def workspace_alias(path):
    """The repository a worktree checkout belongs to, so one project is one hub:
    `repo/@head/sub` → `repo/sub`, `repo-wt/<name>/sub` → `repo/sub` and
    `repo/.claude/worktrees/<name>/sub` → `repo/sub`. Other paths are unchanged."""
    if not isinstance(path, str) or not path.startswith("/"):
        return path
    parts = [part for part in path.rstrip("/").split("/") if part]
    folded, index = [], 0
    while index < len(parts):
        part = parts[index]
        if part.startswith("@") and folded:
            index += 1
        elif part.endswith("-wt") and len(part) > 3 and folded:
            folded.append(part[:-3])
            index += 2
        elif part == ".claude" and parts[index + 1:index + 2] == ["worktrees"] and folded:
            index += 3
        else:
            folded.append(part)
            index += 1
    return "/" + "/".join(folded)


def fold_aliases(memberships):
    """Merge file concepts that name the same file: a bare file name, or a home
    path ending in a repo-relative path, maps to that path when exactly one
    path in the shown memories fits. `memberships` maps memory → concept keys."""
    files = sorted({key for keys in memberships.values() for key in keys if kind_of(key) == "file"})
    relative = [key for key in files if "/" in label(key) and not label(key).startswith(("~", "/"))]
    aliases = {}
    for key in files:
        if label(key).startswith(("~", "/")):
            matches = [other for other in relative if label(key).endswith("/" + label(other))]
            longest = max((len(other) for other in matches), default=0)
            matches = [other for other in matches if len(other) == longest]
            if len(matches) == 1:
                aliases[key] = matches[0]
    paths = sorted({aliases.get(key, key) for key in files if "/" in label(key)})
    for key in files:
        if "/" not in label(key):
            matches = [other for other in paths if label(other).rsplit("/", 1)[1] == label(key)]
            if len(matches) == 1:
                aliases[key] = matches[0]
    return {memory: list(dict.fromkeys(aliases.get(key, key) for key in keys))
            for memory, keys in memberships.items()}


def hubs(memberships, minimum=HUB_MINIMUM, maximum=HUB_MAXIMUM):
    """Concepts shared by at least `minimum` and at most `maximum` memories, each
    with its sorted members. Rarer concepts link nothing; commoner ones link
    everything and say nothing, like a tag on every memory."""
    members = {}
    for memory in sorted(memberships):
        for key in memberships[memory]:
            members.setdefault(key, []).append(memory)
    return {key: found for key, found in sorted(members.items()) if minimum <= len(found) <= maximum}


def communities(memberships, shared=None, rounds=PROPAGATION_ROUNDS, limit=TOPIC_LIMIT):
    """Topics: memories grouped by label propagation over the memory–concept
    graph, using only shared concepts. Each topic lists its members (sorted) and
    its three most common concepts. Largest topics first; singletons dropped."""
    shared = hubs(memberships) if shared is None else shared
    neighbours = {}
    for key, members in shared.items():
        node = "c:" + key
        neighbours[node] = ["m:" + memory for memory in members]
        for memory in members:
            neighbours.setdefault("m:" + memory, []).append(node)
    order = sorted(neighbours)
    labels = {node: node for node in order}
    for _ in range(rounds):
        changed = False
        for node in order:
            counts = {}
            for other in neighbours[node]:
                counts[labels[other]] = counts.get(labels[other], 0) + 1
            best = max(counts.values())
            current = labels[node]
            if counts.get(current) == best:
                continue  # Keeping the label on a tie lets propagation settle.
            labels[node] = min(name for name, count in counts.items() if count == best)
            changed = True
        if not changed:
            break
    groups = {}
    for node in order:
        if node.startswith("m:"):
            groups.setdefault(labels[node], []).append(node[2:])
    topics = []
    for members in groups.values():
        if len(members) < 2:
            continue
        counts = {}
        for memory in members:
            for key in memberships.get(memory, ()):
                if key in shared:
                    counts[key] = counts.get(key, 0) + 1
        concepts = sorted(counts, key=lambda key: (-counts[key], key))[:3]
        topics.append({"members": sorted(members), "concepts": concepts})
    topics.sort(key=lambda topic: (-len(topic["members"]), topic["members"][0]))
    return topics[:limit]
