# -*- coding: utf-8 -*-
"""SubtitleOCR(望言OCR) 引擎封装：通过 C ABI 调用 subocr.dll 识别视频硬字幕。
由 SubtitleOCR-3.1.2 的 interface.h 逆向包装，运行依赖随 EXE 打包为 ocr_engine_runtime。
"""
import ctypes
import os
import re
import subprocess
import sys
import time
import tempfile
import shutil
import threading

from PySide6.QtCore import QThread, Signal

try:
    from m3u8_downloader import M3U8Downloader
except Exception:
    M3U8Downloader = None

LANG = {0: "zh", 1: "en", 2: "ja", 3: "ko"}
LANG_CODE = {v: k for k, v in LANG.items()}

# 打包为窗口程序（--noconsole）时，隐藏 ffmpeg/ffprobe 子进程的黑色控制台窗口
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if getattr(subprocess, "CREATE_NO_WINDOW", None) else 0


def runtime_dir():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.join(base, "ocr_engine_runtime")
    if os.path.isdir(cand):
        return cand
    return None


def available():
    return runtime_dir() is not None


def _tool(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    return p if os.path.isfile(p) else None


def _is_h264(path):
    """用捆绑的 ffprobe 探测视频轨道编码，返回是否 H.264。"""
    probe = _tool("ffprobe.exe")
    if not probe:
        return True  # 探测不可用时不转码，保持原样
    try:
        r = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
            creationflags=_NO_WINDOW)
        codec = (r.stdout or "").strip().lower()
        return codec in ("h264", "avc1", "") or bool(re.match(r"\s*$", codec))
    except Exception:
        return True


def _transcode_to_h264(src, progress_cb=None):
    """把（HEVC 等）视频转码为 H.264 供 subocr 解码，返回转码后路径（ASCII 临时目录）。"""
    ffmpeg = _tool("ffmpeg.exe")
    tmp = tempfile.mkdtemp(prefix="hguo_trc_")
    dst = os.path.join(tmp, "transcoded.mp4")
    if progress_cb:
        progress_cb("转码视频为 H.264…")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-an", "-c:v", "libx264", "-preset", "fast",
           "-pix_fmt", "yuv420p", "-crf", "23", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800,
                           creationflags=_NO_WINDOW)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("视频转码失败：%s" % e)
    if r.returncode != 0 or not os.path.exists(dst):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("视频转码失败：%s" % (r.stderr or b"").decode("utf-8", "ignore"))
    return dst


def _enhance_subtitle(src, scale=1.5):
    """预处理字幕区视频以提升 OCR 识别率：整帧放大（scale 滤镜）。
    捆绑 ffmpeg 为极简版（无 crop/unsharp/eq），但内置 scale，
    放大后字幕变大变清晰、锚点与识别更稳。返回增强后路径（ASCII 临时目录）。"""
    ffmpeg = _tool("ffmpeg.exe")
    tmp = tempfile.mkdtemp(prefix="hguo_enh_")
    dst = os.path.join(tmp, "enh.mp4")
    flt = "scale=w=trunc(iw*%g/2)*2:h=-2:flags=lanczos" % scale
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-an", "-vf", flt,
           "-c:v", "libx264", "-preset", "fast", "-crf", "23", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800,
                           creationflags=_NO_WINDOW)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("字幕区放大失败：%s" % e)
    if r.returncode != 0 or not os.path.exists(dst):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("字幕区放大失败：%s" % (r.stderr or b"").decode("utf-8", "ignore"))
    return dst


def _clean_merge(subs):
    """后处理：清洗文本并合并时间相邻、文本相同的重复字幕行，提升可读性。"""
    if not subs:
        return []
    def norm(t):
        t = re.sub(r"\s+|\u3000", " ", t or "").strip().strip("|")
        return t
    merged = []
    for s, e, t in subs:
        t = norm(t)
        if not t:
            continue
        if merged and t == merged[-1][2] and (s - merged[-1][1]) <= 0.35:
            if e > merged[-1][1]:
                merged[-1][1] = e
            continue
        merged.append([s, e, t])
    return [(round(s, 2), round(e, 2), t) for s, e, t in merged]


def _array_of(item_type):
    class _Arr(ctypes.Structure):
        _fields_ = [("size", ctypes.c_size_t), ("data", ctypes.POINTER(item_type))]
    return _Arr


class CString(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulonglong), ("data", ctypes.c_char_p)]


class _PrimArray(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulonglong), ("data", ctypes.c_void_p)]


