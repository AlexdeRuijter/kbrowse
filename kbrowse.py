#!/usr/bin/env python3
"""kbrowse: a keyboard-driven web browser for the kitty terminal.

kbrowse drives a stock, modern Chromium (snap, apt, or a Playwright build)
over the Chrome DevTools Protocol. It shows the page with the kitty graphics
protocol at the terminal's full pixel resolution, and it works over ssh
(including `kitten ssh`).

Chromium runs as a normal windowed browser inside a private Xvfb display by
default, so sites see a regular desktop Chrome rather than a headless one. Pages
reach the terminal as screencast frames. Keys and mouse go back as real
(trusted) input events.

To stay invisible to bot checks, kbrowse never calls Runtime.enable, never
injects scripts into pages, never overrides the user agent or the device
metrics over CDP, and runs its own small scripts (link hints, search) in an
isolated world that the page cannot see.

Only the Python 3 standard library is needed.
"""

import argparse
import base64
import collections
import fcntl
import json
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.parse
import urllib.request
import zipfile
import io

VERSION = "0.1.0"
LOG = None


def log(*parts):
    if LOG:
        LOG.write(time.strftime("%H:%M:%S") + ("%.3f " % (time.time() % 1))[1:] + " ".join(str(p) for p in parts) + "\n")
        LOG.flush()


# --------------------------------------------------------------------------
# DevTools protocol transports


