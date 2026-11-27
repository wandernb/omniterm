"""Code graph: every project's source files become modules, classes, functions and the references between
them (imports, calls, inheritance), kept current in the background and queried by traversal.

Projects are the ones forge lists (from the worker's cached `forge-projects.json`) plus the git
repositories under `~/Projects` (at most `PROJECT_DEPTH` levels down; where worktrees sit side by
side, only `@head`). Folders macOS guards with a privacy prompt are never read. A project lists its
files with `git ls-files` (so `.gitignore` holds) or, without git, a bounded walk; names in
`SKIPPED_NAMES`, hidden paths and secrets are never opened. `.omni/graph.json` in a project may add
`include` folders the default rules skip (for example vendored code the project owns) and further
`exclude` prefixes.

The graph is derived data in a sidecar SQLite owned by the knowledge worker, rebuilt from scratch on
a schema change. It holds names, paths and line numbers, never source text. References are stored
unresolved, with the dotted name the source implies (`hint`), and resolved when queried, so a changed
file replaces only its own rows. An edge whose hint names its target is `extracted`; one matched by
its bare name only is `inferred`.

File contents are untrusted data. Nothing here runs or interprets them.
"""

import ast
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote


SCHEMA_VERSION = 3
FILES_PER_TICK = 200
TICK_SECONDS = 0.3
BYTES_PER_TICK = 2 * 1024 * 1024
QUERY_REFRESH_SECONDS = 1.5
QUERY_RELIST_SECONDS = 2.0
QUERY_LIST_WAIT = 1.0  # how long a query waits for its project's listing before answering from the index  # a query about a project relists it when its list is older than this
MAX_FILE_BYTES = 512 * 1024
MAX_PROJECT_FILES = 20000
DISCOVERY_SECONDS = 600.0
RELIST_HOT_SECONDS = 30.0  # a project with a recent session, or the one a query names
RELIST_COLD_SECONDS = 1800.0
PROJECT_DEPTH = 4
WALK_DEPTH = 14
GIT_TIMEOUT = 10.0
FORGE_CACHE = "Library/Application Support/OmniTerm/forge-projects.json"
PROTECTED_FOLDERS = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents", "Pictures", "Movies",
                     "Music")
SKIPPED_NAMES = frozenset((
    "node_modules", ".git", ".hg", ".svn", ".attic", ".legacy", ".venv", "venv", "vendor", "Vendor",
    "dist", "build", "tmp", "log", "logs", "coverage", "__pycache__", ".next", ".cache", "public",
    "storage", "archives", "Library", "Pods", "DerivedData", "target", "site-packages", "bower_components",
))
SENSITIVE = re.compile(r"(^|/)(\.env[^/]*|secrets?\.ya?ml|credentials[^/]*|master\.key|id_(rsa|dsa|ecdsa|ed25519)[^/]*|"
                       r"known_hosts|authorized_keys|[^/]*\.(pem|key|p12|p8|pfx|jks|keystore|keychain|asc|gpg))$"
                       r"|(^|/)(config/secrets|\.ssh|\.gnupg)/", re.IGNORECASE)
MODES = ("query", "explain", "neighbors", "path", "affected", "stats", "overview")
CONFIDENCE_RANK = {"extracted": 0, "inferred": 1}
# A bare-name match to one of these is far more often a built-in container or I/O method than the
# project's own definition, so it is never inferred.
COMMON_NAMES = frozenset((
    "get", "set", "items", "keys", "values", "update", "pop", "add", "append", "extend", "insert", "remove",
    "discard", "clear", "copy", "join", "split", "strip", "format", "read", "write", "close", "open", "run",
    "start", "stop", "send", "recv", "encode", "decode", "replace", "find", "index", "count", "sort", "lower",
    "upper", "startswith", "endswith", "exists", "execute", "fetchone", "fetchall", "commit", "load", "loads",
    "dump", "dumps", "parse", "match", "search", "sub", "group", "info", "debug", "warning", "error",
    "exception", "map", "filter", "each", "push", "then", "toString", "length", "size", "next", "wait",
    "acquire", "release", "put", "flush", "seek", "tell", "setdefault", "is_file", "is_dir", "mkdir", "unlink",
    "hexdigest", "digest", "sorted", "joined", "reduce", "compactMap", "flatMap", "contains", "first", "last",
    "forEach", "isEmpty", "slice", "splice", "concat", "includes", "some", "every", "trim", "toLowerCase",
    "to", "not_to", "to_not", "eq", "be", "it", "describe", "context", "expect", "allow", "receive", "and_return",
    "any?", "all?", "none?", "empty?", "present?", "blank?", "include?", "nil?", "key?", "to_s", "to_h", "to_a",
    "to_i", "to_sym", "as_json", "to_json", "id", "uuid", "call", "find", "where", "create!", "update!", "save!",
    "destroy", "order", "limit", "pluck", "exists?", "transaction", "perform_later", "deliver_later",
    "redirect_to", "params", "permit", "fetch", "dig", "merge", "tap", "then", "freeze", "dup", "lazy",
    "write_text", "read_text", "write_bytes", "read_bytes", "chmod", "resolve", "glob", "iterdir", "stat",
    "headers", "body", "email", "service", "text", "json", "data", "result", "response", "user", "account",
    "created_at", "updated_at", "compact", "all", "logger", "New", "String", "Error", "Close", "Run", "Start",
    "Get", "Set", "Len", "Write", "Read", "count", "sum", "min", "max", "keys", "each_with_object", "inject",
))
TEST_PATH = re.compile(r"(^|/)(spec|specs|test|tests|__tests__|e2e)/|(_spec|_test|\.spec|\.test)\.[a-z]+$|(^|/)test_[^/]+\.py$")


def is_test(path):
    return bool(TEST_PATH.search(path or ""))


# Languages whose names can meet: a bare name is never matched across these families.
FAMILY = {"python": "python", "ruby": "ruby", "js": "js", "swift": "apple", "objc": "apple", "go": "go",
          "rust": "rust", "markdown": None}


def _skipped_part(part):
    return part in SKIPPED_NAMES or part.startswith(".") or "backup" in part.lower() or part.endswith(".app")


# ---------------------------------------------------------------- extraction

def _module_name(relative):
    parts = list(Path(relative).parts)
    stem = Path(parts[-1]).stem if parts[-1].endswith(".py") else parts[-1]
    parts[-1] = stem
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(re.sub(r"\W", "_", part) for part in parts)


def _dotted(node):
    """`a.b.c` for a Name/Attribute chain, else None."""
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
        return ".".join(reversed(names))
    return None


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, module, package):
        self.module = module
        self.package = package
        self.nodes = [{"kind": "module", "name": module.rsplit(".", 1)[-1], "qualname": module, "line": 1}]
        self.refs = []
        self.scope = [module]
        self.classes = []  # (qualname, method names)
        self.aliases = {}
        self.defined = {}

    def prepare(self, tree):
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defined[statement.name] = self.module + "." + statement.name

    def _ref(self, kind, hint, line, target=None):
        target = target or (hint.rsplit(".", 1)[-1] if hint else None)
        if target:
            self.refs.append({"src": self.scope[-1], "kind": kind, "target": target, "hint": hint, "line": line})

    def _resolve(self, dotted):
        """The project-qualified name a dotted expression probably refers to, or None."""
        head, _, rest = dotted.partition(".")
        if head in ("self", "cls") and self.classes and rest and "." not in rest:
            qualname, methods = self.classes[-1]
            return qualname + "." + rest if rest in methods else None
        if head in self.aliases:
            return self.aliases[head] + ("." + rest if rest else "")
        if head in self.defined:
            return self.defined[head] + ("." + rest if rest else "")
        return None

    def _absolute(self, module, level):
        if not level:
            return module or ""
        base = self.package.split(".") if self.package else []
        if level > 1:
            base = base[:len(base) - (level - 1)]
        return ".".join(part for part in base + ([module] if module else []) if part)

    def visit_Import(self, node):
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            self._ref("imports", alias.name, node.lineno)

    def visit_ImportFrom(self, node):
        module = self._absolute(node.module, node.level)
        if module:
            self._ref("imports", module, node.lineno)
        for alias in node.names:
            if alias.name == "*":
                continue
            hint = (module + "." if module else "") + alias.name
            self.aliases[alias.asname or alias.name] = hint
            self._ref("imports", hint, node.lineno)

    def _define(self, node, kind):
        qualname = self.scope[-1] + "." + node.name
        self.nodes.append({"kind": kind, "name": node.name, "qualname": qualname, "line": node.lineno})
        return qualname

    def visit_ClassDef(self, node):
        qualname = self._define(node, "class")
        self.scope.append(qualname)
        for base in node.bases:
            dotted = _dotted(base)
            if dotted:
                self._ref("inherits", self._resolve(dotted), node.lineno, target=dotted.rsplit(".", 1)[-1])
        methods = {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.classes.append((qualname, methods))
        for item in node.body:
            self.visit(item)
        self.classes.pop()
        self.scope.pop()

    def _function(self, node):
        in_class = self.classes and self.scope[-1] == self.classes[-1][0]
        nested = any(item["qualname"] == self.scope[-1] and item["kind"] in ("function", "method", "local")
                     for item in self.nodes[-50:])
        qualname = self._define(node, "method" if in_class else "local" if nested else "function")
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.scope.append(qualname)
        for item in node.body:
            self.visit(item)
        self.scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node):
        dotted = _dotted(node.func)
        if dotted:
            self._ref("calls", self._resolve(dotted), node.lineno, target=dotted.rsplit(".", 1)[-1])
        elif isinstance(node.func, ast.Attribute):  # `Store().save()`, `items[0].close()`
            self._ref("calls", None, node.lineno, target=node.func.attr)
        # importlib loading by file name: `Path(__file__).with_name("omni_kb.py")`
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str) and \
                    re.fullmatch(r"[\w-]+\.py", argument.value):
                self._ref("imports", None, node.lineno, target=re.sub(r"\W", "_", argument.value[:-3]))
        self.generic_visit(node)


