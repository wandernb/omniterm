"""Provider-native plain-text messages; persistence belongs to the native app."""

import base64
import hashlib
import html
import json
import os
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path


MAX_MESSAGE_CHARACTERS = 8000
MAX_REGISTRY_BYTES = 65536
MAX_TOKEN_BYTES = 4096
SESSION_ID = re.compile(r"^(claude|codex):([A-Za-z0-9][A-Za-z0-9_-]{0,199})$")
TOKEN = re.compile(r"^[0-9a-f]{32}$")
PEER_TIMEOUT = 3
QUEUE_TIMEOUT = 3
DISCOVERY_TIMEOUT = 12
DISCOVERY_CACHE_SECONDS = 15
MAX_PROXY_FRAME = 1024 * 1024
MAX_LOADED_PAGES = 100


def read_proxy_bytes(process, deadline, buffer, count):
    while len(buffer["data"]) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex discovery timed out")
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            raise TimeoutError("Codex discovery timed out")
        data = os.read(process.stdout.fileno(), 65536)
        if not data:
            raise MessageError("discovery_unavailable", "Codex discovery ended before a response")
        buffer["data"] += data
    data, buffer["data"] = buffer["data"][:count], buffer["data"][count:]
    return data


def write_proxy_bytes(process, data):
    remaining = memoryview(data)
    while remaining:
        written = process.stdin.write(remaining)
        if not written:
            raise MessageError("discovery_unavailable", "Codex discovery transport closed")
        remaining = remaining[written:]
    process.stdin.flush()


def write_proxy_frame(process, payload, opcode=1):
    mask = os.urandom(4)
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    elif length <= 65535:
        header += b"\xfe" + struct.pack("!H", length)
    else:
        header += b"\xff" + struct.pack("!Q", length)
    write_proxy_bytes(process, header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))


def handshake_proxy(process, deadline, buffer):
    # The CLI proxy relays raw WebSocket bytes, not app-server stdio JSON.
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    write_proxy_bytes(process, ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               f"Sec-WebSocket-Key: {key}\r\n\r\n").encode("ascii"))
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        if len(header) >= 16384:
            raise MessageError("discovery_unavailable", "Codex discovery upgrade exceeded its header limit")
        header.extend(read_proxy_bytes(process, deadline, buffer, 1))
    lines = bytes(header).split(b"\r\n")
    headers = {}
    for line in lines[1:-2]:
        name, separator, value = line.partition(b":")
        if not separator or name.lower() in headers:
            raise MessageError("discovery_unavailable", "Codex discovery returned an invalid upgrade")
        headers[name.lower()] = value.strip().lower() if name.lower() != b"sec-websocket-accept" else value.strip()
    expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
    if (lines[0].split()[:2] != [b"HTTP/1.1", b"101"] or
            headers.get(b"upgrade") != b"websocket" or
            b"upgrade" not in [value.strip() for value in headers.get(b"connection", b"").split(b",")] or
            headers.get(b"sec-websocket-accept") != expected or b"sec-websocket-extensions" in headers):
        raise MessageError("discovery_unavailable", "Codex discovery WebSocket upgrade was rejected")


