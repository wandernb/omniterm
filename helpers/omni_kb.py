"""Knowledge-base ingestion: every project's `knowledge/` folder becomes indexed, searchable
sections and one source-linked memory per file.

A knowledge base is a folder named `knowledge` found under the projects root (at most
`PROJECT_DEPTH` levels down), plus `~/.claude/knowledge` and any folders listed in
`EXTRA_ROOTS_FILE`. Each Markdown file is a source of provider `kb`; each heading section
is an indexed message (role `document`), so it is searchable, embedded, and citable as
memory evidence. Each file also yields
one memory (id `kb-<digest>`), revised when the file changes and deleted when the file
disappears.

Where a repository keeps worktrees side by side (`@head`, `@production`, feature
folders), only `@head` is read, so a knowledge base is mapped once.

File contents are untrusted data. Nothing here interprets them as instructions.
"""

import datetime as dt
import hashlib
import os
import json
import re
import time
from pathlib import Path


PROVIDER = "kb"
PROJECT_DEPTH = 4
KB_DEPTH = 6
MAX_KB_FILES = 4000
MAX_FILE_BYTES = 1024 * 1024
DISCOVERY_SECONDS = 600.0
FILES_PER_TICK = 25
TICK_SECONDS = 0.3
BYTES_PER_TICK = 512 * 1024
SECTION_CHARACTERS = 7600
MEMORY_BODY_CHARACTERS = 3000
LEAD_CHARACTERS = 900
MAX_EVIDENCE = 50
SKIPPED_NAMES = frozenset((
    "node_modules", ".git", ".hg", ".svn", ".attic", ".legacy", ".venv", "venv", "vendor", "Vendor",
    "dist", "build", "tmp", "log", "logs", "coverage", "__pycache__", ".next", ".cache", "public",
    "storage", "archives", "Library",
))
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def skipped(name):
    lowered = name.lower()
    return name in SKIPPED_NAMES or "backup" in lowered or lowered.endswith(".app")


EXTRA_ROOTS_FILE = "Library/Application Support/OmniTerm/knowledge-roots"


