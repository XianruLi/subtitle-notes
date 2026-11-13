# 架构与流程图

> 版本 v0.3.0 · 293 tests · 2026-11
> 本文把所有逻辑路径画出来，并列出“输入 × 能力”矩阵与已知限制。

## 0. 一页速览

```mermaid
flowchart TD
    subgraph IN["输入"]
        S1["字幕 srt / vtt"]
        S2["音视频 mp4 / mkv / mp3"]
        S3["在线链接 bilibili / youtube / douyin"]
    end

    W["监听目录 watcher"]
    T["本地转录 transcribe"]
    FX["在线抓取 fetch"]
    N["笔记流水线 pipeline.generate_notes"]
    OUT["output/名字/名字_笔记_日期.md + images/"]

    S1 --> W
    S2 --> W
    S3 --> FX

    W -->|"是字幕"| N
    W -->|"是录音/录像"| W2{"同目录已有字幕?"}
    W2 -->|"有"| N
    W2 -->|"没有"| T
    T -->|"字幕 + sidecar"| N

    FX -->|"平台字幕可用"| N
    FX -->|"没有字幕"| T
    FX -->|"要截图"| T

    N --> OUT
```

三条入口、三条处理方式，最后都汇到同一个笔记流水线。

---

## 1. 数据根与配置解析

```mermaid
flowchart TD
    A["进程启动 load_config"] --> B{"环境变量 SRT_DAEMON_HOME?"}
    B -->|"有"| C["直接用"]
    B -->|"没有"| D{"sys.frozen 打包运行?"}

    D -->|"否 源码"| E["从 src/srt_daemon 向上找 .claude/ 或 doc+src → 检出根"]
    D -->|"是"| F{"向上能找到 src/srt_daemon/config.yaml?"}
    F -->|"能 开发版 dist/SRTDaemon"| G["检出根"]
    F -->|"不能 发布 zip"| H["exe 所在目录"]

    C --> I["数据根"]
    E --> I
    G --> I
    H --> I

    I --> J["config.yaml 相对路径基于这里"]
    I --> K[".env 从这里加载 兼容 BOM"]
    I --> L["output/ logs/ models/ cookies/ state"]
```

| 情形 | 数据根 | 为什么 |
|------|--------|--------|
| 源码运行 | 检出根（含 doc/ + src/） | 开发 |
| 开发版 exe（dist/SRTDaemon/） | **检出根** | 和源码共享 output/ 与状态文件 |
| 发布 zip（解压即用） | exe 目录 | 自包含、可放任意位置 |
| 任意情形 | 环境变量指定 | 测试/特殊部署 |

配置里只有**你自己的数据位置**是绝对路径（监听目录、转录默认目录），
项目产物（output、state、cookies、logs）都是相对路径。

---

## 2. 流水线 A：监听目录

```mermaid
flowchart TD
    A["FileWatcher.run"] --> B["启动扫描 _scan_existing 只扫一层"]
    B --> C["watchdog 文件事件"]
    C --> D["去抖 debounce 默认 2s"]
    D --> E["入队"]
    B --> E
    E --> Q["单线程串行队列"]

    Q --> F["_process_file"]
    F --> G{"状态里已处理且 hash 未变?"}
    G -->|"是"| X["跳过"]
    G -->|"否"| H{"扩展名是音视频?"}

    H -->|"否 字幕"| N["流水线 B"]
    H -->|"是"| I{"同目录或转录输出目录已有字幕?"}
    I -->|"有"| N
    I -->|"没有"| J["流水线 C 转录"]
    J --> N

    N --> K["标记 .processed.json"]
```

要点：

- 只处理**监听目录这一层**，不递归子目录（见第 9 节限制）
- 默认**只吃字幕文件**（transcribe.auto_transcribe_media: false）：录音/视频一律跳过，并在日志里
  提示改用「新建任务」页，避免用猜出来的参数白跑一小时转录
