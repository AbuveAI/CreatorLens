import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from creatorlens import setup
from creatorlens.release import package
from creatorlens.storage import StopCapture, read, write
from creatorlens.workspace import initialize


class SetupReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'workspace'
        initialize(self.root)

    def test_install_resume_does_not_repeat_successful_steps_and_complete_is_local(self):
        calls = []
        fail = [True]
        def runner(command, cwd):
            calls.append(command)
            if command[:2] == ['git', 'checkout']:
                (cwd / 'apis').mkdir(exist_ok=True)
                (cwd / 'apis/xhs_pc_apis.py').write_text('# fixture')
                (cwd / 'xhs_utils').mkdir(exist_ok=True)
                (cwd / 'requirements.txt').write_text('')
            if 'venv' in command:
                exe = cwd / '.venv/bin/python'
                exe.parent.mkdir(parents=True, exist_ok=True)
                exe.write_text('# fixture')
                exe.chmod(0o700)
            if 'pip' in command and fail[0]:
                fail[0] = False
                raise StopCapture('test_install_interruption')
            return setup.REVISION if command == ['git', 'rev-parse', 'HEAD'] else ''
        with patch('creatorlens.setup.shutil.which', return_value='/synthetic/bin'):
            with self.assertRaisesRegex(StopCapture, 'test_install_interruption'):
                setup.install(self.root, runner=runner)
            self.assertFalse(read(self.root / 'config/settings.json')['capture']['spider_source'])
            setup.install(self.root, runner=runner)
            self.assertEqual(sum(c[:2] == ['git', 'fetch'] for c in calls), 1)
            setup.install(self.root, runner=lambda *_: self.fail('repeat must not contact network'))

    def test_existing_source_is_not_adopted_or_overwritten(self):
        source = self.root / 'dependencies/Spider_XHS'
        source.mkdir(parents=True)
        (source / 'mine').write_text('keep')
        with patch('creatorlens.setup.shutil.which', return_value='/synthetic/bin'):
            with self.assertRaisesRegex(StopCapture, 'not_overwritten'):
                setup.install(self.root, runner=lambda *_: self.fail('no command'))
        self.assertEqual((source / 'mine').read_text(), 'keep')

    def test_whitelist_package_excludes_unlisted_private_file(self):
        source = self.base / 'public'
        source.mkdir()
        (source / 'README.md').write_text('Synthetic release')
        (source / 'secret.txt').write_text('must-not-export')
        write(source / 'release-files.json', ['README.md'])
        result = package(self.root, source)
        with zipfile.ZipFile(result['package']) as archive:
            self.assertEqual(set(archive.namelist()), {'creatorlens/README.md', 'creatorlens/RELEASE-MANIFEST.json'})

    def test_private_paths_and_symlinks_cannot_be_whitelisted(self):
        source = self.base / 'public'
        source.mkdir()
        write(source / 'release-files.json', ['../secret.txt'])
        with self.assertRaises(StopCapture):
            package(self.root, source)
        (self.base / 'secret.txt').write_text('private')
        (source / 'README.md').symlink_to(self.base / 'secret.txt')
        write(source / 'release-files.json', ['README.md'])
        with self.assertRaises(StopCapture):
            package(self.root, source)


if __name__ == '__main__':
    unittest.main()
