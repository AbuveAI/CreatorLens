from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from creatorlens.cli import main, doctor
from creatorlens.workspace import (DEFAULT_SETTINGS, WorkspaceError, initialize,
                                   load_settings, workspace_path, validate_settings)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "work"

    def test_initialization_is_private_and_preserves_user_settings(self):
        self.assertTrue(initialize(self.root)["created_settings"])
        target = self.root / "config/settings.json"
        value = load_settings(self.root)
        value["asr"]["key_env"] = "CUSTOM_ASR_KEY"
        target.write_text(json.dumps(value))
        before = target.read_bytes()
        self.assertFalse(initialize(self.root)["created_settings"])
        self.assertEqual(before, target.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "private").stat().st_mode & 0o777, 0o700)

    def test_requires_explicit_workspace(self):
        with self.assertRaises(WorkspaceError):
            workspace_path(environ={})

    def test_cli_path_overrides_environment(self):
        actual = workspace_path(self.root, environ={"CREATORLENS_WORKSPACE": str(self.base / "other")})
        self.assertEqual(actual, self.root)

    def test_rejects_source_overlap_including_symlink(self):
        source = self.base / "source"
        source.mkdir()
        for path in (source, source / "data", self.base):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                workspace_path(path, source_root=source)
        link = self.base / "linked"
        link.symlink_to(source, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            initialize(link / "private", source_root=source)
        self.assertFalse((source / "private").exists())

    def test_rejects_linked_private_directory_without_touching_target(self):
        self.root.mkdir()
        external = self.base / "external"
        external.mkdir()
        (self.root / "private").symlink_to(external, target_is_directory=True)
        with self.assertRaises(WorkspaceError):
            initialize(self.root)
        self.assertFalse((self.root / "config").exists())
        self.assertEqual(list(external.iterdir()), [])

    def test_rejects_linked_config_without_touching_target(self):
        (self.root / "config").mkdir(parents=True)
        external = self.base / "existing.json"
        external.write_text("private content")
        (self.root / "config/settings.json").symlink_to(external)
        with self.assertRaises(WorkspaceError):
            initialize(self.root)
        self.assertEqual(external.read_text(), "private content")

    def test_invalid_existing_settings_are_not_overwritten(self):
        (self.root / "config").mkdir(parents=True)
        target = self.root / "config/settings.json"
        target.write_text("invalid secret material")
        with self.assertRaises(WorkspaceError):
            initialize(self.root)
        self.assertEqual(target.read_text(), "invalid secret material")
        self.assertFalse((self.root / "data").exists())

    def test_rejects_alternative_capture_provider(self):
        for provider in ("browser", "playwright", "xhs_downloader", "unknown"):
            value = deepcopy(DEFAULT_SETTINGS)
            value["capture"]["provider"] = provider
            with self.subTest(provider=provider), self.assertRaises(WorkspaceError):
                validate_settings(value)

    def test_rejects_browser_fallback_setting(self):
        value = deepcopy(DEFAULT_SETTINGS)
        value["capture"]["fallback"] = "browser"
        with self.assertRaises(WorkspaceError):
            validate_settings(value)

    def test_unsupported_platform_and_relative_dependency_paths(self):
        value = deepcopy(DEFAULT_SETTINGS)
        value["capture"]["platform"] = "douyin"
        with self.assertRaises(WorkspaceError):
            validate_settings(value)
        value = deepcopy(DEFAULT_SETTINGS)
        value["capture"]["spider_source"] = "../private"
        with self.assertRaises(WorkspaceError):
            validate_settings(value)

    def test_default_doctor_does_not_report_capture_ready(self):
        initialize(self.root)
        before = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")}
        result = doctor(self.root)
        self.assertFalse(result["local_dependencies_present"])
        self.assertTrue(result["capture_implemented"])
        self.assertFalse(result["release_ready"])
        self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")})

    def test_cli_does_not_print_invalid_config_contents(self):
        initialize(self.root)
        (self.root / "config/settings.json").write_text('{"secret":"DO_NOT_PRINT", broken')
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["doctor", "--workspace", str(self.root)]), 2)
        self.assertNotIn("DO_NOT_PRINT", output.getvalue())

    def test_no_browser_command_or_channel_flag(self):
        for argv in (["receive"], ["download"], ["capture", "--channel", "browser"],
                     ["doctor", "--workspace", str(self.root), "--channel", "browser"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main(argv)
            self.assertEqual(exc.exception.code, 2)

    def test_dependency_files_without_lock_do_not_mean_runtime_ready(self):
        initialize(self.root)
        source = self.base / "spider"
        (source / "apis").mkdir(parents=True)
        (source / "apis/xhs_pc_apis.py").write_text("raise RuntimeError('must not import')")
        python = self.base / "python"
        python.write_text("#!/bin/sh\nexit 99\n")
        python.chmod(0o700)
        settings = load_settings(self.root)
        settings["capture"].update(spider_source=str(source), spider_python=str(python))
        (self.root / "config/settings.json").write_text(json.dumps(settings))
        with patch("creatorlens.cli.shutil.which", return_value="/mock/tool"):
            result = doctor(self.root)
        self.assertFalse(result["local_dependencies_present"])  # Source has not been explicitly locked.
        self.assertTrue(result["capture_implemented"])
        self.assertFalse(result["release_ready"])

    def test_example_matches_default_configuration(self):
        example = Path(__file__).resolve().parent.parent / "configs/settings.example.json"
        self.assertEqual(json.loads(example.read_text()), DEFAULT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
