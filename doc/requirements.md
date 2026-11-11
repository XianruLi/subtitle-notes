# 需求文档 — 字幕(SRT/WebVTT)转Markdown守护脚本

## 1. 项目背景

用户在 `.\视频字幕\` 下手动存放教学视频的字幕文件
（SubRip `.srt` 或 WebVTT `.vtt`）。
需要一个Windows守护脚本，自动检测新文件并调用AI API生成结构化Markdown学习笔记。
教学内容主要覆盖：机器人、ROS2、C++、机电工程自动化等领域。

## 2. 功能需求

### 2.1 文件监控（watcher）

- **FR-01**: 守护进程开机自启动，常驻后台运行
- **FR-02**: 监听 `.\视频字幕\` 目录（路径可配置）
- **FR-03**: 检测到新 `.srt` / `.vtt` 文件时触发处理流水线
- **FR-04**: 支持检测已有文件（启动时扫描一次，处理尚未处理过的文件）
- **FR-05**: 处理过的文件记录到状态文件（避免重复处理）
- **FR-06**: 处理中的文件加锁或标记（避免并发冲突）
- **FR-07**: 对改名/覆盖的文件视为新文件重新处理

### 2.2 字幕解析（parser）

- **FR-08**: 解析标准SRT格式（序号 + 时间戳 + 文本行）
- **FR-09**: 提取纯文本内容，去除时间戳和序号
- **FR-10**: 保留时间戳作为可选元数据（用于生成"时间点导航"）
- **FR-11**: 处理编码问题（UTF-8 / UTF-8-BOM / GBK 自动检测）
- **FR-12**: 处理格式异常（空行数量不一致、缺少序号等）
- **FR-13**: 合并短字幕行（同一说话人的连续短句合并为段落）

### 2.3 AI处理（ai_client）

- **FR-14**: 调用AI API，传入系统提示词（按内容类型从 `doc/prompts/` 加载，见 2.7）和字幕文本
- **FR-15**: AI系统提示词定位为"机器人/ROS2/C++领域资深工程师与技术架构师"
- **FR-16**: AI处理包括：上下文理解、文字纠错、信息提取、知识结构化、文档总结
- **FR-17**: API调用设置超时（连接超时10s，读取超时120s）
- **FR-18**: 支持自动重试（最多3次，指数退避）
- **FR-19**: API密钥从环境变量 `DEEPSEEK_API_KEY` 读取（模型由 `ai.model` 配置，
  默认 `deepseek-flash`，可用 `SRT_AI_MODEL` 环境变量覆盖）
- **FR-20**: 支持可选的模型选择（通过配置文件）
- **FR-21**: 大SRT文件（>2000条字幕）分段发送，最后合并

### 2.4 Markdown输出（writer）

- **FR-22**: 输出文件命名：`{原文件名}_笔记_{日期}.md`
- **FR-23**: 输出目录：`daemons/output/{原文件名}/`
- **FR-24**: Markdown文件头部包含元信息（源文件、处理时间、AI模型等）
- **FR-25**: 输出结构包含：目录、正文（多级标题）、代码块、关键概念表、总结

### 2.5 配置与日志

- **FR-26**: 配置文件 `config.yaml`：监控路径、输出路径、AI模型、超时等
- **FR-27**: 日志文件：`logs/srt-daemon.log`，支持按天轮转
- **FR-28**: 日志级别可配置（DEBUG/INFO/WARNING/ERROR）
- **FR-29**: 正常运行仅输出INFO级别（启动、处理完成、错误）
- **FR-30**: DEBUG模式输出详细处理过程

### 2.6 格式自动识别（format detection）

- **FR-31**: 处理前先自动识别文件格式（SRT / WebVTT），再分别调用对应解析器，
  调用方（watcher / CLI）无需关心格式差异
- **FR-32**: 识别以**文件内容**为主、**扩展名**为辅：内容与扩展名不一致时以内容为准，
  并记录 WARNING 日志（下载的字幕经常命名错误）
- **FR-33**: 解析WebVTT格式：`WEBVTT` 头（含 Kind/Language 等头部元数据）、可选的cue标识符、
  cue设置（line/position/align 等）、可省略小时的时间戳（`MM:SS.mmm`）、
  `NOTE` / `STYLE` / `REGION` 块
- **FR-34**: 清除WebVTT内联标记（`<v>` / `<c>` / `<i>` / `<b>` / 内联时间戳标签），
  解码HTML实体，并将 `<v Speaker Name>` 转为 `Speaker Name:` 前缀
- **FR-35**: 两种格式解析为同一数据结构（SubtitleEntry），下游 AI处理与 Markdown输出 完全复用
- **FR-36**: 输出Markdown的front matter中记录源字幕格式（`format: SRT` / `format: VTT`）

### 2.7 内容类型 profile（course / meeting / interview）

同一份字幕，按内容类型产出不同侧重的笔记：

- **FR-37**: 三种笔记形态：`course` 知识结构化（章节/代码/术语表）、`meeting` 会议纪要
  （议题/决议/待办/分歧）、`interview` 访谈摘要（按人观点/问答/金句）
- **FR-38**: 每个 profile 使用独立的系统提示词模板（`doc/prompts/{course,meeting,interview}.md`），
  由 `config.yaml` 的 `ai.profiles` 配置（提示词文件 + 文件名关键字）
- **FR-39**: profile 解析顺序：`--profile` 命令行 > 文件名关键字 > 内容自动识别 > `ai.default_profile`
- **FR-40**: 内容自动识别只做一次轻量 API 调用（样本取字幕头部+中段，输出限制为单个单词），
  识别失败或不可用时回退到默认 profile，**不影响文件处理**
- **FR-41**: 说话人分两档处理：
  字幕自带标签（Teams/Zoom 的 `姓名: 内容`、WebVTT `<v 姓名>`、SRT `- 说话人:`）
  → 严格按标签归属，不得改名/合并；
  无标签 → 允许 AI 依据称呼、问答交替推测说话人，但必须使用占位名并**明确标注为推测**，禁止编造真实姓名
- **FR-42**: 输出 Markdown 的 front matter 记录 `format` / `profile` / `speakers`；
  输出文件名模板支持 `{profile}` 占位符
- **FR-43**: 分块处理（超过 `chunk_threshold`）时各分块使用同一 profile 提示词，
  合并阶段统一结构、术语与**说话人编号**，避免跨块人物串号
- **FR-44**: profile 选择结果写入日志（含判定依据：cli / filename / content / default），便于事后排查

### 2.8 音频/视频转录（transcribe，可选前置步骤）

对于没有现成字幕的录音/录像（自己录的会议、讲座），提供一个**独立命令**把音频转成
带说话人标签的字幕，再交由上面的流水线出笔记：

- **FR-45**: 独立命令 python -m src.srt_daemon.transcribe，输入音频/视频（mp3/mp4/wav/m4a/…），
  输出带说话人标签的 .vtt（<v Speaker N> 语音标签）或 .srt，
  默认直接写入监听目录，daemon 自动接力出笔记
- **FR-46**: 转录流程：ffmpeg 解码为 16kHz 单声道 → faster-whisper 识别 → 说话人聚类 → 按时间重叠分配说话人 → 写字幕
- **FR-47**: 说话人聚类后端可插拔：sherpa（本地 ONNX 模型，无需账号/令牌）、
  pyannote（需要免费的 HuggingFace 账号与令牌，模型为 gated）、none（纯转写）
- **FR-48**: 支持把聚类编号映射为真实姓名（--speakers "1=张三,2=李四"），
  未映射的保留 Speaker N 占位，**不允许 AI 编造姓名**
- **FR-49**: 转录状态记录（路径+大小+mtime）避免重复转录；--force 强制重跑；
  --list 干跑查看待处理清单
- **FR-50**: 逐段进度日志与结束汇总（段数、说话人数、音频时长、耗时），单文件失败不影响后续文件
- **FR-51**: 转录**不进入 watcher 主循环**（ASR 远慢于笔记流水线，会阻塞字幕处理），
  提供 --model / --device / --compute-type / --language 等参数按机器性能取舍
- **FR-52**: 解码后启用 VAD 过滤静音；开启 word_timestamps 时在词边界切分跨说话人的句子，
  关闭时整段归给主导说话人

### 2.9 桌面界面与打包（GUI / EXE）

- **FR-53**: 提供桌面窗口统一操作监听目录、笔记类型、立即扫描、单文件处理、音频转字幕、设置与日志
- **FR-54**: 长任务（扫描/单文件/转录）在**子进程**中执行并可停止，窗口不阻塞；
  监听守护进程在后台线程运行，可随时启停
- **FR-55**: 打包为 Windows 单文件夹便携版（PyInstaller onedir + windowed），
  双击 SRTDaemon.exe 即用；配置、提示词、模型、输出、日志都放在 exe 同级目录
- **FR-56**: 打包版通过 --selftest 自检依赖导入、配置解析、提示词与模型路径、ffmpeg 可用性
- **FR-57**: 托盘常驻：关闭窗口最小化到托盘，托盘菜单可显示窗口/启停监听/打开输出目录/退出
- **FR-58**: 开机自启（写 HKEY_CURRENT_USER 的 Run 项）+ 可配置启动后自动监听、启动即隐藏
- **FR-59**: 设置页可修改监听/输出目录、AI 模型、默认笔记类型、转录模型、聚类后端；
  写回 config.yaml 时**保留原有注释与布局**（逐行替换，不重写整份文件）
- **FR-60**: 输出目录与状态文件使用绝对路径，保证命令行版与 GUI 版共享同一份历史与去重记录
  （否则 GUI 首次启动会重新处理全部字幕）；高 DPI 屏下界面清晰（进程 DPI 感知）

### 2.10 在线视频转笔记（fetch）

把视频链接直接变成 Markdown 笔记，不依赖外部工具：

- **FR-61**: 命令 python -m src.srt_daemon.fetch <url>，支持 B站 / YouTube（**stable**）、
  抖音 / 快手（**experimental**）；未知站点按通用流程尝试
- **FR-62**: 抓取策略：**优先使用平台自带字幕**（手动字幕优先于自动字幕，按语言偏好排序），
  无字幕时才下载音频并用本地 whisper 转写（慢路径）
- **FR-63**: 字幕格式归一化：WebVTT / SRT 直接使用；B站 JSON、YouTube json3 转换为 WebVTT
- **FR-64**: B站 CC 字幕需要登录态：支持 Netscape cookies.txt（fetch.cookies_file）或
  从浏览器读取 cookie（fetch.cookies_from_browser）；缺少时自动回落到转写
- **FR-65**: 支持代理（配置 fetch.proxy / 环境变量 HTTPS_PROXY / --proxy），
  YouTube 等站点按需使用
- **FR-66**: 多分P（B站）支持：--dry-run 列出分P与可用字幕语言，--part N 选择分P
- **FR-67**: 输出的笔记 front matter 记录 source_url 与 platform，
  标题作为输出子目录名（去掉 Windows 非法字符）
- **FR-68**: 命令行强制 UTF-8 输出，避免 GBK 控制台下中文标题导致崩溃

### 2.11 笔记内嵌截图（screenshots）

- **FR-69**: 三套提示词都要求 AI 在"必须看到画面才能理解"的位置**单独一行**输出
  [[screenshot:HH:MM:SS]]，时间戳必须复制自字幕里真实出现过的 [HH:MM:SS]，禁止估算
- **FR-70**: 开关 screenshots.enabled（默认关）+ 命令行 --screenshots / --no-screenshots；
  开启后 URL 抓取会额外下载视频（高度上限 fetch.max_height）用于抽帧
- **FR-71**: 用 ffmpeg 在标记时间点抽一帧，写为 <output>/<标题>/images/shot-HHMMSS.jpg，
  Markdown 用**相对路径** ![](images/shot-HHMMSS.jpg) 引用（Obsidian/Typora 直接可读）
- **FR-72**: 上限 screenshots.max_count；重复时间戳去重；抽帧失败或没有视频时**删除标记**
  而不是留下无法解析的占位符
- **FR-73**: 本地字幕文件没有视频来源时自动跳过（标记被清掉），并在日志里说明

### 2.12 现代界面（customtkinter）

- **FR-74**: 默认界面改用 customtkinter（圆角卡片、扁平配色、自绘侧边栏）；
  旧 ttk 界面保留为 --classic 回退（customtkinter 缺失时自动回退）
- **FR-75**: 五个页面：任务 / 新建任务 / 在线视频 / 设置 / 日志，侧边栏切换
- **FR-76**: 任务页展示四个后台任务（扫描 / 单文件 / 转录 / 在线视频）的状态条与停止按钮，
  以及最近 20 篇笔记（点击打开）
- **FR-77**: 在线视频页：输入链接 → 解析（--dry-run）显示标题/时长/分P/字幕，
  选择分P、笔记类型、是否插入截图 → 生成笔记，输出实时回显
- **FR-78**: 新建任务页：本地字幕一键出笔记；音视频转录（模型/设备/语言/聚类/人数/映射/格式/强制）
- **FR-79**: 窗口尺寸按屏幕物理尺寸与 DPI 缩放自适应，保证在 125%/150% 缩放下不被截断
- **FR-80**: 深色模式开关（设置页即时切换、写入 gui_settings.json、下次启动沿用）；
  所有配色以 (浅色, 深色) 双值定义，切换时无需重建界面
- **FR-81**: 字幕**覆盖率门槛** fetch.min_subtitle_coverage（默认 0.6）：
  字幕只覆盖视频一小部分时视为不可用，改为本地转写，避免生成"只描述了开头"的假笔记
- **FR-82**: --dry-run 在 B站 链接上额外报告 CC 状态（语言、条数、覆盖率、是否可用），
  让用户在下单前就知道会走字幕还是转写
- **FR-83**: 监听目录接受**音视频文件**（transcribe.auto_transcribe_media，默认开）：
  丢进一个 mp4/mkv/mp3 会先转录成字幕、再自动生成笔记
- **FR-84**: 转录时在字幕旁写 **sidecar**（<名字>.source.json）记录源视频路径；
  生成笔记时据此抽帧，所以**本地视频也能插入截图**，不需要下载
- **FR-85**: fetch.enabled: false 可隐藏「在线视频」页并拒绝 --fetch，
  只用本地流程时不必看到网络功能
- **FR-86**: 长录音自动降级：时长 ≥ transcribe.long_media.threshold_minutes（默认 30 分钟）
  且未显式指定模型时，自动改用 transcribe.long_media.model（默认 medium），
  日志明确打印切换过程；显式 --model 始终优先
- **FR-87**: GPU 支持：transcribe.device: auto 先尝试 cuda（float16），
  失败自动回退 cpu（int8）；计算精度可选 auto/int8/int8_float16/float16/float32；
  模型名可填任意 CTranslate2 模型（large-v3-turbo、distil-large-v3、社区微调）

## 3. 非功能需求

### 3.1 可靠性
- **NFR-01**: 单文件处理失败不影响后续文件
- **NFR-02**: 守护进程崩溃后能自动恢复（通过nssm配置）
- **NFR-03**: API调用失败的临时文件不污染输出目录

### 3.2 性能
- **NFR-04**: 文件检测延迟 < 5秒（从文件写入完成到开始处理）
- **NFR-05**: 单个典型文件（~500条字幕）总处理时间 < 3分钟

### 3.3 可维护性
- **NFR-06**: 代码遵循 PEP 8
- **NFR-07**: 关键函数有docstring
- **NFR-08**: 模块间低耦合（parser / ai_client / writer 可独立测试）

### 3.4 安全性
- **NFR-09**: API密钥不在代码中硬编码
- **NFR-10**: 文件路径操作防止目录穿越
- **NFR-11**: 不执行字幕中的任何内容（纯文本处理）

## 4. 用户故事

### US-1: 首次使用
> 我下载了一个ROS2教程的字幕文件（SRT 或 VTT）放到"视频字幕"文件夹，
> 几分钟后，在output文件夹中看到一份结构清晰的Markdown笔记，
> 有目录、有代码块、有关键概念总结。

### US-2: 批量处理
> 我一口气下载了5个视频的字幕文件，
> 守护脚本逐一处理它们，不会遗漏也不会混乱。

### US-3: 错误恢复
> 网络断了，API调用失败，
> 脚本记录了错误日志，网络恢复后下一个文件正常处理。

### US-4: 技术术语纠错
> 自动语音识别的字幕把"ROS2"写成了"ross two"，
> AI成功纠正为"ROS2"，并保持技术术语的准确性。
