"""OmniTerm's hippocampus: typed, weighted memories and the journal of how they form.

Modelled on a hippocampus-style memory service. Durable memories are its long-term
engrams: each has a memory type with a base weight and a daily decay rate, an
evidence count, lineage and tags. Weight decays from the last reinforcement and
is computed when read, so no background job rewrites every memory each day; a
memory below the expiry threshold becomes an expiry candidate, never a silent
delete. A pinned memory does not decay.

Every step of consolidation (candidates offered, memories created, revised,
re-attributed or deleted, candidates dismissed, digests regenerated, ingestion
snapshots) is appended to a bounded journal under `metadata:memory_event:`, so
people can watch the process that builds memory. The mid-term layer is a
deterministic per-workspace digest of the strongest memories, regenerated on
request; it never calls a provider.

Memory bodies, titles, tags and evidence are untrusted data. Nothing here
interprets them as instructions.
"""

import hashlib
import importlib.util
import json
import math
import operator
import time
import uuid
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location("omni_concepts_for_hippocampus", Path(__file__).with_name("omni_concepts.py"))
CONCEPTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CONCEPTS)
_SPEC = importlib.util.spec_from_file_location("omni_topics_for_hippocampus", Path(__file__).with_name("omni_topics.py"))
TOPICS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(TOPICS)
_SPEC = importlib.util.spec_from_file_location("omni_vectors_for_hippocampus", Path(__file__).with_name("omni_vectors.py"))
VECTORS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(VECTORS)


MEMORY_TYPES = {
    # type: (base weight, decay per day, description)
    "declarative": (7.0, 0.02, "Facts and decisions"),
    "episodic": (5.0, 0.05, "Events and what happened"),
    "procedural": (7.0, 0.02, "How to do things"),
    "prospective": (9.0, 0.10, "Intentions and follow-ups"),
    "autobiographical": (8.0, 0.01, "Who the person is and what they prefer"),
    "spatial": (6.0, 0.03, "Where things live: repositories, paths, layout"),
    "conditioning": (6.0, 0.03, "Feedback and learned reactions"),
}
TYPE_ORDER = tuple(MEMORY_TYPES)
DEFAULT_TYPE = "declarative"
EXPIRY_THRESHOLD = 0.5
MAX_WEIGHT = 10.0
REINFORCE_STEP = 0.5
DAY = 86400.0

EVENT_PREFIX = "metadata:memory_event:"
DIGEST_PREFIX = "metadata:memory_digest:"
EVENT_RETENTION = 5000
EVENT_PRUNE_BATCH = 500
EVENT_KINDS = ("candidates_offered", "memory_created", "memory_revised", "memory_attributes",
               "memory_deleted", "candidate_dismissed", "digest_regenerated", "ingest_snapshot",
               "memory_archived", "memory_restored", "topic_named", "merge_decided")
# Archival: a fading memory nobody used for ARCHIVE_UNUSED_DAYS leaves the default
# views; ARCHIVE_DELETE_DAYS later, still unused, it is deleted. Pinned memories and
# memories with ARCHIVE_EXEMPT_SOURCES or more sources are never archived.
ARCHIVE_UNUSED_DAYS = 30
ARCHIVE_DELETE_DAYS = 90
ARCHIVE_EXEMPT_SOURCES = 3
ARCHIVE_SWEEP_SECONDS = 86400.0
ARCHIVE_CHANGES_PER_TICK = 50
ARCHIVE_SWEEP_DOCUMENT = "metadata:memory_archive_sweep:v1"
# Stable topics: recomputed daily over every non-archived memory, ids carried
# over by member overlap (omni_topics). Merge decisions are remembered per pair.
TOPICS_DOCUMENT = "metadata:memory_topics:v1"
MERGES_DOCUMENT = "metadata:memory_merges:v1"
STABLE_TOPIC_LIMIT = 60
TOPIC_RESPONSE_BYTES = 16 * 1024
LAST_USED_WRITE_SECONDS = 3600.0  # opening a memory writes last_used at most this often
INGEST_SNAPSHOT_INTERVAL = 600.0
DIGEST_MAX_BYTES = 12288
DIGEST_TOPICS = 12
DIGEST_TOPIC_MEMORIES = 300  # topics come from the strongest memories, as in the graph
DIGEST_REFRESH_SECONDS = 3600.0
GRAPH_EDGE_KINDS = ("session", "tag", "lineage", "similarity", "workspace", "concept")
GRAPH_DEFAULT_EDGES = ("session", "tag", "lineage", "similarity")
GRAPH_FOCUS_LIMIT = 120  # an ego network stays readable at about this many nodes
GRAPH_FOCUS_POOL = 500
LIST_LIMIT = 500
MEMORY_LIMIT = 20000
MEMORY_CACHE_SECONDS = 900.0
# Memory embeddings are loaded once by the background tick, then only the
# memories whose vectors changed are re-read; a full reload (seconds on a large
# index, holding the serialized queue) happens at most hourly.
VECTOR_CACHE_SECONDS = 3600.0
PENDING_CACHE_SECONDS = 600.0
# Counting stops at this many candidates (each match costs a few index lookups
# on a large corpus); the overview reports the cap so pages can show "500+".
PENDING_COUNT_CAP = 500
# Corpus counts (a full count of the message index takes seconds on a real
# corpus, and holds the serialized queue while it runs) are display-only, so they
# are reused for thirty minutes; the background tick refreshes them a minute
# before they expire, so opening the Memory page never pays for them.
STATUS_CACHE_SECONDS = 1800.0
STATUS_WARM_SECONDS = 1740.0
EXCERPT_CHARACTERS = 600


class HippocampusError(ValueError):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def memory_type(value):
    if value is None:
        return None
    if not isinstance(value, str) or value not in MEMORY_TYPES:
        raise HippocampusError("memory_type must be one of: " + ", ".join(TYPE_ORDER))
    return value


def weight_value(value, name="weight"):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or \
            not 0 <= value <= MAX_WEIGHT:
        raise HippocampusError(name + " must be a number from 0 to " + str(int(MAX_WEIGHT)))
    return float(value)


def lineage_value(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 20 or \
            not all(isinstance(item, str) and 0 < len(item) <= 128 for item in value):
        raise HippocampusError("lineage must be an array of at most 20 memory IDs")
    return list(dict.fromkeys(value))


def bounded(value, default, maximum, minimum=1):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise HippocampusError("Expected an integer from %d to %d" % (minimum, maximum))
    return value


def attributes(value, now=None):
    """The hippocampus attributes of a stored memory document, with defaults for
    memories written before types existed."""
    kind = value.get("memory_type") if value.get("memory_type") in MEMORY_TYPES else DEFAULT_TYPE
    base, rate, _ = MEMORY_TYPES[kind]
    weight = value.get("weight")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight):
        weight = base
    weight = max(0.0, min(MAX_WEIGHT, float(weight)))
    reinforced = value.get("last_reinforced_at")
    if isinstance(reinforced, bool) or not isinstance(reinforced, (int, float)):
        reinforced = value.get("updated") if isinstance(value.get("updated"), (int, float)) else None
    pinned = value.get("pinned") is True
    evidence = value.get("evidence_count")
    if isinstance(evidence, bool) or not isinstance(evidence, int) or evidence < 0:
        evidence = len(value.get("sources") or [])
    result = {"memory_type": kind, "base_weight": base, "decay_rate": rate, "weight": round(weight, 4),
              "evidence_count": evidence, "lineage": list(value.get("lineage") or []),
              "pinned": pinned, "last_reinforced_at": reinforced}
    result["effective_weight"] = effective_weight(result, now)
    result["expiry_candidate"] = not pinned and result["effective_weight"] < EXPIRY_THRESHOLD
    for name in ("archived_at", "last_used"):
        stamp = value.get(name)
        result[name] = stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None
    result["archived"] = result["archived_at"] is not None
    return result


def last_activity(memory):
    """When a memory was last opened, cited, reinforced or written."""
    return max((stamp for stamp in (memory.get("last_used"), memory.get("last_reinforced_at"),
                                    memory.get("updated"), memory.get("created"))
                if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)), default=0)


def archive_action(memory, now):
    """The archival step due for one memory (attributes included): archive, delete or None."""
    if memory.get("pinned") or len(memory.get("sources") or []) >= ARCHIVE_EXEMPT_SOURCES:
        return None
    unused = now - last_activity(memory)
    if memory.get("archived_at") is not None:
        since = now - max(memory["archived_at"], last_activity(memory))
        return "delete" if since >= ARCHIVE_DELETE_DAYS * DAY else None
    if memory.get("expiry_candidate") and unused >= ARCHIVE_UNUSED_DAYS * DAY:
        return "archive"
    return None


def effective_weight(attributes_value, now=None):
    if attributes_value.get("pinned"):
        return attributes_value["weight"]
    reinforced = attributes_value.get("last_reinforced_at")
    days = 0.0 if reinforced is None else max(0.0, ((now if now is not None else time.time()) - reinforced) / DAY)
    return round(max(0.0, attributes_value["weight"] - attributes_value["decay_rate"] * days), 4)


