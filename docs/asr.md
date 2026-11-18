# 可选豆包 ASR

不启用ASR也能运行图文研究和离线 demo；视频可以导入用户已有的带时间戳逐字稿。CreatorLens 只提供客户端适配，不包含豆包服务实现。

在外置工作区 `config/settings.json` 设置 `asr.enabled: true`。密钥优先从 `asr.key_env` 指定的环境变量读取（默认 `DOUBAO_ASR_API_KEY`），其次从 `asr.key_file` 的绝对路径读取同名 `KEY=value`。凭据文件由用户保管，不复制到源码、任务或共享包，不打印密钥。配置可读不等于本次计费已获授权。

已获本次音频上传和计费授权时，按作品逐个提交：

```sh
python3 -m creatorlens transcribe --task "任务路径" --work-id "作品ID" --allow-paid-asr --local-audio
python3 -m creatorlens asr-query --task "任务路径" --work-id "作品ID"
```

本地模式通过 FFmpeg 提取单声道16kHz MP3，使用旧工作流验证过的 base64 兼容请求。**当前官方标准版文档列出的输入是 HTTPS 音频 URL，base64 模式在新环境的服务端兼容性尚未验收**；接口拒绝时保留任务ID，不自动改模式重新计费。用户已有可访问的音频地址时，可用 `--audio-url "https://..."` 替代 `--local-audio`，不要把地址对应内容与当前视频混淆，也不要把视频上传飞书。

参考：[官方任务提交](https://docs.volcengine.com/docs/doubao-voice/task-submission-http-1?lang=zh)、[官方结果查询](https://docs.volcengine.com/docs/DoubaoVoice/result-query-http-1?lang=zh)。请求使用 `volc.seedasr.auc` 和用户自己的 API Key。项目不会自动部署公开音频存储。

提交前将 UUID、素材摘要、提交方式和授权时间写入 `processing/asr/<id>/job.json`。即使网络超时或响应不确定，再次运行也只查询原任务，不能删除检查点重交。每次查询只请求一次；pending 时按返回建议间隔继续查，避免持续占用 Agent 或重复提交。服务端仍无法确认任务时交由用户核对控制台，不假定“失败所以没收费”。

原始结果、原稿和时间戳保留在本地。成功仅表示自动识别完成；还须按研究流程校订才能生成正式报告。
