#!/usr/bin/env python3
"""Snow Cell host runner for the "Windows 2025 Server TEMP RDP" workflow.

Lifecycle (default command, no arguments):
  1. validate configuration (no secrets are ever logged)
  2. download the existing ``snowcell.zip`` from the private Cloudflare R2 bucket
  3. SHA-256 the download, validate the archive and extract it safely
  4. install the Python dependencies (same interpreter that runs the bots)
  5. launch the five bots concurrently with a sanitized environment
  6. keep them running against ONE shared monotonic deadline (default 355 min)
  7. stop the bots and confirm that every process is gone
  8. rebuild ``snowcell.zip``, validate it, SHA-256 compare it with the download
  9. if it changed: delete the old object, immediately upload the replacement to
     the SAME key, verify it, and restore the original if the upload fails

Other commands:
  --check-config   offline validation of the environment (used as a fast preflight)
  --recover        idempotent crash recovery / publish, meant for an ``if: always()``
                   workflow step after the main step was cancelled, timed out or died

Only the Python standard library is required, except ``boto3`` for R2 access.
The exact S3 key ``snowcell.zip`` in bucket ``snowcell`` is hard-wired as a guard:
this script refuses to run against any other bucket or key.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime as dt
import enum
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #
EXPECTED_BUCKET = "snowcell"
EXPECTED_KEY = "snowcell.zip"
BOT_NAMES: Tuple[str, ...] = (
    "SnowTempbot",
    "SnowMaxbot",
    "SnowKeeperbot",
    "SnowiOSbot",
    "SnowCellbot",
)
PIP_REQUIREMENTS: List[str] = [
    "Telethon==1.44.0",
    "pytz",
    "phonenumbers",
    "aiofiles",
    "aiosqlite",
    "boto3",
]
DEFAULT_RUNTIME_MINUTES = 355
MAX_RUNTIME_MINUTES = 355
JOB_TIMEOUT_MINUTES_DEFAULT = 359
# GitHub force-terminates cancelled jobs/steps after this long
# (docs: "workflow cancellation", step 5).
CANCEL_WINDOW_SECONDS = 300
STATE_SCHEMA = 1


class Exit(enum.IntEnum):
    OK = 0
    UNEXPECTED = 1
    CONFIG = 10
    R2_AUTH = 11
    R2_MISSING = 12
    ARCHIVE_INVALID = 13
    DEPS = 14
    BOT_START = 15
    BOT_CRASH = 16
    SHUTDOWN = 17
    ZIP_BUILD = 18
    HASH = 19
    R2_DELETE = 20
    R2_UPLOAD_RESTORED = 21
    R2_UPLOAD_LOST = 22
    VERIFY = 23
    NO_TIME = 24
    CONFLICT = 25
    R2_TRANSFER = 26
    INTERRUPTED = 130


class StageError(Exception):
    """A lifecycle stage failed; ``code`` becomes the process exit code."""

    def __init__(self, code: Exit, message: str, r2_code: str = "", http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.r2_code = r2_code  # S3 error code of the failed request, when there was one
        self.http_status = http_status


# --------------------------------------------------------------------------- #
# Logging (thread-safe, redacting, GitHub-annotation aware)                   #
# --------------------------------------------------------------------------- #
_PRINT_LOCK = threading.Lock()
_SECRETS: List[str] = []


def register_secret(value: Optional[str]) -> None:
    """Remember a sensitive value so it is masked in every log line."""
    if value and len(value) >= 6 and value not in _SECRETS:
        _SECRETS.append(value)
        _SECRETS.sort(key=len, reverse=True)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def _gh_escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit(line: str) -> None:
    line = redact(line)
    with _PRINT_LOCK:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")


def info(msg: str) -> None:
    _emit(f"[{_stamp()}] {msg}")


def notice(msg: str) -> None:
    _emit(f"::notice::{_gh_escape(msg)}")


def warn(msg: str) -> None:
    _emit(f"::warning::{_gh_escape(msg)}")


def error(msg: str) -> None:
    _emit(f"::error::{_gh_escape(msg)}")


@contextlib.contextmanager
def group(title: str) -> Iterator[None]:
    _emit(f"::group::{title}")
    try:
        yield
    finally:
        _emit("::endgroup::")


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_minutes(seconds: float) -> str:
    return f"{seconds / 60.0:.1f} min"


def fmt_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if num < 1024 or unit == "GiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} GiB"


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
_ACCOUNT_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_ENDPOINT_HOST_RE = re.compile(
    r"^(?P<acct>[0-9a-f]{32})(?:\.(?P<jur>[a-z0-9-]+))?\.r2\.cloudflarestorage\.com$"
)


@dataclasses.dataclass(frozen=True)
class Config:
    endpoint: str
    access_key_id: str
    secret_access_key: str
    account_id: str
    bucket: str
    key: str
    jurisdiction: str
    runtime_seconds: int
    deadline_mode: str  # "bot-start" (as requested) | "job-anchored" (opt-in)
    grace_seconds: int
    heartbeat_seconds: int
    failure_policy: str  # "continue" | "abort"
    replace_policy: str  # "bytes" (default, literal: any SHA-256 difference replaces) | "content" (opt-in)
    launch_mode: str  # "wrapper" | "direct"
    job_start_epoch: Optional[float]
    job_timeout_seconds: int
    post_reserve_seconds: int
    publish_safety_seconds: int
    rescue_seconds: int  # how long a MISSING object is retried before giving up (bounded by job time)
    crash_tail_lines: int
    max_unpacked_bytes: int
    bot_root: Path
    work_dir: Path
    log_dir: Path
    app_status_file: Optional[Path]
    python: str

    @property
    def job_deadline_epoch(self) -> Optional[float]:
        if self.job_start_epoch is None:
            return None
        return self.job_start_epoch + self.job_timeout_seconds

    def describe(self) -> Dict[str, object]:
        """Non-sensitive summary for the log (never includes endpoint/keys)."""
        return {
            "bucket": self.bucket,
            "key": self.key,
            "jurisdiction": self.jurisdiction,
            "runtime_minutes": round(self.runtime_seconds / 60.0, 2),
            "deadline_mode": self.deadline_mode,
            "grace_seconds": self.grace_seconds,
            "failure_policy": self.failure_policy,
            "replace_policy": self.replace_policy,
            "launch_mode": self.launch_mode,
            "bot_root": str(self.bot_root),
            "log_dir": str(self.log_dir),
        }


def validate_endpoint(endpoint: str, account_id: str) -> str:
    """Return the jurisdiction label; never echoes the endpoint or account id."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise StageError(Exit.CONFIG, "R2_ENDPOINT is not a valid URL.")
    if parts.scheme != "https":
        raise StageError(Exit.CONFIG, "R2_ENDPOINT must start with https://")
    if parts.username or parts.password:
        raise StageError(Exit.CONFIG, "R2_ENDPOINT must not contain credentials.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise StageError(
            Exit.CONFIG,
            "R2_ENDPOINT must be the bare S3 API endpoint (no bucket name or path).",
        )
    if port not in (None, 443):
        raise StageError(Exit.CONFIG, "R2_ENDPOINT must not use a custom port.")
    match = _ENDPOINT_HOST_RE.match((parts.hostname or "").lower())
    if not match:
        raise StageError(
            Exit.CONFIG,
            "R2_ENDPOINT host must look like "
            "<account-id>[.<jurisdiction>].r2.cloudflarestorage.com",
        )
    if account_id and account_id.lower() != match.group("acct"):
        raise StageError(
            Exit.CONFIG, "R2_ACCOUNT_ID does not match the account id inside R2_ENDPOINT."
        )
    return match.group("jur") or "default"


def _env_int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise StageError(Exit.CONFIG, f"{name} must be an integer (got a non-numeric value).")
    if not lo <= value <= hi:
        raise StageError(Exit.CONFIG, f"{name} must be between {lo} and {hi}.")
    return value


def _env_choice(env: Mapping[str, str], name: str, default: str, choices: Tuple[str, ...]) -> str:
    raw = (env.get(name) or "").strip().lower() or default
    if raw not in choices:
        raise StageError(Exit.CONFIG, f"{name} must be one of: {', '.join(choices)}.")
    return raw


def _runtime_seconds(env: Mapping[str, str]) -> int:
    """Shared bot runtime. Test hook SNOWCELL_RUNTIME_SECONDS wins; otherwise the
    workflow's SESSION_MINUTES input (blank/invalid -> 355, clamped to 1..355)."""
    override = (env.get("SNOWCELL_RUNTIME_SECONDS") or "").strip()
    if override:
        return _env_int(env, "SNOWCELL_RUNTIME_SECONDS", 1, 1, MAX_RUNTIME_MINUTES * 60)
    raw = (env.get("SESSION_MINUTES") or "").strip()
    try:
        minutes = int(raw) if raw else DEFAULT_RUNTIME_MINUTES
    except ValueError:
        minutes = DEFAULT_RUNTIME_MINUTES
    return max(1, min(MAX_RUNTIME_MINUTES, minutes)) * 60


