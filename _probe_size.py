# -*- coding: utf-8 -*-
"""窗口尺寸探针：只测量，不改动业务逻辑。运行后自动退出。"""
import os
import sys

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
from PySide6.QtWidgets import QApplication, QSplitter, QTabWidget  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

import main  # noqa: E402

app = QApplication(sys.argv)
win = main.MainWindow()
win.move(-3200, -3200)   # 放到屏幕外，避免打扰
win.show()


def dump():
    scr = app.primaryScreen().availableGeometry()
    print("== screen available ==", scr.width(), "x", scr.height())
    print("== window ==", win.width(), "x", win.height(),
          "minHint=", win.minimumSizeHint().width(), "x", win.minimumSizeHint().height())

    print("\n== nav_tabs pages ==")
    nt = win.nav_tabs
    print("navTabs minHint =", nt.minimumSizeHint().width(), "x", nt.minimumSizeHint().height(),
          " size =", nt.width(), "x", nt.height())
    for i in range(nt.count()):
        w = nt.widget(i)
        print(f"  tab[{i}] {nt.tabText(i)!r:22} {w.__class__.__name__:16} "
              f"minHint={w.minimumSizeHint().width()}x{w.minimumSizeHint().height()} "
              f"size={w.width()}x{w.height()}")

    print("\n== sniff page chain ==")
    for name in ("sniff_page", "workspace_page", "home_page"):
        if hasattr(win, name):
            w = getattr(win, name)
            print(f"  {name:16} minHint={w.minimumSizeHint().width()}x{w.minimumSizeHint().height()} "
                  f"size={w.width()}x{w.height()}")
    for w in (win.sniff_page.findChildren(QSplitter)):
        print("  sniff splitter sizes=", w.sizes(), "size=", w.width(), "x", w.height(),
              "minHint=", w.minimumSizeHint().width(), "x", w.minimumSizeHint().height())
        for i in range(w.count()):
            sub = w.widget(i)
            print(f"     pane{i} minHint={sub.minimumSizeHint().width()}x{sub.minimumSizeHint().height()} "
                  f"size={sub.width()}x{sub.height()}")

    print("\n== all splitters ==")
    for sp in win.findChildren(QSplitter):
        parent = sp.parent()
        print("  splitter in", parent.__class__.__name__, getattr(parent, "objectName", lambda: "")(),
              "sizes=", sp.sizes(), "minHint=",
              sp.minimumSizeHint().width(), "x", sp.minimumSizeHint().height())

    print("\n== key widgets ==")
    for name in ("epgrid", "result_tabs", "player_view", "log_view", "home_list",
                 "script_list", "script_edit", "asset_page", "_gen_episode_stk"):
        if hasattr(win, name):
            w = getattr(win, name)
            print(f"  {name:18} minHint={w.minimumSizeHint().width()}x{w.minimumSizeHint().height()} "
                  f"size={w.width()}x{w.height()}")
    app.quit()


QTimer.singleShot(1500, dump)
sys.exit(app.exec())
