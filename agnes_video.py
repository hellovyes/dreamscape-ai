# -*- coding: utf-8 -*-
"""Agnes Video 2.5 Flash video generation client."""
import json, os, time, shutil, mimetypes
import urllib.error, urllib.parse, urllib.request
from PySide6.QtCore import QThread, Signal, QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter

AGNES_VIDEO_MODEL = "agnes-video-2.5-flash"
AGNES_DEFAULT_BASE = "https://apihub.agnes-ai.com/v1"
ASPECT_RATIOS = ["16:9", "21:9", "4:3", "1:1", "3:4", "9:16"]
VIDEO_SECONDS = [str(i) for i in range(2, 13)]

def _headers(api_key):
    return {"Authorization": "Bearer %s" % (api_key or "").strip(),
            "Content-Type": "application/json"}

def create_video_task(api_key, base_url, prompt, seconds="10", aspect="16:9",
                      negative_prompt="", image_urls=None, model=None, mode=None):
    """mode: "reference"（多图参考，images 数组）/"keyframe"（首尾帧，官方验证路径）。

    参考官方 agnes-ai-studio：keyframe 用 first_frame/last_frame（各1张），
    本地图以 base64 data URI 内联直传，不依赖第三方图床。
    """
    base = (base_url or AGNES_DEFAULT_BASE).rstrip("/")
    model = model or AGNES_VIDEO_MODEL
    text = (prompt or "").strip()
    if not text:
        raise ValueError("提示词为空")
    try:
        secs = max(4, min(12, int(seconds)))
    except (ValueError, TypeError):
        secs = 10
    urls = [u.strip() for u in (image_urls or []) if u and u.strip()]
    if mode == "keyframe" and urls:
        gen_mode = "keyframe"
    else:
        gen_mode = "reference" if urls else "text"
    payload = {"model": model, "prompt": text,
               "mode": gen_mode,
               "seconds": str(secs), "size": "720P",
               "aspect_ratio": aspect}
    if negative_prompt and negative_prompt.strip():
        payload["prompt"] = text + "\n负面提示词（请避免）：" + negative_prompt.strip()
    if urls:
        if gen_mode == "keyframe":
            # 官方 keyframe 模式：首帧 + 尾帧（最多 2 张），本地 data URI 可直接解析
            payload["first_frame"] = urls[0]
            if len(urls) >= 2:
                payload["last_frame"] = urls[-1]
        else:
            payload["images"] = urls
    data = json.dumps(payload).encode("utf-8")

    # HTTP 429 限流自动退避重试（瞬时限流可自动恢复，避免创建即失败）
    max_tries = 4
    err_detail = None
    for attempt in range(1, max_tries + 1):
        req = urllib.request.Request(base + "/videos", data=data,
                                     headers=_headers(api_key), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            detail = body[:400]
            try:
                j = json.loads(body)
                em = j.get("error") or j.get("message") or j
                if isinstance(em, dict):
                    detail = em.get("message") or json.dumps(em, ensure_ascii=False)
                else:
                    detail = str(em)
            except Exception:
                pass
            if e.code == 429 and attempt < max_tries:
                # 优先用服务端 Retry-After，否则指数退避
                try:
                    wait = float(e.headers.get("Retry-After", "0") or 0)
                except (AttributeError, ValueError, TypeError):
                    wait = 0
                if wait <= 0:
                    wait = min(2 * attempt + attempt, 12)
                time.sleep(wait)
                continue
            raise RuntimeError("HTTP %s（请求被拒绝）：%s" % (e.code, detail)) from e
        except urllib.error.URLError as e:
            err_detail = "网络错误：%s" % (e.reason,)
            if attempt < max_tries:
                time.sleep(min(2 * attempt, 8))
                continue
            raise RuntimeError(err_detail) from e
    else:
        raise RuntimeError("创建任务失败：%s" % (err_detail or "未知错误"))
    video_id = parsed.get("video_id") or parsed.get("id") or ""
    task_id = parsed.get("task_id") or video_id or ""
    if not video_id:
        raise RuntimeError("创建任务响应缺少 video_id：%s"
                           % json.dumps(parsed, ensure_ascii=False)[:300])
    return {"task_id": task_id, "video_id": video_id, "raw": parsed}


def _multipart_upload(url, field_name, file_path, extra=None, timeout=120):
    """以 multipart/form-data 上传单个文件，返回响应文本。"""
    boundary = "----TraeBoundary" + os.urandom(8).hex()
    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    parts = []
    if extra:
        for k, v in extra.items():
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, k, v)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n" % (boundary, field_name, filename, ctype)).encode("utf-8"))
    parts.append(file_bytes)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def image_to_data_uri(path, max_edge=1024, quality=85, min_edge=384):
    """把本地图片压缩并编码为 base64 data URI（无需公网上传）。

    长边缩到 max_edge、JPEG 质量 quality，控制 JSON 体积；失败则抛错。
    短边保护：若按长边缩放后短边 < min_edge（极端宽高比图会被 Agnes 服务端
    判为无效媒体，报「media URL could not be downloaded」），改以短边为准缩放。
    """
    if not os.path.exists(path):
        raise RuntimeError("本地图片不存在：%s" % path)
    img = QImage(path)
    if img.isNull():
        raise RuntimeError("无法读取图片（格式不支持或文件损坏）：%s" % path)
    w, h = img.width(), img.height()
    scale = 1.0
    if max(w, h) > max_edge:
        scale = min(scale, max_edge / float(max(w, h)))
    if min(w, h) * scale < min_edge:
        scale = max(scale, min_edge / float(min(w, h)))
    if scale != 1.0:
        img = img.scaled(max(1, int(round(w * scale))), max(1, int(round(h * scale))),
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    # 关键：JPEG 不支持透明通道。带 alpha 的 PNG（白模/抠图素材）若直接转 JPEG，
    # Qt 会把透明区填成纯黑，Agnes 服务端会把"黑底图"判为无效媒体而返回
    # 「media URL could not be downloaded or did not return valid supported media」。
    # 这里先把 ARGB 合成到纯白背景，透明部分变成白底，转 JPEG 后始终是有效图片。
    if img.hasAlphaChannel():
        flat = QImage(img.size(), QImage.Format_RGB32)
        flat.fill(Qt.white)
        p = QPainter(flat)
        p.drawImage(0, 0, img)
        p.end()
        img = flat
    ba = QByteArray()
    buf = QBuffer(ba)
    if not buf.open(QIODevice.WriteOnly):
        raise RuntimeError("无法压缩图片：%s" % path)
    if not img.save(buf, "JPEG", quality):
        buf.close()
        raise RuntimeError("图片压缩失败：%s" % path)
    buf.close()
    if ba.size() == 0:
        raise RuntimeError("图片压缩失败：%s" % path)
    b64 = bytes(ba.toBase64()).decode("ascii")
    return "data:image/jpeg;base64," + b64


def upload_image_to_public(path):
    """把本地图片上传到匿名公网图床，返回可公网访问的 URL。

    Agnes 的 reference 模式只接受公网可访问的图片 URL，本地路径会被拒绝（HTTP 400）。
    依次尝试 tmpfiles.org / catbox.moe / 0x0.st，全部失败则抛错。
    """
    if not os.path.exists(path):
        raise RuntimeError("本地图片不存在：%s" % path)

    # 与 image_to_data_uri 一致：透明 PNG 先合成白底，并压缩到 max_edge，
    # 保证图床 URL 返回的是服务端能稳定下载的有效 JPEG（避免黑底/超大图被 400）。
    try:
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtCore import Qt
        img = QImage(path)
        if not img.isNull():
            w, h = img.width(), img.height()
            if max(w, h) > 1024:
                img = (img.scaledToWidth(1024, Qt.SmoothTransformation)
                       if w >= h else img.scaledToHeight(1024, Qt.SmoothTransformation))
            if img.hasAlphaChannel():
                flat = QImage(img.size(), QImage.Format_RGB32)
                flat.fill(Qt.white)
                p = QPainter(flat)
                p.drawImage(0, 0, img)
                p.end()
                img = flat
            upload_path = path + ".pub.jpg"
            img.save(upload_path, "JPEG", 85)
            path = upload_path
    except Exception:
        pass

    # 1) tmpfiles.org —— 返回 JSON，需要 /dl/ 才是直链
    try:
        text = _multipart_upload("https://tmpfiles.org/api/v1/upload", "file", path)
        data = json.loads(text)
        url = (data.get("data") or {}).get("url") or ""
        if url:
            if "/dl/" not in url:
                url = url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/", 1)
            _cleanup_public_tmp(path)
            return url
    except Exception:
        pass

    # 2) catbox.moe —— 返回纯文本 URL
    try:
        text = _multipart_upload("https://catbox.moe/user/api.php", "fileToUpload", path,
                                 extra={"reqtype": "fileupload"}).strip()
        if text.startswith("http"):
            _cleanup_public_tmp(path)
            return text
    except Exception:
        pass

    # 3) 0x0.st —— 返回纯文本 URL
    try:
        text = _multipart_upload("https://0x0.st", "file", path).strip()
        if text.startswith("http"):
            _cleanup_public_tmp(path)
            return text
    except Exception:
        pass

    raise RuntimeError("本地图片上传到公网图床失败，请检查网络，或改用纯文本模式生成")


def _cleanup_public_tmp(path):
    """删除 upload_image_to_public 生成的白底压缩临时文件（path.pub.jpg）。"""
    try:
        tmp = path + ".pub.jpg"
        if tmp != path and os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass

_DONE_STATUS = {"completed", "succeed", "succeeded", "success", "done", "finished"}
_FAIL_STATUS = {"failed", "failure", "error", "cancelled", "canceled"}


def _deep_first_url(obj, depth=0):
    """递归查找响应中第一个 http(s) 视频地址，兼容任意嵌套结构。"""
    if depth > 12:
        return ""
    if isinstance(obj, str):
        return obj if obj.strip().lower().startswith(("http://", "https://")) else ""
    if isinstance(obj, dict):
        for v in obj.values():
            r = _deep_first_url(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            r = _deep_first_url(v, depth + 1)
            if r:
                return r
    return ""


def query_video_status(api_key, base_url, video_id, model_name=None):
    base = (base_url or AGNES_DEFAULT_BASE).rstrip("/")
    model_name = model_name or AGNES_VIDEO_MODEL
    # /agnesapi 在域名根路径，不带 /v1 前缀（官方参考实现）
    if base.endswith("/v1"):
        base = base[:-3]
    qs = urllib.parse.urlencode({"video_id": video_id, "model_name": model_name})
    req = urllib.request.Request(base + "/agnesapi?" + qs, headers=_headers(api_key))
    with urllib.request.urlopen(req, timeout=60) as resp:
        parsed = json.loads(resp.read().decode("utf-8"))
    raw_status = (parsed.get("status") or "").strip().lower()

    metadata = parsed.get("metadata") or {}
    video_url = (parsed.get("videoUrl") or parsed.get("video_url")
                 or metadata.get("url") or metadata.get("video_url")
                 or metadata.get("file")
                 or _deep_first_url(parsed))

    error = None
    if raw_status in _FAIL_STATUS:
        em = parsed.get("error") or {}
        error = (em.get("message") if isinstance(em, dict)
                 else (em if isinstance(em, str) else None))
        error = (error or str(em)) if em else "生成失败"
        return {"status": "failed", "progress": parsed.get("progress"),
                "video_url": video_url, "error": error, "raw": parsed}

    if raw_status in _DONE_STATUS or video_url:
        # 视为完成：状态完结，或已拿到可用视频地址
        return {"status": "completed", "progress": parsed.get("progress") or 100,
                "video_url": video_url, "error": error, "raw": parsed}

    return {"status": "processing", "progress": parsed.get("progress"),
            "video_url": video_url, "error": error, "raw": parsed}

def download_video(url, dest_path, timeout=600):
    dest_path = os.path.abspath(dest_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return dest_path


def save_video_local(url, dest_dir, prefix="生成视频"):
    """把生成的视频（URL 或本地路径）下载/复制保存到本地目录，返回完整本地路径。
    dest_dir 为空时返回原 URL；失败抛错。文件命名带时间戳，同一剧集多批次不冲突。"""
    dest_dir = (dest_dir or "").strip()
    if not dest_dir:
        return url
    if not url:
        raise RuntimeError("没有可保存的视频地址")
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    if os.path.exists(url):
        # 本地路径：复制
        ext = os.path.splitext(url)[1] or ".mp4"
        name = "%s_%s%s" % (prefix, time.strftime("%Y%m%d_%H%M%S"), ext)
        dst = os.path.join(dest_dir, name)
        shutil.copy2(url, dst)
        return dst
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(dest_dir, "%s_%s.mp4" % (prefix, ts))
    download_video(url, dst)
    return dst

class CreateTaskWorker(QThread):
    done = Signal(dict)
    failed = Signal(str)
    def __init__(self, api_key, base_url, prompt, seconds="10", aspect="16:9",
                 negative_prompt="", image_urls=None, model=None, parent=None):
        super().__init__(parent)
        self._args = (api_key, base_url, prompt, seconds, aspect, negative_prompt, image_urls, model)
    def run(self):
        try:
            api_key, base_url, prompt, seconds, aspect, neg, imgs, model = self._args
            if not imgs:
                self.done.emit(create_video_task(api_key, base_url, prompt, seconds,
                                                 aspect, neg, None, model))
                return
            local = [p for p in imgs if os.path.exists(p)]
            http_urls = [str(p).strip() for p in imgs
                         if not os.path.exists(p) and str(p).strip().startswith("http")]

            if local:
                # 首选：本地图压缩成 base64 data URI 内联传参（reference 多图模式，无需公网）
                data_uris = [image_to_data_uri(p) for p in local]
                try:
                    res = create_video_task(api_key, base_url, prompt, seconds, aspect,
                                            neg, data_uris + http_urls or None, model)
                    self.done.emit(res)
                    return
                except RuntimeError as e:
                    # reference 的 images 要求公网可下载 URL，data URI 会被服务端判为
                    # 「media URL could not be downloaded」而 400。此时自动降级为官方
                    # agnes-ai-studio 验证过的 keyframe 首尾帧模式（first/last_frame
                    # 可直接解析 data URI，最多取首帧+尾帧 2 张），彻底绕开第三方图床。
                    emsg = str(e)
                    if "media URL" in emsg or "could not be downloaded" in emsg \
                            or "valid supported media" in emsg:
                        # keyframe 模式单首帧（1张）或首尾帧（≥2张）均为官方合法用法
                        print("[CreateTaskWorker] reference+data URI 被拒，自动降级 keyframe 首尾帧模式"
                              "（第1张 → first_frame%s）"
                              % ("，最后1张 → last_frame" if len(data_uris) >= 2 else ""))
                        res = create_video_task(api_key, base_url, prompt, seconds, aspect,
                                                neg, data_uris + http_urls or None, model,
                                                mode="keyframe")
                        self.done.emit(res)
                        return
                    # 非媒体类错误或仅 1 张图：走公网图床回退（最后手段）
                    public = [upload_image_to_public(p) for p in local]
                    res = create_video_task(api_key, base_url, prompt, seconds, aspect,
                                            neg, public + http_urls or None, model)
                    self.done.emit(res)
                    return

            res = create_video_task(api_key, base_url, prompt, seconds, aspect, neg,
                                    http_urls or None, model)
            self.done.emit(res)
        except Exception as e:
            self.failed.emit(str(e) or "创建任务失败")

class PollWorker(QThread):
    progress = Signal(dict)
    finished_ok = Signal(dict)
    failed = Signal(str)
    def __init__(self, api_key, base_url, video_id, interval=2.0, model=None, parent=None, started_at=None, timeout_sec=3600):
        super().__init__(parent)
        self._args = (api_key, base_url, video_id, max(interval, 1.0), model)
        self._started_at = started_at
        self._stop = False
        self._timeout_sec = timeout_sec  # 轮询总超时，防止服务端卡死导致僵尸线程
    def stop(self):
        self._stop = True
    def run(self):
        api_key, base_url, video_id, interval, model = self._args
        errors = 0
        deadline = None
        if self._timeout_sec and self._timeout_sec > 0:
            deadline = time.time() + self._timeout_sec
        while not self._stop:
            if deadline is not None and time.time() > deadline:
                self.failed.emit("生成轮询超时（%d 分钟未完成），已自动停止" % int(self._timeout_sec // 60))
                return
            try:
                st = query_video_status(api_key, base_url, video_id, model)
            except Exception as e:
                msg = str(e) or ""
                if "429" in msg:
                    time.sleep(max(interval, 10))
                    continue
                errors += 1
                if errors >= 20:
                    self.failed.emit("轮询失败多次：%s" % (e or "网络错误"))
                    return
                time.sleep(interval)
                continue
            errors = 0
            elapsed = self._compute_elapsed()
            if st["status"] == "completed":
                st["elapsed"] = elapsed
                self.finished_ok.emit(st)
                return
            if st["status"] == "failed":
                st["elapsed"] = elapsed
                self.failed.emit(st.get("error") or "生成失败")
                return
            st["elapsed"] = elapsed
            self.progress.emit(st)
            time.sleep(interval)

    def _compute_elapsed(self):
        if not self._started_at:
            return None
        try:
            t0 = time.mktime(time.strptime(self._started_at, "%Y-%m-%d %H:%M:%S"))
            t1 = time.time()
            s = int(t1 - t0)
            if s < 60:
                return "%ds" % s
            m = s // 60
            return "%dm%02ds" % (m, s % 60)
        except Exception:
            return None