def load_config(env: Mapping[str, str]) -> Config:
    required = (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_ENDPOINT",
        "R2_BUCKET_NAME",
        "R2_OBJECT_KEY",
    )
    missing = [name for name in required if not (env.get(name) or "").strip()]
    if missing:
        raise StageError(
            Exit.CONFIG,
            "Missing required secrets/variables: " + ", ".join(missing)
            + ". Create them under Settings > Secrets and variables > Actions.",
        )
    account_id = env["R2_ACCOUNT_ID"].strip()
    access_key_id = env["R2_ACCESS_KEY_ID"].strip()
    secret_access_key = env["R2_SECRET_ACCESS_KEY"].strip()
    endpoint = env["R2_ENDPOINT"].strip()
    bucket = env["R2_BUCKET_NAME"].strip()
    key = env["R2_OBJECT_KEY"].strip()
    for secret in (account_id, access_key_id, secret_access_key, endpoint,
                   (env.get("R2_TOKEN_VALUE") or "").strip(),
                   (env.get("RDP_PASSWORD") or "").strip(),
                   (env.get("TAILSCALE_AUTHKEY") or "").strip()):
        register_secret(secret)
    if not _ACCOUNT_RE.match(account_id):
        raise StageError(Exit.CONFIG, "R2_ACCOUNT_ID must be the 32-character hexadecimal account id.")
    jurisdiction = validate_endpoint(endpoint, account_id)
    if bucket != EXPECTED_BUCKET:
        raise StageError(Exit.CONFIG, f"R2_BUCKET_NAME must be exactly '{EXPECTED_BUCKET}'.")
    if key != EXPECTED_KEY:
        raise StageError(
            Exit.CONFIG,
            f"R2_OBJECT_KEY must be exactly '{EXPECTED_KEY}'; alternate final keys are refused.",
        )

    is_win = os.name == "nt"
    downloads = Path(
        env.get("SNOWCELL_DOWNLOADS_DIR")
        or (r"C:\Users\ATOMA\Downloads" if is_win else str(Path.home() / "Downloads"))
    )
    bot_root = Path(env.get("SNOWCELL_BOT_ROOT") or (downloads / "snowcell"))
    log_dir = Path(env.get("SNOWCELL_LOG_DIR") or (downloads / "snowcell-logs"))
    work_dir = Path(
        env.get("SNOWCELL_WORK_DIR")
        or (Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "snowcell-work")
    )
    status_raw = env.get("SNOWCELL_APP_STATUS_FILE")
    if status_raw is None and is_win:
        status_raw = str(Path(env.get("ProgramData", r"C:\ProgramData")) / "ATOMA-AppSetup" / "status.txt")
    start_raw = (env.get("SNOWCELL_JOB_START_EPOCH") or "").strip()
    try:
        job_start = float(start_raw) - _env_int(env, "SNOWCELL_JOB_START_SKEW_SECONDS", 60, 0, 900) if start_raw else None
    except ValueError:
        raise StageError(Exit.CONFIG, "SNOWCELL_JOB_START_EPOCH must be a unix timestamp.")

    return Config(
        endpoint=endpoint,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        account_id=account_id,
        bucket=bucket,
        key=key,
        jurisdiction=jurisdiction,
        runtime_seconds=_runtime_seconds(env),
        deadline_mode=_env_choice(env, "SNOWCELL_DEADLINE_MODE", "bot-start", ("bot-start", "job-anchored")),
        grace_seconds=_env_int(env, "SNOWCELL_SHUTDOWN_GRACE_SECONDS", 45, 1, 600),
        heartbeat_seconds=_env_int(env, "SNOWCELL_HEARTBEAT_SECONDS", 60, 1, 3600),
        failure_policy=_env_choice(env, "SNOWCELL_FAILURE_POLICY", "continue", ("continue", "abort")),
        replace_policy=_env_choice(env, "SNOWCELL_REPLACE_POLICY", "bytes", ("bytes", "content")),
        launch_mode=_env_choice(env, "SNOWCELL_BOT_LAUNCH", "wrapper", ("wrapper", "direct")),
        job_start_epoch=job_start,
        job_timeout_seconds=_env_int(env, "SNOWCELL_JOB_TIMEOUT_MINUTES", JOB_TIMEOUT_MINUTES_DEFAULT, 1, 360) * 60,
        post_reserve_seconds=_env_int(env, "SNOWCELL_POST_RESERVE_MINUTES", 5, 1, 60) * 60,
        publish_safety_seconds=_env_int(env, "SNOWCELL_PUBLISH_SAFETY_SECONDS", 45, 0, 600),
        rescue_seconds=_env_int(env, "SNOWCELL_RESCUE_SECONDS", 600, 0, 3600),
        crash_tail_lines=_env_int(env, "SNOWCELL_CRASH_TAIL_LINES", 20, 0, 200),
        max_unpacked_bytes=_env_int(env, "SNOWCELL_MAX_UNPACKED_MIB", 8192, 1, 1048576) * 1024 * 1024,
        bot_root=bot_root,
        work_dir=work_dir,
        log_dir=log_dir,
        app_status_file=Path(status_raw) if status_raw else None,
        python=sys.executable,
    )


# --------------------------------------------------------------------------- #
# Hashing                                                                     #
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: Path, chunk: int = 1 << 20) -> str:
    """MD5 is used ONLY to compare with R2's ETag of single-part objects."""
    try:
        digest = hashlib.md5(usedforsecurity=False)  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - python < 3.9
        digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


# --------------------------------------------------------------------------- #
# ZIP validation, safe extraction, deterministic rebuild                      #
# --------------------------------------------------------------------------- #
Parts = Tuple[str, ...]
Manifest = Dict[str, Tuple[int, str]]  # "Bot/Data/x.db" -> (size, sha256)

_JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}
_BAD_CHARS = set('<>"|?*')


def is_junk(parts: Parts) -> bool:
    """OS metadata and Python caches are neither extracted nor re-archived.
    Anything under <Bot>/Data or <Bot>/Sessions is NEVER treated as junk."""
    if not parts:
        return False
    if parts[0].lower() == "__macosx":
        return True
    if len(parts) >= 2 and parts[1] in ("Data", "Sessions"):
        return False
    if any(p == "__pycache__" or p.lower() == ".pytest_cache" for p in parts):
        return True
    name = parts[-1].lower()
    return name in _JUNK_NAMES or name.endswith((".pyc", ".pyo"))


def normalize_member(name: str) -> Parts:
    """Safe path components of a ZIP member name, or StageError (path traversal etc.)."""
    if "\x00" in name:
        raise StageError(Exit.ARCHIVE_INVALID, "Archive contains an entry with a NUL byte.")
    norm = name.replace("\\", "/")  # some Windows tools write backslashes
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise StageError(Exit.ARCHIVE_INVALID, f"Unsafe absolute path in archive: {name!r}")
    parts = tuple(p for p in norm.split("/") if p not in ("", "."))
    if not parts:
        raise StageError(Exit.ARCHIVE_INVALID, f"Empty entry name in archive: {name!r}")
    for part in parts:
        if part == "..":
            raise StageError(Exit.ARCHIVE_INVALID, f"Path traversal in archive: {name!r}")
        if ":" in part:
            raise StageError(Exit.ARCHIVE_INVALID, f"Drive letter / stream marker in archive entry: {name!r}")
        if part.endswith((" ", ".")):
            raise StageError(Exit.ARCHIVE_INVALID, f"Entry component ends with space or dot: {name!r}")
        if part.split(".")[0].rstrip().lower() in _WIN_RESERVED:
            raise StageError(Exit.ARCHIVE_INVALID, f"Reserved Windows device name in archive: {name!r}")
        if any(ord(c) < 32 or c in _BAD_CHARS for c in part):
            raise StageError(Exit.ARCHIVE_INVALID, f"Illegal character in archive entry: {name!r}")
    return parts


@dataclasses.dataclass
class ArchiveInfo:
    files: Dict[Parts, zipfile.ZipInfo]
    dirs: set
    skipped_junk: int
    total_unpacked: int


def validate_archive(zf: zipfile.ZipFile, max_unpacked: int) -> ArchiveInfo:
    """Reject unsafe or malformed archives and enforce the five-bot structure."""
    infos = zf.infolist()
    if len(infos) > 500_000:
        raise StageError(Exit.ARCHIVE_INVALID, "Archive has an unreasonable number of entries.")
    files: Dict[Parts, zipfile.ZipInfo] = {}
    dirs: set = set()
    seen: Dict[str, str] = {}
    skipped = 0
    total = 0
    for zi in infos:
        if zi.flag_bits & 0x1:
            raise StageError(Exit.ARCHIVE_INVALID, "Archive contains encrypted entries.")
        if zi.create_system == 3 and ((zi.external_attr >> 16) & 0o170000) == 0o120000:
            raise StageError(Exit.ARCHIVE_INVALID, f"Archive contains a symbolic link: {zi.filename!r}")
        parts = normalize_member(zi.filename)
        if is_junk(parts):
            skipped += 1
            continue
        if parts[0] not in BOT_NAMES:
            raise StageError(
                Exit.ARCHIVE_INVALID,
                f"Unexpected top-level entry {parts[0]!r}; expected only: {', '.join(BOT_NAMES)}",
            )
        is_dir = zi.is_dir() or zi.filename.endswith(("/", "\\"))
        folded = "/".join(parts).lower()
        if folded in seen and not (is_dir and seen[folded] == "dir"):
            raise StageError(Exit.ARCHIVE_INVALID, f"Duplicate or case-colliding entry: {zi.filename!r}")
        seen[folded] = "dir" if is_dir else "file"
        for i in range(1, len(parts)):
            dirs.add(parts[:i])
        if is_dir:
            dirs.add(parts)
        else:
            files[parts] = zi
            total += zi.file_size
    if total > max_unpacked:
        raise StageError(Exit.ARCHIVE_INVALID, f"Archive would unpack to {fmt_size(total)}, above the configured limit.")
    clash = [p for p in files if p in dirs]
    if clash:
        raise StageError(Exit.ARCHIVE_INVALID, f"Entry is both a file and a directory: {'/'.join(clash[0])}")
    for bot in BOT_NAMES:
        if (bot, "bot.py") not in files:
            raise StageError(Exit.ARCHIVE_INVALID, f"Archive is missing {bot}/bot.py")
    return ArchiveInfo(files=files, dirs=dirs, skipped_junk=skipped, total_unpacked=total)


def prepare_destination(bot_root: Path) -> None:
    """Refuse to extract over an existing installation (no overwrites, no duplicates)."""
    present = [bot for bot in BOT_NAMES if (bot_root / bot).exists()]
    if present:
        raise StageError(
            Exit.ARCHIVE_INVALID,
            f"{bot_root} already contains {', '.join(present)}; refusing to overwrite. "
            "Remove it or set SNOWCELL_BOT_ROOT to an empty location.",
        )


