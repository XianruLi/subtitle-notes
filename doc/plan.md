# 开发计划 — 字幕(SRT/WebVTT)转Markdown守护脚本

## 项目概述

开发一个Windows开机自启动的Python守护脚本，监听 `.\视频字幕\` 目录，
当检测到新的 `.srt` / `.vtt` 字幕文件时，先自动识别格式，再调用AI API进行
上下文理解、文字纠错、信息处理与文档总结，
输出结构化Markdown学习笔记到 `daemons/output/`。

## 架构概览

```
视频字幕/  →  [watcher.py]  →  [subtitle_parser.py]  →  [srt_parser.py / vtt_parser.py]  →  [ai_client.py]  →  [writer.py]  →  output/
                    │                     │                              │                          │                │
                    └─ watchdog           └─ 识别SRT/VTT格式             └─ 解析为SubtitleEntry     └─ 调用AI API    └─ 生成Markdown
                                             (内容优先,扩展名兜底)           (编码检测/文本提取/短句合并)  使用context-prompt  保存到output/
```

## 任务列表

| # | 任务 | 状态 | 预估代码行 | 开发ID | 调试ID | 备注 |
|---|------|------|-----------|--------|--------|------|
| 0 | 框架搭建：项目初始化、公共配置、依赖管理 | ✅ 完成 | ~80 | - | - | orchestrator直接执行 |
| 1 | SRT解析模块：解析SRT文件，提取纯文本和元数据 | ✅ 完成 | ~630 | a609f69d7c0acc007 | - | srt_parser.py |
| 2 | AI客户端模块：封装API调用、重试、错误处理 | ✅ 完成 | ~673 | a88963aba381f01c0 | - | ai_client.py |
| 3 | Markdown写入模块：根据AI输出生成Markdown文件 | ✅ 完成 | ~260 | a92064987ea8ec677 | - | writer.py |
| 4 | 文件监控模块：watchdog集成，检测新文件并触发流水线 | ✅ 完成 | ~955 | a87367bacd1f1213e | - | watcher.py |
| 5 | 主入口与Windows服务集成：main.py + 开机自启动配置 | ✅ 完成 | ~338 | a19db823e5d566113 | - | main.py + run_daemon.bat |
| 6 | 集成测试：端到端流程验证 | ✅ 完成 | ~830 | a5805c4d70d863fe6 | - | tests/ 43 tests |
| 7 | **迭代1**：WebVTT支持 + SRT/VTT格式自动识别 | ✅ 完成 | ~900 | - | - | subtitle_base.py / vtt_parser.py / subtitle_parser.py；tests/ 78 tests |
| 8 | **迭代2**：内容类型profile（课程/会议/访谈）+ 说话人分档处理 | ✅ 完成 | ~1100 | - | - | profiles.py / speakers.py / doc/prompts/*；tests/ 116 tests |
| 9 | **迭代3**：音频/视频转录前端（ASR + 说话人聚类） | ✅ 完成 | ~1300 | - | - | diarize.py / transcribe.py / models/*；tests/ 147 tests |
| 10 | **迭代4**：桌面 GUI + PyInstaller 单文件夹打包 | ✅ 完成 | ~1700 | - | - | gui.py / app.py / srt_daemon.spec / build_exe.ps1；tests/ 168 tests |
| 11 | **迭代5a**：在线视频 URL → 笔记（字幕后端 + 本地转写兜底） | ✅ 完成 | ~900 | - | - | fetch.py / pipeline.py；tests/ 201 tests |
| 12 | **迭代5b**：笔记内嵌截图（AI 标记 + ffmpeg 抽帧） | ✅ 完成 | ~500 | - | - | screenshots.py + 三套提示词；tests/ 227 tests |
| 13 | **迭代5c**：现代界面（customtkinter）+「在线视频」入口 + 任务中心 | ✅ 完成 | ~1300 | - | - | gui_ctk.py / spec 更新；tests/ 241 tests |
| 14 | **迭代5d**：深色模式（配色双值 + 即时切换 + 记住选择） | ✅ 完成 | ~120 | - | - | gui_ctk.py / gui.py（GuiSettings）；tests/ 244 tests |
| 15 | **迭代6**：本地视频优先（监听目录吃视频 + sidecar 截图 + 在线视频开关） | ✅ 完成 | ~420 | - | - | watcher.py / transcribe.py / pipeline.py；tests/ 269 tests |
| 16 | **迭代7**：长音频自动降级 + GPU 加速（auto 回退）+ 任意模型 | ✅ 完成 | ~300 | - | - | transcribe.py / config / gui_ctk；tests/ 280 tests |

## 当前进度
- **状态**：✅ 迭代 1-4 与 5a 完成；5b / 5c 待做
- **重要变更**：AI API已从 Anthropic Claude 切换为 DeepSeek API（openai SDK，OpenAI兼容）

### 迭代1（WebVTT支持）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 公共基类 | src/srt_daemon/subtitle_base.py | SubtitleEntry 数据模型 + 编码检测/文本提取/短句合并/时间工具 |
| SRT解析 | src/srt_daemon/srt_parser.py | 重构为继承 SubtitleParserBase，公开API与行为不变（SrtEntry 保留别名） |
| VTT解析 | src/srt_daemon/vtt_parser.py | WebVTT cue 语法、cue标识符/设置、可省略小时、NOTE/STYLE/REGION、内联标记清理 |
| 格式识别 | src/srt_daemon/subtitle_parser.py | 内容优先+扩展名兜底，parse_subtitle() 统一入口 |
| 接线 | watcher.py / main.py / writer.py / config.yaml | 处理前增加"识别格式"步骤；front matter 增加 format 字段；监听扩展名增加 .vtt |
| 测试 | tests/test_vtt_support.py | 35 个新测试（VTT解析/格式识别/watcher端到端，AI为mock） |

### 迭代2（内容类型 profile）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 配置 | config.yaml `ai.profiles` / `default_profile` / `auto_detect_profile` / `classifier_*` | 每个 profile 的提示词文件、文件名关键字、分类调用预算 |
| 路由 | src/srt_daemon/profiles.py | Profile 注册表 + 解析顺序（CLI > 文件名 > 内容识别 > 默认）+ 音频文本采样 |
| 说话人 | src/srt_daemon/speakers.py | 标签检测（labeled/inferred）、密集型标签规则、注入 AI 的指令文本 |
| AI层 | src/srt_daemon/ai_client.py | 按 profile 加载提示词（进程内缓存）、`classify_profile()` 轻量分类、上下文块、合并阶段统一说话人 |
| 模板 | doc/prompts/{course,meeting,interview}.md | 三套提示词；原 `context-prompt.md` 内容迁移为 `course.md` |
| 接线 | watcher.py / main.py / writer.py | Stage 1.5 解析 profile；`--profile` 参数；front matter 增加 profile/speakers；文件名模板支持 `{profile}` |
| 测试 | tests/test_profiles.py | 34 个新测试（说话人分析/路由/分类/提示词/watcher 端到端/CLI） |

### 迭代3（音频转录前端）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 聚类后端 | src/srt_daemon/diarize.py | SpeakerTurn 抽象 + sherpa-onnx / pyannote / none 三个后端 + 工厂；sherpa 用本地 ONNX 模型，免账号 |
| 转录流水线 | src/srt_daemon/transcribe.py | ffmpeg 解码 → faster-whisper → 聚类 → 时间重叠分配（可选词级切分）→ 写 VTT/SRT；状态去重；独立 CLI |
| 配置 | config.yaml transcribe: + config.py 属性 | 模型/设备/线程/VAD/词级时间戳/输出格式/聚类参数/状态文件 |
| 模型 | models/speaker-diarization/（已 gitignore） | pyannote segmentation 3.0（5.7MB）+ 3D-Speaker ERes2Net 说话人向量（37.8MB） |
| 实测 | 45s 会议音频基准 + 真实片段端到端 | CPU int8：large-v3 x0.39 / medium x0.89 / small x1.65 realtime；P1000 无法加速 |
| 测试 | tests/test_transcribe.py | 31 个新测试（时间戳/VTT与SRT往返/说话人分配/词级切分/状态/CLI/工厂，ASR 全 mock） |

### 迭代4（桌面 GUI + EXE）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 界面 | src/srt_daemon/gui.py | Tkinter 四页（监听/音频转字幕/设置/日志）+ 托盘（pystray）+ 开机自启（HKCU Run）+ DPI 感知 |
| 入口 | src/srt_daemon/app.py | 单入口分发：GUI / --transcribe / --scan / --once / --selftest |
| 任务模型 | ChildJob / WatcherRunner | 长任务走子进程（可停止、崩溃隔离），监听走后台线程 |
| 配置写入 | set_config_value / set_nested_value | 逐行改写 YAML 标量，保留注释；GUI 偏好另存 gui_settings.json |
| 打包 | srt_daemon.spec + build_exe.ps1 | onedir + windowed，收集 ctranslate2/sherpa_onnx/onnxruntime/av 原生库；组装便携目录 |
| 实测 | dist/SRTDaemon 约 290MB，构建约 80 秒 | 打包自检 SELFTEST OK；GUI 实机启动、四页渲染、历史笔记列表正常 |
| 测试 | tests/test_gui.py | 21 个新测试（YAML 改写/GUI 偏好/日志跟随/命令构造/入口分发） |

### 迭代5a（在线视频 → 笔记）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 抓取 | src/srt_daemon/fetch.py | yt-dlp Python API：解析元数据/分P、字幕优先、音频兜底、字幕格式归一化、代理与 cookies、UTF-8 控制台 |
| 共享流水线 | src/srt_daemon/pipeline.py | "字幕 → profile → AI → 写文件"抽成公共函数，CLI / fetch / GUI 三处共用（避免逻辑漂移） |
| 输出 | writer.py | front matter 增加 source_url / platform（仅当存在） |
| 配置 | config.yaml fetch: 段 | 语言偏好、清晰度上限、代理、cookies、平台成熟度 |
| 实测 | YouTube 19s 视频 / B站 160s 视频 | YouTube：26.4s 出笔记（手动字幕路径）；B站：无字幕 → 下音频 → 本地转写 |
| 测试 | tests/test_fetch.py | 33 个新测试（平台识别/语言偏好/字幕转换/yt-dlp 选项/抓取流程/共享流水线/CLI） |

### 迭代5b（笔记内嵌截图）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 标记 | src/srt_daemon/screenshots.py | 解析 [[screenshot:HH:MM:SS]]（支持小时级）、去重、上限、抽帧、改写为图片引用 |
| 提示词 | doc/prompts/*.md | 三套模板各加一节"截图标记"规则：时间戳必须来自字幕、独占一行、宁少勿多 |
| 抽帧 | ffmpeg -ss <秒> -frames:v 1 -vf scale=1280:-2 | 与转录共用同一个 ffmpeg，无新增依赖 |
| 集成 | pipeline.py / fetch.py | 开启后抓取会下载视频（≤1080p）；抽帧后临时视频随临时目录自动删除 |
| 配置 | config.yaml screenshots: | enabled / max_count / width / quality |
| 实测 | YouTube 19s 视频 | 下载视频 → AI 输出 1 个标记 → 抽帧 → images/shot-000005.jpg（78KB）嵌入，26.5s |
| 测试 | tests/test_screenshots.py | 13 个新测试（标记解析/上限/去重/真实 ffmpeg 抽帧/无视频降级/管线集成） |

### 迭代5c（现代界面）

| 阶段 | 产出 | 说明 |
|------|------|------|
| 主题 | src/srt_daemon/gui_ctk.py | customtkinter 6.x：圆角卡片、扁平配色（#2F6FED 主色）、微软雅黑 UI 字体、自绘侧边栏 |
| 页面 | 任务 / 新建任务 / 在线视频 / 设置 / 日志 | 五页侧边栏导航；页面内容各自可滚动 |
| 复用 | gui.py | 设置读写、日志跟随、子进程任务、监视线程、托盘、自启全部复用，只替换控件 |
| 入口 | app.py | 默认新界面；--classic 回退旧界面；缺 customtkinter 自动回退 |
| 打包 | srt_daemon.spec | 收集 customtkinter 主题资源与 yt_dlp 全部子模块；exe 23.9 MB，总计 349 MB |
| 实测 | 源码 + 打包 exe | 五页渲染正常；「解析」按钮真跑通子进程（标题/字幕信息回填）；窗口自适应 125% 缩放 |
| 测试 | tests/test_gui_ctk.py | 6 个新测试（聚类后端解析/页面清单），入口分发测试改为打桩新界面 |

### 各阶段产出汇总

| Phase | 产出 | 代码行数 |
|-------|------|---------|
| 0 — 框架搭建 | config.py, logger.py, config.yaml, requirements.txt, .env.example, .gitignore | ~310 |
| 1 — SRT解析 | srt_parser.py | ~630 |
| 2 — AI客户端 | ai_client.py（DeepSeek API）| ~430 |
| 3 — MD写入 | writer.py | ~260 |
| 4 — 文件监控 | watcher.py | ~955 |
| 5 — 主入口 | main.py + run_daemon.bat | ~338 |
| 6 — 集成测试 | tests/（43 tests）| ~830 |
| **总计** | | **~3753** |

---

## 技术栈

| 层级 | 选择 | 理由 |
|------|------|------|
| 语言 | Python 3.10+ | Windows兼容性好，watchdog库成熟 |
| 文件监控 | watchdog | 跨平台，事件驱动，比轮询高效 |
| AI API | DeepSeek API (openai SDK, OpenAI兼容) | deepseek-flash（默认，快）/ deepseek-v4-pro（推理） |
| 配置管理 | python-dotenv + YAML | .env存密钥，YAML存配置 |
| 日志 | logging（标准库） | 多级日志，支持文件轮转 |
| Windows服务 | nssm (Non-Sucking Service Manager) | 轻量，比pywin32简单 |

## 依赖顺序

```
任务0（框架） → 任务1（SRT解析） → 任务3（MD写入）
                                  ↘ 任务4（文件监控）→ 任务5（主入口+服务）→ 任务6（集成测试）
               → 任务2（AI客户端）↗
```

任务1和任务2可并行开发（无相互依赖）；任务3依赖任务2；任务4依赖任务1；任务5依赖 3+4。
