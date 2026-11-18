# CreatorLens

**把创作者的公开内容，整理成有证据、可追溯、能指导下一次创作的研究。**

CreatorLens 面向已经在使用 Agent 的内容创作者。你给 Agent 一个账号链接，Agent 协助采集、阅读、校订和分析，最终在自己的电脑上拿到报告。

Spider_XHS 提供小红书底层接口能力；CreatorLens 提供混合取样、断点与证据管理、Agent 审阅流程、创作研究结构和本地交付。采集只是起点，研究结论需要 Agent 实际阅读素材。

当前版本：**0.1.0a2 · 早期测试版（Alpha）**。固定版本依赖安装与离线流程已验证；真实账号采集和可选云服务仍需在用户自己的环境验收。不要将其描述为无人值守或生产稳定版。

## 给你的 Agent

将本项目链接或源码目录发给 Agent，并说明：

> 请先阅读 README.md、AGENTS.md 和 skills/creatorlens/SKILL.md。源码与私人工作区分开放，先运行离线 demo，再准备 Spider_XHS 环境。不要读取其他项目的登录信息。等我提供目标账号后再采集，是否使用付费转写和飞书按本次授权处理。

Agent 可直接读取项目内的 Skill，无需先把它全局安装。只收到链接、没有文件访问或执行能力的 Agent 不能完成安装与采集。

## 三种能力边界

| 能力 | 现在可以做什么 |
|---|---|
| 接口采集 | 仅 Spider_XHS；最近有效作品，视频与图文共同取样，图文全部原图，本地会话复用，断点续跑 |
| 本地研究 | 导入逐字稿／可选豆包转写，逐段校订、逐图分析、评论证据、六章研究、Markdown 与 HTML 报告 |
| 可选飞书 | 生成文字、封面及图文原图交接清单；由 Agent 使用用户自己的飞书工具映射与同步，回读验收 |

没有浏览器采集、视觉登录抓取或失败后的浏览器回退。图文阅读属于素材分析，不是浏览器采集。抖音、视频号、B站属于后续规划。

## 本地快速开始

源码和私人工作区应是两个独立目录，例如：

```text
projects/
├── creatorlens/              # 公共源码，可提交 GitHub
└── creatorlens-workspace/    # 配置、登录会话、依赖、素材和报告，不提交
    ├── config/
    ├── dependencies/Spider_XHS/
    ├── private/
    ├── data/
    └── reports/
```

先下载源码：

```sh
git clone https://github.com/AbuveAI/CreatorLens.git creatorlens
cd creatorlens
```

以下命令在源码根目录执行。核心与离线 demo 只需要 Python 3.10+；采集还需要 Git、Node.js/npm、FFmpeg/ffprobe。当前在 macOS 验证；Linux 可按同样入口测试，Windows 原生文件锁尚未支持，可使用独立 Linux 环境。

```sh
export CREATORLENS_WORKSPACE="$(pwd)/../creatorlens-workspace"
python3 -m creatorlens init-workspace
python3 -m creatorlens demo
```

`demo` 只生成自制色块图和示例研究，不访问平台、不收费。命令返回任务及报告路径。重复演示用 `--batch offline-demo-2`，不覆盖旧批次。

然后安装外部依赖：

```sh
python3 -m creatorlens setup
python3 -m creatorlens doctor
```

`setup` 下载固定上游提交、创建独立 Python 环境、安装 Python/npm 依赖，再记录源码指纹。重复执行会复用完成的步骤；不会自动更新上游。操作不登录小红书。依赖首次安装需要联网；依赖安装脚本会在用户机器执行，因此只从本文记录的上游获取。

需要人工准备基础工具时，让 Agent 先检查系统和现有安装；项目不会自动安装系统级软件。`doctor` 返回非零时查看具体缺项，不要直接尝试采集。

## 真实研究

用户提供并授权目标后：

```sh
python3 -m creatorlens init --url "https://www.xiaohongshu.com/user/profile/用户ID" --name "账号名称" --batch "study-001"
python3 -m creatorlens capture --task "上一步返回的task.json路径"
```

首次接口登录需用户扫描本地二维码；不会读取浏览器 Cookie。等待扫码、限频或网络中断时保留现场。后续用同一任务的 `capture --resume` 继续；不要反复新建任务。

采集完成后由 Agent 继续，见 [研究流程](docs/research.md)。这部分不是运行一条命令就能自动完成的语义工作。

```sh
python3 -m creatorlens validate --task "任务路径"
python3 -m creatorlens research-prepare --task "任务路径"
# Agent逐图阅读、逐段审阅并填写研究文件；视频缺转写时先导入或使用可选ASR。
python3 -m creatorlens research-validate --task "任务路径"
python3 -m creatorlens report --task "任务路径"
python3 -m creatorlens report-validate --task "任务路径"
```

每个批次的 `report/report.md` 和 `report/report.html` 是本地成果。网页不加载外部脚本或远端图片。程序核对证据和审阅声明，不能替代对分析质量的判断。

## 配置与可选服务

- [豆包 ASR](docs/asr.md)：默认关闭；使用自己的凭据和本次计费授权。也可导入现有逐字稿，不购买转写服务。
- [飞书增强](docs/feishu.md)：默认不写云端；视频只留本地，封面和图文原图可按授权同步。
- [首次独立目录验收](docs/fresh-validation.md)：给新 Codex 项目的接手清单。
- [采集规则及恢复](docs/capture.md)、[架构](docs/architecture.md)、[维护与升级](docs/maintenance.md)。

所有凭据、会话和实际任务只在外置工作区。不要把报告、评论、原始接口响应或媒体混入源码提交；本地可读不等于获准公开。

## 开源与发布

自有代码使用 [MIT](LICENSE)。第三方来源和边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，本仓库不包含 Spider_XHS 源码、签名实现、ASR 服务或飞书 CLI。

```sh
python3 -m unittest discover -s tests -v
python3 -m creatorlens package
```

`package` 按 `release-files.json` 白名单导出源码 ZIP 和校验清单，不打包整个工程，不发布 GitHub。当前以Alpha源码版本开放测试，稳定版发布前仍需通过真实场景验收；详见 [发布检查](docs/release.md)。
