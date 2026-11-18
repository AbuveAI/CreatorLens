"""Explicit source fingerprints: upgrades are deliberate, never automatic pulls."""
from pathlib import Path
import os
import subprocess

from .storage import digest, ensure, file_hash, read, safe_path, write
from .tasks import now
from .workspace import SOURCE_ROOT, load_settings


def locations(workspace):
    config = load_settings(workspace)["capture"]
    ensure(config["spider_source"] and config["spider_python"], "configure_spider_source_and_python")
    source = Path(config["spider_source"]).expanduser().resolve()
    python = Path(config["spider_python"]).expanduser()  # Keep venv symlink semantics.
    ensure(not source.is_relative_to(SOURCE_ROOT), "third_party_source_must_be_external")
    ensure((source / "apis/xhs_pc_apis.py").is_file(), "spider_api_missing")
    ensure(python.is_file() and os.access(python, os.X_OK), "spider_python_missing")
    return source, python


def fingerprint(source):
    paths = []
    for name in ("apis", "xhs_utils"):
        folder = source / name
        ensure(folder.is_dir() and not folder.is_symlink(), "spider_source_layout_invalid")
        for path in folder.rglob("*"):
            ensure(not path.is_symlink(), "spider_source_symlink_unsupported")
            if path.is_file() and path.suffix in (".py", ".js", ".json"):
                if "__pycache__" not in path.parts:
                    paths.append(path)
    for name in ("requirements.txt", "package.json", "package-lock.json"):
        path = source / name
        ensure(not path.is_symlink(), "spider_source_symlink_unsupported")
        if path.is_file():
            paths.append(path)
    files = {str(path.relative_to(source)): file_hash(path) for path in sorted(paths)}
    return {"sha256": digest(files), "files": files}


def pin(workspace, *, replace=False):
    source, _ = locations(workspace)
    target = safe_path(workspace, "config/spider-xhs.lock.json")
    ensure(not target.exists() or replace, "dependency_already_locked_use_replace_after_review")
    record = {"schema_version": 1, "provider": "spider_xhs", "locked_at": now(),
              "upstream": "https://github.com/cv-cat/Spider_XHS", **fingerprint(source),
              "validation": "local_source_fingerprint_only_not_online_acceptance"}
    if (source / ".git").exists():
        proc = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True)
        import re
        if proc.returncode == 0 and re.fullmatch(r"[a-f0-9]{40,64}", proc.stdout.strip()):
            record["git_commit"] = proc.stdout.strip()
    write(target, record)
    return {"dependency_locked": True, "source_hash": record["sha256"], "online_validated": False}


def validate(workspace):
    source, python = locations(workspace)
    target = safe_path(workspace, "config/spider-xhs.lock.json")
    ensure(target.is_file(), "dependency_lock_required")
    record = read(target)
    current = fingerprint(source)
    ensure(record.get("schema_version") == 1 and record.get("provider") == "spider_xhs"
           and record.get("sha256") == current["sha256"] and record.get("files") == current["files"],
           "dependency_changed_review_before_upgrade")
    return source, python, current["sha256"]
