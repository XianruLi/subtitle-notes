# 操作指南 — Daemon脚本开发与运维

## 一、项目结构总览

```
daemons/
├── .claude/
│   └── agents/
│       ├── orchestrator-agent.md    # 主编排智能体（中文交互，统筹全局）
│       ├── dev-agent.md             # Python开发智能体（写代码+自测）
│       └── debug-agent.md           # 调试/质检智能体（只读审查）
├── doc/
│   ├── plan.md                      # 当前开发计划
│   ├── requirements.md              # 需求文档
│   ├── lessons-learned.md           # 跨项目经验教训库
│   ├── prompts/                     # 按内容类型的AI系统提示词模板
│   │   ├── course.md                #   课程/教程 → 知识结构化笔记
│   │   ├── meeting.md               #   会议 → 纪要/决议/待办/分歧
│   │   └── interview.md             #   访谈/播客 → 观点/问答/金句
│   ├── context-prompt.md            # 旧提示词入口（已迁移，现为指引文件）
│   └── OPERATIONS.md                # 本文件
├── src/
│   ├── _shared/                     # 跨daemon共享模块（未来）
│   └── srt_daemon/                  # 字幕(SRT/WebVTT)转Markdown守护脚本
├── output/                          # daemon输出目录
└── logs/                            # 运行日志
```

---

## 二、快速启动：如何开发第一个Daemon

### 第1步：进入daemons项目

在Claude Code中，将工作目录切换到 `daemons/`：

```bash
cd D:/personal_proj/daemons
```

> `daemons/` 已配置为独立的Claude Code项目（有自己的 `.claude/.claude.json`），
> 这样可以与主项目目录隔离上下文。

### 第2步：启动orchestrator会话

在 `daemons/` 目录下打开Claude Code，直接对你的orchestrator说：

> "我要开发SRT字幕转Markdown的守护脚本。请以orchestrator身份，
> 先读取 doc/plan.md 和 doc/requirements.md 了解项目，
> 然后从 Phase 0 开始执行开发计划。"

orchestrator会：
1. 读取 plan.md — 看到任务列表
2. 读取 requirements.md — 理解需求细节
3. 执行 Phase 0（框架搭建）
4. 逐任务创建 dev-agent 和 debug-agent 进行开发-测试循环

### 第3步：跟随orchestrator的引导

orchestrator会在每个关键节点向你汇报：
- 当前执行到哪个任务
- 开发/测试结果
- 需要你决策的事项（如：是否接受"低质量通过"、是否调整需求）

你只需要回复确认或给出方向性指导，orchestrator会继续推进。

### 第4步：完成后的部署

当 plan.md 所有任务标记为 ✅ 后：

#### 4a. 安装依赖

```bash
cd D:/personal_proj/daemons
pip install -r requirements.txt
```

#### 4b. 配置环境变量

```powershell
# 在 PowerShell 中设置（用户级永久）
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-...", "User")

# 可选：临时覆盖模型（默认取 config.yaml 的 ai.model = deepseek-flash）
# [Environment]::SetEnvironmentVariable("SRT_AI_MODEL", "deepseek-v4-pro", "User")
```

#### 4c. 设置Windows开机自启动（使用nssm）

```powershell
# 1. 下载 nssm (https://nssm.cc/download)
# 2. 解压到 C:\tools\nssm\
# 3. 以管理员身份运行：
C:\tools\nssm\win64\nssm.exe install SRT-Daemon

# 在弹出的GUI中填写：
# Application: C:\Python310\python.exe
# Startup directory: D:\personal_proj\daemons
# Arguments: -m src.srt_daemon.main
# 然后在 "Details" 标签设置服务名和启动类型为 "Automatic"

# 4. 启动服务
C:\tools\nssm\win64\nssm.exe start SRT-Daemon
```

#### 4d. 测试