def next_attributes(existing, arguments, new_evidence, changed, now):
    """Attributes for an upsert. A new memory starts at its type's base weight;
    a revision that adds evidence is reinforced from its current decayed weight."""
    requested_type = memory_type(arguments.get("memory_type"))
    requested_weight = weight_value(arguments.get("weight"))
    requested_lineage = lineage_value(arguments.get("lineage"))
    if existing is None:
        kind = requested_type or DEFAULT_TYPE
        base = MEMORY_TYPES[kind][0]
        return {"memory_type": kind, "weight": requested_weight if requested_weight is not None else base,
                "evidence_count": new_evidence, "lineage": requested_lineage or [],
                "pinned": arguments.get("pinned") is True, "last_reinforced_at": now}
    current = attributes(existing, now)
    kind = requested_type or current["memory_type"]
    weight = current["effective_weight"] if changed else current["weight"]
    reinforced = current["last_reinforced_at"]
    if changed:
        weight = min(MAX_WEIGHT, weight + REINFORCE_STEP * max(1, new_evidence))
        reinforced = now
    if requested_weight is not None:
        weight = requested_weight
    lineage = current["lineage"]
    if requested_lineage:
        lineage = list(dict.fromkeys(lineage + requested_lineage))
    pinned = current["pinned"] if arguments.get("pinned") is None else arguments.get("pinned") is True
    return {"memory_type": kind, "weight": round(weight, 4),
            "evidence_count": current["evidence_count"] + new_evidence,
            "lineage": lineage, "pinned": pinned, "last_reinforced_at": reinforced}


def first_sentence(text, limit=180):
    text = " ".join(str(text).split())
    for mark in (". ", "! ", "? "):
        index = text.find(mark)
        if 0 < index < limit:
            return text[:index + 1]
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def workspace_label(workspace):
    """A hub's name: the folder, or for a worktree checkout (`repo/@head`) the repository."""
    parts = [part for part in workspace.rstrip("/").split("/") if part]
    while len(parts) > 1 and parts[-1].startswith("@"):
        parts.pop()
    return parts[-1] if parts else workspace


def workspace_key(workspace):
    return hashlib.sha256((workspace or "*").encode("utf-8")).hexdigest()[:24]


