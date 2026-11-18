"""Thin adapter around an externally installed Spider_XHS; no upstream core copied."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode, urlparse

from .session import SessionStore, login_expired
from .storage import StopCapture, ensure
from .tasks import now


class RequestGuard:
    def __init__(self, task, *, clock=time.monotonic, sleep=time.sleep):
        self.task, self.clock, self.sleep = task, clock, sleep
        self.last = None
        self.count = 0
        self.polls = 0
        self.fatal = None
        self.expired = False
        self.audit = []

    def stop(self, reason):
        self.fatal = reason
        raise StopCapture(reason)

    def before(self, url, *, media=False):
        if self.fatal:
            raise StopCapture(self.fatal)
        parsed = urlparse(url)
        host = parsed.hostname or ""
        allowed = (host.endswith(".xhscdn.com") or host.endswith(".xiaohongshu.com")) if media else (
            host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
        if parsed.scheme not in ("http", "https") or not allowed or parsed.username or parsed.password:
            self.stop("unexpected_network_destination")
        if not media and any(part in parsed.path for part in
                             ("/comment/post", "/follow/", "/like/", "/collect/", "/publish", "/upload", "/im/")):
            self.stop("write_endpoint_not_supported")
        if self.count >= self.task.config["limits"]["max_requests_per_run"]:
            self.stop("request_budget_reached")
        polling = parsed.path == "/api/qrcode/userinfo"
        if polling and self.polls >= 12:
            self.stop("qr_poll_budget_reached")
        gap = 10 if polling else self.task.config["limits"]["request_gap_seconds"]
        if self.last is not None:
            self.sleep(max(0, gap - (self.clock() - self.last)))
        self.count += 1
        self.polls += int(polling)
        row = {"at": now(), "host": host, "kind": "media" if media else "api", "number": self.count}
        self.audit.append(row)
        return row

    def finished(self, row, *, status=None, error=None):
        self.last = self.clock()
        row.update(status=status, error=error)
        self.task.save("request-audit.json", {"requests": self.audit, "no_headers_or_query_strings": True})

    def check_response(self, response, path, *, method=None, url=None):
        try:
            data = response.json()
        except (ValueError, TypeError):
            data = None
        if response.headers.get("verifyuuid"):
            self.stop("platform_verification_required")
        if login_expired(response.status_code, data, path):
            self.expired = True
            self.stop("session_expired_run_again_for_qr")
        if isinstance(data, dict):
            message = str(data.get("msg") or data.get("message") or "")
            if any(word in message for word in ("验证码", "验证后", "访问频繁", "访问异常")):
                self.stop("platform_verification_required")
            if data.get("success") is False:
                self.stop("platform_business_rejected")
        # Spider_XHS intentionally reads this unfollowed response for initial cookies,
        # then explicitly requests /explore. This is not an API redirect allowance.
        location = response.headers.get("location") or response.headers.get("Location")
        if (response.status_code == 302 and method == "GET"
                and url == "https://www.xiaohongshu.com/"
                and location in ("/explore", "https://www.xiaohongshu.com/explore")):
            return "expected_login_navigation_302"
        if response.status_code >= 300:
            self.stop("platform_http_rejected")

    @contextmanager
    def install(self, client_class):
        original = client_class.request
        guard = self

        def request(client, method, url, **kwargs):
            row = guard.before(url)
            try:
                # Do not follow redirects into another host or request path unnoticed.
                kwargs["allow_redirects"] = False
                response = original(client, method, url, **kwargs)
            except Exception:
                guard.finished(row, error="network_error")
                guard.stop("network_error_no_automatic_retry")
            try:
                kind = guard.check_response(response, urlparse(url).path, method=str(method).upper(), url=url)
                if kind:
                    row["response_kind"] = kind
            except StopCapture:
                row["stop_reason"] = guard.fatal
                raise
            finally:
                guard.finished(row, status=response.status_code)
            return response

        client_class.request = request
        try:
            yield
        except Exception:
            # Upstream login may swallow StopCapture and raise a generic error.
            # Preserve our safe stop reason without exposing upstream details.
            if guard.fatal:
                raise StopCapture(guard.fatal) from None
            raise
        finally:
            client_class.request = original


def image_urls(card):
    items = card.get("image_list")
    ensure(isinstance(items, list) and items, "image_list_empty")
    result = []
    for item in items:
        variants = item.get("info_list") or []
        preferred = [v for v in variants if v.get("image_scene") == "WB_DFT"]
        url = next((v.get("url") for v in preferred + variants if v.get("url")), None)
        url = url or item.get("url_default") or item.get("url")
        ensure(isinstance(url, str) and url, "image_source_missing")
        result.append(url)
    return result


def describe(raw, note_id, account_id, video_resolver):
    ensure(raw.get("success") is True, "detail_unsuccessful")
    items = (raw.get("data") or {}).get("items")
    ensure(isinstance(items, list) and len(items) == 1, "detail_items_invalid")
    item = items[0]
    card = item.get("note_card") or {}
    ensure((item.get("id") or card.get("note_id")) == note_id
           and (card.get("user") or {}).get("user_id") == account_id, "detail_identity_mismatch")
    ensure(card.get("type") in ("video", "normal"), "unknown_work_type")
    images = image_urls(card)
    result = {"id": note_id, "author_id": account_id, "type": "video" if card["type"] == "video" else "image",
              "published_ms": card.get("time"), "title": card.get("title", ""), "body": card.get("desc", ""),
              "metrics": card.get("interact_info") or {}, "image_urls": images}
    if result["type"] == "video":
        duration = ((card.get("video") or {}).get("capa") or {}).get("duration")
        result["duration_seconds"] = duration if type(duration) in (int, float) and math.isfinite(duration) else None
        result["video_url"] = video_resolver(item)
        ensure(isinstance(result["video_url"], str) and result["video_url"], "video_source_missing")
    return result


class SpiderProvider:
    def __init__(self, api, guard, parser):
        self.api, self.guard, self.parser = api, guard, parser

    def result(self, value):
        if self.guard.fatal:
            raise StopCapture(self.guard.fatal)
        ensure(isinstance(value, tuple) and len(value) == 3 and value[0] is True
               and isinstance(value[2], dict), "api_operation_failed")
        return value[2]

    def profile(self, account):
        return self.result(self.api.get_user_info(account))

    def page(self, account, cursor):
        return self.result(self.api.get_user_note_info(account, cursor, "", "pc_user"))

    def detail(self, note_id, token):
        url = f"https://www.xiaohongshu.com/explore/{note_id}?" + urlencode({"xsec_token": token, "xsec_source": "pc_user"})
        return self.result(self.api.get_note_info(url))

    def comments(self, note_id, token):
        return self.result(self.api.get_note_out_comment(note_id, "", token))

    def replies(self, note_id, root, cursor, token):
        return self.result(self.api.get_note_inner_comment({**root, "note_id": note_id}, cursor, token))

    def describe(self, raw, note_id, account_id):
        def resolve(item):
            parsed = self.parser({**item, "url": f"https://www.xiaohongshu.com/explore/{note_id}"})
            return parsed.get("video_addr")
        return describe(raw, note_id, account_id, resolve)

    def download(self, url, path):
        import requests
        row = self.guard.before(url, media=True)
        partial = path.with_suffix(".part")
        ensure(not partial.is_symlink(), "symlink_not_allowed")
        try:
            with requests.get(url, stream=True, timeout=(20, 90), allow_redirects=False) as response:
                self.guard.finished(row, status=response.status_code)
                if response.status_code != 200:
                    self.guard.stop("media_http_rejected")
                with partial.open("wb") as stream:
                    for block in response.iter_content(1024 * 1024):
                        stream.write(block)
                partial.replace(path)
        except requests.RequestException:
            self.guard.finished(row, error="media_network_error")
            self.guard.stop("media_network_error_no_automatic_retry")
        finally:
            self.guard.last = self.guard.clock()

    def inspect(self, path, kind):
        if kind == "video":
            proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                   "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
            ensure(proc.returncode == 0, "video_probe_failed")
            data = json.loads(proc.stdout)
            ensure(any(s.get("codec_type") == "video" for s in data.get("streams", [])), "video_stream_missing")
            duration = float(data["format"]["duration"])
            ensure(math.isfinite(duration) and duration > 0, "video_duration_unknown")
            return {"duration_seconds": duration, "extension": ".mp4"}
        from PIL import Image
        with Image.open(path) as image:
            fmt, size = image.format, image.size
            image.verify()
        ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif", "GIF": ".gif"}.get(fmt)
        ensure(ext and min(size) > 0, "image_format_invalid")
        return {"extension": ext, "format": fmt, "width": size[0], "height": size[1]}


@contextmanager
def connect(task, source, emit):
    """Executed only in the selected third-party interpreter, under task lock."""
    store = SessionStore(task.workspace)
    with store.lock():
        previous_cwd = Path.cwd()
        sys.path.insert(0, str(source))
        auth = None
        guard = RequestGuard(task)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            try:
                os.chdir(source)
                from loguru import logger
                logger.remove()
                from xhs_utils.xhs_core.http import BrowserHttpClient
                from apis.xhs_pc_login_apis import XHSLoginApi
                from xhs_utils.xhs_pc import XHSPcAuth
                from xhs_utils.xhs_pc.state import B1RuntimeState
                from apis.xhs_pc_apis import XHS_Apis
                from xhs_utils.data_util import handle_note_info
                original_qr = XHSLoginApi.__dict__["show_qrcode_image"]
                original_login = XHSLoginApi.qrcode_login

                def show_qr(url):
                    import qrcode
                    path = task.p("login-qr.png")
                    qrcode.make(url).save(path)
                    path.chmod(0o600)
                    emit("qr_ready", path=str(path))

                def login(client, **kwargs):
                    return original_login(client, show_in_terminal=False, timeout_seconds=150, poll_interval=10.0)

                XHSLoginApi.show_qrcode_image = staticmethod(show_qr)
                XHSLoginApi.qrcode_login = login
                try:
                    with guard.install(BrowserHttpClient):
                        emit("session_check")
                        auth = store.load(XHSPcAuth, B1RuntimeState)
                        if auth is None:
                            emit("login_required")
                            auth = XHSPcAuth.from_qrcode_login(show_in_terminal=False)
                        ensure(not guard.fatal, guard.fatal or "login_failed")
                        store.save(auth)
                        emit("session_ready")
                        yield SpiderProvider(XHS_Apis(auth), guard, handle_note_info)
                finally:
                    XHSLoginApi.show_qrcode_image = original_qr
                    XHSLoginApi.qrcode_login = original_login
            finally:
                try:
                    if guard.expired:
                        store.invalidate()
                    elif auth is not None:
                        store.save(auth)
                finally:
                    if auth is not None:
                        auth.close()
                    os.chdir(previous_cwd)
                    sys.path.remove(str(source))