class PipeTransport:
    """--remote-debugging-pipe: NUL-separated JSON on fds 3 (in) and 4 (out)."""

    def __init__(self, write_fd, read_fd):
        self.write_fd = write_fd
        self.read_fd = read_fd
        self.buf = bytearray()
        self.scan = 0
        self.lock = threading.Lock()

    def send(self, text):
        data = text.encode() + b"\0"
        with self.lock:
            view = memoryview(data)
            while view:
                view = view[os.write(self.write_fd, view):]

    def recv(self):
        while True:
            end = self.buf.find(b"\0", self.scan)
            if end >= 0:
                msg = bytes(self.buf[:end])
                del self.buf[: end + 1]
                self.scan = 0
                return msg.decode()
            self.scan = len(self.buf)
            chunk = os.read(self.read_fd, 1 << 20)
            if not chunk:
                return None
            self.buf += chunk

    def close(self):
        for fd in (self.write_fd, self.read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class WsTransport:
    """--remote-debugging-port: a minimal WebSocket client (RFC 6455)."""

    def __init__(self, port, path):
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed the connection")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise ConnectionError(head.split(b"\r\n")[0].decode(errors="replace"))
        sock.settimeout(None)
        self.sock = sock
        self.buf = bytearray(rest)
        self.lock = threading.Lock()

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buf += chunk
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def recv(self):
        parts = []
        while True:
            try:
                head = self._read(2)
                opcode, size = head[0] & 0x0F, head[1] & 0x7F
                if size == 126:
                    size = struct.unpack(">H", self._read(2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", self._read(8))[0]
                mask = self._read(4) if head[1] & 0x80 else None
                data = self._read(size)
            except (EOFError, OSError):
                return None
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                return None
            if opcode == 9:
                self._send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if head[0] & 0x80:
                return b"".join(parts).decode()

    def send(self, text):
        self._send(1, text.encode())

    def _send(self, opcode, data):
        size = len(data)
        head = bytearray([0x80 | opcode])
        if size < 126:
            head.append(0x80 | size)
        elif size < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", size)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", size)
        mask = os.urandom(4)
        head += mask
        if size:
            pad = (mask * (size // 4 + 1))[:size]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(size, "big")
        with self.lock:
            self.sock.sendall(bytes(head) + data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CDPError(Exception):
    pass


class CDP:
    """DevTools client. Events and callback replies are queued for the main
    thread; `call` blocks the calling thread until its reply arrives."""

    class Pending:
        def __init__(self, callback=None):
            self.event = threading.Event()
            self.reply = None
            self.callback = callback

    def __init__(self, transport, wake_fd):
        self.transport = transport
        self.wake_fd = wake_fd
        self.next_id = 0
        self.pending = {}
        self.lock = threading.Lock()
        self.inbox = collections.deque()
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _post(self, item):
        self.inbox.append(item)
        try:
            os.write(self.wake_fd, b"x")
        except (BlockingIOError, OSError):
            pass

    def _reader(self):
        while True:
            try:
                msg = self.transport.recv()
            except Exception as error:  # noqa: BLE001
                log("cdp read failed:", repr(error))
                msg = None
            if msg is None:
                self.closed = True
                with self.lock:
                    for pending in self.pending.values():
                        pending.reply = {"error": {"message": "browser closed"}}
                        pending.event.set()
                self._post(("closed",))
                return
            message = json.loads(msg)
            if "id" in message:
                with self.lock:
                    pending = self.pending.pop(message["id"], None)
                if "error" in message:
                    log("cdp error:", message["id"], message["error"])
                if pending:
                    pending.reply = message
                    if pending.callback:
                        self._post(("reply", pending.callback, message))
                    pending.event.set()
            else:
                self._post(("event", message["method"], message.get("params", {}), message.get("sessionId")))

    def send(self, method, params=None, session=None, callback=None):
        if self.closed:
            return None
        with self.lock:
            self.next_id += 1
            msg_id = self.next_id
            pending = self.Pending(callback)
            if callback:
                self.pending[msg_id] = pending
        message = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            self.transport.send(json.dumps(message))
        except OSError as error:
            log("cdp send failed:", method, error)
        return pending

    def call(self, method, params=None, session=None, timeout=10.0):
        if self.closed:
            raise CDPError("browser closed")
        with self.lock:
            self.next_id += 1
            msg_id = self.next_id
            pending = self.Pending()
            self.pending[msg_id] = pending
        message = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.transport.send(json.dumps(message))
        if not pending.event.wait(timeout):
            with self.lock:
                self.pending.pop(msg_id, None)
            raise CDPError(f"{method}: no reply")
        if "error" in pending.reply:
            raise CDPError(f"{method}: {pending.reply['error'].get('message')}")
        return pending.reply.get("result", {})


# --------------------------------------------------------------------------
# Terminal input


class Key:
    """A key press. `name` is the character for text keys ("a", "A", "é") or
    a name for the others ("Enter", "Up", "F5"). `mods` uses kitty's bits."""

    SHIFT, ALT, CTRL, SUPER = 1, 2, 4, 8

    def __init__(self, name, mods=0):
        self.name = name
        self.mods = mods

    @property
    def text(self):
        if len(self.name) == 1 and not self.mods & (Key.ALT | Key.CTRL | Key.SUPER):
            return self.name
        return ""

    def is_(self, name, mods=0):
        return self.name == name and self.mods & ~Key.SHIFT == mods

    def __repr__(self):
        return f"Key({self.name!r}, {self.mods})"


class Mouse:
    def __init__(self, kind, button, x, y, mods):
        self.kind, self.button, self.x, self.y, self.mods = kind, button, x, y, mods

    def __repr__(self):
        return f"Mouse({self.kind}, {self.button}, {self.x}, {self.y})"


class Paste:
    def __init__(self, text):
        self.text = text


class Report:
    """A reply to a terminal query, e.g. CSI 6;h;w t (cell size)."""

    def __init__(self, params, final):
        self.params, self.final = params, final


TILDE_KEYS = {
    1: "Home", 2: "Insert", 3: "Delete", 4: "End", 5: "PageUp", 6: "PageDown", 7: "Home", 8: "End",
    11: "F1", 12: "F2", 13: "F3", 14: "F4", 15: "F5", 17: "F6", 18: "F7", 19: "F8", 20: "F9",
    21: "F10", 23: "F11", 24: "F12",
}
LETTER_KEYS = {"A": "Up", "B": "Down", "C": "Right", "D": "Left", "H": "Home", "F": "End",
               "P": "F1", "Q": "F2", "R": "F3", "S": "F4"}
KITTY_CODES = {27: "Escape", 13: "Enter", 9: "Tab", 127: "Backspace", 8: "Backspace",
               57414: "Enter", 57399: "0", 57400: "1", 57401: "2", 57402: "3", 57403: "4",
               57404: "5", 57405: "6", 57406: "7", 57407: "8", 57408: "9", 57409: ".",
               57410: "/", 57411: "*", 57412: "-", 57413: "+"}
PASTE_END = b"\x1b[201~"


class InputParser:
    """Turns terminal input bytes into Key, Mouse, Paste and Report events.

    Understands legacy xterm sequences, the kitty keyboard protocol
    (disambiguate mode, so Escape is unambiguous), SGR and SGR-Pixels mouse
    reports and bracketed paste."""

    def __init__(self):
        self.buf = b""
        self.pasting = False

    def feed(self, data):
        self.buf += data
        events = []
        while self.buf:
            if self.pasting:
                end = self.buf.find(PASTE_END)
                if end < 0:
                    break
                events.append(Paste(self.buf[:end].decode(errors="replace")))
                self.buf = self.buf[end + len(PASTE_END):]
                self.pasting = False
                continue
            parsed = self._one(self.buf)
            if parsed is None:
                break
            produced, used = parsed
            events.extend(produced)
            self.buf = self.buf[used:]
        return events

    def _one(self, b):
        c = b[0]
        if c == 0x1B:
            if len(b) == 1:
                # A lone Escape arrives as one read of just ESC; escape
                # sequences arrive whole.
                return [Key("Escape")], 1
            n = b[1]
            if n == ord("["):
                j = 2
                while j < len(b) and not 0x40 <= b[j] <= 0x7E:
                    j += 1
                if j >= len(b):
                    return None
                return self._csi(b[2:j].decode("latin-1"), chr(b[j])), j + 1
            if n == ord("O"):
                if len(b) < 3:
                    return None
                name = LETTER_KEYS.get(chr(b[2]))
                return ([Key(name)] if name else []), 3
            if n == 0x1B:
                return [Key("Escape")], 1
            inner = self._one(b[1:])
            if inner is None:
                return None
            keys, used = inner
            for key in keys:
                if isinstance(key, Key):
                    key.mods |= Key.ALT
            return keys, used + 1
        if c == 0x0D or c == 0x0A:
            return [Key("Enter")], 1
        if c == 0x09:
            return [Key("Tab")], 1
        if c in (0x7F, 0x08):
            return [Key("Backspace")], 1
        if c == 0:
            return [Key(" ", Key.CTRL)], 1
        if c < 0x20:
            return [Key(chr(c + 0x60), Key.CTRL)], 1
        size = 1 if c < 0x80 else 2 if c < 0xE0 else 3 if c < 0xF0 else 4
        if len(b) < size:
            return None
        return [Key(b[:size].decode(errors="replace"))], size

    def _csi(self, params, final):
        if params.startswith("<") and final in "Mm":
            try:
                code, x, y = (int(v) for v in params[1:].split(";"))
            except ValueError:
                return []
            mods = (Key.SHIFT if code & 4 else 0) | (Key.ALT if code & 8 else 0) | (Key.CTRL if code & 16 else 0)
            button = code & 3
            if code & 64:
                return [Mouse("wheel", button, x, y, mods)]
            if code & 32:
                return [Mouse("move", -1 if button == 3 else button, x, y, mods)]
            if code & 128:
                return []
            return [Mouse("press" if final == "M" else "release", button, x, y, mods)]
        if params.startswith("?") or params.startswith(">"):
            return [Report(params, final)]
        fields = params.split(";")

        def mods_at(index):
            try:
                return max(int(fields[index].split(":")[0]) - 1, 0)
            except (IndexError, ValueError):
                return 0

        if final == "u":
            try:
                code = int(fields[0].split(":")[0])
            except ValueError:
                return []
            name = KITTY_CODES.get(code)
            if name is None:
                if 57344 <= code <= 63743:
                    return []  # modifier keys and other private-use codes
                name = chr(code)
            mods = mods_at(1)
            if mods & Key.SHIFT and len(name) == 1:
                name = name.upper()
            return [Key(name, mods)]
        if final in LETTER_KEYS:
            return [Key(LETTER_KEYS[final], mods_at(1))]
        if final == "Z":
            return [Key("Tab", Key.SHIFT)]
        if final == "~":
            try:
                number = int(fields[0])
            except ValueError:
                return []
            if number == 200:
                self.pasting = True
                return []
            name = TILDE_KEYS.get(number)
            return [Key(name, mods_at(1))] if name else []
        if final == "t":
            return [Report(params, final)]
        return []


# --------------------------------------------------------------------------
# Keys to Chromium

SPECIAL_KEYS = {
    # name: (key, code, windowsVirtualKeyCode, text)
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, ""),
    "Backspace": ("Backspace", "Backspace", 8, ""),
    "Escape": ("Escape", "Escape", 27, ""),
    "Delete": ("Delete", "Delete", 46, ""),
    "Insert": ("Insert", "Insert", 45, ""),
    "Home": ("Home", "Home", 36, ""),
    "End": ("End", "End", 35, ""),
    "PageUp": ("PageUp", "PageUp", 33, ""),
    "PageDown": ("PageDown", "PageDown", 34, ""),
    "Up": ("ArrowUp", "ArrowUp", 38, ""),
    "Down": ("ArrowDown", "ArrowDown", 40, ""),
    "Left": ("ArrowLeft", "ArrowLeft", 37, ""),
    "Right": ("ArrowRight", "ArrowRight", 39, ""),
}
for _n in range(1, 13):
    SPECIAL_KEYS[f"F{_n}"] = (f"F{_n}", f"F{_n}", 111 + _n, "")

PUNCTUATION = {" ": ("Space", 32), "-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219),
               "]": ("BracketRight", 221), "\\": ("Backslash", 220), ";": ("Semicolon", 186),
               "'": ("Quote", 222), ",": ("Comma", 188), ".": ("Period", 190), "/": ("Slash", 191),
               "`": ("Backquote", 192)}
SHIFTED = dict(zip('!@#$%^&*()_+{}|:"<>?~', "1234567890-=[]\\;',./`"))


def key_code(char):
    """(code, windowsVirtualKeyCode, needs shift) for a US-layout character."""
    if char.isascii() and char.isalpha():
        return "Key" + char.upper(), ord(char.upper()), char.isupper()
    if char.isascii() and char.isdigit():
        return "Digit" + char, ord(char), False
    shift = char in SHIFTED
    base = SHIFTED.get(char, char)
    if base.isdigit():
        return "Digit" + base, ord(base), shift
    if base in PUNCTUATION:
        return PUNCTUATION[base][0], PUNCTUATION[base][1], shift
    return None


def cdp_modifiers(mods):
    return (1 if mods & Key.ALT else 0) | (2 if mods & Key.CTRL else 0) | (4 if mods & Key.SUPER else 0) | (8 if mods & Key.SHIFT else 0)


def key_events(key):
    """The Input.dispatchKeyEvent parameters for a key, or a string to insert
    with Input.insertText for characters without a key code."""
    mods = cdp_modifiers(key.mods)
    if key.name in SPECIAL_KEYS:
        name, code, vk, text = SPECIAL_KEYS[key.name]
        if mods & 7:
            text = ""
    elif len(key.name) == 1:
        info = key_code(key.name)
        if info is None:
            return key.text or None
        code, vk, shift = info
        if shift:
            mods |= 8
        name, text = key.name, key.text
    else:
        return None
    down = {"type": "keyDown" if text else "rawKeyDown", "key": name, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
    if text:
        down["text"] = down["unmodifiedText"] = text
    up = {"type": "keyUp", "key": name, "code": code, "windowsVirtualKeyCode": vk,
          "nativeVirtualKeyCode": vk, "modifiers": mods}
    return [down, up]


# --------------------------------------------------------------------------
# kitty graphics


class Graphics:
    """Shows the page as one kitty image.

    Pixels go inline (t=d) in 4096-byte chunks, so this works over ssh. The
    image is transmitted once (a=T) and from then on edited in place (a=f on
    the root frame), so it never disappears while a new frame is in transit:
    no black flashes when scrolling. Screencast frames are PNG, which kitty
    decodes itself (f=100), so the base64 from Chromium is passed straight
    through.

    The placement sits under the text layer and under cells with a
    background color, so link hints and menus can be drawn on top as text.
    """

    IMAGE_ID = 7_000_001
    CHUNK = 4096
    Z = -1_073_741_825

    def __init__(self, write):
        self.write = write
        self.placed = None

    def reset(self):
        self.placed = None
        self.write(f"\x1b_Ga=d,d=I,i={self.IMAGE_ID},q=2\x1b\\".encode())

    def draw(self, b64png, width, height, cols, rows):
        place = (width, height, cols, rows)
        out = bytearray()
        if self.placed != place:
            first = (f"a=T,f=100,t=d,i={self.IMAGE_ID},p=1,c={cols},r={rows},"
                     f"z={self.Z},C=1,q=2")
            out += b"\x1b7\x1b[1;1H"
        else:
            first = f"a=f,r=1,X=1,f=100,t=d,x=0,y=0,s={width},v={height},i={self.IMAGE_ID},q=2"
        data = b64png.encode() if isinstance(b64png, str) else b64png
        count = max(1, (len(data) + self.CHUNK - 1) // self.CHUNK)
        for index in range(count):
            chunk = data[index * self.CHUNK:(index + 1) * self.CHUNK]
            more = 1 if index < count - 1 else 0
            head = f"{first},m={more}" if index == 0 else f"m={more},q=2"
            out += b"\x1b_G" + head.encode() + b";" + chunk + b"\x1b\\"
        if self.placed != place:
            out += b"\x1b8"
        else:
            # Re-put the placement (no pixel data) so kitty redraws now; on
            # its own a frame edit can stay invisible until the next redraw.
            out += (f"\x1b7\x1b[1;1H\x1b_Ga=p,i={self.IMAGE_ID},p=1,c={cols},r={rows},"
                    f"z={self.Z},C=1,q=2\x1b\\\x1b8").encode()
        self.placed = place
        self.write(bytes(out))


def png_size(b64png):
    raw = base64.b64decode(b64png[:32])
    return struct.unpack(">II", raw[16:24])


# --------------------------------------------------------------------------
# Scripts run in an isolated world (invisible to the page)

HINT_SELECTOR = ("a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,"
                 "label[for],[onclick],[role=button],[role=link],[role=checkbox],[role=tab],"
                 "[role=menuitem],[role=option],[role=switch],[contenteditable=''],"
                 "[contenteditable=true],[tabindex]:not([tabindex='-1'])")

HINTS_JS = """(function(){
var out=[],seen=new Set(),els=document.querySelectorAll(%s);
for(var i=0;i<els.length&&out.length<676;i++){var e=els[i];
 var rs=e.getClientRects(),r=null;
 for(var k=0;k<rs.length;k++){var c=rs[k];if(c.width>0&&c.height>0&&c.bottom>0&&c.right>0&&c.top<innerHeight&&c.left<innerWidth){r=c;break}}
 if(!r)continue;var s=getComputedStyle(e);if(s.visibility=='hidden'||+s.opacity==0)continue;
 var x=Math.min(Math.max(r.left+Math.min(r.width/2,40),0),innerWidth-1),y=Math.min(Math.max(r.top+r.height/2,0),innerHeight-1);
 var t=document.elementFromPoint(x,y);if(t&&t!==e&&!e.contains(t)&&!t.contains(e)&&!(t.tagName=='LABEL'))continue;
 var key=Math.round(r.left)+','+Math.round(r.top);if(seen.has(key))continue;seen.add(key);
 out.push([r.left,r.top,x,y,e.href?String(e.href):''])}
return out})()""" % json.dumps(HINT_SELECTOR)

EDITABLE_JS = """(function(){var e=document.activeElement;
while(e&&e.shadowRoot&&e.shadowRoot.activeElement)e=e.shadowRoot.activeElement;
if(!e)return 'no';if(e.tagName=='IFRAME'||e.tagName=='FRAME')return 'frame';
if(e.isContentEditable||e.tagName=='TEXTAREA')return 'yes';
if(e.tagName=='INPUT'){var t=(e.type||'text').toLowerCase();
 return /^(button|submit|reset|checkbox|radio|image|file|color|range|hidden)$/.test(t)?'no':'yes'}
return 'no'})()"""

FOCUS_INPUT_JS = """(function(){var e=[].slice.call(document.querySelectorAll(
'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=image]):not([type=file]),textarea,[contenteditable=""],[contenteditable=true]'
)).filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0&&!e.disabled})[0];
if(!e)return false;e.scrollIntoView({block:'center'});e.focus();return true})()"""

SELECT_AT_JS = """(function(x,y){var e=document.elementFromPoint(x,y);
while(e&&e.tagName!='SELECT'&&e.tagName!='OPTION'&&e!==document.body)e=e.parentElement;
if(e&&e.tagName=='OPTION')e=e.parentElement;
if(!e||e.tagName!='SELECT'||e.multiple||e.disabled||e.size>1)return null;
var r=e.getBoundingClientRect();
return {x:r.left,y:r.bottom,sel:e.selectedIndex,opts:[].map.call(e.options,function(o){return [o.text.trim()||o.label||o.value,o.disabled]})}})(%f,%f)"""

SELECT_SET_JS = """(function(x,y,i){var e=document.elementFromPoint(x,y);
while(e&&e.tagName!='SELECT'&&e!==document.body)e=e.parentElement;
if(!e||e.tagName!='SELECT')return false;e.focus();if(e.selectedIndex!=i){e.selectedIndex=i;
e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))}return true})(%f,%f,%d)"""

SELECTION_JS = "String(getSelection())"


_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


def printable(text):
    """Page-supplied text (alerts, titles, menu options) with control
    characters replaced, so a page cannot send escape sequences to the
    terminal (e.g. OSC 52 to overwrite the clipboard)."""
    return _CONTROL.sub(" ", str(text))


# --------------------------------------------------------------------------
# :js — run your own JavaScript in the page, like the DevTools console

JS_WRAPPER = """(async function () {
  const __out = [];
  const __fmt = function (v) {
    if (typeof v === "string") return v;
    if (v === undefined) return "undefined";
    if (typeof v === "function" || typeof v === "symbol" || typeof v === "bigint") return String(v);
    if (v instanceof Error) return v.stack || String(v);
    if (typeof Node !== "undefined" && v instanceof Node) return v.outerHTML ? v.outerHTML.slice(0, 2000) : v.nodeName;
    try { const j = JSON.stringify(v, null, 2); return j === undefined ? String(v) : j; } catch (e) { return String(v); }
  };
  const __log = function (prefix) {
    return function () { __out.push(prefix + Array.prototype.map.call(arguments, __fmt).join(" ")); };
  };
  // Shadows the page's console, so output comes back to kbrowse without
  // Runtime.enable (which bot checks can detect).
  const console = {log: __log(""), info: __log(""), debug: __log(""), dir: __log(""), table: __log(""),
                   warn: __log("warning: "), error: __log("error: ")};
  try {
    const __r = await (async () => {
%s
    })();
    return {out: __out, result: __fmt(__r), ok: true};
  } catch (e) {
    return {out: __out, result: __fmt(e), ok: false};
  }
})()"""


def js_program(code, expression):
    """Wrap user code. As an expression its value is the result (and it may
    use await); as statements the result is whatever it `return`s. The code
    is pasted into the function rather than passed to eval(), so a page's
    Content-Security-Policy cannot block it."""
    body = f"return (\n{code}\n);" if expression else code
    return JS_WRAPPER % body


def scripts_folder():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "kbrowse", "scripts")


def script_path(name):
    """The file for `:js -f NAME`, after checking that nobody else could have
    put or changed it there: a script runs with access to every site you are
    signed in to."""
    folder = scripts_folder()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise ValueError(f"bad script name: {name}")
    if not name.endswith(".js"):
        name += ".js"
    path = os.path.realpath(os.path.join(folder, name))
    real_folder = os.path.realpath(folder)
    if os.path.dirname(path) != real_folder:
        raise ValueError(f"{name} points outside {folder}")
    for checked in (real_folder, path):
        info = os.stat(checked)
        if info.st_uid != os.getuid():
            raise ValueError(f"{checked} is not owned by you")
        if info.st_mode & 0o022:
            raise ValueError(f"{checked} is writable by other users (chmod go-w)")
    return path


def list_scripts():
    try:
        return sorted(n[:-3] for n in os.listdir(scripts_folder()) if n.endswith(".js"))
    except OSError:
        return []


def js_string(text):
    return json.dumps(text)


# --------------------------------------------------------------------------
# Launching Chromium

BROWSERS = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"]
FLATPAK_APPS = ["org.chromium.Chromium", "io.github.ungoogled_software.ungoogled_chromium", "com.google.Chrome"]
FLATPAK_EXPORTS = ("/var/lib/flatpak/exports/bin/", "~/.local/share/flatpak/exports/bin/")


def flatpak_app(path):
    """The Flatpak app id when `path` is a Flatpak launcher (or a link to one)."""
    real = os.path.realpath(path)
    for exports in FLATPAK_EXPORTS:
        if real.startswith(os.path.expanduser(exports)):
            return os.path.basename(real)
    return None


def flatpak_installed(app):
    if not shutil.which("flatpak"):
        return False
    try:
        return subprocess.run(["flatpak", "info", app], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def browser_version(command):
    try:
        out = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
    return match.group(1) if match else None


def find_browser(name):
    """Returns (command, flatpak app id or None). Installed Flatpaks win,
    then browsers on PATH, then Playwright downloads."""
    if name:
        if "." in name and "/" not in name and flatpak_installed(name):
            return ["flatpak", "run", name], name  # --browser org.chromium.Chromium
        path = shutil.which(name) or os.path.expanduser(name)
        if not os.path.exists(path):
            sys.exit(f"kbrowse: browser not found: {name}")
        app = flatpak_app(path)
        return (["flatpak", "run", app], app) if app else ([path], None)
    for app in FLATPAK_APPS:
        if flatpak_installed(app):
            return ["flatpak", "run", app], app
    for candidate in BROWSERS:
        path = shutil.which(candidate)
        if not path:
            continue
        app = flatpak_app(path)
        if app:
            return ["flatpak", "run", app], app
        # Skip stubs such as Ubuntu's chromium-browser that only point to the snap.
        if browser_version([path]):
            return [path], None
    for pattern_dir in (os.path.expanduser("~/.cache/ms-playwright"), "/opt/pw-browsers"):
        if os.path.isdir(pattern_dir):
            for root, _dirs, files in sorted(os.walk(pattern_dir)):
                if "chrome" in files and "headless" not in root:
                    return [os.path.join(root, "chrome")], None
    sys.exit("kbrowse: no Chromium found. Install one (flatpak install flathub org.chromium.Chromium) "
             "or pass --browser")


def is_snap(path):
    return os.path.realpath(path).startswith("/snap/") or path.startswith("/snap/")


def default_profile(command, app):
    if app:
        # A Flatpak app can always write its own ~/.var/app folder, at the
        # same path inside and outside the sandbox.
        return os.path.expanduser(f"~/.var/app/{app}/data/kbrowse-profile")
    if is_snap(command[0]):
        # Snap Chromium can only write inside ~/snap/chromium.
        return os.path.expanduser("~/snap/chromium/common/kbrowse-profile")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "kbrowse", "profile")


# --------------------------------------------------------------------------
# Ad blocking: uBlock Origin Lite, loaded as an unpacked extension. It blocks
# with the same filter lists Brave uses by default (EasyList, EasyPrivacy,
# uBlock filters, Peter Lowe's list), runs inside Chromium itself, and covers
# iframes and workers too, which DevTools-side blocking would miss.

UBLOCK_ID = "ddkjiahejlhfcafbddmgiahcphecmpfh"  # uBlock Origin Lite on the Chrome Web Store
UBLOCK_MAX_AGE = 7 * 24 * 3600


def crx_to_zip(data):
    """The zip archive inside a CRX2/CRX3 package."""
    if data[:4] != b"Cr24":
        if data[:2] == b"PK":
            return data
        raise ValueError("not a CRX file")
    version = struct.unpack("<I", data[4:8])[0]
    if version == 3:
        header = struct.unpack("<I", data[8:12])[0]
        return data[12 + header:]
    if version == 2:
        key, sig = struct.unpack("<II", data[8:16])
        return data[16 + key + sig:]
    raise ValueError(f"CRX version {version}")


def install_ublock(folder, chrome_version):
    """`chrome_version` is a function, called only when a download is needed."""
    """Download uBlock Origin Lite into `folder` (at most once a week).
    Returns the folder, or None when there is no copy to load."""
    manifest = os.path.join(folder, "manifest.json")
    fresh = os.path.exists(manifest) and time.time() - os.path.getmtime(manifest) < UBLOCK_MAX_AGE
    if fresh:
        return folder
    url = os.environ.get("KBROWSE_UBLOCK_URL") or (
        "https://clients2.google.com/service/update2/crx?response=redirect"
        f"&prodversion={chrome_version() or 150}.0.0.0&acceptformat=crx2,crx3"
        f"&x=id%3D{UBLOCK_ID}%26uc")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            archive = zipfile.ZipFile(io.BytesIO(crx_to_zip(response.read())))
        staging = folder + ".new"
        shutil.rmtree(staging, ignore_errors=True)
        archive.extractall(staging)
        with open(os.path.join(staging, "manifest.json")) as handle:
            data = json.load(handle)
        # An unpacked extension gets no permission prompt, so grant the
        # optional all-sites permission up front. uBlock Origin Lite then
        # starts in its "optimal" mode, which adds cosmetic filtering
        # (hiding ad boxes) to network blocking.
        optional = data.get("optional_host_permissions", [])
        if optional:
            data["host_permissions"] = sorted(set(data.get("host_permissions", []) + optional))
            data["optional_host_permissions"] = []
        data.pop("update_url", None)
        with open(os.path.join(staging, "manifest.json"), "w") as handle:
            json.dump(data, handle, indent=1)
        old = folder + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(folder):
            os.rename(folder, old)
        os.rename(staging, folder)
        shutil.rmtree(old, ignore_errors=True)
        log("installed uBlock Origin Lite", data.get("version"), "into", folder)
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        log("uBlock Origin Lite download failed:", repr(error))
        if not os.path.exists(manifest):
            return None
    return folder


def start_xvfb(width, height):
    if not shutil.which("Xvfb"):
        sys.exit("kbrowse: Xvfb not found. Install it (sudo apt install xvfb) or use --headless")
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
        pass_fds=(write_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    os.close(write_fd)
    number = b""
    deadline = time.time() + 15
    while not number.endswith(b"\n") and time.time() < deadline:
        ready, _, _ = select.select([read_fd], [], [], 0.5)
        if ready:
            chunk = os.read(read_fd, 64)
            if not chunk:
                break
            number += chunk
    os.close(read_fd)
    if not number.strip():
        proc.kill()
        sys.exit("kbrowse: Xvfb did not start")
    return proc, ":" + number.decode().strip()


class Browser:
    def __init__(self, args, window_css):
        self.args = args
        self.command, self.flatpak = find_browser(args.browser)
        self.profile = os.path.expanduser(args.profile or default_profile(self.command, self.flatpak))
        os.makedirs(self.profile, exist_ok=True)
        self.xvfb = None
        self.proc = None
        self.transport = None
        self.window_css = window_css
        self.log_file = open(LOG.name + ".chrome", "ab") if LOG else subprocess.DEVNULL
        self.extensions = [os.path.abspath(os.path.expanduser(e)) for e in args.extension]
        self.adblock = None
        if not args.no_adblock:
            # Next to the profile, so a Flatpak browser can read it too.
            folder = os.path.join(os.path.dirname(self.profile.rstrip("/")), "kbrowse-ublock")
            self.adblock = install_ublock(folder, lambda: browser_version(self.command))
            if self.adblock:
                self.extensions.append(self.adblock)

    def flags(self):
        width, height = self.window_css
        flags = [
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--hide-crash-restore-bubble",
            # Without this, --remote-debugging-* sets navigator.webdriver.
            "--disable-blink-features=AutomationControlled",
            f"--force-device-scale-factor={self.args.scale}",
            f"--window-size={width},{height + 120}",
            "--window-position=0,0",
        ]
        if not self.args.headless:
            # There is no window manager in Xvfb; never treat the window as covered.
            flags.append("--disable-backgrounding-occluded-windows")
            # Xvfb has no GPU: keep WebGL working in software, like a normal desktop.
            flags.append("--enable-unsafe-swiftshader")
        if self.extensions:
            flags.append("--load-extension=" + ",".join(self.extensions))
            # Chrome 137+ ignores --load-extension unless this is turned off.
            flags.append("--disable-features=DisableLoadExtensionCommandLineSwitch")
        if self.args.headless:
            flags.append("--headless=new")
            version = browser_version(self.command)
            if version:
                # Headless Chromium says "HeadlessChrome" in its user agent.
                flags.append(f"--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36")
        if self.args.no_sandbox:
            # --test-type hides the "unsupported command-line flag" bar.
            flags += ["--no-sandbox", "--test-type"]
        return flags + self.args.chrome_flag

    def start(self, url, mode):
        env = dict(os.environ)
        if not self.args.headless:
            if self.xvfb is None:
                width, height = self.window_css
                scale = self.args.scale
                self.xvfb, display = start_xvfb(
                    max(3840, int((width + 200) * scale)), max(2400, int((height + 400) * scale)))
                self.display = display
            env["DISPLAY"] = self.display
            env.pop("WAYLAND_DISPLAY", None)
        if self.flatpak and not env.get("XDG_RUNTIME_DIR"):
            # flatpak run needs this; some ssh sessions do not set it.
            runtime = f"/run/user/{os.getuid()}"
            if os.path.isdir(runtime):
                env["XDG_RUNTIME_DIR"] = runtime
        flags = self.flags()
        if mode == "pipe":
            to_chrome_r, to_chrome_w = os.pipe()
            from_chrome_r, from_chrome_w = os.pipe()

            def wire():
                a = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD, 100)
                b = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD, 100)
                os.dup2(a, 3)
                os.dup2(b, 4)

            self.proc = subprocess.Popen(
                [*self.command, "--remote-debugging-pipe", *flags, url],
                stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=self.log_file,
                env=env, close_fds=False, preexec_fn=wire, start_new_session=True,
            )
            os.close(to_chrome_r)
            os.close(from_chrome_w)
            for fd in (to_chrome_w, from_chrome_r):
                os.set_inheritable(fd, False)
            self.transport = PipeTransport(to_chrome_w, from_chrome_r)
        else:
            port_file = os.path.join(self.profile, "DevToolsActivePort")
            try:
                os.unlink(port_file)
            except FileNotFoundError:
                pass
            self.proc = subprocess.Popen(
                [*self.command, "--remote-debugging-port=0", *flags, url],
                stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=self.log_file,
                env=env, start_new_session=True,
            )
            deadline = time.time() + 30
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise CDPError(f"Chromium exited with code {self.proc.returncode}")
                try:
                    with open(port_file) as handle:
                        lines = handle.read().split("\n")
                    if len(lines) >= 2 and lines[1]:
                        self.transport = WsTransport(int(lines[0]), lines[1])
                        break
                except (FileNotFoundError, ValueError, ConnectionError):
                    pass
                time.sleep(0.1)
            else:
                raise CDPError("Chromium did not open its DevTools port")
        log("started", self.command, mode, flags)
        return self.transport

    def stop(self, kill_xvfb=True):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                if self.flatpak:
                    # Killing `flatpak run` can leave the sandboxed browser behind.
                    subprocess.run(["flatpak", "kill", self.flatpak], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        if self.transport:
            self.transport.close()
        if kill_xvfb and self.xvfb:
            self.xvfb.terminate()


# --------------------------------------------------------------------------
# The browser UI


class Tab:
    def __init__(self, target_id, url="", title="", opener=None):
        self.id = target_id
        self.url = url
        self.title = title
        self.opener = opener
        self.session = None
        self.loading = False


HINT_CHARS_SHORT = "asdfghjkl"
HINT_CHARS_LONG = "asdfghjklqwertyuiopzxcvbnm"


def hint_labels(count):
    if count <= len(HINT_CHARS_SHORT):
        return list(HINT_CHARS_SHORT[:count])
    chars = HINT_CHARS_SHORT if count <= len(HINT_CHARS_SHORT) ** 2 else HINT_CHARS_LONG
    return [a + b for a in chars for b in chars][:count]


def to_url(text, search):
    text = text.strip()
    looks_like_url = " " not in text and (
        "://" in text or text.startswith(("about:", "localhost", "chrome:", "file:", "data:")) or "." in text)
    if not looks_like_url:
        return search.replace("{}", urllib.parse.quote_plus(text))
    if "://" in text or text.startswith(("about:", "chrome:", "data:", "file:")):
        return text
    return ("http://" if text.startswith("localhost") else "https://") + text


class App:
    def __init__(self, args):
        self.args = args
        self.tin = sys.stdin.fileno()
        self.tout = sys.stdout.fileno()
        self.parser = InputParser()
        self.gfx = Graphics(self.write)
        self.tabs = {}
        self.order = []
        self.active = None
        self.quit = False
        self.resized = False
        self.frame = None
        self.view = None  # page viewport in CSS pixels, from the last frame
        self.mode = "normal"
        self.pending = ""  # first key(s) of a multi-key binding
        self.prompt = None  # [kind, text, cursor]
        self.hints = None  # {"items": [(label, item, col, row)], "typed": str, "newtab": bool}
        self.menu = None  # select picker
        self.dialog = None
        self.search = None
        self.message = ("", 0)
        self.status_dirty = True
        self.buttons = set()
        self.last_click = (0, -1, 0, 0, 1)  # time, button, x, y, count
        self.pending_move = None
        self.overlay_cells = []
        self.closing = set()
        self.frame_due = None
        self.history = {}  # prompt kind -> earlier entries, for Up/Down
        self.history_pos = None
        self.output = None  # :js output pane: {"lines": [...], "top": int}
        self.next_poll = 0
        self.settle_until = 0
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    # ---- terminal ----------------------------------------------------

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.tout, view):]
            except BlockingIOError:
                select.select([], [self.tout], [], 1)

    def term_size(self):
        rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(self.tout, termios.TIOCGWINSZ, b"\0" * 8))
        return rows, cols, xpix, ypix

    def query_cell_size(self):
        self.write("\x1b[16t")
        deadline = time.time() + 2
        buf = b""
        while time.time() < deadline:
            ready, _, _ = select.select([self.tin], [], [], 0.2)
            if ready:
                buf += os.read(self.tin, 4096)
                match = re.search(rb"\x1b\[6;(\d+);(\d+)t", buf)
                if match:
                    return int(match.group(2)), int(match.group(1))
        return None

    def geometry(self):
        rows, cols, xpix, ypix = self.term_size()
        if xpix and ypix:
            cell_w, cell_h = xpix / cols, ypix / rows
        else:
            size = self.query_cell_size()
            if not size:
                raise SystemExit("kbrowse: the terminal does not report its size in pixels (is this kitty?)")
            cell_w, cell_h = size
        self.rows, self.cols = rows, cols
        self.cell_w, self.cell_h = cell_w, cell_h
        self.image_rows = max(rows - 1, 1)
        self.area = (int(cell_w * cols), int(cell_h * self.image_rows))
        if self.args.scale is None:
            self.args.scale = 2.0 if cell_h >= 28 else 1.0
        self.scale = self.args.scale
        self.target_css = (max(int(self.area[0] / self.scale), 200), max(int(self.area[1] / self.scale), 150))
        # Frames can be sent smaller than the screen area and scaled up by
        # kitty, to save bandwidth on slow links.
        factor = min(max(self.args.downscale, 0.1), 1.0)
        self.frame_px = (int(self.area[0] * factor), int(self.area[1] * factor))
        log("geometry", rows, cols, cell_w, cell_h, self.area, self.scale, self.target_css)

    def enter_terminal(self):
        self.saved_tty = termios.tcgetattr(self.tin)
        tty.setraw(self.tin)
        self.write(
            "\x1b[?1049h\x1b[?25l\x1b[H\x1b[2J"
            "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[?1016h"  # mouse: any motion, SGR pixels
            "\x1b[?2004h"  # bracketed paste
            "\x1b[>1u"  # kitty keyboard protocol: disambiguate escape codes
        )

    def leave_terminal(self):
        self.write(
            "\x1b[<u\x1b[?2004l\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l"
            f"\x1b_Ga=d,d=I,i={Graphics.IMAGE_ID},q=2\x1b\\\x1b[0m\x1b[?25h\x1b[?1049l"
        )
        termios.tcsetattr(self.tin, termios.TCSADRAIN, self.saved_tty)

    # ---- status line and overlays -----------------------------------

    def flash(self, text, seconds=3.0):
        self.message = (text, time.time() + seconds)
        self.status_dirty = True
        log("message:", text)

    def draw_status(self):
        self.status_dirty = False
        tab = self.tabs.get(self.active)
        cursor = None
        if self.dialog:
            kind = self.dialog["type"]
            text = f"{kind}: {self.dialog['message']}".replace("\n", " ")
            if kind == "prompt":
                answer = self.dialog["text"]
                text = f"{text} > {answer}"
                cursor = len(text)
            else:
                text += "  [Enter/y: OK, Esc/n: cancel]" if kind != "alert" else "  [Enter]"
            left = text
        elif self.prompt:
            kind, text, pos = self.prompt
            prefix = {"open": ":open ", "tabopen": ":open -t ", "search": "/", "rsearch": "?", "cmd": ":"}[kind]
            left = prefix + text
            cursor = len(prefix) + pos
        else:
            label = {"insert": "-- INSERT -- ", "hint": "-- HINT -- ", "menu": "-- SELECT -- "}.get(self.mode, "")
            if self.output:
                label = "-- OUTPUT -- (q: close, j/k: scroll, y: copy) "
            message, until = self.message
            if message and time.time() < until:
                left = label + message
            else:
                index = self.order.index(self.active) + 1 if self.active in self.order else 0
                where = ""
                if tab:
                    where = (tab.title + " — " if tab.title and tab.title != tab.url else "") + tab.url
                busy = "… " if tab and tab.loading else ""
                count = f"[{index}/{len(self.order)}] " if len(self.order) > 1 else ""
                left = f"{label}{busy}{count}{where}{self.pending and '  ' + self.pending}"
        if cursor is not None and cursor >= self.cols:
            cut = cursor - self.cols + 1
            left, cursor = left[cut:], cursor - cut
        line = printable(left)[: self.cols].ljust(self.cols)
        out = f"\x1b[{self.rows};1H\x1b[0;7m{line}\x1b[0m"
        if cursor is not None:
            out += f"\x1b[{self.rows};{cursor + 1}H\x1b[?25h"
        else:
            out += "\x1b[?25l"
        self.write(out)

    def clear_overlay(self):
        out = []
        for row, col, length in self.overlay_cells:
            out.append(f"\x1b[{row};{col}H\x1b[0m{' ' * length}")
        self.overlay_cells = []
        if out:
            self.write("".join(out))

    def put_overlay(self, row, col, text, style="\x1b[0;30;48;2;255;215;110;1m"):
        col = max(1, min(col, self.cols - len(text) + 1))
        row = max(1, min(row, self.image_rows))
        self.overlay_cells.append((row, col, len(text)))
        return f"\x1b[{row};{col}H{style}{text}\x1b[0m"

    # ---- coordinates -------------------------------------------------

    def css_per_px(self):
        if self.view:
            return self.view[0] / self.area[0], self.view[1] / self.area[1]
        return 1 / self.scale, 1 / self.scale

    def px_to_css(self, x, y):
        fx, fy = self.css_per_px()
        return (x - 1) * fx, (y - 1) * fy

    def css_to_cell(self, x, y):
        fx, fy = self.css_per_px()
        return int(x / fx / self.cell_w) + 1, int(y / fy / self.cell_h) + 1

    def row_css(self):
        return self.cell_h * self.css_per_px()[1]

    # ---- DevTools helpers --------------------------------------------

    @property
    def tab(self):
        return self.tabs.get(self.active)

    def session(self):
        tab = self.tab
        return tab.session if tab else None

    def evaluate(self, expression, tab=None, timeout=3.0):
        """Run JavaScript in an isolated world of the tab's main frame. The
        page's own scripts cannot see this world or anything defined in it."""
        tab = tab or self.tab
        if not tab or not tab.session:
            return None
        try:
            context = self.cdp.call("Page.createIsolatedWorld",
                                    {"frameId": tab.id, "worldName": "kbrowse", "grantUniveralAccess": False},
                                    tab.session, timeout)["executionContextId"]
            result = self.cdp.call("Runtime.evaluate",
                                   {"expression": expression, "contextId": context, "returnByValue": True,
                                    "silent": True}, tab.session, timeout)
        except CDPError as error:
            log("evaluate failed:", error)
            return None
        if "exceptionDetails" in result:
            log("script error:", result["exceptionDetails"].get("text"), result["exceptionDetails"].get("exception"))
            return None
        return result.get("result", {}).get("value")

    def evaluate_async(self, expression, callback):
        """Like evaluate, but without waiting: a page blocked in alert() would
        otherwise freeze kbrowse until the timeout."""
        tab = self.tab
        if not tab or not tab.session:
            return

        def evaluate(reply):
            context = reply.get("result", {}).get("executionContextId")
            if context:
                self.cdp.send("Runtime.evaluate", {"expression": expression, "contextId": context,
                                                   "returnByValue": True, "silent": True}, tab.session, done)

        def done(reply):
            if tab.id == self.active:
                callback(reply.get("result", {}).get("result", {}).get("value"))

        self.cdp.send("Page.createIsolatedWorld", {"frameId": tab.id, "worldName": "kbrowse",
                                                   "grantUniveralAccess": False}, tab.session, evaluate)

    def mouse(self, kind, x, y, button="none", buttons=0, count=0, mods=0, **extra):
        params = {"type": kind, "x": x, "y": y, "button": button, "buttons": buttons,
                  "clickCount": count, "modifiers": cdp_modifiers(mods), **extra}
        self.cdp.send("Input.dispatchMouseEvent", params, self.session())

    def click(self, x, y, button="left", newtab=False):
        mods = Key.CTRL if newtab else 0
        self.mouse("mouseMoved", x, y, mods=mods)
        self.mouse("mousePressed", x, y, button, 1, 1, mods)
        self.mouse("mouseReleased", x, y, button, 0, 1, mods)

    def wheel(self, dx, dy, at=None):
        x, y = at or (self.target_css[0] / 2, self.target_css[1] / 2)
        if self.view:
            x, y = at or (self.view[0] / 2, self.view[1] / 2)
        self.mouse("mouseWheel", x, y, deltaX=dx, deltaY=dy)

    def send_key(self, key):
        events = key_events(key)
        if events is None:
            return
        if isinstance(events, str):
            self.cdp.send("Input.insertText", {"text": events}, self.session())
            return
        for event in events:
            self.cdp.send("Input.dispatchKeyEvent", event, self.session())

    # ---- tabs ----------------------------------------------------------

    def add_tab(self, info):
        tab = self.tabs.get(info["targetId"])
        if tab is None:
            tab = Tab(info["targetId"], info.get("url", ""), info.get("title", ""), info.get("openerId"))
            self.tabs[tab.id] = tab
            if self.active in self.order:
                self.order.insert(self.order.index(self.active) + 1, tab.id)
            else:
                self.order.append(tab.id)
        return tab

    def activate(self, target_id):
        tab = self.tabs.get(target_id)
        if not tab:
            return
        old = self.tab
        if old and old.session and old.id != target_id:
            self.cdp.send("Page.stopScreencast", {}, old.session)
        self.leave_modes()
        self.active = target_id
        self.view = None
        if not tab.session:
            tab.session = self.cdp.call("Target.attachToTarget", {"targetId": tab.id, "flatten": True})["sessionId"]
            self.cdp.call("Page.enable", {}, tab.session)
        self.show(tab)  # first: background tabs do not follow window resizes
        self.size_window(tab)
        self.start_screencast(tab)
        self.status_dirty = True

    def show(self, tab):
        """Make the tab the visible one. Without a window manager Chromium can
        start with its page marked hidden, which stops rendering entirely
        (and would look odd to bot checks). Page.bringToFront fixes that."""
        for _ in range(3):
            self.cdp.call("Page.bringToFront", {}, tab.session)
            if self.evaluate("document.visibilityState", tab) != "hidden":
                return
            time.sleep(0.2)
        log("page still hidden")

    def start_screencast(self, tab):
        self.frame_due = time.time() + 0.7
        self.cdp.send("Page.startScreencast", {"format": "png", "maxWidth": self.frame_px[0],
                                               "maxHeight": self.frame_px[1], "everyNthFrame": 1}, tab.session)

    def capture(self):
        """Chromium only sends screencast frames when something changes, so
        take a screenshot when no frame arrives after a tab switch."""
        self.frame_due = None
        tab = self.tab
        if not tab or not tab.session:
            return
        target = tab.id

        def done(reply):
            data = reply.get("result", {}).get("data")
            if data and self.active == target and not self.frame:
                self.frame = (data, {}, None, tab.session)

        width, height = self.view or self.target_css
        self.cdp.send("Page.captureScreenshot", {
            "format": "png", "clip": {"x": 0, "y": 0, "width": width, "height": height,
                                      "scale": self.frame_px[0] / self.area[0]}}, tab.session, done)

    def size_window(self, tab):
        """Make the page viewport exactly as big as the terminal's image area."""
        self.settle_until = time.time() + 1.0
        want = self.target_css
        for _ in range(3):
            try:
                window = self.cdp.call("Browser.getWindowForTarget", {"targetId": tab.id})
            except CDPError as error:
                log("no window:", error)
                return
            inner = self.evaluate("[innerWidth,innerHeight]", tab)
            if not inner:
                return
            if tuple(inner) == tuple(want):
                return
            bounds = window["bounds"]
            if bounds.get("windowState", "normal") != "normal":
                self.cdp.call("Browser.setWindowBounds", {"windowId": window["windowId"],
                                                          "bounds": {"windowState": "normal"}})
            width = want[0] + bounds["width"] - inner[0]
            height = want[1] + bounds["height"] - inner[1]
            try:
                self.cdp.call("Browser.setWindowBounds", {"windowId": window["windowId"],
                                                          "bounds": {"left": 0, "top": 0, "width": width,
                                                                     "height": height}})
            except CDPError as error:
                log("resize failed:", error)
                return
            for _ in range(20):
                time.sleep(0.05)
                now = self.evaluate("[innerWidth,innerHeight]", tab)
                if now and now != inner:
                    inner = now
                    break
            log("window", bounds, "->", width, height, "viewport", inner, "want", want)

    def new_tab(self, url):
        target = self.cdp.call("Target.createTarget", {"url": url})["targetId"]
        self.add_tab({"targetId": target, "url": url})
        self.activate(target)

    def close_tab(self, target_id=None):
        target_id = target_id or self.active
        if not target_id:
            return
        if len(self.order) <= 1 and target_id == self.active:
            # Closing Chromium's last tab would quit it: open a blank one first.
            self.new_tab("about:blank")
        self.closing.add(target_id)
        self.cdp.send("Target.closeTarget", {"targetId": target_id})

    def cycle_tab(self, step):
        if len(self.order) > 1 and self.active in self.order:
            index = (self.order.index(self.active) + step) % len(self.order)
            self.activate(self.order[index])

    def navigate(self, url, newtab=False):
        if newtab:
            self.new_tab(url)
        else:
            self.cdp.send("Page.navigate", {"url": url}, self.session())
        self.leave_modes()

    def history(self, step):
        try:
            history = self.cdp.call("Page.getNavigationHistory", {}, self.session())
        except CDPError:
            return
        index = history["currentIndex"] + step
        if 0 <= index < len(history["entries"]):
            self.cdp.send("Page.navigateToHistoryEntry", {"entryId": history["entries"][index]["id"]}, self.session())
        else:
            self.flash("At the " + ("start" if step < 0 else "end") + " of history")

    def yank(self, text, what):
        if not text:
            self.flash(f"Nothing to copy ({what})")
            return
        self.write(f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07")
        self.flash(f"Copied {what}: {text[:200]}")

    # ---- modes -----------------------------------------------------------

    def leave_modes(self):
        if self.hints or self.menu or self.output:
            self.clear_overlay()
        self.hints = None
        self.menu = None
        self.output = None
        self.prompt = None
        self.pending = ""
        if self.mode != "normal":
            self.mode = "normal"
        self.status_dirty = True

    def set_mode(self, mode):
        self.mode = mode
        self.status_dirty = True

    def check_editable(self):
        """qutebrowser-style auto insert mode: after a click, enter insert mode
        if a text field has focus, and leave it if not."""
        def done(state):
            if self.dialog or self.prompt or self.hints or self.menu:
                return
            if state == "yes":
                self.set_mode("insert")
            elif state == "no" and self.mode == "insert":
                self.set_mode("normal")

        self.evaluate_async(EDITABLE_JS, done)

    # ---- input -------------------------------------------------------------

    def on_input(self, event):
        if isinstance(event, Key):
            if event.is_("q", Key.CTRL):
                self.quit = True
                return
            if self.dialog:
                self.dialog_key(event)
            elif self.prompt:
                self.prompt_key(event)
            elif self.hints:
                self.hint_key(event)
            elif self.menu:
                self.menu_key(event)
            elif self.output:
                self.output_key(event)
            elif self.mode == "insert":
                if event.is_("Escape"):
                    self.set_mode("normal")
                else:
                    self.send_key(event)
            else:
                self.normal_key(event)
        elif isinstance(event, Paste):
            if self.prompt:
                kind, text, pos = self.prompt
                clean = event.text.replace("\r", " ").replace("\n", " ")
                self.prompt = [kind, text[:pos] + clean + text[pos:], pos + len(clean)]
                self.status_dirty = True
            elif self.dialog and self.dialog["type"] == "prompt":
                self.dialog["text"] += event.text
                self.status_dirty = True
            else:
                self.cdp.send("Input.insertText", {"text": event.text}, self.session())
        elif isinstance(event, Mouse):
            self.on_mouse(event)

    def on_mouse(self, event):
        if event.kind == "move":
            self.pending_move = event
            return
        self.flush_move()
        in_page = event.y <= self.area[1] and event.x <= self.area[0]
        if self.menu and event.kind == "press":
            self.menu_click(event)
            return
        if self.output and event.kind in ("press", "wheel"):
            if event.kind == "wheel" and event.button in (0, 1):
                self.scroll_output(-3 if event.button == 0 else 3)
            else:
                self.leave_modes()
            return
        if not in_page or self.dialog:
            return
        x, y = self.px_to_css(event.x, event.y)
        if event.kind == "wheel":
            step = self.args.wheel
            dx, dy = {0: (0, -step), 1: (0, step), 2: (-step, 0), 3: (step, 0)}[event.button]
            if event.mods & Key.SHIFT:
                dx, dy = dy, dx
            self.mouse("mouseWheel", x, y, deltaX=dx, deltaY=dy, mods=event.mods)
            return
        names = {0: "left", 1: "middle", 2: "right"}
        if event.button not in names or (event.button == 2 and not self.args.right_click):
            return
        name = names[event.button]
        if event.kind == "press":
            if self.hints:
                self.leave_modes()
            if self.prompt:
                self.prompt = None
                self.status_dirty = True
            if event.button == 0:
                options = self.evaluate(SELECT_AT_JS % (x, y), timeout=1.0)
                if options:
                    self.open_menu(options, x, y)
                    return
            now = time.time()
            last_time, last_button, lx, ly, count = self.last_click
            count = count + 1 if (now - last_time < 0.5 and last_button == event.button
                                  and abs(lx - x) < 5 and abs(ly - y) < 5) else 1
            self.last_click = (now, event.button, x, y, count)
            self.buttons.add(event.button)
            self.mouse("mouseMoved", x, y, buttons=self.button_mask(), mods=event.mods)
            self.mouse("mousePressed", x, y, name, self.button_mask(), count, event.mods)
        else:
            if event.button not in self.buttons:
                return
            self.buttons.discard(event.button)
            self.mouse("mouseReleased", x, y, name, self.button_mask(), self.last_click[4], event.mods)
            if event.button == 0:
                self.check_editable()

    def button_mask(self):
        return sum({0: 1, 1: 4, 2: 2}[b] for b in self.buttons)

    def flush_move(self):
        event, self.pending_move = self.pending_move, None
        if not event or self.menu or self.dialog:
            return
        if event.y > self.area[1] or event.x > self.area[0]:
            return
        x, y = self.px_to_css(event.x, event.y)
        held = {0: "left", 1: "middle", 2: "right"}
        button = next((held[b] for b in (0, 1, 2) if b in self.buttons), "none")
        self.mouse("mouseMoved", x, y, button, self.button_mask(), mods=event.mods)

    def normal_key(self, key):
        page = max(self.image_rows - 2, 1) * self.row_css()
        seq = self.pending + key.name if len(key.name) == 1 and not key.mods & (Key.CTRL | Key.ALT) else None
        self.pending = ""
        self.status_dirty = True
        two = {"gg", "go", "gO", "gi", "gt", "gT", "yy", "yt", "ys", "ZZ", "ZQ"}
        if seq and seq in two:
            return self.normal_command(seq)
        if seq and any(b.startswith(seq) for b in two) and len(seq) == 1:
            self.pending = seq
            return None
        if key.mods & Key.CTRL:
            commands = {"d": lambda: self.wheel(0, page / 2), "u": lambda: self.wheel(0, -page / 2),
                        "f": lambda: self.wheel(0, page), "b": lambda: self.wheel(0, -page)}
            if key.name.lower() in commands:
                return commands[key.name.lower()]()
            return self.send_key(key) if key.name.lower() in "acvxz" else None
        if key.name in ("Escape", "Enter", "Tab", "Left", "Right", "Home", "End", "Backspace", "Delete"):
            return self.send_key(key)
        return self.normal_command(key.name)
        if key.name in ("Escape", "Enter", "Tab"):
            return self.send_key(key)
        return self.normal_command(key.name)

    def normal_command(self, name):
        row = self.row_css()
        page = max(self.image_rows - 2, 1) * row
        tab = self.tab
        if name in ("j", "Down"):
            self.wheel(0, 3 * row)
        elif name in ("k", "Up"):
            self.wheel(0, -3 * row)
        elif name == "h":
            self.wheel(-6 * self.cell_w * self.css_per_px()[0], 0)
        elif name == "l":
            self.wheel(6 * self.cell_w * self.css_per_px()[0], 0)
        elif name in (" ", "PageDown"):
            self.wheel(0, page)
        elif name == "PageUp":
            self.wheel(0, -page)
        elif name == "gg":
            self.evaluate("scrollTo(scrollX,0)")
        elif name == "G":
            self.evaluate("scrollTo(scrollX,document.scrollingElement?document.scrollingElement.scrollHeight:1e9)")
        elif name == "H":
            self.history(-1)
        elif name == "L":
            self.history(1)
        elif name in ("r", "F5"):
            self.cdp.send("Page.reload", {}, self.session())
        elif name == "R":
            self.cdp.send("Page.reload", {"ignoreCache": True}, self.session())
        elif name == "o":
            self.prompt = ["open", "", 0]
        elif name in ("O", "t"):
            self.prompt = ["tabopen", "", 0]
        elif name == "go" and tab:
            self.prompt = ["open", tab.url, len(tab.url)]
        elif name == "gO" and tab:
            self.prompt = ["tabopen", tab.url, len(tab.url)]
        elif name == ":":
            self.prompt = ["cmd", "", 0]
        elif name in ("/", "?"):
            self.prompt = ["search" if name == "/" else "rsearch", "", 0]
        elif name in ("n", "N") and self.search:
            text, backwards = self.search
            self.find(text, backwards ^ (name == "N"))
        elif name in ("f", "F"):
            self.start_hints(newtab=name == "F")
        elif name == "i":
            self.set_mode("insert")
        elif name == "gi":
            if self.evaluate(FOCUS_INPUT_JS):
                self.set_mode("insert")
            else:
                self.flash("No text field on this page")
        elif name == "yy" and tab:
            self.yank(tab.url, "URL")
        elif name == "yt" and tab:
            self.yank(tab.title, "title")
        elif name == "ys":
            self.yank(self.evaluate(SELECTION_JS), "selection")
        elif name in ("J", "gt"):
            self.cycle_tab(1)
        elif name in ("K", "gT"):
            self.cycle_tab(-1)
        elif name == "d":
            self.close_tab()
        elif name in ("ZZ", "ZQ"):
            self.quit = True
        self.status_dirty = True

    def find(self, text, backwards):
        found = self.evaluate(f"window.find({js_string(text)},false,{str(backwards).lower()},true)")
        if not found:
            self.flash(f"Text not found: {text}")

    # ---- prompt ------------------------------------------------------------

    def prompt_key(self, key):
        kind, text, pos = self.prompt
        self.status_dirty = True
        if key.is_("Escape") or (key.is_("Backspace") and not text):
            self.prompt = None
            self.history_pos = None
            return
        if key.is_("Enter"):
            self.prompt = None
            self.history_pos = None
            past = self.history.setdefault(kind, [])
            if text.strip() and (not past or past[-1] != text):
                past.append(text)
                del past[:-200]
            self.run_prompt(kind, text)
            return
        if key.name in ("Up", "Down") and not key.mods:
            past = self.history.get(kind, [])
            if past:
                pos = len(past) if self.history_pos is None else self.history_pos
                pos = max(0, min(pos + (-1 if key.name == "Up" else 1), len(past)))
                self.history_pos = pos
                text = past[pos] if pos < len(past) else ""
                self.prompt = [kind, text, len(text)]
            return
        name = key.name
        if key.mods & Key.CTRL:
            if name == "a":
                pos = 0
            elif name == "e":
                pos = len(text)
            elif name == "u":
                text, pos = text[pos:], 0
            elif name == "w":
                start = len(text[:pos].rstrip().rsplit(" ", 1)[0]) if " " in text[:pos].rstrip() else 0
                text, pos = text[:start] + text[pos:], start
        elif name == "Backspace":
            if pos:
                text, pos = text[: pos - 1] + text[pos:], pos - 1
        elif name == "Delete":
            text = text[:pos] + text[pos + 1:]
        elif name == "Left":
            pos = max(pos - 1, 0)
        elif name == "Right":
            pos = min(pos + 1, len(text))
        elif name == "Home":
            pos = 0
        elif name == "End":
            pos = len(text)
        elif key.text:
            text, pos = text[:pos] + key.text + text[pos:], pos + 1
        self.prompt = [kind, text, pos]

    def run_prompt(self, kind, text):
        if kind in ("open", "tabopen"):
            if text.strip():
                self.navigate(to_url(text, self.args.search), kind == "tabopen")
        elif kind in ("search", "rsearch"):
            if text:
                self.search = (text, kind == "rsearch")
                self.find(text, kind == "rsearch")
        elif kind == "cmd":
            command, _, rest = text.strip().partition(" ")
            if command in ("o", "open"):
                newtab = rest.startswith("-t ")
                rest = rest[3:] if newtab else rest
                if rest.strip():
                    self.navigate(to_url(rest, self.args.search), newtab)
            elif command in ("t", "tabopen", "open-t"):
                self.navigate(to_url(rest or "about:blank", self.args.search), True)
            elif command in ("q", "quit", "qa", "wq", "x"):
                self.quit = True
            elif command in ("back",):
                self.history(-1)
            elif command in ("forward",):
                self.history(1)
            elif command in ("reload", "r"):
                self.cdp.send("Page.reload", {}, self.session())
            elif command in ("tab-close", "close"):
                self.close_tab()
            elif command == "js":
                self.js_command(rest)
            elif command in ("version",):
                version = self.cdp.call("Browser.getVersion")
                self.flash(f"{version.get('product')}  ({version.get('userAgent')})", 10)
            elif command:
                self.flash(f"Unknown command: {command}")

    # ---- :js ----------------------------------------------------------------

    def js_command(self, rest):
        """:js [-main] CODE  or  :js [-main] -f NAME"""
        words = rest.split()
        main = False
        while words and words[0] in ("-main", "-m"):
            main = True
            rest = rest.strip()[len(words.pop(0)):].lstrip()
        if words and words[0] == "-f":
            if len(words) < 2:
                names = list_scripts()
                self.show_output([f"Scripts in {scripts_folder()}:"] + (names or ["(none)"]))
                return
            try:
                with open(script_path(words[1])) as handle:
                    code = handle.read()
            except (OSError, ValueError) as error:
                self.flash(f":js: {error}", 8)
                return
            self.run_js(code, main, expression=False)
        elif rest.strip():
            self.run_js(rest, main, expression=True)
        else:
            self.flash("Usage: :js [-main] CODE  or  :js [-main] -f SCRIPT", 6)

    def run_js(self, code, main, expression):
        """Run code without blocking the UI. By default it runs in kbrowse's
        isolated world: it shares the page's DOM but not its JavaScript, so
        the page can neither see it nor tamper with it. -main runs it in the
        page's own world, to reach the page's variables."""
        tab = self.tab
        if not tab or not tab.session:
            return
        self.flash("running…", 30)

        def evaluate(context):
            params = {"expression": js_program(code, expression), "awaitPromise": True,
                      "returnByValue": True, "silent": True, "userGesture": True}
            if context:
                params["contextId"] = context
            self.cdp.send("Runtime.evaluate", params, tab.session, done)

        def done(reply):
            result = reply.get("result", {})
            details = result.get("exceptionDetails")
            if details and expression:
                # Not an expression (several statements, say): run it as a
                # function body instead.
                self.run_js(code, main, expression=False)
                return
            if "error" in reply:
                self.show_js_result([], reply["error"].get("message", "failed"), False)
            elif details:
                text = (details.get("exception") or {}).get("description") or details.get("text", "error")
                self.show_js_result([], text, False)
            else:
                value = result.get("result", {}).get("value") or {}
                self.show_js_result(value.get("out", []), value.get("result", ""), value.get("ok", False))

        if main:
            evaluate(None)
        else:
            self.cdp.send("Page.createIsolatedWorld", {"frameId": tab.id, "worldName": "kbrowse",
                                                       "grantUniveralAccess": False}, tab.session,
                          lambda reply: evaluate(reply.get("result", {}).get("executionContextId")))

    def show_js_result(self, out, result, ok):
        lines = list(out)
        if not ok:
            lines.append("error: " + str(result))
        elif result != "undefined" or not out:
            lines.append("=> " + str(result))
        text = "\n".join(lines)
        self.message = ("", 0)
        if "\n" not in text and len(text) < self.cols - 20:
            self.flash(printable(text), 10)
        else:
            self.show_output(text.split("\n"))

    def show_output(self, lines):
        """A pane over the bottom half of the page. Esc or q closes it,
        j/k scroll, yy copies everything."""
        width = max(self.cols - 2, 10)
        wrapped = []
        for line in lines[:5000]:
            line = printable(line.expandtabs(4))
            wrapped += [line[i:i + width] for i in range(0, max(len(line), 1), width)]
        self.leave_modes()
        self.output = {"lines": wrapped, "raw": "\n".join(lines), "top": max(len(wrapped) - self.output_height(), 0)}
        self.status_dirty = True
        self.draw_output()

    def output_height(self):
        return max(min(self.image_rows // 2, self.image_rows - 2), 3)

    def draw_output(self):
        self.clear_overlay()
        height = self.output_height()
        first = self.image_rows - height + 1
        lines = self.output["lines"][self.output["top"]:self.output["top"] + height]
        style = "\x1b[0;97;48;2;30;30;30m"
        out = [self.put_overlay(first + i, 1, (" " + (lines[i] if i < len(lines) else "")).ljust(self.cols), style)
               for i in range(height)]
        self.write("".join(out))

    def scroll_output(self, step):
        output = self.output
        output["top"] = max(0, min(output["top"] + step, len(output["lines"]) - self.output_height()))
        self.draw_output()

    def output_key(self, key):
        if key.is_("Escape") or key.is_("q") or key.is_("Enter"):
            self.leave_modes()
        elif key.name in ("j", "Down"):
            self.scroll_output(1)
        elif key.name in ("k", "Up"):
            self.scroll_output(-1)
        elif key.name in (" ", "PageDown") or key.is_("d", Key.CTRL):
            self.scroll_output(self.output_height() - 1)
        elif key.name == "PageUp" or key.is_("u", Key.CTRL):
            self.scroll_output(1 - self.output_height())
        elif key.name == "g":
            self.scroll_output(-len(self.output["lines"]))
        elif key.name == "G":
            self.scroll_output(len(self.output["lines"]))
        elif key.name == "y":
            self.yank(self.output["raw"], "output")
        elif key.name == ":":
            self.leave_modes()
            self.prompt = ["cmd", "", 0]

    # ---- dialogs -----------------------------------------------------------

    def dialog_key(self, key):
        dialog = self.dialog
        accept = None
        if dialog["type"] == "prompt":
            if key.is_("Enter"):
                accept = True
            elif key.is_("Escape"):
                accept = False
            elif key.is_("Backspace"):
                dialog["text"] = dialog["text"][:-1]
            elif key.text:
                dialog["text"] += key.text
        elif key.is_("Enter") or key.is_("y") or (dialog["type"] == "alert" and key.is_("Escape")):
            accept = True
        elif key.is_("Escape") or key.is_("n"):
            accept = False
        self.status_dirty = True
        if accept is not None:
            params = {"accept": accept}
            if dialog["type"] == "prompt":
                params["promptText"] = dialog["text"]
            self.cdp.send("Page.handleJavaScriptDialog", params, dialog["session"])
            self.dialog = None

    # ---- link hints --------------------------------------------------------

    def start_hints(self, newtab=False):
        items = self.evaluate(HINTS_JS) or []
        if not items:
            self.flash("No links on screen")
            return
        labels = hint_labels(len(items))
        placed = []
        for label, item in zip(labels, items):
            col, row = self.css_to_cell(max(item[0], 0), item[3])
            placed.append((label, item, col, row))
        self.hints = {"items": placed, "typed": "", "newtab": newtab}
        self.set_mode("hint")
        self.draw_hints()

    def draw_hints(self):
        self.clear_overlay()
        typed = self.hints["typed"]
        out = []
        for label, _item, col, row in self.hints["items"]:
            if label.startswith(typed):
                out.append(self.put_overlay(row, col, label.upper()))
        self.write("".join(out))

    def hint_key(self, key):
        if key.is_("Escape"):
            self.leave_modes()
            return
        if key.is_("Backspace"):
            self.hints["typed"] = self.hints["typed"][:-1]
            self.draw_hints()
            return
        if len(key.name) != 1 or not key.name.isalpha():
            return
        typed = self.hints["typed"] + key.name.lower()
        matches = [h for h in self.hints["items"] if h[0].startswith(typed)]
        if not matches:
            return
        exact = [h for h in matches if h[0] == typed]
        if not exact:
            self.hints["typed"] = typed
            self.draw_hints()
            return
        _label, item, _col, _row = exact[0]
        newtab = self.hints["newtab"]
        self.leave_modes()
        _left, _top, x, y, href = item
        if newtab and href:
            self.new_tab(href)
            return
        self.click(x, y)
        self.check_editable()

    # ---- <select> menus ------------------------------------------------------
    # Chromium draws <select> popups as separate native windows that the
    # screencast does not show, so kbrowse draws its own menu.

    def open_menu(self, options, x, y):
        self.menu = {"opts": options["opts"], "cursor": max(options["sel"], 0), "x": x, "y": y,
                     "col": self.css_to_cell(options["x"], options["y"])[0],
                     "row": self.css_to_cell(options["x"], options["y"])[1], "top": 0}
        self.set_mode("menu")
        self.draw_menu()

    def menu_layout(self):
        menu = self.menu
        count = len(menu["opts"])
        height = min(count, max(self.image_rows - 2, 3))
        row = menu["row"]
        if row + height - 1 > self.image_rows:
            row = max(1, self.image_rows - height + 1)
        if menu["cursor"] < menu["top"]:
            menu["top"] = menu["cursor"]
        elif menu["cursor"] >= menu["top"] + height:
            menu["top"] = menu["cursor"] - height + 1
        width = min(max(len(o[0]) for o in menu["opts"]) + 2, self.cols - 2, 80)
        return row, height, width

    def draw_menu(self):
        self.clear_overlay()
        menu = self.menu
        row, height, width = self.menu_layout()
        out = []
        for i in range(height):
            index = menu["top"] + i
            text, disabled = menu["opts"][index]
            line = printable(" " + text)[: width].ljust(width)
            if index == menu["cursor"]:
                style = "\x1b[0;97;48;2;40;100;200;1m"
            elif disabled:
                style = "\x1b[0;90;48;2;235;235;235m"
            else:
                style = "\x1b[0;30;48;2;250;250;250m"
            out.append(self.put_overlay(row + i, menu["col"], line, style))
        menu["box"] = (row, menu["col"], height, width)
        self.write("".join(out))

    def menu_key(self, key):
        menu = self.menu
        count = len(menu["opts"])
        if key.is_("Escape") or key.is_("q"):
            self.leave_modes()
            return
        if key.is_("Enter") or key.is_(" "):
            self.choose(menu["cursor"])
            return
        step = {"j": 1, "Down": 1, "k": -1, "Up": -1, "Tab": 1, "PageDown": 10, "PageUp": -10}.get(key.name)
        if key.name == "g":
            menu["cursor"] = 0
        elif key.name == "G":
            menu["cursor"] = count - 1
        elif step:
            menu["cursor"] = max(0, min(count - 1, menu["cursor"] + step))
        elif key.text:
            # Jump to the next option starting with the typed character.
            for offset in range(1, count + 1):
                index = (menu["cursor"] + offset) % count
                if menu["opts"][index][0][:1].lower() == key.text.lower():
                    menu["cursor"] = index
                    break
        self.draw_menu()

    def menu_click(self, event):
        row, col, height, width = self.menu["box"]
        cell_row = int((event.y - 1) / self.cell_h) + 1
        cell_col = int((event.x - 1) / self.cell_w) + 1
        if row <= cell_row < row + height and col <= cell_col < col + width:
            self.choose(self.menu["top"] + cell_row - row)
        else:
            self.leave_modes()

    def choose(self, index):
        menu = self.menu
        if menu["opts"][index][1]:
            return
        self.leave_modes()
        self.evaluate(SELECT_SET_JS % (menu["x"], menu["y"], index))

    # ---- DevTools events ---------------------------------------------------

    def on_event(self, method, params, session):
        if method == "Page.screencastFrame":
            if session == self.session():
                if self.frame:
                    self.cdp.send("Page.screencastFrameAck", {"sessionId": self.frame[2]}, self.frame[3])
                self.frame = (params["data"], params["metadata"], params["sessionId"], session)
            else:
                self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}, session)
        elif method == "Target.targetCreated":
            info = params["targetInfo"]
            if info["type"] == "page" and info.get("url", "").startswith(f"chrome-extension://{UBLOCK_ID}/") \
                    and not info.get("openerId"):
                self.cdp.send("Target.closeTarget", {"targetId": info["targetId"]})  # first-run page
            elif info["type"] == "page":
                self.add_tab(info)
                if info.get("openerId") in self.tabs and info["targetId"] != self.active:
                    self.activate(info["targetId"])  # follow popups, e.g. sign-in windows
                self.status_dirty = True
        elif method == "Target.targetInfoChanged":
            info = params["targetInfo"]
            tab = self.tabs.get(info["targetId"])
            if tab:
                tab.url, tab.title = info.get("url", tab.url), info.get("title", tab.title)
                self.status_dirty = True
                if tab.url.startswith(f"chrome-extension://{UBLOCK_ID}/") and not tab.opener \
                        and tab.id != self.active:
                    self.cdp.send("Target.closeTarget", {"targetId": tab.id})  # first-run page
        elif method == "Target.targetDestroyed":
            self.remove_tab(params["targetId"])
        elif method == "Target.detachedFromTarget":
            for tab in self.tabs.values():
                if tab.session == params.get("sessionId"):
                    tab.session = None
        elif method == "Page.frameNavigated":
            if session == self.session() and not params["frame"].get("parentId"):
                if self.hints or self.menu or self.mode != "normal":
                    self.leave_modes()
        elif method == "Page.frameStartedLoading" or method == "Page.frameStoppedLoading":
            for tab in self.tabs.values():
                if tab.session == session and params.get("frameId") == tab.id:
                    tab.loading = method == "Page.frameStartedLoading"
                    self.status_dirty = True
        elif method == "Page.javascriptDialogOpening":
            if session != self.session():
                for tab in self.tabs.values():
                    if tab.session == session:
                        self.activate(tab.id)
            self.leave_modes()
            self.dialog = {"type": params["type"], "message": params.get("message", ""),
                           "text": params.get("defaultPrompt", ""), "session": session}
            self.status_dirty = True
        elif method == "Page.javascriptDialogClosed":
            if self.dialog and self.dialog["session"] == session:
                self.dialog = None
                self.status_dirty = True

    def poll_title(self):
        """Chromium does not send an event when only document.title changes."""
        self.next_poll = time.time() + 1.0
        tab = self.tab
        if not tab:
            return

        def done(reply):
            info = reply.get("result", {}).get("targetInfo")
            if info and (info.get("title"), info.get("url")) != (tab.title, tab.url):
                tab.title, tab.url = info.get("title", tab.title), info.get("url", tab.url)
                self.status_dirty = True

        self.cdp.send("Target.getTargetInfo", {"targetId": tab.id}, callback=done)

    def remove_tab(self, target_id):
        tab = self.tabs.pop(target_id, None)
        if not tab:
            return
        index = self.order.index(target_id) if target_id in self.order else 0
        if target_id in self.order:
            self.order.remove(target_id)
        self.closing.discard(target_id)
        if target_id == self.active:
            self.active = None
            self.frame = None
            if tab.opener in self.tabs:
                self.activate(tab.opener)
            elif self.order:
                self.activate(self.order[min(index, len(self.order) - 1)])
            else:
                self.new_tab("about:blank")
        self.status_dirty = True

    def draw_frame(self):
        data, metadata, frame_session, session = self.frame
        self.frame = None
        try:
            width, height = png_size(data)
        except (ValueError, struct.error):
            width, height = self.area
        wrong_size = abs(width - self.frame_px[0]) > 2 or abs(height - self.frame_px[1]) > 2
        if wrong_size and (not frame_session or time.time() < self.settle_until):
            # A screenshot, or a frame from before the window resize settled:
            # drawing it would re-place the image and flash. Take a fresh
            # screenshot later in case no other frame comes.
            if frame_session:
                self.cdp.send("Page.screencastFrameAck", {"sessionId": frame_session}, session)
                self.frame_due = max(self.settle_until, time.time()) + 0.2
            return
        self.view = (metadata.get("deviceWidth") or (self.view or self.target_css)[0],
                     metadata.get("deviceHeight") or (self.view or self.target_css)[1])
        self.frame_due = None
        self.gfx.draw(data, width, height, self.cols, self.image_rows)
        log("frame", width, height, len(data))
        if frame_session:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": frame_session}, session)

    def on_resize(self):
        self.resized = False
        old = (self.rows, self.cols, self.area)
        self.geometry()
        if old == (self.rows, self.cols, self.area):
            return
        self.leave_modes()
        self.overlay_cells = []
        self.write("\x1b[H\x1b[2J")
        self.gfx.placed = None
        tab = self.tab
        if tab:
            self.cdp.send("Page.stopScreencast", {}, tab.session)
            self.size_window(tab)
            self.start_screencast(tab)
        self.status_dirty = True

    # ---- main loop -----------------------------------------------------------

    def connect(self):
        self.browser.window_css = self.target_css
        modes = [self.args.cdp] if self.args.cdp != "auto" else ["pipe", "port"]
        if self.args.cdp == "auto" and self.browser.flatpak:
            modes = ["port"]  # flatpak run does not pass the pipe's file descriptors on
        last_error = None
        for mode in modes:
            try:
                transport = self.browser.start("about:blank", mode)
                self.cdp = CDP(transport, self.wake_w)
                self.version = self.cdp.call("Browser.getVersion", timeout=20)
                return
            except (CDPError, OSError) as error:
                last_error = error
                log("connect failed:", mode, error)
                if self.browser.proc and self.browser.proc.poll() is None:
                    self.browser.proc.kill()
                    self.browser.proc.wait()
                self.browser.stop(kill_xvfb=False)
        raise SystemExit(f"kbrowse: could not connect to Chromium ({last_error}). "
                         "Run with --log FILE to see why.")

    def run(self):
        self.geometry()
        self.connect()
        self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        for _ in range(50):  # the first tab can take a moment to appear
            pages = [t for t in self.cdp.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
            if pages:
                break
            time.sleep(0.1)
        log("pages at start:", [(p["targetId"][:8], p.get("url")) for p in pages])
        for info in pages:
            self.add_tab(info)
        self.drain()  # pick up targetCreated events before choosing a tab
        # Chromium may have restored tabs from last time next to the blank
        # tab it opened for us. Keep them only with --restore.
        blank = [p for p in pages if p.get("url") in ("about:blank", "")]
        others = [p for p in pages if p not in blank]
        url = to_url(self.args.url, self.args.search) if self.args.url else None
        if not self.args.restore:
            for extra in others:
                self.close_tab(extra["targetId"])
                self.remove_tab(extra["targetId"])
            others = []
        if blank and (url or not others):
            first = blank[-1]["targetId"]
            for extra in blank[:-1]:
                self.close_tab(extra["targetId"])
                self.remove_tab(extra["targetId"])
        elif others:
            first = others[0]["targetId"]
            for extra in blank:
                self.close_tab(extra["targetId"])
                self.remove_tab(extra["targetId"])
        else:
            first = None
        if first:
            self.activate(first)
            if url:
                self.navigate(url)
        else:
            self.new_tab(url or "about:blank")
        product = self.version.get("product", "")
        blocker = "ad blocker on" if self.browser.adblock else "ad blocker off"
        self.flash(f"{product}{' (headless)' if self.args.headless else ''}, {blocker} — "
                   "o: open, f: hints, i: insert, :q: quit", 6)

        def on_winch(_sig, _frame):
            self.resized = True
            try:
                os.write(self.wake_w, b"w")
            except OSError:
                pass

        signal.signal(signal.SIGWINCH, on_winch)

        while not self.quit:
            timeout = 0.5 if self.message[0] else 1.0
            if self.frame_due:
                timeout = max(0.01, min(timeout or 1, self.frame_due - time.time()))
            try:
                ready, _, _ = select.select([self.tin, self.wake_r], [], [], timeout)
            except InterruptedError:
                ready = []
            if self.tin in ready:
                data = os.read(self.tin, 65536)
                if not data:
                    break
                for event in self.parser.feed(data):
                    log("input", event)
                    self.on_input(event)
                    if self.quit:
                        break
                self.flush_move()
            if self.wake_r in ready:
                try:
                    os.read(self.wake_r, 65536)
                except BlockingIOError:
                    pass
            self.drain()
            if self.cdp.closed:
                break
            if self.resized:
                self.on_resize()
            if time.time() >= self.next_poll:
                self.poll_title()
            if self.frame_due and time.time() >= self.frame_due:
                self.capture()
            if self.frame:
                self.draw_frame()
            if self.message[0] and time.time() >= self.message[1]:
                self.message = ("", 0)
                self.status_dirty = True
            if self.status_dirty:
                self.draw_status()

    def drain(self):
        while self.cdp.inbox:
            item = self.cdp.inbox.popleft()
            try:
                if item[0] == "event":
                    self.on_event(*item[1:])
                elif item[0] == "reply":
                    item[1](item[2])
            except CDPError as error:
                log("event handling failed:", item[:2], error)

    def shutdown(self):
        browser = getattr(self, "browser", None)
        if not browser:
            return
        cdp = getattr(self, "cdp", None)
        if cdp and not cdp.closed:
            # A clean close lets Chromium save cookies, so sign-ins persist.
            cdp.send("Browser.close")
        browser.stop()


def main():
    global LOG
    parser = argparse.ArgumentParser(
        description="Browse the web in kitty with a modern Chromium. Works over ssh.",
        epilog="Keys (normal mode): o/O open (new tab), go edit URL, f/F hints, i insert, gi first "
               "text field, j/k/h/l scroll, ctrl-d/u half page, gg/G top/bottom, H/L back/forward, "
               "r reload, / ? n N search, J/K tabs, d close tab, yy copy URL, :q or ctrl-q quit. "
               "Esc leaves insert mode.")
    parser.add_argument("url", nargs="?", help="page to open")
    parser.add_argument("--browser", help="Chromium/Chrome executable or Flatpak app id, e.g. "
                        "org.chromium.Chromium (default: search PATH, then installed Flatpaks)")
    parser.add_argument("--profile", help="profile directory (cookies, logins); default is kept between runs")
    parser.add_argument("--headless", action="store_true",
                        help="use headless Chromium instead of a window in Xvfb (easier to detect as a bot)")
    parser.add_argument("--scale", type=float,
                        help="device pixel ratio (default: 2 for HiDPI terminals, otherwise 1)")
    parser.add_argument("--no-adblock", action="store_true", help="do not load uBlock Origin Lite")
    parser.add_argument("--extension", action="append", default=[],
                        help="load an unpacked Chromium extension from this folder (repeatable)")
    parser.add_argument("--restore", action="store_true", help="keep the tabs Chromium restores from last time")
    parser.add_argument("--no-sandbox", action="store_true", help="pass --no-sandbox to Chromium")
    parser.add_argument("--cdp", choices=["auto", "pipe", "port"], default="auto",
                        help="how to talk to Chromium (default: pipe, then port)")
    parser.add_argument("--right-click", action="store_true",
                        help="send right clicks to the page (native context menus are not shown)")
    parser.add_argument("--downscale", type=float, default=1.0,
                        help="send frames at this fraction of the terminal's pixel size, e.g. 0.5 on slow links")
    parser.add_argument("--wheel", type=float, default=80, help="pixels per mouse wheel step")
    parser.add_argument("--search", default="https://duckduckgo.com/?q={}", help="search URL, {} is the query")
    parser.add_argument("--chrome-flag", action="append", default=[], help="extra Chromium flag (repeatable)")
    parser.add_argument("--log", help="write a debug log to this file")
    parser.add_argument("--version", action="version", version=f"kbrowse {VERSION}")
    args = parser.parse_args()

    if args.log:
        LOG = open(args.log, "a", buffering=1)
    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        sys.exit("kbrowse: needs a terminal")

    app = App(args)
    if not args.no_adblock:
        print("kbrowse: checking the ad blocker…", file=sys.stderr, flush=True)
    app.browser = Browser(args, None)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: setattr(app, "quit", True) or os.write(app.wake_w, b"q"))
    app.enter_terminal()
    error = None
    try:
        app.run()
    except SystemExit as exit_error:
        error = exit_error
    except Exception as exception:  # noqa: BLE001
        import traceback
        log(traceback.format_exc())
        error = exception
    finally:
        try:
            app.leave_terminal()
        finally:
            app.shutdown()
    if error:
        if isinstance(error, SystemExit):
            raise error
        sys.exit(f"kbrowse: {error!r}")


if __name__ == "__main__":
    main()
