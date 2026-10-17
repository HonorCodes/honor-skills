#!/usr/bin/env python3
"""CLI and HTTP lifecycle tests: terminal status, durable edits, and no leftover servers or files.

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s visual-review/tests -p 'test_*.py' -v
The tests start real loopback servers. They never open a real browser: every `open` passes
--no-browser or names a stub command in VISUAL_REVIEW_BROWSER.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import contextmanager
from pathlib import Path

from fixtures_util import jpeg_header
from test_primitives import STUB

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT = TESTS_DIR.parent / "review.py"
FIXTURES = TESTS_DIR / "fixtures"
LOCK_FILES = {".visual-review.lock", ".visual-review-io.lock"}
TRANSIENT_PREFIX = ".visual-review-"
RUNTIME_FIELDS = ["pid", "session_id", "started_at", "token", "url"]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REVISION_KEY = b"visual-review-markup-revision"
# For tests with slow steps between page requests (CLI commands, large request bodies): a
# connected page that sends no heartbeat ends with the default 0.5 s close timeout.
LONG_CLOSE = ("--close-timeout", "60")

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def result_files(directory):
    """Sorted names in a results folder, without the permanent lock files."""
    return sorted(path.name for path in directory.iterdir() if path.name not in LOCK_FILES)


def transient_directories(directory):
    """Server-owned transient directories in a results folder; the lock files share the prefix."""
    return sorted(path.name for path in directory.iterdir() if path.name.startswith(TRANSIENT_PREFIX) and path.is_dir())


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_hard(pid: int) -> None:
    os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


def ping_alive(session, timeout=2.0):
    """Ping the server of `session` (an `open` result or runtime.json) without review.py's client."""
    try:
        with DIRECT.open(session["url"] + "api/ping", timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return body == {"alive": True, "session_id": session["session_id"]}


def port_refuses(url):
    """True when nothing listens on the URL's loopback port any more."""
    port = urllib.parse.urlsplit(url).port
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


# The gate module runs as sitecustomize in review.py processes started with Gates.env(). A gate
# writes `<name>.reached` (the process ID) into the gate directory, then blocks until the test
# creates `<name>.release`. A gate that fires inside a write waits until its thread has released
# the result-file I/O lock, because a process frozen inside that lock blocks every CLI read.
GATE_SITE = r"""
import io
import json
import os
import sys
import threading
import time

_dir = os.environ.get("VISUAL_REVIEW_TEST_GATE_DIR", "")
_gates = frozenset(filter(None, os.environ.get("VISUAL_REVIEW_TEST_GATES", "").split(",")))
_command = sys.argv[1] if len(sys.argv) > 1 else ""
_sleep = time.sleep
_open = io.open
_replace = os.replace
_os_open = os.open
_os_close = os.close
_guard = threading.Lock()
_fired = set()
_io_lock_fds = set()
_thread = threading.local()


class Unbound:
    # A replacement for a module function. Python 3.9's pathlib stores os functions as class
    # attributes, where a plain function would be bound as a method and get an extra argument.
    def __init__(self, function):
        self.function = function

    def __call__(self, *args, **kwargs):
        return self.function(*args, **kwargs)


def gate(name):
    reached = os.path.join(_dir, name + ".reached")
    with _open(reached + ".tmp", "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
    _replace(reached + ".tmp", reached)
    release = os.path.join(_dir, name + ".release")
    while not os.path.exists(release):
        _sleep(0.01)


def first_time(name):
    with _guard:
        if name not in _gates or name in _fired:
            return False
        _fired.add(name)
        return True


def when_unlocked(action):
    if getattr(_thread, "held", 0):
        _thread.pending = getattr(_thread, "pending", []) + [action]
    else:
        action()


def tracked_open(path, flags, *args, **kwargs):
    fd = _os_open(path, flags, *args, **kwargs)
    if os.path.basename(os.fsdecode(os.fspath(path))) == ".visual-review-io.lock":
        with _guard:
            _io_lock_fds.add(fd)
        _thread.held = getattr(_thread, "held", 0) + 1
    return fd


def tracked_close(fd):
    _os_close(fd)
    with _guard:
        lock_fd = fd in _io_lock_fds
        _io_lock_fds.discard(fd)
    if lock_fd:
        _thread.held = getattr(_thread, "held", 1) - 1
        if not _thread.held:
            pending, _thread.pending = getattr(_thread, "pending", []), []
            for action in pending:
                action()


def written_status(path):
    try:
        with _open(path, encoding="utf-8") as stream:
            return json.load(stream).get("status")
    except (OSError, ValueError, AttributeError):
        return None


def gated_replace(source, destination, *args, **kwargs):
    name = os.path.basename(os.fsdecode(os.fspath(destination)))
    if name == "runtime.json" and "refuse-runtime" in _gates:
        raise OSError("VISUAL_REVIEW_TEST_RUNTIME_REFUSED")
    result = _replace(source, destination, *args, **kwargs)
    if name == "review.json":
        status = written_status(destination)
        gate_name = "save" if status == "open" else "terminal"
        if status is not None and first_time(gate_name):
            when_unlocked(lambda: gate(gate_name))
    elif name.endswith(".marked.png") and first_time("marked"):
        when_unlocked(lambda: gate("marked"))
    return result


if _command == "__serve" and _gates:
    with _open(os.path.join(_dir, "attempts"), "a", encoding="ascii") as attempts:
        attempts.write(str(os.getpid()) + "\n")
    if "fail" in _gates:
        print("VISUAL_REVIEW_TEST_STARTUP_FAILURE", file=sys.stderr, flush=True)
        os._exit(17)
    os.open = Unbound(tracked_open)
    os.close = Unbound(tracked_close)
    os.replace = Unbound(gated_replace)
    if "stall" in _gates:
        import socketserver

        _serve_forever = socketserver.BaseServer.serve_forever

        def stalled_serve_forever(self, *args, **kwargs):
            print("VISUAL_REVIEW_TEST_STALLED_STARTUP", file=sys.stderr, flush=True)
            gate("stall")
            return _serve_forever(self, *args, **kwargs)

        socketserver.BaseServer.serve_forever = stalled_serve_forever
    if "teardown" in _gates:
        import shutil

        _rmtree = shutil.rmtree

        def gated_rmtree(path, *args, **kwargs):
            name = os.path.basename(os.fsdecode(os.fspath(path)))
            if name.startswith(".visual-review-") and first_time("teardown"):
                gate("teardown")
            return _rmtree(path, *args, **kwargs)

        shutil.rmtree = gated_rmtree
    if "opens" in _gates:
        import builtins

        _watched = os.path.realpath(os.environ["VISUAL_REVIEW_TEST_WATCH_DIR"])

        def logged_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                path = os.path.realpath(os.fsdecode(os.fspath(file)))
                if os.path.dirname(path) == _watched:
                    with _open(os.path.join(_dir, "opens.log"), "a", encoding="utf-8") as log:
                        log.write(path + "\n")
            return _open(file, *args, **kwargs)

        io.open = builtins.open = Unbound(logged_open)

if _command == "open" and "wait" in _gates:

    def gated_sleep(seconds):
        if first_time("wait"):
            gate("wait")
        _sleep(seconds)

    time.sleep = gated_sleep
"""


class Gates:
    """Pause points for review.py processes; see GATE_SITE.

    Gates: `terminal` (server, after it writes a terminal review.json), `save` (server, after it
    writes an open review.json), `marked` (server, after it replaces a marked PNG), `wait`
    (`open`, at its first sleep), `stall` (server, before it serves HTTP), `fail` (server exits
    17 at startup), `refuse-runtime` (the server's runtime.json write fails), `teardown` (server,
    when it deletes its transient directory: it no longer answers but still holds runtime.json),
    `opens` (server logs each open() of a file in VISUAL_REVIEW_TEST_WATCH_DIR).
    """

    def __init__(self, directory, names):
        self.directory = directory
        self.names = tuple(names)
        directory.mkdir()
        (directory / "sitecustomize.py").write_text(GATE_SITE, encoding="utf-8")

    def env(self, *only, **extra):
        """Environment for review.py processes; `only` limits which of these gates they use."""
        environment = dict(os.environ)
        search_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(self.directory) + (os.pathsep + search_path if search_path else "")
        environment["VISUAL_REVIEW_TEST_GATE_DIR"] = str(self.directory)
        environment["VISUAL_REVIEW_TEST_GATES"] = ",".join(only or self.names)
        environment.update(extra)
        return environment

    def wait_reached(self, name, timeout=15):
        """Wait until a process reaches the gate; return that process's ID."""
        marker = self.directory / f"{name}.reached"
        deadline = time.monotonic() + timeout
        while not marker.exists():
            if time.monotonic() >= deadline:
                raise AssertionError(f"no process reached the {name} gate within {timeout} s")
            time.sleep(0.01)
        return int(marker.read_text(encoding="ascii"))

    def release(self, name):
        (self.directory / f"{name}.release").touch()

    def release_all(self):
        for name in self.names:
            self.release(name)

    def attempts(self):
        """Process IDs of every server start that saw these gates."""
        path = self.directory / "attempts"
        return [int(line) for line in path.read_text(encoding="ascii").split()] if path.exists() else []

    def opened(self, path):
        """How many times a gated server opened `path` (gate `opens`)."""
        log = self.directory / "opens.log"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return lines.count(os.path.realpath(path))


def png_chunk(kind, payload, *, corrupt_crc=False):
    checksum = zlib.crc32(kind + payload)
    if corrupt_crc:
        checksum ^= 1
    return struct.pack("!I", len(payload)) + kind + payload + struct.pack("!I", checksum)


def fixture_png(width=64, height=48, color=(255, 255, 255)):
    """Independent PNG specification fixture: opaque RGB in one color, filter zero."""
    raw = (b"\x00" + bytes(color) * width) * height
    return (
        PNG_SIGNATURE
        + png_chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw))
        + png_chunk(b"IEND", b"")
    )


def fixture_png_with_ancillary(chunks, width=4, height=2, *, corrupt_crc=False):
    """Insert independently checksummed ancillary chunks between PNG IHDR and IDAT."""
    base = fixture_png(width, height)
    encoded = b"".join(png_chunk(kind, payload, corrupt_crc=corrupt_crc) for kind, payload in chunks)
    return base[:33] + encoded + base[33:]


def png_dimensions(data):
    """Width and height from the IHDR chunk that opens a PNG."""
    if data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise AssertionError("data is not a PNG that starts with IHDR")
    return struct.unpack("!II", data[16:24])


def with_revision_chunk(png, revision):
    """The file an upload of `png` must produce: the same chunks plus one revision tEXt chunk after IHDR."""
    return png[:33] + png_chunk(b"tEXt", REVISION_KEY + b"\x00" + str(revision).encode("ascii")) + png[33:]


def text_chunks(data):
    """(keyword, text) of every tEXt chunk in a PNG, in file order."""
    found = []
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length, kind = struct.unpack("!I4s", data[offset : offset + 8])
        if kind == b"tEXt":
            keyword, _, text = data[offset + 8 : offset + 8 + length].partition(b"\x00")
            found.append((keyword, text))
        offset += 12 + length
    return found


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# The marked PNG that save_marked_baseline uploads for first.png.
BASELINE_MARKED = fixture_png(64, 48, (239, 68, 68))


def save_marked_baseline(harness, opened):
    """Mark first.png and upload its marked PNG, so review.json names a fresh PNG at revision 1."""
    pen = {
        "type": "pen",
        "color": "#ef4444",
        "width": 3,
        "group": "source-bound-baseline",
        "points": [[10, 10], [40, 10]],
    }
    notes = "This verdict belongs to the original white capture."
    harness.prepare(opened)
    harness.request(
        opened,
        "api/save",
        {"index": 0, "revision": 1, "notes": notes, "markup": [pen], "png": None},
    )
    harness.upload(opened, 0, 1, BASELINE_MARKED)
    return notes, pen


def open_args(images, results):
    return [
        sys.executable,
        str(SCRIPT),
        "open",
        "--images",
        str(images),
        "--results",
        str(results),
        "--question",
        "Which capture is correct?",
        "--idle-timeout",
        "30",
        "--close-timeout",
        "0.5",
        "--no-browser",
    ]


def memory_limits():
    """Popen options that cap the address space and CPU time of a child and the server it starts."""
    if sys.platform == "darwin":
        raise unittest.SkipTest("macOS rejects a finite RLIMIT_AS")
    try:
        import resource
    except ImportError:
        raise unittest.SkipTest("memory and CPU limits need the POSIX resource module") from None

    def set_child_limits():
        address_space = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))

    return {"preexec_fn": set_child_limits}


def assert_process_stopped(harness, opened, results=None):
    """No server answers for the session, its port refuses, and it left no runtime files."""
    results = harness.results if results is None else results
    deadline = time.monotonic() + 5
    while True:
        answers = ping_alive(opened, timeout=1)
        refused = port_refuses(opened["url"])
        runtime_left = (results / "runtime.json").exists()
        transient = transient_directories(results) if results.exists() else []
        running = process_exists(opened["pid"])
        stopped = not answers and refused and not runtime_left and not transient and not running
        if stopped or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    harness.assertFalse(answers, "a server still answers on the session URL")
    harness.assertTrue(refused, "the session port still accepts connections")
    harness.assertFalse(runtime_left, "runtime.json was left behind")
    harness.assertEqual(transient, [], "transient directories were left behind")
    harness.assertFalse(running, f"server process {opened['pid']} is still running")


def http_error(harness, opened, endpoint, payload):
    with harness.assertRaises(urllib.error.HTTPError) as failure:
        harness.request(opened, endpoint, payload)
    error = failure.exception
    body = error.read().decode("utf-8")
    error.close()
    return error.code, json.loads(body)


def assert_source_change_error(harness, opened, endpoint, payload):
    code, body = http_error(harness, opened, endpoint, payload)
    harness.assertEqual(code, 400, body)
    harness.assertIn("source changed since open", body.get("error", ""))


