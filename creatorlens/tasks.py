"""Workspace-local tasks with immutable sampling contracts."""
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlparse

from . import __version__
from .storage import digest, ensure, lock, read, safe_path, write
from .workspace import load_settings, workspace_path

ID = re.compile(r"[0-9a-f]{24}")
BATCH = re.compile(r"[A-Za-z0-9_-]{1,80}")
LIMITS = {"max_pages_per_run": 5, "max_requests_per_run": 120,
          "request_gap_seconds": 12, "comment_pages_per_work": 1, "reply_pages_per_work": 2}


def now():
    return datetime.now(timezone.utc).isoformat()


def account_from_url(url):
    try:
        parsed = urlparse(url)
        match = re.fullmatch(r"/user/profile/([0-9a-f]{24})/?", parsed.path)
        ensure(parsed.scheme == "https" and parsed.hostname in ("xiaohongshu.com", "www.xiaohongshu.com")
               and not parsed.username and not parsed.password and parsed.port in (None, 443)
               and match, "invalid_xiaohongshu_profile_url")
    except (TypeError, ValueError):
        from .storage import StopCapture
        raise StopCapture("invalid_xiaohongshu_profile_url") from None
    return match.group(1)


def create(workspace, *, url, name, batch, count=10):
    workspace = workspace_path(workspace)
    load_settings(workspace)
    account = account_from_url(url)
    ensure(isinstance(name, str) and name.strip(), "creator_name_required")
    ensure(isinstance(batch, str) and BATCH.fullmatch(batch), "invalid_batch_id")
    ensure(type(count) is int and 1 <= count <= 500, "invalid_target_count")
    relative = f"data/xiaohongshu/{account}/batches/{batch}/task.json"
    path = safe_path(workspace, relative)
    ensure(not path.parent.exists(), "batch_already_exists_use_status")
    path.parent.mkdir(parents=True, mode=0o700)
    write(path, {"schema_version": 1, "tool_version": __version__, "platform": "xiaohongshu",
                 "provider": "spider_xhs", "account_id": account, "creator_name": name.strip(),
                 "profile_url": f"https://www.xiaohongshu.com/user/profile/{account}",
                 "batch_id": batch, "target_count": count, "max_video_seconds": 1200,
                 "created_at": now(), "limits": LIMITS})
    return Task(workspace, path)


class Task:
    def __init__(self, workspace, path):
        self.workspace = workspace_path(workspace)
        load_settings(self.workspace)
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        ensure(path.is_relative_to(self.workspace), "task_outside_workspace")
        self.path = safe_path(self.workspace, path.relative_to(self.workspace))
        self.config = c = read(self.path)
        ensure(c.get("schema_version") == 1 and c.get("platform") == "xiaohongshu"
               and c.get("provider") == "spider_xhs", "unsupported_task_contract")
        ensure(account_from_url(c.get("profile_url")) == c.get("account_id"), "account_mismatch")
        ensure(isinstance(c.get("batch_id"), str) and BATCH.fullmatch(c["batch_id"]), "invalid_batch_id")
        expected = f"data/xiaohongshu/{c['account_id']}/batches/{c['batch_id']}/task.json"
        ensure(self.path == safe_path(self.workspace, expected), "task_location_mismatch")
        ensure(type(c.get("target_count")) is int and 1 <= c["target_count"] <= 500, "invalid_target_count")
        ensure(c.get("max_video_seconds") == 1200 and c.get("limits") == LIMITS,
               "sampling_contract_changed")
        self.root = self.path.parent
        self.fingerprint = digest(c)

    def p(self, relative):
        return safe_path(self.workspace, (self.root / relative).relative_to(self.workspace))

    def get(self, relative, default=None):
        path = self.p(relative)
        return read(path) if path.exists() else default

    def save(self, relative, value):
        write(self.p(relative), value)

    def lock(self):
        return lock(self.p(".task.lock"))

    def cached(self, relative, fetch):
        path = self.p(relative)
        if path.exists():
            envelope = read(path)
            ensure(envelope.get("sha256") == digest(envelope.get("payload")), "raw_evidence_changed")
            return envelope["payload"]
        value = fetch()
        write(path, {"sha256": digest(value), "payload": value})
        return value

    def status(self):
        state = self.get("state.json", {})
        return {"task": str(self.path), "platform": self.config["platform"], "provider": "spider_xhs",
                "batch_id": self.config["batch_id"], "phase": state.get("phase", "new"),
                "stop_reason": state.get("stop_reason"), "requested": self.config["target_count"],
                "selected_count": len(state.get("selected", [])), "list_exhausted": state.get("list_exhausted", False),
                "research_complete": False}
