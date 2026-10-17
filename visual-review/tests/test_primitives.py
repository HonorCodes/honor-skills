"""Unit tests for the portable primitives in review.py."""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fixtures_util import jpeg_header, png_bytes, webp_header

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT = TESTS_DIR.parent / "review.py"


def load_review():
    spec = importlib.util.spec_from_file_location("visual_review_primitives", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


review = load_review()

LOCK_CHILD = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
from pathlib import Path
with r.FileLock(Path(sys.argv[2])):
    print("acquired", flush=True)
"""

PROXY_CHILD = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("r", sys.argv[1])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
sys.exit(0 if r.server_alive({"url": sys.argv[2], "session_id": "S1"}) else 1)
"""


class FileLockTests(unittest.TestCase):
    def test_second_process_waits_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / review.LOCK_NAME
            with review.FileLock(lock_path):
                child = subprocess.Popen(
                    [sys.executable, "-c", LOCK_CHILD, str(SCRIPT), str(lock_path)],
                    stdout=subprocess.PIPE, text=True)
                time.sleep(1.0)
                self.assertIsNone(child.poll(), "child acquired a held lock")
            out, _ = child.communicate(timeout=10)
            self.assertEqual(out.strip(), "acquired")

    def test_lock_file_is_kept_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / review.IO_LOCK_NAME
            with review.FileLock(lock_path):
                pass
            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.stat().st_size, 0)