class SubtitleAnchor(ctypes.Structure):
    _fields_ = [
        ("center_x", ctypes.c_long),
        ("center_y", ctypes.c_long),
        ("height", ctypes.c_long),
        ("lang", ctypes.c_long),
        ("is_primary", ctypes.c_long),
        ("avg_width", ctypes.c_long),
        ("min_width", ctypes.c_long),
        ("mid_width", ctypes.c_long),
        ("max_width", ctypes.c_long),
    ]


class Subtitle(ctypes.Structure):
    _fields_ = [
        ("start_us", ctypes.c_longlong),
        ("end_us", ctypes.c_longlong),
        ("texts", _array_of(CString)),
    ]


AnchorArray = _array_of(SubtitleAnchor)
SubtitleArray = _array_of(Subtitle)


class _Progress(ctypes.Structure):
    _fields_ = [
        ("is_finished", ctypes.c_long),
        ("start_us", ctypes.c_longlong),
        ("current_us", ctypes.c_longlong),
        ("duration_us", ctypes.c_longlong),
        ("speed_up", ctypes.c_double),
    ]


class SubtitleOcrEngine:
    def __init__(self):
        self._lib = None
        self._ctx = None
        self._lock = threading.Lock()

    def _load(self):
        if self._lib is not None:
            return True
        rd = runtime_dir()
        if not rd:
            return False
        os.add_dll_directory(rd)
        lib = ctypes.CDLL(os.path.join(rd, "subocr.dll"))
        lib.subocr_init.restype = ctypes.c_void_p
        lib.subocr_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.subocr_deinit.argtypes = [ctypes.c_void_p]
        lib.subocr_start_predet.restype = ctypes.c_int
        lib.subocr_start_predet.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64, ctypes.c_int]
        lib.subocr_query_anchors.restype = AnchorArray
        lib.subocr_query_anchors.argtypes = [ctypes.c_void_p]
        lib.subocr_start_pipeline.restype = ctypes.c_int
        lib.subocr_start_pipeline.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, AnchorArray, ctypes.c_int64]
        lib.subocr_query_new_subtitles.restype = SubtitleArray
        lib.subocr_query_new_subtitles.argtypes = [ctypes.c_void_p]
        lib.subocr_query_progress.restype = _Progress
        lib.subocr_query_progress.argtypes = [ctypes.c_void_p]
        self._lib = lib
        return True

    def _ensure_ctx(self):
        if self._ctx:
            return self._ctx
        rd = runtime_dir()
        res = os.path.join(rd, "alg-resources")
        keys = os.path.join(rd, "alg-resources", "keys")
        ctx = self._lib.subocr_init(res.encode("utf-8", "ignore"),
                                    keys.encode("utf-8", "ignore"))
        if not ctx:
            raise RuntimeError("OCR 引擎初始化失败")
        self._ctx = ctx
        return ctx

    @staticmethod
    def _cstr(c):
        if not c.data:
            return ""
        if c.size:
            return ctypes.string_at(c.data, c.size).decode("utf-8", "ignore")
        return ""

    def extract(self, video_path, lang="zh", fps=10, min_subtitle_ms=500, progress_cb=None):
        if not self._load():
            raise RuntimeError("未找到 OCR 引擎运行时，请检查 ocr_engine_runtime 目录")
        with self._lock:
            ctx = self._ensure_ctx()
            lang_i = LANG_CODE.get(lang, 0)
            local = video_path
            # subocr 内置解码器不兼容 HEVC 等编码会丢帧/漏识别，先转成 H.264
            if not _is_h264(local):
                local = _transcode_to_h264(local, progress_cb)
            if progress_cb:
                progress_cb("定位字幕锚点…")
            rc = self._lib.subocr_start_predet(ctx, local.encode("utf-8", "ignore"),
                                               36000000000, lang_i)
            if rc != 1:
                raise RuntimeError("字幕预检测失败")
            for _ in range(200):
                prog = self._lib.subocr_query_progress(ctx)
                if prog.is_finished:
                    break
                time.sleep(0.15)
            anchors = self._lib.subocr_query_anchors(ctx)
            n_anchor = max(anchors.size, 0)
            if n_anchor == 0:
                if progress_cb:
                    progress_cb("未检测到字幕区域（可能是无字幕视频）")
                return []
            if progress_cb:
                progress_cb("开始逐帧识别…")
            self._lib.subocr_start_pipeline(ctx, local.encode("utf-8", "ignore"),
                                            fps, anchors, int(min_subtitle_ms * 1000))
            out = []
            t0 = time.time()
            while time.time() - t0 < 3600:
                arr = self._lib.subocr_query_new_subtitles(ctx)
                for i in range(arr.size):
                    s = arr.data[i]
                    segs = []
                    for j in range(s.texts.size):
                        segs.append(self._cstr(s.texts.data[j]))
                    out.append((s.start_us / 1000000.0, s.end_us / 1000000.0, " | ".join(segs)))
                prog = self._lib.subocr_query_progress(ctx)
                if prog.is_finished:
                    break
                if progress_cb:
                    dur = prog.duration_us / 1000000.0
                    cur = (prog.current_us - prog.start_us) / 1000000.0
                    if dur > 0:
                        progress_cb("识别中 %.0f%%" % min(100.0, cur / dur * 100))
                time.sleep(0.3)
            return _clean_merge(out)

    def close(self):
        with self._lock:
            if self._ctx and self._lib:
                try:
                    self._lib.subocr_deinit(self._ctx)
                except Exception:
                    pass
            self._ctx = None
            self._lib = None