class ReviewHarness(unittest.TestCase):
    """Own real loopback servers and private test folders across lifecycle cases."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="visual-review-test-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(self._stop_everything)
        self.images = self.root / "images"
        self.images.mkdir()
        self.original = fixture_png()
        (self.images / "first.png").write_bytes(self.original)
        (self.images / "untouched.png").write_bytes(self.original)
        self.results = self.root / "results"
        self._gates = []
        self._children = []

    def _stop_everything(self):
        for gates in self._gates:
            gates.release_all()
        for child in self._children:
            try:
                child.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=10)
        if SCRIPT.exists() and self.results.exists():
            self.cli("cleanup", check=False, timeout=30)

    def gates(self, *names):
        gates = Gates(self.root / f"gates-{len(self._gates)}", names)
        self._gates.append(gates)
        return gates

    def spawn(self, args, env=None):
        child = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self._children.append(child)
        return child

    def cli(self, command, *flags, check=True, browser=False, env=None, results=None, memory_limited=False, timeout=25):
        selected_results = self.results if results is None else results
        args = [sys.executable, str(SCRIPT), command, "--results", str(selected_results)]
        if command == "open":
            args += [
                "--images",
                str(self.images),
                "--question",
                "Which capture is correct?",
                "--idle-timeout",
                "30",
                "--close-timeout",
                "0.5",
            ]
            if not browser:
                args.append("--no-browser")
        result = subprocess.run(
            args + list(flags),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
            check=False,
            **(memory_limits() if memory_limited else {}),
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def start(self, *flags, memory_limited=False, env=None, browser=False):
        opened = json.loads(self.cli("open", *flags, memory_limited=memory_limited, env=env, browser=browser).stdout)
        self.assertEqual(opened["status"], "open")
        self.assertIsInstance(opened.get("browser"), bool)
        self.assertIsInstance(opened.get("reused"), bool)
        runtime_path = self.results / "runtime.json"
        self.assertTrue(runtime_path.is_file(), "runtime.json positive control")
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(runtime), RUNTIME_FIELDS)
        self.assertEqual(
            (runtime["url"], runtime["pid"], runtime["session_id"]),
            (opened["url"], opened["pid"], opened["session_id"]),
        )
        self.assertRegex(opened["url"], r"^http://127\.0\.0\.1:[0-9]+/" + re.escape(runtime["token"]) + "/$")
        self.assertTrue(ping_alive(runtime), "a started server answers ping")
        return opened

    def status(self, results=None):
        """review.json as `status` prints it; the CLI reads under the result-file I/O lock."""
        return json.loads(self.cli("status", results=results).stdout)

    def request(self, opened, endpoint, payload=None, origin=None, timeout=5):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(
            opened["url"] + endpoint, data=None if payload is None else json.dumps(payload).encode(), headers=headers
        )
        with DIRECT.open(req, timeout=timeout) as response:
            return json.load(response)

    def in_background(self, call):
        """Run `call` on a worker thread; the result dict gets "response" or "error"."""
        result = {}

        def run():
            try:
                result["response"] = call()
            except BaseException as exc:
                result["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return result, worker

    def request_in_background(self, opened, endpoint, payload):
        return self.in_background(lambda: self.request(opened, endpoint, payload, timeout=30))

    def prepare(self, opened, index=0, size=(64, 48)):
        """Report a display size for an image, as the page does once the image has loaded."""
        prepared = self.request(opened, "api/prepare", {"index": index, "width": size[0], "height": size[1]})
        self.assertTrue(prepared["ready"])
        return prepared

    def upload(self, opened, index, revision, body, timeout=10):
        """POST a marked PNG for a markup revision, as the page does; revision None sends no query."""
        query = "" if revision is None else f"?revision={revision}"
        request = urllib.request.Request(
            opened["url"] + f"api/marked/{index}{query}",
            data=body,
            headers={"Content-Type": "image/png"},
            method="POST",
        )
        with DIRECT.open(request, timeout=timeout) as response:
            return json.load(response)

    def upload_error(self, opened, index, revision, body):
        """Status code and JSON body of an upload the server refuses."""
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.upload(opened, index, revision, body)
        error = failure.exception
        text = error.read().decode("utf-8")
        error.close()
        return error.code, json.loads(text)

    def media_response(self, opened, endpoint):
        with DIRECT.open(opened["url"] + endpoint, timeout=60) as response:
            return response.headers.get("Content-Type"), response.read()

    def ranged(self, opened, endpoint, value):
        request = urllib.request.Request(opened["url"] + endpoint, headers={"Range": value})
        with DIRECT.open(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Range"), response.read()

    @contextmanager
    def browser_heartbeats(self, opened, interval, page_id=None):
        stop = threading.Event()
        failures = []
        beat = {"visible": True} if page_id is None else {"visible": True, "page_id": page_id}
        self.request(opened, "api/heartbeat", beat)

        def send_heartbeats():
            while not stop.wait(interval):
                try:
                    self.request(opened, "api/heartbeat", beat)
                except Exception as exc:
                    failures.append(exc)
                    return

        worker = threading.Thread(target=send_heartbeats, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=6)
            self.assertFalse(worker.is_alive(), "Fixture heartbeat request did not finish")
            self.assertEqual(failures, [], "Fixture lost its live browser connection during inspection")

    def assert_api_rejected(self, opened, endpoint, payload):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request(opened, endpoint, payload)
        error = failure.exception
        body = error.read().decode("utf-8")
        error.close()
        self.assertEqual(error.code, 400, body)

    def wait_reason(self, expected, code):
        result = self.cli("wait", check=False)
        self.assertEqual((result.stdout.strip(), result.returncode), (expected, code), result.stderr)
        review = json.loads((self.results / "review.json").read_text())
        self.assertEqual(review["status"], expected)
        self.assertEqual(review["done"], expected == "done")
        self.assertIsNotNone(review["finished_at"])
        return review

    def wait_for_exit(self, pid, timeout=5):
        deadline = time.monotonic() + timeout
        while process_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_exists(pid), f"process {pid} did not exit")

    def no_leaks(self, opened, marked_files=()):
        assert_process_stopped(self, opened)
        self.assertEqual(result_files(self.results), sorted(["review.json", *marked_files]))
        self.assertEqual((self.images / "first.png").read_bytes(), self.original)
        self.assertEqual((self.images / "untouched.png").read_bytes(), self.original)


class ReviewLifecycle(ReviewHarness):
    """Verify durable edits, terminal reasons, and owned resource cleanup."""

    def test_done_preserves_notes_and_untouched_control(self):
        opened = self.start()
        state = self.request(opened, "api/state")
        self.assertEqual(state["question"], "Which capture is correct?")
        self.request(
            opened, "api/save", {"index": 0, "notes": "Keep the blue light", "markup": [], "png": None, "revision": 1}
        )
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["notes"], "Keep the blue light")
        self.assertFalse((self.results / "untouched.marked.png").exists())
        self.no_leaks(opened)

    def test_close_flushes_before_debounce(self):
        opened = self.start()
        self.request(opened, "api/state")
        self.request(
            opened, "api/close", {"index": 1, "notes": "last keystroke", "markup": [], "png": None, "revision": 1}
        )
        review = self.wait_reason("closed", 10)
        self.assertEqual(review["images"][1]["notes"], "last keystroke")
        self.no_leaks(opened)

    def test_heartbeat_loss_returns_closed(self):
        # The close timeout outlasts wait's 1 s ping interval, so pings must not count as page activity.
        opened = self.start("--close-timeout", "2.5")
        self.request(opened, "api/state")
        self.request(opened, "api/heartbeat", {"visible": True})
        self.wait_reason("closed", 10)
        self.no_leaks(opened)

    def test_idle_without_browser_returns_timeout(self):
        opened = self.start("--idle-timeout", "0.5")
        self.wait_reason("timeout", 11)
        self.no_leaks(opened)

    def test_active_heartbeats_do_not_defeat_idle_timeout(self):
        opened = self.start("--idle-timeout", "2", "--close-timeout", "5")
        stop = threading.Event()
        delivered = []
        rejected = []

        def send_heartbeats():
            while not stop.is_set():
                try:
                    self.request(opened, "api/heartbeat", {"visible": True})
                except urllib.error.HTTPError as error:
                    rejected.append(error.code)
                    error.close()
                    return
                except (urllib.error.URLError, ConnectionError, http.client.HTTPException):
                    return  # The server stopped while this heartbeat was in flight.
                delivered.append(time.monotonic())
                stop.wait(0.1)

        worker = threading.Thread(target=send_heartbeats, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 6
            status = self.status()["status"]
            while status == "open" and time.monotonic() < deadline:
                time.sleep(0.1)
                status = self.status()["status"]
        finally:
            stop.set()
            worker.join(timeout=10)
        self.assertEqual(status, "timeout", "Heartbeats are not edits")
        self.assertEqual(rejected, [], "The server rejected a heartbeat")
        self.assertGreaterEqual(len(delivered), 5, "Heartbeat positive control")
        self.wait_reason("timeout", 11)
        self.no_leaks(opened)

    @unittest.skipIf(os.name == "nt", "a detached Windows server receives no signals")
    def test_signals_persist_interrupted_and_release_resources(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                opened = self.start()
                os.kill(opened["pid"], signum)
                self.wait_reason("interrupted", 12)
                self.no_leaks(opened)

    def test_shutdown_request_persists_interrupted_and_releases_resources(self):
        opened = self.start()
        self.assertEqual(self.request(opened, "api/shutdown", {}), {"status": "interrupted"})
        self.wait_reason("interrupted", 12)
        self.no_leaks(opened)

    def test_killed_server_recovers_stale_state_and_resumes_notes(self):
        first = self.start()
        self.request(
            first, "api/save", {"index": 0, "notes": "survives kill", "markup": [], "png": None, "revision": 1}
        )
        kill_hard(first["pid"])
        self.wait_for_exit(first["pid"])
        stale = json.loads((self.results / "runtime.json").read_text())
        self.assertEqual(stale["session_id"], first["session_id"], "The killed server left its runtime.json")
        self.assertFalse(ping_alive(stale))
        second = self.start()
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertFalse(second["reused"])
        state = self.request(second, "api/state")
        self.assertEqual(state["images"][0]["notes"], "survives kill")
        self.request(second, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(first)
        self.no_leaks(second)

    def test_wait_reports_killed_server_and_cleans_stale_metadata(self):
        opened = self.start()
        kill_hard(opened["pid"])
        self.wait_for_exit(opened["pid"])
        self.assertTrue((self.results / "runtime.json").is_file(), "The killed server left its runtime.json")
        began = time.monotonic()
        self.wait_reason("killed", 13)
        self.assertGreaterEqual(time.monotonic() - began, 1.9, "wait declares death after 3 pings about 1 s apart")
        self.no_leaks(opened)

    def test_duplicate_open_reuses_live_session_and_cleanup_is_idempotent(self):
        opened = self.start()
        self.assertEqual((opened["browser"], opened["reused"]), (False, False))
        duplicate = json.loads(self.cli("open").stdout)
        self.assertEqual(
            (duplicate["pid"], duplicate["session_id"], duplicate["url"]),
            (opened["pid"], opened["session_id"], opened["url"]),
        )
        self.assertEqual((duplicate["browser"], duplicate["reused"]), (False, True))
        self.assertEqual(self.status()["status"], "open")
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        self.wait_reason("interrupted", 12)
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        self.no_leaks(opened)

    def test_terminal_owner_teardown_serializes_reopen_and_preserves_younger_staging(self):
        gates = self.gates("terminal", "wait")
        opened = self.start(env=gates.env("terminal"))
        done, done_worker = self.request_in_background(opened, "api/done", {})
        owner = gates.wait_reached("terminal")
        self.assertEqual(owner, opened["pid"])
        self.assertEqual(self.status()["status"], "done")
        self.assertEqual(json.loads((self.results / "runtime.json").read_text())["pid"], owner)
        self.assertTrue(ping_alive(opened), "The terminal owner still serves until its teardown")
        self.assertTrue(done_worker.is_alive(), "The terminal API response waits for its owner")

        opener = self.spawn(open_args(self.images, self.results), env=gates.env("wait"))
        self.assertEqual(gates.wait_reached("wait"), opener.pid, "open waits for the live terminal owner")
        canary = self.results / f".review-{opener.pid}-younger-staging.tmp"
        canary.write_bytes(b"younger owner's staging control")
        self.assertIsNone(opener.poll())
        self.assertEqual(
            json.loads((self.results / "runtime.json").read_text())["pid"],
            owner,
            "open must not take over runtime.json from a live terminal owner",
        )
        self.assertEqual(self.status()["session_id"], opened["session_id"])

        gates.release("terminal")
        self.wait_for_exit(owner, timeout=15)
        done_worker.join(timeout=10)
        self.assertNotIn("error", done, str(done.get("error")))
        self.assertEqual(done["response"]["status"], "done")
        self.assertFalse((self.results / "runtime.json").exists(), "The owner removed its runtime.json")
        self.assertTrue(canary.is_file(), "Old teardown must not delete another process's staging file")

        gates.release("wait")
        stdout, stderr = opener.communicate(timeout=25)
        self.assertEqual(opener.returncode, 0, stderr + stdout)
        reopened = json.loads(stdout)
        self.assertEqual(reopened["status"], "open")
        self.assertNotEqual(reopened["session_id"], opened["session_id"])
        self.assertEqual(json.loads((self.results / "runtime.json").read_text())["pid"], reopened["pid"])
        self.assertFalse(canary.exists(), "Recovery with no live server removes every staging file")
        self.assertEqual(self.request(reopened, "api/state")["status"], "open")
        self.request(reopened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)
        self.no_leaks(reopened)

    def test_reopen_waits_until_the_owner_releases_runtime(self):
        gates = self.gates("terminal", "teardown", "wait")
        opened = self.start(env=gates.env("terminal", "teardown"))
        done, done_worker = self.request_in_background(opened, "api/done", {})
        self.assertEqual(gates.wait_reached("terminal"), opened["pid"])
        opener = self.spawn(open_args(self.images, self.results), env=gates.env("wait"))
        self.assertEqual(gates.wait_reached("wait"), opener.pid, "open waits for the live terminal owner")
        owner_runtime = (self.results / "runtime.json").read_bytes()
        gates.release("terminal")
        self.assertEqual(gates.wait_reached("teardown"), opened["pid"])
        self.assertFalse(ping_alive(opened), "The owner has stopped answering")
        gates.release("wait")
        time.sleep(1.0)
        self.assertIsNone(opener.poll(), "open must wait while the owner still holds runtime.json")
        self.assertEqual((self.results / "runtime.json").read_bytes(), owner_runtime)
        gates.release("teardown")
        stdout, stderr = opener.communicate(timeout=25)
        self.assertEqual(opener.returncode, 0, stderr + stdout)
        reopened = json.loads(stdout)
        self.assertNotEqual(reopened["session_id"], opened["session_id"])
        done_worker.join(timeout=10)
        self.assertNotIn("error", done, str(done.get("error")))
        self.request(reopened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)
        self.no_leaks(reopened)

    def test_reopen_recovers_an_owner_that_dies_while_open_waits(self):
        gates = self.gates("terminal", "wait")
        opened = self.start(env=gates.env("terminal"))
        _done, done_worker = self.request_in_background(opened, "api/done", {})
        self.assertEqual(gates.wait_reached("terminal"), opened["pid"])
        opener = self.spawn(open_args(self.images, self.results), env=gates.env("wait"))
        self.assertEqual(gates.wait_reached("wait"), opener.pid, "open waits for the live terminal owner")
        kill_hard(opened["pid"])
        self.wait_for_exit(opened["pid"])
        self.assertTrue((self.results / "runtime.json").is_file(), "The killed owner left runtime.json")
        began = time.monotonic()
        gates.release("wait")
        stdout, stderr = opener.communicate(timeout=25)
        self.assertEqual(opener.returncode, 0, stderr + stdout)
        self.assertGreaterEqual(time.monotonic() - began, 1.9, "open declares the owner dead after 3 failed pings")
        reopened = json.loads(stdout)
        self.assertNotEqual(reopened["session_id"], opened["session_id"])
        done_worker.join(timeout=10)
        self.request(reopened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)
        self.no_leaks(reopened)

    def test_cleanup_waits_for_terminal_owner_to_stop_before_releasing_runtime(self):
        gates = self.gates("terminal")
        opened = self.start(env=gates.env())
        cleanup = self.spawn([sys.executable, str(SCRIPT), "cleanup", "--results", str(self.results)])
        self.assertEqual(gates.wait_reached("terminal"), opened["pid"])
        self.assertEqual(self.status()["status"], "interrupted")
        self.assertTrue(ping_alive(opened), "The owner is paused after recording its status, before teardown")
        time.sleep(1.0)
        self.assertIsNone(cleanup.poll(), "cleanup must not return while the owner is still running")
        self.assertEqual(
            json.loads((self.results / "runtime.json").read_text())["pid"],
            opened["pid"],
            "cleanup must not release runtime.json while the owner is still running",
        )
        gates.release("terminal")
        stdout, stderr = cleanup.communicate(timeout=15)
        self.assertEqual(cleanup.returncode, 0, stderr + stdout)
        self.assertEqual(json.loads(stdout), {"status": "interrupted"})
        self.no_leaks(opened)
        reopened = self.start()
        self.assertNotEqual(reopened["session_id"], opened["session_id"])
        self.request(reopened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(reopened)

    def test_cleanup_reports_retained_ownership_when_the_owner_does_not_stop(self):
        gates = self.gates("terminal")
        opened = self.start(env=gates.env())
        began = time.monotonic()
        failure = self.cli("cleanup", check=False, timeout=40)
        self.assertEqual(gates.wait_reached("terminal", timeout=1), opened["pid"])
        self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
        self.assertIn("ownership was retained", json.loads(failure.stdout)["error"])
        self.assertGreaterEqual(time.monotonic() - began, 9.5, "cleanup waits 10 s for the owner to stop")
        self.assertTrue(ping_alive(opened))
        self.assertTrue(process_exists(opened["pid"]))
        self.assertEqual(json.loads((self.results / "runtime.json").read_text())["pid"], opened["pid"])
        gates.release("terminal")
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        self.no_leaks(opened)

    def test_wrong_origin_and_wrong_token_cannot_read_or_modify_private_review(self):
        opened = self.start()
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request(opened, "api/save", {"index": 0, "notes": "evil"}, origin="https://attacker.invalid")
        self.assertEqual(failure.exception.code, 403)
        failure.exception.close()
        wrong = dict(opened, url=opened["url"].rsplit("/", 2)[0] + "/wrong-token/")
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request(wrong, "api/state")
        self.assertEqual(failure.exception.code, 404)
        failure.exception.close()
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_older_save_cannot_overwrite_newer_notes(self):
        opened = self.start()
        for revision, notes in ((2, "latest"), (1, "stale")):
            self.request(
                opened, "api/save", {"index": 0, "notes": notes, "markup": [], "png": None, "revision": revision}
            )
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["notes"], "latest")
        self.no_leaks(opened)

    def test_close_delta_accepts_already_saved_prefix(self):
        opened = self.start()
        self.request(opened, "api/save", {"index": 0, "revision": 1, "notes": "draft", "markup": [], "png": None})
        snapshot = {
            "index": 0,
            "revision": 2,
            "png": None,
            "markup_delta": [],
            "notes_delta": [
                {"revision": 1, "start": 0, "delete": 0, "insert": "draft"},
                {"revision": 2, "start": 0, "delete": 5, "insert": "latest"},
            ],
        }
        try:
            self.request(opened, "api/close", {"snapshots": [snapshot]})
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            error.close()
            self.fail(f"Close must skip the already-durable prefix and save its new tail: HTTP {error.code}: {body}")
        review = self.wait_reason("closed", 10)
        self.assertEqual(
            review["images"][0]["notes"], "latest", "An in-flight save acknowledgement must not lose close edits"
        )
        self.no_leaks(opened)


class ReviewServerLifecycle(ReviewHarness):
    """Ping, shutdown, the close grace and the visible-time close timer."""

    def test_ping_answers_during_a_long_save(self):
        gates = self.gates("save")
        opened = self.start(env=gates.env())
        saved, worker = self.request_in_background(
            opened, "api/save", {"index": 0, "revision": 1, "notes": "slow save", "markup": [], "png": None}
        )
        self.assertEqual(gates.wait_reached("save"), opened["pid"])
        with self.assertRaises(OSError, msg="The paused save holds the state lock"):
            self.request(opened, "api/state", timeout=0.5)
        self.assertTrue(ping_alive(opened), "ping must not wait for the state lock")
        reused = json.loads(self.cli("open").stdout)
        self.assertEqual((reused["session_id"], reused["reused"]), (opened["session_id"], True))
        self.assertTrue(worker.is_alive(), "The save is still paused")
        gates.release("save")
        worker.join(timeout=10)
        self.assertNotIn("error", saved, str(saved.get("error")))
        self.assertTrue(saved["response"]["saved"])
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["notes"], "slow save")
        self.no_leaks(opened)

    def test_shutdown_answers_during_a_long_save(self):
        gates = self.gates("save")
        opened = self.start(env=gates.env())
        saved, worker = self.request_in_background(
            opened, "api/save", {"index": 0, "revision": 1, "notes": "kept through shutdown", "markup": [], "png": None}
        )
        self.assertEqual(gates.wait_reached("save"), opened["pid"])
        self.assertEqual(self.request(opened, "api/shutdown", {}, timeout=2), {"status": "interrupted"})
        self.assertTrue(worker.is_alive(), "The save is still paused")
        gates.release("save")
        worker.join(timeout=10)
        self.assertNotIn("error", saved, str(saved.get("error")))
        self.assertTrue(saved["response"]["saved"])
        review = self.wait_reason("interrupted", 12)
        self.assertEqual(review["images"][0]["notes"], "kept through shutdown")
        self.no_leaks(opened)

    def test_reload_within_close_grace_keeps_the_session(self):
        # With a close timeout above the 5 s grace, only the reload's request can keep the
        # session open past the grace.
        opened = self.start("--close-timeout", "9")
        state = self.request(opened, "api/state")
        self.request(opened, "api/close", {})
        self.request(opened, "api/state")
        time.sleep(5.6)
        self.assertEqual(self.status()["status"], "open", "A page load within the grace cancels the close")
        with self.browser_heartbeats(opened, state["heartbeat_interval_ms"] / 1000):
            time.sleep(4.5)
            self.assertEqual(self.status()["status"], "open", "The review continues after twice the grace")
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        self.wait_reason("interrupted", 12)
        self.no_leaks(opened)

    def test_reload_during_a_slow_close_still_cancels_it(self):
        # The close pauses while it applies its snapshot. A reload's api/state that arrives then
        # must take effect after the close, or the close would end the reloaded review.
        gates = self.gates("save")
        opened = self.start("--close-timeout", "9", env=gates.env())
        self.request(opened, "api/state")
        closing, close_worker = self.request_in_background(
            opened, "api/close", {"index": 0, "revision": 1, "notes": "last edit", "markup": [], "png": None}
        )
        self.assertEqual(gates.wait_reached("save"), opened["pid"])
        closed_at = time.monotonic()
        loading, load_worker = self.request_in_background(opened, "api/state", None)
        time.sleep(0.3)
        gates.release("save")
        close_worker.join(timeout=10)
        load_worker.join(timeout=10)
        self.assertNotIn("error", closing, str(closing.get("error")))
        self.assertEqual(loading["response"]["status"], "open")
        time.sleep(max(0.0, closed_at + 6.0 - time.monotonic()))
        self.assertEqual(self.status()["status"], "open", "The reload cancels the close that it overlapped")
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        review = self.wait_reason("interrupted", 12)
        self.assertEqual(review["images"][0]["notes"], "last edit")
        self.no_leaks(opened)

    def test_close_without_reload_ends_closed(self):
        # With a 30 s close timeout the grace is 5 s, so the end time shows which one ended it.
        opened = self.start("--close-timeout", "30")
        self.request(opened, "api/state")
        self.assertEqual(self.request(opened, "api/close", {})["status"], "closed")
        closed_at = time.monotonic()
        self.assertEqual(self.status()["status"], "open", "The close grace has not elapsed yet")
        self.wait_reason("closed", 10)
        self.assertGreaterEqual(time.monotonic() - closed_at, 4.5, "The review ends after the 5 s grace")
        self.assertLess(time.monotonic() - closed_at, 15, "The grace ends the review, not the close timeout")
        self.no_leaks(opened)

    def test_hidden_page_pauses_the_close_timer(self):
        opened = self.start()
        self.request(opened, "api/state")
        self.request(opened, "api/heartbeat", {"visible": False})
        time.sleep(1.5)
        self.assertEqual(self.status()["status"], "open", "Hidden time does not count toward the close timeout")
        self.request(opened, "api/heartbeat", {"visible": True})
        self.wait_reason("closed", 10)
        self.no_leaks(opened)

    def test_save_while_hidden_keeps_the_close_timer_paused(self):
        opened = self.start()
        self.request(opened, "api/state")
        self.request(opened, "api/heartbeat", {"visible": False})
        self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 1, "notes": "typed before switching tabs", "markup": [], "png": None},
        )
        time.sleep(1.5)
        self.assertEqual(self.status()["status"], "open", "A save must not make a hidden page visible")
        self.request(opened, "api/heartbeat", {"visible": True})
        review = self.wait_reason("closed", 10)
        self.assertEqual(review["images"][0]["notes"], "typed before switching tabs")
        self.no_leaks(opened)

    def test_hidden_heartbeat_after_close_does_not_cancel_the_grace(self):
        opened = self.start("--close-timeout", "1")
        self.request(opened, "api/state")
        self.request(opened, "api/close", {})
        self.request(opened, "api/heartbeat", {"visible": False})
        time.sleep(2.5)
        self.assertEqual(self.status()["status"], "closed", "A late heartbeat must not cancel the close grace")
        self.wait_reason("closed", 10)
        self.no_leaks(opened)

    def test_late_save_after_close_does_not_cancel_the_grace(self):
        # With a 30 s close timeout the grace is 5 s; a cancelled close would run for 30 s.
        opened = self.start("--close-timeout", "30")
        self.request(opened, "api/state")
        self.request(opened, "api/close", {})
        closed_at = time.monotonic()
        self.request(opened, "api/save", {"index": 0, "revision": 1, "notes": "late save", "markup": [], "png": None})
        review = self.wait_reason("closed", 10)
        self.assertLess(time.monotonic() - closed_at, 15, "A late save must not cancel the close grace")
        self.assertEqual(review["images"][0]["notes"], "late save")
        self.no_leaks(opened)

    def test_stale_page_close_saves_its_snapshot_but_keeps_the_review_open(self):
        # A reload's close beacon can arrive after the new page's api/state.
        opened = self.start("--close-timeout", "2")
        self.request(opened, "api/state?page_id=old-page")
        state = self.request(opened, "api/state?page_id=new-page")
        snapshot = {"index": 0, "revision": 1, "notes": "typed before the reload", "markup": [], "png": None}
        closing = self.request(opened, "api/close", {"page_id": "old-page", "snapshots": [snapshot]})
        with self.browser_heartbeats(opened, state["heartbeat_interval_ms"] / 1000, page_id="new-page"):
            time.sleep(4.5)
            review = self.status()
            self.assertEqual(review["status"], "open", "A stale page's close must not start the 2 s grace")
        self.assertEqual(review["images"][0]["notes"], "typed before the reload", "A stale close still saves")
        self.assertEqual(closing["status"], "open", "The close response reports that the review continues")
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        self.wait_reason("interrupted", 12)
        self.no_leaks(opened)

    def test_stale_page_hidden_heartbeat_does_not_pause_the_close_timer(self):
        opened = self.start()
        self.request(opened, "api/state?page_id=old-page")
        self.request(opened, "api/state?page_id=new-page")
        self.request(opened, "api/heartbeat", {"visible": False, "page_id": "old-page"})
        time.sleep(1.5)
        self.assertEqual(self.status()["status"], "closed", "A stale page's hidden heartbeat paused the close timer")
        self.wait_reason("closed", 10)
        self.no_leaks(opened)

    def test_close_whose_snapshot_fails_still_starts_the_grace(self):
        # Hidden, the page has paused the close timer, so only the grace can end the review.
        opened = self.start("--close-timeout", "2")
        self.request(opened, "api/state")
        self.request(opened, "api/heartbeat", {"visible": False})
        (self.images / "first.png").write_bytes(fixture_png(32, 24))
        snapshot = {"index": 0, "revision": 1, "notes": "edit for the replaced source", "markup": [], "png": None}
        assert_source_change_error(self, opened, "api/close", {"snapshots": [snapshot]})
        time.sleep(3.5)
        self.assertEqual(self.status()["status"], "closed", "A close whose snapshot fails must still start the grace")
        review = self.wait_reason("closed", 10)
        self.assertEqual(review["images"][0]["notes"], "")
        assert_process_stopped(self, opened)
        self.assertEqual(result_files(self.results), ["review.json"])

    def test_cleanup_interrupts_a_live_session(self):
        opened = self.start()
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        self.assertFalse((self.results / "runtime.json").exists(), "cleanup returns after the server released it")
        self.assertFalse(ping_alive(opened), "cleanup returns after the server stopped answering")
        self.wait_reason("interrupted", 12)
        self.no_leaks(opened)

    def test_host_header_must_be_the_loopback_address(self):
        opened = self.start()
        port = urllib.parse.urlsplit(opened["url"]).port

        def state_status(host):
            request = urllib.request.Request(opened["url"] + "api/state", headers={"Host": host})
            with DIRECT.open(request, timeout=5) as response:
                return response.status

        self.assertEqual(state_status(f"127.0.0.1:{port}"), 200, "Positive control with the same token")
        with self.assertRaises(urllib.error.HTTPError) as failure:
            state_status(f"localhost:{port}")
        self.assertEqual(failure.exception.code, 403)
        failure.exception.close()
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_responses_name_the_visual_review_server(self):
        opened = self.start()
        with DIRECT.open(opened["url"] + "api/ping", timeout=5) as response:
            server = response.headers.get("Server", "")
        self.assertRegex(server, r"^visual-review/")
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)


class ReviewBrowserLaunch(ReviewHarness):
    """`open` reports whether it opened a tab; a stub stands in for the browser."""

    def browser_stub(self):
        stub = self.root / "browser-stub.py"
        stub.write_text(STUB, encoding="utf-8")
        record = self.root / "browser-argv.txt"
        command = f'"{sys.executable}" "{stub}" "{record}" %s'
        return dict(os.environ, VISUAL_REVIEW_BROWSER=command), record

    def test_open_reports_browser_true_with_custom_command(self):
        environment, record = self.browser_stub()
        opened = self.start(browser=True, env=environment)
        self.assertEqual((opened["browser"], opened["reused"]), (True, False))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (record.exists() and record.stat().st_size):
            time.sleep(0.05)
        self.assertEqual(record.read_text(encoding="utf-8"), opened["url"])
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_open_reports_browser_false_without_a_gui_browser(self):
        hidden = ("VISUAL_REVIEW_BROWSER", "DISPLAY", "WAYLAND_DISPLAY")
        environment = {name: value for name, value in os.environ.items() if name not in hidden}
        environment["BROWSER"] = "lynx"
        marker = self.root / "lynx-ran"
        if os.name != "nt":
            bin_dir = self.root / "bin"
            bin_dir.mkdir()
            lynx = bin_dir / "lynx"
            lynx.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\n", encoding="utf-8")
            lynx.chmod(0o755)
            environment["PATH"] = str(bin_dir) + os.pathsep + environment.get("PATH", "")
        opened = self.start(browser=True, env=environment)
        self.assertEqual((opened["browser"], opened["reused"]), (False, False))
        self.assertFalse(marker.exists(), "A console browser must not be started")
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_second_open_reuses_the_session(self):
        opened = self.start()
        environment, record = self.browser_stub()
        second = json.loads(self.cli("open", browser=True, env=environment).stdout)
        self.assertEqual(
            (second["status"], second["session_id"], second["url"], second["pid"]),
            ("open", opened["session_id"], opened["url"], opened["pid"]),
        )
        self.assertEqual((second["reused"], second["browser"]), (True, False))
        time.sleep(1.0)
        self.assertFalse(record.exists(), "A reused session must not open another tab")
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)


class ReviewOutputEncoding(ReviewHarness):
    """Commands print UTF-8 whatever encoding the locale gives their standard streams."""

    def test_commands_round_trip_non_ascii_text_under_an_ansi_code_page(self):
        # Python on Windows writes piped output in the ANSI code page; PYTHONIOENCODING selects one anywhere.
        ansi = dict(os.environ, PYTHONIOENCODING="cp1252")
        notes = "Move the badge \u2192 left \U0001f642"
        label = "\u2192 here \U0001f642"
        self.results = self.root / "results \u2192"

        broken = self.images / "broken \u2192 \U0001f642.png"
        broken.write_bytes(b"not a PNG")
        failure = self.cli("open", check=False, env=ansi)
        self.assertEqual(failure.returncode, 2, failure.stderr + failure.stdout)
        self.assertIn(broken.name, json.loads(failure.stdout)["error"])
        broken.unlink()

        opened = json.loads(self.cli("open", *LONG_CLOSE, env=ansi).stdout)
        self.assertEqual(opened["results"], str(self.results.resolve()))
        self.prepare(opened)
        mask = {"width": 2, "height": 1, "scale": 1, "png": data_url(fixture_png(2, 1))}
        mark = {"type": "text", "color": "#2563eb", "x": 5, "y": 6, "size": 24, "text": label, "mask": mask}
        self.request(opened, "api/save", {"index": 0, "revision": 1, "notes": notes, "markup": [mark], "png": None})
        for command in ("status", "results"):
            with self.subTest(command=command):
                item = json.loads(self.cli(command, env=ansi).stdout)["images"][0]
                self.assertEqual((item["notes"], item["markup"][0]["text"]), (notes, label))
        self.assertEqual(json.loads(self.cli("cleanup", env=ansi).stdout), {"status": "interrupted"})
        waited = self.cli("wait", check=False, env=ansi)
        self.assertEqual((waited.stdout.strip(), waited.returncode), ("interrupted", 12), waited.stderr)
        self.no_leaks(opened)

    def test_open_prints_a_results_path_that_is_not_valid_utf8(self):
        try:
            name = os.fsdecode(b"results-\xff")
            (self.root / name).mkdir()
        except (OSError, UnicodeError):
            self.skipTest("this platform needs file names that are valid UTF-8")
        self.results = self.root / name
        opened = json.loads(self.cli("open").stdout)
        self.assertEqual(opened["results"], str(self.results.resolve()))
        self.assertEqual(json.loads(self.cli("cleanup").stdout), {"status": "interrupted"})
        assert_process_stopped(self, opened)


class ReviewAdmission(ReviewHarness):
    """Verify request boundaries and durable annotation transactions."""

    def test_sparse_safe_integer_delta_rejects_without_durable_mutation(self):
        opened = self.start(memory_limited=True)
        self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 1, "notes": "durable baseline", "markup": [], "png": None},
        )
        before = self.request(opened, "api/state")["images"][0]
        self.assertEqual((before["revision"], before["notes"], before["markup"]), (1, "durable baseline", []))
        persisted = json.loads((self.results / "review.json").read_text())["images"][0]
        self.assertEqual((persisted["notes"], persisted["markup"]), ("durable baseline", []))
        durable_review = (self.results / "review.json").read_bytes()

        self.assert_api_rejected(
            opened,
            "api/save",
            {
                "index": 0,
                "revision": 2**53 - 1,
                "notes_delta": [{"revision": 2, "start": 0, "delete": 0, "insert": "hostile"}],
                "markup_delta": [],
                "png": None,
            },
        )
        after = self.request(opened, "api/state")["images"][0]
        self.assertEqual((after["revision"], after["notes"], after["markup"]), (1, "durable baseline", []))
        self.assertEqual((self.results / "review.json").read_bytes(), durable_review)
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["notes"], "durable baseline")
        self.no_leaks(opened)

    def test_markup_delta_enforces_merged_aggregate_mark_limit(self):
        opened = self.start(*LONG_CLOSE)
        self.prepare(opened)
        mark = {"type": "pen", "color": "#ef4444", "width": 1, "points": []}
        initial_markup = [mark] * 19_999
        first = self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 1, "notes": "mark boundary", "markup": initial_markup, "png": None},
        )
        self.assertEqual(first["revision"], 1)
        accepted = self.request(
            opened,
            "api/save",
            {
                "index": 0,
                "revision": 2,
                "notes_delta": [],
                "markup_delta": [{"revision": 2, "op": "append", "mark": mark}],
                "png": None,
            },
        )
        self.assertEqual(accepted["revision"], 2)
        boundary_state = self.request(opened, "api/state")["images"][0]
        self.assertEqual(boundary_state["revision"], 2)
        self.assertEqual(len(boundary_state["markup"]), 20_000)
        persisted = json.loads((self.results / "review.json").read_text())["images"][0]
        self.assertEqual(len(persisted["markup"]), 20_000)
        durable_review = (self.results / "review.json").read_bytes()

        self.assert_api_rejected(
            opened,
            "api/save",
            {
                "index": 0,
                "revision": 3,
                "notes_delta": [],
                "markup_delta": [{"revision": 3, "op": "append", "mark": mark}],
                "png": None,
            },
        )
        after = self.request(opened, "api/state")["images"][0]
        self.assertEqual(after["revision"], 2)
        self.assertEqual(len(after["markup"]), 20_000)
        self.assertEqual(after["markup"][-1], mark)
        self.assertEqual((self.results / "review.json").read_bytes(), durable_review)
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(len(review["images"][0]["markup"]), 20_000)
        self.assertTrue(review["images"][0]["png_stale"], "No marked PNG was uploaded for the saved marks")
        self.no_leaks(opened)

    def test_markup_over_32_mib_is_accepted(self):
        opened = self.start(*LONG_CLOSE)
        self.prepare(opened)
        tiny_png = fixture_png(2, 1)
        metadata = b"Comment\x00" + b"a" * (25 * 1024 * 1024)
        ancillary = (
            struct.pack("!I", len(metadata)) + b"tEXt" + metadata + struct.pack("!I", zlib.crc32(b"tEXt" + metadata))
        )
        mask_png = tiny_png[:-12] + ancillary + tiny_png[-12:]
        mask = {
            "width": 2,
            "height": 1,
            "scale": 1,
            "png": "data:image/png;base64," + base64.b64encode(mask_png).decode("ascii"),
        }
        mark = {"type": "text", "color": "#ef4444", "x": 0, "y": 0, "size": 18, "text": "a", "mask": mask}
        markup_json_size = len(json.dumps([mark], separators=(",", ":")).encode())
        self.assertGreater(markup_json_size, 32 * 1024 * 1024)

        saved = self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 1, "notes": "large legal markup", "markup": [mark], "png": None},
        )
        self.assertTrue(saved["saved"])
        persisted = (self.results / "review.json").read_bytes()
        self.assertGreater(len(persisted), 32 * 1024 * 1024)
        durable = json.loads(persisted)["images"][0]
        self.assertEqual(durable["markup"][0]["text"], "a")
        self.assertEqual(durable["markup"][0]["mask"]["scale"], 1)
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["notes"], "large legal markup")
        self.no_leaks(opened)

    def test_text_semantic_limits_and_original_pixel_sizes(self):
        opened = self.start()
        self.prepare(opened)
        mask = {"width": 2, "height": 1, "scale": 1, "png": data_url(fixture_png(2, 1))}

        def text_mark(text, size):
            return {"type": "text", "color": "#ef4444", "x": 0, "y": 0, "size": size, "text": text, "mask": mask}

        max_codepoints = "\U0001f642" * 240
        accepted = self.request(
            opened,
            "api/save",
            {
                "index": 0,
                "revision": 1,
                "notes": "text limits",
                "markup": [text_mark(max_codepoints, 18)],
                "png": None,
            },
        )
        self.assertEqual(accepted["revision"], 1)
        first_state = self.request(opened, "api/state")["images"][0]
        self.assertEqual(first_state["markup"][0]["text"], max_codepoints)
        self.assertEqual(first_state["markup"][0]["size"], 18)

        eight_lines = "row\n" * 7 + "end"
        wide_pen = {"type": "pen", "color": "#ef4444", "width": 900, "points": []}
        accepted = self.request(
            opened,
            "api/save",
            {
                "index": 0,
                "revision": 2,
                "notes": "text limits",
                "markup": [text_mark(eight_lines, 480), wide_pen],
                "png": None,
            },
        )
        self.assertEqual(accepted["revision"], 2)
        boundary_state = self.request(opened, "api/state")["images"][0]
        self.assertEqual(boundary_state["markup"][0]["text"], eight_lines)
        self.assertEqual(boundary_state["markup"][0]["size"], 480)
        self.assertEqual(boundary_state["markup"][1]["width"], 900)
        persisted_boundary = json.loads((self.results / "review.json").read_text())["images"][0]
        self.assertEqual(persisted_boundary["markup"], boundary_state["markup"])
        durable_review = (self.results / "review.json").read_bytes()

        rejected_marks = (
            (text_mark(max_codepoints + "\U0001f642", 18), "codepoint limit"),
            (text_mark(eight_lines + "\nlast", 480), "line limit"),
            (text_mark("zero size", 0), "non-positive text size"),
            ({"type": "pen", "color": "#ef4444", "width": 0, "points": []}, "non-positive stroke width"),
        )
        for mark, boundary in rejected_marks:
            with self.subTest(boundary=boundary):
                self.assert_api_rejected(
                    opened,
                    "api/save",
                    {"index": 0, "revision": 3, "notes": "must not persist", "markup": [mark], "png": None},
                )
                self.assertEqual((self.results / "review.json").read_bytes(), durable_review)
                after = self.request(opened, "api/state")["images"][0]
                self.assertEqual(after["revision"], 2)
                self.assertEqual(after["notes"], "text limits")
                self.assertEqual(after["markup"], boundary_state["markup"])

        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["markup"][0]["text"], eight_lines)
        self.assertEqual(review["images"][0]["markup"][0]["size"], 480)
        self.assertEqual(review["images"][0]["markup"][1]["width"], 900)
        self.no_leaks(opened)

    def test_resume_same_source_preserves_exact_saved_notes_markup_and_png(self):
        opened = self.start()
        pen = {
            "type": "pen",
            "color": "#ef4444",
            "width": 3,
            "group": "source-bound-mark",
            "points": [[10, 10], [40, 10]],
        }
        notes = "This verdict belongs to the original white capture."
        self.prepare(opened)
        self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 1, "notes": notes, "markup": [pen], "png": None},
        )
        self.upload(opened, 0, 1, BASELINE_MARKED)
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        entry = review["images"][0]
        self.assertEqual(entry.get("source_sha256"), hashlib.sha256(self.original).hexdigest())
        self.assertEqual((entry["notes"], entry["markup"]), (notes, [pen]))
        self.assertEqual(
            (entry["marked"], entry["markup_revision"], entry["png_revision"], entry["png_stale"]),
            ("first.marked.png", 1, 1, False),
        )
        self.no_leaks(opened, marked_files=("first.marked.png",))
        marked_path = self.results / "first.marked.png"
        marked_before = marked_path.read_bytes()
        self.assertEqual(marked_before, with_revision_chunk(BASELINE_MARKED, 1))

        resumed = self.start()
        state = self.request(resumed, "api/state")["images"][0]
        self.assertEqual((state["notes"], state["markup"], state["png_stale"]), (notes, [pen], False))
        code, body = self.upload_error(resumed, 0, 1, BASELINE_MARKED)
        self.assertEqual(code, 400, body)
        self.assertIn("prepare", body["error"], "A display size from an earlier session does not carry over")
        prepared = self.prepare(resumed)
        self.assertEqual((prepared["revision"], prepared["png_revision"], prepared["png_stale"]), (1, 1, False))
        self.assertEqual(marked_path.read_bytes(), marked_before)
        self.assertEqual((self.images / "first.png").read_bytes(), self.original)
        self.request(resumed, "api/done", {})
        resumed_review = self.wait_reason("done", 0)
        self.assertEqual(resumed_review["images"][0]["notes"], notes)
        self.assertEqual(resumed_review["images"][0]["markup"], [pen])
        self.assertFalse(resumed_review["images"][0]["png_stale"])
        self.assertEqual(marked_path.read_bytes(), marked_before)
        self.no_leaks(resumed, marked_files=("first.marked.png",))

    def test_reopen_rejects_changed_source_before_mutating_verdict_or_marked_png(self):
        opened = self.start()
        pen = {
            "type": "pen",
            "color": "#ef4444",
            "width": 3,
            "group": "original-capture-mark",
            "points": [[10, 10], [40, 10]],
        }
        notes = "White capture: keep the first pose."
        self.prepare(opened)
        self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 1, "notes": notes, "markup": [pen], "png": None},
        )
        self.upload(opened, 0, 1, BASELINE_MARKED)
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertEqual(review["images"][0]["notes"], notes)
        self.assertEqual(review["images"][0]["markup"], [pen])
        self.no_leaks(opened, marked_files=("first.marked.png",))

        review_path = self.results / "review.json"
        marked_path = self.results / "first.marked.png"
        review_before = review_path.read_bytes()
        marked_before = marked_path.read_bytes()
        self.assertEqual(marked_before, with_revision_chunk(BASELINE_MARKED, 1))
        changed_source = fixture_png(80, 36, (35, 105, 220))
        source_path = self.images / "first.png"
        source_path.write_bytes(changed_source)

        failure = self.cli("open", check=False)
        self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
        error = json.loads(failure.stdout)["error"]
        self.assertIn("first.png", error)
        self.assertEqual(review_path.read_bytes(), review_before)
        self.assertEqual(marked_path.read_bytes(), marked_before)
        self.assertEqual(source_path.read_bytes(), changed_source)
        self.assertEqual(result_files(self.results), ["first.marked.png", "review.json"])

    def test_populated_entry_without_source_identity_fails_closed(self):
        opened = self.start()
        save_marked_baseline(self, opened)
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened, marked_files=("first.marked.png",))

        review_path = self.results / "review.json"
        unbound = json.loads(review_path.read_text())
        unbound["images"][0].pop("source_sha256", None)
        review_path.write_text(json.dumps(unbound))
        review_before = review_path.read_bytes()
        marked_path = self.results / "first.marked.png"
        marked_before = marked_path.read_bytes()
        failure = self.cli("open", check=False)

        self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
        error = json.loads(failure.stdout)["error"]
        self.assertIn("first.png", error)
        self.assertIn("source identity", error)
        self.assertEqual(review_path.read_bytes(), review_before)
        self.assertEqual(marked_path.read_bytes(), marked_before)
        self.assertEqual(result_files(self.results), ["first.marked.png", "review.json"])

    def test_snapshot_rejects_uploaded_png_and_accepts_null(self):
        opened = self.start()
        uploaded_png = "data:image/png;base64," + base64.b64encode(fixture_png(1, 1)).decode("ascii")
        durable_review = (self.results / "review.json").read_bytes()
        rejected = {
            "index": 0,
            "revision": 1,
            "notes": "untrusted upload",
            "markup": [],
            "png": uploaded_png,
        }
        self.assert_api_rejected(
            opened,
            "api/close",
            {"snapshots": [rejected]},
        )
        state = self.request(opened, "api/state")
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["images"][0]["revision"], 0)
        self.assertEqual(state["images"][0]["notes"], "")
        self.assertEqual((self.results / "review.json").read_bytes(), durable_review)

        accepted = {**rejected, "notes": "server-owned output", "png": None}
        response = self.request(opened, "api/close", {"snapshots": [accepted]})
        self.assertEqual(response["status"], "closed")
        review = self.wait_reason("closed", 10)
        self.assertEqual(review["images"][0]["notes"], "server-owned output")
        self.assertIsNone(review["images"][0]["marked"])
        self.no_leaks(opened)

    def test_same_and_symlinked_results_directories_reject_before_image_mutation(self):
        alias = self.root / "images-alias"
        try:
            alias.symlink_to(self.images, target_is_directory=True)
        except (OSError, NotImplementedError):
            alias = None

        def image_signature():
            directory = self.images.stat()
            entries = []
            for path in sorted(self.images.iterdir()):
                info = path.stat()
                entries.append((path.name, info.st_mode, info.st_mtime_ns, info.st_ctime_ns, path.read_bytes()))
            return (directory.st_mode, directory.st_mtime_ns, directory.st_ctime_ns, tuple(entries))

        before = image_signature()
        for results_path in (self.images, alias):
            with self.subTest(results=results_path):
                if results_path is None:
                    self.skipTest("this account cannot create directory symlinks")
                failure = self.cli("open", check=False, results=results_path)
                self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
                self.assertEqual(image_signature(), before)
                self.assertFalse(self.results.exists())

    def test_missing_png_is_flagged_without_live_reader_overwriting_review(self):
        opened = self.start(*LONG_CLOSE)
        pen = {"type": "pen", "color": "#ef4444", "width": 3, "group": "pen-control", "points": [[10, 10], [40, 10]]}
        self.prepare(opened)
        self.request(
            opened, "api/save", {"index": 0, "revision": 1, "notes": "Keep this mark", "markup": [pen], "png": None}
        )
        self.upload(opened, 0, 1, BASELINE_MARKED)
        marked = self.results / "first.marked.png"
        self.assertTrue(marked.is_file(), "PNG freshness probe positive control")
        stable_review = (self.results / "review.json").read_bytes()
        self.assertFalse(json.loads(stable_review)["images"][0]["png_stale"], "Positive control: the upload is fresh")
        marked.unlink()
        reported = json.loads(self.cli("results").stdout)
        self.assertTrue(reported["images"][0]["png_stale"], "A missing marked artifact must not report fresh")
        self.assertEqual(reported["images"][0]["notes"], "Keep this mark")
        self.assertEqual(
            (self.results / "review.json").read_bytes(),
            stable_review,
            "Live result readers must not overwrite the daemon's review snapshot",
        )
        self.request(opened, "api/done", {})
        review = self.wait_reason("done", 0)
        self.assertTrue(review["images"][0]["png_stale"], "Terminal wait must persist detected PNG staleness")
        self.no_leaks(opened)

    def crash_during_first_upload(self):
        """Kill the server after it replaced first.marked.png and before review.json names that PNG."""
        gates = self.gates("marked")
        opened = self.start(*LONG_CLOSE, env=gates.env())
        marked = self.results / "first.marked.png"
        pen = {
            "type": "pen",
            "color": "#ef4444",
            "width": 3,
            "group": "first-mark",
            "points": [[10, 10], [40, 10]],
        }
        self.prepare(opened)
        self.request(
            opened, "api/save", {"index": 0, "revision": 1, "notes": "first mark", "markup": [pen], "png": None}
        )
        committed = (self.results / "review.json").read_bytes()
        entry = json.loads(committed)["images"][0]
        self.assertEqual((entry["markup"], entry["marked"], entry["png_stale"]), ([pen], None, True))
        result, worker = self.in_background(lambda: self.upload(opened, 0, 1, BASELINE_MARKED, timeout=30))
        gated_pid = gates.wait_reached("marked")
        try:
            self.assertEqual(gated_pid, opened["pid"])
            self.assertTrue(worker.is_alive(), "The PNG write must precede the review.json update")
            self.assertEqual(marked.read_bytes(), with_revision_chunk(BASELINE_MARKED, 1))
            self.assertEqual((self.results / "review.json").read_bytes(), committed)

            live = json.loads(self.cli("results").stdout)
            self.assertEqual(live["status"], "open")
            self.assertTrue(live["images"][0]["png_stale"], "review.json does not name the PNG yet")
            self.assertTrue(marked.is_file(), "A live result reader must preserve the in-flight PNG")
            self.assertEqual((self.results / "review.json").read_bytes(), committed)
        finally:
            kill_hard(opened["pid"])

        self.wait_for_exit(opened["pid"])
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "The upload remained blocked after the server was killed")
        self.assertIn("error", result, "Killing the server should interrupt the gated upload")
        self.assertEqual((self.results / "review.json").read_bytes(), committed)
        return opened, marked, pen

    def test_upload_crash_results_recovery_keeps_durable_markup_and_removes_orphans(self):
        crashed, marked, pen = self.crash_during_first_upload()
        orphan = self.results / "untouched.marked.png"
        orphan.write_bytes(BASELINE_MARKED)

        recovered = json.loads(self.cli("results").stdout)
        self.assertEqual(recovered["status"], "killed")
        entry = recovered["images"][0]
        self.assertEqual((entry["markup"], entry["marked"], entry["png_stale"]), ([pen], None, True))
        self.assertEqual(json.loads((self.results / "review.json").read_text())["images"][0], entry)
        self.assertFalse(orphan.exists(), "Recovery removes the marked PNG of an image without durable markup")
        self.assertTrue(marked.is_file(), "The markup of first.png is durable, so its PNG is not an orphan")
        self.no_leaks(crashed, marked_files=("first.marked.png",))

        reopened = self.start()
        prepared = self.prepare(reopened)
        self.assertEqual((prepared["revision"], prepared["png_revision"], prepared["png_stale"]), (1, None, True))
        self.upload(reopened, 0, 1, BASELINE_MARKED)
        self.request(reopened, "api/done", {})
        entry = self.wait_reason("done", 0)["images"][0]
        self.assertEqual(
            (entry["markup"], entry["marked"], entry["png_revision"], entry["png_stale"]),
            ([pen], "first.marked.png", 1, False),
        )
        self.assertEqual(marked.read_bytes(), with_revision_chunk(BASELINE_MARKED, 1))
        self.no_leaks(reopened, marked_files=("first.marked.png",))

    def test_upload_crash_cleanup_keeps_durable_markup_and_removes_orphans(self):
        crashed, marked, pen = self.crash_during_first_upload()
        orphan = self.results / "untouched.marked.png"
        orphan.write_bytes(BASELINE_MARKED)

        cleanup = json.loads(self.cli("cleanup").stdout)
        self.assertEqual(cleanup["status"], "killed")
        entry = self.status()["images"][0]
        self.assertEqual((entry["markup"], entry["marked"], entry["png_stale"]), ([pen], None, True))
        self.assertFalse(orphan.exists(), "Recovery removes the marked PNG of an image without durable markup")
        self.assertTrue(marked.is_file(), "The markup of first.png is durable, so its PNG is not an orphan")
        self.no_leaks(crashed, marked_files=("first.marked.png",))

    def test_resumed_markup_revision_collision_recovers_after_png_replace_crash(self):
        first = self.start()
        pen_a = {
            "type": "pen",
            "color": "#ef4444",
            "width": 3,
            "group": "pen-a",
            "points": [[10, 10], [40, 10]],
        }
        pen_b = {
            "type": "pen",
            "color": "#2563eb",
            "width": 3,
            "group": "pen-b",
            "points": [[10, 30], [40, 30]],
        }
        png_a = BASELINE_MARKED
        png_b = fixture_png(64, 48, (37, 99, 235))
        marked = self.results / "first.marked.png"
        self.prepare(first)
        self.request(
            first,
            "api/save",
            {"index": 0, "revision": 1, "notes": "durable pen A", "markup": [pen_a], "png": None},
        )
        self.upload(first, 0, 1, png_a)
        self.request(first, "api/done", {})
        first_review = self.wait_reason("done", 0)
        self.assertEqual(first_review["images"][0]["markup_revision"], 1)
        self.assertEqual(marked.read_bytes(), with_revision_chunk(png_a, 1))
        self.no_leaks(first, marked_files=("first.marked.png",))

        gates = self.gates("marked")
        resumed = self.start(*LONG_CLOSE, env=gates.env())
        prepared = self.prepare(resumed)
        self.assertEqual((prepared["revision"], prepared["png_revision"], prepared["png_stale"]), (1, 1, False))
        pen_b_save = {"index": 0, "revision": 1, "notes": "uncommitted pen B", "markup": [pen_b], "png": None}
        collision = self.request(resumed, "api/save", pen_b_save)
        self.assertTrue(collision["ignored"], "A resumed page's colliding revision must not replace pen A")
        self.request(resumed, "api/save", {**pen_b_save, "revision": 2})
        durable = self.status()["images"][0]
        self.assertEqual(
            (durable["markup"], durable["markup_revision"], durable["png_revision"], durable["png_stale"]),
            ([pen_b], 2, 1, True),
        )
        code, body = self.upload_error(resumed, 0, 1, png_a)
        self.assertEqual(code, 409, body)
        self.assertEqual(marked.read_bytes(), with_revision_chunk(png_a, 1), "A stale upload must not replace the PNG")

        result, worker = self.in_background(lambda: self.upload(resumed, 0, 2, png_b, timeout=30))
        gated_pid = gates.wait_reached("marked")
        try:
            self.assertEqual(gated_pid, resumed["pid"])
            self.assertTrue(worker.is_alive(), "The PNG replacement must precede the review.json update")
            self.assertEqual(marked.read_bytes(), with_revision_chunk(png_b, 2))
        finally:
            kill_hard(resumed["pid"])
        self.wait_for_exit(resumed["pid"])
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "The upload remained blocked after the server was killed")
        self.assertIn("error", result, "Killing the server should interrupt the gated upload")
        durable = json.loads((self.results / "review.json").read_text())["images"][0]
        self.assertEqual((durable["markup"], durable["markup_revision"], durable["png_revision"]), ([pen_b], 2, 1))

        recovered = json.loads(self.cli("results").stdout)
        self.assertTrue(recovered["images"][0]["png_stale"], "review.json never recorded the replacement")
        reopened = self.start()
        prepared = self.prepare(reopened)
        self.assertEqual((prepared["revision"], prepared["png_revision"], prepared["png_stale"]), (2, 1, True))
        self.upload(reopened, 0, 2, png_b)
        self.request(reopened, "api/done", {})
        repaired = self.wait_reason("done", 0)["images"][0]
        self.assertEqual(
            (repaired["markup"], repaired["markup_revision"], repaired["png_revision"], repaired["png_stale"]),
            ([pen_b], 2, 2, False),
        )
        self.assertEqual(marked.read_bytes(), with_revision_chunk(png_b, 2))
        self.no_leaks(resumed, marked_files=("first.marked.png",))
        self.no_leaks(reopened, marked_files=("first.marked.png",))


class ReviewTerminalOwnerAdmission(ReviewHarness):
    """Exercise source identity after a real terminal owner releases its process."""

    def test_reopen_rechecks_same_dimension_source_after_terminal_owner_wait(self):
        gates = self.gates("terminal", "wait")
        opened = self.start(env=gates.env("terminal"))
        notes, pen = save_marked_baseline(self, opened)
        completion, worker = self.request_in_background(opened, "api/done", {})
        owner_pid = gates.wait_reached("terminal")
        self.assertEqual(owner_pid, opened["pid"])
        terminal_review = json.loads((self.results / "review.json").read_text())
        self.assertEqual(terminal_review["status"], "done")
        self.assertEqual(terminal_review["images"][0]["notes"], notes)
        self.assertEqual(terminal_review["images"][0]["markup"], [pen])

        opener = self.spawn(open_args(self.images, self.results), env=gates.env("wait"))
        self.assertEqual(gates.wait_reached("wait"), opener.pid)
        self.assertTrue(ping_alive(opened), "The terminal owner is still alive while open waits")
        review_path = self.results / "review.json"
        marked_path = self.results / "first.marked.png"
        review_before = review_path.read_bytes()
        marked_before = marked_path.read_bytes()
        blue_source = fixture_png(64, 48, (35, 105, 220))
        source_path = self.images / "first.png"
        self.assertEqual(png_dimensions(blue_source), png_dimensions(self.original), "Same size, other content")
        self.assertNotEqual(blue_source, self.original)
        source_path.write_bytes(blue_source)
        gates.release("wait")
        gates.release("terminal")
        stdout, stderr = opener.communicate(timeout=25)
        if opener.returncode == 0:
            replacement = json.loads(stdout)
            self.cli("cleanup")
            assert_process_stopped(self, replacement)
        self.assertEqual(opener.returncode, 2, stdout + stderr)
        error = json.loads(stdout)["error"]
        self.assertIn("first.png", error)
        self.assertEqual(review_path.read_bytes(), review_before)
        self.assertEqual(marked_path.read_bytes(), marked_before)
        self.assertEqual(source_path.read_bytes(), blue_source)
        self.assertEqual(result_files(self.results), ["first.marked.png", "review.json"])
        self.assertEqual(marked_before, with_revision_chunk(BASELINE_MARKED, 1))
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "Terminal save request did not complete after releasing its owner")
        self.assertEqual(completion.get("response"), {"status": "done", "revisions": []})
        assert_process_stopped(self, opened)


class ReviewArtifactBindingAdmission(ReviewHarness):
    """Protect marked files that have no matching durable image record."""

    def test_unbound_marked_png_fails_closed_and_artifact_free_results_open(self):
        opened = self.start()
        save_marked_baseline(self, opened)
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened, marked_files=("first.marked.png",))
        marked_before = (self.results / "first.marked.png").read_bytes()
        source_before = (self.images / "first.png").read_bytes()
        saved_review = json.loads((self.results / "review.json").read_text())

        orphan_cases = (
            ("no-review-json", None),
            ("missing-image-entry", {**saved_review, "images": [saved_review["images"][1]]}),
        )
        for case, saved_value in orphan_cases:
            with self.subTest(case=case):
                results = self.root / case
                results.mkdir()
                marked_path = results / "first.marked.png"
                marked_path.write_bytes(marked_before)
                review_path = results / "review.json"
                if saved_value is None:
                    review_before = None
                else:
                    review_before = json.dumps(saved_value, ensure_ascii=False).encode("utf-8")
                    review_path.write_bytes(review_before)

                failure = self.cli("open", check=False, results=results)
                review_during_open = review_path.read_bytes() if review_path.exists() else None
                marked_during_open = marked_path.read_bytes() if marked_path.exists() else None
                files_during_open = result_files(results)
                if failure.returncode == 0:
                    replacement = json.loads(failure.stdout)
                    self.cli("cleanup", results=results)
                    self.no_leaks(replacement, marked_files=("first.marked.png",))

                self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
                error = json.loads(failure.stdout)["error"]
                self.assertIn("first.png", error)
                self.assertEqual(review_during_open, review_before)
                self.assertEqual(marked_during_open, marked_before)
                expected_files = ["first.marked.png"]
                if saved_value is not None:
                    expected_files.append("review.json")
                self.assertEqual(files_during_open, expected_files)
                self.assertEqual((self.images / "first.png").read_bytes(), source_before)

        control_results = self.root / "artifact-free-control"
        control = json.loads(self.cli("open", results=control_results).stdout)
        self.assertEqual(control["status"], "open")
        state = self.request(control, "api/state")
        self.assertEqual(state["images"][0]["notes"], "")
        self.assertEqual(state["images"][0]["markup"], [])
        self.request(control, "api/done", {})
        wait = self.cli("wait", results=control_results, check=False)
        self.assertEqual((wait.stdout.strip(), wait.returncode), ("done", 0), wait.stderr)
        self.assertEqual(result_files(control_results), ["review.json"])
        self.no_leaks(control, marked_files=("first.marked.png",))


class ReviewActiveSourceSaveAdmission(ReviewHarness):
    """Refuse durable edits when a live session's source has been replaced."""

    def _assert_changed_source_save_is_rejected(self, endpoint):
        opened = self.start()
        notes, pen = save_marked_baseline(self, opened)
        review_path = self.results / "review.json"
        marked_path = self.results / "first.marked.png"
        review_before = review_path.read_bytes()
        marked_before = marked_path.read_bytes()
        blue_source = fixture_png(64, 48, (35, 105, 220))
        source_path = self.images / "first.png"
        source_path.write_bytes(blue_source)
        self.assertEqual(png_dimensions(blue_source), png_dimensions(self.original), "Same size, other content")
        self.assertNotEqual(blue_source, self.original)
        source_after_replacement = source_path.read_bytes()
        changed_pen = {
            "type": "pen",
            "color": "#2563eb",
            "width": 3,
            "group": "must-not-bind-to-new-source",
            "points": [[10, 30], [40, 30]],
        }
        if endpoint == "api/close":
            snapshot = {
                "index": 0,
                "revision": 3,
                "notes_delta": [{"revision": 2, "start": len(notes), "delete": 0, "insert": " Replacement capture."}],
                "markup_delta": [{"revision": 3, "op": "append", "mark": changed_pen}],
                "png": None,
            }
        else:
            snapshot = {
                "index": 0,
                "revision": 2,
                "notes": "This blue capture must not replace the white verdict.",
                "markup": [changed_pen],
                "png": None,
            }
        assert_source_change_error(self, opened, endpoint, snapshot)
        if endpoint == "api/close":
            self.assertEqual(self.request(opened, "api/state")["status"], "open")

        state = self.request(opened, "api/state")
        self.assertEqual((state["status"], state["images"][0]["notes"]), ("open", notes))
        self.assertEqual(state["images"][0]["revision"], 1)
        self.assertEqual(state["images"][0]["markup"], [pen])
        code, body = self.upload_error(opened, 0, 1, BASELINE_MARKED)
        self.assertEqual(code, 400, body)
        self.assertIn("source changed since open", body.get("error", ""))
        self.assertEqual(review_path.read_bytes(), review_before)
        self.assertEqual(marked_path.read_bytes(), marked_before)
        self.assertEqual(source_path.read_bytes(), source_after_replacement)
        self.assertEqual(marked_before, with_revision_chunk(BASELINE_MARKED, 1))
        self.cli("cleanup")
        assert_process_stopped(self, opened)
        self.assertEqual(result_files(self.results), ["first.marked.png", "review.json"])

    def test_active_regular_save_surfaces_source_change_without_mutation(self):
        self._assert_changed_source_save_is_rejected("api/save")

    def test_active_close_delta_save_surfaces_source_change_without_mutation(self):
        self._assert_changed_source_save_is_rejected("api/close")


