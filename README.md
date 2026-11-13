# 字幕笔记 · Subtitle Notes

把 **字幕 / 录音 / 视频** 变成结构化 Markdown 学习笔记。

> **English**: Turn SRT/WebVTT subtitles — or local audio/video — into AI-written Markdown
> study notes. Transcription runs **locally** (faster-whisper, CPU or CUDA); only the note
> writing calls the DeepSeek API. Windows desktop app + CLI, single-folder EXE build.

![演示](doc/demo.gif)

![任务页](doc/gui-tasks.png)

![新建任务页](doc/gui-new.png)

![在线视频页](doc/gui-online.png)

## 同学怎么用（4 步）

§§§powershell
# 1) 克隆 + 安装依赖
git clone https://github.com/XianruLi/subtitle-notes.git
cd subtitle-notes
pip install -r requirements.txt

# 2) 装 ffmpeg（转录和截图都要用；装过可跳过）
winget install Gyan.FFmpeg

# 3) 填 API Key
copy .env.example .env        # 编辑 .env，写 DEEPSEEK_API_KEY=sk-...

# 4) 把字幕 / 视频丢进 videos/，然后启动
start_gui.bat                 # 图形界面
python -m src.srt_daemon.main --scan   # 或者命令行扫一遍
§§§

笔记默认生成在 `output/<文件名>/` 下（可以改成跟着源文件走，见下）。