def read_proxy_frame(process, deadline, buffer):
    message = bytearray()
    started = False
    for _ in range(1024):
        first, second = read_proxy_bytes(process, deadline, buffer, 2)
        final, opcode, length = bool(first & 0x80), first & 15, second & 127
        if first & 0x70 or second & 0x80 or opcode not in (0, 1, 8, 9, 10):
            raise MessageError("discovery_unavailable", "Codex discovery returned an invalid frame")
        if opcode >= 8 and (not final or length > 125):
            raise MessageError("discovery_unavailable", "Codex discovery returned an invalid control frame")
        if length == 126:
            length = struct.unpack("!H", read_proxy_bytes(process, deadline, buffer, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_proxy_bytes(process, deadline, buffer, 8))[0]
        if length > MAX_PROXY_FRAME or (opcode < 8 and len(message) + length > MAX_PROXY_FRAME):
            raise MessageError("discovery_unavailable", "Codex discovery response exceeded its frame limit")
        payload = read_proxy_bytes(process, deadline, buffer, length)
        if opcode == 8:
            raise MessageError("discovery_unavailable", "Codex discovery ended before a response")
        if opcode == 9:
            write_proxy_frame(process, payload, opcode=10)
            continue
        if opcode == 10:
            continue
        if (opcode == 1 and started) or (opcode == 0 and not started):
            raise MessageError("discovery_unavailable", "Codex discovery returned an invalid continuation")
        started = True
        message.extend(payload)
        if final:
            try:
                return json.loads(message.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise MessageError("discovery_unavailable", "Codex discovery returned invalid JSON") from exc
    raise MessageError("discovery_unavailable", "Codex discovery exceeded its frame count limit")


def native_run_alive(session):
    if (session.get("live_verified") is not True or session.get("live_evidence") != "native_process_group" or
            session.get("state") != "running"):
        return False
    group = session.get("pgid")
    if isinstance(group, bool) or not isinstance(group, int) or group < 2:
        return False
    try:
        os.kill(-group, 0)
        return True
    except OSError:
        return False


class MessageError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def plain_text(value, field, maximum, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum:
        raise MessageError("invalid_message", f"{field} must be plain text of at most {maximum} characters")
    if any((ord(character) < 32 and character not in "\n\t") or
           127 <= ord(character) <= 159 or 0xD800 <= ord(character) <= 0xDFFF
           for character in value):
        raise MessageError("invalid_message", f"{field} contains control characters")
    return value


def session_id(value):
    match = SESSION_ID.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise MessageError("invalid_session", "session_id must be a provider-qualified session ID")
    return match.groups()


def request_id(value):
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != value:
        raise MessageError("invalid_request_id", "request_id must be a UUID")
    return str(parsed)


def message_marker(receipt):
    if not isinstance(receipt, dict):
        raise MessageError("invalid_receipt", "Message receipt must be an object")
    return "[omni-message:" + request_id(receipt.get("request_id")) + "]"


def provider_message_text(receipt):
    if not isinstance(receipt, dict):
        raise MessageError("invalid_receipt", "Message receipt must be an object")
    provider, _ = session_id(receipt.get("session_id"))
    if receipt.get("provider") != provider:
        raise MessageError("invalid_receipt", "Message provider does not match its recipient")
    sender = plain_text(receipt.get("sender_name"), "sender_name", 80)
    if receipt.get("sender_session_id"):
        session_id(receipt["sender_session_id"])
        sender += " (" + receipt["sender_session_id"] + ")"
    body = plain_text(receipt.get("message"), "message", MAX_MESSAGE_CHARACTERS)
    marker = message_marker(receipt)
    if provider == "claude":
        content = f"From {sender}:\n{marker}\n{body}"
        return '<cross-session-message from-name="OmniTerm">\n' + html.escape(content) + "\n</cross-session-message>"
    return f"OmniTerm cross-session message from {sender}:\n{marker}\n{body}"


def private_json(path, maximum):
    """Read an exact owned private regular file, without following a symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        attributes = os.fstat(descriptor)
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != os.getuid() or
                attributes.st_mode & 0o077 or attributes.st_size > maximum):
            raise MessageError("unsafe_peer_metadata", "Peer metadata is not an owned private file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise MessageError("unsafe_peer_metadata", "Peer metadata is too large")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise MessageError("unsafe_peer_metadata", "Peer metadata is malformed")
        return value
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MessageError("unsafe_peer_metadata", "Peer metadata is malformed") from exc
    finally:
        os.close(descriptor)


def registry_json(path, maximum):
    """Claude metadata can be readable when its containing registry is private."""
    path = Path(path)
    if not re.fullmatch(r"[0-9]+\.json", path.name):
        raise MessageError("unsafe_peer_metadata", "Peer registry metadata has an invalid name")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        attributes = os.fstat(directory)
        if (not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != os.getuid() or
                attributes.st_mode & 0o077):
            raise MessageError("unsafe_peer_metadata", "Peer registry directory is not owned and private")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        attributes = os.fstat(descriptor)
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != os.getuid() or
                attributes.st_mode & 0o022 or attributes.st_size > maximum):
            raise MessageError("unsafe_peer_metadata", "Peer registry metadata is not an owned safe file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise MessageError("unsafe_peer_metadata", "Peer registry metadata is too large")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise MessageError("unsafe_peer_metadata", "Peer registry metadata is malformed")
        return value
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MessageError("unsafe_peer_metadata", "Peer registry metadata is malformed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def process_start(pid):
    """Use the same UTC/C-locale start token as Claude's peer registry."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 2:
        return None
    try:
        os.kill(pid, 0)
        result = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=2, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def peer_identity(connection):
    if sys.platform == "darwin":
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        library.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                      ctypes.POINTER(ctypes.c_uint)]
        library.getpeereid.restype = ctypes.c_int
        if library.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise MessageError("unverified_peer", "Cannot verify peer socket owner")
        pid = struct.unpack("i", connection.getsockopt(0, 2, struct.calcsize("i")))[0]
        return pid, uid.value
    if hasattr(socket, "SO_PEERCRED"):
        pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return pid, uid
    raise MessageError("unsupported_transport", "Peer endpoint verification is unavailable on this platform")


def provider_executable(home):
    directories = [str(home / ".local/bin"), str(home / ".codex/bin"),
                   "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    directories.extend(os.environ.get("PATH", "").split(os.pathsep))
    return shutil.which("codex", path=os.pathsep.join(dict.fromkeys(directories)))


class Messenger:
    message_marker = staticmethod(message_marker)
    provider_message_text = staticmethod(provider_message_text)

    def __init__(self, home=None, executable_resolver=None, start_reader=None,
                 identity_reader=None, connector=None, command_runner=None, clock=None,
                 endpoint_stat=None, process_factory=None, proxy_reader=None,
                 codex_discovery=None, codex_socket_stat=None, native_live_reader=None,
                 proxy_handshake=None):
        self.home = Path(home or Path.home())
        self.executable_resolver = executable_resolver or (lambda: provider_executable(self.home))
        self.start_reader = start_reader or process_start
        self.identity_reader = identity_reader or peer_identity
        self.connector = connector or socket.socket
        self.command_runner = command_runner or subprocess.run
        self.clock = clock or time.time
        self.endpoint_stat = endpoint_stat or (lambda path: path.lstat())
        self.process_factory = process_factory or subprocess.Popen
        self.proxy_reader = proxy_reader or read_proxy_frame
        self.proxy_handshake = proxy_handshake or handshake_proxy
        self._injected_discovery = codex_discovery
        self.codex_socket_stat = codex_socket_stat or (lambda path: path.stat())
        self.native_live_reader = native_live_reader or native_run_alive
        self._codex_cache = None

    def _proxy_response(self, process, identifier, deadline, buffer):
        for _ in range(128):
            frame = self.proxy_reader(process, deadline, buffer)
            if not isinstance(frame, dict):
                raise MessageError("discovery_unavailable", "Codex discovery returned an invalid response")
            if frame.get("id") != identifier:
                continue
            if "error" in frame or not isinstance(frame.get("result"), dict):
                raise MessageError("discovery_unavailable", "Codex app server rejected read-only session discovery")
            return frame["result"]
        raise MessageError("discovery_unavailable", "Codex discovery exceeded its response limit")

    @staticmethod
    def _proxy_send(process, frame):
        write_proxy_frame(process, json.dumps(frame, separators=(",", ":")).encode())

    @staticmethod
    def _close_proxy(process):
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        finally:
            if process.stdout:
                process.stdout.close()

    def discover_codex_sessions(self, force=False):
        if self._injected_discovery is not None:
            return self._injected_discovery(force=force)
        now = self.clock()
        if not force and self._codex_cache and 0 <= now - self._codex_cache["refreshed_at"] < DISCOVERY_CACHE_SECONDS:
            return {**self._codex_cache, "cached": True}
        result = {"available": False, "sessions": [], "refreshed_at": now, "cached": False,
                  "capability": "thread/loaded/list", "loaded_only": True, "truncated": False,
                  "error": None}
        process = None
        try:
            endpoint = self.home / ".codex/app-server-control/app-server-control.sock"
            attributes = self.codex_socket_stat(endpoint)
            if not stat.S_ISSOCK(attributes.st_mode) or attributes.st_uid != os.getuid():
                raise MessageError("discovery_unavailable", "Codex app-server socket is not owned by this user")
            executable = self.executable_resolver()
            if not executable:
                raise MessageError("discovery_unavailable", "Codex app-server proxy is unavailable")
            process = self.process_factory([executable, "app-server", "proxy", "--sock", str(endpoint)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
            deadline = time.monotonic() + DISCOVERY_TIMEOUT
            buffer = {"data": b""}
            self.proxy_handshake(process, deadline, buffer)
            self._proxy_send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "omniterm-session-discovery", "version": "1"}}})
            self._proxy_response(process, 1, deadline, buffer)
            self._proxy_send(process, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
            cursor, seen_cursors, identifiers = None, set(), set()
            for page in range(MAX_LOADED_PAGES):
                identifier = page + 2
                params = {"limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                self._proxy_send(process, {"jsonrpc": "2.0", "id": identifier,
                                           "method": "thread/loaded/list", "params": params})
                response = self._proxy_response(process, identifier, deadline, buffer)
                data = response.get("data")
                if not isinstance(data, list) or len(data) > 100:
                    raise MessageError("discovery_unavailable", "Codex loaded-session response has an unsupported shape")
                for raw_id in data:
                    try:
                        parsed = uuid.UUID(raw_id) if isinstance(raw_id, str) else None
                    except ValueError:
                        parsed = None
                    if parsed is None:
                        raise MessageError("discovery_unavailable", "Codex loaded-session response has an unsupported identifier")
                    identifiers.add(str(parsed))
                cursor = response.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen_cursors:
                    raise MessageError("discovery_unavailable", "Codex loaded-session pagination is invalid")
                seen_cursors.add(cursor)
            else:
                result["truncated"] = True
            checked_at = self.clock()
            result.update(available=True, refreshed_at=checked_at, sessions=[{"session_id": "codex:" + identifier,
                          "state": "running", "provider_state": "loaded", "work_state": "unknown",
                          "live_verified": True, "live_evidence": "codex_app_server_loaded",
                          "live_checked_at": checked_at} for identifier in sorted(identifiers)])
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
            result["refreshed_at"] = self.clock()
            result["error"] = str(exc) if isinstance(exc, MessageError) else "Codex app-server session discovery is unavailable"
        finally:
            if process is not None:
                try:
                    self._close_proxy(process)
                except (OSError, subprocess.SubprocessError):
                    result.update(available=False, sessions=[], error="Codex discovery helper could not shut down cleanly")
        self._codex_cache = result
        return dict(result)

    def set_codex_thread_name(self, thread_id, name):
        """Rename a Codex thread through its app-server (`thread/name/set`), as
        Codex's own rename does. Returns True when the app-server accepted it."""
        process = None
        try:
            endpoint = self.home / ".codex/app-server-control/app-server-control.sock"
            attributes = self.codex_socket_stat(endpoint)
            if not stat.S_ISSOCK(attributes.st_mode) or attributes.st_uid != os.getuid():
                return False
            executable = self.executable_resolver()
            if not executable:
                return False
            process = self.process_factory([executable, "app-server", "proxy", "--sock", str(endpoint)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
            deadline = time.monotonic() + DISCOVERY_TIMEOUT
            buffer = {"data": b""}
            self.proxy_handshake(process, deadline, buffer)
            self._proxy_send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "omniterm-session-rename", "version": "1"}}})
            self._proxy_response(process, 1, deadline, buffer)
            self._proxy_send(process, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
            self._proxy_send(process, {"jsonrpc": "2.0", "id": 2, "method": "thread/name/set",
                                       "params": {"threadId": thread_id, "name": name}})
            self._proxy_response(process, 2, deadline, buffer)
            return True
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            return False
        finally:
            if process is not None:
                try:
                    self._close_proxy(process)
                except (OSError, subprocess.SubprocessError):
                    pass

    def _codex_recipient_live(self, identifier, session, force=False):
        discovery = self.discover_codex_sessions(force=force)
        if discovery.get("available") and any(item.get("session_id") == identifier for item in discovery.get("sessions", [])):
            return True
        return isinstance(session, dict) and self.native_live_reader(session)

    def prepare(self, session, request, existing=None):
        if not isinstance(request, dict):
            raise MessageError("invalid_message", "Message request must be an object")
        allowed = {"op", "session_id", "request_id", "message", "sender_session_id", "sender_name"}
        if set(request) - allowed:
            raise MessageError("invalid_message", "Unknown message argument")
        recipient = request.get("session_id")
        provider, raw_id = session_id(recipient)
        identifier = request_id(request.get("request_id"))
        message = plain_text(request.get("message"), "message", MAX_MESSAGE_CHARACTERS)
        sender = request.get("sender_session_id")
        if sender is not None:
            session_id(sender)
        sender_name = plain_text(request.get("sender_name", "OmniTerm agent"), "sender_name", 80)
        values = {"session_id": recipient, "message": message,
                  "sender_session_id": sender, "sender_name": sender_name}
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True,
                                               ensure_ascii=False).encode()).hexdigest()
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise MessageError("request_id_conflict", "request_id was already used for another message")
            return dict(existing)
        if not isinstance(session, dict) or session.get("id", session.get("session_id")) != recipient:
            raise MessageError("session_not_found", "Recipient session does not exist")
        if provider == "codex" and not self._codex_recipient_live(recipient, session):
            raise MessageError("session_not_running", "Recipient has no verified live owner")
        if provider == "claude":
            self._claude_registry(raw_id)
        return {"request_id": identifier, **values, "provider": provider, "fingerprint": fingerprint,
                "state": "queued", "transport": "claude_peer" if provider == "claude" else "codex_queue",
                "created_at": self.clock(), "updated_at": self.clock(),
                "delivered": False, "detail": "Waiting for native durable outbox acknowledgement"}

    def claim(self, receipt):
        if receipt.get("state") != "queued" or receipt.get("attempt_id"):
            raise MessageError("invalid_receipt_state", "Only an unattempted queued message can be claimed")
        return {**receipt, "state": "sending", "attempt_id": str(uuid.uuid4()),
                "updated_at": self.clock(), "detail": "Transport reserved; awaiting native durable acknowledgement"}

    def recover(self, receipt):
        if receipt.get("state") == "sending":
            return {**receipt, "state": "unknown", "updated_at": self.clock(), "delivered": False,
                    "detail": "Application restarted during a send; not retried to avoid duplicate delivery"}
        return dict(receipt)

    def confirm_from_transcript(self, receipt, recipient, role, text, source):
        """Prove recorded user input, without claiming model processing or completion."""
        if (not isinstance(receipt, dict) or receipt.get("state") not in {"queued", "sending", "sent", "unknown"} or
                recipient != receipt.get("session_id") or role != "user" or
                not isinstance(text, str) or not isinstance(source, dict)):
            return None
        try:
            if len(text.encode("utf-8")) > 65536:
                return None
            canonical = provider_message_text(receipt)
            if canonical not in text:
                return None
            provider, _ = session_id(recipient)
            if (source.get("session_id", recipient) != recipient or source.get("provider", provider) != provider or
                    source.get("role", role) != "user"):
                return None
            path = plain_text(source.get("source_path"), "source_path", 4096)
            line = source.get("source_line")
            if isinstance(line, bool) or not isinstance(line, int) or line < 0:
                return None
            identifiers = source.get("message_ids")
            if identifiers is None:
                identifiers = [source.get("message_id")]
            if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 64:
                return None
            identifiers = list(dict.fromkeys(plain_text(identifier, "message_id", 128) for identifier in identifiers))
        except (MessageError, UnicodeError, TypeError, AttributeError):
            return None
        evidence = {"kind": "provider_user_input_record", "provider": provider,
                    "session_id": recipient, "source_path": path, "source_line": line,
                    "message_ids": identifiers, "submitted_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                    "confirmed_at": self.clock()}
        return {**receipt, "state": "delivered", "delivered": True, "updated_at": self.clock(),
                "delivery_evidence": evidence,
                "detail": "Provider transcript recorded this message as user input; processing is not confirmed"}

    def deliver(self, receipt, session):
        """Call only after native persistence has acknowledged the sending reservation."""
        if receipt.get("state") != "sending" or not receipt.get("attempt_id"):
            raise MessageError("invalid_receipt_state", "Message has no durably reserved transport attempt")
        provider, raw_id = session_id(receipt.get("session_id"))
        try:
            if provider == "claude":
                self._send_claude(raw_id, receipt)
                result = {"state": "sent", "detail": "Peer transport sent; recipient processing is unconfirmed"}
            else:
                result = self._send_codex(raw_id, receipt, session)
        except MessageError as exc:
            result = {"state": "rejected", "code": exc.code, "detail": str(exc)}
        except (OSError, subprocess.SubprocessError):
            result = {"state": "unknown", "code": "transport_uncertain",
                      "detail": "Transport outcome is uncertain; message will not be retried"}
        return {**receipt, **result, "updated_at": self.clock(), "delivered": False}

    def _claude_registry(self, raw_id):
        registry = self.home / ".claude/sessions"
        try:
            paths = list(registry.glob("[0-9]*.json"))
        except OSError as exc:
            raise MessageError("session_not_running", "Claude live registry is unavailable") from exc
        for path in paths:
            if not path.stem.isdigit():
                continue
            try:
                record = registry_json(path, MAX_REGISTRY_BYTES)
            except (OSError, MessageError):
                continue
            if record.get("sessionId") != raw_id:
                continue
            pid = int(path.stem)
            if record.get("pid") not in (None, pid):
                continue
            expected_start = record.get("procStart")
            if not isinstance(expected_start, str) or self.start_reader(pid) != expected_start:
                continue
            protocol = record.get("peerProtocol")
            if type(protocol) is not int or protocol != 1:
                raise MessageError("unsupported_transport", "Claude recipient must advertise peer protocol 1")
            endpoint = record.get("messagingSocketPath")
            if (not isinstance(endpoint, str) or not endpoint.startswith("/") or
                    endpoint.startswith("//") or os.path.abspath(endpoint) != endpoint):
                continue
            endpoint_path = Path(endpoint)
            try:
                attributes = self.endpoint_stat(endpoint_path)
                if not stat.S_ISSOCK(attributes.st_mode) or attributes.st_uid != os.getuid():
                    continue
            except OSError:
                continue
            return {**record, "pid": pid, "endpoint": endpoint_path, "procStart": expected_start}
        raise MessageError("session_not_running", "Claude recipient has no verified live peer endpoint")

    def _send_claude(self, raw_id, receipt):
        record = self._claude_registry(raw_id)
        endpoint = record["endpoint"]
        key_hash = hashlib.sha256(str(endpoint).encode()).hexdigest()
        key_path = self.home / ".claude/sessions" / f"{record['pid']}.{key_hash}.key"
        token_data = private_json(key_path, MAX_TOKEN_BYTES)
        token = token_data.get("peerToken")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise MessageError("unsafe_peer_metadata", "Claude peer authentication metadata is invalid")
        if token_data.get("procStart") not in (None, record["procStart"]):
            raise MessageError("unverified_peer", "Claude peer authentication belongs to another process")
        frame = {"msgV": 1, "msg_id": receipt["request_id"], "session_id": raw_id,
                 "type": "user", "priority": "next",
                 "message": {"role": "user", "content": provider_message_text(receipt)}}
        connection = self.connector(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(PEER_TIMEOUT)
            connection.connect(str(endpoint))
            pid, uid = self.identity_reader(connection)
            if pid != record["pid"] or uid != os.getuid() or self.start_reader(pid) != record["procStart"]:
                raise MessageError("unverified_peer", "Connected Claude peer identity changed")
            auth = json.dumps({"type": "auth", "token": token}, separators=(",", ":"))
            data = auth + "\n" + json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n"
            connection.sendall(data.encode())
            connection.shutdown(socket.SHUT_WR)
        finally:
            connection.close()

    def _send_codex(self, raw_id, receipt, session):
        if not self._codex_recipient_live(receipt["session_id"], session, force=True):
            raise MessageError("session_not_running", "Codex recipient no longer has a verified live owner")
        executable = self.executable_resolver()
        if not executable:
            raise MessageError("unsupported_transport", "Codex queue executable is unavailable")
        text = provider_message_text(receipt)
        result = self.command_runner([executable, "queue", "--thread", raw_id, "--message", text],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=QUEUE_TIMEOUT, check=False)
        if result.returncode:
            return {"state": "unknown", "code": "queue_not_confirmed",
                    "detail": "Codex queue did not confirm acceptance; not retried"}
        return {"state": "queued", "detail": "Codex accepted the queue request; recipient processing is unconfirmed"}
