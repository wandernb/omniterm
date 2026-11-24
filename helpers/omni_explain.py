"""Why a memory exists and weighs what it does: one read-only explanation per memory.

The "Why this memory?" view (memo @2026-11-11-01 AI-001) gathers what the store
already records about a memory into one answer: how its weight was set, decays
and when it would expire or be archived; the sessions its evidence comes from;
how it formed (the journal); its UPDATE lineage in both directions; the stable
topics and concepts it belongs to; the memories it links to and why; and the
weekly summaries that cite it. Nothing here writes, so explaining a memory never
counts as using it.

Memory text and transcript excerpts are untrusted data. Nothing here interprets
them as instructions.
"""

import time

DAY = 86400.0
EXPLAIN_SESSIONS = 20
EXPLAIN_EVENTS = 30
EXPLAIN_EDGES = 20
EXPLAIN_RELATIVES = 20
EDGE_KINDS = ("session", "tag", "lineage", "similarity")
WEIGHT_EVENT_KINDS = ("memory_created", "memory_revised", "memory_attributes")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def weight_story(memory, events, archive, now):
    """The weight's history and future: points from the journal, today's decayed
    value, and when the memory reaches the expiry threshold and archival."""
    pinned = memory["pinned"]
    rate = memory["decay_rate"]
    reinforced = _number(memory.get("last_reinforced_at"))
    points = []
    for event in sorted(events, key=lambda item: item.get("at") or 0):
        weight = None
        if event.get("kind") in ("memory_created", "memory_revised"):
            weight = _number(event.get("weight"))
        elif event.get("kind") == "memory_attributes":
            change = (event.get("changes") or {}).get("weight")
            weight = _number(change[1]) if isinstance(change, list) and len(change) == 2 else None
        if weight is not None:
            points.append({"at": event.get("at"), "weight": round(weight, 4), "kind": event.get("kind"),
                           "reinforced": event.get("kind") != "memory_created"})
    points.append({"at": now, "weight": memory["effective_weight"], "kind": "now"})
    expires_at = None
    if not pinned and rate > 0 and reinforced is not None:
        expires_at = reinforced + max(0.0, memory["weight"] - archive["threshold"]) / rate * DAY
    article = "an" if memory["memory_type"][0] in "aeiou" else "a"
    reasons = ["Starts at %.1f as %s %s memory (%s)." % (memory["base_weight"], article, memory["memory_type"],
                                                          memory["type_description"])]
    reinforcements = sum(1 for point in points if point.get("reinforced") and point["kind"] != "now")
    if reinforcements:
        reasons.append("Reinforced %d time%s by revisions, new evidence or by hand." % (reinforcements, "" if reinforcements == 1 else "s"))
    if pinned:
        reasons.append("Pinned, so it does not decay.")
    elif reinforced is not None:
        days = max(0.0, (now - reinforced) / DAY)
        reasons.append("Decays %.2f a day; %.1f days since it was last reinforced." % (rate, days))
        if memory["expiry_candidate"]:
            reasons.append("Below the %.1f expiry threshold: an expiry candidate." % archive["threshold"])
        elif expires_at is not None:
            reasons.append("Reaches the %.1f expiry threshold in %.0f days unless reinforced." %
                           (archive["threshold"], max(0.0, (expires_at - now) / DAY)))
    if archive["exempt"]:
        reasons.append(archive["exempt"])
    return {"memory_type": memory["memory_type"], "base_weight": memory["base_weight"], "decay_rate": rate,
            "weight": memory["weight"], "effective_weight": memory["effective_weight"], "pinned": pinned,
            "last_reinforced_at": reinforced, "expiry_threshold": archive["threshold"],
            "expiry_candidate": memory["expiry_candidate"], "expires_at": expires_at,
            "points": points[-EXPLAIN_EVENTS:], "reasons": reasons}


def archive_story(memory, hippocampus_module, now):
    """Whether and when the archival sweep would take this memory."""
    threshold = hippocampus_module.EXPIRY_THRESHOLD
    sources = len(memory.get("sources") or [])
    exempt = None
    if memory["pinned"]:
        exempt = "Never archived: pinned."
    elif sources >= hippocampus_module.ARCHIVE_EXEMPT_SOURCES:
        exempt = "Never archived: %d sources (archival spares memories with %d or more)." % (
            sources, hippocampus_module.ARCHIVE_EXEMPT_SOURCES)
    last = hippocampus_module.last_activity(memory)
    return {"threshold": threshold, "exempt": exempt, "archived": memory["archived"],
            "archived_at": memory.get("archived_at"), "last_used": memory.get("last_used"),
            "last_activity": last or None, "unused_days": round(max(0.0, now - last) / DAY, 1) if last else None,
            "archive_after_unused_days": hippocampus_module.ARCHIVE_UNUSED_DAYS,
            "delete_after_archived_days": hippocampus_module.ARCHIVE_DELETE_DAYS,
            "next_action": hippocampus_module.archive_action(memory, now)}


