# -*- coding: utf-8 -*-
"""
红果短视频下载器 - 桌面版 (PySide6 + QtWebEngine)

核心逻辑（与浏览器插件一致，无需 API Key）：
  用户粘贴分享链接 → 软件用内嵌 Chromium 自动打开链接并播放
  → 通过网络请求拦截 + 页面 JS 嗅探，捕获真实(已带签名)媒体地址
  → 勾选后下载(mp4 直下 / m3u8 自动合并)
"""
import os
import html
import re
import sys
import json
import time
import ctypes
import ctypes.wintypes
import logging
import threading
import subprocess
from datetime import datetime

from PySide6.QtCore import (Qt, QThread, QTimer, Signal, QUrl, QAbstractNativeEventFilter,
                            QSizeF, QSize, QPoint, QRect, QSettings, QRunnable, QThreadPool, QObject)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QFileDialog, QTableWidget,
    QTableWidgetItem, QProgressBar, QPlainTextEdit, QHeaderView, QMessageBox,
    QGroupBox, QAbstractItemView, QCheckBox, QTextEdit, QSlider, QMenu, QScrollArea, QGridLayout,
    QGraphicsView, QGraphicsScene, QTabWidget, QFormLayout, QDialog, QListWidget, QListWidgetItem,
    QToolButton, QListView, QInputDialog, QFrame, QStackedWidget, QStyle, QStyledItemDelegate, QLayout
)
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo, QWebEngineProfile
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QPen, QFont, QDrag,
                           QTextBlockFormat, QTextCharFormat, QTextListFormat)
from PySide6.QtCore import Qt, Signal, QSize, QUrl, QRect, QTimer, QEvent, QMimeData
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaMetaData
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem, QVideoWidget

from link_module import (list_windows, get_rect, activate, click, press_key,
                         swipe_next, find_button, capture_pointer_sample, Screen,
                         ExtractWorker, load_cfg as load_extract_cfg,
                         save_cfg as save_extract_cfg,
                         VK_C)

import config
from downloader import HongguoDownloader
from m3u8_downloader import M3U8Downloader
from ocr_engine import OcrWorker, get_engine, available as ocr_available, download_to_temp
from video_analyzer import (FrameGrabber, VaDownloadWorker, GlmWorker, CombineWorker,
                            SceneSegmenter, PROMPTS, SEGMENT_PROMPT_ZH,
                            SEGMENT_PROMPT_NO_DIALOGUE_ZH, SEGMENT_COMBINE_ZH,
                            SEGMENT_COMBINE_WITH_DIALOGUE_ZH, SCRIPT_PROMPT_ZH,
                            call_glm)
from agnes_video import (CreateTaskWorker, PollWorker, download_video,
                         ASPECT_RATIOS, VIDEO_SECONDS)
from player_widgets import OverlayPlayer, VolSlider, ClickVideoWidget, vol_icon, VideoPlayerCard, FloatPlayerWindow
from gen_area import GenAreaWidget
import mitm_proxy
from mitm_proxy import CapturingProxy, ensure_ca as mitm_ensure_ca, \
    install_ca as mitm_install_ca, set_system_proxy as mitm_set_proxy

BASE_DIR = (os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(BASE_DIR, "download_log.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                              logging.StreamHandler()])
logger = logging.getLogger("app")

NAME_RULES = [
    ("标题(含集数)_时间戳 (推荐)", "title_ts"),
    ("仅标题(含集数)", "title"),
    ("源文件名_时间戳", "src_ts"),
    ("仅源文件名", "src"),
]

# 识别 第N集（N 支持阿拉伯数字或汉字数字，如 2 / 二 / 十二），兼容 集/话/期/回
EP_RE = re.compile(r"第\s*([0-9]+|[一二三四五六七八九十百千零〇]+)\s*(?:集|话|期|回|part\s*[0-9]+)",
                   re.IGNORECASE)

# 页面标题里常见的推广/冗余文案，命名时剔除
_CLEAN_TOKENS = [
    "跟我一起免费看", "免费好剧，尽在红果", "免费好剧尽在红果", "免费好剧",
    "点击链接打开", "复制本条消息", "复制本消息", "全集", "免费看全集", "免费看",
    "看全集", "全集在线看", "抢先看", "打开红果短剧", "快打开", "速看",
    "更多精彩",
]


def _strip_promos(t):
    """多轮剔除推广文案（处理嵌套词），并清理标题尾部残词"""
    t = t or ""
    changed = True
    while changed:
        changed = False
        for tok in _CLEAN_TOKENS:
            if tok in t:
                t = t.replace(tok, "")
                changed = True
        for tok in _CLEAN_TOKENS:
            if t.endswith(tok):
                t = t[:-len(tok)]
                changed = True
    return t


def _clean_title(title):
    """剔除推广文案，规整分隔符，得到干净的剧名/集数标题"""
    t = _strip_promos(title)
    # 去掉相邻的标点/箭头/空格堆叠，仅保留一个
    t = re.sub(r"[\s｜|·•\[\]【】《》<>()（）.。\-—:*：，,、]+", "_", t)
    t = re.sub(r"_{2,}", "_", t).strip("_ ")
    return t or (title or "")

# ---------- 现代主题 ----------
APP_QSS = """
QMainWindow { background: #eef2f9; }
QWidget { font-family: "Microsoft YaHei UI","Microsoft YaHei"; font-size: 13px; color: #1f2937; }

QGroupBox { background: #ffffff; border: 1px solid #e5eaf3; border-radius: 12px;
            margin-top: 15px; padding: 12px 10px 10px 10px; font-weight: 600; font-size: 13px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 8px; color: #475569; }
QGroupBox#card { padding-top: 14px; }
QGroupBox#card::title { color: #2563eb; }

QFrame#genAreaCard { background: #ffffff; border: 1px solid #dbe3f0; border-radius: 12px; }

QTabBar#navTabBar { background: #eef2f9; }
QTabBar#navTabBar::tab {
    min-height: 34px; padding: 6px 20px;
    font-size: 15px; font-weight: 600; color: #475569;
    background: #e6ecf6; border: 1px solid #dbe3f0; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px; margin: 4px 8px 0 0; }
QTabBar#navTabBar::tab:hover { background: #f1f5fb; }
QTabBar#navTabBar::tab:selected {
    background: #ffffff; color: #1d4ed8; font-weight: 800;
    border-bottom: 3px solid #2563eb; }
QTabWidget#navTabs::pane { border: none; top: -1px; }

QLineEdit, QTextEdit, QSpinBox, QComboBox { background: #fbfdff; border: 1px solid #d1d9e6;
    border-radius: 8px; padding: 7px 10px; selection-background-color: #2563eb; }
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus { border: 2px solid #2563eb; }
QTextEdit { background: #f8fafc; }
QComboBox::drop-down { border: none; width: 22px; }

QLabel { color: #475569; }

QPushButton { border: none; border-radius: 8px; padding: 8px 16px;
             font-weight: 600; background: #eef2f9; color: #334155; }
QPushButton:hover { background: #e2e8f0; }
QPushButton#accentBtn { background: #2563eb; color: #ffffff; font-size: 13px; padding: 10px 20px; }
QPushButton#accentBtn:hover { background: #1d4ed8; }
QPushButton#primaryBtn { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563eb, stop:1 #7c3aed);
    color: #ffffff; font-size: 15px; font-weight: 700; border-radius: 10px; padding: 15px; }
QPushButton#primaryBtn:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2b6df5, stop:1 #8b47f2); }
QPushButton#primaryBtn:disabled { background: #a9b6e8; }
QPushButton#stopBtn { background: #ffffff; color: #dc2626; border: 1px solid #f5a6a6; padding: 9px 14px; }
QPushButton#stopBtn:hover { background: #fef2f2; }
QPushButton#stopBtn:disabled { color: #f0b4b4; border-color: #f7d7d7; background: #fafafa; }
QPushButton#startBtn { background: #16a34a; color: #ffffff; border: 1px solid #16a34a; padding: 9px 14px; }
QPushButton#startBtn:hover { background: #15803d; }
QPushButton#ghostBtn { background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0; padding: 8px 14px; }
QPushButton#ghostBtn:hover { background: #e2e8f0; }

QFrame#vsep { background: #e2e8f0; max-width: 1px; min-width: 1px; margin: 2px 6px; }

QLabel#breadcrumb { font-size: 12px; font-weight: 600; color: #64748b; }
QLabel#sbLogCount { font-size: 12px; font-weight: 600; color: #64748b; }

QTableWidget { background: #ffffff; border: 1px solid #e5eaf3; border-radius: 8px;
               gridline-color: #eef2f7; alternate-background-color: #f8fafc; }
QTableWidget::item { padding: 6px 10px; }
QTableWidget::item:selected { background: #dbeafe; color: #1e3a8a; }
QHeaderView::section { background: #f1f5f9; color: #334155; font-weight: 600; border: none;
    border-bottom: 1px solid #e5eaf3; padding: 7px; }
QTableCornerButton::section { background: #f1f5f9; border: none; }

QProgressBar { background: #e5eaf3; border: none; border-radius: 6px; height: 10px; }
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563eb, stop:1 #7c3aed);
    border-radius: 6px; }

QSlider::groove:horizontal { border: 1px solid #cbd5e1; height: 6px; border-radius: 3px; background: #e5eaf3; }
QSlider::sub-page:horizontal { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563eb, stop:1 #7c3aed);
    border-radius: 3px; }
QSlider::handle:horizontal { width: 16px; margin: -6px 0; border-radius: 8px; background: #ffffff;
    border: 1px solid #c9d4e3; }

QWidget#playerBar { background: transparent; }
QWidget#playerBar QLabel#playTime { background: transparent; color: #ffffff; font-size: 12px;
    font-weight: 600; }
QWidget#playerBar QSlider::groove:horizontal { border: none; height: 4px; border-radius: 2px;
    background: rgba(255, 255, 255, 0.35); }
QWidget#playerBar QSlider::sub-page:horizontal { background: #ffffff; border-radius: 2px; }
QWidget#playerBar QSlider::handle:horizontal { width: 16px; margin: -6px 0; border-radius: 8px;
    background: #ffffff; border: 2px solid rgba(255, 255, 255, 0.6); }
QPushButton#volBtn { border: none; background: transparent; padding: 4px; }
QPushButton#volBtn:hover { background: rgba(255, 255, 255, 0.18); border-radius: 6px; }
QWidget#volPopup { background: rgba(15, 23, 42, 0.62); border-radius: 8px; }
QWidget#volPopup QSlider::groove:vertical { border: none; width: 4px; border-radius: 2px;
    background: rgba(255, 255, 255, 0.32); }
QWidget#volPopup QSlider::sub-page:vertical { background: #ffffff; border-radius: 2px; }
QWidget#volPopup QSlider::handle:vertical { height: 16px; margin: 0 -6px; border-radius: 8px;
    background: #ffffff; border: 2px solid rgba(255, 255, 255, 0.6); }

QPlainTextEdit#logView { background: #0f172a; color: #cbd5e1; border: none; border-radius: 8px;
    font-family: "Consolas","Microsoft YaHei UI"; font-size: 12px; padding: 6px; }
QWidget#appBody { background: transparent; }
QScrollArea#epScroll { background: transparent; border: none; }
QScrollArea#epScroll > QWidget > QWidget { background: transparent; }
QFrame#titleBar { background: #ffffff; border-bottom: 1px solid #e2e8f0; }
QFrame#titleBar QLabel { background: transparent; }
QPushButton#titleBtn { background: transparent; border: none; color: #94a3b8; font-size: 16px;
    border-radius: 6px; padding: 0 10px; min-width: 28px; min-height: 26px; }
QPushButton#titleBtn:hover { background: #e2e8f0; color: #334155; }
QPushButton#titleBtnClose { background: transparent; border: none; color: #94a3b8; font-size: 15px;
    border-radius: 6px; padding: 0 10px; min-width: 28px; min-height: 26px; }
QPushButton#titleBtnClose:hover { background: #ef4444; color: #ffffff; }
QToolTip { background: #ffffff; color: #1f2937; border: 1px solid #d1d9e6; padding: 4px; }
"""

# ---- 窗口尺寸策略（可自由调节：非全屏、非最大化，默认按屏幕可用区自适应）----
WIN_MIN_W, WIN_MIN_H = 900, 700      # 允许拖到的最小尺寸（再小内容会挤，但不会锁死）
WIN_RATIO_W, WIN_RATIO_H = 0.78, 0.82  # 首次启动时占屏幕可用区的比例
WIN_MAX_W, WIN_MAX_H = 1560, 960     # 首次启动的尺寸上限（避免超大屏上铺太满）
WIN_DEFAULT_W, WIN_DEFAULT_H = 1280, 820  # 「重置布局」使用的默认尺寸


class FlowLayout(QLayout):
    """自适应换行布局：窗口够宽时一行放多个（生成区）、窄时自动换行，避免挤压截断。"""
    def __init__(self, parent=None, margin=0, hspacing=10, vspacing=10):
        from PySide6.QtCore import QRect, QPoint, QSize, Qt as _Qt
        super().__init__(parent)
        self._items = []
        self._h = hspacing
        self._v = vspacing
        self._hl = getattr(_Qt, "Horizontal", 1)
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        if 0 <= i < len(self._items):
            return self._items.pop(i)
        return None

    def expandingDirections(self):
        from PySide6.QtCore import Qt as _Qt
        return _Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        try:
            return self._do_flow(int(w), 0, True)
        except Exception:
            return self.minimumSize().height()

    def setGeometry(self, rect):
        from PySide6.QtCore import QRect
        super().setGeometry(rect)
        self._do_flow(rect.width(), 0, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        from PySide6.QtCore import QSize
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_flow(self, width, dummy, only_height):
        from PySide6.QtCore import QRect
        m = self.contentsMargins()
        eff = max(140, width - m.left() - m.right())
        x = 0
        y = 0
        line_h = 0
        for it in self._items:
            w = it.widget()
            if w is not None and w.isHidden():
                continue
            hs = it.sizeHint()
            item_w = hs.width()
            if item_w > eff:
                item_w = eff   # 单个生成区过宽时收缩到可用宽度，避免横向溢出截断
            if line_h > 0 and x + item_w > eff:
                x = 0
                y += line_h + self._v
                line_h = 0
            if not only_height:
                it.setGeometry(QRect(x + m.left(), y + m.top(), item_w, hs.height()))
            x += item_w + self._h
            line_h = max(line_h, hs.height())
        return y + line_h + m.top() + m.bottom()


class WrapFlowLayout(FlowLayout):
    """工具行专用换行布局。

    与 FlowLayout 的区别：会把「换行后的真实高度」通过 heightForWidth 回报给父布局，
    因此嵌在 QVBoxLayout 里也不会重叠；窗口变窄时按钮自动折行，而不是把窗口撑宽。
    （生成卡片区仍然用原来的 FlowLayout，滚动区里的行为保持不变。）
    """
    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        try:
            return self._do_flow(int(w), 0, True)
        except Exception:
            return self.minimumSize().height()

    def sizeHint(self):
        from PySide6.QtCore import QSize as _QSize
        ms = self.minimumSize()
        g = self.geometry()
        w = g.width() if g.width() > 0 else 640
        return _QSize(ms.width(), max(ms.height(), self.heightForWidth(w)))


# 中文右键菜单的多行输入框
class InputTextEdit(QTextEdit):
    def contextMenuEvent(self, event):
        try:
            menu = QMenu(self)

            def add(text, fn, enabled):
                a = menu.addAction(text)
                a.setEnabled(enabled)
                a.triggered.connect(fn)
                return a

            clip = QApplication.clipboard()
            clip_txt = clip.text() or "" if clip else ""
            has_sel = self.textCursor().hasSelection()
            add("撤销", self.undo, self.canUndo())
            add("剪切", self.cut, has_sel)
            add("复制", self.copy, has_sel)
            add("粘贴", self.paste, bool(clip_txt))
            menu.addSeparator()
            add("全选", self.selectAll, bool(self.toPlainText()))
            add("清空", self.clear, bool(self.toPlainText()))
            menu.exec(event.globalPos())
        except Exception:
            # 任何异常都回退到系统默认菜单，保证右键菜单必定出现
            super().contextMenuEvent(event)


# 页面 JS 嗅探脚本：扫描 <video> 及 <source> 的真实媒体地址
MEDIA_SCANNER = r"""
(function(){
  try{
    var out=[];
    function add(u){ if(u && typeof u==='string' && u.trim() && out.indexOf(u)<0) out.push(u.trim()); }
    function abs(u){ try{ return new URL(u, location.href).toString(); }catch(e){ return u; } }
    // 1) 从 fetch/XHR hook 缓存里取
    try{
      if(window.__media_urls && window.__media_urls.length){
        window.__media_urls.forEach(function(u){ add(abs(u)); });
      }
    }catch(e){}
    // 2) video/audio DOM
    document.querySelectorAll('video').forEach(function(v){ if(v.currentSrc) add(v.currentSrc); else if(v.src) add(v.src); });
    document.querySelectorAll('video source, source[type^="video"]').forEach(function(s){ if(s.src) add(s.src); });
    document.querySelectorAll('audio').forEach(function(a){ if(a.currentSrc) add(a.currentSrc); });
    // 3) 网络资源
    try{
      performance.getEntriesByType('resource').forEach(function(r){
        if(r && r.name && /\.m3u8(\?|$)/i.test(r.name)) add(abs(r.name));
      });
    }catch(e){}
    // 4) 脚本文本
    try{
      var scRe=/['"]([^'"]*?\.m3u8(\?[^'"]*)?)['"]/gi, m;
      document.querySelectorAll('script').forEach(function(s){
        var t=s.textContent||'';
        while((m=scRe.exec(t))!==null){ add(abs(m[1])); } scRe.lastIndex=0;
      });
    }catch(e){}
    return JSON.stringify(out);
  }catch(e){ return '[]'; }
})()
"""

# 媒体 URL 拦截 hook：在每个页面加载时注入，覆盖 fetch/XHR/video.src
MEDIA_HOOK_JS = r"""
(function(){
  if(window.__media_hook_installed) return;
  window.__media_hook_installed = true;
  window.__media_urls = [];
  var MAX = 200;
  function is_video_u(u){
    var s = u.toLowerCase();
    if(/\.(m3u8|mp4|flv|m4v|webm|ts)(\?|$)/.test(s)) return true;
    if(/mime_type=video|video_mp4|video\/mp4|\/video\/|\.mp4\?/.test(s)) return true;
    return false;
  }
  function capture(u){
    try{
      if(!u || typeof u!=='string') return;
      if(is_video_u(u)){
        if(window.__media_urls.indexOf(u) < 0){
          window.__media_urls.push(u);
          if(window.__media_urls.length > MAX) window.__media_urls.shift();
        }
      }
    }catch(e){}
  }
  // hook fetch
  var origFetch = window.fetch;
  if(origFetch){
    window.fetch = function(input, init){
      try{
        var u = (typeof input==='string') ? input : (input && input.url ? input.url : '');
        capture(u);
      }catch(e){}
      return origFetch.apply(this, arguments);
    };
  }
  // hook XMLHttpRequest
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url){
    try{ capture(url); }catch(e){}
    return origOpen.apply(this, arguments);
  };
  // hook HTMLMediaElement.src
  try{
    var proto = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    if(proto && proto.set){
      Object.defineProperty(HTMLMediaElement.prototype, 'src', {
        set: function(v){ try{ capture(v); }catch(e){} return proto.set.call(this, v); },
        get: function(){ return proto.get.call(this); },
        configurable: true
      });
    }
  }catch(e){}
  // hook src 属性
  try{
    ['src','data-src','href'].forEach(function(attr){
      var prop = Object.getOwnPropertyDescriptor(HTMLSourceElement.prototype, attr) ||
                 Object.getOwnPropertyDescriptor(HTMLElement.prototype, attr);
      if(prop && prop.set){
        var origSet = prop.set;
        Object.defineProperty(HTMLSourceElement.prototype, attr, {
          set: function(v){ try{ capture(v); }catch(e){} return origSet.call(this, v); },
          get: function(){ return prop.get.call(this); },
          configurable: true
        });
      }
    });
  }catch(e){}
  // MutationObserver: 监听 video 元素添加
  try{
    new MutationObserver(function(muts){
      muts.forEach(function(m){
        m.addedNodes.forEach(function(n){
          if(n.tagName === 'VIDEO' || n.tagName === 'SOURCE'){
            if(n.src) capture(n.src);
            if(n.currentSrc) capture(n.currentSrc);
          }
          if(n.querySelectorAll){
            n.querySelectorAll('video, source, video source').forEach(function(el){
              if(el.src) capture(el.src);
              if(el.currentSrc) capture(el.currentSrc);
            });
          }
        });
      });
    }).observe(document.documentElement, {childList: true, subtree: true});
  }catch(e){}

  // 自动尝试播放所有 video：隐藏的嗅探播放器需主动触发才会加载媒体（muted 绕过自动播放限制）
  function autoplayAll(){
    try{
      document.querySelectorAll('video').forEach(function(v){
        try{
          if(v.src) capture(v.src);
          if(v.currentSrc) capture(v.currentSrc);
          v.muted = true;
          v.setAttribute('muted','');
          var p = v.play();
          if(p && p.catch) p.catch(function(){});
        }catch(e){}
      });
    }catch(e){}
  }
  try{
    document.addEventListener('DOMContentLoaded', function(){ setTimeout(autoplayAll, 600); });
    setTimeout(autoplayAll, 1800);
    setInterval(autoplayAll, 4000);
  }catch(e){}
})();
"""

# 页面描述/集数扫描脚本：抓取 description 文本（含遍历同源 iframe、正文内定位“第N集”）
PAGE_META_SCANNER = r"""
(function(){
  try{
    var texts=[];
    var sel='p.video-info__description, [class*="description"], meta[name="description"], meta[property="og:description"]';
    function grabDoc(doc){
      if(!doc) return;
      try{
        var el=doc.querySelector(sel);
        if(el){ var t = el.tagName==='META' ? (el.getAttribute('content')||'') : (el.innerText||el.textContent||''); if(t) texts.push(String(t).substring(0,400)); }
      }catch(e){}
      try{
        if(doc.body){
          var bt=String(doc.body.innerText||'');
          var i=bt.search(/第\s*[\d一二三四五六七八九十百千零〇]+\s*(集|话|期|回)/);
          if(i>=0){ texts.push(bt.substring(i, i+40)); }
          else if(texts.length===0){ texts.push(bt.substring(0, 120)); }
        }
      }catch(e){}
    }
    grabDoc(document);
    try{ document.querySelectorAll('iframe, frame').forEach(function(f){ try{ grabDoc(f.contentDocument); }catch(e){} }); }catch(e){}
    var all = texts.join('\n');
    return JSON.stringify({text: all.substring(0,800)});
  }catch(e){ return JSON.stringify({text:''}); }
})()
"""


# ---- 网络请求拦截器：捕获页面发出的媒体类型请求（含签名）----
class MediaInterceptor(QWebEngineUrlRequestInterceptor):
    media_found = Signal(str, str)  # (媒体URL, 所属页面URL=referer)

    def interceptRequest(self, info):
        try:
            url = info.requestUrl().toString()
            ref = info.firstPartyUrl().toString()
            u = url.lower()
            is_media_url = (".m3u8" in u or u.rstrip("?").endswith(".mp4")
                            or u.rstrip("?").endswith(".flv")
                            or "mime_type=video" in u or "video_mp4" in u
                            or "/video/" in u or ".mp4?" in u)
            rt = info.resourceType()
            # 1) 媒体资源类型（video/audio/source 直接加载）—— 一律抓，不依赖扩展名
            if rt == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMedia:
                self.media_found.emit(url, ref)
            # 2) fetch/XHR/内嵌子资源里，若 URL 具视频特征也抓。
            #    仅使用本版本枚举实际存在的成员（PySide6 无 ResourceTypeFetch，避免异常吞掉整条）。
            elif is_media_url and rt in (
                QWebEngineUrlRequestInfo.ResourceType.ResourceTypeXhr,
                QWebEngineUrlRequestInfo.ResourceType.ResourceTypeSubResource,
                QWebEngineUrlRequestInfo.ResourceType.ResourceTypePrefetch,
                QWebEngineUrlRequestInfo.ResourceType.ResourceTypeObject,
            ):
                self.media_found.emit(url, ref)
        except Exception:
            pass


# ---- 后台下载线程：对选定媒体 URL 下载（mp4 直下 / m3u8 合并）----
class PlaylistProbeWorker(QThread):
    """后台探测多条 m3u8 的分片数与总时长，用于嗅探增强（选出每集最长/完整版）。
    返回 dict: url -> (segments, duration_seconds)。"""
    done = Signal(dict)

    def __init__(self, urls, headers=None, parent=None):
        super().__init__(parent)
        self.urls = list(dict.fromkeys(urls or []))
        self.headers = headers or {}
        self.headers.setdefault("User-Agent",
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")

    def run(self):
        dl = M3U8Downloader(headers=self.headers, max_workers=1, timeout=15)
        try:
            pairs = dl.probe_all_m3u8(self.urls)
        except Exception:
            pairs = []
        self.done.emit({u: (n, d) for u, n, d in pairs})
        dl.close()
class MitmWorker(QThread):
    """本地 MITM 抓包代理的工作线程：后台运行代理，抓到媒体链接发 signal。"""
    media = Signal(str)
    log = Signal(str)

    def __init__(self, host, port, ca_key, ca_cert, parent=None):
        super().__init__(parent)
        self._proxy = None
        self._host = host
        self._port = port
        self._ca_key = ca_key
        self._ca_cert = ca_cert
        self._run = True

    def run(self):
        self._proxy = CapturingProxy(
            self._host, self._port, self._ca_key, self._ca_cert,
            on_media=self.media.emit, on_log=self.log.emit)
        if not self._proxy.start():
            return
        while self._run and self._proxy._running:
            self.msleep(200)
        self._proxy.stop()

    def stop(self):
        self._run = False
        if self._proxy:
            self._proxy.stop()


class DownloadWorker(QThread):
    item_status = Signal(int, str, int)
    log_msg = Signal(str, str)
    all_done = Signal(int, int)

    def __init__(self, items, output_dir, naming, headers, parent=None):
        """
        items: [(row, media_url, referer, is_m3u8, default_title)]
        """
        super().__init__(parent)
        self.items = items
        self.output_dir = output_dir
        self.naming = naming
        self.headers = headers or {}
        self._stop = threading.Event()
        self._m3u8 = None
        self._session = None

    def stop(self):
        self._stop.set()
        if self._m3u8:
            try:
                self._m3u8.stop()
            except Exception:
                pass

    def run(self):
        import requests
        ok = fail = 0
        self._session = requests.Session()
        self._session.headers.update(self.headers)
        self._m3u8 = M3U8Downloader(headers=self.headers, max_workers=3)
        self._used_paths = set()
        os.makedirs(self.output_dir, exist_ok=True)

        for row, media_url, referer, is_m3u8, default_title in self.items:
            if self._stop.is_set():
                break
            self.item_status.emit(row, "下载中…", 0)
            self._cur_row = row
            self.log_msg.emit(f"下载: {media_url}", "info")
            try:
                out_path = self._download_one(media_url, referer, is_m3u8, default_title)
                if out_path:
                    ok += 1
                    self.item_status.emit(row, "完成 ✓", 100)
                    self.log_msg.emit(f"  ✓ 保存: {os.path.basename(out_path)}", "ok")
                else:
                    fail += 1
                    self.item_status.emit(row, "下载失败", -1)
            except Exception as e:
                fail += 1
                self.item_status.emit(row, f"异常", -1)
                self.log_msg.emit(f"  ✗ {e}", "error")

        try:
            self._m3u8.close()
            self._session.close()
        except Exception:
            pass
        self.all_done.emit(ok, fail)

    def _sanitize(self, s):
        return "".join(c if c not in '\\/:*?"<>|' else "_" for c in (s or "")).strip()

    def _pick_ext(self, url):
        m = url.split("?")[0].lower().rsplit(".", 1)
        return m[-1] if len(m) == 2 and len(m[-1]) in (3, 4) else "mp4"

    @staticmethod
    def _with_episode(title):
        """从标题识别集数(第2集/第二集/第3话)，剔除推广文案后并入标题，如 剧名_第2集"""
        title = _clean_title(title)
        if not title:
            return ""
        m = EP_RE.search(title)
        if not m:
            return title
        num = m.group(1)
        if num.isdigit():
            ep = f"第{int(num)}集"
        else:
            ep = f"第{num}集"
        base = EP_RE.sub("", title)
        base = re.sub(r"_{2,}", "_", _strip_promos(base)).strip("_ ")
        if not base:
            return ep
        return f"{base}_{ep}"

    def _base_name(self, media_url, referer, default_title):
        # 源文件名：取 URL 最后一个路径段
        path = media_url.split("?")[0].strip("/").split("/")[-1]
        src = self._sanitize(path.split(".")[0] if "." in path else path) or "video"
        title = self._with_episode(self._sanitize(default_title)) or src
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.naming == "title":
            return title or src
        if self.naming == "src":
            return src
        if self.naming == "src_ts":
            return f"{src}_{ts}"
        return f"{title}_{ts}"

    def _select_free_path(self, out_path):
        # 同批次内避免文件名相互覆盖：已存在该输出名时追加 _2/_3…
        base, ext = os.path.splitext(out_path)
        cand = out_path
        i = 2
        while cand in self._used_paths:
            cand = f"{base}_{i}{ext}"
            i += 1
        self._used_paths.add(cand)
        return cand

    def _download_one(self, media_url, referer, is_m3u8, default_title):
        base = self._base_name(media_url, referer, default_title)
        headers = self.headers.copy()
        if referer:
            headers["Referer"] = referer

        if is_m3u8 or ".m3u8" in media_url.lower().split("?")[0]:
            out_path = self._select_free_path(os.path.join(self.output_dir, base + ".mp4"))
            # M3U8 分片：按 current/total 回传百分比，但加时间窗节流（0.15s 内只发一次），
            # 避免分片很多时高频 emit 刷 UI（P2）
            last_emit = [0.0]
            def m3u8_prog(_message, current, total, _fname):
                if total and current > 0:
                    now = time.time()
                    if now - last_emit[0] >= 0.15 or int(min(1.0, current / total) * 100) >= 100:
                        last_emit[0] = now
                        self._emit_progress(min(1.0, current / total))
            return self._m3u8.download(media_url, out_path, progress_callback=m3u8_prog)

        # 普通媒体直下（mp4/webm 等），携带 Referer 防盗链
        ext = self._pick_ext(media_url)
        out_path = self._select_free_path(os.path.join(self.output_dir, base + "." + ext))
        resp = self._session.get(media_url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0) or 0)
        got = 0
        last_pct = -1
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                if self._stop.is_set():
                    f.close()
                    os.remove(out_path)
                    return None
                if chunk:
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        # 仅在整数百分比变化时才刷新 UI，避免 64KB 逐块高频发信号
                        pct = int(min(1.0, got / total) * 100)
                        if pct != last_pct:
                            last_pct = pct
                            self._emit_progress(got / total)
        return out_path

    def _emit_progress(self, ratio):
        """把当前条目的真实下载进度(0~1)映射到进度列/进度条。M3U8/直下共用。"""
        row = getattr(self, "_cur_row", None)
        if row is None:
            return
        pct = int(max(0.0, min(1.0, ratio)) * 100)
        self.item_status.emit(row, "下载中 %d%%" % pct, pct)


# ---------------- 批量提取挂件：无边框悬浮窗 + 全局快捷键 ----------------
EXT_ENABLE_KEYS = ["F1", "F2", "Ctrl+F5", "Ctrl+F6", "Ctrl+F7", "Ctrl+F8", "Alt+F5", "Alt+F6",
                       "Alt+F7", "F8", "F9", "F10", "F11", "F12"]
EXT_KEY_VK = {"F1": 0x70, "F2": 0x71, "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78,
              "F10": 0x79, "F11": 0x7A, "F12": 0x7B}
MOD_NOREPEAT = 0x4000
MOD_CONTROL = 0x0002
MOD_ALT = 0x0001
WM_HOTKEY = 0x0312


def _ext_key_code(name):
    mods = MOD_NOREPEAT
    if name.startswith("Ctrl+"):
        mods |= MOD_CONTROL
        name = name[5:]
    elif name.startswith("Alt+"):
        mods |= MOD_ALT
        name = name[4:]
    return mods, EXT_KEY_VK[name]


class ExtHotkeyFilter(QAbstractNativeEventFilter):
    """全局热键：wParam 1=开始提取，2=停止提取"""

    def __init__(self, host):
        super().__init__()
        self.host = host

    def nativeEventFilter(self, eventType, message):
        if eventType not in ("windows_generic_MSG", "windows_dispatcher_MSG"):
            return False
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(ctypes.wintypes.MSG)).contents
        except Exception:
            return False
        if msg.message != WM_HOTKEY:
            return False
        if msg.wParam == 1:
            QTimer.singleShot(0, self.host.ext_hotkey_start)
        elif msg.wParam == 2:
            QTimer.singleShot(0, self.host.ext_hotkey_stop)
        return True


class DragTitleBar(QFrame):
    """无边框窗口的标题栏：双击？(无)、拖拽移动、最小化、关闭"""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setFixedHeight(38)
        self.setObjectName("titleBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 4, 8, 4)
        lab = QLabel(text)
        lab.setStyleSheet("font-weight:700; color:#1e293b; background:transparent;")
        lay.addWidget(lab)
        lay.addStretch(1)
        self.hint = QLabel("拖拽移动")
        self.hint.setObjectName("cap")
        self.hint.setStyleSheet("background:transparent; color:#94a3b8; font-size:11px;")
        lay.addWidget(self.hint)
        self.min_btn = QPushButton("—")
        self.min_btn.setObjectName("titleBtn")
        lay.addWidget(self.min_btn)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("titleBtnClose")
        lay.addWidget(self.close_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)


# ---------------- 选集卡片网格（类似爱奇艺/腾讯/红果选"第N集"） ----------------
class EpCard(QFrame):
    """单个‘选集’卡片：左侧编号+类型，底部分隔条为播放音阶或状态；单击选中、双击播放。"""
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._sel = False
        self._playing = False
        self.setObjectName("epCard")
        self.setFixedSize(56, 58)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("sel", "0")
        self.setProperty("playing", "0")
        v = QVBoxLayout(self); v.setContentsMargins(3, 3, 3, 3); v.setSpacing(1)
        self.num = QLabel("1"); self.num.setObjectName("epNum")
        self.num.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        v.addWidget(self.num)
        self.typ = QLabel("MP4"); self.typ.setObjectName("epTyp")
        self.typ.setAlignment(Qt.AlignHCenter)
        v.addWidget(self.typ)
        self.eq = QLabel(""); self.eq.setObjectName("epEq")
        self.eq.setAlignment(Qt.AlignHCenter)
        v.addWidget(self.eq)
        self.status = QLabel("就绪"); self.status.setObjectName("epStatus")
        self.status.setAlignment(Qt.AlignHCenter)
        v.addWidget(self.status, 1)
        # 右上角删除按钮：点击后从列表移除该单集（不触发卡片选中）
        self.del_btn = QToolButton(self)
        self.del_btn.setText("✕")
        self.del_btn.setObjectName("epDel")
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.setToolTip("删除该集")
        self.del_btn.setFixedSize(15, 15)
        self.del_btn.setGeometry(self.width() - 17, 2, 15, 15)
        self.del_btn.raise_()
        self.del_btn.clicked.connect(lambda: self.host.on_delete(self))
        self._apply_style()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            e.accept()
            m = QMenu(self)
            a = m.addAction("📝 识别字幕")
            a.triggered.connect(lambda: self.host.on_context(self))
            a2 = m.addAction("🎞 视频分析")
            a2.triggered.connect(lambda: self.host.on_vanalyze(self))
            m.exec_(e.globalPosition().toPoint())
            return
        if e.button() == Qt.LeftButton:
            e.accept(); self.host.on_click(self); return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept(); self.host.on_double(self); return
        super().mouseDoubleClickEvent(e)

    def set_selected(self, on):
        self._sel = on; self._apply_style()

    def is_selected(self):
        return self._sel

    def set_playing(self, on):
        self._playing = on; self._apply_style()
        if not on:
            self.eq.setText("")

    def set_status(self, text):
        self.status.setText(text)

    def set_equalizer(self, bars):
        if self._playing:
            self.eq.setText(bars)

    def _apply_style(self):
        if self._playing:
            bg, bd, tn, te, ts = "#ffffff", "#8b5cf6", "#334155", "#ffffff", "#334155"
        elif self._sel:
            bg, bd, tn, te, ts = "#2563eb", "#2563eb", "#ffffff", "#cfe0ff", "#daebff"
        else:
            bg, bd, tn, te, ts = "#ffffff", "#e2e8f0", "#334155", "#059669", "#64748b"
        self.setStyleSheet(
            f"EpCard {{ background:{bg}; border:1px solid {bd}; border-radius:8px; }}"
            f"EpCard QLabel {{ background:transparent; }}"
            f"EpCard QLabel#epNum {{ font-size:16px; font-weight:700; color:{tn}; }}"
            f"EpCard QLabel#epTyp {{ font-size:8px; color:{te}; "
            f"background:rgba(5,150,105,0.10); padding:0px 3px; border-radius:6px; font-weight:600; }}"
            f"EpCard QLabel#epEq {{ font-size:9px; color:#8b5cf6; letter-spacing:0px; }}"
            f"EpCard QLabel#epStatus {{ font-size:8px; color:{ts}; }}"
            f"EpCard QToolButton#epDel {{ background:transparent; border:none; "
            f"color:#cbd5e1; font-size:10px; font-weight:700; padding:0px; border-radius:7px; }}"
            f"EpCard QToolButton#epDel:hover {{ background:#fee2e2; color:#dc2626; }}")

    def set_color(self, *a):
        pass


class EpisodeGrid(QWidget):
    """选集网格容器：多列自动换行，超出限制则滚动；提供选中集合与计数。

    响应式：列数随可用宽度自动增减（宽窗口多列、窄窗口少列），高度由父布局分配，
    窗口变高时能多显示几行，不再写死高度。
    """
    COLS = 9          # 列数上限
    CARD_W = 56       # 卡片宽（EpCard 固定 56x58）
    CARD_H = 58
    GAP = 4           # 网格间距

    def __init__(self, on_change=None, on_play=None, on_ocr=None, on_vanalyze=None, on_delete=None):
        super().__init__()
        self.on_change = on_change or (lambda: None)
        self.on_play = on_play or (lambda i: None)
        self.on_ocr = on_ocr or (lambda i: None)
        self._on_vanalyze = on_vanalyze or (lambda i: None)
        self._on_delete = on_delete or (lambda i: None)
        self._cols = self.COLS
        self._data = []
        self._cards = []
        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(4); self._grid.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._container); self._scroll.setObjectName("epScroll")
        self._scroll.setFrameShape(QFrame.NoFrame)
        L = QVBoxLayout(self); L.setContentsMargins(0, 0, 0, 0); L.addWidget(self._scroll)

    def count(self):
        return len(self._data)

    def add(self, url, referer="", is_m3u8=False, title="", is_local=False):
        i = len(self._data)
        self._data.append({"url": url, "referer": referer, "is_m3u8": is_m3u8, "title": title, "is_local": is_local})
        c = EpCard(self)
        c.num.setText(str(i + 1))
        c.typ.setText("本地" if is_local else ("HLS" if is_m3u8 else "MP4"))
        src = referer.replace("https://", "").replace("http://", "").split("/")[0] if referer else "本地文件"
        tip_lines = [title or "视频", url, f"来源: {src}", ""]
        tip_lines.append("单击=选中 · 双击=预览播放 · 右键=识别/分析")
        c.setToolTip("\n".join(tip_lines))
        self._cards.append(c)
        self._grid.addWidget(c, i // self._cols, i % self._cols)
        self._update_height()
        return i

    def update_title(self, i, title):
        if 0 <= i < len(self._data):
            self._data[i]["title"] = title
            d = self._data[i]
            src = d.get("referer", "").replace("https://", "").replace("http://", "").split("/")[0]
            self._cards[i].setToolTip(
                f"{title or '视频'}\n{d.get('url', '')}\n来源: {src or '页面'}\n\n单击=选中下载 · 双击=预览播放")

    def on_click(self, card):
        card.set_selected(not card.is_selected())
        self.on_change()

    def on_context(self, card):
        self.on_ocr(self._cards.index(card))

    def on_double(self, card):
        self.on_play(self._cards.index(card))

    def on_delete(self, card):
        """删除单个剧集卡片（点卡片右上角 ✕）：从列表移除并重排，通知宿主持久化。"""
        if card not in self._cards:
            return
        i = self._cards.index(card)
        self._data.pop(i)
        self._cards.pop(i)
        self._grid.removeWidget(card)
        card.setParent(None); card.deleteLater()
        for c in self._cards:   # 先全部移除再重排，避免删除中间项后错位
            self._grid.removeWidget(c)
        for j, c in enumerate(self._cards):
            c.num.setText(str(j + 1))
            self._grid.addWidget(c, j // self._cols, j % self._cols)
        self.show_all()
        self.on_change()
        self._on_delete(i)

    def select_all_toggle(self):
        if not self._cards:
            return
        allsel = all(c.is_selected() for c in self._cards)
        for c in self._cards:
            c.set_selected(not allsel)
        self.on_change()

    def selected(self):
        return [i for i, c in enumerate(self._cards) if c.is_selected()]

    def set_playing(self, i):
        for j, c in enumerate(self._cards):
            c.set_playing(j == i)

    def set_equalizer_text(self, i, bars):
        self._cards[i].set_equalizer(bars)

    def set_status(self, i, text):
        self._cards[i].set_status(text)

    def show_all(self):
        """重排并刷新高度：列数按可用宽度算，高度只给一个保底值（其余交给布局伸缩，
        所以窗口变大时能多显示几行，窗口变小时也不会把窗口撑住）。"""
        self._relayout(force=True)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self, force=False):
        """按可用宽度计算列数并重排卡片。"""
        if not self._cards:
            self._update_height()
            return
        try:
            avail = self._scroll.viewport().width() - 2
        except Exception:
            avail = self.width() - 2
        step = self.CARD_W + self.GAP
        cols = max(2, min(self.COLS, (avail + self.GAP) // step)) if avail > 0 else self.COLS
        if not force and cols == self._cols:
            self._update_height()
            return
        self._cols = cols
        for c in self._cards:
            self._grid.removeWidget(c)
        for j, c in enumerate(self._cards):
            self._grid.addWidget(c, j // self._cols, j % self._cols)
        self._update_height()

    def _update_height(self):
        import math
        rows = max(1, math.ceil(len(self._cards) / self._cols)) if self._cards else 1
        cap = 3  # 保底显示 3 行；更高时由父布局分配，内部滚动查看其余集数
        h = min(rows, cap) * (self.CARD_H + self.GAP) + 4
        if self.minimumHeight() != h:
            self.setMinimumHeight(h)

    def remove_urls(self, urls):
        """按媒体地址删除一个或多个卡片（用于把同集较短/预览版整合掉），自动重排编号。"""
        drop = set(urls or [])
        if not drop:
            return False
        keep = []
        hit = False
        for c, d in zip(self._cards, self._data):
            if d.get("url") in drop:
                self._grid.removeWidget(c); c.setParent(None); c.deleteLater()
                hit = True
            else:
                keep.append((c, d))
        if not hit:
            return False
        self._data = [d for _, d in keep]
        self._cards = [c for _, c in keep]
        for c in self._cards:
            self._grid.removeWidget(c)
        for j, c in enumerate(self._cards):
            c.num.setText(str(j + 1))
            self._grid.addWidget(c, j // self._cols, j % self._cols)
        self.show_all()
        self.on_change()
        return True

    def info_at(self, i):
        return self._data[i]

    def data_snapshot(self):
        """返回可序列化的剧集数据（用于历史持久化）"""
        return [dict(x) for x in self._data]

    def load_from(self, items):
        """清空后按历史数据重建卡片（供重启后恢复已识别链接）"""
        for c in self._cards:
            self._grid.removeWidget(c)
            c.deleteLater()
        self._cards = []
        self._data = []
        for it in items or []:
            self.add(it.get("url", ""), it.get("referer", ""),
                     bool(it.get("is_m3u8")), it.get("title", ""),
                     bool(it.get("is_local", False)))


class LocalFileCard(QFrame):
    """本地视频小卡片（生成页侧栏）：序号+类型+文件名；单击选中、双击播放、右键菜单。"""
    def __init__(self, host):
        super().__init__()
        self.host = host
        self._sel = False
        self.setObjectName("lfc")
        self.setFixedSize(96, 62)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("sel", "0")
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 3, 4, 3)
        v.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(3)
        self.num = QLabel("1")
        self.num.setObjectName("lfNum")
        self.num.setAlignment(Qt.AlignCenter)
        top.addWidget(self.num, 0)
        self.typ = QLabel("本地")
        self.typ.setObjectName("lfTyp")
        self.typ.setAlignment(Qt.AlignCenter)
        top.addWidget(self.typ, 0)
        top.addStretch(1)
        v.addLayout(top)
        self.name = QLabel("")
        self.name.setObjectName("lfName")
        self.name.setAlignment(Qt.AlignCenter)
        self.name.setWordWrap(False)
        v.addWidget(self.name, 1)
        # 让卡片内所有文本标签的鼠标事件穿透到卡片本身，
        # 确保双击文件名/序号/类型都能触发播放（否则被 QLabel 吞掉）。
        for lbl in (self.num, self.typ, self.name):
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._apply_style()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            e.accept()
            m = QMenu(self)
            a = m.addAction("▶ 播放")
            a.triggered.connect(lambda: self.host.on_play_file(self))
            b = m.addAction("✏️ 重命名")
            b.triggered.connect(lambda: self.host.on_rename_file(self))
            c = m.addAction("🗑 删除")
            c.triggered.connect(lambda: self.host.on_delete_file(self))
            d = m.addAction("📂 打开目录")
            d.triggered.connect(lambda: self.host.on_open_dir(self))
            m.exec_(e.globalPosition().toPoint())
            return
        if e.button() == Qt.LeftButton:
            e.accept()
            self.host.on_click_file(self)
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            e.accept()
            self.host.on_dbl_file(self)
            return
        super().mouseDoubleClickEvent(e)

    def set_selected(self, on):
        self._sel = on
        self._apply_style()

    def set_name(self, text):
        self.name.setText(text)

    def _apply_style(self):
        if self._sel:
            bg, bd, tn, te, ts = "#2563eb", "#2563eb", "#ffffff", "#ffffff", "#daebff"
        else:
            bg, bd, tn, te, ts = "#ffffff", "#e2e8f0", "#334155", "#059669", "#475569"
        self.setStyleSheet(
            f"LocalFileCard {{ background:{bg}; border:1px solid {bd}; border-radius:8px; }}"
            f"LocalFileCard QLabel {{ background:transparent; }}"
            f"LocalFileCard QLabel#lfNum {{ font-size:12px; font-weight:700; color:{tn}; }}"
            f"LocalFileCard QLabel#lfTyp {{ font-size:8px; color:{te}; "
            f"background:rgba(5,150,105,0.10); padding:0px 3px; border-radius:6px; font-weight:600; }}"
            f"LocalFileCard QLabel#lfName {{ font-size:9px; color:{ts}; }}")


class _CardDelegate(QStyledItemDelegate):
    """项目列表自绘卡片：圆角卡片背景 + 图标 + 名称/时间。仍基于 QListWidget，
    命中测试由 Qt 保证，既保留可靠点击又具备卡片观感。"""
    def paint(self, painter, option, index):
        from PySide6.QtGui import QPainterPath
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(3, 3, -3, -3)
        st = option.state
        # 使用白色背景，参考剧集卡片样式
        if st & QStyle.State_Selected:
            bg = QColor("#eff6ff")
            border = QColor("#2563eb")
        elif st & QStyle.State_MouseOver:
            bg = QColor("#f8fafc")
            border = QColor("#2563eb")
        else:
            bg = QColor("#ffffff")
            border = QColor("#e2e8f0")
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        painter.fillPath(path, bg)
        # 边框
        painter.setPen(QPen(border, 1 if not (st & QStyle.State_Selected) else 2))
        painter.drawPath(path)
        # 图标
        icon = index.data(Qt.DecorationRole)
        if icon:
            isz = 34
            ir = QRect(rect.left() + 14, rect.top() + (rect.height() - isz) // 2, isz, isz)
            icon.paint(painter, ir, Qt.AlignCenter)
        # 文本：第一行名称，第二行时间
        text = str(index.data(Qt.DisplayRole))
        lines = text.split("\n")
        name = lines[0] if lines else ""
        sub = lines[1] if len(lines) > 1 else ""
        tf = option.font
        tf.setPixelSize(15)
        tf.setBold(True)
        painter.setFont(tf)
        painter.setPen(QColor("#1e293b"))
        painter.drawText(rect.adjusted(60, 8, -10, -22),
                         int(Qt.AlignLeft | Qt.AlignVCenter) | int(Qt.TextWordWrap), name)
        sf = option.font
        sf.setPixelSize(12)
        sf.setBold(False)
        painter.setFont(sf)
        painter.setPen(QColor("#64748b"))
        painter.drawText(rect.adjusted(60, -20, -10, -6),
                         int(Qt.AlignLeft | Qt.AlignBottom), sub)
        painter.restore()

    def sizeHint(self, option, index):
        # 跟随所在列表的 gridSize：窗口变宽时卡片跟着变宽，而不是写死 196
        try:
            p = self.parent()
            if p is not None and hasattr(p, "gridSize"):
                g = p.gridSize()
                return QSize(max(150, g.width() - 6), max(90, g.height() - 6))
        except Exception:
            pass
        return QSize(196, 104)


class ProjectCard(QPushButton):
    """首页项目卡片（基于 QPushButton，clicked 信号稳定可靠）。
    点击打开；右上角 ✕ 删除；右键：打开/重命名/删除。"""
    openProject = Signal(str)
    deleted = Signal(str)
    renamed = Signal(str)

    def __init__(self, name, created, parent=None):
        super().__init__(parent)
        self._name = name
        self.setFixedSize(220, 130)
        self.setCursor(Qt.PointingHandCursor)
        self.setText("")
        self.setStyleSheet(
            "QPushButton { background:#ffffff; border:1px solid #e2e8f0; border-radius:14px; text-align:left; }"
            "QPushButton:hover { border:2px solid #2563eb; background:#f8fafc; }"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        ic = QLabel("📁")
        ic.setStyleSheet("font-size:20px; background:transparent;")
        top.addWidget(ic)
        self.nm = QLabel(name)
        self.nm.setWordWrap(True)
        self.nm.setStyleSheet("font-size:15px; font-weight:800; color:#1e293b; background:transparent;")
        top.addWidget(self.nm, 1)
        self.delb = QPushButton("✕")
        self.delb.setFixedSize(22, 22)
        self.delb.setStyleSheet(
            "QPushButton{border:none; border-radius:11px; color:#94a3b8; font-weight:900; background:transparent;}"
            "QPushButton:hover{background:#fee2e2; color:#dc2626;}")
        self.delb.setCursor(Qt.PointingHandCursor)
        self.delb.clicked.connect(lambda: self.deleted.emit(self._name))
        top.addWidget(self.delb)
        v.addLayout(top)
        v.addStretch(1)
        self.ctime = QLabel(created)
        self.ctime.setObjectName("cap")
        self.ctime.setStyleSheet("background:transparent;")
        v.addWidget(self.ctime)
        # 内部文本标签透明穿鼠标事件：点击文字区域同样落到父按钮触发打开
        for _c in (ic, self.nm, self.ctime):
            _c.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.clicked.connect(lambda: self.openProject.emit(self._name))

    def set_data(self, name, created):
        """更新卡片显示（不重建对象，信号连接保持不变）。"""
        self._name = name
        self.nm.setText(name)
        self.ctime.setText(created)

    def contextMenuEvent(self, ev):
        m = QMenu(self)
        m.addAction("📂 打开").triggered.connect(lambda: self.openProject.emit(self._name))
        m.addSeparator()
        m.addAction("✏️ 重命名").triggered.connect(lambda: self.renamed.emit(self._name))
        m.addAction("🗑 删除").triggered.connect(lambda: self.deleted.emit(self._name))
        m.exec_(ev.globalPos())


class EpGenCard(QFrame):
    """视频生成页剧集卡片：点击打开对应生成区，右上角 ✕ / 右键删除。

    用 QFrame 承载而非 QPushButton，避免“按钮内嵌按钮”导致删除按钮点击失效。"""
    opened = Signal(str)
    deleted = Signal(str)
    renamed = Signal(str)
    moved = Signal(str, str)

    def __init__(self, name, episode_count=0, parent=None):
        super().__init__(parent)
        self._name = name
        self._selected = False
        self._was_double_clicked = False
        self._press_pos = None
        self._dragging = False
        self.setAcceptDrops(True)
        self.setFixedSize(220, 130)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("epGenCard")
        self.setStyleSheet(self._card_css())
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        ic = QLabel("🎬")
        ic.setStyleSheet("font-size:20px; background:transparent;")
        top.addWidget(ic)
        self.nm = QLabel(name)
        self.nm.setWordWrap(True)
        self.nm.setStyleSheet("font-size:15px; font-weight:800; color:#1e293b; background:transparent;")
        top.addWidget(self.nm, 1)
        self.delb = QPushButton("✕")
        self.delb.setFixedSize(22, 22)
        self.delb.setStyleSheet(
            "QPushButton{border:none; border-radius:11px; color:#94a3b8; font-weight:900; background:transparent;}"
            "QPushButton:hover{background:#fee2e2; color:#dc2626;}")
        self.delb.setCursor(Qt.PointingHandCursor)
        self.delb.setFocusPolicy(Qt.NoFocus)
        self.delb.clicked.connect(lambda: self.deleted.emit(self._name))
        top.addWidget(self.delb)
        v.addLayout(top)
        v.addStretch(1)
        self.esc = QLabel("%d 个生成区" % episode_count)
        self.esc.setObjectName("cap")
        self.esc.setStyleSheet("background:transparent; color:#64748b; font-size:12px;")
        v.addWidget(self.esc)
        for _c in (ic, self.nm, self.esc):
            _c.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 单击：切换选中 + 延迟打开；双击：直接打开
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(300)
        self._click_timer.timeout.connect(self._fire_open)

    def _card_css(self):
        if self._selected:
            return ("QFrame#epGenCard { background:#eff6ff; border:2px solid #2563eb; border-radius:14px; }"
                    "QFrame#epGenCard:hover { border:2px solid #1d4ed8; background:#dbeafe; }")
        return ("QFrame#epGenCard { background:#ffffff; border:1px solid #e2e8f0; border-radius:14px; }"
                "QFrame#epGenCard:hover { border:2px solid #2563eb; background:#f8fafc; }")

    def _check_double_click(self):
        """检测是否为双击：先延迟，若已标记为双击则触发打开"""
        if getattr(self, "_was_double_clicked", False):
            self._was_double_clicked = False
            return
        self._click_timer.start()

    def _fire_open(self):
        """单点击发后打开剧集"""
        if not getattr(self, "_was_double_clicked", False):
            self.opened.emit(self._name)

    def mouseDoubleClickEvent(self, ev):
        """双击标记"""
        if ev.button() == Qt.LeftButton:
            self._was_double_clicked = True
            self._click_timer.stop()
            self.opened.emit(self._name)
            return
        super().mouseDoubleClickEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press_pos = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        # 按住左键拖出一定距离 → 启动拖放，方便调整剧集顺序
        if (ev.buttons() & Qt.LeftButton) and self._press_pos is not None:
            p = ev.position().toPoint() if hasattr(ev, "position") else ev.pos()
            if (p - self._press_pos).manhattanLength() > QApplication.startDragDistance():
                self._dragging = True
                drag = QDrag(self)
                mime = QMimeData()
                mime.setText(self._name)
                drag.setMimeData(mime)
                drag.setPixmap(self.grab())
                drag.exec(Qt.MoveAction)
                self._dragging = False
                return
        super().mouseMoveEvent(ev)

    def dragEnterEvent(self, ev):
        src = ev.source()
        if isinstance(src, EpGenCard) and src is not self:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        src = ev.source()
        if isinstance(src, EpGenCard) and src is not self:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        src = ev.source()
        if isinstance(src, EpGenCard) and src is not self:
            self.moved.emit(src._name, self._name)
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def mouseReleaseEvent(self, ev):
        if self._dragging:
            self._dragging = False
            self._press_pos = None
            return
        if ev.button() == Qt.LeftButton and self._press_pos is not None:
            self._press_pos = None
            self._toggle_select()
            self._check_double_click()
            return
        super().mouseReleaseEvent(ev)

    def _toggle_select(self):
        """点击卡片切换选中状态"""
        self.set_selected(not self._selected)

    def set_selected(self, on):
        """设置选中状态"""
        self._selected = on
        self.setStyleSheet(self._card_css())

    def is_selected(self):
        return self._selected

    def contextMenuEvent(self, ev):
        m = QMenu(self)
        m.addAction("📝 重命名").triggered.connect(lambda: self._do_rename())
        m.addAction("🗑 删除").triggered.connect(lambda: self.deleted.emit(self._name))
        m.exec_(ev.globalPos())

    def _do_rename(self):
        name, ok = QInputDialog.getText(self, "重命名剧集", "新名称：", text=self._name)
        name = (name or "").strip()
        if not ok or not name or name == self._name:
            return
        self.renamed.emit(name)


# ---------------- 资产管理 · AI 一键提取（人物/场景/道具） ----------------

_ASSET_CHAR_PROMPT_ZH = """你是一位专业的角色分析师，擅长从剧本中提取和分析角色信息。

【语言要求】所有字段的值必须使用中文，禁止出现英文内容（role 字段除外，固定为 main/supporting/minor）。

【名称铁律】name 必须严格逐字照抄剧本原文中出现的角色名字——原文写什么就是什么：
- 禁止添加称谓、头衔、职业、修饰语（如原文写"赵总"，不得写成"赵总经理"或"赵廷岳总经理"）
- 禁止缩写、改名、翻译、润色、增删任何字
- 同一角色原文有多种称呼时，取其中完整的名字，但必须是原文出现的原词

你的任务：根据下面提供的剧本/分镜文本，提取剧中出现的所有有名字的角色（忽略无名路人与背景角色）。对每个角色输出：
- name：角色名字（严格照抄原文，见【名称铁律】）
- role：角色类型，固定值之一 main / supporting / minor
- appearance：外貌描述（中文，100-200字，包含性别、年龄、体型、面部特征、发型、服装风格等，不含任何场景或环境信息）
- description：背景故事和角色关系（中文，50-100字）

主要角色外貌要详细，次要角色可以简化。
只输出一个 JSON 数组，不要输出解释文字、前后缀或代码块标记。格式如下：
[{"name":"角色名","role":"main","appearance":"外貌描述","description":"背景描述"}]

【剧本/分镜内容】
{剧本}
"""

_ASSET_SCENE_PROMPT_ZH = """你是一位专业的场景分析师，擅长从剧本中识别场景，并为图片生成编写纯背景提示词。

【语言要求】name 与 prompt 字段必须为中文；风格词如 realistic 可保留。

【名称铁律】name 必须严格逐字照抄剧本原文中出现的地点/场景表述——原文写什么就是什么：
- 禁止自行拼接时间、天气、光线等（原文没写"·日"就不加"·日"，不得加"白天""夜晚"等）
- 禁止改写、润色、增删任何字
- 同一场景原文有多种表述时，取其中完整的表述，但必须是原文出现的原词

你的任务：从下面提供的剧本/分镜文本中提取所有不同的场景。为每个场景输出：
- name：场景名称（严格照抄原文，见【名称铁律】）
- prompt：详细的中文图片生成提示词

要求：
1. 场景描述必须是纯背景，不能包含人物、角色、动作等元素
2. prompt 只描述空间结构、建筑/陈设、光线、氛围、色调、天气、景深等背景信息
3. prompt 面向“资产图”使用：是一张无人场景参考照，不要出现任何人形描述
只输出一个 JSON 数组，不要输出解释文字、前后缀或代码块标记。格式：
[{"name":"场景名","prompt":"详细提示词"}]

【剧本/分镜内容】
{剧本}
"""

_ASSET_PROP_PROMPT_ZH = """你是一位专业的剧本道具分析师，擅长从剧本中提取具有视觉特征的关键道具。

【语言要求】name、description、image_prompt 均使用中文（风格词如 realistic、studio 可保留）。

【名称铁律】name 必须严格逐字照抄剧本原文中出现的道具名称——原文写什么就是什么：
- 禁止改写、缩写、加修饰语或形容词（如原文写"红木太师椅"，不得写成"太师椅"或"旧红木太师椅"）
- 禁止增删任何字

你的任务：从下面提供的剧本/分镜文本中，提取所有对剧情有重要作用或有特殊视觉特征的关键道具。对每个道具输出：
- name：道具名称（严格照抄原文，见【名称铁律】）
- description：归属者、作用或来源（中文），只能写在此字段
- image_prompt：面向资产生成的图片提示词，用中文按「产品主图/资产白模照」标准撰写

image_prompt 规则：
1. 只描述道具本体（造型、材质、颜色、工艺、磨损/做旧、尺寸感）
2. 强制纯色无缝摄影棚背景，画面干净，无场景、无杂物
3. 明确排除人物、手、家具、台面、其他物体与环境叙事元素
4. 禁止出现剧本人名、地名、组织名、台词与剧情专有词，用泛化视觉词替代
5. 禁止无依据扩写（不凭空添加配饰、品牌叙事、煽情形容词）

只输出一个 JSON 数组，不要输出解释文字、前后缀或代码块标记。格式：
[{"name":"道具名","description":"归属与作用","image_prompt":"白模照提示词"}]

【剧本/分镜内容】
{剧本}
"""

_ASSET_AI_MAX_CHARS = 40000   # 单次提交给 AI 的剧本文本上限（字符），防止超长截断关键人物/场景/道具




class EpGenCardAdd(QFrame):
    """新建剧集卡片：显示 + 号，点击触发新建"""
    added = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(220, 130)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("epGenCardAdd")
        self.setStyleSheet("""
            QFrame#epGenCardAdd {
                background:#f8fafc;
                border:2px dashed #cbd5e1;
                border-radius:14px;
            }
            QFrame#epGenCardAdd:hover {
                border:2px solid #2563eb;
                background:#eff6ff;
            }
        """)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addStretch(1)
        ic = QLabel("+")
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet("font-size:32px; font-weight:300; color:#94a3b8; background:transparent;")
        v.addWidget(ic)
        v.addStretch(1)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.added.emit()
        super().mousePressEvent(event)

class AssetEpCard(QFrame):
    """资产管理 · 分集卡片：点击进入该分集的资产编辑。"""
    opened = Signal(str)

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self._name = name
        self.setFixedSize(220, 110)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("assetEpCard")
        self.setStyleSheet(
            "QFrame#assetEpCard { background:#ffffff; border:1px solid #e2e8f0; border-radius:14px; }"
            "QFrame#assetEpCard:hover { border:2px solid #2563eb; background:#eff6ff; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 12)
        v.setSpacing(8)
        top = QHBoxLayout()
        ic = QLabel("🎨")
        ic.setStyleSheet("font-size:20px; background:transparent;")
        top.addWidget(ic)
        self.nm = QLabel(name)
        self.nm.setWordWrap(True)
        self.nm.setStyleSheet("font-size:15px; font-weight:800; color:#1e293b; background:transparent;")
        top.addWidget(self.nm, 1)
        v.addLayout(top)
        v.addStretch(1)
        self.esc = QLabel("查看/提取该分集资产")
        self.esc.setObjectName("cap")
        self.esc.setStyleSheet("background:transparent; color:#64748b; font-size:12px;")
        v.addWidget(self.esc)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.opened.emit(self._name)
        super().mouseReleaseEvent(event)

class AssetAiWorker(QThread):
    """AI 一键提取资产：串行调用 GLM 文本接口，分别提取人物/场景/道具三类 JSON 数组。"""
    progress = Signal(str)
    done = Signal(dict)     # {"characters":[...],"scenes":[...],"props":[...]}
    failed = Signal(str)

    def __init__(self, api_key, base_url, model, text, parent=None):
        super().__init__(parent)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.text = (text or "")[:_ASSET_AI_MAX_CHARS]

    @staticmethod
    def _extract_json_array(raw):
        """从模型输出中稳健解析 JSON 数组。
        兼容 (a) 代码块/前后缀 (b) 弯引号“ ” ‘ ’ (c) 字符串值内部未转义双引号 (d) 全角标点。
        逐对象逐字符修复后再解析，尽量不依赖 LLM 配合。"""
        s = (raw or "").strip()
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()

        def try_parse(t):
            try:
                v = json.loads(t)
                if isinstance(v, list):
                    return v
            except Exception:
                pass
            return None

        # 直接试一次（已经规范时）
        r = try_parse(s)
        if r is not None:
            return r

        # 全角标点归一化（句号/分号/冒号/逗号）
        s2 = (s.replace("\u3002", ".").replace("\uff1b", ";")
               .replace("\uff1a", ":").replace("\uff0c", ","))
        r = try_parse(s2)
        if r is not None:
            return r

        # 弯引号归一化：把“ ” 都替换成 "（JSON 双引号），‘ ’ 替换成 '
        s3 = (s2.replace("\u201c", '"').replace("\u201d", '"')
                  .replace("\u2018", "'").replace("\u2019", "'"))
        r = try_parse(s3)
        if r is not None:
            return r

        # 定位 JSON 数组范围 [ ... ]
        a, b = s3.find("["), s3.rfind("]")
        if a < 0 or b <= a:
            # 可能是 {"name":...} 单个对象缺外层 []，或整体缺失
            r = try_parse(s3)
            if r is not None:
                return [r]
            raise RuntimeError("AI 未返回 JSON 数组：%s" % s[:500])

        seg = s3[a:b + 1]
        r = try_parse(seg)
        if r is not None:
            return r

        # 逐对象修复：从 [ 后到 ] 前扫描，按 { } 配对切块
        inner = seg[1:-1]
        objs = []
        depth = 0
        cur = ""
        for ch in inner:
            if ch == '{':
                depth += 1
                cur += ch
                continue
            elif ch == '}':
                depth -= 1
                cur += ch
                if depth == 0:
                    objs.append(cur)
                    cur = ""
                continue
            else:
                if depth >= 1:
                    cur += ch

        repaired = [_repair_json_object(ob) for ob in objs]
        cand = "[" + ",".join(repaired) + "]"
        r = try_parse(cand)
        if r is not None:
            return r
        raise RuntimeError("AI 返回的不是合法 JSON 数组：%s" % s[:500])

    def run(self):
        try:
            if not self.api_key:
                self.failed.emit("未配置文本分析 API Key，请在「⚙ AI 服务 → 视频分析」中配置")
                return
            prompts = [
                ("characters", _ASSET_CHAR_PROMPT_ZH.replace("{剧本}", self.text)),
                ("scenes", _ASSET_SCENE_PROMPT_ZH.replace("{剧本}", self.text)),
                ("props", _ASSET_PROP_PROMPT_ZH.replace("{剧本}", self.text)),
            ]
            out = {"characters": [], "scenes": [], "props": []}
            labels = {"characters": "人物", "scenes": "场景", "props": "道具"}
            for key, prompt in prompts:
                self.progress.emit("AI 提取%s中…" % labels[key])
                raw = call_glm(self.api_key, prompt, [],
                               base_url=self.base_url, model=self.model,
                               timeout=300, max_tokens=8192)
                arr = self._extract_json_array(raw)
                out[key] = [it for it in arr if isinstance(it, dict)]
            self.progress.emit("AI 提取完成")
            self.done.emit(out)
        except Exception as e:
            self.failed.emit(str(e))


def _repair_json_object(ob):
    """把 { ... } 块修复成合法 JSON 对象字符串。
    逐键值对解析：值字符串贪婪匹配到本对象最后一个未转义双引号，
    把内容里所有未转义的 " 转义成 \\"。这是"逐对象贪婪到末尾"策略，
    能正确处理模型在字符串值内部塞进未转义双引号的常见情况。"""
    out = ['{']
    body = ob[1:-1]  # 去掉外层 { }
    pairs = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch in ' \n\t,':
            i += 1
            continue
        # 期望是 "key"
        if ch != '"':
            i += 1
            continue
        # 读 key 字符串：找下一个未转义 "
        j = i + 1
        while j < n:
            if body[j] == '\\':
                j += 2
                continue
            if body[j] == '"':
                break
            j += 1
        key = body[i+1:j]
        i = j + 1
        # 跳过 : 和空白
        while i < n and body[i] in ' \n\t':
            i += 1
        if i < n and body[i] == ':':
            i += 1
        while i < n and body[i] in ' \n\t':
            i += 1
        # 值字符串：贪婪匹配到整个 body 的最后一个 "
        last_quote = body.rfind('"')
        if last_quote < i:
            last_quote = i
        val = body[i:last_quote]
        # 把 val 里所有未转义的 " 转义成 \"
        val_fixed = []
        k = 0
        while k < len(val):
            c = val[k]
            if c == '\\' and k + 1 < len(val):
                val_fixed.append(c + val[k+1])
                k += 2
                continue
            if c == '"':
                val_fixed.append('\\"')
            else:
                val_fixed.append(c)
            k += 1
        pairs.append('"%s":"%s"' % (key, "".join(val_fixed)))
        i = last_quote + 1
    out.append(",".join(pairs))
    out.append('}')
    return "".join(out)


class AssetImageRunnable(QRunnable):
    """资产生成图可运行任务：调用 /images/generations 不阻塞界面。"""
    def __init__(self, api_key, base_url, model, prompt, size, out_dir, name_hint):
        super().__init__()
        self.setAutoDelete(True)
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._prompt = prompt
        self._size = size
        self._out_dir = out_dir
        self._name_hint = name_hint
        self.name = name_hint
        self.error = None
        self.image_path = None
        self.success = False

    def run(self):
        try:
            from gen_area import create_image_task
            result = create_image_task(
                prompt=self._prompt,
                api_key=self._api_key,
                base_url=self._base_url,
                model=self._model,
                size=self._size,
                out_dir=self._out_dir,
                name_hint=self._name_hint,
            )
            if result and result.get("success"):
                self.success = True
                self.image_path = result["image_path"]
            else:
                self.error = (result or {}).get("error", "未知错误")
        except Exception as e:
            self.error = str(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"幻镜AI v{config.APP_VERSION}")
        # 窗口尺寸可自由调节：默认按屏幕可用区取一个舒适比例（不全屏、不最大化），
        # 上次的尺寸/位置/分割条比例会记到 QSettings，下次启动自动恢复。
        self._settings = QSettings("HongguoSniffer", "VideoSniffer")
        self.setMinimumSize(WIN_MIN_W, WIN_MIN_H)
        self._geo_ready = False        # 布局完成前不做自适应计算
        self._rz_timer = QTimer(self)
        self._rz_timer.setSingleShot(True)
        self._rz_timer.setInterval(120)
        self._rz_timer.timeout.connect(self._apply_responsive)
        self.media_repo = {}      # media_url -> {referer, is_m3u8, title}
        self._page_ep = ""        # 当前页面描述里识别到的集数，如 “第1集”
        self._page_desc = ""      # 当前页面描述文本（用于命名）
        # ---- 字幕识别状态 ----
        self._ocr_lang = "zh"
        self._ocr_fps = 10
        self._ocr_crop_bottom = True  # 仅识别底部字幕区（更精准）
        self._ocr_scale = 1.5         # 字幕区放大倍数（提升小字/模糊识别率）
        self._ocr_min_ms = 100
        self._ocr_worker = None
        self._ocr_ep = None
        self._ocr_wtitle = ""
        self._last_subs = []
        self._ocr_batch = False
        self._ocr_queue = []      # 批量模式下待识别剧集索引
        self._ocr_total = 0       # 批量识别总集数
        self._ocr_header = ""     # 当前集的结果块标题
        self._ocr_prog = ""       # 当前集进度标签（批量用）
        config.load_glm_config()
        self._va_worker = None    # 视频分析工作线程
        self._va_ep = None
        self._va_wtitle = ""
        self._va_mode = "segments"   # video analysis 模式：single / batch / segments
        self._va_frames = 6
        self._va_batch_mode = False  # 批量多剧集分析是否在进行
        self._va_queue = []          # 批量待分析剧集索引队列
        self._va_ep_total = 0        # 批量总集数
        self._va_accum = ""          # 批量累加的分析结果文本
        self._results_dir = os.path.join(config._EXE_DIR, "视频分析结果")  # 每集分析结果落盘目录
        self._scripts_dir = os.path.join(config._EXE_DIR, "剧本")   # 转剧本结果落盘目录
        self._gen_videos_dir = os.path.join(config._EXE_DIR, "生成视频")  # 生成视频自动保存目录
        self._va_local_batch = False  # 本地视频批量分析中
        self._va_local_queue = []     # 本地视频批量队列
        self._va_local_id = 0         # 本地批量序号
        # ---- 项目化：首页 + 项目卡片（完全隔离各自的数据） ----
        self._projects_dir = os.path.join(config._EXE_DIR, "项目合集")
        self._projects_file = os.path.join(config._EXE_DIR, "projects.json")
        self._current_project = None      # 当前激活的项目名（None=未打开）
        self._project_cards = []          # 首页已创建的卡片 widget
        os.makedirs(self._projects_dir, exist_ok=True)
        self.interceptor = MediaInterceptor()
        self.interceptor.media_found.connect(self._on_media_url)
        self.worker = None
        self._batch_urls = []
        self._batch_idx = -1
        self._load_sign = None
        self.setStyleSheet(APP_QSS)
        self._build_ui()
        self._init_window_geometry()     # 窗口尺寸/位置：恢复上次或按屏幕自适应（不全屏）
        self._setup_web_view()
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._run_page_scan)
        self._scan_timer.start(2500)
        self._consolidating = False
        self._consolidate_worker = None
        # 周期整合同集多个 m3u8：保留最长/完整版，吃掉预览版，避免重复行与只下到30s
        self._cons_timer = QTimer(self)
        self._cons_timer.setInterval(6000)
        self._cons_timer.timeout.connect(self._run_consolidate)
        self._cons_timer.start()
        self._capture_mode = False        # 抓包开关：播放时把可用的视频链接写入粘贴框
        self._mitm_worker = None          # 本地 MITM 抓包代理线程
        self._proxy_port = 8899
        self._sys_proxy_on = False
        self._ca_key_path = os.path.join(config._EXE_DIR, "mitm_ca.key")
        self._ca_cert_path = os.path.join(config._EXE_DIR, "mitm_ca.crt")
        self._ca_pem = b""
        self._ext_worker = None
        self._ext_tmpl = {}
        self._ext_map = {}
        self._ext_pending_pick = None
        self._ext_pick_timer = None
        self._ext_pending_cap = None
        self._ext_cap_timer = None

    # ---------- 批量提取分享链接（集成自批量提取工具） ----------
    def _ext_refresh_windows(self):
        if not hasattr(self, "ext_target"):
            return
        self.ext_target.clear()
        try:
            wins = list_windows()
        except Exception:
            wins = []
        for hwnd, title, _ in wins:
            if not title.strip():
                continue
            self.ext_target.addItem(f"{hwnd} | {title.strip()[:36]}", (hwnd, title))

    def _ext_load_persisted(self):
        cfg = load_extract_cfg()
        for key, attr in (("share_xy", ("ext_share_x", "ext_share_y")),
                          ("copy_xy", ("ext_copy_x", "ext_copy_y"))):
            v = cfg.get(key)
            if isinstance(v, list) and len(v) == 2:
                getattr(self, attr[0]).setValue(int(v[0]))
                getattr(self, attr[1]).setValue(int(v[1]))
        if "swipe_method" in cfg and cfg["swipe_method"] == "drag":
            self.ext_swipe.setCurrentIndex(1)
        if "swipe_amount" in cfg:
            self.ext_amount.setValue(int(cfg["swipe_amount"]))
        if "share_wait" in cfg:
            self.ext_share_wait.setValue(float(cfg["share_wait"]))
        if "next_wait" in cfg:
            self.ext_next_wait.setValue(float(cfg["next_wait"]))
        if cfg.get("hk_start") in EXT_ENABLE_KEYS:
            self.ext_hk_start.setCurrentText(cfg["hk_start"])
        if cfg.get("hk_stop") in EXT_ENABLE_KEYS:
            self.ext_hk_stop.setCurrentText(cfg["hk_stop"])

    def _ext_persist(self):
        save_extract_cfg({
            "share_xy": [self.ext_share_x.value(), self.ext_share_y.value()],
            "copy_xy": [self.ext_copy_x.value(), self.ext_copy_y.value()],
            "swipe_method": "wheel" if self.ext_swipe.currentIndex() == 0 else "drag",
            "swipe_amount": self.ext_amount.value(),
            "share_wait": self.ext_share_wait.value(),
            "next_wait": self.ext_next_wait.value(),
            "hk_start": self.ext_hk_start.currentText(),
            "hk_stop": self.ext_hk_stop.currentText(),
        })

    def _ext_setup(self, idx):
        img = (idx == 1)
        self._ext_coord_group.setVisible(not img)
        self._ext_tmpl_group.setVisible(img)
        self._ext_load_persisted()

    def _ext_pick(self, which):
        if self._ext_pending_pick:
            self._ext_cancel_pick()
            return
        self._ext_pending_pick = which
        btn = {"share": self.ext_pick_share, "copy": self.ext_pick_copy}[which]
        btn.setText("⌛3秒后取点(再点取消)")
        btn.setStyleSheet("background:#7a5b2f; color:#fff;")
        name = {"share": "「分享」", "copy": "「复制链接」"}[which]
        self._log(f"点『{name}取点』后请立刻把鼠标移到对应按钮上，3 秒后自动取坐标。", "info")
        self._ext_pick_timer = QTimer(self)
        self._ext_pick_timer.setSingleShot(True)
        self._ext_pick_timer.timeout.connect(lambda: self._ext_do_pick(which))
        self._ext_pick_timer.start(3000)

    def _ext_cancel_pick(self):
        if self._ext_pick_timer:
            self._ext_pick_timer.stop()
        btn = {"share": self.ext_pick_share, "copy": self.ext_pick_copy}[self._ext_pending_pick]
        self._ext_pending_pick = None
        btn.setStyleSheet("")
        btn.setText("🎯 分享坐标" if btn is self.ext_pick_share else "🎯 复制坐标")
        self._log("已取消取点", "info")

    def _ext_do_pick(self, which):
        btn = {"share": self.ext_pick_share, "copy": self.ext_pick_copy}[which]
        btn.setStyleSheet("")
        btn.setText("🎯 分享坐标" if btn is self.ext_pick_share else "🎯 复制坐标")
        self._ext_pending_pick = None
        import ctypes
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        if which == "share":
            self.ext_share_x.setValue(pt.x); self.ext_share_y.setValue(pt.y)
        else:
            self.ext_copy_x.setValue(pt.x); self.ext_copy_y.setValue(pt.y)
        self._log(f"✓ 已记录『{which}』坐标 ({pt.x},{pt.y})", "ok")

    def _ext_cap(self, which):
        if self._ext_pending_cap:
            self._ext_cancel_cap()
            return
        self._ext_pending_cap = which
        btn = {"share": self.ext_cap_share, "copy": self.ext_cap_copy}[which]
        btn.setText("⌛3秒后采集(再点取消)")
        btn.setStyleSheet("background:#7a5b2f; color:#fff;")
        self._log("点『采集』后请立刻把鼠标移到目标按钮上，3 秒后自动截取模板。", "info")
        self._ext_cap_timer = QTimer(self)
        self._ext_cap_timer.setSingleShot(True)
        self._ext_cap_timer.timeout.connect(lambda: self._ext_do_cap(which))
        self._ext_cap_timer.start(3000)

    def _ext_cancel_cap(self):
        if self._ext_cap_timer:
            self._ext_cap_timer.stop()
        btn = {"share": self.ext_cap_share, "copy": self.ext_cap_copy}[self._ext_pending_cap]
        self._ext_pending_cap = None
        btn.setStyleSheet("")
        btn.setText("📷 分享推广" if btn is self.ext_cap_share else "📷 复制链接")
        self._log("已取消采集", "info")

    def _ext_do_cap(self, which):
        btn = {"share": self.ext_cap_share, "copy": self.ext_cap_copy}[which]
        btn.setStyleSheet("")
        btn.setText("📷 分享推广" if btn is self.ext_cap_share else "📷 复制链接")
        self._ext_pending_cap = None
        img, (cx, cy) = capture_pointer_sample()
        if img is None:
            self._log("采集失败，未截到图像", "warn")
            return
        self._ext_tmpl[which] = img
        st = {"share": self.ext_state_share, "copy": self.ext_state_copy}[which] if which in ("share", "copy") else None
        if st:
            st.setText(f"✓ 已采集（中心 {cx},{cy}）")
            st.setStyleSheet("color:#16a34a;")
        self._log(f"✓ 已采集『{which}』模板 ({cx},{cy})", "ok")

    def _ext_clr(self, which):
        self._ext_tmpl.pop(which, None)
        if which == "share":
            self.ext_state_share.setText("未采集"); self.ext_state_share.setStyleSheet("")
        elif which == "copy":
            self.ext_state_copy.setText("未采集（回退 Ctrl+C）"); self.ext_state_copy.setStyleSheet("")
        self._log(f"已清除『{which}』模板", "info")

    def _ext_test_share(self):
        sel = self._selected_ext()
        if not sel:
            self._log("请先选择目标窗口", "warn")
            return
        tmpl = self._ext_tmpl.get("share")
        if tmpl is None:
            self._log("请先采集「分享」模板", "warn")
            return
        hwnd, _ = self._selected_ext()
        activate(hwnd)
        import time as _t
        _t.sleep(0.3)
        x, y, w, h = get_rect(hwnd)
        shot = Screen().grab(x, y, w, h)
        loc = find_button(tmpl, shot, thresh=0.5) if shot is not None else None
        if loc:
            bx, by, sc = loc
            self.ext_state_share.setStyleSheet("color:#16a34a;")
            self.ext_state_share.setText(f"✓ 识别到 ({x+bx},{y+by}) 置信{sc:.2f}")
            click(x + bx, y + by)
        else:
            self.ext_state_share.setStyleSheet("color:#dc2626;")
            self.ext_state_share.setText("识别失败，请贴近重新采集")

    def _open_ext_panel(self):
        win = self.ext_window
        if win.isMinimized():
            # 最小化状态：重置为正常并唤到前台，而不是隐藏
            win.showNormal()
            win.raise_()
            win.activateWindow()
            return
        if win.isVisible():
            win.hide()
            return
        try:
            self._ext_refresh_windows()
        except Exception:
            pass
        win.show()
        win.raise_()
        win.activateWindow()

    # ---------- 全局自定义快捷键 ----------
    def _ext_install_hotkeys(self):
        if not hasattr(self, "ext_hk_start"):
            return
        user32 = ctypes.windll.user32
        try:
            hwnd = int(self.winId())
        except Exception:
            QTimer.singleShot(500, self._ext_install_hotkeys)
            return
        try:
            user32.UnregisterHotKey(hwnd, 1)
            user32.UnregisterHotKey(hwnd, 2)
        except Exception:
            pass
        ok1 = ok2 = False
        try:
            m, v = _ext_key_code(self.ext_hk_start.currentText())
            ok1 = bool(user32.RegisterHotKey(hwnd, 1, m, v))
        except Exception:
            ok1 = False
        try:
            m, v = _ext_key_code(self.ext_hk_stop.currentText())
            ok2 = bool(user32.RegisterHotKey(hwnd, 2, m, v))
        except Exception:
            ok2 = False
        if not getattr(self, "_ext_hk_filter", None) and hasattr(QApplication, "instance") and QApplication.instance():
            self._ext_hk_filter = ExtHotkeyFilter(self)
            QApplication.instance().installNativeEventFilter(self._ext_hk_filter)
        st = self.ext_status
        if ok1 and ok2:
            self._ext_retry = 0
            st.setText(f"快捷键: {self.ext_hk_start.currentText()} 开始 / {self.ext_hk_stop.currentText()} 停止")
            self._log(f"全局快捷键已启用: {self.ext_hk_start.currentText()} 开始 · {self.ext_hk_stop.currentText()} 停止", "ok")
        else:
            st.setText("快捷键被占用/注册失败，2 秒后自动重试…")
            self._log("全局快捷键注册失败（可能与其他软件冲突），将自动重试；也可直接点下方按钮提取", "warn")
            rc = int(getattr(self, "_ext_retry", 0))
            if rc < 5:
                self._ext_retry = rc + 1
                QTimer.singleShot(2000, self._ext_install_hotkeys)
            else:
                st.setText("快捷键持续被占用，请换键或直接点下方按钮")

    def ext_hotkey_start(self):
        if self._ext_worker and self._ext_worker.isRunning():
            return
        if not self.ext_window.isVisible():
            self.ext_window.show()
        self._ext_start()

    def ext_hotkey_stop(self):
        self._ext_stop()

    def _selected_ext(self):
        if self.ext_target.count() == 0:
            self._ext_refresh_windows()
        sel = self.ext_target.currentData()
        if sel is not None:
            return sel
        if self.ext_target.count():
            self.ext_target.setCurrentIndex(0)
            return self.ext_target.currentData()
        return None

    def _ext_start(self):
        sel = self._selected_ext()
        if not sel:
            self._log("请先在「目标窗口」选择桌面 App 窗口", "warn")
            return
        hwnd, title = sel
        self._ext_persist()
        self._ext_worker = ExtractWorker(self)
        self._ext_worker.hwnd = hwnd
        self._ext_worker.esc_close = self.ext_esc.isChecked()
        self._ext_worker.swipe = "drag" if self.ext_swipe.currentIndex() == 1 else "wheel"
        self._ext_worker.max_found = self.ext_found_limit.value()
        if self.ext_alg.currentIndex() == 1:
            if self._ext_tmpl.get("share") is None:
                self._log("请先采集「分享」推广模板", "warn"); return
            if self._ext_tmpl.get("copy") is None:
                self._log("请先采集「复制链接」模板（找不到时会回退 Ctrl+C）", "warn"); return
            self._ext_worker.mode = "img"
            self._ext_worker.share_tmpl = self._ext_tmpl["share"]
            self._ext_worker.copy_tmpl = self._ext_tmpl["copy"]
            self._log(f"开始图片识别提取 → {title}", "info")
        else:
            zero = ((self.ext_share_x.value(), self.ext_share_y.value()) == (0, 0))
            if zero:
                self._log("请先设置「分享」坐标", "warn"); return
            self._ext_worker.share = (self.ext_share_x.value(), self.ext_share_y.value())
            self._ext_worker.copy = (self.ext_copy_x.value(), self.ext_copy_y.value())
            self._log(f"开始坐标提取 → {title}", "info")
        self._ext_worker.log.connect(lambda m: self._log(m, "info"))
        self._ext_worker.found.connect(self._ext_add_found)
        self._ext_worker.state.connect(lambda s, n: self.ext_status.setText(f"第 {s} 集 · 已提取 {n} 条"))
        self._ext_worker.finished.connect(self._on_ext_done)
        self.ext_start.setEnabled(False)
        self.ext_stop.setEnabled(True)
        self.ext_status.setText("提取中…")
        self._ext_worker.start()

    def _on_ext_done(self):
        if not hasattr(self, "ext_start"):
            return
        self.ext_start.setEnabled(True)
        self.ext_stop.setEnabled(False)
        w = getattr(self, "_ext_worker", None)
        if w is not None and getattr(w, "max_reached", False):
            self.ext_status.setText(f"已提取满 {w.max_found} 条，自动停止")
            self._log(f"已达到提取条数上限 {w.max_found} 条，自动停止", "ok")
        else:
            self.ext_status.setText("已停止")

    def _ext_stop(self):
        if self._ext_worker and self._ext_worker.isRunning():
            self._ext_worker.stop()
            self._ext_worker.wait(3000)
        self.ext_start.setEnabled(True)
        self.ext_stop.setEnabled(False)
        self.ext_status.setText("已停止")

    def _ext_add_found(self, url):
        """把提取到的分享链接导入上方粘贴框（去重）"""
        existing = set()
        for u in self.extract_urls(self.input_edit.toPlainText()):
            existing.add(u)
        if url in existing:
            return
        txt = self.input_edit.toPlainText()
        self.input_edit.setPlainText((txt + "\n" + url).strip())
        self._update_count()
        self._log(f"已导入粘贴框: {url}", "ok")

    # ---------- UI ----------
    def _paste_from_clipboard(self, force=True):
        """从剪贴板读取分享链接填入输入框（仅供「从剪贴板读取」按钮手动调用）"""
        try:
            txt = QApplication.clipboard().text() or ""
        except Exception:
            txt = ""
        txt = txt.strip()
        if self.extract_urls(txt):
            if force or not self.input_edit.toPlainText().strip():
                self.input_edit.setPlainText(txt)
                self._update_count()
                self._log("已从剪贴板填入分享链接，点击【识别并打开】即可播放识别。", "info")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶层：首页(项目管理) 与 工作区(嗅探+生成) 用 QStackedWidget 切换 ----
        self._stack = QStackedWidget()
        root.addWidget(self._stack)
        # 工作区：顶部【返回首页 + 当前项目名】工具条 + (视频嗅探/视频生成) 二级 tab
        self.workspace_page = QWidget()
        self.ws_layout = QVBoxLayout(self.workspace_page)
        self.ws_layout.setContentsMargins(0, 0, 0, 0)
        self.ws_layout.setSpacing(0)
        wsbar = QHBoxLayout()
        wsbar.setContentsMargins(12, 6, 12, 6)
        self.proj_back = QPushButton("← 返回首页")
        self.proj_back.setObjectName("ghostBtn")
        self.proj_back.setCursor(Qt.PointingHandCursor)
        self.proj_back.clicked.connect(self._proj_back_clicked)
        self.proj_back_target = "home"
        wsbar.addWidget(self.proj_back)
        self.proj_title = QLabel("未打开项目")
        self.proj_title.setStyleSheet("font-size:14px; font-weight:700; color:#1e293b;")
        # 标题 + 当前分集纯文字（紧跟剧名，如「📁 xxx · 第2集」），整体占 stretch 使右侧按钮靠右
        _proj_grp = QHBoxLayout()
        _proj_grp.setContentsMargins(0, 0, 0, 0)
        _proj_grp.setSpacing(6)
        _proj_grp.addWidget(self.proj_title)
        self.gen_ep_badge_lbl = QLabel("")
        self.gen_ep_badge_lbl.setStyleSheet("font-size:13px; font-weight:700; color:#1e293b;")
        self.gen_ep_badge_lbl.setVisible(False)
        _proj_grp.addWidget(self.gen_ep_badge_lbl)
        _proj_grp.addStretch(1)
        wsbar.addLayout(_proj_grp, 1)
        # 全局运行状态灯：绿色=运行中，红色=空闲（跨页面可见，一眼掌握任务状态）
        self.global_status = QLabel("● 空闲")
        self.global_status.setStyleSheet("color:#ef4444; font-weight:800; font-size:13px;")
        self.global_status.setToolTip("当前无任务运行")
        self.global_status.setCursor(Qt.PointingHandCursor)
        self.global_status.mousePressEvent = self._global_status_click
        wsbar.addWidget(self.global_status)
        # 全局「设置」入口（统一配置 AI 服务 / 分段设置；状态灯旁，跨页面可见）
        self.ai_services_btn = QPushButton("⚙ 设置")
        self.ai_services_btn.setObjectName("ghostBtn")
        self.ai_services_btn.setCursor(Qt.PointingHandCursor)
        self.ai_services_btn.setToolTip("AI 服务（视频分析 / 视频生成 / 资产生成图）与分段设置")
        self.ai_services_btn.clicked.connect(self._gen_open_settings_menu)
        wsbar.addWidget(self.ai_services_btn)
        # 「重置布局」：把窗口尺寸与内部分割比例恢复到默认（窗口可自由拖拽缩放，忘了拖坏可一键复原）
        self.reset_lay_btn = QPushButton("⤢ 重置布局")
        self.reset_lay_btn.setObjectName("ghostBtn")
        self.reset_lay_btn.setCursor(Qt.PointingHandCursor)
        self.reset_lay_btn.setToolTip("恢复默认窗口大小与内部面板比例（窗口本来就支持自由拖拽缩放）")
        self.reset_lay_btn.clicked.connect(self._reset_layout)
        wsbar.addWidget(self.reset_lay_btn)
        self.ws_layout.addLayout(wsbar)
        self.nav_tabs = QTabWidget()
        self.nav_tabs.setObjectName("navTabs")
        self.nav_tabs.tabBar().setObjectName("navTabBar")
        self.sniff_page = QWidget()
        self.sniff_layout = QVBoxLayout(self.sniff_page)
        self.sniff_layout.setContentsMargins(0, 0, 0, 0)
        self.ws_layout.addWidget(self.nav_tabs)
        # ---- 底部全局状态栏：左侧面包屑（项目 › 页名 › 分集）｜中间操作/进度｜右侧日志计数 ----
        sb = QHBoxLayout()
        sb.setContentsMargins(12, 4, 12, 4)
        sb.setSpacing(10)
        self.breadcrumb = QLabel("首页")
        self.breadcrumb.setObjectName("breadcrumb")
        sb.addWidget(self.breadcrumb)
        sb.addStretch(1)
        self.sb_action = QLabel("就绪")
        self.sb_action.setObjectName("breadcrumb")
        self.sb_action.setStyleSheet(self.breadcrumb.styleSheet() + "; color:#2563eb;")
        sb.addWidget(self.sb_action)
        self.sb_log_count = QLabel("日志: 0 条")
        self.sb_log_count.setObjectName("sbLogCount")
        sb.addWidget(self.sb_log_count)
        sbw = QWidget()
        sbw.setLayout(sb)
        self.ws_layout.addWidget(sbw)

        # ---- ① 首页：项目卡片 + 新建项目 ----
        self.home_page = QWidget()
        hp_root = QVBoxLayout(self.home_page)
        hp_root.setContentsMargins(24, 20, 24, 20)
        hp_root.setSpacing(14)
        hdr = QHBoxLayout()
        htitle = QLabel("🏠 我的项目")
        htitle.setStyleSheet("font-size:22px; font-weight:900; color:#1e293b;")
        hdr.addWidget(htitle)
        hdr.addSpacing(10)
        hcap = QLabel("每个项目独立保存识别剧集、分析结果、生成历史与下载目录")
        hcap.setObjectName("cap")
        hdr.addWidget(hcap)
        hdr.addStretch(1)
        self.new_proj_btn = QPushButton("＋ 新建项目")
        self.new_proj_btn.setObjectName("accentBtn")
        self.new_proj_btn.setMinimumHeight(38)
        self.new_proj_btn.setMinimumWidth(140)
        self.new_proj_btn.clicked.connect(self._new_project)
        hdr.addWidget(self.new_proj_btn)
        hp_root.addLayout(hdr)
        # 项目卡片区：用 QListWidget（命中测试由 Qt 保证，杜绝点击丢失）
        self.home_list = QListWidget()
        self.home_list.setObjectName("homeList")
        self.home_list.setIconSize(QSize(36, 36))
        # IconMode 网格 + 自绘卡片：命中测试可靠（依然是 QListWidget），观感为卡片网格
        self.home_list.setViewMode(QListView.IconMode)
        self.home_list.setResizeMode(QListView.Adjust)
        self.home_list.setMovement(QListView.Static)
        self.home_list.setUniformItemSizes(True)
        self.home_list.setGridSize(QSize(202, 110))
        self.home_list.setSpacing(4)
        self.home_list.setItemDelegate(_CardDelegate(self.home_list))
        self.home_list.setMinimumHeight(260)
        self.home_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.home_list.customContextMenuRequested.connect(self._home_menu)
        self.home_list.itemClicked.connect(self._on_home_item)
        hp_root.addWidget(self.home_list, 1)
        self._stack.addWidget(self.home_page)
        self._stack.setCurrentWidget(self.home_page)

        # ---- ① 批量输入卡片 ----
        in_card = QGroupBox("批量粘贴分享链接（自动识别网址，支持整段带文字的分享消息）")
        in_card.setObjectName("card")
        ic = QVBoxLayout(in_card)
        self.input_edit = InputTextEdit()
        self.input_edit.setAcceptRichText(False)
        self.input_edit.setMaximumHeight(74)
        self.input_edit.setMinimumHeight(56)
        self.input_edit.setPlaceholderText(
            "在此粘贴分享链接：支持整段带文字的分享消息，或多行批量链接，将自动识别其中所有网址并逐个打开播放。\n"
            "示例（整段直接粘贴即可）：\n"
            "漫剧《万妖图录传AI真人版第二季》 - 免费好剧，尽在红果\n"
            "点击链接打开👉`https://novelquickapp.com/s/TdjUIM3Qz_g/`\n"
            "识别到的网址个数会显示；点击【识别并打开】开始播放识别，勾选右侧视频即可下载。")
        ic.addWidget(self.input_edit)

        # 工具行用 FlowLayout：窗口变窄时自动换行，不再把整个窗口撑到很宽
        ctl = WrapFlowLayout(hspacing=8, vspacing=6)
        clp = QPushButton("📋 从剪贴板读取")
        clp.setObjectName("ghostBtn")
        clp.setToolTip("把剪贴板里的分享链接填入输入框（覆盖当前内容）")
        clp.clicked.connect(lambda: self._paste_from_clipboard(force=True))
        ctl.addWidget(clp)
        _v1 = QFrame(); _v1.setObjectName("vsep"); _v1.setFrameShape(QFrame.VLine)
        ctl.addWidget(_v1)
        self.capture_btn = QPushButton("📦 抓包")
        self.capture_btn.setObjectName("ghostBtn")
        self.capture_btn.setToolTip(
            "抓包：内置浏览器 / 本地代理抓第三方的 m3u8·mp4 播放地址 → 写入粘贴框，"
            "再走识别嗅探下载。点开可一键开启本地代理并安装受信 CA。")
        self.capture_btn.clicked.connect(self._open_capture_menu)
        ctl.addWidget(self.capture_btn)
        extbtn = QPushButton("🧩 批量提取分享链接")
        extbtn.setObjectName("ghostBtn")
        extbtn.setToolTip("打开/关闭「批量提取分享链接」小挂件")
        extbtn.clicked.connect(self._open_ext_panel)
        ctl.addWidget(extbtn)
        self.open_btn = QPushButton("▶ 识别并打开 / 批量播放")
        self.open_btn.setObjectName("accentBtn")
        self.open_btn.clicked.connect(self._open_url)
        ctl.addWidget(self.open_btn)
        _v2 = QFrame(); _v2.setObjectName("vsep"); _v2.setFrameShape(QFrame.VLine)
        ctl.addWidget(_v2)
        self.count_lbl = QLabel("识别链接: 0 个")
        self.count_lbl.setStyleSheet("color:#2563eb; font-weight:700;")
        ctl.addWidget(self.count_lbl)
        ctl.addWidget(QLabel("每页停留(秒):"))
        self.hold_spin = QSpinBox()
        self.hold_spin.setRange(1, 600)
        self.hold_spin.setValue(1)
        ctl.addWidget(self.hold_spin)
        self.status_lbl = QLabel("未开始")
        self.status_lbl.setStyleSheet("color:#64748b;")
        ctl.addWidget(self.status_lbl)
        self.stop_browse_btn = QPushButton("⏹ 停止批量浏览")
        self.stop_browse_btn.setObjectName("stopBtn")
        self.stop_browse_btn.clicked.connect(self._stop_browse)
        self.stop_browse_btn.setEnabled(False)
        ctl.addWidget(self.stop_browse_btn)
        # 把 WrapFlowLayout 装进 QWidget 再 addWidget，父布局才能正确收到换行后的真实高度
        ctl_wrap = QWidget()
        ctl_wrap.setLayout(ctl)
        ic.addWidget(ctl_wrap)
        self.sniff_layout.addWidget(in_card)

        # ---- ①.5 批量提取分享链接（独立小挂件，提取后自动导入上方粘贴框） ----
        ex_card = QWidget()
        ex_card.setObjectName("app")
        ex_card.setFixedWidth(430)
        ex_card.setWindowFlag(Qt.FramelessWindowHint)
        ex_card.setWindowFlag(Qt.Window)
        ex_card.setWindowFlag(Qt.WindowStaysOnTopHint)
        ebody = QVBoxLayout(ex_card)
        ebody.setContentsMargins(0, 0, 0, 0)
        ebody.setSpacing(0)
        ext_title = DragTitleBar("🧩 批量提取分享链接", ex_card)
        ext_title.min_btn.clicked.connect(ex_card.showMinimized)
        ext_title.close_btn.clicked.connect(ex_card.hide)
        ebody.addWidget(ext_title)
        ext_body = QWidget()
        ext_body.setObjectName("appBody")
        bd = QVBoxLayout(ext_body)
        bd.setContentsMargins(12, 10, 12, 10)
        ebody.addWidget(ext_body, 1)
        hdr = QHBoxLayout()
        self.ext_pintop = QCheckBox("窗口置顶")
        self.ext_pintop.setChecked(True)
        self.ext_pintop.toggled.connect(lambda on: ex_card.setWindowFlag(Qt.WindowStaysOnTopHint, on) or ex_card.show())
        hdr.addWidget(self.ext_pintop)
        hdr.addStretch(1)
        tip = QLabel("提取的分享链接将自动导入主界面粘贴框")
        tip.setObjectName("cap")
        hdr.addWidget(tip)
        bd.addLayout(hdr)

        er0 = QHBoxLayout()
        er0.addWidget(QLabel("目标窗口:"))
        self.ext_target = QComboBox()
        self.ext_target.setMinimumWidth(180)
        er0.addWidget(self.ext_target, 1)
        refb = QPushButton("刷新")
        refb.setObjectName("ghostBtn")
        refb.clicked.connect(self._ext_refresh_windows)
        er0.addWidget(refb)
        er0.addWidget(QLabel("识别方式:"))
        self.ext_alg = QComboBox()
        self.ext_alg.addItems(["① 坐标脚本（精准）", "② 图片/文字识别（自动）"])
        self.ext_alg.currentIndexChanged.connect(self._ext_setup)
        er0.addWidget(self.ext_alg)
        bd.addLayout(er0)

        # 坐标脚本容器
        self._ext_coord_group = QWidget()
        cgb = QVBoxLayout(self._ext_coord_group)
        cgb.setContentsMargins(0, 0, 0, 0)
        self.ext_share_x = QSpinBox(); self.ext_share_x.setRange(0, 20000); self.ext_share_x.setMaximumWidth(80)
        self.ext_share_y = QSpinBox(); self.ext_share_y.setRange(0, 20000); self.ext_share_y.setMaximumWidth(80)
        self.ext_copy_x = QSpinBox(); self.ext_copy_x.setRange(0, 20000); self.ext_copy_x.setMaximumWidth(80)
        self.ext_copy_y = QSpinBox(); self.ext_copy_y.setRange(0, 20000); self.ext_copy_y.setMaximumWidth(80)
        self.ext_pick_share = QPushButton("🎯 分享坐标"); self.ext_pick_share.clicked.connect(lambda: self._ext_pick("share"))
        self.ext_pick_copy = QPushButton("🎯 复制坐标"); self.ext_pick_copy.clicked.connect(lambda: self._ext_pick("copy"))
        r1 = QHBoxLayout(); r1.addWidget(QLabel("分享:"))
        r1.addWidget(QLabel("X")); r1.addWidget(self.ext_share_x); r1.addWidget(QLabel("Y")); r1.addWidget(self.ext_share_y)
        r1.addWidget(self.ext_pick_share, 1); cgb.addLayout(r1)
        r2 = QHBoxLayout(); r2.addWidget(QLabel("复制:"))
        r2.addWidget(QLabel("X")); r2.addWidget(self.ext_copy_x); r2.addWidget(QLabel("Y")); r2.addWidget(self.ext_copy_y)
        r2.addWidget(self.ext_pick_copy, 1); cgb.addLayout(r2)
        tip = QLabel("点取点按钮后 3 秒内把鼠标移到目标上，到点自动填坐标；再点取消")
        tip.setObjectName("cap"); cgb.addWidget(tip)
        bd.addWidget(self._ext_coord_group)

        # 识别模板容器
        self._ext_tmpl_group = QWidget()
        tg = QVBoxLayout(self._ext_tmpl_group)
        tg.setContentsMargins(0, 0, 0, 0)
        self.ext_cap_share = QPushButton("📷 分享推广")
        self.ext_cap_share.clicked.connect(lambda: self._ext_cap("share"))
        self.ext_clr_share = QPushButton("🗑 清除"); self.ext_clr_share.clicked.connect(lambda: self._ext_clr("share"))
        self.ext_state_share = QLabel("未采集")
        self.ext_cap_copy = QPushButton("📷 复制链接"); self.ext_cap_copy.clicked.connect(lambda: self._ext_cap("copy"))
        self.ext_clr_copy = QPushButton("🗑 清除"); self.ext_clr_copy.clicked.connect(lambda: self._ext_clr("copy"))
        self.ext_state_copy = QLabel("未采集（回退 Ctrl+C）")
        t1 = QHBoxLayout(); t1.addWidget(QLabel("分享：")); t1.addWidget(self.ext_cap_share); t1.addWidget(self.ext_clr_share); t1.addWidget(self.ext_state_share, 1)
        t2 = QHBoxLayout(); t2.addWidget(QLabel("复制：")); t2.addWidget(self.ext_cap_copy); t2.addWidget(self.ext_clr_copy); t2.addWidget(self.ext_state_copy, 1)
        self.ext_test = QPushButton("🔍 测试识别分享"); self.ext_test.clicked.connect(self._ext_test_share)
        t2.addWidget(self.ext_test, 1)
        ttip = QLabel("点采集后 3 秒内把鼠标移到目标上，到点自动截模板")
        ttip.setObjectName("cap")
        tg.addLayout(t1); tg.addLayout(t2); tg.addWidget(ttip)
        bd.addWidget(self._ext_tmpl_group)

        # 公共参数
        ga = QHBoxLayout()
        self.ext_esc = QCheckBox("④ Esc 退出弹窗")
        self.ext_esc.setChecked(True)
        self.ext_esc.setToolTip("提取到链接后，勾选则按 Esc 关闭『复制链接』弹窗；取消则跳过关闭直接滑动下一集")
        ga.addWidget(self.ext_esc)
        ga.addSpacing(8)
        ga.addWidget(QLabel("滑动:"))
        self.ext_swipe = QComboBox(); self.ext_swipe.addItems(["滚轮下滑", "拖拽上滑"]); ga.addWidget(self.ext_swipe)
        ga.addSpacing(8)
        ga.addWidget(QLabel("量:"))
        self.ext_amount = QSpinBox(); self.ext_amount.setRange(1, 20); self.ext_amount.setValue(4); ga.addWidget(self.ext_amount)
        ga.addStretch(1)
        bd.addLayout(ga)
        wt = QHBoxLayout()
        wt.addWidget(QLabel("分享后等待:"))
        self.ext_share_wait = QDoubleSpinBox(); self.ext_share_wait.setRange(0.3, 10.0); self.ext_share_wait.setValue(1.0)
        wt.addWidget(self.ext_share_wait, 1)
        wt.addWidget(QLabel("秒"))
        wt.addSpacing(10)
        wt.addWidget(QLabel("切集后等待:"))
        self.ext_next_wait = QDoubleSpinBox(); self.ext_next_wait.setRange(0.3, 20.0); self.ext_next_wait.setValue(1.0)
        wt.addWidget(self.ext_next_wait, 1)
        wt.addWidget(QLabel("秒"))
        bd.addLayout(wt)

        # 提取条数上限（按累计提取到的链接条数自动停止）
        fd = QHBoxLayout()
        fd.addWidget(QLabel("提取条数上限:"))
        self.ext_found_limit = QSpinBox()
        self.ext_found_limit.setRange(0, 9999)
        self.ext_found_limit.setValue(0)
        self.ext_found_limit.setToolTip("累计提取到该数量的链接后自动停止；0 表示不限")
        fd.addWidget(self.ext_found_limit, 1)
        fd.addWidget(QLabel("条 (0=不限)"))
        fd.addStretch(1)
        bd.addLayout(fd)

        # 全局自定义快捷键行
        hk = QHBoxLayout()
        hk.addWidget(QLabel("全局快捷键:"))
        hk.addWidget(QLabel("开始"))
        self.ext_hk_start = QComboBox()
        self.ext_hk_start.addItems(EXT_ENABLE_KEYS)
        self.ext_hk_start.setCurrentText("F1")
        self.ext_hk_start.currentTextChanged.connect(lambda _: self._ext_install_hotkeys())
        hk.addWidget(self.ext_hk_start)
        hk.addWidget(QLabel("停止"))
        self.ext_hk_stop = QComboBox()
        self.ext_hk_stop.addItems(EXT_ENABLE_KEYS)
        self.ext_hk_stop.setCurrentText("F2")
        self.ext_hk_stop.currentTextChanged.connect(lambda _: self._ext_install_hotkeys())
        hk.addWidget(self.ext_hk_stop)
        hk.addStretch(1)
        bd.addLayout(hk)

        er = QHBoxLayout()
        self.ext_start = QPushButton("▶ 开始提取")
        self.ext_start.setObjectName("primaryBtn")
        self.ext_start.setFixedHeight(44)
        self.ext_start.setStyleSheet("font-size:15px; font-weight:700;")
        self.ext_start.clicked.connect(self._ext_start)
        er.addWidget(self.ext_start, 2)
        self.ext_stop = QPushButton("⏹ 停止")
        self.ext_stop.setObjectName("stopBtn")
        self.ext_stop.setFixedHeight(44)
        self.ext_stop.setStyleSheet("font-size:15px; font-weight:700;")
        self.ext_stop.setEnabled(False)
        self.ext_stop.clicked.connect(self._ext_stop)
        er.addWidget(self.ext_stop, 1)
        self.ext_status = QLabel("未开始")
        self.ext_status.setStyleSheet("color:#64748b;")
        er.addWidget(self.ext_status, 2)
        bd.addLayout(er)
        self.ext_window = ex_card
        self._ext_setup(self.ext_alg.currentIndex())
        self._ext_refresh_windows()
        # 延迟安装全局快捷键（等主窗 winId 有效）
        QTimer.singleShot(600, self._ext_install_hotkeys)

        # ---- ② 下载设置卡片 ----
        opt_card = QGroupBox("下载设置")
        opt_card.setObjectName("card")
        oc = QVBoxLayout(opt_card)

        opt = QHBoxLayout()
        opt.addWidget(QLabel("命名规则:"))
        self.name_combo = QComboBox()
        for text, key in NAME_RULES:
            self.name_combo.addItem(text, key)
        opt.addWidget(self.name_combo)
        opt.addSpacing(14)
        opt.addWidget(QLabel("保存到:"))
        self.dir_edit = QLineEdit(config.DEFAULT_DOWNLOAD_PATH)
        self.dir_edit.setMinimumWidth(120)   # 允许窗口缩窄时压缩路径框，而不是撑住整个窗口
        opt.addWidget(self.dir_edit, 1)
        o = QPushButton("📂 打开目录")
        o.setObjectName("ghostBtn")
        o.setToolTip("打开当前下载目录，查看已下载的视频文件")
        o.clicked.connect(self._open_download_dir)
        opt.addWidget(o)
        c = QPushButton("更改…")
        c.setObjectName("ghostBtn")
        c.setToolTip("选择新的下载保存文件夹")
        c.clicked.connect(self._browse_dir)
        opt.addWidget(c)
        oc.addLayout(opt)
        self.sniff_layout.addWidget(opt_card)

        splitter = QSplitter(Qt.Horizontal)

        # 左侧：视频预览播放器 + 播放控制条（网页引擎仅在后台用于嗅探，不显示）
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.vwidget = None
        self._seeking = False
        self._vol_t = QTimer(self); self._vol_t.setSingleShot(True)
        self._vol_t.setInterval(300); self._vol_t.timeout.connect(self._hide_volpopup)

        # 悬浮控制条（移入视频浮现，无背景）：仅一条两段色进度条 + 白色线条喇叭
        self.ctlbar = QWidget(); self.ctlbar.setObjectName("playerBar")
        cb = QHBoxLayout(self.ctlbar)
        cb.setContentsMargins(0, 0, 0, 0); cb.setSpacing(0)
        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(0, 1000)
        self.sld.setObjectName("seekSlider")
        self.sld.sliderMoved.connect(self._seek)
        self.sld.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.sld.sliderReleased.connect(self._seek_released)
        cb.addWidget(self.sld, 1)
        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setObjectName("playTime")
        self.time_lbl.setMinimumWidth(86)
        self.time_lbl.setAlignment(Qt.AlignCenter)
        cb.addWidget(self.time_lbl)
        self.vol_btn = QPushButton()
        self.vol_btn.setObjectName("volBtn")
        self.vol_btn.setIcon(self._make_vol_icon())
        self.vol_btn.setIconSize(QSize(18, 18))
        self.vol_btn.setToolTip("音量（鼠标放上来调节）")
        self.vol_btn.setCursor(Qt.PointingHandCursor)
        cb.addWidget(self.vol_btn)

        # 竖向音量弹窗（鼠标悬停喇叭显示 / 移开隐藏）
        self.vol_popup = QWidget(); self.vol_popup.setObjectName("volPopup")
        vp = QVBoxLayout(self.vol_popup)
        vp.setContentsMargins(4, 6, 4, 6)
        self.vol_slider = VolSlider()
        self.vol_slider.valueChanged.connect(self._set_volume)
        vp.addWidget(self.vol_slider, 1)

        self.vol_btn.enterEvent = lambda e: self._show_volpopup()
        self.vol_btn.leaveEvent = lambda e: self._vol_t.start()
        self.vol_popup.enterEvent = lambda e: self._vol_t.stop()
        self.vol_popup.leaveEvent = lambda e: self._vol_t.start()

        self.player_view = OverlayPlayer(self.player, on_tap=self._player_toggle)
        self.player_view.set_controls(self.ctlbar, self.vol_popup)
        ll.addWidget(self.player_view, 3)   # 播放器 : 结果区 ≈ 3 : 2，随窗口高度一起缩放

        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(lambda e, s: self._log(f"播放出错: {s}", "error"))

        # ---- AI 识别结果区（播放器下方，字幕识别 / 视频分析 两个标签页）----
        self.result_tabs = QTabWidget()
        self.result_tabs.setObjectName("resultTabs")
        # --- 标签页1：字幕识别 ---
        sub_page = QWidget()
        sb = QVBoxLayout(sub_page)
        sb.setContentsMargins(8, 6, 8, 6); sb.setSpacing(4)
        sbar = WrapFlowLayout(hspacing=6, vspacing=6)   # 窄窗口自动换行，不撑宽窗口
        self.sub_status = QLabel("未识别")
        self.sub_status.setStyleSheet("color:#64748b;")
        sbar.addWidget(self.sub_status)
        self.sub_batch = QPushButton("🎬 批量识别")
        self.sub_batch.setObjectName("ghostBtn")
        self.sub_batch.setToolTip("按分享链接剧集依次识别，结果按集累积显示")
        self.sub_batch.clicked.connect(self._batch_ocr)
        sbar.addWidget(self.sub_batch)
        _sbar_sep = QFrame(); _sbar_sep.setObjectName("vsep"); _sbar_sep.setFrameShape(QFrame.VLine)
        sbar.addWidget(_sbar_sep)
        self.sub_copy = QPushButton("📋 复制")
        self.sub_copy.setObjectName("ghostBtn")
        self.sub_copy.clicked.connect(self._sub_copy)
        self.sub_export = QPushButton("💾 导出 txt")
        self.sub_export.setObjectName("ghostBtn")
        self.sub_export.clicked.connect(self._sub_export)
        self.sub_clear = QPushButton("清空")
        self.sub_clear.setObjectName("ghostBtn")
        self.sub_clear.clicked.connect(self._sub_clear)
        self.sub_set_btn = QPushButton("⚙ 设置")
        self.sub_set_btn.setObjectName("ghostBtn")
        self.sub_set_btn.setToolTip("字幕识别设置：语言 / 采样帧率 / 最短字幕时长 / 仅识别底部字幕区 / 引擎状态 / 测试识别")
        self.sub_set_btn.clicked.connect(self._open_ocr_menu)
        sbar.addWidget(self.sub_copy); sbar.addWidget(self.sub_export); sbar.addWidget(self.sub_clear)
        sbar.addWidget(self.sub_set_btn)
        _sbar_wrap = QWidget(); _sbar_wrap.setLayout(sbar)
        sb.addWidget(_sbar_wrap)
        self.sub_text = QPlainTextEdit()
        self.sub_text.setReadOnly(True)
        self.sub_text.setObjectName("logView")
        sb.addWidget(self.sub_text, 1)
        self.result_tabs.addTab(sub_page, "📝 字幕识别")
        # --- 标签页2：视频分析（GLM-5.3 / GLM-5.3-Flash） ---
        va_page = QWidget()
        vp = QVBoxLayout(va_page)
        vp.setContentsMargins(8, 6, 8, 6); vp.setSpacing(4)
        var = WrapFlowLayout(hspacing=6, vspacing=6)   # 9 个按钮原本一行要 800+px，改成可换行
        self.va_status = QLabel("未分析")
        self.va_status.setStyleSheet("color:#64748b;")
        var.addWidget(self.va_status)
        self.va_batch = QPushButton("▶ 开始分析")
        self.va_batch.setObjectName("startBtn")
        self.va_batch.setToolTip("开始分析全部已载入剧集（逐集分析，结果按集累加）；分析中点击可停止")
        self.va_batch.clicked.connect(self._va_batch)
        var.addWidget(self.va_batch)
        _vasep1 = QFrame(); _vasep1.setObjectName("vsep"); _vasep1.setFrameShape(QFrame.VLine)
        var.addWidget(_vasep1)
        self.va_local = QPushButton("📁 分析本地视频")
        self.va_local.setObjectName("ghostBtn")
        self.va_local.setToolTip("选择一个或多个本地视频文件批量分析（可多选）")
        self.va_local.clicked.connect(self._va_analyze_local_pick)
        var.addWidget(self.va_local)
        self.va_script = QPushButton("📜 一键转剧本")
        self.va_script.setObjectName("accentBtn")
        self.va_script.setToolTip("把当前的分析结果（可含多集）反推生成标准剧本")
        self.va_script.clicked.connect(self._va_to_script)
        var.addWidget(self.va_script)
        self.va_script_local = QPushButton("📂 本地转剧本")
        self.va_script_local.setObjectName("ghostBtn")
        self.va_script_local.setToolTip("读取本地已保存的分析结果文件，反推生成剧本")
        self.va_script_local.clicked.connect(self._va_to_script_local)
        var.addWidget(self.va_script_local)
        self.va_set = QPushButton("⚙ 设置 / 模型")
        self.va_set.setObjectName("ghostBtn")
        self.va_set.setToolTip("视频分析：模式 / 模型(GLM-5.3·Flash) / 抽帧数 / API Key")
        self.va_set.clicked.connect(self._open_va_menu)
        var.addWidget(self.va_set)
        _vasep2 = QFrame(); _vasep2.setObjectName("vsep"); _vasep2.setFrameShape(QFrame.VLine)
        var.addWidget(_vasep2)
        self.va_copy = QPushButton("📋 复制")
        self.va_copy.setObjectName("ghostBtn")
        self.va_copy.clicked.connect(self._va_copy)
        self.va_export = QPushButton("💾 导出 txt")
        self.va_export.setObjectName("ghostBtn")
        self.va_export.clicked.connect(self._va_export)
        self.va_clear = QPushButton("清空")
        self.va_clear.setObjectName("ghostBtn")
        self.va_clear.clicked.connect(self._va_clear)
        var.addWidget(self.va_copy); var.addWidget(self.va_export); var.addWidget(self.va_clear)
        _var_wrap = QWidget(); _var_wrap.setLayout(var)
        vp.addWidget(_var_wrap)
        self.va_text = QPlainTextEdit()
        self.va_text.setReadOnly(True)
        self.va_text.setObjectName("logView")
        vp.addWidget(self.va_text, 1)
        self.result_tabs.addTab(va_page, "🎞 视频分析")
        # 视频分析置为第一个标签（原字幕识别在前，两者对调）
        self.result_tabs.insertTab(0, va_page, "🎞 视频分析")
        self.result_tabs.setCurrentIndex(0)  # 识别结果区默认显示视频分析界面
        # 结果区高度随窗口高度自适应（_apply_responsive 里按窗口比例重算），不再写死 280
        ll.addWidget(self.result_tabs, 2)
        self._last_subs = []
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        table_box = QGroupBox("识别到的视频")
        tb = QVBoxLayout(table_box)
        row_ops = WrapFlowLayout(hspacing=6, vspacing=6)
        sel_all = QPushButton("☑ 全选")
        sel_all.setObjectName("ghostBtn")
        sel_all.clicked.connect(self._select_all)
        row_ops.addWidget(sel_all)
        add_local = QPushButton("📁 添加本地视频")
        add_local.setObjectName("ghostBtn")
        add_local.clicked.connect(self._grid_add_local)
        row_ops.addWidget(add_local)
        hint = QLabel("单击=选中 · 双击=播放 · 右键=识别/分析")
        hint.setStyleSheet("color:#94a3b8;")
        hint.setWordWrap(True)
        row_ops.addWidget(hint)
        _rowops_wrap = QWidget(); _rowops_wrap.setLayout(row_ops)
        tb.addWidget(_rowops_wrap)

        self.epgrid = EpisodeGrid(on_change=self._update_download_btn, on_play=self._grid_play,
                                  on_ocr=self._grid_ocr, on_vanalyze=self._grid_vanalyze,
                                  on_delete=self._on_ep_deleted)
        tb.addWidget(self.epgrid, 1)   # 剧集网格跟随窗口高度伸缩，行数随宽度自适应
        self._playing_idx = -1
        self._eq_frame = 0
        self._eq_timer = QTimer(self)
        self._eq_timer.setInterval(140)
        self._eq_timer.timeout.connect(self._eq_tick)
        self.bar2 = QProgressBar()
        self.bar2.setValue(0)
        tb.addWidget(self.bar2)
        self.download_btn = QPushButton("⬇ 下载选中（0）")
        self.download_btn.setObjectName("primaryBtn")
        self.download_btn.clicked.connect(self._download_selected)
        tb.addWidget(self.download_btn)
        rl.addWidget(table_box, 1)

        self.epgrid.show_all()

        log_box = QGroupBox("日志")
        lb = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        lb.addWidget(self.log_view)
        rl.addWidget(log_box, int(1))

        splitter.addWidget(right)
        # 左右两栏按比例伸缩（原写死 [560,640] 会把窗口最小宽度抬到 1200+）
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        self.main_splitter = splitter
        self.sniff_layout.addWidget(splitter, 1)

        # 加载生成页配置，并把三个页面挂到导航
        config.load_agnes_video_config()
        config.load_image_gen_config()
        config.load_segment_split_config()
        self._build_generate_page()
        self._build_script_page()
        self._build_asset_page()
        self.nav_tabs.addTab(self.script_page, "📖 剧本")
        self.nav_tabs.addTab(self.asset_page, "🎨 资产管理")
        self.nav_tabs.addTab(self._gen_episode_stk, "🎬 视频生成")
        self.nav_tabs.currentChanged.connect(self._on_nav_tab_changed)
        self.nav_tabs.setCurrentIndex(0)
        # 状态栏面包屑 + 日志计数初始化（切页时 _on_nav_tab_changed 负责刷新）
        self._update_breadcrumb()
        self._stack.addWidget(self.workspace_page)
        self._stack.setCurrentWidget(self.home_page)  # 默认进入项目首页

        self._ep_history_file = os.path.join(config._EXE_DIR, "episode_history.json")
        self._migrate_legacy_ep_list()
        self._refresh_project_cards()

        # 全局运行状态灯轮询（500ms 检查一次各任务是否在跑）
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._global_status_tick)
        self._status_timer.start()

    # ---- 已识别剧集历史持久化：重启后不丢失识别的链接 ----
    def _save_ep_list(self):
        try:
            items = self.epgrid.data_snapshot()
            with open(self._ep_history_file, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _on_ep_deleted(self, i):
        self._save_ep_list()
        self._log(f"已删除单集 #{i + 1}，识别列表已更新", "info")

    def _restore_ep_list(self):
        try:
            if not os.path.exists(self._ep_history_file):
                return
            with open(self._ep_history_file, "r", encoding="utf-8") as f:
                items = json.load(f)
            if not items:
                return
            self.media_repo = {}
            for it in items:
                url = it.get("url")
                if url:
                    self.media_repo[url] = {
                        "referer": it.get("referer", ""),
                        "is_m3u8": bool(it.get("is_m3u8")),
                        "title": it.get("title", ""),
                    }
            self.epgrid.load_from(items)
            self.epgrid.show_all()
            self._log(f"已恢复 {len(items)} 条已识别剧集", "ok")
        except Exception as e:
            self._log(f"恢复剧集历史失败: {e}", "warn")

    # =====================================================================
    # 项目化管理：首页 + 项目卡片（每个项目独立保存识别/分析/生成历史/下载目录）
    # =====================================================================
    def _project_dir(self, name):
        """项目数据目录。"""
        return os.path.join(self._projects_dir, str(name))

    def _load_projects(self):
        try:
            if os.path.exists(self._projects_file):
                with open(self._projects_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_projects(self, projects):
        try:
            with open(self._projects_file, "w", encoding="utf-8") as f:
                json.dump(projects, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _migrate_legacy_ep_list(self):
        """旧版全局剧集历史(episode_history.json)若存在，迁移为首个项目。"""
        try:
            if not os.path.exists(self._ep_history_file):
                return
            with open(self._ep_history_file, "r", encoding="utf-8") as f:
                items = json.load(f)
            if not items:
                return
            projects = self._load_projects()
            if not projects:
                name = "默认项目"
                pd = self._project_dir(name)
                os.makedirs(pd, exist_ok=True)
                with open(os.path.join(pd, "episodes.json"), "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=1)
                projects.append({"name": name, "created": time.strftime("%Y-%m-%d %H:%M")})
                self._save_projects(projects)
                self._log(f"已把旧识别记录迁移到项目「{name}」", "info")
        except Exception:
            pass

    def _new_project(self):
        name, ok = QInputDialog.getText(self, "新建项目", "项目名称：", text=f"项目{int(time.time()) % 10000}")
        if not ok or not (name or "").strip():
            return
        name = name.strip()
        projects = self._load_projects()
        existed = any(p.get("name") == name for p in projects)
        pd = self._project_dir(name)
        os.makedirs(pd, exist_ok=True)
        if not existed:
            projects.append({"name": name, "created": time.strftime("%Y-%m-%d %H:%M")})
            self._save_projects(projects)
        self._refresh_project_cards()
        self._open_project(name)

    def _delete_project(self, name):
        ret = QMessageBox.question(self, "删除项目", f"确定删除项目「{name}」及其全部数据吗？\n(识别列表/分析结果/生成历史/下载文件)不可恢复。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        projects = [p for p in self._load_projects() if p.get("name") != name]
        self._save_projects(projects)
        import shutil
        try:
            shutil.rmtree(self._project_dir(name), ignore_errors=True)
        except Exception:
            pass
        if self._current_project == name:
            self._current_project = None
        self._refresh_project_cards()
        self._log(f"已删除项目「{name}」", "info")

    def _refresh_project_cards(self):
        self.home_list.clear()
        projects = self._load_projects()
        if not projects:
            it = QListWidgetItem("　还没有项目，点击右上角「＋ 新建项目」开始。")
            it.setForeground(Qt.gray)
            it.setTextAlignment(Qt.AlignCenter)
            self.home_list.addItem(it)
            return
        for p in projects:
            name = p.get("name", "未命名")
            created = p.get("created", "")
            it = QListWidgetItem()
            it.setIcon(self._proj_icon())
            it.setText(f"{name}\n       创建于 {created}")
            it.setData(Qt.UserRole, name)
            it.setSizeHint(QSize(260, 78))
            self.home_list.addItem(it)

    def _proj_icon(self):
        if getattr(self, "_proj_pix", None) is None:
            from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPainterPath
            pm = QPixmap(72, 72)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(6, 6, 60, 60, 12, 12)
            p.fillPath(path, QColor("#2563eb"))
            p.setPen(QColor("white"))
            f = p.font()
            f.setPixelSize(30)
            p.setFont(f)
            p.drawText(6, 6, 60, 60, Qt.AlignCenter, "📁")
            p.end()
            self._proj_pix = QIcon(pm)
        return self._proj_pix

    def _on_home_item(self, item):
        name = item.data(Qt.UserRole)
        if name:
            self._open_project(name)

    def _home_menu(self, pos):
        item = self.home_list.itemAt(pos)
        if not item:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        m = QMenu(self)
        m.addAction("📂 打开").triggered.connect(lambda: self._open_project(name))
        m.addSeparator()
        m.addAction("✏️ 重命名").triggered.connect(lambda: self._rename_project(name))
        m.addAction("🗑 删除").triggered.connect(lambda: self._delete_project(name))
        m.exec_(self.home_list.mapToGlobal(pos))

    def _save_current_ctx(self):
        """把当前界面状态保存回当前项目。"""
        if not self._current_project:
            return
        pd = self._project_dir(self._current_project)
        os.makedirs(pd, exist_ok=True)
        try:
            # 识别剧集列表
            items = self.epgrid.data_snapshot()
            with open(os.path.join(pd, "episodes.json"), "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=1)
            # 输入框链接
            try:
                with open(os.path.join(pd, "input.txt"), "w", encoding="utf-8") as f:
                    f.write(self.input_edit.toPlainText())
            except Exception:
                pass
            # 视频分析结果文本
            try:
                with open(os.path.join(pd, "analysis.txt"), "w", encoding="utf-8") as f:
                    f.write(self.va_text.toPlainText())
            except Exception:
                pass
            # 当前剧集的生成区内容
            try:
                self._save_gen_areas_to_episode()
            except Exception:
                pass
            # 资产仓库
            try:
                self._asset_save()
            except Exception:
                pass
        except Exception:
            pass

    def _apply_project_ctx(self, name):
        """把当前界面切换为指定项目的内容。"""
        pd = self._project_dir(name)
        os.makedirs(pd, exist_ok=True)
        self._current_project = name
        # 数据路径指向项目目录
        self._ep_history_file = os.path.join(pd, "episodes.json")
        self._results_dir = os.path.join(pd, "分析结果")
        self._scripts_dir = os.path.join(pd, "剧本")
        self._gen_videos_dir = os.path.join(pd, "生成视频")
        os.makedirs(self._results_dir, exist_ok=True)
        os.makedirs(self._scripts_dir, exist_ok=True)
        os.makedirs(self._gen_videos_dir, exist_ok=True)
        # 资产仓库目录（人物/场景/道具，随项目隔离）
        self._assets_dir = os.path.join(pd, "资产仓库")
        os.makedirs(self._assets_dir, exist_ok=True)
        self._asset_load()
        # 初始化生成页剧集卡片系统
        self._gen_episode_init(name)
        if hasattr(self, "_gen_file_lay"):
            self._gen_files_refresh()
        # 剧集列表：直接加载项目 episodes.json
        items = []
        try:
            if os.path.exists(self._ep_history_file):
                with open(self._ep_history_file, "r", encoding="utf-8") as f:
                    items = json.load(f)
        except Exception:
            items = []
        self.media_repo = {}
        for it in items:
            url = it.get("url")
            if url:
                self.media_repo[url] = {
                    "referer": it.get("referer", ""),
                    "is_m3u8": bool(it.get("is_m3u8")),
                    "title": it.get("title", ""),
                }
        self.epgrid.load_from(items)
        self.epgrid.show_all()
        # 输入框
        try:
            ip = os.path.join(pd, "input.txt")
            if os.path.exists(ip):
                with open(ip, "r", encoding="utf-8") as f:
                    self.input_edit.setPlainText(f.read())
        except Exception:
            pass
        # 视频分析结果、字幕
        try:
            ap = os.path.join(pd, "analysis.txt")
            if os.path.exists(ap):
                with open(ap, "r", encoding="utf-8") as f:
                    self.va_text.setPlainText(f.read())
            else:
                self.va_text.clear()
        except Exception:
            self.va_text.clear()
        self.sub_text.clear()
        # 剧本页文件库跟随项目刷新
        try:
            self._script_refresh()
        except Exception:
            pass
        # 下载目录 → 项目下载目录
        try:
            dd = os.path.join(pd, "下载")
            os.makedirs(dd, exist_ok=True)
            self.dir_edit.setText(dd)
        except Exception:
            pass
        # 生成历史切换到项目
        try:
            self._gen_history_refresh()
        except Exception:
            pass
        self._log(f"已打开项目「{name}」", "ok")

    def _open_project(self, name):
        try:
            # 先保存上一个项目
            if self._current_project and self._current_project != name:
                self._save_current_ctx()
            self._apply_project_ctx(name)
        except Exception as e:
            self._log(f"打开项目「{name}」失败: {e}", "error")
        # 无论加载是否成功都进入工作区（嗅探 + 生成二级页面）
        self.proj_title.setText(f"📁 {name}")
        self.nav_tabs.setCurrentIndex(0)
        self._stack.setCurrentWidget(self.workspace_page)

    def _go_home(self):
        try:
            if self._current_project:
                self._save_current_ctx()
        except Exception:
            pass
        self.proj_title.setText("未打开项目")
        try:
            self._refresh_project_cards()
        except Exception:
            pass
        self._stack.setCurrentWidget(self.home_page)

    def _proj_back_clicked(self):
        """顶部返回按钮统一入口：视频生成页回剧集列表，其余页回首页。"""
        if getattr(self, "proj_back_target", "home") == "episode":
            self._gen_nav_back()
        else:
            self._go_home()

    def _on_nav_tab_changed(self, idx):
        """切页时联动顶部返回按钮的文案与行为（依据是否处于某集生成区）。"""
        self._refresh_top_back_btn()
        self._update_breadcrumb()
        # 视频生成页正停留在某分集时，切入「资产管理」页自动跳到同一分集的资产页，
        # 让两处“当前分集”保持联动（例：第10集视频生成页 → 点资产管理 → 资产的“资产·第10集”）
        try:
            on_asset = self.nav_tabs.widget(idx) is self.asset_page
        except Exception:
            on_asset = False
        if on_asset:
            target = getattr(self, "_current_gen_episode", None)
            ep_names = [e.get("name") for e in (getattr(self, "_gen_episodes", []) or [])]
            if target and target in ep_names and \
                    getattr(self, "_cur_asset_episode", None) != target:
                self._asset_ep_open(target)

    def _refresh_top_back_btn(self):
        """按当前导航页 + 剧集内部层级刷新顶部返回按钮：
        - 视频生成页且正停留在某集生成区(第2层) → 「← 返回剧集列表」
        - 其余 → 「← 返回首页」"""
        try:
            on_gen = self.nav_tabs.currentWidget() is self._gen_episode_stk
            in_ep_area = bool(on_gen and getattr(self, "_gen_episode_stk", None)
                              and self._gen_episode_stk.currentIndex() == 1)
            if in_ep_area:
                self.proj_back_target = "episode"
                self.proj_back.setText("← 返回剧集列表")
            else:
                self.proj_back_target = "home"
                self.proj_back.setText("← 返回首页")
        except Exception:
            self.proj_back_target = "home"
            self.proj_back.setText("← 返回首页")

    def _gen_nav_back(self):
        """视频生成页的返回：在生成区工作区时回剧集列表；在剧集列表时回首页。"""
        if getattr(self, "_gen_episode_stk", None) and self._gen_episode_stk.currentIndex() == 1:
            self._gen_episode_back()
        else:
            self._go_home()

    def _rename_project(self, old):
        name, ok = QInputDialog.getText(self, "重命名项目", "新名称：", text=old)
        name = (name or "").strip()
        if not ok or not name:
            return
        if name == old:
            return
        projects = self._load_projects()
        if any(p.get("name") == name for p in projects):
            QMessageBox.warning(self, "重命名项目", f"已存在同名项目「{name}」。")
            return
        for p in projects:
            if p.get("name") == old:
                p["name"] = name
        self._save_projects(projects)
        import shutil
        old_dir, new_dir = self._project_dir(old), self._project_dir(name)
        try:
            if os.path.exists(old_dir) and not os.path.exists(new_dir):
                shutil.move(old_dir, new_dir)
        except Exception:
            pass
        if self._current_project == old:
            self._current_project = name
            self.proj_title.setText(f"📁 {name}")
        self._refresh_project_cards()
        self._log(f"已重命名项目「{old}」→「{name}」", "ok")

    # =====================================================================
    # 剧本页（参考 openframe 富文本剧本工作台：左侧文件库 + 居中富文本编辑器）
    # 存放「视频分析结果」与「剧本」两类内容，嗅探页分析/转剧本后自动导入
    # =====================================================================
    def _build_script_page(self):
        p = QWidget()
        pp = QVBoxLayout(p)
        pp.setContentsMargins(12, 10, 12, 10)
        pp.setSpacing(8)

        head = QHBoxLayout()
        t = QLabel("📖 剧本工作台")
        t.setStyleSheet("font-size:15px; font-weight:800; color:#1e293b;")
        head.addWidget(t)
        cap = QLabel("视频分析结果 / 剧本 统一存放 · 富文本编辑")
        cap.setObjectName("cap")
        head.addWidget(cap)
        head.addStretch(1)
        self.script_status = QLabel("")
        self.script_status.setStyleSheet("color:#64748b;")
        head.addWidget(self.script_status)
        pp.addLayout(head)

        # 子 tab：视频嗅探 / 剧本编辑
        sub_tabs = QTabWidget()
        sub_tabs.addTab(self.sniff_page, "📡 视频嗅探")
        editor_tab = QWidget()
        el = QVBoxLayout(editor_tab)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(8)
        sub_tabs.addTab(editor_tab, "📖 剧本编辑")
        pp.addWidget(sub_tabs, 1)

        ops = QHBoxLayout()
        self.script_import_analysis = QPushButton("📥 导入分析结果")
        self.script_import_analysis.setObjectName("ghostBtn")
        self.script_import_analysis.setToolTip("把「视频嗅探」页当前分析结果导入剧本页（分析完成时也会自动导入）")
        self.script_import_analysis.clicked.connect(self._script_import_from_va)
        ops.addWidget(self.script_import_analysis)
        self.script_import_script = QPushButton("📥 导入剧本")
        self.script_import_script.setObjectName("ghostBtn")
        self.script_import_script.setToolTip("把「视频嗅探」页当前生成的剧本导入剧本页（生成时也会自动导入）")
        self.script_import_script.clicked.connect(self._script_import_from_script)
        ops.addWidget(self.script_import_script)
        self.script_save = QPushButton("💾 保存")
        self.script_save.setObjectName("accentBtn")
        self.script_save.setToolTip("保存当前编辑内容到左侧选中的文件（未选中则新建）")
        self.script_save.clicked.connect(self._script_save)
        ops.addWidget(self.script_save)
        self.script_export = QPushButton("📤 导出 txt")
        self.script_export.setObjectName("ghostBtn")
        self.script_export.clicked.connect(self._script_export)
        ops.addWidget(self.script_export)
        self.script_clear = QPushButton("🗑 清空")
        self.script_clear.setObjectName("ghostBtn")
        self.script_clear.clicked.connect(self._script_clear)
        ops.addWidget(self.script_clear)
        ops.addStretch(1)
        pp.addLayout(ops)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 左侧：文件库（视频分析结果 / 剧本 两组）
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        lib_lbl = QLabel("📚 项目文件库")
        lib_lbl.setStyleSheet("font-weight:700; color:#334155;")
        ll.addWidget(lib_lbl)
        self.script_list = QListWidget()
        self.script_list.setObjectName("scriptList")
        self.script_list.setMinimumWidth(200)
        self.script_list.setMaximumWidth(280)
        self.script_list.itemClicked.connect(self._script_on_list)
        self.script_list.itemDoubleClicked.connect(self._script_on_list)
        self.script_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.script_list.customContextMenuRequested.connect(self._script_list_menu)
        ll.addWidget(self.script_list, 1)
        splitter.addWidget(left)

        # 右侧：格式工具栏 + 富文本编辑器（居中、宽松行距，仿 openframe）
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        fmt = QHBoxLayout()
        fmt.addWidget(QLabel("格式:"))
        for label, fn in (("H1", "_script_fmt_h1"), ("H2", "_script_fmt_h2"),
                          ("B", "_script_fmt_bold"), ("I", "_script_fmt_italic"),
                          ("• 列表", "_script_fmt_ul"), ("1. 列表", "_script_fmt_ol"),
                          ("❝ 引用", "_script_fmt_quote")):
            b = QPushButton(label)
            b.setObjectName("ghostBtn")
            b.setFixedHeight(26)
            b.setStyleSheet("padding:2px 10px; font-weight:700;")
            b.clicked.connect(lambda _=False, _fn=fn: getattr(self, _fn)())
            fmt.addWidget(b)
        fmt.addStretch(1)
        rl.addLayout(fmt)
        self.script_edit = QTextEdit()
        self.script_edit.setObjectName("scriptEdit")
        self.script_edit.setAcceptRichText(True)
        doc = self.script_edit.document()
        bf = QTextBlockFormat()
        bf.setLineHeight(150, 1)  # 1 = ProportionalHeight
        doc.setDefaultFont(QFont("Microsoft YaHei UI", 13))
        self.script_edit.setStyleSheet(
            "QTextEdit#scriptEdit { background:#ffffff; border:1px solid #dbe3f0; border-radius:10px;"
            " padding:24px 32px; font-size:14px; }")
        rl.addWidget(self.script_edit, 1)
        splitter.addWidget(right)
        splitter.setSizes([240, 900])
        el.addWidget(splitter, 1)

        self.script_page = p
        self._script_items = []   # [(kind, path, name)]
        self._script_refresh()

    # ---- 剧本页：文件库 ----
    def _script_refresh(self):
        """扫描项目「剧本」「分析结果」目录，重建左侧文件库（剧本在前，可折叠）。"""
        self.script_list.clear()
        self._script_items = []
        self._script_groups = {}   # kind -> {"dir", "items": [...], "collapsed": bool}
        groups = [("📜 剧本", self._scripts_dir),
                  ("📂 视频分析结果", self._results_dir)]
        for kind, d in groups:
            if not d or not os.path.isdir(d):
                continue
            files = sorted(os.listdir(d))
            files = [f for f in files if f.lower().endswith(".txt")]
            g = QListWidgetItem("▼ " + kind)
            g.setFlags(Qt.ItemIsEnabled)
            g.setData(Qt.UserRole, ("group", kind, d, ""))
            font = g.font(); font.setBold(True)
            g.setFont(font)
            g.setForeground(QColor("#2563eb"))
            self.script_list.addItem(g)
            file_items = []
            for f in files:
                it = QListWidgetItem(f)
                it.setData(Qt.UserRole, ("file", kind, os.path.join(d, f), f))
                file_items.append(it)
                self._script_items.append((kind, os.path.join(d, f), f))
                self.script_list.addItem(it)
            self._script_groups[kind] = {"dir": d, "items": file_items, "collapsed": False}
        self.script_status.setText(f"共 {len(self._script_items)} 个文件")

    def _script_on_list(self, item):
        data = item.data(Qt.UserRole)
        if not data:
            return
        if data[0] == "group":
            # 点击分组标题 → 折叠 / 展开
            kind = data[1]
            g = self._script_groups.get(kind)
            if not g:
                return
            g["collapsed"] = not g["collapsed"]
            for it in g["items"]:
                it.setHidden(g["collapsed"])
            item.setText(("▶ " if g["collapsed"] else "▼ ") + kind)
            return
        kind, path, name = data[1], data[2], data[3]
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self._log(f"读取 {name} 失败: {e}", "warn")
            return
        self.script_edit.setHtml(self._text_to_html(content))
        self.script_status.setText(f"已打开 {kind} · {name}")

    def _script_list_menu(self, pos):
        item = self.script_list.itemAt(pos)
        if not item:
            return
        data = item.data(Qt.UserRole)
        if not data or data[0] != "file":
            return
        kind, path, name = data[1], data[2], data[3]
        menu = QMenu(self)
        a_rename = menu.addAction("✏️ 重命名")
        a_delete = menu.addAction("🗑 删除")
        act = menu.exec_(self.script_list.mapToGlobal(pos))
        if act == a_rename:
            self._script_rename_file(kind, path, name)
        elif act == a_delete:
            self._script_delete_file(kind, path, name)

    def _script_rename_file(self, kind, path, name):
        base = os.path.splitext(name)[0]
        new_name, ok = QInputDialog.getText(self, "重命名", "新文件名（不含 .txt）:", text=base)
        if not ok or not new_name.strip():
            return
        new_name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", new_name.strip()).strip()[:60] or "未命名"
        new_path = os.path.join(os.path.dirname(path), f"{new_name}.txt")
        if new_path == path:
            return
        if os.path.exists(new_path):
            self._log(f"已存在同名文件: {new_name}.txt", "warn")
            return
        try:
            os.rename(path, new_path)
            self._log(f"已重命名: {name} → {new_name}.txt", "ok")
            self._script_refresh()
        except Exception as e:
            self._log(f"重命名失败: {e}", "warn")

    def _script_delete_file(self, kind, path, name):
        ret = QMessageBox.question(self, "删除文件", f"确定删除「{name}」吗？",
                                   QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        try:
            os.remove(path)
            self._log(f"已删除: {name}", "ok")
            self._script_refresh()
        except Exception as e:
            self._log(f"删除失败: {e}", "warn")

    def _script_add_file(self, kind, name, content):
        """把内容写入对应目录并刷新文件库，返回文件路径。"""
        d = self._results_dir if kind.startswith("📂") else self._scripts_dir
        try:
            os.makedirs(d, exist_ok=True)
            safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "未命名")).strip()[:60] or "未命名"
            path = os.path.join(d, f"{safe}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content or "")
            self._script_refresh()
            # 选中新文件并载入
            for i in range(self.script_list.count()):
                it = self.script_list.item(i)
                data = it.data(Qt.UserRole)
                if data and data[0] == "file" and data[2] == path:
                    self.script_list.setCurrentItem(it)
                    self._script_on_list(it)
                    break
            return path
        except Exception as e:
            self._log(f"写入 {kind} 失败: {e}", "warn")
            return None

    def _script_import_from_va(self):
        txt = self.va_text.toPlainText().strip()
        if not txt:
            self._log("「视频嗅探」页暂无分析结果，请先完成视频分析", "warn")
            return
        title = self._va_wtitle or time.strftime("%Y%m%d_%H%M%S")
        self._script_add_file("📂 视频分析结果", f"{title}_分析结果", txt)
        self._log(f"已导入分析结果到剧本页: {title}", "ok")

    def _script_import_from_script(self):
        txt = self.va_text.toPlainText().strip()
        if not txt:
            self._log("「视频嗅探」页暂无剧本内容，请先一键转剧本", "warn")
            return
        title = self._va_wtitle or time.strftime("%Y%m%d_%H%M%S")
        self._script_add_file("📜 剧本", f"{title}_剧本", txt)
        self._log(f"已导入剧本到剧本页: {title}", "ok")

    def _script_save(self):
        cur = self.script_list.currentItem()
        data = cur.data(Qt.UserRole) if cur else None
        content = self.script_edit.toPlainText()
        if data and data[0] != "group" and data[1]:
            try:
                with open(data[1], "w", encoding="utf-8") as f:
                    f.write(content)
                self._log(f"已保存: {data[2]}", "ok")
                self.script_status.setText(f"已保存 {data[2]}")
            except Exception as e:
                self._log(f"保存失败: {e}", "warn")
            return
        # 未选中文件 → 另存为
        path, _ = QFileDialog.getSaveFileName(
            self, "保存剧本", os.path.join(self._scripts_dir, f"剧本_{time.strftime('%Y%m%d_%H%M%S')}.txt"),
            "文本 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._log(f"已保存: {os.path.basename(path)}", "ok")
            self._script_refresh()
        except Exception as e:
            self._log(f"保存失败: {e}", "warn")

    def _script_export(self):
        content = self.script_edit.toPlainText().strip()
        if not content:
            self._log("暂无可导出的内容", "warn")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出剧本", os.path.join(self._scripts_dir, f"剧本_{time.strftime('%Y%m%d_%H%M%S')}.txt"),
            "文本 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._log(f"已导出: {path}", "ok")
        except Exception as e:
            self._log(f"导出失败: {e}", "warn")

    def _script_clear(self):
        self.script_edit.clear()
        self.script_status.setText("已清空编辑器")

    # ---- 剧本页：纯文本 → 富文本 ----
    def _text_to_html(self, text):
        import html as _html
        lines = (text or "").split("\n")
        out = []
        in_list = False
        for raw in lines:
            s = raw.strip()
            if not s:
                if in_list:
                    out.append("</ul>"); in_list = False
                out.append("<p>&nbsp;</p>")
                continue
            if re.match(r'^={3,}', s) or re.match(r'^—{2,}', s):
                if in_list:
                    out.append("</ul>"); in_list = False
                out.append(f"<h2 style='color:#1e3a8a;'>{_html.escape(s)}</h2>")
                continue
            if re.match(r'^\[\d{1,2}:\d{2}', s):
                if in_list:
                    out.append("</ul>"); in_list = False
                out.append(f"<blockquote style='color:#475569; border-left:3px solid #93c5fd;"
                           f" padding-left:10px; margin:4px 0;'>{_html.escape(s)}</blockquote>")
                continue
            m = re.match(r'^[-*•]\s+(.*)$', s)
            if m:
                if not in_list:
                    out.append("<ul>"); in_list = True
                out.append(f"<li>{_html.escape(m.group(1))}</li>")
                continue
            if re.match(r'^\d+[\.、]\s+', s):
                if in_list:
                    out.append("</ul>"); in_list = False
                out.append(f"<p><b>{_html.escape(s)}</b></p>")
                continue
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{_html.escape(s)}</p>")
        if in_list:
            out.append("</ul>")
        return "\n".join(out)

    # ---- 剧本页：格式工具栏 ----
    def _script_fmt_h1(self):
        self._script_wrap_block("h1")
    def _script_fmt_h2(self):
        self._script_wrap_block("h2")
    def _script_fmt_bold(self):
        self._script_toggle_char(QTextCharFormat.FontWeight, QFont.Bold)
    def _script_fmt_italic(self):
        self._script_toggle_char(QTextCharFormat.FontItalic, True)
    def _script_fmt_ul(self):
        self._script_toggle_list(QTextListFormat.ListDisc)
    def _script_fmt_ol(self):
        self._script_toggle_list(QTextListFormat.ListDecimal)
    def _script_fmt_quote(self):
        self._script_wrap_block("quote")

    def _script_wrap_block(self, kind):
        c = self.script_edit.textCursor()
        c.beginEditBlock()
        if kind == "h1":
            fmt = QTextBlockFormat(); fmt.setHeadingLevel(1)
            c.mergeBlockFormat(fmt)
            cf = QTextCharFormat(); cf.setFontPointSize(20); cf.setFontWeight(QFont.Bold)
            c.mergeCharFormat(cf)
        elif kind == "h2":
            fmt = QTextBlockFormat(); fmt.setHeadingLevel(2)
            c.mergeBlockFormat(fmt)
            cf = QTextCharFormat(); cf.setFontPointSize(16); cf.setFontWeight(QFont.Bold)
            c.mergeCharFormat(cf)
        elif kind == "quote":
            fmt = QTextBlockFormat()
            fmt.setIndent(1)
            c.mergeBlockFormat(fmt)
            cf = QTextCharFormat(); cf.setForeground(QColor("#475569"))
            c.mergeCharFormat(cf)
        c.endEditBlock()
        self.script_edit.setTextCursor(c)
        self.script_edit.setFocus()

    def _script_toggle_char(self, prop, val):
        c = self.script_edit.textCursor()
        cf = QTextCharFormat()
        if prop == QTextCharFormat.FontWeight:
            cur = c.charFormat().fontWeight()
            cf.setFontWeight(QFont.Normal if cur == QFont.Bold else QFont.Bold)
        else:
            cur = c.charFormat().fontItalic()
            cf.setFontItalic(not cur)
        c.mergeCharFormat(cf)
        self.script_edit.setTextCursor(c)
        self.script_edit.setFocus()

    def _script_toggle_list(self, style):
        c = self.script_edit.textCursor()
        c.beginEditBlock()
        fmt = QTextListFormat()
        fmt.setStyle(style)
        c.createList(fmt)
        c.endEditBlock()
        self.script_edit.setTextCursor(c)
        self.script_edit.setFocus()

    # =====================================================================
    # 资产管理页（提取人物/场景/道具 → 一键生图 → 自动填充参考图槽）
    # =====================================================================
    def _build_asset_ep_list_page(self):
        """资产管理 · 第0页：分集卡片列表（与视频生成剧集共用同一套分集，点击进入单集资产）。"""
        p = QWidget()
        lay = QVBoxLayout(p)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        hd = QHBoxLayout()
        t = QLabel("🎨 资产管理 · 选择分集")
        t.setStyleSheet("font-size:15px; font-weight:800; color:#1e293b;")
        hd.addWidget(t)
        cap = QLabel("分集与视频生成剧集共用，资产集间互通：同名资产自动复用")
        cap.setObjectName("cap")
        hd.addWidget(cap)
        hd.addStretch(1)
        lay.addLayout(hd)

        self._asset_ep_scroll = QScrollArea()
        self._asset_ep_scroll.setWidgetResizable(True)
        self._asset_ep_scroll.setFrameShape(QFrame.NoFrame)
        self._asset_ep_wrap = QWidget()
        self._asset_ep_flow = FlowLayout(self._asset_ep_wrap, margin=4, hspacing=10, vspacing=10)
        self._asset_ep_scroll.setWidget(self._asset_ep_wrap)
        lay.addWidget(self._asset_ep_scroll, 1)

        self._asset_ep_cards = []
        self._asset_ep_empty = QLabel("暂无分集\n请先到「🎬 视频生成」创建剧集，或提取资产")
        self._asset_ep_empty.setAlignment(Qt.AlignCenter)
        self._asset_ep_empty.setStyleSheet("color:#94a3b8; font-size:14px;")
        lay.addWidget(self._asset_ep_empty)
        return p

    def _asset_ep_render_cards(self):
        """重渲染资产分集卡片列表（读取视频生成剧集）。"""
        for c in self._asset_ep_cards:
            c.deleteLater()
        self._asset_ep_cards.clear()
        while self._asset_ep_flow.count():
            it = self._asset_ep_flow.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        eps = getattr(self, "_gen_episodes", []) or []
        if not eps:
            self._asset_ep_empty.setVisible(True)
            self._asset_ep_scroll.setVisible(False)
            return
        self._asset_ep_empty.setVisible(False)
        self._asset_ep_scroll.setVisible(True)
        for ep in eps:
            name = ep.get("name", "未命名")
            card = AssetEpCard(name)
            card.opened.connect(lambda n=name: self._asset_ep_open(n))
            self._asset_ep_cards.append(card)
            self._asset_ep_flow.addWidget(card)
        self._asset_ep_flow.invalidate()
        self._asset_ep_wrap.updateGeometry()

    def _asset_ep_open(self, name):
        """进入某分集的资产管理。"""
        self._cur_asset_episode = name
        try:
            if getattr(self, "_asset_stk", None):
                self._asset_stk.setCurrentIndex(1)
        except Exception:
            pass
        try:
            self._asset_ep_title.setText("资产 · %s" % name)
        except Exception:
            pass
        self._asset_ep_header_vis()
        self._asset_reload()
        self._asset_status("当前分集：%s（资产与其他集互通，同名自动复用）" % name)

    def _asset_ep_back_nav(self):
        """返回资产分集卡片列表。"""
        self._cur_asset_episode = None
        try:
            if getattr(self, "_asset_stk", None):
                self._asset_stk.setCurrentIndex(0)
        except Exception:
            pass
        self._asset_ep_render_cards()

    def _asset_ep_header_vis(self):
        """单集页面顶部按当前是否为项目内分集调整返回按钮可用性。"""
        try:
            in_project = bool(getattr(self, "_assets_dir", ""))
            self._asset_ep_back.setEnabled(True)
            self._asset_ep_back.setVisible(True)
        except Exception:
            pass

    def _asset_visible_items(self):
        """当前资产编辑页可见项：已进入某分集→只显示该集相关（episodes含当前集或全局空eps）；未进入→显示全部。"""
        cur = self._cur_asset_episode
        if not cur:
            return self._asset_items
        out = []
        for it in self._asset_items:
            eps = it.get("episodes") or []
            if cur in eps or not eps:
                out.append(it)
        return out

    def _build_asset_page(self):
        p = QWidget()
        root_lay = QVBoxLayout(p)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)
        # 两级结构：第0页=分集卡片列表，第1页=单集资产内容
        self._asset_stk = QStackedWidget()
        root_lay.addWidget(self._asset_stk)
        self._asset_stk.addWidget(self._build_asset_ep_list_page())

        # ── 第1页：单集资产内容（原资产管理三分类+提取+预览）──
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 单集标题栏（当前集名 + 返回分集列表）
        self._asset_ep_header = QHBoxLayout()
        self._asset_ep_header.setSpacing(6)
        self._asset_ep_back = QPushButton("← 返回分集列表")
        self._asset_ep_back.setObjectName("ghostBtn")
        self._asset_ep_back.setCursor(Qt.PointingHandCursor)
        self._asset_ep_back.clicked.connect(self._asset_ep_back_nav)
        self._asset_ep_header.addWidget(self._asset_ep_back)
        self._asset_ep_title = QLabel("资产 · 第1集")
        self._asset_ep_title.setStyleSheet("font-size:14px; font-weight:800; color:#1e293b;")
        self._asset_ep_header.addWidget(self._asset_ep_title)
        self._asset_ep_header.addStretch(1)
        layout.addLayout(self._asset_ep_header)

        h = WrapFlowLayout(hspacing=6, vspacing=6)
        _atip = QLabel("🎨 资产管理 · 人物/场景/道具 资产仓库 · 手动上传或从剧本/分镜一键提取，AI 生图复用为参考图")
        _atip.setWordWrap(True)
        h.addWidget(_atip)
        self.asset_pick_btn = QPushButton("📂 选择分析结果")
        self.asset_pick_btn.setObjectName("ghostBtn")
        self.asset_pick_btn.clicked.connect(self._asset_pick_source)
        h.addWidget(self.asset_pick_btn)
        self.asset_extract_btn = QPushButton("⚡ 一键提取资产")
        self.asset_extract_btn.setObjectName("accentBtn")
        self.asset_extract_btn.clicked.connect(self._asset_extract)
        h.addWidget(self.asset_extract_btn)
        self.asset_script_btn = QPushButton("📜 从剧本/分镜提取")
        self.asset_script_btn.setObjectName("ghostBtn")
        self.asset_script_btn.clicked.connect(self._asset_extract_from_scripts)
        h.addWidget(self.asset_script_btn)
        self.asset_gen_btn = QPushButton("🖼 生成全部")
        self.asset_gen_btn.setObjectName("accentBtn")
        self.asset_gen_btn.clicked.connect(self._asset_gen_all)
        h.addWidget(self.asset_gen_btn)
        _vsep_asset = QFrame(); _vsep_asset.setObjectName("vsep"); _vsep_asset.setFrameShape(QFrame.VLine)
        h.addWidget(_vsep_asset)
        self.asset_more_btn = QToolButton()
        self.asset_more_btn.setObjectName("ghostBtn")
        self.asset_more_btn.setText("⋯ 更多")
        self.asset_more_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.asset_more_btn.setToolTip("刷新 / 绑定本地图片 / AI 服务")
        self.asset_more_menu = QMenu(self.asset_more_btn)
        _mr1 = self.asset_more_menu.addAction("🔄 刷新资产列表")
        _mr1.triggered.connect(self._asset_refresh_tree)
        _mr2 = self.asset_more_menu.addAction("📂 绑定本地图片")
        _mr2.triggered.connect(lambda: self._asset_scan_local_images(silent=False))
        _mr2.setToolTip("把「资产仓库/images」中已下载的图片绑定到资产：先按文件名自动匹配，剩余图片弹窗手动对应")
        _mr3 = self.asset_more_menu.addAction("⚙ AI 服务")
        _mr3.triggered.connect(lambda: self._ai_services_settings(2))
        self.asset_more_btn.setMenu(self.asset_more_menu)
        self.asset_more_btn.setPopupMode(QToolButton.InstantPopup)
        h.addWidget(self.asset_more_btn)
        _hwrap = QWidget(); _hwrap.setLayout(h)
        layout.addWidget(_hwrap)

        # 三分类卡片网格（人物 / 场景 / 道具）
        self._asset_lists = {}
        grid_row = QHBoxLayout()
        grid_row.setSpacing(6)
        for tkey, ticon in [("人物", "👤"), ("场景", "🏠"), ("道具", "🎒")]:
            box = QGroupBox("%s %s" % (ticon, tkey))
            box.setStyleSheet(
                "QGroupBox{font-weight:700; color:#1e293b; border:1px solid #e2e8f0; border-radius:8px;"
                " margin-top:10px; background:#ffffff;}"
                "QGroupBox::title{subcontrol-origin:margin; left:10px; padding:0 4px;}")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(4, 4, 4, 4)
            bl.setSpacing(4)
            lst = QListWidget()
            lst.setViewMode(QListView.IconMode)
            lst.setFlow(QListView.LeftToRight)
            lst.setWrapping(True)
            lst.setResizeMode(QListView.Fixed)
            lst.setIconSize(QSize(72, 72))
            lst.setSpacing(4)
            lst.setProperty("asset_grid_maxcols", 5)
            lst.installEventFilter(self)
            lst.setStyleSheet(
                "QListWidget{background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px;}"
                "QListWidget::item{border:none; padding:2px; background:transparent;}"
                "QListWidget::item:hover{background:#eef4ff; border-radius:6px;}"
                "QListWidget::item:selected{background:#dbeafe; border-radius:6px;}")
            lst.itemClicked.connect(lambda it, _t=tkey: self._asset_on_select(it, _t))
            lst.itemDoubleClicked.connect(lambda it, _t=tkey: self._asset_on_select(it, _t))
            lst.setContextMenuPolicy(Qt.CustomContextMenu)
            lst.customContextMenuRequested.connect(lambda pos, _t=tkey: self._asset_grid_menu(pos, _t))
            bl.addWidget(lst, 1)
            box.setMinimumHeight(150)
            grid_row.addWidget(box, 1)
            self._asset_lists[tkey] = lst
        self._asset_adding = False   # 防止连续点击「+」叠加多个添加对话框
        layout.addLayout(grid_row, 1)

        paste_h = QHBoxLayout()
        paste_h.setSpacing(8)
        self.asset_paste_edit = QTextEdit()
        self.asset_paste_edit.setPlaceholderText("📋 在此粘贴剧本/分析文本，一键提取人物/场景/道具资产（与「选择分析结果→一键提取」同一套 AI 提示词）")
        self.asset_paste_edit.setMinimumHeight(120)
        self.asset_paste_edit.setStyleSheet("background:#fbfdff; border:1px solid #d1d9e6; border-radius:8px; padding:6px; font-size:12px; color:#1e293b;")
        self.asset_paste_edit.setAcceptRichText(False)
        paste_h.addWidget(self.asset_paste_edit, 2)
        # 右侧信息框：提取动态（准备中/各阶段/完成/失败原因）+ 资产卡片描述（占粘贴框匀出的 1/3 宽度）
        self.asset_info_edit = QTextEdit()
        self.asset_info_edit.setPlaceholderText("提取动态与资产描述将显示在这里")
        self.asset_info_edit.setReadOnly(True)
        self.asset_info_edit.setStyleSheet("background:#f8fafc; border:1px solid #d1d9e6; border-radius:8px; padding:6px; font-size:12px; color:#475569;")
        paste_h.addWidget(self.asset_info_edit, 1)
        right = QVBoxLayout()
        right.setSpacing(8)
        self.asset_paste_extract_btn = QPushButton("⚡ 一键提取资产")
        self.asset_paste_extract_btn.setObjectName("accentBtn")
        self.asset_paste_extract_btn.clicked.connect(self._asset_extract_from_paste)
        right.addWidget(self.asset_paste_extract_btn)
        self.asset_paste_clear_btn = QPushButton("🗑 清空")
        self.asset_paste_clear_btn.setObjectName("ghostBtn")
        self.asset_paste_clear_btn.clicked.connect(lambda: self.asset_paste_edit.clear())
        right.addWidget(self.asset_paste_clear_btn)
        right.addStretch(1)
        paste_h.addLayout(right)
        layout.addLayout(paste_h)

        # 右侧原有的横向小预览区（保留但折叠进状态栏提示），生成单资产图移入右键菜单
        self.asset_preview_label = QLabel("")
        self.asset_preview_label.setVisible(False)
        self.asset_gen_single_btn = QPushButton("🖼 生成此资产图")
        self.asset_gen_single_btn.setObjectName("accentBtn")
        self.asset_gen_single_btn.clicked.connect(self._asset_gen_single)
        self.asset_gen_single_btn.setVisible(False)

        # 将单集内容页加入两级结构
        self._asset_stk.addWidget(content)
        self._asset_stk.setCurrentIndex(0)

        self.asset_page = p
        self._asset_items = []
        self._asset_selected = None
        self._asset_source_text = ""
        self._asset_src_path = ""
        self._assets_dir = ""
        self._cur_asset_episode = None
        # 资产生成图后台队列状态
        self._asset_img_queue = []
        self._asset_img_index = 0
        self._asset_img_total = 0
        self._asset_img_ok = 0
        self._asset_img_worker = None
        self._asset_img_current = None

    def _asset_pick_source(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择分析结果", self._results_dir,
            "分析结果 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._asset_source_text = f.read()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", str(e))
            return
        self._asset_src_path = path
        self.asset_pick_btn.setText("已选: " + os.path.basename(path))
        self._asset_status("已加载「%s」，请点击「一键提取资产」" % os.path.basename(path))

    def _asset_extract_from_paste(self):
        """粘贴文本 → 一键提取资产：与「选择分析结果→一键提取」同一套 AI 提示词/本地兜底。"""
        text = self.asset_paste_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "提示", "请先在下方文本框粘贴剧本/分析文本")
            return
        self._asset_source_text = text
        self._asset_status("已加载粘贴文本（%d 字），点击「一键提取资产」提取人物/场景/道具" % len(text))
        self._asset_extract()

    def _asset_extract(self):
        """一键提取资产：优先 AI 提取（人物/场景/道具）；未配置 AI Key 时询问是否本地规则兜底。"""
        if not self._asset_source_text:
            QMessageBox.warning(self, "提示", "请先选择分析结果文件，或点击「📜 从剧本/分镜提取」")
            return
        key = config.GLM_AI.get("api_key", "")
        if not key:
            ret = QMessageBox.question(
                self, "未配置 AI",
                "尚未配置文本分析 AI Key（「⚙ AI 服务 → 视频分析」），无法进行智能提取。\n"
                "是否使用本地规则粗略提取？（人物/场景/道具识别较粗糙）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret == QMessageBox.Yes:
                self._asset_extract_local()
            return
        model = config.GLM_AI.get("model") or "glm-5.3-flash"
        # base_url 按模型自动切换对应服务商（智谱 GLM / Agnes 中国站 / Agnes 国际站）
        base = config.GLM_AI.get("base_url") or config.base_url_for(model)
        self.asset_extract_btn.setEnabled(False)
        self.asset_script_btn.setEnabled(False)
        self.asset_paste_extract_btn.setEnabled(False)
        self._asset_status("AI 提取准备中…")
        w = AssetAiWorker(key, base, model, self._asset_source_text, parent=self)
        w.progress.connect(self._asset_status)
        w.done.connect(self._asset_ai_done)
        w.failed.connect(self._asset_ai_failed)
        self._asset_ai_worker = w   # 保持引用防回收
        w.start()

    def _asset_ai_done(self, data):
        self.asset_extract_btn.setEnabled(True)
        self.asset_script_btn.setEnabled(True)
        self.asset_paste_extract_btn.setEnabled(True)
        try:
            chars = data.get("characters") or []
            scenes = data.get("scenes") or []
            props = data.get("props") or []
            items = []
            seen = set()
            # 人物：prompt 由外貌描述包装成「角色定妆照」图生提示词
            for c in chars:
                name = str(c.get("name") or "").strip()
                if not name:
                    continue
                key_ = ("人物", name)
                if key_ in seen:
                    continue
                seen.add(key_)
                app = str(c.get("appearance") or "").strip()
                prompt = ("%s。角色定妆照，写实，人像摄影，简洁纯色背景，无场景无杂物。"
                          % app) if app else ("%s 角色定妆照，写实，人像摄影，纯色背景" % name)
                items.append({"name": name, "type": "人物", "prompt": prompt,
                              "image": None, "role": str(c.get("role") or ""),
                              "appearance": app,
                              "description": str(c.get("description") or "")})
            # 场景：prompt 直接使用 AI 生成的纯背景提示词
            for sc in scenes:
                name = str(sc.get("name") or "").strip()
                prompt = str(sc.get("prompt") or "").strip()
                if not name or not prompt:
                    continue
                key_ = ("场景", name)
                if key_ in seen:
                    continue
                seen.add(key_)
                items.append({"name": name, "type": "场景", "prompt": prompt,
                              "image": None, "description": ""})
            # 道具：prompt 使用 AI 生成的白模照 image_prompt
            for p in props:
                name = str(p.get("name") or "").strip()
                prompt = str(p.get("image_prompt") or "").strip()
                if not name or not prompt:
                    continue
                key_ = ("道具", name)
                if key_ in seen:
                    continue
                seen.add(key_)
                items.append({"name": name, "type": "道具", "prompt": prompt,
                              "image": None,
                              "description": str(p.get("description") or "")})
            self._asset_merge_extracted(items)
            summary = ("AI 提取完成：人物%d / 场景%d / 道具%d（已与现有资产去重复用）"
                       % (len(chars), len(scenes), len(props)))
            self._asset_info(summary, "ok")
            self._log("AI 资产提取：人物%d / 场景%d / 道具%d"
                      % (len(chars), len(scenes), len(props)), "ok")
        except Exception as e:
            self._asset_info("AI 提取处理结果出错: %s" % e, "error")
            self._log("AI 资产提取出错: %s" % e, "error")

    def _asset_ai_failed(self, err):
        self.asset_extract_btn.setEnabled(True)
        self.asset_script_btn.setEnabled(True)
        self.asset_paste_extract_btn.setEnabled(True)
        self._asset_info("AI 提取失败：%s" % err, "error")
        self._log("AI 资产提取失败: %s" % err, "error")
        QMessageBox.warning(self, "AI 提取失败", "提取失败：%s" % err)

    def _asset_extract_local(self):
        """本地规则提取（未配置 AI 时的兜底，保持旧行为）"""
        if not self._asset_source_text:
            QMessageBox.warning(self, "提示", "请先选择分析结果文件")
            return
        from gen_area import parse_storyboard_report
        report = parse_storyboard_report(self._asset_source_text)

        chars = []
        scenes = []
        props = []

        lines = self._asset_source_text.split("\n")
        in_chars = False
        in_scenes = False
        in_props = False
        for line in lines:
            if "人物表" in line or "人物名称" in line:
                in_chars, in_scenes, in_props = True, False, False
                continue
            if "场景" in line or "环境" in line:
                in_chars, in_scenes, in_props = False, True, False
                continue
            if "道具" in line or "服饰" in line:
                in_chars, in_scenes, in_props = False, False, True
                continue
            if line.startswith("- ") or line.startswith("* ") or line.startswith("• "):
                item = line[2:].strip()
                if not item:
                    continue
                if in_chars:
                    chars.append(item)
                elif in_scenes:
                    scenes.append(item)
                elif in_props:
                    props.append(item)
            elif line.startswith("#") and not line.startswith("###"):
                in_chars, in_scenes, in_props = False, False, False

        # 也尝试从 segments 的对话中识别角色名
        for seg in (report.get("segments") or []):
            if len(seg) > 3 and seg[3]:
                speaker = seg[3].get("speaker", "")
                if speaker and speaker not in chars:
                    chars.append(speaker)

        # 从画面分析中提取场景关键词
        for seg in (report.get("segments") or []):
            if len(seg) > 2 and seg[2]:
                ctx = seg[2]
                if "室内" in ctx or "客厅" in ctx or "房间" in ctx:
                    if "室内场景" not in scenes:
                        scenes.append("室内场景（根据剧情）")
                if "室外" in ctx or "街道" in ctx:
                    if "室外场景" not in scenes:
                        scenes.append("室外场景（根据剧情）")

        # 若段落式提取为空，尝试剧本/分镜正则提取（角色名：台词、场景:xx、道具:xx）
        if not chars and not scenes and not props:
            chars = self._regex_extract_chars(self._asset_source_text)
            scenes = self._regex_extract_items(
                self._asset_source_text, ("场景", "地点", "环境", "布景"))
            props = self._regex_extract_items(
                self._asset_source_text, ("道具", "物品", "服装", "服饰"))

        new_items = []
        for name in chars:
            new_items.append({"name": name, "type": "人物",
                              "prompt": "人物: %s，写实风格，高清人像摄影" % name, "image": None})
        for name in scenes:
            new_items.append({"name": name, "type": "场景",
                              "prompt": "场景: %s，电影级布光，真实摄影" % name, "image": None})
        for name in props:
            new_items.append({"name": name, "type": "道具",
                              "prompt": "道具: %s，写实风格，高清细节" % name, "image": None})

        self._asset_merge_extracted(new_items)
        self._asset_status("已提取 %d 个资产（人物%d/场景%d/道具%d，已与现有资产去重复用）" % (
            len(chars)+len(scenes)+len(props), len(chars), len(scenes), len(props)))

    def _asset_fuzzy_match_image(self, name, want_type=""):
        """在资产仓库 images 目录按资产名做模糊搜索，返回最匹配的图片路径或 None。
        匹配规则：文件名去掉前后缀/多余修饰后，与资产名存在包含关系即视为命中；
        多个候选时选“文件名与资产名重合字符最多”的一张；已绑定其它资产的图优先跳过。"""
        img_dir = os.path.join(self._asset_root_dir(), "images")
        if not os.path.isdir(img_dir):
            return None
        try:
            fnames = os.listdir(img_dir)
        except Exception:
            return None
        used = set()
        for it in self._asset_items:
            p = it.get("image")
            if p and isinstance(p, str):
                used.add(os.path.abspath(p))
        name_norm = re.sub(r'[\s_\-\.]+', '', str(name or "").lower())
        if not name_norm:
            return None
        best = None
        best_score = -1
        for fn in fnames:
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                continue
            p = os.path.join(img_dir, fn)
            if os.path.abspath(p) in used:
                continue
            base = os.path.splitext(fn)[0]
            # 去掉 asset_/upload_ 前缀与时间戳后缀，留下语义文件名
            core = re.sub(r'^(asset_|upload_|绑定_|bind_)', '', base)
            core = re.sub(r'_\d{10,}$', '', core)
            core_norm = re.sub(r'[\s_\-\.]+', '', core.lower())
            if not core_norm:
                continue
            # 包含关系评分：重合字符越多越好
            if name_norm in core_norm or core_norm in name_norm:
                overlap = len(set(name_norm) & set(core_norm))
                score = overlap
                if best_score < score:
                    best_score = score
                    best = p
        return best

    def _asset_merge_extracted(self, items):
        """把提取结果合并进全局资产库（全局查重 name+type）：
        - 已存在同名同类型 → 沿用旧资产（保留已生成图片），补充缺失字段，并给当前集打 episodes 标记
        - 不存在 → 作为新资产追加，标记当前集
        集与集之间资产互通：第2集提取到第1集已有的资产时直接复用，不新建副本。"""
        cur = self._cur_asset_episode
        reused = 0
        newadd = 0
        for ni in items:
            if not isinstance(ni, dict) or not ni.get("name"):
                continue
            name = str(ni.get("name") or "").strip()
            typ = str(ni.get("type") or "").strip()
            target = None
            for it in self._asset_items:
                if str(it.get("name") or "").strip() == name and str(it.get("type") or "").strip() == typ:
                    target = it
                    break
            if target is not None:
                reused += 1
                # 沿用并在 fields 上补充缺失字段（不覆盖已有图片/名称）
                for k in ("prompt", "role", "appearance", "description"):
                    if k in ni and ni.get(k) and not target.get(k):
                        target[k] = ni[k]
                # 复用资产若无图：尝试按名称在资产仓库 images 模糊匹配自动补图
                if not (target.get("image") and isinstance(target["image"], str)
                        and os.path.isfile(target["image"])):
                    auto_img = self._asset_fuzzy_match_image(name, typ)
                    if auto_img:
                        target["image"] = auto_img
                self._asset_tag_episode(target)
            else:
                newadd += 1
                item = dict(ni)
                # 新建资产若无图：尝试按名称在资产仓库 images 模糊匹配自动补图
                auto_img = self._asset_fuzzy_match_image(name, typ)
                if auto_img:
                    item["image"] = auto_img
                self._asset_tag_episode(item)
                self._asset_items.append(item)
        self._asset_refresh_tree()
        self._sync_all_asset_lists()
        self._asset_save()
        if cur:
            self._log("资产合并到「%s」：复用%d / 新建%d" % (cur, reused, newadd), "ok")

    def _asset_tag_episode(self, item):
        """给资产打上当前集标记（episodes 列表记录被哪些集使用）。"""
        cur = self._cur_asset_episode
        if not cur:
            return
        eps = item.get("episodes")
        if not isinstance(eps, list):
            eps = []
            item["episodes"] = eps
        if cur not in eps:
            eps.append(cur)

    def _regex_extract_chars(self, text):
        """从剧本台词「名字：台词」格式提取高频角色名"""
        from collections import Counter
        names = Counter()
        for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9]{2,6})[:：]", text):
            n = m.group(1).strip()
            if n and n not in ("第", "集") and not re.fullmatch(r"\d+", n):
                names[n] += 1
        out = []
        for n, c in names.most_common(12):
            if c >= 2 and n not in out:
                out.append(n)
        return out

    def _regex_extract_items(self, text, keywords):
        """按「场景: xxx」「道具: xxx」等关键词提取资产"""
        out = []
        for kw in keywords:
            for m in re.finditer(kw + r"[:：]\s*([^\n，。；;、]+)", text):
                v = m.group(1).strip()
                if v and v not in out:
                    out.append(v)
        return out

    def _asset_refresh_tree(self):
        visible = self._asset_visible_items()
        for tkey in ("人物", "场景", "道具"):
            lst = self._asset_lists.get(tkey)
            if lst is None:
                continue
            lst.blockSignals(True)
            lst.clear()
            for it in visible:
                if it.get("type") != tkey:
                    continue
                item = QListWidgetItem()
                item.setData(Qt.UserRole, it)
                item.setSizeHint(QSize(108, 128))
                lst.addItem(item)
                lst.setItemWidget(item, self._asset_card_widget(it))
            # 分类框末尾的「+」添加卡片：点击直接添加本类资产
            add_item = QListWidgetItem()
            add_item.setData(Qt.UserRole, "__ADD_ASSET__")
            add_item.setSizeHint(QSize(108, 128))
            lst.addItem(add_item)
            lst.setItemWidget(add_item, self._asset_add_card_widget(tkey))
            lst.blockSignals(False)
            # 布局稳定后按当前可视宽度重算列宽（宽屏锁 4 列，窄窗自动降列）
            QTimer.singleShot(0, lambda _l=lst: self._asset_recalc_grid(_l))

    def _asset_recalc_grid(self, lst):
        """把资产网格的列宽对齐到「最多 4 列」：窗口够宽时每行锁 4 个卡片，
        窗口太窄时依次降为 3/2/1 列，避免卡片被压得过小。"""
        max_cols = int(lst.property("asset_grid_maxcols") or 4)
        sp = lst.spacing()
        vw = lst.viewport().width()
        if vw <= 0 or max_cols <= 1:
            return
        min_col = 80
        cols = max_cols
        while cols > 1 and (vw - (cols - 1) * sp) // cols < min_col:
            cols -= 1
        col_w = (vw - (cols - 1) * sp) // cols
        item_h = 128
        for i in range(lst.count()):
            it = lst.item(i)
            if it is not None:
                it.setSizeHint(QSize(col_w, item_h))

    def _asset_add_card_widget(self, tkey):
        """资产卡片流末尾的「+ 添加」卡片：紧随最后一张资产卡之后，点击添加本类资产。
        与资产卡同尺寸（84x100），新卡插入后自动顺移，方便连续添加下一个。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        plus = QLabel("＋")
        plus.setFixedSize(72, 72)
        plus.setAlignment(Qt.AlignCenter)
        plus.setStyleSheet(
            "font-size:34px; font-weight:700; color:#3b82f6;"
            "background:#fbfdff; border:1.5px dashed #93c5fd; border-radius:12px;")
        plus.setToolTip("添加%s资产（本地上传图片，稍后也可 AI 生成）" % tkey)
        lay.addWidget(plus, 0, Qt.AlignCenter)
        nm = QLabel("添加%s" % tkey)
        nm.setAlignment(Qt.AlignCenter)
        nm.setStyleSheet("font-size:10px; color:#3b82f6; background:transparent;")
        lay.addWidget(nm, 0, Qt.AlignCenter)
        # 卡片整体可点击（itemClicked），无需为子控件安装事件
        w.setToolTip("添加%s资产：选择本地图片上传；不选图片可先命名，稍后用 AI 生成" % tkey)
        return w

    def _asset_card_widget(self, asset):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        img = QLabel()
        img.setFixedSize(72, 72)
        img.setAlignment(Qt.AlignCenter)
        p = asset.get("image")
        if p and isinstance(p, str) and os.path.isfile(p):
            img.setPixmap(QPixmap(p).scaled(68, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            img.setToolTip("点击放大预览")
            img.setCursor(Qt.PointingHandCursor)
            img.setProperty("asset_img_path", p)
            img.setStyleSheet("border:1px solid #cbd5e1; border-radius:6px; background:#e2e8f0;")
        else:
            img.setText("无图")
            img.setToolTip("点击「生成此资产图」用 AI 生图")
            img.setProperty("asset_img_path", "")
            img.setStyleSheet("border:1px dashed #cbd5e1; border-radius:6px; background:#f8fafc; color:#94a3b8; font-size:10px;")
        img.setProperty("asset_data_name", asset["name"])
        img.setProperty("asset_data_type", asset["type"])
        img.installEventFilter(self)
        lay.addWidget(img, 0, Qt.AlignCenter)
        nm = QLabel(asset["name"])
        nm.setAlignment(Qt.AlignCenter)
        nm.setWordWrap(True)
        nm.setStyleSheet(
            "font-size:13px; font-weight:600; color:#1e293b; background:transparent;"
            "line-height:14px;")
        tip = asset["name"]
        role = asset.get("role") or ""
        desc = asset.get("description") or ""
        if role:
            tip += "\n类型：" + {"main": "主要", "supporting": "次要", "minor": "龙套"}.get(role, role)
        if desc:
            tip += "\n简介：" + desc[:80]
        nm.setToolTip(tip)
        lay.addWidget(nm)
        return w

    def _asset_on_select(self, item, _tkey=None):
        data = item.data(Qt.UserRole)
        if data is None:
            return
        if data == "__ADD_ASSET__":
            # 点击分类框末尾「+」：直接手动添加该分类资产
            self._asset_add_manual(fixed_type=_tkey)
            return
        self._asset_selected = data
        self._asset_show_preview(data)

    def _asset_show_preview(self, data):
        img = data.get("image")
        if img:
            if isinstance(img, str) and os.path.isfile(img):
                pix = QPixmap(img).scaled(240, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.asset_preview_label.setPixmap(pix)
            elif isinstance(img, QImage):
                pix = QPixmap.fromImage(img).scaled(240, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.asset_preview_label.setPixmap(pix)
            else:
                self.asset_preview_label.setText("（无预览图）")
        else:
            info = "<b>%s</b><br/><font size=2 color='#64748b'>%s</font>" % (data["name"], data["type"])
            role = data.get("role") or ""
            if role:
                info += "<br/><font size=2 color='#64748b'>%s</font>" % (
                    "主要角色" if role == "main" else "次要角色" if role == "supporting" else "龙套角色")
            desc = data.get("description") or ""
            if desc:
                info += "<br/><font size=2 color='#475569'>%s</font>" % desc[:120]
            info += "<br/><br/>未生成图片"
            self.asset_preview_label.setText(info)
        desc = data.get("description") or ""
        role = data.get("role") or ""
        info_txt = "「%s」· %s" % (data["name"], data["type"])
        if role:
            info_txt += "（%s）" % ("主要角色" if role == "main" else "次要角色" if role == "supporting" else "龙套角色")
        if desc:
            info_txt += "\n" + desc
        info_txt += "\n提示词：%s" % (data.get("prompt", "") or "")
        self._asset_info(info_txt, "desc")

    def _asset_grid_menu(self, pos, tkey):
        lst = self._asset_lists.get(tkey)
        if lst is None:
            return
        item = lst.itemAt(pos)
        data = item.data(Qt.UserRole) if item else None
        if data is None or data == "__ADD_ASSET__":
            return
        menu = QMenu()
        act_edit = menu.addAction("✏ 编辑资产名")
        act_up = menu.addAction("📁 本地上传图片")
        is_person = (str(data.get("type") or "") == "人物")
        act_var = menu.addAction("🧵 管理服装变体") if is_person else None
        act_gen = menu.addAction("🖼 生成此资产图")
        act_del = menu.addAction("🗑 删除资产")
        act_use = menu.addAction("📎 用作参考图")
        act = menu.exec_(lst.mapToGlobal(pos))
        if act == act_edit:
            newName, ok = QInputDialog.getText(self, "编辑资产", "资产名称:", text=data["name"])
            if ok and newName.strip():
                new_name = newName.strip()
                old_name = data["name"]
                self._log("编辑资产: %s → %s" % (old_name, new_name), "ok")
                # 修改 _asset_items 中的 dict（name+type 唯一标识）
                target = None
                for it in self._asset_items:
                    if it.get("name") == old_name and it.get("type") == tkey:
                        it["name"] = new_name
                        if it.get("prompt"):
                            it["prompt"] = it["prompt"].replace(f"人物: {old_name}", f"人物: {new_name}") \
                                                      .replace(f"场景: {old_name}", f"场景: {new_name}") \
                                                      .replace(f"道具: {old_name}", f"道具: {new_name}")
                        target = it
                        break
                if target is None:
                    self._log("警告: 未找到对应资产项", "error")
                    return
                self._asset_save()
                self._asset_refresh_tree()
                # 同步生成区并触发关键词重匹配（旧名token→新名资产，高亮/参考图随之更新）
                try:
                    self._sync_all_asset_lists()
                except Exception:
                    pass
                # 重新选中
                lst = self._asset_lists.get(tkey)
                for i in range(lst.count() - 1):
                    it = lst.item(i)
                    d = it.data(Qt.UserRole)
                    if d and d.get("name") == new_name and d.get("type") == tkey:
                        lst.setCurrentItem(it)
                        self._asset_on_select(it, _t=tkey)
                        break
                self._log("资产名已改为「%s」" % new_name, "ok")
                return
        elif act == act_up:
            self._asset_upload_local(data)
        elif act == act_var and act_var is not None:
            self._asset_variant_manager(data)
        elif act == act_gen:
            self._asset_selected = data
            if getattr(self, "_asset_img_worker", None) is not None and \
                    self._asset_img_worker.isRunning():
                QMessageBox.information(self, "提示", "已有资产生成任务进行中，请稍候再试")
            else:
                self._asset_gen_single()
        elif act == act_del:
            self._asset_items.remove(data)
            self._asset_refresh_tree()
            if self._asset_selected is data:
                self._asset_selected = None
            self.asset_preview_label.setText("点击资产卡片查看大图与提示词")
            self._asset_info("")
            self._asset_save()
            # 同步生成区：被删资产的参考图/高亮立即移除
            try:
                self._sync_all_asset_lists()
            except Exception:
                pass
        elif act == act_use:
            self._asset_selected = data
            self._asset_use_as_ref(data)

    def _asset_set_image_by_key(self, name, typ, img_path):
        """按 name+type 在 self._asset_items 定位并写入图片路径，返回命中的资产 dict 或 None。"""
        target = None
        for it in self._asset_items:
            if str(it.get("name") or "") == str(name) and str(it.get("type") or "") == str(typ):
                target = it
                break
        if target is None:
            return None
        target["image"] = img_path
        return target

    def _asset_commit_and_reload(self, name, typ, img_path):
        """写入图片 → 合并保存 → 从磁盘重载（统一内存/文件/卡片引用）→ 定位预览。"""
        self._asset_set_image_by_key(name, typ, img_path)
        self._asset_save()
        self._asset_reload()
        sel = None
        for it in self._asset_items:
            if str(it.get("name") or "") == str(name) and str(it.get("type") or "") == str(typ):
                sel = it
                break
        if sel is not None:
            self._asset_selected = sel
            self._asset_show_preview(sel)
        return sel is not None

    def _asset_variant_manager(self, data):
        """管理人物资产的面部/服装变体（variants 列表）。支持：
        - 添加变体：填服装标签 + 从本地选一张变体图
        - 删除变体
        变体图写入资产仓库 images，写入该人物的 variants。
        """
        name = str(data.get("name") or "")
        typ = str(data.get("type") or "")
        # 定位真实资产 dict（避免操作副本）
        target = None
        for it in self._asset_items:
            if str(it.get("name") or "") == name and str(it.get("type") or "") == typ:
                target = it
                break
        if target is None:
            QMessageBox.warning(self, "提示", "未能在资产列表中找到「%s」" % name)
            return
        variants = target.get("variants")
        if not isinstance(variants, list):
            variants = []
            target["variants"] = variants

        repo = os.path.join(self._asset_root_dir(), "images")
        try:
            os.makedirs(repo, exist_ok=True)
        except Exception:
            pass

        while True:
            names = [str(v.get("label") or "") for v in variants]
            lines = []
            for i, v in enumerate(variants):
                img = str(v.get("image") or "")
                state = "有图" if (img and os.path.isfile(img)) else "无图"
                lines.append("%d. %s（%s）" % (i + 1, str(v.get("label") or "未命名"), state))
            header = "当前「%s」的服装变体：\n" % name
            if lines:
                header += "\n".join(lines) + "\n\n"
            else:
                header += "（暂无变体，提示词用「%s[服装描述]」可指定变体图）\n\n" % name
            item_text, ok = QInputDialog.getItem(
                self, "管理服装变体", header + "选择操作",
                ["＋ 添加变体"] + names, 0, False)
            if not ok or not item_text:
                return
            if item_text == "＋ 添加变体":
                newlabel, ok2 = QInputDialog.getText(self, "添加变体", "服装/造型标签（如：黑色西装）:", text="")
                if not ok2 or not newlabel.strip():
                    continue
                path, _ = QFileDialog.getOpenFileName(
                    self, "选择「%s」的变体图" % newlabel.strip(), repo,
                    "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)")
                if not path or not os.path.isfile(path):
                    QMessageBox.information(self, "提示", "已取消添加（未选择图片）")
                    continue
                img = self._asset_store_image(path, newlabel.strip())
                variants.append({"label": newlabel.strip(), "image": img})
                self._asset_save()
                self._asset_reload()
                self._log("已为「%s」添加服装变体「%s」" % (name, newlabel.strip()), "ok")
            else:
                idx = names.index(item_text)
                ret = QMessageBox.question(
                    self, "删除变体", "确认删除变体「%s」？" % item_text,
                    QMessageBox.Yes | QMessageBox.No)
                if ret == QMessageBox.Yes:
                    variants.pop(idx)
                    self._asset_save()
                    self._asset_reload()
                    self._log("已删除变体「%s」" % item_text, "ok")

    def _asset_upload_local(self, data):
        """上传/绑定本地图片为该资产主图（右键「本地上传图片」与无图卡片双击统一入口）：
        - 所选图片已在资产仓库 images 内 → 直接引用，不重复复制；
        - 否则复制进仓库 images 再关联。"""
        if data is None:
            return
        name = str(data.get("name") or "")
        typ = str(data.get("type") or "")
        repo = os.path.join(self._asset_root_dir(), "images")
        try:
            os.makedirs(repo, exist_ok=True)
        except Exception:
            pass
        path, _ = QFileDialog.getOpenFileName(
            self, "上传本地图片到「%s」" % name, repo,
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)")
        if not path:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "提示", "所选文件不存在")
            return
        if os.path.abspath(path).startswith(os.path.abspath(repo)):
            dst = path  # 已在仓库：直接引用，避免复制出重复副本
        else:
            dst = self._asset_store_image(path, name)
            if not dst or not os.path.isfile(dst):
                QMessageBox.warning(self, "提示", "图片复制失败：%s" % dst)
                return
        if self._asset_commit_and_reload(name, typ, dst):
            self._asset_status("已设置本地图片到「%s」" % name)
        else:
            QMessageBox.warning(self, "提示", "未能在资产列表中找到「%s」，请刷新后重试" % name)

    def _asset_store_image(self, src, asset_name=None):
        """复制本地图片进资产仓库 images；带资产名则写入文件名，便于日后按名找回。"""
        try:
            d = self._asset_root_dir("images")
            if not d:
                return src
            os.makedirs(d, exist_ok=True)
            ext = os.path.splitext(src)[1] or ".png"
            safe = ""
            if asset_name:
                safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(asset_name).strip()).strip("._")[:40]
            fn = ("asset_%s_%d%s" % (safe, int(time.time() * 1000), ext)
                  if safe else "upload_%d%s" % (int(time.time() * 1000), ext))
            dst = os.path.join(d, fn)
            shutil.copyfile(src, dst)
            return dst
        except Exception:
            return src

    def _asset_bind_local(self, data=None):
        """从本地资产仓库 images 目录选择已下载的图片绑定到资产：
        - data 为空 → 弹出可视化绑定窗口，左侧图片缩略图、右侧资产下拉逐一对应；
        - data 非空 → 直接为指定资产选一张本地图。"""
        if data is None:
            self._asset_open_bind_dialog()
            return
        name = str(data.get("name") or "")
        typ = str(data.get("type") or "")
        default_dir = os.path.join(self._asset_root_dir(), "images")
        path, _ = QFileDialog.getOpenFileName(
            self, "绑定本地图片到「%s」" % name, default_dir,
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)")
        if not path:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "提示", "所选文件不存在")
            return
        if self._asset_commit_and_reload(name, typ, path):
            self._asset_status("已将本地图片绑定到「%s」" % name)
        else:
            QMessageBox.warning(self, "提示", "未能在资产列表中找到「%s」，请刷新后重试" % name)

    def _asset_open_bind_dialog(self):
        """可视批量绑定：把本地已下载图片对应到资产卡片（免重新生成）。"""
        img_dir = os.path.join(self._asset_root_dir(), "images")
        if not os.path.isdir(img_dir):
            QMessageBox.information(self, "提示", "本地图片目录不存在：\n%s" % img_dir)
            return
        imgs = [os.path.join(img_dir, fn) for fn in sorted(os.listdir(img_dir))
                if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
        # 已被某个资产绑定的图片跳过
        used = set()
        for it in self._asset_items:
            p = it.get("image")
            if p and isinstance(p, str):
                used.add(os.path.abspath(p))
        free_imgs = [p for p in imgs if os.path.abspath(p) not in used]
        if not free_imgs:
            QMessageBox.information(self, "提示", "本地图片目录没有未绑定的图片。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("绑定本地已下载图片到资产")
        dlg.resize(760, 520)
        lay = QVBoxLayout(dlg)
        tip = QLabel("左侧为「资产仓库/images」中已下载但尚未绑定的图片（可点选预览）。\n"
                     "选中一张图片 → 右侧选择目标资产 → 点击「⬅ 绑定到该资产」。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#475569; font-size:12px;")
        lay.addWidget(tip)

        mid = QHBoxLayout()
        # 左：图片列表
        self._bind_pics = QListWidget()
        self._bind_pics.setViewMode(QListView.IconMode)
        self._bind_pics.setIconSize(QSize(120, 120))
        self._bind_pics.setGridSize(QSize(132, 132))
        self._bind_pics.setResizeMode(QListView.Adjust)
        self._bind_pics.setStyleSheet(
            "QListWidget{background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px;}"
            "QListWidget::item{padding:4px;}")
        for p in free_imgs:
            item = QListWidgetItem()
            pm = QPixmap(p)
            if not pm.isNull():
                item.setIcon(QIcon(pm.scaled(116, 116, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setText(os.path.basename(p))
            item.setToolTip(p)
            item.setData(Qt.UserRole, p)
            self._bind_pics.addItem(item)
        mid.addWidget(self._bind_pics, 3)
        # 中：按钮
        bcol = QVBoxLayout()
        bcol.addStretch(1)
        b_bind = QPushButton("⬅ 绑定到资产")
        b_bind.setObjectName("accentBtn")
        b_bind.clicked.connect(lambda: self._bind_pic_to_asset(dlg))
        bcol.addWidget(b_bind)
        b_open = QPushButton("📂 打开文件夹")
        b_open.setObjectName("ghostBtn")
        b_open.clicked.connect(lambda: self._asset_open_img_dir(img_dir))
        bcol.addWidget(b_open)
        b_done = QPushButton("✅ 完成")
        b_done.setObjectName("ghostBtn")
        b_done.clicked.connect(dlg.accept)
        bcol.addWidget(b_done)
        bcol.addStretch(1)
        mid.addLayout(bcol, 0)
        # 右：资产选择
        self._bind_asset_cb = QComboBox()
        self._bind_asset_cb.setMinimumWidth(190)
        self._bind_assets = [it for it in self._asset_items
                             if not (it.get("image") and isinstance(it["image"], str)
                                     and os.path.isfile(it["image"]))]
        for it in self._bind_assets:
            self._bind_asset_cb.addItem("%s · %s" % (it["type"], it["name"]))
        if not self._bind_assets:
            self._bind_asset_cb.addItem("（所有资产均已绑定图片）")
        rcol = QVBoxLayout()
        rcol.addWidget(QLabel("绑定到资产："))
        rcol.addWidget(self._bind_asset_cb)
        rcol.addStretch(1)
        rp = QWidget(); rp.setLayout(rcol)
        rp.setFixedWidth(240)
        mid.addWidget(rp, 0)
        lay.addLayout(mid, 1)
        dlg.exec()

    def _bind_pic_to_asset(self, dlg):
        pic = self._bind_pics.currentItem()
        if pic is None:
            QMessageBox.information(dlg, "提示", "请先在左侧选择一张图片")
            return
        idx = self._bind_asset_cb.currentIndex()
        if idx < 0 or idx >= len(self._bind_assets):
            QMessageBox.information(dlg, "提示", "没有可绑定的资产")
            return
        p = pic.data(Qt.UserRole)
        asset = self._bind_assets[idx]
        asset["image"] = p
        # 从待绑定列表移除并刷新
        self._bind_pics.takeItem(self._bind_pics.row(pic))
        self._bind_assets.pop(idx)
        self._bind_asset_cb.removeItem(idx)
        if not self._bind_assets:
            self._bind_asset_cb.addItem("（所有资产均已绑定图片）")
        if self._bind_pics.count() == 0:
            self._asset_save()
            self._asset_refresh_tree()
            self._sync_all_asset_lists()
            dlg.accept()
        self._asset_save()
        self._asset_refresh_tree()
        self._sync_all_asset_lists()
        self._asset_status("已绑定「%s」的本地图片" % asset["name"])

    def _asset_open_img_dir(self, img_dir=None):
        img_dir = img_dir or os.path.join(self._asset_root_dir(), "images")
        if os.path.isdir(img_dir):
            os.startfile(img_dir)  # noqa  Windows 打开资源管理器

    def _asset_use_as_ref(self, data):
        """把资产图片填入当前生成区参考图槽"""
        p = data.get("image")
        if not p or not os.path.isfile(p):
            QMessageBox.warning(self, "提示", "该资产还没有图片，请先手动上传图片或点击「生成此资产图」")
            return
        area = getattr(self, "_gen_current_area", None)
        if area is not None:
            area._ref_imgs.append(p)
            area._update_ref_ui()
            self._asset_status("已将「%s」填入当前生成区参考图" % data["name"])
        else:
            QMessageBox.warning(self, "提示", "当前没有生成区，请先在「视频生成」页添加一个生成区")

    def _asset_add_manual(self, fixed_type=None):
        """手动上传资产：fixed_type 为 人物/场景/道具 时跳过分类选择（由各分类框“+”调用）。"""
        if getattr(self, "_asset_adding", False):
            return
        self._asset_adding = True
        try:
            self._asset_add_manual_impl(fixed_type)
        finally:
            self._asset_adding = False

    def _asset_add_manual_impl(self, fixed_type=None):
        types = ["人物", "场景", "道具"]
        if fixed_type in types:
            t = fixed_type
        else:
            t, ok = QInputDialog.getItem(self, "➕ 手动上传资产", "选择分类:", types, 0, False)
            if not ok or not t:
                return
        name, ok2 = QInputDialog.getText(
            self, "手动上传资产", "资产名称（留空则用图片文件名）:", text="")
        # 名称允许留空；留空时若选了图片则用“上传图片的文件名”命名
        name = (name or "").strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择资产图片（可跳过，稍后用 AI 生图）",
            "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*.*)")
        if not name:
            if path and os.path.isfile(path):
                stem, _ext = os.path.splitext(os.path.basename(path))
                name = (stem or "影像资产").strip()
            else:
                QMessageBox.warning(self, "手动上传资产", "未填写资产名称且未选择图片，无法命名资产。")
                return
        img = self._asset_store_image(path, name) if path else None
        prompt, ok3 = QInputDialog.getMultiLineText(
            self, "手动上传资产", "生成提示词（可选，用于 AI 生图）:",
            "%s: %s，写实风格，高清细节" % (t, name))
        self._asset_items.append({
            "name": name, "type": t,
            "prompt": (prompt or "").strip(), "image": img})
        self._asset_tag_episode(self._asset_items[-1])
        self._asset_save()
        self._asset_reload()
        # 滚动到该分类列表末尾（新增卡片与跟随其后的「+」自动可见，方便继续添加）
        lst = self._asset_lists.get(t)
        if lst is not None:
            lst.scrollToBottom()
        self._asset_status("已手动添加「%s」到%s分类" % (name.strip(), t))

    def _asset_root_dir(self, sub=""):
        """资产持久化根目录：打开项目用项目资产仓库，否则回退 EXE 旁「资产仓库」，保证总能落盘。"""
        base = self._assets_dir or os.path.join(config._EXE_DIR, "资产仓库")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        return os.path.join(base, sub) if sub else base

    def _asset_extract_from_scripts(self):
        texts = []
        for d in (getattr(self, "_scripts_dir", ""), getattr(self, "_results_dir", "")):
            if d and os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".txt"):
                        try:
                            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                                texts.append(f.read())
                        except Exception:
                            pass
        if not texts:
            gp = getattr(self, "gen_prompt", None)
            if gp is not None and gp.toPlainText().strip():
                texts = [gp.toPlainText()]
        if not texts:
            QMessageBox.warning(self, "提示",
                "未找到剧本/分镜文本。请先在「📖 剧本」页创建剧本，或填写生成页的全局提示词。")
            return
        self._asset_source_text = "\n\n".join(texts)
        self.asset_pick_btn.setText("已选: 剧本/分镜提取")
        self._asset_extract()

    def _asset_save(self):
        """合并式保存资产到 assets.json：磁盘上已存在的有效图片路径不被内存空值覆盖，
        防止多实例残留进程用旧空列表整表覆盖导致上传/生成的图“丢失”。"""
        try:
            d = self._asset_root_dir()
            p = os.path.join(d, "assets.json")
            disk_map = {}
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        old = json.load(f)
                    for o in old if isinstance(old, list) else []:
                        if isinstance(o, dict) and o.get("name") is not None:
                            disk_map[(str(o.get("name")), str(o.get("type") or ""))] = o
                except Exception:
                    disk_map = {}
            merged = []
            for it in self._asset_items:
                if not isinstance(it, dict):
                    continue
                key = (str(it.get("name") or ""), str(it.get("type") or ""))
                img = it.get("image")
                # 内存空 → 若磁盘有有效图则回填，避免空覆盖已生成的图
                if not (img and isinstance(img, str) and os.path.isfile(img)):
                    old_it = disk_map.get(key)
                    if old_it and old_it.get("image") and \
                            isinstance(old_it["image"], str) and os.path.isfile(old_it["image"]):
                        it["image"] = old_it["image"]
                merged.append(it)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            self._asset_items = merged
        except Exception as e:
            self._log("保存资产失败: %s" % e, "error")

    def _asset_reload(self):
        """从磁盘 assets.json 重新载入，统一内存与文件，防止引用错位。"""
        try:
            p = os.path.join(self._asset_root_dir(), "assets.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._asset_items = [it for it in loaded if isinstance(it, dict) and it.get("type") in ("人物", "场景", "道具")]
            else:
                self._asset_items = []
        except Exception as e:
            self._log("重载资产失败: %s" % e, "error")
        self._asset_refresh_tree()
        # 同步所有视频生成区的资产列表，并触发关键词重匹配（补充参考图/高亮）
        self._sync_all_asset_lists()

    def _asset_load(self):
        try:
            p = os.path.join(self._asset_root_dir(), "assets.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._asset_items = [it for it in loaded if isinstance(it, dict) and it.get("type") in ("人物", "场景", "道具")]
            else:
                self._asset_items = []
        except Exception as e:
            self._log("加载资产失败: %s" % e, "error")
            self._asset_items = []
        self._asset_refresh_tree()
        # 扫描本地已下载图片，按文件名中的资产名自动回填卡片
        self._asset_scan_local_images(silent=True)

    def _asset_scan_local_images(self, silent=False):
        """扫描本地「资产仓库/images」中已下载的图片，若文件名含某资产名，
        且该资产尚未绑定图片，则自动关联显示（无需重新生成）。"""
        img_dir = os.path.join(self._asset_root_dir(), "images")
        files = []
        if os.path.isdir(img_dir):
            for fn in os.listdir(img_dir):
                if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    files.append(fn)
        if not files:
            return 0
        bound = 0
        for it in self._asset_items:
            cur = it.get("image")
            if cur and isinstance(cur, str) and os.path.isfile(cur):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            # 找文件名包含资产名的图片（排除已绑定到其它资产的）
            hit = None
            for fn in files:
                if name in fn:
                    p = os.path.join(img_dir, fn)
                    if os.path.isfile(p):
                        hit = p
                        break
            if hit:
                it["image"] = hit
                bound += 1
        if bound:
            self._asset_refresh_tree()
            self._sync_all_asset_lists()
            self._asset_save()
            if not silent:
                self._asset_status("已从本地图片目录自动关联 %d 个资产" % bound)
                self._log("本地图片扫描：已关联 %d 个资产的图片" % bound, "ok")
        if not silent and self._asset_has_unbound_local_images():
            # 自动按名匹配后仍有没有绑定来源的图片 → 弹可视化窗口让用户手动对应
            self._asset_open_bind_dialog()
        return bound

    def _asset_has_unbound_local_images(self):
        """本地 images 目录是否存在未被任何资产绑定的图片。"""
        img_dir = os.path.join(self._asset_root_dir(), "images")
        if not os.path.isdir(img_dir):
            return False
        used = set()
        for it in self._asset_items:
            p = it.get("image")
            if p and isinstance(p, str):
                used.add(os.path.abspath(p))
        for fn in os.listdir(img_dir):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                if os.path.abspath(os.path.join(img_dir, fn)) not in used:
                    return True
        return False

    def _asset_gen_single(self):
        if not self._asset_selected:
            QMessageBox.warning(self, "提示", "请先选中一个资产")
            return
        if self._asset_img_running():
            QMessageBox.information(self, "提示", "已有资产生成任务进行中，请稍候再试")
            return
        self._asset_img_queue = [self._asset_selected]
        self._asset_img_index = 0
        self._asset_img_total = 1
        self._asset_img_ok = 0
        self._asset_img_slots = 0        # 当前正在跑的并发 slot 数
        self.asset_gen_single_btn.setEnabled(False)
        self._asset_start_next_imgs()

    def _asset_img_running(self):
        w = getattr(self, "_asset_img_worker", None)
        return w is not None and w.isRunning()

    def _asset_img_max_concurrent(self):
        """返回配置的最大并发数（最少 1）。"""
        n = config.IMAGE_GEN.get("max_concurrent") or 1
        try:
            return max(1, int(n))
        except (TypeError, ValueError):
            return 1

    def _asset_gen_all(self):
        if self._asset_img_running():
            QMessageBox.information(self, "提示", "已有资产生成任务进行中，请稍候再试")
            return
        pending = [it for it in self._asset_items if it.get("image") is None]
        if not pending:
            QMessageBox.information(self, "提示", "所有资产已有图片，无需重新生成")
            return
        self._asset_img_queue = list(pending)
        self._asset_img_index = 0
        self._asset_img_total = len(pending)
        self._asset_img_ok = 0
        self._asset_img_slots = 0
        self.asset_gen_btn.setEnabled(False)
        self.asset_gen_single_btn.setEnabled(False)
        self._asset_status("⏳ 开始批量生成（共 %d 个资产）…" % len(pending))
        self._asset_start_next_imgs()

    def _asset_start_next_imgs(self):
        """按最大并发数启动尽可能多的生成任务；队列为空或全完成时收尾。"""
        cfg = config.IMAGE_GEN
        if not cfg.get("api_key"):
            QMessageBox.critical(self, "API未配置",
                "请先在「⚙ AI 服务」→「资产生成图」页签中配置 API Key")
            self._asset_finish_img_queue()
            return
        out_dir = self._asset_root_dir("images")
        max_conc = self._asset_img_max_concurrent()
        # 扫描队列中第一个有图的跳过，直到找到需要生成的
        while self._asset_img_index < len(self._asset_img_queue):
            if self._asset_img_slots >= max_conc:
                break   # 已达并发上限，等待回调
            item = self._asset_img_queue[self._asset_img_index]
            self._asset_img_index += 1
            if item.get("image"):
                continue   # 已有图，跳过
            self._asset_img_slots += 1
            self._asset_status("正在生成「%s」（%d/%d，并发 slot %d/%d）…" % (
                item["name"],
                self._asset_img_ok + self._asset_img_slots,
                self._asset_img_total,
                self._asset_img_slots, max_conc))
            r = AssetImageRunnable(
                api_key=cfg.get("api_key", ""),
                base_url=cfg.get("base_url", ""),
                model=cfg.get("model", ""),
                prompt=item["prompt"],
                size=cfg.get("size", "1024x1024"),
                out_dir=out_dir,
                name_hint=item["name"],
            )
            r.result_callback = lambda run=r, it=item: self._asset_on_img_runnable_done(run, it)
            t = getattr(self, "_asset_img_threadpool", None)
            if t is None:
                t = QThreadPool()
                t.setMaxThreadCount(max_conc)
                self._asset_img_threadpool = t
            t.start(r)
        # 检查是否全部完成
        if self._asset_img_index >= len(self._asset_img_queue) and self._asset_img_slots == 0:
            self._asset_finish_img_queue()

    def _asset_on_img_runnable_done(self, runnable, item):
        """单个可运行任务完成后的处理：写回图片并推进状态。"""
        self._asset_img_slots -= 1
        name = str(item.get("name") or "")
        typ = str(item.get("type") or "")
        if runnable.success:
            self._asset_set_image_by_key(name, typ, runnable.image_path)
            self._asset_img_ok += 1
            self._log("「%s」生成成功 ✓" % name, "ok")
            self._asset_status("「%s」生成成功 ✓" % name)
        else:
            err = runnable.error or "未知错误"
            self._log("「%s」生成失败: %s" % (name, err), "warn")
            self._asset_status("「%s」生成失败: %s" % (name, err))
        self._asset_save()
        self._asset_reload()
        # 继续填充并发 slot
        self._asset_start_next_imgs()

    def _asset_finish_img_queue(self):
        self.asset_gen_btn.setEnabled(True)
        self.asset_gen_single_btn.setEnabled(True)
        self._asset_refresh_tree()
        self._sync_all_asset_lists()
        self._asset_save()
        if self._asset_img_total > 1:
            self._asset_status("批量生成完成：成功 %d / %d 个资产" % (
                self._asset_img_ok, self._asset_img_total))
        else:
            if self._asset_img_ok:
                self._asset_status("「%s」生成成功 ✓" % (self._asset_img_queue[-1]["name"]))
        self._asset_img_queue = []
        self._asset_img_worker = None
        self._asset_img_current = None
        self._asset_img_slots = 0
        tp = getattr(self, "_asset_img_threadpool", None)
        if tp is not None:
            tp.clear()
            self._asset_img_threadpool = None

    def _asset_status(self, msg):
        self._asset_info(msg)

    def _asset_info(self, msg, kind="info"):
        """写入资产信息框。kind: info(蓝)/ok(绿)/error(红)/desc(深灰)"""
        if not hasattr(self, "asset_info_edit"):
            return
        colors = {"info": "#2563eb", "ok": "#16a34a", "error": "#dc2626", "desc": "#475569"}
        color = colors.get(kind, "#475569")
        self.asset_info_edit.setHtml(
            '<div style="color:%s; font-size:12px;">%s</div>'
            % (color, html.escape(str(msg)).replace("\n", "<br>")))

    # =====================================================================
    # 视频生成页（Agnes Video 2.5 Flash）
    # =====================================================================
    def _gen_page(self):
        p = QWidget()
        pp = QVBoxLayout(p)
        pp.setContentsMargins(0, 0, 0, 0)
        pp.setSpacing(0)

        head = QWidget()
        hd = WrapFlowLayout(head, margin=0, hspacing=8, vspacing=6)
        self.gen_key_lbl = QLabel("未配置 Key")
        self.gen_key_lbl.setStyleSheet("color:#64748b;")
        hd.addWidget(self.gen_key_lbl)
        self.gen_all_btn = QPushButton("⚡ 一键全部生成")
        self.gen_all_btn.setObjectName("accentBtn")
        self.gen_all_btn.setMinimumHeight(34)
        self.gen_all_btn.setToolTip("按顺序自动为每个已填提示词的生成区逐集生成，完成后自动保存到当前剧集文件夹（生成剧集\\<剧集名>\\）")
        self.gen_all_btn.clicked.connect(self._gen_toggle_batch)
        hd.addWidget(self.gen_all_btn)
        self.gen_split_btn = QPushButton("🎬 一键分镜")
        self.gen_split_btn.setObjectName("accentBtn")
        self.gen_split_btn.setMinimumHeight(34)
        self.gen_split_btn.setToolTip("按「视频嗅探」页分段分析的每个分镜，一键创建独立生成区（自动填入该段提示词与时长）")
        self.gen_split_btn.clicked.connect(self._gen_split_storyboard)
        hd.addWidget(self.gen_split_btn)
        self.gen_split_local_btn = QPushButton("📂 本地分镜")
        self.gen_split_local_btn.setObjectName("accentBtn")
        self.gen_split_local_btn.setMinimumHeight(34)
        self.gen_split_local_btn.setToolTip("选择一个分镜文件（Shot 01 … 或 视频编号01（总时长：10s）…），"
                                            "按段一键创建生成区：画面风格自动填入全局提示词框，时长自动填入对应生成区")
        self.gen_split_local_btn.clicked.connect(self._gen_split_local)
        hd.addWidget(self.gen_split_local_btn)
        self.gen_add_ep = QPushButton("➕ 添加剧集")
        self.gen_add_ep.setObjectName("accentBtn")
        self.gen_add_ep.setMinimumHeight(34)
        self.gen_add_ep.setToolTip("为需要生成的每一集添加一个独立生成区")
        self.gen_add_ep.clicked.connect(self._gen_add_area)
        hd.addWidget(self.gen_add_ep)
        pp.addWidget(head)

        # 全局提示词（画面风格、统一约束）：生成时自动前置到每个生成区
        gbox = QGroupBox("🌐 全局设置")
        gbox.setObjectName("card")
        gh = QHBoxLayout(gbox)
        gh.setContentsMargins(8, 6, 8, 8)
        gh.setSpacing(12)
        # 左：全局提示词
        left_v = QVBoxLayout()
        left_v.setSpacing(4)
        left_v.addWidget(QLabel("🌐 全局提示词："))
        self.gen_global_edit = QPlainTextEdit()
        self.gen_global_edit.setPlaceholderText("现代都市真人短剧，写实摄影，自然窗光，保持角色资产与空间轴线连续……")
        self.gen_global_edit.setFixedHeight(60)
        left_v.addWidget(self.gen_global_edit)
        gh.addLayout(left_v)
        # 中：全局画幅
        mid_v = QVBoxLayout()
        mid_v.setSpacing(4)
        mid_v.addWidget(QLabel("画幅："))
        self._gen_global_aspect = "16:9"
        self.gen_global_aspect = QComboBox()
        self.gen_global_aspect.addItems(ASPECT_RATIOS)
        try:
            cur = self._gen_areas[0].aspect.currentText() if self._gen_areas else "16:9"
        except Exception:
            cur = "16:9"
        if cur and cur in ASPECT_RATIOS:
            self.gen_global_aspect.setCurrentText(cur)
        self._gen_global_aspect = self.gen_global_aspect.currentText()
        self.gen_global_aspect.currentIndexChanged.connect(self._gen_global_aspect_changed)
        mid_v.addWidget(self.gen_global_aspect)
        mid_v.addStretch(1)
        gh.addLayout(mid_v)
        # 右：分镜提示词 + 分段按钮
        right_v = QVBoxLayout()
        right_v.setSpacing(4)
        right_v.addWidget(QLabel("📋 分镜提示词："))
        self.gen_storyboard_edit = QPlainTextEdit()
        self.gen_storyboard_edit.setPlaceholderText("在此粘贴本地分镜文本，点击「▶ 按分镜生成」自动切段创建生成区…")
        self.gen_storyboard_edit.setFixedHeight(60)
        right_v.addWidget(self.gen_storyboard_edit)
        self.gen_storyboard_btn = QPushButton("▶ 按分镜生成")
        self.gen_storyboard_btn.setObjectName("accentBtn")
        self.gen_storyboard_btn.setMinimumHeight(32)
        self.gen_storyboard_btn.clicked.connect(self._gen_storyboard_from_paste)
        right_v.addWidget(self.gen_storyboard_btn)
        right_v.addStretch(1)
        gh.addLayout(right_v)
        pp.addWidget(gbox)

        self.gen_area_wrap = QWidget()
        self.gen_area_lay = FlowLayout(self.gen_area_wrap, margin=4, hspacing=6, vspacing=6)

        # 顶部合并工具条
        self._merge_bar = QHBoxLayout()
        self._merge_bar.setContentsMargins(8, 0, 8, 0)
        self._merge_bar.setSpacing(6)
        self._merge_label = QLabel("未选生成区")
        self._merge_label.setStyleSheet("font-size:12px; color:#64748b; font-weight:600;")
        self._merge_bar.addWidget(self._merge_label)
        self._merge_bar.addStretch(1)
        # 全选/取消全选按钮
        self._merge_select_all_btn = QPushButton("☑ 全选")
        self._merge_select_all_btn.setObjectName("ghostBtn")
        self._merge_select_all_btn.setMinimumHeight(28)
        self._merge_select_all_btn.clicked.connect(self._gen_select_all_areas)
        self._merge_bar.addWidget(self._merge_select_all_btn)
        # 删除选中按钮
        self._merge_del_btn = QPushButton("🗑 删除选中")
        self._merge_del_btn.setObjectName("dangerBtn")
        self._merge_del_btn.setMinimumHeight(28)
        self._merge_del_btn.clicked.connect(self._gen_delete_selected_areas)
        self._merge_bar.addWidget(self._merge_del_btn)
        self._merge_ok_btn = QPushButton("✅ 合并选中")
        self._merge_ok_btn.setObjectName("accentBtn")
        self._merge_ok_btn.setEnabled(False)
        self._merge_ok_btn.setMinimumHeight(28)
        self._merge_ok_btn.clicked.connect(self._gen_merge_selected)
        self._merge_clear_btn = QPushButton("清除选中")
        self._merge_clear_btn.setObjectName("ghostBtn")
        self._merge_clear_btn.setMinimumHeight(28)
        self._merge_clear_btn.clicked.connect(self._gen_clear_merge)
        self._merge_bar.addWidget(self._merge_ok_btn)
        self._merge_bar.addWidget(self._merge_clear_btn)
        self._merge_panel = QWidget()
        self._merge_panel.setLayout(self._merge_bar)
        self._merge_panel.setStyleSheet("background:#eff6ff; border-bottom:1px solid #bfdbfe;")
        self._merge_panel.setVisible(False)
        self.gen_area_scroll = QScrollArea()
        self.gen_area_scroll.setWidgetResizable(True)
        self.gen_area_scroll.setWidget(self.gen_area_wrap)
        self.gen_area_scroll.setFrameShape(QFrame.NoFrame)
        # 拉宽窗口/任务区时，让生成区面板按新宽度重新撑满（见 _gen_relayout_areas）
        self.gen_area_scroll.setProperty("gen_area_scroll", True)
        self.gen_area_scroll.installEventFilter(self)
        # 顶部工具条 + 滚动区
        self._tool_area = QWidget()
        self._tool_lay = QVBoxLayout(self._tool_area)
        self._tool_lay.setContentsMargins(0, 0, 0, 0)
        self._tool_lay.setSpacing(0)
        self._tool_lay.addWidget(self._merge_panel)
        self._tool_lay.addWidget(self.gen_area_scroll)
        # 右侧侧栏：视频任务 + 本地文件（生成视频）
        self.gen_side = self._build_gen_side()
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.addWidget(self._tool_area)
        splitter.addWidget(self.gen_side)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        pp.addWidget(splitter, 1)
        self._gen_batch_q = []
        self._gen_batch_run = False
        self._gen_429_pending = []   # 429 限速待整轮重试的生成区
        self._gen_429_waiting_all = False
        # 批量生成：每完成一个任务独立计时，满 60s 立即补一个（单槽位补满）
        self._gen_batch_interval_sec = 60
        self._gen_batch_last_submit_ts = 0
        self._gen_batch_submit_timer = None
        return p

    def _build_gen_side(self):
        """生成页右侧侧栏：🎬 播放区 + 📌 视频任务"""
        side = QWidget()
        side.setObjectName("genSide")
        side.setMinimumWidth(180)
        side.setMaximumWidth(420)
        side.setStyleSheet("QWidget#genSide{ background:#0f172a; border-left:1px solid #1e293b; }")
        lay = QVBoxLayout(side)
        # 收紧侧栏内边距与分段间距，让播放区/任务区更贴近左边的生成区
        lay.setContentsMargins(3, 6, 3, 6)
        lay.setSpacing(4)

        # ---- 视频播放区：内嵌播放器已隐藏，点击任务/文件卡片以弹窗播放 ----
        t_play = QLabel("▶ 视频以弹窗播放（点击任务或文件卡片）")
        t_play.setStyleSheet("color:#64748b; font-weight:800; font-size:11px;")
        lay.addWidget(t_play)

        # 播放器初始化
        self.gen_audio = QAudioOutput(self)
        self.gen_player = QMediaPlayer(self)
        self.gen_player.setAudioOutput(self.gen_audio)
        self.gen_seek_t = False
        self.gen_vol_t = QTimer(self); self.gen_vol_t.setSingleShot(True)
        self.gen_vol_t.setInterval(300); self.gen_vol_t.timeout.connect(lambda: None)

        # 控制条
        self.gen_ctlbar = QWidget(); self.gen_ctlbar.setObjectName("playerBar")
        cb = QHBoxLayout(self.gen_ctlbar)
        cb.setContentsMargins(0, 0, 0, 0); cb.setSpacing(0)
        self.gen_sld = QSlider(Qt.Horizontal)
        self.gen_sld.setRange(0, 1000)
        self.gen_sld.setObjectName("seekSlider")
        self.gen_sld.sliderMoved.connect(self._gen_seek)
        self.gen_sld.sliderPressed.connect(lambda: setattr(self, "gen_seek_t", True))
        self.gen_sld.sliderReleased.connect(self._gen_seek_released)
        cb.addWidget(self.gen_sld, 1)
        self.gen_time_lbl = QLabel("00:00 / 00:00")
        self.gen_time_lbl.setObjectName("playTime")
        self.gen_time_lbl.setMinimumWidth(86)
        self.gen_time_lbl.setAlignment(Qt.AlignCenter)
        cb.addWidget(self.gen_time_lbl)
        self.gen_vol_btn = QPushButton()
        self.gen_vol_btn.setObjectName("volBtn")
        self.gen_vol_btn.setIcon(self._make_vol_icon())
        self.gen_vol_btn.setIconSize(QSize(18, 18))
        self.gen_vol_btn.setToolTip("音量（鼠标放上来调节）")
        self.gen_vol_btn.setCursor(Qt.PointingHandCursor)
        cb.addWidget(self.gen_vol_btn)

        # 竖向音量弹窗
        self.gen_vol_popup = QWidget(); self.gen_vol_popup.setObjectName("volPopup")
        vp = QVBoxLayout(self.gen_vol_popup)
        vp.setContentsMargins(4, 6, 4, 6)
        self.gen_vol_slider = VolSlider()
        self.gen_vol_slider.valueChanged.connect(self._gen_set_volume)
        vp.addWidget(self.gen_vol_slider, 1)

        self.gen_vol_btn.enterEvent = lambda e: self._gen_show_volpopup()
        self.gen_vol_btn.leaveEvent = lambda e: self.gen_vol_t.start()
        self.gen_vol_popup.enterEvent = lambda e: self.gen_vol_t.stop()
        self.gen_vol_popup.leaveEvent = lambda e: self.gen_vol_t.start()

        self.gen_player_view = OverlayPlayer(self.gen_player, on_tap=self._gen_player_toggle)
        self.gen_player_view.set_controls(self.gen_ctlbar, self.gen_vol_popup)
        lay.addWidget(self.gen_player_view, 3)
        # 隐藏内嵌播放器及其控制条，视频统一改为弹窗播放
        self.gen_player_view.setVisible(False)
        self.gen_ctlbar.setVisible(False)
        self.gen_vol_popup.setVisible(False)

        self.gen_player.positionChanged.connect(self._gen_on_pos)
        self.gen_player.durationChanged.connect(self._gen_on_dur)
        self.gen_player.playbackStateChanged.connect(self._gen_on_state)
        self.gen_player.mediaStatusChanged.connect(self._gen_on_media_status)
        self.gen_player.errorOccurred.connect(lambda e, s: self._log(f"播放出错: {s}", "error"))

        # ---------- 生成区播放控制 ----------
        self.gen_seek_t = False
        self.gen_vol_t = QTimer(self); self.gen_vol_t.setSingleShot(True)
        self.gen_vol_t.setInterval(300); self.gen_vol_t.timeout.connect(lambda: None)

        self.gen_ctlbar.enterEvent = lambda e: self.gen_vol_popup.show()
        self.gen_ctlbar.leaveEvent = lambda e: self.gen_vol_t.start()
        self.gen_vol_popup.enterEvent = lambda e: self.gen_vol_t.stop()
        self.gen_vol_popup.leaveEvent = lambda e: self.gen_vol_t.start()

        # ---- 视频任务列表 ----
        t1_lay = QHBoxLayout()
        t1 = QLabel("📌 视频任务")
        t1.setStyleSheet("color:#e2e8f0; font-weight:800;")
        t1_lay.addWidget(t1)
        t1_lay.addStretch(1)
        self.gen_task_lbl = QLabel("尚无任务")
        self.gen_task_lbl.setStyleSheet("color:#94a3b8; font-size:12px;")
        t1_lay.addWidget(self.gen_task_lbl)
        lay.addLayout(t1_lay)
        self.gen_task_list = QListWidget()
        self.gen_task_list.setFrameShape(QFrame.NoFrame)
        self.gen_task_list.setMinimumHeight(90)
        self.gen_task_list.setStyleSheet("QListWidget{ background:#0f172a; color:#cbd5e1; border:1px solid #1e293b; border-radius:6px; }")
        self.gen_task_list.itemClicked.connect(self._gen_task_open)

        # ---- 任务区 + 日志区（上下可拖拽分割，日志高度可自由拉动）----
        self._gen_side_split = QSplitter(Qt.Vertical)
        self._gen_side_split.setChildrenCollapsible(False)
        self._gen_side_split.setHandleWidth(5)

        # 任务区容器
        task_box = QWidget()
        tblay = QVBoxLayout(task_box)
        tblay.setContentsMargins(0, 0, 0, 0)
        tblay.setSpacing(4)

        # 任务操作按钮（均分一行）
        btn_lay = QHBoxLayout()
        btn_lay.setSpacing(6)
        self.gen_task_clear_btn = QPushButton("🗑 清空")
        self.gen_task_clear_btn.setObjectName("ghostBtn")
        self.gen_task_clear_btn.setMinimumHeight(26)
        self.gen_task_clear_btn.setToolTip("清空所有已结束(已完成/失败)的视频任务记录，保留排队/生成中")
        self.gen_task_clear_btn.clicked.connect(self._gen_tasks_clear)
        btn_lay.addWidget(self.gen_task_clear_btn, 1)
        self.gen_task_select_all_btn = QPushButton("☑ 全选")
        self.gen_task_select_all_btn.setObjectName("ghostBtn")
        self.gen_task_select_all_btn.setMinimumHeight(26)
        self.gen_task_select_all_btn.setToolTip("勾选/取消全部任务")
        self.gen_task_select_all_btn.clicked.connect(self._gen_tasks_toggle_select_all)
        btn_lay.addWidget(self.gen_task_select_all_btn, 1)
        self.gen_task_del_btn = QPushButton("❌ 删除勾选")
        self.gen_task_del_btn.setObjectName("dangerBtn")
        self.gen_task_del_btn.setMinimumHeight(26)
        self.gen_task_del_btn.setToolTip("删除已勾选的任务记录")
        self.gen_task_del_btn.clicked.connect(self._gen_tasks_delete_selected)
        btn_lay.addWidget(self.gen_task_del_btn, 1)
        tblay.addLayout(btn_lay)

        # 打开目录按钮（占满一行）
        self.gen_open_dir_btn = QPushButton("📂 打开生成目录")
        self.gen_open_dir_btn.setObjectName("ghostBtn")
        self.gen_open_dir_btn.setMinimumHeight(28)
        self.gen_open_dir_btn.clicked.connect(self._gen_files_open_dir)
        tblay.addWidget(self.gen_open_dir_btn)

        tblay.addWidget(self.gen_task_list, 1)

        # 生成日志小窗（合并 dock：Tab 页签「生成日志 / 全部日志」，与嗅探页主日志同源收口）
        gen_log_box = QGroupBox("日志")
        gen_log_box.setStyleSheet(
            "QGroupBox{ color:#e2e8f0; font-weight:800; border:1px solid #1e293b; border-radius:6px;"
            " margin-top:8px; } QGroupBox::title{ subcontrol-origin:margin; left:6px; padding:0 3px; }")
        glb = QVBoxLayout(gen_log_box)
        glb.setContentsMargins(4, 4, 4, 4)
        glb.setSpacing(0)
        self.gen_log_tabs = QTabWidget()
        self.gen_log_tabs.setObjectName("genLogTabs")
        self.gen_log_view = QPlainTextEdit()
        self.gen_log_view.setObjectName("logView")
        self.gen_log_view.setReadOnly(True)
        self.gen_log_view.setMinimumHeight(60)
        self.gen_log_tabs.addTab(self.gen_log_view, "⚙ 生成日志")
        self.gen_log_all_tab = QPlainTextEdit()
        self.gen_log_all_tab.setObjectName("logView")
        self.gen_log_all_tab.setReadOnly(True)
        self.gen_log_all_tab.setMinimumHeight(60)
        self.gen_log_all_tab.setPlaceholderText("全部日志（与嗅探页日志同源）")
        self.gen_log_tabs.addTab(self.gen_log_all_tab, "📜 全部日志")
        self.gen_log_tabs.setTabPosition(QTabWidget.South)
        glb.addWidget(self.gen_log_tabs)

        self._gen_side_split.addWidget(task_box)
        self._gen_side_split.addWidget(gen_log_box)
        self._gen_side_split.setStretchFactor(0, 3)
        self._gen_side_split.setStretchFactor(1, 1)
        self._gen_side_split.setSizes([360, 120])
        lay.addWidget(self._gen_side_split, 1)

        # 浮窗播放器（单例，用于右键菜单等场景）
        self._float_player = None
        return side

    def _get_float_player(self):
        """获取或创建浮窗播放器单例"""
        if self._float_player is None or self._float_player.isClosed():
            self._float_player = FloatPlayerWindow(self)
        try:
            self._float_player._on_play_error = self._log
        except Exception:
            pass
        return self._float_player

    def _gen_file_media(self, path):
        from PySide6.QtCore import QUrl as _QU
        return _QU.fromLocalFile(path)

    def _gen_files_refresh(self):
        d = getattr(self, "_gen_videos_dir", "")
        self._gen_file_cards = []
        # 如果存在布局，清空旧卡片（现在已移除本地文件卡片区域）
        lay = getattr(self, "_gen_file_lay", None)
        if lay is not None:
            while lay.count():
                it = lay.takeAt(0)
                w = it.widget()
                if w:
                    w.setParent(None)
                    w.deleteLater()
        if not d or not os.path.isdir(d):
            return
        # 收集文件信息（不再显示卡片，仅更新数据）
        for i, fn in enumerate(sorted(os.listdir(d), key=str.lower, reverse=True)):
            fp = os.path.join(d, fn)
            if os.path.isfile(fp):
                self._gen_file_cards.append((fn, fp))

    def _gen_files_open_dir(self):
        d = getattr(self, "_gen_videos_dir", "")
        if d and os.path.isdir(d):
            os.startfile(d)

    def _file_card_path(self, card):
        return os.path.join(getattr(self, "_gen_videos_dir", ""), card.name.text())

    def _play_local_file(self, p):
        if not p or not os.path.exists(p):
            self._log("文件不存在: %s" % p, "error")
            return
        self._gen_selected_file = p
        fp = self._get_float_player()
        fp.set_source(p)
        fp.play()
        fp.show()
        fp.raise_()
        fp.activateWindow()

    def on_click_file(self, card):
        for _, c in self._gen_file_cards:
            c.set_selected(c is card)
        self._gen_selected_file = self._file_card_path(card)

    def on_dbl_file(self, card):
        self._play_local_file(self._file_card_path(card))

    def on_play_file(self, card):
        self._play_local_file(self._file_card_path(card))

    def on_rename_file(self, card):
        self._gen_selected_file = self._file_card_path(card)
        self._gen_files_rename()

    def on_delete_file(self, card):
        self._gen_selected_file = self._file_card_path(card)
        self._gen_files_delete()

    def on_open_dir(self, card=None):
        self._gen_files_open_dir()

    def _gen_files_selected_path(self):
        p = getattr(self, "_gen_selected_file", "")
        if p and os.path.exists(p):
            return p
        return ""

    def _gen_files_play(self, item):
        if hasattr(item, "name"):
            p = self._file_card_path(item)
        else:
            p = os.path.join(getattr(self, "_gen_videos_dir", ""), item.text())
        self._play_local_file(p)

    def _gen_files_rename(self):
        p = self._gen_files_selected_path()
        if not p:
            QMessageBox.information(self, "重命名", "请先在本地文件列表选中一个视频文件")
            return
        old = os.path.basename(p)
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "重命名", "新文件名：", text=old)
        if not ok or not name.strip():
            return
        name = re.sub(r'[\\/:*?"<>|]', "", name.strip())
        if not name:
            return
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        dst = os.path.join(os.path.dirname(p), name)
        if os.path.exists(dst):
            QMessageBox.warning(self, "重命名", "已存在同名文件")
            return
        try:
            os.rename(p, dst)
            self._log("已重命名: %s → %s" % (old, name), "ok")
            self._gen_files_refresh()
        except Exception as e:
            self._log("重命名失败: %s" % e, "error")

    def _gen_files_delete(self):
        p = self._gen_files_selected_path()
        if not p:
            QMessageBox.information(self, "删除", "请先在本地文件列表选中一个视频文件")
            return
        ret = QMessageBox.question(self, "删除", "确定删除文件「%s」？" % os.path.basename(p))
        if ret != QMessageBox.Yes:
            return
        try:
            os.remove(p)
            self._log("已删除本地视频: %s" % os.path.basename(p), "ok")
            fp = getattr(self, "_float_player", None)
            if fp and not fp.isClosed():
                fp.stop()
            self._gen_selected_file = ""
            self._gen_files_refresh()
        except Exception as e:
            self._log("删除失败: %s" % e, "error")

    def _gen_sniffer_text(self):
        src = self.va_text.toPlainText().strip()
        if not src:
            src = self.sub_text.toPlainText().strip()
        return src

    def _gen_add_area(self):
        idx = len(self._gen_areas)
        area = GenAreaWidget(idx,
                             get_sniffer_text=self._gen_sniffer_text,
                             on_log=self._log,
                             get_save_dir=self._gen_videos_dir_getter,
                             on_saved=self._gen_files_refresh,
                             on_task=self._gen_task_update,
                             get_global_prompt=self._gen_global_prompt_getter,
                             get_asset_info=self._get_asset_callback,
                             on_import_asset=self._gen_import_asset,
                             get_global_aspect=self._gen_global_aspect_getter)
        area.remove_requested.connect(self._gen_remove_area)
        area.generated.connect(self._gen_batch_continue)
        area.set_parent(self)
        self._gen_areas.append(area)
        # 新生成区默认应用当前全局画幅（未显式设定期用自身默认）
        _ga = getattr(self, "_gen_global_aspect", "") or ""
        if _ga and _ga in ASPECT_RATIOS:
            try:
                area.aspect.setCurrentText(_ga)
            except Exception:
                pass
        self.gen_area_lay.addWidget(area)   # FlowLayout：宽屏两列、窄屏自动换行
        self.gen_area_wrap.updateGeometry()  # 强制刷新布局高度，支持多生成区滚动
        self._gen_relayout_areas()            # 按当前宽度让生成区面板横向撑满（贴近右侧视频任务面板）
        self._sync_asset_list_to_area(area)
        self.gen_add_ep.setText("➕ 添加剧集（已 %d 个）" % (idx + 1))
        # 提示词/参数/参考图变化 → 防抖自动持久化到当前剧集（退出/切剧集也不丢）
        area.state_changed.connect(self._gen_area_schedule_save)
        # 立即持久化当前剧集（含生成区内容），保证退出/切剧集不丢失
        if not getattr(self, "_gen_suppress_save", False) and getattr(self, "_current_gen_episode", None):
            self._save_gen_areas_to_episode()
        return area

    def _gen_area_schedule_save(self, *_a):
        """生成区内容变化后延迟写入磁盘（防抖），退出/切剧集前必然落盘。"""
        if not getattr(self, "_current_gen_episode", None):
            return
        if not hasattr(self, "_gen_area_save_timer"):
            self._gen_area_save_timer = QTimer(self)
            self._gen_area_save_timer.setSingleShot(True)
            self._gen_area_save_timer.setInterval(600)
            self._gen_area_save_timer.timeout.connect(self._gen_flush_areas)
        self._gen_area_save_timer.start()

    def _gen_flush_areas(self):
        """立即把当前剧集生成区写到磁盘。"""
        try:
            # 加载/切换期间不落盘，避免把旧分集的半清空内容写进新分集
            if getattr(self, "_gen_suppress_save", False):
                return
            if getattr(self, "_current_gen_episode", None):
                self._save_gen_areas_to_episode()
        except Exception:
            pass

    def _get_asset_callback(self, name):
        """查询资产信息（供生成区自动填充参考图用）"""
        for it in getattr(self, "_asset_items", []):
            if it.get("name") == name and it.get("image"):
                return it
        return None

    def _gen_import_asset(self, area):
        """从资产管理导入参考图到指定生成区。"""
        items = [it for it in getattr(self, "_asset_items", [])
                 if it.get("image") and isinstance(it["image"], str) and os.path.isfile(it["image"])]
        if not items:
            QMessageBox.information(
                self, "从资产管理导入",
                "资产管理中还没有带图片的资产，请先在「🎨 资产管理」页上传图片或点击「生成此资产图」。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("从资产管理导入参考图（可多选，最多5张）")
        dlg.resize(600, 430)
        lay = QVBoxLayout(dlg)
        tip = QLabel("点击资产卡片可多选，导入后自动加入参考图列表（最多5张）")
        tip.setObjectName("cap")
        lay.addWidget(tip)
        lst = QListWidget()
        lst.setViewMode(QListView.IconMode)
        lst.setFlow(QListView.LeftToRight)
        lst.setWrapping(True)
        lst.setResizeMode(QListView.Adjust)
        lst.setIconSize(QSize(72, 72))
        lst.setSpacing(8)
        lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
        lst.setStyleSheet(
            "QListWidget{background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;}"
            "QListWidget::item{padding:4px; border-radius:6px;}"
            "QListWidget::item:selected{background:#dbeafe; border:1px solid #3b82f6;}")
        for it in items:
            p = it["image"]
            item = QListWidgetItem()
            item.setText(it.get("name", ""))
            item.setData(Qt.UserRole, p)
            pix = QPixmap(p)
            if not pix.isNull():
                item.setIcon(QIcon(pix.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setToolTip("%s · %s\n%s" % (it.get("name", ""), it.get("type", ""), p))
            lst.addItem(item)
        lay.addWidget(lst, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("✅ 导入选中")
        ok.setObjectName("accentBtn")
        cancel = QPushButton("取消")
        cancel.setObjectName("ghostBtn")
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted:
            return
        picked = []
        for item in lst.selectedItems():
            p = item.data(Qt.UserRole)
            if p and p not in picked:
                picked.append(p)
        if not picked:
            return
        refs = list(getattr(area, "_ref_imgs", []))
        for p in picked:
            if len(refs) >= 5:
                break
            if p not in refs:
                refs.append(p)
        area._ref_imgs = refs
        if hasattr(area, "_update_ref_ui"):
            area._update_ref_ui()
        tl = getattr(area, "title_lbl", None)
        area_name = tl.text() if tl is not None else "生成区"
        self._log("已从资产管理导入 %d 张参考图到生成区「%s」" % (len(picked), area_name), "ok")

    def _asset_items_for_ep(self, ep_name):
        """返回指定分集的资产（与「资产管理」按分集筛选的可见规则保持一致）：
        episodes 含该分集、或未标记分集（全局通用）的资产都算可见。"""
        items = getattr(self, "_asset_items", []) or []
        if not ep_name:
            return items
        out = []
        for it in items:
            eps = it.get("episodes") or []
            if ep_name in eps or not eps:
                out.append(it)
        return out

    def _sync_asset_list_to_area(self, area):
        """把「当前视频生成分集」的资产列表同步给指定生成区，使其自动填充参考图
        与资产管理该分集可见内容一致（第N集只拉取标记到第N集或全局通用的资产，
        不再混入其它分集的内容）。"""
        ep_name = getattr(self, "_current_gen_episode", None)
        items = self._asset_items_for_ep(ep_name)
        area._asset_list = [it for it in items if it.get("image")]

    def _sync_all_asset_lists(self):
        """当资产库更新后，同步所有已有生成区的资产列表，并触发关键词重匹配（高亮/参考图）。"""
        for area in getattr(self, "_gen_areas", []):
            self._sync_asset_list_to_area(area)
            try:
                area._fill_assets_from_prompt()
            except Exception:
                pass
    def _gen_videos_dir_getter(self):
        return getattr(self, "_gen_videos_dir", "")

    def _gen_current_videos_dir(self):
        """返回当前视频保存目录：有当前剧集 → 生成剧集\<剧集名>\；否则项目级「生成视频」。"""
        ep = getattr(self, "_current_gen_episode", "")
        if ep:
            try:
                rec = next((e for e in self._gen_episodes if e.get("name") == ep), None)
                ep_dir = (rec or {}).get("dir") or ep
            except Exception:
                ep_dir = ep
            d = os.path.join(self._gen_episode_cards_dir, ep_dir)
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
            return d
        return getattr(self, "_gen_videos_dir", "") or os.path.join(config._EXE_DIR, "生成视频")

    def _gen_remove_area(self, area):
        if area in self._gen_areas:
            self._gen_areas.remove(area)
            self.gen_area_lay.removeWidget(area)
            area.deleteLater()
            self._gen_relayout_areas()
            self.gen_add_ep.setText("➕ 添加剧集（已 %d 个）" % len(self._gen_areas))
            self._gen_clear_merge()
            if not getattr(self, "_gen_suppress_save", False) and getattr(self, "_current_gen_episode", None):
                self._save_gen_areas_to_episode()

    def _gen_clear_merge(self):
        """清除所有生成区的合并选中状态。"""
        for a in getattr(self, "_gen_areas", []):
            if hasattr(a, "merge_btn"):
                a.merge_status_style(False)
                a._merge_selected = False
        self._merge_panel.setVisible(False)
        self._update_merge_buttons()

    def _gen_select_all_areas(self):
        """全选/取消全选所有生成区。"""
        areas = getattr(self, "_gen_areas", [])
        if not areas:
            return
        all_selected = all(getattr(a, "_merge_selected", False) for a in areas)
        for a in areas:
            if hasattr(a, "merge_btn"):
                a.merge_status_style(not all_selected)
                a._merge_selected = not all_selected
        self._merge_select_all_btn.setText("☐ 取消全选" if not all_selected else "☑ 全选")
        self._merge_selection_changed()

    def _gen_delete_selected_areas(self):
        """删除所有选中的生成区。"""
        selected = [a for a in getattr(self, "_gen_areas", [])
                    if getattr(a, "_merge_selected", False)]
        if not selected:
            QMessageBox.information(self, "删除生成区", "请先在生成区卡片上点击「合并」按钮选中要删除的区域。")
            return
        reply = QMessageBox.question(self, "删除生成区",
            "确定删除 %d 个选中的生成区？" % len(selected),
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for a in selected:
            if a in self._gen_areas:
                self._gen_areas.remove(a)
                self.gen_area_lay.removeWidget(a)
                a.deleteLater()
        self._gen_relayout_areas()
        self.gen_add_ep.setText("➕ 添加剧集（已 %d 个）" % len(self._gen_areas))
        # 清除所有选中状态
        self._gen_clear_merge()
        self._log("已删除 %d 个生成区" % len(selected), "ok")
        if getattr(self, "_current_gen_episode", None):
            self._save_gen_areas_to_episode()

    def _update_merge_buttons(self):
        """更新工具条按钮状态。"""
        selected = [a for a in getattr(self, "_gen_areas", [])
                    if getattr(a, "_merge_selected", False)]
        n = len(selected)
        # 更新删除按钮文本
        if n > 0:
            self._merge_del_btn.setText("🗑 删除选中 (%d)" % n)
        else:
            self._merge_del_btn.setText("🗑 删除选中")

    def _merge_selection_changed(self):
        """由 GenAreaWidget 调用，同步顶部工具条显示。"""
        selected = [a for a in getattr(self, "_gen_areas", [])
                    if getattr(a, "_merge_selected", False)]
        n = len(selected)
        if n > 0:
            self._merge_label.setText("已选 %d 个生成区，合并后保留第1个" % n)
            self._merge_ok_btn.setEnabled(n >= 2)
            self._merge_panel.setVisible(True)
            self._update_merge_buttons()
        else:
            self._merge_panel.setVisible(False)
            self._update_merge_buttons()

    def _gen_merge_selected(self):
        """合并所有选中的生成区：提示词拼接，时长累加，删除其余。"""
        selected = [a for a in getattr(self, "_gen_areas", [])
                    if getattr(a, "_merge_selected", False)]
        if len(selected) < 2:
            return
        target = selected[0]
        target_prompt_parts = []
        total_seconds = 0
        for a in selected:
            p = a.prompt.toPlainText().strip()
            if p:
                target_prompt_parts.append(
                    ("\n" if target_prompt_parts else "") +
                    ("%s\n" % (a.title_lbl.text().strip())) + p)
            try:
                total_seconds += int(a.seconds.currentText())
            except (ValueError, TypeError):
                pass
        if target_prompt_parts:
            target.prompt.setPlainText("\n---\n".join(target_prompt_parts))
        cur_items = {target.seconds.itemText(i) for i in range(target.seconds.count())}
        clamped = max(4, min(12, total_seconds))
        if str(clamped) in cur_items:
            target.seconds.setCurrentText(str(clamped))
        else:
            nearest = min(cur_items, key=lambda s: abs(int(s) - clamped)) if cur_items else "10"
            target.seconds.setCurrentText(nearest)
        for a in selected[1:]:
            self._gen_areas.remove(a)
            self.gen_area_lay.removeWidget(a)
            a.deleteLater()
        self._gen_relayout_areas()
        self._gen_clear_merge()
        self.gen_add_ep.setText("➕ 添加剧集（已 %d 个）" % len(self._gen_areas))
        self._log("合并 %d 个生成区 → 保留第1个，其余已删除" % len(selected), "ok")

    def _gen_merge_all_auto(self):
        """一键自动合并：按顺序贪心合并相邻分镜，每份时长不超过目标值（如6s），剧情保持连贯。"""
        areas = getattr(self, "_gen_areas", [])
        if len(areas) < 2:
            QMessageBox.information(self, "提示", "需要至少 2 个生成区才能一键合并")
            return
        # 获取用户设定的单份最大时长
        try:
            max_dur = int(self.gen_merge_duration.currentText())
        except (ValueError, TypeError):
            max_dur = 6
        max_dur = max(4, min(12, max_dur))

        # 贪心相邻合并：从前往后累加，超过 max_dur 就切一刀
        groups = []  # list of list of GenAreaWidget
        current_group = [areas[0]]
        current_dur = self._area_dur(areas[0])
        for a in areas[1:]:
            d = self._area_dur(a)
            if current_dur + d > max_dur:
                groups.append(current_group)
                current_group = [a]
                current_dur = d
            else:
                current_group.append(a)
                current_dur += d
        groups.append(current_group)

        # 用新区域替换旧区域
        new_areas = []
        for g in groups:
            first = g[0]
            # 合并提示词（相邻剧情自然衔接）
            extra_prompts = []
            for a in g[1:]:
                p = a.prompt.toPlainText().strip()
                if p:
                    extra_prompts.append(
                        ("\n" if extra_prompts else "") +
                        ("%s\n" % a.title_lbl.text().strip()) + p)
            if extra_prompts:
                existing = first.prompt.toPlainText().strip()
                first.prompt.setPlainText(existing + ("\n---\n" if existing else "") + "\n---\n".join(extra_prompts))
            # 时长按 max_dur 钳制
            combined_dur = sum(self._area_dur(a) for a in g)
            combined = max(4, min(12, combined_dur))
            cur_items = {first.seconds.itemText(i) for i in range(first.seconds.count())}
            if str(combined) in cur_items:
                first.seconds.setCurrentText(str(combined))
            else:
                nearest = min(cur_items, key=lambda s: abs(int(s) - combined)) if cur_items else str(combined)
                first.seconds.setCurrentText(nearest)
            new_areas.append(first)
            # 删除多余区域
            for a in g[1:]:
                self._gen_areas.remove(a)
                self.gen_area_lay.removeWidget(a)
                a.deleteLater()
        # 替换主区域列表
        self._gen_areas = new_areas
        self._gen_relayout_areas()
        self._gen_clear_merge()
        self.gen_add_ep.setText("➕ 添加剧集（已 %d 个）" % len(new_areas))
        total_merged = len(areas) - len(new_areas)
        self._log("一键自动合并 %d 个 → %d 个（每份最多 %ds，相邻剧情连贯）" % (len(areas), len(new_areas), max_dur), "ok")

    def _area_dur(self, area):
        """获取生成区的秒数（int）。"""
        try:
            return int(area.seconds.currentText())
        except (ValueError, TypeError):
            return 3

    def _gen_relayout_areas(self):
        """生成区面板新增/删除后收拢序号，并按左侧工具区当前宽度让面板横向撑满
        （最多一行 3 个），使最右侧面板贴近右侧「视频任务」面板，不留右侧空白。
        窗口/分栏变窄时按最小宽度自动降列、换行。"""
        for i, a in enumerate(self._gen_areas):
            a.index = i
            try:
                a.title_lbl.setText("🎬 生成区 %d" % (i + 1))
            except Exception:
                pass
        try:
            scroll = getattr(self, "gen_area_scroll", None)
            vp = scroll.viewport() if (scroll is not None and hasattr(scroll, "viewport")) else None
            w = vp.width() if vp is not None else self.gen_area_wrap.width()
            if w <= 0:
                return
            lay = self.gen_area_lay
            widgets = []
            for i in range(lay.count()):
                it = lay.itemAt(i)
                wd = it.widget() if it is not None else None
                if wd is not None and not wd.isHidden():
                    widgets.append(wd)
            n = len(widgets)
            if n == 0:
                return
            m = lay.contentsMargins()
            eff = max(360, w - m.left() - m.right())
            hsp = lay._h
            min_panel = 320          # 参考图选择器 290 + 内边距，避免压得过窄导致内容溢出
            max_cols = 3
            cols = min(max_cols, n)
            while cols > 1 and (eff - (cols - 1) * hsp) // cols < min_panel:
                cols -= 1
            col_w = (eff - (cols - 1) * hsp) // cols
            for wd in widgets:
                wd.setFixedWidth(col_w)
            lay.invalidate()
            self.gen_area_wrap.updateGeometry()
            self.gen_area_wrap.setMinimumHeight(0)
            h = lay.heightForWidth(w)
            if h > 0:
                self.gen_area_wrap.setMinimumHeight(h)
            self.gen_area_wrap.updateGeometry()
        except Exception:
            pass

    # ---------------- 视频任务面板（登记生成中的各集任务） ----------------
    _GENTASK_STYLE = {"queued": "#94a3b8", "processing": "#f59e0b",
                      "completed": "#22c55e", "failed": "#ef4444"}

    def _gen_task_update(self, stage, info):
        task_id = (info or {}).get("task_id") or ""
        video_id = (info or {}).get("video_id") or ""
        # 兼容 task_id 为空时用 video_id 做 key
        effective_key = task_id or video_id or ("t" + str(len(self._gentasks)))
        # 调试日志：记录任务状态更新
        try:
            self._log("任务面板更新: stage=%s key=%s video_id=%s task_id=%s" % (stage, effective_key[:12], video_id[:12], task_id[:12]), "debug")
        except Exception:
            pass
        if stage == "created":
            rec = dict(info)
            rec["status"] = "queued"
            self._gentasks[effective_key] = rec
            self._gen_tasks_render(force=True)
            self._gen_tasks_save()
            return
        elif effective_key in self._gentasks:
            rec = self._gentasks[effective_key]
            if stage == "progress":
                new_prog = info.get("progress")
                # 进度未变时不重刷 UI / 不写盘，降低每 tick 的无谓开销
                if rec.get("progress") == new_prog and rec.get("status") == "processing":
                    return
                rec["status"] = "processing"
                rec["progress"] = new_prog
                # 状态/进度未变不写盘；只刷新任务面板文字
                if new_prog == rec.get("_last_saved_prog"):
                    self._gen_tasks_render()
                    return
                rec["_last_saved_prog"] = new_prog
                self._gen_tasks_render()
                self._gen_tasks_save()
            elif stage == "completed":
                rec["status"] = "completed"
                rec["video_url"] = info.get("video_url", "")
                rec["local_file"] = info.get("local_file", "")
                self._gen_tasks_render()
                self._gen_tasks_save()
                if info.get("local_file"):
                    self._gen_files_refresh()
            elif stage == "failed":
                rec["status"] = "failed"
                rec["error"] = info.get("error", "")
                self._gen_tasks_render()
                self._gen_tasks_save()

    def _gen_tasks_path(self):
        """视频任务记录文件（按项目隔离），退出后重开仍可查看最近任务。"""
        try:
            if getattr(self, "_current_project", None):
                pd = self._project_dir(self._current_project)
                return os.path.join(pd, "gen_tasks.json")
        except Exception:
            pass
        return os.path.join(BASE_DIR, "gen_tasks.json")

    def _gen_tasks_save(self):
        """把视频任务面板最近记录持久化（保留最新 200 条）。"""
        try:
            recs = list(self._gentasks.values())[-200:]
            with open(self._gen_tasks_path(), "w", encoding="utf-8") as f:
                json.dump(recs, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _gen_tasks_load(self):
        """打开项目/启动后恢复上次的视频任务记录到右侧任务面板，
        并自动接续上次中断（API 后台仍在生成）的任务继续轮询。"""
        try:
            p = self._gen_tasks_path()
            if not os.path.exists(p):
                self._gentasks = {}
                self._gen_tasks_render()
                return
            with open(p, "r", encoding="utf-8") as f:
                recs = json.load(f)
            if not isinstance(recs, list):
                self._gentasks = {}
            else:
                out = {}
                for rec in recs:
                    if not isinstance(rec, dict):
                        continue
                    k = rec.get("task_id") or rec.get("video_id") or ""
                    if not k:
                        continue
                    # 上次运行中断/未完成的：保留为待接续，重启后自动从后台继续轮询，
                    # 不再直接标记失败（API 后台任务可能仍在生成）
                    if rec.get("status") in ("queued", "processing", "in_progress", "pending"):
                        rec = dict(rec)
                        rec["status"] = "pending"
                        rec["error"] = "上次运行中断，重启后自动接续"
                    out[k] = rec
                self._gentasks = out
            self._gen_tasks_render()
            self._gen_resume_pending_tasks()
        except Exception:
            self._gentasks = {}
            self._gen_tasks_render()

    # ---------------- 重启接续在途任务 ----------------

    def _gen_resume_pending_tasks(self):
        """重启后接续：对上次中断且带 video_id 的任务重新启动轮询，
        从 API 后台继续查询生成状态，完成后自动下载到本地。"""
        if not hasattr(self, "_gentasks"):
            return
        resumed = 0
        for rec in list(self._gentasks.values()):
            if rec.get("status") != "pending":
                continue
            vid = rec.get("video_id") or ""
            if not vid:
                continue
            try:
                if config.is_tokenplan_mode():
                    key = config.get_tokenplan_key_round_robin()
                    interval = 60.0
                else:
                    key = rec.get("api_key") or config.AGNES_VIDEO.get("api_key") or ""
                    interval = float(config.AGNES_VIDEO.get("interval") or 2.0)
                if not key:
                    continue
                base_url = (rec.get("base_url")
                            or config.AGNES_VIDEO.get("base_url")
                            or config.AGNES_VIDEO_DEFAULT_BASE)
                w = PollWorker(key, base_url, vid, interval=interval,
                               model=rec.get("model"), parent=self,
                               started_at=rec.get("created_at") or None)
                w.progress.connect(lambda st, rid=vid: self._gen_resume_progress(rid, st))
                w.finished_ok.connect(lambda st, rid=vid: self._gen_resume_done(rid, st))
                w.failed.connect(lambda err, rid=vid: self._gen_resume_fail(rid, err))
                self._gen_resume_workers = getattr(self, "_gen_resume_workers", [])
                self._gen_resume_workers.append(w)
                w.start()
                resumed += 1
            except Exception:
                continue
        if resumed:
            self._log("已接续 %d 个中断的视频任务，自动从后台继续轮询生成状态…" % resumed, "warn")
            self._gen_tasks_render()

    def _find_gen_task_by_video(self, vid):
        for rec in self._gentasks.values():
            if rec.get("video_id") == vid or rec.get("task_id") == vid:
                return rec
        return None

    def _gen_resume_progress(self, vid, st):
        rec = self._find_gen_task_by_video(vid)
        if rec:
            rec["status"] = "processing"
            rec["progress"] = st.get("progress") or ""
            rec["elapsed"] = st.get("elapsed") or ""
            rec["error"] = ""
            self._gen_tasks_render()

    def _gen_resume_done(self, vid, st):
        rec = self._find_gen_task_by_video(vid)
        if not rec:
            return
        rec["status"] = "completed"
        rec["video_url"] = st.get("video_url") or rec.get("video_url") or ""
        rec["progress"] = ""
        rec["elapsed"] = st.get("elapsed") or ""
        rec["error"] = ""
        self._gen_tasks_render()
        self._gen_tasks_save()
        url = rec.get("video_url") or ""
        if url:
            self._resume_download_video(rec, url)
        else:
            self._log("任务 %s 已在后台生成完成，但未返回视频地址" % vid, "warn")

    def _gen_resume_fail(self, vid, err):
        rec = self._find_gen_task_by_video(vid)
        if rec:
            rec["status"] = "failed"
            rec["error"] = str(err)
            self._gen_tasks_render()
            self._gen_tasks_save()
        self._log("接续任务 %s 失败: %s" % (vid, err), "error")

    def _resume_download_video(self, rec, url):
        """接续完成后后台下载视频到本地。"""
        try:
            from gen_area import _SaveVideoThread
            d = self._gen_current_videos_dir()
            prefix = "分镜%s" % (rec.get("episode") or "1")
            st = _SaveVideoThread(url, d, prefix, parent=self)
            st.done.connect(lambda path, rid=rec.get("video_id"): self._resume_save_done(rid, path))
            st.fail.connect(lambda err, rid=rec.get("video_id"): self._resume_save_fail(rid, err))
            self._gen_resume_saves = getattr(self, "_gen_resume_saves", [])
            self._gen_resume_saves.append(st)
            st.start()
        except Exception as e:
            self._log("接续任务自动下载失败: %s" % e, "error")

    def _resume_save_done(self, vid, path):
        rec = self._find_gen_task_by_video(vid)
        if rec:
            rec["local_file"] = path
            rec["saved_path"] = path
            self._gen_tasks_render()
            self._gen_tasks_save()
            self._gen_files_refresh()
        self._log("已保存到本地: %s" % path, "ok")

    def _resume_save_fail(self, vid, err):
        self._log("接续任务自动保存本地失败: %s" % err, "error")

    def _gen_tasks_render(self, force=False):
        """渲染任务面板。
        默认增量更新（只改已存在条目的文字/颜色，避免每次轮询 tick 都 clear()+逐项重建）；
        force=True 时全量重建（用于任务面板被外部改动、或需要重新排序/补漏时）。"""
        if not hasattr(self, "gen_task_list"):
            return
        total = {"queued": 0, "processing": 0, "completed": 0, "failed": 0}
        for rec in self._gentasks.values():
            st = rec.get("status")
            if st in total:
                total[st] += 1
        if not force:
            # 数量一致时走增量路径：逐项原地更新文字/颜色，不删不加，避免频繁重建 QListWidget
            if self.gen_task_list.count() == len(self._gentasks):
                self.gen_task_list.blockSignals(True)
                for rec in self._gentasks.values():
                    key = rec.get("task_id") or rec.get("video_id") or ""
                    it = self._item_by_data_key(key)
                    if it is None:
                        continue
                    it.setText(self._gen_task_line(rec))
                    st = rec.get("status")
                    it.setForeground(QColor(self._GENTASK_STYLE.get(st, "#cbd5e1")))
                self.gen_task_list.blockSignals(False)
            else:
                self._gen_tasks_render(force=True)
                self._gen_task_cnt_label(total)
                if total.get("completed"):
                    self._gen_files_refresh()
                return
        else:
            self.gen_task_list.blockSignals(True)
            self.gen_task_list.clear()
            for rec in self._gentasks.values():
                st = rec.get("status")
                line = self._gen_task_line(rec)
                it = QListWidgetItem(line)
                it.setForeground(QColor(self._GENTASK_STYLE.get(st, "#cbd5e1")))
                it.setData(Qt.UserRole, rec)
                it.setData(101, rec.get("task_id") or rec.get("video_id") or "")
                self.gen_task_list.addItem(it)
            self.gen_task_list.blockSignals(False)
        self._gen_task_cnt_label(total)
        # 有新本地文件时同步刷新列表
        if total.get("completed"):
            self._gen_files_refresh()

    def _item_by_data_key(self, key):
        """按任务 key（存于 UserRole=101）在现有条目里查 item；供增量渲染用。"""
        if not key:
            return None
        n = self.gen_task_list.count()
        for i in range(n):
            it = self.gen_task_list.item(i)
            if it and it.data(101) == key:
                return it
        return None

    @staticmethod
    def _gen_task_line(rec):
        st = rec.get("status")
        ep = rec.get("episode")
        name = "分镜%s" % ep if ep else "生成"
        model = rec.get("model") or ""
        stxt = {"queued": "排队中", "processing": "生成中",
                "completed": "已完成", "failed": "失败"}.get(st, st)
        line = "%s · %s · %s" % (name, stxt, model)
        if rec.get("progress"):
            line += " %s" % rec["progress"]
        elapsed = rec.get("elapsed") or ""
        if elapsed:
            line += " · %s" % elapsed
        return line

    def _gen_task_cnt_label(self, total):
        cnt = sum(total.values())
        self.gen_task_lbl.setText("共 %d 个任务　·　排队 %d　生成 %d　完成 %d　失败 %d"
                                  % (cnt, total["queued"], total["processing"],
                                     total["completed"], total["failed"]))

    def _gen_task_open(self, item):
        # 点击左侧复选框区域时仅切换勾选，不打开任务
        pos = self.gen_task_list.viewport().mapFromGlobal(QCursor.pos())
        r = self.gen_task_list.visualItemRect(item)
        if pos.x() - r.x() < 26:
            return
        rec = item.data(Qt.UserRole) or {}
        local = rec.get("local_file") or ""
        if not local or not os.path.exists(local):
            # 记录里的本地路径缺失/失效时，按分镜号扫描已保存的视频（带时间戳后缀）
            local = self._locate_gen_video_file(rec.get("episode")) or ""
            if local:
                rec["local_file"] = local
        if local and os.path.exists(local):
            self._play_gen_task_video(local)
            return
        # 本地尚未下载/保存：若有视频地址，直接在线播放，保证点击即可看
        if rec.get("video_url"):
            self._log("任务 %s 本地视频尚未保存，改为在线播放" % rec.get("task_id", ""), "warn")
            self._play_gen_task_video(rec["video_url"])
            return
        self._log("该任务无可用本地视频，无法播放", "warn")

    def _locate_gen_video_file(self, ep):
        """在生成视频目录中定位某分镜已保存的视频文件（支持 分镜N_时间戳.mp4 命名）。"""
        d = getattr(self, "_gen_videos_dir", "")
        if not ep or not d or not os.path.isdir(d):
            return ""
        try:
            prefix = "分镜%s_" % str(ep)
            cands = []
            for fn in os.listdir(d):
                fp = os.path.join(d, fn)
                if not os.path.isfile(fp):
                    continue
                if not fn.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                    continue
                if fn.startswith(prefix) or fn == ("分镜%s.mp4" % str(ep)) or fn.startswith("分镜%s." % str(ep)):
                    cands.append((os.path.getmtime(fp), fp))
            if cands:
                cands.sort(key=lambda t: t[0], reverse=True)
                return cands[0][1]
        except Exception:
            pass
        return ""

    def _play_gen_task_video(self, local):
        """点击已完成/在线任务 → 弹出悬浮播放器播放。"""
        self._gen_selected_file = local
        fp = self._get_float_player()
        if fp is None:
            self._log("无法创建播放器", "error")
            return
        fp.set_source(local)
        fp.show()
        fp.raise_()
        fp.activateWindow()
        fp.play()

    def _gen_tasks_clear(self):
        """清空所有已结束的视频任务记录（已完成/失败），保留排队/生成中的在途任务"""
        if not self._gentasks:
            return
        kept = {k: v for k, v in self._gentasks.items()
                if v.get("status") in ("queued", "processing")}
        deleted = len(self._gentasks) - len(kept)
        if deleted == 0:
            QMessageBox.information(self, "清空任务", "没有可清空的已结束记录。")
            return
        reply = QMessageBox.question(self, "清空任务",
            "确定清空 %d 个已结束的视频任务（已完成/失败）？" % deleted,
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._gentasks = kept
        self._gen_tasks_save()
        self._gen_tasks_render()
        self._log("已清空 %d 个已结束任务" % deleted, "ok")

    def _gen_tasks_toggle_select_all(self):
        """全选/取消全选视频任务"""
        if self.gen_task_list.count() == 0:
            return
        all_checked = all(self.gen_task_list.item(i).checkState() == Qt.Checked
                          for i in range(self.gen_task_list.count()))
        new_state = Qt.Unchecked if all_checked else Qt.Checked
        for i in range(self.gen_task_list.count()):
            self.gen_task_list.item(i).setCheckState(new_state)

    def _gen_tasks_delete_selected(self):
        """删除勾选的任务记录"""
        to_del = []
        for i in range(self.gen_task_list.count()):
            it = self.gen_task_list.item(i)
            if it.checkState() != Qt.Checked:
                continue
            rec = it.data(Qt.UserRole) or {}
            task_id = rec.get("task_id") or rec.get("video_id") or ""
            if task_id and task_id in self._gentasks:
                to_del.append(task_id)
        if not to_del:
            QMessageBox.information(self, "删除任务", "请先在列表中勾选要删除的任务。")
            return
        reply = QMessageBox.question(self, "删除任务",
            "确定删除 %d 个已勾选的任务？" % len(to_del),
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for task_id in to_del:
            if task_id in self._gentasks:
                del self._gentasks[task_id]
        self._gen_tasks_save()
        self._gen_tasks_render()
        self._log("已删除 %d 个勾选任务" % len(to_del), "ok")

    # ---------------- 一键全部生成/停止（二合一按钮） ----------------
    def _gen_toggle_batch(self):
        """点击按钮：未运行时开始生成，运行中时停止。"""
        if getattr(self, "_gen_batch_run", False):
            self._gen_stop_all()
        else:
            self._gen_all_start()

    def _gen_batch_status(self, text):
        if hasattr(self, "gen_status"):
            try:
                self.gen_status.setText(text)
            except Exception:
                pass
        if hasattr(self, "gen_task_lbl") and "失败" not in text and "停止" not in text:
            pass  # 任务统计由任务面板独立维护，不在状态行覆盖

    def _gen_all_start(self):
        if getattr(self, "_gen_batch_run", False):
            self._gen_batch_status("批量生成中，请先「全部停止」")
            return
        q = []
        skipped_done = 0
        for a in self._gen_areas:
            if not a.prompt.toPlainText().strip():
                continue
            if a._creating or (a._poll and a._poll.isRunning()):
                continue
            # 已生成且参数未变 → 跳过，避免停止后重复生成
            done_key = getattr(a, "_gen_done_key", "")
            if done_key:
                try:
                    if done_key == a._gen_done_marker():
                        skipped_done += 1
                        continue
                except Exception:
                    pass
            q.append(a)
        if not q:
            if skipped_done:
                QMessageBox.information(self, "一键全部生成",
                                        "全部 %d 集均已完成生成，跳过重复生成。"
                                        "\n修改提示词/画幅/时长后将自动重新生成该集。" % skipped_done)
            else:
                QMessageBox.information(self, "一键全部生成", "没有可生成的剧集。请先为生成区填写提示词。")
            return
        if skipped_done:
            self._log("一键全部生成：跳过 %d 集已生成，本次仅生成 %d 集未完成的" % (skipped_done, len(q)), "info")
        self._gen_batch_run = True
        self._gen_batch_q = q
        self._gen_batch_round = 0
        self._gen_batch_done_in_round = 0
        # 切换按钮为停止样式
        self.gen_all_btn.setText("⏹ 全部停止")
        self.gen_all_btn.setObjectName("dangerBtn")
        self._gen_429_count = 0
        self._gen_429_wait = config.AGNES_VIDEO.get("retry_wait_sec", 60)
        self._gen_batch_status("批量生成：准备生成 %d 集…%s"
                               % (len(q), ("（跳过 %d 集已生成）" % skipped_done) if skipped_done else ""))
        if config.is_tokenplan_mode():
            self._log("视频批量生成开始：使用 TokenPlan Keys（Key×%d ×5 满速并发），共 %d 集"
                      % (config.get_tokenplan_key_count(), len(q)), "ok")
        else:
            self._log("视频批量生成开始：使用普通 API Keys（Key×%d 并发），共 %d 集"
                      % (config.get_api_key_count(), len(q)), "ok")
        self._gen_batch_launch_round()

    def _gen_batch_launch_round(self):
        """第一轮：提交全部并发任务。
        默认模式并发 = Key 数；TokenPlan 模式并发 = Key 数 × 5（每 Key 5 RPM），启动即满速。"""
        if not getattr(self, "_gen_batch_run", False):
            return
        # 把429退避池中的任务并入队首，参与本轮整轮重试
        pend = getattr(self, "_gen_429_pending", [])
        if pend:
            for a in pend:
                if a not in self._gen_batch_q:
                    self._gen_batch_q.insert(0, a)
            self._gen_429_pending = []
        self._gen_429_waiting_all = False
        q = self._gen_batch_q
        if not q:
            self._gen_batch_finish()
            return
        if config.is_tokenplan_mode():
            cap = config.get_tokenplan_key_count() * 5
            launch = q[:cap]
            for a in launch:
                a._key = config.get_tokenplan_key_round_robin()
                a._start()
        else:
            cap = config.get_api_key_count()
            launch = q[:cap]
            for a in launch:
                a._key = config.get_api_key_round_robin()
                a._start()
        self._gen_batch_cap = max(cap, 1)  # 每轮并发槽位数
        self._gen_batch_active = list(launch)
        for a in launch:
            self._gen_batch_q.remove(a)
        self._gen_batch_last_submit_ts = time.time()
        self._gen_batch_status("🚀 启动 %d 路并发（第 1 轮）" % len(launch))
        self._log("视频批量生成：启动 %d 路并发" % len(launch), "info")
        # 第一轮提交后，启动定时器进行单槽位补满
        self._gen_batch_start_submit_timer()

    def _gen_batch_start_submit_timer(self):
        """启动周期性检查定时器（每 5 秒检查一次，间隔满 60s 则补一个）。"""
        if self._gen_batch_submit_timer is not None:
            self._gen_batch_submit_timer.stop()
        self._gen_batch_submit_timer = QTimer(self)
        self._gen_batch_submit_timer.setInterval(5000)  # 每 5 秒检查一次
        self._gen_batch_submit_timer.timeout.connect(self._gen_batch_check_and_submit)
        self._gen_batch_submit_timer.start()

    def _gen_batch_check_and_submit(self):
        """定时器回调：检查队列，间隔满 60s 补一批任务。
        默认模式每轮补 1 个；TokenPlan 模式把并发补满到 Key*5。"""
        if not getattr(self, "_gen_batch_run", False):
            return
        q = self._gen_batch_q
        if not q or not self._gen_batch_active:
            return
        elapsed = time.time() - self._gen_batch_last_submit_ts
        if elapsed >= self._gen_batch_interval_sec:
            # 重新计时
            self._gen_batch_last_submit_ts = time.time()
            if config.is_tokenplan_mode():
                cap = getattr(self, "_gen_batch_cap", config.get_tokenplan_key_count() * 5)
                slot = max(0, cap - len(self._gen_batch_active))
                batch = q[:slot]
                for nb in batch:
                    q.remove(nb)
                    nb._key = config.get_tokenplan_key_round_robin()
                    self._gen_batch_active.append(nb)
                    nb._start()
                if batch:
                    self._gen_batch_round += len(batch)
                    self._gen_batch_status("🚀 补足 %d 路并发（%d 集剩余）"
                                           % (len(batch), len(q)))
            else:
                # 只提交一个，然后重新计时
                next_area = q.pop(0)
                next_area._key = config.get_api_key_round_robin()
                self._gen_batch_active.append(next_area)
                next_area._start()
                self._gen_batch_round += 1
                remaining = len(q) + 1
                self._gen_batch_status("🚀 第%d集提交（%d 集剩余）"
                                       % (self._gen_batch_round, remaining))
            if not q:
                self._gen_batch_status("最后一集已提交，等待完成…")

    def _gen_check_all_429(self):
        """判断是否“全部任务均被限速”：
        仅当 429 退避池非空、且已无正在运行任务、且队列无剩余时，才停止补位定时器并安排整轮退避重试。
        返回 True 表示已安排退避，调用方应直接 return。"""
        pending = getattr(self, "_gen_429_pending", [])
        if not pending:
            return False
        if getattr(self, "_gen_batch_active", None):
            return False
        if getattr(self, "_gen_batch_q", None):
            return False
        self._gen_429_waiting_all = True
        wait_sec = getattr(self, "_gen_429_wait", 60)
        self._gen_batch_status("⏳ 所有任务均被限速，%d 秒后整轮重试…（%d 集）"
                               % (wait_sec, len(pending)))
        if getattr(self, "_gen_batch_submit_timer", None) is not None:
            self._gen_batch_submit_timer.stop()
            self._gen_batch_submit_timer = None
        QTimer.singleShot(wait_sec * 1000, self._gen_batch_launch_round)
        return True

    def _gen_batch_continue(self, area, ok):
        if not getattr(self, "_gen_batch_run", False):
            return
        self._log("批量生成回调: area_index=%d ok=%s active=%d q=%d" % (
            getattr(area, "index", -1), ok,
            len(getattr(self, "_gen_batch_active", [])),
            len(getattr(self, "_gen_batch_q", []))), "debug")
        # 检测 429
        err = getattr(area, "_last_error", "") or ""
        is_429 = "429" in str(err) or "rate limit" in str(err).lower()
        if is_429:
            # 释放该集占用的并发槽位，进入429退避池（不立即重提交，避免反复触发限流）
            self._gen_429_count += 1
            if area in self._gen_batch_active:
                self._gen_batch_active.remove(area)
            if area not in self._gen_429_pending:
                self._gen_429_pending.append(area)
            self._log("Key 被限速(429)，该集进入退避重试池，等整轮重试", "warn")
            # 当前已无真正在跑的任务且无剩余队列 → 全部被限速，统一退避后整轮重试
            self._gen_check_all_429()
            return
        # 正常完成：从活跃列表中移除
        if area in self._gen_batch_active:
            self._gen_batch_active.remove(area)
        self._gen_batch_done_in_round += 1
        # 队列为空但有429待重试项 → 也进入统一退避，避免误判为整批结束
        if self._gen_check_all_429():
            return
        if not self._gen_batch_q:
            self._gen_batch_finish()

    def _gen_batch_finish(self):
        self._gen_batch_run = False
        self._gen_429_count = 0
        self._gen_429_pending = []
        self._gen_429_waiting_all = False
        self._gen_batch_round = 0
        self._gen_batch_done_in_round = 0
        self._gen_batch_active = []
        if self._gen_batch_submit_timer is not None:
            self._gen_batch_submit_timer.stop()
            self._gen_batch_submit_timer = None
        # 切换按钮恢复生成样式
        self.gen_all_btn.setText("⚡ 一键全部生成")
        self.gen_all_btn.setObjectName("accentBtn")
        self._gen_batch_status("批量生成完成，结果已保存到本地文件栏")
        self._log("视频批量生成完成", "ok")
        self._gen_files_refresh()

    # ---------- 生成区播放控制 ----------
    def _gen_player_toggle(self):
        if self.gen_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.gen_player.pause()
        else:
            self.gen_player.play()

    def _gen_on_state(self, state):
        pass

    def _gen_on_media_status(self, status):
        pass

    def _gen_on_pos(self, ms):
        dur = self.gen_player.duration()
        if not getattr(self, "gen_seek_t", False):
            self.gen_sld.blockSignals(True)
            self.gen_sld.setValue(int(ms / max(dur, 1) * 1000) if dur > 0 else 0)
            self.gen_sld.blockSignals(False)
        self.gen_time_lbl.setText(f"{self._fmt(ms)} / {self._fmt(dur)}")

    def _gen_on_dur(self, dur):
        self.gen_sld.setMaximum(1000)
        self.gen_time_lbl.setText(f"00:00 / {self._fmt(dur)}")

    def _gen_seek(self, val):
        dur = self.gen_player.duration()
        if dur > 0:
            self.gen_player.setPosition(int(val / 1000 * dur))

    def _gen_seek_released(self):
        self.gen_seek_t = False
        self._gen_seek(self.gen_sld.value())

    def _gen_set_volume(self, val):
        self.gen_audio.setVolume(val / 100)

    def _gen_show_volpopup(self):
        self.gen_vol_t.stop()
        self.gen_player_view._relayout()
        self.gen_vol_popup.show(); self.gen_vol_popup.raise_()

    def _gen_play_file(self, path):
        """播放视频（本地路径或 http/https 地址）→ 统一弹出悬浮播放器。"""
        if not path:
            self._log("无可播放的视频地址", "warn")
            return
        self._gen_selected_file = path
        fp = self._get_float_player()
        if fp is None:
            self._log("无法创建播放器", "error")
            return
        fp.set_source(path)
        fp.show()
        fp.raise_()
        fp.activateWindow()
        fp.play()

    def _gen_stop_all(self):
        self._gen_batch_run = False
        self._gen_429_count = 0
        self._gen_429_pending = []
        self._gen_429_waiting_all = False
        self._gen_batch_q = []
        self._gen_batch_round = 0
        self._gen_batch_done_in_round = 0
        if getattr(self, "_gen_batch_submit_timer", None) is not None:
            self._gen_batch_submit_timer.stop()
        # 停止单任务模式的轮询线程（防止批量停止后轮询线程仍残留）
        if getattr(self, "_gen_poll", None) is not None and self._gen_poll.isRunning():
            self._gen_poll.stop()
            self._gen_poll.wait(3000)
        self._gen_batch_active = []
        for a in self._gen_areas:
            try:
                a._stop()
            except Exception:
                pass
        # 切换按钮恢复生成样式
        self.gen_all_btn.setText("⚡ 一键全部生成")
        self.gen_all_btn.setObjectName("accentBtn")
        self._gen_batch_status("已停止全部生成")
        self._log("已停止全部视频生成任务", "info")

    def _gen_split_storyboard(self):
        """打开剧本页的视频分析结果列表，选择一条后按分镜时间线一键创建生成区。"""
        from gen_area import parse_storyboard_report, build_shot_prompt
        if not os.path.isdir(self._results_dir):
            QMessageBox.warning(self, "提示", "当前项目暂无视频分析结果文件。\n请先在「视频嗅探」页完成视频分析。")
            return
        files = sorted(f for f in os.listdir(self._results_dir) if f.lower().endswith(".txt"))
        if not files:
            QMessageBox.warning(self, "提示", "当前项目暂无视频分析结果文件。\n请先在「视频嗅探」页完成视频分析。")
            return
        # 弹窗选择文件
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频分析结果", self._results_dir,
            "分析结果文本 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            QMessageBox.warning(self, "读取失败", "无法读取文件：%s" % e)
            return
        report = parse_storyboard_report(text)
        shots = report.get("shots") or []
        if shots:
            created = 0
            for i, shot in enumerate(shots):
                dur = max(4, min(int(round(shot[1] - shot[0])), 12))
                prompt = build_shot_prompt(report, i, shot)
                area = self._gen_add_area()
                area.preset(prompt, dur, label="分镜 %d" % (i + 1))
                created += 1
            self.gen_split_btn.setText("🎬 一键分镜（已 %d 个）" % created)
            self._log("一键分镜：从「%s」创建 %d 个生成区" % (os.path.basename(path), created), "ok")
            return
        # 兜底：按分段分析原始结构建区
        segs = report.get("segments") or []
        if not segs:
            QMessageBox.information(self, "一键分镜",
                "该分析结果中未找到可解析的分镜时间线。\n请确认分析报告中包含「分镜时间线」章节。")
            self._log("一键分镜：报告无可解析的分镜时间线", "warn")
            return
        created = 0
        for i, seg in enumerate(segs):
            start = float(seg[0] or 0)
            end = float(seg[1]) if len(seg) > 1 else start + 6.0
            dur = max(4, min(int(round(end - start)), 12))
            prompt = build_shot_prompt(report, i, (start, end, ""))
            area = self._gen_add_area()
            area.preset(prompt, dur, label="分镜 %d" % (i + 1))
            created += 1
        self.gen_split_btn.setText("🎬 一键分镜（已 %d 个）" % created)
        self._log("一键分镜：从「%s」创建 %d 个生成区" % (os.path.basename(path), created), "ok")

    def _build_generate_page(self):
        self.gen_page = self._gen_page()
        self.gen_page.setObjectName("genPage")
        self._gen_areas = []
        # 加载/切换分集期间抑制自动落盘，避免把旧分集生成区写进新分集文件
        self._gen_suppress_save = False
        self._gentasks = {}
        self._gen_add_area()
        self._gen_files_refresh()
        self._refresh_gen_key()
        # 剧集卡片列表（生成页子层）
        self._gen_episode_stk = QStackedWidget()
        self._gen_episode_stk.setObjectName("genEpStk")
        self._gen_card_page = self._build_gen_episode_card_page()
        self._gen_episode_stk.addWidget(self._gen_card_page)
        self._gen_episode_stk.addWidget(self.gen_page)
        self._gen_episode_stk.setCurrentIndex(0)
        self._gen_episode_cards = []
        self._gen_episodes_file = None
        self._gen_episode_cards_dir = None
        self._gen_card_page.setVisible(True)

    def _build_gen_episode_card_page(self):
        """生成页剧集卡片列表（点击卡片进入对应生成区）"""
        p = QWidget()
        lay = QVBoxLayout(p)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        # 标题栏
        hd = QHBoxLayout()
        t = QLabel("🎬 视频生成 · 选择剧集")
        t.setStyleSheet("font-size:15px; font-weight:800; color:#1e293b;")
        hd.addWidget(t)
        cap = QLabel("为每一集创建独立生成区，视频保存到对应剧集文件夹")
        cap.setObjectName("cap")
        hd.addWidget(cap)
        hd.addStretch(1)
        lay.addLayout(hd)

        # 卡片列表
        self.gen_card_scroll = QScrollArea()
        self.gen_card_scroll.setWidgetResizable(True)
        self.gen_card_scroll.setFrameShape(QFrame.NoFrame)
        self.gen_card_wrap = QWidget()
        self.gen_card_lay = FlowLayout(self.gen_card_wrap, margin=4, hspacing=10, vspacing=10)
        self.gen_card_scroll.setWidget(self.gen_card_wrap)
        lay.addWidget(self.gen_card_scroll, 1)

        self.gen_empty_tip = QLabel("暂无剧集\n点击「新建剧集」开始")
        self.gen_empty_tip.setAlignment(Qt.AlignCenter)
        self.gen_empty_tip.setStyleSheet("color:#94a3b8; font-size:14px;")
        lay.addWidget(self.gen_empty_tip)

        return p

    # ---- 剧集卡片管理 ----
    def _gen_episode_init(self, project_name):
        """项目打开时初始化剧集卡片数据"""
        pd = self._project_dir(project_name)
        self._gen_episodes_file = os.path.join(pd, "ep_gen.json")
        self._gen_episode_cards_dir = os.path.join(pd, "生成剧集")
        os.makedirs(self._gen_episode_cards_dir, exist_ok=True)
        # 复位：清空上一个项目残留的当前剧集/生成区，回到剧集卡片列表页
        self._current_gen_episode = None
        try:
            self._clear_gen_areas()
        except Exception:
            pass
        self._gen_episodes = self._load_gen_episodes()
        self._render_gen_episode_cards()
        # 同步资产分集卡片列表
        try:
            if hasattr(self, "_asset_ep_render_cards"):
                self._asset_ep_render_cards()
        except Exception:
            pass
        # 打开新项目默认回到卡片列表，隐藏顶部当前分集徽标
        try:
            b = getattr(self, "gen_ep_badge_lbl", None)
            if b is not None:
                b.setVisible(False)
        except Exception:
            pass
        # 打开项目默认显示剧集卡片列表（点卡片才进入对应生成区）
        try:
            if self._gen_episode_stk.currentIndex() != 0:
                self._gen_episode_stk.setCurrentIndex(0)
        except Exception:
            pass
        # 打开项目/切项目后恢复上次视频任务记录
        try:
            self._gen_tasks_load()
        except Exception:
            pass
        # 顶部返回按钮（当前不在某集生成区 → 返回首页）
        try:
            self._refresh_top_back_btn()
        except Exception:
            pass

    def _load_gen_episodes(self):
        """加载剧集卡片数据"""
        try:
            if self._gen_episodes_file and os.path.exists(self._gen_episodes_file):
                with open(self._gen_episodes_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_gen_episodes(self):
        """保存剧集卡片数据"""
        if not self._gen_episodes_file:
            return
        try:
            with open(self._gen_episodes_file, "w", encoding="utf-8") as f:
                json.dump(self._gen_episodes, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _render_gen_episode_cards(self):
        """渲染剧集卡片列表"""
        # 清空旧卡片
        for card in self._gen_episode_cards:
            card.deleteLater()
        self._gen_episode_cards.clear()
        # 清空 FlowLayout
        while self.gen_card_lay.count():
            item = self.gen_card_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._gen_episodes:
            self.gen_card_scroll.setVisible(True)
            self.gen_empty_tip.setVisible(True)
        else:
            self.gen_card_scroll.setVisible(True)
            self.gen_empty_tip.setVisible(False)

        for ep in self._gen_episodes:
            name = ep.get("name", "未命名剧集")
            ep_dir = ep.get("dir", "")
            card = EpGenCard(name, episode_count=ep.get("area_count", 0))
            card.opened.connect(lambda n=name: self._gen_episode_open(n))
            card.deleted.connect(lambda n=name: self._gen_episode_delete(n))
            card.moved.connect(lambda s, d=name: self._gen_episode_move(s, d))
            # 卡片内已弹出输入框并 emit 新名称，这里直接把 (旧名, 新名) 交给处理函数，避免二次弹窗
            card.renamed.connect(lambda new, old=name: self._gen_episode_rename(old, new))
            card.setProperty("ep_dir", ep_dir)
            self._gen_episode_cards.append(card)
            self.gen_card_lay.addWidget(card)

        # 添加 + 卡片（新建按钮）
        add_card = EpGenCardAdd()
        add_card.added.connect(self._gen_episode_new)
        self.gen_card_lay.addWidget(add_card)

        # 跨页进入时 FlowLayout 可能在拿到真实宽度前就排布，首帧只显示部分卡片。
        # 延迟到已显示、拿到视口宽度后强制重排一次，确保所有卡片一次排满。
        from PySide6.QtCore import QTimer as _QTimer
        _QTimer.singleShot(0, self._gen_cards_relayout)

    def _gen_cards_relayout(self):
        """按滚动区当前视口宽度让剧集卡片按固定 220x130 自然换行排布，
        并让卡片区高度适配所有卡片，避免首帧只留出第一行高度。"""
        try:
            w = self.gen_card_scroll.viewport().width()
            if w <= 0:
                return
            # 固定卡片尺寸（不拉伸），Flow 布局按宽度自动换行
            for i in range(self.gen_card_lay.count()):
                it = self.gen_card_lay.itemAt(i)
                wd = it.widget() if it is not None else None
                if wd is not None and not wd.isHidden():
                    wd.setFixedSize(220, 130)
            self.gen_card_lay.invalidate()
            self.gen_card_wrap.updateGeometry()
            try:
                self.gen_card_wrap.setMinimumHeight(0)
                h = self.gen_card_lay.heightForWidth(w)
                if h > 0:
                    self.gen_card_wrap.setMinimumHeight(h)
            except Exception:
                pass
            self.gen_card_wrap.updateGeometry()
        except Exception:
            pass

    def _gen_episode_new(self):
        """新建剧集"""
        # 自动生成剧集名称：第N集
        next_num = 1
        for ep in self._gen_episodes:
            m = re.match(r"第(\d+)集", ep.get("name", ""))
            if m:
                next_num = max(next_num, int(m.group(1)) + 1)
        name = f"第{next_num}集"
        # 检查重名
        if any(ep.get("name") == name for ep in self._gen_episodes):
            QMessageBox.warning(self, "新建剧集", f"已存在同名剧集「{name}」。")
            return
        # 创建目录
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
        ep_dir = os.path.join(self._gen_episode_cards_dir, safe_name)
        os.makedirs(ep_dir, exist_ok=True)
        self._gen_episodes.append({
            "name": name,
            "dir": safe_name,
            "area_count": 0,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        self._save_gen_episodes()
        self._render_gen_episode_cards()
        self._log("新建剧集「%s」" % name, "ok")

    def _gen_episode_open(self, name):
        """打开剧集生成区"""
        ep = next((e for e in self._gen_episodes if e.get("name") == name), None)
        if not ep:
            return
        ep_dir = ep.get("dir", "")
        # 切走前先把上一个剧集的生成区内容落盘，避免直接点其它卡片时丢失
        try:
            if getattr(self, "_current_gen_episode", None) and \
                    self._current_gen_episode != name:
                self._save_gen_areas_to_episode()
        except Exception:
            pass
        # 视频一律保存到当前剧集卡片目录本身（生成剧集\<剧集名>\），不再套"生成视频"子目录
        self._current_gen_episode = name
        gen_videos_dir = self._gen_current_videos_dir()
        # 切换显示
        self._gen_episode_stk.setCurrentIndex(1)
        # 顶部栏在剧名后紧跟当前分集纯文字（如「· 第2集」），由上方 proj_title 承担剧名
        try:
            b = getattr(self, "gen_ep_badge_lbl", None)
            if b is not None:
                b.setText("· %s" % name)
                b.setVisible(True)
        except Exception:
            pass
        # 更新生成区的保存路径
        self._gen_videos_dir = gen_videos_dir
        # 同步加载生成区数据
        self._log("正在加载剧集「%s」..." % name, "info")
        self._gen_load_areas_sync(name)

    def _gen_load_areas_sync(self, ep_name):
        """同步加载剧集生成区。

        加载期间抑制各生成区增删触发的自动落盘（否则清空旧区时会把旧分集的
        内容写进新分集的 gen_areas.json），全部重建后落盘一次，并让出事件循环
        使窗口在批量建区过程中保持可响应。"""
        from PySide6.QtWidgets import QApplication
        import time
        t0 = time.time()
        # 清除旧生成区
        self._gen_suppress_save = True
        try:
            self._clear_gen_areas()
            # 加载数据
            saved = self._load_gen_areas_data(ep_name)
            ep = next((e for e in self._gen_episodes if e.get("name") == ep_name), None)
            if saved:
                for st in saved:
                    area = self._gen_add_area()
                    try:
                        area.import_state(st)
                    except Exception:
                        pass
                    QApplication.processEvents()
            else:
                # 没有已保存数据：按分集记录的生成区数量重建。
                # 新建分集 area_count=0 → 一个区都不建，保持真正空白
                n = ep.get("area_count", 0) if ep else 0
                for i in range(n):
                    self._gen_add_area()
                    QApplication.processEvents()
        finally:
            self._gen_suppress_save = False
            # 加载完成后统一落盘一次（写入的是「当前分集」自己的数据，不会再混入别集）
            if getattr(self, "_current_gen_episode", None):
                self._save_gen_areas_to_episode()
        elapsed = time.time() - t0
        self._gen_files_refresh()
        self._refresh_top_back_btn()
        self._log("已加载剧集「%s」(%d个生成区, %.1fs)" % (ep_name, len(self._gen_areas), elapsed), "ok")

    def _gen_episode_delete(self, name):
        """删除剧集卡片：仅从列表移除卡片，保留已保存的生成视频文件（不删除视频）。"""
        reply = QMessageBox.question(self, "删除剧集",
            "确定删除剧集「%s」卡片？已保存的生成视频文件会保留。" % name,
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        ep = next((e for e in self._gen_episodes if e.get("name") == name), None)
        if ep:
            self._gen_episodes = [e for e in self._gen_episodes if e.get("name") != name]
            self._save_gen_episodes()
            self._render_gen_episode_cards()
            self._log("已删除剧集「%s」（已保存的生成视频保留）" % name, "info")

    def _gen_episode_move(self, src, dst):
        """拖动卡片交换剧集位置（src 拖到 dst 所在位置）"""
        if not src or not dst or src == dst:
            return
        names = [e.get("name") for e in self._gen_episodes]
        if src not in names or dst not in names:
            return
        si = names.index(src)
        di = names.index(dst)
        if si == di:
            return
        ep = self._gen_episodes.pop(si)
        self._gen_episodes.insert(di, ep)
        self._save_gen_episodes()
        self._render_gen_episode_cards()
        self._log("已调整剧集顺序：%s → %s" % (src, dst), "info")

    def _gen_episode_rename(self, old_name, new_name=None):
        """重命名剧集。new_name 为空时弹出输入框交互改名；否则直接应用传入的新名称。"""
        if new_name is None:
            name, ok = QInputDialog.getText(self, "重命名剧集", "新名称：", text=old_name)
            name = (name or "").strip()
            if not ok or not name or name == old_name:
                return
        else:
            name = (new_name or "").strip()
            if not name or name == old_name:
                return
        if any(e.get("name") == name for e in self._gen_episodes):
            QMessageBox.warning(self, "重命名剧集", f"已存在同名剧集「{name}」。")
            return
        for ep in self._gen_episodes:
            if ep.get("name") == old_name:
                old_dir = ep.get("dir", "")
                safe_name = re.sub(r'[<>:"/\\|?]', '_', name)
                ep["name"] = name
                ep["dir"] = safe_name
                # 原目录存在才重命名磁盘目录；失败不阻塞元数据保存/渲染（避免“改名了但没保存”）
                try:
                    old_path = os.path.join(self._gen_episode_cards_dir, old_dir)
                    new_path = os.path.join(self._gen_episode_cards_dir, safe_name)
                    if old_dir and old_dir != safe_name and os.path.isdir(old_path) \
                            and not os.path.exists(new_path):
                        os.rename(old_path, new_path)
                except Exception as e:
                    self._log("剧集目录重命名失败（已忽略）: %s" % e, "warn")
                break
        self._save_gen_episodes()
        self._render_gen_episode_cards()
        self._log("已重命名「%s」→「%s」" % (old_name, name), "info")

    def _gen_episode_back(self):
        """返回剧集卡片列表"""
        self._gen_episode_stk.setCurrentIndex(0)
        # 隐藏顶部当前分集徽标（已回到卡片列表）
        try:
            b = getattr(self, "gen_ep_badge_lbl", None)
            if b is not None:
                b.setVisible(False)
        except Exception:
            pass
        # 保存当前生成区数据到剧集
        self._save_gen_areas_to_episode()
        # 刷新卡片以显示最新的生成区数量
        self._render_gen_episode_cards()
        # 回到剧集列表页 → 顶部按钮显示「返回首页」
        self._refresh_top_back_btn()

    def _save_gen_areas_to_episode(self):
        """保存当前剧集的生成区数量，并把生成区内容持久化到剧集目录。"""
        if not self._gen_episodes:
            return
        cur = getattr(self, "_current_gen_episode", None)
        if not cur:
            return
        self._save_gen_areas_data(cur)
        for ep in self._gen_episodes:
            if ep.get("name") == cur:
                ep["area_count"] = len(self._gen_areas)
                break
        self._save_gen_episodes()

    def _gen_episode_areas_file(self, ep_name):
        ep = next((e for e in self._gen_episodes if e.get("name") == ep_name), None)
        if not ep:
            return ""
        d = os.path.join(self._gen_episode_cards_dir, ep.get("dir", ""))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "gen_areas.json")

    def _save_gen_areas_data(self, ep_name=None):
        """把当前内存中的生成区逐个序列化到剧集目录 gen_areas.json。"""
        cur = ep_name or getattr(self, "_current_gen_episode", None)
        if not cur:
            return
        p = self._gen_episode_areas_file(cur)
        if not p:
            return
        try:
            data = [a.export_state() for a in getattr(self, "_gen_areas", [])]
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _load_gen_areas_data(self, ep_name):
        p = self._gen_episode_areas_file(ep_name)
        if not p or not os.path.exists(p):
            return []
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _gen_current_episode(self):
        """获取当前打开的剧集名"""
        return getattr(self, "_current_gen_episode", None)

    def _clear_gen_areas(self):
        """清空所有生成区"""
        for area in self._gen_areas[:]:
            self._gen_remove_area(area)

    def _gen_global_prompt_getter(self):
        if hasattr(self, "gen_global_edit"):
            return self.gen_global_edit.toPlainText()
        return ""

    def _gen_global_aspect_getter(self):
        return getattr(self, "_gen_global_aspect", "16:9")

    def _gen_global_aspect_changed(self, *_):
        """全局画幅变化：记录当前值，并同步到所有已存在的生成区（保持整体一致）。"""
        self._gen_global_aspect = self.gen_global_aspect.currentText()
        for a in getattr(self, "_gen_areas", []) or []:
            try:
                if self.gen_global_aspect.currentText() in ASPECT_RATIOS:
                    a.aspect.setCurrentText(self.gen_global_aspect.currentText())
                    a.state_changed.emit()
            except Exception:
                pass
        self._log("已把各区画幅批量设为 %s（默认），之后各区仍可单独修改" % self._gen_global_aspect, "info")

    def _gen_storyboard_from_paste(self):
        """从粘贴框读取分镜文本，复用本地分镜解析逻辑一键创建生成区。"""
        text = self.gen_storyboard_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "分镜生成", "分镜提示词粘贴框为空，请先粘贴分镜文本。")
            return
        # 画面风格 → 全局提示词框
        style = ""
        m = re.search(r"【画面风格】([^\n]*)", text)
        if m:
            style = m.group(1).strip()
            if hasattr(self, "gen_global_edit"):
                self.gen_global_edit.setPlainText(style)
                self._log("已将画面风格填入全局提示词框", "ok")
        # 分段模式分发
        if getattr(config, "SEGMENT_SPLIT_MODE", "hdr") == "ai":
            self._gen_split_local_ai(text, style)
            return
        pat = self._gen_split_hdr_pattern()
        try:
            hdr_re = re.compile(
                r"(?:#{1,6}\s*)?(?:%s)(?=\n|\s|[（(｜|,，：:]|(?:\s*总时长)|$)" % pat,
                re.IGNORECASE)
        except re.error:
            QMessageBox.warning(self, "分镜生成", "段头正则无效：%s\n已回退默认格式。" % pat)
            pat = self._gen_split_hdr_default()
            hdr_re = re.compile(
                r"(?:#{1,6}\s*)?(?:%s)(?=\n|\s|[（(｜|,，：:]|(?:\s*总时长)|$)" % pat,
                re.IGNORECASE)
        # 过滤：←承上/→衔接 注释行中的编号不是新段头，跳过
        starts = []
        for m in hdr_re.finditer(text):
            line_start = text.rfind('\n', 0, m.start()) + 1
            prefix = text[line_start:m.start()]
            if '←' in prefix or '→' in prefix:
                continue
            starts.append(m.start())
        starts = sorted(set(starts))
        if not starts:
            QMessageBox.warning(self, "分镜生成",
                                "未识别到分镜段落。当前段头正则：\n%s" % pat)
            return
        segs = []
        for i, st in enumerate(starts):
            en = starts[i + 1] if i + 1 < len(starts) else len(text)
            chunk = text[st:en].strip("\n\r \t")
            if not chunk:
                continue
            dur = None
            dm = re.search(r"总时长\s*[：:]?\s*(\d+(?:\.\d+)?)\s*(?:每秒|秒|s)", chunk, re.IGNORECASE)
            if not dm:
                dm = re.search(r"[，,]\s*(\d+(?:\.\d+)?)\s*s\s*[）)]", chunk, re.IGNORECASE)
            if dm:
                try:
                    dur = max(2, min(int(round(float(dm.group(1)))), 12))
                except Exception:
                    dur = None
            body = re.sub(
                r"(?:#{1,6}\s*)?(?:%s)\s*"
                r"(?:[（(][^（()）]*[）)])?\s*"
                r"(?:[｜|,，]?\s*总时长\s*[：:]?\s*[\d.]+\s*(?:每秒|秒|s)?)?\s*"
                r"[｜|：:]?\s*" % pat,
                "", chunk, flags=re.IGNORECASE)
            body = body.strip().strip("，,；;。:： \t\n\r")
            if body:
                segs.append((dur, body))
        if not segs:
            QMessageBox.warning(self, "分镜生成", "分镜内容为空")
            return
        self._gen_split_create_areas(segs, "粘贴分镜")
        self._log("已按粘贴分镜创建 %d 个生成区" % len(segs), "ok")

    def _gen_split_local(self):
        """从本地分镜文件一键创建生成区。
        支持两类分镜段落（自动识别、按文件顺序切分，可跨多行）：
        - Shot 型：Shot 01（00:00–00:03，3s）：…
        - 视频编号型：视频编号01（总时长：10s）｜场景：…｜人物：…
        画面风格【画面风格】填入全局提示词框，时长自动填到对应生成区。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择分镜文件", BASE_DIR,
            "分镜文本 (*.txt *.md);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            self._log("读取分镜文件失败: %s" % e, "error")
            QMessageBox.warning(self, "本地分镜", "读取文件失败：%s" % e)
            return

        # 1) 画面风格 → 全局提示词框
        style = ""
        m = re.search(r"【画面风格】([^\n]*)", text)
        if m:
            style = m.group(1).strip()
            if hasattr(self, "gen_global_edit"):
                self.gen_global_edit.setPlainText(style)
                self._log("已将画面风格填入全局提示词框", "ok")

        # 2) 分段模式分发：AI 分段 或 按段头格式切分
        if getattr(config, "SEGMENT_SPLIT_MODE", "hdr") == "ai":
            self._gen_split_local_ai(text, style)
            return
        pat = self._gen_split_hdr_pattern()
        try:
            hdr_re = re.compile(
                r"(?:#{1,6}\s*)?(?:%s)(?=\n|\s|[（(｜|,，：:]|(?:\s*总时长)|$)" % pat,
                re.IGNORECASE)
        except re.error:
            QMessageBox.warning(self, "本地分镜", "段头正则无效：%s\n已回退默认格式。" % pat)
            pat = self._gen_split_hdr_default()
            hdr_re = re.compile(
                r"(?:#{1,6}\s*)?(?:%s)(?=\n|\s|[（(｜|,，：:]|(?:\s*总时长)|$)" % pat,
                re.IGNORECASE)
        # 过滤：←承上/→衔接 注释行中的编号不是新段头，跳过
        starts = []
        for m in hdr_re.finditer(text):
            line_start = text.rfind('\n', 0, m.start()) + 1
            prefix = text[line_start:m.start()]
            if '←' in prefix or '→' in prefix:
                continue
            starts.append(m.start())
        starts = sorted(set(starts))
        if not starts:
            QMessageBox.warning(self, "本地分镜",
                                "未识别到分镜段落。当前段头正则：\n%s" % pat)
            return

        segs = []
        for i, st in enumerate(starts):
            en = starts[i + 1] if i + 1 < len(starts) else len(text)
            chunk = text[st:en].strip("\n\r \t")
            if not chunk:
                continue
            # 时长：优先“总时长…s”（有括号/无括号均可）；Shot 型回退括号内“，Ns”
            dur = None
            dm = re.search(r"总时长\s*[：:]?\s*(\d+(?:\.\d+)?)\s*(?:每秒|秒|s)", chunk, re.IGNORECASE)
            if not dm:
                dm = re.search(r"[，,]\s*(\d+(?:\.\d+)?)\s*s\s*[）)]", chunk, re.IGNORECASE)
            if dm:
                try:
                    dur = max(2, min(int(round(float(dm.group(1)))), 12))
                except Exception:
                    dur = None
            # 正文：去掉行首前缀、段头关键字（按用户正则）与可选括号/总时长段
            body = re.sub(
                r"(?:#{1,6}\s*)?(?:%s)\s*"
                r"(?:[（(][^（()）]*[）)])?\s*"
                r"(?:[｜|,，]?\s*总时长\s*[：:]?\s*[\d.]+\s*(?:每秒|秒|s)?)?\s*"
                r"[｜|：:]?\s*" % pat,
                "", chunk, flags=re.IGNORECASE)
            body = body.strip().strip("，,；;。:： \t\n\r")
            if body:
                segs.append((dur, body))

        if not segs:
            QMessageBox.warning(self, "本地分镜", "分镜内容为空")
            return
        self._gen_split_create_areas(segs, os.path.basename(path))

    def _gen_split_hdr_default(self):
        """默认段头正则（视频编号 / 分镜 / 镜头 / Shot / Scene）。"""
        return (r"(?:视频编号\s*\d+)|(?:分镜(?:号|\.|#)?\s*\d+)|"
                r"(?:镜头\s*\d+)|(?:Shot\s*\d+)|(?:Scene\s*\d+)")

    def _gen_split_hdr_pattern(self):
        """汇总预设段头列表（config.SEGMENT_HEADER_PRESETS）：逐条转正则，多条用 | 并联。"""
        presets = list(getattr(config, "SEGMENT_HEADER_PRESETS", []) or [])
        pat = self._gen_split_nlp_to_re(presets) if presets else ""
        return pat or self._gen_split_hdr_default()

    def _gen_split_nlp_to_re(self, presets):
        """把多条大白话段头一并转换成分镜段头正则（用 | 并联）。

        逐条转换策略：
        1. 整行含半角正则特征字符（\\ [ ] ( ) . * + ? { } | ^ $）→ 原样当作正则；
        2. 整行只是「已知关键词」组合（镜头/分镜/视频编号/集编号/shot/scene/数字/编号等，
           用空格、顿号、逗号等分隔，如"镜头 分镜"）→ 关键词并联：
           任一词命中即可切分（镜头 → 镜头\\d+、分镜 → 分镜(?:号|\\.|#)?\\d+ 等）；
        3. 其余含中文/完整格式模板 → 逐字符智能转换为"完整模板"正则（任意大白话都能转）：
           - 视频编号/集编号 → 关键词 + 必含编号（\\s*\\d+\\s*）；
           - 镜头/分镜     → 关键词 + 可选编号（\\s*\\d*\\.?\\d*，编号可省略）；
           - 总时长       → 可选冒号 + 可选括号 + 时长槽 \\d+\\.?\\d*\\s*[sS秒]?\\s*；
                            若模板里 总时长 后未写任何数字/单位提示，时长槽做成可选组，
                            "总时长"与"总时长10s"两种真实段头都能匹配；
           - 数字/编号 一词或模板中的数字 → \\d*\\.?\\d*（真实编号可不同）；
           - s/S/秒 → [sS秒]?；空格 → \\s*；，、；。等 → [\\s，,、;；。]*；
           - ｜/| → \\s*[｜|]\\s*；其余中文字（是/为/场景/画面…）→ 按字面匹配。
        """
        _PH_ID = "\x00ID\x00"
        _PH_IDJ = "\x00IDE\x00"
        _PH_DUR = "\x00DUR\x00"
        _PH_DUR_OPT = "\x00DOPT\x00"
        _PH_SHOT = "\x00SHOT\x00"
        _PH_FB = "\x00FB\x00"
        _PH_COLON = "\x00COL\x00"
        _PH_OPENP = "\x00OPB\x00"
        _PH_CLOSEP = "\x00CLB\x00"

        _KNOWN_TOKENS = {
            "视频编号", "集编号", "分镜", "镜头", "幕", "场景",
            "shot", "scene", "数字", "编号",
            "镜头数字", "镜头编号", "分镜数字", "分镜编号",
            "shot数字", "shot编号", "scene数字", "scene编号",
            "视频编号数字", "集编号数字",
        }

        _TOKEN_RE = {
            "视频编号": r"(?:视频编号|集编号)\s*\d+",
            "集编号": r"(?:视频编号|集编号)\s*\d+",
            "分镜": r"分镜\s*(?:号|\.|#)?\s*\d+",
            "镜头": r"镜头\s*\d+",
            "shot": r"Shot\s*\d+",
            "scene": r"Scene\s*\d+",
            "数字": r"\s*\d+\.?\d*",
            "编号": r"\s*\d+\.?\d*",
            "场景": r"场景\s*\d*",
            "幕": r"(?:第\s*)?\d+\s*幕",
            "镜头数字": r"镜头\s*\d+",
            "镜头编号": r"镜头\s*\d+",
            "分镜数字": r"分镜(?:号|\.|#)?\s*\d+",
            "分镜编号": r"分镜(?:号|\.|#)?\s*\d+",
            "shot数字": r"Shot\s*\d+",
            "shot编号": r"Shot\s*\d+",
            "scene数字": r"Scene\s*\d+",
            "scene编号": r"Scene\s*\d+",
            "视频编号数字": r"(?:视频编号|集编号)\s*\d+",
            "集编号数字": r"(?:视频编号|集编号)\s*\d+",
        }

        def _token_regex(tok):
            """关键词并联模式下单个关键词 → 正则（按整词映射，避免子串误判）。"""
            return _TOKEN_RE.get(tok.lower(), re.escape(tok))

        def _build_custom_pat(raw):
            # 去掉模板末尾的分隔符（冒号/竖线/逗号等），
            # 让外层 wrap 的尾随 lookahead「(?=[（(｜|,，：:]|总时长)」去匹配正文中的真实边界符；
            # 否则完整模板（如 …｜场景：）因末尾已被模板自身消费而让 lookahead 落空。
            raw = (raw or "").strip().rstrip("：:|｜,，、；; \t\r\n\u3000")
            pat = raw
            # 1. 关键词占位（整词替换，字面关键词保留在占位值里）
            for kw, ph in (("视频编号", _PH_ID), ("集编号", _PH_IDJ)):
                idx = pat.find(kw)
                if idx >= 0:
                    pat = pat[:idx] + ph + pat[idx + len(kw):]
                    break
            for kw, ph in (("分镜", _PH_FB), ("镜头", _PH_SHOT)):
                idx = pat.find(kw)
                if idx >= 0:
                    pat = pat[:idx] + ph + pat[idx + len(kw):]
                    break
            # 2. 总时长槽：冒号/括号均做「可选」，模板里的数字视为示例值
            #    - 模板 总时长 后带了数字（如 总时长10s、总时长：10s）→ 时长槽整体可选；
            #    - 只带单位（总时长s）→ 数字必含、单位可选；
            #    - 什么都没带（总时长｜）→ 时长槽整体可选。
            #    真实段头有冒号无冒号、有括号无括号都能命中。
            tl = pat.find("总时长")
            if tl >= 0:
                head = pat[:tl]
                # 总时长前的左括号（（(）→ 做成可选，真实段头可带可不带
                # （跳过尾部关键词占位符后再判断）
                j = len(head) - 1
                if j >= 0 and head[j] == "\x00":
                    k = j - 1
                    while k >= 0 and head[k] != "\x00":
                        k -= 1
                    j = k - 1
                chk = head[j] if 0 <= j < len(head) else ""
                if chk in "（(":
                    p = head[:j] + _PH_OPENP + head[j + 1:]
                else:
                    p = head
                p += "总时长"
                q = tl + len("总时长")
                has_colon = False
                if q < len(pat) and pat[q] in "：:":
                    has_colon = True
                    q += 1
                openb = False
                if q < len(pat) and pat[q] in "（(":
                    openb = True
                    q += 1
                span = ""
                while q < len(pat) and pat[q] in "0123456789.sS秒":
                    span += pat[q]
                    q += 1
                closeb = False
                if q < len(pat) and pat[q] in "）)":
                    closeb = True
                    q += 1
                has_digit = any(ch.isdigit() for ch in span)
                has_unit = any(ch in "sS秒" for ch in span)
                if has_colon:
                    p += _PH_COLON
                if openb:
                    p += _PH_OPENP
                # 带示例数字/无单位提示 → 时长槽整体可选；只带单位 → 数字必含
                p += _PH_DUR_OPT if (has_digit or not has_unit) else _PH_DUR
                if closeb:
                    p += _PH_CLOSEP
                pat = p + pat[q:]
            # 3. 其余字符处理
            parts = []
            i = 0
            while i < len(pat):
                c = pat[i]
                if c == '\x00':
                    replaced = False
                    for ph_name, ph_val in (
                        (_PH_ID, r'视频编号\s*\d+\s*'),
                        (_PH_IDJ, r'集编号\s*\d+\s*'),
                        (_PH_DUR, r'\d+\.?\d*\s*[sS秒]?\s*'),
                        (_PH_DUR_OPT, r'(?:\d+\.?\d*\s*[sS秒]?\s*)?'),
                        (_PH_SHOT, r'镜头\s*\d*\.?\d*'),
                        (_PH_FB, r'分镜\s*\d*\.?\d*'),
                        (_PH_COLON, r'[:：]?'),
                        (_PH_OPENP, r'[（(]?'),
                        (_PH_CLOSEP, r'[）)]?'),
                    ):
                        if pat[i:i + len(ph_name)] == ph_name:
                            parts.append(ph_val)
                            i += len(ph_name)
                            replaced = True
                            break
                    if not replaced:
                        parts.append(re.escape(c))
                        i += 1
                elif c.isdigit():
                    j = i
                    while j < len(pat) and (pat[j].isdigit() or pat[j] == '.'):
                        j += 1
                    parts.append(r'\d*\.?\d*')
                    i = j
                elif pat[i:i + 2] in ('数字', '编号'):
                    parts.append(r'\d*\.?\d*')
                    i += 2
                elif c.isspace():
                    j = i
                    while j < len(pat) and pat[j].isspace():
                        j += 1
                    parts.append(r'\s*')
                    i = j
                elif c in '，,、;；。':
                    j = i
                    while j < len(pat) and pat[j] in '，,、;；。 \t':
                        j += 1
                    parts.append(r'[\s，,、;；。]*')
                    i = j
                elif c in ('｜', '|'):
                    # 竖线分隔符：全角｜/半角| 通用，并容忍两侧空格（AI 生成分镜常用 "| "）
                    parts.append(r'\s*[｜|]\s*')
                    i += 1
                elif c in ('s', 'S'):
                    parts.append(r'[sS秒]?')
                    i += 1
                elif c == '秒':
                    parts.append(r'[sS秒]?')
                    i += 1
                else:
                    parts.append(re.escape(c))
                    i += 1
            return ''.join(parts)

        if not presets:
            return self._gen_split_hdr_default()
        res = []
        for raw in presets:
            raw = (raw or "").strip()
            if not raw:
                continue
            # 含半角正则元字符 → 原样使用
            if re.search(r"[\\\[\]().*+?{}|^$]", raw):
                if raw not in res:
                    res.append(raw)
                continue
            # 纯已知关键词组合（"镜头 分镜"、"Shot数字"）→ 关键词并联
            toks = [t for t in re.split(r"[\s、，,;；/]+", raw) if t]
            if toks and all(t.lower() in _KNOWN_TOKENS for t in toks):
                reg = "|".join(dict.fromkeys(_token_regex(t) for t in toks))
                if reg not in res:
                    res.append(reg)
                continue
            # 完整大白话模板（"视频编号｜总时长s｜场景："、"第13集"、"场景：客厅"…）
            pat = _build_custom_pat(raw)
            if pat not in res:
                res.append(pat)
        if not res:
            return self._gen_split_hdr_default()
        return "|".join(res)

    def _gen_open_segment_settings(self):
        """分词设置弹窗（与「⚙ API 设置」同款）：分段模式 + 每行一条的大白话段头预设。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("分段设置")
        dlg.resize(460, 430)
        lay = QVBoxLayout(dlg)

        form = QFormLayout()
        form.setSpacing(10)
        mode_cb = QComboBox()
        mode_cb.addItem("按段头格式（默认）", "hdr")
        mode_cb.addItem("AI 分段（LLM）", "ai")
        cur_mode = getattr(config, "SEGMENT_SPLIT_MODE", "hdr")
        for i in range(mode_cb.count()):
            if mode_cb.itemData(i) == cur_mode:
                mode_cb.setCurrentIndex(i)
                break
        form.addRow("分段模式：", mode_cb)

        hdr_edit = QPlainTextEdit()
        hdr_edit.setPlaceholderText(
            "每行一条大白话段头，自动转正则；粘贴正则也行\n"
            "例：\n镜头数字\nScene编号\n分镜\n第X幕")
        hdr_edit.setPlainText("\n".join(getattr(config, "SEGMENT_HEADER_PRESETS", [])))
        hdr_edit.setMinimumHeight(170)
        form.addRow("段头格式（每行一条）：", hdr_edit)

        hint = QLabel("自定义段头支持大白话：如「视频编号｜总时长s｜场景：」「第13集」「镜头 分镜」，"
                      "会自动转成能正确分段的正则（编号/时长/单位均可省略或可变）；"
                      "「AI 分段」模式不使用段头正则，交由 LLM 智能切分。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        form.addRow(hint)
        lay.addLayout(form)
        lay.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_ok = QPushButton("保存")
        b_ok.setObjectName("primaryBtn")
        b_can = QPushButton("取消")
        b_can.setObjectName("ghostBtn")
        btns.addWidget(b_can)
        btns.addWidget(b_ok)
        lay.addLayout(btns)

        def _save():
            lines = [ln.strip() for ln in hdr_edit.toPlainText().split("\n") if ln.strip()]
            config.save_segment_split_config({
                "mode": mode_cb.currentData(),
                "headers": lines,
            })
            self._log("分段设置已保存（模式：%s，段头 %d 条）" % (
                "AI 分段" if config.SEGMENT_SPLIT_MODE == "ai" else "按段头格式",
                len(config.SEGMENT_HEADER_PRESETS)), "ok")
            dlg.accept()

        b_ok.clicked.connect(_save)
        b_can.clicked.connect(dlg.reject)
        dlg.exec_()

    def _gen_split_create_areas(self, segs, src_name=""):
        """把 [(seconds, body), ...] 顺序建为生成区：首个若为空区则复用。"""
        created = 0
        for dur, body in segs:
            if not body:
                continue
            if created == 0 and self._gen_areas and \
                    not self._gen_areas[0].prompt.toPlainText().strip():
                area = self._gen_areas[0]
            else:
                area = self._gen_add_area()
            area.preset(body, dur if dur is not None else 10, label="分镜 %d" % (created + 1))
            created += 1
        self.gen_split_local_btn.setText("📂 本地分镜（已 %d 个）" % created)
        self._log("本地分镜：从“%s”创建 %d 个生成区"
                  % (src_name or "—", created), "ok")

    def _gen_split_local_ai(self, text, style):
        """AI 分段：交给 GLM/Agnes 把分镜文本切分为 JSON 分镜数组。"""
        key = config.GLM_AI.get("api_key", "")
        if not key:
            QMessageBox.warning(self, "AI 分段",
                "未配置视频分析 API Key。\n请先在「AI 服务 · 视频分析」中配置后可再次尝试。")
            return
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        base_url = config.GLM_AI.get("base_url") or config.GLM_BASE_URL
        ep = ""
        for _a in ("_gen_cur_episode", "_gen_cur_name", "_cur_episode", "_gen_ep_title"):
            _v = getattr(self, _a, None)
            if _v:
                ep = str(_v)
                break
        ep_txt = ("第%s集" % ep) if ep else "当前剧集"
        prompt = (
            "# 分镜改写为 Minimax H3 参考生视频提示词\n\n"
            "你是资深影视分镜工程师。请将下面《%s》的分镜/剧本文字，严格按规范改写为"
            " minimaxh3「参考模式」生成视频提示词，供逐段生成视频。\n\n"
            "改写规范：\n"
            "1. 以『视频编号 XX（总时长：Ys）』段头为切分边界：输入中每个『视频编号』段头及其下全部内容"
            "（含其中多个镜头）整体作为一个分镜段，禁止按内部『镜头』再拆分成多段。\n"
            "2. 全部使用中文撰写，正文要求详尽明确：交代当前构图、主体外貌与画面位置、环境与灯光、"
            "动作与状态变化、镜头运动（景别/推拉摇移/角度），以及该镜头关联到的参考资产（人物/场景/道具），"
            "避免写成剧情梗概。\n"
            "3. 台词格式严格为：姓名：“台词”。在人物姓名与台词之间不得加入任何修饰词或连接语。\n"
            "4. 每一段与下一段之间不得出现衔接的重复台词或重复动作（防止 minimaxh3 重复乱说话）；"
            "各段独立收束、不拖泥带水。\n"
            "5. 每一段末尾统一注明：本段无字幕，本段无配乐。\n"
            "6. 段头严格按如下格式分段：视频编号 01（总时长：10s）| 场景：…。"
            "其中“视频编号”后为该段编号（从 01 起按顺序递增），“总时长”为该段时长（整数秒），"
            "“场景：”后填写该段所属场景。\n"
            "7. 每段画面中出现的【人物】+【道具】总数不超过 4 个（相同人物/道具可重复），"
            "【人物】+【场景】+【道具】总数不超过 5 个。\n\n%s\n\n"
            "请严格输出一个 JSON 数组（不要任何多余文字、Markdown 或注释），每个元素对应一个分镜段，格式：\n"
            "[{\"body\": \"该段完整中文提示词（含段头与全部镜头描述、规范台词与无字幕/无配乐标注）\", \"seconds\": 5}]\n"
            "seconds 优先取该段段头『总时长』的数值（如 5.4s 取 5，须为 2~12 的整数秒）；"
            "段头无总时长时，按内容合理估算 2~12 的整数秒。"
        ) % (ep_txt, text.strip()[:60000])
        self._log("AI 分段：使用 %s 切分中…" % model, "info")
        w = CombineWorker(key, base_url, model, prompt, max_tokens=16384, parent=self)
        w.done.connect(self._gen_split_local_ai_done)
        w.failed.connect(self._gen_split_local_ai_failed)
        self._gen_split_ai_worker = w
        w.start()

    def _gen_split_local_ai_done(self, resp):
        segs = self._parse_split_json(resp)
        if not segs:
            self._log("AI 分段失败：无法解析返回的分镜 JSON。返回内容：%s"
                      % (str(resp or "")[:500]), "error")
            QMessageBox.warning(self, "AI 分段",
                "AI 返回的内容未能解析为分镜段落，请更换模型或重试。")
            return
        self._gen_split_create_areas(segs)

    def _gen_split_local_ai_failed(self, err):
        self._log("AI 分段失败：%s" % err, "error")
        QMessageBox.warning(self, "AI 分段", "AI 分段失败：%s" % err)

    def _parse_split_json(self, text):
        """解析 AI 返回的分镜 JSON 数组 → [(seconds, body)]；失败返回 []。"""
        if not text:
            return []
        s = text.strip()
        # 去掉 Markdown 代码围栏
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```\s*$", "", s).strip()
        # 规范化中文弯引号（AI 常输出“ ”导致 JSON 解析失败）
        s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        # 截取首尾 [ ] 之间的 JSON
        a = s.find("["); b = s.rfind("]")
        if 0 <= a < b:
            s = s[a:b + 1]
        data = None
        try:
            data = json.loads(s)
        except Exception:
            data = None
        if data is None:
            # 整体解析失败时，逐个提取 {…} 对象兜底（容忍尾逗号、混排文字等）
            try:
                objs = re.findall(r"\{[^{}]*\}", s)
                data = []
                for o in objs:
                    o = re.sub(r",\s*}", "}", o)   # 去掉对象内尾逗号
                    try:
                        data.append(json.loads(o))
                    except Exception:
                        continue
            except Exception:
                data = None
            if not data:
                return []
        if isinstance(data, dict):
            # 顶层是对象时，尝试常见包装键
            for k in ("segments", "data", "result", "results", "shots", "items", "list"):
                v = data.get(k)
                if isinstance(v, list):
                    data = v
                    break
            else:
                return []
        segs = []
        if isinstance(data, list):
            for it in data:
                if not isinstance(it, dict):
                    continue
                body = str(it.get("body") or it.get("prompt") or "").strip()
                if not body:
                    continue
                dur = it.get("seconds")
                try:
                    dur = max(2, min(int(round(float(dur))), 12))
                except Exception:
                    dur = None
                segs.append((dur, body))
        return segs

    def _refresh_gen_key(self):
        cfg = config.AGNES_VIDEO
        # TokenPlan 优先：有 TokenPlan Key 就用它，明确标注
        tp = config.get_tokenplan_key_count()
        if config.is_tokenplan_mode():
            self.gen_key_lbl.setText("TokenPlan：Key×%d（×5 并发）" % tp)
            self.gen_key_lbl.setStyleSheet("color:#7c3aed; font-weight:700;")
            self.gen_key_lbl.setToolTip(
                "当前使用 TokenPlan Keys：共 %d 把，一键生成按 %d×5=%d 路满速并发，任务创建后超过 60s 才轮询。"
                % (tp, tp, tp * 5))
            return
        keys = cfg.get("api_keys") or []
        primary = cfg.get("api_key", "")
        if keys:
            display = "Key×%d" % len(keys)
            self.gen_key_lbl.setText("Key：" + display)
            self.gen_key_lbl.setStyleSheet("color:#16a34a; font-weight:700;")
        elif primary:
            self.gen_key_lbl.setText("Key：" + config.mask_api_key(primary))
            self.gen_key_lbl.setStyleSheet("color:#16a34a; font-weight:700;")
        else:
            self.gen_key_lbl.setText("未配置 Key")
            self.gen_key_lbl.setStyleSheet("color:#dc2626;")

    def _gen_history_path(self):
        if getattr(self, "_current_project", None):
            return os.path.join(self._project_dir(self._current_project), "gen_history.json")
        return os.path.join(BASE_DIR, "agnes_video_history.json")

    def _gen_load_history(self):
        try:
            if not os.path.exists(self._gen_history_path()):
                return []
            with open(self._gen_history_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _gen_save_history(self, records):
        try:
            with open(self._gen_history_path(), "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _gen_history_refresh(self):
        self.gen_history.clear()
        for rec in self._gen_load_history():
            st = rec.get("status", "")
            if st == "completed":
                mark = "✅"
            elif st in ("pending", "processing", "in_progress", "queued"):
                mark = "⏳"
            elif st == "failed":
                mark = "❌"
            else:
                mark = "•"
            prompt = (rec.get("prompt") or "").replace("\n", " ")[:40]
            err = rec.get("error") or ""
            item = QListWidgetItem(f"{mark} {prompt or '(空提示词)'} · {rec.get('aspect_ratio','16:9')}"
                                   + (f" ⚠{err[:24]}" if err else ""))
            item.setData(Qt.UserRole, rec)
            if err:
                item.setToolTip(err)
            self.gen_history.addItem(item)

    def _gen_history_picked(self, cur, _prev):
        if not cur:
            return
        rec = cur.data(Qt.UserRole)
        if not rec:
            return
        if rec.get("status") == "completed" and rec.get("video_url"):
            self._gen_latest_url = rec.get("video_url")
            self._gen_latest_path = rec.get("saved_path") or ""
            self._gen_batch_status("已载入历史生成视频")
            self.gen_meta.setText(("已保存 " + os.path.basename(self._gen_latest_path)) if self._gen_latest_path else "")
            self.gen_save.setEnabled(True)
            if self._gen_latest_path and os.path.exists(self._gen_latest_path):
                self._gen_open_video(self._gen_latest_path)
            elif self._gen_latest_url:
                self._gen_open_video(self._gen_latest_url)
        else:
            self._gen_batch_status(("历史任务状态：" + str(rec.get("status"))) if rec.get("status") else "")

    def _gen_history_clear(self):
        self._gen_save_history([])
        self._gen_history_refresh()

    def eventFilter(self, obj, event):
        if isinstance(obj, QLabel):
            # 资产卡片图片：双击——有图→放大预览；无图→统一走本地上传逻辑
            if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                a = obj.property("asset_img_path")
                name = obj.property("asset_data_name")
                typ = obj.property("asset_data_type")
                if name:
                    if a and isinstance(a, str) and os.path.isfile(a):
                        self._gen_show_ref_preview(a)
                    else:
                        self._asset_upload_local({"name": name, "type": typ})
                    return True
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                a = obj.property("asset_img_path")
                if a:
                    self._gen_show_ref_preview(a)
                    return True
        if isinstance(obj, QListWidget) and int(obj.property("asset_grid_maxcols") or 0) > 0:
            if event.type() == QEvent.Resize:
                self._asset_recalc_grid(obj)
                return False
        if obj is not None and bool(obj.property("gen_area_scroll")) and event.type() == QEvent.Resize:
            # 生成区面板随左侧工具区宽度重新撑满（同理由帧延迟）
            QTimer.singleShot(0, self._gen_relayout_areas)
            return False
        return super().eventFilter(obj, event)

    def _gen_show_ref_preview(self, path):
        # 单例预览框：重复打开直接复用同一实例更新内容，前一张自动被替换，避免多个对话框叠加
        dlg = getattr(self, "_ref_view_dialog", None)
        if dlg is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("参考图预览")
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
            dlg.resize(700, 500)
            _lay = QVBoxLayout(dlg)
            _img = QLabel()
            _img.setAlignment(Qt.AlignCenter)
            _lay.addWidget(_img)
            _lay.addStretch()
            dlg.setModal(False)
            # 点击预览框外部时关闭
            def on_mouse_release(e):
                if e.button() == Qt.LeftButton and not dlg.rect().contains(e.pos()):
                    dlg.close()
            dlg.mouseReleaseEvent = on_mouse_release
            self._ref_view_dialog = dlg
            self._ref_view_img = _img
        dlg.setWindowTitle("参考图预览 - " + os.path.basename(path))
        _img = self._ref_view_img
        pix = QPixmap(path)
        if pix.isNull():
            _img.setText("无法加载图片")
            _img.setStyleSheet("color:#94a3b8; font-size:14px;")
        else:
            _img.setStyleSheet("")
            _img.setPixmap(pix.scaled(860, 620, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _gen_reuse(self):
        src = self.va_text.toPlainText().strip()
        if not src:
            src = self.sub_text.toPlainText().strip()
        if not src:
            self._log("暂无嗅探/分析结果可复用，请先在「视频嗅探」页完成视频分析", "warn")
            self._gen_batch_status("暂无可用提示词，请先完成视频分析")
            self.nav_tabs.setCurrentIndex(0)
            return
        # 优先提取『核心提示词：』那一行
        core = ""
        for line in src.splitlines():
            if "核心提示词" in line:
                idx = line.find("：")
                if idx >= 0:
                    core = line[idx + 1:].strip()
                    break
        final = core if core else src
        self.gen_prompt.setPlainText(final)
        self._gen_batch_status("已复用嗅探分析提示词")
        self._log("已把视频分析提示词结果复用到视频生成页", "info")
        self.nav_tabs.setCurrentIndex(1)

    def _gen_open_settings(self):
        self._ai_services_settings(1)
        return

    def _gen_open_settings_menu(self):
        """顶栏「⚙ 设置」下拉菜单：AI 服务 / 分段设置集中到一个按钮。"""
        src = getattr(self, "ai_services_btn", None) or getattr(self, "gen_settings_btn", None)
        if src is None:
            return
        m = QMenu(self)
        act_ai = m.addAction("🎬 AI 服务")
        act_ai.setToolTip("统一配置 AI 服务（视频分析 / 视频生成 / 资产生成图），支持任意 OpenAI 兼容接口")
        act_ai.triggered.connect(lambda: self._ai_services_settings(0))
        act_seg = m.addAction("➗ 分段设置")
        act_seg.setToolTip("分段模式与段头格式预设（每行一条大白话）")
        act_seg.triggered.connect(self._gen_open_segment_settings)
        m.exec_(src.mapToGlobal(QPoint(0, src.height())))
        return

    def _gen_start(self):
        if getattr(self, "_gen_creating", False) or (self._gen_poll and self._gen_poll.isRunning()):
            self._gen_batch_status("已有任务进行中，请先停止或等待完成")
            return
        self._gen_task = None
        cfg = config.AGNES_VIDEO
        api_key = cfg.get("api_key", "")
        if not api_key:
            self._gen_batch_status("未配置 Agnes API Key")
            self._log("未配置 Agnes Video API Key，请点击「API 设置」", "warn")
            self._gen_open_settings()
            return
        prompt = self.gen_prompt.toPlainText().strip()
        if not prompt:
            self._gen_batch_status("请先填写提示词")
            return
        seconds = self.gen_seconds.currentText()
        aspect = self.gen_aspect.currentText()
        neg = self.gen_negative.text().strip()
        ref = []
        if len(ref) > 5:
            self._gen_batch_status("参考图片最多 5 张")
            return
        base_url = cfg.get("base_url") or config.AGNES_VIDEO_DEFAULT_BASE
        self.gen_go.setEnabled(False)
        self.gen_stop.setEnabled(True)
        self._gen_batch_status("正在创建生成任务…")
        self._gen_creating = True
        self._gen_create = CreateTaskWorker(
            api_key, base_url, prompt, seconds=seconds, aspect=aspect,
            negative_prompt=neg, image_urls=ref or None, parent=self)
        self._gen_create.done.connect(self._gen_on_created)
        self._gen_create.failed.connect(self._gen_on_create_failed)
        self._gen_create.start()

    def _gen_on_created(self, res):
        self._gen_creating = False
        video_id = res.get("video_id") or ""
        task_id = res.get("task_id") or ""
        if not video_id:
            self._gen_on_create_failed("创建任务响应缺少 video_id：%s" % json.dumps(res.get("raw", {}), ensure_ascii=False)[:300])
            return
        self._gen_task = res
        self._gen_batch_status("任务已创建，等待生成…")
        self._log(f"Agnes 视频任务已创建 video_id={video_id} task_id={task_id}", "info")

        rec = {"task_id": task_id, "video_id": video_id,
               "prompt": self.gen_prompt.toPlainText().strip(),
               "negative_prompt": self.gen_negative.text().strip(),
               "seconds": self.gen_seconds.currentText(),
               "aspect_ratio": self.gen_aspect.currentText(),
               "status": "pending", "video_url": "", "saved_path": "",
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        recs = self._gen_load_history()
        recs.insert(0, rec)
        self._gen_save_history(recs)
        self._gen_history_refresh()

        cfg = config.AGNES_VIDEO
        base_url = cfg.get("base_url") or config.AGNES_VIDEO_DEFAULT_BASE
        self._gen_poll = PollWorker(cfg.get("api_key", ""), base_url, video_id,
                                    interval=float(cfg.get("interval") or 2.0), parent=self)
        self._gen_poll.progress.connect(self._gen_on_progress)
        self._gen_poll.finished_ok.connect(self._gen_on_done)
        self._gen_poll.failed.connect(self._gen_on_fail)
        self._gen_poll.start()

    def _gen_on_create_failed(self, err):
        self._gen_creating = False
        self.gen_go.setEnabled(True)
        self.gen_stop.setEnabled(False)
        self._gen_batch_status("创建任务失败")
        self._log(f"视频生成创建失败: {err}", "error")
        if self._gen_task:
            self._update_history_status("failed", err)
        else:
            rec = {"task_id": "", "video_id": "",
                   "prompt": self.gen_prompt.toPlainText().strip(),
                   "negative_prompt": self.gen_negative.text().strip(),
                   "seconds": self.gen_seconds.currentText(),
                   "aspect_ratio": self.gen_aspect.currentText(),
                   "status": "failed", "video_url": "", "saved_path": "",
                   "error": str(err),
                   "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            recs = self._gen_load_history()
            recs.insert(0, rec)
            self._gen_save_history(recs)
            self._gen_history_refresh()

    def _gen_on_progress(self, st):
        prog = st.get("progress")
        txt = "生成中…"
        if prog is not None:
            txt += f" {prog}"
        self._gen_batch_status(txt)
        self._update_history_status("processing", "", prog)

    def _gen_on_done(self, st):
        url = st.get("video_url") or ""
        self.gen_go.setEnabled(True)
        self.gen_stop.setEnabled(False)
        if not url:
            self._gen_on_fail("任务完成但响应中未找到视频地址")
            return
        self._gen_latest_url = url
        self._gen_latest_path = ""
        self._gen_batch_status("✅ 生成完成")
        self.gen_meta.setText(url[:80])
        self.gen_save.setEnabled(True)
        self._gen_open_video(url)
        self._update_history_status("completed", "", "", url)
        self._log("视频生成完成", "ok")

    def _gen_on_fail(self, err):
        self.gen_go.setEnabled(True)
        self.gen_stop.setEnabled(False)
        self._gen_batch_status("生成失败")
        self._log(f"视频生成失败: {err}", "error")
        self._update_history_status("failed", err)

    def _update_history_status(self, status, err="", prog="", url=""):
        recs = self._gen_load_history()
        if not recs or not self._gen_task:
            self._gen_history_refresh()
            return
        vid = self._gen_task.get("video_id")
        changed = False
        for r in recs:
            if r.get("video_id") == vid:
                r["status"] = status
                if err:
                    r["error"] = err
                if url:
                    r["video_url"] = url
                changed = True
                break
        if changed:
            self._gen_save_history(recs)
        self._gen_history_refresh()

    def _gen_stop(self):
        if self._gen_poll and self._gen_poll.isRunning():
            self._gen_poll.stop()
            self._gen_poll.wait(3000)
        self._gen_creating = False
        self.gen_go.setEnabled(True)
        self.gen_stop.setEnabled(False)
        self._gen_batch_status("已停止")

    def _gen_open_video(self, target):
        try:
            fp = self._get_float_player()
            if fp is None:
                self._log("无法创建播放器", "error")
                return
            fp.set_source(target)
            fp.show()
            fp.raise_()
            fp.activateWindow()
            fp.play()
        except Exception as e:
            self._log(f"预览打开失败: {e}", "error")

    def _gen_play_toggle(self):
        if not self._gen_latest_url and not self._gen_latest_path:
            self._gen_batch_status("暂无视频可播放，请先生成或选择历史记录")
            return
        self._gen_open_video(self._gen_latest_path or self._gen_latest_url)

    def _gen_save(self):
        url = self._gen_latest_url
        if not url:
            self._gen_batch_status("暂无可用视频地址")
            return
        default_dir = config.DEFAULT_DOWNLOAD_PATH
        ts = time.strftime("%Y%m%d_%H%M%S")
        default = os.path.join(default_dir, f"AI生成视频_{ts}.mp4")
        path, _ = QFileDialog.getSaveFileName(self, "保存生成的视频", default, "视频文件 (*.mp4)")
        if not path:
            return
        save_button_text = self.gen_save.text()
        self.gen_save.setText("下载中…")
        self.gen_save.setEnabled(False)
        self._gen_batch_status("正在下载到本地…")
        self._gen_dl_path = path
        try:
            done_path = download_video(url, path)
            self._gen_latest_path = done_path
            self._gen_batch_status("✅ 已保存：" + done_path)
            self.gen_meta.setText(os.path.basename(done_path))
            self._update_history_saved(done_path)
            self._log(f"生成视频已保存: {done_path}", "ok")
        except Exception as e:
            self._gen_batch_status("保存失败")
            self._log(f"保存生成视频失败: {e}", "error")
        finally:
            self.gen_save.setText(save_button_text)
            self.gen_save.setEnabled(True)

    def _update_history_saved(self, path):
        recs = self._gen_load_history()
        if not recs or not self._gen_task:
            return
        vid = self._gen_task.get("video_id")
        for r in recs:
            if r.get("video_id") == vid:
                r["saved_path"] = path
                break
        self._gen_save_history(recs)
        self._gen_history_refresh()

    def _setup_web_view(self):
        # 网页引擎仅在后台使用（负责加载链接、发出媒体请求供嗅探），不显示在界面上
        self.web_view = QWebEngineView(self)
        self.web_view.hide()
        self.web_view.titleChanged.connect(lambda t: self.setWindowTitle(f"幻镜AI - {t}"))
        profile = QWebEngineProfile.defaultProfile()
        profile.setUrlRequestInterceptor(self.interceptor)
        # 注入 fetch/XHR/video.src hook：在文档创建时自动执行，确保页面 JS 运行前 hook 就位
        from PySide6.QtWebEngineCore import QWebEngineScript
        script = QWebEngineScript()
        script.setName("media_hook")
        script.setSourceCode(MEDIA_HOOK_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setRunsOnSubFrames(True)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        profile.scripts().insert(script)
        self.web_view.loadFinished.connect(self._on_load_finished)
        self.web_view.setUrl(QUrl("about:blank"))
        # 输入框内容变化时动态刷新识别到的链接数
        self.input_edit.textChanged.connect(self._update_count)
        self._update_count()

    # ---------- URL 识别与批量浏览 ----------
    @staticmethod
    def extract_urls(text):
        """从整段文字(含中文/反引号/换行)中识别所有 http/https 网址"""
        # 排除空白、角括号、反引号、引号与中文全角标点；保留 ASCII 的 . ? : & = 以完整截取域名/查询串
        found = re.findall(
            r'https?://[^\s<>\x60"\u2018\u2019\u201c\u201d，。；：！？（）【】“”]+',
            text or "")
        urls = []
        for u in found:
            u = re.sub(r"[。，,;；.:：!！?？）)】\]]+$", "", u)
            if u and u not in urls:
                urls.append(u)
        return urls

    def _update_count(self):
        n = len(self.extract_urls(self.input_edit.toPlainText()))
        self.count_lbl.setText(f"识别链接: {n} 个")

    def _open_url(self):
        urls = self.extract_urls(self.input_edit.toPlainText())
        if not urls:
            QMessageBox.information(self, "提示", "未识别到网址。请粘贴包含 http/https 链接的分享内容。")
            return
        if len(urls) == 1:
            self._log(f"单链接打开: {urls[0]}", "info")
            self._open_page(urls[0], None)
        else:
            self._log(f"识别到 {len(urls)} 个网址，开始批量播放…", "info")
            self._start_batch(urls)

    def _open_page(self, url, sign):
        self._load_sign = sign
        self.web_view.setUrl(QUrl(url))

    def _start_batch(self, urls):
        self._batch_urls = urls
        self._batch_idx = -1
        self.stop_browse_btn.setEnabled(True)
        self._advance()

    def _advance(self):
        self._batch_idx += 1
        if self._batch_idx >= len(self._batch_urls):
            self._finish_batch()
            return
        total = len(self._batch_urls)
        cur = self._batch_urls[self._batch_idx]
        self.status_lbl.setText(f"批量浏览: 第 {self._batch_idx + 1}/{total} 页")
        self._log(f"({self._batch_idx + 1}/{total}) 打开: {cur}", "info")
        self._open_page(cur, self._batch_idx)

    def _on_load_finished(self, ok):
        sign = self._load_sign
        self._load_sign = None
        # 页面加载完成后立即注入 fetch/XHR/video.src hook
        try:
            self.web_view.page().runJavaScript(MEDIA_HOOK_JS, 0)
        except Exception:
            pass
        if sign is None:
            return
        total = len(self._batch_urls)
        if not ok:
            self.status_lbl.setText(f"第 {sign + 1}/{total} 页加载失败，继续下一个…")
            self._log(f"  ✗ 第 {sign + 1} 页加载失败 ({self.web_view.url().toString()})", "warn")
        else:
            self.status_lbl.setText(f"正在播放第 {sign + 1}/{total} 页，停留 {self.hold_spin.value()} 秒…")
        # 停留一段时间（期间 JS 扫描 + 网络拦截持续嗅探），随后自动切换
        QTimer.singleShot(int(self.hold_spin.value() * 1000), self._advance)

    def _finish_batch(self):
        self._batch_urls = []
        self._batch_idx = -1
        self._load_sign = None
        self.stop_browse_btn.setEnabled(False)
        found = self.epgrid.count()
        self.status_lbl.setText(f"批量浏览完成，共识别到 {found} 个视频")
        self._log(f"批量浏览结束，共识别到 {found} 个视频。勾选后即可下载。", "ok")
        self._update_download_btn()

    def _stop_browse(self):
        self._batch_urls = []
        self._batch_idx = -1
        self._load_sign = None
        self.stop_browse_btn.setEnabled(False)
        self.status_lbl.setText("已停止批量浏览")
        self._log("已停止批量浏览，已识别到的视频保留。", "warn")

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _open_download_dir(self):
        d = self.dir_edit.text().strip() or config.DEFAULT_DOWNLOAD_PATH
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as e:
            QMessageBox.warning(self, "无法打开", f"无法打开下载目录:\n{e}")

    # ---------- 嗅探 ----------
    def _on_media_url(self, media_url, referer):
        self._add_media(media_url, referer)

    def _run_page_scan(self):
        try:
            self.web_view.page().runJavaScript(MEDIA_SCANNER, 0, self._on_scan_result)
            self.web_view.page().runJavaScript(PAGE_META_SCANNER, 0, self._on_page_meta)
        except Exception:
            pass

    def _on_page_meta(self, result):
        if not result:
            return
        try:
            data = json.loads(result)
        except Exception:
            return
        text = (data or {}).get("text", "") or ""
        self._page_desc = text
        m = EP_RE.search(text)
        if not m:
            self._page_ep = ""
            return
        num = m.group(1)
        self._page_ep = f"第{int(num)}集" if num.isdigit() else f"第{num}集"
        # 识别到集数后，同步补进此前标题缺集数的媒体（应对媒体先于描述被嗅探的时序）
        grid = getattr(self, "epgrid", None)
        if grid is not None:
            for idx, info in enumerate(grid._data):
                t = info.get("title") or ""
                if t and not EP_RE.search(t):
                    base = EP_RE.sub("", _clean_title(t)).strip("_ ")
                    rep = f"{base}_{self._page_ep}" if base else self._page_ep
                    info["title"] = rep
                    grid.update_title(idx, rep)
            self._save_ep_list()

    def _on_scan_result(self, result):
        if not result:
            return
        try:
            urls = json.loads(result)
        except Exception:
            return
        cur = self.web_view.url().toString()
        for u in urls or []:
            self._add_media(u, cur)

    def _is_m3u8(self, url):
        return ".m3u8" in url.lower().split("?")[0]

    def _toggle_capture(self):
        self._capture_mode = not self._capture_mode
        btn = getattr(self, "capture_btn", None)
        if btn:
            btn.setChecked(self._capture_mode)
            btn.setText("🎯 抓包开" if self._capture_mode else "🎯 抓包关")
            btn.setStyleSheet(("background:#2563eb;color:#fff;border-radius:8px;padding:6px 12px;"
                               if self._capture_mode else ""))
        self._log("抓包已开启：播放时自动把可用的视频链接写入粘贴框" if self._capture_mode
                  else "抓包已关闭", "ok" if self._capture_mode else "info")

    def _append_capture_to_input(self, urls):
        """开启抓包时，把可用的 m3u8 / mp4 / flv 播放地址去重后追加到粘贴框。"""
        if not self._capture_mode:
            return
        edit = getattr(self, "input_edit", None)
        if edit is None:
            return
        existing = edit.toPlainText()
        got = []
        for u in urls or []:
            u = (u or "").strip()
            low = u.lower().split("?")[0]
            if not u.startswith("http"):
                continue
            if not (".m3u8" in low or ".mp4" in low or ".flv" in low):
                continue  # 忽略 .ts 分片及其它资源，只要播放列表或整段视频
            if u in existing:
                continue
            got.append(u)
        if not got:
            return
        seen = list(dict.fromkeys(got))
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        edit.setPlainText(existing + sep + "\n".join(seen))

    # ---- 本地 MITM 代理抓包（抓第三方客户端/网页）----
    def _proxy_running(self):
        return self._mitm_worker is not None and self._mitm_worker.isRunning()

    def _start_proxy(self):
        if self._proxy_running():
            self._log("本地代理已在运行", "info")
            return
        try:
            ca_key, ca_cert = mitm_ensure_ca(self._ca_key_path, self._ca_cert_path)
        except Exception as e:
            self._log("生成 CA 失败：" + str(e), "err")
            return
        self._ca_pem = ca_cert
        w = MitmWorker("127.0.0.1", self._proxy_port, ca_key, ca_cert, self)
        w.media.connect(self._on_proxy_media)
        w.log.connect(lambda s: self._log(s, "info"))
        self._mitm_worker = w
        w.start()
        self._log(f"本地抓包代理启动中：127.0.0.1:{self._proxy_port}", "ok")
        self._log(f"请把要抓的客户端/浏览器“手动代理”指到 127.0.0.1:{self._proxy_port}，"
                  "或在系统代理中一并开启。", "info")

    def _stop_proxy(self):
        if self._mitm_worker:
            self._mitm_worker.stop()
            self._mitm_worker.wait(3000)
            self._mitm_worker = None
        if self._sys_proxy_on:
            mitm_set_proxy(False, self._proxy_port, log=lambda s: self._log(s, "info"))
            self._sys_proxy_on = False
        self._log("本地抓包代理已停止", "info")

    def _install_ca(self):
        if not self._ca_pem:
            try:
                _, self._ca_pem = mitm_ensure_ca(self._ca_key_path, self._ca_cert_path)
            except Exception as e:
                self._log("生成 CA 失败：" + str(e), "err")
                return
        ok = mitm_install_ca(self._ca_pem, log=lambda s: self._log(s, "info"))
        if ok:
            self._log("受信 CA 已装好；https 视频地址可被抓取", "ok")

    def _toggle_sys_proxy(self, on):
        self._sys_proxy_on = bool(on)
        mitm_set_proxy(self._sys_proxy_on, self._proxy_port,
                       log=lambda s: self._log(s, "info"))
        w = self._mitm_worker
        if w is not None:
            w.set_sys_proxy(bool(on))
        if self._sys_proxy_on and not self._proxy_running():
            self._start_proxy()

    def _on_proxy_media(self, url):
        self._append_from_proxy(url)

    def _diag_proxy(self):
        """诊断代理状态，把结果输出到日志。"""
        lines = []
        # 1. 代理是否在监听
        listening = False
        try:
            import socket as _sk
            t = _sk.create_connection(("127.0.0.1", self._proxy_port), timeout=1)
            t.close()
            listening = True
        except Exception as e:
            lines.append(f"× 端口 {self._proxy_port} 未监听: {e}")
        if listening:
            lines.append(f"✓ 端口 {self._proxy_port} 正在监听")
        # 2. 代理内部状态
        w = self._mitm_worker
        if w and w._proxy:
            st = w._proxy.status
            lines.append(f"  运行中: {st['running']}")
            lines.append(f"  连接数: {st['connections']}")
            lines.append(f"  已抓媒体: {st['media_found']} 条")
            if st['last_error']:
                lines.append(f"  最近错误: {st['last_error']}")
        else:
            lines.append("× 代理未启动（MitmWorker 为空）")
        # 3. CA 文件
        if os.path.exists(self._ca_cert_path):
            lines.append(f"✓ CA 证书文件存在: {self._ca_cert_path}")
        else:
            lines.append(f"× CA 证书文件不存在: {self._ca_cert_path}")
        # 4. 系统代理设置
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            en, _ = winreg.QueryValueEx(key, "ProxyEnable")
            try:
                srv, _ = winreg.QueryValueEx(key, "ProxyServer")
            except Exception:
                srv = "(未设置)"
            winreg.CloseKey(key)
            lines.append(f"  系统代理: {'开' if en else '关'} → {srv}")
            if not en:
                lines.append("  ⚠ 系统代理未开启，浏览器/客户端不会走抓包代理")
        except Exception as e:
            lines.append(f"× 读取系统代理设置失败: {e}")
        # 5. CA 是否在受信根
        try:
            r = subprocess.run(["certutil", "-user", "-store", "Root", "HuGuo MITM CA"],
                               capture_output=True, text=True, timeout=5)
            if "HuGuo MITM CA" in (r.stdout or ""):
                lines.append("✓ CA 已在受信根证书库")
            else:
                lines.append("× CA 不在受信根证书库（需点「安装受信 CA」）")
        except Exception as e:
            lines.append(f"× 查询 CA 失败: {e}")
        # 输出
        lines.append("─" * 40)
        if listening and (w and w._proxy and w._proxy.status['connections'] == 0):
            lines.append("诊断：代理在监听但收到 0 个连接 → 客户端没走代理。")
            lines.append("解决：①确认已开「系统代理(全局)」；②或手动在客户端/浏览器设置代理 127.0.0.1:" + str(self._proxy_port))
            lines.append("③部分桌面客户端(红果/番茄 App)可能不走系统代理，需在其设置里找代理配置")
        elif listening and (w and w._proxy and w._proxy.status['connections'] > 0 and w._proxy.status['media_found'] == 0):
            lines.append("诊断：有连接进来但没抓到媒体 → 可能 CA 不受信(https握手失败)或客户端证书锁定。")
            lines.append("解决：①确认已装受信 CA；②浏览器需重启生效；③部分 App 有证书锁定无法解密")
        for ln in lines:
            self._log(ln, "info")
        QMessageBox.information(self, "抓包诊断", "\n".join(lines))

    def _append_from_proxy(self, url):
        """把代理抓到的可用媒体地址写入粘贴框。"""
        edit = getattr(self, "input_edit", None)
        if edit is None:
            return
        u = (url or "").strip()
        if not u.startswith("http"):
            return
        existing = edit.toPlainText()
        if u in existing:
            return
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        edit.setPlainText(existing + sep + u)
        self._log("已把抓到的视频地址写入粘贴框: " + (u[-70:]), "ok")

    def _open_capture_menu(self):
        m = QMenu(self)
        a = m.addAction("🎯 抓包(内置浏览器)：" + ("开" if self._capture_mode else "关"))
        a.setCheckable(True)
        a.setChecked(self._capture_mode)
        a.toggled.connect(lambda _on: self._toggle_capture())
        m.addSeparator()
        if self._proxy_running():
            m.addAction("⏹ 停止本地代理").triggered.connect(self._stop_proxy)
        else:
            m.addAction("🚀 启动本地代理(抓第三方)").triggered.connect(self._start_proxy)
        m.addAction("🔑 安装受信 CA(一次)").triggered.connect(self._install_ca)
        sa = m.addAction("🌐 系统代理(全局)：开" if self._sys_proxy_on else "🌐 系统代理(全局)：关")
        sa.setCheckable(True)
        sa.setChecked(self._sys_proxy_on)
        sa.toggled.connect(self._toggle_sys_proxy)
        m.addSeparator()
        m.addAction("🔍 诊断抓包状态").triggered.connect(self._diag_proxy)
        m.addSeparator()
        st = "运行中" if self._proxy_running() else "未运行"
        tip = m.addAction(f"本地代理 {st} · 端口 {self._proxy_port}\n"
                          f"需在目标客户端/浏览器把代理指向 127.0.0.1:{self._proxy_port}")
        tip.setEnabled(False)
        m.exec_(self.capture_btn.mapToGlobal(
            self.capture_btn.rect().bottomLeft()))

    def _run_consolidate(self):
        if self._consolidating:
            return
        grid = getattr(self, "epgrid", None)
        if grid is None or not grid.count():
            return
        m3u8s = [d["url"] for d in grid._data if d.get("is_m3u8")]
        m3u8s = list(dict.fromkeys(m3u8s))
        if len(m3u8s) < 2:
            return  # 只有一条或没有 m3u8，无需整合
        w = PlaylistProbeWorker(m3u8s, headers=getattr(self, "_dl_headers", None), parent=self)
        w.done.connect(self._on_consolidate)
        w.finished.connect(w.deleteLater)
        self._consolidate_worker = w
        self._consolidating = True
        w.start()

    def _on_consolidate(self, result_map):
        self._consolidating = False
        grid = getattr(self, "epgrid", None)
        if grid is None:
            return
        # 按 (标题, 来源页) 分组：仅同集(标题一致且在同一个页面)才考虑整合，
        # 避免把不同剧集的同名行误合并。
        by_key = {}
        for info in grid._data:
            if not info.get("is_m3u8"):
                continue
            key = (info.get("title") or "", info.get("referer") or "")
            n = (result_map.get(info.get("url", "")) or (0, 0))[0]
            by_key.setdefault(key, []).append((info.get("url", ""), n))
        drop = []
        changed = False
        for key, rows in by_key.items():
            best = max(rows, key=lambda r: r[1])
            if best[1] <= 0:
                continue  # 探测失败，一律不动，保守不删
            for url, n in rows:
                # 仅当较短行明显是预览版才整合：分片数严格更多且 ≤ 一半（全新比预览长得多）
                if url != best[0] and 0 < n < best[1] and n * 2 <= best[1] and url not in drop:
                    drop.append(url)
                    changed = True
        if changed:
            if grid.remove_urls(drop):
                for u in drop:
                    self.media_repo.pop(u, None)
                self._save_ep_list()
                self._update_download_btn()
                self._log("已整合同集预览版/完整版 m3u8，保留最长一条", "ok")

    def _add_media(self, url, referer):
        if not url or not url.startswith("http"):
            return
        # 抓包模式开启时，把可用视频链接同时写入粘贴框
        self._append_capture_to_input([url])
        if url in self.media_repo:
            return
        is_m3u8 = self._is_m3u8(url)
        base_title = _clean_title(self.web_view.title() or self._suggest_title(url))
        page_ep = getattr(self, "_page_ep", "")
        # 页面标题没带集数时，用描述文本里识别到的“第N集”并入，作为下载命名依据
        if page_ep and not EP_RE.search(base_title):
            base_title = f"{base_title}_{page_ep}" if base_title else page_ep
        title = _clean_title(base_title)
        self.media_repo[url] = {
            "referer": referer or "",
            "is_m3u8": is_m3u8,
            "title": title,
        }

        self.epgrid.add(url, referer or "", is_m3u8, title)
        self.epgrid.show_all()
        self._save_ep_list()
        self._update_download_btn()
        self._log(f"嗅探到视频: {url}", "ok")

    @staticmethod
    def _display(url):
        try:
            from urllib.parse import urlparse
            p = urlparse(url)
            return (p.hostname or "") + p.path[-50:]
        except Exception:
            return url[-50:]

    @staticmethod
    def _suggest_title(url):
        seg = url.split("?")[0].strip("/").split("/")
        return seg[-1] if seg else "video"

    def _select_all(self):
        # 全选切换：全部选中时点一次则全部取消
        self.epgrid.select_all_toggle()
        self._update_download_btn()

    def _grid_play(self, idx):
        info = self.epgrid.info_at(idx)
        url = info.get("url", "")
        if not url:
            return
        self._log(f"预览播放: {url}", "info")
        self.player.stop()
        self.player.setSource(QUrl(url))
        self.player.play()
        self.epgrid.set_playing(idx)
        self._playing_idx = idx
        self._eq_frame = 0
        self._eq_timer.start()

    # ---------- 字幕识别（SubtitleOCR / 望言OCR） ----------
    def _grid_ocr(self, idx):
        info = self.epgrid.info_at(idx)
        url = info.get("url", "")
        if not url:
            return
        is_local = bool(info.get("is_local"))
        title = info.get("title", "") or f"第{idx + 1}集"
        self._start_ocr(url, is_m3u8=is_local, title=title, ep_idx=idx)

    def _grid_vanalyze(self, idx):
        info = self.epgrid.info_at(idx)
        url = info.get("url", "")
        if not url:
            return
        is_local = bool(info.get("is_local"))
        if is_local:
            self._va_analyze_local(url)
        else:
            self._va_analyze_one(idx)

    def _grid_add_local(self):
        if getattr(self, "_va_running", False):
            self._log("视频分析正在运行，请等待完成", "warn")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择本地视频文件（可多选）", "",
            "视频文件 (*.mp4 *.mkv *.mov *.avi *.flv *.wmv *.webm *.ts);;所有文件 (*)")
        if not paths:
            return
        added = 0
        for p in paths:
            name = os.path.basename(p)
            self.epgrid.add(p, is_m3u8=False, title=name, is_local=True)
            added += 1
        self.epgrid.show_all()
        self._update_download_btn()
        self._log(f"已添加 {added} 个本地视频", "ok")

    def _batch_ocr(self):
        if getattr(self, "_ocr_worker", None) and self._ocr_worker.isRunning():
            self._log("字幕识别正在运行，请等待完成", "warn")
            return
        n = self.epgrid.count()
        if n == 0:
            self._log("当前没有已识别的剧集，请先打开分享链接", "warn")
            return
        self._ocr_total = n
        self._ocr_queue = list(range(n))
        self.sub_text.setPlainText("")
        self.sub_status.setText(f"批量识别开始：共 {n} 集")
        self.sub_batch.setText("⏳ 批量识别中…")
        self._log(f"开始批量字幕识别，共 {n} 集", "info")
        self._ocr_run_next()

    def _ocr_run_next(self):
        total = getattr(self, "_ocr_total", 0)
        if not self._ocr_queue:
            self._ocr_batch = False
            self.sub_batch.setText("🎬 批量识别")
            self.sub_status.setText(f"批量识别完成 · 共 {total} 集")
            self._log(f"批量字幕识别完成，共 {total} 集", "ok")
            return
        idx = self._ocr_queue.pop(0)
        info = self.epgrid.info_at(idx)
        url = info.get("url", "")
        if not url:
            self.sub_status.setText(f"[{total - len(self._ocr_queue) - 1}/{total}] 某集无视频地址，跳过")
            self._log("某集无视频地址，已跳过", "warn")
            self._ocr_run_next()
            return
        title = info.get("title", "") or f"第{idx + 1}集"
        header = f"========== {title} =========="
        cur = self.sub_text.toPlainText()
        block = (cur.rstrip() + "\n\n" if cur.strip() else "") + header + "\n"
        self.sub_text.setPlainText(block)
        self.sub_status.setText(f"[{total - len(self._ocr_queue)}/{total}] 识别中 {title}…")
        self._log(f"批量识别 {title} ({total - len(self._ocr_queue)}/{total})", "info")
        self._start_ocr(url, bool(info.get("is_m3u8")), title, ep_idx=idx, batch=True)

    def _start_ocr(self, url, is_m3u8, title, ep_idx=None, batch=False):
        if getattr(self, "_ocr_worker", None) and self._ocr_worker.isRunning():
            self._log("字幕识别正在运行，请等待完成", "warn")
            return
        self._ocr_wtitle = title or "视频"
        self._ocr_ep = ep_idx
        self._ocr_batch = batch
        if not batch:
            self._last_subs = []
            self.sub_text.setPlainText("")
        self.sub_status.setText("准备识别…")
        if ep_idx is not None:
            self.epgrid.set_status(ep_idx, "识别中…")
        w = OcrWorker(url, is_m3u8=is_m3u8, title=title, lang=self._ocr_lang,
                  fps=self._ocr_fps, min_subtitle_ms=self._ocr_min_ms,
                  crop_bottom=self._ocr_crop_bottom,
                  subtitle_scale=self._ocr_scale, parent=self)
        w.progress.connect(self._ocr_progress)
        w.done.connect(self._ocr_done)
        w.failed.connect(self._ocr_failed)
        self._ocr_worker = w
        self._log(f"开始识别字幕: {title or url}", "info")
        w.start()

    def _ocr_progress(self, msg):
        if self._ocr_batch:
            total = getattr(self, "_ocr_total", 0)
            cur = total - len(self._ocr_queue)
            self.sub_status.setText(f"[{cur}/{total}] {msg}")
        else:
            self.sub_status.setText(msg)

    def _fmt_ts(self, sec):
        sec = max(0.0, sec)
        return f"{int(sec // 60):02d}:{sec % 60:04.1f}"

    def _ocr_done(self, subs):
        self._last_subs = subs or []
        ep = self._ocr_ep
        if ep is not None and hasattr(self, "epgrid"):
            self.epgrid.set_status(ep, "字幕%d条" % len(self._last_subs) if self._last_subs else "无字幕")
        lines = [f"[{self._fmt_ts(s)}-{self._fmt_ts(e)}] {t}" for s, e, t in self._last_subs]
        if self._ocr_batch:
            if lines:
                new = self.sub_text.toPlainText().rstrip() + "\n" + "\n".join(lines)
            else:
                new = self.sub_text.toPlainText().rstrip()
            self.sub_text.setPlainText(new)
            self._log(f"识别到 {len(self._last_subs)} 条字幕: {self._ocr_wtitle}",
                      "ok" if lines else "warn")
            self.sub_status.setText(f"[{self._ocr_total - len(self._ocr_queue)}/{self._ocr_total}] "
                                    f"完成 · {len(self._last_subs)} 条")
            self._ocr_run_next()
            return
        if not self._last_subs:
            self.sub_status.setText("未检测到字幕")
            self._log(f"字幕识别完成，未检测到字幕: {self._ocr_wtitle}", "warn")
            return
        self.sub_text.setPlainText("\n".join(lines))
        self.sub_status.setText(f"共 {len(self._last_subs)} 条 · {self._ocr_wtitle}")
        self._log(f"识别到 {len(self._last_subs)} 条字幕: {self._ocr_wtitle}", "ok")

    def _ocr_failed(self, err):
        if self._ocr_ep is not None:
            self.epgrid.set_status(self._ocr_ep, "识别失败")
        if self._ocr_batch:
            cur = self.sub_text.toPlainText().rstrip()
            self.sub_text.setPlainText(cur + "\n[识别失败] " + err + "\n")
            self._log(f"批量 {self._ocr_wtitle} 识别失败: {err}", "error")
            self._ocr_run_next()
            return
        self.sub_status.setText("识别失败")
        self._log(f"字幕识别失败: {err}", "error")

    def _set_ocr_lang(self, code):
        labels = {"zh": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文"}
        self._ocr_lang = code
        self._log(f"字幕识别语言: {labels.get(code, code)}", "info")

    def _open_ocr_menu(self):
        m = QMenu(self)
        lm = m.addMenu("识别语言")
        cur = self._ocr_lang
        for code, label in [("zh", "简体中文"), ("en", "英文"), ("ja", "日文"), ("ko", "韩文")]:
            a = lm.addAction(("✓ " if code == cur else "") + label)
            a.triggered.connect(lambda _c=code: self._set_ocr_lang(_c))
        fm = m.addMenu("采样帧率 (FPS)")
        fps_label = {"5": "5 · 速度优先", "8": "8", "10": "10 · 均衡(默认)", "15": "15", "20": "20 · 精度优先"}
        for v in (5, 8, 10, 15, 20):
            a = fm.addAction(("✓ " if v == self._ocr_fps else "") + fps_label[str(v)])
            a.triggered.connect(lambda _v=v: self._set_ocr_fps(_v))
        tmm = m.addMenu("最短字幕时长(ms)")
        tm_label = {"100": "100 · 更全(默认)", "200": "200", "350": "350", "500": "500 · 更准", "800": "800"}
        for v in (100, 200, 350, 500, 800):
            a = tmm.addAction(("✓ " if v == self._ocr_min_ms else "") + tm_label[str(v)])
            a.triggered.connect(lambda _v=v: self._set_ocr_min_ms(_v))
        sm = m.addMenu("字幕放大(提升识别率)")
        sc_label = {1.0: "1× · 不放大", 1.25: "1.25×", 1.5: "1.5× · 均衡(默认)", 2.0: "2× · 更高精度"}
        for v in (1.0, 1.25, 1.5, 2.0):
            a = sm.addAction(("✓ " if abs(self._ocr_scale - v) < 0.01 else "") + sc_label[v])
            a.triggered.connect(lambda _v=v: self._set_ocr_scale(_v))
        m.addSeparator()
        crop_a = m.addAction("仅识别底部字幕区（1/3·更精准）")
        crop_a.setCheckable(True)
        crop_a.setChecked(self._ocr_crop_bottom)
        crop_a.toggled.connect(self._set_ocr_crop)
        m.addSeparator()
        st = "✓ 引擎已就绪" if ocr_available() else "✗ 未检测到引擎数据"
        stl = m.addAction(st)
        stl.setEnabled(False)
        m.addAction("识别本地视频（测试）…").triggered.connect(self._start_ocr_dialog)
        anchor = getattr(self, "sub_set_btn", None)
        if anchor is None:
            return
        m.exec_(anchor.mapToGlobal(QPoint(0, anchor.height())))

    def _set_ocr_fps(self, v):
        self._ocr_fps = v
        self._log(f"字幕采样帧率: {v} FPS", "info")

    def _set_ocr_min_ms(self, v):
        self._ocr_min_ms = v
        self._log(f"最短字幕时长: {v} ms", "info")

    def _set_ocr_crop(self, on):
        self._ocr_crop_bottom = bool(on)
        self._log(f"字幕识别{'开启' if on else '关闭'}仅底部字幕区", "info")

    def _set_ocr_scale(self, v):
        self._ocr_scale = float(v)
        self._log(f"字幕放大倍数: {v}×", "info")

    def _start_ocr_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频测试字幕识别", "",
            "视频 (*.mp4 *.mkv *.mov *.webm *.flv *.avi);;所有文件 (*)")
        if not path:
            return
        self._log(f"测试识别本地视频: {os.path.basename(path)}", "info")
        self._start_ocr(path, False, os.path.basename(path), ep_idx=None)

    def _sub_copy(self):
        txt = self.sub_text.toPlainText()
        if txt:
            QApplication.clipboard().setText(txt)
            self._log("已复制字幕到剪贴板", "ok")
        else:
            self._log("暂无可复制的字幕", "warn")

    def _sub_export(self):
        txt = self.sub_text.toPlainText().strip()
        if not txt:
            self._log("暂无可导出的字幕", "warn")
            return
        name = re.sub(r'[\\/:*?"<>|]', "_", self._ocr_wtitle or "字幕")
        # 如果是本地视频，在文件名后加上剧集序号便于区分
        ep_suffix = ""
        if self._ocr_ep is not None:
            ep_suffix = f"_第{self._ocr_ep + 1}集"
        path, _ = QFileDialog.getSaveFileName(self, "导出字幕", f"{name}{ep_suffix}.txt", "文本 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.sub_text.toPlainText())
            self._log(f"字幕已导出: {path}", "ok")
        except Exception as ex:
            self._log(f"导出失败: {ex}", "error")

    def _sub_clear(self):
        self.sub_text.setPlainText("")
        self.sub_status.setText("未识别")
        self._last_subs = []
        self._ocr_ep = None

    # ---------- 视频分析（GLM-5.3 / GLM-5.3-Flash 多模态） ----------
    def _va_grid_idx(self, idx):
        self._va_start(from_idx=idx)

    def _va_target(self):
        sel = self.epgrid.selected()
        if sel:
            return sel[0]
        if getattr(self, "_playing_idx", -1) >= 0 and self._playing_idx < self.epgrid.count():
            return self._playing_idx
        if self.epgrid.count() > 0:
            return 0
        return None

    def _va_hold(self, w):
        """登记分析管线的工作线程对象，避免背靠背替换时遗漏对旧线程的引用，
        导致其 C++ 线程仍在收尾期就被回收（批量多集逐集换线程时必现崩溃）。"""
        if not hasattr(self, "_va_stage_pool"):
            self._va_stage_pool = []
        self._va_stage = w
        if w is not None:
            self._va_stage_pool.append(w)
        # 只保留仍在运行的线程（isFinished==False 的 QThread）或当前对象；
        # 已结束线程/一次性 QObject 可安全释放，避免批量逐集换线程时累积
        self._va_stage_pool = [x for x in self._va_stage_pool
                               if x is w or (hasattr(x, "isFinished") and not x.isFinished())]

    def _va_start(self, from_idx=None):
        if getattr(self, "_va_running", False):
            self._log("视频分析正在运行，请等待完成", "warn")
            return
        idx = from_idx if from_idx is not None else self._va_target()
        if idx is None:
            self._log("当前没有可分析的剧集，请先打开分享链接", "warn")
            return
        self._va_batch_mode = False
        self._va_queue = []
        self._va_accum = ""
        self.va_text.setPlainText("")
        self.va_status.setText("准备分析…")
        self.result_tabs.setCurrentIndex(0)
        self._va_analyze_one(idx)

    def _va_batch(self):
        # 运行中点击 → 停止；空闲点击 → 开始
        if getattr(self, "_va_running", False):
            self._va_stop()
            return
        count = self.epgrid.count()
        if count <= 0:
            self._log("当前没有可分析的剧集，请先打开分享链接", "warn")
            return
        queue = [i for i in range(count) if self.epgrid.info_at(i).get("url")]
        if not queue:
            self._log("没有剧集具备有效的视频地址，无法批量分析", "warn")
            return
        key = config.GLM_AI.get("api_key", "")
        if not key:
            self._log("未配置 GLM API Key，请在「视频分析 · 设置」中配置", "warn")
            self.result_tabs.setCurrentIndex(0)
            self._va_settings_dialog()
            return
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        desc = {"segments": "分段·场景切分·台词",
                "batch": "多帧序列",
                "single": "单帧"}.get(self._va_mode, "多帧序列")
        self._va_stop_flag = False
        self._va_batch_mode = True
        self._va_queue = queue
        self._va_ep_total = len(queue)
        self._va_accum = ""
        self.va_text.setPlainText("")
        self.result_tabs.setCurrentIndex(0)
        self._log(f"批量视频分析: 共 {self._va_ep_total} 集待分析 · {model}（{desc}）", "info")
        self._va_update_btn()
        self._va_analyze_one(self._va_queue.pop(0))

    def _va_stop(self):
        """停止当前视频分析/批量分析。"""
        self._va_stop_flag = True
        self._va_running = False
        self._va_batch_mode = False
        self._va_local_batch = False
        self._va_queue = []
        self._va_local_queue = []
        # 停止 OCR 工作线程
        ow = getattr(self, "_va_ocr_worker", None)
        if ow is not None and ow.isRunning():
            try:
                ow.stop()
                ow.wait(2000)
            except Exception:
                pass
        self._va_update_btn()
        self.va_status.setText("已停止分析")
        self._log("已停止视频分析", "warn")

    def _va_update_btn(self):
        """根据运行状态切换「开始分析/停止分析」按钮的文本与颜色。"""
        running = bool(getattr(self, "_va_running", False))
        if running:
            self.va_batch.setText("⏹ 停止分析")
            self.va_batch.setObjectName("stopBtn")
        else:
            self.va_batch.setText("▶ 开始分析")
            self.va_batch.setObjectName("startBtn")
        self.va_batch.style().unpolish(self.va_batch)
        self.va_batch.style().polish(self.va_batch)
        self.va_batch.update()

    def _va_analyze_local_pick(self):
        if getattr(self, "_va_running", False):
            self._log("视频分析正在运行，请等待完成", "warn")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要分析的本地视频（可多选批量）", "",
            "视频文件 (*.mp4 *.mkv *.mov *.avi *.flv *.wmv *.webm *.ts);;所有文件 (*)")
        if not paths:
            return
        key = config.GLM_AI.get("api_key", "")
        if not key:
            self._log("未配置 GLM API Key，请在「视频分析 · 设置」中配置", "warn")
            self.result_tabs.setCurrentIndex(0)
            self._va_settings_dialog()
            return
        self._va_local_batch = True
        self._va_local_queue = list(paths)
        self._va_ep_total = len(paths)
        self._va_accum = ""
        self._va_local_id = 0
        self._va_stop_flag = False
        self.va_text.setPlainText("")
        self.result_tabs.setCurrentIndex(0)
        self._log(f"本地视频批量分析: 共 {self._va_ep_total} 个待分析", "info")
        self._va_local_next()

    def _va_local_next(self):
        if getattr(self, "_va_stop_flag", False):
            return
        if self._va_local_queue:
            p = self._va_local_queue.pop(0)
            self._va_local_id += 1
            self.va_status.setText(f"本地批量分析 {self._va_local_id}/{self._va_ep_total} …")
            self._va_analyze_local(p, batch=True)
            return
        self._va_local_batch = False
        self._va_running = False
        self._va_update_btn()
        self.va_status.setText(f"本地批量分析完成 · 共 {self._va_ep_total} 个")
        self._log(f"本地视频批量分析完成: 共 {self._va_ep_total} 个", "ok")

    def _va_analyze_local(self, path, batch=False):
        if not batch and getattr(self, "_va_running", False):
            self._log("视频分析正在运行，请等待完成", "warn")
            return
        if not path or not os.path.exists(path):
            self._log("本地视频文件不存在，无法分析", "warn")
            return
        key = config.GLM_AI.get("api_key", "")
        if not key:
            self._log("未配置 GLM API Key，请在「视频分析 · 设置」中配置", "warn")
            self.result_tabs.setCurrentIndex(0)
            self._va_settings_dialog()
            return
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        self._va_batch_mode = False
        self._va_queue = []
        self._va_ep = None
        self._va_wtitle = os.path.splitext(os.path.basename(path))[0] or "本地视频"
        if self._va_local_batch:
            self._va_wtitle = f"[{self._va_local_id}] {self._va_wtitle}"
        self.va_text.setPlainText("")
        self.result_tabs.setCurrentIndex(0)
        frame_n = 1 if self._va_mode == "single" else self._va_frames
        desc = {"segments": "分段·场景切分·台词",
                "batch": "多帧序列",
                "single": "单帧"}.get(self._va_mode, "多帧序列")
        self._va_running = True
        self._va_update_btn()
        self.va_status.setText("准备分析本地视频…")
        self._log(f"开始分析本地视频 {os.path.basename(path)}: {model}（{desc}）", "info")
        if self._va_mode == "segments":
            self._va_run_segments(path)
        else:
            self._va_grab_frames(path, frame_n)

    def _va_analyze_one(self, idx):
        self._va_stop_flag = False
        info = self.epgrid.info_at(idx)
        url = info.get("url", "")
        if not url:
            if self._va_batch_mode:
                self._log(f"第 {idx + 1} 集无有效地址，跳过", "warn")
                self.epgrid.set_status(idx, "分析失败")
                self._va_next_batch()
                return
            self._va_failed("该集没有可用的视频地址，无法分析")
            return
        title = info.get("title", "") or f"第{idx + 1}集"
        key = config.GLM_AI.get("api_key", "")
        if not key:
            if self._va_batch_mode:
                self._log("未配置 GLM API Key，批量分析终止", "warn")
            self._va_failed("未配置 GLM API Key")
            return
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        self._va_running = True
        self._va_update_btn()
        self._va_ep = idx
        self._va_wtitle = title
        self.epgrid.set_status(idx, "分析中…")
        if self._va_batch_mode:
            cur = self._va_ep_total - len(self._va_queue)
            self.va_status.setText(f"批量分析 {cur}/{self._va_ep_total} · {title}…")
        else:
            self.va_status.setText("准备分析…")
        frame_n = 1 if self._va_mode == "single" else self._va_frames
        desc = {"segments": "分段·场景切分·台词",
                "batch": "多帧序列",
                "single": "单帧"}.get(self._va_mode, "多帧序列")
        self._log(f"开始视频分析 {title}: {model}（{desc}）", "info")
        is_online = not (url.startswith("file://") or os.path.exists(url))
        if is_online:
            w = VaDownloadWorker(url, bool(info.get("is_m3u8")), parent=self)
            w.progress.connect(self._va_progress)
            w.done.connect(self._va_on_local)
            w.failed.connect(self._va_failed)
            self._va_hold(w)
            self.va_status.setText("下载临时视频…")
            w.start()
        else:
            if self._va_mode == "segments":
                self._va_run_segments(url)
            else:
                self._va_grab_frames(url, frame_n)

    def _va_on_local(self, path):
        if getattr(self, "_va_stop_flag", False):
            return
        if not path or not os.path.exists(path):
            self._va_failed("临时视频下载失败或内容无效")
            return
        if self._va_mode == "segments":
            self._va_run_segments(path)
        else:
            self._va_grab_frames(path, self._va_frames)

    def _va_grab_frames(self, path, frame_n):
        try:
            self.va_status.setText("打开视频…")
            self._va_grabber = FrameGrabber(path, count=frame_n, max_w=768, parent=self)
            self._va_hold(self._va_grabber)
            self._va_grabber.progress.connect(self._va_progress)
            self._va_grabber.finished.connect(self._va_on_frames)
            self._va_grabber.failed.connect(self._va_failed)
            self._va_grabber.start()
        except Exception as e:
            self._va_failed("打开视频失败: %s" % e)

    def _va_on_frames(self, frames):
        if getattr(self, "_va_stop_flag", False):
            return
        if not frames:
            self._va_failed("未能从视频中抽取到任何帧")
            return
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        prompt = PROMPTS.get(self._va_mode, PROMPTS["batch"])
        w = GlmWorker(config.GLM_AI.get("api_key", ""),
                      config.GLM_AI.get("base_url") or config.GLM_BASE_URL,
                      model, prompt, frames, parent=self)
        w.progress.connect(self._va_progress)
        w.done.connect(self._va_done)
        w.failed.connect(self._va_failed)
        self._va_hold(w)
        w.start()

    # ---- 分段场景分析（segments 模式）：场景切分 + 台词 + 逐段分析 + 整合 ----

    def _va_run_segments(self, path):
        try:
            self.va_status.setText("切分场景…")
            self._va_seg_data = None
            self._va_parts = []
            self._va_seg_idx = 0
            self._va_seg_total = 0
            self._va_local_path = path
            self._va_subs = []
            # 场景切分（主线程 Qt 播放，按画面切换切出 6-10 秒小段）
            seg = SceneSegmenter(path, parent=self)
            seg.progress.connect(self._va_progress)
            seg.finished.connect(self._va_on_segmented)
            seg.failed.connect(self._va_failed)
            self._va_hold(seg)
            seg.start()
        except Exception as e:
            self._va_failed("启动分段分析失败: %s" % e)

    def _va_on_segmented(self, data):
        if not self._va_running:
            return
        self._va_seg_data = data
        segs = data.get("segments") or []
        if not segs:
            self._va_failed("未能从视频中切分出片段")
            return
        self._va_subs = None
        self._va_parts = []
        self._va_seg_idx = 0
        self._va_seg_total = len(segs)
        # 先本机 OCR 识别真实字幕，再进入各段画面分析（台词以 OCR 为准）
        if ocr_available() and self._va_local_path and os.path.exists(self._va_local_path):
            self.va_status.setText("SubtitleOCR 识别台词中…")
            self._log(f"{self._va_wtitle}: 本机 OCR 识别字幕中…", "info")
            ow = OcrWorker(self._va_local_path, is_m3u8=False, title=self._va_wtitle,
                           lang=self._ocr_lang, fps=self._ocr_fps,
                min_subtitle_ms=self._ocr_min_ms, crop_bottom=self._ocr_crop_bottom,
                subtitle_scale=self._ocr_scale, parent=self)
            ow.progress.connect(self._va_ocr_progress)
            ow.done.connect(self._va_ocr_done)
            ow.failed.connect(self._va_ocr_failed)
            self._va_ocr_worker = ow
            self._va_hold(ow)
            ow.start()
            return
        if ocr_available():
            self._log(f"切分出 {len(segs)} 段（OCR 引擎无本地视频，改由模型按画面推断台词）", "info")
        else:
            self._log(f"切分出 {len(segs)} 段（OCR 引擎不可用，改由模型按画面推断台词）", "info")
        self._va_run_next_seg()

    def _va_ocr_progress(self, msg):
        self.va_status.setText("SubtitleOCR 识别台词中…")

    def _va_ocr_done(self, subs):
        if getattr(self, "_va_stop_flag", False):
            return
        self._va_subs = subs or []
        self._log(f"{self._va_wtitle}: OCR 识别到 {len(self._va_subs)} 条台词，开始画面分析", "ok")
        self.va_status.setText(f"OCR 完成，识别到 {len(self._va_subs)} 条台词，开始分析各段…")
        self._va_run_next_seg()

    def _va_ocr_failed(self, err):
        if getattr(self, "_va_stop_flag", False):
            return
        self._va_subs = []
        self._log(f"{self._va_wtitle}: 本机 OCR 识别失败，继续分析（无台词）：{err}", "warn")
        self.va_status.setText("OCR 识别失败，继续画面分析（无台词）…")
        self._va_run_next_seg()

    def _va_run_next_seg(self):
        if self._va_seg_idx >= self._va_seg_total:
            self._va_combine()
            return
        i = self._va_seg_idx
        seg = self._va_seg_data["segments"][i]
        frames = self._va_seg_data["seg_frames"][i]
        if self._va_subs:
            prompt = SEGMENT_PROMPT_NO_DIALOGUE_ZH.format(seg=i + 1, t0=seg[0], t1=seg[1])
            seg_label = "（画面）"
        else:
            prompt = SEGMENT_PROMPT_ZH.format(seg=i + 1, t0=seg[0], t1=seg[1])
            seg_label = "（画面推断台词）"
        key = config.GLM_AI.get("api_key", "")
        base_url = config.GLM_AI.get("base_url") or config.GLM_BASE_URL
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        if frames:
            w = GlmWorker(key, base_url, model, prompt, frames, parent=self)
        else:
            w = CombineWorker(key, base_url, model, prompt, parent=self)
        w.progress.connect(self._va_progress)
        w.done.connect(self._va_on_seg_one)
        w.failed.connect(self._va_failed)
        self._va_hold(w)
        self.va_status.setText(f"分析段 {i + 1}/{self._va_seg_total}{seg_label}…")
        w.start()

    def _va_on_seg_one(self, text):
        if getattr(self, "_va_stop_flag", False):
            return
        self._va_parts.append(text)
        self._va_seg_idx += 1
        self._va_run_next_seg()

    def _va_combine(self):
        if getattr(self, "_va_stop_flag", False):
            return
        if not self._va_parts:
            self._va_failed("所有分段均未返回分析结果")
            return
        parts = "\n\n".join("——第 %d 段——\n%s" % (i + 1, t)
                            for i, t in enumerate(self._va_parts))
        key = config.GLM_AI.get("api_key", "")
        base_url = config.GLM_AI.get("base_url") or config.GLM_BASE_URL
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        if self._va_subs is None:
            self._va_subs = []
        if self._va_subs:
            lines = []
            for s, e, t in self._va_subs:
                lines.append("[%s - %s] %s"
                             % (self._fmt_ts(s), self._fmt_ts(e), (t or "").strip()))
            dialogue = "\n".join(lines) if lines else "（OCR 未识别到任何台词）"
            prompt = SEGMENT_COMBINE_WITH_DIALOGUE_ZH.format(parts=parts, dialogue=dialogue)
            self.va_status.setText("整合分段结果与 OCR 台词…")
        else:
            prompt = SEGMENT_COMBINE_ZH.format(parts=parts)
            self.va_status.setText("整合分段结果…")
        w = CombineWorker(key, base_url, model, prompt, parent=self)
        w.progress.connect(self._va_progress)
        w.done.connect(self._va_done)
        w.failed.connect(self._va_failed)
        self._va_hold(w)
        w.start()

    def _va_progress(self, msg):
        self.va_status.setText(msg)

    def _va_done(self, text):
        if getattr(self, "_va_stop_flag", False):
            return
        if self._va_ep is not None and hasattr(self, "epgrid"):
            self.epgrid.set_status(self._va_ep, "已分析")
        if self._va_local_batch:
            self._acc_episode(text)
            self._va_local_next()
            return
        if self._va_batch_mode:
            self._acc_episode(text)
            self._va_next_batch()
            return
        self._va_running = False
        self._va_update_btn()
        self.va_text.setPlainText(text)
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        self.va_status.setText(f"完成 · {self._va_wtitle}（{model}）")
        self._log(f"视频分析完成: {self._va_wtitle}", "ok")
        self._save_analysis_result(self._va_ep, (self._va_wtitle or f"第{self._va_ep + 1}集"), text)
        self._script_add_file("📂 视频分析结果",
                              f"{self._va_wtitle or f'第{self._va_ep + 1}集'}_分析结果", text)

    def _acc_episode(self, text):
        title = (self._va_wtitle or "") or f"第{self._va_ep + 1}集"
        header = f"\n{'=' * 30}\n  {title}\n{'=' * 30}\n"
        body = (text or "").strip()
        self._va_accum += header + body + "\n"
        self.va_text.setPlainText(self._va_accum)
        self._save_analysis_result(self._va_ep, title, body)
        self._script_add_file("📂 视频分析结果", f"{title}_分析结果", body)

    def _save_analysis_result(self, idx, title, text):
        """每完成一集(或本地视频)分析，立即把结果保存为独立 txt，目录「视频分析结果」。"""
        try:
            os.makedirs(self._results_dir, exist_ok=True)
            safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (title or "")).strip()[:40] or "未命名"
            # 判断是否是本集 grid 条目（idx 有效且是网格中的第 idx 个）
            is_grid_local = False
            grid_fname = ""
            if idx is not None and hasattr(self, "epgrid"):
                info = self.epgrid.info_at(idx)
                is_grid_local = bool(info.get("is_local"))
                grid_fname = info.get("title", "")
            if is_grid_local and grid_fname:
                # 本地视频：用原文件名 + 剧集序号
                base = re.sub(r'[\\/:*?"<>|\r\n]+', "_", grid_fname).strip()[:60]
                name = f"第{idx + 1}集_{base}.txt"
            else:
                name = f"第{idx + 1}集_{safe_title}.txt" if idx is not None else f"{safe_title}.txt"
            path = os.path.join(self._results_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text or "")
            self._log(f"已保存该分析结果: {os.path.basename(path)}", "ok")
        except Exception as e:
            self._log(f"保存分析结果失败: {e}", "warn")

    def _va_next_batch(self):
        if getattr(self, "_va_stop_flag", False):
            return
        if self._va_queue:
            nxt = self._va_queue.pop(0)
            self._log(f"批量分析: 进入第 {self._va_ep_total - len(self._va_queue)}/{self._va_ep_total} 集…", "info")
            self._va_analyze_one(nxt)
            return
        self._va_running = False
        self._va_update_btn()
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        self.va_status.setText(f"批量分析完成 · 共 {self._va_ep_total} 集（{model}）")
        self._log(f"批量视频分析完成: 共 {self._va_ep_total} 集", "ok")

    def _va_failed(self, err):
        if getattr(self, "_va_stop_flag", False):
            return
        if self._va_ep is not None and hasattr(self, "epgrid"):
            self.epgrid.set_status(self._va_ep, "分析失败")
        if self._va_local_batch:
            self._log(f"本地文件分析失败: {err}，继续下一个", "warn")
            self._acc_episode("（本地文件分析失败：%s）" % err)
            self._va_local_next()
            return
        if self._va_batch_mode:
            ep = self._va_ep if self._va_ep is not None else 0
            self._log(f"第 {ep + 1} 集分析失败: {err}，继续下一集", "warn")
            self._acc_episode("（本集分析失败：%s）" % err)
            self._va_next_batch()
            return
        self._va_running = False
        self._va_update_btn()
        self.va_status.setText("分析失败")
        self._log(f"视频分析失败: {err}", "error")

    def _va_to_script(self):
        if getattr(self, "_va_running", False):
            self._log("当前有分析或转换正在运行，请稍候", "warn")
            return
        src = self.va_text.toPlainText().strip()
        if not src:
            self._log("没有可用的分析结果，请先完成视频分析（可批量）", "warn")
            return
        key = config.GLM_AI.get("api_key", "")
        if not key:
            self._log("未配置 GLM API Key，请在「视频分析 · 设置」中配置", "warn")
            self.result_tabs.setCurrentIndex(0)
            self._va_settings_dialog()
            return
        model = config.GLM_AI.get("model", "glm-5.3-flash")
        base_url = config.GLM_AI.get("base_url") or config.GLM_BASE_URL
        self._va_running = True
        self._va_batch_mode = False
        self._va_update_btn()
        self.va_status.setText("正在反推剧本…")
        self.result_tabs.setCurrentIndex(0)
        self._log(f"一键转剧本：基于分析结果反推剧本（{model}）", "info")
        w = CombineWorker(key, base_url, model, SCRIPT_PROMPT_ZH.format(report=src), parent=self)
        w.progress.connect(self._va_progress)
        w.done.connect(self._va_on_script)
        w.failed.connect(self._va_failed)
        self._va_hold(w)
        w.start()

    def _va_to_script_local(self):
        if getattr(self, "_va_running", False):
            self._log("当前有分析或转换正在运行，请稍候", "warn")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择本地分析结果文件", self._results_dir,
            "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self._log(f"读取本地结果失败: {e}", "warn")
            return
        if not content.strip():
            self._log("所选文件内容为空，无法转剧本", "warn")
            return
        self.va_text.setPlainText(content)
        self.result_tabs.setCurrentIndex(0)
        self._log(f"已载入本地结果: {os.path.basename(path)}，开始反推剧本", "info")
        self._va_to_script()

    def _va_on_script(self, text):
        self._va_running = False
        self._va_update_btn()
        self.va_text.setPlainText(text)
        self.va_status.setText("剧本已生成 · 已保存到项目「剧本」目录")
        self._log("一键转剧本完成", "ok")
        self._save_script(text)
        self._script_add_file("📜 剧本", f"{self._va_wtitle or '剧本'}_剧本", text)

    def _save_script(self, text):
        """把转换生成的剧本落盘到程序目录「剧本」文件夹。"""
        try:
            os.makedirs(self._scripts_dir, exist_ok=True)
            name = f"剧本_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path = os.path.join(self._scripts_dir, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text or "")
            self._log(f"已保存剧本: {os.path.basename(path)}", "ok")
        except Exception as e:
            self._log(f"保存剧本失败: {e}", "warn")

    def _set_va_mode(self, mode):
        self._va_mode = mode
        label = {"segments": "分段·场景切分·台词",
                 "batch": "多帧序列",
                 "single": "单帧"}.get(mode, mode)
        self._log(f"视频分析模式: {label}", "info")

    def _set_va_frames(self, n):
        self._va_frames = n
        self._log(f"视频分析抽帧数: {n}", "info")

    def _set_va_model(self, model):
        # 切换模型时自动带上该服务商对应的默认接口地址（GLM / Agnes Hub 不同）
        config.save_glm_config({"model": model, "base_url": config.base_url_for(model)})
        self._log(f"视频分析模型: {model}", "info")

    def _ai_services_settings(self, tab=0):
        """统一 AI 服务配置：视频分析 / 视频生成 / 资产生成图（支持任意 OpenAI 兼容接口）"""
        dlg = QDialog(self)
        dlg.setWindowTitle("AI 服务设置（统一配置 · 支持任意 OpenAI 兼容接口）")
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)
        tabs = QTabWidget()

        # ---- Tab 0 视频分析 ----
        va_w = QWidget()
        va_f = QFormLayout(va_w)
        va_f.setSpacing(10)
        va_key = QLineEdit(config.GLM_AI.get("api_key", ""))
        va_key.setEchoMode(QLineEdit.Password)
        va_key.setPlaceholderText("粘贴 API Key（智谱 / Agnes / 任意 OpenAI 兼容服务）")
        va_model = QComboBox()
        for label, val in [("GLM-5.3-Flash（多模态，推荐）", "glm-5.3-flash"),
                           ("GLM-5.3（旗舰）", "glm-5.3"),
                           ("Agnes 2.5 Flash（海外）", "agnes-2.5-flash"),
                           ("Agnes 2.5 Flash（中国站）", "agnes-2.5-flash-cn"),
                           ("自定义（OpenAI 兼容）", "__custom__")]:
            va_model.addItem(label, val)
        cur_model = config.GLM_AI.get("model", "glm-5.3-flash")
        for i in range(va_model.count()):
            if va_model.itemData(i) == cur_model:
                va_model.setCurrentIndex(i)
                break
        va_url = QLineEdit(config.GLM_AI.get("base_url") or config.GLM_BASE_URL)
        va_model.currentIndexChanged.connect(
            lambda *_a: va_url.setText(config.base_url_for(va_model.currentData()))
            if va_model.currentData() != "__custom__" else None)
        va_f.addRow("API Key：", va_key)
        va_f.addRow("模型：", va_model)
        va_f.addRow("接口地址：", va_url)
        va_hint = QLabel("用于「视频分析」。模型选择自动切换服务商地址，也可手动填写任意 OpenAI 兼容端点。")
        va_hint.setWordWrap(True)
        va_hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        va_f.addRow(va_hint)
        tabs.addTab(va_w, "🎞 视频分析")

        # ---- Tab 1 视频生成 ----
        vg_w = QWidget()
        vg_f = QVBoxLayout(vg_w)
        vg_f.setSpacing(8)
        vg_form = QFormLayout()
        vg_form.setSpacing(10)
        vg_model = QComboBox()
        vg_model.addItems(getattr(config, "VIDEO_GEN_MODELS", ["agnes-video-2.5-flash"]))
        cur_model = config.AGNES_VIDEO.get("model") or config.AGNES_VIDEO_MODEL
        if cur_model in [vg_model.itemData(i) or vg_model.itemText(i)
                         for i in range(vg_model.count())]:
            vg_model.setCurrentText(cur_model)
        vg_form.addRow("生成模型：", vg_model)
        # 服务区域（海外 / 中国站）：切换自动填充对应 Base URL，模型名保持不变（与视频分析同域）
        vg_region = QComboBox()
        _regions = getattr(config, "AGNES_VIDEO_REGIONS",
                           (("overseas", "海外（apihub.agnes-ai.com）"),
                            ("cn", "中国（api.agnes-ai.cn）")))
        for _rid, _rlabel in _regions:
            vg_region.addItem(_rlabel, _rid)
        cur_region = config.AGNES_VIDEO.get("region", "overseas")
        for i in range(vg_region.count()):
            if vg_region.itemData(i) == cur_region:
                vg_region.setCurrentIndex(i)
                break
        vg_url = QLineEdit(config.AGNES_VIDEO.get("base_url") or config.agnes_video_base_url(cur_region))

        # 分站点 Key：切换站点时先把当前站点输入框内容存回内存，再载入目标站点已填写的 Key。
        # 注意 currentIndexChanged 触发时 currentData() 已是"目标"站点，须用闭包记录"输入框正在编辑的站点"
        # 来回存旧站，否则会把旧站内容误存到新站，导致框内 keys 看似"没变化"。
        editing_region = [cur_region]

        def _read_vg_keys():
            k = [l.strip() for l in vg_keys.toPlainText().split("\n") if l.strip()]
            t = [l.strip() for l in vg_tp_keys.toPlainText().split("\n") if l.strip()]
            return k, t

        def _apply_region_keys(rid):
            rk = config.agnes_video_region_keys(rid)
            vg_keys.setPlainText("\n".join(rk["api_keys"]))
            vg_tp_keys.setPlainText("\n".join(rk["tokenplan_keys"]))

        def _vg_region_sync(*_a):
            old = editing_region[0]
            k, t = _read_vg_keys()
            config.agnes_video_store_region_keys(old, k, t)   # 把"旧站"输入框内容回存旧站
            new = vg_region.currentData() or "overseas"
            editing_region[0] = new
            vg_url.setText(config.agnes_video_base_url(new))
            _apply_region_keys(new)

        vg_region.currentIndexChanged.connect(_vg_region_sync)
        vg_form.addRow("服务区域：", vg_region)
        vg_form.addRow("Base URL：", vg_url)
        vg_keys = QPlainTextEdit()
        vg_keys.setPlaceholderText("每行粘贴一个 API Key\n（多 Key 轮询调用，避免速率限制；单 Key 填一行即可）")
        rk_init = config.agnes_video_region_keys(config.AGNES_VIDEO.get("region", "overseas"))
        existing_keys = rk_init["api_keys"]
        if existing_keys:
            vg_keys.setPlainText("\n".join(existing_keys))
        elif config.AGNES_VIDEO.get("api_key"):
            vg_keys.setPlainText(config.AGNES_VIDEO["api_key"])
        vg_keys.setMinimumHeight(120)
        vg_form.addRow("API Keys（每行一个）：", vg_keys)
        vg_tp_keys = QPlainTextEdit()
        vg_tp_keys.setPlaceholderText("（可选）Token Plan Key，每行一个\n配置后一键生成按 Key数×5 满速并发，任务创建后超过 60s 才轮询")
        existing_tp = rk_init["tokenplan_keys"]
        if existing_tp:
            vg_tp_keys.setPlainText("\n".join(existing_tp))
        vg_tp_keys.setMinimumHeight(80)
        vg_form.addRow("TokenPlan Keys：", vg_tp_keys)
        # TokenPlan 模式开关
        vg_tp_enabled_cb = QCheckBox("启用 TokenPlan 模式（Key×5 并发，60s 轮询）")
        vg_tp_enabled_cb.setChecked(bool(config.AGNES_VIDEO.get("tp_enabled")))
        vg_form.addRow("", vg_tp_enabled_cb)
        vg_f.addLayout(vg_form)
        vg_hint = QLabel("用于「视频生成」（Agnes Video）。API Keys 支持多 Key 轮询与 429 限速自动等待；"
                         "TokenPlan Keys 单独填写时，一键生成按 Key数×5 满速并发（每 Key 5 RPM），任务创建后超过 60s 才轮询状态。")
        vg_hint.setWordWrap(True)
        vg_hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        vg_f.addWidget(vg_hint)
        # 用滚动区包裹，防止内容较多时 TokenPlan 等输入框被裁出可视区域
        vg_scroll = QScrollArea()
        vg_scroll.setWidgetResizable(True)
        vg_scroll.setWidget(vg_w)
        vg_scroll.setFrameShape(QFrame.NoFrame)
        tabs.addTab(vg_scroll, "🎬 视频生成")

        # ---- Tab 2 资产生成图 ----
        ig_w = QWidget()
        ig_f = QFormLayout(ig_w)
        ig_f.setSpacing(10)
        ig_provider = QComboBox()
        for key, pres in config.IMAGE_GEN_PRESETS.items():
            ig_provider.addItem(pres["name"], key)
        ig_key = QLineEdit(config.IMAGE_GEN.get("api_key", ""))
        ig_key.setEchoMode(QLineEdit.Password)
        ig_key.setPlaceholderText("粘贴生图服务的 API Key")
        ig_url = QLineEdit(config.IMAGE_GEN.get("base_url") or config.IMAGE_GEN_DEFAULT_BASE)
        ig_model = QLineEdit(config.IMAGE_GEN.get("model") or "cogview-3-flash")
        ig_size = QComboBox()
        for s in config.IMAGE_GEN_SIZES:
            ig_size.addItem(s, s)
        cur_size = config.IMAGE_GEN.get("size", "1024x1024")
        for i in range(ig_size.count()):
            if ig_size.itemData(i) == cur_size:
                ig_size.setCurrentIndex(i)
                break
        if config.IMAGE_GEN.get("base_url", "").startswith("agnes"):
            ig_provider.setCurrentIndex(1)
        elif config.IMAGE_GEN.get("base_url", "").startswith("http") and \
                "open.bigmodel.cn" not in config.IMAGE_GEN.get("base_url", ""):
            ig_provider.setCurrentIndex(2)

        def _ig_sync():
            pres = config.IMAGE_GEN_PRESETS.get(ig_provider.currentData())
            if pres and pres.get("base_url"):
                ig_url.setText(pres["base_url"])
            if pres and pres.get("model"):
                ig_model.setText(pres["model"])

        ig_provider.currentIndexChanged.connect(lambda *_a: _ig_sync())
        ig_f.addRow("服务商：", ig_provider)
        ig_f.addRow("API Key：", ig_key)
        ig_f.addRow("接口地址：", ig_url)
        ig_f.addRow("模型：", ig_model)
        ig_f.addRow("尺寸：", ig_size)
        ig_hint = QLabel("用于「资产生成图」（OpenAI 兼容 /images/generations）。智谱 cogview-3-flash 可免费生图；Agnes 图像模型需开通；也可填任意兼容端点。")
        ig_hint.setWordWrap(True)
        ig_hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        ig_f.addRow(ig_hint)
        tabs.addTab(ig_w, "🖼 资产生成图")

        tabs.setCurrentIndex(min(max(tab, 0), 2))
        lay.addWidget(tabs, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        b_ok = QPushButton("保存全部")
        b_ok.setObjectName("primaryBtn")
        b_can = QPushButton("取消")
        b_can.setObjectName("ghostBtn")
        btns.addWidget(b_can)
        btns.addWidget(b_ok)
        lay.addLayout(btns)

        def _save_all():
            # 视频分析
            mid = va_model.currentData()
            mdl = mid if mid != "__custom__" else (va_model.currentText() or "glm-5.3-flash")
            config.save_glm_config({
                "api_key": va_key.text().strip(),
                "model": mdl,
                "base_url": va_url.text().strip() or config.GLM_BASE_URL,
                "enabled": bool(va_key.text().strip())})
            # 视频生成
            raw = vg_keys.toPlainText().strip()
            lines = [l.strip() for l in raw.split("\n") if l.strip()]
            tp_raw = vg_tp_keys.toPlainText().strip()
            tp_lines = [l.strip() for l in tp_raw.split("\n") if l.strip()]
            _region = vg_region.currentData() or "overseas"
            # 分站点存储：把当前站点填写的 Keys 写回 keys_by_region（普通 Key 与 TokenPlan Key 各自独立）
            config.agnes_video_store_region_keys(_region, lines, tp_lines)
            config.save_agnes_video_config({
                "api_key": lines[0] if lines else "",
                "base_url": vg_url.text().strip() or config.agnes_video_base_url(_region),
                "api_keys": lines,
                "tokenplan_keys": tp_lines,
                "tp_enabled": vg_tp_enabled_cb.isChecked(),
                "model": vg_model.currentText().strip() or config.AGNES_VIDEO_MODEL,
                "region": _region})
            # 资产生成图
            config.save_image_gen_config({
                "api_key": ig_key.text().strip(),
                "base_url": ig_url.text().strip() or config.IMAGE_GEN_DEFAULT_BASE,
                "model": ig_model.text().strip() or "cogview-3-flash",
                "size": ig_size.currentData() or "1024x1024",
                "enabled": bool(ig_key.text().strip())})
            self._refresh_gen_key()
            self._log("AI 服务配置已保存（视频分析 / 视频生成 / 资产生成图）", "ok")
            dlg.accept()

        b_ok.clicked.connect(_save_all)
        b_can.clicked.connect(dlg.reject)
        dlg.exec_()

    def _open_va_menu(self):
        m = QMenu(self)
        mm = m.addMenu("分析模式")
        for key, label in [("segments", "分段·场景切分·台词（默认·推荐）"),
                           ("single", "单帧（快速）"),
                           ("batch", "多帧序列")]:
            a = mm.addAction(("✓ " if self._va_mode == key else "") + label)
            a.triggered.connect(lambda *_, _k=key: self._set_va_mode(_k))
        modm = m.addMenu("模型")
        for mid, label in [("glm-5.3-flash", "GLM-5.3-Flash（多模态·推荐）"),
                           ("glm-5.3", "GLM-5.3（旗舰）"),
                           ("agnes-2.5-flash", "Agnes 2.5 Flash（海外·代码/指令）"),
                           ("agnes-2.5-flash-cn", "Agnes 2.5 Flash（中国站·代码/指令）"),
                           ("agnes-3.0-flash", "Agnes 3.0 Flash（海外·代码/指令）"),
                           ("agnes-3.0-flash-cn", "Agnes 3.0 Flash（中国站·代码/指令）")]:
            a = modm.addAction(("✓ " if config.GLM_AI.get("model") == mid else "") + label)
            a.triggered.connect(lambda *_, _m=mid: self._set_va_model(_m))
        fcm = m.addMenu("抽帧数(序列)")
        for v in (4, 6, 8, 10, 12, 24, 30):
            a = fcm.addAction(("✓ " if self._va_frames == v else "") + f"{v} 帧")
            a.triggered.connect(lambda *_, _v=v: self._set_va_frames(_v))
        m.addSeparator()
        crop_a = m.addAction("仅识别底部字幕区（1/3·同步字幕识别）")
        crop_a.setCheckable(True)
        crop_a.setChecked(self._ocr_crop_bottom)
        crop_a.toggled.connect(lambda on: self._set_ocr_crop(on))
        vsm = m.addMenu("字幕放大(提升识别率·同步)")
        vs_label = {1.0: "1×", 1.25: "1.25×", 1.5: "1.5× · 均衡(默认)", 2.0: "2× · 更高精度"}
        for v in (1.0, 1.25, 1.5, 2.0):
            a = vsm.addAction(("✓ " if abs(self._ocr_scale - v) < 0.01 else "") + vs_label[v])
            a.triggered.connect(lambda _, _v=v: self._set_ocr_scale(_v))
        m.addSeparator()
        m.addAction("🔑 API 设置…").triggered.connect(lambda: self._ai_services_settings(0))
        if config.GLM_AI.get("api_key"):
            info = "✓ 已配置 Key：" + config.mask_api_key(config.GLM_AI.get("api_key"))
        else:
            info = "✗ 未配置 API Key"
        stl = m.addAction(info); stl.setEnabled(False)
        if config.GLM_AI.get("enabled"):
            m.addAction("API 已启用").setEnabled(False)
        m.exec_(self.va_set.mapToGlobal(QPoint(0, self.va_set.height())))

    def _va_settings_dialog(self):
        self._ai_services_settings(0)
        return

    def _va_save_settings(self, dlg, api_key, model, base_url):
        base_url = base_url.strip() or config.GLM_BASE_URL
        cfg = {"api_key": api_key, "model": model, "base_url": base_url,
               "enabled": bool(api_key)}
        if api_key:
            config.save_glm_config(cfg)
            self._log(f"GLM 视频分析配置已保存：{model} · Key {config.mask_api_key(api_key)}", "ok")
        else:
            config.save_glm_config({"api_key": "", "enabled": False})
            self._log("已清空 GLM API Key，视频分析待配置", "warn")
        dlg.accept()

    def _va_copy(self):
        txt = self.va_text.toPlainText()
        if txt:
            QApplication.clipboard().setText(txt)
            self._log("已复制视频分析结果到剪贴板", "ok")
        else:
            self._log("暂无可复制的分析结果", "warn")

    def _va_export(self):
        txt = self.va_text.toPlainText().strip()
        if not txt:
            self._log("暂无可导出的分析结果", "warn")
            return
        name = re.sub(r'[\\/:*?"<>|]', "_", self._va_wtitle or "视频分析")
        path, _ = QFileDialog.getSaveFileName(self, "导出视频分析", f"{name}_视频分析.txt", "文本 (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.va_text.toPlainText())
            self._log(f"分析结果已导出: {path}", "ok")
        except Exception as ex:
            self._log(f"导出失败: {ex}", "error")

    def _va_clear(self):
        self.va_text.setPlainText("")
        self.va_status.setText("未分析")
        if self._va_ep is not None and hasattr(self, "epgrid"):
            self.epgrid.set_status(self._va_ep, "就绪")
        self._va_ep = None

    def _eq_tick(self):
        if self._playing_idx < 0 or self._playing_idx >= self.epgrid.count():
            self._eq_timer.stop()
            return
        pats = ["▁▃▅▇▃", "▃▇▅▂▅", "▅▂▇▃▆", "▇▅▃▁▇", "▂▆▇▅▃"]
        self._eq_frame += 1
        bars = pats[self._eq_frame % len(pats)]
        bars = " ".join(bars)
        self.epgrid.set_equalizer_text(self._playing_idx, bars)

    def _stop_playback(self):
        self.player.stop()
        self._eq_timer.stop()
        self.epgrid.set_playing(-1)
        self._playing_idx = -1

    # ---------- 播放控制 ----------
    def _player_toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self._eq_timer.stop()
        else:
            self._eq_timer.start()
            self.player.play()

    def _on_state(self, state):
        # 播放/暂停状态已由点画面控制，无需按钮反馈
        pass

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._eq_timer.stop()
            self.epgrid.set_playing(-1)
            self._playing_idx = -1

    def _on_pos(self, ms):
        dur = self.player.duration()
        if not getattr(self, "_seeking", False):
            self.sld.blockSignals(True)
            self.sld.setValue(int(ms / max(dur, 1) * 1000) if dur > 0 else 0)
            self.sld.blockSignals(False)
        self.time_lbl.setText(f"{self._fmt(ms)} / {self._fmt(dur)}")

    def _on_dur(self, dur):
        self.sld.setMaximum(1000)
        self.time_lbl.setText(f"00:00 / {self._fmt(dur)}")

    @staticmethod
    def _fmt(ms):
        if ms < 0:
            ms = 0
        s = int(ms // 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _seek(self, val):
        dur = self.player.duration()
        if dur > 0:
            self.player.setPosition(int(val / 1000 * dur))

    def _seek_released(self):
        self._seeking = False
        self._seek(self.sld.value())

    def _set_volume(self, val):
        self.audio.setVolume(val / 100)

    def _show_volpopup(self):
        self._vol_t.stop()
        self.player_view._relayout()
        self.vol_popup.show(); self.vol_popup.raise_()

    def _hide_volpopup(self):
        self.vol_popup.hide()

    def _make_vol_icon(self):
        return vol_icon()

    def _update_download_btn(self):
        n = self._checked_rows()
        self.download_btn.setText(f"⬇ 下载选中（{len(n)}）")

    def _checked_rows(self):
        return self.epgrid.selected()

    # ---------- 下载 ----------
    def _download_selected(self):
        rows = self._checked_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先勾选要下载的视频（可用「全选」）。")
            return
        output_dir = self.dir_edit.text().strip() or config.DEFAULT_DOWNLOAD_PATH
        os.makedirs(output_dir, exist_ok=True)
        naming = self.name_combo.currentData()

        items = []
        for r in rows:
            info = self.epgrid.info_at(r)
            url = info.get("url", "")
            if not url or bool(info.get("is_local")):
                continue
            items.append((r, url, info.get("referer", "") or "", info.get("is_m3u8", False),
                          info.get("title", "")))
        if not items:
            return

        self.download_btn.setEnabled(False)
        self.bar2.setValue(0)
        self._log(f"开始下载 {len(items)} 个视频 → {output_dir}", "info")

        self.worker = DownloadWorker(items, output_dir, naming, {"User-Agent": config.DEFAULT_HEADERS.get("User-Agent", "")}, self)
        self.worker.item_status.connect(self._on_item_status)
        self.worker.log_msg.connect(self._on_log)
        self.worker.all_done.connect(self._on_all_done)
        self.worker.start()

    def _on_item_status(self, row, text, pct):
        self.epgrid.set_status(row, text)
        if pct and pct >= 0:
            self.bar2.setValue(max(self.bar2.value(), pct))

    def _on_log(self, msg, level):
        self._log(msg, level)

    # ---- 全局运行状态灯 ----
    def _global_status_tick(self):
        tasks = []
        if getattr(self, "_va_running", False):
            tasks.append("视频分析")
        ocr = getattr(self, "_ocr_worker", None)
        if ocr is not None and ocr.isRunning():
            tasks.append("字幕识别")
        ext = getattr(self, "_ext_worker", None)
        if ext is not None and ext.isRunning():
            tasks.append("批量提取")
        if getattr(self, "_batch_idx", -1) >= 0:
            tasks.append("批量浏览")
        dl = getattr(self, "worker", None)
        if dl is not None and dl.isRunning():
            tasks.append("下载")
        # 视频生成任务
        if getattr(self, "_gen_creating", False):
            tasks.append("创建生成任务…")
        gp = getattr(self, "_gen_poll", None)
        if gp is not None and gp.isRunning():
            tasks.append("视频生成中")
        if getattr(self, "_gen_batch_run", False):
            idx = len(getattr(self, "_gen_batch_q", []))
            tasks.append(f"批量生成剩{idx}集")
        # 各生成区卡片自管的生成线程（创建 / 轮询 / 保存）——此前漏检导致生成时状态灯恒显"空闲"
        ga = getattr(self, "_gen_areas", None) or []
        _run_areas = 0
        _save_areas = 0
        for _a in ga:
            if getattr(_a, "_creating", False) or \
               (getattr(_a, "_poll", None) is not None and _a._poll.isRunning()):
                _run_areas += 1
            elif getattr(_a, "_save_thread", None) is not None and _a._save_thread.isRunning():
                _save_areas += 1
        if _run_areas:
            tasks.append(f"视频生成·{_run_areas}区")
        if _save_areas:
            tasks.append(f"视频保存·{_save_areas}区")
        if tasks:
            desc = "，".join(tasks)
            self.global_status.setText("● " + desc)
            self.global_status.setStyleSheet("color:#22c55e; font-weight:800; font-size:13px;")
            self.global_status.setToolTip(desc)
        else:
            self.global_status.setText("● 空闲")
            self.global_status.setStyleSheet("color:#ef4444; font-weight:800; font-size:13px;")
            self.global_status.setToolTip("当前无任务运行")

    def _global_status_click(self, event):
        # 点击状态灯：把日志面板切到前台并聚焦，方便查看进度明细
        try:
            self.nav_tabs.setCurrentWidget(self.sniff_page)
            self.log_view.setFocus()
        except Exception:
            pass

    def _update_breadcrumb(self):
        """刷新底部状态栏面包屑：项目 › 页名（› 分集）+ 当前操作区提示。"""
        if not hasattr(self, "breadcrumb"):
            return
        proj = self.proj_title.text() if hasattr(self, "proj_title") else "未打开项目"
        if proj in ("未打开项目", ""):
            proj = "首页"
        try:
            w = self.nav_tabs.currentWidget()
        except Exception:
            w = None
        page = "视频嗅探"
        ep = ""
        if w is self.asset_page:
            page = "资产管理"
            ep = getattr(self, "_cur_asset_episode", "") or ""
        elif w is getattr(self, "_gen_episode_stk", None):
            page = "视频生成"
            ep = getattr(self, "_current_gen_episode", "") or ""
        parts = [proj, page]
        if ep:
            parts.append(ep)
        self.breadcrumb.setText("  ›  ".join(parts))

    def _update_log_count(self):
        """刷新底部状态栏右侧的日志条数（与嗅探主日志同源）。"""
        if not hasattr(self, "sb_log_count"):
            return
        try:
            n = self.log_view.blockCount()
            self.sb_log_count.setText("日志: %d 条" % n)
        except Exception:
            pass

    def _log(self, msg, level="info"):
        colors = {"info": "#38bdf8", "ok": "#4ade80", "warn": "#facc15",
                  "error": "#f87171", "debug": "#94a3b8"}
        color = colors.get(level, "#e2e8f0")
        ts = datetime.now().strftime("%H:%M:%S")
        html = f'<span style="color:{color}">[{ts}] {msg.replace(chr(60), "&lt;")}</span>'
        self.log_view.appendHtml(html)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())
        # 全部日志页签：镜像全量（与嗅探主日志同源）
        ga = getattr(self, "gen_log_all_tab", None)
        if ga is not None:
            ga.appendHtml(html)
            s2 = ga.verticalScrollBar()
            s2.setValue(s2.maximum())
        # 生成日志页签：仅当用户停在生成页时跟随写入，避免其他页签切换打断阅读
        gt = getattr(self, "gen_log_tabs", None)
        gv = getattr(self, "gen_log_view", None)
        if gt is not None and gv is not None and gt.currentIndex() == 0:
            gv.appendHtml(html)
            s3 = gv.verticalScrollBar()
            s3.setValue(s3.maximum())
        # 底部状态栏：日志计数 + 最近一条日志摘要（带级别标记）
        mark = {"info": "", "ok": "✓ ", "warn": "⚠ ", "error": "✗ ", "debug": ""}.get(level, "")
        try:
            self._update_log_count()
            if hasattr(self, "sb_action"):
                self.sb_action.setText((mark + str(msg))[:40])
        except Exception:
            pass

    def _on_all_done(self, ok, fail):
        self.download_btn.setEnabled(True)
        self.bar2.setValue(100)
        self._log(f"下载结束：成功 {ok} 项，失败 {fail} 项。", "ok" if fail == 0 else "warn")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        if self._mitm_worker and self._mitm_worker.isRunning():
            if self._sys_proxy_on:
                mitm_set_proxy(False, self._proxy_port)
            self._mitm_worker.stop()
            self._mitm_worker.wait(2000)
        # 统一停止其余后台线程/定时器/播放器，避免进程残留
        self._shutdown_background()
        # 退出前保存当前剧集的生成区内容与数量
        try:
            self._save_gen_areas_to_episode()
        except Exception:
            pass
        self._save_window_geometry()
        event.accept()

    def _shutdown_background(self):
        """关闭窗口时统一停止后台线程、定时器与播放器，防止进程残留。"""
        # 1) 生成区轮询 / 创建任务 / AI 分段线程
        for name in ("_gen_poll", "_gen_create", "_gen_split_ai_worker"):
            th = getattr(self, name, None)
            if th is not None:
                try:
                    if th.isRunning():
                        th.stop()
                        th.wait(2000)
                except Exception:
                    pass
        # 2) 重启接续的轮询线程（下载线程无法中断，仅解除引用）
        for th in list(getattr(self, "_gen_resume_workers", None) or []):
            try:
                if th.isRunning():
                    th.stop()
                    th.wait(2000)
            except Exception:
                pass
        self._gen_resume_workers = []
        self._gen_resume_saves = []
        # 3) 视频分析：置停止标志 + 停止 OCR 抽帧线程
        self._va_running = False
        self._va_stop_flag = True
        ow = getattr(self, "_va_ocr_worker", None)
        if ow is not None:
            try:
                if ow.isRunning():
                    ow.stop()
                    ow.wait(2000)
            except Exception:
                pass
        gb = getattr(self, "_va_grabber", None)
        if gb is not None:
            try:
                gb.stop()
            except Exception:
                pass
        # 4) 提取工作线程
        ew = getattr(self, "_ext_worker", None)
        if ew is not None:
            try:
                if ew.isRunning():
                    ew.stop()
                    ew.wait(2000)
            except Exception:
                pass
        # 5) 定时器全部停止
        for tname in ("_gen_batch_submit_timer", "_scan_timer", "_cons_timer",
                      "_ext_pick_timer", "_ext_cap_timer"):
            t = getattr(self, tname, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
        # 6) 播放器停止并释放媒体资源
        for pname in ("player", "gen_player"):
            pl = getattr(self, pname, None)
            if pl is not None:
                try:
                    pl.stop()
                    pl.setSource(QUrl())
                except Exception:
                    pass

    # ---------------- 窗口尺寸：可自由调节 + 记住上次大小 ----------------
    def _init_window_geometry(self):
        """恢复上次的窗口几何；首次启动按屏幕可用区取一个舒适尺寸（不全屏、不最大化）。"""
        try:
            saved = self._settings.value("geometry", None)
            if saved:
                self.restoreGeometry(saved)
                st = self._settings.value("splitter", None)
                if st and hasattr(self, "main_splitter"):
                    self.main_splitter.restoreState(st)
                # 上次尺寸超出当前屏幕（换显示器 / 改分辨率）时退回自适应
                scr = QApplication.primaryScreen().availableGeometry()
                g = self.frameGeometry()
                if g.width() > scr.width() or g.height() > scr.height() or self.width() < WIN_MIN_W:
                    self._apply_default_geometry()
            else:
                self._apply_default_geometry()
        except Exception:
            self._apply_default_geometry()
        self._geo_ready = True
        # 首帧布局完成后：给分割条一个均分比例，并按窗口尺寸刷新一次内部组件
        QTimer.singleShot(120, self._post_init_layout)

    def _post_init_layout(self):
        try:
            if hasattr(self, "main_splitter"):
                sp = self.main_splitter
                if not self._settings.value("splitter", None):
                    w = max(2, sp.width())
                    half = w // 2
                    sp.setSizes([half, w - half])
            self._apply_responsive()
        except Exception:
            pass

    def _apply_default_geometry(self):
        """按屏幕可用区计算默认尺寸并居中（四周留边，明确不是全屏/最大化）。"""
        scr = QApplication.primaryScreen().availableGeometry()
        w = min(WIN_MAX_W, max(WIN_MIN_W, int(scr.width() * WIN_RATIO_W)))
        h = min(WIN_MAX_H, max(WIN_MIN_H, int(scr.height() * WIN_RATIO_H)))
        w = min(w, max(WIN_MIN_W, scr.width() - 40))
        h = min(h, max(WIN_MIN_H, scr.height() - 40))
        self.resize(w, h)
        fg = self.frameGeometry()
        fg.moveCenter(scr.center())
        self.move(fg.topLeft())

    def _save_window_geometry(self):
        try:
            self._settings.setValue("geometry", self.saveGeometry())
            if hasattr(self, "main_splitter"):
                self._settings.setValue("splitter", self.main_splitter.saveState())
        except Exception:
            pass

    def _reset_layout(self):
        """「⤢ 重置布局」：窗口尺寸与内部面板比例回到默认（不动任何数据）。"""
        self._settings.remove("geometry")
        self._settings.remove("splitter")
        self._apply_default_geometry()
        if hasattr(self, "main_splitter"):
            w = max(2, self.main_splitter.width())
            half = w // 2
            self.main_splitter.setSizes([half, w - half])
        self._apply_responsive()
        self._log("已重置窗口布局：可直接拖拽窗口边缘自由调整大小", "ok")

    def resizeEvent(self, event):
        """窗口尺寸变化时让内部组件按比例跟着走（120ms 防抖，拖动更顺）。"""
        super().resizeEvent(event)
        if getattr(self, "_geo_ready", False):
            self._rz_timer.start()

    def _apply_responsive(self):
        """按当前窗口尺寸调整内部组件：结果区高度、首页卡片列宽、剧集网格列数。"""
        if not getattr(self, "_geo_ready", False):
            return
        try:
            h = max(1, self.height())
            # ① 播放器下方的结果区：约占窗口高度 42%，设上下限避免小窗口挤没播放器
            if hasattr(self, "result_tabs"):
                self.result_tabs.setMaximumHeight(max(200, min(560, int(h * 0.42))))
            # ② 首页项目卡片：列数随宽度增减，卡宽在合理区间浮动
            if hasattr(self, "home_list"):
                avail = max(320, self.home_page.width() - 48)
                cols = max(2, min(8, avail // 200))
                cw = max(180, (avail - (cols - 1) * 4) // cols)
                self.home_list.setGridSize(QSize(cw + 6, 110))
            # ③ 剧集网格：列数随栏宽变化
            if hasattr(self, "epgrid"):
                self.epgrid._relayout()
        except Exception:
            pass


def main():
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    # 幻镜AI Logo
    _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo-c-icon.png")
    if os.path.exists(_icon_path):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(_icon_path))
    app.setApplicationName("幻镜AI")
    # 单实例互斥：项目/资产/剧本等都以本地文件持久化，多实例同时运行会互相覆盖（
    # 典型现象：资产生成图文件已落盘、但 assets.json 被另一实例用旧数据整表覆盖，卡片无图）。
    from PySide6.QtCore import QSharedMemory
    _singleton = QSharedMemory("HuanjingAI_SingleInstance_v1")
    if not _singleton.create(1):
        QMessageBox.information(
            None, "程序已在运行",
            "本程序已在运行，请切换到已打开的窗口使用。\n\n"
            "多开会导致多个窗口同时写入同一项目，造成资产生成图、"
            "剧本与历史记录互相覆盖丢失，因此已限制为单实例。")
        return 0
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