class ReviewImageApi(ReviewHarness):
    """Display sizes, marked PNG uploads and original media, with no image decoding in the server."""

    PEN = {"type": "pen", "color": "#ef4444", "width": 4, "points": [[20, 20], [280, 180]]}

    def wide_session(self):
        """Open a session that includes wide.png, a 300×200 image; return the session and its index."""
        (self.images / "wide.png").write_bytes(fixture_png(300, 200))
        opened = self.start(*LONG_CLOSE)
        state = self.request(opened, "api/state")
        index = next(i for i, image in enumerate(state["images"]) if image["name"] == "wide.png")
        return opened, index

    def save_markup(self, opened, index, revision, markup):
        return self.request(
            opened, "api/save", {"index": index, "revision": revision, "notes": "", "markup": markup, "png": None}
        )

    def test_prepare_accepts_raw_and_swapped_size_and_rejects_others(self):
        opened, index = self.wide_session()
        for width, height in ((300, 200), (200, 300)):
            with self.subTest(accepted=(width, height)):
                prepared = self.request(opened, "api/prepare", {"index": index, "width": width, "height": height})
                self.assertEqual(
                    prepared,
                    {
                        "ready": True,
                        "width": width,
                        "height": height,
                        "revision": 0,
                        "markup_revision": 0,
                        "png_revision": None,
                        "png_stale": False,
                    },
                )
        for width, height in ((301, 200), (300, 199), (200, 200), (300, 300)):
            with self.subTest(rejected=(width, height)):
                code, body = http_error(self, opened, "api/prepare", {"index": index, "width": width, "height": height})
                self.assertEqual(code, 400, body)
                self.assertIn("wide.png", body["error"])
                self.assertIn(f"{width}×{height}", body["error"])
                self.assertIn("300×200", body["error"])
        invalid_sizes = {
            "missing": {},
            "zero": {"width": 0, "height": 200},
            "fractional": {"width": 300.5, "height": 200},
            "text": {"width": "300", "height": "200"},
            "boolean": {"width": True, "height": 200},
        }
        for label, size in invalid_sizes.items():
            with self.subTest(invalid=label):
                code, body = http_error(self, opened, "api/prepare", {"index": index, **size})
                self.assertEqual(code, 400, body)
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_markup_save_requires_prepare(self):
        opened = self.start(*LONG_CLOSE)
        pen = self.PEN
        durable = (self.results / "review.json").read_bytes()
        code, body = http_error(self, opened, "api/prepare", {"index": 0, "width": 65, "height": 48})
        self.assertEqual(code, 400, body)
        refused = {
            "full snapshot": {"index": 0, "revision": 1, "notes": "", "markup": [pen], "png": None},
            "markup delta": {
                "index": 0,
                "revision": 1,
                "notes_delta": [],
                "markup_delta": [{"revision": 1, "op": "append", "mark": pen}],
            },
        }
        for label, payload in refused.items():
            with self.subTest(label):
                code, body = http_error(self, opened, "api/save", payload)
                self.assertEqual(code, 400, body)
                self.assertIn("prepare", body["error"])
                self.assertEqual((self.results / "review.json").read_bytes(), durable)
        notes_only = self.request(
            opened, "api/save", {"index": 0, "revision": 1, "notes": "notes need no prepare", "markup": [], "png": None}
        )
        self.assertEqual((notes_only["revision"], notes_only["ignored"]), (1, False))
        self.request(
            opened,
            "api/save",
            {
                "index": 1,
                "revision": 1,
                "notes_delta": [{"revision": 1, "start": 0, "delete": 0, "insert": "delta notes"}],
                "markup_delta": [],
            },
        )
        self.prepare(opened)
        self.request(
            opened,
            "api/save",
            {"index": 0, "revision": 2, "notes": "notes need no prepare", "markup": [pen], "png": None},
        )
        self.request(opened, "api/done", {})
        first, untouched = self.wait_reason("done", 0)["images"]
        self.assertEqual(first["notes"], "notes need no prepare")
        self.assertEqual(
            (first["markup"], first["markup_revision"], first["marked"], first["png_stale"]),
            ([pen], 2, None, True),
            "A markup change without an upload leaves the PNG stale",
        )
        self.assertEqual(untouched["notes"], "delta notes")
        self.no_leaks(opened)

    def test_save_and_prepare_report_the_markup_revision_an_upload_needs(self):
        opened, index = self.wide_session()
        self.assertEqual(self.prepare(opened, index, (300, 200))["markup_revision"], 0)
        pen = self.request(
            opened,
            "api/save",
            {
                "index": index,
                "revision": 1,
                "notes_delta": [],
                "markup_delta": [{"revision": 1, "op": "append", "mark": self.PEN}],
            },
        )
        self.assertEqual((pen["revision"], pen["markup_revision"]), (1, 1))
        notes = self.request(
            opened,
            "api/save",
            {
                "index": index,
                "revision": 2,
                "notes_delta": [{"revision": 2, "start": 0, "delete": 0, "insert": "notes"}],
                "markup_delta": [],
            },
        )
        self.assertEqual(
            (notes["revision"], notes["markup_revision"]), (2, 1), "A notes edit keeps the markup revision"
        )
        mixed = self.request(
            opened,
            "api/save",
            {
                "index": index,
                "revision": 4,
                "notes_delta": [{"revision": 4, "start": 5, "delete": 0, "insert": "!"}],
                "markup_delta": [{"revision": 3, "op": "undo"}],
            },
        )
        self.assertEqual((mixed["revision"], mixed["markup_revision"]), (4, 3), "The newest markup patch names it")
        self.assertEqual(self.prepare(opened, index, (300, 200))["markup_revision"], 3)
        code, body = self.upload_error(opened, index, 4, fixture_png(300, 200))
        self.assertEqual(code, 409, body)
        restored = self.request(
            opened,
            "api/save",
            {
                "index": index,
                "revision": 5,
                "notes_delta": [],
                "markup_delta": [{"revision": 5, "op": "append", "mark": self.PEN}],
            },
        )
        self.assertEqual(restored["markup_revision"], 5)
        stale = self.request(
            opened, "api/save", {"index": index, "revision": 5, "notes": "", "markup": [], "png": None}
        )
        self.assertEqual(
            (stale["ignored"], stale["markup_revision"]), (True, 5), "An ignored save reports the stored one"
        )
        uploaded = self.upload(opened, index, restored["markup_revision"], fixture_png(300, 200))
        self.assertEqual((uploaded["png_revision"], uploaded["png_stale"]), (5, False))
        self.request(opened, "api/done", {})
        entry = self.wait_reason("done", 0)["images"][index]
        self.assertEqual((entry["notes"], entry["markup_revision"], entry["png_stale"]), ("notes!", 5, False))
        self.no_leaks(opened, marked_files=("wide.marked.png",))

    def test_marked_upload_writes_png_with_revision(self):
        opened, index = self.wide_session()
        self.prepare(opened, index, (300, 200))
        self.save_markup(opened, index, 1, [self.PEN])
        marked = self.results / "wide.marked.png"
        body = fixture_png(300, 200, (10, 20, 30))
        response = self.upload(opened, index, 1, body)
        self.assertEqual(response, {"marked": "wide.marked.png", "png_revision": 1, "png_stale": False})
        self.assertEqual(marked.read_bytes(), with_revision_chunk(body, 1))
        self.assertEqual(text_chunks(marked.read_bytes()), [(REVISION_KEY, b"1")])
        entry = self.status()["images"][index]
        self.assertEqual(
            (entry["marked"], entry["markup_revision"], entry["png_revision"], entry["png_stale"]),
            ("wide.marked.png", 1, 1, False),
        )
        reported = json.loads(self.cli("results").stdout)["images"][index]
        self.assertFalse(reported["png_stale"], "results reads the revision the server wrote")

        arrow = {"type": "arrow", "color": "#2563eb", "width": 2, "x1": 10, "y1": 10, "x2": 100, "y2": 50}
        self.save_markup(opened, index, 2, [self.PEN, arrow])
        entry = self.status()["images"][index]
        self.assertEqual(
            (entry["marked"], entry["markup_revision"], entry["png_revision"], entry["png_stale"]),
            ("wide.marked.png", 2, 1, True),
            "A markup change makes the uploaded PNG stale",
        )
        self.assertEqual(marked.read_bytes(), with_revision_chunk(body, 1))

        # The upload's own text chunks stay; a revision chunk it carries is replaced.
        plain = fixture_png(300, 200, (40, 50, 60))
        comment = png_chunk(b"tEXt", b"Comment\x00kept")
        forged = png_chunk(b"tEXt", REVISION_KEY + b"\x00999")
        self.upload(opened, index, 2, plain[:33] + comment + forged + plain[33:])
        self.assertEqual(marked.read_bytes(), with_revision_chunk(plain[:33] + comment + plain[33:], 2))
        self.assertEqual(text_chunks(marked.read_bytes()), [(REVISION_KEY, b"2"), (b"Comment", b"kept")])
        self.request(opened, "api/done", {})
        entry = self.wait_reason("done", 0)["images"][index]
        self.assertEqual((entry["marked"], entry["png_revision"], entry["png_stale"]), ("wide.marked.png", 2, False))
        self.no_leaks(opened, marked_files=("wide.marked.png",))

    def test_marked_upload_rejects_stale_revision_wrong_size_and_non_png(self):
        opened, index = self.wide_session()
        self.prepare(opened, index, (300, 200))
        self.save_markup(opened, index, 1, [self.PEN])
        self.save_markup(opened, index, 2, [self.PEN, self.PEN])
        durable = (self.results / "review.json").read_bytes()
        self.assertEqual(json.loads(durable)["images"][index]["markup_revision"], 2)
        marked = self.results / "wide.marked.png"
        good = fixture_png(300, 200)
        refused = (
            ("an earlier revision", 1, good, 409),
            ("a later revision", 3, good, 409),
            ("one column short", 2, fixture_png(299, 200), 400),
            ("the swapped size, which was not prepared", 2, fixture_png(200, 300), 400),
            ("not a PNG", 2, b"junk", 400),
            ("an empty body", 2, b"", 400),
            ("no IEND chunk", 2, good[:-12], 400),
            ("a revision that is not a number", "two", good, 400),
            ("a negative revision", -2, good, 400),
            ("no revision", None, good, 400),
        )
        for label, revision, body, expected in refused:
            with self.subTest(label):
                code, error = self.upload_error(opened, index, revision, body)
                self.assertEqual(code, expected, error)
                self.assertFalse(marked.exists())
                self.assertEqual((self.results / "review.json").read_bytes(), durable)

        self.prepare(opened, 0)
        code, error = self.upload_error(opened, 0, 0, fixture_png(64, 48))
        self.assertEqual(code, 400, f"An image without markup has no marked PNG: {error}")
        self.assertFalse((self.results / "first.marked.png").exists())

        self.upload(opened, index, 2, good)
        self.assertEqual(marked.read_bytes(), with_revision_chunk(good, 2), "Positive control at the current revision")
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened, marked_files=("wide.marked.png",))

    def test_marked_upload_refuses_changed_source(self):
        opened, index = self.wide_session()
        self.prepare(opened, index, (300, 200))
        self.save_markup(opened, index, 1, [self.PEN])
        durable = (self.results / "review.json").read_bytes()
        marked = self.results / "wide.marked.png"
        source = self.images / "wide.png"
        original = source.read_bytes()
        source.write_bytes(fixture_png(300, 200, (35, 105, 220)))
        code, body = self.upload_error(opened, index, 1, fixture_png(300, 200))
        self.assertEqual(code, 400, body)
        self.assertIn("source changed since open: wide.png", body["error"])
        self.assertFalse(marked.exists())
        self.assertEqual((self.results / "review.json").read_bytes(), durable)

        source.write_bytes(original)
        self.upload(opened, index, 1, fixture_png(300, 200))
        self.assertTrue(marked.is_file(), "The same upload succeeds once the original source is back")
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened, marked_files=("wide.marked.png",))

    def test_clearing_markup_deletes_the_png(self):
        opened, index = self.wide_session()
        self.prepare(opened, index, (300, 200))
        marked = self.results / "wide.marked.png"
        clears = {
            "full snapshot": lambda revision: {
                "index": index,
                "revision": revision,
                "notes": "",
                "markup": [],
                "png": None,
            },
            "clear delta": lambda revision: {
                "index": index,
                "revision": revision,
                "notes_delta": [],
                "markup_delta": [{"revision": revision, "op": "clear"}],
            },
        }
        revision = 0
        for label, clear in clears.items():
            with self.subTest(label):
                revision += 1
                self.save_markup(opened, index, revision, [self.PEN])
                self.upload(opened, index, revision, fixture_png(300, 200))
                self.assertTrue(marked.is_file(), "Positive control: the upload wrote the PNG")
                revision += 1
                self.request(opened, "api/save", clear(revision))
                self.assertFalse(marked.exists())
                entry = self.status()["images"][index]
                self.assertEqual(
                    (entry["markup"], entry["markup_revision"], entry["marked"], entry["png_revision"]),
                    ([], revision, None, None),
                )
                self.assertFalse(entry["png_stale"])
                self.assertFalse(json.loads(self.cli("results").stdout)["images"][index]["png_stale"])
        self.request(opened, "api/done", {})
        self.assertIsNone(self.wait_reason("done", 0)["images"][index]["marked"])
        self.no_leaks(opened)

    def test_results_reconciles_freshness_from_the_png(self):
        opened, index = self.wide_session()
        self.prepare(opened, index, (300, 200))
        self.save_markup(opened, index, 1, [self.PEN])
        body = fixture_png(300, 200)
        self.upload(opened, index, 1, body)
        marked = self.results / "wide.marked.png"
        fresh = marked.read_bytes()
        server_review = (self.results / "review.json").read_bytes()

        def reported_stale():
            return json.loads(self.cli("results").stdout)["images"][index]["png_stale"]

        self.assertFalse(reported_stale(), "Positive control: the uploaded PNG is fresh")
        replacements = {
            "deleted": None,
            "another revision": with_revision_chunk(body, 7),
            "no revision chunk": body,
        }
        for label, replacement in replacements.items():
            with self.subTest(label):
                if replacement is None:
                    marked.unlink()
                else:
                    marked.write_bytes(replacement)
                self.assertTrue(reported_stale())
                self.assertEqual(
                    (self.results / "review.json").read_bytes(), server_review, "A live reader must not write"
                )
                marked.write_bytes(fresh)
                self.assertFalse(reported_stale(), "The restored PNG is fresh again")
        self.request(opened, "api/done", {})
        self.assertFalse(self.wait_reason("done", 0)["images"][index]["png_stale"])
        self.no_leaks(opened, marked_files=("wide.marked.png",))

    def test_prepare_reports_a_png_removed_or_replaced_behind_the_review(self):
        opened, index = self.wide_session()
        self.prepare(opened, index, (300, 200))
        self.save_markup(opened, index, 1, [self.PEN])
        body = fixture_png(300, 200)
        self.upload(opened, index, 1, body)
        marked = self.results / "wide.marked.png"
        marked.unlink()
        prepared = self.prepare(opened, index, (300, 200))
        self.assertEqual((prepared["png_revision"], prepared["png_stale"]), (1, True), "The page must upload again")
        self.assertTrue(self.status()["images"][index]["png_stale"], "prepare records the staleness it found")
        self.upload(opened, index, 1, body)
        self.request(opened, "api/done", {})
        self.assertFalse(self.wait_reason("done", 0)["images"][index]["png_stale"])
        self.no_leaks(opened, marked_files=("wide.marked.png",))

        marked.write_bytes(with_revision_chunk(body, 7))
        reopened = self.start(*LONG_CLOSE)
        prepared = self.prepare(reopened, index, (300, 200))
        self.assertEqual((prepared["png_revision"], prepared["png_stale"]), (1, True), "Replaced while no server ran")
        self.upload(reopened, index, 1, body)
        self.assertFalse(self.prepare(reopened, index, (300, 200))["png_stale"], "Positive control after the upload")
        self.request(reopened, "api/done", {})
        self.assertFalse(self.wait_reason("done", 0)["images"][index]["png_stale"])
        self.no_leaks(reopened, marked_files=("wide.marked.png",))

    def test_mask_without_scale_fails_closed(self):
        opened = self.start()
        self.prepare(opened)
        mask = {"width": 2, "height": 1, "scale": 1, "png": data_url(fixture_png(2, 1))}
        text = {"type": "text", "color": "#2563eb", "x": 5, "y": 6, "size": 24, "text": "label", "mask": mask}
        unscaled = {**text, "mask": {key: value for key, value in mask.items() if key != "scale"}}
        code, body = http_error(
            self, opened, "api/save", {"index": 0, "revision": 1, "notes": "", "markup": [unscaled], "png": None}
        )
        self.assertEqual(code, 400, body)
        self.assertIn("scale", body["error"])
        self.request(opened, "api/save", {"index": 0, "revision": 1, "notes": "label", "markup": [text], "png": None})
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

        review_path = self.results / "review.json"
        stored = json.loads(review_path.read_text())
        del stored["images"][0]["markup"][0]["mask"]["scale"]
        review_path.write_text(json.dumps(stored))
        review_before = review_path.read_bytes()
        for command in ("open", "status"):
            with self.subTest(command=command):
                failure = self.cli(command, check=False)
                self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
                self.assertEqual(
                    json.loads(failure.stdout),
                    {"error": "text mark has no mask scale; this review.json is not supported"},
                )
        self.assertEqual(review_path.read_bytes(), review_before)
        self.assertEqual(result_files(self.results), ["review.json"])

    def test_corrupt_text_mask_is_rejected(self):
        opened = self.start()
        self.prepare(opened)

        def text_mark(png):
            mask = {"width": 4, "height": 2, "scale": 1, "png": data_url(png)}
            return {"type": "text", "color": "#ef4444", "x": 0, "y": 0, "size": 18, "text": "mask", "mask": mask}

        comment = [(b"tEXt", b"Comment\x00mask")]
        corrupt = {
            "chunk checksum": fixture_png_with_ancillary(comment, corrupt_crc=True),
            "truncated before IEND": fixture_png_with_ancillary(comment)[:-12],
        }
        durable = (self.results / "review.json").read_bytes()
        for label, png in corrupt.items():
            mark = text_mark(png)
            snapshots = {
                "full": {"index": 0, "revision": 1, "notes": "", "markup": [mark], "png": None},
                "delta": {
                    "index": 0,
                    "revision": 1,
                    "notes_delta": [],
                    "markup_delta": [{"revision": 1, "op": "append", "mark": mark}],
                },
            }
            for shape, payload in snapshots.items():
                with self.subTest(mask=label, snapshot=shape):
                    code, body = http_error(self, opened, "api/save", payload)
                    self.assertEqual(code, 400, body)
                    self.assertIn("text mask", body["error"])
                    self.assertEqual((self.results / "review.json").read_bytes(), durable)
        valid = text_mark(fixture_png_with_ancillary(comment))
        self.request(opened, "api/save", {"index": 0, "revision": 1, "notes": "", "markup": [valid], "png": None})
        self.request(opened, "api/done", {})
        self.assertEqual(self.wait_reason("done", 0)["images"][0]["markup"], [valid])
        self.no_leaks(opened)

    def test_media_serves_images_and_videos(self):
        # fixtures/loop-1s.webm comes from the ffmpeg test source:
        #   ffmpeg -y -f lavfi -i testsrc=size=160x120:rate=24:duration=1 -c:v libvpx-vp9 -pix_fmt yuv420p \
        #     -b:v 0 -crf 40 -an -fflags +bitexact -map_metadata -1 loop-1s.webm
        (self.images / "video.webm").write_bytes((FIXTURES / "loop-1s.webm").read_bytes())
        opened = self.start()
        state = self.request(opened, "api/state")
        expected = (
            ("first.png", "image/png", "image", 64, 48),
            ("untouched.png", "image/png", "image", 64, 48),
            ("video.webm", "video/webm", "video", None, None),
        )
        self.assertEqual([item["name"] for item in state["images"]], [name for name, *_ in expected])
        fields = {"name", "kind", "width", "height", "url", "notes", "markup"}
        fields |= {"markup_revision", "png_revision", "png_stale", "revision"}
        for index, (name, mime, kind, width, height) in enumerate(expected):
            with self.subTest(name=name):
                item = state["images"][index]
                self.assertLessEqual(fields, set(item))
                self.assertFalse({"preview_width", "preview_height", "codec"} & set(item), sorted(item))
                self.assertEqual(
                    (item["kind"], item["width"], item["height"], item["url"]), (kind, width, height, f"media/{index}")
                )
                self.assertEqual(self.media_response(opened, item["url"]), (mime, (self.images / name).read_bytes()))
        self.assertEqual(
            self.ranged(opened, "media/0", "bytes=0-7"), (206, f"bytes 0-7/{len(self.original)}", self.original[:8])
        )
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.media_response(opened, "image/0")
        self.assertEqual(failure.exception.code, 404)
        failure.exception.close()
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_hidden_files_are_not_review_items(self):
        # macOS can write an AppleDouble file of metadata, named ._NAME, beside each file on some drives.
        (self.images / "._first.png").write_bytes(struct.pack(">II16sH", 0x00051607, 0x00020000, b"Mac OS X", 0))
        (self.images / ".preview.png").write_bytes(fixture_png())
        opened = self.start(*LONG_CLOSE)
        names = [item["name"] for item in self.request(opened, "api/state")["images"]]
        self.assertEqual(names, ["first.png", "untouched.png"])
        self.assertEqual([item["name"] for item in self.status()["images"]], names)
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_large_image_admitted_from_header_only(self):
        header = PNG_SIGNATURE + png_chunk(b"IHDR", struct.pack("!IIBBBBB", 10_000, 5_000, 8, 2, 0, 0, 0))
        (self.images / "header-only.png").write_bytes(header)
        (self.images / "progressive.jpg").write_bytes(jpeg_header(10_000, 5_000, sof=0xC2))
        opened = self.start(*LONG_CLOSE)
        state = self.request(opened, "api/state")
        durable = self.status()
        for name in ("header-only.png", "progressive.jpg"):
            with self.subTest(name=name):
                index = next(i for i, item in enumerate(state["images"]) if item["name"] == name)
                item, entry = state["images"][index], durable["images"][index]
                self.assertEqual((item["width"], item["height"]), (10_000, 5_000))
                self.assertEqual((entry["width"], entry["height"]), (10_000, 5_000))
                self.assertFalse({"preview_width", "preview_height"} & set(entry), sorted(entry))
                for size in ((10_000, 5_000), (5_000, 10_000)):
                    prepared = self.prepare(opened, index, size)
                    self.assertEqual((prepared["width"], prepared["height"]), size)
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    @unittest.skipIf(os.name == "nt", "file permissions cannot make a file unreadable on Windows")
    def test_unreadable_image_is_reported_by_name(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("the superuser can read any file")
        locked = self.images / "locked.png"
        locked.write_bytes(fixture_png())
        locked.chmod(0)
        failure = self.cli("open", check=False)
        self.assertEqual(failure.returncode, 2, failure.stdout + failure.stderr)
        self.assertTrue(
            json.loads(failure.stdout)["error"].startswith("cannot read image locked.png: "), failure.stdout
        )
        self.assertEqual(result_files(self.results), [])


class ReviewStartup(ReviewHarness):
    """A server that fails or stalls during startup is reported once and leaves nothing behind."""

    def test_failed_startup_reports_once_and_leaves_no_server_or_files(self):
        for variant in ("fail", "refuse-runtime", "stall"):
            with self.subTest(startup=variant):
                case_dir = self.root / f"{variant}-startup"
                case_dir.mkdir()
                self.results = case_dir / "results"
                gates = self.gates(variant)
                opener = self.spawn(open_args(self.images, self.results), env=gates.env())
                runtime = None
                if variant == "stall":
                    gates.wait_reached("stall")
                    runtime_path = self.results / "runtime.json"
                    deadline = time.monotonic() + 15
                    while not runtime_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                    self.assertFalse(port_refuses(runtime["url"]), "The stalled server holds its port")
                    self.assertFalse(ping_alive(runtime, timeout=1), "The stalled server does not answer")
                stdout, stderr = opener.communicate(timeout=60)
                self.assertEqual(opener.returncode, 2, stdout + stderr)
                error = json.loads(stdout)["error"]
                attempts = gates.attempts()
                self.assertEqual(len(attempts), 1, "One open invocation must not retry the start")
                if variant == "fail":
                    self.assertIn("VISUAL_REVIEW_TEST_STARTUP_FAILURE", error, "The error carries the server log")
                    expected_status = "killed"
                elif variant == "refuse-runtime":
                    self.assertIn("VISUAL_REVIEW_TEST_RUNTIME_REFUSED", error, "The error carries the server log")
                    expected_status = "interrupted"
                else:
                    self.assertIn("did not become ready", error)
                    self.assertIn("VISUAL_REVIEW_TEST_STALLED_STARTUP", error, "The error carries the server log")
                    expected_status = "interrupted" if os.name != "nt" else "killed"
                    assert_process_stopped(self, {**runtime, "pid": attempts[0]})
                self.assertFalse(process_exists(attempts[0]), "open stops the server it started")
                self.assertFalse((self.results / "runtime.json").exists())
                self.assertEqual(transient_directories(self.results), [])
                self.assertEqual(result_files(self.results), ["review.json"])
                self.assertEqual(json.loads((self.results / "review.json").read_text())["status"], expected_status)


class ReviewVideoItems(ReviewHarness):
    """A video is one notes-only item, streamed with byte ranges."""

    def test_video_streams_byte_ranges_and_refuses_markup(self):
        clip = (FIXTURES / "loop-1s.webm").read_bytes()
        size = len(clip)
        (self.images / "clip.webm").write_bytes(clip)
        opened = self.start()
        state = self.request(opened, "api/state")
        index = next(i for i, item in enumerate(state["images"]) if item["name"] == "clip.webm")
        video = state["images"][index]
        self.assertEqual((video["kind"], video["url"], video["markup"]), ("video", f"media/{index}", []))
        endpoint = f"media/{index}"
        self.assertEqual(
            self.ranged(opened, endpoint, "bytes=-16"), (206, f"bytes {size - 16}-{size - 1}/{size}", clip[-16:])
        )
        self.assertEqual(self.ranged(opened, endpoint, "bytes=100-"), (206, f"bytes 100-{size - 1}/{size}", clip[100:]))
        # A multi-range request is not parsed; the whole file is the valid answer.
        self.assertEqual(self.ranged(opened, endpoint, "bytes=0-0,5-6"), (200, None, clip))
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.ranged(opened, endpoint, f"bytes={size}-")
        self.assertEqual(
            (failure.exception.code, failure.exception.headers.get("Content-Range")), (416, f"bytes */{size}")
        )
        failure.exception.close()
        code, body = http_error(self, opened, "api/prepare", {"index": index, "width": 160, "height": 120})
        self.assertEqual(code, 400, body)
        self.assertIn("video", body.get("error", ""))
        code, body = self.upload_error(opened, index, video["markup_revision"], fixture_png(160, 120))
        self.assertEqual(code, 400, body)
        self.assertIn("video", body.get("error", ""))
        revision = video["revision"] + 1
        mark = {"type": "rect", "color": "#ff0000", "width": 2, "x": 1, "y": 1, "w": 5, "h": 5}
        code, body = http_error(
            self,
            opened,
            "api/save",
            {
                "index": index,
                "revision": revision,
                "markup_delta": [{"revision": revision, "op": "append", "mark": mark}],
            },
        )
        self.assertEqual(code, 400, body)
        self.assertIn("video items do not accept markup", body.get("error", ""))
        self.request(
            opened,
            "api/save",
            {
                "index": index,
                "revision": revision,
                "notes_delta": [{"revision": revision, "start": 0, "delete": 0, "insert": "Seam is clean."}],
            },
        )
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)
        saved = json.loads((self.results / "review.json").read_text())["images"][index]
        self.assertEqual(
            (saved["kind"], saved["notes"], saved["markup"], saved["marked"]), ("video", "Seam is clean.", [], None)
        )
        self.assertFalse((self.results / "clip.marked.png").exists())

    def test_save_does_not_reread_a_large_unchanged_source(self):
        clip = (FIXTURES / "loop-1s.webm").read_bytes()
        large = 32 * 1024 * 1024
        source = self.images / "large.webm"
        source.write_bytes(clip + bytes(large - len(clip)))
        gates = self.gates("opens")
        opened = self.start(env=gates.env(VISUAL_REVIEW_TEST_WATCH_DIR=str(self.images)))
        state = self.request(opened, "api/state")
        index = next(i for i, item in enumerate(state["images"]) if item["name"] == "large.webm")
        self.assertEqual(self.ranged(opened, f"media/{index}", "bytes=0-0")[0], 206)
        self.assertEqual(gates.opened(source), 1, "Positive control: the log records the server's source opens")
        revision = state["images"][index]["revision"]
        opens = []
        for step in range(3):
            revision += 1
            before = gates.opened(source)
            self.request(
                opened,
                "api/save",
                {
                    "index": index,
                    "revision": revision,
                    "notes_delta": [{"revision": revision, "start": step, "delete": 0, "insert": "x"}],
                },
            )
            opens.append(gates.opened(source) - before)
        # The first save may hash the source once; later saves must not open it again.
        self.assertLessEqual(opens[0], 1, opens)
        self.assertEqual(opens[1:], [0, 0], opens)
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_first_media_request_does_not_hash_the_source(self):
        clip = (FIXTURES / "loop-1s.webm").read_bytes()
        large = 32 * 1024 * 1024
        source = self.images / "large.webm"
        source.write_bytes(clip + bytes(large - len(clip)))
        gates = self.gates("opens")
        opened = self.start(env=gates.env(VISUAL_REVIEW_TEST_WATCH_DIR=str(self.images)))
        index = next(
            i for i, item in enumerate(self.request(opened, "api/state")["images"]) if item["name"] == "large.webm"
        )
        before = gates.opened(source)
        self.assertEqual(self.ranged(opened, f"media/{index}", "bytes=0-0")[0], 206)
        self.assertEqual(gates.opened(source) - before, 1, "Streaming opens the source once; hashing opens it again")
        self.request(opened, "api/close", {})
        self.no_leaks(opened)

    def test_mp4_that_starts_with_any_top_level_box_is_accepted(self):
        def box(kind, payload):
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload

        (self.images / "free-first.mp4").write_bytes(box(b"free", b"") + box(b"ftyp", b"isom" + bytes(4)))
        opened = self.start()
        state = self.request(opened, "api/state")
        index, video = next((i, item) for i, item in enumerate(state["images"]) if item["name"] == "free-first.mp4")
        self.assertEqual((video["kind"], video["url"]), ("video", f"media/{index}"))
        self.request(opened, "api/done", {})
        self.wait_reason("done", 0)
        self.no_leaks(opened)

    def test_media_etag_follows_the_current_source(self):
        clip = (FIXTURES / "loop-1s.webm").read_bytes()
        source = self.images / "clip.webm"
        source.write_bytes(clip)
        opened = self.start()
        index = next(
            i for i, item in enumerate(self.request(opened, "api/state")["images"]) if item["name"] == "clip.webm"
        )

        def etag():
            request = urllib.request.Request(opened["url"] + f"media/{index}", headers={"Range": "bytes=0-0"})
            with DIRECT.open(request, timeout=5) as response:
                return response.headers.get("ETag")

        before = etag()
        self.assertTrue(before)
        time.sleep(0.01)
        source.write_bytes(clip[:-1] + bytes([clip[-1] ^ 0xFF]))
        self.assertNotEqual(etag(), before)
        self.request(opened, "api/close", {})
        self.no_leaks(opened)

    def test_video_with_a_wrong_signature_is_refused(self):
        (self.images / "fake.mp4").write_bytes(b"not a video at all")
        result = self.cli("open", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fake.mp4 does not contain video/mp4 data", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
