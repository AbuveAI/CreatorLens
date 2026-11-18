"""Local research workflow and explicitly requested network adapters."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from . import __version__, dependency
from .storage import StopCapture, lock, safe_path
from .tasks import Task, create
from .collection import verify
from .workspace import SOURCE_ROOT, WorkspaceError, initialize, load_settings, workspace_path


def doctor(root):
    settings = load_settings(root)
    capture = settings['capture']
    source = Path(capture['spider_source']).expanduser().resolve() if capture['spider_source'] else None
    python = Path(capture['spider_python']).expanduser() if capture['spider_python'] else None
    checks = {'python_3_10_or_newer': sys.version_info >= (3, 10),
              **{name + '_found': shutil.which(name) is not None for name in ('git', 'node', 'npm', 'ffmpeg', 'ffprobe')},
              'spider_source_external': bool(source and not source.is_relative_to(SOURCE_ROOT)),
              'spider_api_file_found': bool(source and (source / 'apis/xhs_pc_apis.py').is_file()),
              'spider_python_executable': bool(python and python.is_file() and os.access(python, os.X_OK))}
    try:
        dependency.validate(root)
        checks['dependency_lock_matches'] = True
    except (StopCapture, OSError):
        checks['dependency_lock_matches'] = False
    return {'version': __version__, 'workspace': str(root), 'capture_provider': 'spider_xhs',
            'stage': 'alpha_local_workflow', 'checks': checks,
            'local_dependencies_present': all(checks.values()), 'capture_implemented': True,
            'online_validated': False, 'release_ready': False,
            'optional': {'asr_enabled': settings['asr']['enabled'], 'feishu_enabled': settings['feishu']['enabled']},
            'not_checked': ['登录有效性', 'ASR与飞书权限', '线上采集与完整真实研究流程', '当前环境运行时兼容性'],
            'note': '只读本机检查；离线流程已实现，真实平台及可选云服务须按本次任务验收'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='CreatorLens · Agent协作内容研究（纯接口）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    descriptions = {
        'init-workspace': '建立外置私人工作区', 'doctor': '只读本机检查，不访问网络',
        'setup': '下载固定版本Spider_XHS及独立依赖，不登录平台', 'package': '白名单导出公共源码',
        'demo': '自制离线样本和报告，无平台或云端调用', 'dependency-lock': '锁定已配置的外部源码指纹',
        'init': '创建本地研究批次', 'status': '查看批次状态', 'validate': '验证采集资料',
        'capture': '通过Spider_XHS执行网络采集，可能需要扫码', 'research-prepare': '生成Agent审阅模板和证据目录',
        'research-validate': '验证审阅声明和证据引用', 'report': '生成本地Markdown和HTML报告',
        'report-validate': '验证报告与当前研究一致', 'transcript-import': '导入带时间戳的外部逐字稿',
        'transcribe': '提交可选豆包ASR，须有本次计费授权', 'asr-query': '只查已有ASR任务，不重交',
        'feishu-plan': '生成飞书Agent交接计划，不写云端',
    }
    task_commands = {'status', 'validate', 'capture', 'research-prepare', 'research-validate', 'report',
                     'report-validate', 'transcript-import', 'transcribe', 'asr-query', 'feishu-plan'}
    for name, description in descriptions.items():
        cmd = sub.add_parser(name, help=description)
        cmd.add_argument('--workspace', help='外置工作区；也可设CREATORLENS_WORKSPACE')
        if name in task_commands:
            cmd.add_argument('--task', required=True, help='init返回的task.json路径')
        if name == 'dependency-lock':
            cmd.add_argument('--replace', action='store_true')
        if name == 'demo':
            cmd.add_argument('--batch', default='offline-demo')
        if name == 'init':
            cmd.add_argument('--url', required=True)
            cmd.add_argument('--name', required=True)
            cmd.add_argument('--batch', required=True)
            cmd.add_argument('--count', type=int, default=10)
        if name == 'capture':
            cmd.add_argument('--resume', action='store_true')
        if name in ('transcript-import', 'transcribe', 'asr-query'):
            cmd.add_argument('--work-id', required=True)
        if name == 'transcript-import':
            cmd.add_argument('--file', required=True)
        if name == 'transcribe':
            cmd.add_argument('--allow-paid-asr', action='store_true', help='仅在用户已授权本次上传及计费时使用')
            choice = cmd.add_mutually_exclusive_group()
            choice.add_argument('--local-audio', action='store_true', help='本地音频base64兼容模式，新环境须验证服务端支持')
            choice.add_argument('--audio-url', help='用户提供的HTTPS音频地址，不自动上传公共存储')
    args = parser.parse_args(argv)
    try:
        root = workspace_path(args.workspace)
        if args.command == 'init-workspace':
            result = initialize(root)
        elif args.command == 'doctor':
            result = doctor(root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['local_dependencies_present'] else 2
        elif args.command == 'setup':
            from .setup import install
            result = install(root, emit=lambda row: print(json.dumps(row), flush=True))
        elif args.command == 'package':
            from .release import package
            load_settings(root)
            with lock(safe_path(root, "private/package.lock")):
                result = package(root)
        elif args.command == 'demo':
            from .demo import run
            initialize(root)
            result = run(root, args.batch)
        elif args.command == 'dependency-lock':
            result = dependency.pin(root, replace=args.replace)
        elif args.command == 'init':
            result = create(root, url=args.url, name=args.name, batch=args.batch, count=args.count).status()
        else:
            from . import research, asr, feishu
            task = Task(root, args.task)
            if args.command == 'capture' and not (task.p('capture.json').exists() and args.resume):
                _, python, _ = dependency.validate(root)
                command = [str(python), '-m', 'creatorlens.worker', '--workspace', str(root), '--task', str(task.path)]
                if args.resume:
                    command.append('--resume')
                env = {**os.environ, 'PYTHONPATH': str(SOURCE_ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
                return subprocess.run(command, env=env, cwd=SOURCE_ROOT).returncode
            with task.lock():
                operations = {'validate': verify, 'capture': verify, 'research-prepare': research.prepare,
                              'research-validate': research.validate, 'report': research.report,
                              'report-validate': research.report_status, 'feishu-plan': feishu.plan}
                if args.command in operations:
                    result = operations[args.command](task)
                elif args.command == 'status':
                    result = task.status()
                    if task.p('report/receipt.json').exists():
                        try:
                            result.update(research.report_status(task))
                        except (StopCapture, OSError):
                            result.update(research_complete=False, report_status='stale_or_invalid')
                elif args.command == 'transcript-import':
                    result = research.import_transcript(task, args.work_id, args.file)
                elif args.command == 'asr-query':
                    result = asr.query(task, args.work_id)
                else:
                    result = asr.submit(task, args.work_id, allow_paid=args.allow_paid_asr,
                                        local_audio=args.local_audio, audio_url=args.audio_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (WorkspaceError, StopCapture, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        message = str(exc) if isinstance(exc, (WorkspaceError, StopCapture)) else '本地文件或数据格式异常，请核对配置及文档；未打印可能含隐私的原始异常'
        print(json.dumps({'status': 'failed', 'message': message}, ensure_ascii=False), file=sys.stderr)
        return 2