1. 放一个 `.srt` 或 `.vtt` 文件到 `D:\personal_proj\视频字幕\`
2. 观察 `daemons/logs/srt-daemon.log`（日志会打印 Detected SRT/VTT format ...）
3. 检查 `daemons/output/` 是否生成了Markdown文件
   （front matter 中的 `format:` 字段显示实际识别到的格式）

#### 4e. 指定笔记类型（profile）

默认自动判断，顺序为：

`--profile` 命令行 > 文件名关键字 > 内容自动识别（一次轻量调用） > `ai.default_profile`

文件名关键字（大小写不敏感，命中即生效）：

| 类型 | 效果 | 常见关键字 |
|------|------|-----------|
| `course` | 课程笔记：章节/代码/术语表/总结 | 课程、教程、课堂、讲座、lecture、course、tutorial |
| `meeting` | 会议纪要：议题/决议/待办（负责人+期限）/分歧 | 会议、例会、周会、评审、复盘、meeting、standup、sync |
| `interview` | 访谈摘要：按人观点/问答/金句/提及资源 | 访谈、采访、播客、对话、interview、podcast |

```bash
# 单个文件强制按会议处理
python -m src.srt_daemon.main --once "D:/personal_proj/视频字幕/standup.vtt" --profile meeting

# 扫描/守护进程整体强制某个类型
python -m src.srt_daemon.main --scan --profile interview
```

生成的笔记 front matter 会记录实际使用的类型与说话人情况：

```yaml
format: VTT
profile: meeting
speakers: "Alice Chen, Bob Li"     # 无标签时会写作 inferred
```

> 说话人取自字幕本身（Teams/Zoom 导出、WebVTT `<v 姓名>`、SRT `- 说话人:`）。
> 字幕没有标签时，AI 只能**推测**发言人边界，文档中会明确标注，人名不会被编造。

#### 4f. 音频/视频转字幕（可选前置步骤）

自己录的会议/讲座没有字幕时，先用转录命令生成**带说话人标签**的字幕，daemon 会自动接力出笔记。

**一次性准备**（约 45MB）：

```bash
pip install sherpa-onnx          # 本地说话人聚类，无需账号/token
mkdir models\speaker-diarization
curl -L -o models\speaker-diarization\seg.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
curl -L -o models\speaker-diarization\3dspeaker.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
```

把 seg.tar.bz2 解压到 models/speaker-diarization/sherpa-onnx-pyannote-segmentation-3-0/，
路径与 config.yaml 的 transcribe.diarization 保持一致。模型目录已在 .gitignore 中。

**用法**：

```bash
python -m src.srt_daemon.transcribe --new                       # 扫描 source_dir 中未转录的文件
python -m src.srt_daemon.transcribe "D:/桌面/meeting_yide.mp3"  # 指定文件
python -m src.srt_daemon.transcribe --dir "D:/桌面" --recursive --new
python -m src.srt_daemon.transcribe --list                      # 干跑，只看清单
python -m src.srt_daemon.transcribe meeting.mp3 --speakers "1=张三,2=李四"   # 聚类编号→真名
python -m src.srt_daemon.transcribe meeting.mp3 --num-speakers 3 --model small
python -m src.srt_daemon.transcribe meeting.mp3 --diarizer none  # 不要说话人标签
```

**本机实测速度**（i7-8850H，CPU int8，beam 5，VAD 开）：

| 模型 | 实时倍率 | 1 小时音频约 |
|------|---------|-------------|
| large-v3 | x0.39 | 2.5 小时 |
| medium | x0.89 | 67 分钟 |
| small | x1.65 | 36 分钟 |

> 说话人聚类额外约音频时长的 1/3（实测 40 秒音频约 15 秒）；Quadro P1000 无法加速
> （int8 需 compute capability ≥ 7.0，float16 实测报错）。长会议建议先用 --model small 看结构，
> 需要高质量文本再上 --model medium。

**说话人聚类调参**（重要）：

自动聚类会**过度切分**——实测一段 4 人左右的会议录音在默认阈值下被切成 8 个说话人。两种解法：

```bash
# 首选：直接告诉它几个人（最准）
python -m src.srt_daemon.transcribe meeting.mp3 --num-speakers 4