- 打开自动转录后：音视频文件如果旁边已有字幕后缀（lecture.en.vtt 也算），**不会重复转录**
- 同一个文件处理失败不会影响后续文件；进程内用 in_progress 防重入

---

## 3. 流水线 B：字幕 → 笔记（核心）

```mermaid
flowchart TD
    A["字幕文件"] --> B["parse_subtitle 内容优先识别 SRT/VTT"]
    B --> C{"解析到条目?"}
    C -->|"否"| X["报错跳过 不写笔记"]
    C -->|"是"| D["analyse_speakers → labeled / inferred"]

    D --> E["profile 解析（优先级从上到下）"]
    E --> E1["1 CLI --profile"]
    E1 --> E2["2 文件名关键字 lecture / meeting / interview"]
    E2 --> E3["3 AI 分类 classifier 只看开头样本"]
    E3 --> E4["4 ai.default_profile 默认 course"]

    E4 --> F["按 profile 载入提示词"]
    F --> F2["注入说话人规则 labeled 严格归属 / inferred 只能推测"]
    F2 --> G{"条目数 > chunk_threshold 默认 2000?"}
    G -->|"是"| H["分块 → 逐块调用 → 拼接"]
    G -->|"否"| I["单次调用"]
    H --> J["Markdown 草稿"]
    I --> J

    J --> K["write_note → 流水线 E 截图"]
    K --> L["output/名字/名字_笔记_日期.md"]
```

front matter 会记录：来源文件名、处理时间、模型、格式、profile、说话人、条目数、
文本长度、截图数；在线抓取时还多 source_url 和 platform。

---

## 4. 流水线 C：音视频 → 字幕

```mermaid
flowchart TD
    A["transcribe 源文件"] --> B{"状态说已转录?"}
    B -->|"是 且未指定 --force"| X["跳过"]
    B -->|"否"| C["长音频策略"]
    C --> C1{"long_media.enabled 且 时长 ≥ 阈值 且未显式 --model?"}
    C1 -->|"是（可选，默认关闭）"| C2["改用 long_media.model 默认 medium"]
    C1 -->|"否"| C3["用配置模型 large-v3"]

    C2 --> D["_ensure_asr 设备尝试链"]
    C3 --> D
    D --> D1["cuda + float16"]
    D1 -->|"失败"| D2["cpu + int8"]
    D2 --> E["ffmpeg 解码 16kHz 单声道"]

    E --> F["faster-whisper ASR 词级时间戳"]
    F --> G["说话人聚类 sherpa-onnx / pyannote / none"]
    G --> H["按时间重叠分配说话人 跨说话人句子按词边界切分"]
    H --> I["写字幕 vtt / srt / both"]
    I --> J{"源文件是视频?"}
    J -->|"是"| K["写 sidecar 名字.source.json 记录视频路径"]
    J -->|"否"| L["只写字幕"]
    K --> M["写入 .transcribed.json"]
    L --> M
```

写出的字幕落在监听目录（transcribe.output_dir），所以下一轮 watcher 会自动接力出笔记。
手动路径可以一步到位：`--transcribe ... --notes --profile meeting` 转录完成后直接走流水线 B
出笔记（GUI「新建任务」页的「转录后自动生成笔记」就是这个开关）。

---

## 5. 流水线 D：在线视频 → 笔记

```mermaid
flowchart TD
    A["fetch URL"] --> B["platform_of 识别平台"]
    B --> C["probe 快速取元数据"]
    C --> C1{"B站?"}
    C1 -->|"是"| C2["view API 一次拿到全部分P标题/时长"]
    C1 -->|"否"| C3["yt-dlp extract_flat 只取列表"]

    C2 --> D["resolve_part 只解析选中的那个分P"]
    C3 --> D

    D --> E{"prefer_subtitles 且 有字幕?"}
    E -->|"B站"| F1["player/v2 取 CC 上传者字幕 / ai-zh"]
    F1 --> F2{"合理性 + 覆盖率 ≥ 60%?"}
    F2 -->|"是"| G["转成 WebVTT"]
    F2 -->|"否 最多重试 2 次"| H["放弃字幕"]

    E -->|"其它站"| F3["yt-dlp 字幕 手动 > 自动 按语言偏好"]
    F3 --> F4{"格式转换成功 VTT/SRT/B站JSON/json3?"}
    F4 -->|"是"| G
    F4 -->|"否"| H

    H --> I{"要插截图?"}
    I -->|"是"| J["下载视频 ≤ fetch.max_height"]
    I -->|"否"| K["下载音频 bestaudio"]
    J --> L["流水线 C 转录"]
    K --> L
    L --> M["流水线 B（带上视频路径）"]
    G --> N["流水线 B"]
```