def python_extract(relative, text):
    module = _module_name(relative)
    package = module if relative.endswith("__init__.py") else module.rpartition(".")[0]
    visitor = _PythonVisitor(module, package)
    try:
        tree = ast.parse(text, filename=relative)
    except (SyntaxError, ValueError, RecursionError) as exc:
        line = getattr(exc, "lineno", None)
        return visitor.nodes, [], "syntax error" + (" at line %d" % line if line else "")
    visitor.prepare(tree)
    try:
        visitor.visit(tree)
    except RecursionError:
        return visitor.nodes, visitor.refs, "too deeply nested"
    return visitor.nodes, visitor.refs, None


# ---- other languages: line patterns, with brace depth (or `end`/indentation) for scope

KEYWORDS = frozenset((
    "if", "for", "while", "switch", "return", "catch", "func", "function", "def", "sizeof", "typeof", "elif",
    "else", "guard", "case", "when", "unless", "until", "do", "in", "and", "or", "not", "new", "delete", "throw",
    "await", "async", "yield", "super", "init", "fn", "match", "loop", "impl", "where", "as", "is", "let", "var",
    "const", "import", "require", "require_relative", "include", "extend", "puts", "print", "p", "raise",
    "defined", "lambda", "proc", "assert", "static_assert", "NSLog", "dispatch_async", "struct", "class",
    "enum", "self", "this", "Self", "String", "Int", "Bool", "Array", "Dictionary", "Set", "Error", "Promise",
    "Object", "Number", "Boolean", "Date", "Map", "Math", "JSON", "Optional", "Result", "Vec", "Box", "Some",
    "None", "Ok", "Err", "NSString", "NSArray", "NSDictionary", "NSNumber", "NSObject", "NSError", "YES", "NO",
))
STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n]){0,200}\'|`(?:\\.|[^`\\])*`')
CALL = re.compile(r"(?:\b(self|this)\s*(?:\.|->)\s*)?(?<![\w$])([A-Za-z_$][\w$]*[!?]?)\s*\(")
RUBY_SEND = re.compile(r"(?:\b(self)\.|(?<=[\w)\]])\.)([a-z_]\w*[!?]?)(?![\w(])")
# A bare identifier where a Ruby statement or argument starts: a method call without parentheses
# (or a local variable, which matches no project definition).
RUBY_BARE = re.compile(r"(?:^\s*|[=(,|&!]\s*|\b(?:return|if|unless|and|or|not)\s+)([a-z_]\w*[!?]?)(?=\s*(?:$|[)\],]|\s(?:if|unless)\b))")
OBJC_SEND = re.compile(r"\[\s*(self|super|[\w.]+|\[[^\]]*\])\s+([A-Za-z_]\w*)\s*[\]:]")
CONSTANT = re.compile(r"(?<![\w$.:])([A-Z][a-z][A-Za-z0-9_]+)\b")
CODE_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)|`([\w./@-]+/[\w.@-]+\.[A-Za-z]{1,6})`")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

LANGUAGES = {
    "ruby": {
        "scope": "indent",
        "comment": "#",
        "defs": [
            (re.compile(r"^\s*(class|module)\s+([A-Z][\w:]*)(?:\s*<\s*([A-Z][\w:]*))?"), "container"),
            (re.compile(r"^\s*def\s+(?:self\.)?([a-zA-Z_]\w*[!?=]?)"), "function"),
        ],
        "imports": [re.compile(r"\brequire_relative\s*\(?\s*['\"]([^'\"]+)['\"]"),
                    re.compile(r"\b(?:require|load)\s*\(?\s*['\"]([^'\"]+)['\"]")],
    },
    "js": {
        "scope": "brace",
        "comment": "//",
        "defs": [
            (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(class)\s+([A-Za-z_$][\w$]*)"
                        r"(?:<[^>]*>)?(?:\s+extends\s+([\w$.]+))?"), "container"),
            (re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(interface|enum)\s+([A-Za-z_$][\w$]*)"
                        r"(?:<[^>]*>)?(?:\s+extends\s+([\w$.]+))?"), "container"),
            (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
             "function"),
            (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
                        r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"), "function"),
            (re.compile(r"^\s*(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
                        r"\*?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\([^;]*$"), "member"),
        ],
        "imports": [re.compile(r"\bimport\s+(?:[^'\"]*?\bfrom\s+)?['\"]([^'\"]+)['\"]"),
                    re.compile(r"\b(?:require|import)\(\s*['\"]([^'\"]+)['\"]\s*\)"),
                    re.compile(r"^\s*export\s+[^'\"]*?\bfrom\s+['\"]([^'\"]+)['\"]")],
    },
    "swift": {
        "scope": "brace",
        "comment": "//",
        "defs": [
            (re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|fileprivate|internal|open|final|indirect|"
                        r"nonisolated)\s+)*(class|struct|enum|protocol|extension|actor)\s+([A-Za-z_]\w*)"
                        r"(?:<[^>]*>)?(?:\s*:\s*([^{]+))?"), "container"),
            (re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|fileprivate|internal|open|final|static|"
                        r"class|override|mutating|nonisolated|convenience|required)\s+)*func\s+([A-Za-z_]\w*)"),
             "function"),
        ],
        "imports": [],
    },
    "objc": {
        "scope": "brace",
        "comment": "//",
        "defs": [
            (re.compile(r"^\s*@(interface|implementation|protocol)\s+([A-Za-z_]\w*)(?:\s*\([^)]*\))?"
                        r"(?:\s*:\s*([A-Za-z_]\w*))?"), "container"),
            (re.compile(r"^\s*[-+]\s*\([^)]*\)\s*([A-Za-z_]\w*)"), "function"),
            (re.compile(r"^(?:static\s+|extern\s+|inline\s+|const\s+)*[A-Za-z_][\w \t*]*?[\s*]([A-Za-z_]\w*)\s*\("
                        r"[^;]*$"), "toplevel"),
        ],
        "imports": [re.compile(r'^\s*#\s*(?:import|include)\s+"([^"]+)"')],
    },
    "go": {
        "scope": "brace",
        "comment": "//",
        "defs": [
            (re.compile(r"^type\s+([A-Za-z_]\w*)\s+(struct|interface)\b"), "gotype"),
            (re.compile(r"^func\s+(?:\(\s*\w*\s*\*?([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*)?([A-Za-z_]\w*)"), "gofunc"),
        ],
        "imports": [],
    },
    "rust": {
        "scope": "brace",
        "comment": "//",
        "defs": [
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|union)\s+([A-Za-z_]\w*)"), "container"),
            (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:([\w:]+)(?:<[^>]*>)?\s+for\s+)?([\w:]+)"), "rustimpl"),
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"\w+\"\s+)?"
                        r"fn\s+([A-Za-z_]\w*)"), "function"),
        ],
        "imports": [re.compile(r"^\s*(?:pub\s+)?mod\s+([a-z_]\w*)\s*;")],
    },
}
CONTAINER_KINDS = {"class": "class", "module": "class", "interface": "class", "enum": "class", "struct": "class",
                   "protocol": "class", "extension": "class", "actor": "class", "implementation": "class",
                   "trait": "class", "union": "class"}


def _strip_line(line, comment, state):
    """The line without strings and comments; state carries an open block comment."""
    if state["block"]:
        end = line.find("*/")
        if end < 0:
            return ""
        line = line[end + 2:]
        state["block"] = False
    line = STRING.sub('""', line)
    if comment == "//":
        while True:
            start = line.find("/*")
            if start < 0:
                break
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                state["block"] = True
                break
            line = line[:start] + " " + line[end + 2:]
    position = line.find(comment)
    return line[:position] if position >= 0 else line


