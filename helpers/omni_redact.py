"""Best-effort masking of credentials in transcript text before it is offered for memory.

Pattern matching reduces, but does not eliminate, the chance that a secret reaches a
memory: the consolidator's prompt still forbids extracting credentials. Masks replace
only the secret value, so the surrounding text stays readable as evidence.

This module also decides which user turns are worth offering as memory candidates:
typed prose (not injected command or system markup, not a pasted block) carrying a
correction, decision or preference cue.
"""

import importlib.util
import re
from pathlib import Path


MASK = "[redacted]"
MAX_CHARACTERS = 4000
EXCERPT_CHARACTERS = 300
USER_MINIMUM_CHARACTERS = 12
USER_MAXIMUM_CHARACTERS = 2000  # longer prose is a pasted document or a session summary
# Agent harnesses record their own notices as user turns; they are not the person's words.
_INJECTED = re.compile(r"\A\W*(?:Another Claude session sent a message|A session-scoped Stop hook|"
                       r"Stop hook feedback|This session is being continued from a previous conversation|"
                       r"\[Request interrupted|\[SYSTEM NOTIFICATION|Caveat: The messages below|"
                       r"Base directory for this skill|The user hasn't heard from you|Tool loaded\.|"
                       r"Continue from where you left off|<task-notification|# AGENTS\.md instructions|"
                       r"Goal check-in|\[OmniTerm wrap-up|<agent-message)", re.I)
# A message the person typed while an agent was busy arrives wrapped in this notice.
_RELAYED = re.compile(r"\A\s*The user sent a new message while you were working:\s*", re.I)
PASTED_LINES = 40
PASTED_SYMBOL_RATIO = 0.3
PASTED_LINE_SHARE = 0.6

_SPEC = importlib.util.spec_from_file_location("omni_episodes_for_redact",
                                               Path(__file__).with_name("omni_episodes.py"))
_EPISODES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_EPISODES)

_TOKEN = r"[A-Za-z0-9_\-]"
_SECRET_NAME = (r"[A-Za-z0-9_.\-]*?(?:password|passwd|passphrase|secret|token|api[_\-]?key|access[_\-]?key|"
                r"private[_\-]?key)(?:[_\-]?key)?")
# Order matters: whole blocks and structured shapes first, so a later, looser
# pattern never masks half of an earlier match.
PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), MASK),
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@", re.I), r"\1" + MASK + "@"),
    (re.compile(r"\b(authorization\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=\-]+", re.I),
     r"\1" + MASK),
    (re.compile(r"\b(bearer\s+)[A-Za-z0-9._~+/\-]{16,}=*", re.I), r"\1" + MASK),
    (re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?" + _TOKEN + r"{20,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), MASK),
    (re.compile(r"\bglpat-" + _TOKEN + r"{20,}"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), MASK),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MASK),
    (re.compile(r"\bAIza" + _TOKEN + r"{35}"), MASK),
    (re.compile(r"\beyJ" + _TOKEN + r"{8,}\.eyJ" + _TOKEN + r"{8,}\." + _TOKEN + r"{8,}"), MASK),
    (re.compile(r"\b(" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)(?!=)(\"[^\"\n]+\"|'[^'\n]+'|[^\s,;'\"]+)", re.I),
     r"\1" + MASK),
)

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_CODE_LINE = re.compile(r"^(?:\s{2,}|\t)(?![-*•]\s|\d+[.)]\s)\S|[;{}\\]\s*$|\d{2}:\d{2}:\d{2}|"
                        r"^\s*(?:at |File \"|Traceback|\$ |>>> |#include|import |from \S+ import |def |class |func |"
                        r"let |var |const |return )|\b\w*(?:Error|Exception)\b:")
_BLOCK = re.compile(r"<([a-z][a-z0-9_\-]*)(?:\s[^>]*)?>.*?</\1>", re.S)
_CUE = re.compile(r"\b(?:no|don[’']?t|do not|instead|always|never|prefers?|preferred|should(?:n[’']?t)?|"
                  r"must(?:n[’']?t)?|remember|from now on)\b", re.I)


def redact(text):
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def cap(text, limit=MAX_CHARACTERS):
    """Keep the head and the tail: a request is usually stated first, a conclusion last."""
    if len(text) <= limit:
        return text
    marker = "\n[… {} characters omitted …]\n".format(len(text) - limit)
    head = (limit - len(marker)) * 3 // 4
    tail = limit - len(marker) - head
    return text[:head] + marker + text[len(text) - tail:]


def clean(text, limit=MAX_CHARACTERS):
    """Redact before capping, so a cut never leaves half of a secret unmasked."""
    return cap(redact(text or ""), limit)


def user_text(text):
    """The person's own words in a user turn: a slash command with its arguments, or
    prose with injected command and system blocks removed. None when too short."""
    text = text or ""
    command = _EPISODES.COMMAND.search(text)
    if command:
        arguments = _EPISODES.COMMAND_ARGS.search(text)
        text = command.group(1) + (" " + arguments.group(1) if arguments and arguments.group(1) else "")
    else:
        text = _EPISODES.MARKUP.sub("", _BLOCK.sub("", text))
    text = _RELAYED.sub("", text.strip()).strip()
    return text if len(text) >= USER_MINIMUM_CHARACTERS else None


def pasted(text):
    """Mostly code or log output rather than something the person typed: many lines,
    mostly code- or log-shaped lines, or dense with symbols."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > PASTED_LINES:
        return True
    if len(lines) >= 4 and sum(1 for line in lines if _CODE_LINE.search(line)) >= PASTED_LINE_SHARE * len(lines):
        return True
    visible = [character for character in text if not character.isspace()]
    symbols = sum(1 for character in visible if not character.isalnum())
    return len(visible) >= 60 and symbols / len(visible) > PASTED_SYMBOL_RATIO


def is_user_candidate(text):
    """Judged on the typed prose only: fenced code is an example, not the request."""
    stripped = user_text(text)
    if _INJECTED.search(text or "") or _INJECTED.search(stripped or ""):
        return False
    prose = _FENCE.sub("", stripped or "").strip()
    return bool(USER_MINIMUM_CHARACTERS <= len(prose) <= USER_MAXIMUM_CHARACTERS and not pasted(prose)
                and _CUE.search(prose))
