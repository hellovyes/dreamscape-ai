# -*- coding: utf-8 -*-
"""响应式布局验证：在多个窗口尺寸下截图，并检测工具行换行后是否重叠。"""
import os
import sys

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer, QRect  # noqa: E402

import main  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_shot")
os.makedirs(OUT, exist_ok=True)

app = QApplication(sys.argv)
win = main.MainWindow()
win.move(-3200, -3200)
win.show()

# 造一些剧集卡片，便于观察网格换列效果
for i in range(14):
    win.epgrid.add("http://t/%d.mp4" % i, referer="https://example.com/x",
                   is_m3u8=(i % 3 == 0), title="测试第%d集" % (i + 1))


def overlap_check(tag):
    """检查所有 WrapFlowLayout 内的控件是否有重叠（换行后挤在一起时会出现相交）。"""
    from main import WrapFlowLayout
    bad = []
    for w in win.findChildren(main.QWidget if hasattr(main, "QWidget") else object):
        lay = w.layout()
        if isinstance(lay, WrapFlowLayout):
            rects = []
            for i in range(lay.count()):
                it = lay.itemAt(i)
                wd = it.widget()
                if wd is None or wd.isHidden():
                    continue
                rects.append((wd.objectName() or wd.__class__.__name__, wd.geometry()))
            for a in range(len(rects)):
                for b in range(a + 1, len(rects)):
                    r1, r2 = rects[a][1], rects[b][1]
                    inter = r1.intersected(r2)
                    if inter.width() > 2 and inter.height() > 2:
                        bad.append((tag, rects[a][0], rects[b][0], inter))
    return bad


def shot(name):
    win.grab().save(os.path.join(OUT, name + ".png"))
    print("saved", name)


def step():
    scr = app.primaryScreen().availableGeometry()
    print("screen:", scr.width(), "x", scr.height())
    allbad = []

    sizes = [(1100, 760), (1420, 900), (900, 700)]
    for w, h in sizes:
        win.resize(w, h)
        app.processEvents()
        QTimer.singleShot(0, lambda: None)
        app.processEvents()
        print(f"--- {w}x{h} window real={win.width()}x{win.height()} "
              f"minHint={win.minimumSizeHint().width()}x{win.minimumSizeHint().height()}")
        # 首页
        win._stack.setCurrentWidget(win.home_page)
        app.processEvents()
        shot("home_%dx%d" % (w, h))
        # 工作区：嗅探页
        win._stack.setCurrentWidget(win.workspace_page)
        win.nav_tabs.setCurrentIndex(0)
        app.processEvents()
        shot("sniff_%dx%d" % (w, h))
        allbad += overlap_check("sniff_%dx%d" % (w, h))
        # 生成页
        win.nav_tabs.setCurrentIndex(3)
        app.processEvents()
        shot("gen_%dx%d" % (w, h))
        allbad += overlap_check("gen_%dx%d" % (w, h))
        # 资产页
        win.nav_tabs.setCurrentIndex(2)
        app.processEvents()
        shot("asset_%dx%d" % (w, h))
        allbad += overlap_check("asset_%dx%d" % (w, h))

    print("\n=== overlap issues ===")
    if not allbad:
        print("none")
    for b in allbad[:20]:
        print("  ", b)
    app.quit()


QTimer.singleShot(2200, step)
sys.exit(app.exec())
