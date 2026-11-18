"""External workspace configuration. No platform, credential or cloud calls."""
from copy import deepcopy
import json
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
DIRECTORIES = ("config", "private", "profiles", "data", "reports", "tmp")
DEFAULT_SETTINGS = {
    "schema_version": 1,
    "capture": {
        "platform": "xiaohongshu", "provider": "spider_xhs",
        "spider_source": "", "spider_python": "",
    },
    "asr": {"enabled": False, "provider": "doubao", "key_env": "DOUBAO_ASR_API_KEY", "key_file": ""},
    "feishu": {"enabled": False, "target_file": ""},
}


class WorkspaceError(ValueError):
    """User-safe error text; never contains configuration values or secrets."""


def require(condition, message):
    if not condition:
        raise WorkspaceError(message)


def workspace_path(value=None, *, environ=None, source_root=None):
    env = os.environ if environ is None else environ
    raw = value if value is not None else env.get("CREATORLENS_WORKSPACE")
    require(isinstance(raw, (str, Path)) and bool(str(raw).strip()),
            "请通过 --workspace 或 CREATORLENS_WORKSPACE 指定外置工作区")
    path = Path(raw).expanduser().resolve()
    source = (SOURCE_ROOT if source_root is None else Path(source_root)).resolve()
    require(not path.is_relative_to(source) and not source.is_relative_to(path),
            "工作区必须与源码目录分离，不能放在源码内或作为源码的上级目录")
    require(not path.exists() or path.is_dir(), "工作区路径已被文件占用")
    return path


def validate_settings(value):
    require(isinstance(value, dict), "配置必须是 JSON 对象")
    require(type(value.get("schema_version")) is int and value["schema_version"] == 1,
            "不支持的工作区配置版本")
    require(set(value) == set(DEFAULT_SETTINGS), "配置字段不符合当前模板")
    for name in ("capture", "asr", "feishu"):
        require(isinstance(value[name], dict) and set(value[name]) == set(DEFAULT_SETTINGS[name]),
                "配置字段不符合当前模板")
    capture = value["capture"]
    require(capture["platform"] == "xiaohongshu" and capture["provider"] == "spider_xhs",
            "首版采集只允许 xiaohongshu / spider_xhs 接口")
    require(value["asr"]["provider"] == "doubao", "当前仅规划豆包转写适配")
    require(isinstance(value["asr"]["key_env"], str) and value["asr"]["key_env"].strip(),
            "ASR 凭据环境变量名称无效")
    for name in ("asr", "feishu"):
        require(type(value[name]["enabled"]) is bool, "可选服务 enabled 必须为布尔值")
    for section, name in (("capture", "spider_source"), ("capture", "spider_python"),
                          ("asr", "key_file"), ("feishu", "target_file")):
        raw = value[section][name]
        require(isinstance(raw, str), "配置中的文件位置必须为字符串")
        require(not raw or Path(raw).expanduser().is_absolute(), "配置中的文件位置须为空或绝对路径")
    return value


def check_layout(root):
    for relative in (*DIRECTORIES, "config/settings.json"):
        path = root / relative
        require(not path.is_symlink(), "工作区标准目录和配置不能是符号链接")
    for name in DIRECTORIES:
        require(not (root / name).exists() or (root / name).is_dir(), "工作区目录被普通文件占用")


def load_settings(root):
    check_layout(root)
    try:
        with (root / "config/settings.json").open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise WorkspaceError("工作区配置缺失或不可读；先运行 init-workspace 并核对 JSON 格式") from exc
    return validate_settings(value)


def initialize(value, *, environ=None, source_root=None):
    root = workspace_path(value, environ=environ, source_root=source_root)
    check_layout(root)
    target = root / "config/settings.json"
    if target.exists():
        load_settings(root)  # Validate before any mutation; never overwrite settings.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in DIRECTORIES:
        (root / name).mkdir(exist_ok=True, mode=0o700)
    for path in (root, root / "config", root / "private"):
        path.chmod(0o700)
    created = False
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        load_settings(root)
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(deepcopy(DEFAULT_SETTINGS), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        created = True
    target.chmod(0o600)
    return {"workspace": str(root), "created_settings": created,
            "capture_provider": "spider_xhs", "capture_implemented": True,
            "note": "仅建立本地配置与目录；未采集、未登录、未调用云端服务"}
