"""Stable topics and near-duplicate pairs for the background memory agent.

Topics come from omni_concepts.communities over every non-archived memory, so
their membership no longer depends on which memories happen to be strongest
today. A recomputed topic keeps the id of the previous topic it overlaps most
(member Jaccard >= CARRY_JACCARD); otherwise it gets a new id from its members.
A topic needs a name when it has none or its members drifted (Jaccard between
the members it was named from and its current members < NAME_JACCARD).

Merge candidates are pairs of memories whose vectors score >= MERGE_COSINE and
that no earlier decision covered. Everything here is pure: the caller supplies
the memberships, vectors and stored state, and persists what comes back.
"""

import hashlib
import time


CARRY_JACCARD = 0.5
NAME_JACCARD = 0.7          # Renamed once more than ~30% of members changed.
MERGE_COSINE = 0.85
TOPIC_EXEMPLARS = 8
TOPICS_PER_RUN = 20
MERGES_PER_RUN = 10
RECOMPUTE_SECONDS = 86400
NAME_CHARACTERS = 60
SUMMARY_CHARACTERS = 280
DECISIONS = ("merge", "supersede", "keep")


def jaccard(left, right):
    left, right = set(left), set(right)
    if not left and not right:
        return 1.0
    return len(left & right) / float(len(left | right))


def topic_id(members):
    digest = hashlib.sha1("\n".join(sorted(members)).encode("utf-8")).hexdigest()
    return "topic:" + digest[:12]


def carry_over(previous, current):
    """Give each current community (dicts with `members`, `concepts`) an id.

    `previous` is the stored topic list. Pairs are matched greedily by highest
    Jaccard, ties broken by ids, so the outcome does not depend on input order.
    A carried topic keeps its name, summary and the members it was named from."""
    previous = [topic for topic in previous or () if isinstance(topic, dict) and topic.get("id")]
    candidates = []
    for index, community in enumerate(current):
        for old in previous:
            score = jaccard(community["members"], old.get("members", ()))
            if score >= CARRY_JACCARD:
                candidates.append((-score, old["id"], index))
    candidates.sort()
    taken_ids, assigned = set(), {}
    for _, old_id, index in candidates:
        if old_id in taken_ids or index in assigned:
            continue
        taken_ids.add(old_id)
        assigned[index] = old_id
    by_id = {topic["id"]: topic for topic in previous}
    topics = []
    for index, community in enumerate(current):
        members = sorted(community["members"])
        topic = {"id": assigned.get(index) or topic_id(members), "members": members,
                 "concepts": list(community.get("concepts", ())), "name": None, "summary": None,
                 "named_members": [], "named_at": None}
        old = by_id.get(assigned.get(index))
        if old:
            for key in ("name", "summary", "named_members", "named_at"):
                topic[key] = old.get(key, topic[key])
        topics.append(topic)
    return topics


def fallback_label(topic):
    concepts = [key.split(":", 1)[-1] for key in topic.get("concepts", ())]
    return ", ".join(concepts[:3]) or "Topic"


def label(topic):
    return topic.get("name") or fallback_label(topic)


def needs_name(topic):
    if not topic.get("name"):
        return True
    return jaccard(topic.get("named_members", ()), topic.get("members", ())) < NAME_JACCARD


def naming_queue(topics, limit=TOPICS_PER_RUN):
    """Topics to name, largest first."""
    waiting = [topic for topic in topics if needs_name(topic)]
    waiting.sort(key=lambda topic: (-len(topic["members"]), topic["id"]))
    return waiting[:limit]


def apply_name(topics, identifier, name, summary, now=None):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Topic name is required")
    if not isinstance(summary, str):
        raise ValueError("Topic summary must be text")
    name = " ".join(name.split())[:NAME_CHARACTERS]
    summary = " ".join(summary.split())[:SUMMARY_CHARACTERS]
    for topic in topics:
        if topic["id"] == identifier:
            topic["name"], topic["summary"] = name, summary
            topic["named_members"] = list(topic["members"])
            topic["named_at"] = time.time() if now is None else now
            return topic
    raise KeyError(identifier)


def pair_key(left, right):
    return "\n".join(sorted((left, right)))


def merge_candidates(memory_ids, neighbours, decided, limit=MERGES_PER_RUN, threshold=MERGE_COSINE):
    """Undecided pairs scoring >= threshold, best first.

    `neighbours(memory_id)` returns [(other_id, cosine)] for that memory; it is
    asked once per memory, in sorted id order, so the scan is deterministic."""
    known = set(memory_ids)
    found = {}
    for memory in sorted(known):
        for other, score in neighbours(memory) or ():
            if other == memory or other not in known or score < threshold:
                continue
            key = pair_key(memory, other)
            if key in decided:
                continue
            if score > found.get(key, -1.0):
                found[key] = score
    ranked = sorted(found.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"pair": key.split("\n"), "score": round(score, 4)} for key, score in ranked]
