"""批量提取分享链接——行为层模块（整合进视频嗅探下载器）
在红果/番茄/抖音等桌面App中循环：点分享 → 点复制链接 → 读剪贴板收录 → 关闭弹窗 → 滑动下一集。
两种识别方式：①坐标脚本(精准)  ②图片/文字识别(模板匹配，自动定位)。
本模块不包含任何 UI，抛出 found 信号供宿主把链接导入下载粘贴框。
"""
import os
import re
import json
import time
import ctypes
import ctypes.wintypes

import numpy as np
import cv2
import mss

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_C = 0x43
VK_ESCAPE = 0x1B

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "extract_config.json")

URL_RE = re.compile(r"https?://[^\s，。、；：！？（）()【】《》‘’“”'\"，\`,]+")


def extract_urls(text):
    return list(dict.fromkeys(URL_RE.findall(text or "")))


def load_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def list_windows():
    """枚举当前打开的可见窗口，返回 [(hwnd, 标题, pid), ...]"""
    res = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        t = buf.value
        if t.strip():
            res.append((hwnd, t, pid.value))
        return True

    user32.EnumWindows(CB(cb), 0)
    return res


def get_rect(hwnd):
    r = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)


def activate(hwnd):
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass


class Screen:
    """截图器"""

    def __init__(self):
        self._sct = mss.mss()

    def grab(self, x, y, w, h):
        if w <= 4 or h <= 4:
            return None
        img = self._sct.grab({"left": int(x), "top": int(y),
                              "width": int(w), "height": int(h)})
        return cv2.cvtColor(np.asarray(img), cv2.COLOR_BGRA2BGR)


def find_button(tmpl, screen, thresh=0.5, scales=None):
    """多尺度模板匹配，返回 (中心x, 中心y, score)。坐标相对 screen 局部。"""
    if tmpl is None or screen is None:
        return None
    if scales is None:
        scales = np.linspace(0.5, 1.8, 14)
    th, sw = tmpl.shape[:2]
    sh, sw2 = screen.shape[:2]
    best = None
    for s in scales:
        wt = max(4, int(sw * s))
        ht = max(4, int(th * s))
        if wt > sw2 or ht > sh:
            continue
        rsz = cv2.resize(tmpl, (wt, ht), interpolation=cv2.INTER_AREA)
        r = cv2.matchTemplate(screen, rsz, cv2.TM_CCOEFF_NORMED)
        _, mx, _, mLoc = cv2.minMaxLoc(r)
        if best is None or mx > best[0]:
            best = (mx, mLoc, wt, ht)
    if best is None or best[0] < thresh:
        return None
    score, (px, py), wt, ht = best
    return int(px + wt / 2), int(py + ht / 2), float(score)


def set_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


def click(x, y):
    set_cursor(x, y)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def press_key(vk, ctrl=False):
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def swipe_next(hwnd, method="wheel", amount=4):
    """切下一集：wheel(滚轮下滑) / drag(垂直拖拽)"""
    x, y, w, h = get_rect(hwnd)
    cx, cy = x + w // 2, y + h // 2
    set_cursor(cx, cy)
    time.sleep(0.05)
    if method == "wheel":
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(-120 * amount), 0)
    else:
        sx, sy = cx, cy + 110
        ex, ey = cx, cy - 110
        set_cursor(sx, sy)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        steps = 12
        for i in range(1, steps + 1):
            t = i / steps
            set_cursor(sx + (ex - sx) * t, sy + (ey - sy) * t)
            time.sleep(0.02)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)