def extra_roots(home):
    """Read further shared knowledge folders, one `label=path` (or bare path) per line.

    The file lives at `EXTRA_ROOTS_FILE` under the home folder. Blank lines and lines
    starting with `#` are ignored; `~` expands to the home folder. A bare path is
    labelled with the name of the folder that holds `knowledge`.
    """
    home = Path(home)
    try:
        lines = (home / EXTRA_ROOTS_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    roots = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, separator, path = line.partition("=")
        if not separator:
            label, path = "", line
        label, path = label.strip(), path.strip()
        if not path:
            continue
        path = Path(str(home) + path[1:]) if path == "~" or path.startswith("~/") else Path(path)
        label = tag(label or (path.parent.name if path.name == "knowledge" else path.name))
        if label and label not in ("projects", "shared"):
            roots.append((label, path))
    return roots


def default_roots(home):
    home = Path(home)
    return [("projects", home / "Projects"), ("shared", home / ".claude/knowledge")] + extra_roots(home)


def discover(roots):
    """Return (knowledge_dir, project, workspace, label) for every knowledge base."""
    found = []
    for kind, root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        root = root.resolve()
        if not root.is_dir():
            continue
        if kind != "projects":
            label = "claude-shared" if kind == "shared" else kind
            found.append((root, label, str(root), label))
            continue
        _walk_projects(root, root, 0, found)
    return found


def _walk_projects(root, directory, depth, found):
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return
    names = {entry.name for entry in entries}
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or skipped(entry.name):
            continue
        # Side-by-side worktrees: read only the canonical checkout.
        if "@head" in names and entry.name != "@head":
            continue
        path = Path(entry.path)
        if entry.name == "knowledge":
            if depth == 0:
                continue
            relative = [part for part in path.parent.relative_to(root).parts if part != "@head"]
            project = relative[0]
            found.append((path, project, str(path.parent), "/".join(relative)))
            continue
        if depth + 1 < PROJECT_DEPTH:
            _walk_projects(root, path, depth + 1, found)


def markdown_files(knowledge_dir):
    files = []
    stack = [(Path(knowledge_dir), 0)]
    while stack and len(files) < MAX_KB_FILES:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or entry.name.startswith(".") or skipped(entry.name):
                continue
            if entry.is_dir() and depth < KB_DEPTH:
                stack.append((Path(entry.path), depth + 1))
            elif entry.is_file() and entry.name.endswith(".md"):
                files.append(Path(entry.path))
    return files


def sections(text):
    """Split Markdown into (line, heading, body) sections; headings inside code fences are text."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, min(len(lines), 200)):
            if lines[index].strip() == "---":
                start = index + 1
                break
    result, fenced = [], False
    current = {"line": start + 1, "heading": None, "level": 0, "lines": []}
    for index in range(start, len(lines)):
        line = lines[index]
        if FENCE.match(line):
            fenced = not fenced
        matched = None if fenced else HEADING.match(line)
        if matched:
            result.append(current)
            current = {"line": index + 1, "heading": matched.group(2).strip(), "level": len(matched.group(1)),
                       "lines": []}
        else:
            current["lines"].append(line)
    result.append(current)
    output = []
    for section in result:
        body = "\n".join(section["lines"]).strip()
        if section["heading"] is None and not body:
            continue
        output.append({"line": section["line"], "heading": section["heading"], "level": section["level"],
                       "body": body})
    return output


def frontmatter(text):
    lines = text.splitlines()
    values = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:200]:
            if line.strip() == "---":
                break
            key, separator, value = line.partition(":")
            if separator and key.strip() and not key.startswith(" "):
                values[key.strip().lower()] = value.strip().strip('"\'')
    return values


def memory_type(relative):
    lowered = relative.lower()
    if "wrapup" in lowered or "brainstorm" in lowered or "incident" in lowered or "postmortem" in lowered:
        return "episodic"
    if any(word in lowered for word in ("guideline", "convention", "pattern", "commands/", "runbook", "howto",
                                          "how-to", "playbook", "doctrine", "checklist")):
        return "procedural"
    if any(word in lowered for word in ("architecture", "index.md", "readme", "layout", "map", "structure",
                                          "deployment", "infrastructure")):
        return "spatial"
    return "declarative"


def tag(value):
    value = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")
    return value[:80]


class KnowledgeBases:
    def __init__(self, store, roots=None, clock=time.time):
        self.store = store
        self.roots = roots if roots is not None else default_roots(store.home)
        self.clock = clock
        self._files = None
        self._discovered_at = None
        self._queue = []
        self._stats = {"knowledge_bases": 0, "files": 0, "ingested": 0, "pending": 0, "removed": 0,
                       "discovered_at": None, "errors": 0}

    @property
    def connection(self):
        return self.store.connection

    def _source_key(self, path):
        return "source:kb:" + digest(str(path))

    def _rediscover(self, now):
        files = {}
        bases = discover(self.roots)
        for knowledge_dir, project, workspace, label in bases:
            for path in markdown_files(knowledge_dir):
                try:
                    attributes = path.stat()
                except OSError:
                    continue
                if attributes.st_size > MAX_FILE_BYTES:
                    continue
                relative = str(path.relative_to(knowledge_dir))
                files[str(path)] = {"path": path, "project": project, "workspace": workspace, "label": label,
                                    "relative": relative, "size": attributes.st_size,
                                    "mtime": attributes.st_mtime_ns}
        self._files = files
        self._discovered_at = now
        known = {row["path"]: row for row in self.connection.execute(
            "SELECT key,path,size,mtime FROM sources WHERE provider=?", (PROVIDER,)).fetchall()}
        queue = [item for path, item in sorted(files.items())
                 if path not in known or known[path]["size"] != item["size"] or known[path]["mtime"] != item["mtime"]]
        removed = [row["key"] for path, row in known.items() if path not in files]
        self._queue = [("remove", key) for key in removed] + [("ingest", item) for item in queue]
        self._stats.update(knowledge_bases=len(bases), files=len(files), discovered_at=now)

    def tick(self, max_files=FILES_PER_TICK, byte_budget=BYTES_PER_TICK):
        now = self.clock()
        if self._files is None or (not self._queue and now - self._discovered_at >= DISCOVERY_SECONDS):
            self._rediscover(now)
        processed, consumed, started = 0, 0, time.monotonic()
        while (self._queue and processed < max_files and consumed < byte_budget and
               time.monotonic() - started < TICK_SECONDS):
            action, item = self._queue.pop(0)
            processed += 1
            try:
                if action == "remove":
                    self.remove(item)
                    self._stats["removed"] += 1
                else:
                    consumed += self.ingest(item)
                    self._stats["ingested"] += 1
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                self._stats["errors"] += 1
                self.store._errors.append({"source_path": str(item if action == "remove" else item["path"]),
                                           "error": str(exc)[:300], "at": now})
        self._stats["pending"] = len(self._queue)
        self.connection.commit()
        return {"processed": processed, "bytes": consumed, "pending": len(self._queue)}

    def status(self):
        return dict(self._stats)

    def pending(self):
        return self._files is None or bool(self._queue)

    def remove(self, key):
        row = self.connection.execute("SELECT path FROM sources WHERE key=?", (key,)).fetchone()
        while not self.store._delete_source(key):
            pass
        if row is not None:
            self.store.delete_memory({"id": "kb-" + digest(row["path"])[:32]})

    def ingest(self, item):
        path = item["path"]
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        key = self._source_key(path)
        session_id = PROVIDER + ":" + item["label"] + "/" + item["relative"]
        parsed = sections(text)
        meta = frontmatter(text)
        title_heading = next((section["heading"] for section in parsed if section["level"] == 1), None)
        title = (meta.get("title") or title_heading or Path(item["relative"]).stem.replace("-", " ")).strip()
        modified = item["mtime"] / 1e9
        stamp = dt.datetime.fromtimestamp(modified, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        source = {"key": key, "session_id": session_id, "provider": PROVIDER, "path": str(path),
                  "workspace": item["workspace"], "started": modified, "updated": modified, "archived": 0,
                  "offset": item["size"], "line": len(text.splitlines()), "size": item["size"],
                  "mtime": item["mtime"], "inode": None, "anchor": digest(text), "title": title[:200],
                  "parent_session_id": None, "malformed": 0, "project": item["project"],
                  "knowledge_base": item["label"], "relative_path": item["relative"]}
        self.store._put_source(source)
        messages, occurrences = [], {}
        for section in parsed:
            heading = section["heading"] or ""
            occurrences[heading] = occurrences.get(heading, 0) + 1
            stem = key + "\0" + heading + "\0" + str(occurrences[heading])
            body = (("#" * section["level"] + " " + heading + "\n\n") if heading else "") + section["body"]
            body = body.strip()
            if not body:
                continue
            for part, offset in enumerate(range(0, len(body), SECTION_CHARACTERS)):
                messages.append({"id": "kb-" + digest(stem + "\0" + str(part))[:40], "source_key": key,
                                 "session_id": session_id, "provider": PROVIDER, "workspace": item["workspace"],
                                 "source_path": str(path), "source_line": section["line"], "part": part,
                                 "role": "document", "kind": "message",
                                 "text": body[offset:offset + SECTION_CHARACTERS], "timestamp": stamp,
                                 "heading": heading})
        wanted = {message["id"] for message in messages}
        for row in self.connection.execute("SELECT id FROM messages WHERE source_key=?", (key,)).fetchall():
            if row["id"] not in wanted:
                self.store._document("message:" + row["id"], None)
                self.connection.execute("DELETE FROM " + self.store.write_table("messages") + " WHERE id=?",
                                        (row["id"],))
        existing = {row["id"]: row["text"] for row in self.connection.execute(
            "SELECT id,text FROM messages WHERE source_key=?", (key,)).fetchall()}
        for message in messages:
            if existing.get(message["id"]) != message["text"]:
                self.store._put_message({name: value for name, value in message.items() if name != "heading"})
        if messages:
            self._remember(item, title, meta, parsed, messages, session_id)
        else:
            self.store.delete_memory({"id": "kb-" + digest(str(path))[:32]})
        return len(raw)

    def _remember(self, item, title, meta, parsed, messages, session_id):
        lead = next((section["body"] for section in parsed if section["body"]), "")
        lead = re.sub(r"\n{3,}", "\n\n", lead)[:LEAD_CHARACTERS].strip()
        outline = []
        for section in parsed:
            if section["heading"] and section["level"] >= 2:
                first = section["body"].split("\n\n", 1)[0].replace("\n", " ").strip()
                outline.append("- " + section["heading"] + (": " + first[:160] if first else ""))
        header = item["label"] + " knowledge · " + item["relative"]
        status = " · ".join(value for value in (meta.get("status"), meta.get("updated")) if value)
        body = header + ("\nStatus: " + status if status else "") + "\n\n" + lead
        if outline:
            body += "\n\nSections:\n" + "\n".join(outline)
        body = body[:MEMORY_BODY_CHARACTERS]
        relative_parts = Path(item["relative"]).parts
        tags = ["knowledge-base", tag(item["project"])]
        if item["label"] != item["project"]:
            tags.append(tag(item["label"]))
        if len(relative_parts) > 1:
            tags.append(tag(relative_parts[0]))
        evidence = []
        for message in messages:
            if message["part"] == 0:
                reference = {"session_id": session_id, "message_id": message["id"]}
                if reference not in evidence:
                    evidence.append(reference)
            if len(evidence) == MAX_EVIDENCE:
                break
        identifier = "kb-" + digest(str(item["path"]))[:32]
        existing = self.connection.execute("SELECT sources FROM memories WHERE id=?", (identifier,)).fetchone()
        # Evidence the consolidator added from transcripts (an UPDATE) outlives a
        # re-mapping of the file; only the file's own section references are replaced.
        for reference in json.loads(existing["sources"]) if existing else []:
            if len(evidence) >= 50:
                break
            if not str(reference.get("session_id", "")).startswith("kb:") and reference not in evidence:
                evidence.append(reference)
        arguments = {"id": identifier, "title": (item["project"] + " · " + title)[:200], "body": body,
                     "sources": evidence, "tags": list(dict.fromkeys(name for name in tags if name)),
                     "reason": "Mapped from knowledge base file " + item["relative"], "agent": "knowledge-base"}
        # The type is chosen once; a later retype by the owner is kept.
        if not self.connection.execute("SELECT 1 FROM memories WHERE id=?", (identifier,)).fetchone():
            arguments["memory_type"] = memory_type(item["relative"])
        self.store.upsert_memory(arguments)
