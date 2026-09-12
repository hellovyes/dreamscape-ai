# -*- coding: utf-8 -*-
"""视频智能分析：抽取视频帧，调用智谱 GLM 多模态大模型生成结构化分析 / AI 视频生成提示词。
对标 prompt-lens 的“视频分析”(lib/ai/analyzer.ts + frame-extractor)。

适配模型：GLM-5.3（旗舰）、GLM-5.3-Flash（原生多模态，推荐用于视频/图片分析）。
接口：OpenAI 兼容的 BigModel /v4/chat/completions，无需第三方依赖。

抽帧方案：本软件捆绑的 ffmpeg 为 --disable-everything 精简版（仅 mp4 复用 + libx264 编码，
无法输出 JPG/PNG 图片），因此改用 Qt Multimedia 的 QMediaPlayer＋QVideoSink 在 GUI 主线程
静音播放视频并截图取帧（QVideoFrame.toImage→PNG→base64），再由 GLM 网络请求线程发送分析。
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QThread, Signal, QUrl, Qt, QBuffer, QByteArray, QIODevice
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PySide6.QtGui import QImage

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


# ---------------- 提示词模板（源自 prompt-lens lib/ai/prompts/zh.ts） ----------------

SINGLE_PROMPT_ZH = (
    "# 视频镜头提示词反推专家\n\n"
    "你是一位精通视觉语言和AI视频生成的提示词工程师。请对这张视频截图进行专业级深度分析，"
    "并输出可直接用于AI视频生成的结构化提示词。\n\n"
    "请按以下维度逐项分析：画面主体(人物特征/动作/服装配饰)、环境场景(类型/时段天气/背景/空间深度)、"
    "镜头语言(角度/景别/运镜/焦点/构图)、光影照明(光源/方向/光比/色温)、美术风格(视觉风格/色彩/质感/后期)、"
    "氛围情绪(基调/叙事/感官/节奏)。\n\n"
    "输出格式严格为：\n"
    "第一行【画面深度描述】：用150-200字生动精准地整体描述画面。\n"
    "空一行后输出【AI视频生成提示词】板块：必须包含一行以『核心提示词：』开头、可直接用于AI视频生成的精炼提示词"
    "(含最关键元素)，再分项列出主体详细、场景环境、镜头语言、光影照明、美术风格、氛围情绪，"
    "最后是技术参数建议(宽高比/运动强度/时长建议/负面提示词)。\n\n"
    "注意：禁止任何开场白、自我介绍、寒暄语；禁止使用 Markdown 语法；第一个输出字符必须是【。"
)

BATCH_PROMPT_ZH = (
    "# 视频镜头序列提示词反推专家\n\n"
    "你是一位精通影视语言和AI视频生成的提示词工程师。这组截图来自同一视频的不同帧，请进行序列级分析，"
    "重点关注时间连贯性、镜头运动、视觉一致性与叙事节奏。\n\n"
    "分析维度：帧间变化与运动轨迹、运镜类型与速度方向、跨帧视觉一致性(色调/光影/质感)、叙事节奏与情绪曲线、"
    "最突出的视觉冲击帧。\n\n"
    "输出格式严格为：\n"
    "第一行【视频整体分析】：含叙事概述(150字以内)与镜头运动分析。\n"
    "接着输出【视觉风格统一分析】：含风格特征与逐帧关键差异。\n"
    "最后输出【AI视频复现提示词】：必须包含一行以『核心提示词：』开头、含运镜/主体/场景核心描述的精炼提示词，"
    "再分项列出运镜参数、主体元素、场景设定、光影风格、美术风格、氛围情绪及技术参数建议。\n\n"
    "注意：禁止任何开场白、自我介绍、寒暄语；禁止使用 Markdown 语法；第一个输出字符必须是【。"
)

PROMPTS = {"single": SINGLE_PROMPT_ZH, "batch": BATCH_PROMPT_ZH}

SEGMENT_PROMPT_ZH = (
    "# 视频分段镜头分析专家\n\n"
    "这是某视频按时间切出的第 {seg} 小段，时间范围约 {t0:.1f}-{t1:.1f} 秒。没有提供字幕文本，"
    "请完全依靠这几张画面帧进行推断。\n"
    "请结合本段画面帧，逐项输出：\n"
    "一、本段概述（60字内）。\n"
    "二、人物与动作：登场人物、外貌、姿态、动作。\n"
    "三、台词/对白：根据画面中人物的口型、字幕痕迹、场景与肢体语言推断本段人物说的话与对话往来，"
    "逐句整理并标注说话人；所有推断台词每句注明「（推断）」。\n"
    "四、镜头与场景：景别、运镜、构图、背景环境、光影色调。\n"
    "五、AI视频生成提示词片段：以『核心提示词：』开头给出一句精炼提示，再分项列出主体/场景/运镜/光影/风格/氛围。\n\n"
    "禁止开场白与自我介绍，禁止 Markdown 语法，第一个输出字符必须是「一」或「【」。"
)

# 不含台词推断的段分析提示词：台词改由本机 SubtitleOCR 识别，分析只聚焦画面
SEGMENT_PROMPT_NO_DIALOGUE_ZH = (
    "# 视频分段镜头分析专家\n\n"
    "这是某视频按时间切出的第 {seg} 小段，时间范围约 {t0:.1f}-{t1:.1f} 秒。"
    "本段仅做画面内容分析，不涉及台词/字幕。\n"
    "请结合本段画面帧，逐项输出：\n"
    "一、本段概述（60字内）。\n"
    "二、人物与动作：登场人物、外貌、姿态、动作。\n"
    "三、镜头与场景：景别、运镜、构图、背景环境、光影色调。\n"
    "四、AI视频生成提示词片段：以『核心提示词：』开头给出一句精炼提示，再分项列出主体/场景/运镜/光影/风格/氛围。\n\n"
    "禁止开场白与自我介绍，禁止 Markdown 语法，第一个输出字符必须是「一」或「【」。"
)

SEGMENT_COMBINE_ZH = (
    "# 视频整体整合专家\n\n"
    "以下是同一条视频按时间顺序切分出的多个小段分析（每段已含画面与对白）。请整合成一份完整报告：\n"
    "1.【视频整体概述】叙事主线、主题与节奏（200字内）。\n"
    "2.【人物表】全部登场人物、性格与关系。\n"
    "3.【完整台词对白】按时间顺序整理所有人物对话并标注说话人（无法确定的推断句注明「推断」）。\n"
    "4.【分镜时间线】按小段列出：时间范围 + 关键画面 + 剧情节点。\n"
    "5.【AI视频复现提示词】以『核心提示词：』开头给出可复现整段视频的代表性提示词，再分项列出。\n\n"
    "各小段分析如下：\n\n{parts}\n\n"
    "禁止开场白与自我介绍，禁止 Markdown 语法，第一个输出字符必须是「一」或「【」。"
)

# 整合提示词（带 OCR 真实字幕）：台词以 OCR 识别结果为准，画面分析不含台词
SEGMENT_COMBINE_WITH_DIALOGUE_ZH = (
    "# 视频整体整合专家\n\n"
    "以下是同一条视频按时间顺序切分出的多个小段画面分析（每段仅含画面，不含台词）。"
    "另附由字幕识别引擎(SubtitleOCR)从视频硬字幕中真实识别出的台词时间线：\n\n"
    "{dialogue}\n\n"
    "请整合成一份完整报告：\n"
    "1.【视频整体概述】叙事主线、主题与节奏（200字内）。\n"
    "2.【人物表】全部登场人物、性格与关系。\n"
    "3.【完整台词对白】以 OCR 识别到的台词为准，按时间顺序逐条整理并标注说话人"
    "（说话人依据画面与剧情合理判断；某时间点若 OCR 未识别到则省略）。\n"
    "4.【分镜时间线】按小段列出：时间范围 + 关键画面 + 剧情节点。\n"
    "5.【AI视频复现提示词】以『核心提示词：』开头给出可复现整段视频的代表性提示词，再分项列出。\n\n"
    "各小段画面分析如下：\n\n{parts}\n\n"
    "禁止开场白与自我介绍，禁止 Markdown 语法，第一个输出字符必须是「一」或「【」。"
)

# 一键将「视频分析报告（可含多剧集累加）」反推为完整剧本
SCRIPT_PROMPT_ZH = (
    "# 短视频影视剧本创作专家\n\n"
    "下面是若干条短视频的「视频智能分析报告」，可能包含整体概述、人物表、台词对白、分镜时间线、"
    "AI复现提示词等。请你据此反推成可直接用于拍摄/制作的分集【剧本】。\n\n"
    "编写要求：\n"
    "1. 逐集（每集一条完整剧本）组织，集与集之间用「第N集」标题区分；若报告只有一集则只写一集。\n"
    "2. 使用标准剧本格式：场景标题（【场景/景别·地点·时间】）、画面动作与氛围描述、"
    "人物台词（每句标注说话人）、必要的镜头/运镜提示、情绪节奏提示。\n"
    "3. 严格依据报告中的台词、人物与分镜时间线还原剧情，不虚构报告未提及的关键情节；"
    "报告未给出、但剧情衔接必需的细节，用【待补充：…】标注。\n"
    "4. 台词忠于原文，长段按说话人分行；动作/描写是剧本中加粗或括号说明。\n\n"
    "分析报告如下：\n\n{report}\n\n"
    "直接输出剧本正文，禁止开场白与 Markdown 语法，第一个输出字符必须是「第」或「【」。"
)


def normalize_path(p):
    """去掉 file:// 前缀"""
    if isinstance(p, str) and p.startswith("file://"):
        import urllib.request as _ur
        return _ur.url2pathname(p[len("file://"):])
    return p


