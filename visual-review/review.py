#!/usr/bin/env python3
"""Private, local visual review server and command-line lifecycle manager."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import html
import http.client
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, unquote, urlsplit

SCHEMA_VERSION = 1
TERMINAL_CODES = {"done": 0, "closed": 10, "timeout": 11, "interrupted": 12, "killed": 13}
SUPPORTED_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
VIDEO_MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg"}
MEDIA_CHUNK_BYTES = 256 * 1024
MP4_TOP_LEVEL_BOXES = frozenset({b"ftyp", b"free", b"skip", b"wide", b"moov", b"mdat", b"styp", b"uuid"})
MAX_NOTES_UTF16 = 6_000
MAX_TEXT_CODEPOINTS = 240
MAX_TEXT_LINES = 8
MAX_MARKS = 20_000
MAX_POINTS_PER_SEGMENT = 512
MAX_GROUP_ID_LENGTH = 128
MAX_STARTUP_DIAGNOSTIC_BYTES = 8_192


class ReviewError(Exception):
    """An expected command or request error safe to return to the reviewer."""


class StaleRevisionError(ReviewError):
    """A marked PNG upload names a markup revision that is not the current one."""


LOCK_NAME = ".visual-review.lock"
IO_LOCK_NAME = ".visual-review-io.lock"
LOCK_RETRY_SECONDS = 0.05
BROWSER_LAUNCH_SECONDS = 15
CONSOLE_BROWSERS = frozenset({"www-browser", "links", "elinks", "lynx", "w3m"})
RUNTIME_NAME = "runtime.json"
TRANSIENT_PREFIX = ".visual-review-"
CLOSE_GRACE_SECONDS = 5.0
MONITOR_SECONDS = 0.05
REQUEST_DRAIN_SECONDS = 2.0
STARTUP_SECONDS = 20
STOP_GRACE_SECONDS = 5.0
CLEANUP_STOP_SECONDS = 10.0
WAIT_POLL_SECONDS = 0.1
PING_INTERVAL_SECONDS = 1.0
DEAD_AFTER_FAILED_PINGS = 3


class FileLock:
    """Exclusive lock on one file that blocks until acquired.

    Windows uses non-blocking attempts in a loop because its blocking mode gives up after
    ten seconds, and `open` can hold the session lock longer than that.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(LOCK_RETRY_SECONDS)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc_info: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def session_lock(results: Path) -> FileLock:
    return FileLock(results / LOCK_NAME)


def io_lock(results: Path) -> FileLock:
    return FileLock(results / IO_LOCK_NAME)


def fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)


def atomic_write(path: Path, data: bytes) -> None:
    directory = path.parent
    fd, temporary = tempfile.mkstemp(prefix=f".review-{os.getpid()}-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with io_lock(directory):
            os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    fsync_directory(directory)


def read_locked(path: Path) -> bytes:
    with io_lock(path.parent):
        return path.read_bytes()


def remove_locked(path: Path) -> None:
    with io_lock(path.parent):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _detached_options() -> dict[str, Any]:
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        # A console without a window. Under DETACHED_PROCESS, a virtual-environment launcher would start
        # the real interpreter in a new visible console, and closing that console would stop the child.
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


def spawn_server(command: list[str], log_path: Path) -> subprocess.Popen:
    with open(log_path, "ab") as log:
        return subprocess.Popen(command, stdout=log, stderr=log, **_detached_options())


_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def runtime_call(
    runtime: dict[str, Any],
    route: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any] | None:
    """Call the server named by runtime.json; None when it does not answer with JSON."""
    url = str(runtime.get("url", ""))
    if not url.startswith("http://127.0.0.1:"):
        return None
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url + route, data=data, method="GET" if data is None else "POST")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error.close()
        return None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return body if isinstance(body, dict) else None


def server_alive(runtime: dict[str, Any]) -> bool:
    body = runtime_call(runtime, "api/ping")
    return bool(body) and body.get("alive") is True and body.get("session_id") == runtime.get("session_id")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def open_in_browser(url: str) -> bool:
    """Open url through a helper process so browser output never reaches this process's stdout."""
    command = [sys.executable, str(Path(__file__).resolve()), "__open-url", url]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=BROWSER_LAUNCH_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0


def launch_custom_browser(template: str, url: str) -> int:
    parts = shlex.split(template, posix=os.name != "nt")
    if os.name == "nt":
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    if not parts:
        return 1
    if any("%s" in part for part in parts):
        argv = [part.replace("%s", url) for part in parts]
    else:
        argv = parts + [url]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached_options())
    except OSError:
        return 1
    return 0


