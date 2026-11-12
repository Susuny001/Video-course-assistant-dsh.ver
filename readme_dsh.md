# readme_dsh：实现说明与使用手册

这个文件由 deepseek harness 版本维护，记录实际实现。仓库根目录的 README.md 是需求方原文，未改动。

## 1. 这是什么

一个本地运行的网课播放器。导入视频或录音后，它会：

1. 提取音轨，按时间切块做语音识别；
2. 把转写、课堂画面截图、参考文字稿一起交给视觉模型，纠正同音字、术语、人名和数字；
3. 生成原文 + 译文字幕（可关），支持“专业名词保留英文”；
4. 按停顿自动切分课程段落，为每段生成标题、摘要、要点；
5. 在播放器里显示带分段刻度的进度条，可按段跳转、续播；
6. 选中任意时间区间，把该段字幕和画面帧交给答疑模型提问；
7. 按关键词从全课中提取重点讲解及时间点；
8. 导出双语 SRT、Markdown 笔记、完整 JSON。

没有配置 API Key 时进入演示模式：转写内容用参考文稿按时间铺开，问答走本地字幕检索，用于先看界面和流程，不是真实识别结果。

## 2. 运行

### 源码方式

```bat
setup.bat        :: 后台安装依赖、下载 ffmpeg、创建桌面快捷方式，完成后自动启动
start.bat        :: 启动（优先用 dist\YunKeAssistant.exe，其次 .venv，最后系统 Python）
```

也可以手动：

```bat
python -m pip install -r requirements.txt
python run.py                 :: 原生窗口
python run.py --server        :: 只开 HTTP 服务
python run.py --server --port 8877
```

启动后只监听 `127.0.0.1`，默认 8765 端口；端口被占用会自动换一个。窗口基于 WebView2（Windows 10/11 自带），没有时会退回系统浏览器。

### 打包 exe

```bat
build_exe.bat
```

产物：`dist\YunKeAssistant.exe`。单文件，运行时会把前端资源解包到临时目录；数据仍然写在 exe 同级的 `data\` 目录。`samples\` 不打进 exe，需要示例课程时把 `samples` 目录放在 exe 旁边（否则课程库里的“导入示例课程”会自动隐藏）。

打包版没有控制台，启动日志写到 `data\app.log`。首次运行单文件 exe 可能被杀毒软件扫描几十秒，之后启动通常在几秒内。

## 3. 模型配置

打开「设置」。所有服务商都按 OpenAI 兼容协议调用，Key 只写入本机 `data/config.json`，不会上传到别处。

内置预设：

| 预设 | 用途 | 备注 |
| --- | --- | --- |
| 硅基流动 SiliconFlow | 对话 + 视觉 + 语音识别 | `FunAudioLLM/SenseVoiceSmall` 可做转写，Qwen 系列有免费额度 |
| 阿里云百炼 DashScope | 对话 + 视觉 | qwen-vl 系列支持看图 |
| 智谱 GLM | 对话 + 视觉 | glm-4-flash / glm-4v-flash 免费 |
| OpenRouter | 对话 + 视觉 | 需要能访问；`:free` 模型免费 |
| 本地 Ollama | 对话 + 视觉 | 完全离线，需先 `ollama pull qwen2.5vl:7b` |
| 内置演示模式 | 全部环节 | 不联网假数据 |

四个环节可分别指定服务商和模型：

- 语音识别：必须是 OpenAI 兼容的 `/audio/transcriptions` 接口；
- 画面纠错 / 翻译：需要视觉模型，能看到抽取的课堂截图；
- 分段摘要 / 关键词：普通文本模型即可；
- 答疑默认模型：可在问答面板里临时切换，切换后会写回配置。

“自定义服务商”支持任意 OpenAI 兼容地址和任意模型名，模型列表每行一个。测试按钮会用第一个模型发一条最小请求。

思考开关：Qwen3 / GLM-4.5 / DeepSeek-V3.1 系列发送 `enable_thinking`，OpenAI o 系列发送 `reasoning_effort`，其它模型忽略该参数。

## 4. 分析流程

代码在 `app/pipeline.py`：

1. `ffprobe` 读取时长、分辨率、编码；非浏览器可播放的容器/编码会先转成 H.264 MP4；
2. `ffmpeg` 抽取 16k 单声道音轨，按默认 300 秒切片（设置项 `asr_chunk_seconds`）；
3. 每片调用语音识别，拿到句级时间戳；接口不返回时间戳时（例如 SenseVoice）按字数比例分配；
4. 每片抽取 4 张画面帧（纯音频跳过），从参考文字稿里按二字组重合度挑出最相关的段落，连同上下文和术语表一起发给视觉模型；模型只返回与输入 cue 一一对应的 `i / text / translation`，时间轴不会被模型改写；
5. 按目标时长（默认 8 分钟）在句间停顿处切段，再并发请求每段的标题、摘要、要点；
6. 用各段摘要生成课程总览和术语表；
7. 写入 `data/videos/<id>/subs.json`、`subs.srt`、`notes.md`，同时更新 SQLite。

单片识别失败不会中断整个任务，会记录日志并继续；所有片都失败才报错。任务进度、日志通过 `/api/jobs/{id}` 轮询展示，可取消。

## 5. 数据位置

```
data/
  app.db                 课程、分段、字幕、任务、对话
  config.json            API Key、模型分工、字幕偏好
  app.log                打包版（无控制台）的运行日志
  setup.log              一键安装日志
  videos/<id>/
    audio.mp3            提取出的音轨
    chunks/              切块音频
    frames/              分析用截图、按需抽帧
    cover.jpg            封面（纯音频会用 PIL 生成）
    subs.json            完整结构化字幕与分段
    subs.srt             双语 SRT
    notes.md             分段笔记
