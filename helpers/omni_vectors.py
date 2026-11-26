"""Exact in-memory vector search for the knowledge worker.

Every vector of one corpus sits in a single contiguous float32 array. A query scores all
rows with one matrix-vector product (Accelerate's cblas_sgemv through ctypes, or a pure
Python fallback), then walks rows from the best score down, keeping the best chunk of each
record that passes the caller's filter. Nothing is approximated: the ranking equals a
brute-force scan, at a few tens of milliseconds for half a million 512-d vectors.
"""

import array
import calendar
import ctypes
import time
import heapq
import json
import math

ACCELERATE = "/System/Library/Frameworks/Accelerate.framework/Accelerate"
ROW_MAJOR, NO_TRANSPOSE = 101, 111
COMPACT_FRACTION = 0.25
# Rows converted per step when a snapshot is read or written, so only this many
# rows' temporary copies (int8 bytes, scaled floats) exist besides the matrix.
CODEC_ROWS = 4096
# Rows widened to floats per step when scoring a quantized matrix (4 MB at 512-d).
QUANTIZED_SCORE_ROWS = 2048

_blas = None


def blas():
    """cblas_sgemv from Accelerate, or None when it cannot be loaded."""
    global _blas
    if _blas is None:
        try:
            library = ctypes.CDLL(ACCELERATE)
            function = library.cblas_sgemv
            function.restype = None
            function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_float,
                                 ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_float,
                                 ctypes.c_void_p, ctypes.c_int]
            _blas = function
        except (OSError, AttributeError):
            _blas = False
    return _blas or None


def normalized(vector):
    norm = math.sqrt(math.fsum(value * value for value in vector))
    return [value / norm for value in vector] if norm > 0 else None


def pairwise(vectors, dimensions, accelerate=True):
    """Cosine similarities between every pair of (normalised) vectors; None vectors score 0."""
    present = [index for index, vector in enumerate(vectors) if vector is not None]
    result = [[0.0] * len(vectors) for _ in vectors]
    if not present:
        return result
    flat = array.array("f")
    for index in present:
        flat.extend(vectors[index])
    function = blas() if accelerate else None
    scores = array.array("f", bytes(4 * len(present)))
    for position, index in enumerate(present):
        if function is not None:
            query = array.array("f", vectors[index])
            function(ROW_MAJOR, NO_TRANSPOSE, len(present), dimensions, 1.0, flat.buffer_info()[0], dimensions,
                     query.buffer_info()[0], 1, 0.0, scores.buffer_info()[0], 1)
            row = list(scores)
        else:
            row = [math.fsum(a * b for a, b in zip(vectors[index], vectors[other])) for other in present]
        for other_position, other in enumerate(present):
            result[index][other] = row[other_position]
    return result