def extract_archive(zf: zipfile.ZipFile, info: ArchiveInfo, dest_root: Path) -> Manifest:
    """Extract validated members; every byte is CRC-checked by zipfile and SHA-256'd."""
    dest_root.mkdir(parents=True, exist_ok=True)
    root = dest_root.resolve()
    for parts in sorted(info.dirs):
        dest_root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    manifest: Manifest = {}
    for parts, zi in sorted(info.files.items()):
        target = dest_root.joinpath(*parts)
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise StageError(Exit.ARCHIVE_INVALID, "Refusing to write outside the extraction directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with zf.open(zi) as src, open(target, "wb") as out:
                for block in iter(lambda: src.read(1 << 20), b""):
                    out.write(block)
                    digest.update(block)
                    size += len(block)
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
            raise StageError(Exit.ARCHIVE_INVALID, f"Could not extract {'/'.join(parts)}: {type(exc).__name__}")
        if size != zi.file_size:
            raise StageError(Exit.ARCHIVE_INVALID, f"Size mismatch while extracting {'/'.join(parts)}")
        with contextlib.suppress(OverflowError, ValueError, OSError):
            stamp = time.mktime(tuple(zi.date_time) + (0, 0, -1))
            os.utime(target, (stamp, stamp))
        manifest["/".join(parts)] = (size, digest.hexdigest())
    for bot in BOT_NAMES:
        for sub in ("Data", "Sessions"):
            (dest_root / bot / sub).mkdir(parents=True, exist_ok=True)
    return manifest


def iter_archive_members(bot_root: Path) -> Iterator[Tuple[Parts, Path, bool]]:
    """Deterministic walk of everything that belongs in snowcell.zip."""
    for bot in BOT_NAMES:
        base = bot_root / bot
        if not base.is_dir():
            raise StageError(Exit.ZIP_BUILD, f"Missing bot directory: {bot}")
        yield (bot,), base, True
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            rel = Path(dirpath).relative_to(bot_root).parts
            dirnames[:] = sorted(d for d in dirnames if not is_junk(rel + (d,)))
            for name in dirnames:
                yield rel + (name,), Path(dirpath) / name, True
            for name in sorted(filenames):
                parts = rel + (name,)
                path = Path(dirpath) / name
                if is_junk(parts):
                    continue
                if path.is_symlink():
                    warn(f"Skipping symbolic link (not archived): {'/'.join(parts)}")
                    continue
                yield parts, path, False


def build_archive(bot_root: Path, dest: Path) -> Manifest:
    """Write ``dest`` (via a .part file) and return the manifest of what was archived."""
    part = dest.with_name(dest.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    manifest: Manifest = {}
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
            for parts, path, is_dir in iter_archive_members(bot_root):
                arcname = "/".join(parts)
                zi = zipfile.ZipInfo.from_file(path, arcname, strict_timestamps=False)
                if is_dir:
                    # Directory mtimes carry no information; a fixed stamp keeps rebuilds of an
                    # unchanged tree byte-identical (so "no changes" can really be detected).
                    zi.date_time = (1980, 1, 1, 0, 0, 0)
                    zf.writestr(zi, b"")
                    continue
                zi.compress_type = zipfile.ZIP_DEFLATED
                digest = hashlib.sha256()
                size = 0
                with open(path, "rb") as src, zf.open(zi, "w", force_zip64=path.stat().st_size >= 2**31 - 1) as out:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        out.write(block)
                        digest.update(block)
                        size += len(block)
                manifest[arcname] = (size, digest.hexdigest())
        os.replace(part, dest)
    except StageError:
        raise
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise StageError(Exit.ZIP_BUILD, f"Could not build the new archive: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(OSError):
            if part.exists():
                part.unlink()
    return manifest


def validate_new_archive(path: Path, expected: Manifest, max_unpacked: int) -> None:
    """The replacement must open, CRC-check, have the five bots, and match the disk walk."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise StageError(Exit.ZIP_BUILD, f"CRC error inside the new archive: {bad}")
            info = validate_archive(zf, max_unpacked)
    except zipfile.BadZipFile as exc:
        raise StageError(Exit.ZIP_BUILD, f"The new archive cannot be opened: {exc}")
    except StageError as exc:
        raise StageError(Exit.ZIP_BUILD, f"The new archive failed validation: {exc.message}")
    archived = {"/".join(p): zi.file_size for p, zi in info.files.items()}
    if set(archived) != set(expected):
        raise StageError(Exit.ZIP_BUILD, "The new archive does not contain exactly the files found on disk.")
    for name, size in archived.items():
        if size != expected[name][0]:
            raise StageError(Exit.ZIP_BUILD, f"Size mismatch inside the new archive for {name}")


# --------------------------------------------------------------------------- #
# Cloudflare R2 through its S3-compatible API (boto3)                         #
# --------------------------------------------------------------------------- #
SINGLE_PUT_MAX = int(4.5 * 1024**3)  # R2 allows ~5 GiB per single PutObject
_AUTH_CODES = {
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "Forbidden",
    "InvalidToken", "ExpiredToken", "Unauthorized", "401", "403",
}
_TRANSIENT_CODES = {
    "RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable",
    "429", "500", "502", "503", "504",
}


class TransferError(OSError):
    """An incomplete or inconsistent transfer (safe to include in logs)."""


@dataclasses.dataclass(frozen=True)
class ObjectInfo:
    size: int
    etag: str
    sha256_b64: Optional[str]


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return str(response.get("Error", {}).get("Code", ""))


def _http_status(exc: BaseException) -> int:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return 0
    try:
        return int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    except (TypeError, ValueError):
        return 0


def _is_not_found(exc: BaseException) -> bool:
    code, status = _error_code(exc), _http_status(exc)
    return code in ("404", "NoSuchKey", "NotFound") or (status == 404 and code in ("", "404"))


def classify_client_error(exc: BaseException) -> Tuple[Exit, str]:
    code, status = _error_code(exc), _http_status(exc)
    if code == "NoSuchBucket":
        return Exit.CONFIG, (
            "Bucket not found. Check R2_BUCKET_NAME and that R2_ENDPOINT carries the "
            "bucket's jurisdiction (default / eu / fedramp / us)."
        )
    if _is_not_found(exc):
        return Exit.R2_MISSING, "The object does not exist in the bucket."
    if code in _AUTH_CODES or status in (401, 403):
        return Exit.R2_AUTH, (
            f"R2 rejected the request ({code or status}). Check R2_ACCESS_KEY_ID / "
            "R2_SECRET_ACCESS_KEY and that the token has Object Read & Write on this bucket."
        )
    return Exit.R2_TRANSFER, f"R2 request failed ({code or status or 'unknown error'})."


class R2Store:
    """Thin, retrying wrapper that only ever touches the one configured object."""

    def __init__(self, cfg: Config, client=None, errors: Optional[Tuple[type, type]] = None):
        self.cfg = cfg
        if client is None:
            client, errors = self._build_client(cfg)
        assert errors is not None
        self._client = client
        self._client_error, self._core_error = errors
        self._send_checksum = True

    @staticmethod
    def _build_client(cfg: Config):
        try:
            import boto3  # type: ignore
            from botocore.config import Config as BotoConfig  # type: ignore
            from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
        except ImportError:
            raise StageError(Exit.DEPS, "boto3 is not installed in this Python interpreter.")
        # R2 does not implement the SDK's default CRC32 request checksums (boto3 >= 1.36):
        # only compute/validate checksums when an operation requires them.
        os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "WHEN_REQUIRED")
        os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "WHEN_REQUIRED")
        base = dict(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=20,
            read_timeout=300,
            s3={"addressing_style": "path"},
        )
        try:
            boto_cfg = BotoConfig(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                **base,
            )
        except TypeError:  # older botocore without those options
            boto_cfg = BotoConfig(**base)
        client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name="auto",
            config=boto_cfg,
        )
        return client, (ClientError, BotoCoreError)

    # -- plumbing ---------------------------------------------------------- #
    @staticmethod
    def _sleep(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 15))

    def _retrying(self, label: str, fn: Callable[[], object], attempts: int = 3):
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except self._client_error as exc:
                transient = _error_code(exc) in _TRANSIENT_CODES or _http_status(exc) in (429, 500, 502, 503, 504)
                if transient and attempt < attempts:
                    self._sleep(attempt)
                    continue
                code, hint = classify_client_error(exc)
                raise StageError(code, f"{label}: {hint}", r2_code=_error_code(exc), http_status=_http_status(exc))
            except StageError:
                raise
            except (self._core_error, OSError) as exc:
                if attempt < attempts:
                    self._sleep(attempt)
                    continue
                detail = str(exc) if isinstance(exc, TransferError) else type(exc).__name__
                raise StageError(
                    Exit.R2_TRANSFER,
                    f"{label}: network/transfer error ({detail}). Check R2_ENDPOINT, DNS and the WARP/Tailscale state.",
                )
        raise StageError(Exit.R2_TRANSFER, f"{label}: failed")  # pragma: no cover

    # -- operations -------------------------------------------------------- #
    def head(self) -> Optional[ObjectInfo]:
        def once(with_checksum: bool):
            kwargs = {"Bucket": self.cfg.bucket, "Key": self.cfg.key}
            if with_checksum:
                kwargs["ChecksumMode"] = "ENABLED"
            try:
                return self._client.head_object(**kwargs)
            except self._client_error as exc:
                if _is_not_found(exc):
                    return None
                if with_checksum and _http_status(exc) in (400, 501):
                    return once(False)
                raise

        resp = self._retrying("HEAD object", lambda: once(True))
        if resp is None:
            return None
        return ObjectInfo(
            size=int(resp["ContentLength"]),
            etag=str(resp.get("ETag", "")).strip('"'),
            sha256_b64=resp.get("ChecksumSHA256"),
        )

    def download(self, dest: Path, expected: ObjectInfo) -> Tuple[str, int, float]:
        """Stream to ``dest`` atomically. Returns (sha256_hex, bytes, seconds)."""
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)

        def once():
            started = time.monotonic()
            response = self._client.get_object(Bucket=self.cfg.bucket, Key=self.cfg.key)
            body = response["Body"]
            total = int(response.get("ContentLength", expected.size))
            digest = hashlib.sha256()
            got = 0
            try:
                with open(part, "wb") as out:
                    for chunk in body.iter_chunks(1 << 20):
                        out.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            finally:
                with contextlib.suppress(Exception):
                    body.close()
            if got != total or got != expected.size:
                raise TransferError(f"incomplete download ({got} of {expected.size} bytes)")
            os.replace(part, dest)
            return digest.hexdigest(), got, time.monotonic() - started

        return self._retrying("Download", once, attempts=4)  # type: ignore[return-value]

    # S3 error codes that mean "the bytes were damaged", never "the checksum header is unsupported"
    _DAMAGE_CODES = {"BadDigest", "InvalidDigest", "IncompleteBody", "RequestTimeout", "EntityTooSmall", "EntityTooLarge"}

    def upload(self, src: Path, sha256_hex: str, size: int) -> None:
        """PUT the archive to the exact key. The SHA-256 is sent as ``x-amz-checksum-sha256`` so R2
        verifies it while storing. If R2 ever refuses that header, the upload is repeated without it
        (the result is verified afterwards anyway) - the restore of the original uses the same path,
        so an incompatible header can never turn into a lost object."""
        extra = {"ContentType": "application/zip", "Metadata": {"sha256": sha256_hex}}

        def once(with_checksum: bool):
            if size <= SINGLE_PUT_MAX:
                kwargs = dict(Bucket=self.cfg.bucket, Key=self.cfg.key, ContentLength=size, **extra)
                if with_checksum:
                    kwargs["ChecksumSHA256"] = sha256_b64(sha256_hex)  # R2 verifies it server-side
                with open(src, "rb") as handle:
                    self._client.put_object(Body=handle, **kwargs)
            else:  # multipart (no whole-object checksum possible)
                self._client.upload_file(str(src), self.cfg.bucket, self.cfg.key, ExtraArgs=extra)

        if self._send_checksum:
            try:
                self._retrying("Upload", lambda: once(True), attempts=3)
                return
            except StageError as exc:
                refused = (exc.code == Exit.R2_TRANSFER and exc.http_status in (400, 501)
                           and exc.r2_code not in self._DAMAGE_CODES)
                if not refused:
                    raise
                warn(f"R2 refused the upload together with its checksum header ({exc.r2_code or exc.http_status}); "
                     "repeating it without that header. The stored object is verified afterwards.")
                self._send_checksum = False
        self._retrying("Upload", lambda: once(False), attempts=3)

    def delete(self) -> None:
        self._retrying(
            "Delete", lambda: self._client.delete_object(Bucket=self.cfg.bucket, Key=self.cfg.key), attempts=3
        )
        if self.head() is not None:
            raise StageError(Exit.R2_DELETE, "The object still exists after DeleteObject.")

    @staticmethod
    def matches(info: ObjectInfo, sha256_hex: str, md5_hex: Optional[str], size: int) -> Optional[bool]:
        """True/False when provable from metadata alone, None when unknown."""
        if info.size != size:
            return False
        if info.sha256_b64:
            return info.sha256_b64 == sha256_b64(sha256_hex)
        if md5_hex and info.etag and "-" not in info.etag:
            return info.etag.lower() == md5_hex.lower()
        return None

    def verify(self, sha256_hex: str, size: int, md5_hex: Optional[str], full: bool, scratch: Path) -> str:
        info = self.head()
        if info is None:
            raise StageError(Exit.VERIFY, "The object is missing after the upload.")
        verdict = self.matches(info, sha256_hex, md5_hex, size)
        if verdict is False:
            raise StageError(Exit.VERIFY, "Stored object size/checksum differs from the uploaded archive.")
        how = "size + server-side checksum" if verdict else "size only"
        if full:
            probe = scratch / "verify-download.zip"
            try:
                got, _, _ = self.download(probe, info)
            finally:
                with contextlib.suppress(OSError):
                    probe.unlink()
            if got != sha256_hex:
                raise StageError(Exit.VERIFY, "Re-downloaded object has a different SHA-256 than the uploaded archive.")
            how = "full re-download SHA-256"
        return how


# --------------------------------------------------------------------------- #
# Dependencies and the sanitized bot environment                              #
# --------------------------------------------------------------------------- #
def _run_pip(python: str, packages: List[str], timeout: int = 900) -> None:
    cmd = [python, "-m", "pip", "install", *packages]
    # pip may run package build code, so it gets the same credential-free environment as the bots.
    env = _child_env({"PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    try:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StageError(Exit.DEPS, f"pip install timed out after {timeout}s.")
    except OSError as exc:
        raise StageError(Exit.DEPS, f"Could not run pip: {exc}")
    if proc.returncode != 0:
        tail = "\n".join(redact(proc.stdout or "").splitlines()[-15:])
        raise StageError(Exit.DEPS, f"pip install failed with exit code {proc.returncode}:\n{tail}")


def ensure_boto3(python: str) -> None:
    """boto3 is needed before the archive can be downloaded, so bootstrap it first."""
    import importlib
    import importlib.util

    if importlib.util.find_spec("boto3") is None:
        info("boto3 not found; installing it first (needed for the R2 download).")
        _run_pip(python, ["boto3"])
        importlib.invalidate_caches()


def install_dependencies(python: str) -> None:
    """Exactly: python -m pip install "Telethon==1.44.0" pytz phonenumbers aiofiles aiosqlite boto3"""
    started = time.monotonic()
    _run_pip(python, PIP_REQUIREMENTS)
    info(f"Dependencies installed in {time.monotonic() - started:.0f}s: {', '.join(PIP_REQUIREMENTS)}")


_SENSITIVE_EXACT = {
    "RDP_PASSWORD", "TAILSCALE_AUTHKEY", "GITHUB_TOKEN", "GH_TOKEN", "SYSTEM_ACCESSTOKEN",
    "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
}
_SENSITIVE_PREFIXES = ("R2_", "AWS_", "CLOUDFLARE_", "CF_", "ACTIONS_", "SNOWCELL_")
_SENSITIVE_PATTERN = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|AUTHKEY|AUTH_KEY|API_?KEY|PRIVATE_?KEY|CREDENTIAL|ACCESS_?KEY)", re.I
)


def sanitized_env(env: Mapping[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Environment for the bot processes: ordinary Windows/Python variables only.
    Returns (clean_env, names_of_removed_variables); values are never logged."""
    clean: Dict[str, str] = {}
    removed: List[str] = []
    for name, value in env.items():
        upper = name.upper()
        leaked = any(secret in value for secret in _SECRETS)
        if (upper in _SENSITIVE_EXACT or upper.startswith(_SENSITIVE_PREFIXES)
                or _SENSITIVE_PATTERN.search(upper) or leaked):
            removed.append(name)
        else:
            clean[name] = value
    clean.update(PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return clean, sorted(removed)


def _child_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Environment for every helper program this script starts (pip, taskkill, the PowerShell process
    scan): ordinary variables only, exactly like the bots - infrastructure secrets are never inherited."""
    clean, _ = sanitized_env(os.environ)
    if extra:
        clean.update(extra)
    return clean


# --------------------------------------------------------------------------- #
# Bot supervision                                                             #
# --------------------------------------------------------------------------- #
IS_WINDOWS = os.name == "nt"
STARTUP_CHECK_SECONDS = 5.0
TERMINATE_WAIT_SECONDS = 10.0  # after terminate, before kill
KILL_WAIT_SECONDS = 10.0
SETTLE_SECONDS = 2.0  # let Windows release file handles after the bots exit


@dataclasses.dataclass
class BotProc:
    name: str
    directory: Path
    log_path: Path
    popen: Optional["subprocess.Popen[bytes]"] = None
    pid: int = 0
    started_mono: float = 0.0
    exit_code: Optional[int] = None
    launch_error: str = ""
    stop_method: str = ""  # graceful | terminated | killed | (blank = still running / crashed)
    crashed: bool = False
    stop_file: Optional[Path] = None  # fallback channel for a graceful stop (see bot_runner.py)

    @property
    def running(self) -> bool:
        return self.popen is not None and self.popen.poll() is None


class Interrupt:
    """Flag-style signal handling: handlers never raise, so the critical section
    (delete -> upload) can never be torn in half by a Ctrl-C inside this process."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.name = ""
        self.epoch: Optional[float] = None  # when the first stop request arrived (unix time)

    def install(self) -> None:
        def handler(signum, _frame):  # noqa: ANN001
            self.name = signal.Signals(signum).name
            if self.epoch is None:
                self.epoch = time.time()
            self.event.set()

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, handler)


def describe_exit(code: Optional[int]) -> str:
    if code is None:
        return "no exit code"
    if IS_WINDOWS and (code < 0 or code > 255):
        return f"{code} (0x{code & 0xFFFFFFFF:08X})"
    return str(code)


def tail_lines(path: Path, count: int) -> List[str]:
    if count <= 0:
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 64 * 1024))
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-count:]


