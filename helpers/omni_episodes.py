"""Session episodes: one episodic memory per project and day, built from transcripts without a model.

Each memory lists the day's top-level sessions in one project: what was asked (the first
meaningful user turn, or the slash command and its arguments) and how it ended (the last
assistant message). Evidence is those two records per session, so every line links back
to its transcript. A bucket is recomputed only when its sessions change, at most every
`REFRESH_SECONDS`; episodic memories decay, so old days fade on their own.

Transcript text is untrusted data. Nothing here interprets it as instructions.
"""

import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path


WINDOW_DAYS = 45
BUCKETS_PER_TICK = 12
# The tick runs on the serialized knowledge queue; stop early so requests never wait long.
TICK_SECONDS = 0.3
REFRESH_SECONDS = 1800.0
DISCOVERY_SECONDS = 300.0
MAX_SESSIONS = 40
MAX_EVIDENCE = 50
BODY_CHARACTERS = 6000
REQUEST_CHARACTERS = 180
OUTCOME_CHARACTERS = 220
STATE_PREFIX = "metadata:episode:"
COMMAND = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
MARKUP = re.compile(r"<[^>]{1,80}>")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_of(workspace, projects_root):
    if not workspace:
        return None
    path = Path(workspace)
    try:
        parts = [part for part in path.relative_to(projects_root).parts if part != "@head"]
    except ValueError:
        return None
    return parts[0] if parts else None


def one_line(text, limit):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def request_text(text):
    """The person's request: a slash command with its arguments, or plain prose."""
    command = COMMAND.search(text)
    if command:
        arguments = COMMAND_ARGS.search(text)
        return command.group(1) + (" " + arguments.group(1) if arguments and arguments.group(1) else "")
    stripped = text.strip()
    if stripped.startswith("<") or len(stripped) < 12:
        return None
    return MARKUP.sub("", stripped)


class SessionEpisodes:
    def __init__(self, store, projects_root=None, clock=time.time):
        self.store = store
        self.projects_root = Path(projects_root or Path(store.home) / "Projects")
        try:
            self.projects_root = self.projects_root.resolve()
        except OSError:
            pass
        self.clock = clock
        self._queue = []
        self._discovered_at = None
        self._stats = {"buckets": 0, "pending": 0, "written": 0, "discovered_at": None}

    @property
    def connection(self):
        return self.store.connection

    def _buckets(self, now):
        since = now - WINDOW_DAYS * 86400
        rows = self.connection.execute(
            "SELECT session_id,MAX(workspace) workspace,MIN(COALESCE(started,updated)) started,MAX(updated) updated,"
            "MAX(title) title FROM sources WHERE updated>? AND parent_session_id IS NULL AND provider IN ('claude','codex') "
            "GROUP BY session_id", (since,)).fetchall()
        excluded = self.store._memory_sessions
        buckets = {}
        for row in rows:
            if row["session_id"] in excluded or row["started"] is None:
                continue
            project = project_of(row["workspace"], self.projects_root)
            if not project:
                continue
            day = dt.datetime.fromtimestamp(row["started"]).date().isoformat()
            buckets.setdefault((project, day), []).append(dict(row))
        return buckets

    def _rediscover(self, now):
        buckets = self._buckets(now)
        queue = []
        for (project, day), sessions in sorted(buckets.items(), key=lambda item: item[0][1], reverse=True):
            fingerprint = digest(json.dumps(sorted((item["session_id"], item["updated"]) for item in sessions)))
            identifier = "episode-" + digest(project + "\0" + day)[:32]
            state = self.store._document_value(STATE_PREFIX + identifier) or {}
            if state.get("fingerprint") == fingerprint:
                continue
            if state.get("computed_at") and now - state["computed_at"] < REFRESH_SECONDS:
                continue
            queue.append((identifier, project, day, sessions, fingerprint))
        self._queue = queue
        self._discovered_at = now
        self._stats.update(buckets=len(buckets), discovered_at=now)

    def pending(self):
        return bool(self._queue)

    def status(self):
        return dict(self._stats, pending=len(self._queue))

    def tick(self, max_buckets=BUCKETS_PER_TICK):
        now = self.clock()
        if not self._queue and (self._discovered_at is None or now - self._discovered_at >= DISCOVERY_SECONDS):
            self._rediscover(now)
        written, started = 0, time.monotonic()
        while self._queue and written < max_buckets and time.monotonic() - started < TICK_SECONDS:
            identifier, project, day, sessions, fingerprint = self._queue.pop(0)
            if self._write(identifier, project, day, sessions, now):
                written += 1
            self.store._document(STATE_PREFIX + identifier, {"fingerprint": fingerprint, "computed_at": now})
        self._stats["written"] += written
        self._stats["pending"] = len(self._queue)
        self.connection.commit()
        return {"written": written, "pending": len(self._queue)}

    def _first_request(self, session_id):
        rows = self.connection.execute(
            "SELECT id,text FROM messages WHERE session_id=? AND role='user' AND kind='message' "
            "ORDER BY source_line,part,id LIMIT 8", (session_id,)).fetchall()
        for row in rows:
            text = request_text(row["text"])
            if text:
                return row["id"], text
        return None, None

    def _outcome(self, session_id):
        rows = self.connection.execute(
            "SELECT id,text FROM messages WHERE session_id=? AND role='assistant' AND kind='message' "
            "AND length(trim(text))>0 ORDER BY source_line DESC,part DESC,id DESC LIMIT 4", (session_id,)).fetchall()
        for row in rows:
            text = MARKUP.sub("", row["text"]).strip()
            if len(text) >= 8:
                return row["id"], text
        return None, None

    def _write(self, identifier, project, day, sessions, now):
        sessions = sorted(sessions, key=lambda item: item["started"])[:MAX_SESSIONS]
        lines, evidence = [], []
        for session in sessions:
            request_id, request = self._first_request(session["session_id"])
            if not request_id:
                continue
            outcome_id, outcome = self._outcome(session["session_id"])
            # A title derived from raw transcript text may carry markup; use it only when clean.
            title = request_text(session["title"]) if session["title"] else None
            line = "- " + one_line(title or request, REQUEST_CHARACTERS)
            if title and request != title:
                line += " (asked: " + one_line(request, REQUEST_CHARACTERS) + ")"
            if outcome:
                line += " → " + one_line(outcome, OUTCOME_CHARACTERS)
            lines.append(line)
            for message_id in (request_id, outcome_id):
                if message_id and len(evidence) < MAX_EVIDENCE:
                    evidence.append({"session_id": session["session_id"], "message_id": message_id})
        if not evidence:
            return False
        weekday = dt.date.fromisoformat(day).strftime("%a")
        title = project + " · " + weekday + " " + day + " — " + str(len(lines)) + " session" + ("s" if len(lines) != 1 else "")
        body = ("What happened in " + project + " on " + day + ", one line per session (request → how it ended):\n\n" +
                "\n".join(lines))[:BODY_CHARACTERS]
        arguments = {"id": identifier, "title": title, "body": body, "sources": evidence,
                     "tags": ["episode", re.sub(r"[^a-z0-9._-]+", "-", project.lower()).strip("-")[:80]],
                     "reason": "Daily episode built from " + str(len(lines)) + " transcripts", "agent": "session-episodes"}
        if not self.connection.execute("SELECT 1 FROM memories WHERE id=?", (identifier,)).fetchone():
            arguments["memory_type"] = "episodic"
        self.store.upsert_memory(arguments)
        return True
