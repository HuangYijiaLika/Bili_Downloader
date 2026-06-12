# Bili Downloader

B站追番/收藏夹管理与批量下载工具，带交互式 TUI 界面。

## 功能

- **追番管理** — 浏览追番列表、详情、剧集信息，批量下载整部番剧
- **收藏夹管理** — 浏览收藏夹、视频详情（含收藏时间），一键批量下载
- **并行下载** — 可配置的最大并行数，多行实时进度条
- **智能查重** — 本地 `.biliadl` 记录已下载视频，跳过重复
- **清晰度自选** — 8K→360P，所选不可用时自动降级
- **配置系统** — 下载目录、临时目录、并行数等均可自定义

## 安装

```bash
git clone https://github.com/HuangYijiaLika/Bili_Downloader.git
cd Bili_Downloader
pip install -r requirements.txt
```

依赖：
- `bilibili-api-python>=17` — B站 API
- `questionary>=2` — 交互式终端 UI
- `httpx>=0.24` — HTTP 客户端

## 使用

```bash
python -m biliapi
```

或双击 `start.bat`（Windows）。

首次运行会提示输入 cookie，按指引粘贴浏览器导出的 cookie.json 即可登录。

## 配置

登录后在「⚙️ 设置」中可配置：
- 下载目录（默认 `./download`）
- 临时目录（默认 `./temp`）
- 最大并行下载数（1-8）

配置文件位置：`~/.biliapi/config.json`

## 项目结构

```
biliapi/
├── __main__.py          # 入口
├── tui.py               # 交互式 TUI 界面
├── commands.py          # API 调用与下载逻辑
├── auth.py              # 凭证管理
├── config.py            # 配置读写
└── cascade_checkbox.py  # 级联复选框组件
```
