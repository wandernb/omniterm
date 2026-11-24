"""The search query grammar shared by transcript and memory search.

`parse("type:decision ws:omniterm since:7d \"sqlite vacuum\" -codex")` splits a typed
query into free text, filters, quoted phrases and negated terms. Keys it does not know,
and values it cannot read, stay in the free text, so a query never fails to parse.
Pure: no database, no clock unless `now` is omitted.
"""

import collections
import datetime as dt
import re
import time


Query = collections.namedtuple("Query", "text filters phrases negations")

PROVIDERS = ("claude", "codex", "kb")
MAX_TERMS = 32
SNIPPET_OPEN, SNIPPET_CLOSE, SNIPPET_ELLIPSIS = "«", "»", "…"
SNIPPET_CHARACTERS = 240

_KEYS = {"type": "type", "ws": "workspace", "workspace": "workspace", "provider": "provider",
         "role": "role", "concept": "concept", "session": "session_id", "after": "after",
         "before": "before", "since": "since", "archived": "archived"}
# One token: an optional minus, an optional key, then a quoted or bare value.
_TOKEN = re.compile(r'(-?)(?:([A-Za-z_]+):)?(?:"([^"]*)"?|(\S+))')
_SINCE = re.compile(r"\A(\d{1,5})([hdw])\Z", re.I)
_SINCE_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400}
_WORD = re.compile(r"\w+", re.UNICODE)
# Everyday words for the memory types, so `type:decision` finds declarative memories.
TYPE_ALIASES = {"decision": "declarative", "fact": "declarative", "event": "episodic", "episode": "episodic",
                "howto": "procedural", "procedure": "procedural", "todo": "prospective",
                "intention": "prospective", "followup": "prospective", "preference": "autobiographical",
                "person": "autobiographical", "location": "spatial", "path": "spatial", "place": "spatial",
                "feedback": "conditioning", "correction": "conditioning"}


def memory_type(value):
    """The memory type a `type:` value names: a type itself or an everyday alias."""
    lowered = (value or "").lower()
    return TYPE_ALIASES.get(lowered, lowered)


def parse_time(value):
    """Epoch seconds (UTC) for an ISO date or date-time, or None when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=dt.timezone.utc).timestamp()
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def timestamp_epoch(value):
    """Epoch seconds for a stored message timestamp (ISO text or epoch digits), or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        return parse_time(text)


def _filter(key, value, now):
    """(filter name, value) for one key:value token, or None to keep it as text."""
    if value == "":
        return None
    if key == "provider":
        value = value.lower()
        return ("provider", value) if value in PROVIDERS else None
    if key in ("after", "before"):
        epoch = parse_time(value)
        return (key, epoch) if epoch is not None else None
    if key == "since":
        match = _SINCE.match(value)
        if not match:
            return None
        return ("after", now - int(match.group(1)) * _SINCE_SECONDS[match.group(2).lower()])
    if key == "archived":
        lowered = value.lower()
        return ("archived", lowered in ("true", "yes", "1")) if lowered in ("true", "false", "yes", "no", "1", "0") else None
    if key in ("type", "role"):
        value = value.lower()
    return (_KEYS[key], value)


def parse(text, now=None):
    """Query(free text, filters, phrases, negations) for a typed search string."""
    now = time.time() if now is None else now
    words, filters, phrases, negations = [], {}, [], []
    for match in _TOKEN.finditer(text or ""):
        minus, key, quoted, bare = match.groups()
        value = quoted if quoted is not None else (bare or "")
        name = key.lower() if key else None
        if name in _KEYS and not minus:
            parsed = _filter(name, value.strip(), now)
            if parsed is not None:
                filters[parsed[0]] = parsed[1]
                continue
        if key:
            # Unknown key, unreadable value or a negated key: the whole token is plain text.
            value = match.group(0)[len(minus):]
            quoted = None
        if minus and value.strip():
            negations.append(value.strip())
        elif quoted is not None:
            if quoted.strip():
                phrases.append(quoted.strip())
        elif value.strip():
            words.append(value.strip())
    return Query(" ".join(words), filters, phrases, negations)


