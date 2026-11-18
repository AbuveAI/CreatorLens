"""Only tool-issued API sessions; no browser profile or credential discovery."""
from dataclasses import asdict

from .storage import ensure, lock, read, safe_path, write


def login_expired(status, data, path):
    message = str((data or {}).get("msg") or (data or {}).get("message") or "") if isinstance(data, dict) else ""
    if any(word in message for word in ("验证", "频繁", "异常", "风控")):
        return False
    if status == 401 and path in ("/api/sns/web/v1/user/me", "/api/sns/web/v2/user/me"):
        return True
    return (isinstance(data, dict) and data.get("success") is False
            and any(word in message for word in ("登录已过期", "登录过期", "登录失效", "未登录", "请先登录")))


class SessionStore:
    def __init__(self, workspace):
        self.root = safe_path(workspace, "private/xiaohongshu")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = safe_path(workspace, "private/xiaohongshu/session.json")
        self.lock_path = safe_path(workspace, "private/xiaohongshu/session.lock")

    def lock(self):
        return lock(self.lock_path)

    def save(self, auth):
        local, session = auth.profile.browser_storage_snapshot()
        p = auth.profile
        data = {"schema_version": 1, "cookies": p.cookie_map,
                "kwargs": {"local_storage": local, "session_storage": session,
                           "host_cookie_state": auth._cookie_store.export_state(),
                           "web_build": p.web_build, "rap_fingerprint_hex": p.rap_fingerprint_hex,
                           "web_profile_fields": p.web_profile_fields, "web_profile_i12_seed": p.web_profile_i12_seed,
                           "web_profile_fi": p.web_profile_fi, "mns_env": auth.mns_env},
                "b1_state": asdict(p.b1_state) if p.b1_state else None}
        write(self.path, data)

    def load(self, auth_class, b1_class):
        if not self.path.exists():
            return None
        data = read(self.path)
        ensure(data.get("schema_version") == 1 and isinstance(data.get("cookies"), dict)
               and data["cookies"].get("web_session") and isinstance(data.get("kwargs"), dict),
               "session_file_invalid_preserved")
        kwargs = dict(data["kwargs"])
        if data.get("b1_state"):
            kwargs["b1_state"] = b1_class(**data["b1_state"])
        # Upstream validates through user/me; a stored file is not proof of login.
        return auth_class.from_cookie(data["cookies"], **kwargs)

    def invalidate(self):
        self.path.unlink(missing_ok=True)