def launch_bots(cfg: Config, env: Dict[str, str]) -> List[BotProc]:
    """Start the five bots concurrently (never sequentially, never twice)."""
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).resolve().with_name("bot_runner.py")
    use_wrapper = cfg.launch_mode == "wrapper" and runner.is_file()
    if cfg.launch_mode == "wrapper" and not use_wrapper:
        warn("scripts/bot_runner.py not found; launching bot.py directly (Windows stop will be abrupt).")
    popen_kwargs: Dict[str, object] = {}
    if IS_WINDOWS:
        # Own process group => CTRL_BREAK_EVENT can target exactly one bot.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True
    stop_dir = cfg.work_dir / "stop-requests"
    with contextlib.suppress(OSError):
        stop_dir.mkdir(parents=True, exist_ok=True)
    procs: List[BotProc] = []
    for name in BOT_NAMES:
        directory = cfg.bot_root / name
        script = directory / "bot.py"
        bp = BotProc(name=name, directory=directory, log_path=cfg.log_dir / f"{name}.log")
        if use_wrapper:
            bp.stop_file = stop_dir / f"{name}.stop"
            with contextlib.suppress(OSError):
                bp.stop_file.unlink()  # never inherit a stale request
            cmd = [cfg.python, "-u", str(runner), "--stop-file", str(bp.stop_file), str(script)]
        else:
            cmd = [cfg.python, "-u", str(script)]
        try:
            with open(bp.log_path, "ab") as log:
                log.write(f"=== {name} starting {utc_iso()} ===\n".encode())
                log.flush()
                bp.popen = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell
                    cmd, cwd=str(directory), env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, shell=False, **popen_kwargs,
                )
            bp.pid = bp.popen.pid
            bp.started_mono = time.monotonic()
            info(f"Launched {name} (pid {bp.pid})")
        except OSError as exc:
            bp.launch_error = f"{type(exc).__name__}: {exc}"
            bp.crashed = True
            error(f"Could not launch {name}: {bp.launch_error}")
        procs.append(bp)
    if not any(bp.popen for bp in procs):
        raise StageError(Exit.BOT_START, "None of the five bots could be launched.")
    return procs


