"""播放控件组件：OverlayPlayer、VolSlider、ClickVideoWidget、vol_icon、VideoPlayerCard、FloatPlayerWindow"""

import os
from PySide6.QtCore import Qt, QTimer, QSize, QRectF
from PySide6.QtGui import QIcon, QPainter, QColor, QPixmap, QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton,
    QSlider, QGraphicsView, QGraphicsScene, QGraphicsItem,
    QFrame, QSizePolicy,
)


# ─────────────────────────────────────────────
#  vol_icon – 返回喇叭图标的 QIcon（白色线条风格）
# ─────────────────────────────────────────────
def vol_icon():
    """返回一个白色喇叭图标的 QIcon。"""
    size = 24
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor(200, 200, 200))
    painter.setBrush(QColor(200, 200, 200))
    # 喇叭主体
    from PySide6.QtCore import QPoint
    painter.drawPolygon([
        QPoint(3, 8), QPoint(3, 16), QPoint(7, 16),
        QPoint(11, 20), QPoint(11, 4), QPoint(7, 8),
    ])
    # 声波弧线
    for r in (14, 17, 20):
        painter.drawArc(11, size // 2 - r // 2, r, r, 45 * 16, -90 * 16)
    painter.end()
    return QIcon(pixmap)


# ─────────────────────────────────────────────
#  VolSlider – 竖向音量滑块
# ─────────────────────────────────────────────
class VolSlider(QSlider):
    """竖向音量滑块，显示在悬浮的 vol_popup 里。"""

    def __init__(self, parent=None):
        super().__init__(Qt.Vertical, parent)
        self.setRange(0, 100)
        self.setValue(100)
        self.setFixedWidth(22)
        self.setFixedHeight(100)
        self.setStyleSheet("""
            QSlider::groove:vertical {
                background: rgba(255,255,255,0.15);
                width: 4px; border-radius: 2px;
            }
            QSlider::handle:vertical {
                height: 10px; width: 10px; margin: -3px 0;
                background: #fff; border-radius: 5px;
            }
            QSlider::sub-page:vertical {
                background: #3b82f6; border-radius: 2px;
            }
        """)


# ─────────────────────────────────────────────
#  VideoFrameItem – 用 QVideoSink 接收视频帧并自绘的画面项
#  兼容新版 Qt6 多媒体：QGraphicsVideoItem 在新后端不再渲染，改由这里绘制
# ─────────────────────────────────────────────
class VideoFrameItem(QGraphicsItem):
    def __init__(self, view=None):
        super().__init__()
        self._img = QImage()
        self._view = view  # 可选：收到帧后自动 fitInView 保持比例

    def set_frame(self, frame):
        if frame is None or not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            return
        self._img = img
        self.prepareGeometryChange()
        self.update()
        if self._view is not None:
            try:
                self._view.fitInView(self, Qt.KeepAspectRatio)
            except Exception:
                pass

    def boundingRect(self):
        return QRectF(0, 0, self._img.width() or 16, self._img.height() or 16)

    def paint(self, painter, option, widget=None):
        if not self._img.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(QRectF(0, 0, self._img.width(), self._img.height()), self._img)


# ─────────────────────────────────────────────
#  OverlayPlayer – 覆盖层播放器
#  在 QGraphicsView 上叠加控制条，鼠标移入时浮现。
# ─────────────────────────────────────────────
class OverlayPlayer(QGraphicsView):
    """在视频画面上叠放控制条，鼠标移入/移出控制浮沉。"""

    def __init__(self, player, on_tap=None, parent=None):
        super().__init__(parent)
        self.player = player
        self.on_tap = on_tap
        self._ctlbar = None
        self._vol_popup = None
        self._show_ctl = False
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet("background:black; border:none;")
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 视频输出
        self.video_item = VideoFrameItem(view=self)
        scene = QGraphicsScene(self)
        scene.addItem(self.video_item)
        self.setScene(scene)
        self._vsink = QVideoSink(self)
        self.player.setVideoSink(self._vsink)
        self._vsink.videoFrameChanged.connect(self.video_item.set_frame)

    def set_controls(self, ctlbar, vol_popup):
        """绑定外部控制条和音量弹窗。"""
        self._ctlbar = ctlbar
        self._vol_popup = vol_popup
        ctlbar.setParent(self)
        ctlbar.hide()
        vol_popup.setParent(self)
        vol_popup.hide()
        self._relayout()

    def _relayout(self):
        if self._ctlbar is not None:
            w, h = self.width(), self.height()
            self._ctlbar.setGeometry(0, h - 44, w, 44)
        if self._vol_popup is not None:
            w, h = self.width(), self.height()
            self._vol_popup.setGeometry(w - 36, h // 2 - 50, 30, 100)
        # 让视频画面自适应填充整个视图，并保持宽高比
        try:
            vr = self.video_item.boundingRect()
            if vr.width() > 0 and vr.height() > 0:
                self.fitInView(self.video_item, Qt.KeepAspectRatio)
        except Exception:
            pass

    def _on_tap(self, event):
        if self.on_tap:
            self.on_tap()

    def mousePressEvent(self, event):
        # 替代被移除的子控件覆盖层：直接在视图上拦截点击实现“点击播放/暂停”
        self._on_tap(event)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._show_ctl = True
        if self._ctlbar:
            self._ctlbar.show()
            self._ctlbar.raise_()
        self._relayout()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._show_ctl = False
        if self._ctlbar:
            self._ctlbar.hide()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()


# ─────────────────────────────────────────────
#  ClickVideoWidget – 带点击播放功能的视频 widget（兼容旧接口）
# ─────────────────────────────────────────────
class ClickVideoWidget(QWidget):
    """封装 QGraphicsView + 点击播放，主要用于旧版代码兼容。"""

    def __init__(self, player=None, parent=None):
        super().__init__(parent)
        self.player = player or QMediaPlayer(self)
        self.scene = QGraphicsScene(self)
        self.video_item = VideoFrameItem()
        self.scene.addItem(self.video_item)
        self._vsink = QVideoSink(self)
        self.player.setVideoSink(self._vsink)
        self._vsink.videoFrameChanged.connect(self.video_item.set_frame)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        view = QGraphicsView(self.scene, self)
        view.setStyleSheet("background:black; border:none;")
        view.setDragMode(QGraphicsView.NoDrag)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.player_view = view
        self.video_item._view = view
        lay.addWidget(view, 1)

        self._clicked = False

    def set_source(self, url):
        from PySide6.QtCore import QUrl
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(url) if os.path.exists(url) else QUrl(url))

    def play(self):
        self.player.play()

    def pause(self):
        self.player.pause()

    def stop(self):
        self.player.stop()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()


# ─────────────────────────────────────────────
#  VideoPlayerCard – 嵌入式视频卡片（兼容旧接口，生成区已改用 FloatPlayerWindow）
# ─────────────────────────────────────────────
class VideoPlayerCard(QWidget):
    """嵌入式视频播放卡片，保留以兼容旧代码。生成区已改用 FloatPlayerWindow。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("videoCard")
        self.setStyleSheet("""
            QWidget#videoCard {
                background: #0f172a;
                border: 1px solid #1e293b;
                border-radius: 8px;
            }
        """)
        self._closed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 视频画面
        self.player_view = QGraphicsView(self)
        self.player_view.setStyleSheet("background:black; border:none;")
        self.player_view.setDragMode(QGraphicsView.NoDrag)
        self.player_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.player_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.video_item = VideoFrameItem(view=self.player_view)
        scene = QGraphicsScene(self)
        scene.addItem(self.video_item)
        self.player_view.setScene(scene)

        self.audio = QAudioOutput(self)
        self.audio.setVolume(1.0)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self._vsink = QVideoSink(self)
        self.player.setVideoSink(self._vsink)
        self._vsink.videoFrameChanged.connect(self.video_item.set_frame)

        lay.addWidget(self.player_view, 1)

        # 底部控制条
        ctl = QWidget()
        ctl.setObjectName("cardCtrl")
        ctl.setStyleSheet("""
            QWidget#cardCtrl {
                background: qlineargradient(x1:0,y1:1,x1:0,y1:0,
                    stop:0 rgba(0,0,0,0.85), stop:1 rgba(0,0,0,0));
                border-top: 1px solid rgba(255,255,255,0.1);
            }
        """)
        cb = QHBoxLayout(ctl)
        cb.setContentsMargins(10, 6, 10, 10)
        cb.setSpacing(8)

        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(32, 32)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.15); color:#fff;
                border-radius: 16px; font-size: 13px; font-weight:bold;
            }
            QPushButton:hover { background: rgba(255,255,255,0.3); }
        """)
        self.play_btn.clicked.connect(self._toggle_play)
        cb.addWidget(self.play_btn)

        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(0, 1000)
        self.sld.setStyleSheet("""
            QSlider::groove:horizontal { height:4px; background:rgba(255,255,255,0.2); border-radius:2px; }
            QSlider::sub-page:horizontal { background:#3b82f6; border-radius:2px; }
            QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;
                background:#fff; border-radius:6px; }
        """)
        self._seeking = False
        self._want_play = False
        self.sld.sliderMoved.connect(self._seek)
        self.sld.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.sld.sliderReleased.connect(self._seek_released)
        cb.addWidget(self.sld, 1)

        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setStyleSheet("color:#e2e8f0; font-size:11px;")
        self.time_lbl.setMinimumWidth(80)
        self.time_lbl.setAlignment(Qt.AlignCenter)
        cb.addWidget(self.time_lbl)

        self.vol_btn = QPushButton("🔊")
        self.vol_btn.setFixedSize(28, 28)
        self.vol_btn.setCursor(Qt.PointingHandCursor)
        self.vol_btn.setStyleSheet("background:transparent; color:#fff; border:none; font-size:14px;")
        self.vol_btn.clicked.connect(self._toggle_mute)
        cb.addWidget(self.vol_btn)

        lay.addWidget(ctl)

        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state):
        self.play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_pos(self, ms):
        dur = self.player.duration()
        if not self._seeking:
            self.sld.blockSignals(True)
            self.sld.setValue(int(ms / max(dur, 1) * 1000) if dur > 0 else 0)
            self.sld.blockSignals(False)
        self.time_lbl.setText(f"{self._fmt(ms)} / {self._fmt(dur)}")

    def _on_dur(self, dur):
        self.sld.setMaximum(1000)
        self.time_lbl.setText(f"00:00 / {self._fmt(dur)}")

    def _seek(self, val):
        dur = self.player.duration()
        if dur > 0:
            self.player.setPosition(int(val / 1000 * dur))

    def _seek_released(self):
        self._seeking = False
        self._seek(self.sld.value())

    def _toggle_mute(self):
        if self.audio.volume() > 0:
            self._prev_vol = self.audio.volume()
            self.audio.setVolume(0)
            self.vol_btn.setText("🔇")
        else:
            self.audio.setVolume(self._prev_vol if hasattr(self, "_prev_vol") else 1.0)
            self.vol_btn.setText("🔊")

    @staticmethod
    def _fmt(ms):
        if ms < 0:
            ms = 0
        s = int(ms // 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    def set_source(self, target):
        self._want_play = False
        self.player.stop()
        from PySide6.QtCore import QUrl
        self.player.setSource(QUrl.fromLocalFile(target) if os.path.exists(target) else QUrl(target))

    def play(self):
        self._want_play = True
        st = self.player.mediaStatus()
        if st in (QMediaPlayer.LoadedMediaStatus, QMediaPlayer.BufferedMediaStatus,
                  QMediaPlayer.StalledMediaStatus, QMediaPlayer.EndedMediaStatus,
                  QMediaPlayer.PausedMediaStatus):
            self.player.play()
            self._maybe_refit()
        # 若媒体仍在加载中（LoadingMediaStatus），_on_media_status 到可读态时会自动开播

    def stop(self):
        self._want_play = False
        self.player.stop()

    def _maybe_refit(self):
        try:
            vr = self.video_item.boundingRect()
            if vr.width() > 16 and vr.height() > 16:
                self.player_view.fitInView(self.video_item, Qt.KeepAspectRatio)
        except Exception:
            pass

    def isClosed(self):
        return self._closed

    def closeEvent(self, event):
        self.player.stop()
        self._closed = True
        event.accept()


# ─────────────────────────────────────────────
#  FloatPlayerWindow – 悬浮小播放器窗口（QQ 浏览器风格）
# ─────────────────────────────────────────────
class FloatPlayerWindow(QWidget):
    """悬浮小播放器窗口（QQ 浏览器风格）：双击文件 / 点击播放三角时弹出。
    可拖拽移动、最小化、关闭；控制条悬浮在视频底部，无背景。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setWindowTitle("播放器")
        self.resize(480, 270)
        self._closed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 播放器主体
        self.audio = QAudioOutput(self)
        self.audio.setVolume(1.0)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self._seeking = False
        self._vol_t = QTimer(self)
        self._vol_t.setSingleShot(True)
        self._vol_t.setInterval(300)
        self._vol_t.timeout.connect(self._hide_volpopup)

        # 用 QVideoWidget 直接渲染（比 QVideoSink + 自绘 更稳定，避免收不到帧导致的黑屏）
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.video_widget = QVideoWidget(self)
        self.video_widget.setStyleSheet("background:#000000; border:none;")
        self.player.setVideoOutput(self.video_widget)
        layout.addWidget(self.video_widget, 1)

        # 控制条
        self.ctlbar = QWidget()
        self.ctlbar.setObjectName("floatCtrl")
        self.ctlbar.setStyleSheet("""
            QWidget#floatCtrl {
                background: qlineargradient(x1:0,y1:1,x1:0,y1:0,
                    stop:0 rgba(0,0,0,0.85), stop:1 rgba(0,0,0,0));
                border-top: 1px solid rgba(255,255,255,0.1);
            }
        """)
        cb = QHBoxLayout(self.ctlbar)
        cb.setContentsMargins(10, 6, 10, 10)
        cb.setSpacing(8)

        # 播放/暂停按钮
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(32, 32)
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.15); color:#fff;
                border-radius: 16px; font-size: 13px; font-weight:bold;
            }
            QPushButton:hover { background: rgba(255,255,255,0.3); }
        """)
        self.play_btn.clicked.connect(self._toggle_play)
        cb.addWidget(self.play_btn)

        # 进度条
        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(0, 1000)
        self.sld.setStyleSheet("""
            QSlider::groove:horizontal { height:4px; background:rgba(255,255,255,0.2); border-radius:2px; }
            QSlider::sub-page:horizontal { background:#3b82f6; border-radius:2px; }
            QSlider::handle:horizontal { width:12px; height:12px; margin:-4px 0;
                background:#fff; border-radius:6px; }
        """)
        self.sld.sliderMoved.connect(self._seek)
        self.sld.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.sld.sliderReleased.connect(self._seek_released)
        cb.addWidget(self.sld, 1)

        # 时间
        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setStyleSheet("color:#e2e8f0; font-size:11px;")
        self.time_lbl.setMinimumWidth(80)
        self.time_lbl.setAlignment(Qt.AlignCenter)
        cb.addWidget(self.time_lbl)

        # 音量按钮
        self.vol_btn = QPushButton("🔊")
        self.vol_btn.setFixedSize(28, 28)
        self.vol_btn.setCursor(Qt.PointingHandCursor)
        self.vol_btn.setStyleSheet("background:transparent; color:#fff; border:none; font-size:14px;")
        self.vol_btn.setToolTip("点击静音/恢复")
        self.vol_btn.clicked.connect(self._toggle_mute)
        cb.addWidget(self.vol_btn)

        layout.addWidget(self.ctlbar)

        # 信号连接
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_media_error)
        self._want_play = False
        self._on_play_error = None  # 可选回调 fn(msg, level)，把播放错误反馈给主界面日志

        # 拖拽支持
        self._drag_pos = None

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state):
        if state == QMediaPlayer.PlayingState:
            self.play_btn.setText("⏸")
        else:
            self.play_btn.setText("▶")

    def _on_media_status(self, status):
        """媒体状态变化：可读（Loaded/Buffered）时若要求播放则自动开播；结束时重新适配画面。"""
        if status in (QMediaPlayer.LoadedMediaStatus, QMediaPlayer.BufferedMediaStatus,
                      QMediaPlayer.StalledMediaStatus):
            if self._want_play:
                self.player.play()
        elif status == QMediaPlayer.EndedMediaStatus:
            self._maybe_refit()

    def _on_media_error(self, error, error_string, error_description):
        # 播放失败（在线地址过期、解码器缺失、文件损坏等）时给出可见反馈，避免静默黑屏
        try:
            if error == QMediaPlayer.NoError:
                return
            detail = error_description or error_string or str(error)
            print("[播放器错误] %s" % detail)
            if self._on_play_error:
                self._on_play_error("播放失败: %s" % detail, "error")
        except Exception:
            pass

    def _maybe_refit(self):
        # QVideoWidget 自动按宽高比适配画面，无需手动 fitInView
        pass

    def _on_pos(self, ms):
        dur = self.player.duration()
        if not self._seeking:
            self.sld.blockSignals(True)
            self.sld.setValue(int(ms / max(dur, 1) * 1000) if dur > 0 else 0)
            self.sld.blockSignals(False)
        self.time_lbl.setText(f"{self._fmt(ms)} / {self._fmt(dur)}")

    def _on_dur(self, dur):
        self.sld.setMaximum(1000)
        self.time_lbl.setText(f"00:00 / {self._fmt(dur)}")

    def _seek(self, val):
        dur = self.player.duration()
        if dur > 0:
            self.player.setPosition(int(val / 1000 * dur))

    def _seek_released(self):
        self._seeking = False
        self._seek(self.sld.value())

    def _toggle_mute(self):
        if self.audio.volume() > 0:
            self._prev_vol = self.audio.volume()
            self.audio.setVolume(0)
            self.vol_btn.setText("🔇")
        else:
            self.audio.setVolume(self._prev_vol if hasattr(self, "_prev_vol") else 1.0)
            self.vol_btn.setText("🔊")

    @staticmethod
    def _fmt(ms):
        if ms < 0:
            ms = 0
        s = int(ms // 1000)
        return f"{s // 60:02d}:{s % 60:02d}"

    def set_source(self, target):
        self._want_play = False
        self.player.stop()
        from PySide6.QtCore import QUrl
        self.player.setSource(QUrl.fromLocalFile(target) if os.path.exists(target) else QUrl(target))

    def play(self):
        self._want_play = True
        st = self.player.mediaStatus()
        if st in (QMediaPlayer.LoadedMediaStatus, QMediaPlayer.BufferedMediaStatus,
                  QMediaPlayer.StalledMediaStatus, QMediaPlayer.EndedMediaStatus,
                  QMediaPlayer.PausedMediaStatus):
            self.player.play()
            self._maybe_refit()
        # 若媒体仍在加载中（LoadingMediaStatus），_on_media_status 到可读态时会自动开播

    def stop(self):
        self._want_play = False
        self.player.stop()

    def isClosed(self):
        return self._closed

    def closeEvent(self, event):
        self.player.stop()
        self._closed = True
        event.accept()

    # ---- 拖拽移动 ----
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if event.pos().y() < self.height() - 44:
                self._drag_pos = event.globalPos()
                event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._drag_pos is not None:
            delta = event.globalPos() - self._drag_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self._drag_pos = event.globalPos()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def _hide_volpopup(self):
        pass