def capture_pointer_sample(size=72):
    """截取当前鼠标位置为中心方块，返回 BGR numpy 图与中心坐标"""
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    cx, cy = pt.x, pt.y
    img = Screen().grab(cx - size // 2, cy - size // 2, size, size)
    return img, (cx, cy)


class ExtractWorker(QThread):
    log = Signal(str)
    found = Signal(str)
    state = Signal(int, int)  # (当前集序号, 已提取数量)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hwnd = None
        self.mode = "coord"      # coord坐标脚本 / img图片文字识别
        self.share = (0, 0)
        self.copy = (0, 0)
        self.close = (0, 0)
        self.share_tmpl = None
        self.copy_tmpl = None
        self.close_tmpl = None
        self.esc_close = True     # 勾选后提取到链接即按 Esc 退出弹窗
        self.swipe = None         # 滑动方式，None 用配置默认（wheel/drag）
        self.max_found = 0        # 累计提取到该条数后自动停止；0=不限
        self.max_reached = False
        self._stop = False
        self._seen = set()

    def stop(self):
        self._stop = True

    def run(self):
        cfg = load_cfg()
        share_wait = cfg.get("share_wait", 1.0)
        next_wait = cfg.get("next_wait", 1.0)
        method = getattr(self, "swipe", None) or cfg.get("swipe_method", "wheel")
        amount = cfg.get("swipe_amount", 4)
        thresh = cfg.get("thresh", 0.5)
        mode = self.mode
        sx, sy = self.share
        cp = self.copy
        clk = self.close
        share_t, copy_t, close_t = self.share_tmpl, self.copy_tmpl, self.close_tmpl

        def _read_clipboard():
            try:
                return QApplication.clipboard().text() or ""
            except Exception:
                return ""

        def _collect(txt):
            new = False
            for u in extract_urls(txt):
                if u not in self._seen:
                    self._seen.add(u)
                    self.found.emit(u)
                    new = True
            return new

        seq = 0
        nf = 0
        while not self._stop:
            # 达到提取条数上限自动停止
            if self.max_found and len(self._seen) >= self.max_found:
                self.max_reached = True
                break
            seq += 1
            activate(self.hwnd)
            time.sleep(0.25)
            x, y, w, h = get_rect(self.hwnd)

            if mode == "img":
                shot = Screen().grab(x, y, w, h) if (w > 4 and h > 4) else None
                if shot is None:
                    self.log.emit("  窗口截图失败，跳过本集")
                    self.state.emit(seq, len(self._seen))
                    continue
                loc = find_button(share_t, shot, thresh=thresh)
                if not loc:
                    nf += 1
                    self.log.emit(f"[第{seq}集] 未识别到分享，自动切下集(连续{nf})")
                    swipe_next(self.hwnd, method=method, amount=amount)
                    time.sleep(next_wait)
                    self.state.emit(seq, len(self._seen))
                    continue
                nf = 0
                aex, aey, sc1 = loc
                self.log.emit(f"[第{seq}集] ① 识别并点分享 ({x+aex},{y+aey}) 置信{sc1:.2f}")
                click(x + aex, y + aey)
                time.sleep(share_wait)
                shot2 = Screen().grab(x, y, w, h)
                loc2 = find_button(copy_t, shot2, thresh=thresh) if shot2 is not None else None
                if loc2:
                    cex, cey, sc2 = loc2
                    self.log.emit(f"  ② 识别『复制链接』({x+cex},{y+cey}) 置信{sc2:.2f}")
                    click(x + cex, y + cey)
                else:
                    self.log.emit("  ② 未识别到『复制链接』，回退 Ctrl+C")
                    press_key(VK_C, ctrl=True)
                time.sleep(0.6)
                if _collect(_read_clipboard()):
                    self.log.emit(f"  ✓ 第{seq}集 提取到新链接")
                if getattr(self, "esc_close", True):
                    self.log.emit("  ③ 按 Esc 关闭弹窗")
                    press_key(VK_ESCAPE)
                time.sleep(0.5)
                self.log.emit(f"[第{seq}集] 滑动切下一集…")
                swipe_next(self.hwnd, method=method, amount=amount)
                time.sleep(next_wait)
                self.state.emit(seq, len(self._seen))
                continue

            # ① 坐标脚本模式
            self.log.emit(f"[第{seq}集] ① 点分享 ({sx},{sy})")
            click(sx, sy)
            time.sleep(share_wait)
            self.log.emit(f"  ② 点『复制链接』 ({cp[0]},{cp[1]})")
            click(cp[0], cp[1])
            time.sleep(0.6)
            if _collect(_read_clipboard()):
                self.log.emit(f"  ✓ 第{seq}集 提取到新链接")
            if getattr(self, "esc_close", True):
                self.log.emit("  ③ 按 Esc 关闭弹窗")
                press_key(VK_ESCAPE)
            time.sleep(0.5)
            self.log.emit(f"[第{seq}集] 滑动切下一集…")
            swipe_next(self.hwnd, method=method, amount=amount)
            time.sleep(next_wait)
            self.state.emit(seq, len(self._seen))

        self.state.emit(seq, len(self._seen))


if __name__ == "__main__":
    print("link_module: 行为层模块，请从宿主(视频嗅探下载器)调用。")