_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = SubtitleOcrEngine()
        return _ENGINE


def _download_direct(url, dest, progress_cb=None):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(512 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return dest


def download_to_temp(url, is_m3u8, progress_cb=None):
    """下载到临时目录，返回 (视频路径, 临时目录)；调用方负责用完 rmtree 临时目录。"""
    tmp = tempfile.mkdtemp(prefix="hguo_ocr_")
    dest = os.path.join(tmp, "video.mp4")
    if is_m3u8 and M3U8Downloader is not None:
        dl = M3U8Downloader()
        try:
            # 统一回调签名 (message, current, total, filename)，上层只关心 message
            dl.download(url, dest,
                        progress_callback=(lambda m, c, t, f: progress_cb(m)) if progress_cb else None)
        finally:
            dl.close()
        return dest, tmp
    _download_direct(url, dest, progress_cb)
    return dest, tmp


class OcrWorker(QThread):
    progress = Signal(str)
    done = Signal(list)          # [(start_s, end_s, text), ...]
    failed = Signal(str)

    def __init__(self, url, is_m3u8=False, title="", lang="zh", fps=10, min_subtitle_ms=500, crop_bottom=True, subtitle_scale=1.5, parent=None):
        super().__init__(parent)
        self.url = url
        self.is_m3u8 = is_m3u8
        self.title = title
        self.lang = lang
        self.fps = fps
        self.min_subtitle_ms = min_subtitle_ms
        self.crop_bottom = crop_bottom
        self.subtitle_scale = subtitle_scale
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        local = None
        ocr_tmp_dir = None
        try:
            local = self.url
            is_local = self.url.startswith("file://") or os.path.exists(self.url)
            if not is_local:
                self.progress.emit("下载临时文件…")
                local, ocr_tmp_dir = download_to_temp(self.url, self.is_m3u8,
                                                       lambda s: self.progress.emit(s))
            self.progress.emit("初始化 OCR 引擎…")
            crop_cleanup = None
            eng = get_engine()
            try:
                src = local
                if self.subtitle_scale and self.subtitle_scale > 1 and _tool("ffmpeg.exe"):
                    self.progress.emit("字幕放大 x%s 增强…" % self.subtitle_scale)
                    src = _enhance_subtitle(local, scale=self.subtitle_scale)
                    crop_cleanup = src
                subs = eng.extract(src, lang=self.lang, fps=self.fps,
                                   min_subtitle_ms=self.min_subtitle_ms,
                                   progress_cb=lambda s: (not self._stop) and self.progress.emit(s))
            finally:
                if crop_cleanup:
                    shutil.rmtree(os.path.dirname(crop_cleanup), ignore_errors=True)
                    crop_cleanup = None
            if self._stop:
                return
            self.done.emit(subs if subs else [])
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            # 清理 OCR 临时下载目录（仅当确实下载了远程文件到 %TEMP%），避免堆积
            if ocr_tmp_dir:
                shutil.rmtree(ocr_tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    v = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "ocr_extract", "test.mp4")
    lg = sys.argv[2] if len(sys.argv) > 2 else "en"
    eng = get_engine()
    print("available:", available())
    try:
        subs = eng.extract(v, lang=lg, progress_cb=lambda s: print(s))
        print("SUBTITLES", len(subs))
        for s, e, t in subs:
            print(f"[{s:.2f}-{e:.2f}]", t)
    except Exception as e:
        print("ERR", repr(e))