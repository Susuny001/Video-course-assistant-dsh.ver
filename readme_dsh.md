# readme_dsh：实现说明与使用手册

这个文件由 deepseek harness 版本维护，记录实际实现。仓库根目录的 README.md 是需求方原文，未改动。

## 1. 这是什么

一个本地运行的网课播放器。导入视频或录音后，它会：

1. 提取音轨，按时间切块做语音识别；也可以直接导入课程自带的 SRT，跳过语音识别；
2. 语音识别和画面 OCR 同时跑，转写和画面文字再交给文本模型结合上下文纠错；
3. 生成原文 + 译文字幕（可关），支持“专业名词保留英文”；只做外语译中，中文课自动跳过；
4. 按教学内容由文本模型判断主题边界并分段，为每段生成标题、摘要、要点；
5. 三条处理进度线（语音 / OCR / 修正）按时间区间着色，处理到哪就能看到哪；
6. 播放器把当前时间上报，识别、OCR、修正优先处理正在看的位置，之后再回头补前面；
7. 选中任意时间区间，把该段字幕和画面帧交给答疑模型提问，提示词可按课单独设置；
8. 按关键词从全课中提取重点讲解及时间点；
9. 导出双语 SRT、Markdown 笔记、完整 JSON。

没有配置 API Key 时不会生成任何字幕或摘要：分析任务会直接报错并提示去设置，界面不会出现假数据。

`samples/` 里的录音和文字稿只用于自测：真实跑完一次分析后，拿导出的 SRT / 笔记和文字稿对照，检查转写是否可用。它们不会自动进入课程库，也不会被当成字幕或摘要的来源。

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

产物：`dist\YunKeAssistant.exe`。单文件，运行时会把前端资源解包到临时目录。exe 在项目的 `dist\` 下时，会回到上一级使用仓库的 `data\`，和源码方式共用课程库；把 exe 单独拷到别处时，则使用 exe 同级的 `data\`。`samples\` 不打进 exe（录音 100MB）。

打包版没有控制台，启动日志写到 `data\app.log`。首次运行单文件 exe 可能被杀毒软件扫描几十秒，之后启动通常在几秒内。

## 3. 模型配置

打开「设置」。所有服务商都按 OpenAI 兼容协议调用，Key 只写入本机 `data/config.json`，不会上传到别处。

内置预设：

| 预设 | 用途 | 备注 |
| --- | --- | --- |
| 智谱 GLM | 对话 + 视觉 | `glm-4-flash`、`glm-4.7-flash`（文本）和 `glm-4.6v-flash`（视觉）长期免费 |
| 讯飞 MaaS | OCR + 免费文本 | `xoppaddleocrv16` / `xopdeepseekocr` / `xophunyuanocr` 做画面 OCR；`spark-x2.5-4b` / `spark-x2.6-4b` 为 0 元文本模型 |
| 硅基流动 SiliconFlow | 对话 + 视觉 + 语音识别 | 按量计费，需要自己填 Key |
| 阿里云百炼 DashScope | 对话 + 视觉 | 按量计费；`qwen3.8-flash` 输入 0.8 / 输出 2.7 元每百万 tokens，不是免费 |
| OpenRouter | 对话 + 视觉 | 免费模型每天有次数上限，超额返回 429；只保留手动可选 |
| 本地 Ollama | 对话 + 视觉 | 接口保留，默认不参与自动调用 |

默认自动链路：`glm-4-flash` → `glm-4.7-flash` → `spark-x2.5-4b` → `spark-x2.6-4b`。余额不足、无权限的模型会在本次任务内被熔断跳过；429 属于临时限流，仍会继续轮换。

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
2. `ffmpeg` 抽取 16k 单声道音轨，先做响度归一化（网课录音经常只有 -50dB，不归一化识别会胡说）；有 SRT 时跳过音轨和语音识别；
3. 语音识别按默认 300 秒切片（`asr_chunk_seconds`），每片拿到句级时间戳；接口不返回时间戳时按字数比例分配；
4. 语音识别和 OCR 同时进行：OCR 按 20 句一批抽 1 帧，三个 OCR 模型按顺序试、单个 8 秒超时；
5. 修正从第一句开始，前提是这一批已有语音和 OCR，且全课语音、OCR 各自覆盖 ≥25%。每批用“前面已经修正过的句子 + 后面原文 + OCR 文字”做上下文，模型只返回 `i / text / translation`，时间轴不会被改写；
6. 翻译单独排队，与后续批次的纠错并行；最后再补翻一次空译文；
7. 播放器上报当前时间（`/focus`），语音、OCR、修正在挑选任务时优先处理正在看的时间段，做完再回头补前面；
8. 字幕按 40 句窗口交给文本模型判断主题边界，按内容切段，再并发请求每段标题、摘要、要点；模型给不出边界时才退回长停顿切分；
9. 用各段摘要生成课程总览和术语表；
10. 写入 `data/videos/<id>/subs.json`、`subs.srt`、`notes.md`，同时更新 SQLite。

并发控制：每个模型一把信号量（如 `glm-4-flash` 12、`glm-4.7-flash` 6、`xfyun` 每个 OCR 模型 6、DeepSeek 16），worker 数量可以更多，超出的在信号量上排队。语音、OCR、纠错、翻译各自有 worker 池，某一批完成就立即进入下一阶段，不用等整条线结束。单片识别失败不会中断任务；所有片都失败才报错。任务进度、日志和三条覆盖区间通过 `/api/jobs/{id}` 轮询展示，可取消。

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
| POST | `/api/import/upload` | 浏览器拖拽上传（multipart，字段 `files`），自动配对同名 SRT |
| POST | `/api/videos/{id}/subtitle` | 手动给课程挂 SRT |
| POST | `/api/videos/{id}/prompt` | 设置本课单独的助教提示词 |
| POST | `/api/videos/{id}/analyze` | 启动分析任务 |
| GET | `/api/jobs/{id}` | 任务进度、日志和三条覆盖区间 |
| POST | `/api/videos/{id}/focus` | 上报当前播放时间，实时调整处理优先级 |
| GET | `/api/videos/{id}/cues` | 增量拉取字幕（边分析边看） |
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
  search.py      本地关键词检索
  export.py      SRT / Markdown
  dialog.py      原生文件选择框
web/             前端（原生 HTML/CSS/JS，无构建步骤）
scripts/         setup.ps1、图标生成
samples/         自测用录音与文字稿（不会自动导入课程库）
```