def open_url_helper(url: str) -> int:
    template = os.environ.get("VISUAL_REVIEW_BROWSER", "").strip()
    if template:
        return launch_custom_browser(template, url)
    try:
        controller = webbrowser.get()
    except webbrowser.Error:
        return 1
    if os.path.basename(str(getattr(controller, "name", ""))) in CONSOLE_BROWSERS:
        return 1
    try:
        return 0 if controller.open(url, new=1) else 1
    except (webbrowser.Error, OSError):
        return 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def secure_results(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ReviewError("results path must be a directory")
    os.chmod(path, 0o700)
    return path


def load_review(results: Path) -> dict[str, Any] | None:
    path = results / "review.json"
    if not path.exists():
        return None
    try:
        value = json.loads(read_locked(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"cannot read results/review.json: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ReviewError("results/review.json has an unsupported schema")
    if not isinstance(value.get("images"), list) or not isinstance(value.get("status"), str):
        raise ReviewError("results/review.json is malformed")
    for image in value["images"]:
        markup = image.get("markup") if isinstance(image, dict) else None
        for mark in markup if isinstance(markup, list) else []:
            if (
                isinstance(mark, dict)
                and mark.get("type") == "text"
                and isinstance(mark.get("mask"), dict)
                and "scale" not in mark["mask"]
            ):
                raise ReviewError("text mark has no mask scale; this review.json is not supported")
    return value


def remove_known_temps(results: Path, owner_pid: int | None = None) -> None:
    """Delete staging files: the owner's own, or every one when the caller found no live server."""
    prefix = ".review-" if owner_pid is None else f".review-{owner_pid}-"
    try:
        entries = list(results.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(prefix) and entry.name.endswith(".tmp") and entry.is_file():
            try:
                entry.unlink()
            except OSError:
                pass


def transient_directory(results: Path, session_id: str) -> Path:
    return results / f"{TRANSIENT_PREFIX}{session_id}"


def remove_transient_directories(results: Path) -> None:
    try:
        entries = list(results.iterdir())
    except OSError:
        return
    for entry in entries:
        # The lock files share the prefix.
        if entry.name.startswith(TRANSIENT_PREFIX) and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def remove_runtime(results: Path, session_id: str | None = None) -> None:
    """Delete runtime.json; with session_id, only while the file still names that session."""
    path = results / RUNTIME_NAME
    with io_lock(results):
        if session_id is not None:
            try:
                runtime = json.loads(path.read_bytes().decode("utf-8"))
            except (OSError, UnicodeError, ValueError):
                return
            if not isinstance(runtime, dict) or runtime.get("session_id") != session_id:
                return
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def read_runtime_bytes(results: Path) -> bytes | None:
    try:
        return read_locked(results / RUNTIME_NAME)
    except FileNotFoundError:
        return None


def parse_runtime(raw: bytes) -> dict[str, Any]:
    """The runtime.json fields; {} for a damaged file, which then never answers a ping."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def live_owner(raw: bytes | None) -> dict[str, Any] | None:
    """The fields of runtime.json contents `raw` when the server they name answers ping."""
    if raw is None:
        return None
    runtime = parse_runtime(raw)
    return runtime if server_alive(runtime) else None


def live_runtime(results: Path) -> dict[str, Any] | None:
    return live_owner(read_runtime_bytes(results))


def wait_for_owner_exit(results: Path, owner_raw: bytes) -> None:
    """Wait until the server named by runtime.json contents `owner_raw` has ended.

    It has ended when it no longer answers and runtime.json no longer holds those bytes, or when
    it failed DEAD_AFTER_FAILED_PINGS pings about a second apart without releasing runtime.json.
    """
    owner = parse_runtime(owner_raw)
    failed_pings = 0
    last_failure: float | None = None
    while True:
        if server_alive(owner):
            failed_pings = 0
        elif read_runtime_bytes(results) != owner_raw:
            return
        elif last_failure is None or time.monotonic() - last_failure >= PING_INTERVAL_SECONDS:
            failed_pings += 1
            last_failure = time.monotonic()
            if failed_pings >= DEAD_AFTER_FAILED_PINGS:
                return
        time.sleep(0.05)


def recover_dead_session(results: Path, review: dict[str, Any] | None) -> dict[str, Any] | None:
    """Clean up after a server that no longer answers. The caller holds the session lock."""
    if review is not None:
        if review.get("status") == "open":
            review["status"] = "killed"
            review["done"] = False
            review["updated_at"] = utc_now()
            review["finished_at"] = review["updated_at"]
            write_json(results / "review.json", review)
        for image in review.get("images", []):
            if not isinstance(image, dict) or image.get("markup") != []:
                continue
            image_name = image.get("name")
            if (
                not isinstance(image_name, str)
                or Path(image_name).name != image_name
                or Path(image_name).suffix.casefold() not in SUPPORTED_MIME
            ):
                continue
            remove_locked(results / marked_filename(image_name))
    remove_runtime(results)
    remove_transient_directories(results)
    remove_known_temps(results)
    return review


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MARKUP_REVISION_KEY = b"visual-review-markup-revision"
JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def png_chunks(data: bytes) -> Iterator[tuple[bytes, int, int]]:
    """Yield (type, data_start, data_end) for each chunk after checking its CRC, through IEND."""
    if not data.startswith(PNG_SIGNATURE):
        raise ReviewError("data is not a PNG file")
    offset = len(PNG_SIGNATURE)
    while True:
        if offset + 12 > len(data):
            raise ReviewError("PNG data is truncated")
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if length > 0x7FFFFFFF or end + 4 > len(data):
            raise ReviewError("PNG data is truncated")
        if zlib.crc32(data[offset + 4 : end]) & 0xFFFFFFFF != int.from_bytes(data[end : end + 4], "big"):
            raise ReviewError("PNG chunk checksum does not match")
        yield kind, start, end
        if kind == b"IEND":
            return
        offset = end + 4


def png_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) from IHDR after checking every chunk through IEND."""
    chunks = png_chunks(data)
    kind, start, end = next(chunks)
    if kind != b"IHDR" or end - start != 13:
        raise ReviewError("PNG does not start with an IHDR chunk")
    width = int.from_bytes(data[start : start + 4], "big")
    height = int.from_bytes(data[start + 4 : start + 8], "big")
    for _ in chunks:
        pass
    if width <= 0 or height <= 0:
        raise ReviewError("PNG size must be positive")
    return width, height


def _text_chunk(key: bytes, value: bytes) -> bytes:
    body = b"tEXt" + key + b"\x00" + value
    return (len(body) - 4).to_bytes(4, "big") + body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")


def with_markup_revision(data: bytes, revision: int) -> bytes:
    """Return the PNG with exactly one markup-revision tEXt chunk, placed after IHDR."""
    pieces = [PNG_SIGNATURE]
    for kind, start, end in png_chunks(data):
        if kind == b"tEXt" and data[start:end].split(b"\x00", 1)[0] == MARKUP_REVISION_KEY:
            continue
        pieces.append(data[start - 8 : end + 4])
        if kind == b"IHDR":
            pieces.append(_text_chunk(MARKUP_REVISION_KEY, str(revision).encode("ascii")))
    return b"".join(pieces)


def read_png_revision(path: Path) -> int | None:
    """Read the revision from the text chunks before IDAT, where with_markup_revision puts it."""
    try:
        with open(path, "rb") as stream:
            if stream.read(8) != PNG_SIGNATURE:
                return None
            while True:
                header = stream.read(8)
                if len(header) < 8:
                    return None
                length = int.from_bytes(header[:4], "big")
                kind = header[4:]
                if kind in (b"IDAT", b"IEND"):
                    return None
                # The revision chunk is under 50 bytes. A longer text chunk is skipped unread, so a
                # forged length cannot size a read.
                if kind != b"tEXt" or length > 4096:
                    stream.seek(length + 4, os.SEEK_CUR)
                    continue
                body = stream.read(length)
                crc = stream.read(4)
                if len(body) < length or len(crc) < 4:
                    return None
                if zlib.crc32(kind + body) & 0xFFFFFFFF != int.from_bytes(crc, "big"):
                    return None
                key, _, value = body.partition(b"\x00")
                if key != MARKUP_REVISION_KEY:
                    continue
                # Revisions run from 0 to 2**53 - 1, at most 16 digits. Checking the length first keeps
                # int() independent of the interpreter's limit on long digit strings.
                if not value.isdigit() or len(value) > 16:
                    return None
                revision = int(value)
                return revision if revision <= 2**53 - 1 else None
    # ValueError: open() refuses a name with a NUL, or on POSIX a lone surrogate, from review.json.
    except (OSError, ValueError):
        return None


def _jpeg_size(stream: BinaryIO, name: str) -> tuple[int, int]:
    stream.seek(2)
    while True:
        if stream.read(1) != b"\xff":
            raise ReviewError(f"{name} does not contain a JPEG image")
        marker = stream.read(1)
        while marker == b"\xff":
            marker = stream.read(1)
        if not marker:
            raise ReviewError(f"{name} does not contain a JPEG image")
        code = marker[0]
        if code == 0x01 or 0xD0 <= code <= 0xD7:
            continue
        if code in (0xD9, 0xDA):
            raise ReviewError(f"{name} does not contain a JPEG image")
        length_bytes = stream.read(2)
        if len(length_bytes) < 2 or int.from_bytes(length_bytes, "big") < 2:
            raise ReviewError(f"{name} does not contain a JPEG image")
        length = int.from_bytes(length_bytes, "big")
        if code in JPEG_SOF_MARKERS:
            frame = stream.read(5)
            if len(frame) < 5:
                raise ReviewError(f"{name} does not contain a JPEG image")
            return int.from_bytes(frame[3:5], "big"), int.from_bytes(frame[1:3], "big")
        stream.seek(length - 2, os.SEEK_CUR)


def _webp_size(stream: BinaryIO, name: str) -> tuple[int, int]:
    stream.seek(12)
    fourcc = stream.read(8)[:4]
    data = stream.read(10)
    if fourcc == b"VP8X" and len(data) >= 10:
        return 1 + int.from_bytes(data[4:7], "little"), 1 + int.from_bytes(data[7:10], "little")
    if fourcc == b"VP8L" and len(data) >= 5 and data[0] == 0x2F:
        bits = int.from_bytes(data[1:5], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if fourcc == b"VP8 " and len(data) >= 10 and data[3:6] == b"\x9d\x01\x2a":
        return int.from_bytes(data[6:8], "little") & 0x3FFF, int.from_bytes(data[8:10], "little") & 0x3FFF
    raise ReviewError(f"{name} does not contain a WEBP image")


def image_raw_size(path: Path) -> tuple[int, int]:
    """Read width and height from the file header, matched to the extension."""
    suffix = path.suffix.lower()
    with open(path, "rb") as stream:
        head = stream.read(32)
        if suffix == ".png":
            if len(head) < 24 or not head.startswith(PNG_SIGNATURE) or head[12:16] != b"IHDR":
                raise ReviewError(f"{path.name} does not contain a PNG image")
            size = (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))
        elif suffix in (".jpg", ".jpeg"):
            if not head.startswith(b"\xff\xd8"):
                raise ReviewError(f"{path.name} does not contain a JPEG image")
            size = _jpeg_size(stream, path.name)
        elif suffix == ".webp":
            if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
                raise ReviewError(f"{path.name} does not contain a WEBP image")
            size = _webp_size(stream, path.name)
        else:
            raise ReviewError(f"{path.name} is not a supported image type")
    if size[0] <= 0 or size[1] <= 0:
        raise ReviewError(f"{path.name} has a non-positive image size")
    return size


def validate_text_mask_png(data_url: str, width: int, height: int) -> None:
    prefix = "data:image/png;base64,"
    if not isinstance(data_url, str) or not data_url.startswith(prefix):
        raise ReviewError("text mask must be a PNG data URL")
    try:
        data = base64.b64decode(data_url[len(prefix) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReviewError("text mask is not valid base64") from exc
    try:
        size = png_size(data)
    except ReviewError as exc:
        raise ReviewError(f"text mask PNG is invalid: {exc}") from exc
    if size != (width, height):
        raise ReviewError("text mask size does not match its PNG")


def video_metadata(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            head = source.read(12)
    except OSError as exc:
        raise ReviewError(f"cannot read video {path.name}: {exc}") from exc
    suffix = path.suffix.casefold()
    valid = {
        ".mp4": len(head) >= 8 and head[4:8] in MP4_TOP_LEVEL_BOXES,
        ".webm": head[:4] == b"\x1a\x45\xdf\xa3",
        ".ogv": head[:4] == b"OggS",
    }[suffix]
    if not valid:
        raise ReviewError(f"video {path.name} does not contain {VIDEO_MIME[suffix]} data")
    return {"kind": "video", "width": None, "height": None}


def item_mime(path: Path) -> str:
    suffix = path.suffix.casefold()
    return SUPPORTED_MIME.get(suffix) or VIDEO_MIME[suffix]


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReviewError(f"cannot read image {path.name}: {exc}") from exc
    return digest.hexdigest()


def enumerate_images(image_dir_value: str) -> tuple[Path, list[dict[str, Any]]]:
    image_dir = Path(image_dir_value).expanduser().resolve(strict=True)
    if not image_dir.is_dir():
        raise ReviewError("images path must be a directory")
    images: list[dict[str, Any]] = []
    stems: set[str] = set()
    for path in sorted(image_dir.iterdir(), key=lambda item: item.name.casefold()):
        suffix = path.suffix.casefold()
        if (
            path.name.startswith(".")
            or path.is_symlink()
            or not path.is_file()
            or (suffix not in SUPPORTED_MIME and suffix not in VIDEO_MIME)
        ):
            continue
        stem = path.stem.casefold()
        if stem in stems:
            raise ReviewError(f"image names must have unique stems: {path.stem}")
        stems.add(stem)
        if suffix in VIDEO_MIME:
            metadata = video_metadata(path)
        else:
            try:
                width, height = image_raw_size(path)
            except OSError as exc:
                raise ReviewError(f"cannot read image {path.name}: {exc}") from exc
            metadata = {"kind": "image", "width": width, "height": height}
        images.append(
            {
                "name": path.name,
                "path": str(path),
                "mime": item_mime(path),
                **metadata,
                "source_sha256": source_sha256(path),
            }
        )
    if not images:
        raise ReviewError("images directory has no supported PNG, JPEG, WebP, MP4, WebM, or OGV files")
    return image_dir, images


def initial_review(results: Path, image_files: list[dict[str, Any]], question: str) -> dict[str, Any]:
    previous = load_review(results)
    by_name = {item.get("name"): item for item in previous.get("images", [])} if previous else {}
    now = utc_now()
    images: list[dict[str, Any]] = []
    for source in image_files:
        old = by_name.get(source["name"], {})
        markup = copy.deepcopy(old.get("markup", [])) if isinstance(old.get("markup", []), list) else []
        if source["kind"] == "video":
            markup = []
        marked = (
            old.get("marked")
            if isinstance(old.get("marked"), str) and Path(old.get("marked", "")).name == old.get("marked")
            else None
        )
        markup_revision = old.get("markup_revision")
        if isinstance(markup_revision, bool) or not isinstance(markup_revision, int):
            markup_revision = 0
        png_revision = old.get("png_revision") if marked else None
        if isinstance(png_revision, bool) or not isinstance(png_revision, int):
            png_revision = None
        images.append(
            {
                "name": source["name"],
                "kind": source["kind"],
                "source_sha256": source["source_sha256"],
                "width": source["width"],
                "height": source["height"],
                "notes": old.get("notes", "") if isinstance(old.get("notes", ""), str) else "",
                "updated_at": old.get("updated_at", now),
                "markup": markup,
                "marked": marked,
                "markup_revision": markup_revision,
                "png_revision": png_revision,
                "png_stale": bool(markup and (marked is None or png_revision != markup_revision)),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": uuid.uuid4().hex,
        "question": question,
        "status": "open",
        "done": False,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "images": images,
    }


def validate_resume_sources(
    results: Path,
    review: dict[str, Any] | None,
    image_files: list[dict[str, Any]],
) -> None:
    saved_by_name: dict[str, dict[str, Any]] = {}
    saved_images = review.get("images", []) if review is not None else []
    if not isinstance(saved_images, list):
        saved_images = []
    current_sources = {image["name"]: image["source_sha256"] for image in image_files}
    for saved in saved_images:
        if not isinstance(saved, dict):
            continue
        name = saved.get("name")
        valid_name = isinstance(name, str) and Path(name).name == name
        if isinstance(name, str) and valid_name:
            saved_by_name[name] = saved
        populated = bool(saved.get("notes") or saved.get("markup") or saved.get("marked") is not None)
        if not populated and isinstance(name, str) and valid_name:
            populated = (results / marked_filename(name)).is_file()
        if not populated:
            continue
        current_identity = current_sources.get(name) if isinstance(name, str) else None
        if current_identity is None or saved.get("source_sha256") != current_identity:
            affected = name if isinstance(name, str) and name else "an unnamed image"
            raise ReviewError(
                f"cannot resume populated review for {affected}: source identity is missing or changed; "
                "use a separate results folder"
            )

    current_names_by_stem = {Path(image["name"]).stem.casefold(): image["name"] for image in image_files}
    marked_suffix = ".marked.png"
    for artifact in results.iterdir():
        if not artifact.is_file() or not artifact.name.casefold().endswith(marked_suffix):
            continue
        stem = artifact.name[: -len(marked_suffix)].casefold()
        source_name = current_names_by_stem.get(stem)
        if source_name is None:
            raise ReviewError(
                f"cannot resume marked artifact {artifact.name}: source image is missing; use a separate results folder"
            )
        if source_name not in saved_by_name:
            raise ReviewError(
                f"cannot resume marked artifact for {source_name}: image metadata is missing; "
                "use a separate results folder"
            )


def set_terminal(results: Path, review: dict[str, Any], status: str) -> dict[str, Any]:
    if review.get("status") == "open":
        now = utc_now()
        review["status"] = status
        review["done"] = status == "done"
        review["updated_at"] = now
        review["finished_at"] = now
        write_json(results / "review.json", review)
    return review


def finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ReviewError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ReviewError(f"{name} must be a finite number")
    return result


def parse_color(value: Any) -> tuple[int, int, int, int]:
    if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value) is None:
        raise ReviewError("markup colors must use #rrggbb")
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), 255


def validate_group_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_GROUP_ID_LENGTH}}}", value) is None:
        raise ReviewError(f"group must be a 1–{MAX_GROUP_ID_LENGTH} character ASCII identifier")
    return value


def validate_markup(markup: Any) -> list[dict[str, Any]]:
    if not isinstance(markup, list) or len(markup) > MAX_MARKS:
        raise ReviewError(f"markup must be an array of at most {MAX_MARKS} marks")
    result: list[dict[str, Any]] = []
    for mark in markup:
        if not isinstance(mark, dict):
            raise ReviewError("each markup item must be an object")
        kind = mark.get("type")
        color = mark.get("color")
        parse_color(color)
        clean: dict[str, Any] = {"type": kind, "color": color}
        if "group" in mark:
            clean["group"] = validate_group_id(mark["group"])
        if kind in ("pen", "arrow", "rect"):
            width = finite_number(mark.get("width"), "width")
            if width <= 0:
                raise ReviewError("markup width must be positive")
            clean["width"] = width
        if kind == "pen":
            points = mark.get("points")
            if not isinstance(points, list) or len(points) > MAX_POINTS_PER_SEGMENT:
                raise ReviewError(f"each pen segment may contain at most {MAX_POINTS_PER_SEGMENT} points")
            clean_points: list[list[float]] = []
            for point in points:
                if not isinstance(point, list) or len(point) != 2:
                    raise ReviewError("each pen point must contain x and y")
                clean_points.append([finite_number(point[0], "x"), finite_number(point[1], "y")])
            clean["points"] = clean_points
        elif kind == "arrow":
            for field in ("x1", "y1", "x2", "y2"):
                clean[field] = finite_number(mark.get(field), field)
        elif kind == "rect":
            for field in ("x", "y", "w", "h"):
                clean[field] = finite_number(mark.get(field), field)
        elif kind == "text":
            for field in ("x", "y", "size"):
                clean[field] = finite_number(mark.get(field), field)
            if clean["size"] <= 0:
                raise ReviewError("text size must be positive")
            text = mark.get("text")
            if not isinstance(text, str) or len(text) > MAX_TEXT_CODEPOINTS:
                raise ReviewError("text markup must be a string of at most 240 code points")
            if len(text.split("\n")) > MAX_TEXT_LINES:
                raise ReviewError("text markup may contain at most 8 lines")
            clean["text"] = text
            mask = mark.get("mask")
            if not isinstance(mask, dict):
                raise ReviewError("text markup requires a raster mask")
            mask_width, mask_height = mask.get("width"), mask.get("height")
            if (
                isinstance(mask_width, bool)
                or not isinstance(mask_width, int)
                or isinstance(mask_height, bool)
                or not isinstance(mask_height, int)
            ):
                raise ReviewError("text mask dimensions must be integers")
            if mask_width < 1 or mask_height < 1:
                raise ReviewError("text mask dimensions must be positive")
            scale = finite_number(mask.get("scale"), "text mask scale")
            if scale <= 0:
                raise ReviewError("text mask scale must be positive")
            mask_data = mask.get("png")
            validate_text_mask_png(mask_data, mask_width, mask_height)
            clean["mask"] = {"width": mask_width, "height": mask_height, "scale": scale, "png": mask_data}
        else:
            raise ReviewError("markup type must be pen, arrow, rect, or text")
        result.append(clean)
    return result


def note_length_ok(value: str) -> bool:
    return len(value.encode("utf-16-le")) // 2 <= MAX_NOTES_UTF16


def validate_notes(notes: Any) -> str:
    if not isinstance(notes, str):
        raise ReviewError("notes must be a string")
    if not note_length_ok(notes):
        raise ReviewError("notes exceed 6,000 UTF-16 code units")
    return notes


def marked_filename(image_name: str) -> str:
    return Path(image_name).stem + ".marked.png"


def image_index(value: Any, count: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= count:
        raise ReviewError("image index is out of range")
    return value


def validate_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**53 - 1:
        raise ReviewError("revision must be a non-negative safe integer")
    return value


def validate_display_size(width: Any, height: Any) -> tuple[int, int]:
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ReviewError("width and height must be positive integers")
    return width, height


def validate_page_id(value: Any) -> str | None:
    """The page instance a request names, or None for a request that names none."""
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        raise ReviewError("page_id must be a string of 1 to 128 characters")
    return value


def final_snapshots(payload: dict[str, Any]) -> list[Any]:
    """The snapshots of an api/done or api/close body: a list, one bare snapshot, or none."""
    snapshots = payload.get("snapshots")
    if snapshots is None and "index" in payload:
        snapshots = [payload]
    if snapshots is None:
        snapshots = []
    if not isinstance(snapshots, list):
        raise ReviewError("snapshots must be an array")
    return snapshots


def apply_delta_snapshot(
    snapshot: dict[str, Any], current_image: dict[str, Any], current_revision: int
) -> tuple[dict[str, Any], int]:
    revision = validate_revision(snapshot.get("revision"))
    if revision <= current_revision:
        return current_image, current_revision
    notes = validate_notes(current_image.get("notes", ""))
    markup = validate_markup(current_image.get("markup", []))
    original_markup = copy.deepcopy(markup)
    notes_delta = snapshot.get("notes_delta", [])
    markup_delta = snapshot.get("markup_delta", [])
    if not isinstance(notes_delta, list) or not isinstance(markup_delta, list):
        raise ReviewError("notes_delta and markup_delta must be arrays")
    events: dict[int, tuple[str, dict[str, Any]]] = {}
    for item in notes_delta:
        if not isinstance(item, dict):
            raise ReviewError("each notes delta must be an object")
        patch_revision = validate_revision(item.get("revision"))
        if patch_revision <= current_revision:
            continue
        if patch_revision > revision or patch_revision in events:
            raise ReviewError("notes delta revisions must be unique and newer than stored state")
        start, delete, insert = item.get("start"), item.get("delete"), item.get("insert")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(delete, bool)
            or not isinstance(delete, int)
        ):
            raise ReviewError("note splice requires non-negative start/delete and string insert")
        if start < 0 or delete < 0 or not isinstance(insert, str):
            raise ReviewError("note splice requires non-negative start/delete and string insert")
        events[patch_revision] = ("note", {"start": start, "delete": delete, "insert": insert})
    for item in markup_delta:
        if not isinstance(item, dict):
            raise ReviewError("each markup delta must be an object")
        patch_revision = validate_revision(item.get("revision"))
        if patch_revision <= current_revision:
            continue
        if patch_revision > revision or patch_revision in events:
            raise ReviewError("markup delta revisions must be unique and newer than stored state")
        op = item.get("op")
        if op == "append":
            payload = {"op": op, "mark": validate_markup([item.get("mark")])[0]}
        elif op == "replace":
            idx = item.get("index")
            if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
                raise ReviewError("replace requires a non-negative mark index")
            payload = {"op": op, "index": idx, "mark": validate_markup([item.get("mark")])[0]}
        elif op == "undo_group":
            payload = {"op": op, "group": validate_group_id(item.get("group"))}
        elif op in ("undo", "clear"):
            payload = {"op": op}
        else:
            raise ReviewError("markup delta op must be append, replace, undo, undo_group, or clear")
        events[patch_revision] = ("markup", payload)
    if revision - current_revision != len(events):
        raise ReviewError("close deltas must contain every revision since the last acknowledged save")
    expected_revisions = sorted(events)
    if any(
        patch_revision != current_revision + offset for offset, patch_revision in enumerate(expected_revisions, start=1)
    ):
        raise ReviewError("close deltas must contain every revision since the last acknowledged save")
    for patch_revision in expected_revisions:
        kind, payload = events[patch_revision]
        if kind == "note":
            start, delete, insert = payload["start"], payload["delete"], payload["insert"]
            if start > len(notes) or delete > len(notes) - start:
                raise ReviewError("note splice is outside the current note string")
            notes = notes[:start] + insert + notes[start + delete :]
            validate_notes(notes)
        else:
            op = payload["op"]
            if op == "append":
                markup.append(payload["mark"])
            elif op == "replace":
                idx = payload["index"]
                if idx >= len(markup):
                    raise ReviewError("replace mark index is out of range")
                markup[idx] = payload["mark"]
            elif op == "undo_group":
                group = payload["group"]
                markup[:] = [mark for mark in markup if mark.get("group") != group]
            elif op == "undo":
                if markup:
                    markup.pop()
            else:
                markup.clear()
            if len(markup) > MAX_MARKS:
                raise ReviewError(f"markup exceeds {MAX_MARKS} marks")
    markup = validate_markup(markup)
    markup_revision = current_image.get("markup_revision", 0)
    if markup != original_markup:
        markup_revision = max(patch_revision for patch_revision, (kind, _payload) in events.items() if kind == "markup")
    return {**current_image, "notes": notes, "markup": markup, "markup_revision": markup_revision}, revision


_IDENTITY_CACHE: dict[str, tuple[tuple[int, ...], str]] = {}


def cached_source_sha256(path: Path) -> str:
    # The digest is reused while device, inode, size, mtime and ctime are unchanged.
    # A write updates ctime on POSIX. On Windows ctime is the creation time, so a
    # same-size write that restores the mtime is not detected there.
    try:
        info = path.stat()
    except OSError as exc:
        raise ReviewError(f"cannot read image {path.name}: {exc}") from exc
    key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    cached = _IDENTITY_CACHE.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    digest = source_sha256(path)
    _IDENTITY_CACHE[str(path)] = (key, digest)
    return digest


def _verify_source_identity(image_file: dict[str, Any]) -> None:
    name = image_file["name"]
    try:
        current_identity = cached_source_sha256(Path(image_file["path"]))
    except ReviewError as exc:
        raise ReviewError(f"source changed since open: {name}") from exc
    if current_identity != image_file.get("source_sha256"):
        raise ReviewError(f"source changed since open: {name}")


def _verify_snapshot_sources(image_files: list[dict[str, Any]], snapshots: list[Any]) -> None:
    checked: set[int] = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise ReviewError("each snapshot must be an object")
        index = image_index(snapshot.get("index"), len(image_files))
        if index in checked:
            continue
        checked.add(index)
        _verify_source_identity(image_files[index])


class ReviewState:
    """Owns synchronized review data, revisions, and the session timers."""

    def __init__(
        self,
        results: Path,
        review: dict[str, Any],
        image_files: list[dict[str, Any]],
        idle_timeout: float,
        close_timeout: float,
        session_id: str,
    ):
        self.results = results
        self.review = review
        self.image_files = image_files
        self.idle_timeout = idle_timeout
        self.close_timeout = close_timeout
        self.session_id = session_id
        self.revisions = [image.get("markup_revision", 0) for image in review["images"]]
        # Item index -> the (width, height) that api/prepare accepted in this session.
        self.display_sizes: dict[int, tuple[int, int]] = {}
        self.lock = threading.RLock()
        # Guards the counters and timer fields below. It is held only for short updates, so
        # api/ping and api/shutdown never wait behind a save that holds `lock`.
        self.lifecycle_lock = threading.Lock()
        self.inflight_requests = 0
        self.open_requests = 0
        self.terminalizing = False
        self.stop_event = threading.Event()
        self.shutdown_requested = False
        self.signal_seen = False
        self.last_edit_at = time.monotonic()
        self.page_connected = False
        self.page_visible = True
        self.last_visible_at = time.monotonic()
        self.close_requested_at: float | None = None
        # Set by the latest api/state that names a page; a page that reloads gets a new ID.
        self.current_page_id: str | None = None
        self.runtime: dict[str, Any] = {}
        self.token = ""
        self.url = ""
        self.httpd: ThreadingHTTPServer | None = None

    def persist_runtime(self) -> None:
        runtime = {
            "url": self.url,
            "token": self.token,
            "pid": os.getpid(),
            "session_id": self.session_id,
            "started_at": utc_now(),
        }
        write_json(self.results / RUNTIME_NAME, runtime)
        self.runtime = runtime

    @contextmanager
    def activity_request(self) -> Iterator[None]:
        with self.lifecycle_lock:
            if self.terminalizing:
                raise ReviewError("review session is no longer open")
            self.inflight_requests += 1
        try:
            yield
        finally:
            with self.lifecycle_lock:
                self.inflight_requests -= 1

    def _commit_image(self, index: int, fields: dict[str, Any]) -> None:
        """Write review.json with new values for one image entry. The caller holds `lock`."""
        images = list(self.review["images"])
        images[index] = {**images[index], **fields}
        candidate = {**self.review, "images": images, "updated_at": utc_now()}
        write_json(self.results / "review.json", candidate)
        self.review = candidate

    def prepare_image(self, index: Any, width: Any, height: Any) -> dict[str, Any]:
        """Accept the size the browser decoded as the image's display size; report its PNG freshness."""
        with self.activity_request():
            with self.lock:
                index = image_index(index, len(self.review["images"]))
                if self.review["status"] != "open":
                    raise ReviewError("review session is no longer open")
                image_file = self.image_files[index]
                if image_file["kind"] != "image":
                    raise ReviewError("video items have no markup to prepare")
                size = validate_display_size(width, height)
                raw_width, raw_height = image_file["width"], image_file["height"]
                if size not in ((raw_width, raw_height), (raw_height, raw_width)):
                    raise ReviewError(
                        f"display size {size[0]}×{size[1]} of {image_file['name']} does not match its file "
                        f"header ({raw_width}×{raw_height}, or {raw_height}×{raw_width} when rotated)"
                    )
                self.display_sizes[index] = size
                stale = marked_png_stale(self.results, self.review["images"][index])
                if self.review["images"][index].get("png_stale") is not stale:
                    self._commit_image(index, {"png_stale": stale})
                return {
                    "ready": True,
                    "width": size[0],
                    "height": size[1],
                    "revision": self.revisions[index],
                    "markup_revision": self.review["images"][index].get("markup_revision", 0),
                    "png_revision": self.review["images"][index].get("png_revision"),
                    "png_stale": stale,
                }

    def store_marked_png(self, index: int, revision: int, data: bytes) -> dict[str, Any]:
        """Store the page's rendering of the current markup as the image's marked PNG."""
        with self.activity_request():
            with self.lock:
                index = image_index(index, len(self.review["images"]))
                if self.review["status"] != "open":
                    raise ReviewError("review session is no longer open")
                image_file = self.image_files[index]
                if image_file["kind"] != "image":
                    raise ReviewError("video items have no marked PNG")
                display_size = self.display_sizes.get(index)
                if display_size is None:
                    raise ReviewError(f"prepare {image_file['name']} before uploading its marked PNG")
                _verify_source_identity(image_file)
                image = self.review["images"][index]
                current = image.get("markup_revision", 0)
                if revision != current:
                    raise StaleRevisionError(
                        f"marked PNG revision {revision} is not the current markup revision {current}"
                    )
                if not image.get("markup"):
                    raise ReviewError(f"{image_file['name']} has no markup to render")
                width, height = png_size(data)
                if (width, height) != display_size:
                    raise ReviewError(
                        f"marked PNG is {width}×{height}, but the display size of {image_file['name']} is "
                        f"{display_size[0]}×{display_size[1]}"
                    )
                marked_path = self.results / marked_filename(image["name"])
                # The PNG is in place before review.json names it, so review.json never calls a PNG fresh
                # that a crash left missing or older.
                atomic_write(marked_path, with_markup_revision(data, revision))
                fields = {"marked": marked_path.name, "png_revision": revision, "png_stale": False}
                self._commit_image(index, {**fields, "updated_at": utc_now()})
                return fields

    def _prepare_snapshot(
        self, snapshot: Any, candidate_review: dict[str, Any], revisions: list[int], patches: bool
    ) -> tuple[int, int, dict[str, Any], bool, bool]:
        """Validate one snapshot: (index, revision, updated entry, delete the marked PNG, ignored)."""
        if not isinstance(snapshot, dict):
            raise ReviewError("each snapshot must be an object")
        if snapshot.get("png") is not None:
            raise ReviewError("client-supplied marked PNGs are not accepted")
        index = image_index(snapshot.get("index"), len(candidate_review["images"]))
        current_revision = revisions[index]
        revision = validate_revision(snapshot.get("revision"))
        image = candidate_review["images"][index]
        if revision <= current_revision:
            return index, current_revision, image, False, True
        if patches and ("notes_delta" in snapshot or "markup_delta" in snapshot):
            updated, revision = apply_delta_snapshot(snapshot, image, current_revision)
        else:
            updated = dict(image)
            updated["notes"] = validate_notes(snapshot.get("notes"))
            updated["markup"] = validate_markup(snapshot.get("markup"))
            if updated["markup"] != image.get("markup", []):
                updated["markup_revision"] = revision
        updated["updated_at"] = utc_now()
        markup = updated["markup"]
        if markup and self.image_files[index]["kind"] == "video":
            raise ReviewError("video items do not accept markup")
        markup_revision = updated.get("markup_revision", 0)
        if isinstance(markup_revision, bool) or not isinstance(markup_revision, int) or markup_revision < 0:
            raise ReviewError("markup_revision must be a non-negative integer")
        delete_png = False
        if markup != image.get("markup", []):
            if index not in self.display_sizes:
                raise ReviewError(f"prepare {updated['name']} before saving its markup")
            if markup:
                updated["png_stale"] = True
            else:
                delete_png = True
                updated.update({"marked": None, "png_revision": None, "png_stale": False})
        return index, revision, updated, delete_png, False

    def _commit_snapshots(self, snapshots: list[Any], patches: bool) -> tuple[int, list[dict[str, Any]]]:
        if len(snapshots) > len(self.review["images"]):
            raise ReviewError("too many snapshots in one request")
        with self.activity_request():
            with self.lock:
                if self.review["status"] != "open":
                    raise ReviewError("review session is no longer open")
                _verify_snapshot_sources(self.image_files, snapshots)
                candidate = copy.deepcopy(self.review)
                revisions = list(self.revisions)
                prepared: list[tuple[int, int, dict[str, Any], bool, bool]] = []
                touched: set[int] = set()
                for snapshot in snapshots:
                    item = self._prepare_snapshot(snapshot, candidate, revisions, patches)
                    index, revision, updated, _delete_png, ignored = item
                    if index in touched:
                        raise ReviewError("a request may contain only one snapshot per image")
                    touched.add(index)
                    if not ignored:
                        candidate["images"][index] = updated
                        revisions[index] = revision
                    prepared.append(item)
                edited = any(not item[4] for item in prepared)
                if edited:
                    candidate["updated_at"] = utc_now()
                # Deleting first means a failed delete commits nothing, and the page retries the save.
                for _index, _revision, updated, delete_png, _ignored in prepared:
                    if delete_png:
                        remove_locked(self.results / marked_filename(updated["name"]))
                if edited:
                    write_json(self.results / "review.json", candidate)
                    self.review = candidate
                    self.revisions = revisions
                result = (
                    len([item for item in prepared if not item[4]]),
                    [
                        {
                            "index": item[0],
                            "revision": self.revisions[item[0]],
                            "markup_revision": self.review["images"][item[0]].get("markup_revision", 0),
                            "saved": True,
                            "ignored": item[4],
                        }
                        for item in prepared
                    ],
                )
            if edited:
                with self.lifecycle_lock:
                    if not self.terminalizing:
                        self.last_edit_at = time.monotonic()
            return result

    def save_one(self, payload: dict[str, Any]) -> dict[str, Any]:
        _count, result = self._commit_snapshots([payload], patches=True)
        return {
            "saved": True,
            "revision": result[0]["revision"],
            "markup_revision": result[0]["markup_revision"],
            "updated_at": self.review["images"][result[0]["index"]]["updated_at"],
            "ignored": result[0]["ignored"],
        }

    def close_snapshots(self, payload: dict[str, Any], terminal: str | None) -> dict[str, Any]:
        """Apply the final snapshots, then finish with `terminal`, or start the close grace for None.

        Only the current page's close starts the grace, and it does so even when its snapshots
        fail: the page is unloading, so its rejected edits are lost either way.
        """
        page_id = validate_page_id(payload.get("page_id")) if terminal is None else None
        # The state lock orders this close against a reload's api/state, which cancels it.
        with self.activity_request(), self.lock:
            with self.lifecycle_lock:
                closing = terminal is None and self._is_current_page(page_id)
            try:
                applied = self._apply_final_snapshots(final_snapshots(payload))
            finally:
                if closing and self.review["status"] == "open":
                    self.request_close()
            if terminal is None:
                return {"status": "closed" if closing else "open", "revisions": applied}
            self.finish(terminal)
            return {"status": terminal, "revisions": applied}

    def _apply_final_snapshots(self, snapshots: list[Any]) -> list[dict[str, Any]]:
        _count, applied = self._commit_snapshots(snapshots, patches=True)
        applied_by_index = {item["index"]: item for item in applied}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise ReviewError("each snapshot must be an object")
            index = image_index(snapshot.get("index"), len(self.review["images"]))
            requested_revision = validate_revision(snapshot.get("revision"))
            if applied_by_index[index]["revision"] < requested_revision:
                raise ReviewError("close snapshot revision was not committed")
            applied_by_index[index]["saved"] = True
            applied_by_index[index]["ignored"] = False
        return applied

    def state_value(self) -> dict[str, Any]:
        with self.lock:
            value = copy.deepcopy(self.review)
            value["heartbeat_interval_ms"] = max(1, int(min(1.0, self.close_timeout / 3) * 1000))
            for index, image in enumerate(value["images"]):
                metadata = self.image_files[index]
                image.update(
                    {
                        "kind": metadata["kind"],
                        "width": metadata["width"],
                        "height": metadata["height"],
                        "url": f"media/{index}",
                        "revision": self.revisions[index],
                    }
                )
            return value

    @contextmanager
    def handling_request(self) -> Iterator[None]:
        with self.lifecycle_lock:
            self.open_requests += 1
        try:
            yield
        finally:
            with self.lifecycle_lock:
                self.open_requests -= 1

    def wait_for_requests(self, timeout: float) -> None:
        """Let handlers finish their responses before the process exits, up to timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lifecycle_lock:
                if not self.open_requests:
                    return
            time.sleep(0.01)

    def load_page(self, page_id: str | None = None) -> None:
        """Record a page load (GET of the page or of api/state); a reload cancels a pending close."""
        with self.lifecycle_lock:
            if page_id is not None:
                self.current_page_id = page_id
            self.page_connected = True
            self.page_visible = True
            self.last_visible_at = time.monotonic()
            self.close_requested_at = None

    def load_state(self, page_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            self.load_page(page_id)
            return self.state_value()

    def _is_current_page(self, page_id: str | None) -> bool:
        """Callers hold lifecycle_lock. A request that names no page acts for the current page."""
        return page_id is None or self.current_page_id is None or page_id == self.current_page_id

    def touch_page(self) -> None:
        """Record a page request that is neither a load nor a heartbeat; ping and shutdown are not page requests."""
        with self.lifecycle_lock:
            self.page_connected = True
            if self.page_visible:
                self.last_visible_at = time.monotonic()

    def heartbeat(self, visible: bool, page_id: str | None = None) -> None:
        """Only the current page's heartbeat changes visibility; while hidden, the close timer does not count."""
        with self.lifecycle_lock:
            if not self._is_current_page(page_id):
                return
            self.page_connected = True
            self.page_visible = visible
            if visible:
                self.last_visible_at = time.monotonic()

    def request_close(self) -> None:
        with self.lifecycle_lock:
            self.close_requested_at = time.monotonic()

    def request_shutdown(self) -> None:
        self.shutdown_requested = True

    def due_terminal_status(self) -> str | None:
        """The status a timer ends the session with now; None while a request is in progress."""
        with self.lifecycle_lock:
            if self.terminalizing or self.inflight_requests:
                return None
            now = time.monotonic()
            grace = min(CLOSE_GRACE_SECONDS, self.close_timeout)
            if self.close_requested_at is not None and now - self.close_requested_at >= grace:
                return "closed"
            if self.page_connected and self.page_visible and now - self.last_visible_at > self.close_timeout:
                return "closed"
            if now - self.last_edit_at > self.idle_timeout:
                return "timeout"
            return None

    def finish(self, status: str) -> None:
        with self.lifecycle_lock:
            if self.terminalizing:
                return
            self.terminalizing = True
        with self.lock:
            set_terminal(self.results, self.review, status)
            self.stop_event.set()


class ReviewHandler(BaseHTTPRequestHandler):
    """Serve local review requests under the loopback session's origin policy."""

    server_version = "visual-review/1"
    sys_version = ""

    @property
    def state(self) -> ReviewState:
        return self.server.review_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8")

    def _path(self) -> str | None:
        parsed = urlsplit(self.path)
        prefix = f"/{self.state.token}/"
        if not parsed.path.startswith(prefix):
            return None
        return unquote(parsed.path[len(prefix) :])

    def _check_origin(self) -> bool:
        host = f"127.0.0.1:{self.server.server_address[1]}"
        if self.headers.get("Host") != host:
            self._json(403, {"error": "invalid host"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            self._json(403, {"error": "origin is not allowed"})
            return False
        return True

    def _read_body(self) -> bytes | None:
        """The request body of exactly Content-Length bytes; None once an error response is sent."""
        length_value = self.headers.get("Content-Length")
        if length_value is None:
            self._json(411, {"error": "Content-Length is required"})
            return None
        try:
            length = int(length_value)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if self.headers.get("Transfer-Encoding"):
            self._json(400, {"error": "chunked request bodies are not supported"})
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._json(400, {"error": "request body is incomplete"})
            return None
        return raw

    def _read_json(self) -> dict[str, Any] | None:
        raw = self._read_body()
        if raw is None:
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            self._json(400, {"error": "request body must be UTF-8 JSON"})
            return None
        if not isinstance(value, dict):
            self._json(400, {"error": "request body must be a JSON object"})
            return None
        return value

    def _query_revision(self) -> int:
        values = parse_qs(urlsplit(self.path).query).get("revision", [])
        if len(values) != 1 or re.fullmatch(r"[0-9]{1,16}", values[0]) is None:
            raise ReviewError("revision must be a non-negative safe integer")
        return validate_revision(int(values[0]))

    def do_GET(self) -> None:
        if not self._check_origin():
            return
        path = self._path()
        if path is None:
            self._json(404, {"error": "not found"})
            return
        if path == "api/ping":
            self._json(200, {"alive": True, "session_id": self.state.session_id})
            return
        with self.state.handling_request():
            if path not in ("", "api/state"):
                self.state.touch_page()
            self._get_page_route(path)

    def _get_page_route(self, path: str) -> None:
        if path == "":
            self.state.load_page()
            page_path = Path(__file__).with_name("index.html")
            try:
                page = page_path.read_text(encoding="utf-8")
            except OSError:
                self._json(500, {"error": "review page is unavailable"})
                return
            title = f"Visual review · {self.state.session_id}"
            title_tag = f"<title>{html.escape(title)}</title>"
            if re.search(r"<title\b[^>]*>.*?</title\s*>", page, flags=re.IGNORECASE | re.DOTALL):
                page = re.sub(
                    r"<title\b[^>]*>.*?</title\s*>", title_tag, page, count=1, flags=re.IGNORECASE | re.DOTALL
                )
            else:
                page = re.sub(r"(<head\b[^>]*>)", r"\1" + title_tag, page, count=1, flags=re.IGNORECASE)
            csp = (
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'"
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
            return
        if path == "api/state":
            try:
                page_id = validate_page_id(parse_qs(urlsplit(self.path).query).get("page_id", [None])[-1])
            except ReviewError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, self.state.load_state(page_id))
            return
        match = re.fullmatch(r"media/([0-9]{1,9})", path)
        if match and int(match.group(1)) < len(self.state.image_files):
            self._send_media(self.state.image_files[int(match.group(1))])
            return
        self._json(404, {"error": "not found"})

    def _send_media(self, item: dict[str, Any]) -> None:
        # Seeking and looping need byte ranges. A Range header this handler cannot
        # parse (for example several ranges) is ignored, and the whole file is sent.
        try:
            source = Path(item["path"]).open("rb")
        except OSError:
            self._json(404, {"error": "media file is unavailable"})
            return
        with source:
            info = os.fstat(source.fileno())
            size = info.st_size
            start, end, status = 0, size - 1, 200
            requested = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if requested and (requested.group(1) or requested.group(2)):
                if requested.group(1):
                    start = int(requested.group(1))
                    end = min(int(requested.group(2)), size - 1) if requested.group(2) else size - 1
                else:
                    start = max(0, size - int(requested.group(2)))
                if start > end or start >= size:
                    self._send(416, b"", item["mime"], {"Content-Range": f"bytes */{size}"})
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", item["mime"])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            # The session URL is unique. A changed source fails every save, so
            # bytes cached from the open file are never wrong for review.
            self.send_header("Cache-Control", "private, max-age=31536000, immutable")
            # From the open file's stat: free to compute, and it matches the bytes sent.
            self.send_header("ETag", f'"{info.st_size:x}-{info.st_mtime_ns:x}-{info.st_ctime_ns:x}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()
            source.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = source.read(min(MEDIA_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_POST(self) -> None:
        if not self._check_origin():
            return
        path = self._path()
        if path is None or not path.startswith("api/"):
            self._json(404, {"error": "not found"})
            return
        with self.state.handling_request():
            # api/marked/<n> carries PNG bytes; every other route carries a JSON object.
            upload = re.fullmatch(r"api/marked/([0-9]{1,9})", path)
            payload = self._read_body() if upload else self._read_json()
            if payload is None:
                return
            if path == "api/shutdown":
                # The monitor loop records the status, so this never waits for the state lock.
                self.state.request_shutdown()
                self._json(200, {"status": "interrupted"})
                return
            self._post_page_route(path, payload, int(upload.group(1)) if upload else None)

    def _post_page_route(self, path: str, payload: Any, upload_index: int | None) -> None:
        try:
            if path == "api/heartbeat":
                visible = payload.get("visible", True)
                if not isinstance(visible, bool):
                    raise ReviewError("visible must be true or false")
                self.state.heartbeat(visible, validate_page_id(payload.get("page_id")))
                result: Any = {"alive": True}
            else:
                self.state.touch_page()
                if upload_index is not None or path in ("api/prepare", "api/save"):
                    try:
                        if upload_index is not None:
                            result = self.state.store_marked_png(upload_index, self._query_revision(), payload)
                        elif path == "api/prepare":
                            result = self.state.prepare_image(
                                payload.get("index"), payload.get("width"), payload.get("height")
                            )
                        else:
                            result = self.state.save_one(payload)
                    finally:
                        # The close timer restarts when a long request ends, not when it began.
                        self.state.touch_page()
                elif path in ("api/done", "api/close"):
                    result = self.state.close_snapshots(payload, "done" if path == "api/done" else None)
                else:
                    self._json(404, {"error": "not found"})
                    return
        except StaleRevisionError as exc:
            self._json(409, {"error": str(exc)})
            return
        except ReviewError as exc:
            self._json(400, {"error": str(exc)})
            return
        except (OSError, ValueError) as exc:
            self._json(500, {"error": str(exc) or "internal server error"})
            return
        self._json(200, result)


class ReviewHTTPServer(ThreadingHTTPServer):
    """Bind the review HTTP handler exclusively to loopback."""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, state: ReviewState):
        super().__init__(("127.0.0.1", 0), ReviewHandler)
        self.review_state = state


def release_standard_streams() -> None:
    """Point stdout and stderr at the null device; Windows cannot delete a log the process holds open."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    try:
        null = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        for fd in (1, 2):
            try:
                os.dup2(null, fd)
            except OSError:
                pass
    finally:
        os.close(null)


def serve_process(
    results: Path,
    session_id: str,
    image_dir: Path,
    idle_timeout: float,
    close_timeout: float,
) -> None:
    review = load_review(results)
    if review is None or review.get("session_id") != session_id or review.get("status") != "open":
        raise ReviewError("review session was not initialized")
    image_files = []
    for image in review["images"]:
        source = image_dir / image["name"]
        if not source.is_file() or source.is_symlink():
            raise ReviewError(f"review image disappeared: {image['name']}")
        image_files.append(
            {
                "name": image["name"],
                "path": str(source),
                "mime": item_mime(source),
                "kind": image.get("kind", "image"),
                "source_sha256": image.get("source_sha256"),
                "width": image["width"],
                "height": image["height"],
            }
        )
    state = ReviewState(results, review, image_files, idle_timeout, close_timeout, session_id)
    state.token = new_token()
    state.httpd = ReviewHTTPServer(state)
    state.url = f"http://127.0.0.1:{state.httpd.server_address[1]}/{state.token}/"
    server_thread = threading.Thread(
        target=state.httpd.serve_forever,
        kwargs={"poll_interval": MONITOR_SECONDS},
        name="visual-review-http",
        daemon=True,
    )

    def request_interrupt(_signum: int, _frame: Any) -> None:
        state.signal_seen = True

    # Only POSIX delivers these to a detached server; on Windows, api/shutdown is the clean stop.
    previous_handlers = {signum: signal.signal(signum, request_interrupt) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        server_thread.start()
        state.persist_runtime()
        while not state.stop_event.wait(MONITOR_SECONDS):
            if state.signal_seen or state.shutdown_requested:
                state.finish("interrupted")
                break
            status = state.due_terminal_status()
            if status is not None:
                state.finish(status)
                break
    except BaseException:
        state.finish("interrupted")
        raise
    finally:
        if server_thread.is_alive():
            state.httpd.shutdown()
        state.httpd.server_close()
        server_thread.join(timeout=2)
        state.wait_for_requests(REQUEST_DRAIN_SECONDS)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        remove_known_temps(results, os.getpid())
        # A server that never published runtime.json leaves its log for `open` to report.
        if state.runtime:
            release_standard_streams()
            shutil.rmtree(transient_directory(results, session_id), ignore_errors=True)
            # Last: once runtime.json is gone, nothing of this session remains but the exit.
            remove_runtime(results, session_id)


def session_response(results: Path, runtime: dict[str, Any], *, browser: bool, reused: bool) -> dict[str, Any]:
    return {
        "status": "open",
        "url": runtime["url"],
        "pid": runtime["pid"],
        "results": str(results),
        "session_id": runtime["session_id"],
        "browser": browser,
        "reused": reused,
    }


def server_log_tail(log_path: Path) -> str:
    try:
        with log_path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - MAX_STARTUP_DIAGNOSTIC_BYTES))
            data = log.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "".join(char for char in text if char in "\n\t" or char.isprintable()).strip()


def stop_process(process: subprocess.Popen) -> bool:
    """Stop a server this process started, through its Popen handle; False if it outlives both stops."""
    for stop in (process.terminate, process.kill):
        try:
            stop()
        except OSError:
            pass
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            continue
    return False


def start_review_server(
    args: argparse.Namespace,
    results: Path,
    image_dir: Path,
    image_files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Start a detached server for a new session and return its runtime.json fields once it answers."""
    review = initial_review(results, image_files, args.question)
    write_json(results / "review.json", review)
    session_id = review["session_id"]
    transient = transient_directory(results, session_id)
    transient.mkdir(mode=0o700)
    log_path = transient / "server.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "__serve",
        "--results",
        str(results),
        "--images",
        str(image_dir),
        "--session-id",
        session_id,
        "--idle-timeout",
        str(args.idle_timeout),
        "--close-timeout",
        str(args.close_timeout),
    ]
    try:
        process = spawn_server(command, log_path)
    except OSError as exc:
        recover_dead_session(results, load_review(results))
        raise ReviewError(f"could not start review server: {exc}") from exc
    deadline = time.monotonic() + STARTUP_SECONDS
    while True:
        raw = read_runtime_bytes(results)
        if raw is not None:
            runtime = parse_runtime(raw)
            if runtime.get("session_id") == session_id and server_alive(runtime):
                return runtime
        code = process.poll()
        if code is not None:
            failure = f"review server failed to start (exit code {code})"
            break
        if time.monotonic() >= deadline:
            if not stop_process(process):
                raise ReviewError("review server remained alive after startup timeout; its ownership was retained")
            failure = f"review server did not become ready within {STARTUP_SECONDS} seconds"
            break
        time.sleep(0.05)
    output = server_log_tail(log_path)
    recover_dead_session(results, load_review(results))
    raise ReviewError(f"{failure}; server output: {output}" if output else failure)


def open_command(args: argparse.Namespace) -> dict[str, Any]:
    image_dir = Path(args.images).expanduser().resolve(strict=True)
    if not image_dir.is_dir():
        raise ReviewError("images path must be a directory")
    results_candidate = Path(args.results).expanduser().resolve()
    if results_candidate == image_dir or image_dir in results_candidate.parents:
        raise ReviewError("results path must not be inside the images directory")
    results = secure_results(args.results)
    while True:
        with session_lock(results):
            review = load_review(results)
            owner_raw = read_runtime_bytes(results)
            owner = live_owner(owner_raw)
            if owner is None:
                image_dir, image_files = enumerate_images(args.images)
                if results_candidate == image_dir or image_dir in results_candidate.parents:
                    raise ReviewError("results path must not be inside the images directory")
                review = load_review(results)
                owner_raw = read_runtime_bytes(results)
                owner = live_owner(owner_raw)
            if owner is None:
                validate_resume_sources(results, review, image_files)
                recover_dead_session(results, review)
                runtime = start_review_server(args, results, image_dir, image_files)
                break
            if (
                review is not None
                and review.get("status") == "open"
                and review.get("session_id") == owner.get("session_id")
            ):
                return session_response(results, owner, browser=False, reused=True)
        # A server that is ending its session owns the folder until it releases runtime.json.
        wait_for_owner_exit(results, owner_raw)
    browser = False if args.no_browser else open_in_browser(runtime["url"])
    return session_response(results, runtime, browser=browser, reused=False)


def marked_png_stale(results: Path, image: dict[str, Any]) -> bool:
    """False when the entry needs no marked PNG and names none, or names a PNG that carries markup_revision."""
    marked = image.get("marked")
    if not image.get("markup", []):
        return marked is not None
    markup_revision = image.get("markup_revision")
    if (
        not isinstance(marked, str)
        or Path(marked).name != marked
        or isinstance(markup_revision, bool)
        or not isinstance(markup_revision, int)
        or image.get("png_revision") != markup_revision
    ):
        return True
    with io_lock(results):
        return read_png_revision(results / marked) != markup_revision


def reconcile_freshness(results: Path, review: dict[str, Any], *, persist: bool = False) -> dict[str, Any]:
    changed = False
    for image in review.get("images", []):
        if not isinstance(image, dict):
            continue
        stale = marked_png_stale(results, image)
        if image.get("png_stale") is not stale:
            image["png_stale"] = stale
            changed = True
    if changed:
        review["updated_at"] = utc_now()
        if persist:
            write_json(results / "review.json", review)
    return review


def status_command(results: Path) -> dict[str, Any]:
    review = load_review(results)
    if review is None:
        raise ReviewError("no review results exist")
    return review


def results_command(results: Path) -> dict[str, Any]:
    with session_lock(results):
        review = load_review(results)
        alive = live_runtime(results) is not None
        if not alive:
            review = recover_dead_session(results, review)
        if review is None:
            raise ReviewError("no review results exist")
        return reconcile_freshness(results, review, persist=not alive)


def terminal_result(results: Path, review: dict[str, Any]) -> tuple[str, int]:
    status = str(reconcile_freshness(results, review, persist=True).get("status"))
    return status, TERMINAL_CODES.get(status, TERMINAL_CODES["killed"])


def wait_command(results: Path) -> tuple[str, int]:
    watched: bytes | None = None
    failed_pings = 0
    next_ping = 0.0
    while True:
        if load_review(results) is None:
            raise ReviewError("no review results exist")
        raw = read_runtime_bytes(results)
        if raw is None:
            with session_lock(results):
                review = load_review(results)
                if review is None:
                    raise ReviewError("no review results exist")
                if read_runtime_bytes(results) is None:
                    if review.get("status") == "open":
                        recover_dead_session(results, review)
                    return terminal_result(results, review)
            continue
        if raw != watched:
            watched, failed_pings = raw, 0
        now = time.monotonic()
        if now >= next_ping:
            next_ping = now + PING_INTERVAL_SECONDS
            failed_pings = 0 if server_alive(parse_runtime(raw)) else failed_pings + 1
            if failed_pings >= DEAD_AFTER_FAILED_PINGS:
                with session_lock(results):
                    if read_runtime_bytes(results) == raw and not server_alive(parse_runtime(raw)):
                        review = recover_dead_session(results, load_review(results))
                        if review is None:
                            raise ReviewError("no review results exist")
                        return terminal_result(results, review)
                failed_pings = 0
        time.sleep(WAIT_POLL_SECONDS)


def cleanup_command(results: Path) -> dict[str, Any]:
    with session_lock(results):
        runtime = live_runtime(results)
        if runtime is not None:
            runtime_call(runtime, "api/shutdown", {})
            deadline = time.monotonic() + CLEANUP_STOP_SECONDS
            while read_runtime_bytes(results) is not None or server_alive(runtime):
                if time.monotonic() >= deadline:
                    raise ReviewError("could not stop the owned review server; runtime ownership was retained")
                time.sleep(0.05)
        review = recover_dead_session(results, load_review(results))
        return {"status": review.get("status", "closed") if review else "closed"}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="review.py", description="Run and manage a private local image review session.")
    commands = root.add_subparsers(dest="command", required=True)
    opener = commands.add_parser("open", help="start or resume a review session")
    opener.add_argument("--images", required=True, metavar="DIR")
    opener.add_argument("--results", required=True, metavar="DIR")
    opener.add_argument("--question", required=True, metavar="TEXT")
    opener.add_argument("--idle-timeout", type=float, default=1800, metavar="SECONDS")
    opener.add_argument("--close-timeout", type=float, default=30, metavar="SECONDS")
    opener.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser tab")
    for name, help_text in (
        ("status", "print persisted review state"),
        ("results", "emit persisted review.json"),
        ("wait", "wait for a terminal review status"),
        ("cleanup", "stop an owned server or recover stale artifacts"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--results", required=True, metavar="DIR")
    return root


def private_parser(argv: list[str]) -> argparse.Namespace:
    parser_private = argparse.ArgumentParser(add_help=False)
    sub = parser_private.add_subparsers(dest="internal", required=True)
    server = sub.add_parser("__serve")
    server.add_argument("--results", required=True)
    server.add_argument("--images", required=True)
    server.add_argument("--session-id", required=True)
    server.add_argument("--idle-timeout", required=True, type=float)
    server.add_argument("--close-timeout", required=True, type=float)
    return parser_private.parse_args(argv)


def use_utf8_output() -> None:
    """Encode stdout and stderr as UTF-8 whatever the locale; Windows pipes otherwise get the ANSI code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # A lone surrogate from an undecodable POSIX file name becomes a valid JSON escape.
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["__open-url"]:
        return open_url_helper(args_list[1]) if len(args_list) == 2 else 2
    try:
        if args_list[:1] == ["__serve"]:
            internal = private_parser(args_list)
            results = secure_results(internal.results)
            serve_process(
                results,
                internal.session_id,
                Path(internal.images).resolve(),
                internal.idle_timeout,
                internal.close_timeout,
            )
            return 0
        parsed = parser().parse_args(args_list)
        if parsed.command == "open":
            if (
                not math.isfinite(parsed.idle_timeout)
                or parsed.idle_timeout <= 0
                or not math.isfinite(parsed.close_timeout)
                or parsed.close_timeout <= 0
            ):
                raise ReviewError("idle and close timeouts must be positive finite numbers")
            value = open_command(parsed)
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
            return 0
        results = secure_results(parsed.results)
        if parsed.command in ("status", "results"):
            value = status_command(results) if parsed.command == "status" else results_command(results)
            sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            return 0
        if parsed.command == "wait":
            status, code = wait_command(results)
            sys.stdout.write(status + "\n")
            return code
        if parsed.command == "cleanup":
            value = cleanup_command(results)
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
            return 0
        raise ReviewError("unknown command")
    except ReviewError as exc:
        sys.stdout.write(json.dumps({"error": str(exc)}, ensure_ascii=True) + "\n")
        return 2
    except KeyboardInterrupt:
        sys.stdout.write(json.dumps({"error": "interrupted"}) + "\n")
        return 2
    except OSError as exc:
        sys.stdout.write(json.dumps({"error": str(exc)}, ensure_ascii=True) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
