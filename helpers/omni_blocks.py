"""Blocked provider sessions: hook events in, guarded answers out.

A provider hook (`omni hook claude|codex`) reports that a session stopped and
is waiting. The knowledge worker keeps one record per session and the
unblocker agent may answer an *idle* block with a plain session message. The
kind of a block comes only from the hook's event type, never from the prompt
text, so text inside a transcript cannot turn a permission or trust prompt
into something answerable. Anything this module does not recognise is
treated as protected.
"""

import datetime
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path


IDLE = "idle"            # the turn ended; the session waits for a person's next message
PERMISSION = "permission"  # a tool approval dialog
INPUT = "input"          # an elicitation, folder trust or other dialog that is not a message
ANSWERABLE_KINDS = frozenset((IDLE,))
# A permission dialog the PermissionRequest hook holds for the unblocker. Only a
# session working toward a stated goal is held, and only for HOLD_SECONDS; after
# that the dialog goes to the person as before.
HOLD_SECONDS = 150
PERMISSION_HOOK_TIMEOUT = HOLD_SECONDS + 30
MAX_TOOL_INPUT = 3000
# Built-in goal commands; the owner policy adds more ("Goal commands: /forge:ship, ...").
GOAL_COMMANDS = ("/goal", "/loop")
# Actions the unblocker can never allow, whatever it decides: lost forever, the
# person's own (non-development) files, or production systems.
NEVER_ALLOW = re.compile(
    r"(\brm\s+-\w*[rR]\w*\s+(/|~|\$HOME)/?(?=[\s\x22\x27`;&|)]|$)|\bgit\s+push\b[^\n]*(--force\b|\s-f\b)[^\n]*\b(main|master)\b"
    r"|\b(drop|truncate)\s+(database|schema|table)\b|\bproduction\b|\bprod\b|--prod\b"
    r"|~/(Documents|Desktop|Pictures|Movies|Music|Downloads|Library/Mobile Documents)\b"
    r"|/Users/[^/\s]+/(Documents|Desktop|Pictures|Movies|Music|Downloads)\b"
    r"|\b(diskutil\s+erase|mkfs|dd\s+if=)|\bsecurity\s+(delete|add)-|\.ssh/|\.aws/|\.env\b|secrets?\.ya?ml)", re.I)
PROTECTED_KINDS = frozenset((PERMISSION, INPUT))

MAX_PROMPT = 4000
MAX_ANSWER = 2000
MAX_REASON = 300
ANSWERS_PER_HOUR = 6
# How long a session waits before it is answered for the person: long enough for
# them to reply first if they are there (OmniScheduler.m OmniUnblockerMinimumAge).
UNBLOCK_AFTER = 45
MIN_ANSWER_AGE = UNBLOCK_AFTER
DEFAULT_MIN_AGE = UNBLOCK_AFTER
# The person's own standing rules for the unblocker: which projects are live
# products and what must wait for them. A local file, never part of OmniTerm.
OWNER_POLICY = ".omni/unblocker/policy.md"
RETENTION_SECONDS = 7 * 86400
KEY_PREFIX = "block:"
CLAUDE_IDLE_MESSAGE = "Claude is waiting for your input"
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
BLOCK_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
# Runs of OmniTerm's own unattended agents must not report themselves.
MANAGED_AGENTS = frozenset(("Omni Memory", "Omni Unblocker"))


class BlockError(ValueError):
    def __init__(self, message, code="invalid_block_request"):
        super().__init__(message)
        self.code = code


def clean_text(value, maximum):
    if not isinstance(value, str):
        return ""
    text = "".join(character for character in value
                   if character in "\n\t" or not (ord(character) < 32 or 127 <= ord(character) <= 159))
    return text.strip()[:maximum]


def _session(provider, raw):
    if not isinstance(raw, str) or not SESSION_PATTERN.fullmatch(raw):
        return None
    return f"{provider}:{raw}"


def transcript_tail(path, home=None, max_bytes=262144):
    """The last assistant text in a Claude JSONL transcript under ~/.claude/projects."""
    texts = assistant_texts(path, home, 1, max_bytes)
    return texts[0] if texts else ""