def _resolve_path(relative, value, extension):
    """A relative import (`./util`, `../lib/x.rb`) as a project path without leading `./`; others as written."""
    value = value.strip()
    for prefix in ("@/", "~/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    if not value.startswith("."):
        return value
    parts = relative.split("/")[:-1]
    for part in value.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    path = "/".join(parts)
    if extension and not os.path.splitext(path)[1]:
        path += extension
    return path


def pattern_extract(language, relative, text):
    spec = LANGUAGES[language]
    module = relative
    stem = os.path.splitext(relative.rsplit("/", 1)[-1])[0]
    nodes = [{"kind": "module", "name": stem, "qualname": module, "line": 1}]
    refs, seen_refs = [], set()
    scopes = []  # dicts: qualname, kind, container, open, entered, line
    depth, state = 0, {"block": False}

    def current(container_only=False):
        for scope in reversed(scopes):
            if not container_only or scope["container"]:
                return scope
        return None

    def ref(kind, target, hint, line, src=None):
        src = src or (current() or {"qualname": module})["qualname"]
        key = (src, kind, target, hint)
        if target and key not in seen_refs:
            seen_refs.add(key)
            refs.append({"src": src, "kind": kind, "target": target, "hint": hint, "line": line})

    for number, raw in enumerate(text.splitlines()[:40000], 1):
        line = _strip_line(raw, spec["comment"], state)
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        # Close scopes.
        if spec["scope"] == "indent":
            if re.match(r"end\b", stripped):
                while scopes and scopes[-1]["open"] >= indent:
                    closed = scopes.pop()
                    if closed["open"] == indent:
                        break
                continue
        if language == "objc" and stripped.startswith("@end"):
            while scopes and scopes[-1]["kind"] != "objc":
                scopes.pop()
            if scopes:
                scopes.pop()
            continue
        defined_here = set()
        for pattern, role in spec["defs"]:
            matched = pattern.match(line)
            if not matched:
                continue
            owner = current(container_only=True)
            if role in ("container", "gotype", "rustimpl"):
                if role == "container":
                    word, name, bases = matched.group(1), matched.group(2), matched.group(3) if matched.lastindex >= 3 else None
                elif role == "gotype":
                    name, word, bases = matched.group(1), "struct", None
                else:
                    trait, name = matched.group(1), matched.group(2)
                    word, bases = "impl", trait
                name = name.split("::")[-1]
                kind = "class"
                qualname = (owner["qualname"] + "." if owner and spec["scope"] == "indent" else "") + name
                if word not in ("extension", "impl"):
                    nodes.append({"kind": kind, "name": name, "qualname": qualname, "line": number})
                defined_here.add(name)
                for base in re.split(r"[,&]", bases or ""):
                    base = re.sub(r"<.*", "", base).strip().split(".")[-1].split("::")[-1]
                    if re.fullmatch(r"[A-Za-z_]\w*", base or ""):
                        ref("inherits", base, None, number, src=qualname)
                single = spec["scope"] == "indent" and re.search(r"\bend\s*$", stripped) and \
                    not stripped.startswith(("class", "module")) or stripped.endswith(";")
                if not single and role != "gotype" or (role == "gotype" and "{" in line):
                    scopes.append({"qualname": qualname, "kind": "objc" if language == "objc" else kind,
                                   "container": True, "open": indent if spec["scope"] == "indent" else depth,
                                   "entered": False, "line": number})
                break
            if role == "member" and not (owner and owner["kind"] == "class" and depth == owner["open"] + 1):
                continue
            if role == "toplevel" and (depth != 0 or scopes):
                continue
            if role == "toplevel" and stripped.endswith(";"):
                defined_here.add(matched.group(1))  # a prototype declares, it does not call
                continue
            if role == "gofunc":
                receiver, name = matched.group(1), matched.group(2)
                qualname = (receiver + "." if receiver else "") + name
                kind = "method" if receiver else "function"
            else:
                name = matched.group(1)
                if name in KEYWORDS:
                    continue
                kind = "method" if owner else "function"
                qualname = (owner["qualname"] + "." if owner else "") + name
            nodes.append({"kind": kind, "name": name, "qualname": qualname, "line": number})
            defined_here.add(name)
            bodiless = stripped.endswith(";") or (spec["scope"] == "indent" and
                                                  (re.search(r"\bend\s*$", stripped) or re.search(r"\)\s*=\s*\S", stripped)))
            if not bodiless:
                scopes.append({"qualname": qualname, "kind": kind, "container": False,
                               "open": indent if spec["scope"] == "indent" else depth, "entered": False,
                               "line": number})
            break
        for pattern in spec["imports"]:
            for matched in pattern.finditer(raw):
                value = matched.group(1)
                extension = {"ruby": ".rb", "rust": ".rs"}.get(language, "")
                if language == "rust":
                    value = "./" + value
                hint = _resolve_path(relative, value, extension)
                target = os.path.splitext(hint.rsplit("/", 1)[-1])[0]
                ref("imports", target, hint, number)
        owner = current(container_only=True)
        called = set()
        for matched in CALL.finditer(line):
            receiver, name = matched.group(1), matched.group(2)
            called.add(name)
            if name in KEYWORDS or name in defined_here:
                continue
            hint = owner["qualname"] + "." + name if receiver and owner else None
            ref("calls", name, hint, number)
        if language == "ruby":
            for matched in RUBY_BARE.finditer(line):
                name = matched.group(1)
                if name not in KEYWORDS and name not in defined_here and name not in ("end", "self", "nil", "true",
                                                                                         "false", "then", "begin"):
                    ref("mentions", name, None, number)  # resolved within its own file only
            for matched in RUBY_SEND.finditer(line):
                name = matched.group(2)
                if name not in KEYWORDS and name not in defined_here:
                    ref("calls", name, owner["qualname"] + "." + name if matched.group(1) and owner else None, number)
        if language == "objc":
            for matched in OBJC_SEND.finditer(line):
                receiver, name = matched.group(1), matched.group(2)
                if name not in KEYWORDS:
                    ref("calls", name, owner["qualname"] + "." + name if receiver == "self" and owner else None,
                        number)
        for matched in CONSTANT.finditer(line):
            name = matched.group(1)
            if name not in KEYWORDS and name not in defined_here and name not in called and \
                    not (owner and owner["qualname"].rsplit(".", 1)[-1] == name):
                ref("uses", name, None, number)
        # Track braces and close what they end.
        if spec["scope"] == "brace":
            depth += line.count("{") - line.count("}")
            depth = max(depth, 0)
            for scope in scopes:
                if depth > scope["open"]:
                    scope["entered"] = True
            while scopes:
                top = scopes[-1]
                if top["kind"] == "objc":
                    break
                if (top["entered"] and depth <= top["open"]) or (not top["entered"] and number - top["line"] > 4):
                    scopes.pop()
                else:
                    break
    return nodes, refs, None


def markdown_extract(relative, text):
    stem = os.path.splitext(relative.rsplit("/", 1)[-1])[0]
    nodes = [{"kind": "module", "name": stem, "qualname": relative, "line": 1}]
    refs, seen, fenced = [], set(), False
    for number, line in enumerate(text.splitlines()[:40000], 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        heading = None if fenced else HEADING.match(line)
        if heading:
            title = heading.group(2).strip()[:200]
            nodes.append({"kind": "section", "name": title, "qualname": relative + "#" + title, "line": number})
        for matched in CODE_LINK.finditer(line):
            value = matched.group(1) or matched.group(2)
            if "://" in value or value.startswith(("mailto:", "/")):
                continue
            suffix = os.path.splitext(value)[1]
            if suffix not in CODE_EXTENSIONS:
                continue
            # A link is relative to the document; a backticked path is relative to the project.
            hint = _resolve_path(relative, value, "") if matched.group(1) or value.startswith(".") else value
            target = os.path.splitext(hint.rsplit("/", 1)[-1])[0]
            if (target, hint) not in seen:
                seen.add((target, hint))
                refs.append({"src": relative, "kind": "references", "target": target, "hint": hint, "line": number})
    return nodes, refs, None


EXTENSIONS = {".py": "python", ".rb": "ruby", ".rake": "ruby", ".js": "js", ".jsx": "js", ".mjs": "js",
              ".cjs": "js", ".ts": "js", ".tsx": "js", ".swift": "swift", ".m": "objc", ".mm": "objc",
              ".h": "objc", ".go": "go", ".rs": "rust", ".md": "markdown"}
CODE_EXTENSIONS = frozenset(extension for extension, language in EXTENSIONS.items() if language != "markdown")
EXTRACTORS = {"python": python_extract, "markdown": markdown_extract}
for _language in LANGUAGES:
    EXTRACTORS[_language] = (lambda language: lambda relative, text: pattern_extract(language, relative, text))(_language)


def language_of(relative, opener=None):
    suffix = os.path.splitext(relative)[1]
    if suffix:
        return EXTENSIONS.get(suffix)
    # An extension-less script says what it is on its first line.
    if opener is None:
        return None
    try:
        head = opener()
    except OSError:
        return None
    if head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0]:
        return "python"
    return None


# ---------------------------------------------------------------- discovery

def _protected(path, home):
    for folder in PROTECTED_FOLDERS:
        base = str(Path(home) / folder)
        if path == base or path.startswith(base + "/"):
            return True
    return path == "/Volumes" or path.startswith("/Volumes/")


def _checkout(root):
    head = root / "@head"
    return head if head.is_dir() else root


def _walk_repositories(base, directory, depth, found):
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return
    names = {entry.name for entry in entries}
    if ".git" in names and depth > 0:
        found.append(Path(directory))
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or _skipped_part(entry.name):
            continue
        if "@head" in names and entry.name != "@head":
            continue
        if depth + 1 <= PROJECT_DEPTH:
            _walk_repositories(base, entry.path, depth + 1, found)


def default_roots(home):
    """[(project id, label, checkout path)]: forge's projects first, then other repositories."""
    home = Path(home)
    roots, seen = [], set()

    def add(identifier, label, path):
        try:
            checkout = _checkout(Path(path)).resolve()
        except OSError:
            return
        key = str(checkout)
        if key in seen or not checkout.is_dir() or _protected(key, home):
            return
        seen.add(key)
        roots.append((identifier, label, checkout))

    try:
        data = json.loads((home / FORGE_CACHE).read_text(encoding="utf-8"))
        projects = data.get("projects") if isinstance(data, dict) else None
    except (OSError, ValueError):
        projects = None
    for project in projects or []:
        if isinstance(project, dict) and isinstance(project.get("root"), str):
            root = project["root"]
            add(project.get("id") if isinstance(project.get("id"), str) else root,
                str(project.get("name") or Path(root).name), root)
    found = []
    if (home / "Projects").is_dir():
        _walk_repositories(home / "Projects", str(home / "Projects"), 0, found)
    for path in found:
        root = path.parent if path.name == "@head" else path
        add(str(root), root.name, root)
    return roots


_GIT = []


def git_binary():
    if not _GIT:
        found = next((path for path in ("/opt/homebrew/bin/git", "/usr/local/bin/git") if os.path.isfile(path)), None)
        if found is None and os.path.isfile("/usr/bin/git"):
            # /usr/bin/git is a shim that offers to install the developer tools when they are missing.
            try:
                ok = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=5,
                                    stdin=subprocess.DEVNULL).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            found = "/usr/bin/git" if ok else None
        _GIT.append(found)
    return _GIT[0]