def edge_reason(edge, other_title):
    kind = edge["kind"]
    if kind == "session":
        reason = "Cites evidence from the same session%s" % ("s" if edge["strength"] < 1 else "")
    elif kind == "tag":
        reason = "Shares tags: " + (edge.get("label") or "")
    elif kind == "lineage":
        reason = "UPDATE lineage"
    elif kind == "similarity":
        reason = "Similar text (cosine %.2f)" % edge["strength"]
    else:
        reason = kind
    return {"id": None, "title": other_title, "kind": kind, "strength": edge["strength"], "reason": reason}


class Explainer:
    def __init__(self, store, hippocampus_module):
        self.store = store
        self.module = hippocampus_module

    @property
    def hippocampus(self):
        return self.store.hippocampus

    @property
    def connection(self):
        return self.store.connection

    def explain(self, arguments, now=None):
        now = time.time() if now is None else now
        identifier = arguments.get("id")
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 128:
            raise self.module.HippocampusError("id must be a memory ID")
        row = self.connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise self.module.HippocampusError("Memory not found", "not_found")
        hippocampus = self.hippocampus
        memory = hippocampus.enrich(self.store._memory(row), now)
        memory["type_description"] = self.module.MEMORY_TYPES[memory["memory_type"]][2]
        stored = hippocampus._stored(identifier) or {}
        events = hippocampus.events({"memory_id": identifier, "limit": 200})["events"]
        archive = archive_story(memory, self.module, now)
        result = {"id": identifier, "title": memory.get("title", ""), "memory_type": memory["memory_type"],
                  "created": memory.get("created"), "updated": memory.get("updated"),
                  "revision": memory.get("revision"), "tags": memory.get("tags", []),
                  "workspaces": memory.get("workspaces", []),
                  "weight": weight_story(memory, [event for event in events if event.get("kind") in WEIGHT_EVENT_KINDS],
                                         archive, now),
                  "archival": archive,
                  "recall": {"last_used": memory.get("last_used"),
                             "recall_count": stored.get("recall_count") if _number(stored.get("recall_count")) else 0},
                  "sources": self.sources(memory),
                  "formation": self.formation(events),
                  "lineage": self.lineage(memory, now),
                  "topics": self.topics(identifier),
                  "concepts": [self.module.CONCEPTS.label(key) for key in hippocampus.concepts(memory)][:20],
                  "links": self.links(identifier, now)}
        summary = stored.get("summary")
        if isinstance(summary, dict):
            result["summarizes"] = {key: summary.get(key) for key in ("project", "week", "start", "end", "cites")}
        result["cited_by_summaries"] = self.citing_summaries(identifier)
        result["why"] = self.headline(result)
        return {"explanation": result}

    def sources(self, memory):
        """Evidence grouped by session, with what each session was and when."""
        sessions = {}
        for source in memory.get("sources") or []:
            entry = sessions.setdefault(source["session_id"], {"session_id": source["session_id"], "messages": 0,
                                                               "available": 0, "roles": [], "first": None,
                                                               "last": None})
            entry["messages"] += 1
            message = self.connection.execute("SELECT role,timestamp,workspace,provider FROM messages "
                                              "WHERE id=? AND session_id=?",
                                              (source["message_id"], source["session_id"])).fetchone()
            if message is None:
                continue
            entry["available"] += 1
            if message["role"] not in entry["roles"]:
                entry["roles"].append(message["role"])
            entry.setdefault("workspace", message["workspace"])
            entry.setdefault("provider", message["provider"])
            stamp = message["timestamp"]
            if stamp:
                entry["first"] = stamp if entry["first"] is None or stamp < entry["first"] else entry["first"]
                entry["last"] = stamp if entry["last"] is None or stamp > entry["last"] else entry["last"]
        for entry in sessions.values():
            row = self.connection.execute("SELECT title,provider,workspace FROM sources WHERE session_id=? "
                                          "ORDER BY title IS NULL LIMIT 1", (entry["session_id"],)).fetchone()
            if row is not None:
                entry["session_title"] = row["title"]
                entry.setdefault("provider", row["provider"])
                if not entry.get("workspace"):
                    entry["workspace"] = row["workspace"]
        ordered = sorted(sessions.values(), key=lambda entry: (-entry["messages"], entry["session_id"]))
        return {"sessions": ordered[:EXPLAIN_SESSIONS], "session_count": len(ordered),
                "evidence_count": memory["evidence_count"],
                "missing": sum(entry["messages"] - entry["available"] for entry in ordered)}

    def formation(self, events):
        """How the memory formed, newest first, with who did each step."""
        counts = {}
        for event in events:
            counts[event["kind"]] = counts.get(event["kind"], 0) + 1
        created = next((event for event in reversed(events) if event.get("kind") == "memory_created"), None)
        steps = []
        for event in events[:EXPLAIN_EVENTS]:
            step = {key: event.get(key) for key in ("id", "kind", "at", "actor", "reason", "changed", "decision",
                                                    "other_id") if event.get(key) is not None}
            steps.append(step)
        return {"created_by": created.get("actor") if created else None,
                "created_reason": created.get("reason") if created else None,
                "counts": counts, "steps": steps, "journal_complete": len(events) < 200}

    def lineage(self, memory, now):
        titles = {}
        descendants = []
        for other in self.hippocampus._all(now, workspaces=False):
            titles[other["id"]] = other.get("title", "")
            if memory["id"] in (other.get("lineage") or []):
                descendants.append({"id": other["id"], "title": other.get("title", ""), "archived": other["archived"]})
        ancestors = [{"id": ancestor, "title": titles.get(ancestor), "exists": ancestor in titles}
                     for ancestor in memory.get("lineage") or []]
        return {"ancestors": ancestors[:EXPLAIN_RELATIVES], "descendants": descendants[:EXPLAIN_RELATIVES]}

    def topics(self, identifier):
        found = []
        for topic in self.hippocampus.stored_topics() or []:
            if identifier in topic.get("members", []):
                found.append({"id": topic.get("id"), "name": topic.get("name"), "summary": topic.get("summary"),
                              "size": len(topic["members"]),
                              "concepts": [self.module.CONCEPTS.label(key) for key in topic.get("concepts", [])][:8]})
        return found

    def links(self, identifier, now):
        graph = self.hippocampus.graph({"focus": identifier, "hops": 1, "limit": 60, "edges": list(EDGE_KINDS)}, now)
        titles = {node["id"]: node.get("label", "") for node in graph["nodes"]}
        links = []
        for edge in graph["edges"]:
            if identifier not in (edge["source"], edge["target"]):
                continue
            other = edge["target"] if edge["source"] == identifier else edge["source"]
            link = edge_reason(edge, titles.get(other, ""))
            link["id"] = other
            links.append(link)
        links.sort(key=lambda link: (-link["strength"], link["kind"], link["id"]))
        return {"items": links[:EXPLAIN_EDGES], "total": len(links),
                "similarity_available": graph.get("similarity_available")}

    def citing_summaries(self, identifier):
        cache = self.hippocampus._memory_cache or {}
        found = []
        for other, entry in cache.items():
            summary = entry[1].get("summary") if isinstance(entry[1], dict) else None
            if isinstance(summary, dict) and identifier in (summary.get("cites") or []):
                found.append({"id": other, "title": entry[0].get("title", ""), "week": summary.get("week"),
                              "project": summary.get("project")})
        found.sort(key=lambda item: (item.get("week") or "", item["id"]), reverse=True)
        return found[:EXPLAIN_RELATIVES]

    @staticmethod
    def headline(result):
        """One plain sentence a person reads first."""
        sources = result["sources"]
        creator = (result["formation"].get("created_by") or {}).get("agent")
        parts = ["%s memory" % result["memory_type"].capitalize()]
        parts.append("from %d evidence message%s in %d session%s" % (
            sources["evidence_count"], "" if sources["evidence_count"] == 1 else "s",
            sources["session_count"], "" if sources["session_count"] == 1 else "s"))
        if creator:
            parts.append("written by " + creator)
        sentence = " ".join(parts) + "; weight %.1f of %.1f." % (result["weight"]["effective_weight"],
                                                                 result["weight"]["base_weight"])
        if result["topics"]:
            named = [topic["name"] for topic in result["topics"] if topic.get("name")]
            if named:
                sentence += " Topic: " + ", ".join(named[:2]) + "."
        if result["links"]["total"]:
            sentence += " Linked to %d other memor%s." % (result["links"]["total"],
                                                          "y" if result["links"]["total"] == 1 else "ies")
        return sentence
