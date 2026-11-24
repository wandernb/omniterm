"""Weekly project summaries: the time spine above daily episodes (memo @2026-11-11-01 Idea 6c).

For each project (a folder under ~/Projects, the same key daily episodes use) and
ISO week, a deterministic candidate lists that week's episodes and the memories
created or revised in it, with the evidence a summary would cite. The background
memory agent writes the summary prose; `write_weekly_summary` rebuilds the
candidate itself, so the evidence and the cited memory IDs always come from the
store, never from the model. Writes are capped per day so the job never spends
more of the owner's provider quota than intended.

The summary is an episodic memory tagged `weekly-summary`, with id
`summary-<hash of project and week>`, and its document carries a `summary`
record (project, week, bounds, cited memory IDs, fingerprint of what it was
written from), so a newer memory in that week marks it stale.

Memory and transcript text are untrusted data. Nothing here interprets them as
instructions.
"""

import datetime as dt
import hashlib
import json
import re
import time

SUMMARY_TAG = "weekly-summary"
EPISODE_TAG = "episode"
WINDOW_WEEKS = 6  # complete weeks considered when nothing is named
CANDIDATES_PER_CALL = 2
CANDIDATES_MAXIMUM = 3
SUMMARIES_PER_DAY = 6
MIN_ITEMS = 2  # a week with fewer episodes plus memories is not worth a summary
MAX_EPISODES = 7
MAX_MEMORIES = 40
MAX_SOURCES = 50
MAX_CITES = 200
EXCERPT_CHARACTERS = 280
STATE_DOCUMENT = "metadata:weekly_summaries:v1"
WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
DAY_IN_TITLE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
PROJECT = re.compile(r"^[^/\x00]{1,200}$")


class SummaryError(ValueError):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def week_key(stamp):
    year, week, _ = dt.datetime.fromtimestamp(stamp).date().isocalendar()
    return "%04d-W%02d" % (year, week)


def week_bounds(key):
    """Local Monday 00:00 of the ISO week and of the next one, as timestamps."""
    match = WEEK.match(key) if isinstance(key, str) else None
    if not match:
        raise SummaryError("week must look like 2026-W45")
    try:
        monday = dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        raise SummaryError("week must look like 2026-W45")
    start = dt.datetime.combine(monday, dt.time())
    return start.timestamp(), (start + dt.timedelta(days=7)).timestamp()


def summary_id(project, week):
    return "summary-" + hashlib.sha256((project + "\0" + week).encode("utf-8")).hexdigest()[:32]


def project_tag(project):
    return re.sub(r"[^a-z0-9._-]+", "-", project.lower()).strip("-")[:80] or "project"


def excerpt(text, limit=EXCERPT_CHARACTERS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def is_episode(memory):
    return memory["id"].startswith("episode-") and EPISODE_TAG in (memory.get("tags") or [])


def is_summary(memory):
    return memory["id"].startswith("summary-") or SUMMARY_TAG in (memory.get("tags") or [])


def episode_day(memory):
    """An episode's day from its title (`project · Mon 2026-11-09 — n sessions`)."""
    match = DAY_IN_TITLE.search(memory.get("title") or "")
    if match:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(match.group(1)), dt.time()).timestamp() + 43200
        except ValueError:
            pass
    return memory.get("created")


def item_time(memory, start, end):
    """When a memory belongs to the week [start, end): an episode by its day, any
    other memory when it was created or last revised in it. None otherwise."""
    if is_episode(memory):
        day = episode_day(memory)
        return day if isinstance(day, (int, float)) and start <= day < end else None
    for stamp in (memory.get("created"), memory.get("updated")):
        if isinstance(stamp, (int, float)) and start <= stamp < end:
            return stamp
    return None


def fingerprint(items):
    return hashlib.sha256(json.dumps(sorted((item["id"], item.get("revision") or 0) for item in items))
                          .encode("utf-8")).hexdigest()[:24]