class Hippocampus:
    """Store-facing operations. `store` is the KnowledgeStore; writes go through
    its `_document`/`_put_memory` so the native app persists them."""

    def __init__(self, store):
        self.store = store
        self._last_ingest_snapshot = 0.0
        self._last_event_ms = None
        self._pending_cache = None
        self._pending_job = None
        self._status_cache = None
        self._intake_cache = None
        # Enriched memories (evidence availability, stored attributes, workspaces)
        # by id. Memory writes mark their id changed in the store, so a read
        # re-enriches only those; a full rebuild happens at most every
        # MEMORY_CACHE_SECONDS to pick up evidence removed by source deletion.
        self._memory_cache = None
        self._memory_cache_at = 0.0
        self._vector_cache = None
        self._vector_cache_at = 0.0
        self._vector_loading = None
        self._workspace_backfill = set()
        self._similarity_cache = {}
        # Concept keys by memory id, with the revision and workspaces they were
        # extracted from; the hourly full rebuild reuses unchanged memories'.
        self._concept_cache = {}

    @property
    def connection(self):
        return self.store.connection

    # --- Journal ---

    def record(self, kind, now=None, **fields):
        if kind not in EVENT_KINDS:
            raise HippocampusError("Unknown memory event kind")
        now = time.time() if now is None else now
        # IDs sort in journal order: strictly increasing milliseconds, resumed
        # from the newest stored event after a restart.
        if self._last_event_ms is None:
            row = self.connection.execute("SELECT key FROM documents WHERE key>? AND key<? ORDER BY key DESC LIMIT 1",
                                          (EVENT_PREFIX, EVENT_PREFIX[:-1] + ";")).fetchone()
            self._last_event_ms = int(row["key"][len(EVENT_PREFIX):].split(":")[0]) if row else 0
        stamp = max(int(now * 1000), self._last_event_ms + 1)
        self._last_event_ms = stamp
        identifier = "%013d:%s" % (stamp, uuid.uuid4().hex[:12])
        event = {"id": identifier, "kind": kind, "at": now}
        event.update({key: value for key, value in fields.items() if value is not None})
        self.store._document(EVENT_PREFIX + identifier, event)
        if kind in ("candidate_dismissed", "memory_created", "memory_revised") and self._pending_cache is not None:
            # Stale, but kept to show until the background count replaces it.
            self._pending_cache = (0.0, self._pending_cache[1])
        self._prune_events()
        return event

    def _event_count(self):
        return self.connection.execute("SELECT COUNT(*) FROM documents WHERE key>? AND key<?",
                                       (EVENT_PREFIX, EVENT_PREFIX[:-1] + ";")).fetchone()[0]

    def _prune_events(self):
        if self._event_count() <= EVENT_RETENTION + EVENT_PRUNE_BATCH:
            return
        for row in self.connection.execute("SELECT key FROM documents WHERE key>? AND key<? ORDER BY key LIMIT ?",
                                           (EVENT_PREFIX, EVENT_PREFIX[:-1] + ";", EVENT_PRUNE_BATCH)).fetchall():
            self.store._document(row["key"], None)

    def events(self, arguments):
        limit = bounded(arguments.get("limit"), 50, 200)
        before = arguments.get("before")
        if before is not None and (not isinstance(before, str) or len(before) > 64):
            raise HippocampusError("before must be an event ID")
        kind = arguments.get("kind")
        if kind is not None and kind not in EVENT_KINDS:
            raise HippocampusError("kind must be one of: " + ", ".join(EVENT_KINDS))
        memory_id = arguments.get("memory_id")
        if memory_id is not None and (not isinstance(memory_id, str) or not 0 < len(memory_id) <= 128):
            raise HippocampusError("memory_id must be a memory ID")
        upper = EVENT_PREFIX + before if before else EVENT_PREFIX[:-1] + ";"
        events, cursor, scanned = [], upper, 0
        # Filters scan newest first in bounded pages, so a rare kind costs at
        # most the retained journal, never the transcript corpus.
        while len(events) < limit and scanned < EVENT_RETENTION + EVENT_PRUNE_BATCH:
            rows = self.connection.execute("SELECT key,value FROM documents WHERE key>? AND key<? ORDER BY key DESC LIMIT 200",
                                           (EVENT_PREFIX, cursor)).fetchall()
            if not rows:
                break
            for row in rows:
                scanned += 1
                cursor = row["key"]
                event = json.loads(row["value"])
                if kind and event.get("kind") != kind:
                    continue
                if memory_id and memory_id != event.get("memory_id") and memory_id not in (event.get("memory_ids") or []):
                    continue
                events.append(event)
                if len(events) == limit:
                    break
        more = len(events) == limit
        return {"events": events, "next": events[-1]["id"] if more else None}

    def maybe_refresh_digest(self, now=None):
        """The mid-term reflex: regenerate the all-workspaces digest when memories
        changed since it was written, at most once an hour."""
        now = time.time() if now is None else now
        stored = self.store._document_value(DIGEST_PREFIX + workspace_key(None))
        if stored is None:
            if not self.connection.execute("SELECT 1 FROM memories LIMIT 1").fetchone():
                return None
        elif now - stored.get("generated_at", 0) < DIGEST_REFRESH_SECONDS or not self.digest({}, now)["digest"]["stale"]:
            return None
        return self.digest({"regenerate": True}, now)["digest"]

    def maybe_warm_status(self, now=None):
        """Refresh the cached corpus counts and the pending-candidate count off
        the interactive path, shortly before the Memory page would otherwise
        recompute them itself."""
        now = time.time() if now is None else now
        if self._pending_cache is None or now - self._pending_cache[0] >= PENDING_CACHE_SECONDS - 60:
            if self.store.readthrough:
                if not self.step_pending(now):
                    return None  # still counting; another window next tick
            else:
                self._pending_cache = None
                self.pending_candidates(now=now)
            self.intake_counts(now=now, refresh=True)
        if self._status_cache is not None and now - self._status_cache[0] < STATUS_WARM_SECONDS:
            return None
        if not self.store.step_row_counts():
            return None  # still counting; another slice next tick
        self._status_cache = (now, self.store.quick_status())
        return self._status_cache[1]

    def maybe_snapshot_ingest(self, now=None):
        now = time.time() if now is None else now
        if now - self._last_ingest_snapshot < INGEST_SNAPSHOT_INTERVAL:
            return None
        self._last_ingest_snapshot = now
        status = self.maybe_warm_status(now) or self._status_cache[1]
        embedding = status.get("embedding") or {}
        return self.record("ingest_snapshot", now=now, messages=status["messages"], sessions=status["sessions"],
                           memories=status["memories"], backlog_files=status["backlog_files"],
                           scan_in_progress=status["scan_in_progress"], vectors=embedding.get("vectors"),
                           pending_vectors=embedding.get("pending_records"))

    # --- Reading memories ---

    def _stored(self, identifier):
        return self.store._document_value("memory:" + identifier)

    def _workspaces(self, sources):
        found = []
        for source in sources:
            row = self.connection.execute("SELECT workspace FROM messages WHERE id=? AND session_id=?",
                                          (source.get("message_id"), source.get("session_id"))).fetchone()
            workspace = row["workspace"] if row else None
            if workspace is None:
                row = self.connection.execute("SELECT workspace FROM sources WHERE session_id=? AND workspace IS NOT NULL LIMIT 1",
                                              (source.get("session_id"),)).fetchone()
                workspace = row["workspace"] if row else None
            if workspace and workspace not in found:
                found.append(workspace)
        return found

    def enrich(self, memory, now=None, workspaces=True):
        stored = self._stored(memory["id"]) or memory
        value = dict(memory)
        value.update(attributes({**stored, "sources": memory.get("sources") or stored.get("sources")}, now))
        if workspaces:
            value["workspaces"] = self._workspaces(value.get("sources") or [])
        return value

    def _cached_entry(self, row, session_workspaces=None, stored=None):
        """A memory for lists and the graph. Its workspaces are stored on the
        memory when it is written, so a cold build reads only the memory rows and
        documents; looking up every evidence record took seconds on a large index.
        Evidence availability is checked where it is shown, in get_memory."""
        memory = dict(row)
        memory["sources"] = json.loads(memory["sources"])
        memory["tags"] = json.loads(memory["tags"])
        stored = stored or self._stored(memory["id"]) or memory
        workspaces = stored.get("workspaces")
        if not isinstance(workspaces, list):
            workspaces = self.evidence_workspaces(memory["sources"], session_workspaces)
            self._workspace_backfill.add(memory["id"])
        self.concepts(memory, workspaces)
        return memory, stored, workspaces

    def concepts(self, memory, workspaces=None):
        """A memory's concept keys, extracted from its title, tags and body once
        per revision."""
        workspaces = list(memory.get("workspaces") or []) if workspaces is None else list(workspaces)
        key = (memory.get("revision"), tuple(workspaces))
        cached = self._concept_cache.get(memory["id"])
        if cached is not None and cached[0] == key:
            return cached[1]
        found = CONCEPTS.extract(memory.get("title"), memory.get("body"), memory.get("tags") or [], workspaces)
        self._concept_cache[memory["id"]] = (key, found)
        return found

    def evidence_workspaces(self, sources, session_workspaces=None):
        """The workspaces of a memory's evidence, in one query per memory."""
        identifiers = list(dict.fromkeys(source.get("message_id") for source in sources if source.get("message_id")))
        found = {}
        for start in range(0, len(identifiers), 500):
            chunk = identifiers[start:start + 500]
            for message in self.connection.execute("SELECT id,session_id,workspace FROM messages WHERE id IN (" +
                                                   ",".join("?" for _ in chunk) + ")", chunk):
                found[(message["id"], message["session_id"])] = message["workspace"]
        session_workspaces = {} if session_workspaces is None else session_workspaces
        workspaces = []
        for source in sources:
            workspace = found.get((source.get("message_id"), source.get("session_id")))
            if workspace is None:
                session = source.get("session_id")
                if session not in session_workspaces:
                    hit = self.connection.execute("SELECT workspace FROM sources WHERE session_id=? AND workspace IS NOT NULL LIMIT 1",
                                                  (session,)).fetchone()
                    session_workspaces[session] = hit["workspace"] if hit else None
                workspace = session_workspaces[session]
            if workspace and workspace not in workspaces:
                workspaces.append(workspace)
        return workspaces

    def backfill_workspaces(self, budget_seconds=0.3):
        """Store workspaces on memories written before they were stored."""
        started, done = time.monotonic(), 0
        while self._workspace_backfill and time.monotonic() - started < budget_seconds:
            identifier = self._workspace_backfill.pop()
            stored = self._stored(identifier)
            if not isinstance(stored, dict) or isinstance(stored.get("workspaces"), list):
                continue
            stored = dict(stored, workspaces=self.evidence_workspaces(stored.get("sources") or []))
            self.store._document("memory:" + identifier, stored)
            done += 1
        return done

    def _all(self, now, workspaces=True):
        changed = self.store._memory_changes
        if self._memory_cache is None or time.monotonic() - self._memory_cache_at >= MEMORY_CACHE_SECONDS:
            changed.clear()
            rows = self.connection.execute("SELECT * FROM memories ORDER BY id LIMIT ?", (MEMORY_LIMIT,)).fetchall()
            session_workspaces = {}
            # One range read of the memory documents: thousands of point lookups
            # were most of a cold build.
            documents = {}
            for document in self.connection.execute("SELECT key,value FROM documents WHERE key>='memory:' AND key<'memory;'"):
                try:
                    documents[document["key"][len("memory:"):]] = json.loads(document["value"])
                except ValueError:
                    pass
            self._memory_cache = {row["id"]: self._cached_entry(row, session_workspaces, documents.get(row["id"]))
                                  for row in rows}
            self._concept_cache = {identifier: entry for identifier, entry in self._concept_cache.items()
                                   if identifier in self._memory_cache}
            self._memory_cache_at = time.monotonic()
        elif changed:
            identifiers = list(changed)
            changed.clear()
            for identifier in identifiers:
                row = self.connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    self._memory_cache.pop(identifier, None)
                    self._concept_cache.pop(identifier, None)
                else:
                    self._memory_cache[identifier] = self._cached_entry(row)
        result = []
        for identifier in sorted(self._memory_cache):
            memory, stored, found = self._memory_cache[identifier]
            value = dict(memory)
            value.update(attributes({**stored, "sources": memory.get("sources") or stored.get("sources")}, now))
            if workspaces:
                value["workspaces"] = list(found)
            result.append(value)
        return result

    def list(self, arguments, now=None):
        now = time.time() if now is None else now
        limit = bounded(arguments.get("limit"), 100, LIST_LIMIT)
        offset = bounded(arguments.get("offset"), 0, 100000, minimum=0)
        kind = memory_type(arguments.get("memory_type"))
        tag = arguments.get("tag")
        workspace = arguments.get("workspace")
        sort = arguments.get("sort", "weight")
        if sort not in ("weight", "recent", "evidence", "title"):
            raise HippocampusError("sort must be weight, recent, evidence or title")
        query = arguments.get("query")
        if query is not None and (not isinstance(query, str) or len(query) > 2000):
            raise HippocampusError("query must be a string")
        include_expired = arguments.get("include_expired", True) is not False
        archived = arguments.get("archived", False)
        if not isinstance(archived, bool):
            raise HippocampusError("archived must be true or false")
        if query and query.strip():
            ids = {row["id"] for row in self.store._lexical_rows("memories", query, {}, LIST_LIMIT, relax=False)}
            memories = [memory for memory in self._all(now) if memory["id"] in ids]
        else:
            memories = self._all(now)
        memories = [memory for memory in memories
                    if (kind is None or memory["memory_type"] == kind) and
                    (tag is None or tag in memory.get("tags", [])) and
                    (workspace is None or workspace in memory.get("workspaces", [])) and
                    (include_expired or not memory["expiry_candidate"])]
        archived_total = sum(1 for memory in memories if memory["archived"])
        # Archived memories leave the default list; archived:true lists only them.
        memories = [memory for memory in memories if memory["archived"] == archived]
        keys = {"weight": lambda m: (-m["effective_weight"], -(m.get("updated") or 0), m["id"]),
                "recent": lambda m: (-(m.get("updated") or 0), m["id"]),
                "evidence": lambda m: (-m["evidence_count"], -m["effective_weight"], m["id"]),
                "title": lambda m: (m.get("title", "").lower(), m["id"])}
        memories.sort(key=keys[sort])
        return {"memories": memories[offset:offset + limit], "total": len(memories), "offset": offset,
                "archived_total": archived_total,
                "types": {name: {"base_weight": spec[0], "decay_rate": spec[1], "description": spec[2]}
                          for name, spec in MEMORY_TYPES.items()}}

    def get(self, arguments, now=None):
        now = time.time() if now is None else now
        identifier = arguments.get("id")
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 128:
            raise HippocampusError("id must be a memory ID")
        row = self.connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise HippocampusError("Memory not found", "not_found")
        self.touch(identifier, now)
        memory = self.enrich(self.store._memory(row), now)
        evidence = []
        for source in memory.get("sources", []):
            item = dict(source)
            message = self.connection.execute("SELECT * FROM messages WHERE id=? AND session_id=?",
                                              (source["message_id"], source["session_id"])).fetchone()
            if message is not None:
                text = message["text"]
                item.update(role=message["role"], kind=message["kind"], timestamp=message["timestamp"],
                            workspace=message["workspace"], provider=message["provider"],
                            excerpt=text if len(text) <= EXCERPT_CHARACTERS else text[:EXCERPT_CHARACTERS - 1] + "…",
                            truncated=len(text) > EXCERPT_CHARACTERS)
            session = self.connection.execute("SELECT title FROM sources WHERE session_id=? AND title IS NOT NULL LIMIT 1",
                                              (source["session_id"],)).fetchone()
            if session is not None:
                item["session_title"] = session["title"]
            evidence.append(item)
        memory["evidence"] = evidence
        prefix = "memory_revision:" + identifier + ":"
        revisions = []
        for revision in self.connection.execute("SELECT value FROM documents WHERE key>=? AND key<?",
                                                (prefix, prefix + "\U0010ffff")).fetchall():
            value = json.loads(revision["value"])
            revisions.append({key: value.get(key) for key in ("revision", "title", "body", "tags", "updated", "memory_type")})
        revisions.sort(key=lambda value: value.get("revision") or 0, reverse=True)
        memory["revisions"] = revisions
        memory["events"] = self.events({"memory_id": identifier, "limit": 50})["events"]
        related = self.graph({"focus": identifier, "limit": 60}, now)
        memory["related"] = [edge for edge in related["edges"]
                             if identifier in (edge["source"], edge["target"])][:20]
        return {"memory": memory}

    # --- Overview ---

    def overview(self, arguments=None, now=None):
        now = time.time() if now is None else now
        everything = self._all(now, workspaces=False)
        memories = [memory for memory in everything if not memory["archived"]]
        types = {name: {"count": 0, "base_weight": spec[0], "decay_rate": spec[1], "description": spec[2],
                        "mean_weight": 0.0} for name, spec in MEMORY_TYPES.items()}
        tags = {}
        for memory in memories:
            entry = types[memory["memory_type"]]
            entry["count"] += 1
            entry["mean_weight"] += memory["effective_weight"]
            for tag in memory.get("tags", []):
                tags[tag] = tags.get(tag, 0) + 1
        for entry in types.values():
            entry["mean_weight"] = round(entry["mean_weight"] / entry["count"], 3) if entry["count"] else 0.0
        if self._intake_cache is None and self.store.readthrough:
            # Counted by the background tick; never on the request path of a cold launch.
            considered = cited = excluded_sessions = None
        else:
            considered, cited, excluded_sessions = self.intake_counts(now)
        recent = {}
        for event in self.events({"limit": 200})["events"]:
            if event["at"] >= now - DAY:
                recent[event["kind"]] = recent.get(event["kind"], 0) + 1
        warming = False
        if self._status_cache is None and self.store.readthrough:
            # Counting a large native index takes seconds; the background tick
            # warms it within one tick of launch, so the page shows "counting"
            # instead of waiting for it.
            checked_at, status, warming = None, {}, True
        else:
            if self._status_cache is None or now - self._status_cache[0] >= STATUS_CACHE_SECONDS:
                self._status_cache = (now, self.store.quick_status())
            checked_at, status = self._status_cache
        embedding = status.get("embedding") or {}
        return {"overview": {
            "memories": len(memories), "types": types,
            "pinned": sum(1 for memory in memories if memory["pinned"]),
            "archived": len(everything) - len(memories),
            "expiry_candidates": sum(1 for memory in memories if memory["expiry_candidate"]),
            "tags": sorted(({"tag": tag, "count": count} for tag, count in tags.items()),
                           key=lambda item: (-item["count"], item["tag"]))[:30],
            "pipeline": {
                "sessions": status.get("sessions"), "messages": status.get("messages"),
                "backlog_files": status.get("backlog_files"), "scan_in_progress": status.get("scan_in_progress"),
                "stale_native_sources": status.get("stale_native_sources"),
                "mirrored_sessions": status.get("mirrored_sessions"), "orphan_threads": status.get("orphan_threads"),
                "vectors": embedding.get("vectors"), "pending_vectors": embedding.get("pending_records"),
                "coverage": embedding.get("coverage"),
                "embedding_available": embedding.get("available"),
                "candidates_pending": (self._pending_cache[1] if self._pending_cache else None) if warming
                                      else self.pending_candidates(now=now),
                "candidates_pending_cap": PENDING_COUNT_CAP, "counts_warming": warming,
                "candidates_considered": considered,
                "candidates_cited": cited, "candidates_dismissed": None if considered is None else considered - cited,
                "excluded_consolidator_sessions": excluded_sessions,
                "memories": len(memories), "expiry_candidates": sum(1 for m in memories if m["expiry_candidate"]),
                "status_checked_at": checked_at},
            "events_24h": recent, "expiry_threshold": EXPIRY_THRESHOLD,
            "generated_at": now}}

    def intake_counts(self, now=None, refresh=False):
        """Considered, cited and excluded-session counts. Every evidence record a
        memory cites is an intake document (tens of thousands once knowledge
        bases are mapped), so the counts are reused like the pending count."""
        now = time.time() if now is None else now
        if not refresh and self._intake_cache is not None and now - self._intake_cache[0] < PENDING_CACHE_SECONDS:
            return self._intake_cache[1]
        intake_upper = "metadata:memory_intake;"
        considered = self.connection.execute("SELECT COUNT(*) FROM documents WHERE key>'metadata:memory_intake:' AND key<?",
                                             (intake_upper,)).fetchone()[0]
        cited = self.connection.execute("SELECT COUNT(*) FROM documents WHERE key>'metadata:memory_intake:' AND key<? "
                                        "AND json_extract(value,'$.memory_id') IS NOT NULL", (intake_upper,)).fetchone()[0]
        excluded = self.connection.execute("SELECT COUNT(*) FROM documents WHERE key>'metadata:memory_session:' "
                                           "AND key<'metadata:memory_session;'").fetchone()[0]
        self._intake_cache = (now, (considered, cited, excluded))
        return self._intake_cache[1]

    def step_pending(self, now=None, budget_seconds=0.25, window=2000):
        """Count unconsidered candidates (up to the cap) on the native index in
        rowid windows, newest first, resuming across ticks: a full walk can take
        seconds once recent records are mostly considered or knowledge-base
        sections. Returns True when a fresh count is cached."""
        now = time.time() if now is None else now
        conditions, bindings = self.store._candidate_conditions()
        where = " AND ".join(conditions)
        if self._pending_job is None:
            top = self.connection.execute("SELECT MAX(rowid) FROM persisted.messages").fetchone()[0] or 0
            overlay = self.connection.execute("SELECT COUNT(*) FROM (SELECT 1 FROM main.overlay_messages m WHERE " + where +
                                              " LIMIT ?)", bindings + [PENDING_COUNT_CAP + 1]).fetchone()[0]
            self._pending_job = {"upper": top + 1, "count": overlay}
        job, started, first = self._pending_job, time.monotonic(), True
        while job["count"] <= PENDING_COUNT_CAP and job["upper"] > 0:
            if not first and time.monotonic() - started >= budget_seconds:
                return False
            first = False
            lower = max(0, job["upper"] - window)
            job["count"] += self.connection.execute(
                "SELECT COUNT(*) FROM persisted.messages m WHERE m.rowid>=? AND m.rowid<? AND " + where +
                " AND NOT EXISTS(SELECT 1 FROM main.shadow_documents s WHERE s.key='message:'||m.id)",
                [lower, job["upper"]] + bindings).fetchone()[0]
            job["upper"] = lower
        self._pending_job = None
        self._pending_cache = (now, min(job["count"], PENDING_COUNT_CAP + 1))
        return True

    def pending_candidates(self, cap=None, now=None):
        cap = PENDING_COUNT_CAP if cap is None else cap
        # The count scans the transcript corpus on the serialized knowledge
        # queue, so a page refreshing every few seconds reuses it for a minute.
        now = time.time() if now is None else now
        if self._pending_cache is not None and now - self._pending_cache[0] < PENDING_CACHE_SECONDS:
            return self._pending_cache[1]
        if self.store.readthrough and cap == PENDING_COUNT_CAP:
            # On the native index the background tick counts (step_pending); a
            # request shows the last count instead of walking the corpus.
            return self._pending_cache[1] if self._pending_cache is not None else None
        conditions, bindings = self.store._candidate_conditions()
        count = self.store._newest_candidate_rows(conditions, bindings, cap + 1, count_only=True)
        self._pending_cache = (now, count)
        return count

    # --- Graph and mind map ---

    def _memory_vectors(self, identifiers):
        """Normalized vectors of the given memories, from a cache the background
        tick refreshes; only the first graph after a launch pays to load them."""
        if self._vector_cache is None:
            if self.store.readthrough and self.store._semantics is not False:
                # Loading every memory's vectors takes seconds on a large index;
                # the background tick does it, and the graph adds similarity
                # edges once it has.
                return None
            self.refresh_vectors()
        if self._vector_cache is None:
            return None
        return {identifier: vector for identifier, vector in self._vector_cache.items() if identifier in identifiers}

    def maybe_refresh_vectors(self, now=None, budget_seconds=0.3):
        """Returns True while a full reload is under way. A full reload reads the
        memories' vectors in slices of about budget_seconds per tick, so no single
        worker call holds the queue for the seconds a whole reload takes."""
        changes = self.store._memory_vector_changes
        if self._vector_loading is None and (self._vector_cache is None or
                                             time.monotonic() - self._vector_cache_at >= VECTOR_CACHE_SECONDS):
            changes.clear()
            rows = self.connection.execute("SELECT id FROM memories ORDER BY id").fetchall()
            self._vector_loading = ([row["id"] for row in rows], {})
            return True
        if self._vector_loading is not None:
            remaining, loaded = self._vector_loading
            started, first = time.monotonic(), True
            while remaining and (first or time.monotonic() - started < budget_seconds):
                first = False
                batch, remaining[:] = remaining[:50], remaining[50:]
                vectors = self._load_memory_vectors(set(batch), by_record=True)
                if vectors is None:
                    self._vector_loading = None
                    return False
                loaded.update(vectors)
            if not remaining:
                self._vector_loading = None
                self._vector_cache = loaded
                self._vector_cache_at = time.monotonic()
                self._similarity_cache = {}
            return True
        if changes:
            batch = list(changes)[:200]
            changes.difference_update(batch)
            hashed = {entry[len("hash:"):] for entry in batch if entry.startswith("hash:")}
            identifiers = [entry for entry in batch if not entry.startswith("hash:")]
            if hashed:
                # A removed vector names its memory only by the ID's hash.
                identifiers += [identifier for identifier in self._vector_cache
                                if hashlib.sha256(identifier.encode("utf-8")).hexdigest() in hashed]
            loaded = self._load_memory_vectors(set(identifiers), by_record=True) or {}
            for identifier in identifiers:
                if identifier in loaded:
                    self._vector_cache[identifier] = loaded[identifier]
                else:
                    self._vector_cache.pop(identifier, None)
            # Similarity edges are recomputed with the hourly full reload, not on
            # every newly embedded memory (each recompute holds the queue ~1 s).
        return False

    def refresh_vectors(self):
        vectors = self._load_memory_vectors(None)
        self._vector_cache = vectors
        self._vector_cache_at = time.monotonic()
        self._similarity_cache = {}

    def _load_memory_vectors(self, identifiers, by_record=False):
        semantics = self.store._semantics
        if semantics is False:
            return None
        try:
            semantics = self.store.semantics
            model, dimensions = semantics.embedding.model_id, semantics.embedding.dimensions
            sums = {}
            if by_record:
                rows = [row for identifier in identifiers
                        for row in semantics._vector_rows("memory", model, dimensions, identifier=identifier)]
            else:
                rows = semantics._vector_rows("memory", model, dimensions)
            for row in rows:
                if identifiers is not None and row["record_id"] not in identifiers:
                    continue
                if row["chunk_index"] < 0:
                    continue  # the title vector serves search; similarity averages the body
                vector = semantics.decode_vector(row["vector"], dimensions)
                if vector is None:
                    continue
                total = sums.setdefault(row["record_id"], [0.0] * dimensions)
                for index, value in enumerate(vector):
                    total[index] += value
            vectors = {}
            for identifier, total in sums.items():
                norm = math.sqrt(math.fsum(value * value for value in total))
                if norm > 0:
                    vectors[identifier] = [value / norm for value in total]
            return vectors
        except Exception:  # The model is optional; the graph degrades without similarity.
            return None

    def graph(self, arguments, now=None):
        now = time.time() if now is None else now
        limit = bounded(arguments.get("limit"), 300, 500)
        kinds = arguments.get("edges", list(GRAPH_DEFAULT_EDGES))
        if not isinstance(kinds, list) or not all(kind in GRAPH_EDGE_KINDS for kind in kinds):
            raise HippocampusError("edges must list kinds from: " + ", ".join(GRAPH_EDGE_KINDS))
        threshold = arguments.get("min_similarity", 0.8)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise HippocampusError("min_similarity must be from 0 to 1")
        kind_filter = memory_type(arguments.get("memory_type"))
        focus = arguments.get("focus")
        if focus is not None and (not isinstance(focus, str) or not 0 < len(focus) <= 256):
            raise HippocampusError("focus must be a memory ID")
        hops = arguments.get("hops")
        if hops is not None and (isinstance(hops, bool) or hops not in (1, 2)):
            raise HippocampusError("hops must be 1 or 2")
        if hops is not None and not focus:
            raise HippocampusError("hops needs focus")
        workspace = arguments.get("workspace")
        if workspace is not None and (not isinstance(workspace, str) or not 0 < len(workspace) <= 4096):
            raise HippocampusError("workspace must be a path")
        after = arguments.get("after")
        if after is not None and (isinstance(after, bool) or not isinstance(after, (int, float)) or after < 0):
            raise HippocampusError("after must be a time in seconds since 1970")
        min_weight = arguments.get("min_weight")
        if min_weight is not None and (isinstance(min_weight, bool) or not isinstance(min_weight, (int, float))
                                       or not 0 <= min_weight <= MAX_WEIGHT):
            raise HippocampusError("min_weight must be a number from 0 to " + str(int(MAX_WEIGHT)))
        # Archived memories are hidden unless asked for (or focused, from an open memory).
        show_archived = arguments.get("archived") is True
        memories = [memory for memory in self._all(now)
                    if (show_archived or not memory["archived"] or memory["id"] == focus)
                    and (kind_filter is None or memory["memory_type"] == kind_filter)
                    and (workspace is None or CONCEPTS.workspace_alias(workspace) in
                         {CONCEPTS.workspace_alias(path) for path in memory.get("workspaces") or []})
                    and (after is None or (memory.get("updated") or 0) >= after)
                    and (min_weight is None or memory["effective_weight"] >= min_weight)]
        # Strongest first by weight in half-point steps: exact decayed weights move
        # every second and would reshuffle the memories near the cut on every
        # refresh, so the shown set would never stay the same.
        memories.sort(key=lambda memory: (-round(memory["effective_weight"] * 2), memory["id"]))
        total = len(memories)
        # An ego network is cut from a wider pool, so a weak memory's neighbours
        # are found even when they fall outside the strongest `limit`.
        chosen = memories[:GRAPH_FOCUS_POOL if hops else limit]
        if focus and not any(memory["id"] == focus for memory in chosen):
            chosen = [memory for memory in memories if memory["id"] == focus] + chosen[:max(0, len(chosen) - 1)]
        # Membership comes from weight; the payload is ordered by id so a
        # refresh whose weights drifted sends the same graph in the same order.
        chosen.sort(key=lambda memory: memory["id"])
        nodes = [{"id": memory["id"], "kind": "memory", "label": memory.get("title", ""),
                  "memory_type": memory["memory_type"], "weight": memory["weight"],
                  "effective_weight": memory["effective_weight"], "expiry_candidate": memory["expiry_candidate"],
                  "pinned": memory["pinned"], "tags": memory.get("tags", []),
                  "workspaces": memory.get("workspaces", []), "evidence_count": memory["evidence_count"],
                  "updated": memory.get("updated")} for memory in chosen]
        index = {node["id"]: node for node in nodes}
        edges = {}

        def connect(a, b, kind, strength, label=None):
            if a == b or a not in index or b not in index:
                return
            source, target = sorted((a, b))
            key = (source, target, kind)
            if key not in edges or edges[key]["strength"] < strength:
                edges[key] = {"source": source, "target": target, "kind": kind, "strength": round(strength, 4)}
                if label:
                    edges[key]["label"] = label

        by_id = {memory["id"]: memory for memory in chosen}
        if "session" in kinds:
            sessions = {}
            for memory in chosen:
                for session in {source["session_id"] for source in memory.get("sources", [])}:
                    sessions.setdefault(session, []).append(memory["id"])
            for session, members in sessions.items():
                if len(members) > 40:
                    continue  # A hub session links everything; it says nothing.
                for i, a in enumerate(members):
                    for b in members[i + 1:]:
                        shared = len({s["session_id"] for s in by_id[a]["sources"]} & {s["session_id"] for s in by_id[b]["sources"]})
                        smaller = min(len({s["session_id"] for s in by_id[a]["sources"]}),
                                      len({s["session_id"] for s in by_id[b]["sources"]}))
                        connect(a, b, "session", shared / max(1, smaller))
        if "tag" in kinds:
            tagged = {}
            for memory in chosen:
                for tag in memory.get("tags", []):
                    tagged.setdefault(tag, []).append(memory["id"])
            for tag, members in tagged.items():
                if len(members) > 60:
                    continue
                for i, a in enumerate(members):
                    for b in members[i + 1:]:
                        left, right = set(by_id[a].get("tags", [])), set(by_id[b].get("tags", []))
                        connect(a, b, "tag", len(left & right) / max(1, len(left | right)),
                                ", ".join(sorted(left & right)))
        if "lineage" in kinds:
            for memory in chosen:
                for ancestor in memory.get("lineage", []):
                    connect(memory["id"], ancestor, "lineage", 1.0)
        similarity_available = None
        if "similarity" in kinds:
            warming = (self._vector_cache is None and self.store.readthrough and self.store._semantics is not False)
            vectors = self._memory_vectors(set(index))
            similarity_available = None if warming else vectors is not None
            if vectors:
                for a, b, score in self._similar_pairs(vectors, threshold):
                    connect(a, b, "similarity", score)
        if "workspace" in kinds:
            for memory in chosen:
                # Worktree checkouts of one repository share its hub.
                for workspace in dict.fromkeys(CONCEPTS.workspace_alias(path) for path in memory.get("workspaces", [])):
                    hub = "workspace:" + workspace
                    if hub not in index:
                        index[hub] = {"id": hub, "kind": "workspace", "label": workspace_label(workspace),
                                      "path": workspace}
                        nodes.append(index[hub])
                    connect(memory["id"], hub, "workspace", 0.5)
        mindmap = arguments.get("mindmap") is True
        # Concepts are extracted once per revision, so communities cost one
        # label propagation; every memory node names its community.
        memberships = CONCEPTS.fold_aliases({memory["id"]: self.concepts(memory) for memory in chosen})
        shared = CONCEPTS.hubs(memberships)
        topics = CONCEPTS.communities(memberships, shared)
        stable = self.topics_for(memory["id"] for memory in chosen)
        if stable:
            # Named, day-stable topics first. They are computed over every memory, so
            # most of the heaviest memories shown here belong to none; those keep the
            # communities of the graph shown, so the overview still has regions.
            covered = {member for topic in stable for member in topic["members"]}
            rest = []
            for topic in topics:
                members = [member for member in topic["members"] if member not in covered]
                if len(members) >= 2:
                    rest.append(dict(topic, members=members))
            topics = stable + rest
        if "concept" in kinds:
            for key, members in shared.items():
                hub = "concept:" + key
                index[hub] = {"id": hub, "kind": "concept", "label": CONCEPTS.label(key),
                              "concept_kind": CONCEPTS.kind_of(key), "count": len(members)}
                nodes.append(index[hub])
                for member in members:
                    connect(member, hub, "concept", 0.5)
        for topic in topics:
            # Named by its smallest member, so the id survives small shifts.
            community = {"id": topic.get("id") or "community:" + topic["members"][0],
                         "label": topic.get("label") or ", ".join(CONCEPTS.label(key) for key in topic["concepts"])}
            for member in topic["members"]:
                if member in index:
                    index[member]["community"] = community
        edge_list = list(edges.values())
        if hops:
            nodes, edge_list = self._ego_network(nodes, edge_list, focus, hops, min(limit, GRAPH_FOCUS_LIMIT))
        nodes.sort(key=lambda node: (node["kind"] != "memory", node["id"]))
        result = {"nodes": nodes, "edges": sorted(edge_list, key=lambda e: (e["kind"], e["source"], e["target"])),
                  "total_memories": total,
                  "truncated": total > len(chosen) if not hops else len(nodes) >= min(limit, GRAPH_FOCUS_LIMIT),
                  "similarity_available": similarity_available}
        if hops:
            result["focus"] = focus
            result["hops"] = hops
        if mindmap:
            result["mindmap"] = self.mindmap(chosen, topics)
        return result

    @staticmethod
    def _ego_network(nodes, edges, focus, hops, cap):
        """The focus, its neighbours and (with two hops) theirs, strongest paths
        first, cut at `cap` nodes. A hub (workspace or concept) is a neighbour
        but is not walked through: it links everything it names."""
        kinds = {node["id"]: node["kind"] for node in nodes}
        if focus not in kinds:
            return [], []
        adjacent = {}
        for edge in edges:
            adjacent.setdefault(edge["source"], []).append((edge["target"], edge["strength"]))
            adjacent.setdefault(edge["target"], []).append((edge["source"], edge["strength"]))
        best = {focus: (0, 1.0)}
        frontier = [focus]
        for depth in range(1, hops + 1):
            reached = []
            for current in frontier:
                if current != focus and kinds.get(current) != "memory":
                    continue
                for other, strength in adjacent.get(current, ()):
                    score = best[current][1] * max(0.05, strength)
                    known = best.get(other)
                    if known is None:
                        best[other] = (depth, score)
                        reached.append(other)
                    elif known[0] == depth and score > known[1]:
                        best[other] = (depth, score)
            frontier = sorted(set(reached))
        order = sorted(best, key=lambda identifier: (best[identifier][0], -best[identifier][1], identifier))[:cap]
        keep = set(order)
        return ([node for node in nodes if node["id"] in keep],
                [edge for edge in edges if edge["source"] in keep and edge["target"] in keep])

    def _similar_pairs(self, vectors, threshold):
        """Each memory's eight closest neighbours above the threshold. Pair scores
        are kept until the next full vector reload, so a graph whose node set
        shifted slightly (weights decay) only scores its new pairs."""
        scores = self._similarity_cache.setdefault("scores", {})
        ids = sorted(vectors)
        pairs = []
        multiply = operator.mul
        for i, a in enumerate(ids):
            left = vectors[a]
            best = []
            for b in ids[i + 1:]:
                score = scores.get((a, b))
                if score is None:
                    score = scores[(a, b)] = sum(map(multiply, left, vectors[b]))
                if score >= threshold:
                    best.append((score, b))
            for score, b in sorted(best, reverse=True)[:8]:
                pairs.append((a, b, score))
        if len(scores) > 400000:
            scores.clear()
        return pairs

    @staticmethod
    def mindmap(memories, topics=None):
        """Memory → type → leading tag → memory, a memory browser's
        long-term tree as a radial map, and Memory → topics → topic → memory
        for concept communities. A memory appears in both, so a topic leaf's id
        is `topic:<n>:<memory id>` and names the memory in `memory_id`."""
        root = {"id": "root", "label": "Memory", "kind": "root", "children": []}
        for kind in TYPE_ORDER:
            members = [memory for memory in memories if memory["memory_type"] == kind]
            if not members:
                continue
            branch = {"id": "type:" + kind, "label": kind, "kind": "type", "count": len(members), "children": []}
            groups = {}
            for memory in members:
                tag = (memory.get("tags") or ["untagged"])[0]
                groups.setdefault(tag, []).append(memory)
            for tag in sorted(groups, key=lambda name: (-len(groups[name]), name)):
                leaves = sorted(groups[tag], key=lambda memory: (-memory["effective_weight"], memory["id"]))
                branch["children"].append({
                    "id": "tag:" + kind + ":" + tag, "label": tag, "kind": "tag", "count": len(leaves),
                    "children": [{"id": memory["id"], "label": memory.get("title", ""), "kind": "memory",
                                  "memory_type": kind, "effective_weight": memory["effective_weight"],
                                  "expiry_candidate": memory["expiry_candidate"]} for memory in leaves]})
            root["children"].append(branch)
        by_id = {memory["id"]: memory for memory in memories}
        branch = {"id": "topics", "label": "Topics", "kind": "topics", "children": []}
        for number, topic in enumerate(topics or [], 1):
            leaves = sorted((by_id[member] for member in topic["members"] if member in by_id),
                            key=lambda memory: (-memory["effective_weight"], memory["id"]))
            topic_id = topic.get("id") or "topic:%d" % number
            branch["children"].append({
                "id": topic_id, "label": topic.get("label") or ", ".join(CONCEPTS.label(key) for key in topic["concepts"]),
                "kind": "topic", "count": len(leaves), "concepts": topic["concepts"], "summary": topic.get("summary"),
                "children": [{"id": "%s:%s" % (topic_id, memory["id"]), "memory_id": memory["id"],
                              "label": memory.get("title", ""), "kind": "memory", "memory_type": memory["memory_type"],
                              "effective_weight": memory["effective_weight"],
                              "expiry_candidate": memory["expiry_candidate"]} for memory in leaves]})
        if branch["children"]:
            branch["count"] = sum(topic["count"] for topic in branch["children"])
            root["children"].append(branch)

        def count_children(node):
            # A closed branch shows how many children it holds.
            children = node.get("children")
            if children:
                node["children_count"] = len(children)
                for child in children:
                    count_children(child)
        count_children(root)
        return root

    # --- Digest (mid-term layer) ---

    def digest(self, arguments, now=None):
        now = time.time() if now is None else now
        workspace = arguments.get("workspace")
        if workspace is not None and (not isinstance(workspace, str) or not 0 < len(workspace) <= 4096):
            raise HippocampusError("workspace must be a path")
        key = DIGEST_PREFIX + workspace_key(workspace)
        stored = self.store._document_value(key)
        memories = [memory for memory in self._all(now)
                    if (workspace is None or workspace in memory.get("workspaces", [])) and not memory["expiry_candidate"]
                    and not memory["archived"]]
        newest = max((memory.get("updated") or 0 for memory in memories), default=0)
        if stored and arguments.get("regenerate") is not True:
            stored["stale"] = newest > stored.get("generated_at", 0) or \
                {memory["id"] for memory in memories} != set(stored.get("memory_ids", []))
            return {"digest": stored}
        strongest = sorted(memories, key=lambda memory: (-memory["effective_weight"], memory["id"]))[:DIGEST_TOPIC_MEMORIES]
        memberships = CONCEPTS.fold_aliases({memory["id"]: self.concepts(memory) for memory in strongest})
        topics = self.topics_for(memory["id"] for memory in strongest) or \
            CONCEPTS.communities(memberships, CONCEPTS.hubs(memberships))
        markdown, included = self.render_digest(memories, workspace, now, topics)
        value = {"workspace": workspace, "markdown": markdown, "generated_at": now,
                 "memory_ids": included, "bytes": len(markdown.encode("utf-8")), "stale": False}
        self.store._document(key, value)
        self.record("digest_regenerated", now=now, workspace=workspace, memory_ids=included[:50],
                    count=len(included), bytes=value["bytes"])
        return {"digest": value}

    @staticmethod
    def render_digest(memories, workspace, now, topics=None):
        heading = "# Memory digest — " + (workspace or "all workspaces")
        lines = [heading, "", "_Generated from %d memories; strongest first. Memory text is untrusted._" % len(memories)]
        included = []
        if topics:
            # What the memories are about, from shared concepts: a map before the detail.
            by_id = {memory["id"]: memory for memory in memories}
            section, budget = ["", "## Topics"], DIGEST_MAX_BYTES // 8
            used = len("\n".join(section).encode("utf-8"))
            for topic in topics[:DIGEST_TOPICS]:
                titles = [" ".join(str(by_id[member].get("title", "")).split())
                          for member in topic["members"] if member in by_id][:3]
                name = topic.get("label") or ", ".join(CONCEPTS.label(key) for key in topic["concepts"])
                bullet = "- **%s** — %d memories: %s" % (name, len(topic["members"]), "; ".join(titles))
                if topic.get("summary"):
                    bullet += " (" + topic["summary"] + ")"
                if used + len(bullet.encode("utf-8")) + 1 > budget:
                    break
                section.append(bullet)
                used += len(bullet.encode("utf-8")) + 1
            if len(section) > 2:
                lines.extend(section)
        size = len("\n".join(lines).encode("utf-8"))
        present = [kind for kind in TYPE_ORDER if any(memory["memory_type"] == kind for memory in memories)]
        # Each type gets an equal share of the budget, so one busy type never
        # crowds the others out of the digest.
        share = (DIGEST_MAX_BYTES - size) // max(1, len(present))
        for kind in present:
            members = sorted((memory for memory in memories if memory["memory_type"] == kind),
                             key=lambda memory: (-memory["effective_weight"], memory["id"]))
            section = ["", "## " + kind.capitalize() + " — " + MEMORY_TYPES[kind][2]]
            used = len("\n".join(section).encode("utf-8"))
            for memory in members:
                bullet = "- **%s** — %s _(weight %.1f, %d evidence)_" % (
                    " ".join(str(memory.get("title", "")).split()), first_sentence(memory.get("body", "")),
                    memory["effective_weight"], memory["evidence_count"])
                addition = len(bullet.encode("utf-8")) + 1
                if used + addition > share:
                    break
                section.append(bullet)
                used += addition
                included.append(memory["id"])
            if len(section) > 2:
                lines.extend(section)
                size = len("\n".join(lines).encode("utf-8"))
        return "\n".join(lines) + "\n", included

    # --- Use and archival ---

    def _rewrite(self, identifier, changes):
        """Write fields into a memory's document without a revision or a new weight."""
        row = self.connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return False
        memory = self.store._memory(row)
        stored = self._stored(identifier) or {}
        value = dict(stored)
        value.update({key: memory[key] for key in ("id", "title", "body", "sources", "tags", "revision",
                                                   "created", "updated")})
        for evidence in value["sources"]:
            evidence.pop("available", None)
        for key, item in changes.items():
            if item is None:
                value.pop(key, None)
            else:
                value[key] = item
        self.store._put_memory(value)
        return True

    def touch(self, identifier, now=None):
        """Opening or citing a memory marks it used (written at most hourly), which
        keeps it out of archival."""
        now = time.time() if now is None else now
        stored = self._stored(identifier) or {}
        used = stored.get("last_used")
        if isinstance(used, (int, float)) and not isinstance(used, bool) and now - used < LAST_USED_WRITE_SECONDS:
            return False
        if self._rewrite(identifier, {"last_used": now}):
            self.connection.commit()
            return True
        return False

    def set_archived(self, identifier, archived, now=None, actor=None, reason=None):
        now = time.time() if now is None else now
        row = self.connection.execute("SELECT title FROM memories WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise HippocampusError("Memory not found", "not_found")
        stored = self._stored(identifier) or {}
        if bool(attributes(stored, now)["archived"]) == archived:
            return False
        # A restore counts as a use, so the memory is not archived again the next day.
        self._rewrite(identifier, {"archived_at": now} if archived else {"archived_at": None, "last_used": now})
        self.record("memory_archived" if archived else "memory_restored", now=now, memory_id=identifier,
                    title=row["title"], actor=actor, reason=reason)
        return True

    def maybe_archive(self, now=None, force=False):
        """The daily archival sweep, a bounded number of changes per tick: fading,
        unused memories are archived, and archived ones unused for another 90 days
        are deleted. Returns the changes made, or None when the sweep is not due."""
        now = time.time() if now is None else now
        sweep = self.store._document_value(ARCHIVE_SWEEP_DOCUMENT) or {}
        if not force and not sweep.get("pending") and now - (sweep.get("at") or 0) < ARCHIVE_SWEEP_SECONDS:
            return None
        changes = {"archived": 0, "deleted": 0}
        due = [(memory, archive_action(memory, now)) for memory in self._all(now, workspaces=False)]
        due = [(memory, action) for memory, action in due if action]
        for memory, action in due[:ARCHIVE_CHANGES_PER_TICK]:
            if action == "archive":
                self.set_archived(memory["id"], True, now,
                                  reason="Fading and unused for %d days" % ARCHIVE_UNUSED_DAYS)
                changes["archived"] += 1
            else:
                self.store.delete_memory({"id": memory["id"], "reason": "archived_unused"})
                changes["deleted"] += 1
        self.store._document(ARCHIVE_SWEEP_DOCUMENT, {"at": now, "pending": len(due) > ARCHIVE_CHANGES_PER_TICK,
                                                      "last_changes": changes})
        self.connection.commit()
        return changes

    # --- Attribute changes ---

    def update(self, arguments, now=None):
        now = time.time() if now is None else now
        identifier = arguments.get("id")
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 128:
            raise HippocampusError("id must be a memory ID")
        row = self.connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise HippocampusError("Memory not found", "not_found")
        memory = self.store._memory(row)
        stored = self._stored(identifier) or memory
        before = attributes({**stored, "sources": memory["sources"]}, now)
        kind = memory_type(arguments.get("memory_type")) or before["memory_type"]
        weight = weight_value(arguments.get("weight"))
        pinned = arguments.get("pinned")
        if pinned is not None and not isinstance(pinned, bool):
            raise HippocampusError("pinned must be true or false")
        reinforce = arguments.get("reinforce") is True
        archived = arguments.get("archived")
        if archived is not None and not isinstance(archived, bool):
            raise HippocampusError("archived must be true or false")
        if archived is not None and archived != before["archived"]:
            # Archiving or restoring is its own change: no weight or type moves with it.
            self.set_archived(identifier, archived, now, actor=self.store._actor(arguments))
            self.connection.commit()
            return {"memory": self.enrich(self.store._memory(self.connection.execute(
                "SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()), now), "changed": True}
        after = {"memory_type": kind, "weight": before["weight"], "evidence_count": before["evidence_count"],
                 "lineage": before["lineage"], "pinned": before["pinned"] if pinned is None else pinned,
                 "last_reinforced_at": before["last_reinforced_at"]}
        if reinforce:
            after["weight"] = round(min(MAX_WEIGHT, before["effective_weight"] + REINFORCE_STEP), 4)
            after["last_reinforced_at"] = now
        if weight is not None:
            after["weight"] = weight
            after["last_reinforced_at"] = now
        if kind != before["memory_type"] and weight is None and not reinforce:
            after["weight"] = MEMORY_TYPES[kind][0]
            after["last_reinforced_at"] = now
        changes = {key: [before[key], after[key]] for key in ("memory_type", "weight", "pinned")
                   if before[key] != after[key]}
        if not changes and not reinforce:
            return {"memory": self.enrich(memory, now), "changed": False}
        value = {key: memory[key] for key in ("id", "title", "body", "sources", "tags", "revision", "created", "updated")}
        for evidence in value["sources"]:
            evidence.pop("available", None)
        value.update(after)
        for key in ("archived_at", "last_used", "workspaces"):
            if stored.get(key) is not None:
                value[key] = stored[key]
        if reinforce:
            value["last_used"] = now
        self.store._put_memory(value)
        self.record("memory_attributes", now=now, memory_id=identifier, title=memory["title"], changes=changes,
                    reinforced=reinforce or None, actor=self.store._actor(arguments))
        self.connection.commit()
        return {"memory": self.enrich(self.store._memory(self.connection.execute(
            "SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()), now), "changed": True}

    # --- Stable topics and merge review (background memory agent) ---

    def stable_topics(self, now=None, refresh=False):
        """The stored topic set, recomputed when missing, a day old or forced.
        Graph, mind map and digest read it; only this method writes it."""
        now = time.time() if now is None else now
        stored = self.store._document_value(TOPICS_DOCUMENT) or {}
        if not refresh and stored.get("topics") is not None and \
                now - (stored.get("computed_at") or 0) < TOPICS.RECOMPUTE_SECONDS:
            return stored["topics"]
        memories = [memory for memory in self._all(now, workspaces=False) if not memory["archived"]]
        memberships = CONCEPTS.fold_aliases({memory["id"]: self.concepts(memory) for memory in memories})
        current = CONCEPTS.communities(memberships, CONCEPTS.hubs(memberships), limit=STABLE_TOPIC_LIMIT)
        topics = TOPICS.carry_over(stored.get("topics"), current)
        self.store._document(TOPICS_DOCUMENT, {"computed_at": now, "topics": topics})
        self.connection.commit()
        return topics

    def stored_topics(self):
        """The last computed topics without recomputing (None before the first run)."""
        stored = self.store._document_value(TOPICS_DOCUMENT) or {}
        return stored.get("topics")

    def topics_for(self, memory_ids):
        """Stored topics restricted to `memory_ids`, in the communities() shape plus
        id, name and summary; None when no topics were computed yet."""
        topics = self.stored_topics()
        if topics is None:
            return None
        chosen, result = set(memory_ids), []
        for topic in topics:
            members = [member for member in topic["members"] if member in chosen]
            if len(members) >= 2:
                result.append({"id": topic["id"], "members": members, "concepts": topic.get("concepts", []),
                               "label": TOPICS.label(topic), "summary": topic.get("summary")})
        result.sort(key=lambda topic: (-len(topic["members"]), topic["id"]))
        return result

    def memory_topics(self, arguments, now=None):
        now = time.time() if now is None else now
        limit = bounded(arguments.get("limit"), TOPICS.TOPICS_PER_RUN, TOPICS.TOPICS_PER_RUN)
        topics = self.stable_topics(now)
        by_id = {memory["id"]: memory for memory in self._all(now, workspaces=False)}
        result, response_bytes = [], 0
        queue = TOPICS.naming_queue(topics, limit)
        for topic in queue:
            members = sorted((by_id[member] for member in topic["members"] if member in by_id),
                             key=lambda memory: (-memory["effective_weight"], memory["id"]))
            item = {"id": topic["id"], "size": len(topic["members"]), "current_name": topic.get("name"),
                    "concepts": [CONCEPTS.label(key) for key in topic.get("concepts", [])],
                    "examples": [{"title": first_sentence(memory.get("title", ""), 120),
                                  "excerpt": first_sentence(memory.get("body", ""), 120)}
                                 for memory in members[:TOPICS.TOPIC_EXEMPLARS]]}
            item_bytes = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            # Harnesses cut long tool results; topics that do not fit wait for the next call.
            if result and response_bytes + item_bytes > TOPIC_RESPONSE_BYTES:
                break
            result.append(item)
            response_bytes += item_bytes
        return {"topics": result, "total_topics": len(topics), "remaining": len(queue) - len(result)}

    def name_topic(self, arguments, now=None):
        now = time.time() if now is None else now
        identifier = arguments.get("id")
        if not isinstance(identifier, str) or not identifier.startswith("topic:") or len(identifier) > 64:
            raise HippocampusError("id must be a topic ID")
        stored = self.store._document_value(TOPICS_DOCUMENT) or {}
        topics = stored.get("topics") or []
        try:
            topic = TOPICS.apply_name(topics, identifier, arguments.get("name"), arguments.get("summary", ""), now)
        except KeyError:
            raise HippocampusError("Topic not found", "not_found")
        except ValueError as error:
            raise HippocampusError(str(error))
        stored["topics"] = topics
        self.store._document(TOPICS_DOCUMENT, stored)
        self.record("topic_named", now=now, topic_id=identifier, name=topic["name"], size=len(topic["members"]),
                    actor=self.store._actor(arguments))
        self.connection.commit()
        return {"topic": {"id": identifier, "name": topic["name"], "summary": topic["summary"],
                          "size": len(topic["members"])}}

    def merge_candidates(self, arguments, now=None):
        now = time.time() if now is None else now
        limit = bounded(arguments.get("limit"), TOPICS.MERGES_PER_RUN, TOPICS.MERGES_PER_RUN)
        memories = {memory["id"]: memory for memory in self._all(now, workspaces=False) if not memory["archived"]}
        vectors = self._memory_vectors(set(memories))
        if not vectors:
            return {"pairs": [], "similarity_available": False}
        identifiers = sorted(identifier for identifier in memories if identifier in vectors)
        decided = (self.store._document_value(MERGES_DOCUMENT) or {}).get("decided", {})
        if not identifiers:
            return {"pairs": [], "similarity_available": True}
        # The matrix is rebuilt only when the vector cache or the memory set changed.
        key = (id(self._vector_cache), self._vector_cache_at, tuple(identifiers))
        cached = getattr(self, "_merge_matrix", None)
        if cached is not None and cached[0] == key:
            matrix = cached[1]
        else:
            matrix = VECTORS.VectorMatrix(len(vectors[identifiers[0]]))
            for identifier in identifiers:
                matrix.upsert(identifier, identifier, 0, "", vectors[identifier])
            self._merge_matrix = (key, matrix)

        def neighbours(identifier):
            return [(item[1], item[0]) for item in matrix.top(vectors[identifier], 6)]
        pairs = TOPICS.merge_candidates(identifiers, neighbours, decided, limit)
        result = []
        for pair in pairs:
            items = []
            for identifier in pair["pair"]:
                memory = memories[identifier]
                items.append({"id": identifier, "title": memory.get("title", ""), "body": memory.get("body", "")[:1200],
                              "memory_type": memory["memory_type"], "updated": memory.get("updated"),
                              "source_count": len(memory.get("sources") or [])})
            result.append({"memories": items, "score": pair["score"]})
        return {"pairs": result, "similarity_available": True}

    def decide_merge(self, arguments, now=None):
        now = time.time() if now is None else now
        decision = arguments.get("decision")
        if decision not in TOPICS.DECISIONS:
            raise HippocampusError("decision must be one of: " + ", ".join(TOPICS.DECISIONS))
        keep, drop = arguments.get("keep"), arguments.get("drop")
        for name, value in (("keep", keep), ("drop", drop)):
            if not isinstance(value, str) or not 0 < len(value) <= 128:
                raise HippocampusError(name + " must be a memory ID")
        if keep == drop:
            raise HippocampusError("keep and drop must differ")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not 0 < len(reason) <= 500:
            raise HippocampusError("reason must be a short text")
        rows = {row["id"]: row for row in self.connection.execute(
            "SELECT * FROM memories WHERE id IN (?,?)", (keep, drop)).fetchall()}
        if keep not in rows or drop not in rows:
            raise HippocampusError("Memory not found", "not_found")
        actor = self.store._actor(arguments)
        if decision == "merge":
            kept = self.store._memory(rows[keep])
            dropped = self.store._memory(rows[drop])
            sources = [{"session_id": item["session_id"], "message_id": item["message_id"]}
                       for item in dropped["sources"]][:50]
            title = arguments.get("title") or kept["title"]
            body = arguments.get("body") or kept["body"]
            update = {"id": keep, "title": title, "body": body, "tags": kept.get("tags") or [],
                      "keep_sources": True, "reason": ("Merged " + drop + ": " + reason)[:500],
                      "run_id": arguments.get("run_id"), "agent": arguments.get("agent")}
            if sources:
                update["sources"] = sources
            else:
                update["sources"] = [{"session_id": item["session_id"], "message_id": item["message_id"]}
                                     for item in kept["sources"]][:50]
            self.store.upsert_memory({key: value for key, value in update.items() if value is not None})
        if decision in ("merge", "supersede"):
            self.set_archived(drop, True, now, actor=actor,
                              reason=("Merged into " if decision == "merge" else "Superseded by ") + keep)
        stored = self.store._document_value(MERGES_DOCUMENT) or {}
        decided = stored.get("decided") or {}
        decided[TOPICS.pair_key(keep, drop)] = {"decision": decision, "at": now}
        stored["decided"] = decided
        self.store._document(MERGES_DOCUMENT, stored)
        self.record("merge_decided", now=now, memory_id=keep, other_id=drop, decision=decision, reason=reason,
                    actor=actor)
        self.connection.commit()
        return {"decision": decision, "keep": keep, "drop": drop}