def _report_crash(cfg: Config, bp: BotProc) -> None:
    uptime = fmt_minutes(time.monotonic() - bp.started_mono)
    error(f"{bp.name} exited unexpectedly with code {describe_exit(bp.exit_code)} after {uptime} (pid {bp.pid})")
    lines = tail_lines(bp.log_path, cfg.crash_tail_lines)
    if lines:
        with group(f"Last {len(lines)} log lines of {bp.name} (set SNOWCELL_CRASH_TAIL_LINES=0 to hide)"):
            for line in lines:
                _emit(f"    {line}")


def _reap(cfg: Config, procs: List[BotProc]) -> None:
    for bp in procs:
        if bp.popen is not None and bp.exit_code is None:
            code = bp.popen.poll()
            if code is not None:
                bp.exit_code = code
                bp.crashed = True
                _report_crash(cfg, bp)


def startup_check(cfg: Config, procs: List[BotProc], stop: threading.Event) -> None:
    """Give the bots a moment; report any that die immediately (bad import, syntax...)."""
    end = time.monotonic() + STARTUP_CHECK_SECONDS
    while time.monotonic() < end and not stop.is_set():
        stop.wait(0.5)
        _reap(cfg, procs)
    alive = [bp.name for bp in procs if bp.running]
    info(f"Startup check: {len(alive)}/{len(BOT_NAMES)} bots running ({', '.join(alive) or 'none'})")
    if not alive:
        raise StageError(Exit.BOT_START, "All bots exited during startup; see the log lines above.")