# 备选：调高阈值（越高越少人：0.5→8 人、0.8→4 人、0.95→4 人）
python -m src.srt_daemon.transcribe meeting.mp3 --cluster-threshold 0.9
```

聚类只能给出 Speaker 1/2/3 这类编号，**真实姓名必须由你提供**：

```bash
python -m src.srt_daemon.transcribe meeting.mp3 --num-speakers 3 --speakers "1=张三,2=李四,3=王五"
```

不提供映射时，笔记里的说话人保持 Speaker N 占位，AI 不会编造人名。

**为什么不做成 daemon 的一个阶段**：ASR 一次要几十分钟到几小时，watcher 又是串行处理文件的，
放进去会把所有字幕笔记堵住。所以转录是独立命令，也可以以后挂第二个计划任务。

#### 4g. 桌面版（GUI / EXE）

不想敲命令时，直接用桌面窗口。构建一次即可（约 80 秒）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_exe.ps1
```

产物是**单文件夹便携版**：dist/SRTDaemon/

```text
SRTDaemon.exe       双击打开窗口（也可当命令行用）
config.yaml         主配置（监听目录 / AI 模型 / 转录参数 / 笔记模板）
.env                DeepSeek API Key
doc/prompts/        三套笔记模板，可随时改
models/             说话人聚类模型
output/ logs/       笔记与日志
```

窗口分四页：**监听**（目录、笔记类型、启停、立即扫描、单文件、最近笔记）、
**音频转字幕**（模型/语言/聚类/人数映射/列出待处理/开始转录）、
**设置**（路径、AI 模型、默认类型、转录模型、聚类后端、开机自启、托盘行为）、
**日志**（实时跟随 + 子任务输出）。

行为要点：

- 关闭窗口默认**最小化到托盘**（监听继续），托盘右键可退出
- 设置里的「开机自动启动」写 HKEY_CURRENT_USER 的 Run 项，无需管理员
- 长任务在**子进程**里跑，可随时点「停止」，窗口不会卡
- 输出目录与状态文件是绝对路径，所以 GUI 和命令行共享同一份笔记历史与去重记录

自检（打包后必备）：

```powershell
dist\SRTDaemon\SRTDaemon.exe --selftest
```

它会打印依赖导入、配置根目录、提示词与模型是否存在、ffmpeg 是否可用，并写一份
selftest.log 到 exe 同级目录。详细使用说明见 doc/GUI.md。

> 注意：dist/ 里的 .env 含你的 API Key，不要把这个文件夹发给别人。

#### 4h. 在线视频转笔记（URL → Markdown）

```powershell
# 先看看能抓到什么（不下载）：标题、时长、分P、可用字幕语言
python -m src.srt_daemon.fetch --dry-run "https://www.bilibili.com/video/BV1xx411c7mD"
python -m src.srt_daemon.fetch --dry-run "https://youtu.be/xxxxxxxxxxx"

# 直接出笔记
python -m src.srt_daemon.fetch "https://www.bilibili.com/video/BV1xx411c7mD"
python -m src.srt_daemon.fetch "https://youtu.be/xxxxxxxxxxx" --profile meeting

# 多分P / 强制转写 / 换模型
python -m src.srt_daemon.fetch --part 2 <url>
python -m src.srt_daemon.fetch --no-subtitles --model small <url>
```

**抓取策略**：优先平台自带字幕（手动 > 自动，按 fetch.subtitle_languages 顺序）→
没有字幕才下载音频用本地 whisper 转写。所以"有字幕"是秒级，"没字幕"是分钟到小时级。

**字幕覆盖率门槛**：B站 的 AI 字幕（ai-zh）经常是**不完整**的——实测某个 20 分钟的分P
只有 51 条字幕、只覆盖 9%，而且不同时刻请求到的结果还会变（131 条 / 51 条 / 6 条）。
因此抓取前会检查"字幕覆盖了视频的多大比例"，低于 fetch.min_subtitle_coverage（默认 60%）
就不采用，自动改为本地转写。--dry-run 会把这条信息打出来：