class VectorMatrix:
    """Rows of one corpus and model. Records carry the metadata filters need."""

    def __init__(self, dimensions, accelerate=True, quantized=False):
        self.dimensions = dimensions
        self.accelerate = accelerate
        # Quantized: each row is held as int8 values with a float scale (a quarter of
        # float32's memory) and converted a bounded chunk at a time while scoring.
        # Only for vectors already stored as int8, which it holds exactly; needs
        # Accelerate, so without it a matrix stays float32.
        self.quantized = bool(quantized) and accelerate and blas() is not None and vdsp() is not None
        self.data = array.array("f")
        self.qdata = bytearray()
        self.scales = array.array("f")
        self.row_record = array.array("l")  # record slot, or -1 for a deleted row
        self.row_chunk = array.array("l")
        self.row_key = []
        self.key_row = {}
        self.records = []  # record id per slot
        self.record_meta = []  # (content hash, metadata dict) per slot
        self.record_slot = {}
        self.deleted = 0
        # Mean-centring (0 = off): the sentence model maps every text near one common
        # direction, so boilerplate scores high against any query. Scoring
        # cos(q - a*mu, d - a*mu) over the corpus mean mu removes that shared part.
        self.center_alpha = 0.0
        self._centre = None  # (row count at computation, mu, |mu|^2, [d . mu] per row)

    def __len__(self):
        return len(self.key_row)

    def _slot(self, record_id, content_hash, meta):
        slot = self.record_slot.get(record_id)
        if slot is None:
            slot = len(self.records)
            self.record_slot[record_id] = slot
            self.records.append(record_id)
            self.record_meta.append((content_hash, meta))
        else:
            self.record_meta[slot] = (content_hash, meta)
        return slot

    def row(self, row):
        """One row's (normalised) values as floats."""
        start, dimensions = row * self.dimensions, self.dimensions
        if not self.quantized:
            return self.data[start:start + dimensions]
        values = array.array("b")
        values.frombytes(bytes(self.qdata[start:start + dimensions]))
        scale = self.scales[row]
        return array.array("f", (value * scale for value in values))

    def _store(self, row, vector):
        """Write a normalised vector at `row`, or append it when row == number of rows."""
        dimensions = self.dimensions
        if not self.quantized:
            if row * dimensions == len(self.data):
                self.data.extend(vector)
            else:
                self.data[row * dimensions:(row + 1) * dimensions] = array.array("f", vector)
            return
        encoded = encode_int8(vector)
        scale = array.array("f"); scale.frombytes(encoded[:4])
        if row == len(self.scales):
            self.scales.append(scale[0])
            self.qdata += encoded[4:]
        else:
            self.scales[row] = scale[0]
            self.qdata[row * dimensions:(row + 1) * dimensions] = encoded[4:]

    def upsert(self, key, record_id, chunk_index, content_hash, vector, meta=None):
        if len(vector) != self.dimensions:
            return False
        vector = normalized(vector)
        if vector is None or not all(math.isfinite(value) for value in vector):
            return False
        slot = self._slot(record_id, content_hash, meta or {})
        row = self.key_row.get(key)
        if row is None:
            row = len(self.row_key)
            self.key_row[key] = row
            self.row_key.append(key)
            self.row_record.append(slot)
            self.row_chunk.append(chunk_index)
            self._store(row, vector)
        else:
            self.row_record[row] = slot
            self.row_chunk[row] = chunk_index
            self._store(row, vector)
        return True

    def delete(self, key):
        row = self.key_row.pop(key, None)
        if row is None:
            return False
        self.row_record[row] = -1
        self.row_key[row] = None
        self._store(row, [0.0] * self.dimensions)
        self.deleted += 1
        if self.deleted > COMPACT_FRACTION * max(1, len(self.row_key)):
            self.compact()
        return True

    @staticmethod
    def epoch(stamp):
        """Seconds since 1970 of a stored timestamp (ISO text or epoch digits), or None."""
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            return float(stamp)
        if not isinstance(stamp, str) or not stamp:
            return None
        if stamp[:4].isdigit() and stamp[4:5] == "-":
            # Whole seconds, UTC, as the index's own julianday() conversion reads them.
            try:
                return float(calendar.timegm(time.strptime(stamp[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")))
            except ValueError:
                try:
                    return float(calendar.timegm(time.strptime(stamp[:10], "%Y-%m-%d")))
                except ValueError:
                    return None
        try:
            return float(stamp)
        except ValueError:
            return None

    def slots_in_time(self, after=None, before=None, lookup=None):
        """Record slots whose metadata timestamp lies in [after, before), plus every slot
        whose time is unknown (the caller's filter judges those). `lookup(record ids)`
        may supply {id: timestamp} for slots without one; the answer is kept."""
        times = self.__dict__.setdefault("_slot_times", array.array("d"))
        if len(times) < len(self.records):
            missing = []
            for slot in range(len(times), len(self.records)):
                value = self.epoch((self.record_meta[slot][1] or {}).get("timestamp"))
                times.append(float("nan") if value is None else value)
                if value is None:
                    missing.append(slot)
            if missing and lookup is not None:
                found = lookup([self.records[slot] for slot in missing]) or {}
                for slot in missing:
                    value = self.epoch(found.get(self.records[slot]))
                    if value is not None:
                        times[slot] = value
        low = float("-inf") if after is None else after
        high = float("inf") if before is None else before
        return {slot for slot, value in enumerate(times) if value != value or low <= value < high}

    def hide_records(self, record_ids):
        """Take records out of search without deleting their rows (no compaction): their
        rows score nothing until an upsert of the same key brings them back. Returns rows hidden."""
        slots = {self.record_slot[record] for record in record_ids if record in self.record_slot}
        hidden = 0
        if slots:
            for row in range(len(self.row_record)):
                if self.row_record[row] in slots:
                    self.row_record[row] = -1
                    hidden += 1
        return hidden

    def compact(self):
        data, records, chunks, keys = array.array("f"), array.array("l"), array.array("l"), []
        qdata, scales = bytearray(), array.array("f")
        dimensions = self.dimensions
        for row, key in enumerate(self.row_key):
            if key is None:
                continue
            if self.row_record[row] < 0:  # hidden: dropped from the compacted copy
                self.key_row.pop(key, None)
                continue
            self.key_row[key] = len(keys)
            keys.append(key)
            records.append(self.row_record[row])
            chunks.append(self.row_chunk[row])
            if self.quantized:
                qdata += self.qdata[row * dimensions:(row + 1) * dimensions]
                scales.append(self.scales[row])
            else:
                data.extend(self.data[row * dimensions:(row + 1) * dimensions])
        self.data, self.row_record, self.row_chunk, self.row_key, self.deleted = data, records, chunks, keys, 0
        self.qdata, self.scales = qdata, scales
        self._centre = None

    def scores(self, query):
        """Cosine similarity of every row with the (normalised) query."""
        rows = len(self.row_key)
        result = array.array("f", bytes(4 * rows))
        if not rows:
            return result
        query = array.array("f", query)
        function = blas() if self.accelerate else None
        if self.quantized:
            return self._quantized_scores(query, result)
        if function is not None:
            address = lambda buffer: buffer.buffer_info()[0]  # noqa: E731  (arrays are not resized during the call)
            function(ROW_MAJOR, NO_TRANSPOSE, rows, self.dimensions, 1.0, address(self.data), self.dimensions,
                     address(query), 1, 0.0, address(result), 1)
            return result
        dimensions, data = self.dimensions, self.data
        for row in range(rows):
            start = row * dimensions
            result[row] = math.fsum(a * b for a, b in zip(data[start:start + dimensions], query))
        return result

    def _quantized_scores(self, query, result):
        """scores() over int8 rows: each chunk is widened to floats (vDSP_vflt8),
        multiplied by the query (cblas_sgemv), then by its rows' scales (vDSP_vmul)."""
        rows, dimensions = len(self.row_key), self.dimensions
        routines, gemv = vdsp(), blas()
        chunk = max(1, min(rows, QUANTIZED_SCORE_ROWS))
        buffer = zeros(chunk * dimensions)
        raw = zeros(chunk)
        source = (ctypes.c_char * len(self.qdata)).from_buffer(self.qdata)
        try:
            base = ctypes.addressof(source)
            target, raw_address = buffer.buffer_info()[0], raw.buffer_info()[0]
            scales, out, query_address = self.scales.buffer_info()[0], result.buffer_info()[0], query.buffer_info()[0]
            for first in range(0, rows, chunk):
                count = min(chunk, rows - first)
                routines["vDSP_vflt8"](base + first * dimensions, 1, target, 1, count * dimensions)
                gemv(ROW_MAJOR, NO_TRANSPOSE, count, dimensions, 1.0, target, dimensions, query_address, 1, 0.0,
                     raw_address, 1)
                routines["vDSP_vmul"](raw_address, 1, scales + 4 * first, 1, out + 4 * first, 1, count)
        finally:
            del source
        return result

    def _centring(self):
        """The corpus mean and each row's dot with it, recomputed once the matrix grew
        or shrank by a hundredth since (cheap for the memory corpus it is used on)."""
        rows = len(self.row_key)
        live = len(self.key_row)
        if self._centre is not None and abs(self._centre[0] - live) <= max(8, live // 100) and \
                len(self._centre[3]) == rows:
            return self._centre
        dimensions, data = self.dimensions, self.data
        mean = [0.0] * dimensions
        for row in range(rows):
            if self.row_record[row] < 0:
                continue
            for index, value in enumerate(self.row(row)):
                mean[index] += value
        mean = [value / max(1, live) for value in mean]
        dots = self.scores(mean) if rows else array.array("f")
        self._centre = (live, mean, math.fsum(value * value for value in mean), dots)
        return self._centre

    def centred_scores(self, query, scores):
        """Cosine of (query - a*mu) with (row - a*mu), from the plain cosine scores."""
        alpha = self.center_alpha
        _, mean, mean_square, dots = self._centring()
        query_dot = math.fsum(a * b for a, b in zip(query, mean))
        query_norm = math.sqrt(max(1e-12, 1 - 2 * alpha * query_dot + alpha * alpha * mean_square))
        offset = alpha * alpha * mean_square - alpha * query_dot
        result = array.array("f", bytes(4 * len(scores)))
        for row, score in enumerate(scores):
            dot = dots[row] if row < len(dots) else 0.0
            norm = math.sqrt(max(1e-12, 1 - 2 * alpha * dot + alpha * alpha * mean_square))
            result[row] = (score - alpha * dot + offset) / (norm * query_norm)
        return result

    def top(self, query, limit, accept=None, centred=True, slots=None):
        """[(score, record id, chunk index, key)] for the best `limit` records, best chunk each.

        `accept(record id, metadata)` filters records; rejected records never take a place,
        so a narrow filter returns as many results as a brute-force scan would.
        """
        query = normalized(query)
        if query is None or limit < 1:
            return []
        scores = self.scores(query)
        if self.center_alpha and centred:
            scores = self.centred_scores(query, scores)
        row_record = self.row_record
        eligible = (lambda row: row_record[row] >= 0) if slots is None else (lambda row: row_record[row] in slots)
        verdicts, results = {}, []
        # The best `width` eligible rows first, holding only those; when too few
        # of them pass the filter, one full sort of the eligible rows follows.
        # nlargest(n) equals the first n of that sort, so the ranking is a
        # brute-force scan's either way.
        width, start = max(64, limit * 16), 0
        while len(results) < limit:
            eligible_rows = filter(eligible, range(len(self.row_key)))
            candidates = heapq.nlargest(width, eligible_rows, key=scores.__getitem__) if start == 0 else \
                sorted(eligible_rows, key=scores.__getitem__, reverse=True)
            for row in candidates[start:]:
                slot = row_record[row]
                verdict = verdicts.get(slot)
                if verdict is None:
                    record_id = self.records[slot]
                    verdict = accept(record_id, self.record_meta[slot][1]) if accept else True
                    verdicts[slot] = verdict
                    if verdict:
                        results.append((max(-1.0, min(1.0, scores[row])), record_id, self.row_chunk[row],
                                        self.row_key[row]))
                        verdicts[slot] = "taken"
                        if len(results) == limit:
                            break
            if start or len(candidates) < width:
                break  # every eligible row was looked at
            start = len(candidates)
        return results

    def to_snapshot(self):
        """(metadata, float32 rows) for saving; deleted rows are compacted away first."""
        if self.deleted:
            self.compact()
        metadata = {"dimensions": self.dimensions,
                    "rows": [[key, self.row_record[row], self.row_chunk[row]] for row, key in enumerate(self.row_key)],
                    "records": [[record, meta[0], meta[1]] for record, meta in zip(self.records, self.record_meta)]}
        return metadata, self.data

    def write_rows(self, handle, int8):
        """The rows as a snapshot file: int8 (scales, then bytes) or float32."""
        if self.quantized and int8:
            self.scales.tofile(handle)
            handle.write(self.qdata)
        elif self.quantized:
            for row in range(len(self.row_key)):
                self.row(row).tofile(handle)
        elif int8:
            write_int8_rows(handle, self.data, self.dimensions)
        else:
            self.data.tofile(handle)

    @classmethod
    def from_int8_snapshot(cls, metadata, handle, accelerate=True):
        """A quantized matrix straight from an int8 snapshot file, without widening it."""
        dimensions, rows = metadata.get("dimensions"), len(metadata.get("rows") or [])
        matrix = cls(dimensions if isinstance(dimensions, int) and dimensions > 0 else 1, accelerate, quantized=True)
        if not matrix.quantized:
            return cls.from_snapshot(metadata, read_int8_rows(handle, rows, dimensions), accelerate)
        scales = array.array("f")
        scales.frombytes(handle.read(4 * rows))
        qdata = bytearray(rows * dimensions)
        if len(scales) != rows or handle.readinto(qdata) != len(qdata) or handle.read(1):
            raise ValueError("int8 matrix size does not match its scales")
        return cls.from_snapshot(metadata, (scales, qdata), accelerate, quantized=True)

    @classmethod
    def from_snapshot(cls, metadata, data, accelerate=True, quantized=False):
        """A matrix rebuilt from to_snapshot's output; None when the parts disagree."""
        dimensions = metadata.get("dimensions")
        rows, records = metadata.get("rows"), metadata.get("records")
        if quantized:
            scales, qdata = data
            if (not isinstance(dimensions, int) or dimensions < 1 or not isinstance(rows, list) or
                    not isinstance(records, list) or len(qdata) != len(rows) * dimensions or len(scales) != len(rows)):
                return None
            matrix = cls(dimensions, accelerate, quantized=True)
            matrix.scales, matrix.qdata = scales, qdata
        else:
            if (not isinstance(dimensions, int) or dimensions < 1 or not isinstance(rows, list) or
                    not isinstance(records, list) or len(data) != len(rows) * dimensions):
                return None
            matrix = cls(dimensions, accelerate)
            matrix.data = data
        for record, content_hash, meta in records:
            matrix.record_slot[record] = len(matrix.records)
            matrix.records.append(record)
            matrix.record_meta.append((content_hash, meta or {}))
        for key, slot, chunk in rows:
            if not 0 <= slot < len(matrix.records):
                return None
            matrix.key_row[key] = len(matrix.row_key)
            matrix.row_key.append(key)
            matrix.row_record.append(slot)
            matrix.row_chunk.append(chunk)
        return matrix

    def content_hash(self, record_id):
        slot = self.record_slot.get(record_id)
        return self.record_meta[slot][0] if slot is not None else None


# Stored vector encodings ---------------------------------------------------------------------
# A compacted knowledge index stores each vector as a blob: float32 ("<f" * d) or int8 with a
# per-vector scale ("<f" scale, then d signed bytes; value = byte * scale). The two lengths
# (4d and d+4) never coincide for d > 1, so a reader tells them apart without a flag.

_vdsp = None


def vdsp():
    """The few vDSP routines the int8 codecs use, or None when Accelerate cannot be loaded."""
    global _vdsp
    if _vdsp is None:
        try:
            library = ctypes.CDLL(ACCELERATE)
            routines = {}
            for name, argtypes in (
                    ("vDSP_maxmgv", [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong]),
                    ("vDSP_vsmul", [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_long, ctypes.c_ulong]),
                    ("vDSP_vfixr8", [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong]),
                    ("vDSP_vflt8", [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong]),
                    ("vDSP_vmul", [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p,
                                   ctypes.c_long, ctypes.c_ulong])):
                function = getattr(library, name)
                function.restype = None
                function.argtypes = argtypes
                routines[name] = function
            _vdsp = routines
        except (OSError, AttributeError):
            _vdsp = False
    return _vdsp or None


def encode_float32(vector):
    return array.array("f", vector).tobytes()


def encode_int8(vector):
    """One vector as "<f" scale + int8 bytes (symmetric, per-vector scale)."""
    peak = max((abs(value) for value in vector), default=0.0)
    scale = peak / 127.0 if peak > 0 else 0.0
    quantized = array.array("b", (max(-127, min(127, int(round(value / scale)))) if scale else 0
                                  for value in vector))
    return array.array("f", [scale]).tobytes() + quantized.tobytes()


def encode_vector(vector, encoding):
    if encoding == "int8":
        return encode_int8(vector)
    if encoding == "float32":
        return encode_float32(vector)
    raise ValueError("Unknown vector encoding")


def decode_blob(raw, dimensions):
    """A float list from a float32 or int8 blob, or None when the length fits neither."""
    raw = bytes(raw)
    if len(raw) == 4 * dimensions:
        values = array.array("f")
        values.frombytes(raw)
        return list(values)
    if len(raw) == dimensions + 4:
        scale = array.array("f")
        scale.frombytes(raw[:4])
        quantized = array.array("b")
        quantized.frombytes(raw[4:])
        return [value * scale[0] for value in quantized]
    return None


def quantize_rows(data, dimensions):
    """(per-row float32 scales, int8 bytes) for a contiguous float32 matrix."""
    rows = len(data) // dimensions if dimensions else 0
    scales = array.array("f", bytes(4 * rows))
    routines = vdsp()
    if routines is None or not rows:
        quantized = bytearray()
        for row in range(rows):
            encoded = encode_int8(data[row * dimensions:(row + 1) * dimensions])
            scales[row] = array.array("f", encoded[:4])[0]
            quantized += encoded[4:]
        return scales, bytes(quantized)
    scaled = array.array("f", bytes(4 * rows * dimensions))
    peak, factor = ctypes.c_float(), ctypes.c_float()
    source, target = data.buffer_info()[0], scaled.buffer_info()[0]
    for row in range(rows):
        offset = 4 * row * dimensions
        routines["vDSP_maxmgv"](source + offset, 1, ctypes.addressof(peak), dimensions)
        scales[row] = peak.value / 127.0 if peak.value > 0 else 0.0
        factor.value = 127.0 / peak.value if peak.value > 0 else 0.0
        routines["vDSP_vsmul"](source + offset, 1, ctypes.addressof(factor), target + offset, 1, dimensions)
    quantized = ctypes.create_string_buffer(rows * dimensions)
    routines["vDSP_vfixr8"](target, 1, ctypes.addressof(quantized), 1, rows * dimensions)
    return scales, quantized.raw


def zeros(count):
    """A float32 array of `count` zeros, allocated once (no temporary bytes object)."""
    return array.array("f", [0.0]) * count


def _dequantize_into(data, first_row, scales, chunk, dimensions):
    """Decode int8 rows `chunk` (rows first_row onwards) into the float32 matrix `data`."""
    rows = len(chunk) // dimensions if dimensions else 0
    routines = vdsp()
    if routines is None:
        values = array.array("b")
        values.frombytes(chunk)
        for row in range(rows):
            scale, base, start = scales[first_row + row], (first_row + row) * dimensions, row * dimensions
            for index in range(dimensions):
                data[base + index] = values[start + index] * scale
        return
    buffer = ctypes.create_string_buffer(bytes(chunk), len(chunk))
    target = data.buffer_info()[0] + 4 * first_row * dimensions
    routines["vDSP_vflt8"](ctypes.addressof(buffer), 1, target, 1, rows * dimensions)
    factor = ctypes.c_float()
    for row in range(rows):
        factor.value = scales[first_row + row]
        offset = target + 4 * row * dimensions
        routines["vDSP_vsmul"](offset, 1, ctypes.addressof(factor), offset, 1, dimensions)


def dequantize_rows(scales, quantized, dimensions):
    """The float32 matrix quantize_rows encoded."""
    rows = len(scales)
    if len(quantized) != rows * dimensions:
        raise ValueError("int8 matrix size does not match its scales")
    data = zeros(rows * dimensions)
    view = memoryview(quantized)
    for first in range(0, rows, CODEC_ROWS):
        _dequantize_into(data, first, scales, view[first * dimensions:(first + CODEC_ROWS) * dimensions], dimensions)
    return data


def read_int8_rows(handle, rows, dimensions):
    """An int8 snapshot file (rows scales, then rows * dimensions bytes) as a float32
    matrix, read and decoded a bounded number of rows at a time."""
    scales = array.array("f")
    scales.frombytes(handle.read(4 * rows))
    if len(scales) != rows:
        raise ValueError("int8 matrix is shorter than its scales")
    data = zeros(rows * dimensions)
    for first in range(0, rows, CODEC_ROWS):
        count = min(CODEC_ROWS, rows - first)
        chunk = handle.read(count * dimensions)
        if len(chunk) != count * dimensions:
            raise ValueError("int8 matrix size does not match its scales")
        _dequantize_into(data, first, scales, chunk, dimensions)
    if handle.read(1):
        raise ValueError("int8 matrix size does not match its scales")
    return data


def write_int8_rows(handle, data, dimensions):
    """Write a float32 matrix as read_int8_rows reads it, quantizing a bounded number of
    rows at a time; the scales are written last, into the space left for them."""
    rows = len(data) // dimensions if dimensions else 0
    scales = zeros(rows)
    handle.seek(4 * rows)
    for first in range(0, rows, CODEC_ROWS):
        count = min(CODEC_ROWS, rows - first)
        chunk_scales, quantized = quantize_rows(data[first * dimensions:(first + count) * dimensions], dimensions)
        scales[first:first + count] = chunk_scales
        handle.write(quantized)
    handle.seek(0)
    scales.tofile(handle)


def write_json(handle, value, chunk=CODEC_ROWS):
    """json.dumps(value) written to a binary handle, its top-level lists encoded a
    chunk of items at a time: the C encoder's speed without the whole text in memory."""
    if not isinstance(value, dict):
        handle.write(json.dumps(value).encode("utf-8"))
        return
    handle.write(b"{")
    for index, (key, item) in enumerate(value.items()):
        handle.write((", " if index else "").encode() + json.dumps(key).encode("utf-8") + b": ")
        if not isinstance(item, list):
            handle.write(json.dumps(item).encode("utf-8"))
            continue
        handle.write(b"[")
        for start in range(0, len(item), chunk):
            part = json.dumps(item[start:start + chunk])[1:-1]
            handle.write(((", " if start else "") + part).encode("utf-8"))
        handle.write(b"]")
    handle.write(b"}")


def read_float32_rows(handle):
    """A float32 snapshot file as a matrix, read a bounded number of bytes at a time."""
    data = array.array("f")
    while True:
        chunk = handle.read(4 * 512 * CODEC_ROWS)
        if not chunk:
            return data
        if len(chunk) % 4:
            raise ValueError("float32 matrix has a partial value")
        data.frombytes(chunk)