def merge(arguments, query):
    """Search arguments with the parsed filters added; arguments given explicitly win."""
    merged = dict(arguments)
    for name, value in query.filters.items():
        if merged.get(name) is None:
            merged[name] = value
    merged["query"] = query.text
    merged["phrases"] = list(query.phrases)
    merged["negations"] = list(query.negations)
    return merged


def terms(text, phrases=()):
    """The searchable words of free text and phrases, in order, at most MAX_TERMS."""
    found = _WORD.findall(text or "")
    for phrase in phrases:
        found.extend(_WORD.findall(phrase))
    return found[:MAX_TERMS]


def _quoted(words):
    return '"' + " ".join(word.replace('"', '""') for word in words) + '"'


STOPWORD_DROP_AFTER = 4

# Words that match most of the corpus, English and French as the person writes both.
RELAX_STOPWORDS = frozenset("""a an and are as at be by can de des did do does du en et for from how i if in is
it la le les of on or ou our pour que qui the this to un une we what when where which who why with you""".split())


def fts_expression(text, phrases=(), negations=(), any_term=False):
    """An FTS5 MATCH expression: words and phrases ANDed (ORed with any_term), negations
    excluded; None when nothing positive remains (FTS5 cannot match on NOT alone)."""
    words = _WORD.findall(text or "")
    # Stopwords match most of the corpus: they cost time and rank little. A long query
    # drops them (STOPWORD_DROP_AFTER content words or more); a short one keeps them,
    # since there each word narrows the match. Matching any word always drops them.
    content = [word for word in words if word.lower() not in RELAX_STOPWORDS]
    if any_term:
        words = content
    elif len(content) >= STOPWORD_DROP_AFTER:
        words = content
    positive = [_quoted([word]) for word in words]
    for phrase in phrases:
        words = _WORD.findall(phrase)
        if words:
            positive.append(_quoted(words))
    positive = positive[:MAX_TERMS]
    if not positive:
        return None
    expression = (" OR " if any_term else " AND ").join(positive)
    negative = []
    for negation in negations:
        words = _WORD.findall(negation)
        if words:
            negative.append(_quoted(words))
    if negative:
        expression = "(" + expression + ") NOT (" + " OR ".join(negative[:MAX_TERMS]) + ")"
    return expression


def excluded(text, negations):
    """True when text contains a negated word or phrase (case-insensitive, whole words)."""
    lowered = " " + " ".join(_WORD.findall((text or "").lower())) + " "
    for negation in negations:
        words = _WORD.findall(negation.lower())
        if words and (" " + " ".join(words) + " ") in lowered:
            return True
    return False


def expression_words(expression):
    """The words and phrases an FTS expression from fts_expression() looks for,
    negations left out; a phrase stays one item so it is marked as one span."""
    positive = (expression or "").split(") NOT (", 1)[0]
    return [phrase.replace('""', '"') for phrase in re.findall(r'"((?:[^"]|"")*)"', positive)]


def mark(text, words, width=SNIPPET_CHARACTERS):
    """A window of text around the first matched word, matches wrapped in «»."""
    text = " ".join((text or "").split())
    wanted = sorted({word.lower() for word in words if word}, key=len, reverse=True)
    if not wanted:
        return text[:width] + (SNIPPET_ELLIPSIS if len(text) > width else "")
    pattern = re.compile(r"\b(" + "|".join(r"\s+".join(re.escape(part) for part in word.split()) for word in wanted) +
                         r")\w*", re.I | re.UNICODE)
    first = pattern.search(text)
    start = 0 if first is None else max(0, first.start() - width // 4)
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < (first.start() if first else start) else start
    window = text[start:start + width]
    marked = pattern.sub(lambda match: SNIPPET_OPEN + match.group(0) + SNIPPET_CLOSE, window)
    return (SNIPPET_ELLIPSIS if start else "") + marked + (SNIPPET_ELLIPSIS if start + width < len(text) else "")