```text
bilibili cc: 中文 (ai-zh) - 51 cue(s), covers 9% -> NOT usable, transcription instead
```

分P 之间差异可能很大（同一视频 p1 只有 9%，p2 却是 100%），可以先用 --part N
或 GUI 的「分P」下拉框挑一个字幕完整的分P。

**B站 CC 字幕需要登录态**（没有 cookie 就只能转写）：

1. 浏览器装一个 "Get cookies.txt" 类扩展，登录 B站后导出 cookies.txt
2. 放到 daemons/cookies/bilibili.txt（该目录需要在 .gitignore 里排除）
3. 在 config.yaml 里设 fetch.cookies_file: "cookies/bilibili.txt"，
   或临时用 --cookies cookies/bilibili.txt

**YouTube** 通常无需登录；如果下载失败，把代理写进 fetch.proxy（例如
http://127.0.0.1:10808）或设置环境变量 HTTPS_PROXY。

**实测会遇到的两种平台拒绝**（都已内置处理）：

- HTTP Error 567（B站 CDN 风控）：短时间内连续抓多个视频后出现，重试也不一定恢复。
  程序会退避重试 2 次（5s / 20s），仍失败则给出带原始错误信息的 FAILED 行。
  **解决办法**：配置 cookies（登录态请求很少被 567），或等几分钟再试。
- Could not copy Chrome cookie database：--cookies-from-browser 在浏览器**正在运行**时
  通常失败（Windows 文件锁，yt-dlp 已知限制）。请**关闭浏览器**再试，或改用上面的
  cookies.txt 导出方式（更稳）。

**平台成熟度**：bilibili / youtube = stable；douyin / kuaishou = experimental
（需要 cookie，且 yt-dlp 需保持更新）。运行时会给 EXPERIMENTAL 警告，但不阻止执行。

> 下载的音频/字幕都在临时目录里，出完笔记**自动删除**；笔记 front matter 会记录
> source_url 和 platform，方便回溯。

#### 4j. 桌面界面（现代版）

默认窗口是 customtkinter 版，五个页面：

| 页面 | 能做什么 |
|------|----------|
| 任务 | 目录、监听开关、四个后台任务状态、最近 20 篇笔记 |
| 新建任务 | 本地字幕一键出笔记；音视频转录（模型/语言/聚类/说话人数/映射） |
| 在线视频 | 粘链接 → 解析（标题/时长/分P/字幕）→ 生成笔记，可勾选插入截图 |
| 设置 | 路径 / AI / 转录 / 抓取（代理、cookies）/ 截图 / 窗口与自启 / 深色模式 |
| 日志 | 实时日志 + 后台任务输出 |

启动方式：

```powershell
python -m src.srt_daemon.app              # 默认新界面
python -m src.srt_daemon.app --classic    # 旧 ttk 界面（回退用）
```

重新打包后 exe 会同时带上 customtkinter 与 yt-dlp：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_exe.ps1
dist\SRTDaemon\SRTDaemon.exe --selftest
```

#### 4l. GPU 加速 / 换模型 / 长音频自动降级

**设备自动选择**

```yaml
transcribe:
  device: "auto"        # auto = 先试 cuda，失败自动回退 cpu
  compute_type: "auto"  # auto = cuda 用 float16，cpu 用 int8
```

启动时会打印实际用的设备：

```text
Loading whisper model small (device=cuda compute=float16 ...)
WARNING Cannot load small on cuda/float16: target device or backend ...
Model ready in 2.9s (device=cpu compute=int8)
```

**在同学的 NVIDIA 显卡上启用 GPU**（例如 RTX 5070）：

1. 装最新显卡驱动
2. 装 **CUDA 12.x + cuDNN 9**（CTranslate2 4.7 的要求）
3. config.yaml 里保持 device: auto（或显式写 cuda）
4. 显存参考：large-v3 float16 约 3–4 GB，8 GB 以上可以放心用

> 打包的 exe **没有内置** CUDA 运行库，依赖系统 PATH 里的 CUDA/cuDNN。
> 只想在 exe 里用 GPU 的话，最稳是装 CUDA Toolkit；或直接用源码跑。

**模型选择**（可以填任意 CTranslate2 模型名）

```yaml
transcribe:
  model: "large-v3"