```

原始视频不复制进 `data`，只记录路径；移动或删除原文件会导致无法播放。

## 6. 主要接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/videos` | 课程列表 |
| POST | `/api/import` | 按路径导入 |
| POST | `/api/import/dialog` | 打开系统文件选择框 |
| POST | `/api/videos/{id}/analyze` | 启动分析任务 |
| GET | `/api/jobs/{id}` | 任务进度与日志 |
| GET | `/api/videos/{id}/media` | 带 Range 的媒体流 |
| GET | `/api/videos/{id}/frame?t=秒` | 指定时间抽帧 |
| POST | `/api/chat` | 流式问答（纯文本分块） |
| POST | `/api/videos/{id}/keywords` | 关键词重点提取 |
| GET | `/api/videos/{id}/export?fmt=srt\|md\|json` | 导出 |

## 7. 目录结构

```
app/
  main.py        FastAPI + pywebview 启动
  api.py         路由、媒体流、问答流
  pipeline.py    分析流水线
  asr.py         OpenAI 兼容语音识别
  llm.py         对话 / 视觉 / 流式 / 思考参数
  prompts.py     提示词
  media.py       ffmpeg 封装
  db.py          SQLite
  config.py      配置与预设
  demo.py        演示模式
  refdoc.py      参考文稿切片与检索
  export.py      SRT / Markdown
  dialog.py      原生文件选择框
web/             前端（原生 HTML/CSS/JS，无构建步骤）
scripts/         setup.ps1、图标生成
samples/         示例课程录音与文字稿
```

## 8. 已知限制

- 依赖 ffmpeg；`setup.bat` 会尝试下载到 `bin\`，失败时需手动安装并加入 PATH。
- 语音识别按 5 分钟硬切，跨切点的句子可能被截断；没有做说话人分离和词级时间戳。
- 视觉纠错效果取决于模型是否真的支持图片；纯文本模型会被跳过或返回空。
- 演示模式不产生真实转写和译文，仅用于流程演示。
- 长课程的分析耗时与费用取决于所选服务商；免费模型通常有速率限制。
- 视频格式支持取决于 WebView2/Chromium；放不了会自动转码一次。
- 字幕、笔记存在本机，不提供云同步。

## 9. 常见问题

- **401 / 403**：Key 错误或无该模型权限；在设置里点“测试连接”。
- **429**：触发限流，降低并发或换模型；流水线本身是分片串行识别。
- **一直显示“提取音轨”**：确认 ffmpeg 可用，`python run.py --server` 看控制台输出。
- **窗口打不开**：缺少 WebView2 运行时会退回浏览器；也可直接访问启动日志里的地址。
- **字幕和声音对不上**：多半是语音接口没返回时间戳，按字数估算的；换支持 `verbose_json` 的接口可改善。
- **端口被占用**：自动选择空闲端口，无需处理。