## 8. 已知限制

- 依赖 ffmpeg；`setup.bat` 会尝试下载到 `bin\`，失败时需手动安装并加入 PATH。
- 语音识别按 5 分钟硬切，跨切点的句子可能被截断；没有做说话人分离和词级时间戳。
- 视觉纠错效果取决于模型是否真的支持图片；纯文本模型会被跳过或返回空。
- 讯飞 OCR 偶尔会超时或返回 503，失败的批次会自动退回纯文本纠错，不影响整体完成。
- 长课程的分析耗时与费用取决于所选服务商；免费模型通常有速率限制，并发不是越大越好。
- 分段和总览必须等全部字幕完成，这两步无法边看边出。
- 视频格式支持取决于 WebView2/Chromium；放不了会自动转码一次。
- 字幕、笔记存在本机，不提供云同步。

## 9. 常见问题

- **401 / 403**：Key 错误或无该模型权限；在设置里点“测试连接”。
- **429**：临时限流，会自动换下一个模型；连续失败也不会中断任务。余额不足（402）、无权限（401/403）、模型不存在（404）会在本次任务内直接跳过。
- **OpenRouter 免费模型 429**：免费额度按天计算，超额会提示 `free-models-per-day`，需要等次日或充值解锁。
- **一直显示“提取音轨”**：确认 ffmpeg 可用，`python run.py --server` 看控制台输出。
- **窗口打不开**：缺少 WebView2 运行时会退回浏览器；也可直接访问启动日志里的地址。
- **字幕和声音对不上**：多半是语音接口没返回时间戳，按字数估算的；换支持 `verbose_json` 的接口可改善。
- **端口被占用**：自动选择空闲端口，无需处理。