class AtomicWriteTests(unittest.TestCase):
    def test_replaces_content_and_leaves_no_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "review.json"
            review.atomic_write(target, b"one")
            review.atomic_write(target, b"two")
            self.assertEqual(review.read_locked(target), b"two")
            leftovers = [p.name for p in Path(root).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [])
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_replace_waits_for_the_io_lock(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            results = Path(root)
            target = results / "review.json"
            done = threading.Event()
            with review.io_lock(results):
                worker = threading.Thread(
                    target=lambda: (review.atomic_write(target, b"x"), done.set()))
                worker.start()
                time.sleep(0.5)
                self.assertFalse(done.is_set(), "write ignored the held I/O lock")
            worker.join(10)
            self.assertTrue(done.is_set())

    def test_remove_locked_ignores_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            absent = Path(root) / "absent.json"
            review.remove_locked(absent)
            self.assertFalse(absent.exists())

    def test_remove_locked_deletes_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            present = Path(root) / "present.json"
            present.write_bytes(b"x")
            review.remove_locked(present)
            self.assertFalse(present.exists())


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/tok/api/ping":
            body = json.dumps({"alive": True, "session_id": "S1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args: object) -> None:
        pass


class MalformedHandler(BaseHTTPRequestHandler):
    """Answers like a service that does not speak HTTP."""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self.wfile.write(b"not an HTTP response\r\n\r\n")

    def log_message(self, *args: object) -> None:
        pass


class PingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_alive_only_for_matching_token_and_session(self) -> None:
        self.assertTrue(review.server_alive({"url": f"{self.base}/tok/", "session_id": "S1"}))
        self.assertFalse(review.server_alive({"url": f"{self.base}/tok/", "session_id": "S2"}))
        self.assertFalse(review.server_alive({"url": f"{self.base}/bad/", "session_id": "S1"}))

    def test_proxy_settings_are_ignored(self) -> None:
        # urllib.request.urlopen builds one global opener, with the proxy environment it sees at
        # its first use. A probe inside this process could pass on an opener built earlier, so the
        # probe runs in a fresh interpreter whose environment names a proxy that does not answer.
        env = {key: value for key, value in os.environ.items() if key.lower() != "no_proxy"}
        env["http_proxy"] = env["HTTP_PROXY"] = "http://127.0.0.1:9"
        completed = subprocess.run(
            [sys.executable, "-c", PROXY_CHILD, str(SCRIPT), f"{self.base}/tok/"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(
            completed.returncode, 0,
            "server_alive did not reach the local server while a proxy was configured\n"
            + completed.stdout + completed.stderr)

    def test_closed_port_is_not_alive(self) -> None:
        port = self.server.server_address[1]
        self.tearDown()
        self.assertFalse(review.server_alive({"url": f"http://127.0.0.1:{port}/tok/", "session_id": "S1"}))
        self.setUp()

    def test_service_that_does_not_speak_http_is_not_alive(self) -> None:
        stranger = ThreadingHTTPServer(("127.0.0.1", 0), MalformedHandler)
        threading.Thread(target=stranger.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{stranger.server_address[1]}/tok/"
            self.assertFalse(review.server_alive({"url": url, "session_id": "S1"}))
        finally:
            stranger.shutdown()
            stranger.server_close()


class SpawnTests(unittest.TestCase):
    def test_detached_child_writes_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "server.log"
            child = review.spawn_server(
                [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"], log)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and b"started" not in log.read_bytes():
                    time.sleep(0.05)
                self.assertIn(b"started", log.read_bytes())
                if os.name != "nt":
                    self.assertEqual(os.getsid(child.pid), child.pid)
            finally:
                child.kill()
                child.wait(10)

    def test_windows_children_get_a_console_without_a_window(self) -> None:
        # Win32 values. Windows ignores CREATE_NO_WINDOW next to DETACHED_PROCESS.
        new_group, no_window, detached = 0x00000200, 0x08000000, 0x00000008
        flags = {"CREATE_NEW_PROCESS_GROUP": new_group, "CREATE_NO_WINDOW": no_window, "DETACHED_PROCESS": detached}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(review.os, "name", "nt"))
            for name, value in flags.items():
                stack.enter_context(mock.patch.object(review.subprocess, name, value, create=True))
            options = review._detached_options()
        self.assertEqual(options.get("creationflags"), new_group | no_window)
        self.assertNotIn("start_new_session", options)


class OutputEncodingTests(unittest.TestCase):
    def test_error_json_needs_no_utf8_stream(self) -> None:
        # Like a replaced sys.stdout: it encodes as cp1252 and has no reconfigure().
        written = bytearray()

        class Cp1252Stream:
            def write(self, text: str) -> int:
                written.extend(text.encode("cp1252"))
                return len(text)

            def flush(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as root:
            occupied = Path(root) / "taken \u2192"
            occupied.write_bytes(b"")
            with mock.patch.object(sys, "stdout", Cp1252Stream()), mock.patch.object(sys, "stderr", io.StringIO()):
                code = review.main(["status", "--results", str(occupied)])
        self.assertEqual(code, 2)
        self.assertIn(occupied.name, json.loads(written.decode("cp1252"))["error"])


STUB = """
import sys
from pathlib import Path
Path(sys.argv[1]).write_text("\\n".join(sys.argv[2:]), encoding="utf-8")
"""


class BrowserLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {k: os.environ.get(k) for k in ("VISUAL_REVIEW_BROWSER", "BROWSER", "PATH")}
        self.root = tempfile.TemporaryDirectory()
        self.stub = Path(self.root.name) / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.record = Path(self.root.name) / "argv.txt"

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.root.cleanup()

    def wait_for_record(self) -> str:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (self.record.exists() and self.record.stat().st_size):
            time.sleep(0.05)
        return self.record.read_text(encoding="utf-8")

    def test_custom_command_substitutes_the_url(self) -> None:
        os.environ["VISUAL_REVIEW_BROWSER"] = f'"{sys.executable}" "{self.stub}" "{self.record}" --app=%s'
        self.assertTrue(review.open_in_browser("http://127.0.0.1:1/t/"))
        self.assertEqual(self.wait_for_record(), "--app=http://127.0.0.1:1/t/")

    def test_custom_command_without_placeholder_appends_the_url(self) -> None:
        os.environ["VISUAL_REVIEW_BROWSER"] = f'"{sys.executable}" "{self.stub}" "{self.record}"'
        self.assertTrue(review.open_in_browser("http://127.0.0.1:1/t/"))
        self.assertEqual(self.wait_for_record(), "http://127.0.0.1:1/t/")

    @unittest.skipIf(os.name == "nt", "needs an executable lynx script found through PATH")
    def test_console_browser_counts_as_no_browser(self) -> None:
        # The lynx on PATH is a stub that leaves a marker, so a launch cannot go unnoticed: without
        # the console check, webbrowser would start it and the call would report a browser.
        marker = Path(self.root.name) / "lynx-ran"
        bin_dir = Path(self.root.name) / "bin"
        bin_dir.mkdir()
        lynx = bin_dir / "lynx"
        lynx.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\n", encoding="utf-8")
        lynx.chmod(0o755)
        os.environ.pop("VISUAL_REVIEW_BROWSER", None)
        os.environ["BROWSER"] = "lynx"
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        self.assertFalse(review.open_in_browser("http://127.0.0.1:1/t/"))
        self.assertFalse(marker.exists(), "a console browser must not be started")

    def test_helper_that_cannot_start_counts_as_no_browser(self) -> None:
        with mock.patch.object(review.subprocess, "run", side_effect=FileNotFoundError("no interpreter")):
            self.assertFalse(review.open_in_browser("http://127.0.0.1:1/t/"))

    def test_helper_that_times_out_counts_as_no_browser(self) -> None:
        with mock.patch.object(review.subprocess, "run", side_effect=subprocess.TimeoutExpired("helper", 1)):
            self.assertFalse(review.open_in_browser("http://127.0.0.1:1/t/"))


class TokenTests(unittest.TestCase):
    def test_token_is_url_safe_and_long(self) -> None:
        token = review.new_token()
        self.assertGreaterEqual(len(token), 43)
        self.assertRegex(token, r"^[A-Za-z0-9_-]+$")


REVISION_KEY = b"visual-review-markup-revision"
IHDR_2X2 = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
IDAT_2X2 = zlib.compress((b"\x00" + b"\x00" * 8) * 2)


def png_from_chunks(*chunks: tuple[bytes, bytes]) -> bytes:
    """A PNG made of exactly the given (type, payload) chunks, each with a correct CRC."""
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        for kind, payload in chunks)


def revision_in(data: bytes) -> int | None:
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "a.marked.png"
        path.write_bytes(data)
        return review.read_png_revision(path)


@contextlib.contextmanager
def int_digit_limit(limit: int) -> Iterator[None]:
    """Set the int/str conversion limit for a block; 0 means none, as before CPython 3.9.14 and 3.10.7."""
    if not hasattr(sys, "set_int_max_str_digits"):
        yield
        return
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(limit)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(previous)


class PngTests(unittest.TestCase):
    def test_size_of_a_valid_png(self) -> None:
        self.assertEqual(review.png_size(png_bytes(3, 2)), (3, 2))

    def test_bad_crc_truncation_and_missing_iend_are_rejected(self) -> None:
        good = png_bytes(2, 2)
        broken = bytearray(good)
        broken[-1] ^= 0xFF  # corrupt the IEND CRC
        for data in (bytes(broken), good[:-12], good[:20], b"not a png"):
            with self.assertRaises(review.ReviewError):
                review.png_size(data)

    def test_revision_chunk_is_inserted_once_after_ihdr(self) -> None:
        once = review.with_markup_revision(png_bytes(2, 2), 7)
        twice = review.with_markup_revision(once, 9)
        kinds = [kind for kind, _, _ in review.png_chunks(twice)]
        self.assertEqual(kinds[:2], [b"IHDR", b"tEXt"])
        self.assertEqual(kinds.count(b"tEXt"), 1)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.marked.png"
            path.write_bytes(twice)
            self.assertEqual(review.read_png_revision(path), 9)
            path.write_bytes(png_bytes(2, 2))
            self.assertIsNone(review.read_png_revision(path))

    def test_unrelated_text_chunks_are_kept(self) -> None:
        data = review.with_markup_revision(png_bytes(2, 2, [b"Comment\x00hi"]), 1)
        texts = [data[s:e] for kind, s, e in review.png_chunks(data) if kind == b"tEXt"]
        self.assertIn(b"Comment\x00hi", texts)

    def test_png_with_a_malformed_header_is_rejected(self) -> None:
        ihdr_too_long = png_from_chunks((b"IHDR", IHDR_2X2 + b"\x00"), (b"IDAT", IDAT_2X2), (b"IEND", b""))
        text_first = png_from_chunks((b"tEXt", b"Comment\x00hi"), (b"IHDR", IHDR_2X2), (b"IEND", b""))
        for label, data in (("zero width", png_bytes(0, 2)), ("zero height", png_bytes(2, 0)),
                            ("IHDR of 14 bytes", ihdr_too_long), ("IHDR not first", text_first)):
            with self.subTest(label):
                with self.assertRaises(review.ReviewError):
                    review.png_size(data)

    def test_revision_is_read_only_from_an_intact_chunk_before_the_image_data(self) -> None:
        ours = REVISION_KEY + b"\x00"
        header, image, end = (b"IHDR", IHDR_2X2), (b"IDAT", IDAT_2X2), (b"IEND", b"")
        valid = png_from_chunks(header, (b"tEXt", ours + b"12"), image, end)
        self.assertEqual(revision_in(valid), 12)
        other_first = png_from_chunks(header, (b"tEXt", b"Other\x0099"), (b"tEXt", ours + b"12"), image, end)
        self.assertEqual(revision_in(other_first), 12)
        unusable = {
            "damaged chunk": valid.replace(ours + b"12", ours + b"13"),
            "after the image data": png_from_chunks(header, image, (b"tEXt", ours + b"12"), end),
            "wrong signature": b"X" * 8 + valid[8:],
            "truncated": valid[:45],
            "not a PNG": b"not a png",
            "empty": b"",
        }
        for value in (b"", b"abc", b"-3", b" 5", b"7" * 5000):
            unusable[f"value {value[:8]!r}"] = png_from_chunks(header, (b"tEXt", ours + value), image, end)
        for label, data in unusable.items():
            with self.subTest(label):
                self.assertIsNone(revision_in(data))
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(review.read_png_revision(Path(root) / "missing.png"))
            self.assertIsNone(review.read_png_revision(Path(root)))

    def test_revision_text_is_limited_to_the_revisions_the_server_writes(self) -> None:
        ours = REVISION_KEY + b"\x00"
        cases = ((b"0", 0), (b"9007199254740991", 2**53 - 1), (b"9007199254740992", None),
                 (b"9" * 17, None), (b"7" * 1000, None), (b"7" * 5000, None))
        for limit in (0, 640):  # no limit, and the smallest limit CPython accepts
            with int_digit_limit(limit):
                for text, expected in cases:
                    with self.subTest(limit=limit, text=text[:8]):
                        data = png_from_chunks((b"IHDR", IHDR_2X2), (b"tEXt", ours + text),
                                               (b"IDAT", IDAT_2X2), (b"IEND", b""))
                        self.assertEqual(revision_in(data), expected)

    def test_a_forged_text_chunk_length_does_not_size_a_read(self) -> None:
        ours = REVISION_KEY + b"\x00"
        forged_start = png_from_chunks((b"IHDR", IHDR_2X2)) + struct.pack(">I", 0xFFFFFFFF)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "forged.png"
            for kind in (b"tEXt", b"zTXt"):
                with self.subTest(kind):
                    path.write_bytes(forged_start + kind + b"short")
                    tracemalloc.start()
                    try:
                        tracemalloc.reset_peak()
                        revision = review.read_png_revision(path)
                        peak = tracemalloc.get_traced_memory()[1]
                    finally:
                        tracemalloc.stop()
                    self.assertIsNone(revision)
                    self.assertLess(peak, 1_000_000)
        header, image, end = (b"IHDR", IHDR_2X2), (b"IDAT", IDAT_2X2), (b"IEND", b"")
        large_other_text = (b"tEXt", b"Other\x00" + b"x" * 5000)
        skipped_then_found = png_from_chunks(header, large_other_text, (b"tEXt", ours + b"12"), image, end)
        self.assertEqual(revision_in(skipped_then_found), 12)

    def test_a_file_name_that_cannot_be_opened_has_no_revision(self) -> None:
        # review.json can name a marked PNG this way; open() raises ValueError, not OSError, for a
        # NUL in a file name, and on POSIX also for a lone surrogate.
        with tempfile.TemporaryDirectory() as root:
            for name in ("first\x00.marked.png", "first\ud800.marked.png"):
                with self.subTest(name=ascii(name)):
                    self.assertIsNone(review.read_png_revision(Path(root) / name))


class ImageHeaderTests(unittest.TestCase):
    def check(self, name: str, data: bytes, expected: tuple[int, int]) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / name
            path.write_bytes(data)
            self.assertEqual(review.image_raw_size(path), expected)

    def rejected(self, name: str, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / name
            path.write_bytes(data)
            with self.assertRaises(review.ReviewError):
                review.image_raw_size(path)

    def test_png(self) -> None:
        self.check("a.png", png_bytes(10, 5), (10, 5))

    def test_baseline_and_progressive_jpeg_after_large_segments(self) -> None:
        self.check("a.jpg", jpeg_header(640, 480, 0xC0, big_app=200_000), (640, 480))
        self.check("b.jpeg", jpeg_header(10000, 5000, 0xC2), (10000, 5000))

    def test_webp_variants(self) -> None:
        for kind in ("VP8 ", "VP8L", "VP8X"):
            with self.subTest(kind=kind):
                self.check("a.webp", webp_header(kind, 300, 200), (300, 200))

    def test_wrong_content_for_extension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.png"
            path.write_bytes(jpeg_header(4, 4))
            with self.assertRaises(review.ReviewError):
                review.image_raw_size(path)

    def test_extension_case_is_ignored(self) -> None:
        self.check("A.PNG", png_bytes(2, 3), (2, 3))
        self.check("B.JPG", jpeg_header(7, 9), (7, 9))
        self.check("C.WebP", webp_header("VP8L", 11, 13), (11, 13))

    def test_content_must_match_the_extension_and_the_extension_must_be_known(self) -> None:
        samples = {".png": png_bytes(4, 4), ".jpg": jpeg_header(4, 4), ".webp": webp_header("VP8L", 4, 4)}
        for extension in samples:
            for content, data in samples.items():
                if content != extension:
                    with self.subTest(extension=extension, content=content):
                        self.rejected("a" + extension, data)
        self.rejected("a.gif", b"GIF89a" + b"\x00" * 26)

    def test_damaged_empty_truncated_and_zero_sized_headers_are_rejected(self) -> None:
        png = png_bytes(4, 4)
        png_cut_in_height = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                             + struct.pack(">II", 1, 65536)[:6])
        png_text_first = png_from_chunks((b"tEXt", b"Comment\x00hi"), (b"IHDR", IHDR_2X2), (b"IEND", b""))
        jpeg = jpeg_header(640, 480)
        webp = webp_header("VP8L", 4, 4)
        cases = {
            "empty png": ("a.png", b""),
            "empty jpeg": ("a.jpg", b""),
            "empty webp": ("a.webp", b""),
            "png signature damaged": ("a.png", b"X" * 8 + png[8:]),
            "png whose first chunk is not IHDR": ("a.png", png_text_first),
            "png cut inside the height": ("a.png", png_cut_in_height),
            "jpeg without a start marker": ("a.jpg", b"\x00\x00" + jpeg_header(4, 4)[2:]),
            "jpeg cut inside the frame header": ("a.jpg", jpeg[:jpeg.index(b"\xff\xc0") + 8]),
            "jpeg without a frame header": ("a.jpg", b"\xff\xd8\xff\xd9"),
            "riff header damaged": ("a.webp", b"RIFX" + webp[4:]),
            "riff form is not webp": ("a.webp", webp[:8] + b"WAVE" + webp[12:]),
            "webp cut inside the payload": ("a.webp", webp_header("VP8X", 300, 200)[:26]),
            "zero width png": ("a.png", png_bytes(0, 5)),
            "zero width jpeg": ("a.jpg", jpeg_header(0, 5)),
            "zero width webp": ("a.webp", webp_header("VP8 ", 0, 5)),
        }
        for label, (name, data) in cases.items():
            with self.subTest(label):
                self.rejected(name, data)

    def test_jpeg_segments_that_are_not_frame_headers_are_skipped(self) -> None:
        base = jpeg_header(640, 480)
        huffman_table = b"\xff\xc4" + struct.pack(">H", 19) + b"\x00" * 17
        reserved = b"\xff\xc8" + struct.pack(">H", 4) + b"\x00\x00"
        arithmetic_conditioning = b"\xff\xcc" + struct.pack(">H", 4) + b"\x00\x00"
        self.check("a.jpg", base[:2] + huffman_table + reserved + arithmetic_conditioning + base[2:], (640, 480))
        frame = base[base.index(b"\xff\xc0"):-2]
        scan = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
        self.rejected("b.jpg", b"\xff\xd8" + scan + frame + b"\xff\xd9")

    def test_webp_lossy_scale_bits_are_not_part_of_the_size(self) -> None:
        self.check("a.webp", webp_header("VP8 ", 300 | 0xC000, 200 | 0x4000), (300, 200))

    def test_webp_payloads_without_their_signature_are_rejected(self) -> None:
        lossless = bytearray(webp_header("VP8L", 300, 200))
        lossless[20] = 0x00  # the signature byte opens the payload
        lossy = bytearray(webp_header("VP8 ", 300, 200))
        lossy[23:26] = b"\x00\x00\x00"  # the start code follows the 3-byte frame tag
        for name, data in (("lossless", lossless), ("lossy", lossy)):
            with self.subTest(name):
                self.rejected("a.webp", bytes(data))


class TextMaskTests(unittest.TestCase):
    def test_mask_must_be_a_valid_png_of_the_declared_size(self) -> None:
        good = "data:image/png;base64," + base64.b64encode(png_bytes(4, 3)).decode()
        review.validate_text_mask_png(good, 4, 3)
        for url, w, h in ((good, 5, 3), ("data:image/png;base64,!!", 4, 3),
                          ("data:image/jpeg;base64,AAAA", 4, 3)):
            with self.subTest(url=url[:30], w=w):
                with self.assertRaises(review.ReviewError):
                    review.validate_text_mask_png(url, w, h)

    def test_non_strings_and_damaged_pngs_are_rejected(self) -> None:
        def url(data: bytes) -> str:
            return "data:image/png;base64," + base64.b64encode(data).decode()

        good = png_bytes(4, 3)
        damaged = bytearray(good)
        damaged[-1] ^= 0xFF
        for label, value in (("none", None), ("bad crc", url(bytes(damaged))),
                             ("truncated", url(good[:-12])), ("not a png", url(b"junk")),
                             ("other image type", "data:image/gif;base64," + base64.b64encode(good).decode()),
                             ("stray character", url(good)[:40] + "\n" + url(good)[40:])):
            with self.subTest(label):
                with self.assertRaises(review.ReviewError):
                    review.validate_text_mask_png(value, 4, 3)


if __name__ == "__main__":
    unittest.main()