def spread_sources(items, limit=MAX_SOURCES):
    """Evidence for the summary, round-robin across the cited items so every day
    and memory is represented before any one contributes a second source."""
    queues = [list(item.get("sources") or []) for item in items]
    chosen, seen = [], set()
    while len(chosen) < limit and any(queues):
        for queue in queues:
            while queue:
                source = queue.pop(0)
                key = (source.get("session_id"), source.get("message_id"))
                if key in seen or None in key:
                    continue
                seen.add(key)
                chosen.append({"session_id": key[0], "message_id": key[1]})
                break
            if len(chosen) >= limit:
                break
    return chosen


class WeeklySummaries:
    def __init__(self, store, project_of, projects_root, clock=time.time):
        self.store = store
        self.project_of = project_of
        self.projects_root = projects_root
        self.clock = clock

    @property
    def connection(self):
        return self.store.connection

    @property
    def hippocampus(self):
        return self.store.hippocampus

    def _projects(self, memory):
        found = []
        for workspace in memory.get("workspaces") or []:
            project = self.project_of(workspace, self.projects_root)
            if project and project not in found:
                found.append(project)
        if not found and is_episode(memory):
            name = (memory.get("title") or "").split(" · ")[0].strip()
            if name:
                found.append(name)
        return found

    def _memories(self, now):
        """Non-archived, non-summary memories, each with its projects."""
        return [(memory, self._projects(memory)) for memory in self.hippocampus._all(now)
                if not memory["archived"] and not is_summary(memory)]

    def _stored_summary(self, identifier):
        stored = self.store._document_value("memory:" + identifier) or {}
        summary = stored.get("summary")
        exists = self.connection.execute("SELECT 1 FROM memories WHERE id=?", (identifier,)).fetchone() is not None
        return (summary if isinstance(summary, dict) else {}) if exists else None

    def _source_ok(self, source):
        return self.connection.execute("SELECT 1 FROM messages WHERE id=? AND session_id=?",
                                       (source["message_id"], source["session_id"])).fetchone() is not None

    def build(self, project, week, now=None, memories=None):
        """The candidate for one project and week, or None when the week is too quiet."""
        now = self.clock() if now is None else now
        start, end = week_bounds(week)
        memories = self._memories(now) if memories is None else memories
        episodes, others = [], []
        for memory, projects in memories:
            if project not in projects:
                continue
            stamp = item_time(memory, start, end)
            if stamp is None:
                continue
            (episodes if is_episode(memory) else others).append((stamp, memory))
        if len(episodes) + len(others) < MIN_ITEMS:
            return None
        episodes.sort(key=lambda item: (item[0], item[1]["id"]))
        others.sort(key=lambda item: (-item[1]["effective_weight"], item[1]["id"]))
        episodes = [memory for _, memory in episodes][:MAX_EPISODES]
        others = [memory for _, memory in others][:MAX_MEMORIES]
        cited = episodes + others
        sources = [source for source in spread_sources(cited, MAX_SOURCES * 2) if self._source_ok(source)][:MAX_SOURCES]
        identifier = summary_id(project, week)
        existing = self._stored_summary(identifier)
        current = fingerprint(cited)
        return {"id": identifier, "project": project, "week": week, "start": start, "end": end,
                "complete": end <= now,
                "episodes": [{"id": memory["id"], "title": memory.get("title", ""),
                              "body": excerpt(memory.get("body"), 1600)} for memory in episodes],
                "memories": [{"id": memory["id"], "title": memory.get("title", ""),
                              "memory_type": memory["memory_type"], "excerpt": excerpt(memory.get("body")),
                              "effective_weight": memory["effective_weight"]} for memory in others],
                "cites": [memory["id"] for memory in cited][:MAX_CITES],
                "sources": sources, "fingerprint": current,
                "existing": None if existing is None else {"fingerprint": existing.get("fingerprint"),
                                                           "written_at": existing.get("written_at")},
                "stale": existing is not None and existing.get("fingerprint") != current}

    def _daily(self, now):
        state = self.store._document_value(STATE_DOCUMENT) or {}
        today = dt.datetime.fromtimestamp(now).date().isoformat()
        written = state.get("written") if state.get("day") == today else 0
        return state, today, written if isinstance(written, int) else 0

    def candidates(self, arguments, now=None):
        now = self.clock() if now is None else now
        limit = arguments.get("limit", CANDIDATES_PER_CALL)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= CANDIDATES_MAXIMUM:
            raise SummaryError("limit must be an integer from 1 to %d" % CANDIDATES_MAXIMUM)
        project, week = arguments.get("project"), arguments.get("week")
        if project is not None and (not isinstance(project, str) or not PROJECT.match(project)):
            raise SummaryError("project must be a project folder name")
        if week is not None:
            week_bounds(week)
        _, _, written = self._daily(now)
        daily = {"written_today": written, "cap": SUMMARIES_PER_DAY,
                 "remaining_today": max(0, SUMMARIES_PER_DAY - written)}
        memories = self._memories(now)
        if project and week:
            candidate = self.build(project, week, now, memories)
            return {"candidates": [candidate] if candidate else [], "remaining": 0, "daily": daily}
        # Every complete week in the window with enough activity, per project.
        current_start, _ = week_bounds(week_key(now))
        weeks = [week] if week else []
        if not weeks:
            day = dt.datetime.fromtimestamp(current_start).date()
            for back in range(1, WINDOW_WEEKS + 1):
                year, number, _ = (day - dt.timedelta(days=7 * back)).isocalendar()
                weeks.append("%04d-W%02d" % (year, number))
        counts = {}
        bounds = [(candidate_week, week_bounds(candidate_week)) for candidate_week in weeks]
        for memory, projects in memories:
            for candidate_week, (start, end) in bounds:
                if not projects or item_time(memory, start, end) is None:
                    continue
                for name in projects:
                    if project is None or name == project:
                        counts[(name, candidate_week)] = counts.get((name, candidate_week), 0) + 1
        due = []
        for (name, candidate_week), count in sorted(counts.items(), key=lambda item: (item[0][1], item[1]), reverse=True):
            if count < MIN_ITEMS:
                continue
            candidate = self.build(name, candidate_week, now, memories)
            if candidate and (candidate["existing"] is None or candidate["stale"]):
                due.append(candidate)
        chosen = due[:min(limit, daily["remaining_today"])]
        return {"candidates": chosen, "remaining": len(due) - len(chosen), "daily": daily}

    def write(self, arguments, now=None):
        now = self.clock() if now is None else now
        project, week = arguments.get("project"), arguments.get("week")
        if not isinstance(project, str) or not PROJECT.match(project):
            raise SummaryError("project must be a project folder name")
        week_bounds(week)
        title, body = arguments.get("title"), arguments.get("body")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise SummaryError("title must be nonempty text, at most 200 characters")
        if not isinstance(body, str) or not body.strip() or len(body) > 8000:
            raise SummaryError("body must be nonempty text, at most 8000 characters")
        state, today, written = self._daily(now)
        if written >= SUMMARIES_PER_DAY:
            raise SummaryError("The daily cap of %d weekly summaries is reached" % SUMMARIES_PER_DAY, "daily_cap")
        candidate = self.build(project, week, now)
        if candidate is None:
            raise SummaryError("That project and week have too little memory to summarise", "nothing_to_summarize")
        if not candidate["sources"]:
            raise SummaryError("That week's memories cite no indexed messages", "source_not_found")
        upsert = {"id": candidate["id"], "title": title, "body": body, "sources": candidate["sources"],
                  "tags": [SUMMARY_TAG, project_tag(project), week],
                  "reason": "Weekly summary of %d episodes and %d memories" % (len(candidate["episodes"]),
                                                                                len(candidate["memories"])),
                  "run_id": arguments.get("run_id"), "agent": arguments.get("agent")}
        if candidate["existing"] is None:
            upsert["memory_type"] = "episodic"
        result = self.store.upsert_memory({key: value for key, value in upsert.items() if value is not None})
        self.hippocampus._rewrite(candidate["id"], {"summary": {
            "project": project, "week": week, "start": candidate["start"], "end": candidate["end"],
            "cites": candidate["cites"], "fingerprint": candidate["fingerprint"], "written_at": now}})
        self.store._document(STATE_DOCUMENT, dict(state, day=today, written=written + 1))
        self.connection.commit()
        return {"memory": result["memory"], "changed": result["changed"], "cites": candidate["cites"],
                "daily": {"written_today": written + 1, "cap": SUMMARIES_PER_DAY}}