```

| 模型 | 说明 |
|------|------|
| large-v3 | 官方最大、最准；CPU 约 2.5x 实时，GPU 快 10–30 倍 |
| large-v3-turbo | 809M 参数（约一半），更快，精度略低 |
| distil-large-v3 | 蒸馏版，约 6 倍速，偏英文 |
| Systran/faster-whisper-* | HuggingFace 上的 CTranslate2 模型 id，直接填 |
| 社区微调 | 中文/领域微调的 CTranslate2 模型同样可用 |

> Whisper 官方**没有比 large-v3 更大的模型**；想更准一般是换**中文/领域微调**的 large-v3。

**长音频自动降级**（省时间的关键）

```yaml
transcribe:
  long_media:
    enabled: true
    threshold_minutes: 30
    model: "medium"
```

默认**关闭**（无论多长都用配置模型）。打开 `long_media.enabled: true` 后，
超过 30 分钟且**没有显式** --model 时自动换 medium，日志会写：

```text
Long recording (95.0 min >= 30 min): switching model large-v3 -> medium
```

想全程用 large-v3：把 enabled 设成 false，或显式传 --model large-v3。
#### 4k. 本地视频 / 录音工作流（推荐主流程）

用 IDM（或任何下载器）把视频存到本机，然后：

**方式一：丢进监听目录**（最省事）

```powershell
# 把 mp4 / mkv / mp3 丢进 config.yaml 里配置的监听目录即可
# daemon 会自动：转录音频 → 写字幕 → 生成笔记（含截图）
```

转录用 transcribe.auto_transcribe_media 控制（默认开）。第一次处理某个大文件会比较久
（20 分钟视频用 large-v3 约 50 分钟），之后按"路径+大小+mtime"去重不会重复跑。

**方式二：手动转录页面**

GUI →「新建任务 → 音频 / 视频」→ 选文件 → 开始转录。也可以直接用命令行：

```powershell
python -m src.srt_daemon.transcribe "D:/videos/lecture.mp4" --model medium
```

**关于截图**：转录时会自动写一个 <名字>.source.json 记录源视频，
生成笔记时按 [[screenshot:HH:MM:SS]] 标记从**本地视频**抽帧 ——
不需要再下载视频、不涉及 cookie、也不会被 B站 567 限流。
所以在本地流程里打开 screenshots.enabled 是**零网络成本**的。

> 不需要先用格式工厂提取音频：ffmpeg 直接解码 mp4/mkv 里的音轨。

#### 4i. 笔记内嵌截图

AI 在"必须看到画面才能理解"的地方会单独输出一行标记，程序把该时间点的画面截下来插进笔记：

```markdown
[[screenshot:00:03:21]]
```

→ 变成

```markdown
![](images/shot-000321.jpg)
```

图片存放在 output/<视频标题>/images/，与笔记同级，Markdown 用相对路径引用
（Obsidian / Typora / VS Code 都能直接显示）。

**开启方式**（默认关闭，因为要额外下载视频）：

```powershell
# 单次
python -m src.srt_daemon.fetch --screenshots "<url>"

# 永久：config.yaml 里改
screenshots:
  enabled: true
  max_count: 12
  width: 0                 # 0 = 原始分辨率（1080p → 1920×1080）
  best_frame_window: 6     # 标记后 0–6 秒内自动挑内容最丰富的一帧
  quality: 3