> **不想装 Python？** 直接下 Windows 便携版（解压即用，不用装依赖）：
> [Releases · v0.4.0](https://github.com/XianruLi/subtitle-notes/releases/tag/v0.4.0)
> —— 解压后双击 `SRTDaemon.exe`，第一次仍需自己填 `.env`。

## 三种入口，按场景选

| 你手里的东西 | 用哪个入口 | 为什么 |
|---|---|---|
| 只有字幕（srt / vtt） | **丢进监听目录**，全自动 | 零成本、秒级，唯一要判断的"类型"能自动识别 |
| 字幕 + 同名视频 | 同上，或「新建任务 → 本地字幕」 | 同名视频自动用于截图，**不会重新转录** |
| 录音 / 录像（会议、采访） | **新建任务 → 音频/视频→字幕** | 只有这里能指定**说话人数**、姓名映射、类型、模型 |
| 在线视频（B站 / YouTube） | **在线视频页** | 解析 → 选分P → 类型 → 出笔记，优先用平台字幕 |

> 监听目录**默认只处理字幕**（`transcribe.auto_transcribe_media: false`）：
> 长时间转录不应该由程序替你猜参数。想恢复全自动，把它设成 `true`。

## 能做什么

- **监听目录**：丢一个 `.srt` / `.vtt`，自动生成笔记；按课程 / 会议 / 访谈三套模板，
  并处理说话人（谁说的）
- **本地视频 / 录音**：GUI「新建任务」页选文件，指定说话人数 / 类型 / 模型后转录，
  勾选「转录后自动生成笔记」就能一步出笔记（本地 whisper + 说话人聚类）
- **截图**：AI 在"必须看到画面"的地方输出标记，程序用 ffmpeg 从视频抽帧插入笔记；
  **默认保持原片分辨率**（1080p → 1920×1080），并在标记后 0–6 秒内自动挑内容最丰富的一帧
- **笔记保存位置**：集中在 `output/`，或跟着字幕/视频所在文件夹走（`output.mode`）
- **在线视频（可选）**：B站 / YouTube / 抖音 链接直接出笔记，优先用平台字幕
- **桌面 GUI**：五个页面、深色模式、系统托盘、开机自启、任务进度与日志
- **EXE**：一键打包成单文件夹便携版，双击即用

## 笔记放在哪里

`config.yaml`：

§§§yaml
output:
  directory: "output/"    # 集中模式下的根目录
  mode: central           # central（默认）| source
§§§

| 模式 | 笔记落在 | 适合 |
|---|---|---|
| `central` | `output/<名字>/<名字>_笔记_日期.md` + `images/` | 统一管理 |
| `source` | `<字幕/视频所在文件夹>/<名字>/` + `images/` | 一个项目一个文件夹，笔记跟着走 |

界面也能改：**设置 → 路径 → 保存位置**。
在线视频（URL）的笔记永远写集中目录；状态文件始终在集中目录。

## 截图怎么来的

1. 有视频时，提示词会明确要求模型在"必须看画面"的地方输出
   `[[screenshot:HH:MM:SS]]`（3–8 个，时间戳必须照抄字幕）
2. 每个标记会在 **0/2/4/6/8 秒** 内取样几帧，用灰度方差挑**内容最丰富**的一帧
   （黑屏、淡入、空白幻灯片分数低），其余删掉
3. 图片按标记时间命名（`images/shot-000321.jpg`），重新生成笔记时旧图会自动清理
4. 视频来源优先级：**显式传入 > 转录 sidecar > 同目录同名视频**

> 视频文件名带语言后缀也能配对：`lecture.en.vtt` ↔ `lecture.mp4`。
> 想改图片宽度：`screenshots.width`（`0` = 原片分辨率）。

## 快速开始（Windows）

**要求**：Python 3.10+（测试于 3.13）、ffmpeg、一个 DeepSeek API Key

1. 安装依赖

   §§§powershell
   pip install -r requirements.txt
   §§§

2. 安装 ffmpeg（转录和截图都要用）

   §§§powershell
   winget install Gyan.FFmpeg
   §§§

3. 配置 API Key

   §§§powershell
   copy .env.example .env
   # 编辑 .env，填入 DEEPSEEK_API_KEY=sk-...
   §§§

4. 放一个字幕到 `videos/` 目录

5. 启动

   §§§powershell
   # 图形界面（也可以双击 start_gui.bat）
   python -m src.srt_daemon.app

   # 或者命令行扫一遍
   python -m src.srt_daemon.main --scan
   §§§

6. 笔记生成在 `output/<字幕名>/` 下

> 默认配置 `config.yaml` 用**相对路径**（`videos/`、`output/`、`logs/`），
> 想用别的目录直接改它即可。

## 模型与速度

转录是本地跑的，速度只取决于你的 CPU/GPU：

| 模型 | 参数 | CPU（8 线程 int8） | GPU（RTX 5070 等） |
|------|------|--------------------|---------------------|
| small | 244M | 约 0.6× 音频时长 | 接近实时 |
| medium | 769M | 约 1.1× | 快 |
| large-v3 | 1550M | 约 2.5× | 快 10–30 倍 |

**长音频自动降级（可选，默认关闭）**：打开后超过 30 分钟且没有显式指定模型时，
自动从 `large-v3` 换成 `medium`（日志会写 `Long recording ... switching model`）。
默认 `enabled: false`，也就是无论多长都用你配置的模型：

§§§yaml
transcribe:
  long_media:
    enabled: true
    threshold_minutes: 30
    model: "medium"
§§§

关闭时行为就是"永远用配置的模型"；命令里显式 `--model` 永远优先于这套策略。

## GPU 加速（RTX 30/40/50 系列）

§§§yaml
transcribe:
  device: "auto"        # auto = 先试 cuda，失败自动回退 cpu
  compute_type: "auto"  # cuda 用 float16，cpu 用 int8
§§§

1. 装最新显卡驱动
2. 装 **CUDA 12.x + cuDNN 9**（CTranslate2 4.7 的要求）
3. 保持 `device: auto`，启动日志会显示实际用的设备：

§§§text
Model ready in 2.9s (device=cuda compute=float16)
§§§

显存参考：`large-v3` float16 约 3–4 GB；8 GB 以上可以放心用。

> 没有可用的 GPU 也没关系：程序会打印警告并自动回退到 `cpu/int8`。

## 支持的模型

`transcribe.model` 可以填任意 **CTranslate2** 模型名：

| 值 | 说明 |
|----|------|
| `large-v3` | 官方最大、最准（默认） |
| `large-v3-turbo` | 809M 参数，更快，精度略低 |
| `distil-large-v3` | 蒸馏版，约 6 倍速，偏英文 |
| `Systran/faster-whisper-*` | HuggingFace 上的模型 id，直接填 |
| 社区微调 | 中文 / 领域微调的 CTranslate2 模型同样可用 |

> Whisper 官方没有比 large-v3 更大的模型；更准一般靠**微调版**或更好的音频质量。

## 在线视频（可选）

默认开启。解析用 yt-dlp，**优先使用平台自带字幕**，没有字幕才下载音频转写。

- **B站**：CC 字幕（含 AI 字幕）需要登录 cookie。用浏览器扩展导出 Netscape 格式的
  `cookies.txt`，放到 `cookies/` 下，然后在 `config.yaml` 里填路径：

  §§§yaml
  fetch:
    cookies_file: "cookies/bilibili.txt"
  §§§

  程序会检查字幕**覆盖率**（低于 60% 视为不可用，自动转写），并校验字幕时长是否与
  视频匹配，避免拿到别的视频的字幕。
- **YouTube**：通常不需要登录；连不上就在 `fetch.proxy` 里填代理。
- **抖音 / 快手**：实验性，通常需要 cookie。
- 不想要网络功能：`fetch.enabled: false`，页面和 `--fetch` 命令都会消失。

## 目录结构

§§§text
config.yaml        配置（相对路径可移植）
.env               API Key（不要提交）
videos/            放字幕 / 视频 / 录音（监听目录）
output/            生成的笔记（含 images/ 截图）
logs/              日志
cookies/           B站 cookie（可选，已在 .gitignore）
models/            说话人聚类 ONNX 模型（按需下载）
src/srt_daemon/    源码
doc/prompts/       三套提示词模板，可以直接改
doc/architecture.md  全部流程图（监听 / 笔记 / 转录 / 抓取 / 截图）
doc/GUI.md         界面说明
doc/OPERATIONS.md  部署、GPU、运维
§§§

说话人聚类模型需要放到 `models/speaker-diarization/`，见 `doc/OPERATIONS.md` 4f；
不装也能用，设 `transcribe.diarization.backend: none` 即可。

## 命令行

§§§powershell
python -m src.srt_daemon.app                       # 打开界面
python -m src.srt_daemon.main --scan               # 处理 videos/ 里所有新文件
python -m src.srt_daemon.main --once "x.vtt" --profile meeting
python -m src.srt_daemon.transcribe "videos/lecture.mp4" --model medium
python -m src.srt_daemon.transcribe --list         # 还有哪些没转录

# 转录 + 出笔记一步完成（指定说话人数与类型）
python -m src.srt_daemon.main --transcribe "会议.mp3" --num-speakers 3 --notes --profile meeting

python -m src.srt_daemon.fetch "https://b23.tv/xxx" --screenshots
python -m src.srt_daemon.fetch --dry-run "https://youtu.be/xxx"
§§§

> `transcribe` 只生产字幕；`main --transcribe ... --notes` 会在转录完成后
> 直接调用笔记流水线（GUI 里就是「转录后自动生成笔记」那个勾选）。

## 打包成 EXE

§§§powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_exe.ps1
# 产物：dist/SRTDaemon/SRTDaemon.exe（单文件夹便携版）
dist\SRTDaemon\SRTDaemon.exe --selftest
§§§

打包前请先关掉正在运行的 `SRTDaemon.exe`，否则会因文件占用失败。

## 常见问题

**杀毒软件报毒**：PyInstaller 打包的 exe 未签名，容易被误报，加白名单即可。

**笔记不生成**：看日志页 —— 常见是 API Key 没填、ffmpeg 不在 PATH、字幕没有可解析内容。

**笔记生成了但正文是空的（v0.3.0 的问题）**：v0.4.0 起，模型返回空内容会被当成失败并
自动重试，重试仍失败则报错，不会再写出一个只有文件头的空笔记。

**把视频丢进监听目录没反应**：这是默认行为（`auto_transcribe_media: false`）。
录音/视频请走「新建任务」页，那里能设说话人数和类型。

**选项目文件夹里的字幕能截图吗**：能。同目录同名视频（含 `talk.en.vtt` 这类语言后缀）
会自动被用作截图来源。

**转录太慢**：换 `medium`/`small`，或开 GPU；也可以打开长音频自动降级。

**GPU 加载失败**：确认 CUDA 12 + cuDNN 9 已装、驱动够新；不改也能自动回退 CPU。

**B站没有字幕**：确认 cookies 有效；有的分P 字幕只覆盖很小一部分，程序会拒绝这种字幕。

**说话人分错**：字幕没有音频时只能"推测说话人"；想要真实区分，请把音视频交给转录并
填「说话人数」。

## 更新日志

**v0.4.0（2026-11-13）**

- 笔记可以在源文件夹里（`output.mode: source`），也可以继续集中存放
- 录音/视频默认走手动流程；`--notes` / `--profile` 让「转录 → 笔记」一条命令完成
- 截图：保持原片分辨率、标记后 6 秒内自动挑最佳帧、重新生成自动清理旧图
- 监听路径与手动路径统一到同一套流水线（修掉"监听不截图"的 bug）
- 空响应自动重试（修掉"生成空笔记"）；界面新增任务进度条与状态
- 字幕卡片可选笔记类型；修复选择项目文件夹外字幕时路径丢失
- 长音频自动降级改为可选（默认关闭）

**v0.3.0**：首个可分享版本（GUI + 便携版 EXE + 短视频/长音频/在线视频）

## 许可

MIT，见 [LICENSE](LICENSE)。欢迎提 issue / PR。
