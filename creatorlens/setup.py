"""Pinned external dependency installer. No platform login, no automatic upgrades."""
import os
import re
import shutil
import subprocess
import sys
from . import dependency
from .storage import StopCapture, ensure, lock, read, safe_path, write
from .tasks import now
from .workspace import initialize, load_settings

UPSTREAM = 'https://github.com/cv-cat/Spider_XHS.git'
REVISION = '23dab803a0465e35e01cf87137353a900b47e93d'


def run(command, cwd):
    try:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=900,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'})
    except (OSError, subprocess.TimeoutExpired):
        raise StopCapture('dependency_install_command_failed_or_timed_out') from None
    # Package managers can echo authenticated index/proxy URLs. Never print their raw output.
    ensure(proc.returncode == 0, 'dependency_install_command_failed_check_network_and_prerequisites')
    return proc.stdout.strip()


def install(root, *, runner=run, emit=lambda value: None):
    initialize(root)
    for binary in ('git', 'node', 'npm', 'ffmpeg', 'ffprobe'):
        ensure(shutil.which(binary), 'missing_prerequisite_' + binary)
    ensure(os.name == 'posix', 'setup_requires_macos_or_linux')
    with lock(safe_path(root, 'private/setup.lock')):
        settings = load_settings(root)
        receipt = safe_path(root, 'config/setup.json')
        state = read(receipt) if receipt.exists() else {}
        source = safe_path(root, 'dependencies/Spider_XHS')
        python = source / '.venv/bin/python'
        if state.get('complete'):
            dependency.validate(root)
            return {'installed': True, 'reused': True, 'revision': REVISION, 'platform_contacted': False}
        ensure(not settings['capture']['spider_source'] or settings['capture']['spider_source'] == str(source),
               'existing_external_configuration_not_overwritten')
        if not state:
            ensure(not source.exists(), 'existing_dependency_directory_not_overwritten')
            state = {'schema_version': 1, 'revision': REVISION, 'completed_steps': [], 'started_at': now()}
            write(receipt, state)
        ensure(state.get('revision') == REVISION, 'installer_revision_changed_use_new_workspace')
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        steps = [
            ('git_init', ['git', 'init', '.']),
            ('git_fetch', ['git', 'fetch', '--depth', '1', UPSTREAM, REVISION]),
            ('git_checkout', ['git', 'checkout', '--detach', REVISION]),
            ('venv', [sys.executable, '-m', 'venv', str(source / '.venv')]),
            ('python_packages', [str(python), '-m', 'pip', 'install', '-r', 'requirements.txt', 'Pillow', 'cryptography']),
            ('node_packages', ['npm', 'install', '--ignore-scripts', '--no-audit', '--no-fund']),
            ('imports', [str(python), '-c', 'import requests, curl_cffi, PIL, cryptography; from apis.xhs_pc_apis import XHS_Apis']),
        ]
        for name, command in steps:
            if name not in state['completed_steps']:
                emit({'setup_step': name})
                runner(command, source)
                state['completed_steps'].append(name)
                write(receipt, state)
        ensure(runner(['git', 'rev-parse', 'HEAD'], source) == REVISION, 'installed_commit_mismatch')
        settings['capture'].update(spider_source=str(source), spider_python=str(python))
        write(safe_path(root, 'config/settings.json'), settings)
        # Pin only after every installation step, including generated npm lockfile, succeeds.
        dependency.pin(root, replace=True)
        state.update(complete=True, completed_at=now())
        write(receipt, state)
        return {'installed': True, 'reused': False, 'revision': REVISION,
                'source': str(source), 'platform_contacted': False}
