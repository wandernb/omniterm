"""Chunked local-vector retrieval over the app-owned knowledge documents."""

import array
import collections
import hashlib
import heapq
import importlib.util
import json
import math
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location("omni_local_embeddings", Path(__file__).with_name("omni_embeddings.py"))
EMBEDDINGS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(EMBEDDINGS)
_QUERY_SPEC = importlib.util.spec_from_file_location("omni_query_for_semantics", Path(__file__).with_name("omni_query.py"))
QUERY = importlib.util.module_from_spec(_QUERY_SPEC)
_QUERY_SPEC.loader.exec_module(QUERY)
_VECTORS_SPEC = importlib.util.spec_from_file_location("omni_vector_matrix", Path(__file__).with_name("omni_vectors.py"))
VECTORS = importlib.util.module_from_spec(_VECTORS_SPEC)
_VECTORS_SPEC.loader.exec_module(VECTORS)
SCAN_RECORDS = 256
MATRIX_LOAD_ROWS = 500
MATRIX_LOAD_SECONDS = 1.5  # per background embed request, only while warming after launch
QUERY_LOAD_SECONDS = 0.2
SNAPSHOT_VERSION = 1  # float32 rows (.f32)
SNAPSHOT_INT8_VERSION = 2  # per-row scales then int8 rows (.i8), once the index stores int8 vectors
STORAGE_CHECK_SECONDS = 60
COMPACTION_DOCUMENT = "metadata:knowledge_compaction"  # written by helpers/omni_compact.py
# The loaded matrices are saved at most this often, so a relaunch warms in
# seconds. A save rewrites the whole snapshot (about 870 MB on a large index) and
# holds the worker meanwhile; rows added since the last save load after a launch
# in bounded slices, so a rarer save costs a little warm-up, not a stall.
SNAPSHOT_SECONDS = 4 * 3600
RRF_K = 60
# Per-list weights for Reciprocal Rank Fusion, chosen with scripts/memory-eval.py.
RRF_WEIGHTS = {"semantic_rank": 0.5, "lexical_rank": 1.0, "weight_rank": 1.0}
MMR_ENABLED = True
SIMILAR_TEXT_CHARACTERS = 600  # a candidate's opening, embedded to find its nearest memories
FUSION_DEPTH = 50
HYBRID_LEXICAL_DEPTH = 30  # transcript lexical matches fused in hybrid; 20–50 scored the same (eval 2026-11-12-2250)
MMR_LAMBDA = 0.7
EMBED_CACHE_ENTRIES = 5000  # ~10 MB of float32: repeats arrive close together (mirrors, pasted logs)
SCAN_CHECKPOINT = "metadata:semantic_scan:v1"
PRUNE_RECORDS = 64
PRUNE_CHECKPOINT = "metadata:semantic_prune:v1"
# Priority walk: the person's own turns, then recent messages, then the id-order rest.
# Each phase reads message tables newest-first by rowid (ingestion order) in windows;
# the schema has no timestamp index, and a sort by timestamp reads the whole corpus.
PRIORITY_PHASES = ("user", "recent")
# Measured on the live 24 GB index: 2000 rows read in ~0.15 s, so a call reads at most ~0.08 s.
PRIORITY_WINDOW = 500
PRIORITY_WINDOWS_PER_CALL = 2
RECENT_DAYS = 14
# While the native index stores vectors as JSON text, the id-order walk stops here
# (the priority walk is not capped); the cap lifts once the store reports blob vectors.
TRANSCRIPT_VECTOR_SOFT_CAP = 300000
# A memory's title gets a vector of its own (key suffix "title", chunk index -1): the
# English sentence model ranks short titles far better than long bodies (frozen eval,
# memory semantic nDCG@10 0.057 -> 0.107 offline). Manifests record the chunking, so
# memories embedded before it are re-queued once; transcripts keep chunking 1.
TITLE_CHUNK = -1
# Mean-centring per corpus (omni_vectors.VectorMatrix.center_alpha), chosen on the frozen
# eval offline: memory semantic nDCG@10 0.107 -> 0.139 at 0.8. Transcripts: no gain measured.
CENTER_ALPHA = {"memory": 0.8, "transcript": 0.0}
MEMORY_CHUNKING = 2
VECTOR_COUNT_SECONDS = 600
BLOB_CHECK_SECONDS = 3600
# Coverage: embedded over embeddable records, recounted by a bounded walk.
COVERAGE_DOCUMENT = "metadata:semantic_coverage:v1"
# A window also looks up each row's manifest: ~0.4 s per 2000 rows live, so 250 per window.
COVERAGE_WINDOW = 250
COVERAGE_RECOUNT_SECONDS = 86400
COVERAGE_THREAD_SECONDS = 3600  # over the app's index the counts run hourly, off the worker
COVERAGE_TIMEOUT_SECONDS = 600


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vector_prefix(corpus, identifier):
    return "vector:" + corpus + ":" + content_hash(identifier) + ":"


TOOL_TEXT_PREFIX = "[external_agent_tool_"  # some agents record tool calls and results as assistant text
# Turns the harness writes, not the person or the agent: approval-review prompts and their
# verdicts, injected reminders, hook feedback, skill and command wrappers. On the live index
# they were a third of all transcript vectors, and their near-identical short chunks scored
# about 0.61 against any query, filling the semantic top 10. They stay lexically searchable.
# Each prefix fits in the 32-character opening the walks read.
MACHINE_PREFIXES = (
    TOOL_TEXT_PREFIX,
    "The following is the Codex agent",  # Codex approval review
    '{"risk_level":', '{"outcome":',  # its verdicts
    "You are judging one planned",
    "<system-reminder>", "[SYSTEM NOTIFICATION", "<task-notification>",
    "Stop hook feedback:", "A session-scoped Stop hook",
    "<skill>", "<command-name>", "<command-message>", "<local-command-",
    "Caveat: The messages below were",
    "<multi_agent_mode>", "<multi_agent_role>", "<codex_internal_context",
    "<permissions instructions>", "# AGENTS.md instructions for",
    "# MCP Server Instructions", "# Environment\nYou have been",
    "You are Codex, an agent based on", "---\n## Forge — session brief",
    "[OmniTerm wrap-up", "Consolidate up to three bounded",
)


def machine_text(opening):
    return (opening or "").startswith(MACHINE_PREFIXES)


def embeddable(role, kind, opening=None):
    """Tool calls, tool output and harness-written turns stay lexically searchable but are
    not vector-indexed. `opening` is the start of the text, enough to spot them."""
    return role != "tool" and kind != "tool" and not machine_text(opening)


def record_chunks(corpus, row, text):
    """[(chunk index, text)] to embed for a record: its bounded chunks, plus a memory's
    title on its own (TITLE_CHUNK) when it has one."""
    chunks = list(enumerate(EMBEDDINGS.chunk_text(text)))
    if corpus == "memory":
        title = " ".join(str(row["title"] or "").split())
        if title:
            chunks.append((TITLE_CHUNK, title[:EMBEDDINGS.MAX_EMBED_CHARACTERS]))
    return chunks


def count_coverage(path, model, timeout=COVERAGE_TIMEOUT_SECONDS):
    """Embeddable transcript messages and those with a current vector manifest, from
    a read-only connection of its own; None when the counts fail or run out of time."""
    try:
        connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    except sqlite3.Error:
        return None
    deadline = time.monotonic() + timeout
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100000)
    try:
        excluded = " AND ".join(["substr(text,1,%d)!=?" % len(prefix) for prefix in MACHINE_PREFIXES])
        embeddable_count = connection.execute(
            "SELECT count(*) FROM messages WHERE role!='tool' AND kind!='tool' AND " + excluded,
            MACHINE_PREFIXES).fetchone()[0]
        embedded = connection.execute(
            "SELECT count(*) FROM records WHERE key>='vector:transcript:' AND key<'vector:transcript;' "
            "AND key LIKE '%:manifest' AND json_array_length(value,'$.vector_keys')>0 "
            "AND json_extract(value,'$.model')=?", (model,)).fetchone()[0]
        return {"embeddable": embeddable_count, "embedded": embedded}
    except sqlite3.Error:
        return None
    finally:
        connection.close()