def assistant_texts(path, home=None, count=4, max_bytes=1048576):
    """The last `count` distinct assistant texts, newest first. A session that keeps
    answering "still blocked" has its real question in an earlier reply."""
    if not isinstance(path, str) or not path:
        return ""
    root = (Path(home) if home else Path.home()) / ".claude/projects"
    try:
        candidate = Path(path).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            return ""
        with open(candidate, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    texts = []
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(part.get("text", "") for part in content
                             if isinstance(part, dict) and part.get("type") == "text")
        else:
            text = ""
        text = clean_text(text, MAX_PROMPT) if text.strip() else ""
        if text and text not in texts:
            texts.append(text)
            if len(texts) >= count:
                break
    return texts


def transcript_request(path, home=None, max_bytes=1048576):
    """What the session was last asked before its final reply: the person's prompt
    or the goal or stop-hook feedback that drove the turn (tool results are skipped).
    A session that reports only what needs the person may still have work its goal
    lists; this is where that shows."""
    if not isinstance(path, str) or not path:
        return ""
    root = (Path(home) if home else Path.home()) / ".claude/projects"
    try:
        candidate = Path(path).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            return ""
        with open(candidate, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    seen_reply = False
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if entry.get("type") == "assistant":
            seen_reply = True
            continue
        if entry.get("type") != "user" or not seen_reply:
            continue
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            if any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
                continue
            text = "\n".join(part.get("text", "") for part in content
                             if isinstance(part, dict) and part.get("type") == "text")
        else:
            text = ""
        if text.strip() and not text.lstrip().startswith("<system-reminder>"):
            return clean_text(text, MAX_PROMPT)
    return ""


# A turn that ends by handing something to the person. Such a session is looked
# at as soon as it stops, instead of after the usual wait.
ASKS_PERSON = re.compile(
    r"\bblocked\b|\bneeds? you\b|\bneeds your\b|\bwaiting (?:for|on) (?:you|your)\b|\bover to you\b|"
    r"\bwhen you(?:'|\u2019)?re back\b|\byour (?:go-ahead|go ahead|call|decision|approval|yes)\b|"
    r"\btell me (?:to|whether|if|which)\b|\bshould i\b|^\s*!\s*\S", re.IGNORECASE | re.MULTILINE)


def asks_person(text):
    return bool(isinstance(text, str) and ASKS_PERSON.search(text[-4000:]))


def transcript_size(path):
    try:
        return os.stat(path).st_size if path else None
    except OSError:
        return None


def moved_on(path, size, max_bytes=4194304):
    """Whether the conversation went on after a stop recorded at `size` bytes:
    a person's prompt, a queued message or a tool call. A /goal hook re-running
    the turn, and replies that only restate the wait, are the same wait. Claude Code keeps appending bookkeeping after a turn ends (turn
    duration, titles, mode, session owner), which is not the session moving on.
    A file that shrank or was replaced counts as moved on."""
    current = transcript_size(path)
    if size is None or current is None or current == size:
        return False
    if current < size:
        return True
    try:
        with open(path, "rb") as handle:
            handle.seek(size)
            data = handle.read(max_bytes)
    except OSError:
        return True
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "user" and entry.get("isMeta") is not True:
            return True
        if entry.get("type") == "assistant":
            # A /goal hook's feedback (a meta record) and a reply that only
            # restates the wait keep the same block; a tool call is new work.
            content = (entry.get("message") or {}).get("content") if isinstance(entry.get("message"), dict) else None
            if not isinstance(content, list) or any(
                    isinstance(part, dict) and part.get("type") == "tool_use" for part in content):
                return True
    return False


COMMAND = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Harness commands that do not change what the session is working on.
NEUTRAL_COMMANDS = frozenset(("/compact", "/model", "/status", "/cost", "/context", "/config", "/permissions",
                              "/mcp", "/help", "/fast", "/effort", "/usage", "/resume", "/rename", "/memory",
                              "/hooks", "/doctor", "/btw", "/advisor"))
INTERVAL = re.compile(r"^\s*\d+\s*[smhd]\b", re.I)
POLICY_COMMANDS = re.compile(r"^\s*[-*]?\s*goal commands\s*:\s*(.+)$", re.I | re.M)


# A plain prompt states a goal when it asks for work carried through to the end.
STATED = re.compile(r"\b(finish|complete|do (it|them|this|everything)? ?all|end[- ]to[- ]end|until|don'?t stop|"
                    r"keep going|deliver|close (all|every)|fix (all|every)|run all|without (asking|stopping)|"
                    r"take care of (it )?all)\b", re.I)


def stated_goal(text):
    """The person's own prompt as a goal, when it clearly states one: an
    objective of some length that asks for work to be carried through."""
    text = clean_text(text, 1000)
    if len(text) < 40 or text.startswith(RELAYED + ("Stop hook feedback",)) or "[omni-message:" in text:
        return None
    if not STATED.search(text) or text.rstrip().endswith("?"):
        return None
    return {"command": "stated", "objective": text, "state": "active"}


def goal_commands(policy=""):
    """The commands that state a goal: the built-ins plus the owner policy's list."""
    extra = []
    for line in POLICY_COMMANDS.findall(policy or ""):
        extra += [word.strip("`.,;") for word in re.split(r"[\s,]+", line) if word.strip("`.,;")[:1] in "/$"]
    return tuple(dict.fromkeys(GOAL_COMMANDS + tuple(word.strip("`.,;") for word in extra)))


def _record_text(entry):
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
            return ""
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return ""


RELAYED = ("<", "[", "Another Claude session", "This session is being continued", "Caveat:",
           "Unblock waiting sessions now")


def person_prompt(entry, text):
    """Whether a user record is the person typing, not a relayed or harness message.
    Claude Code marks the person's prompts origin.kind "human"; older records carry
    no origin, so relayed text is recognised by how it starts."""
    origin = entry.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human" and "[omni-message:" not in text
    stripped = text.lstrip()
    return not stripped.startswith(RELAYED) and "[omni-message:" not in text


def session_goal(path, home=None, commands=GOAL_COMMANDS, max_bytes=8388608):
    """The goal a Claude session is working toward, read from harness records only.

    A goal is stated by a goal command the person ran (/goal, /loop, or one the
    owner policy names) or by Claude Code's own goal records. Assistant text and
    tool output never count, so a transcript cannot state a goal for itself.
    Returns None, or {"command", "objective", "state"} where state is "active",
    "done" (the goal was met or cleared, or the loop stopped) or "scheduled"
    (a loop waiting for its next wake-up: nothing to unblock)."""
    if not isinstance(path, str) or not path:
        return None
    root = (Path(home) if home else Path.home()) / ".claude/projects"
    try:
        candidate = Path(path).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            return None
        with open(candidate, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return None
    goal = None
    wakeup = None  # the loop's last scheduling call in the current turn
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "attachment" and isinstance(entry.get("attachment"), dict) and \
                entry["attachment"].get("type") == "goal_status":
            status = entry["attachment"]
            objective = clean_text(status.get("condition"), 1000)
            if objective:
                goal = {"command": "/goal", "objective": objective, "state": "done" if status.get("met") else "active"}
            continue
        if kind == "assistant":
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            for part in message.get("content") if isinstance(message.get("content"), list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_use" and \
                        part.get("name") in ("ScheduleWakeup", "CronCreate", "CronDelete"):
                    wakeup = {"name": part["name"], "stop": bool((part.get("input") or {}).get("stop"))}
            continue
        if kind == "user" and entry.get("isMeta") is not True:
            text = _record_text(entry)
            if text.strip() and not text.lstrip().startswith(("<system-reminder>", "Stop hook feedback")):
                wakeup = None  # a new turn began
                if person_prompt(entry, text) and \
                        not (goal and goal["command"] != "stated" and goal["state"] == "active"):
                    # Outside a running goal command, each plain prompt is the new goal, if it
                    # states one; a short reply ("continue", "yes, B") keeps a stated goal going.
                    short = len(text.strip()) < 40 and not text.rstrip().endswith("?")
                    goal = stated_goal(text) or (goal if short and goal and goal["command"] == "stated" else None)
        elif not (kind == "system" and entry.get("subtype") == "local_command"):
            continue
        text = _record_text(entry).strip()
        if text.startswith("<local-command-stdout>Goal set:"):
            objective = clean_text(text[len("<local-command-stdout>Goal set:"):].split("</local-command-stdout>")[0], 1000)
            goal = {"command": "/goal", "objective": objective, "state": "active"}
            continue
        if text.startswith("<local-command-stdout>Goal cleared"):
            if goal and goal["command"] == "/goal":
                goal = {**goal, "state": "done"}
            continue
        found = COMMAND.search(text)
        if not found or not text.lstrip().startswith(("<command-name>", "<command-message>")):
            continue
        name = found.group(1)
        args_match = COMMAND_ARGS.search(text)
        args = clean_text(args_match.group(1), 1000) if args_match else ""
        if name == "/clear":
            goal = None
        elif name == "/goal":
            if args.lower() in ("", "clear", "stop", "off", "cancel", "status"):
                if args.lower() != "status" and goal and goal["command"] == "/goal":
                    goal = {**goal, "state": "done"}
            else:
                goal = {"command": "/goal", "objective": args, "state": "active"}
        elif name in commands:
            goal = {"command": name, "objective": args, "state": "active"}
            if name == "/loop" and INTERVAL.match(args):
                goal["interval"] = True
        elif goal and goal["command"] != "/goal" and name not in NEUTRAL_COMMANDS:
            # The person moved on to another command: that goal command's run is over.
            goal = None
    if goal and goal["command"] == "/loop" and goal["state"] == "active":
        if wakeup and wakeup["name"] == "ScheduleWakeup" and wakeup["stop"]:
            goal["state"] = "done"
        elif goal.pop("interval", False) or (wakeup and wakeup["name"] in ("ScheduleWakeup", "CronCreate")):
            goal["state"] = "scheduled"
    if goal:
        goal.pop("interval", None)
    return goal


def codex_goal(inputs, commands=GOAL_COMMANDS):
    """What a Codex turn's own input says about its goal: a goal dict (a goal
    command, or a plainly stated objective), "end" (the person ran another
    command or asked something new), or None (only unblocker answers, or no input)."""
    for text in reversed(inputs if isinstance(inputs, list) else []):
        if not isinstance(text, str) or "[omni-message:" in text or not text.strip():
            continue
        words = text.strip().split(None, 1)
        if words[0] in commands:
            return {"command": words[0], "objective": clean_text(words[1] if len(words) > 1 else "", 1000),
                    "state": "active"}
        if words[0][:1] in "/$" and words[0] not in NEUTRAL_COMMANDS:
            return "end"
        return stated_goal(text) or "reply"
    return None


def questions_of(tool_input):
    """AskUserQuestion's questions and their option labels, for the unblocker to choose from."""
    found = []
    for question in (tool_input.get("questions") if isinstance(tool_input.get("questions"), list) else [])[:4]:
        if not isinstance(question, dict) or not isinstance(question.get("question"), str):
            continue
        found.append({"question": question["question"][:1000], "multiSelect": question.get("multiSelect") is True,
                      "options": [{"label": option.get("label"), "description": clean_text(option.get("description"), 300)}
                                  for option in question.get("options", [])
                                  if isinstance(option, dict) and isinstance(option.get("label"), str)][:6],
                      })
    return found


def tool_summary(tool_input):
    try:
        text = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = ""
    return clean_text(text, MAX_TOOL_INPUT)


def claude_event(payload, now=None, home=None):
    """Map a Claude Code hook payload to a block event, or None to ignore it."""
    if not isinstance(payload, dict):
        return None
    session = _session("claude", payload.get("session_id"))
    if not session:
        return None
    name = payload.get("hook_event_name")
    event = {"session_id": session, "provider": "claude", "ts": now or time.time(),
             "cwd": clean_text(payload.get("cwd"), 1024)}
    if name == "UserPromptSubmit":
        return {**event, "event": "resume"}
    transcript = clean_text(payload.get("transcript_path"), 1024)
    if name in ("Stop", "PermissionRequest"):
        event["goal"] = session_goal(transcript, home, goal_commands(owner_policy(home)))
    if name == "Stop":
        prompt = clean_text(payload.get("last_assistant_message"), MAX_PROMPT) or \
            transcript_tail(payload.get("transcript_path"), home)
        return {**event, "event": "stop", "kind": IDLE, "prompt": prompt, "transcript_path": transcript}
    if name == "PermissionRequest":
        tool = clean_text(payload.get("tool_name"), 200)
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        request = {"tool_name": tool, "tool_input": tool_summary(tool_input)}
        try:
            full = json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            full = ""
        # The floor reads the whole input; a cut summary could hide a path or a tail.
        request["protected"] = bool(NEVER_ALLOW.search(full)) or len(full) > MAX_TOOL_INPUT or not full
        if tool == "AskUserQuestion":
            request["questions"] = questions_of(tool_input)
        return {**event, "event": "permission", "kind": PERMISSION, "transcript_path": transcript,
                "prompt": clean_text(f"{tool}: {request['tool_input']}", MAX_PROMPT), "request": request}
    if name == "PermissionDenied":
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        exact = grant_key(clean_text(payload.get("tool_name"), 200), tool_input)
        return {**event, "event": "denied",
                "denial": {"tool_name": clean_text(payload.get("tool_name"), 200),
                           "tool_input": tool_summary(payload.get("tool_input")),
                           "reason": clean_text(payload.get("reason"), MAX_REASON),
                           # The exact refused call, so a go-ahead can pre-approve that call and no other.
                           "exact": exact if exact and len(exact) <= MAX_TOOL_INPUT else "",
                           "protected": not exact or len(exact) > MAX_TOOL_INPUT or bool(NEVER_ALLOW.search(exact))}}
    if name == "Notification":
        notification = payload.get("notification_type")
        message = clean_text(payload.get("message"), MAX_PROMPT)
        if notification == "auth_success":
            return None
        if notification == "idle_prompt" or (notification is None and message == CLAUDE_IDLE_MESSAGE):
            kind = IDLE
        elif notification == "permission_prompt":
            kind = PERMISSION
        else:
            # elicitation_dialog, a newer type, or no type with unknown text: fail closed.
            kind = INPUT
        prompt = message
        if kind == IDLE:
            prompt = transcript_tail(payload.get("transcript_path"), home) or message
        return {**event, "event": "notification", "kind": kind, "prompt": prompt,
                "transcript_path": clean_text(payload.get("transcript_path"), 1024)}
    return None


def codex_event(payload, now=None, home=None):
    """Map a Codex `notify` payload. Codex only reports turn completion."""
    if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
        return None
    session = _session("codex", payload.get("thread-id") or payload.get("thread_id"))
    if not session:
        return None
    event = {"session_id": session, "provider": "codex", "ts": now or time.time(), "event": "stop",
             "kind": IDLE, "cwd": clean_text(payload.get("cwd"), 1024),
             "prompt": clean_text(payload.get("last-assistant-message"), MAX_PROMPT)}
    goal = codex_goal(payload.get("input-messages"), goal_commands(owner_policy(home)))
    if isinstance(goal, dict):
        event["goal"] = goal
    elif goal == "end":
        event["goal_end"] = True
    return event


def notification_for(block):
    """What `omni hook` raises for a protected block. Text is shown as plain text."""
    what = "approval" if block["kind"] == PERMISSION else "input in a dialog"
    provider = block["provider"].capitalize()
    body = (f"{provider} session {block['session_id']} is waiting for {what}. OmniTerm does not answer these; "
            f"open the session to decide.\n\nWorkspace: {block.get('cwd') or 'unknown'}\n\n{block.get('prompt') or ''}")
    return {"op": "notify", "title": f"{provider} is waiting for your {what}", "body": body[:4000],
            "severity": "urgent", "dedupe_key": "blocked:" + block["session_id"]}


# Plan approval is the person's: a plan is a proposal, never auto-accepted.
NEVER_HOLD = frozenset(("ExitPlanMode", "EnterPlanMode"))


def holdable(event):
    """Whether a PermissionRequest may wait for the unblocker at all."""
    request = event.get("request") if isinstance(event, dict) and isinstance(event.get("request"), dict) else {}
    return (isinstance(event, dict) and event.get("event") == "permission" and goal_active(event) and
            request.get("tool_name") not in NEVER_HOLD and request.get("protected") is False)


# A go-ahead for a refused call pre-approves exactly that call, once, for this long.
ASK_SETTLE_SECONDS = 5  # lets a goal hook re-run the turn before the unblocker looks
SKIP_HOLD_SECONDS = 1800  # a goal loop left for the person is looked at again after this
CONTINUE_SECONDS = 600  # a Stop this soon after the last one, with no prompt between, is the same wait
GRANT_SECONDS = 1800
GRANT_NOTE = ("OmniTerm has pre-approved, once, exactly the command{s} that auto mode refused: {names}. "
              "Run {it} again unchanged; any other command goes through the usual checks.")


def grant_key(tool_name, tool_input):
    """The comparable form of a tool call: a Bash command with its whitespace
    collapsed, any other tool's input as sorted JSON."""
    if not tool_name or not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        command = tool_input.get("command")
        return "Bash:" + " ".join(command.split()) if isinstance(command, str) and command.strip() else ""
    try:
        return tool_name + ":" + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def live_grants(block, now):
    return [grant for grant in block.get("grants", []) if not grant.get("used") and grant.get("expires", 0) > now]


def pretool_installed(home=None):
    """Whether Claude's settings carry OmniTerm's PreToolUse hook."""
    try:
        settings = json.loads(((Path(home) if home else Path.home()) / ".claude/settings.json").read_text())
        return any(_is_ours(hook.get("command"), "claude")
                   for group in settings.get("hooks", {}).get("PreToolUse", []) if isinstance(group, dict)
                   for hook in group.get("hooks", []) if isinstance(hook, dict))
    except (OSError, ValueError, AttributeError):
        return False


def pretool_output():
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "Pre-approved by the Omni Unblocker for this goal"}}


def goal_active(block):
    goal = block.get("goal")
    return isinstance(goal, dict) and goal.get("state") == "active"


def goal_key(goal):
    """Which goal a block works toward, so a release holds for that goal only."""
    if not isinstance(goal, dict):
        return ""
    text = f"{goal.get('command') or ''}\n{goal.get('objective') or ''}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def person_spoke_since(path, since, home=None, max_bytes=1048576):
    """Whether the person typed into a Claude session after `since` (epoch
    seconds). Claude Code marks what the person types origin.kind "human"; a
    loop's prompt, goal-check feedback and relayed messages are not."""
    if not isinstance(path, str) or not path:
        return False
    root = (Path(home) if home else Path.home()) / ".claude/projects"
    try:
        candidate = Path(path).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            return False
        with open(candidate, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            data = handle.read()
    except OSError:
        return False
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        origin = entry.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "human":
            continue
        try:
            at = datetime.datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        return at > since
    return False


def worked_since(path, since, home=None, max_bytes=1048576):
    """Whether a Claude session called any tool after `since`: a round of the
    unblocker that was followed by work counts as progress."""
    if not isinstance(path, str) or not path:
        return False
    root = (Path(home) if home else Path.home()) / ".claude/projects"
    try:
        candidate = Path(path).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            return False
        with open(candidate, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            data = handle.read()
    except OSError:
        return False
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        if '"tool_use"' not in line:
            continue
        try:
            entry = json.loads(line)
            at = datetime.datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "tool_use" for part in content):
            return at > since
    return False


RELEASE_ROUNDS = 3  # rounds in a row the unblocker could not move a goal before it ends it
RELEASED_DIR = "Library/Application Support/OmniTerm/released-goals"


def _release_marker(session, home=None):
    return (Path(home) if home else Path.home()) / RELEASED_DIR / (session.replace(":", "-") + ".json")


def write_release_marker(session, goal, at, reason, home=None):
    """Tell the session's own Stop hook that OmniTerm ended this goal."""
    path = _release_marker(session, home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"goal": goal, "at": at, "reason": reason}))
        os.replace(temporary, path)
    except OSError:
        pass


def clear_release_marker(session, home=None):
    try:
        _release_marker(session, home).unlink()
    except OSError:
        pass


def stop_output(payload, home=None):
    """Stop hook reply for a goal OmniTerm released: end the turn. Claude Code
    then pauses the goal ("a hook ended the turn"), and only the person typing
    resumes it; loop prompts and relayed messages do not. None otherwise."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return None
    session = _session("claude", payload.get("session_id"))
    if not session:
        return None
    path = _release_marker(session, home)
    try:
        marker = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    transcript = clean_text(payload.get("transcript_path"), 1024)
    goal = session_goal(transcript, home, goal_commands(owner_policy(home)))
    if goal_key(goal) != marker.get("goal") or person_spoke_since(transcript, marker.get("at", 0), home):
        clear_release_marker(session, home)
        return None
    reason = clean_text(marker.get("reason"), 300)
    return {"continue": False,
            "stopReason": f"OmniTerm ended this goal: {reason} Type a message to resume it, or /goal clear."}


# Terminals whose AppleScript can type into the session that runs on a tty.
TYPING_HOSTS = {"/iTerm.app/": "com.googlecode.iterm2", "/OmniTerm.app/": "com.omniterm.app"}
TYPE_SCRIPT = """on run argv
  set wanted to item 1 of argv
  tell application id "BUNDLE"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is wanted then
            tell s to write text (item 2 of argv)
            return "typed"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run"""


def _claude_process(raw, home=None):
    """The live Claude process of a session, from Claude Code's own session files."""
    for path in ((Path(home) if home else Path.home()) / ".claude/sessions").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("sessionId") == raw and isinstance(entry.get("pid"), int):
            try:
                os.kill(entry["pid"], 0)
            except OSError:
                continue
            return entry["pid"], path
    return None, None


def _ps(pid, field):
    try:
        return subprocess.run(["/bin/ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def type_goal_clear(session, home=None, wait=90, sleep=time.sleep):
    """Type /goal clear into a released Claude session's terminal: Claude Code
    clears a goal only from typed input, never from a relayed message. Runs only
    once the session is idle, through the terminal's own AppleScript, and only
    for terminals that have one. Returns what happened."""
    raw = session.split(":", 1)[1] if session.startswith("claude:") else ""
    pid, record = _claude_process(raw, home)
    if not pid:
        return "no_process"
    for _ in range(max(1, wait // 3)):
        try:
            status = json.loads(record.read_text()).get("status")
        except (OSError, ValueError):
            status = None
        if status == "idle":
            break
        sleep(3)
    else:
        return "busy"
    tty = _ps(pid, "tty")
    if not tty or tty.startswith("?"):
        return "no_tty"
    bundle, parent = None, pid
    for _ in range(12):
        parent = _ps(parent, "ppid")
        if not parent.isdigit() or parent in ("0", "1"):
            break
        command = _ps(parent, "comm")
        bundle = next((value for marker, value in TYPING_HOSTS.items() if marker in command), None)
        if bundle:
            break
    if not bundle:
        return "no_terminal"
    try:
        # The id is compiled in: a terminal's own words (sessions, tty, write text)
        # resolve only against an application named in the script itself.
        result = subprocess.run(["/usr/bin/osascript", "-e", TYPE_SCRIPT.replace("BUNDLE", bundle),
                                 "/dev/" + tty, "/goal clear"],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return result.stdout.strip() or "failed"


RELEASE_NOTE = """OmniTerm is releasing this goal: it cannot move on without the owner, so nobody will push it further.
Why: {reason}

Stop working toward it now. Cancel any /loop or scheduled wake-up you set for it (CronList, then CronDelete; or ScheduleWakeup with stop), and when a goal check fires again with nothing changed, answer in one line without doing anything. Reply once, for the owner: what is done, what is blocked and why, and the exact command or decision that would unblock it. Then end your turn. From now on OmniTerm ends this session's turns, which pauses the goal until the owner types; /goal clear ends it."""


def permission_output(decision, tool_input=None):
    """The PermissionRequest hook's reply to an allow, or None to show the dialog."""
    if not isinstance(decision, dict) or decision.get("behavior") != "allow":
        return None
    result = {"behavior": "allow"}
    if decision.get("answers"):
        # AskUserQuestion takes the person's choices as answers beside its own questions.
        result["updatedInput"] = {**(tool_input if isinstance(tool_input, dict) else {}), "answers": decision["answers"]}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": result}}


class BlockStore:
    """One document per session, `block:<session_id>`, kept by the knowledge worker."""

    def __init__(self, read, write, scan, clock=None):
        self.read, self.write, self.scan = read, write, scan
        self.clock = clock or time.time
        # Pre-approval works only while the PreToolUse hook is installed.
        self.grants_enabled = True
        # Where Claude transcripts live; the person's own prompts end a release.
        self.home = None

    def record(self, event):
        if not isinstance(event, dict):
            raise BlockError("event must be an object")
        session = event.get("session_id")
        if not isinstance(session, str) or not re.fullmatch(r"(claude|codex):[A-Za-z0-9][A-Za-z0-9_-]{0,199}", session):
            raise BlockError("session_id must be provider-qualified")
        kind = event.get("kind")
        name = event.get("event")
        if name not in ("resume", "stop", "notification", "permission", "denied"):
            raise BlockError("event must be resume, stop, notification, permission or denied")
        if name not in ("resume", "denied") and kind not in ANSWERABLE_KINDS | PROTECTED_KINDS:
            raise BlockError("kind must be idle, permission or input")
        now = self.clock()
        ts = event.get("ts") if isinstance(event.get("ts"), (int, float)) else now
        key = KEY_PREFIX + session
        block = self.read(key) or {}
        if block and ts < block.get("updated", 0):
            return {"block": block, "escalate": False}  # an older event arrived late
        is_open = block.get("state") == "open"
        if name == "denied":
            # An auto-mode classifier denial: kept so the unblocker knows what was
            # refused when the session later stops. It changes no block state.
            denial = event.get("denial") if isinstance(event.get("denial"), dict) else {}
            block = {**block, "session_id": session, "provider": session.split(":")[0],
                     "updated": max(block.get("updated", 0), ts),
                     "denials": (block.get("denials", []) + [{**denial, "at": ts}])[-5:]}
            self.write(key, block)
            return {"block": block if block.get("block_id") else None, "escalate": False}
        if name == "permission":
            goal = event.get("goal") if isinstance(event.get("goal"), dict) else None
            held = holdable(event) and block.get("auto_unblock") is not False
            updated = {**block, "session_id": session, "provider": "claude", "kind": PERMISSION, "state": "open",
                       "updated": ts, "created": ts, "block_id": str(uuid.uuid4()),
                       "cwd": clean_text(event.get("cwd"), 1024) or block.get("cwd", ""),
                       "prompt": clean_text(event.get("prompt"), MAX_PROMPT),
                       "transcript_path": clean_text(event.get("transcript_path"), 1024) or block.get("transcript_path", ""),
                       "request": event.get("request") if isinstance(event.get("request"), dict) else {},
                       "goal": goal, "held": held, "hold_until": ts + HOLD_SECONDS if held else None}
            for stale in ("resolved_at", "skip_reason", "decision"):
                updated.pop(stale, None)
            self.write(key, updated)
            # A held dialog waits for the unblocker first; the hook escalates if it does not decide.
            return {"block": updated, "escalate": not held}
        if name == "resume":
            if is_open:
                block = {**block, "state": "resolved", "updated": ts, "resolved_at": ts}
                self.write(key, block)
            return {"block": block or None, "escalate": False}
        # An open dialog stays protected until the turn really ends (Stop) or
        # the person replies; an idle reminder must not downgrade it.
        if is_open and block.get("kind") in PROTECTED_KINDS and kind == IDLE and name != "stop":
            block = {**block, "updated": ts}
            self.write(key, block)
            return {"block": block, "escalate": False}
        released = block.get("released") if isinstance(block.get("released"), dict) else None
        if released and kind == IDLE and name in ("stop", "notification"):
            # A notification carries no goal: Claude's "waiting for input" after a
            # released turn is the same goal and must not reopen it.
            goal = event.get("goal") if name == "stop" and isinstance(event.get("goal"), dict) else None
            if name != "stop" or block.get("provider", session.split(":")[0]) != "claude":
                goal = goal or (None if event.get("goal_end") else block.get("goal"))
            path = clean_text(event.get("transcript_path"), 1024) or block.get("transcript_path", "")
            if goal_key(goal) == released.get("goal") and not person_spoke_since(path, released.get("at", 0), self.home):
                # Released for this goal: its loop and goal checks end turns with
                # nothing for anyone until the person speaks or the goal changes.
                block = {**block, "state": "released", "updated": ts}
                self.write(key, block)
                if block.get("provider", session.split(":")[0]) == "claude" and \
                        not _release_marker(session, self.home).exists():
                    write_release_marker(session, released.get("goal"), released.get("at", ts),
                                         block.get("skip_reason") or "only the owner can move it.", self.home)
                return {"block": block, "escalate": False}
            block = {key_: value for key_, value in block.items() if key_ not in ("released", "skip_streak")}
            clear_release_marker(session, self.home)
        # Every Stop is a new turn: a /goal hook or a queued message may have
        # continued the session without a UserPromptSubmit.
        # A /goal or /loop hook that keeps the session going ends a turn every
        # few seconds with nobody typing. Those turns continue the same wait, so
        # the block keeps its age and id instead of starting over each Stop.
        continuing = (name == "stop" and kind == IDLE and block.get("kind") == IDLE
                      and block.get("state") in ("open", "skipped") and ts - block.get("updated", 0) < CONTINUE_SECONDS)
        if continuing and block.get("state") == "skipped" and ts - block.get("skipped_at", 0) < SKIP_HOLD_SECONDS and not any(
                denial.get("at", 0) > block.get("skipped_at", 0) for denial in block.get("denials", [])):
            # Left for the person recently and nothing new was refused: stay left.
            block = {**block, "updated": ts}
            self.write(key, block)
            return {"block": block, "escalate": False}
        opens = not is_open or block.get("kind") != kind or (name == "stop" and not continuing)
        updated = {**block, "session_id": session, "provider": event.get("provider") or session.split(":")[0],
                   "kind": kind, "state": "open", "updated": ts,
                   "cwd": clean_text(event.get("cwd"), 1024) or block.get("cwd", ""),
                   "prompt": clean_text(event.get("prompt"), MAX_PROMPT) or (block.get("prompt", "") if not opens else ""),
                   "transcript_path": clean_text(event.get("transcript_path"), 1024) or block.get("transcript_path", "")}
        if name == "stop":
            # Claude's goal is read afresh from the transcript; Codex reports only the
            # turn's own input, so its last stated goal carries over.
            goal = event.get("goal") if isinstance(event.get("goal"), dict) else None
            if updated["provider"] == "claude":
                updated["goal"] = goal
            else:
                # A short reply keeps the goal going; another command ends it.
                updated["goal"] = goal or (None if event.get("goal_end") else block.get("goal"))
            updated["held"] = False
        updated["transcript_size"] = transcript_size(updated["transcript_path"])
        if opens:
            updated.update({"block_id": str(uuid.uuid4()), "created": ts})
            updated.pop("resolved_at", None)
            updated.pop("skip_reason", None)
            updated.pop("decision", None)
            if name == "stop":
                updated["denials"] = [denial for denial in block.get("denials", []) if ts - denial.get("at", 0) < 3600]
        self.write(key, updated)
        return {"block": updated, "escalate": opens and kind in PROTECTED_KINDS}

    def _all(self):
        now = self.clock()
        blocks = []
        for key, value in self.scan(KEY_PREFIX):
            if not isinstance(value, dict):
                continue
            # A session switched off keeps its setting; anything else expires.
            if now - value.get("updated", 0) > RETENTION_SECONDS and value.get("auto_unblock") is not False:
                self.write(key, None)
                continue
            if value.get("block_id") and self._due_again(value, now):
                # A goal session left for the person gets another look after a
                # while, even when it never ends another turn (a /goal that
                # Claude Code paused sits silent). Each look waits twice as long.
                value = {**value, "state": "open", "created": now, "updated": now,
                         "reopened": value.get("reopened", 0) + 1}
                value.pop("skip_reason", None)
                self.write(key, value)
            if value.get("block_id"):
                blocks.append(value)
        return blocks

    @staticmethod
    def _due_again(block, now):
        if block.get("state") != "skipped" or block.get("kind") not in ANSWERABLE_KINDS or \
                block.get("auto_unblock") is False or not goal_active(block):
            return False
        hold = SKIP_HOLD_SECONDS * 2 ** min(block.get("reopened", 0), 4)
        return now - (block.get("skipped_at") or block.get("updated", now)) >= hold

    def held(self, block):
        """A permission dialog still waiting for the unblocker's decision."""
        return (block.get("kind") == PERMISSION and block.get("held") is True and block.get("state") == "open" and
                self.clock() < (block.get("hold_until") or 0) and block.get("auto_unblock") is not False and
                goal_active(block))

    def public(self, block):
        now = self.clock()
        answers = [answer for answer in block.get("answers", []) if now - answer.get("at", 0) < 3600]
        idle = (block.get("state") == "open" and block.get("kind") in ANSWERABLE_KINDS and goal_active(block) and
                len(answers) < ANSWERS_PER_HOUR and block.get("auto_unblock") is not False)
        return {key: block.get(key) for key in ("block_id", "session_id", "provider", "kind", "state", "cwd",
                                                "prompt", "created", "updated", "skip_reason", "goal",
                                                "request", "decision", "hold_until", "release_message")} | {
            "age_seconds": round(now - block.get("created", now)),
            "auto_unblock": block.get("auto_unblock") is not False,
            "held": self.held(block),
            "denials": [{**{key: value for key, value in denial.items() if key != "exact"}, "index": index,
                         "grantable": self.grants_enabled and bool(denial.get("exact")) and
                         denial.get("protected") is False}
                        for index, denial in enumerate(block.get("denials", [])[-5:])],
            "grants": [{"tool_name": grant.get("tool_name"), "expires": grant.get("expires"),
                        "used": bool(grant.get("used"))} for grant in block.get("grants", [])[-5:]],
            "answerable": idle or self.held(block),
            "asks_person": asks_person(block.get("prompt")),
            "answers_last_hour": len(answers)}

    def list(self, arguments):
        state = arguments.get("state", "open")
        if state not in ("open", "all"):
            raise BlockError("state must be open or all")
        min_age = arguments.get("min_age_seconds", 0)
        limit = arguments.get("limit", 50)
        if type(min_age) is not int or not 0 <= min_age <= 86400 or type(limit) is not int or not 1 <= limit <= 200:
            raise BlockError("min_age_seconds and limit must be bounded integers")
        blocks = [self.public(block) for block in self._all()
                  if state == "all" or block.get("state") == "open"]
        # A held dialog, and a turn that ends by asking for the person, are looked
        # at once: there is nothing to wait for.
        blocks = [block for block in blocks if block["age_seconds"] >= min_age or block["held"] or
                  (block["asks_person"] and block["age_seconds"] >= ASK_SETTLE_SECONDS)]
        if arguments.get("answerable_only") is True:
            blocks = [block for block in blocks if block["answerable"]]
        blocks.sort(key=lambda block: block.get("created") or 0)
        # Over the limit, the newest are kept: old waits must not hide new ones.
        return {"blocks": blocks[-limit:]}

    def find(self, block_id):
        if not isinstance(block_id, str) or not BLOCK_ID.fullmatch(block_id):
            raise BlockError("block_id must be a block UUID")
        for block in self._all():
            if block.get("block_id") == block_id:
                return block
            if any(answer.get("block_id") == block_id for answer in block.get("answers", [])):
                return block
        raise BlockError("Block not found", "block_not_found")

    def claim_answer(self, arguments):
        """Validate an answer without recording it; call `record_answer` once it is sent."""
        block = self.find(arguments.get("block_id"))
        message = clean_text(arguments.get("message"), MAX_ANSWER + 1)
        reason = clean_text(arguments.get("reason"), MAX_REASON + 1)
        if not message or len(message) > MAX_ANSWER:
            raise BlockError(f"message must be 1 to {MAX_ANSWER} characters")
        if not reason or len(reason) > MAX_REASON:
            raise BlockError(f"reason must be 1 to {MAX_REASON} characters")
        previous = next((answer for answer in block.get("answers", [])
                         if answer.get("block_id") == arguments["block_id"]), None)
        if previous:
            return {"block": block, "answer": previous, "replay": True}
        if block.get("state") != "open":
            raise BlockError("This block is no longer open", "block_closed")
        if block.get("kind") not in ANSWERABLE_KINDS:
            raise BlockError("Only idle sessions may be answered; permission, trust and dialog prompts go to the "
                             "person", "block_protected")
        if block.get("auto_unblock") is False:
            raise BlockError("Auto-unblock is off for this session", "unblock_disabled")
        if not goal_active(block):
            raise BlockError("The session is not working toward a stated goal (a /goal, a loop or a goal command); "
                             "it is left for the person", "no_goal")
        now = self.clock()
        if now - block.get("created", now) < MIN_ANSWER_AGE:
            raise BlockError("The session has not been idle long enough to answer", "block_too_recent")
        recent = [answer for answer in block.get("answers", []) if now - answer.get("at", 0) < 3600]
        if len(recent) >= ANSWERS_PER_HOUR:
            raise BlockError(f"This session was already answered {ANSWERS_PER_HOUR} times in the last hour",
                             "rate_limited")
        if moved_on(block.get("transcript_path"), block.get("transcript_size")):
            raise BlockError("The session has moved on since it stopped; nothing was sent", "block_stale")
        grants = self._grants(block, arguments.get("grant"), now)
        if grants:
            names = ", ".join(f"`{grant['summary']}`" for grant in grants)
            message = (message + "\n\n" + GRANT_NOTE.format(s="" if len(grants) == 1 else "s", names=names,
                                                            it="it" if len(grants) == 1 else "them"))[:MAX_ANSWER * 3]
        answer = {"block_id": block["block_id"], "request_id": block["block_id"], "at": now,
                  "message": message, "reason": reason, "grants": grants}
        return {"block": block, "answer": answer, "replay": False}

    def _grants(self, block, indexes, now):
        """The refused calls a go-ahead names (`grant`: denial indexes), checked
        against the floor on their full text; never anything the session did not try."""
        if indexes is None or indexes == []:
            return []
        if not self.grants_enabled:
            raise BlockError("Pre-approval needs OmniTerm's PreToolUse hook (omni hooks install); name the "
                             "action in the message instead", "grants_unavailable")
        denials = block.get("denials", [])[-5:]
        if not isinstance(indexes, list) or any(type(index) is not int for index in indexes):
            raise BlockError("grant must be a list of denial indexes")
        grants = []
        for index in sorted(set(indexes)):
            if not 0 <= index < len(denials):
                raise BlockError(f"grant {index} is not one of this block's denials", "invalid_grant")
            denial = denials[index]
            exact = denial.get("exact") or ""
            if not exact or denial.get("protected") is not False or NEVER_ALLOW.search(exact):
                raise BlockError("That refused call touches production, personal files, secrets or permanent "
                                 "deletion, or was not recorded in full; only the person may allow it",
                                 "block_protected")
            grants.append({"tool_name": denial.get("tool_name"), "exact": exact, "expires": now + GRANT_SECONDS,
                           "summary": clean_text(denial.get("tool_input"), 160)})
        return grants

    def decide(self, arguments):
        """The unblocker's decision on a held permission dialog. The hook waiting on
        it reads the decision back; nothing is sent to the session."""
        block = self.find(arguments.get("block_id"))
        decision = arguments.get("decision", "allow")
        reason = clean_text(arguments.get("reason"), MAX_REASON + 1)
        if decision != "allow":
            raise BlockError("A held dialog can only be allowed; skip it to leave it for the person")
        if not reason or len(reason) > MAX_REASON:
            raise BlockError(f"reason must be 1 to {MAX_REASON} characters")
        if block.get("block_id") != arguments["block_id"] or not self.held(block):
            raise BlockError("This dialog is no longer held for the unblocker; it is the person's", "block_closed")
        request = block.get("request") or {}
        if request.get("protected") is not False or NEVER_ALLOW.search(request.get("tool_input", "")):
            raise BlockError("This action touches production, personal files, secrets or permanent deletion; "
                             "only the person may allow it", "block_protected")
        result = {"behavior": "allow", "reason": reason, "at": self.clock()}
        if request.get("tool_name") == "AskUserQuestion":
            result["answers"] = self._answers(request.get("questions") or [], arguments.get("answers"))
        updated = {**block, "state": "decided", "updated": self.clock(), "decision": result}
        self.write(KEY_PREFIX + block["session_id"], updated)
        return {"block": self.public(updated)}

    @staticmethod
    def _answers(questions, answers):
        if not questions or not isinstance(answers, dict):
            raise BlockError("answers must give one offered option label for each question")
        chosen = {}
        for question in questions:
            value = answers.get(question["question"])
            labels = [option["label"] for option in question.get("options", [])]
            if not isinstance(value, str):
                picked = []
            elif value.strip() in labels:  # a label may itself contain a comma
                picked = [value.strip()]
            else:
                picked = [part.strip() for part in value.split(",")]
            if not picked or any(part not in labels for part in picked) or \
                    (len(picked) > 1 and not question.get("multiSelect")):
                raise BlockError(f"Answer {question['question']!r} with one of: {', '.join(labels)}", "invalid_answer")
            chosen[question["question"]] = ", ".join(picked)
        return chosen

    def next_streak(self, block):
        """Rounds in a row the unblocker looked at this goal and the session did
        no work in between, answers and skips alike. Work, the person typing or
        a new goal starts the count over."""
        key = goal_key(block.get("goal"))
        streak = block.get("skip_streak") if isinstance(block.get("skip_streak"), dict) else {}
        path = block.get("transcript_path", "")
        since = streak.get("at", 0)
        same = streak.get("goal") == key and not worked_since(path, since, self.home) and \
            not person_spoke_since(path, since, self.home)
        return {"goal": key, "count": streak.get("count", 0) + 1 if same else 1, "at": self.clock()}

    def stalled(self, block):
        """This round would be the last the goal gets: release instead of answering."""
        return block.get("kind") in ANSWERABLE_KINDS and goal_active(block) and \
            self.next_streak(block)["count"] >= RELEASE_ROUNDS

    def record_answer(self, block, answer, receipt_state):
        now = self.clock()
        updated = {**block, "skip_streak": self.next_streak(block), "state": "answered", "updated": now,
                   "answers": (block.get("answers", []) + [{**answer, "delivery": receipt_state}])[-20:],
                   "grants": (live_grants(block, now) + answer.get("grants", []))[-5:]}
        self.write(KEY_PREFIX + block["session_id"], updated)
        return updated

    def consume_grant(self, arguments):
        """PreToolUse: allow this call if a go-ahead pre-approved exactly it. Single use."""
        session = self._session_key(arguments.get("session_id"))
        key = grant_key(clean_text(arguments.get("tool_name"), 200), arguments.get("tool_input"))
        block = self.read(KEY_PREFIX + session) or {}
        now = self.clock()
        if not key or block.get("auto_unblock") is False or NEVER_ALLOW.search(key):
            return {"allow": False}
        for grant in block.get("grants", []):
            if not grant.get("used") and grant.get("expires", 0) > now and grant.get("exact") == key:
                grant["used"] = now
                self.write(KEY_PREFIX + session, {**block, "updated": max(block.get("updated", 0), now)})
                return {"allow": True, "tool_name": grant.get("tool_name"), "summary": grant.get("summary")}
        return {"allow": False}

    def any_live_grants(self):
        now = self.clock()
        return any(live_grants(value, now) for _, value in self.scan(KEY_PREFIX) if isinstance(value, dict))

    def setting(self, arguments):
        """A session's auto-unblock switch. On unless the person turned it off."""
        session = self._session_key(arguments.get("session_id"))
        block = self.read(KEY_PREFIX + session) or {}
        enabled = arguments.get("enabled")
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise BlockError("enabled must be a boolean")
            block = {**block, "session_id": session, "provider": session.split(":")[0],
                     "auto_unblock": enabled, "updated": max(block.get("updated", 0), self.clock())}
            self.write(KEY_PREFIX + session, block)
        waiting = self.public(block) if block.get("block_id") and block.get("state") == "open" else None
        return {"session_id": session, "auto_unblock": block.get("auto_unblock") is not False, "waiting": waiting}

    @staticmethod
    def _session_key(value):
        if not isinstance(value, str) or not re.fullmatch(r"(claude|codex):[A-Za-z0-9][A-Za-z0-9_-]{0,199}", value):
            raise BlockError("session_id must be provider-qualified")
        return value

    def skip(self, arguments):
        block = self.find(arguments.get("block_id"))
        reason = clean_text(arguments.get("reason"), MAX_REASON + 1)
        if not reason or len(reason) > MAX_REASON:
            raise BlockError(f"reason must be 1 to {MAX_REASON} characters")
        if block.get("state") != "open" or block.get("block_id") != arguments["block_id"]:
            raise BlockError("This block is no longer open", "block_closed")
        now = self.clock()
        updated = {**block, "state": "skipped", "updated": now, "skip_reason": reason, "skipped_at": now}
        key = goal_key(block.get("goal"))
        updated["skip_streak"] = self.next_streak(block)
        count = updated["skip_streak"]["count"]
        if (arguments.get("release") is True or count >= RELEASE_ROUNDS) and \
                block.get("kind") in ANSWERABLE_KINDS and goal_active(block):
            # Nothing anyone but the person can do moves this goal, or three rounds
            # in a row found nothing to send: end it rather than look again every
            # loop tick. The session's Stop hook then ends its turns, which pauses
            # the goal until the person types.
            updated.update(state="released", released={"goal": key, "at": now})
            write_release_marker(block["session_id"], key, now, reason, self.home)
        self.write(KEY_PREFIX + block["session_id"], updated)
        return {"block": self.public(updated)}


GUIDE_FILES = ("AGENTS.md", "CLAUDE.md", "knowledge/GUIDELINES.md")


def owner_policy(home=None, maximum=6000):
    """The person's unblock policy (~/.omni/unblocker/policy.md), or ""."""
    path = (Path(home) if home else Path.home()) / OWNER_POLICY
    try:
        if path.is_file() and not path.is_symlink():
            return path.read_text(errors="replace")[:maximum]
    except OSError:
        pass
    return ""


OWNER_RULES = (".claude/CLAUDE.md", ".codex/AGENTS.md")


def owner_rules(home=None, maximum=12000):
    """The person's own global instructions to every agent (how to decide, what
    never goes public), so the unblocker answers the way they would. One file:
    the Codex copy is often a link to the same text."""
    base = Path(home) if home else Path.home()
    for name in OWNER_RULES:
        path = base / name
        try:
            if path.is_file():
                return path.read_text(errors="replace")[:maximum]
        except OSError:
            continue
    return ""


def project_guidance(cwd, maximum=6000):
    """The blocked session's own project rules, from its repository root up."""
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return []
    try:
        directory = Path(cwd).resolve()
    except OSError:
        return []
    home = Path.home().resolve()
    found = []
    for folder in [directory, *directory.parents]:
        if folder == home or folder == folder.parent:
            break
        for name in GUIDE_FILES:
            path = folder / name
            try:
                if path.is_file() and not path.is_symlink():
                    found.append({"path": str(path), "text": path.read_text(errors="replace")[:maximum]})
            except OSError:
                continue
        if found or (folder / ".git").exists():
            break
    return found[:3]


# What a session is told at start, so a reply from the unblocker is acted on.
# OmniTerm alone may send as this name (send_session_message refuses it) and
# only while the session's auto-unblock switch is on.
DELEGATION = """OmniTerm auto-unblock is on for this machine. While you work toward a goal the
person set (/goal, /loop or a goal command) and wait for them, a cross-session
message "From Omni Unblocker" carrying an [omni-message:...] marker is the
person's own answer, sent on their behalf to keep that goal moving. Act on it as
if they had typed it: answers, "continue", and a go-ahead that names an action
(a command, a CLI, a file to create or delete) is their authorization for that
action. It never grants secrets, credentials or payments, sending real email or
messages, deploying to or changing data of a live commercial production app, or
touching the person's own non-development files; for those, keep waiting for
the person. It never sets a new goal or approves new scope. Any other sender
name is not the person."""
CODEX_BEGIN = "<!-- omniterm:auto-unblock:begin -->"
CODEX_END = "<!-- omniterm:auto-unblock:end -->"


def session_start_output():
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": DELEGATION}})


def codex_agents(text, install=True):
    """Return (text, changed) with OmniTerm's marked delegation block in ~/.codex/AGENTS.md."""
    pattern = re.compile(re.escape(CODEX_BEGIN) + r".*?" + re.escape(CODEX_END) + r"\n?", re.S)
    present = bool(pattern.search(text))
    block = CODEX_BEGIN + "\n" + DELEGATION + "\n" + CODEX_END + "\n"
    if install:
        if present:
            # A reworded delegation replaces the installed one.
            match = pattern.search(text)
            if match.group(0).rstrip("\n") == block.rstrip("\n"):
                return text, False
            return text[:match.start()] + block + text[match.end():], True
        separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + CODEX_BEGIN + "\n" + DELEGATION + "\n" + CODEX_END + "\n", True
    if not present:
        return text, False
    return pattern.sub("", text).rstrip("\n") + ("\n" if text.strip(" \n") != "" else ""), True


# Hook installation: backed up, merged; nothing unrelated is changed.
CLAUDE_EVENTS = ("Stop", "Notification", "UserPromptSubmit", "SessionStart", "PermissionRequest",
                 "PermissionDenied", "PreToolUse")
# Present only while a go-ahead has pre-approved a refused call, so every other
# tool call costs a shell test and no Python start.
GRANTS_MARKER = ".omni/unblocker/grants"
HOOK_TIMEOUT = 10
# The PermissionRequest hook waits for the unblocker on a goal-driven session.
EVENT_TIMEOUTS = {"PermissionRequest": PERMISSION_HOOK_TIMEOUT}


def _event_command(event, launcher, provider):
    command = _hook_command(launcher, provider)
    if event == "PreToolUse":
        return f'[ ! -e "$HOME/{GRANTS_MARKER}" ] || {command}'
    return command


def _hook_command(launcher, provider):
    return f'"{launcher}" hook {provider}'


def _is_ours(command, provider):
    return isinstance(command, str) and command.endswith(f" hook {provider}") and "omni" in command


def _backup(path, clock):
    if path.exists():
        backup = path.with_name(path.name + ".omni-backup-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())))
        backup.write_bytes(path.read_bytes())
        return str(backup)
    return None


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".omni-tmp")
    temporary.write_text(text)
    if path.exists():
        os.chmod(temporary, path.stat().st_mode & 0o777)
    os.replace(temporary, path)


def claude_hooks(settings, launcher, install=True):
    """Return (settings, changed) with OmniTerm's Claude hooks added or removed."""
    settings = json.loads(json.dumps(settings))
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError("Claude settings have a hooks value that is not an object; nothing was changed")
    changed = False
    for event in CLAUDE_EVENTS:
        command = _event_command(event, launcher, "claude")
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Claude settings hooks.{event} is not a list; nothing was changed")
        present = any(_is_ours(hook.get("command"), "claude") for group in groups if isinstance(group, dict)
                      for hook in group.get("hooks", []) if isinstance(hook, dict))
        timeout = EVENT_TIMEOUTS.get(event, HOOK_TIMEOUT)
        if install and not present:
            groups = groups + [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
            changed = True
        elif install:
            for group in groups:
                for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                    if isinstance(hook, dict) and _is_ours(hook.get("command"), "claude") and \
                            (hook.get("timeout") != timeout or hook.get("command") != command):
                        hook["timeout"] = timeout
                        hook["command"] = command
                        changed = True
        elif not install and present:
            kept = []
            for group in groups:
                if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                    remaining = [hook for hook in group["hooks"]
                                 if not (isinstance(hook, dict) and _is_ours(hook.get("command"), "claude"))]
                    if remaining:
                        kept.append({**group, "hooks": remaining})
                else:
                    kept.append(group)
            groups = kept
            changed = True
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings, changed


def _notify_span(text):
    """(start, end, value) of Codex's top-level `notify`; value False if unreadable, None if absent."""
    offset, lines = 0, text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.match(r"\s*\[", line):
            return None
        if re.match(r"\s*notify\s*=", line):
            body, end = line, offset + len(line)
            # A multi-line array runs until its brackets balance.
            following = index + 1
            while body.count("[") > body.count("]") and following < len(lines):
                body += lines[following]
                end += len(lines[following])
                following += 1
            raw = re.sub(r"#[^\n\"]*$", "", body.split("=", 1)[1].strip(), flags=re.M).strip()
            raw = re.sub(r",\s*]$", "]", raw.replace("\n", " ").strip())
            try:
                value = json.loads(raw)
            except ValueError:
                value = False
            return offset, end, value if isinstance(value, list) and all(isinstance(item, str) for item in value) else False
        offset += len(line)
    return None


def top_level_notify(text):
    """Codex's top-level `notify`: a list, `False` for a value we cannot read, None if absent."""
    span = _notify_span(text)
    return span[2] if span else None


def _ours(value):
    return isinstance(value, list) and len(value) >= 3 and value[1:3] == ["hook", "codex"] and "omni" in value[0]


def codex_notify(text, launcher, install=True):
    """Return (text, changed, note) for Codex's top-level `notify` program.

    Codex runs one notify program. Another one (Computer Use's turn-ended, say)
    is chained: OmniTerm records the turn, then runs it with the same payload.
    Uninstalling restores it as it was."""
    span = _notify_span(text)
    current = span[2] if span else None
    if install:
        if _ours(current):
            return text, False, None
        if current is False:
            return text, False, ("Codex's notify setting could not be read; OmniTerm left it alone. Add "
                                 f"{json.dumps([launcher, 'hook', 'codex'])} to it yourself to report Codex turns.")
        if current:
            line = "notify = " + json.dumps([launcher, "hook", "codex", "--then", *current]) + \
                "  # OmniTerm: report finished turns, then run the previous notify program\n"
            return text[:span[0]] + line + text[span[1]:], True, None
        line = "notify = " + json.dumps([launcher, "hook", "codex"]) + "  # OmniTerm: report finished turns\n"
        return line + text, True, None
    if not _ours(current):
        return text, False, None
    previous = current[current.index("--then") + 1:] if "--then" in current else []
    replacement = ("notify = " + json.dumps(previous) + "\n") if previous else ""
    return text[:span[0]] + replacement + text[span[1]:], True, None


def install_hooks(home=None, launcher=None, install=True, providers=("claude", "codex"), clock=None):
    home = Path(home or Path.home())
    launcher = str(launcher or home / ".local/bin/omni")
    clock = clock or time.time
    report = {}
    if "claude" in providers:
        path = home / ".claude/settings.json"
        raw = path.read_text() if path.exists() else "{}"
        try:
            settings = json.loads(raw or "{}")
        except ValueError:
            raise ValueError(f"{path} is not valid JSON; nothing was changed")
        if not isinstance(settings, dict):
            raise ValueError(f"{path} is not a JSON object; nothing was changed")
        updated, changed = claude_hooks(settings, launcher, install)
        entry = {"path": str(path), "changed": changed}
        if changed:
            entry["backup"] = _backup(path, clock)
            _atomic_write(path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
        report["claude"] = entry
    if "codex" in providers:
        path = home / ".codex/config.toml"
        if not path.exists() and not install:
            report["codex"] = {"path": str(path), "changed": False}
        else:
            text = path.read_text() if path.exists() else ""
            updated, changed, note = codex_notify(text, launcher, install)
            entry = {"path": str(path), "changed": changed}
            if note:
                entry["note"] = note
            if changed:
                entry["backup"] = _backup(path, clock)
                _atomic_write(path, updated)
            report["codex"] = entry
        agents = home / ".codex/AGENTS.md"
        if agents.exists() or install:
            text = agents.read_text() if agents.exists() else ""
            updated, changed = codex_agents(text, install)
            if changed:
                report["codex_agents"] = {"path": str(agents), "changed": True, "backup": _backup(agents, clock)}
                _atomic_write(agents, updated)
    return report


def hooks_status(home=None):
    home = Path(home or Path.home())
    status = {"claude": False, "codex": False}
    try:
        settings = json.loads((home / ".claude/settings.json").read_text())
        status["claude"] = all(any(_is_ours(hook.get("command"), "claude")
                                   for group in settings.get("hooks", {}).get(event, []) if isinstance(group, dict)
                                   for hook in group.get("hooks", []) if isinstance(hook, dict))
                               for event in CLAUDE_EVENTS)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        status["codex"] = _ours(top_level_notify((home / ".codex/config.toml").read_text()))
    except (OSError, ValueError):
        pass
    return status


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["type-goal-clear"] and len(sys.argv) == 3:
        print(type_goal_clear(sys.argv[2]))
