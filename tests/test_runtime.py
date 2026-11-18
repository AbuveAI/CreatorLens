from contextlib import redirect_stdout
from dataclasses import dataclass
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from creatorlens import dependency
from creatorlens.cli import main
from creatorlens.session import SessionStore, login_expired
from creatorlens.spider import RequestGuard, SpiderProvider
from creatorlens.storage import StopCapture, read, write
from creatorlens.tasks import create
from creatorlens.workspace import initialize, load_settings


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.workspace = self.base / "workspace"
        initialize(self.workspace)
        self.task = create(self.workspace, url="https://www.xiaohongshu.com/user/profile/" + "a" * 24,
                           name="Synthetic", batch="runtime")

    def guard(self):
        return RequestGuard(self.task, clock=lambda: 100, sleep=lambda _: None)

    def test_rate_limit_preserves_session_and_stops_retry(self):
        guard = self.guard()
        calls = []
        class Client:
            def request(self, method, url, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(status_code=429, headers={}, json=lambda: {"success": False, "msg": "访问频繁"})
        original = Client.request
        with guard.install(Client):
            for _ in range(2):
                with self.assertRaises(StopCapture):
                    Client().request("GET", "https://www.xiaohongshu.com/api/sns/web/v2/user/me?token=SECRET")
        self.assertEqual(len(calls), 1)
        self.assertIs(Client.request, original)
        self.assertFalse(guard.expired)
        self.assertFalse(calls[0]["allow_redirects"])
        self.assertNotIn("SECRET", self.task.p("request-audit.json").read_text())

    def test_verification_does_not_mark_expired(self):
        guard = self.guard()
        response = SimpleNamespace(status_code=401, headers={"verifyuuid": "synthetic"}, json=lambda: {})
        with self.assertRaisesRegex(StopCapture, "verification"):
            guard.check_response(response, "/api/sns/web/v2/user/me")
        self.assertFalse(guard.expired)

    def test_swallowed_login_redirect_preserves_safe_stop_reason(self):
        guard = self.guard()
        calls = []

        class Client:
            def request(self, method, url, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(status_code=302, headers={}, json=lambda: {})

        original = Client.request
        with self.assertRaisesRegex(StopCapture, '^platform_http_rejected$'):
            with guard.install(Client):
                try:
                    Client().request('GET', 'https://www.xiaohongshu.com/')
                except Exception:
                    pass  # Mimic upstream QR login returning no authenticated Cookie.
                raise RuntimeError('private upstream error must not be exposed')
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]['allow_redirects'])
        self.assertIs(Client.request, original)
        self.assertEqual(self.task.get('request-audit.json')['requests'][0]['status'], 302)

    def test_unrelated_runtime_error_is_not_relabelled(self):
        class Client:
            request = Mock()

        original = Client.request
        with self.assertRaisesRegex(RuntimeError, '^synthetic failure$'):
            with self.guard().install(Client):
                raise RuntimeError('synthetic failure')
        self.assertIs(Client.request, original)

    def test_only_explicit_login_expiry_invalidates_session(self):
        self.assertTrue(login_expired(401, {}, "/api/sns/web/v2/user/me"))
        self.assertFalse(login_expired(401, {}, "/api/sns/web/v1/feed"))
        self.assertFalse(login_expired(429, {"success": False, "msg": "验证异常请先登录"}, "/other"))
        self.assertTrue(login_expired(200, {"success": False, "msg": "登录已过期"}, "/other"))

    def test_network_exception_never_leaks_body_or_retries(self):
        guard = self.guard()
        calls = []
        class Client:
            def request(self, *args, **kwargs):
                calls.append(1)
                raise RuntimeError("SECRET_COOKIE")
        with guard.install(Client):
            for _ in range(2):
                with self.assertRaisesRegex(StopCapture, "network_error_no_automatic_retry"):
                    Client().request("GET", "https://www.xiaohongshu.com/api/sns/web/v2/user/me")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("SECRET_COOKIE", self.task.p("request-audit.json").read_text())

    def test_write_endpoint_and_external_host_rejected_before_call(self):
        for url in ("https://www.xiaohongshu.com/api/comment/post", "https://example.org/a",
                    "https://xiaohongshu.com.evil.test/a", "file:///tmp/a"):
            with self.subTest(url=url), self.assertRaises(StopCapture):
                self.guard().before(url)

    def test_request_and_qr_budgets_stop(self):
        guard = self.guard()
        guard.count = 120
        with self.assertRaisesRegex(StopCapture, "request_budget"):
            guard.before("https://www.xiaohongshu.com/read")
        guard = self.guard()
        guard.polls = 12
        with self.assertRaisesRegex(StopCapture, "qr_poll_budget"):
            guard.before("https://www.xiaohongshu.com/api/qrcode/userinfo")

    def test_login_navigation_302_is_returned_without_following(self):
        guard = self.guard()
        calls = []
        cookie_marker = object()
        responses = [SimpleNamespace(status_code=302, headers={"Location": "/explore"},
                                     cookies=cookie_marker, json=lambda: None),
                     SimpleNamespace(status_code=200, headers={}, json=lambda: None)]
        class Client:
            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return responses[len(calls) - 1]
        original = Client.request
        with guard.install(Client):
            client = Client()
            first = client.request("GET", "https://www.xiaohongshu.com/")
            self.assertIs(first.cookies, cookie_marker)
            self.assertEqual(len(calls), 1)  # No hidden Location request.
            client.request("GET", "https://www.xiaohongshu.com/explore")
        self.assertIs(Client.request, original)
        self.assertTrue(all(not c[2]["allow_redirects"] for c in calls))
        self.assertIsNone(guard.fatal)
        self.assertEqual(self.task.get("request-audit.json")["requests"][0]["response_kind"],
                         "expected_login_navigation_302")

    def test_navigation_requires_exact_source_method_status_and_target(self):
        cases = [
            ("POST", "https://www.xiaohongshu.com/", 302, "/explore"),
            ("GET", "http://www.xiaohongshu.com/", 302, "/explore"),
            ("GET", "https://www.xiaohongshu.com/?token=SECRET", 302, "/explore"),
            ("GET", "https://www.xiaohongshu.com/api/sns/web/v2/user/me", 302, "/explore"),
            ("GET", "https://www.xiaohongshu.com/explore", 302, "/explore"),
            ("GET", "https://www.xiaohongshu.com/", 301, "/explore"),
            ("GET", "https://www.xiaohongshu.com/", 302, "https://example.org/explore"),
            ("GET", "https://www.xiaohongshu.com/", 302, "//evil.test/explore"),
            ("GET", "https://www.xiaohongshu.com/", 302, "/explore?token=SECRET"),
            ("GET", "https://www.xiaohongshu.com/", 302, "/explore#SECRET"),
            ("GET", "https://www.xiaohongshu.com/", 302, "/captcha"),
            ("GET", "https://www.xiaohongshu.com/", 302, None),
        ]
        for method, url, status, location in cases:
            with self.subTest(method=method, url=url, location=location):
                guard = self.guard()
                class Client:
                    def request(self, *args, **kwargs):
                        return SimpleNamespace(status_code=status, headers={"location": location}, json=lambda: None)
                with self.assertRaisesRegex(StopCapture, "platform_http_rejected"):
                    with guard.install(Client):
                        Client().request(method, url)
                self.assertNotIn("SECRET", self.task.p("request-audit.json").read_text())

    def test_absolute_expected_navigation_target_is_allowed(self):
        guard = self.guard()
        response = SimpleNamespace(status_code=302, headers={"location": "https://www.xiaohongshu.com/explore"}, json=lambda: None)
        self.assertEqual(guard.check_response(response, "/", method="GET", url="https://www.xiaohongshu.com/"),
                         "expected_login_navigation_302")

    def test_navigation_exception_never_overrides_verification_or_business_failure(self):
        for headers, payload in [({"verifyuuid": "SECRET"}, None), ({}, {"success": False}),
                                 ({}, {"msg": "验证码"})]:
            guard = self.guard()
            response = SimpleNamespace(status_code=302, headers={"location": "/explore", **headers}, json=lambda: payload)
            with self.assertRaises(StopCapture):
                guard.check_response(response, "/", method="GET", url="https://www.xiaohongshu.com/")
            self.assertFalse(guard.expired)

    def test_navigation_response_without_request_context_is_rejected(self):
        response = SimpleNamespace(status_code=302, headers={"location": "/explore"}, json=lambda: None)
        with self.assertRaisesRegex(StopCapture, "platform_http_rejected"):
            self.guard().check_response(response, "/")

    def configure_dependency(self):
        source = self.base / "spider"
        (source / "apis").mkdir(parents=True)
        (source / "xhs_utils").mkdir()
        (source / "apis/xhs_pc_apis.py").write_text("# synthetic source\n")
        python = self.base / "env/bin/python"
        python.parent.mkdir(parents=True)
        target = self.base / "fake-python"
        target.write_text("#!/bin/sh\nexit 99\n")
        target.chmod(0o700)
        python.symlink_to(target)
        settings = load_settings(self.workspace)
        settings["capture"].update(spider_source=str(source), spider_python=str(python))
        write(self.workspace / "config/settings.json", settings)
        return source, python

    def test_dependency_pin_detects_updates_and_preserves_venv_path(self):
        source, python = self.configure_dependency()
        dependency.pin(self.workspace)
        self.assertEqual(dependency.validate(self.workspace)[1], python)
        (source / "apis/xhs_pc_apis.py").write_text("# changed source\n")
        with self.assertRaisesRegex(StopCapture, "dependency_changed"):
            dependency.validate(self.workspace)
        with self.assertRaisesRegex(StopCapture, "already_locked"):
            dependency.pin(self.workspace)
        dependency.pin(self.workspace, replace=True)
        dependency.validate(self.workspace)

    def test_dependency_pin_excludes_private_files(self):
        source, _ = self.configure_dependency()
        (source / ".env").write_text("SECRET")
        dependency.pin(self.workspace)
        record = read(self.workspace / "config/spider-xhs.lock.json")
        self.assertNotIn(".env", record["files"])
        self.assertNotIn("SECRET", json.dumps(record))

    def test_session_roundtrip_validates_through_upstream_factory(self):
        store = SessionStore(self.workspace)
        profile = SimpleNamespace(cookie_map={"web_session": "SYNTHETIC_SESSION"}, web_build="test",
                                  rap_fingerprint_hex="", web_profile_fields={}, web_profile_i12_seed=0,
                                  web_profile_fi="", b1_state=None,
                                  browser_storage_snapshot=lambda: ({}, {}))
        auth = SimpleNamespace(profile=profile, mns_env={},
                               _cookie_store=SimpleNamespace(export_state=lambda: {}))
        store.save(auth)
        factory = Mock()
        factory.from_cookie.return_value = auth
        self.assertIs(store.load(factory, object), auth)
        factory.from_cookie.assert_called_once()
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.root.stat().st_mode & 0o777, 0o700)

    def test_invalid_session_is_not_silently_deleted_or_rescanned(self):
        store = SessionStore(self.workspace)
        store.path.write_text("invalid private data")
        with self.assertRaises(StopCapture):
            store.load(Mock(), object)
        self.assertEqual(store.path.read_text(), "invalid private data")

    def test_session_lock_serializes_across_tasks(self):
        first, second = SessionStore(self.workspace), SessionStore(self.workspace)
        with first.lock():
            with self.assertRaisesRegex(StopCapture, "busy"):
                with second.lock():
                    pass

    def test_cli_init_and_status_do_not_require_spider(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["init", "--workspace", str(self.workspace), "--url", self.task.config["profile_url"],
                         "--name", "Synthetic", "--batch", "cli"])
        self.assertEqual(code, 0)
        path = json.loads(output.getvalue())["task"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["status", "--workspace", str(self.workspace), "--task", path]), 0)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Image decoding uses the Spider_XHS environment's Pillow")
    def test_real_image_decode_and_corruption(self):
        from PIL import Image
        path = self.base / "image.png"
        Image.new("RGB", (7, 5), (20, 40, 60)).save(path)
        provider = SpiderProvider(None, self.guard(), None)
        info = provider.inspect(path, "image")
        self.assertEqual((info["width"], info["height"]), (7, 5))
        path.write_bytes(b"not an image")
        with self.assertRaises(Exception):
            provider.inspect(path, "image")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_video_probe(self):
        path = self.base / "video.mp4"
        proc = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=16x16:r=10",
                               "-t", "0.5", "-c:v", "mpeg4", str(path)], capture_output=True)
        self.assertEqual(proc.returncode, 0)
        provider = SpiderProvider(None, self.guard(), None)
        info = provider.inspect(path, "video")
        self.assertGreater(info["duration_seconds"], 0)
        self.assertLess(info["duration_seconds"], 1)


if __name__ == "__main__":
    unittest.main()
