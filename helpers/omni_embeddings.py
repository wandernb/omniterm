"""Bounded on-device embeddings from macOS NaturalLanguage, without downloads."""

import collections
import contextlib
import ctypes
import math
import re
import sys
import threading


MAX_EMBED_CHARACTERS = 8000
MAX_EMBED_TOKENS = 400
WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


class EmbeddingUnavailable(ValueError):
    code = "embedding_unavailable"


def normalized(vector, dimensions):
    if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
        raise EmbeddingUnavailable("The local model returned an invalid vector")
    magnitude = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise EmbeddingUnavailable("The local model returned an empty vector")
    return [float(value / magnitude) for value in vector]


def chunk_text(text, max_tokens=400, overlap=80, max_characters=1600):
    """Produce bounded overlapping chunks, including long unbroken tool output."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 1000 or
            isinstance(overlap, bool) or not isinstance(overlap, int) or not 0 <= overlap < max_tokens or
            isinstance(max_characters, bool) or not isinstance(max_characters, int) or
            not 16 <= max_characters <= MAX_EMBED_CHARACTERS):
        raise ValueError("Invalid chunk bounds")
    tokens = list(re.finditer(r"\S+", text))
    result = []
    index = 0
    while index < len(tokens):
        end = min(index + max_tokens, len(tokens))
        start_character = tokens[index].start()
        while end > index + 1 and tokens[end - 1].end() - start_character > max_characters:
            end -= 1
        stop_character = tokens[end - 1].end()
        part = text[start_character:stop_character]
        if len(part) <= max_characters:
            result.append(part)
        else:
            for offset in range(0, len(part), max_characters):
                result.append(part[offset:offset + max_characters])
        if end == len(tokens):
            break
        index = max(index + 1, end - min(overlap, (end - index) // 2))
    return result


class _NaturalLanguageBackend:
    language = "en"

    def __init__(self):
        if sys.platform != "darwin":
            raise EmbeddingUnavailable("Local NaturalLanguage embeddings require macOS")
        try:
            self._foundation = ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
            self._natural_language = ctypes.CDLL("/System/Library/Frameworks/NaturalLanguage.framework/NaturalLanguage")
            self._objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
            self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
            self._objc.objc_getClass.restype = ctypes.c_void_p
            self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
            self._objc.sel_registerName.restype = ctypes.c_void_p
            pointer = ctypes.c_void_p
            self._pointer = ctypes.CFUNCTYPE(pointer, pointer, pointer)(("objc_msgSend", self._objc))
            self._pointer_argument = ctypes.CFUNCTYPE(pointer, pointer, pointer, pointer)(
                ("objc_msgSend", self._objc))
            self._utf8 = ctypes.CFUNCTYPE(pointer, pointer, pointer, ctypes.c_char_p)(
                ("objc_msgSend", self._objc))
            self._integer = ctypes.CFUNCTYPE(ctypes.c_ulong, pointer, pointer)(("objc_msgSend", self._objc))
            self._void = ctypes.CFUNCTYPE(None, pointer, pointer)(("objc_msgSend", self._objc))
            self._get_vector = ctypes.CFUNCTYPE(ctypes.c_bool, pointer, pointer,
                                                ctypes.POINTER(ctypes.c_float), pointer)(
                ("objc_msgSend", self._objc))
            self._selectors = {}
            self._model = None
            self._string_class = self._objc.objc_getClass(b"NSString")
            self._pool_class = self._objc.objc_getClass(b"NSAutoreleasePool")
            model_class = self._objc.objc_getClass(b"NLEmbedding")
            if not all((self._string_class, self._pool_class, model_class)):
                raise EmbeddingUnavailable("The local NaturalLanguage model API is unavailable")
            with self._pool():
                language = self._string("en")
                model = self._pointer_argument(model_class, self._selector("sentenceEmbeddingForLanguage:"), language)
                self.algorithm = "sentence_l2_v1"
                if not model:
                    model = self._pointer_argument(model_class, self._selector("wordEmbeddingForLanguage:"), language)
                    self.algorithm = "word_mean_l2_v1"
                if not model:
                    raise EmbeddingUnavailable("No English local sentence or word embedding model is installed")
                self._model = self._pointer(model, self._selector("retain"))
                self.dimensions = int(self._integer(self._model, self._selector("dimension")))
                self.revision = int(self._integer(self._model, self._selector("revision")))
                if not 1 <= self.dimensions <= 4096:
                    raise EmbeddingUnavailable("The local model has unsupported dimensions")
        except (OSError, AttributeError) as exc:
            raise EmbeddingUnavailable("The local NaturalLanguage framework is unavailable") from exc

    def _selector(self, name):
        if name not in self._selectors:
            self._selectors[name] = self._objc.sel_registerName(name.encode("ascii"))
        return self._selectors[name]

    def _string(self, text):
        return self._utf8(self._string_class, self._selector("stringWithUTF8String:"), text.encode("utf-8"))

    @contextlib.contextmanager
    def _pool(self):
        pool = self._pointer(self._pool_class, self._selector("alloc"))
        pool = self._pointer(pool, self._selector("init"))
        try:
            yield
        finally:
            self._void(pool, self._selector("drain"))

    def vector(self, text):
        with self._pool():
            vector = (ctypes.c_float * self.dimensions)()
            present = self._get_vector(self._model, self._selector("getVector:forString:"), vector, self._string(text))
            return list(vector) if present else None

    def __del__(self):
        try:
            if self._model:
                self._void(self._model, self._selector("release"))
                self._model = None
        except (AttributeError, TypeError):
            pass


class LocalEmbedding:
    def __init__(self, backend_factory=None, cache_size=2048):
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or not 1 <= cache_size <= 8192:
            raise ValueError("cache_size must be between 1 and 8192")
        self._factory = backend_factory or _NaturalLanguageBackend
        self._backend = None
        self._initialized = False
        self._error = None
        self._lock = threading.RLock()
        self._cache_size = cache_size
        self._word_cache = collections.OrderedDict()
        self._document_cache = collections.OrderedDict()

    def _initialize(self):
        if not self._initialized:
            self._initialized = True
            try:
                backend = self._factory()
                if (backend.algorithm not in {"sentence_l2_v1", "word_mean_l2_v1"} or
                        not 1 <= backend.dimensions <= 4096 or backend.language != "en"):
                    raise EmbeddingUnavailable("The local embedding model has unsupported metadata")
                self._backend = backend
            except EmbeddingUnavailable as exc:
                self._error = str(exc)

    def status(self):
        with self._lock:
            self._initialize()
            backend = self._backend
            return {"available": backend is not None, "model_id": self._model_id(),
                    "dimensions": backend.dimensions if backend else 0,
                    "algorithm": backend.algorithm if backend else None,
                    "language": backend.language if backend else "en",
                    "revision": backend.revision if backend else None, "error": self._error}

    def _model_id(self):
        backend = self._backend
        if backend is None:
            return None
        return f"apple_naturallanguage:{backend.algorithm}:{backend.language}:r{backend.revision}:d{backend.dimensions}"

    @property
    def model_id(self):
        return self.status()["model_id"]

    @property
    def dimensions(self):
        return self.status()["dimensions"]

    @property
    def available(self):
        return self.status()["available"]

    @staticmethod
    def _remember(cache, key, value, maximum):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > maximum:
            cache.popitem(last=False)

    def _word(self, word):
        if word in self._word_cache:
            self._word_cache.move_to_end(word)
            return self._word_cache[word]
        vector = self._backend.vector(word)
        if vector is not None:
            vector = normalized(vector, self._backend.dimensions)
        self._remember(self._word_cache, word, vector, self._cache_size)
        return vector

    def embed(self, text):
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingUnavailable("Embedding input must contain text")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
            raise EmbeddingUnavailable("Embedding input contains invalid Unicode")
        text = text[:MAX_EMBED_CHARACTERS]
        with self._lock:
            self._initialize()
            if self._backend is None:
                raise EmbeddingUnavailable(self._error or "The local embedding model is unavailable")
            if text in self._document_cache:
                self._document_cache.move_to_end(text)
                return list(self._document_cache[text])
            dimensions = self._backend.dimensions
            if self._backend.algorithm == "sentence_l2_v1":
                vector = self._backend.vector(text)
                if vector is None:
                    raise EmbeddingUnavailable("The local sentence model could not encode this text")
                result = normalized(vector, dimensions)
            else:
                words = WORD.findall(text)[:MAX_EMBED_TOKENS]
                vectors = [self._word(word.lower()) for word in words]
                vectors = [vector for vector in vectors if vector is not None]
                if not vectors:
                    raise EmbeddingUnavailable("The local English word model has no known words in this text")
                result = normalized([math.fsum(vector[index] for vector in vectors) / len(vectors)
                                     for index in range(dimensions)], dimensions)
            self._remember(self._document_cache, text, tuple(result), min(128, self._cache_size))
            return list(result)