```

**成本与边界**：

- 开启后即使有字幕也会**额外下载视频**（高度上限 fetch.max_height，默认 1080p）用于抽帧
- 本地字幕文件（监听目录）没有视频来源，标记会被清掉，不会留下坏图
- 抽帧失败（时间点超出视频长度等）会删除该标记，不会出现无法显示的图片
- 抽帧用已有的 ffmpeg，**无新增依赖**；视频文件在临时目录里，出完笔记自动删除
- 图片是"原样截帧 + 时间戳文件名"，不会自动描述画面内容（DeepSeek 没有视觉能力）

---

## 三、对已有Daemon进行迭代

当需要修改/增强已有daemon时：

> "orchestrator，我要对SRT daemon做一个功能迭代：{描述}。
> 请更新 plan.md 并执行开发-测试循环。"

orchestrator会：
1. 读取当前代码和文档
2. 在 plan.md 中新增迭代任务
3. 创建新的 dev-agent 和 debug-agent（注意：是新建，不是resume之前的）
4. 走完整的开发-测试循环

---

## 四、开发新的Daemon

当你需要开发第二个daemon（比如"自动备份脚本"）时：

> "orchestrator，我要开发一个新的daemon：{描述}。
> 请按照多daemon管理流程，先创建文档再开始开发。"

orchestrator会：
1. 读取 `lessons-learned.md` — 继承之前daemon的经验
2. 检查 `src/_shared/` — 是否有可复用的模块
3. 创建新的 plan.md（或更新现有plan添加新项目）
4. 在 `src/{new_daemon}/` 下开发

**关键原则**：
- 每个daemon在 `src/` 下有独立子目录
- `lessons-learned.md` 跨项目共享
- 公共代码放 `src/_shared/`
- 每个daemon有独立的 `doc/context-prompt.md`（可命名为 `context-prompt-{daemon}.md`）

---

## 五、子智能体管理速查

### 5.1 获取子智能体ID

当orchestrator创建子智能体后，用以下命令获取裸ID：

```bash
# 在 daemons/ 目录下
ls -lt ~/.claude/projects/*/*/subagents/agent-*.meta.json | head -3
```

文件名 `agent-a95e84cd0b54c85ad.meta.json` → 裸ID = `a95e84cd0b54c85ad`

### 5.2 Resume子智能体

```bash
# orchestrator在Claude Code中的操作：
# 使用 Agent 工具，resume 参数填裸ID
Agent(
  resume: "a95e84cd0b54c85ad",
  subagent_type: "dev-agent",
  prompt: "根据反馈修改代码..."
)
```

### 5.3 查看子智能体输出

```bash
# 如果子智能体回复没有显示在UI中
cat ~/.claude/projects/*/*/subagents/agent-{ID}.jsonl | tail -50
```

---

## 六、日志与复盘

### 6.1 主日志

`logs/session-log.md`：orchestrator记录的开发过程日志

### 6.2 运行日志

`logs/srt-daemon.log`：daemon脚本本身的运行日志

### 6.3 子智能体日志

每个 dev-agent 和 debug-agent 的输出都保存在其 JSONL 文件中。

### 6.4 复盘

当项目完成后，orchestrator应更新 `lessons-learned.md`，
总结整个开发过程中的关键经验。

---

## 七、常见问题

### Q: orchestrator在哪个目录运行？
**A**: `D:/personal_proj/daemons/`。这是daemon项目的根目录。

### Q: 子智能体找不到怎么办？
**A**: 用命令 `ls -lt ~/.claude/projects/*/*/subagents/agent-*.meta.json | head -5` 查找最近创建的子智能体。

### Q: 如何修改AI处理提示词？
**A**: 编辑 `doc/context-prompt.md`，然后告诉orchestrator更新相应的任务。

### Q: 多个daemon如何管理配置？
**A**: 每个daemon在 `src/{daemon_name}/` 下有独立的 `config.yaml`。
跨daemon共享的配置（如API密钥）通过环境变量管理。

### Q: 如何回滚某个任务的代码？
**A**: 建议用git管理daemons目录。在当前状态做 `git commit`，需要回滚时用 `git checkout`。
告诉orchestrator你需要回滚，它会协调后续步骤。