# ---------------- GLM 接口调用 ----------------

def _ascii_key(key):
    """仅保留可见 ASCII 字符，过滤中文/文档文字/控制符/空白。
    防止误粘贴的无关文本进入 HTTP Authorization 头导致 latin-1 编码崩溃。"""
    if not key:
        return ""
    return "".join(ch for ch in str(key) if 33 <= ord(ch) <= 126)


def call_glm(api_key, prompt, frames, base_url=None, model="glm-5.3-flash",
             timeout=240, progress_cb=None, max_tokens=4096):
    """调用智谱 GLM 多模态接口（/v4/chat/completions），返回分析文本。frames 为 base64 data URL 列表。"""
    base_url = base_url or DEFAULT_BASE_URL
    # "agnes-2.5-flash-cn" 仅用于 UI 哨兵以选中中国站 Base URL；发送时需还原成真实模型名
    if model == "agnes-2.5-flash-cn":
        model = "agnes-2.5-flash"
    elif model == "agnes-3.0-flash-cn":
        model = "agnes-3.0-flash"
    api_key = _ascii_key(api_key)
    if not api_key:
        raise RuntimeError("API Key 无效：当前配置值包含中文/文档文字等非 ASCII 字符，"
                           "请到「⚙ AI 服务 → 视频分析」填入有效的密钥")
    content = [{"type": "text", "text": prompt}]
    for fr in frames:
        content.append({"type": "image_url", "image_url": {"url": fr}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"},
        method="POST",
    )
    if progress_cb:
        progress_cb("调用 GLM 分析…")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            det = json.loads(body)
            msg = det.get("error", {}).get("message") or det.get("message") or body
        except Exception:
            msg = body or str(e)
        raise RuntimeError("GLM 接口错误(%s): %s" % (e.code, msg))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("GLM 未返回分析内容")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError("GLM 返回内容为空")
    return content


# ---------------- 帧抓取（GUI 主线程，Qt 静音播放截图） ----------------

class FrameGrabber(QObject):
    progress = Signal(str)
    finished = Signal(list)      # base64 data URL（PNG）
    failed = Signal(str)

    def __init__(self, path, count=6, max_w=768, playback_rate=1.0, parent=None):
        super().__init__(parent)
        self.path = normalize_path(path)
        self.count = max(1, int(count))
        self.max_w = max_w
        self._player = QMediaPlayer(self)
        self._ao = QAudioOutput(self)
        self._ao.setMuted(True)
        self._player.setAudioOutput(self._ao)
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_err)
        self._player.positionChanged.connect(self._on_pos)
        self._times = []
        self._grabbed = set()
        self._frames = []
        self._started = False
        self._fin = False
        self._last_pos = -1

    def start(self):
        if self._started or self._fin:
            return
        self._started = True
        self.progress.emit("打开视频…")
        self._player.setSource(QUrl.fromLocalFile(self.path))
        self._player.play()

    def _on_status(self, st):
        p = self._player
        if st in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                  QMediaPlayer.MediaStatus.BufferingMedia):
            dur = p.duration()
            if not self._times and dur > 0:
                n = max(1, len(self._frames)) if self._frames else self.count
                self._times = [int(dur * (i + 1) / (n + 1)) for i in range(n)]
                self.progress.emit("开始抽帧…")
        elif st == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish()
        elif st in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            if not self._frames:
                self._fail("无法打开视频媒体")

    def _on_pos(self, pos):
        self._last_pos = pos

    def _on_frame(self, frame):
        if not frame.isValid() or self._fin:
            return
        pos = self._player.position()
        if pos < 0:
            pos = 0
        for i, t in enumerate(self._times):
            if i in self._grabbed:
                continue
            if pos >= t:
                du = self._frame_to_dataurl(frame)
                if du:
                    self._grabbed.add(i)
                    self._frames.append(du)
                    self.progress.emit("抽取帧 %d/%d" % (len(self._frames), self.count))
        if len(self._frames) >= self.count:
            self._finish()

    def _frame_to_dataurl(self, frame):
        try:
            img = frame.toImage()
            if img.isNull():
                return None
            if img.width() > self.max_w:
                h = max(1, int(img.height() * self.max_w / img.width()))
                img = img.scaled(self.max_w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            ok = img.save(buf, "PNG")
            buf.close()
            if not ok or not ba:
                return None
            raw = bytes(ba.data())
            return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except Exception:
            return None

    def _on_err(self, err, s):
        if not self._frames:
            self._fail("播放器错误: %s" % (s or str(err)))

    def _fail(self, msg):
        if self._fin:
            return
        self._fin = True
        try:
            self._player.stop()
        except Exception:
            pass
        self.failed.emit(msg)

    def _finish(self):
        if self._fin:
            return
        self._fin = True
        try:
            self._player.stop()
        except Exception:
            pass
        if self._frames:
            self.finished.emit(self._frames)
        else:
            self.failed.emit("未能从视频中抽取到任何帧")


# ---------------- 场景切分（GUI 主线程，Qt 静音播放 + 帧差分检测镜头切换） ----------------

class SceneSegmenter(QObject):
    """按场景/画面切换把视频切分为 6-10 秒的小段，并为每段收集代表帧。

    播放时周期性换算灰度拇指图，计算相邻采样帧差分，超过阈值判为镜头切换；
    在到达场景切换点且段落已满约 6 秒时闭合一段，最多 10 秒强切。
    finished 携带 {duration, segments:[(s,e),...], seg_frames:[[dataurl,...],...]}。
    """
    progress = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    MIN_SEG = 6.0
    MAX_SEG = 10.0
    SAMPLE = 0.3      # 帧差分采样间隔（秒）
    CUT_THR = 0.34    # 归一化灰度差分阈值

    def __init__(self, path, max_w=448, parent=None):
        super().__init__(parent)
        self.path = normalize_path(path)
        self.max_w = max_w
        self._player = QMediaPlayer(self)
        self._ao = QAudioOutput(self)
        self._ao.setMuted(True)
        self._player.setAudioOutput(self._ao)
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_err)
        self._started = False
        self._fin = False
        self._seen = False
        self._duration = 0.0
        self._cur_time = 0.0
        self._last_analyze = -1.0
        self._prev_gray = None
        self._seg_start = 0.0
        self._frames_now = []     # [(time, dataurl), ...] 当前段候选帧
        self._segments = []
        self._seg_frames = []

    def start(self):
        if self._started or self._fin:
            return
        self._started = True
        self.progress.emit("切分场景（画面解析中）…")
        self._player.setSource(QUrl.fromLocalFile(self.path))
        self._player.play()

    def _on_status(self, st):
        if st in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
                  QMediaPlayer.MediaStatus.BufferingMedia):
            if self._duration <= 0:
                d = self._player.duration()
                if d > 0:
                    self._duration = d / 1000.0
        elif st == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish()
        elif st in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            if not self._seen:
                self._fail("无法打开视频媒体")

    def _on_frame(self, frame):
        if self._fin or not frame.isValid():
            return
        self._seen = True
        t = self._player.position() / 1000.0
        if t < 0:
            t = self._cur_time
        self._cur_time = max(self._cur_time, t)
        if t - self._last_analyze < self.SAMPLE:
            return
        self._last_analyze = t
        gray = self._gray_bytes(frame)
        du = self._frame_to_dataurl(frame)
        if gray is None:
            return
        self._analyze(t, gray, du)

    def _analyze(self, t, gray, dataurl):
        if dataurl:
            self._frames_now.append((t, dataurl))
            # 只保留当前段窗口内的候选帧，避免内存膨胀
            if len(self._frames_now) > 60:
                self._frames_now = self._frames_now[-60:]
        diff = 0.0
        if self._prev_gray is not None:
            diff = self._gray_diff(self._prev_gray, gray)
        self._prev_gray = gray
        age = t - self._seg_start
        cut = diff > self.CUT_THR
        if cut and age >= (self.MIN_SEG - 1.0):
            self._close(t)
        elif age >= self.MAX_SEG:
            self._close(t)

    def _close(self, boundary_t):
        if self._fin:
            return
        self._close_impl(boundary_t)

    def _close_impl(self, boundary_t):
        end = min(boundary_t, self._duration or boundary_t)
        seg = (self._seg_start, end)
        if end - seg[0] < 0.5:
            return
        frames = self._pick(self._frames_now, seg)
        self._segments.append(seg)
        self._seg_frames.append(frames)
        self._seg_start = end
        self._frames_now = [f for f in self._frames_now if f[0] >= end - 0.6][:1]
        self._prev_gray = None
        self.progress.emit("切分场景：已分 %d 段…" % len(self._segments))

    def _pick(self, cand, seg):
        s, e = seg
        fs = [x for x in cand if s - 0.8 <= x[0] <= e + 0.8]
        if not fs:
            fs = list(cand)
        if len(fs) <= 3:
            return [d for _, d in fs]
        fs.sort(key=lambda x: x[0])
        mid = (s + e) / 2.0
        mid_dup = min(fs, key=lambda x: abs(x[0] - mid))
        out = [fs[0][1], mid_dup[1], fs[-1][1]]
        seen, res = set(), []
        for d in out:
            if d not in seen:
                seen.add(d)
                res.append(d)
        return res[:3]

    def _gray_bytes(self, frame, w=48, h=27):
        try:
            img = frame.toImage()
            if img.isNull():
                return None
            g = img.convertToFormat(QImage.Format_Grayscale8)
            g = g.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if g.isNull():
                return None
            data = bytes(g.constBits())
            want = g.sizeInBytes()
            return data[:want] if len(data) > want else data
        except Exception:
            return None

    @staticmethod
    def _gray_diff(a, b):
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        s = 0
        for i in range(n):
            s += abs(a[i] - b[i])
        return s / (n * 255.0)

    def _frame_to_dataurl(self, frame):
        try:
            img = frame.toImage()
            if img.isNull():
                return None
            if img.width() > self.max_w:
                h = max(1, int(img.height() * self.max_w / img.width()))
                img = img.scaled(self.max_w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            ok = img.save(buf, "PNG")
            buf.close()
            if not ok or not ba:
                return None
            return "data:image/png;base64," + base64.b64encode(bytes(ba.data())).decode("ascii")
        except Exception:
            return None

    def _on_err(self, err, s):
        if not self._seen:
            self._fail("播放器错误: %s" % (s or str(err)))

    def _fail(self, msg):
        if self._fin:
            return
        self._fin = True
        try:
            self._player.stop()
        except Exception:
            pass
        self.failed.emit(msg)

    def _finish(self):
        if self._fin:
            return
        self._fin = True
        try:
            self._player.stop()
        except Exception:
            pass
        dur = self._duration if self._duration > 0 else self._cur_time
        rest = dur - self._seg_start
        if rest >= self.MIN_SEG * 0.6 and self._seg_start < dur:
            self._close_impl(dur)
        elif self._segments:
            s0, e0 = self._segments[-1]
            self._segments[-1] = (s0, max(s0, dur))
        if not self._segments:
            frames = [d for _, d in self._frames_now][:3]
            self._segments = [(0.0, dur)]
            self._seg_frames = [frames]
            if not frames:
                self.failed.emit("未能从视频中切分出片段")
                return
        self.finished.emit({"duration": dur, "segments": self._segments, "seg_frames": self._seg_frames})


# ---------------- 工作线程 ----------------

class VaDownloadWorker(QThread):
    progress = Signal(str)
    done = Signal(str)           # 本地临时视频路径
    failed = Signal(str)

    def __init__(self, url, is_m3u8=False, parent=None):
        super().__init__(parent)
        self.url = url
        self.is_m3u8 = is_m3u8

    def run(self):
        try:
            from ocr_engine import download_to_temp
            p = download_to_temp(self.url, self.is_m3u8,
                                 lambda s: self.progress.emit(s))
            self.done.emit(p)
        except Exception as e:
            self.failed.emit(str(e))


class GlmWorker(QThread):
    progress = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, api_key, base_url, model, prompt, frames, timeout=240, parent=None):
        super().__init__(parent)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.prompt = prompt
        self.frames = frames
        self.timeout = timeout

    def run(self):
        try:
            if not self.api_key:
                self.failed.emit("未配置 GLM API Key，请在「视频分析 · 设置」中配置")
                return
            if not self.frames:
                self.failed.emit("没有可用的视频帧")
                return
            res = call_glm(self.api_key, self.prompt, self.frames,
                          base_url=self.base_url, model=self.model,
                          timeout=self.timeout,
                          progress_cb=lambda s: self.progress.emit(s))
            self.progress.emit("完成")
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class CombineWorker(QThread):
    """纯文本（无帧）GLM 调用：用于把多个分段结果整合成整体报告。"""
    progress = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, api_key, base_url, model, prompt, timeout=240, max_tokens=4096, parent=None):
        super().__init__(parent)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.prompt = prompt
        self.timeout = timeout
        self.max_tokens = max_tokens

    def run(self):
        try:
            if not self.api_key:
                self.failed.emit("未配置 GLM API Key，请在「视频分析 · 设置」中配置")
                return
            res = call_glm(self.api_key, self.prompt, [],
                           base_url=self.base_url, model=self.model,
                           timeout=self.timeout, max_tokens=self.max_tokens,
                           progress_cb=lambda s: self.progress.emit(s))
            self.progress.emit("整合完成")
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(str(e))