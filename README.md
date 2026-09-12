# 短视频下载器

基于 PySide6 的中文短视频下载与管理工具，附带视频 AI 生成能力（Agnes Video）。

## 功能特性

- **视频嗅探下载**：通过 Edge 浏览器内核嗅探并下载短视频（m3u8 / mp4）
- **字幕 & OCR 引擎**：识别视频中的字幕与文字
- **视频分析**：调用 GLM / Agnes 系模型分析视频结构、提取分镜与对白
- **AI 视频生成**：对接 Agnes Video，支持多参考图、多 Key 并发、TokenPlan 批量生成
- **资产库管理**：人物 / 场景 / 道具资产，支持组内一键 AI 生图（本地图片 + AI 生成）
- **剧集 & 分镜管理**：剧集卡片拖拽排序、分段设置、接续任务自动下载

## 技术栈

Python 3.10 · PySide6 · OpenCV · m3u8 · requests

## 快速开始

```bash
pip install -r requirements.txt
python main.py
```

## 打包为 exe（PyInstaller 单文件）

```bash
python -m PyInstaller --clean --noconfirm --workpath build --distpath dist dreamscape-ai.spec
```