class StatusWatcher:
    """Preserves the old keep-alive behaviour of printing ATOMA app-install status changes."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.last: Optional[str] = None
        self.next_check = 0.0

    def poll(self) -> None:
        if self.path is None or time.monotonic() < self.next_check:
            return
        self.next_check = time.monotonic() + 30
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return
        if text and text != self.last:
            info(f"ATOMA app status: {text}")
            self.last = text


def monitor_bots(cfg: Config, procs: List[BotProc], stop: threading.Event, deadline: float) -> str:
    """One shared deadline for all bots. Returns why monitoring ended."""
    watcher = StatusWatcher(cfg.app_status_file)
    next_beat = 0.0
    while True:
        _reap(cfg, procs)
        running = [bp for bp in procs if bp.running]
        if not running:
            return "all-exited"
        if cfg.failure_policy == "abort" and any(bp.crashed for bp in procs):
            return "aborted-by-failure-policy"
        if stop.is_set():
            return "interrupted"
        now = time.monotonic()
        if now >= deadline:
            return "deadline"
        watcher.poll()
        if now >= next_beat:
            crashed = [bp.name for bp in procs if bp.crashed]
            info(
                f"Session active; {max(0, int((deadline - now) // 60))} minutes remaining; "
                f"bots running {len(running)}/{len(BOT_NAMES)}"
                + (f"; CRASHED: {', '.join(crashed)}" if crashed else "")
            )
            next_beat = now + cfg.heartbeat_seconds
        stop.wait(min(1.0, max(0.05, deadline - now)))


# -- stopping -------------------------------------------------------------- #
def _send_graceful(bp: BotProc) -> None:
    """Ask one bot to stop like Ctrl+C would. Exactly ONE channel fires (a bot must never get two
    interrupts, the second could cut its clean-up short): the console signal, or - only when that
    could not be delivered - the stop file watched by bot_runner.py."""
    try:
        if IS_WINDOWS:
            os.kill(bp.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(bp.pid, signal.SIGINT)
        return
    except (OSError, ValueError) as exc:
        warn(f"{bp.name}: could not deliver the graceful stop signal ({type(exc).__name__}); trying the stop file.")
    if bp.stop_file is None:
        warn(f"{bp.name}: no stop file is available (direct launch); it will be terminated after the grace period.")
        return
    try:
        bp.stop_file.parent.mkdir(parents=True, exist_ok=True)
        bp.stop_file.write_text("stop\n", encoding="ascii")
        info(f"{bp.name}: stop file written; bot_runner.py will raise KeyboardInterrupt inside the bot.")
    except OSError as exc:
        warn(f"{bp.name}: could not write the stop file ({type(exc).__name__}); it will be terminated after the grace period.")


def _terminate(bp: BotProc) -> None:
    assert bp.popen is not None
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(bp.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, env=_child_env())
    else:
        with contextlib.suppress(OSError):
            os.killpg(bp.pid, signal.SIGTERM)


def _kill(bp: BotProc) -> None:
    assert bp.popen is not None
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(bp.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, env=_child_env())
    else:
        with contextlib.suppress(OSError):
            os.killpg(bp.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        bp.popen.kill()


def _wait_all(procs: List[BotProc], seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and any(bp.running for bp in procs):
        time.sleep(0.2)


def stop_bots(cfg: Config, procs: List[BotProc]) -> List[str]:
    """Graceful stop -> terminate -> kill. Returns problems (empty == all stopped)."""
    live = [bp for bp in procs if bp.running]
    if live:
        info(f"Stopping {len(live)} bot(s); grace period {cfg.grace_seconds}s ({'CTRL_BREAK' if IS_WINDOWS else 'SIGINT'}).")
        for bp in live:
            _send_graceful(bp)
        _wait_all(live, cfg.grace_seconds)
        for bp in live:
            if not bp.running:
                bp.stop_method = "graceful"
        stragglers = [bp for bp in live if bp.running]
        for bp in stragglers:
            warn(f"{bp.name} ignored the graceful stop for {cfg.grace_seconds}s; terminating it.")
            _terminate(bp)
        _wait_all(stragglers, TERMINATE_WAIT_SECONDS)
        for bp in stragglers:
            if not bp.running:
                bp.stop_method = "terminated"
        hard = [bp for bp in stragglers if bp.running]
        for bp in hard:
            warn(f"{bp.name} survived terminate; killing it.")
            _kill(bp)
        _wait_all(hard, KILL_WAIT_SECONDS)
        for bp in hard:
            if not bp.running:
                bp.stop_method = "killed"
    problems: List[str] = []
    for bp in procs:
        if bp.popen is None:
            continue
        code = bp.popen.poll()
        if code is None:
            problems.append(f"{bp.name} (pid {bp.pid}) is still running")
            continue
        if bp.exit_code is None:
            bp.exit_code = code
        if bp.stop_method:
            info(f"{bp.name}: stopped ({bp.stop_method}), exit code {describe_exit(code)}")
    return problems


# -- stray processes (verification and crash recovery) ---------------------- #
def find_stray_processes(root: Path) -> List[Tuple[int, str]]:
    """Processes whose command line mentions the bot root (never this process)."""
    me = {os.getpid(), os.getppid()}
    found: List[Tuple[int, str]] = []
    needle = str(root)
    if IS_WINDOWS:
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
            "$_.CommandLine.Contains($env:SNOWCELL_SCAN_ROOT) } | "
            "ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.Name }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                env=_child_env({"SNOWCELL_SCAN_ROOT": needle}), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=60, check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return found
        for line in out.splitlines():
            pid_text, _, name = line.partition("|")
            if pid_text.strip().isdigit() and int(pid_text) not in me:
                found.append((int(pid_text), name.strip()))
    else:
        proc_dir = Path("/proc")
        if not proc_dir.is_dir():
            return found
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit() or int(entry.name) in me:
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if needle in cmdline:
                found.append((int(entry.name), cmdline.split(" ", 1)[0]))
    return found


def kill_strays(root: Path) -> List[str]:
    """Kill any leftover bot process; returns descriptions of those that survived."""
    survivors: List[str] = []
    for pid, name in find_stray_processes(root):
        warn(f"Killing stray process {name or '?'} (pid {pid}) that still references the bot directory.")
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, env=_child_env())
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    time.sleep(1.0)
    for pid, name in find_stray_processes(root):
        survivors.append(f"{name or '?'} (pid {pid})")
    return survivors


# --------------------------------------------------------------------------- #
# Run state (write-ahead log), job-time budget, publish and restore           #
# --------------------------------------------------------------------------- #
class State:
    """JSON file rewritten atomically after every phase, so a crash, cancel or timeout
    of this process can be finished safely by ``--recover``."""

    def __init__(self, path: Path, data: Optional[Dict[str, object]] = None):
        self.path = path
        self.data: Dict[str, object] = data if data is not None else {
            "schema": STATE_SCHEMA, "created_at": utc_iso(), "phase": "init"}

    @classmethod
    def load(cls, path: Path) -> Optional["State"]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StageError(Exit.CONFIG, f"Cannot read the run state file: {type(exc).__name__}")
        try:
            data = json.loads(raw)
        except ValueError:
            raise StageError(Exit.CONFIG, "The run state file is corrupted.")
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            raise StageError(Exit.CONFIG, "The run state file has an unknown format.")
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def update(self, *, best_effort: bool = False, **fields: object) -> None:
        """Record progress. ``best_effort`` is used once the R2 object may already be deleted: a
        full or locked disk must never stop the upload that follows."""
        self.data.update(fields)
        self.data["updated_at"] = utc_iso()
        try:
            self.save()
        except OSError as exc:
            if not best_effort:
                raise
            warn(f"Could not write the run state file ({type(exc).__name__}); continuing anyway - "
                 "crash recovery is limited.")

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)

    def section(self, key: str) -> Dict[str, object]:
        value = self.data.get(key)
        return value if isinstance(value, dict) else {}


def save_manifest(path: Path, manifest: Manifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: [v[0], v[1]] for k, v in sorted(manifest.items())}), encoding="utf-8")


def load_manifest(path: Path) -> Manifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: (int(v[0]), str(v[1])) for k, v in data.items()}


def diff_manifests(old: Manifest, new: Manifest) -> Tuple[int, int, int]:
    added = len(new.keys() - old.keys())
    removed = len(old.keys() - new.keys())
    modified = sum(1 for k in old.keys() & new.keys() if old[k] != new[k])
    return added, removed, modified


def write_github_output(**pairs: object) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with contextlib.suppress(OSError):
        with open(target, "a", encoding="utf-8") as handle:
            for name, value in pairs.items():
                handle.write(f"{name}={value}\n")


def write_step_summary(lines: List[str]) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with contextlib.suppress(OSError):
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(redact("\n".join(lines)) + "\n")


# -- job-time budget ---------------------------------------------------------- #
def job_time_left(cfg: Config, in_recovery: bool, interrupt_epoch: Optional[float] = None) -> Optional[float]:
    """Seconds until GitHub force-ends the job (or, during recovery, the always() step).

    After a cancellation - a person pressing Cancel, or the job timeout - GitHub gives the
    remaining steps at most 5 minutes counted from the cancel request; ``interrupt_epoch`` is
    the moment this process received that request."""
    now = time.time()
    if in_recovery and interrupt_epoch is not None:
        return interrupt_epoch + CANCEL_WINDOW_SECONDS - now
    deadline = cfg.job_deadline_epoch
    if deadline is None:
        return None
    remaining = deadline - now
    if remaining > 0 or not in_recovery:
        return remaining
    return CANCEL_WINDOW_SECONDS + remaining


def _interrupt_epoch(state: "State") -> Optional[float]:
    value = state.get("interrupt_epoch")
    try:
        return float(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def report_budget(cfg: Config, planned_runtime_seconds: float, title: str = "Job-time budget") -> None:
    """Print where the planned shutdown falls inside the job limit. Called twice per run: once at the start
    (a forecast: the setup time is not known yet) and once when the bots have started (the measured value)."""
    deadline = cfg.job_deadline_epoch
    if deadline is None:
        warn("SNOWCELL_JOB_START_EPOCH is not set: the job-time budget cannot be checked.")
        return
    elapsed = time.time() - (deadline - cfg.job_timeout_seconds)
    stop_at = elapsed + planned_runtime_seconds
    window = cfg.job_timeout_seconds - stop_at
    with group(title):
        info(f"Job timeout                : {fmt_minutes(cfg.job_timeout_seconds)}")
        info(f"Elapsed before bots start  : {fmt_minutes(elapsed)}")
        info(f"Planned bot runtime        : {fmt_minutes(planned_runtime_seconds)}")
        info(f"Shutdown would begin at    : {fmt_minutes(stop_at)} after job start")
        info(f"Post-runtime window        : {fmt_minutes(window)} (stop + zip + hash + delete + upload + verify)")
    if window < 0:
        warn(
            "GitHub will cancel the job BEFORE the bots reach their deadline. The always() recovery step "
            "becomes the only chance to save this session. Set the repository variable "
            "SNOWCELL_DEADLINE_MODE=job-anchored to shorten the bot runtime automatically."
        )
    elif window < cfg.post_reserve_seconds:
        warn(
            f"Only {fmt_minutes(window)} remain after the bots stop; stopping, zipping, hashing and replacing the "
            "R2 object may not fit. Consider SNOWCELL_DEADLINE_MODE=job-anchored."
        )


def compute_deadline(cfg: Config, start_mono: float) -> Tuple[float, str]:
    deadline = start_mono + cfg.runtime_seconds
    note = ""
    if cfg.deadline_mode == "job-anchored" and cfg.job_deadline_epoch is not None:
        limit = start_mono + (cfg.job_deadline_epoch - time.time()) - cfg.post_reserve_seconds
        if limit < deadline:
            deadline = max(start_mono + 60.0, limit)
            note = (f"deadline-mode=job-anchored: runtime shortened from {fmt_minutes(cfg.runtime_seconds)} to "
                    f"{fmt_minutes(deadline - start_mono)} so that {fmt_minutes(cfg.post_reserve_seconds)} remain after the bots stop")
    return deadline, note


_NO_RESTART = {
    Exit.CONFIG, Exit.R2_AUTH, Exit.R2_MISSING, Exit.ARCHIVE_INVALID, Exit.DEPS,
    Exit.BOT_START, Exit.R2_UPLOAD_LOST, Exit.CONFLICT,
}


def restart_allowed(code: Exit, cfg: Config) -> bool:
    """Whether the auto-restart chain may continue (prevents hot failure loops)."""
    if code in _NO_RESTART:
        return False
    if code == Exit.INTERRUPTED:  # a person cancelling stops the chain; the job timeout does not
        left = job_time_left(cfg, False)
        return left is not None and left < 180
    return True


# -- publish -------------------------------------------------------------------- #
@dataclasses.dataclass
class PublishOutcome:
    code: Exit
    decision: str
    verification: str = ""


def estimate_publish_seconds(cfg: Config, state: State, size: int) -> Tuple[float, float]:
    """(seconds needed to delete+upload+HEAD, extra seconds for a full re-download check)."""
    original = state.section("original")
    rate = float(original.get("bytes", 0) or 0) / max(float(original.get("seconds", 1.0) or 1.0), 0.5)
    rate = max(rate, 1024.0 * 1024.0)  # never assume less than 1 MiB/s
    transfer = size / rate
    return 20.0 + 1.5 * transfer + cfg.publish_safety_seconds, 1.2 * transfer


def _can_full_verify(cfg: Config, state: State, size: int, in_recovery: bool) -> bool:
    left = job_time_left(cfg, in_recovery, _interrupt_epoch(state))
    if left is None:
        return True
    need, extra = estimate_publish_seconds(cfg, state, size)
    return left >= need + extra


def _restore_attempt(cfg: Config, store: R2Store, state: State, in_recovery: bool) -> Optional[Exit]:
    """Put the retained original archive back under the exact key and verify it.
    Returns None on success, otherwise the Exit code that describes why it failed."""
    orig = state.section("original")
    path = Path(str(orig.get("path", "")))
    sha, size = str(orig.get("sha256", "")), int(orig.get("size", 0) or 0)
    warn(f"Restoring the ORIGINAL archive to {cfg.bucket}/{cfg.key} ...")
    try:
        try:
            intact = path.is_file() and sha256_file(path) == sha
        except OSError:
            intact = False
        if not intact:
            raise StageError(Exit.HASH, "the retained local copy of the original archive is missing or corrupted")
        store.upload(path, sha, size)
        how = store.verify(sha, size, str(orig.get("md5") or "") or None,
                           _can_full_verify(cfg, state, size, in_recovery), cfg.work_dir)
    except Exception as exc:  # noqa: BLE001 - a failed restore must always be reported, never crash
        detail = exc.message if isinstance(exc, StageError) else f"{type(exc).__name__}: {exc}"
        error(f"Restoring the original archive FAILED: {detail}")
        state.update(publish_phase="restore-failed", best_effort=True)
        return exc.code if isinstance(exc, StageError) else Exit.UNEXPECTED
    state.update(publish_phase="restored", best_effort=True)
    notice(f"Original archive restored and verified ({how}).")
    return None


def restore_original(cfg: Config, store: R2Store, state: State, in_recovery: bool) -> bool:
    return _restore_attempt(cfg, store, state, in_recovery) is None


_WAITING_CANNOT_HELP = {Exit.R2_AUTH, Exit.HASH, Exit.CONFIG}


def _rescue_sleep(seconds: float) -> None:
    time.sleep(seconds)


def restore_original_persistently(cfg: Config, store: R2Store, state: State, in_recovery: bool) -> bool:
    """The object may be MISSING at this moment (it was deleted and the upload failed), so a single
    failed restore is not the end: keep trying for a bounded time - R2 or network hiccups (WARP!)
    usually pass within minutes - for as long as the job has time left."""
    failure = _restore_attempt(cfg, store, state, in_recovery)
    if failure is None:
        return True
    started, waited, attempt = time.monotonic(), 0.0, 1
    while max(time.monotonic() - started, waited) < cfg.rescue_seconds:
        if failure in _WAITING_CANNOT_HELP:
            warn("Waiting will not help (credentials rejected or the local copy of the original is unusable).")
            break
        left = job_time_left(cfg, in_recovery, _interrupt_epoch(state))
        if left is not None and left < cfg.publish_safety_seconds + 30:
            warn("Not enough job time is left to keep retrying the restore.")
            break
        pause = min(15.0 * attempt, 60.0)
        warn(f"{cfg.bucket}/{cfg.key} is still missing; retrying the restore in {pause:.0f}s (attempt {attempt + 1}).")
        _rescue_sleep(pause)
        waited += pause
        attempt += 1
        failure = _restore_attempt(cfg, store, state, in_recovery)
        if failure is None:
            return True
    return False


def _replacement_already_in_place(store: R2Store, state: State) -> bool:
    """True only when R2 provably holds the replacement already (an upload whose response was lost)."""
    new = state.section("new")
    try:
        remote = store.head()
        return remote is not None and store.matches(
            remote, str(new["sha256"]), str(new.get("md5") or "") or None, int(new["size"])) is True
    except Exception:  # noqa: BLE001 - if R2 cannot be asked, fall through to the restore
        return False


def _upload_new(cfg: Config, store: R2Store, state: State, in_recovery: bool) -> Optional[PublishOutcome]:
    """Upload the validated replacement to the SAME key. None == success."""
    new = state.section("new")
    info(f"Uploading the replacement archive ({fmt_size(int(new['size']))}) to {cfg.bucket}/{cfg.key} ...")
    try:
        store.upload(Path(str(new["path"])), str(new["sha256"]), int(new["size"]))
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the original must be put back
        detail = exc.message if isinstance(exc, StageError) else f"{type(exc).__name__}: {exc}"
        error(f"Replacement upload FAILED: {detail}")
        if _replacement_already_in_place(store, state):
            warn("The object in R2 already equals the replacement archive (the upload went through although its "
                 "response was lost); it is verified below instead of restoring the original.")
            state.update(publish_phase="uploaded", best_effort=True)
            return None
        if restore_original_persistently(cfg, store, state, in_recovery):
            error("The replacement was NOT uploaded; the original archive is back in place. This session's changes are not saved.")
            return PublishOutcome(Exit.R2_UPLOAD_RESTORED, "upload-failed-original-restored")
        error(
            "CRITICAL: the replacement upload AND the restore failed; the R2 object may be missing. "
            f"Local copies on the runner: {state.section('original').get('path')} (original) and {new.get('path')} (new)."
        )
        return PublishOutcome(Exit.R2_UPLOAD_LOST, "upload-failed-restore-failed")
    state.update(publish_phase="uploaded", best_effort=True)
    return None


def _verify_with_patience(cfg: Config, store: R2Store, state: State, full_verify: bool,
                          in_recovery: bool) -> Tuple[str, Optional[StageError]]:
    """Verify the uploaded object. A PROVEN mismatch / missing object fails at once; when R2 merely
    cannot be reached the check is repeated for a bounded time. Returns (how, None) or ("", error)."""
    new = state.section("new")
    started, waited, attempt = time.monotonic(), 0.0, 0
    while True:
        try:
            how = store.verify(str(new["sha256"]), int(new["size"]), str(new.get("md5") or "") or None,
                               full_verify, cfg.work_dir)
            return how, None
        except StageError as exc:
            problem = exc
        except Exception as exc:  # noqa: BLE001 - anything unexpected counts as a failed verification
            return "", StageError(Exit.VERIFY, f"{type(exc).__name__}: {exc}")
        if problem.code in (Exit.VERIFY, Exit.R2_MISSING, Exit.R2_AUTH, Exit.CONFIG):
            return "", problem
        if max(time.monotonic() - started, waited) >= cfg.rescue_seconds:
            return "", problem
        left = job_time_left(cfg, in_recovery, _interrupt_epoch(state))
        if left is not None and left < cfg.publish_safety_seconds + 30:
            return "", problem
        pause = min(15.0 * (attempt + 1), 60.0)
        warn(f"R2 could not be reached to verify the upload ({problem.message}); asking again in {pause:.0f}s.")
        _rescue_sleep(pause)
        waited += pause
        attempt += 1


def publish(cfg: Config, store: R2Store, state: State, in_recovery: bool = False) -> PublishOutcome:
    """Compare, and if the archive really changed: delete the old object, immediately
    upload the replacement to the same key, verify it, restore the original on failure.
    The decision is based on the *observed* remote state, so it is safe to resume."""
    orig, new = state.section("original"), state.section("new")
    orig_sha, new_sha = str(orig["sha256"]), str(new["sha256"])
    info(f"SHA-256 of the downloaded archive : {orig_sha}")
    info(f"SHA-256 of the rebuilt archive    : {new_sha}")
    if orig_sha == new_sha:
        notice("No changes detected: the rebuilt archive is byte-identical to the download. The R2 object is left untouched.")
        state.update(publish_phase="unchanged")
        return PublishOutcome(Exit.OK, "unchanged-bytes")
    info("The archive bytes differ.")
    old_manifest = load_manifest(Path(str(state.get("manifest_original"))))
    new_manifest = load_manifest(Path(str(state.get("manifest_new"))))
    added, removed, modified = diff_manifests(old_manifest, new_manifest)
    if cfg.replace_policy == "content" and old_manifest == new_manifest:
        notice(f"No changes detected: all {len(new_manifest)} files are byte-identical to the download; only the ZIP "
               "container bytes differ. The R2 object is left untouched.")
        state.update(publish_phase="unchanged-content")
        return PublishOutcome(Exit.OK, "unchanged-content")
    info(f"Changes detected: {added} added, {removed} removed, {modified} modified file(s).")

    need, extra = estimate_publish_seconds(cfg, state, int(new["size"]))
    left = job_time_left(cfg, in_recovery, _interrupt_epoch(state))
    if left is not None and left < need:
        error(f"Only {left:.0f}s of job time remain but about {need:.0f}s are needed to replace the object safely. "
              "The R2 object was NOT touched; this session's changes are not saved.")
        state.update(publish_phase="skipped-no-time")
        return PublishOutcome(Exit.NO_TIME, "skipped-no-time")
    full_verify = left is None or left >= need + extra

    for label, key, sha in (("original", "original", orig_sha), ("new", "new", new_sha)):
        path = Path(str(state.section(key).get("path", "")))
        try:
            intact = path.is_file() and sha256_file(path) == sha
        except OSError:
            intact = False
        if not intact:
            raise StageError(Exit.HASH, f"The local {label} archive is missing or changed; refusing to touch R2.")

    remote = store.head()
    state.update(publish_phase="intent", intent_at=utc_iso())
    already_new = remote is not None and store.matches(remote, new_sha, str(new.get("md5") or "") or None, int(new["size"])) is True
    same_as_original = (remote is not None and remote.size == int(orig["size"])
                        and (not orig.get("etag") or remote.etag == orig.get("etag")))
    if already_new:
        info("The R2 object already equals the rebuilt archive (resuming after an interruption); verifying it.")
    elif remote is None:
        warn(f"{cfg.bucket}/{cfg.key} does not exist right now; uploading the replacement to the exact key.")
        outcome = _upload_new(cfg, store, state, in_recovery)
        if outcome:
            return outcome
    elif same_as_original:
        info(f"Deleting the old object {cfg.bucket}/{cfg.key} ...")
        try:
            store.delete()
        except StageError as exc:
            try:
                gone = store.head() is None
            except StageError:
                gone = False
            if not gone:
                error(f"Deleting the old object FAILED: {exc.message}")
                state.update(publish_phase="delete-failed", best_effort=True)
                return PublishOutcome(Exit.R2_DELETE, "delete-failed")
            warn("DeleteObject reported an error but the object is gone; continuing with the upload.")
        state.update(publish_phase="deleted", best_effort=True)
        outcome = _upload_new(cfg, store, state, in_recovery)
        if outcome:
            return outcome
    else:
        error("The R2 object changed while the bots were running (it no longer matches the downloaded copy) and it is "
              "not the rebuilt archive. Refusing to overwrite it. The rebuilt archive stays on the runner only.")
        state.update(publish_phase="conflict", best_effort=True)
        return PublishOutcome(Exit.CONFLICT, "conflict")

    how, problem = _verify_with_patience(cfg, store, state, full_verify, in_recovery)
    if problem is not None:
        error(f"Verification of the uploaded object FAILED: {problem.message}")
        if problem.code not in (Exit.VERIFY, Exit.R2_MISSING):
            # R2 could not be asked at all (network/auth). The upload itself was accepted - R2 checks the
            # SHA-256 sent with the PUT - so overwriting it blindly with the original would be the wrong move.
            error("The replacement was uploaded but could NOT be verified because R2 was unreachable. It is left in "
                  "place, and this run is reported as failed. Compare its SHA-256 with the value logged above.")
            return PublishOutcome(Exit.VERIFY, "verify-unreachable")
        restored = restore_original_persistently(cfg, store, state, in_recovery)
        return PublishOutcome(Exit.VERIFY if restored else Exit.R2_UPLOAD_LOST,
                              "verify-failed-original-restored" if restored else "verify-failed-restore-failed")
    state.update(publish_phase="verified", best_effort=True)
    notice(f"Replacement archive uploaded to {cfg.bucket}/{cfg.key} and verified ({how}).")
    return PublishOutcome(Exit.OK, "replaced", how)


# --------------------------------------------------------------------------- #
# Lifecycle: run / recover / check-config                                     #
# --------------------------------------------------------------------------- #
def _paths(cfg: Config) -> Dict[str, Path]:
    work = cfg.work_dir
    return {
        "state": work / "state.json",
        "original": work / "original.zip",
        "new": work / "new.zip",
        "manifest_original": work / "manifest.original.json",
        "manifest_new": work / "manifest.new.json",
    }


def _build_and_record(cfg: Config, state: State, paths: Dict[str, Path]) -> None:
    """Rebuild the archive from the (stopped) bot directories, validate and hash it."""
    with group("Rebuild snowcell.zip from the bot directories"):
        manifest = build_archive(cfg.bot_root, paths["new"])
        validate_new_archive(paths["new"], manifest, cfg.max_unpacked_bytes)
        try:
            sha, md5, size = sha256_file(paths["new"]), md5_file(paths["new"]), paths["new"].stat().st_size
        except OSError as exc:
            raise StageError(Exit.HASH, f"Could not hash the new archive: {type(exc).__name__}")
        save_manifest(paths["manifest_new"], manifest)
        state.update(phase="built", manifest_new=str(paths["manifest_new"]),
                     new={"path": str(paths["new"]), "sha256": sha, "md5": md5, "size": size})
        info(f"New archive validated: {fmt_size(size)}, {len(manifest)} files, SHA-256 {sha}")


def _finish_publish(state: State, outcome: PublishOutcome) -> None:
    terminal = outcome.code in (Exit.OK, Exit.R2_UPLOAD_RESTORED, Exit.VERIFY, Exit.CONFLICT)
    state.update(phase="complete" if terminal else "publish-failed", publish_decision=outcome.decision,
                 final_exit=int(outcome.code), best_effort=True)


def run(cfg: Config, interrupt: Interrupt) -> Exit:
    paths = _paths(cfg)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    existing = State.load(paths["state"])
    if existing is not None and existing.get("phase") != "complete":
        raise StageError(Exit.CONFIG, "A previous run left an unfinished state. Run "
                         "'python scripts/setup.py --recover' first, or delete the work directory.")
    state = State(paths["state"])
    state.update(bot_root=str(cfg.bot_root), work_dir=str(cfg.work_dir))
    _RUN["owns_state"] = True

    with group("Configuration (secrets are never printed)"):
        for name, value in cfg.describe().items():
            info(f"{name:16}: {value}")
        info("R2_TOKEN_VALUE  : not used (the S3 API authenticates with the access key pair)")
    report_budget(cfg, cfg.runtime_seconds)

    ensure_boto3(cfg.python)
    store = R2Store(cfg)

    deps_errors: List[StageError] = []

    def deps_worker() -> None:
        try:
            install_dependencies(cfg.python)
        except StageError as exc:
            deps_errors.append(exc)

    deps_thread = threading.Thread(target=deps_worker, name="pip-install", daemon=True)
    deps_thread.start()  # runs while the archive downloads and extracts

    with group("Download snowcell.zip from R2"):
        remote = store.head()
        if remote is None:
            raise StageError(Exit.R2_MISSING, f"{cfg.key} does not exist in bucket {cfg.bucket}. "
                             f"Upload your initial {cfg.key} once, then run again.")
        info(f"Found {cfg.bucket}/{cfg.key} ({fmt_size(remote.size)})")
        sha, size, seconds = store.download(paths["original"], remote)
        try:
            md5 = md5_file(paths["original"])
        except OSError as exc:
            raise StageError(Exit.HASH, f"Could not hash the downloaded archive: {type(exc).__name__}")
        info(f"Downloaded {fmt_size(size)} in {seconds:.1f}s; SHA-256 {sha}")
        state.update(phase="downloaded", original={
            "path": str(paths["original"]), "sha256": sha, "md5": md5, "size": size,
            "etag": remote.etag, "seconds": seconds, "bytes": size})

    with group("Validate and extract the archive"):
        try:
            with zipfile.ZipFile(paths["original"]) as zf:
                archive = validate_archive(zf, cfg.max_unpacked_bytes)
                info(f"Archive OK: {len(archive.files)} files, {len(archive.dirs)} directories, "
                     f"{fmt_size(archive.total_unpacked)} unpacked, {archive.skipped_junk} junk entries ignored")
                prepare_destination(cfg.bot_root)
                manifest = extract_archive(zf, archive, cfg.bot_root)
        except zipfile.BadZipFile as exc:
            raise StageError(Exit.ARCHIVE_INVALID, f"The downloaded file is not a valid ZIP archive: {exc}")
        save_manifest(paths["manifest_original"], manifest)
        state.update(phase="extracted", manifest_original=str(paths["manifest_original"]))
        info(f"Extracted into {cfg.bot_root}")

    deps_thread.join()
    if deps_errors:
        raise deps_errors[0]

    env, removed = sanitized_env(os.environ)
    info(f"Bot environment: {len(removed)} credential-bearing variable(s) removed "
         f"({', '.join(removed) or 'none'}); R2/RDP/Tailscale secrets are not inherited.")
    leftovers = [n for n in env if n.upper().startswith(("R2_", "AWS_")) or n.upper() in _SENSITIVE_EXACT]
    if leftovers:  # defence in depth; cannot happen unless sanitized_env is broken
        raise StageError(Exit.CONFIG, "Internal error: credential variables survived sanitising.")

    procs = launch_bots(cfg, env)
    state.update(phase="running", bots=[{"name": b.name, "pid": b.pid} for b in procs if b.popen],
                 bots_started_at=utc_iso())
    reason, problems = "error", []  # type: str, List[str]
    try:
        startup_check(cfg, procs, interrupt.event)
        started = time.monotonic()
        deadline, note = compute_deadline(cfg, started)
        if note:
            warn(note)
        info(f"All bots launched. Shared deadline: {fmt_minutes(deadline - started)} from now "
             f"(one monotonic clock for all five; failure policy '{cfg.failure_policy}').")
        report_budget(cfg, deadline - started, title="Job-time budget (measured now that the bots are running)")
        reason = monitor_bots(cfg, procs, interrupt.event, deadline)
    finally:
        info(f"Monitoring ended ({reason}); stopping the bots.")
        if interrupt.event.is_set():  # the cancellation window is counted from this moment
            state.update(interrupt_epoch=interrupt.epoch or time.time(), best_effort=True)
        stop_cfg = dataclasses.replace(cfg, grace_seconds=min(cfg.grace_seconds, 6)) if interrupt.event.is_set() else cfg
        problems = stop_bots(stop_cfg, procs)
        problems += [f"stray process {s}" for s in kill_strays(cfg.bot_root)]
        state.update(bot_results=[{"name": b.name, "exit": b.exit_code, "crashed": b.crashed,
                                  "launch_error": bool(b.launch_error), "stop": b.stop_method} for b in procs])
    if problems:
        state.update(phase="shutdown-failed")
        raise StageError(Exit.SHUTDOWN, "Could not confirm that every bot stopped: " + "; ".join(problems)
                         + ". The archive was NOT rebuilt and the R2 object was NOT touched.")
    time.sleep(SETTLE_SECONDS)  # let Windows release SQLite/file handles
    state.update(phase="stopped", stop_reason=reason)
    info("All bot processes have stopped; nothing is writing to the bot directories any more.")

    if reason == "interrupted":
        warn(f"Interrupted by {interrupt.name or 'a signal'}: the bots are stopped, but the archive/upload is left to the "
             "always() recovery step (this process may be killed within seconds).")
        state.update(phase="interrupted", interrupt_epoch=interrupt.epoch or time.time())
        return Exit.INTERRUPTED

    _build_and_record(cfg, state, paths)
    with group("Compare SHA-256 and update R2"):
        outcome = publish(cfg, store, state)
    _finish_publish(state, outcome)

    code = outcome.code
    if code == Exit.OK:
        if any(b.launch_error for b in procs):
            code = Exit.BOT_START
        elif any(b.crashed for b in procs):
            code = Exit.BOT_CRASH
        notice("Persistent data saved: bots stopped, archive rebuilt and validated, R2 object is current.")
    state.update(final_exit=int(code), best_effort=True)
    return code


_RECOVERY = {"acted": False}  # lets main() skip the summary when recovery had nothing to do
_RUN = {"owns_state": False}  # True once run() has created its own state file in this process


def _prior_exit(state: State) -> Exit:
    """How the main step ended according to the state file. A step that was killed or cancelled
    never recorded an exit code; that is treated like an interruption (INTERRUPTED)."""
    try:
        return Exit(int(state.get("final_exit")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return Exit.INTERRUPTED


def record_final_exit(cfg: Config, code: Exit) -> None:
    """Remember how the main step ended, so --recover can make the right restart decision."""
    if not _RUN["owns_state"]:  # never touch a state file left by an earlier, unfinished run
        return
    with contextlib.suppress(Exception):
        state = State.load(_paths(cfg)["state"])
        if state is not None:
            state.update(final_exit=int(code), best_effort=True)


def _recorded_archive_is_valid(state: State) -> bool:
    """True when the replacement archive recorded by an earlier attempt still exists unchanged,
    so a resumed publish works with exactly the bytes it was already comparing and uploading."""
    new = state.section("new")
    if not new:
        return False
    try:
        return (sha256_file(Path(str(new.get("path")))) == new.get("sha256")
                and Path(str(state.get("manifest_new"))).is_file())
    except OSError:
        return False


def recover(cfg: Config) -> Exit:
    """Idempotent safety net for an ``if: always()`` step."""
    paths = _paths(cfg)
    state = State.load(paths["state"])
    if state is None:
        info("Recovery: no run state found; nothing to recover.")
        return Exit.OK
    phase = str(state.get("phase"))
    if phase == "complete":
        info("Recovery: the main step finished; nothing to do.")
        return Exit.OK
    if phase in ("init", "downloaded", "extracted"):
        info(f"Recovery: the run stopped in phase '{phase}', before any bot started; nothing to save.")
        return Exit.OK
    warn(f"Recovery: the main step ended in phase '{phase}'; finishing the job now.")
    prior = _prior_exit(state)
    if _interrupt_epoch(state) is None and state.get("final_exit") is None:
        # The main step was killed (cancel or job timeout) before it could say so: GitHub gave this
        # step the remainder of a 5-minute window that opened a few seconds ago.
        state.update(interrupt_epoch=time.time() - 15, best_effort=True)
    _RECOVERY["acted"] = True
    ensure_boto3(cfg.python)
    store = R2Store(cfg)
    survivors = kill_strays(cfg.bot_root)
    if survivors:
        raise StageError(Exit.SHUTDOWN, "Bot processes are still running: " + "; ".join(survivors)
                         + ". The R2 object was NOT touched.")
    time.sleep(SETTLE_SECONDS)
    if not _recorded_archive_is_valid(state):
        _build_and_record(cfg, state, paths)
    with group("Recovery: compare SHA-256 and update R2"):
        outcome = publish(cfg, store, state, in_recovery=True)
    _finish_publish(state, outcome)
    if outcome.code == Exit.OK:
        notice("Recovery complete: persistent data saved.")
    allowed = restart_allowed(prior, cfg) and restart_allowed(outcome.code, cfg)
    write_github_output(restart_ok=str(allowed).lower())
    return outcome.code


def check_config(cfg: Config) -> Exit:
    with group("Configuration check (offline; no secret values are printed)"):
        for name, value in cfg.describe().items():
            info(f"{name:16}: {value}")
        info("R2_TOKEN_VALUE  : not used by this script")
        report_budget(cfg, cfg.runtime_seconds)
    notice("R2 configuration is structurally valid (endpoint, account id, bucket and key).")
    return Exit.OK


def write_summary(cfg: Optional[Config], code: Exit, recovery: bool = False) -> None:
    state = State.load(_paths(cfg)["state"]) if cfg else None
    title = "## 🤖 Snow Cell bots (recovery step)" if recovery else "## 🤖 Snow Cell bots"
    lines = [title, "", "| Item | Value |", "|------|-------|",
             f"| Result | `{code.name}` (exit code {int(code)}) |"]
    if state:
        orig, new = state.section("original"), state.section("new")
        lines += [f"| Phase | `{state.get('phase')}` |",
                  f"| Publish decision | `{state.get('publish_decision', 'n/a')}` |"]
        if orig:
            lines.append(f"| Original SHA-256 | `{orig.get('sha256')}` |")
        if new:
            lines.append(f"| Rebuilt SHA-256 | `{new.get('sha256')}` |")
        results = state.get("bot_results")
        if isinstance(results, list) and results:
            lines += ["", "| Bot | Exit | Crashed | Stopped by |", "|-----|------|---------|-----------|"]
            for item in results:
                lines.append(f"| {item.get('name')} | {item.get('exit')} | {'yes' if item.get('crashed') else 'no'} | {item.get('stop') or '-'} |")
    write_step_summary(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Snow Cell host runner (see the module docstring).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="validate the environment offline and exit")
    mode.add_argument("--recover", action="store_true", help="finish an interrupted run (idempotent)")
    args = parser.parse_args(argv)
    _RECOVERY["acted"] = False  # module state is per process; tests call main() many times
    _RUN["owns_state"] = False
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    interrupt = Interrupt()
    interrupt.install()
    cfg: Optional[Config] = None
    code = Exit.UNEXPECTED
    try:
        cfg = load_config(os.environ)
        if args.check_config:
            code = check_config(cfg)
        elif args.recover:
            code = recover(cfg)
        else:
            code = run(cfg, interrupt)
    except StageError as exc:
        error(exc.message)
        code = exc.code
    except KeyboardInterrupt:
        code = Exit.INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - last-resort reporting
        import traceback

        with group("Unexpected error (traceback, secrets redacted)"):
            _emit(traceback.format_exc())
        error(f"Unexpected {type(exc).__name__}; see the traceback above.")
        code = Exit.UNEXPECTED
    if not args.check_config and not args.recover:
        if cfg is not None:
            record_final_exit(cfg, code)
        write_github_output(exit_code=int(code),
                            restart_ok=str(bool(cfg and restart_allowed(code, cfg))).lower())
    if not args.check_config and cfg is not None and (not args.recover or _RECOVERY["acted"] or code != Exit.OK):
        with contextlib.suppress(Exception):
            write_summary(cfg, code, recovery=args.recover)
    if code == Exit.OK:
        info("Done: success.")
    else:
        error(f"Finished with exit code {int(code)} ({code.name}).")
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