class SemanticIndex:
    def __init__(self, store, embedding=None):
        self.store = store
        self.connection = store.connection
        self.embedding = embedding or EMBEDDINGS.LocalEmbedding()
        self._jobs = collections.deque()
        self._active = set()
        self._cursors = {"transcript": "", "memory": ""}
        checkpoint = self.store._document_value(SCAN_CHECKPOINT)
        if isinstance(checkpoint, dict) and checkpoint.get("model") == self.embedding.model_id:
            cursors = checkpoint.get("cursors")
            if isinstance(cursors, dict):
                for corpus in self._cursors:
                    value = cursors.get(corpus)
                    if isinstance(value, str) and len(value) <= 300:
                        self._cursors[corpus] = value
        self._safe_cursors = dict(self._cursors)
        self._priority = {phase: {} for phase in PRIORITY_PHASES}
        if isinstance(checkpoint, dict) and checkpoint.get("model") == self.embedding.model_id:
            saved = checkpoint.get("priority")
            for phase in PRIORITY_PHASES:
                tables = saved.get(phase) if isinstance(saved, dict) else None
                for table, state in (tables.items() if isinstance(tables, dict) else ()):
                    if (isinstance(state, dict) and all(
                            state.get(name) is None or (isinstance(state.get(name), int) and
                                                         not isinstance(state.get(name), bool))
                            for name in ("high", "below", "top"))):
                        self._priority[phase][table] = {"high": state.get("high") or 0,
                                                        "below": state.get("below"), "top": state.get("top")}
        self._safe_priority = json.loads(json.dumps(self._priority))
        self._checkpoint = checkpoint
        self._vector_count = (None, None)  # (count, monotonic time)
        self._blob_check = (None, None)
        self._rejected_tick = 0
        self._walking = False
        coverage = self.store._document_value(COVERAGE_DOCUMENT)
        self._coverage = self._fresh_coverage()
        if isinstance(coverage, dict) and coverage.get("model") == self.embedding.model_id:
            self._coverage.update({key: coverage[key] for key in self._coverage if key in coverage})
        self._coverage_saved = json.loads(json.dumps(self._coverage))
        self._prune_cursors = {"semantic_chunks": "", "documents": ""}
        pruning = self.store._document_value(PRUNE_CHECKPOINT)
        if isinstance(pruning, dict):
            for table in self._prune_cursors:
                value = pruning.get(table)
                if isinstance(value, str) and len(value) <= 512 and (not value or "vector:" < value < "vector;"):
                    self._prune_cursors[table] = value
        self._errors = collections.deque(maxlen=20)
        self._matrices = {}
        self._matrix_loader = {}
        self._embed_cache = collections.OrderedDict()
        self.reset_matrices()
        if not getattr(store, "readthrough", False):
            self.connection.execute("CREATE TABLE IF NOT EXISTS semantic_chunks (key TEXT PRIMARY KEY,corpus TEXT NOT NULL,record_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,chunk TEXT NOT NULL)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS semantic_records ON semantic_chunks(corpus,record_id)")

    def _table(self, name):
        resolver = getattr(self.store, "write_table", None)
        return resolver(name) if resolver else name

    def restore_vector(self, key, value):
        if not isinstance(value, dict) or value.get("corpus") not in ("transcript", "memory"):
            return
        identifier = value.get("record_id")
        if not isinstance(identifier, str) or not identifier:
            return
        if value.get("kind") == "manifest":
            return
        dimensions, vector = value.get("dimensions"), value.get("vector")
        if vector is None and isinstance(value.get("vector_hex"), str) and isinstance(dimensions, int):
            vector = self.decode_vector(value["vector_hex"], dimensions, hexadecimal=True)
        if (isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= 4096 or
                not isinstance(vector, list) or len(vector) != dimensions or
                not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)
                        for item in vector) or not isinstance(value.get("model"), str) or
                not isinstance(value.get("content_hash"), str) or not isinstance(value.get("chunk"), str) or
                not isinstance(value.get("chunk_index"), int)):
            return
        shadow = getattr(self.store, "_shadow_document", None)
        if shadow:
            shadow(key)
        self.connection.execute("INSERT OR REPLACE INTO " + self._table("vectors") + " VALUES(?,?,?,?,?)",
                                (key, value["model"], dimensions, value["content_hash"], struct.pack("<" + "f" * dimensions, *vector)))
        self.connection.execute("INSERT OR REPLACE INTO " + self._table("semantic_chunks") + " VALUES(?,?,?,?,?)",
                                (key, value["corpus"], identifier, value["chunk_index"], value["chunk"]))
        self.apply_document(key, value)

    # In-memory vector matrices ---------------------------------------------------------------

    def _quantized_corpus(self, corpus):
        """Transcript vectors stay int8 in memory when the index stores them as int8:
        they are held exactly at a quarter of float32's size. Memories stay float32
        (few, and mean-centred)."""
        if corpus != "transcript" or not getattr(self.store, "readthrough", False):
            return False
        try:
            return self.vector_storage().get("encoding") == "int8"
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            return False

    def _new_matrix(self, corpus, dimensions):
        matrix = VECTORS.VectorMatrix(dimensions, quantized=self._quantized_corpus(corpus))
        matrix.center_alpha = CENTER_ALPHA.get(corpus, 0.0)
        return matrix

    def reset_matrices(self):
        """Forget the matrices; they are reloaded in slices (load_matrices)."""
        dimensions = getattr(self.embedding, "dimensions", 0)
        self._matrix_model = (getattr(self.embedding, "model_id", None), dimensions)
        self._matrices = {corpus: self._new_matrix(corpus, dimensions) for corpus in ("transcript", "memory")}
        phases = ["main", "persisted"] if getattr(self.store, "readthrough", False) else ["main"]
        self._matrix_loader = {corpus: {"phases": list(phases), "after": 0, "persisted_after": 0, "fresh": set(),
                                        "restored": False} for corpus in ("memory", "transcript")}
        self._snapshot_at = None
        self._snapshot_changes = 0

    def _current_matrices(self):
        if self._matrix_model != (getattr(self.embedding, "model_id", None), getattr(self.embedding, "dimensions", 0)):
            self.reset_matrices()  # a different model's vectors are never mixed in
        return self._matrices

    @property
    def matrices_warming(self):
        self._current_matrices()
        return any(state["phases"] for state in self._matrix_loader.values())

    def _record_meta(self, corpus, record_id):
        if corpus != "transcript":
            return {}
        row = self.connection.execute("SELECT provider,workspace,session_id,role,kind,substr(text,1,32) AS opening,"
                                      "timestamp FROM messages WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row is not None else None

    def load_matrices(self, budget_seconds=MATRIX_LOAD_SECONDS):
        """Load stored vectors into the matrices, a bounded slice per call; True while more remain.

        Memories load first (small, used by the graph and memory search), then transcripts.
        The helper's own rows load before the attached native index, whose rows the helper
        has replaced or deleted are skipped. Later writes reach the matrices through
        apply_document, so a row is never loaded twice from a stale copy.
        """
        import time
        deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
        self._current_matrices()
        model, dimensions = self.embedding.model_id, self.embedding.dimensions
        readthrough = getattr(self.store, "readthrough", False)
        for corpus in ("memory", "transcript"):
            state = self._matrix_loader[corpus]
            if readthrough and not state["restored"]:
                # The snapshot is hundreds of megabytes: restore it a stage per
                # call, so no single worker call holds a waiting request for long.
                steps = state.get("restore_steps")
                if steps is None:
                    steps = state["restore_steps"] = self._restore_steps(corpus, model, dimensions)
                while True:
                    try:
                        next(steps)
                    except StopIteration:
                        state["restored"], state["restore_steps"] = True, None
                        break
                    if deadline is not None and (time.monotonic() >= deadline or self._request_waiting()):
                        return True
            matrix = self._matrices[corpus]
            while state["phases"]:
                if deadline is not None and time.monotonic() >= deadline:
                    return True
                phase = state["phases"][0]
                if phase == "persisted":
                    joined = corpus == "transcript"
                    # Transcripts walk the chunk table by rowid (NOT INDEXED): through the corpus
                    # index every slice would sort the whole corpus. Memories are few, so their
                    # corpus index is the short path. CROSS JOIN keeps the chunk table driving.
                    sql = ("SELECT c.rowid AS rid,c.key,c.record_id,c.chunk_index,v.vector,v.content_hash" +
                           (",m.provider,m.workspace,m.session_id,m.role,m.kind,substr(m.text,1,32) AS opening,"
                            "m.timestamp"
                            if joined else "") +
                           " FROM persisted.semantic_chunks c" + (" NOT INDEXED" if joined else "") +
                           (" CROSS JOIN persisted.messages m ON m.id=c.record_id" if joined else "") +
                           " CROSS JOIN persisted.vectors v ON v.key=c.key"
                           " WHERE c.corpus=? AND v.model=? AND v.dimensions=? AND c.rowid>?" +
                           (" AND m.role!='tool' AND m.kind!='tool' AND substr(m.text,1,%d)!='%s'"
                            % (len(TOOL_TEXT_PREFIX), TOOL_TEXT_PREFIX) if joined else "") +
                           " AND NOT EXISTS(SELECT 1 FROM main.shadow_documents s WHERE s.key=c.key)"
                           " ORDER BY c.rowid LIMIT ?")
                else:
                    prefix = "overlay_" if readthrough else ""
                    sql = ("SELECT c.rowid AS rid,c.key,c.record_id,c.chunk_index,v.vector,v.content_hash FROM main." +
                           prefix + "semantic_chunks c JOIN main." + prefix + "vectors v ON v.key=c.key"
                           " WHERE c.corpus=? AND v.model=? AND v.dimensions=? AND c.rowid>? ORDER BY c.rowid LIMIT ?")
                after = state["after"] or (state["persisted_after"] if phase == "persisted" else 0)
                rows = self.connection.execute(sql, (corpus, model, dimensions, after, MATRIX_LOAD_ROWS)).fetchall()
                for row in rows:
                    if phase == "persisted" and corpus == "transcript":
                        meta = {name: row[name] for name in ("provider", "workspace", "session_id", "role", "kind",
                                                             "timestamp")}
                        meta["opening"] = row["opening"]
                    else:
                        meta = self._record_meta(corpus, row["record_id"])
                        if meta is None:
                            continue
                    if corpus == "transcript" and not embeddable(meta.get("role"), meta.get("kind"), meta.get("opening")):
                        continue
                    if phase == "persisted" and row["key"] in state["fresh"]:
                        continue  # the helper's newer copy already loaded
                    vector = self.decode_vector(row["vector"], dimensions)
                    if vector is not None:
                        matrix.upsert(row["key"], row["record_id"], row["chunk_index"], row["content_hash"], vector, meta)
                        if phase == "main":
                            state["fresh"].add(row["key"])
                if rows and phase == "persisted":
                    state["persisted_after"] = max(state["persisted_after"], rows[-1]["rid"])
                if len(rows) < MATRIX_LOAD_ROWS:
                    state["phases"].pop(0)
                    state["after"] = 0
                else:
                    state["after"] = rows[-1]["rid"]
        return False

    def apply_document(self, key, value):
        """Keep the matrices current with a vector document written or deleted by this helper."""
        parts = key.split(":")
        matrices = self._current_matrices()
        if len(parts) < 4 or parts[1] not in matrices:
            return
        matrix = matrices[parts[1]]
        if value is None:
            if matrix.delete(key):
                self._snapshot_changes += 1
            return
        if not isinstance(value, dict) or value.get("kind") != "chunk" or value.get("model") != self.embedding.model_id:
            return
        vector = value.get("vector")
        if vector is None and isinstance(value.get("vector_hex"), str) and isinstance(value.get("dimensions"), int):
            vector = self.decode_vector(value["vector_hex"], value["dimensions"], hexadecimal=True)
        if not isinstance(vector, list):
            return
        meta = self._record_meta(parts[1], value.get("record_id"))
        if meta is None or (parts[1] == "transcript" and
                            not embeddable(meta.get("role"), meta.get("kind"), meta.get("opening"))):
            matrix.delete(key)
            return
        matrix.upsert(key, value["record_id"], value.get("chunk_index", 0), value.get("content_hash"), vector, meta)
        self._matrix_loader[parts[1]]["fresh"].add(key)
        self._snapshot_changes += 1

    # Snapshots: the loaded matrices on disk, beside the native index ------------------------

    def _snapshot_paths(self, corpus):
        index = getattr(self.store, "_read_index", None)
        if index is None:
            return None
        base = index.parent / (index.stem + "-vectors-" + corpus)
        return base.with_suffix(".json"), base.with_suffix(".f32"), base.with_suffix(".i8")

    def _index_identity(self):
        index = self.store._read_index
        return {"index": str(index), "inode": os.stat(index).st_ino}

    def _request_waiting(self):
        waiting = getattr(self.store, "request_waiting", None)
        return bool(waiting and waiting())

    def _restore_snapshot(self, corpus, model, dimensions):
        """Start from the saved matrix and load only rows the native index gained since.

        Rows deleted since the snapshot stay until the next save, but never surface:
        search accepts a row only when its record exists with the vector's hash."""
        steps = self._restore_steps(corpus, model, dimensions)
        while True:
            try:
                next(steps)
            except StopIteration as done:
                return bool(done.value)

    def _restore_steps(self, corpus, model, dimensions):
        """_restore_snapshot in stages (its metadata, its rows, the matrix, then
        installing it), yielding between them; the generator's value says whether
        the snapshot was restored."""
        paths = self._snapshot_paths(corpus)
        try:
            metadata = json.loads(paths[0].read_text())
            if (metadata.get("version") not in (SNAPSHOT_VERSION, SNAPSHOT_INT8_VERSION) or metadata.get("model") != model or
                    metadata.get("dimensions") != dimensions or metadata.get("identity") != self._index_identity()):
                return False
            newest = self.connection.execute("SELECT max(rowid) FROM persisted.semantic_chunks").fetchone()[0] or 0
            if metadata.get("persisted_after", 0) > newest:
                return False  # the index was rebuilt: its rowids no longer line up
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False
        yield
        try:
            # Read and decoded in bounded steps: the whole file is never held
            # beside the matrix it becomes.
            quantized = metadata["version"] == SNAPSHOT_INT8_VERSION and self._quantized_corpus(corpus)
            data = None
            if quantized:
                with open(paths[2], "rb") as handle:
                    matrix = VECTORS.VectorMatrix.from_int8_snapshot(metadata, handle)
            elif metadata["version"] == SNAPSHOT_INT8_VERSION:
                with open(paths[2], "rb") as handle:
                    data = VECTORS.read_int8_rows(handle, len(metadata.get("rows") or []), dimensions)
            else:
                with open(paths[1], "rb") as handle:
                    data = VECTORS.read_float32_rows(handle)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False
        yield
        try:
            if data is not None:
                matrix = VECTORS.VectorMatrix.from_snapshot(metadata, data)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False
        if matrix is None:
            return False
        yield
        matrix.center_alpha = CENTER_ALPHA.get(corpus, 0.0)
        if corpus == "transcript":
            # A snapshot saved before the harness-turn rule still holds their rows.
            matrix.hide_records([record for record, meta in zip(matrix.records, matrix.record_meta)
                                 if not embeddable((meta[1] or {}).get("role"), (meta[1] or {}).get("kind"),
                                                   (meta[1] or {}).get("opening"))])
        self._matrices[corpus] = matrix
        self._matrix_loader[corpus]["persisted_after"] = metadata.get("persisted_after", 0)
        return True

    def maybe_save_snapshot(self, now=None):
        """Save the warm matrices when they changed and the last save is old enough."""
        now = time.monotonic() if now is None else now
        if (not getattr(self.store, "readthrough", False) or self.matrices_warming or
                (self._snapshot_at is not None and (now - self._snapshot_at < SNAPSHOT_SECONDS or
                                                    not self._snapshot_changes))):
            return False
        identity = self._index_identity()
        # An index whose vectors are int8 already lost that precision; its snapshot is int8 too.
        int8 = self.vector_storage().get("encoding") == "int8"
        for corpus, matrix in self._matrices.items():
            paths = self._snapshot_paths(corpus)
            metadata, _ = matrix.to_snapshot()
            metadata.update(version=SNAPSHOT_INT8_VERSION if int8 else SNAPSHOT_VERSION, model=self.embedding.model_id,
                            identity=identity, persisted_after=self._matrix_loader[corpus]["persisted_after"])
            # Quantized and encoded as they are written, so a save never holds a
            # second copy of the matrix or the whole metadata text.
            rows = (paths[2] if int8 else paths[1], lambda handle: matrix.write_rows(handle, int8))
            for path, write in (rows, (paths[0], lambda handle: VECTORS.write_json(handle, metadata))):
                temporary = path.with_name(path.name + ".tmp")
                with open(temporary, "wb") as handle:
                    write(handle)
                os.replace(temporary, path)
            stale = paths[1] if int8 else paths[2]
            try:
                stale.unlink()
            except OSError:
                pass
        self._snapshot_at, self._snapshot_changes = now, 0
        return True

    @staticmethod
    def decode_vector(raw, dimensions, hexadecimal=False):
        """A stored vector: JSON text (a version-1 native index), or a float32 or int8 blob
        (this helper, a compacted index); `hexadecimal` reads a blob written as hex text."""
        try:
            if hexadecimal:
                raw = bytes.fromhex(raw)
            vector = json.loads(raw) if isinstance(raw, str) else VECTORS.decode_blob(raw, dimensions)
        except (ValueError, TypeError, struct.error):
            return None
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions or not all(isinstance(value, (int, float)) and math.isfinite(value)
                                                for value in vector):
            return None
        return vector

    def _delete_vector(self, key):
        self.connection.execute("DELETE FROM " + self._table("vectors") + " WHERE key=?", (key,))
        self.connection.execute("DELETE FROM " + self._table("semantic_chunks") + " WHERE key=?", (key,))
        self.store._document(key, None)
        self.apply_document(key, None)

    @staticmethod
    def _record_text(corpus, row):
        return row["text"] if corpus == "transcript" else row["title"] + "\n" + row["body"]

    def _row(self, corpus, identifier, conditions=(), bindings=()):
        table = "messages" if corpus == "transcript" else "memories"
        sql = "SELECT * FROM " + table + " m WHERE id=?"
        if conditions:
            sql += " AND " + " AND ".join(conditions)
        return self.connection.execute(sql, [identifier] + list(bindings)).fetchone()

    def _vector_rows(self, corpus, model, dimensions=None, identifier=None, digest=None):
        # Joining the union views builds temporary indexes containing every
        # vector. The paired projections share a key and are written atomically.
        contexts = [("main", "")] if not getattr(self.store, "readthrough", False) else [
            ("main", "overlay_"), ("persisted", "")]
        cursors = []
        for schema, prefix in contexts:
            sql = ("SELECT c.key,c.record_id,c.chunk_index,c.chunk,v.vector,v.content_hash FROM " +
                   schema + "." + prefix + "semantic_chunks c CROSS JOIN " + schema + "." + prefix +
                   "vectors v ON v.key=c.key WHERE c.corpus=? AND v.model=?")
            arguments = [corpus, model]
            for column, value in (("v.dimensions", dimensions), ("c.record_id", identifier),
                                  ("v.content_hash", digest)):
                if value is not None:
                    sql += " AND " + column + "=?"
                    arguments.append(value)
            if schema == "persisted":
                sql += " AND NOT EXISTS(SELECT 1 FROM main.shadow_documents s WHERE s.key=c.key)"
            sql += " ORDER BY c.record_id,c.chunk_index,c.key"
            cursors.append(self.connection.execute(sql, arguments))
        try:
            yield from heapq.merge(*cursors, key=lambda row: (row["record_id"], row["chunk_index"], row["key"]))
        finally:
            for cursor in cursors:
                cursor.close()

    def _prune_keys(self, table, after):
        contexts = [("main", table)] if not getattr(self.store, "readthrough", False) else [
            ("main", "overlay_" + table), ("persisted", "records" if table == "documents" else table)]
        keys = set()
        # Bound physical windows before resolving shadows: a large deleted
        # prefix must not turn a small logical LIMIT into an unbounded scan.
        for schema, physical in contexts:
            keys.update(row["key"] for row in self.connection.execute(
                "SELECT key FROM " + schema + "." + physical + " WHERE key>? AND key<'vector;' ORDER BY key LIMIT ?",
                (after or "vector:", PRUNE_RECORDS)))
        return sorted(keys)[:PRUNE_RECORDS]

    def _prune(self):
        cursors = dict(self._prune_cursors)
        for projection in cursors:
            keys = self._prune_keys(projection, cursors[projection])
            for key in keys:
                if projection == "documents":
                    if not key.endswith(":manifest"):
                        continue
                    record = self.store._document_value(key)
                else:
                    record = self.connection.execute(
                        "SELECT corpus,record_id FROM semantic_chunks WHERE key=?", (key,)).fetchone()
                record = dict(record) if record is not None else {}
                if record.get("corpus") not in ("transcript", "memory"):
                    continue
                if record["corpus"] == "transcript":
                    # A vector outlives neither its message nor the decision not to embed tool rows.
                    source = self.connection.execute("SELECT role,kind,substr(text,1,32) AS opening FROM messages "
                                                     "WHERE id=?", (record.get("record_id"),)).fetchone()
                    if source is not None and embeddable(source["role"], source["kind"], source["opening"]):
                        continue
                elif self.connection.execute("SELECT 1 FROM memories WHERE id=?", (record.get("record_id"),)).fetchone():
                    continue
                if projection == "semantic_chunks":
                    self._delete_vector(key)
                else:
                    self.store._document(key, None)
            cursors[projection] = keys[-1] if len(keys) == PRUNE_RECORDS else ""
        if cursors != self._prune_cursors:
            self.store._document(PRUNE_CHECKPOINT, dict(cursors))
            self._prune_cursors = cursors
        return any(cursors.values())

    def prune(self):
        """One bounded pruning pass; True while a sweep is mid-way."""
        return self._prune()

    def _offer(self, corpus, row):
        """Queue one record for embedding unless it is already indexed; True when queued."""
        reference = (corpus, row["id"])
        if reference in self._active:
            return False
        if corpus == "transcript" and not embeddable(row["role"], row["kind"], row["text"][:32]):
            return False
        text = self._record_text(corpus, row)
        digest = content_hash(text)
        manifest = self.store._document_value(vector_prefix(*reference) + "manifest")
        chunking = MEMORY_CHUNKING if corpus == "memory" else 1
        if (manifest and manifest.get("content_hash") == digest and manifest.get("model") == self.embedding.model_id and
                manifest.get("chunking", 1) == chunking):
            if manifest.get("retained") is False:
                return False  # the retention pass dropped these vectors on purpose
            actual = {item["key"] for item in self._vector_rows(
                corpus, self.embedding.model_id, identifier=row["id"], digest=digest)}
            if actual == set(manifest.get("vector_keys", [])):
                return False
        chunks, keys = record_chunks(corpus, row, text), []
        if (manifest and manifest.get("content_hash") == digest and manifest.get("model") == self.embedding.model_id and
                manifest.get("chunking", 1) != chunking and manifest.get("retained") is not False):
            # Only the chunking changed (memories embedded before title vectors): keep the
            # stored body vectors and embed just what is new, the title (~10 ms, not ~0.6 s).
            stored = {item["key"] for item in self._vector_rows(corpus, self.embedding.model_id,
                                                                identifier=row["id"], digest=digest)}
            prefix = vector_prefix(*reference)
            kept = [key for key in manifest.get("vector_keys", []) if key in stored]
            if kept and set(kept) == {prefix + str(index) for index, _ in chunks if index != TITLE_CHUNK} & stored:
                keys = kept
                chunks = [(index, chunk) for index, chunk in chunks if index == TITLE_CHUNK]
        self._active.add(reference)
        self._jobs.append({"corpus": corpus, "record_id": row["id"], "text": text,
                           "content_hash": digest, "chunks": chunks, "chunking": chunking,
                           "next": 0, "keys": keys, "skipped": manifest.get("skipped_chunks", 0) if keys else 0,
                           "first": not (manifest and manifest.get("model") == self.embedding.model_id and
                                         manifest.get("vector_keys"))})
        return True

    def _message_tables(self):
        """The physical message tables, each with the condition hiding rows the overlay replaced."""
        if getattr(self.store, "readthrough", False):
            return [("main.overlay_messages", "0"),
                    ("persisted.messages", "EXISTS(SELECT 1 FROM main.shadow_documents s WHERE s.key='message:'||m.id)")]
        return [("main.messages", "0")]

    def _window(self, table, shadowed, below, floor, limit):
        """A window of message rows newest-first by rowid, without their full text."""
        return self.connection.execute(
            "SELECT m.rowid AS rid,m.id,m.role,m.kind,m.part,m.timestamp,substr(m.text,1,32) AS opening," +
            shadowed + " AS shadowed FROM " + table + " m WHERE m.rowid<? AND m.rowid>? ORDER BY m.rowid DESC LIMIT ?",
            (below, floor, limit)).fetchall()

    @staticmethod
    def _recent_cutoff(now=None):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((time.time() if now is None else now) - RECENT_DAYS * 86400))

    @staticmethod
    def _is_recent(row, cutoff):
        stamp = row["timestamp"]
        return isinstance(stamp, str) and len(stamp) >= 19 and stamp[4:5] == "-" and stamp[:19] >= cutoff

    def _priority_matches(self, phase, row, cutoff):
        if phase == "user":
            return row["role"] == "user" and row["part"] == 0
        return self._is_recent(row, cutoff)

    def _priority_step(self, phase, maximum, now=None):
        """Walk one priority phase; returns (queued, finished). A phase is finished when
        every table's lap reached its previous high-water mark and nothing newer arrived."""
        cutoff = self._recent_cutoff(now)
        queued, windows, finished = 0, 0, True
        for table, shadowed in self._message_tables():
            state = self._priority[phase].setdefault(table, {"high": 0, "below": None, "top": None})
            while queued < maximum:
                if state["below"] is None:
                    top = self.connection.execute("SELECT max(rowid) FROM " + table).fetchone()[0] or 0
                    if top <= state["high"]:
                        break
                    state["below"], state["top"] = top + 1, top
                if windows == PRIORITY_WINDOWS_PER_CALL:
                    finished = False
                    break
                windows += 1
                rows = self._window(table, shadowed, state["below"], state["high"], PRIORITY_WINDOW)
                inspected, in_range = 0, False
                for row in rows:
                    if queued == maximum:
                        break
                    inspected += 1
                    state["below"] = row["rid"]
                    if phase == "recent" and self._is_recent(row, cutoff):
                        in_range = True
                    if (row["shadowed"] or not self._priority_matches(phase, row, cutoff) or
                            not embeddable(row["role"], row["kind"], row["opening"])):
                        continue
                    full = self._row("transcript", row["id"])
                    if full is not None and self._offer("transcript", full):
                        queued += 1
                if inspected == len(rows) and (len(rows) < PRIORITY_WINDOW or (phase == "recent" and not in_range)):
                    # Lap done: older rows are past this phase (recent: a whole window was old).
                    state["high"], state["below"], state["top"] = state["top"], None, None
            if state["below"] is not None:
                finished = False
            if queued == maximum:
                return queued, False
        return queued, finished

    def _transcript_vector_count(self):
        count, at = self._vector_count
        if count is None or time.monotonic() - at >= VECTOR_COUNT_SECONDS:
            counted = getattr(self.store, "_counted_rows", None)
            count = counted("vectors") if counted and getattr(self.store, "readthrough", False) else \
                self.connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            self._vector_count = (count, time.monotonic())
        return count

    def vectors_are_blobs(self):
        """True once the store keeps vectors as blobs (compaction ran): the soft cap lifts."""
        storage = getattr(self, "vector_storage", None)  # the compaction migration's own report
        if storage is not None:
            try:
                return storage().get("blob_vectors") is True
            except Exception:  # its probe failing never blocks embedding; fall back below
                pass
        if not getattr(self.store, "readthrough", False):
            return True  # this helper's own store has always written float32 blobs
        value, at = self._blob_check
        if value is None or time.monotonic() - at >= BLOB_CHECK_SECONDS:
            row = self.connection.execute("SELECT typeof(vector) FROM persisted.vectors "
                                          "ORDER BY rowid DESC LIMIT 1").fetchone()
            value = row is not None and row[0] == "blob"
            self._blob_check = (value, time.monotonic())
        return value

    def vector_cap_reached(self):
        if self.vectors_are_blobs():
            return False
        count = self._transcript_vector_count()
        return count is not None and count >= TRANSCRIPT_VECTOR_SOFT_CAP

    def _walk_ids(self, corpus, maximum):
        """The id-order walk over one corpus; returns how many records it queued."""
        table = "messages" if corpus == "transcript" else "memories"
        rows = self.connection.execute("SELECT * FROM " + table + " WHERE id>? ORDER BY id LIMIT ?",
                                       (self._cursors[corpus], SCAN_RECORDS)).fetchall()
        inspected, enqueued = 0, 0
        for row in rows:
            if enqueued == maximum:
                break
            inspected += 1
            self._cursors[corpus] = row["id"]
            if self._offer(corpus, row):
                enqueued += 1
        if (inspected == len(rows) and len(rows) < SCAN_RECORDS and
                not any(reference[0] == corpus for reference in self._active)):
            self._cursors[corpus] = ""
        return enqueued

    def _enqueue(self, maximum):
        """Memories first (few, wanted by the graph), then the person's turns newest
        first, then recent messages, then the id-order rest of the transcripts."""
        self._walking = False
        queued = self._walk_ids("memory", maximum)
        if queued >= maximum:
            return
        for phase in PRIORITY_PHASES:
            count, finished = self._priority_step(phase, maximum - queued)
            queued += count
            if not finished or queued >= maximum:
                self._walking = not finished
                return  # strict order: the next phase waits for this one's lap
        if not self.vector_cap_reached():
            self._walk_ids("transcript", maximum - queued)

    def _checkpoint_scan(self):
        pending = {corpus for corpus, _ in self._active}
        for corpus, cursor in self._cursors.items():
            if corpus not in pending:
                self._safe_cursors[corpus] = cursor
        if "transcript" not in pending:
            self._safe_priority = json.loads(json.dumps(self._priority))
        checkpoint = {"model": self.embedding.model_id, "cursors": dict(self._safe_cursors),
                      "priority": self._safe_priority}
        if checkpoint != self._checkpoint:
            # Native batches commit in emission order. A frontier follows all
            # completed vectors/manifests and never passes an unfinished job.
            self.store._document(SCAN_CHECKPOINT, checkpoint)
            self._checkpoint = checkpoint
        if self._coverage != self._coverage_saved:
            self.store._document(COVERAGE_DOCUMENT, self._coverage)
            self._coverage_saved = json.loads(json.dumps(self._coverage))

    # Coverage ------------------------------------------------------------------------------

    def _fresh_coverage(self):
        return {"model": self.embedding.model_id, "embeddable": None, "embedded": None, "counted_at": None,
                "embedded_since": 0, "rejected_chunks": 0, "pass": None}

    def step_coverage(self, budget_seconds=0.1, now=None):
        """Advance the bounded recount of embeddable and embedded transcript records.
        A pass walks each message table newest-first in windows; between passes the
        embedded count grows as manifests are written. True while a pass is mid-way.
        Over the app's index the pass is two read-only counts on a thread of its own
        (about a minute and a half on a 24 GB index), hourly, instead of hours of slices."""
        now = time.time() if now is None else now
        coverage = self._coverage
        path = getattr(self.store, "_read_index", None) if getattr(self.store, "readthrough", False) else None
        if path:
            return self._threaded_coverage(Path(path), now)
        deadline = time.monotonic() + budget_seconds
        if coverage["pass"] is None:
            if coverage["counted_at"] is not None and now - coverage["counted_at"] < COVERAGE_RECOUNT_SECONDS:
                return False
            coverage["pass"] = {"tables": {}, "embeddable": 0, "embedded": 0, "started": now}
        walk = coverage["pass"]
        model = self.embedding.model_id
        windows = 0
        for table, shadowed in self._message_tables():
            below = walk["tables"].get(table)
            if below == 0:
                continue
            if below is None:
                below = (self.connection.execute("SELECT max(rowid) FROM " + table).fetchone()[0] or 0) + 1
            while below > 0:
                if windows and time.monotonic() >= deadline:  # at least one window per call
                    walk["tables"][table] = below
                    return True
                windows += 1
                rows = self._window(table, shadowed, below, 0, COVERAGE_WINDOW)
                for row in rows:
                    if row["shadowed"] or not embeddable(row["role"], row["kind"], row["opening"]):
                        continue
                    walk["embeddable"] += 1
                    manifest = self.store._document_value(vector_prefix("transcript", row["id"]) + "manifest")
                    if isinstance(manifest, dict) and manifest.get("model") == model and manifest.get("vector_keys"):
                        walk["embedded"] += 1
                below = rows[-1]["rid"] if len(rows) == COVERAGE_WINDOW else 0
            walk["tables"][table] = 0
        coverage.update(embeddable=walk["embeddable"], embedded=walk["embedded"], counted_at=now,
                        embedded_since=0)
        coverage["pass"] = None
        return False

    def _threaded_coverage(self, path, now):
        coverage = self._coverage
        thread = getattr(self, "_coverage_thread", None)
        if thread is not None:
            if thread.is_alive():
                return True
            self._coverage_thread = None
            result = self._coverage_result
            if isinstance(result, dict):
                coverage.update(embeddable=result["embeddable"], embedded=result["embedded"], counted_at=now,
                                embedded_since=0)
                coverage["pass"] = None
                self.store._document(COVERAGE_DOCUMENT, coverage)
            return False
        if coverage["counted_at"] is not None and now - coverage["counted_at"] < COVERAGE_THREAD_SECONDS:
            return False
        model = self.embedding.model_id

        def run():
            self._coverage_result = count_coverage(path, model)
        self._coverage_result = None
        self._coverage_thread = threading.Thread(target=run, name="omni-semantic-coverage", daemon=True)
        self._coverage_thread.start()
        return True

    def coverage(self):
        """Coverage per corpus from the counters; memories are few, so they are counted directly."""
        coverage = self._coverage
        embeddable_count = coverage["embeddable"]
        embedded = None if coverage["embedded"] is None else coverage["embedded"] + coverage["embedded_since"]
        if embedded is not None and embeddable_count is not None:
            embedded = min(embedded, embeddable_count)
        memories = self.connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        memory_embedded = self.connection.execute(
            "SELECT COUNT(*) FROM documents WHERE key>='vector:memory:' AND key<'vector:memory;' "
            "AND key LIKE '%:manifest' AND json_array_length(value,'$.vector_keys')>0 "
            "AND json_extract(value,'$.model')=?", (self.embedding.model_id,)).fetchone()[0]

        def ratio(done, total):
            return None if done is None or not total else round(done / total, 4)

        return {"transcript": {"embeddable": embeddable_count, "embedded": embedded,
                               "ratio": ratio(embedded, embeddable_count), "counted_at": coverage["counted_at"],
                               "counting": coverage["pass"] is not None},
                "memory": {"embeddable": memories, "embedded": min(memory_embedded, memories),
                           "ratio": ratio(min(memory_embedded, memories), memories)},
                "rejected_chunks": coverage["rejected_chunks"], "rejected_last_tick": self._rejected_tick,
                "priority": {phase: "pending" if not tables else
                             "walking" if any(state.get("below") is not None for state in tables.values())
                             else "caught_up" for phase, tables in self._priority.items()},
                "transcript_vector_cap": {"limit": TRANSCRIPT_VECTOR_SOFT_CAP,
                                          "blob_vectors": self.vectors_are_blobs(),
                                          "capped": self.vector_cap_reached()}}

    def _embed_chunk(self, chunk):
        """Embed once per distinct chunk text; a repeated chunk reuses the recent vector."""
        cache_key = (self.embedding.model_id, content_hash(" ".join(chunk.split())))
        cached = self._embed_cache.get(cache_key)
        if cached is not None:
            self._embed_cache.move_to_end(cache_key)
            return list(cached)
        vector = self.embedding.embed(chunk)
        self._embed_cache[cache_key] = array.array("f", vector)
        while len(self._embed_cache) > EMBED_CACHE_ENTRIES:
            self._embed_cache.popitem(last=False)
        return vector

    def ensure_vectors(self, max_items=16):
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 128:
            raise ValueError("max_items must be between 1 and 128")
        self._prune()
        if not self.embedding.available:
            return {"indexed_chunks": 0, "embedding": self.embedding.status()}
        if not self._jobs:
            self._enqueue(max_items)
        indexed, attempted = 0, 0
        while self._jobs and attempted < max_items:
            job = self._jobs[0]
            reference = (job["corpus"], job["record_id"])
            row = self._row(*reference)
            if row is None or content_hash(self._record_text(job["corpus"], row)) != job["content_hash"]:
                if row is not None:
                    self._cursors[job["corpus"]] = ""
                    self._safe_cursors[job["corpus"]] = ""
                self._jobs.popleft()
                self._active.discard(reference)
                continue
            if job["next"] < len(job["chunks"]):
                chunk_index, chunk = job["chunks"][job["next"]]
                job["next"] += 1
                attempted += 1
                try:
                    vector = self._embed_chunk(chunk)
                except EMBEDDINGS.EmbeddingUnavailable as exc:
                    job["skipped"] += 1
                    self._rejected_tick += 1  # the English-only model had no vector for it
                    self._coverage["rejected_chunks"] += 1
                    self._errors.append({"corpus": job["corpus"], "record_id": job["record_id"], "error": str(exc)})
                    continue
                key = vector_prefix(*reference) + ("title" if chunk_index == TITLE_CHUNK else str(chunk_index))
                value = {"key": key, "kind": "chunk", "corpus": job["corpus"], "record_id": job["record_id"],
                         "chunk_index": chunk_index, "chunk": chunk, "content_hash": job["content_hash"],
                         "model": self.embedding.model_id, "dimensions": self.embedding.dimensions, "vector": vector}
                self.restore_vector(key, value)
                self.store._document(key, self._wire_vector(value))
                job["keys"].append(key)
                indexed += 1
            if job["next"] == len(job["chunks"]):
                retained = set(job["keys"])
                for row in self.connection.execute("SELECT key FROM semantic_chunks WHERE corpus=? AND record_id=?", reference).fetchall():
                    if row["key"] not in retained:
                        self._delete_vector(row["key"])
                key = vector_prefix(*reference) + "manifest"
                manifest = {"key": key, "kind": "manifest", "corpus": reference[0], "record_id": reference[1],
                            "content_hash": job["content_hash"], "model": self.embedding.model_id,
                            "dimensions": self.embedding.dimensions, "vector_keys": job["keys"], "skipped_chunks": job["skipped"],
                            "chunking": job.get("chunking", 1)}
                self.store._document(key, manifest)
                if reference[0] == "transcript" and job.get("first") and job["keys"]:
                    self._coverage["embedded_since"] += 1
                self._jobs.popleft()
                self._active.discard(reference)
        self._checkpoint_scan()
        self.connection.commit()
        return {"indexed_chunks": indexed, "attempted_chunks": attempted, "pending_records": len(self._jobs)}

    index_pending = ensure_vectors

    def vector_storage(self):
        """How the native index stores vectors: {"schema_version", "blob_vectors", "encoding"}.

        A compacted index (schema version 2, helpers/omni_compact.py) keeps one blob per vector,
        so new vectors travel as `vector_hex` in that encoding. Anything older keeps JSON lists."""
        if not getattr(self.store, "readthrough", False):
            return {"schema_version": None, "blob_vectors": True, "encoding": None}
        cached, at = getattr(self, "_storage_check", (None, None))
        if cached is not None and time.monotonic() - at < STORAGE_CHECK_SECONDS:
            return cached
        try:
            version = self.connection.execute("SELECT max(version) FROM persisted.knowledge_schema").fetchone()[0] or 0
            row = self.connection.execute("SELECT value FROM persisted.records WHERE key=?",
                                          (COMPACTION_DOCUMENT,)).fetchone()
            document = json.loads(row[0]) if row else {}
        except (ValueError, TypeError, sqlite3.DatabaseError):
            version, document = 0, {}
        encoding = document.get("vector_encoding") if isinstance(document, dict) else None
        compacted = version >= 2 and encoding in ("int8", "float32")
        result = {"schema_version": version, "blob_vectors": compacted, "encoding": encoding if compacted else None}
        self._storage_check = (result, time.monotonic())
        return result

    def _wire_vector(self, value):
        """The vector document as persisted: a hex blob for a compacted index, else the JSON list."""
        encoding = self.vector_storage()["encoding"]
        if not encoding:
            return value
        wire = {name: item for name, item in value.items() if name != "vector"}
        wire["vector_hex"] = VECTORS.encode_vector(value["vector"], encoding).hex()
        return wire

    def status(self, vectors=None, count_vectors=True):
        """`vectors` is a count kept warm elsewhere, or None while it warms; with
        `count_vectors` false it is never counted here, which reads gigabytes of
        index on a large store."""
        status = self.embedding.status()
        if vectors is None and count_vectors:
            vectors = self.connection.execute("SELECT COUNT(*) FROM vectors WHERE model=?", (status["model_id"],)).fetchone()[0]
        status.update(vectors=vectors,
                      pending_records=len(self._jobs), errors=list(self._errors),
                      max_chunk_tokens=400, overlap_tokens=80, max_chunk_characters=1600,
                      coverage=self.coverage(), vector_storage=self.vector_storage())
        return status

    def _allowed_memories(self, arguments, post_filter):
        """IDs of every memory passing the scope filters and the store's post-filter,
        or None when they cannot be judged up front (the caller then checks hits one
        by one)."""
        try:
            conditions, bindings = self.store._filters(arguments)
            memories = self.store.hippocampus._all(time.time(), workspaces=False)
            scoped = None
            if conditions:
                # The store's scope cache: cited messages looked up by key in
                # batches, kept until the memories change.
                scoped = set(self.store._scoped_memory_ids("memories", conditions, bindings))
            return {memory["id"] for memory in memories
                    if (scoped is None or memory["id"] in scoped) and post_filter(memory, arguments)}
        except Exception:
            return None

    def _matches_memory_filters(self, memory, arguments):
        conditions, bindings = self.store._filters(arguments)
        if not conditions:
            return True
        for source in memory["sources"]:
            if self.connection.execute("SELECT 1 FROM messages m WHERE m.id=? AND " + " AND ".join(conditions),
                                       [source["message_id"]] + bindings).fetchone():
                return True
        return False

    def _lexical(self, corpus, arguments):
        if corpus == "transcript":
            return self.store.search_transcripts({**arguments, "mode": "lexical", "limit": HYBRID_LEXICAL_DEPTH})["matches"]
        if hasattr(self.store, "_search_expression") and arguments.get("_parsed"):
            expression = self.store._search_expression(arguments)
            if expression is None:
                return []
            results = []
            for row in self.store._lexical_rows("memories", None, arguments, 200, expression=expression):
                memory = self.store._memory(row)
                if self._matches_memory_filters(memory, arguments) and self.store._memory_matches(memory, arguments):
                    memory["score"] = -row["rank"]
                    memory.pop("rank", None)
                    results.append(memory)
            return results
        if hasattr(self.store, "_lexical_memory_rows"):
            rows = self.store._lexical_memory_rows(arguments["query"], limit=200)
        else:
            rows = self.connection.execute("SELECT m.*,bm25(memory_fts) rank FROM memory_fts JOIN memories m ON m.rowid=memory_fts.rowid WHERE memory_fts MATCH ? ORDER BY rank,m.id LIMIT 200",
                                           (self.store._fts_query(arguments["query"]),)).fetchall()
        results = []
        for row in rows:
            memory = self.store._memory(row)
            if self._matches_memory_filters(memory, arguments):
                memory["score"] = -row["rank"]
                memory.pop("rank", None)
                results.append(memory)
        return results

    def search(self, query, filters=None, mode="semantic", limit=20, corpus="transcript", vector=None, exclude=None):
        """Semantic or hybrid search. `vector` replaces the query's embedding (more like this);
        `exclude` is a record id never returned."""
        if (not isinstance(query, str) or (vector is None and not query.strip()) or len(query) > 2000 or
                mode not in ("semantic", "hybrid") or corpus not in ("transcript", "memory") or
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50):
            raise ValueError("Invalid semantic search arguments")
        arguments = {**(filters or {}), "mode": mode, "limit": limit}
        parsed_before = bool(arguments.get("_parsed"))
        if not parsed_before:
            arguments["query"] = query
        if hasattr(self.store, "_query_arguments"):
            arguments = self.store._query_arguments(arguments)
        # The caller's text is already free of grammar when it parsed it (see search_transcripts).
        embed_text = query if parsed_before else " ".join(
            [arguments.get("query") or ""] + list(arguments.get("phrases") or ())).strip()
        if vector is None and not embed_text:
            raise ValueError("Invalid semantic search arguments")
        words = QUERY.terms(arguments.get("query"), arguments.get("phrases") or ())
        negations = arguments.get("negations") or ()
        conditions, bindings = self.store._filters(arguments)
        simple = [(condition, value) for condition, value in zip(conditions, bindings)
                  if condition.endswith("=?") and condition.count("?") == 1]
        if corpus == "transcript" and hasattr(self.store, "_time_conditions"):
            times, time_values = self.store._time_conditions(arguments, self.store._timestamp_sql("m"))
            conditions, bindings = conditions + times, bindings + time_values
        if self.matrices_warming:
            # A private store (tests, restore) loads at once; the app's worker loads in
            # background slices and a search only nudges the load along meanwhile.
            self.load_matrices(QUERY_LOAD_SECONDS if getattr(self.store, "readthrough", False) else None)
        query_vector = self.embedding.embed(embed_text) if vector is None else vector
        model = self.embedding.model_id
        lexical_items = self._lexical(corpus, arguments) if mode == "hybrid" and words else []
        best_lexical = max((item.get("score", 0) for item in lexical_items), default=0)
        matrix = self._current_matrices()[corpus]
        wanted = {condition.split(".", 1)[1].split("=", 1)[0]: value
                  for condition, value in simple} if corpus == "transcript" else {}
        post_filter = getattr(self.store, "_memory_matches", None)
        sources, stale, built = {}, [0], {}
        allowed = None
        if corpus == "memory" and post_filter is not None and (conditions or arguments.get("negations") or any(
                arguments.get(name) is not None for name in ("type", "concept", "after", "before"))):
            # A narrow filter would reject most vectors one row read at a time (a
            # scope check reads every cited message); judge every memory once, in
            # one scope query and over the cached memories, and skip the rest unread.
            allowed = self._allowed_memories(arguments, post_filter)

        verdict = getattr(self.store, "transcript_filter_verdict", None) if corpus == "transcript" else None

        def accept(identifier, meta):
            if identifier == exclude:
                return False
            if allowed is not None and identifier not in allowed:
                return False
            if any(meta.get(field) != value for field, value in wanted.items()):
                return False
            if verdict is not None and conditions and corpus == "transcript" and \
                    verdict(identifier, conditions, bindings) is False:
                return False  # rejected on the small filter copy, without reading the message
            source = self._row(corpus, identifier, conditions if corpus == "transcript" else (),
                               bindings if corpus == "transcript" else ())
            if source is None:
                return False
            if corpus == "transcript" and machine_text(source["text"][:32]):
                machine.append(identifier)  # embedded before it was excluded; pruned in the background
                return False
            if content_hash(self._record_text(corpus, source)) != matrix.content_hash(identifier):
                stale[0] += 1
                return False
            if corpus == "memory":
                if allowed is not None:  # judged up front
                    sources[identifier] = source
                    return True
                memory = self.store._memory(source) if conditions or post_filter else None
                if conditions and not self._matches_memory_filters(memory, arguments):
                    return False
                if post_filter is not None and not post_filter(memory, arguments):
                    return False
                if memory is not None:
                    built[identifier] = memory
            elif negations and QUERY.excluded(source["text"], negations):
                return False
            sources[identifier] = source
            return True

        machine = []
        slots = None
        if corpus == "transcript" and (arguments.get("after") is not None or arguments.get("before") is not None):
            # A time window: only records inside it (or of unknown time) are scored, so a
            # narrow window does not judge tens of thousands of candidates one by one.
            slots = matrix.slots_in_time(arguments.get("after"), arguments.get("before"), self._message_times)
        semantic_hits = matrix.top(query_vector, limit if mode == "semantic" else max(limit, FUSION_DEPTH), accept,
                                   slots=slots)
        if machine:
            matrix.hide_records(machine)  # later queries skip them unread; prune deletes them for good
        candidates = {}
        for rank, (semantic, identifier, chunk_index, key) in enumerate(semantic_hits):
            chunk = self.connection.execute("SELECT chunk FROM semantic_chunks WHERE key=?", (key,)).fetchone()
            item = self.store._message(sources[identifier]) if corpus == "transcript" else (
                built.get(identifier) or self.store._memory(sources[identifier]))
            item.update(semantic_score=semantic, lexical_score=0.0, embedding_model=model,
                        chunk=chunk["chunk"] if chunk else "", chunk_index=chunk_index)
            if chunk_index == TITLE_CHUNK:
                item["chunk"] = ""  # the title matched; the snippet shows the body instead
            item["snippet"] = QUERY.mark(item["chunk"] or self._record_text(corpus, sources[identifier]), words)
            candidates[identifier] = {"item": item, "semantic_rank": rank, "key": key}
        for rank, exact in enumerate(lexical_items):
            if exact["id"] == exclude:
                continue
            entry = candidates.get(exact["id"])
            if entry is None:
                item = dict(exact)
                item.update(semantic_score=None, embedding_model=model)
                entry = candidates[exact["id"]] = {"item": item, "key": None}
            entry["lexical_rank"] = rank
            entry["item"]["lexical_score"] = exact.get("score", 0) / best_lexical if best_lexical > 0 else 0.0
        if mode == "semantic":
            for entry in candidates.values():
                entry["item"]["score"] = entry["item"]["semantic_score"]
        else:
            # Reciprocal Rank Fusion needs no score normalisation between the two lists.
            lists = ["semantic_rank", "lexical_rank"]
            if corpus == "memory" and (filters or {}).get("weight_prior"):
                by_weight = sorted(candidates, key=lambda key: -(candidates[key]["item"].get("effective_weight") or 0))
                for rank, identifier in enumerate(by_weight):
                    candidates[identifier]["weight_rank"] = rank
                lists.append("weight_rank")
            for entry in candidates.values():
                entry["item"]["score"] = sum(RRF_WEIGHTS[name] / (RRF_K + 1 + entry[name]) for name in lists if name in entry)
        ranked = sorted(candidates.values(), key=lambda entry: (-entry["item"]["score"], entry["item"]["id"]))
        if mode == "hybrid" and MMR_ENABLED:
            ranked = self._diversify(ranked, matrix, limit)
        results = [entry["item"] for entry in ranked]
        if corpus == "transcript":
            # A Codex record mirrored by JSONL and the provider database is one match.
            unique, seen = [], set()
            for item in results:
                identity = (item.get("session_id"), item.get("source_line"), item.get("part"), item.get("kind"),
                            item.get("text")) if item.get("provider") == "codex" else (item["id"],)
                if identity not in seen:
                    seen.add(identity)
                    unique.append(item)
            results = unique
        bounded, size = [], 0
        for item in results[:limit]:
            item_size = len(json.dumps(item, ensure_ascii=False, default=str).encode("utf-8"))
            if bounded and size + item_size > 384 * 1024:
                break
            bounded.append(item)
            size += item_size
        warming = self.matrices_warming
        return {"matches" if corpus == "transcript" else "memories": bounded, "mode": mode,
                "embedding": self.embedding.status(), "vectors_warming": warming,
                "partial_index": warming or bool(self._jobs) or stale[0] > 0,
                "indexed_records": len(matrix.record_slot), "indexed_vectors": len(matrix)}

    def _message_times(self, identifiers):
        """{message id: stored timestamp} for records restored without one (older snapshots),
        read once in batches, from the filter sidecar when the store has it."""
        source = "filters.message_filters" if getattr(self.store, "_filter_cursor", None) else "messages"
        found = {}
        for start in range(0, len(identifiers), 5000):
            batch = json.dumps(identifiers[start:start + 5000])
            found.update((row[0], row[1]) for row in self.connection.execute(
                "SELECT id,timestamp FROM " + source + " WHERE id IN (SELECT value FROM json_each(?))", (batch,)))
        return found

    def similar_memories(self, text, limit=5):
        """[(memory id, cosine)] nearest to a text, or [] while memory vectors load.

        Used for memory intake, so it never loads or embeds anything else: one
        embedding of the text's opening, scored against the memory matrix."""
        self._current_matrices()
        if self._matrix_loader["memory"]["phases"] and not getattr(self.store, "readthrough", False):
            self.load_matrices(None)  # a private store loads at once, as search does
        if self._matrix_loader["memory"]["phases"] or not text.strip():
            return []
        try:
            vector = self.embedding.embed(text[:SIMILAR_TEXT_CHARACTERS])
        except EMBEDDINGS.EmbeddingUnavailable:
            return []
        matrix = self._matrices["memory"]

        def current(identifier, meta):
            source = self._row("memory", identifier)
            return source is not None and content_hash(self._record_text("memory", source)) == matrix.content_hash(identifier)
        # Raw cosine: intake compares it with a fixed minimum (SIMILAR_MINIMUM_COSINE).
        return [(identifier, score) for score, identifier, _, _ in matrix.top(vector, limit, current, centred=False)]

    @staticmethod
    def _diversify(ranked, matrix, limit):
        """Maximal Marginal Relevance: near-duplicate chunks (one session's repeats) give way."""
        dimensions = matrix.dimensions

        def vector(entry):
            row = matrix.key_row.get(entry["key"]) if entry.get("key") else None
            return matrix.row(row) if row is not None else None

        pool = ranked[:FUSION_DEPTH]
        top = pool[0]["item"]["score"] if pool else 0
        relevance = [entry["item"]["score"] / top if top else 0 for entry in pool]
        similarity = VECTORS.pairwise([vector(entry) for entry in pool], dimensions, matrix.accelerate)
        remaining, chosen, redundancy = list(range(len(pool))), [], [0.0] * len(pool)
        while remaining and len(chosen) < limit:
            best = max(remaining, key=lambda index: (MMR_LAMBDA * relevance[index] -
                                                     (1 - MMR_LAMBDA) * redundancy[index], -index))
            remaining.remove(best)
            chosen.append(best)
            for index in remaining:
                redundancy[index] = max(redundancy[index], similarity[best][index])
        chosen = [pool[index] for index in chosen] + [pool[index] for index in remaining]
        return chosen + ranked[FUSION_DEPTH:]

    @staticmethod
    def _search_text(arguments):
        """What to embed: the free text plus quoted phrases once the grammar is parsed."""
        query = arguments.get("query")
        if not arguments.get("_parsed") or not isinstance(query, str):
            return query
        return " ".join([query] + list(arguments.get("phrases") or ())).strip()

    def search_transcripts(self, arguments):
        return self.search(self._search_text(arguments), arguments, arguments.get("mode", "semantic"),
                           arguments.get("limit", 20), "transcript")

    def search_memories(self, arguments):
        return self.search(self._search_text(arguments), arguments, arguments.get("mode", "semantic"),
                           arguments.get("limit", 20), "memory")

    def _stored_vector(self, corpus, record_id):
        """The stored vector of a record's first chunk, from the loaded matrix, or None."""
        matrix = self._current_matrices()[corpus]
        row = matrix.key_row.get(vector_prefix(corpus, record_id) + "0")
        if row is None:
            return None
        return list(matrix.row(row))

    def search_like(self, arguments, corpus):
        """More like this: records nearest to `arguments["like"]`'s stored vector, itself excluded.
        None when the record has no stored vector (the caller falls back to a lexical query)."""
        record = self.store._like_record(arguments["like"])
        if record is None:
            return None
        if self.matrices_warming:
            self.load_matrices(QUERY_LOAD_SECONDS if getattr(self.store, "readthrough", False) else None)
        vector = self._stored_vector(record[0], record[1])
        if vector is None:
            return None
        limit = arguments.get("limit", 20)
        filters = {**arguments, "query": arguments.get("query") or ""}
        result = self.search(filters["query"], filters, "semantic", limit if isinstance(limit, int) else 20,
                             corpus, vector=vector, exclude=record[1])
        result["like"] = arguments["like"]
        return result
