# video-finder —— 多平台内容采集器

## 这是什么

自动从 B站 / 抖音 / 知乎 / 贴吧 / TapTap 拉取视频和图文内容，
本地网页界面按平台分列展示，本地 AI 生成摘要并打分排序，附本地搜索。

- 图文平台（知乎 / 贴吧 / TapTap 图文帖）：直接生成摘要，不转写
- 视频平台（B站 / 抖音 / TapTap 视频帖）：下载音频 → 本地转写 → 生成摘要

## 环境要求

- Windows 10/11
- Python 3.12+
- 微软 Edge 浏览器（采集时用一个独立的调试浏览器实例）
- ffmpeg（视频转写切音轨用，需能在命令行直接调用）

## 安装依赖

```powershell
pip install requests websocket-client faster-whisper yt-dlp av
```

## 启动

```powershell
cd d:\video-finder
python main.py serve
```

浏览器打开 http://127.0.0.1:8765 ，勾选平台 → 点「开始拉取」。

## 命令行子命令

| 命令 | 作用 |
|---|---|
| `python main.py serve` | 启动本地网页界面 |
| `python main.py fetch` | 拉取 B站 |
| `python main.py fetch-douyin --n 30` | 拉取抖音 |
| `python main.py fetch-zhihu --n 10` | 拉取知乎 |
| `python main.py fetch-tieba --n 20` | 拉取贴吧 |
| `python main.py fetch-tap --n 20` | 拉取 TapTap |
| `python main.py digest` | 生成 AI 摘要 |
| `python main.py transcribe` | 视频转写 |
| `python main.py search 关键词` | 本地搜索 |
| `python main.py stats` | 查看统计 |
| `python main.py rescore` | 重算评分 |

## 配置

1. 复制 `config.example.json` 为 `config.json`
2. 填入各平台 Cookie（B站 Cookie 必填；抖音 / 知乎 / TapTap 用调试浏览器的登录态即可）
3. AI 摘要走本地 Ollama（默认 http://127.0.0.1:11434 ，模型 qwen2.5:3b）

## 目录说明

```
d:\video-finder
├── main.py               # 入口
├── config.json           # 配置（含 Cookie，勿提交）
├── videofinder/          # 核心代码
│   ├── bilibili.py       # B站
│   ├── douyin.py         # 抖音（含调试浏览器 CDP 基础设施）
│   ├── zhihu.py          # 知乎
│   ├── tieba.py          # 贴吧
│   ├── tap.py            # TapTap
│   ├── asr.py            # 语音转写
│   ├── summarize.py      # AI 摘要
│   ├── scoring.py        # 评分
│   └── web.py            # 网页服务
└── data/                 # 数据库、调试浏览器登录态（勿提交）
```

## 注意事项

- 调试浏览器用独立 Edge profile（`data/edge-profile`），各平台登录态存在里面
- `data/` 含采集数据库和登录态，`config.json` 含 Cookie，**都不要提交到公开仓库**
- 首次视频转写会自动下载 whisper 模型，需要联网

## 扩展新平台

见 `平台扩展指引.txt`：把接入需求按模板发给 AI，即可新增 `fetch-XXX` 命令并接入新平台。
