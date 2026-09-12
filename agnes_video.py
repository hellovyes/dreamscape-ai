# -*- coding: utf-8 -*-
"""Agnes Video 2.5 Flash video generation client."""
import json, os, time, shutil, mimetypes
import urllib.error, urllib.parse, urllib.request
from PySide6.QtCore import QThread, Signal, QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

AGNES_VIDEO_MODEL = "agnes-video-2.5-flash"
AGNES_DEFAULT_BASE = "https://apihub.agnes-ai.com/v1"
ASPECT_RATIOS = ["16:9", "21:9", "4:3", "1:1", "3:4", "9:16"]
VIDEO_SECONDS = [str(i) for i in range(2, 13)]

def _headers(api_key):
    return {"Authorization": "Bearer %s" % (api_key or "").strip(),
            "Content-Type": "application/json"}

def create_video_task(api_key, base_url, prompt, seconds="10", aspect="16:9",
                      negative_prompt="", image_urls=None, model=None):
    base = (base_url or AGNES_DEFAULT_BASE).rstrip("/")
    model = model or AGNES_VIDEO_MODEL
    text = (prompt or "").strip()
    if not text:
        raise ValueError("提示词为空")
    try:
        secs = max(4, min(12, int(seconds)))
    except (ValueError, TypeError):
        secs = 10
    payload = {"model": model, "prompt": text,
               "mode": "reference" if image_urls else "text",
               "seconds": str(secs), "size": "720P",
               "aspect_ratio": aspect}
    if negative_prompt and negative_prompt.strip():
        payload["prompt"] = text + "\n负面提示词（请避免）：" + negative_prompt.strip()
    if image_urls:
        urls = [u.strip() for u in image_urls if u and u.strip()]
        if urls:
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


def image_to_data_uri(path, max_edge=1024, quality=85):
    """把本地图片压缩并编码为 base64 data URI（无需公网上传）。

    长边缩到 max_edge、JPEG 质量 quality，控制 JSON 体积；失败则抛错。
    """
    if not os.path.exists(path):
        raise RuntimeError("本地图片不存在：%s" % path)
    img = QImage(path)
    if img.isNull():
        raise RuntimeError("无法读取图片（格式不支持或文件损坏）：%s" % path)
    w, h = img.width(), img.height()
    if max(w, h) > max_edge:
        if w >= h:
            img = img.scaledToWidth(max_edge, Qt.SmoothTransformation)
        else:
            img = img.scaledToHeight(max_edge, Qt.SmoothTransformation)
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

    # 1) tmpfiles.org —— 返回 JSON，需要 /dl/ 才是直链
    try:
        text = _multipart_upload("https://tmpfiles.org/api/v1/upload", "file", path)
        data = json.loads(text)
        url = (data.get("data") or {}).get("url") or ""
        if url:
            if "/dl/" not in url:
                url = url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/", 1)
            return url
    except Exception:
        pass

    # 2) catbox.moe —— 返回纯文本 URL
    try:
        text = _multipart_upload("https://catbox.moe/user/api.php", "fileToUpload", path,
                                 extra={"reqtype": "fileupload"}).strip()
        if text.startswith("http"):
            return text
    except Exception:
        pass

    # 3) 0x0.st —— 返回纯文本 URL
    try:
        text = _multipart_upload("https://0x0.st", "file", path).strip()
        if text.startswith("http"):
            return text
    except Exception:
        pass

    raise RuntimeError("本地图片上传到公网图床失败，请检查网络，或改用纯文本模式生成")

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
                # 首选：本地图压缩成 base64 data URI 内联传参，无需公网
                data_uris = [image_to_data_uri(p) for p in local]
                try:
                    res = create_video_task(api_key, base_url, prompt, seconds, aspect,
                                            neg, data_uris + http_urls or None, model)
                    self.done.emit(res)
                    return
                except Exception:
                    # base64 未被接口接受，回退：上传公网图床再重试
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
