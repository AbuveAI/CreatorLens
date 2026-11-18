# 纯接口采集与续跑

当前版本 `0.1.0a2` 已通过自制样本的离线回归和本机上游接口/纯解析检查，尚未线上验收。
采集运行面向 macOS/Linux；锁使用 fcntl，Windows 未适配。

## 1. 准备外部依赖

CreatorLens 公共包只使用 Python 标准库。实际采集在 Spider_XHS 的独立 Python 环境执行，
需要按上游说明安装 Python/Node 依赖，并具备 Pillow、cryptography、ffprobe。
不需要安装浏览器或其他下载器。

推荐运行安装器，由它下载固定提交、准备独立环境并填写工作区配置：

```sh
python3 -m creatorlens init-workspace --workspace ../creatorlens-workspace
python3 -m creatorlens setup --workspace ../creatorlens-workspace
python3 -m creatorlens doctor --workspace ../creatorlens-workspace
```

上游位于工作区 `dependencies/Spider_XHS`，不复制维护者旧环境，也不自动升级。
已有独立安装的用户可手工填写 `config/settings.json` 的 `capture.spider_source` 与 `capture.spider_python` 绝对路径，再执行 `dependency-lock`；解释器保持虚拟环境路径，不解析到系统Python。platform/provider固定为xiaohongshu/spider_xhs。

dependency-lock 记录 apis/、xhs_utils/ 的 Python/JavaScript/JSON 及依赖声明摘要，不读取 .env 或会话。
它不执行上游代码，指纹仅证明文件版本一致，不能证明登录有效、接口可用或环境依赖完整。
修改上游后须检查变更并使用 dependency-lock --replace；同一批次不接受中途更换源码指纹。
指纹不覆盖 node_modules、虚拟环境包和可执行文件，环境版本仍需在实际验收中记录。

## 2. 新建批次

在当前用户已指定目标和范围后，用真实主页链接替换占位内容：

```sh
python3 -m creatorlens init --workspace ../creatorlens-workspace \
  --url '<小红书主页 HTTPS 链接>' --name '<账号名称>' --batch '<唯一批次ID>'
```

init 只创建本地任务。默认目标 10 条有效作品；`--count` 可指定数量。
任务路径：data/xiaohongshu/账号ID/batches/批次ID/task.json。名称不用于账号身份识别。
重复批次拒绝覆盖。任务创建后不要在已开始采集时修改范围或限制，否则续跑会拒绝。

## 3. 执行采集

下面的 capture 会访问平台，执行前核对本次授权；配置和历史任务不替用户授权。

```sh
python3 -m creatorlens capture --workspace ../creatorlens-workspace --task '<init返回的task.json路径>'
```

二维码通过 qr_ready 事件返回本地路径，由 Agent 展示给用户扫码。
复用工作区 private/xiaohongshu/session.json 前，先由上游接口校验登录。
会话目录权限 0700、文件 0600；同一工作区只允许一个采集会话进程。
锁忙时直接提示，其他窗口不会另开二维码。确认登录失效才移除失效会话；下一次显式执行才重新扫码。
网络、验证码、限频不会被当作登录失效，程序不自动重试或更换渠道。
首页 GET https://www.xiaohongshu.com/ 返回指向固定 /explore 的302是登录导航的已验证响应；程序将原响应交回上游处理Cookie，不自动跟随Location。其他重定向仍停止，验证提示优先处理。

## 4. 取样与文件

为避免置顶和跨页顺序导致漏采，当前实现先遍历列表至 has_more=false，再按实际发布时间排序和去重。
每次最多取 5 页；达到页数预算时保存游标并停止，需要显式续跑。作品很多的账号可能需要多轮。
这是采集时间窗口内的快照，不保证平台在翻页期间完全不变。

- 视频与图文共同占用作品名额。
- 视频仅在接口明确或本地实测严格超过 1200 秒时排除并补位；恰好 1200 秒保留。
- 类型、身份、发布时间未知或冲突时停止，不跳过补后面的作品。
- 图文所有图片按 01 起编号下载、实际解码、记录尺寸与 SHA256；首图同时作为封面引用。
- 视频保存本地视频和原始封面，实际读取视频流与时长。
- 每作品采样 1 页顶层评论与最多 2 页补充回复，保留内嵌回复并去重。
- 总评论数未知时保持 null；线程归属已知不代表直接父评论已知；评论图片只记录数量，未下载。
- 只有确认列表到底后才交付样本不足说明；若全部视频超长或账号为空，可以交付零作品范围记录。

采集结果位于批次内：

```text
task.json                 固定任务范围
runtime.json              使用的外部源码指纹
state.json                游标、候选、已完成作品与停止原因（可能含临时平台令牌，保持私有）
raw/                      接口原始证据和内部摘要；不上传或打印整份
works/作品ID/work.json    作品正文、指标、媒体清单、未审阅状态
works/作品ID/comments.json 评论样本与覆盖边界
works/作品ID/media/       原始媒体、逐文件摘要
capture.json              本地采集范围与文件验收清单
request-audit.json         当前执行请求计数/状态；不含请求头或查询参数
```

程序设置 semantic_review_status=not_reviewed、research_complete=false。
采集验收只证明本地材料与契约匹配，不证明 Agent 已阅读，也不证明分析正确。

## 5. 中断与恢复

```sh
python3 -m creatorlens status --workspace ../creatorlens-workspace --task '<task.json路径>'
python3 -m creatorlens capture --workspace ../creatorlens-workspace --task '<task.json路径>' --resume
python3 -m creatorlens validate --workspace ../creatorlens-workspace --task '<task.json路径>'
```

只有用户明确继续或已授权范围内可恢复时才执行 resume。验证码、限频等仍须先解决，不能用反复续跑代替处理。
成功保存的原始响应与媒体会复用；单张图片失败后，前面已验收的图片不重复下载。
未完整下载的 .part 文件可在显式续跑时重新下载；已记录文件摘要不符时停止，不静默覆盖。
完成的批次使用 resume 只做本地验收，不重新登录或采集。

普通请求及媒体间隔至少 12 秒，二维码轮询至少 10 秒，每轮最多 120 个受控请求、12 次二维码轮询。
间隔不保证平台不会验证或限频。平台返回拒绝、验证、网络故障时停止后续请求。

## 6. 从采集进入研究

采集后按 [研究协议](research.md) 准备转写、逐图阅读和逐段校订，再生成本地报告。飞书为可选的Agent交接流程。
不自动接手旧schema_version=2的任务，禁止移动旧任务到新目录后直接续跑。仍不提供浏览器命令或其他采集底座。
