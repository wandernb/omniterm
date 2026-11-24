"""Checkpointable, bounded JSON events for one provider transcript object.

Events are provisional until ``complete`` is emitted. Callers must stage them
and discard the record if validation fails, rather than indexing partial JSON.
Object keys are buffered within a bound; only value strings produce fragments.
"""

import codecs
import copy
import json
import math
import re


MAX_FEED_BYTES = 65536
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
WHITESPACE = " \t\r\n"
DELIMITERS = WHITESPACE + ",]}"
ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
           "n": "\n", "r": "\r", "t": "\t"}


class JSONStreamError(ValueError):
    code = "invalid_stream_json"


def bounded_option(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise JSONStreamError(f"{name} must be between {minimum} and {maximum}")
    return value


class IncrementalJSONParser:
    def __init__(self, fragment_size=8192, max_depth=128, max_scalar_chars=4096,
                 max_path_chars=16384):
        self.fragment_size = bounded_option(fragment_size, "fragment_size", 1, 8192)
        self.max_depth = bounded_option(max_depth, "max_depth", 1, 256)
        self.max_scalar_chars = bounded_option(max_scalar_chars, "max_scalar_chars", 1, 8192)
        self.max_path_chars = bounded_option(max_path_chars, "max_path_chars", 1, 65536)
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._input_kind = None
        self._stack = []
        self._root = "value"
        self._token = None
        self._finalized = False
        self._failed = False

    @property
    def complete(self):
        return self._finalized and not self._failed and self._root == "done"

    def _path(self):
        path = [frame["label"] for frame in self._stack[1:]]
        if self._stack:
            frame = self._stack[-1]
            path.append(frame["key"] if frame["kind"] == "object" else frame["index"])
        if sum(len(part) for part in path if isinstance(part, str)) > self.max_path_chars:
            raise JSONStreamError("JSON path exceeds its character limit")
        return tuple(path)

    @staticmethod
    def _event(kind, path, **values):
        return {"kind": kind, "path": tuple(path), **values}

    def _value_finished(self):
        if not self._stack:
            self._root = "done"
            return
        frame = self._stack[-1]
        frame["state"] = "comma_or_end"
        if frame["kind"] == "array":
            frame["index"] += 1

    def _start_value(self, character, events):
        path = self._path()
        if not self._stack and character != "{":
            raise JSONStreamError("A transcript record must be one JSON object")
        if character in "{[":
            if len(self._stack) >= self.max_depth:
                raise JSONStreamError("JSON nesting exceeds its depth limit")
            kind = "object" if character == "{" else "array"
            frame = {"kind": kind, "label": path[-1] if path else None,
                     "state": "key_or_end" if kind == "object" else "value_or_end"}
            frame.update({"key": None} if kind == "object" else {"index": 0})
            self._stack.append(frame)
            self._root = "active"
            events.append(self._event("start_" + kind, path))
        elif character == '"':
            self._start_string(False, path, events)
        elif character in "-0123456789":
            self._token = {"kind": "number", "path": list(path), "text": character}
        elif character in "tfn":
            self._token = {"kind": "literal", "path": list(path), "text": character,
                           "expected": {"t": "true", "f": "false", "n": "null"}[character]}
        else:
            raise JSONStreamError("Expected a JSON value")

    def _start_string(self, is_key, path, events):
        self._token = {"kind": "string", "path": list(path), "is_key": is_key,
                       "buffer": [], "mode": "normal", "unicode": "", "high": None}
        if not is_key:
            events.append(self._event("string_start", path))

    def _flush_string(self, events):
        token = self._token
        if token and not token["is_key"] and token["buffer"]:
            events.append(self._event("string_chunk", token["path"], value="".join(token["buffer"])))
            token["buffer"] = []

    def _append_string(self, character, events):
        token = self._token
        token["buffer"].append(character)
        if token["is_key"]:
            if len(token["buffer"]) > self.max_scalar_chars:
                raise JSONStreamError("JSON object key exceeds its character limit")
        elif len(token["buffer"]) >= self.fragment_size:
            self._flush_string(events)

    def _string_character(self, character, events):
        token = self._token
        mode = token["mode"]
        if mode == "low_slash":
            if character != "\\":
                raise JSONStreamError("An escaped high surrogate requires a low surrogate")
            token["mode"] = "low_u"
        elif mode == "low_u":
            if character != "u":
                raise JSONStreamError("An escaped high surrogate requires a low surrogate")
            token.update(mode="unicode", unicode="")
        elif mode == "unicode":
            if character not in "0123456789abcdefABCDEF":
                raise JSONStreamError("Invalid JSON Unicode escape")
            token["unicode"] += character
            if len(token["unicode"]) == 4:
                number = int(token["unicode"], 16)
                if token["high"] is not None:
                    if not 0xDC00 <= number <= 0xDFFF:
                        raise JSONStreamError("Invalid JSON surrogate pair")
                    number = 0x10000 + ((token["high"] - 0xD800) << 10) + number - 0xDC00
                    token["high"] = None
                elif 0xD800 <= number <= 0xDBFF:
                    token.update(high=number, mode="low_slash", unicode="")
                    return
                elif 0xDC00 <= number <= 0xDFFF:
                    raise JSONStreamError("Unpaired JSON low surrogate")
                self._append_string(chr(number), events)
                token.update(mode="normal", unicode="")
        elif mode == "escape":
            if character == "u":
                token.update(mode="unicode", unicode="")
            elif character in ESCAPES:
                self._append_string(ESCAPES[character], events)
                token["mode"] = "normal"
            else:
                raise JSONStreamError("Invalid JSON string escape")
        elif character == '"':
            if token["is_key"]:
                self._stack[-1].update(key="".join(token["buffer"]), state="colon")
            else:
                self._flush_string(events)
                events.append(self._event("string_end", token["path"]))
                self._value_finished()
            self._token = None
        elif character == "\\":
            token["mode"] = "escape"
        elif ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF:
            raise JSONStreamError("Invalid character in JSON string")
        else:
            self._append_string(character, events)

    def _finish_scalar(self, events):
        token = self._token
        text = token["text"]
        if token["kind"] == "number":
            if not NUMBER.fullmatch(text):
                raise JSONStreamError("Invalid JSON number")
            try:
                value = json.loads(text)
            except ValueError as error:
                raise JSONStreamError("JSON number exceeds the supported scalar range") from error
            if isinstance(value, float) and not math.isfinite(value):
                raise JSONStreamError("JSON number exceeds the supported scalar range")
        else:
            if text != token["expected"]:
                raise JSONStreamError("Incomplete JSON literal")
            value = {"true": True, "false": False, "null": None}[text]
        events.append(self._event("scalar", token["path"], value=value, raw=text))
        self._token = None
        self._value_finished()

    def _token_character(self, character, events):
        token = self._token
        if token["kind"] == "string":
            self._string_character(character, events)
            return True
        if token["kind"] == "number" and character in "-+0123456789.eE":
            token["text"] += character
        elif token["kind"] == "literal" and character not in DELIMITERS:
            token["text"] += character
            if not token["expected"].startswith(token["text"]):
                raise JSONStreamError("Invalid JSON literal")
        else:
            self._finish_scalar(events)
            return False
        if len(token["text"]) > self.max_scalar_chars:
            raise JSONStreamError("JSON scalar exceeds its character limit")
        return True

    def _structural_character(self, character, events):
        if character in WHITESPACE:
            return
        if not self._stack:
            if self._root != "value":
                raise JSONStreamError("Unexpected data after the JSON object")
            self._start_value(character, events)
            return
        frame = self._stack[-1]
        state = frame["state"]
        if frame["kind"] == "object":
            if state in ("key_or_end", "key") and character == '"':
                self._start_string(True, (), events)
                return
            if state == "colon" and character == ":":
                frame["state"] = "value"
                return
            if character == "}" and state in ("key_or_end", "comma_or_end"):
                self._close_container(events)
                return
        elif character == "]" and state in ("value_or_end", "comma_or_end"):
            self._close_container(events)
            return
        if state == "comma_or_end" and character == ",":
            frame["state"] = "key" if frame["kind"] == "object" else "value"
        elif state in ("value", "value_or_end"):
            self._start_value(character, events)
        else:
            raise JSONStreamError("Unexpected JSON punctuation")

    def _close_container(self, events):
        frame = self._stack.pop()
        path = tuple(item["label"] for item in self._stack[1:])
        if frame["label"] is not None:
            path += (frame["label"],)
        events.append(self._event("end_" + frame["kind"], path))
        self._value_finished()

    def feed(self, data=b"", final=False):
        if self._failed:
            raise JSONStreamError("A failed JSON parser cannot be resumed")
        if not isinstance(final, bool) or not isinstance(data, (bytes, str)):
            raise JSONStreamError("feed requires bytes or text and a boolean final flag")
        if self._finalized:
            if data:
                raise JSONStreamError("The JSON parser is already finalized")
            return []
        try:
            kind = "bytes" if isinstance(data, bytes) else "str"
            if data and self._input_kind not in (None, kind):
                raise JSONStreamError("Do not mix byte and text feeds")
            if data:
                self._input_kind = kind
            if len(data) > MAX_FEED_BYTES:
                raise JSONStreamError("A JSON input feed exceeds 64 KiB")
            if isinstance(data, str):
                if len(data.encode("utf-8")) > MAX_FEED_BYTES:
                    raise JSONStreamError("A JSON input feed exceeds 64 KiB")
                text = data
                if final and self._input_kind == "bytes":
                    text += self._decoder.decode(b"", final=True)
            else:
                text = self._decoder.decode(data, final=final)
            events = []
            position = 0
            while position < len(text):
                character = text[position]
                if self._token:
                    if self._token_character(character, events):
                        position += 1
                else:
                    self._structural_character(character, events)
                    position += 1
            if self._token and self._token["kind"] == "string":
                self._flush_string(events)
            if final:
                if self._token and self._token["kind"] != "string":
                    self._finish_scalar(events)
                if self._token or self._stack or self._root != "done":
                    raise JSONStreamError("Incomplete JSON object")
                self._finalized = True
                events.append(self._event("complete", ()))
            return events
        except (UnicodeError, JSONStreamError) as error:
            self._failed = True
            if isinstance(error, JSONStreamError):
                raise
            raise JSONStreamError("Invalid UTF-8 in JSON input") from error

    def snapshot(self):
        if self._failed:
            raise JSONStreamError("A failed JSON parser has no resumable checkpoint")
        buffered, decoder_flag = self._decoder.getstate()
        token = copy.deepcopy(self._token)
        if token and token["kind"] == "string":
            token["buffer"] = "".join(token["buffer"])
        return {"version": 1, "options": {"fragment_size": self.fragment_size,
                "max_depth": self.max_depth, "max_scalar_chars": self.max_scalar_chars,
                "max_path_chars": self.max_path_chars}, "stack": copy.deepcopy(self._stack),
                "root": self._root, "token": token, "input_kind": self._input_kind,
                "decoder_bytes": buffered.hex(), "decoder_flag": decoder_flag,
                "finalized": self._finalized}

    @classmethod
    def restore(cls, state):
        try:
            if not isinstance(state, dict) or state.get("version") != 1:
                raise JSONStreamError("Unsupported JSON parser checkpoint")
            parser = cls(**state["options"])
            stack = copy.deepcopy(state["stack"])
            if not isinstance(stack, list) or len(stack) > parser.max_depth:
                raise JSONStreamError("Invalid JSON parser stack checkpoint")
            for index, frame in enumerate(stack):
                if not isinstance(frame, dict) or frame.get("kind") not in ("object", "array") or "label" not in frame:
                    raise JSONStreamError("Invalid JSON parser container checkpoint")
                allowed = ("key_or_end", "key", "colon", "value", "comma_or_end") if frame["kind"] == "object" else ("value_or_end", "value", "comma_or_end")
                if frame.get("state") not in allowed:
                    raise JSONStreamError("Invalid JSON parser state checkpoint")
                label = frame.get("label")
                if index == 0 and (label is not None or frame["kind"] != "object"):
                    raise JSONStreamError("Invalid JSON root checkpoint")
                if index and (isinstance(label, bool) or not isinstance(label, (str, int))):
                    raise JSONStreamError("Invalid JSON path checkpoint")
                if isinstance(label, int) and label < 0:
                    raise JSONStreamError("Invalid JSON path checkpoint")
                if isinstance(label, str) and len(label) > parser.max_scalar_chars:
                    raise JSONStreamError("JSON checkpoint key exceeds its limit")
                if frame["kind"] == "object":
                    key = frame.get("key")
                    if key is not None and (not isinstance(key, str) or len(key) > parser.max_scalar_chars):
                        raise JSONStreamError("Invalid JSON checkpoint object key")
                    if frame["state"] in ("colon", "value", "comma_or_end") and not isinstance(key, str):
                        raise JSONStreamError("Missing JSON checkpoint object key")
                elif isinstance(frame.get("index"), bool) or not isinstance(frame.get("index"), int) or frame["index"] < 0:
                    raise JSONStreamError("Invalid JSON checkpoint array index")
            if state["root"] not in ("value", "active", "done") or not isinstance(state["finalized"], bool):
                raise JSONStreamError("Invalid JSON root state checkpoint")
            if state["input_kind"] not in (None, "bytes", "str"):
                raise JSONStreamError("Invalid JSON input checkpoint")
            buffered = bytes.fromhex(state["decoder_bytes"])
            if len(buffered) > 3 or state["decoder_flag"] != 0:
                raise JSONStreamError("Invalid UTF-8 decoder checkpoint")
            if buffered:
                probe = codecs.getincrementaldecoder("utf-8")("strict")
                if state["input_kind"] != "bytes" or probe.decode(buffered, final=False) or probe.getstate()[0] != buffered:
                    raise JSONStreamError("Invalid UTF-8 decoder checkpoint")
            parser._decoder.setstate((buffered, 0))
            token = copy.deepcopy(state["token"])
            if token is not None:
                if not isinstance(token, dict) or token.get("kind") not in ("string", "number", "literal"):
                    raise JSONStreamError("Invalid JSON token checkpoint")
                path = token.get("path")
                if not isinstance(path, list) or len(path) > parser.max_depth:
                    raise JSONStreamError("Invalid JSON token path checkpoint")
                if any(isinstance(part, bool) or not isinstance(part, (str, int)) or
                       (isinstance(part, int) and part < 0) or
                       (isinstance(part, str) and len(part) > parser.max_scalar_chars) for part in path):
                    raise JSONStreamError("Invalid JSON token path checkpoint")
                if sum(len(part) for part in path if isinstance(part, str)) > parser.max_path_chars:
                    raise JSONStreamError("JSON token checkpoint path exceeds its limit")
                if token["kind"] == "string":
                    if (not isinstance(token.get("is_key"), bool) or not isinstance(token.get("buffer"), str) or
                            "high" not in token or
                            len(token["buffer"]) > (parser.max_scalar_chars if token.get("is_key") else parser.fragment_size) or
                            token.get("mode") not in ("normal", "escape", "unicode", "low_slash", "low_u") or
                            not isinstance(token.get("unicode"), str) or len(token["unicode"]) > 3 or
                            any(character not in "0123456789abcdefABCDEF" for character in token["unicode"])):
                        raise JSONStreamError("Invalid JSON string checkpoint")
                    if token.get("high") is not None and (isinstance(token["high"], bool) or
                            not isinstance(token["high"], int) or not 0xD800 <= token["high"] <= 0xDBFF):
                        raise JSONStreamError("Invalid JSON surrogate checkpoint")
                    token["buffer"] = list(token["buffer"])
                elif not isinstance(token.get("text"), str) or len(token["text"]) > parser.max_scalar_chars:
                    raise JSONStreamError("Invalid JSON scalar checkpoint")
                elif token["kind"] == "literal" and token.get("expected") not in ("true", "false", "null"):
                    raise JSONStreamError("Invalid JSON literal checkpoint")
            parser._stack, parser._root, parser._token = stack, state["root"], token
            parser._input_kind, parser._finalized = state["input_kind"], state["finalized"]
            if ((parser._root == "active") != bool(stack) or
                    (parser._root == "done" and token is not None)):
                raise JSONStreamError("Inconsistent JSON root checkpoint")
            if sum(len(frame["label"]) for frame in stack if isinstance(frame["label"], str)) > parser.max_path_chars:
                raise JSONStreamError("JSON checkpoint path exceeds its limit")
            if parser._finalized and (parser._root != "done" or stack or token or buffered):
                raise JSONStreamError("Invalid finalized JSON checkpoint")
            return parser
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, JSONStreamError):
                raise
            raise JSONStreamError("Invalid JSON parser checkpoint") from error