两个校验是踩过坑后加的：

- **时长合理**：字幕最后一条不能超出视频长度太多（防止 CDN 串台）
- **覆盖率**：字幕必须覆盖视频 ≥ 60%（B站 ai-zh 有时只有前 9%）

---

## 6. 流水线 E：截图

```mermaid
flowchart TD
    A["AI 输出 [[screenshot:HH:MM:SS]]"] --> B["parse_markers 支持小时 去重 上限 max_count"]
    B --> C["找视频 优先级"]
    C --> V1["1 显式传入（fetch / GUI）"]
    V1 --> V2["2 sidecar 名字.source.json（转录产物）"]
    V2 --> V3["3 同目录同名字幕对应的视频 lecture.en.vtt ↔ lecture.mp4"]

    V3 --> D{"拿到视频?"}
    D -->|"是"| E["ffmpeg -ss 时间 -frames:v 1（width: 0 → 原始分辨率）"]
    E --> F["images/shot-HHMMSS.jpg"]
    F --> G["标记替换为 ![](images/shot-HHMMSS.jpg)"]
    D -->|"否"| H["静默删除标记 不留占位符"]
    G --> I["写笔记并把图片移到笔记同级 images/"]
    H --> I
```

**挑帧策略**：模型标的是"讲到这里"的时间戳，画面往往晚 1–3 秒出现，
可能正好落在转场或黑屏上。所以每个标记会向后取 0/2/4/6/8 秒几帧（
screenshots.best_frame_window，默认 6 秒），用灰度标准差挑**内容最丰富**的一帧
（黑帧、淡入、空白幻灯片分数低），其余删掉；最终文件名仍按标记时间命名。

截图失败（时间点超出视频、ffmpeg 报错）时同样是删除标记，绝不留下坏图。
分辨率由 screenshots.width 控制：**0 = 保持原片分辨率**（1080p 就是 1920×1080），
设成正数则等比缩放到该宽度。

---

## 7. 桌面界面

```mermaid
flowchart TD
    subgraph PAGES["五个页面"]
        P1["任务 目录 / 启停监听 / 后台任务 / 最近笔记"]
        P2["新建任务 本地字幕 / 音视频转录"]
        P3["在线视频 粘链接 / 解析 / 分P / 截图开关"]
        P4["设置 路径 / AI / 转录 / 抓取 / 截图 / 窗口"]
        P5["日志 实时日志 + 任务输出"]
    end

    subgraph JOBS["后台执行"]
        J1["监听 = 本进程线程"]
        J2["扫描/单文件/转录/抓取 = 子进程 调用自己"]
    end

    P1 --> J1
    P1 --> J2
    P2 --> J2
    P3 --> J2
    J2 --> R["完成 → 刷新笔记列表 / 托盘提示"]
```

- 长任务都走子进程，可单独停止，崩了也不影响窗口
- 关闭窗口默认最小化到托盘；设置页可开开机自启

---

## 8. 状态与去重

| 文件 | 作用 | 键 |
|------|------|----|
| output/.processed.json | 字幕是否已出过笔记 | 绝对路径 + SHA-256 |
| output/.transcribed.json | 录音是否已转录 | 路径 + 大小 + mtime |
| 名字.source.json | 字幕来自哪个视频 | 字幕文件同级 |
| gui_settings.json | 窗口偏好（主题、托盘、最近选择） | — |