def _settings(checkout):
    try:
        data = json.loads((checkout / ".omni" / "graph.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []

    def prefixes(key):
        values = data.get(key) if isinstance(data.get(key), list) else []
        return [value.strip("/") for value in values[:50] if isinstance(value, str) and value.strip("/") and
                ".." not in Path(value).parts and not value.startswith("/")]
    return prefixes("include"), prefixes("exclude")


def _walk_files(checkout, start, limit, skip=True):
    files, stack = [], [(checkout / start if start else checkout, 0)]
    while stack and len(files) < limit:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or entry.name.startswith(".") or (skip and _skipped_part(entry.name)):
                continue
            if entry.is_dir():
                if depth < WALK_DEPTH:
                    stack.append((Path(entry.path), depth + 1))
            elif entry.is_file():
                files.append(str(Path(entry.path).relative_to(checkout)))
    return files


def list_files(checkout, limit=MAX_PROJECT_FILES):
    """Relative paths of the project's candidate source files (any language), and whether git listed them."""
    include, exclude = _settings(checkout)
    git = git_binary() if (checkout / ".git").exists() else None
    paths = None
    if git:
        try:
            result = subprocess.run([git, "-C", str(checkout), "ls-files", "-z", "--cached", "--others",
                                     "--exclude-standard"], capture_output=True, timeout=GIT_TIMEOUT,
                                    stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                                                                   "GIT_OPTIONAL_LOCKS": "0"})
            if result.returncode == 0:
                paths = [path for path in result.stdout.decode("utf-8", "replace").split("\0") if path]
        except (OSError, subprocess.SubprocessError):
            paths = None
    if paths is None:
        paths = _walk_files(checkout, "", limit * 4)
    kept = [path for path in paths if not any(_skipped_part(part) for part in path.split("/")[:-1]) and
            not path.rsplit("/", 1)[-1].startswith(".")]
    for prefix in include:
        kept.extend(_walk_files(checkout, prefix, limit, skip=False))
    seen, result = set(), []
    for path in kept:
        if path in seen or SENSITIVE.search(path) or any(path == p or path.startswith(p + "/") for p in exclude):
            continue
        seen.add(path)
        result.append(path)
    return result, git is not None and paths is not None


# ---------------------------------------------------------------- store

# Rows store integer ids (a project path and a file path are not repeated on every reference); the views
# give queries the readable shape.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects(pid INTEGER PRIMARY KEY, project TEXT UNIQUE NOT NULL, label TEXT,
  root TEXT NOT NULL, listed_at REAL, stamp TEXT, files INTEGER DEFAULT 0, truncated INTEGER DEFAULT 0,
  git INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS file_rows(fid INTEGER PRIMARY KEY, pid INTEGER NOT NULL, path TEXT NOT NULL, lang TEXT,
  mtime INTEGER, size INTEGER, indexed_at REAL, error TEXT, UNIQUE(pid, path));
CREATE TABLE IF NOT EXISTS node_rows(id INTEGER PRIMARY KEY, fid INTEGER NOT NULL, kind TEXT NOT NULL,
  name TEXT NOT NULL, qualname TEXT NOT NULL, line INTEGER);
CREATE INDEX IF NOT EXISTS node_names ON node_rows(name);
CREATE INDEX IF NOT EXISTS node_files ON node_rows(fid);
CREATE TABLE IF NOT EXISTS ref_rows(fid INTEGER NOT NULL, src INTEGER NOT NULL, kind TEXT NOT NULL,
  target TEXT NOT NULL, hint TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS ref_targets ON ref_rows(target);
CREATE INDEX IF NOT EXISTS ref_files ON ref_rows(fid, src);
CREATE VIEW IF NOT EXISTS files AS SELECT f.fid, p.project, f.path, f.lang, f.mtime, f.size, f.indexed_at, f.error
  FROM file_rows f JOIN projects p ON p.pid=f.pid;
CREATE VIEW IF NOT EXISTS nodes AS SELECT n.id, p.project, f.path, n.kind, n.name, n.qualname, n.line, n.fid
  FROM node_rows n JOIN file_rows f ON f.fid=n.fid JOIN projects p ON p.pid=f.pid;
CREATE VIEW IF NOT EXISTS refs AS SELECT p.project, f.path, s.qualname AS src, r.kind, r.target, r.hint, r.line,
  r.fid, r.src AS src_id FROM ref_rows r JOIN file_rows f ON f.fid=r.fid JOIN projects p ON p.pid=f.pid
  JOIN node_rows s ON s.id=r.src;
"""


def _hint_matches(hint, qualname):
    if qualname == hint or qualname.endswith("." + hint):
        return True
    if "/" not in qualname and "/" not in hint:
        return False
    # A path module: `src/util` names `src/util.ts` and `src/util/index.ts`; `Foo.h` names `x/Foo.h`.
    bare = os.path.splitext(qualname)[0]
    for value in (qualname, bare, bare[:-len("/index")] if bare.endswith("/index") else None):
        if value and (value == hint or value.endswith("/" + hint)):
            return True
    return False


class CodeGraph:
    def __init__(self, path=":memory:", roots=None, home=None, clock=time.time, active=None):
        self.file = str(path)
        self.home = Path(home or Path.home())
        self._roots = roots
        self.clock = clock
        self._active = active  # callable -> workspaces with a recent session
        self._queue = deque()
        self._listing = None  # a project being listed on a background thread
        self._discovered_at = None
        self._hot = set()
        self._errors = deque(maxlen=10)
        self._stats = {"indexed": 0, "removed": 0, "errors": 0, "last_tick": None}
        self._open()

    # -- storage

    def _open(self):
        if self.file != ":memory:":
            Path(self.file).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.file, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.Error:
            version = None
        if version is not None and version[0] != str(SCHEMA_VERSION):
            self.db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(self.file + suffix)
                except OSError:
                    pass
            self.db = sqlite3.connect(self.file, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
        if self.file != ":memory:":
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()

    def close(self):
        self.db.close()

    # -- maintenance

    def _discover(self, now):
        roots = self._roots if self._roots is not None else default_roots(self.home)
        wanted = {}
        for item in roots:
            identifier, label, checkout = item if len(item) == 3 else (str(item[1]), item[0], item[1])
            try:
                checkout = Path(checkout).resolve()
            except OSError:
                continue
            wanted[str(identifier)] = (str(label), str(checkout))
        known = {row["project"]: row for row in self.db.execute("SELECT project, root FROM projects")}
        with self.db:
            for project in set(known) - set(wanted):
                pid = self.db.execute("SELECT pid FROM projects WHERE project=?", (project,)).fetchone()[0]
                for table in ("ref_rows", "node_rows"):
                    self.db.execute("DELETE FROM " + table + " WHERE fid IN (SELECT fid FROM file_rows WHERE pid=?)",
                                    (pid,))
                self.db.execute("DELETE FROM file_rows WHERE pid=?", (pid,))
                self.db.execute("DELETE FROM projects WHERE pid=?", (pid,))
            for project, (label, root) in wanted.items():
                if project not in known:
                    self.db.execute("INSERT INTO projects(project, label, root) VALUES(?,?,?)", (project, label, root))
                else:
                    self.db.execute("UPDATE projects SET label=?, root=? WHERE project=?", (label, root, project))
        self._queue = deque(item for item in self._queue if item[0] in wanted)
        self._discovered_at = now
        self._refresh_hot()

    def _refresh_hot(self):
        workspaces = set()
        if self._active is not None:
            try:
                workspaces = {str(path) for path in self._active() if path}
            except Exception:  # noqa: BLE001 - activity only orders the work
                workspaces = set()
        hot = set()
        for row in self.db.execute("SELECT project, root FROM projects"):
            root = row["root"]
            if any(path == root or path.startswith(root + "/") or root.startswith(path + "/@") for path in workspaces):
                hot.add(row["project"])
        self._hot = hot

    @staticmethod
    def _stamp(checkout):
        """Changes when a commit, checkout, pull or staging happens."""
        git = Path(checkout) / ".git"
        parts = []
        for path in (git / "HEAD", git / "index"):
            try:
                parts.append(str(path.stat().st_mtime_ns))
            except OSError:
                parts.append("-")
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: "):
                parts.append(str((git / head[5:]).stat().st_mtime_ns))
        except (OSError, UnicodeDecodeError):
            pass
        return ":".join(parts)

    def _due(self, now):
        """The project whose file list is most overdue, or None."""
        best = None
        for row in self.db.execute("SELECT project, root, listed_at, stamp FROM projects ORDER BY listed_at IS NOT NULL, listed_at"):
            if row["listed_at"] is None:
                return row["project"]
            interval = RELIST_HOT_SECONDS if row["project"] in self._hot else RELIST_COLD_SECONDS
            if now - row["listed_at"] >= interval:
                return row["project"]
            if best is None and (Path(row["root"]) / ".git").is_dir() and self._stamp(row["root"]) != row["stamp"]:
                best = row["project"]
        return best

    @staticmethod
    def scan_files(checkout):
        """The project's source files as {path: (language, mtime, size)}, with its stamp. No database work, so
        it runs off the worker's thread: listing a large repository takes most of a second."""
        checkout = Path(checkout)
        stamp = CodeGraph._stamp(checkout)
        try:
            paths, by_git = list_files(checkout) if checkout.is_dir() else ([], False)
            error = None if checkout.is_dir() else "folder missing"
        except OSError as exc:
            paths, by_git, error = [], False, str(exc)[:200]
        current = {}
        for relative in paths:
            full = checkout / relative

            def opener(full=full):
                with open(full, "rb") as handle:
                    return handle.read(128)
            language = language_of(relative, opener if "." not in relative.rsplit("/", 1)[-1] else None)
            if language is None:
                continue
            try:
                attributes = full.stat()
            except OSError:
                continue
            if attributes.st_size > MAX_FILE_BYTES or not os.path.isfile(full):
                continue
            current[relative] = (language, attributes.st_mtime_ns, attributes.st_size)
            if len(current) >= MAX_PROJECT_FILES:
                break
        return {"current": current, "stamp": stamp, "git": by_git, "error": error}

    def _start_listing(self, project):
        """List a project on a background thread; the tick or a query applies the result."""
        if self._listing is not None:
            return self._listing["project"] == project
        row = self.db.execute("SELECT root FROM projects WHERE project=?", (project,)).fetchone()
        if row is None:
            return False
        listing = {"project": project, "result": None, "started": self.clock()}

        def run(root=row["root"]):
            try:
                listing["result"] = self.scan_files(root)
            except Exception as exc:  # noqa: BLE001 - reported on the project, never raised into the worker
                listing["result"] = {"current": {}, "stamp": None, "git": False, "error": str(exc)[:200]}
        listing["thread"] = threading.Thread(target=run, name="omni-codegraph-list", daemon=True)
        self._listing = listing
        listing["thread"].start()
        return True

    def _finish_listing(self, wait=0.0):
        """Apply a finished listing; returns the project it was for, or None while it runs."""
        listing = self._listing
        if listing is None:
            return None
        listing["thread"].join(wait)
        if listing["thread"].is_alive():
            return None
        self._listing = None
        result = listing["result"]
        if result["stamp"] is None and result["error"] and not result["current"]:
            # A listing that failed outright keeps what was indexed and retries on the cold interval.
            with self.db:
                self.db.execute("UPDATE projects SET listed_at=?, error=? WHERE project=?",
                                (listing["started"], result["error"], listing["project"]))
            return listing["project"]
        self._apply(listing["project"], result, listing["started"])
        return listing["project"]

    def relist(self, project, now=None):
        """List a project now, on this thread, and queue the differences. Returns the count queued."""
        row = self.db.execute("SELECT root FROM projects WHERE project=?", (project,)).fetchone()
        if row is None:
            return 0
        return self._apply(project, self.scan_files(row["root"]), self.clock() if now is None else now)

    def _apply(self, project, result, now):
        current = result["current"]
        known = {item["path"]: item for item in self.db.execute(
            "SELECT path, mtime, size FROM files WHERE project=?", (project,))}
        queued = {(item[0], item[1]) for item in self._queue}
        added = 0
        for relative, (language, mtime, size) in sorted(current.items()):
            old = known.get(relative)
            if (old is None or old["mtime"] != mtime or old["size"] != size) and (project, relative) not in queued:
                self._queue.append((project, relative, language))
                added += 1
        for relative in known:
            if relative not in current and (project, relative) not in queued:
                self._queue.appendleft((project, relative, None))
                added += 1
        with self.db:
            self.db.execute("UPDATE projects SET listed_at=?, stamp=?, files=?, truncated=?, git=?, error=? WHERE project=?",
                            (now, result["stamp"], len(current), int(len(current) >= MAX_PROJECT_FILES),
                             int(result["git"]), result["error"], project))
        return added

    def _index(self, project, relative, language):
        """Replace one file's rows. Returns bytes read."""
        self.__dict__.pop("_unique_cache", None)
        self.__dict__.pop("_family_cache", None)
        row = self.db.execute("SELECT pid, root FROM projects WHERE project=?", (project,)).fetchone()
        if row is None:
            return 0
        pid, full = row["pid"], Path(row["root"]) / relative
        existing = self.db.execute("SELECT fid FROM file_rows WHERE pid=? AND path=?", (pid, relative)).fetchone()
        if existing is not None:
            for table in ("ref_rows", "node_rows"):
                self.db.execute("DELETE FROM " + table + " WHERE fid=?", (existing[0],))
        try:
            attributes = full.stat() if language is not None else None
            raw = full.read_bytes() if attributes and attributes.st_size <= MAX_FILE_BYTES else b""
        except OSError:
            attributes = None
        if attributes is None:
            if existing is not None:
                self.db.execute("DELETE FROM file_rows WHERE fid=?", (existing[0],))
            self._stats["removed"] += 1
            return 0
        nodes, refs, error = EXTRACTORS[language](relative, raw.decode("utf-8", "replace"))
        values = (language, attributes.st_mtime_ns, attributes.st_size, self.clock(), error)
        if existing is None:
            fid = self.db.execute("INSERT INTO file_rows(pid, path, lang, mtime, size, indexed_at, error) "
                                  "VALUES(?,?,?,?,?,?,?)", (pid, relative) + values).lastrowid
        else:
            fid = existing[0]
            self.db.execute("UPDATE file_rows SET lang=?, mtime=?, size=?, indexed_at=?, error=? WHERE fid=?",
                            values + (fid,))
        ids = {}
        for node in nodes:
            identifier = self.db.execute("INSERT INTO node_rows(fid, kind, name, qualname, line) VALUES(?,?,?,?,?)",
                                         (fid, node["kind"], node["name"], node["qualname"], node.get("line"))).lastrowid
            ids.setdefault(node["qualname"], identifier)
        module = ids.get(nodes[0]["qualname"]) if nodes else None
        rows, seen = [], set()
        for ref in refs:
            key = (ref["src"], ref["kind"], ref["target"], ref.get("hint"))
            if key in seen:
                continue  # one edge per source and target; the first line stands for the rest
            seen.add(key)
            rows.append((fid, ids.get(ref["src"], module), ref["kind"], ref["target"], ref.get("hint"), ref.get("line")))
        self.db.executemany("INSERT INTO ref_rows(fid, src, kind, target, hint, line) VALUES(?,?,?,?,?,?)", rows)
        self._stats["indexed"] += 1
        if error:
            self._stats["errors"] += 1
        return len(raw)

    def _drain(self, max_files, byte_budget, seconds, project=None):
        processed, consumed, started = 0, 0, time.monotonic()
        with self.db:
            while self._queue and processed < max_files and consumed < byte_budget and \
                    time.monotonic() - started < seconds:
                if project is not None:
                    index = next((i for i, item in enumerate(self._queue) if item[0] == project), None)
                    if index is None:
                        break
                    item = self._queue[index]
                    del self._queue[index]
                else:
                    item = self._queue.popleft()
                processed += 1
                try:
                    consumed += self._index(*item)
                except (OSError, ValueError, UnicodeDecodeError, MemoryError) as exc:
                    self._errors.append({"project": item[0], "path": item[1], "error": str(exc)[:200]})
        return processed, consumed

    def tick(self, max_files=FILES_PER_TICK, byte_budget=BYTES_PER_TICK, seconds=TICK_SECONDS):
        now = self.clock()
        started = time.monotonic()
        if self._discovered_at is None or now - self._discovered_at >= DISCOVERY_SECONDS:
            self._discover(now)
        listed = self._finish_listing()
        if listed is None and self._listing is None and not self._queue:
            due = self._due(now)
            if due is not None:
                self._start_listing(due)
        remaining = max(0.0, seconds - (time.monotonic() - started))
        processed, consumed = self._drain(max_files, byte_budget, remaining)
        self._stats["last_tick"] = now
        return {"processed": processed, "bytes": consumed, "pending": len(self._queue), "listed": listed,
                "listing": self._listing["project"] if self._listing else None}

    def pending(self):
        return self._discovered_at is None or bool(self._queue) or self._listing is not None

    def status(self):
        totals = self.db.execute("SELECT COUNT(*) AS projects, COALESCE(SUM(files), 0) AS files, "
                                 "SUM(listed_at IS NULL) AS unlisted FROM projects").fetchone()
        indexed = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return {"projects": totals["projects"], "files": totals["files"], "indexed_files": indexed,
                "unlisted_projects": totals["unlisted"] or 0, "pending": len(self._queue),
                "indexing": bool(self._queue) or bool(totals["unlisted"]), **self._stats,
                "recent_errors": list(self._errors)}

    # -- queries

    def _project(self, value):
        """A project id, its folder, or any path inside it, to the project id."""
        if not value:
            return None
        value = str(value).rstrip("/") or "/"
        rows = self.db.execute("SELECT project, root FROM projects").fetchall()
        for row in rows:
            if value == row["project"]:
                return row["project"]
        values = {value, os.path.realpath(value)} if value.startswith("/") else {value}
        best = None
        for row in rows:
            root = row["root"]
            parent = str(Path(root).parent) if Path(root).name == "@head" else root
            for base in {root, parent, row["project"]}:
                if any(item == base or item.startswith(base + "/") for item in values):
                    if best is None or len(base) > best[1]:
                        best = (row["project"], len(base))
        if best is None:
            raise ValueError("unknown project: " + value)
        return best[0]

    def refresh_project(self, project):
        """Bring one project's index up to date before answering about it, within a short budget."""
        row = self.db.execute("SELECT listed_at FROM projects WHERE project=?", (project,)).fetchone()
        if row is None:
            return
        self._hot.add(project)
        now = self.clock()
        started = time.monotonic()
        if row["listed_at"] is None or now - row["listed_at"] >= QUERY_RELIST_SECONDS:
            if self._listing is not None and self._listing["project"] != project:
                self._finish_listing(wait=QUERY_LIST_WAIT)  # another project's list: apply it if it is done
            if self._listing is None or self._listing["project"] == project:
                self._start_listing(project)
                self._finish_listing(wait=QUERY_LIST_WAIT)
        remaining = max(0.0, QUERY_REFRESH_SECONDS - (time.monotonic() - started))
        self._drain(FILES_PER_TICK, BYTES_PER_TICK * 4, remaining, project=project)

    @staticmethod
    def _node(row):
        return {"project": row["project"], "path": row["path"], "line": row["line"], "kind": row["kind"],
                "qualname": row["qualname"]}

    def _find(self, name, project=None, path=None, limit=20):
        """Candidate nodes for a name (bare, dotted suffix) or a file path."""
        params, where = [], []
        if project:
            where.append("project=?")
            params.append(project)
        if path or (name and ("/" in name or os.path.splitext(name)[1] in EXTENSIONS)):
            target = (path or name).lstrip("./")
            rows = self.db.execute("SELECT * FROM nodes WHERE kind='module'" + "".join(" AND " + w for w in where) +
                                   " AND (path=? OR path LIKE ?) ORDER BY length(path) LIMIT ?",
                                   params + [target, "%/" + target, limit]).fetchall()
            if path and name:
                bare = name.rsplit(".", 1)[-1]
                rows = self.db.execute("SELECT * FROM nodes WHERE name=?" + "".join(" AND " + w for w in where) +
                                       " AND (path=? OR path LIKE ?) LIMIT ?",
                                       [bare] + params + [target, "%/" + target, limit]).fetchall()
            return rows
        bare = name.rsplit(".", 1)[-1]
        rows = self.db.execute("SELECT * FROM nodes WHERE name=?" + "".join(" AND " + w for w in where) +
                               " LIMIT 500", [bare] + params).fetchall()
        if "." in name:
            rows = [row for row in rows if _hint_matches(name, row["qualname"])]
        order = {"class": 0, "function": 1, "method": 2, "module": 3, "section": 4, "local": 5}
        return sorted(rows, key=lambda row: (order.get(row["kind"], 9), row["path"], row["line"] or 0))[:limit]

    def _resolve(self, arguments, key="name"):
        name = arguments.get(key)
        path = arguments.get("path") if key == "name" else None
        if not name and not path:
            raise ValueError(key + " is required")
        rows = self._find(name or "", arguments.get("_project"), path)
        if not rows:
            return None, []
        if len(rows) > 1 and name and not path and "/" not in name:
            # A bare name means a definition before a file of that name.
            definitions = [row for row in rows if row["kind"] != "module"]
            rows = definitions or rows
        if len(rows) == 1:
            return rows[0], []
        exact = [row for row in rows if row["qualname"] == name]
        if len(exact) == 1:
            return exact[0], []
        if len({(row["project"], row["qualname"]) for row in rows}) == 1:
            # One definition declared in several files (a header and its implementation, Swift extensions):
            # the implementation answers.
            return sorted(rows, key=lambda row: (row["path"].endswith(".h"), row["path"]))[0], []
        return None, rows

    def _unique(self, project, name):
        """True when a bare name can only mean one definition in the project."""
        if name in COMMON_NAMES or name.startswith("__"):
            return False
        key = (project, name)
        cache = self.__dict__.setdefault("_unique_cache", {})
        if key not in cache:
            if len(cache) > 20000:
                cache.clear()
            cache[key] = self.db.execute("SELECT COUNT(DISTINCT qualname) FROM nodes WHERE project=? AND name=? AND "
                                         "kind NOT IN ('module', 'section', 'local')", key).fetchone()[0] == 1
        return cache[key]

    def _family(self, project, path):
        cache = self.__dict__.setdefault("_family_cache", {})
        key = (project, path)
        if key not in cache:
            if len(cache) > 50000:
                cache.clear()
            row = self.db.execute("SELECT lang FROM files WHERE project=? AND path=?", key).fetchone()
            cache[key] = FAMILY.get(row[0]) if row else None
        return cache[key]

    def _same_family(self, project, path, other):
        """Whether a bare name in `path` can mean a definition in `other`: same language family, and never
        application code reaching into a test file (a test helper's `name` is not what `user.name` calls)."""
        if is_test(other) and not is_test(path):
            return False
        family = self._family(project, path)
        return family is None or family == self._family(project, other)

    def _incoming(self, node, kinds=("calls", "imports", "inherits", "uses", "references")):
        """References that point at this node: (ref row, confidence). A bare Ruby mention and a nested
        function's callers count within their own file only."""
        found = []
        unique = node["kind"] == "module" or self._unique(node["project"], node["name"])
        kinds = tuple(kinds) + (("mentions",) if "calls" in kinds else ())
        marks = ",".join("?" * len(kinds))
        for ref in self.db.execute("SELECT * FROM refs WHERE project=? AND target=? AND kind IN (" + marks + ")",
                                   (node["project"], node["name"]) + tuple(kinds)):
            if ref["kind"] == "mentions" or node["kind"] == "local":
                if ref["path"] == node["path"] and not ref["hint"] and \
                        self.db.execute("SELECT COUNT(DISTINCT qualname) FROM nodes WHERE project=? AND path=? AND name=?",
                                        (node["project"], node["path"], node["name"])).fetchone()[0] == 1:
                    found.append((ref, "inferred"))
            elif ref["hint"]:
                if _hint_matches(ref["hint"], node["qualname"]):
                    found.append((ref, "extracted"))
            elif unique and (ref["kind"] not in ("imports", "references")) == (node["kind"] != "module") and \
                    self._same_family(node["project"], ref["path"], node["path"]):
                found.append((ref, "inferred"))
        return found

    def _outgoing(self, node, kinds=("calls",), per_ref=3):
        """Project nodes this node refers to: (ref row, target node row, confidence)."""
        found = []
        kinds = tuple(kinds) + (("mentions",) if "calls" in kinds else ())
        marks = ",".join("?" * len(kinds))
        if node["kind"] == "module":
            refs = self.db.execute("SELECT * FROM refs WHERE project=? AND path=? AND kind IN (" + marks + ")",
                                   (node["project"], node["path"]) + tuple(kinds)).fetchall()
        else:
            refs = self.db.execute("SELECT * FROM refs WHERE project=? AND path=? AND (src=? OR src LIKE ?) AND kind IN (" +
                                   marks + ")", (node["project"], node["path"], node["qualname"],
                                                 node["qualname"] + ".%") + tuple(kinds)).fetchall()
        for ref in refs:
            candidates = self.db.execute("SELECT * FROM nodes WHERE project=? AND name=? LIMIT 50",
                                         (node["project"], ref["target"])).fetchall()
            local = [row for row in candidates if row["path"] == ref["path"] and row["kind"] != "module"]
            if ref["kind"] == "mentions" or (local and all(row["kind"] == "local" for row in local)):
                if len({row["qualname"] for row in local}) == 1:
                    found.append((ref, local[0], "inferred"))
                continue
            candidates = [row for row in candidates if row["kind"] != "local"]
            if ref["hint"]:
                matched = [row for row in candidates if _hint_matches(ref["hint"], row["qualname"]) and
                           (ref["kind"] not in ("imports", "references") or row["kind"] == "module" or "/" not in ref["hint"])]
                found.extend((ref, row, "extracted") for row in matched[:per_ref])
            elif ref["kind"] in ("imports", "references"):
                found.extend((ref, row, "inferred") for row in candidates if row["kind"] == "module")
            elif len(candidates) == 1 and self._unique(node["project"], ref["target"]) and \
                    self._same_family(node["project"], ref["path"], candidates[0]["path"]):
                found.append((ref, candidates[0], "inferred"))
        return found

    def _edge(self, ref, confidence, node=None):
        value = {"path": ref["path"], "line": ref["line"], "from": ref["src"],
                 "kind": "calls" if ref["kind"] == "mentions" else ref["kind"],
                 "confidence": confidence}
        if node is not None:
            value["to"] = self._node(node)
        elif "src_id" in ref.keys():
            source = self.db.execute("SELECT * FROM nodes WHERE id=?", (ref["src_id"],)).fetchone()
            if source is not None:
                value["source"] = self._node(source)  # the definition the edge starts in
        return value

    @staticmethod
    def _sorted(edges):
        return sorted(edges, key=lambda edge: (CONFIDENCE_RANK[edge["confidence"]], edge["path"], edge["line"] or 0))

    def _ambiguous(self, candidates):
        return {"ambiguous": True, "candidates": [self._node(row) for row in candidates],
                "hint": "Pass a dotted name (module.Class.method), path, or project to choose one."}

    def explain(self, arguments, limit):
        node, candidates = self._resolve(arguments)
        if node is None:
            return self._ambiguous(candidates) if candidates else {"found": False}
        incoming = self._incoming(node)
        callers = self._sorted([self._edge(ref, c) for ref, c in incoming if ref["kind"] in ("calls", "mentions")])
        importers = self._sorted([self._edge(ref, c) for ref, c in incoming if ref["kind"] == "imports"])
        subclasses = self._sorted([self._edge(ref, c) for ref, c in incoming if ref["kind"] == "inherits"])
        used_by = self._sorted([self._edge(ref, c) for ref, c in incoming if ref["kind"] == "uses"])
        documents = self._sorted([self._edge(ref, c) for ref, c in incoming if ref["kind"] == "references"])
        callees, seen = [], set()
        for ref, target, confidence in self._outgoing(node):
            key = target["qualname"]  # a header and its implementation are one callee
            if key not in seen:
                seen.add(key)
                callees.append(self._edge(ref, confidence, target))
        callees = self._sorted(callees)
        if node["kind"] == "module" and node["qualname"] == node["path"]:
            # A path-named module (not Python): its top-level definitions, or a document's sections.
            members = [self._node(row) for row in self.db.execute(
                "SELECT * FROM nodes WHERE project=? AND path=? AND kind!='module' AND (kind='section' OR "
                "instr(qualname, '.')=0) ORDER BY line", (node["project"], node["path"]))]
        else:
            members = [self._node(row) for row in self.db.execute(
                "SELECT * FROM nodes WHERE project=? AND path=? AND qualname LIKE ? AND qualname NOT LIKE ? ORDER BY line",
                (node["project"], node["path"], node["qualname"] + ".%", node["qualname"] + ".%.%"))]
        file_row = self.db.execute("SELECT error FROM files WHERE project=? AND path=?",
                                   (node["project"], node["path"])).fetchone()
        result = {"node": self._node(node), "link": self.link(node),
                  "counts": {"callers": len(callers), "callees": len(callees),
                                                       "importers": len(importers), "subclasses": len(subclasses),
                                                       "members": len(members), "used_by": len(used_by),
                                                       "documented_in": len(documents)},
                  "callers": callers[:limit], "callees": callees[:limit], "importers": importers[:limit],
                  "subclasses": subclasses[:limit], "used_by": used_by[:limit], "documented_in": documents[:limit],
                  "members": members[:limit]}
        if file_row is not None and file_row["error"]:
            result["file_error"] = file_row["error"]
        if self.memory_lookup is not None:
            root = self.db.execute("SELECT root FROM projects WHERE project=?", (node["project"],)).fetchone()
            result["memories"] = self.memory_lookup(dict(self._node(node), name=node["name"],
                                                         root=root["root"] if root else node["project"]), limit)
        return result

    memory_lookup = None  # set by the knowledge store: (node row, limit) -> memories that mention it

    def _neighbours(self, node, reverse_only=False):
        """(neighbour node row, edge kind, direction, confidence). A dependency walk (reverse_only) skips
        documents, which mention code but do not break with it."""
        result = []
        kinds = ("calls", "imports", "inherits", "uses") if reverse_only else \
            ("calls", "imports", "inherits", "uses", "references")
        for ref, confidence in self._incoming(node, kinds):
            source = self.db.execute("SELECT * FROM nodes WHERE id=?", (ref["src_id"],)).fetchone()
            if source is not None:
                result.append((source, ref["kind"], "in", confidence))
        if not reverse_only:
            for ref, target, confidence in self._outgoing(node, kinds=("calls", "imports", "inherits", "uses",
                                                                       "references")):
                result.append((target, ref["kind"], "out", confidence))
        return result

    def locate(self, value, workspaces, code_only=False):
        """Indexed files a memory's file concept names: [(project, path)]. A relative value counts only in the
        projects of the memory's workspaces; an absolute or `~` value names its own project."""
        if value.startswith("~/"):
            value = str(self.home) + value[1:]
        rows = self.db.execute("SELECT project, root FROM projects").fetchall()
        found = []
        if value.startswith("/"):
            for row in rows:
                for base in (row["root"], row["project"]):
                    if value.startswith(base.rstrip("/") + "/"):
                        relative = value[len(base.rstrip("/")) + 1:]
                        if self.db.execute("SELECT 1 FROM files WHERE project=? AND path=?" +
                                           (" AND lang!='markdown'" if code_only else ""),
                                           (row["project"], relative)).fetchone():
                            found.append((row["project"], relative))
            return list(dict.fromkeys(found))
        projects = set()
        for workspace in workspaces or ():
            try:
                projects.add(self._project(workspace))
            except ValueError:
                continue
        for project in sorted(projects):
            for row in self.db.execute("SELECT path FROM files WHERE project=? AND (path=? OR path LIKE ?)" +
                                       (" AND lang!='markdown'" if code_only else "") + " LIMIT 3",
                                       (project, value, "%/" + value)):
                found.append((project, row["path"]))
        return found

    def neighbors(self, arguments, limit):
        node, candidates = self._resolve(arguments)
        if node is None:
            return self._ambiguous(candidates) if candidates else {"found": False}
        seen, items = set(), []
        for other, kind, direction, confidence in self._neighbours(node):
            key = (other["path"], other["qualname"], kind, direction)
            if key not in seen and other["id"] != node["id"]:
                seen.add(key)
                items.append({"node": self._node(other), "edge": kind, "direction": direction,
                              "confidence": confidence})
        items.sort(key=lambda item: (CONFIDENCE_RANK[item["confidence"]], item["direction"], item["node"]["path"]))
        return {"node": self._node(node), "total": len(items), "neighbors": items[:limit]}

    def affected(self, arguments, limit, depth=3, cap=400):
        node, candidates = self._resolve(arguments)
        if node is None:
            return self._ambiguous(candidates) if candidates else {"found": False}
        starts = [node]
        if node["kind"] == "module":
            starts = self.db.execute("SELECT * FROM nodes WHERE project=? AND path=?",
                                     (node["project"], node["path"])).fetchall()
        seen = {row["id"]: None for row in starts}
        frontier = [(row, "extracted") for row in starts]
        found = []
        for level in range(1, depth + 1):
            following = []
            for current, confidence in frontier:
                for other, kind, _, edge_confidence in self._neighbours(current, reverse_only=True):
                    if other["id"] in seen or len(seen) >= cap:
                        continue
                    weakest = max(confidence, edge_confidence, key=CONFIDENCE_RANK.get)
                    seen[other["id"]] = level
                    found.append({"node": self._node(other), "depth": level, "via": kind,
                                  "because_of": current["qualname"], "confidence": weakest})
                    following.append((other, weakest))
            frontier = following
            if not frontier:
                break
        found.sort(key=lambda item: (item["depth"], CONFIDENCE_RANK[item["confidence"]], item["node"]["path"]))
        files = sorted({item["node"]["path"] for item in found})
        return {"node": self._node(node), "total": len(found), "truncated": len(seen) >= cap, "files": files[:200],
                "affected": found[:limit]}

    def path(self, arguments, limit, max_depth=6, max_expanded=3000):
        start, candidates = self._resolve(arguments)
        if start is None:
            return self._ambiguous(candidates) if candidates else {"found": False, "missing": "name"}
        goal, candidates = self._resolve(arguments, key="target")
        if goal is None:
            return self._ambiguous(candidates) if candidates else {"found": False, "missing": "target"}
        if goal["project"] != start["project"]:
            return {"found": False, "reason": "the two names are in different projects"}
        previous = {start["id"]: None}
        rows = {start["id"]: start}
        queue, expanded = deque([(start, 0)]), 0
        while queue and expanded < max_expanded:
            current, level = queue.popleft()
            if current["id"] == goal["id"]:
                break
            if level >= max_depth:
                continue
            expanded += 1
            for other, kind, direction, confidence in self._neighbours(current):
                if other["id"] not in previous:
                    previous[other["id"]] = (current["id"], kind, direction, confidence)
                    rows[other["id"]] = other
                    queue.append((other, level + 1))
        if goal["id"] not in previous:
            return {"found": False, "from": self._node(start), "to": self._node(goal), "searched": expanded}
        hops, cursor = [], goal["id"]
        while previous[cursor] is not None:
            parent, kind, direction, confidence = previous[cursor]
            hops.append({"from": self._node(rows[parent]), "to": self._node(rows[cursor]), "edge": kind,
                         "direction": direction, "confidence": confidence})
            cursor = parent
        hops.reverse()
        weakest = max((hop["confidence"] for hop in hops), key=CONFIDENCE_RANK.get, default="extracted")
        return {"found": True, "length": len(hops), "confidence": weakest, "hops": hops[:limit]}

    def _forge_roots(self):
        """Roots of the projects forge lists: only those have a page in OmniTerm to link to."""
        cached = self.__dict__.get("_forge_roots_cache")
        if cached is not None and time.monotonic() - cached[0] < 60:
            return cached[1]
        try:
            data = json.loads((self.home / FORGE_CACHE).read_text(encoding="utf-8"))
            roots = {item["root"] for item in data.get("projects", []) if isinstance(item, dict) and
                     isinstance(item.get("root"), str)}
        except (OSError, ValueError, AttributeError, TypeError):
            roots = set()
        self._forge_roots_cache = (time.monotonic(), roots)
        return roots

    def link(self, node):
        """omniterm://project/<root>?code=<name>&file=<path>: the node on its project's Code tab, for a
        person to open; None when the project has no page."""
        project = node["project"]
        if project not in self._forge_roots():
            return None
        name = "" if node["kind"] == "module" else node["qualname"]
        return ("omniterm://project" + quote(project) + "?code=" + quote(name, safe="") + "&file=" +
                quote(node["path"], safe="/"))

    def query(self, arguments, limit):
        text = (arguments.get("name") or "").strip()
        if not text:
            raise ValueError("name is required")
        project = arguments.get("_project")
        where, params = ("project=? AND ", [project]) if project else ("", [])
        kind = arguments.get("kind")
        if kind is not None:
            if kind not in ("module", "class", "function", "method", "section", "local"):
                raise ValueError("kind must be module, class, function, method, section or local")
            where, params = where + "kind=? AND ", params + [kind]
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.db.execute("SELECT * FROM nodes WHERE " + where + "(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\') "
                               "LIMIT 2000", params + [pattern, pattern]).fetchall()
        lowered = text.lower()

        def rank(row):
            name = row["name"].lower()
            return (0 if name == lowered else 1 if name.startswith(lowered) else 2 if lowered in name else 3,
                    {"class": 0, "function": 1, "method": 2, "module": 3}.get(row["kind"], 9), len(row["qualname"]))
        rows = sorted(rows, key=rank)
        return {"total": len(rows), "results": [self._node(row) for row in rows[:limit]]}

    def stats(self, arguments, limit):
        project = arguments.get("_project")
        where, params = (" WHERE project=?", [project]) if project else ("", [])
        projects = []
        for row in self.db.execute("SELECT * FROM projects" + where + " ORDER BY project", params):
            counts = self.db.execute("SELECT COUNT(*) FROM nodes WHERE project=?", (row["project"],)).fetchone()[0]
            refs = self.db.execute("SELECT COUNT(*) FROM refs WHERE project=?", (row["project"],)).fetchone()[0]
            indexed = self.db.execute("SELECT COUNT(*), SUM(error IS NOT NULL) FROM files WHERE project=?",
                                      (row["project"],)).fetchone()
            languages = {item[0]: item[1] for item in self.db.execute(
                "SELECT lang, COUNT(*) FROM files WHERE project=? GROUP BY lang", (row["project"],))}
            projects.append({"project": row["project"], "label": row["label"], "root": row["root"],
                             "files": row["files"], "indexed_files": indexed[0], "files_with_errors": indexed[1] or 0,
                             "languages": languages, "nodes": counts, "references": refs,
                             "listed_at": row["listed_at"], "truncated": bool(row["truncated"]),
                             "pending": sum(1 for item in self._queue if item[0] == row["project"]),
                             "error": row["error"]})
        projects.sort(key=lambda item: -item["nodes"])
        errors = [dict(row) for row in self.db.execute(
            "SELECT project, path, error FROM files WHERE error IS NOT NULL" + (" AND project=?" if project else "") +
            " LIMIT ?", params + [min(limit, 20)])]
        return {"status": self.status(), "projects": projects[:max(limit, 1) if not project else 1],
                "file_errors": errors}

    def request(self, arguments):
        mode = arguments.get("mode")
        if mode not in MODES:
            raise ValueError("mode must be one of " + ", ".join(MODES))
        limit = arguments.get("limit", 30)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        for key in ("name", "target", "path", "project", "workspace", "kind"):
            value = arguments.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 1000):
                raise ValueError(key + " must be text, at most 1000 characters")
        arguments = dict(arguments)
        if self._discovered_at is None:
            self._discover(self.clock())
        project = self._project(arguments.get("project")) if arguments.get("project") else None
        if project is None and isinstance(arguments.get("workspace"), str):
            try:
                project = self._project(arguments["workspace"])
            except ValueError:
                project = None  # a workspace outside every project searches them all
        arguments["_project"] = project
        if project is not None:
            self.refresh_project(project)
        if mode == "overview":
            return self.overview(arguments, limit)
        result = getattr(self, mode)(arguments, limit)
        if project is not None:
            result.setdefault("project", project)
            pending = sum(1 for item in self._queue if item[0] == project)
            unlisted = self.db.execute("SELECT listed_at IS NULL FROM projects WHERE project=?", (project,)).fetchone()
            if pending or (unlisted and unlisted[0]):
                result["indexing"] = {"pending_files": pending, "partial": True}
        return result

    def overview(self, arguments, limit):
        """Hub definitions, clusters of files that depend on each other, and import cycles, for one project."""
        project = arguments.get("_project")
        if project is None:
            row = self.db.execute("SELECT project FROM files GROUP BY project ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
            if row is None:
                return {"found": False}
            project = row[0]
        version = self.db.execute("SELECT COUNT(*), MAX(indexed_at) FROM files WHERE project=?", (project,)).fetchone()
        cache = self.__dict__.setdefault("_overview_cache", {})
        cached = cache.get(project)
        if cached is None or cached[0] != tuple(version):
            cache[project] = (tuple(version), self._build_overview(project))
        result = dict(cache[project][1])
        result["hubs"] = result["hubs"][:limit]
        result["clusters"] = result["clusters"][:limit]
        result["project"] = project
        label = self.db.execute("SELECT label FROM projects WHERE project=?", (project,)).fetchone()
        result["markdown"] = overview_markdown(label[0] if label and label[0] else project, result)
        return result

    def _build_overview(self, project, max_refs=400000):
        definitions = {}  # bare name -> (qualnames, paths) for names that name one definition
        for row in self.db.execute("SELECT name, qualname, path, kind, line FROM nodes WHERE project=? AND "
                                   "kind NOT IN ('module', 'section', 'local')", (project,)):
            entry = definitions.setdefault(row["name"], [set(), set(), row])
            entry[0].add(row["qualname"])
            entry[1].add(row["path"])
        unique = {name: entry for name, entry in definitions.items()
                  if len(entry[0]) == 1 and name not in COMMON_NAMES and not name.startswith("__")}
        modules = {}
        for row in self.db.execute("SELECT name, qualname, path FROM nodes WHERE project=? AND kind='module'", (project,)):
            modules.setdefault(row["name"], []).append((row["qualname"], row["path"]))
        languages = {row[0]: FAMILY.get(row[1]) for row in self.db.execute(
            "SELECT path, lang FROM files WHERE project=?", (project,))}
        documents = {path for path, family in languages.items() if family is None}
        edges, imports, fan_in = {}, {}, {}
        for count, ref in enumerate(self.db.execute(
                "SELECT DISTINCT path, kind, target, hint FROM refs WHERE project=? AND kind IN "
                "('calls', 'imports', 'inherits', 'uses')", (project,))):
            if count >= max_refs:
                break
            source = ref["path"]
            targets = []
            if ref["kind"] == "imports":
                targets = [path for qualname, path in modules.get(ref["target"], ())
                           if ref["hint"] and _hint_matches(ref["hint"], qualname)]
            elif ref["target"] in unique:
                qualnames, paths, _ = unique[ref["target"]]
                hinted = bool(ref["hint"]) and _hint_matches(ref["hint"], next(iter(qualnames)))
                family = languages.get(source)
                if hinted or (not ref["hint"] and all(languages.get(path) == family and
                                                      (is_test(source) or not is_test(path)) for path in paths)):
                    targets = sorted(paths)
                    fan_in.setdefault(ref["target"], set()).add(source)
            for target in targets[:2]:
                if target == source:
                    continue
                key = tuple(sorted((source, target)))
                edges[key] = edges.get(key, 0) + 1
                if ref["kind"] == "imports":
                    imports.setdefault(source, set()).add(target)
        hubs = []
        for name, sources in sorted(fan_in.items(), key=lambda item: (-len(item[1]), item[0]))[:100]:
            _, _, row = unique[name]
            hubs.append({"qualname": row["qualname"], "kind": row["kind"], "path": row["path"], "line": row["line"],
                         "files_using": len(sources)})
        clusters = _label_clusters(edges)
        named = []
        for members in clusters:
            if len(members) < 2:
                continue
            outside = {}
            for (a, b), weight in edges.items():
                if (a in members) != (b in members):
                    other = b if a in members else a
                    outside[other] = outside.get(other, 0) + weight
            degree = {member: 0 for member in members}
            for (a, b), weight in edges.items():
                if a in members and b in members:
                    degree[a] += weight
                    degree[b] += weight
            top = sorted(members, key=lambda member: (-degree[member], member))
            named.append({"name": _cluster_name(members), "files": len(members), "top_files": top[:5],
                          "depends_on_files_outside": len(outside)})
        named.sort(key=lambda cluster: (-cluster["files"], cluster["name"]))
        # Two groups under one folder are told apart by their central file.
        counts = {}
        for cluster in named:
            counts[cluster["name"]] = counts.get(cluster["name"], 0) + 1
        for cluster in named:
            if counts[cluster["name"]] > 1:
                cluster["name"] += " · " + cluster["top_files"][0].rsplit("/", 1)[-1]
        cycles = [sorted(component) for component in _cycles(imports)][:20]
        total = self.db.execute("SELECT COUNT(*) FROM files WHERE project=?", (project,)).fetchone()[0]
        return {"files": total, "documents": len(documents), "file_links": len(edges), "hubs": hubs,
                "clusters": named, "import_cycles": cycles}


def _label_clusters(edges, rounds=20):
    """Communities of files by weighted label propagation, in a fixed order, so the same graph gives the
    same clusters."""
    neighbours = {}
    for (a, b), weight in edges.items():
        neighbours.setdefault(a, {})[b] = weight
        neighbours.setdefault(b, {})[a] = weight
    label = {node: node for node in neighbours}
    order = sorted(neighbours)
    for _ in range(rounds):
        changed = False
        for node in order:
            scores = {}
            for other, weight in neighbours[node].items():
                scores[label[other]] = scores.get(label[other], 0) + weight
            best = min(scores.items(), key=lambda item: (-item[1], item[0]))[0]
            if best != label[node] and scores[best] > scores.get(label[node], 0):
                label[node] = best
                changed = True
        if not changed:
            break
    groups = {}
    for node, value in label.items():
        groups.setdefault(value, set()).add(node)
    return sorted(groups.values(), key=lambda members: (-len(members), min(members)))


def _cluster_name(members):
    """The folder most members share, two levels deep at most."""
    counts = {}
    for member in members:
        parts = member.split("/")[:-1]
        for depth in (1, 2, 3):
            if len(parts) >= depth:
                prefix = "/".join(parts[:depth])
                counts[prefix] = counts.get(prefix, 0) + 1
    if not counts:
        return "(top level)"
    # The deepest prefix still covering most members.
    best = max(counts.items(), key=lambda item: (item[1] >= len(members) * 0.6, item[1] if item[1] < len(members) * 0.6
                                                 else item[0].count("/"), item[1]))
    return best[0] + "/"


def _cycles(graph):
    """Strongly connected components of two or more files (Tarjan, iterative)."""
    index, low, on_stack, stack, result, counter = {}, {}, set(), [], [], [0]
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(component)
    return sorted(result, key=lambda component: (-len(component), min(component)))


def overview_markdown(project, overview):
    lines = ["# Code map: " + project, "",
             "%d files (%d documents), %d links between files." % (overview["files"], overview["documents"],
                                                                     overview["file_links"]), ""]
    if overview["hubs"]:
        lines += ["## Hubs", "", "The definitions most files depend on.", ""]
        lines += ["- `%s` (%s) in `%s:%s`, used by %d files" % (hub["qualname"], hub["kind"], hub["path"], hub["line"],
                                                              hub["files_using"]) for hub in overview["hubs"]]
        lines.append("")
    if overview["clusters"]:
        lines += ["## Clusters", "", "Files that depend mostly on each other.", ""]
        for cluster in overview["clusters"]:
            lines.append("- **%s**: %d files, linked to %d files outside. Central: %s" % (
                cluster["name"], cluster["files"], cluster["depends_on_files_outside"],
                ", ".join("`%s`" % path for path in cluster["top_files"][:3])))
        lines.append("")
    if overview["import_cycles"]:
        lines += ["## Import cycles", ""]
        lines += ["- " + " ↔ ".join("`%s`" % path for path in cycle[:8]) + (" …" if len(cycle) > 8 else "")
                  for cycle in overview["import_cycles"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
