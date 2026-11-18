"""Local evidence files, private permissions and explicit single-writer locks."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile


class StopCapture(RuntimeError):
    """Only fixed, non-sensitive reason codes should be passed here."""


def ensure(condition, reason):
    if not condition:
        raise StopCapture(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_path(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    ensure(not relative.is_absolute() and ".." not in relative.parts, "invalid_relative_path")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        ensure(not candidate.is_symlink(), "symlink_not_allowed")
    ensure(candidate.resolve().is_relative_to(root), "path_outside_workspace")
    return candidate


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise StopCapture("local_json_unreadable") from None


def write(path, value):
    path = Path(path)
    ensure(not path.is_symlink(), "symlink_not_allowed")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".creatorlens-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def lock(path):
    try:
        import fcntl
    except ImportError:
        raise StopCapture("platform_lock_not_supported") from None
    path = Path(path)
    ensure(not path.is_symlink(), "symlink_not_allowed")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopCapture("workspace_or_task_busy") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def write_text(path, content):
    """Atomic UTF-8 output with the same privacy permissions as JSON records."""
    path = Path(path)
    ensure(not path.is_symlink(), 'symlink_not_allowed')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.creatorlens-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