同一个文件内容变了（hash 不同）会重新出笔记；--force 可强制重跑转录。

---

## 9. 能力矩阵与已知限制

| 你手里的东西 | 现在能做什么 | 怎么用 |
|--------------|--------------|--------|
| 只有字幕 .srt/.vtt | ✅ 直接出笔记 | 放进监听目录 / --once |
| 字幕 + 同名视频（同目录） | ✅ 笔记 + 截图，且**不会**重复转录视频 | 同上（本轮新增） |
| 字幕名带语言后缀 lecture.en.vtt | ✅ 仍能配对视频 | 同上（本轮新增） |
| 只有视频 / 录音 | ✅ 转录 + 笔记 | 丢进监听目录 / 转录页 |
| 视频 + 字幕但**不同名** | ⚠️ 只出笔记（无截图） | 改名成一致，或转录一遍拿 sidecar |
| 在线链接 | ✅ 笔记（字幕优先） | 在线视频页 / --fetch |
| 子目录里的文件 | ⚠️ 监听不递归 | 用 transcribe --recursive 或 --once |
| 多个监听目录 | ⚠️ 只能配一个 | 用 --once / --scan 指定 |
| 同一视频的多语言字幕 | ⚠️ 用你放进去的那个 | 选定一个再处理 |

---

## 10. 你的场景：YouTube 视频 + CC 字幕

假设下载到 D:\\Videos\\youtube\\：

```text
lecture.mp4          ← 视频（IDM 或别的下载器）
lecture.en.vtt       ← Chrome 扩展导出的 CC 字幕
```

两条路，都不用转录（字幕已经是现成的）：

**A. 放进监听目录（最省事，会被自动处理）**

```powershell
copy "D:\\Videos\\youtube\\lecture.*" "D:\\personal_proj\\视频字幕\\"
# daemon 监听中就会自动处理；或跑一次：
cd D:\\personal_proj\\daemons
python -m src.srt_daemon.main --scan
```

**B. 原地处理（不搬文件）**

```powershell
cd D:\\personal_proj\\daemons
python -m src.srt_daemon.main --once "D:\\Videos\\youtube\\lecture.en.vtt" --profile course
```

两者结果一样：

```text
output/lecture.en/
  lecture.en_笔记_2026-11-13.md
  images/shot-000321.jpg        ← 如果开了截图
```

注意：

- 字幕解析成功就**不会**再转录视频（省几十分钟）
- 视频只用于截图；没有视频也能出笔记，只是标记会被清掉
- 文件名对不上（例如 Some Title (1080p).mp4 对 Some Title.en.vtt）时不会自动配对

---

## 11. 代码地图

| 文件 | 职责 |
|------|------|
| app.py | 单一入口：GUI / --scan --once / --transcribe / --fetch / --selftest |
| main.py | 字幕→笔记 CLI、--scan、--once |
| watcher.py | 监听、去抖、队列、状态、视频跳过转录 |
| subtitle_parser.py subtitle_base.py srt_parser.py vtt_parser.py | 格式识别与解析 |
| profiles.py | 课程/会议/访谈 profile 与解析顺序 |
| speakers.py | 说话人标签检测（labeled / inferred） |
| ai_client.py | DeepSeek 调用、分块、重试、提示词 |
| pipeline.py | 字幕→笔记公共流水线、截图嵌入、sidecar、同名视频 |
| writer.py | Markdown 与 front matter |
| transcribe.py | 解码 / ASR / 聚类 / 字幕输出 / 长音频降级 |
| diarize.py | sherpa-onnx / pyannote / none 后端 |
| fetch.py | yt-dlp + B站 API 抓取、字幕校验、覆盖率 |
| screenshots.py | 标记解析与 ffmpeg 抽帧 |
| gui.py gui_ctk.py | 旧 ttk 界面 / 现在的界面 |
| config.py | 配置、数据根、路径解析 |
