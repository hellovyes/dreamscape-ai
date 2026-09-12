# -*- coding: utf-8 -*-
"""离屏冒烟测试：验证 JS 嗅探能否捕获页面内视频地址"""
import os, sys
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-gpu --no-sandbox'

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QUrl, QTimer

from main import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.show()

# 验证现代化 UI 初始化成功
print("UI init OK; 下载按钮文本=", win.download_btn.text(),
      " 表列数=", win.table.columnCount(),
      " 播放器=", type(win.player).__name__,
      " 控制滑块=", type(win.sld).__name__,
      " 日志控件=", win.log_view.objectName() or "无")
print("主按钮高度=", win.download_btn.sizeHint().height())
assert win.table.columnCount() == 4, "应恢复为选择栏4列"

# 剪贴板读取 + 中文右键菜单输入框
from PySide6.QtWidgets import QApplication as App
assert type(win.input_edit).__name__ == "InputTextEdit", "输入框应为中文右键菜单类"
sam = "漫剧《测试》\n点击链接打开`https://novelquickapp.com/s/abcdefg/`"
App.clipboard().setText(sam)
win._paste_from_clipboard(force=True)
got = win.input_edit.toPlainText()
assert "novelquickapp.com/s/abcdefg" in got, "剪贴板分享链接应填入输入框"
assert win.count_lbl.text().startswith("识别链接: 1"), "应识别出1个网址: " + win.count_lbl.text()
print("剪贴板自动读取+URL识别 PASS; 输入框类=", type(win.input_edit).__name__)

# 命名：集数识别 + 推广文案剔除
from main import _clean_title, DownloadWorker
ep = DownloadWorker._with_episode
for t in ["万妖图录传 第2集 跟我一起免费看",
          "漫剧《主角》第二集·免费好剧，尽在红果",
          "神仙剧情 第12话 全集免费看",
          "剧名 免费好剧"]:
    print(" 命名[", t, "] ->", ep(t))
assert ep("万妖图录传 第2集 跟我一起免费看") == "万妖图录传_第2集"
assert ep("神仙剧情 第12话 全集免费看").endswith("第12集")
print("命名逻辑 PASS")

html = ("<html><body><h1>T</h1><video src='https://cdn.example.com/x/demo_video.mp4' controls></video>"
        "<video><source src='https://cdn.example.com/hls/master.m3u8'></video></body></html>")
data = "data:text/html;charset=utf-8," + html
win.web_view.setUrl(QUrl(data))

result = {"ok": False}

def check():
    win._run_page_scan()
    QTimer.singleShot(1800, check2)

def check2():
    win._run_page_scan()
    urls = list(win.media_repo.keys())
    print("=== 捕获到的媒体 ===")
    for u in urls:
        print("  ", u, "HLS" if win.media_repo[u]["is_m3u8"] else "MP4")
    # 全选切换 + 显示上限高度
    win._select_all()
    a = len(win._checked_rows())
    total = win.table.rowCount()
    win._select_all()
    b = len(win._checked_rows())
    h = win.table.height()
    print(f"全选→全不选: {a}/{total} → {b}; 上限={win.maxrows_combo.currentData()} 表格高={h}")
    assert a == total and b == 0, "全选/取消逻辑错误"
    assert total > 0 and h <= 34 + total * 44, "显示上限高度应受限制"
    result["ok"] = len(urls) > 0
    app.quit()

QTimer.singleShot(2000, check)
QTimer.singleShot(25000, app.quit)
app.exec()
print("SMOKE_RESULT:", "PASS" if result["ok"] else "FAIL")
sys.exit(0 if result["ok"] else 1)