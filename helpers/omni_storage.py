"""Where the knowledge index's bytes are, a soft budget, and a vector-retention pass.

The report reads SQLite's dbstat over the native index once a day on a background
thread with its own read-only connection, so no worker request waits for it (a full
dbstat walk over a 24 GB file takes minutes). The result is cached in a document.
Crossing the budget raises one Inbox item and runs the retention pass, which drops
vectors only: transcript text is never deleted.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

REPORT_DOCUMENT = "metadata:storage_report:v1"
SETTINGS_DOCUMENT = "metadata:storage_settings:v1"
STATE_DOCUMENT = "metadata:storage_state:v1"
REPORT_SECONDS = 86400.0
REPORT_TIMEOUT_SECONDS = 1200.0  # a report that takes longer is abandoned until tomorrow
DEFAULT_BUDGET_GB = 30.0
MAXIMUM_BUDGET_GB = 100000.0
GB = 1024 ** 3
RETENTION_DAYS = 180
RETENTION_SECONDS = 0.25  # per memory tick while a pass runs
RETENTION_BATCH = 200
NOTIFY_DEDUPE_KEY = "knowledge-storage-budget"
CATEGORIES = ("records", "messages", "fts", "vectors", "indexes", "other")


def category(name, kind, table):
    """The report category of one dbstat btree (a table or an index)."""
    if "_fts" in name or "_fts" in (table or ""):
        return "fts"
    if kind == "index":
        return "indexes"
    if table in ("vectors", "semantic_chunks"):
        return "vectors"
    if table == "messages":
        return "messages"
    if table == "records":
        return "records"
    return "other"


def measure(path, deadline_seconds=REPORT_TIMEOUT_SECONDS, clock=time.monotonic, progress_ops=100000):
    """The storage report of one database file, read-only. Blocking: run it off the worker."""
    path = Path(path)
    started = time.time()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    try:
        deadline = clock() + deadline_seconds
        connection.set_progress_handler(lambda: 1 if clock() >= deadline else 0, progress_ops)
        objects = {row[0]: (row[1], row[2]) for row in connection.execute(
            "SELECT name,type,tbl_name FROM sqlite_master WHERE type IN ('table','index')")}
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        freelist = connection.execute("PRAGMA freelist_count").fetchone()[0]
        bytes_by = {name: 0 for name in CATEGORIES}
        tables = {}
        for name, size in connection.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name"):
            kind, table = objects.get(name, ("table", name))
            bytes_by[category(name, kind, table)] += size or 0
            tables[name] = size or 0
    except sqlite3.Error as exc:
        return {"error": str(exc)[:300], "computed_at": time.time(), "path": str(path)}
    finally:
        connection.close()
    largest = sorted(tables.items(), key=lambda item: -item[1])[:12]
    return {"computed_at": time.time(), "seconds": round(time.time() - started, 1), "path": str(path),
            "file_bytes": path.stat().st_size if path.exists() else None,
            "page_size": page_size, "page_count": page_count, "freelist_count": freelist,
            "total_bytes": page_size * page_count, "free_bytes": page_size * freelist,
            "bytes": bytes_by, "largest": [{"name": name, "bytes": size} for name, size in largest]}


class StorageMonitor:
    def __init__(self, store, measure_function=measure):
        self.store = store
        self._measure = measure_function
        self._thread = None
        self._result = None
        self._lock = threading.Lock()
        self._cited = None

    # Settings and report ------------------------------------------------------------------

    def settings(self):
        value = self.store._document_value(SETTINGS_DOCUMENT) or {}
        budget = value.get("budget_gb")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 1 <= budget <= MAXIMUM_BUDGET_GB:
            budget = DEFAULT_BUDGET_GB
        return {"budget_gb": budget}

    def configure(self, arguments):
        budget = arguments.get("budget_gb")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 1 <= budget <= MAXIMUM_BUDGET_GB:
            raise ValueError("budget_gb must be a number from 1 to %d" % MAXIMUM_BUDGET_GB)
        self.store._document(SETTINGS_DOCUMENT, {"budget_gb": budget})
        state = self._state()
        state["notified"] = False  # a new budget is judged afresh
        self.store._document(STATE_DOCUMENT, state)
        return self.status()

    def _path(self):
        path = getattr(self.store, "_read_index", None)
        return Path(path) if path else None

    def _state(self):
        return dict(self.store._document_value(STATE_DOCUMENT) or {})

    def status(self):
        report = self.store._document_value(REPORT_DOCUMENT)
        state = self._state()
        budget = self.settings()["budget_gb"]
        total = report.get("total_bytes") if isinstance(report, dict) else None
        return {"report": report, "computing": self._thread is not None and self._thread.is_alive(),
                "budget_gb": budget, "budget_bytes": int(budget * GB),
                "over_budget": total is not None and total >= budget * GB,
                "retention": {key: state.get(key) for key in ("retention_running", "retention_dropped",
                                                              "retention_for", "retention_finished_at")},
                "available": self._path() is not None, "code_graph_bytes": self._code_graph_bytes()}

    def _code_graph_bytes(self):
        """The code graph's sidecar (derived, rebuildable) beside the index, outside the budget."""
        path = self._path()
        if path is None:
            return None
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += path.with_name("code-graph.sqlite" + suffix).stat().st_size
            except OSError:
                pass
        return total

    def _start(self, path):
        def run():
            result = self._measure(path)
            with self._lock:
                self._result = result
        self._thread = threading.Thread(target=run, name="omni-storage-report", daemon=True)
        self._thread.start()

    # The tick ------------------------------------------------------------------------------

    def tick(self, now=None, semantics=None, cited=None, budget_seconds=RETENTION_SECONDS):
        """One bounded step: collect a finished report, start a due one, judge the budget,
        and advance the retention pass. Returns a notification to raise, or None."""
        now = time.time() if now is None else now
        with self._lock:
            result, self._result = self._result, None
        if result is not None:
            self.store._document(REPORT_DOCUMENT, result)
            self._thread = None
        report = self.store._document_value(REPORT_DOCUMENT)
        path = self._path()
        if (path is not None and self._thread is None and
                (not isinstance(report, dict) or now - (report.get("computed_at") or 0) >= REPORT_SECONDS)):
            self._start(path)
        if not isinstance(report, dict) or not isinstance(report.get("total_bytes"), int):
            return None
        state = self._state()
        budget = self.settings()["budget_gb"]
        notify = None
        if report["total_bytes"] >= budget * GB:
            if not state.get("notified"):
                state["notified"] = True
                notify = {"title": "Knowledge index is over its storage budget",
                          "body": "The knowledge index uses %.1f GB of its %.0f GB budget. OmniTerm is dropping "
                                  "vectors of uncited assistant messages older than %d days; transcript text is "
                                  "kept. Raise the budget with configure_storage, or compact the index."
                                  % (report["total_bytes"] / GB, budget, RETENTION_DAYS),
                          "severity": "attention", "dedupe_key": NOTIFY_DEDUPE_KEY}
            if state.get("retention_for") != report.get("computed_at"):
                state.update(retention_for=report.get("computed_at"), retention_running=True,
                             retention_cursor="", retention_dropped=0, retention_finished_at=None)
                self._cited = None
        else:
            state["notified"] = False  # re-armed: a later crossing notifies again
        if state.get("retention_running") and semantics is not None:
            semantics = semantics() if callable(semantics) else semantics
            self.retention_step(state, now, semantics, cited, budget_seconds)
        if state != self._state():
            self.store._document(STATE_DOCUMENT, state)
        return notify

    # Vector retention ----------------------------------------------------------------------

    def _manifest_keys(self, after, limit):
        """Transcript manifest keys after a cursor, each physical table read in key order."""
        if getattr(self.store, "readthrough", False):
            tables = ("main.overlay_documents", "persisted.records")
        else:
            tables = ("main.documents",)
        keys = set()
        for table in tables:
            keys.update(row[0] for row in self.store.connection.execute(
                "SELECT key FROM " + table + " WHERE key>? AND key<'vector:transcript;' AND key LIKE '%:manifest' "
                "ORDER BY key LIMIT ?", (after or "vector:transcript:", limit)))
        return sorted(keys)[:limit]

    def retention_step(self, state, now, semantics, cited=None, budget_seconds=RETENTION_SECONDS):
        """Drop vectors of assistant messages older than RETENTION_DAYS that no memory
        cites. The person's turns, cited messages and knowledge-base documents keep theirs.
        The manifest records the drop, so the embedder does not put them back."""
        deadline = time.monotonic() + budget_seconds
        if cited is None:
            if self._cited is None:
                self._cited = {source.get("message_id") for row in self.store.connection.execute(
                    "SELECT sources FROM memories") for source in json.loads(row[0] or "[]")}
            cited = self._cited
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - RETENTION_DAYS * 86400))
        first = True
        while first or time.monotonic() < deadline:
            first = False
            keys = self._manifest_keys(state.get("retention_cursor") or "", RETENTION_BATCH)
            for key in keys:
                state["retention_cursor"] = key
                manifest = self.store._document_value(key)
                if (not isinstance(manifest, dict) or manifest.get("retained") is False or
                        not manifest.get("vector_keys")):
                    continue
                identifier = manifest.get("record_id")
                if identifier in cited:
                    continue
                message = self.store.connection.execute(
                    "SELECT role,provider,timestamp FROM messages WHERE id=?", (identifier,)).fetchone()
                if (message is None or message["role"] != "assistant" or message["provider"] == "kb" or
                        not isinstance(message["timestamp"], str) or message["timestamp"][:19] >= cutoff):
                    continue
                for vector_key in manifest["vector_keys"]:
                    semantics._delete_vector(vector_key)
                self.store._document(key, dict(manifest, vector_keys=[], retained=False, dropped_at=now))
                state["retention_dropped"] = (state.get("retention_dropped") or 0) + 1
            if len(keys) < RETENTION_BATCH:
                state.update(retention_running=False, retention_cursor="", retention_finished_at=now)
                return False
        return True
