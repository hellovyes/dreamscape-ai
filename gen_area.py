# -*- coding: utf-8 -*-
"""视频生成区组件：每个实例即为一个独立的「剧集」生成面板，
可同时存在多个（一个窗口容纳多个生成区），各自独立生成/预览/保存。"""
import json
import os
import re
import tempfile
import time

from PySide6.QtCore import Qt, Signal, QSize, QUrl, QThread, QEvent, QPoint, QTimer, QRect
from PySide6.QtGui import (QPixmap, QTextCharFormat, QFont, QColor, QTextCursor,
                           QImage, QPainter, QBrush, QPen,
                           QGuiApplication, QIcon)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QLabel, QComboBox, QLineEdit, QPlainTextEdit,
                               QPushButton, QListWidget, QListWidgetItem,
                               QListView, QAbstractItemView, QToolButton,
                               QFileDialog, QMessageBox, QFrame,
                               QGridLayout, QDialog, QMenu, QButtonGroup)

import config
from agnes_video import CreateTaskWorker, PollWorker, download_video, save_video_local, ASPECT_RATIOS


# 参考图原始保留上限：允许填充/手动添加超过 5 张，发送时按「≤5 张」自动合并为 单图 + 拼接图。
# 5 组 × 最高 3 合一 = 15 张，保证组数不超过 5 且能尽量保留全部原图。
_MAX_REF = 15


def _new_merge_dir():
    """生成一次发送用的独立临时目录（每次生成/发送都新建，避免长期堆积在 %TEMP%）。
    用完由调用方 rmtree。"""
    import shutil
    try:
        return tempfile.mkdtemp(prefix="dreamscape_ref_merge_")
    except Exception:
        return None


def _rmtree_merge_dir(d):
    """清理一次发送的临时目录（拼接/标注图）。失败静默。"""
    if not d:
        return
    try:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def create_image_task(api_key, base_url, prompt, model=None, size="1024x1024",
                      out_dir=None, parent=None, name_hint=None):
    """OpenAI 兼容 /images/generations 生图并保存到本地。

    返回 {"success": bool, "image_path": str|None, "error": str|None}
    name_hint：可选的资产名，保存文件名会带上便于日后按名找回
    （形如：asset_陆知归_<毫秒时间戳>.png）。
    """
    import base64
    import time as _t
    import urllib.request
    import urllib.error
    url = (base_url or "").strip() or config.IMAGE_GEN_DEFAULT_BASE
    mdl = (model or "").strip() or config.IMAGE_GEN.get("model", "cogview-3-flash")
    sz = (size or "").strip() or "1024x1024"
    body = json.dumps({"model": mdl, "prompt": prompt, "size": sz, "n": 1}).encode("utf-8")

    def _err_detail(ebody, code):
        try:
            j = json.loads(ebody or "")
            em = j.get("error") or j.get("message") or j
            if isinstance(em, dict):
                return str(em.get("message") or json.dumps(em, ensure_ascii=False))[:400]
            return str(em)[:400]
        except Exception:
            return (ebody or "")[:400] or "（服务端未返回详情）"

    # HTTP 429/5xx 属暂时性错误：自动退避重试（服务过载可自动恢复）
    max_tries = 4
    last_hint = ""
    data = None
    for attempt in range(1, max_tries + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer %s" % (api_key or "").strip())
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            try:
                ebody = e.read().decode("utf-8", "replace")
            except Exception:
                ebody = ""
            detail = _err_detail(ebody, e.code)
            if e.code in (429, 500, 502, 503, 504) and attempt < max_tries:
                try:
                    wait = float(e.headers.get("Retry-After", "0") or 0)
                except (AttributeError, ValueError, TypeError):
                    wait = 0
                if wait <= 0:
                    wait = min(3 * attempt, 15)
                _t.sleep(wait)
                last_hint = "HTTP %s：%s" % (e.code, detail)
                continue
            hint = ""
            if e.code == 503:
                hint = "（服务端暂时不可用：可能过载维护，或模型名不被服务商支持，请核对模型是否为官方合法名）"
            elif e.code in (401, 403):
                hint = "（API Key 无效或无权访问该模型）"
            elif e.code == 404:
                hint = "（接口地址或模型不存在，请检查 base_url / model）"
            return {"success": False, "image_path": None,
                    "error": "HTTP %s %s%s" % (e.code, detail, hint)}
        except urllib.error.URLError as e:
            last_hint = "网络错误：%s" % (e.reason,)
            if attempt < max_tries:
                _t.sleep(min(2 * attempt, 8))
                continue
            return {"success": False, "image_path": None, "error": last_hint}
    if data is None:
        return {"success": False, "image_path": None, "error": last_hint or "请求失败"}
    items = data.get("data") or []
    if not items:
        return {"success": False, "image_path": None,
                "error": "响应缺少 data: %s" % str(data)[:200]}
    first = items[0]
    img_data = None
    try:
        if first.get("b64_json"):
            img_data = base64.b64decode(first["b64_json"])
            ext = ".png"
        elif first.get("url"):
            ireq = urllib.request.Request(first["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(ireq, timeout=180) as r:
                img_data = r.read()
            ext = os.path.splitext(urllib.request.urlparse(first["url"]).path)[1] or ".png"
            if len(ext) > 5:
                ext = ".png"
    except Exception as e:
        return {"success": False, "image_path": None, "error": "下载图片失败：%s" % e}
    if not img_data:
        return {"success": False, "image_path": None, "error": "响应无可下载图片"}
    out = out_dir or os.path.join(config._EXE_DIR, "资产仓库", "images")
    os.makedirs(out, exist_ok=True)
    # 文件名带资产名（清洗路径非法字符），便于关联丢失后按名恢复
    safe = ""
    if name_hint:
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name_hint).strip())
        safe = safe.strip("._")[:40]
    name = ("asset_%s_%d%s" % (safe, int(time.time() * 1000), ext)
            if safe else "asset_%d%s" % (int(time.time() * 1000), ext))
    path = os.path.join(out, name)
    with open(path, "wb") as f:
        f.write(img_data)
    return {"success": True, "image_path": path, "error": None}


# ---------- 异步保存下载线程 ----------
class _SaveVideoThread(QThread):
    """后台线程：下载视频到本地，不阻塞 UI。"""
    done = Signal(str)   # path
    fail = Signal(str)   # error

    def __init__(self, url, dest_dir, prefix, parent=None):
        super().__init__(parent)
        self._url = url
        self._dest_dir = dest_dir
        self._prefix = prefix

    def run(self):
        try:
            import os as _os
            if self._dest_dir and not _os.path.exists(self._dest_dir):
                _os.makedirs(self._dest_dir, exist_ok=True)
            path = save_video_local(self._url, self._dest_dir, self._prefix)
            self.done.emit(path)
        except Exception as e:
            self.fail.emit(str(e))


class _RefPrepWorker(QThread):
    """后台线程：把参考图整理为带标注/拼接的发送图，避免点生成时 UI 卡顿。
    在子线程里做 QImage 读图 + 缩放 + QPainter + 写 PNG。完成后回到 UI 线程发任务。"""
    done = Signal(list, str)     # (merged_paths, tmp_dir)
    fail = Signal(str)

    def __init__(self, widget, paths, parent=None):
        super().__init__(parent)
        self._w = widget
        self._paths = paths or []

    def run(self):
        try:
            # 关键：_refs_for_send 只做离屏 QImage/QPainter 操作，不触碰 QWidget，可在子线程安全运行
            merged, tmp_dir = self._w._refs_for_send(self._paths)
            self.done.emit(merged, tmp_dir or "")
        except Exception as e:
            # 整理失败也要把临时目录带回主线程清理，避免残留
            try:
                self._w._cleanup_ref_tmp_dir_safe(getattr(self._w, "_ref_tmp_dir", None))
            except Exception:
                pass
            self.fail.emit(str(e) or "参考图整理失败")


def _read_api_key():
    return (config.AGNES_VIDEO.get("api_key") or "")


def extract_core_prompt(text):
    """从一段分析文本中提取『核心提示词：』所在行的内容；没有该行则整段返回。"""
    src = (text or "").strip()
    if not src:
        return ""
    for line in src.splitlines():
        if "核心提示词" in line:
            idx = line.find("：")
            if idx < 0:
                idx = line.find(":")
            if idx >= 0:
                core = line[idx + 1:].strip()
                if core.startswith("】"):   # 【核心提示词：】xxx 形式，去掉前导闭合括号
                    core = core[1:].strip()
                return core
    return src


# ---------------- 视频分析报告解析 + 一键分镜（按「四、分镜时间线」） ----------------

_RE_SECTION_HEAD = re.compile(r'^\s*(?:[一二三四五六七八九十]+|[0-9]+|【|#)')
_RE_TIME = re.compile(
    r'(?P<h1>\d{1,2})[:：](?P<m1>\d{1,2})(?::(?P<s1>\d{1,2}))?\s*[-~—–至到]\s*'
    r'(?P<h2>\d{1,2})[:：](?P<m2>\d{1,2})(?::(?P<s2>\d{1,2}))?')

_SECTION_KEYWORDS = {
    "overview": ["整体概述", "整体分析"],
    "characters": ["人物表"],
    "dialogue": ["完整台词对白", "台词对白", "台词"],
    "timeline": ["分镜时间线", "时间线"],
    "repro": ["复现提示词", "AI视频复现", "复现提示"],
}


def _time_to_sec(h, m, s):
    h, m = int(h), int(m or 0)
    if s is not None:
        # 三部分 时:分:秒
        return h * 3600 + m * 60 + int(s)
    # 两部分 分:秒（如 0:09 = 0分9秒 = 9秒；1:20 = 80秒）
    return h * 60 + m


def _line_starts_shot(line):
    if _RE_TIME.search(line):
        return True
    if re.match(r'^\s*(?:第\s*[0-9一二三四五六七八九十]+\s*(?:段|镜|个)|镜\s*\d+|[\-·•]|\d+\s*[.、])', line):
        return True
    return False


def parse_storyboard_report(text):
    """把视频分析报告按五个方面切分，并从「分镜时间线」里解析出每一镜 [(开始秒,结束秒,该镜文本),...]。"""
    text = text or ""
    secs = {"overview": "", "characters": "", "dialogue": "", "timeline": "", "repro": ""}
    order = ["overview", "characters", "dialogue", "timeline", "repro"]
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        kind = None
        if _RE_SECTION_HEAD.match(s):
            for k in order:
                if any(kw in s for kw in _SECTION_KEYWORDS.get(k, [])):
                    kind = k
                    break
        if kind:
            cur = kind
            # 标题行常自带内容：如「一、【视频整体概述】女主与男主…」
            # 取首个「】」之后作为该节内容；无括号形式则取命中关键字之后
            seg_end = s.find("】")
            if seg_end < 0:
                for kw in _SECTION_KEYWORDS.get(kind, []):
                    p = s.find(kw)
                    if p >= 0:
                        seg_end = p + len(kw)
                        break
            if seg_end >= 0:
                tail = s[seg_end + 1:].strip().lstrip(":：-—•")
                if tail:
                    secs[cur] += tail + "\n"
        elif cur:
            secs[cur] += s + "\n"
    out = {k: v.strip() for k, v in secs.items()}
    out["shots"] = []
    for block in _split_timeline_blocks(out["timeline"]):
        m = _RE_TIME.search(block)
        if not m:
            continue
        start = _time_to_sec(m.group("h1"), m.group("m1"), m.group("s1"))
        end = _time_to_sec(m.group("h2"), m.group("m2"), m.group("s2"))
        if end > start:
            out["shots"].append((start, end, block.strip()))
    return out


def _split_timeline_blocks(timeline_text):
    lines = [l.strip() for l in (timeline_text or "").splitlines() if l.strip()]
    blocks, cur = [], []
    for line in lines:
        if _line_starts_shot(line) and cur:
            blocks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _dialogue_in_range(dialogue, start, end):
    if not dialogue:
        return ""
    picked = []
    for line in (l.strip() for l in dialogue.splitlines() if l.strip()):
        m = _RE_TIME.search(line)
        if not m:
            continue
        s = _time_to_sec(m.group("h1"), m.group("m1"), m.group("s1"))
        e = _time_to_sec(m.group("h2"), m.group("m2"), m.group("s2"))
        if e <= s:
            picked.append(line)
        elif s < end and e > start:
            picked.append(line)
    return "\n".join(picked) if picked else ""


def _fmt_seconds(sec):
    sec = max(0, int(round(float(sec))))
    return "%d秒" % sec


def build_shot_prompt(report, idx, shot):
    """根据视频分析报告的五方面，为某一镜组装包含五方面、且贴合该镜时间线的生成提示词。"""
    start, end, desc = shot
    dur = _fmt_seconds(end - start)
    dlg = _dialogue_in_range(report.get("dialogue"), start, end)
    if not dlg:
        dlg = report.get("dialogue") or "（本镜时间范围内未识别到明确台词，请依据画面与剧情展开对白）"
        dlg = "（以下为台词对白全段，仅保留落在本镜时间内的台词）\n" + dlg
    p = [
        "# 分镜 %d 生成提示词（含视频分析五个方面）" % (idx + 1),
        "【一、本镜时间线与画面】第 %d 镜 · 时间范围 %s~%s（时长约 %s）" % (idx + 1, start, end, dur),
        "本镜关键画面/剧情节点：%s" % desc,
        "【二、视频整体概述（叙事/主题/节奏基准）】%s" % (report.get("overview") or "（详见整体分析）"),
        "【三、人物表（本镜相关登场人物/外貌/动作/道具依据）】%s" % (report.get("characters") or "（详见人物分析）"),
        "【四、本镜台词对白】%s" % dlg,
        "【五、镜头/美术/风格基准（整体AI视频复现提示词）】%s" % (report.get("repro") or "（详见复现提示词）"),
        "请以这五个方面为依据，针对本镜的时间范围与画面，生成一段具体、可直接用于AI视频生成的完整提示词：",
        "包含本镜的主体人物（外貌/服装/动作）、场景环境与道具、画面构图与运镜、光影、美术风格、氛围情绪，",
        "以及本镜台词对白对应的口型、表情与说话动作；本镜与前后镜之间保持人物、场景、道具与美术风格的一致性。",
    ]
    return "\n".join(p)


# ---------------- 从用户粘贴的中文分镜计划里抽取对白时间轴 ----------------

# 镜头头：中文字符「镜头」或「Shot」开头，后跟数字 + 可选分隔符（含中文括号） + 时长（数字 . 数字 s）
_RE_SHOT = re.compile(r'(?:镜头|Shot)\s*(\d+)\s*[，,、｜（\s]*?(\d+(?:\.\d+)?)\s*s', re.IGNORECASE)

# 独立说话人 + 引号对白（排除已归类为"台词"行的场景，因为台词行走另一分支）
_RE_QUOTE_STANDALONE = re.compile(r'^\s*([^："":]{0,36}?)\s*[：:]\s*[“"]([^""]{1,320})[″"]')

# 被视作"无对白"的文案
_TALK_EMPTY = {"", "无", "没有", "无。", "没有。", "—", "-", "N/A", "none", "（无台词）"}


def _clean_speaker(raw):
    """去掉括号中的说明（如「（凌厉）」、「【返乡装】」）、逗号后的附加标签，只留称谓核心。"""
    raw = (raw or "").strip().strip("，,、。 ")
    raw = re.sub(r'【[^】]*】', '', raw)
    raw = re.sub(r'（[^）]*）|\([^)]*\)', '', raw)
    # 去掉「称谓，附加说明」结构里的逗号之后所有内容（如"沈见微，逐项汇报"→"沈见微"）
    raw = re.split(r'[，,]', raw, maxsplit=1)[0]
    return raw.strip().strip("，,、。 ")


def _extract_dialogue(text):
    """
    从中文分镜计划文本中抽取对白，并映射到各镜头。
    返回 [{line, shot, start, speaker, speaker_clean, sid, text}]。
    - shot：第几镜（按 本视频编号/---/## 等块内重新计数）
    - start：本镜在块内的累计起点秒
    - sid：说话人稳定编号（按首次发声顺序分配，同名复用）
    - speaker_clean：剥离括号后的称谓核心（用于 Subject 绑定）
    """
    lines = (text or "").splitlines()

    # 块边界正则：从分段设置的段头预设中，只提取"视频编号/集编号"相关条目
    # （块边界只认「视频编号」类标题行，不认镜头/分镜/Scene 等内部段落头）
    # 若预设中包含纯正则（含反斜杠等特征字符），也作为块边界候选
    _seg_presets = list(getattr(config, "SEGMENT_HEADER_PRESETS", []) or [])
    _block_pats = []   # 块边界用的正则片段列表
    for _raw in _seg_presets:
        _raw = (_raw or "").strip()
        if not _raw:
            continue
        # 纯正则 → 整条使用
        if re.search(r"[\\\[\]().*+?{}|^$]", _raw):
            if _raw not in _block_pats:
                _block_pats.append(_raw)
            continue
        # 拆词：只保留含"视频编号"或"集编号"的词作为块边界
        for _p in re.split(r"[\s,，、;；/]+", _raw):
            if not _p:
                continue
            _lp = _p.lower()
            if ("视频编号" in _lp) or ("集编号" in _lp):
                _reg = r"视频编号\s*\d+"
                if _reg not in _block_pats:
                    _block_pats.append(_reg)
                break  # 一条预设里只要命中一次就不继续拆词

    if _block_pats:
        _blk_pat = "|".join(_block_pats)
        # 负向先行排除 ←承上视频编号XX / →衔接至视频编号XX 注释行（含箭头标记）
        block_boundary = re.compile(r'^(?!.*[←→].*' + _blk_pat + r').*(?:' + _blk_pat + r')')
    else:
        block_boundary = re.compile(r'^(?!.*[←→].*视频编号).*(视频编号)')

    # 第一步：按块分组，每块记录：(块开始行号, 本块的 shots 列表, 本块的对白列表)
    blocks = []  # [{"line": start_line, "shots": [...], "out": [...]}]
    cur_block = None
    shots, acc, block_count = [], 0.0, 0

    for ln, line in enumerate(lines):
        if block_boundary.search(line):
            # 若之前已有块，先保存
            if cur_block is not None:
                blocks.append(cur_block)
            # 开启新块
            cur_block = {"line": ln, "shots": [], "out": []}
            shots, acc, block_count = [], 0.0, 0
            continue
        m = _RE_SHOT.search(line)
        if m:
            dur = float(m.group(2))
            block_count += 1
            shots.append({"line": ln, "seq": block_count, "start": acc})
            acc += dur

    # 保存最后一个块
    if cur_block is not None:
        cur_block["shots"] = shots
        blocks.append(cur_block)

    # 第二步：扫描对白行（两种形式），按原始顺序收集
    out = []  # [(ln, speaker, dlg)]
    for ln, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        speaker, dlg, matched = "", "", False

        m = _RE_QUOTE_STANDALONE.match(s)
        if m and not s.startswith("台词"):
            speaker, dlg, matched = m.group(1), m.group(2), True

        if not matched and s.startswith("台词"):
            payload = s[2:].lstrip("：:").lstrip("｜|").strip()
            core = re.sub(r'【[^】]*】|\([^)]*\)|（[^）]*）', '', payload).strip().strip('"“”\'')
            if core in _TALK_EMPTY or not core:
                continue
            if ('"' in payload) or ('"' in payload):
                qm = re.search(r'[""]([^""]{1,320})[""]', payload)
                dlg = qm.group(1).strip() if qm else payload.strip().strip('"“”\'')
                speaker = payload[:qm.start()].strip() if qm else ""
            else:
                if "：" in payload:
                    speaker, dlg = payload.split("：", 1)
                elif ":" in payload:
                    speaker, dlg = payload.split(":", 1)
                else:
                    speaker, dlg = "", payload
            speaker, dlg = speaker.strip(), dlg.strip().strip('"“”\'')
            if not dlg or dlg in _TALK_EMPTY:
                continue
            out.append((ln, speaker, dlg))

        if matched:
            speaker, dlg = _clean_speaker(speaker), dlg.strip()
            if dlg and dlg not in _TALK_EMPTY:
                out.append((ln, speaker, dlg))

    # 第三步：对白归属到本块内的最近前向镜头；按首次发声分配 sid
    spk_id = {}  # 全局说话人ID（跨块复用同一角色名）
    result = []
    for (ln, speaker, dlg) in out:
        # 找到 ln 行所属的块
        target_block = None
        for bi, block in enumerate(blocks):
            blk_start = block["line"]
            blk_end = blocks[bi + 1]["line"] if bi + 1 < len(blocks) else len(lines)
            if blk_start <= ln < blk_end:
                target_block = block
                break

        if target_block is None:
            continue

        # 在该块内找最近的镜头（对白落在该块最后一个镜头之后，归属最后一镜）
        cur_seq, cur_start = 0, 0.0
        for s in target_block["shots"]:
            if s["line"] <= ln:
                cur_seq = s["seq"]
                cur_start = s["start"]

        key = _clean_speaker(speaker) if speaker else ""
        if key:
            if key not in spk_id:
                spk_id[key] = len(spk_id) + 1
            sid = spk_id[key]
        else:
            sid = 0
        result.append({
            "line": ln,
            "shot": cur_seq,
            "start": cur_start,
            "speaker": speaker,
            "speaker_clean": key,
            "sid": sid,
            "text": dlg,
        })
    return result


def _mmss(sec):
    """秒数转 MM:SS.mmm。"""
    sec = float(sec or 0)
    m = int(sec // 60)
    s = sec - m * 60
    mm = int(s // 1)
    ms = int(round((s - mm) * 1000))
    if ms >= 1000:
        ms -= 1000
        mm += 1
    return "%d:%02d.%03d" % (m, mm, ms)


def _bind_subject(speaker_raw, name_to_subj):
    """说话人名称包含某参考资产名时，绑定到 <Subject N>；取最具体的（最短）匹配。"""
    s = _clean_speaker(speaker_raw or "")
    if not s:
        return ""
    best, best_len = None, 9999
    for nm, idx in name_to_subj.items():
        if nm and (nm in s or s in nm):
            if len(nm) < best_len:
                best, best_len = idx, len(nm)
    return "<Subject %d>" % best if best is not None else ""



class GenAreaWidget(QWidget):
    """一个剧集生成区：提示词 + 参数 + 参考图 + 预览 + 保存，逻辑自包含。"""
    remove_requested = Signal(object)   # 请求移除本区
    generated = Signal(object, bool)    # 生成本区完成（area, 是否成功）——供批量逐集联动
    state_changed = Signal()            # 本区任何内容变化（提示词/参数/参考图），供持久化

    def __init__(self, index, parent=None, get_sniffer_text=None, on_log=None,
                 get_save_dir=None, on_saved=None, on_task=None, get_global_prompt=None,
                 get_asset_info=None, on_import_asset=None, get_global_aspect=None):
        super().__init__(parent)
        self.index = index
        # 允许窗口缩小到较窄宽度
        self.setMinimumWidth(220)
        self._get_sniffer = get_sniffer_text or (lambda: "")
        self._log = on_log or (lambda msg, lvl="info": None)
        self._get_save_dir = get_save_dir or (lambda: "")   # 返回项目「生成视频」目录
        self._on_saved = on_saved or (lambda path: None)    # 本地保存成功 → 通知侧栏刷新
        self._on_task = on_task or (lambda stage, info: None)  # 视频任务登记（created/progress/completed/failed）
        self._get_global = get_global_prompt or (lambda: "")   # 全局提示词（画面风格等，生成时前置）
        self._get_asset_info = get_asset_info or None
        self._on_import_asset = on_import_asset or None     # 从资产管理导入（主窗口回调）
        self._get_global_aspect = get_global_aspect or (lambda: "")  # 全局画幅（生成时若设置则覆盖各区画幅）
        self._model = config.AGNES_VIDEO.get("model") or "agnes-video-2.5-flash"

        # 任务状态（本区独立）
        self._creating = False
        self._task = None
        self._poll = None
        self._create_worker = None
        self._latest_url = ""
        self._latest_path = ""
        self._dl_path = ""
        self._gen_done_key = ""      # 最近一次成功生成的参数快照（用于一键生成时跳过已生成区）
        self._ref_imgs = []
        # @资产：已选中的资产 token → 图片路径 映射（用于反向删除）
        self._asset_refs = {}          # {token: img_path}
        self._asset_highlight_fmt = None  # 懒加载高亮格式

        self._build_ui()

        # 提示词输入时，实时解析 @资产名 并自动填充参考图（防抖）
        self._prompt_fill_timer = QTimer(self)
        self._prompt_fill_timer.setSingleShot(True)
        self._prompt_fill_timer.setInterval(400)
        self._prompt_fill_timer.timeout.connect(self._fill_assets_from_prompt)
        self.prompt.textChanged.connect(self._on_prompt_changed)
        # 用 eventFilter 监听 @ 键触发资产选择弹窗（不覆盖原有关键事件处理）
        self.prompt.installEventFilter(self)

        self._update_ref_ui()

    def _on_prompt_changed(self):
        # 若资产选择列表开着、但光标前已不是 @（用户手动继续输入），自动收起列表
        picker = getattr(self, "_asset_picker", None)
        if picker is not None and picker.isVisible():
            cur = self.prompt.textCursor()
            pos = cur.position()
            prev = self.prompt.document().characterAt(pos - 1) if pos > 0 else ''
            if prev != '@':
                picker.hide()
        self._prompt_fill_timer.start()
        self.state_changed.emit()   # 提示词变化 → 触发上层持久化

    # ---------------- @资产选择弹窗 ----------------

    def _show_asset_picker(self, prefix='@'):
        """在提示词光标位置弹出「人物/场景/道具」分类资产选择框。
        顶部三个分类标签，点击切换只显示对应类型资产。"""
        assets = getattr(self, "_asset_list", []) or []
        if not assets:
            return
        picker = QWidget()  # 顶层容器：顶部分类按钮 + 下方资产列表
        picker.setAttribute(Qt.WA_StyledBackground, True)
        picker.setWindowFlags(Qt.Popup | Qt.NoDropShadowWindowHint)
        picker.setObjectName("AssetPickerBox")
        picker.setStyleSheet(
            "QWidget#AssetPickerBox{background:#ffffff; border:1.5px solid #93c5fd;"
            " border-radius:8px;}")
        outer = QVBoxLayout(picker)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        # 顶部三个分类标签（互斥：同一时刻仅一个高亮）
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._picker_btns = {}
        bg = QButtonGroup(self)
        bg.setExclusive(True)
        for idx, cat in enumerate(("人物", "场景", "道具")):
            b = QToolButton()
            b.setText(cat)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton{border:1px solid #cbd5e1; border-radius:6px; padding:3px 10px;"
                " font-size:12px; color:#475569; background:#f1f5f9;}"
                "QToolButton:checked{background:#2563eb; color:#fff; border-color:#2563eb;}")
            b.clicked.connect(lambda _=False, c=cat: self._reload_picker_items(c))
            bar.addWidget(b)
            bg.addButton(b, idx)
            self._picker_btns[cat] = b
        bar.addStretch(1)
        outer.addLayout(bar)

        # 资产列表
        picker.list = QListWidget()
        picker.list.setUniformItemSizes(True)
        picker.list.setIconSize(QSize(24, 24))
        picker.list.setStyleSheet(
            "QListWidget{background:#ffffff; border:1px solid #e2e8f0; border-radius:6px;"
            " font-size:13px; padding:2px;}"
            "QListWidget::item{padding:1px 6px; border-radius:6px; color:#1e293b;}"
            "QListWidget::item:selected{background:#dbeafe; color:#2563eb;}")
        picker.list.itemActivated.connect(
            lambda item: self._pick_asset_from_list(item, picker))
        picker.list.itemClicked.connect(
            lambda item: self._pick_asset_from_list(item, picker))
        picker.list.installEventFilter(self)   # ESC/回车/失焦自动隐藏
        outer.addWidget(picker.list, 1)

        self._asset_picker = picker
        # 默认选中第一个分类并填充
        first = ("人物", "场景", "道具")
        cur_cat = next((c for c in first if any((a.get("type") or "") == c for a in assets)), "人物")
        for c, b in self._picker_btns.items():
            b.setChecked(c == cur_cat)
        self._reload_picker_items(cur_cat)

        # 定位到提示词光标右下角（全局坐标，顶层窗口直接用）
        cursor = self.prompt.textCursor()
        rect = self.prompt.cursorRect(cursor)
        local = self.prompt.mapToGlobal(rect.bottomRight())
        x = local.x()
        y = local.y() + 4

        # 防止超出屏幕可用区域：从底部向上退、从右向左退
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if y + picker.height() > screen.bottom():
            y = local.y() - rect.height() - picker.height() - 4
        if x + picker.width() > screen.right():
            x = screen.right() - picker.width() - 4
        picker.move(x, y)
        picker.show()
        picker.raise_()
        picker.setFocus()   # 让弹窗持有焦点：便于键盘上下选择；失焦/点击外部由事件过滤器自动关闭

    def _reload_picker_items(self, cat):
        """按分类（人物/场景/道具）填充资产选择列表，并让弹窗高度适配当前项数。"""
        picker = getattr(self, "_asset_picker", None)
        if not picker:
            return
        assets = getattr(self, "_asset_list", []) or []
        ql = picker.list
        ql.clear()
        item_h = 24
        slot_items = [a for a in assets if (a.get("type") or "") == cat]
        for a in slot_items:
            name = str(a.get("name", ""))
            if not name:
                continue
            item = QListWidgetItem(name)
            img = a.get("image")
            if isinstance(img, str) and os.path.isfile(img):
                pm = QPixmap(img)
                if not pm.isNull():
                    item.setIcon(QIcon(
                        pm.scaled(20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setData(Qt.UserRole, a)
            ql.addItem(item)
        ql.setCurrentRow(0) if slot_items else None
        # 让每个项显式给定高度：避免仅 1 项时列表行高被压缩到看不见
        row_h = 26
        ql.clearSelection()
        for i in range(ql.count()):
            it = ql.item(i)
            if it is not None:
                it.setSizeHint(QSize(0, row_h))
        # 高度：头部约 36 + 项目列表。用 sizeHint 计算实际高度，最少保留 1 行的可视高度
        max_list_h = 460
        need = len(slot_items)
        if need <= 0:
            list_h = row_h + 4
        else:
            list_h = min(max_list_h, row_h * need + 4)
        picker.setMaximumHeight(36 + max_list_h)
        ql.setFixedHeight(list_h)
        picker.setFixedWidth(290)
        picker.adjustSize()

    def _pick_asset_from_list(self, item, picker):
        """从列表选中资产：光标前已有 @ 则只补资产名，随后高亮整个 @资产名。"""
        data = item.data(Qt.UserRole)
        if not data:
            picker.hide()
            picker.deleteLater()
            if self._asset_picker is picker:
                self._asset_picker = None
            return
        name = str(data.get("name", ""))
        cursor = self.prompt.textCursor()
        pos = cursor.position()
        has_at = pos > 0 and self.prompt.document().characterAt(pos - 1) == '@'
        cursor.insertText(name)
        # 高亮整个 @资产名（含前面已经上屏的 @）
        end = cursor.position()
        start = end - len(name) - (1 if has_at else 0)
        hl = self.prompt.textCursor()
        hl.setPosition(max(start, 0))
        hl.setPosition(end, QTextCursor.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#dbeafe"))
        fmt.setFontWeight(QFont.Bold)
        hl.mergeCharFormat(fmt)
        picker.hide()
        self.prompt.setFocus()
        # 防抖延迟触发填充参考图
        self._prompt_fill_timer.stop()
        self._prompt_fill_timer.start()

    def _select_asset_from_picker(self):
        """按 Enter 确认选择。"""
        picker = getattr(self, "_asset_picker", None)
        if not picker or not picker.isVisible():
            return
        row = picker.list.currentRow()
        if row < 0:
            return
        item = picker.list.item(row)
        self._pick_asset_from_list(item, picker)

    def _highlight_token(self, cursor, token):
        """给已插入的 @token 设置高亮格式（浅蓝底）。"""
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#dbeafe"))
        fmt.setFontWeight(QFont.Bold)
        # 选中文本并应用格式
        start = cursor.position() - len(token)
        cursor.setPosition(start)
        cursor.setPosition(start + len(token), QTextCursor.KeepAnchor)
        cursor.mergeCharFormat(fmt)

    def _remove_asset_highlight(self, token):
        """移除提示词中 token 的高亮格式（兼容传入带 @ 或不带 @，正文明文一并清除）。"""
        token = token.lstrip("@")
        if not token:
            return
        text = self.prompt.toPlainText()
        # 同时清除 @token 与正文明文 token 两种出现形式
        for token_str in ("@%s" % token, token):
            idx = text.find(token_str)
            if idx < 0:
                continue
            cursor = self.prompt.textCursor()
            cursor.setPosition(idx)
            cursor.setPosition(idx + len(token_str), QTextCursor.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setBackground(Qt.NoBrush)
            fmt.setFontWeight(QFont.Normal)
            cursor.mergeCharFormat(fmt)

    def _highlight_prompt_mentions(self, tok, explicit):
        """高亮提示词中所有出现：显式引用高亮「@tok」，正文关键词高亮「tok」明文。"""
        text = self.prompt.toPlainText()
        token_str = "@%s" % tok if explicit else tok
        if not token_str:
            return
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#dbeafe"))
        fmt.setFontWeight(QFont.Bold)
        cursor = self.prompt.textCursor()
        start = 0
        while True:
            idx = text.find(token_str, start)
            if idx < 0:
                break
            cursor.setPosition(idx)
            cursor.setPosition(idx + len(token_str), QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(fmt)
            start = idx + len(token_str)

    # ---------------- UI ----------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("genAreaCard")
        card.setFrameShape(QFrame.StyledPanel)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(6, 6, 6, 6)
        cv.setSpacing(4)

        # 标题栏
        title_row = QHBoxLayout()
        self.title_lbl = QLabel("🎬 生成区 %d" % (self.index + 1))
        self.title_lbl.setStyleSheet("font-size:13px; font-weight:800; color:#1e293b;")
        title_row.addWidget(self.title_lbl)
        title_row.addSpacing(6)
        cap = QLabel("Agnes Video 2.5 Flash · 720P")
        cap.setObjectName("cap")
        title_row.addWidget(cap)
        title_row.addStretch(1)
        rm = QPushButton("✖ 移除本集")
        rm.setObjectName("ghostBtn")
        rm.setToolTip("删除本生成区")
        rm.clicked.connect(lambda: self.remove_requested.emit(self))
        title_row.addWidget(rm)
        self.merge_btn = QPushButton("＋")
        self.merge_btn.setObjectName("ghostBtn")
        self.merge_btn.setFixedSize(28, 28)
        self.merge_btn.setCursor(Qt.PointingHandCursor)
        self.merge_btn.setStyleSheet("font-size:16px; font-weight:bold; padding:0;")
        self.merge_btn.setToolTip("加入合并列表（再次点击取消选中）")
        self.merge_btn.clicked.connect(self._toggle_merge)
        title_row.addWidget(self.merge_btn)
        cv.addLayout(title_row)

        # 左：提示词 + 参数 + 参考图 + 运行（纵向紧凑堆叠）
        left = QWidget()
        lf = QVBoxLayout(left)
        lf.setContentsMargins(0, 0, 0, 0)
        lf.setSpacing(4)

        # 提示词（单行）
        h1 = QHBoxLayout()
        h1.setSpacing(4)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("输入画面描述，可复用嗅探/分析提示词（正文直接写资产名，如“陆知归在陆家老宅外村道”，自动拉取对应参考图）")
        self.prompt.setMinimumHeight(48)
        self.prompt.setMaximumHeight(48)
        h1.addWidget(self.prompt, 1)
        bcol = QVBoxLayout()
        bcol.setSpacing(2)
        self.go = QPushButton("🎬 生成")
        self.go.setObjectName("accentBtn")
        self.go.setToolTip("开始生成视频")
        self.go.setMinimumHeight(24)
        self.go.clicked.connect(self._start)
        bcol.addWidget(self.go)
        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setToolTip("停止生成任务")
        self.stop_btn.setMinimumHeight(24)
        bcol.addWidget(self.stop_btn)
        bcol.addStretch(1)
        h1.addLayout(bcol)
        lf.addLayout(h1)

        # 参数（单行紧凑）
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        row2.addWidget(QLabel("时长:"), 0)
        self.seconds = QComboBox()
        self.seconds.addItems(getattr(config, "VIDEO_SECONDS", ["5", "10"]))
        self.seconds.setCurrentText("10")
        self.seconds.setMinimumWidth(56)
        self.seconds.currentIndexChanged.connect(lambda *_: self.state_changed.emit())
        row2.addWidget(self.seconds, 0)
        row2.addWidget(QLabel("画幅:"), 0)
        self.aspect = QComboBox()
        self.aspect.addItems(ASPECT_RATIOS)
        self.aspect.setCurrentText("16:9")
        self.aspect.setMinimumWidth(56)
        self.aspect.currentIndexChanged.connect(lambda *_: self.state_changed.emit())
        row2.addWidget(self.aspect, 0)
        row2.addWidget(QLabel("负面:"), 0)
        self.negative = QLineEdit()
        self.negative.setPlaceholderText("模糊、畸变…")
        self.negative.setMinimumWidth(80)
        self.negative.textChanged.connect(lambda *_: self.state_changed.emit())
        row2.addWidget(self.negative, 0)
        lf.addLayout(row2)

        # 参考图片（单行紧凑）
        refrow = QHBoxLayout()
        refrow.setSpacing(3)
        self.reflist = QListWidget()
        self.reflist.setViewMode(QListView.IconMode)
        self.reflist.setDragDropMode(QAbstractItemView.InternalMove)
        self.reflist.setDefaultDropAction(Qt.MoveAction)
        self.reflist.setFlow(QListView.LeftToRight)
        self.reflist.setWrapping(False)
        self.reflist.setResizeMode(QListView.Adjust)
        self.reflist.setIconSize(QSize(60, 90))
        self.reflist.setFixedHeight(96)
        self.reflist.setSpacing(3)
        self.reflist.setStyleSheet("QListWidget{background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px;} QListWidget::item{border:none; padding:2px;} QListWidget::item:selected{background:#e2e8f0;}")
        self.reflist.setToolTip("点击缩略图放大预览 · 悬停右上角 ✕ 删除 · 可拖动排序 · 末尾 + 添加图片（可超5张，发送时自动合并为≤5）")
        self.reflist.model().rowsMoved.connect(self._sync_ref_order)
        refrow.addWidget(self.reflist, 1)
        lf.addLayout(refrow)
        tip = QLabel("点击缩略图放大 · 悬停✕删除 · 末尾+添加 · 不选则纯文生视频")
        tip.setObjectName("cap")
        tip.setStyleSheet("font-size:10px; color:#94a3b8;")
        tip.setWordWrap(True)
        lf.addWidget(tip)

        # 创建任务等提示：专门的提示框（位于底部、停止按钮下沿区域），区别于 meta 历史信息
        self.status = QLabel("就绪")
        self.status.setStyleSheet(
            "background:#eff6ff; color:#1d4ed8; font-weight:600; font-size:11px;"
            " border:1px solid #bfdbfe; border-radius:6px; padding:6px 8px;")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lf.addWidget(self.status)

        # 历史信息行（meta，显示次数/耗时/尺寸等）
        st_row = QHBoxLayout()
        st_row.setSpacing(4)
        st_row.addStretch(1)
        self.meta = QLabel("")
        self.meta.setStyleSheet("color:#64748b; font-size:10px;")
        self.meta.setWordWrap(True)
        self.meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        st_row.addWidget(self.meta, 1)
        lf.addLayout(st_row)

        cv.addWidget(left, 1)

        outer.addWidget(card)

    # ---------------- 参考图片 ----------------

    def _pick_images(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择参考图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if not files:
            return
        new_list = list(self._ref_imgs)
        for fp in files:
            if fp in new_list:
                continue
            if len(new_list) >= _MAX_REF:
                break
            new_list.append(fp)
        self._ref_imgs = new_list
        self._update_ref_ui()

    def _remove_image(self, path):
        if path in self._ref_imgs:
            self._ref_imgs.remove(path)
        # 手动删除：同步清掉该图对应的所有自动映射（token→img），避免关键词还在时又被自动填回
        for tok in [t for t, p in self._asset_refs.items() if p == path]:
            self._asset_refs.pop(tok, None)
            self._remove_asset_highlight(tok)
        self._update_ref_ui()

    def _fill_assets_from_prompt(self):
        """解析提示词中的 @资产名 标记 + 正文命中的资产关键词，自动填充参考图：
        - 新增/命中的 token → 匹配资产，填充参考图并记录映射
        - 消失的 token → 移除对应的参考图
        资产名不区分大小写；支持「名称完全相等 > 名称包含 token > token 包含名称」三级匹配；
        正文直接出现的资产关键词（如“陆知归走在陆家老宅外村道”）无需 @ 也会自动拉取参考图。"""
        assets = getattr(self, "_asset_list", []) or []
        if not assets:
            return
        prompt = self.prompt.toPlainText() or ""
        # 1) 显式 @token（优先级最高，用户主动指定）
        explicit_tokens = []
        for m in re.finditer(r"@([^\s@，。,.!！?？:：;；、\n]+)", prompt):
            tok = m.group(1).strip()
            if tok and tok not in explicit_tokens:
                explicit_tokens.append(tok)

        # 2) 正文关键词 token：正文中出现与某资产名相同的连续文本
        #    （排除已被 @ 占用的位置，避免重复；短于2字的资产名不做关键词自动匹配，仅支持 @）
        keyword_tokens = self._keyword_tokens_from_prompt(prompt, assets, explicit_tokens)

        # 合并顺序：显式 @ 优先，再关键词（保持正文出现顺序）
        current_tokens = explicit_tokens + [t for t in keyword_tokens if t not in explicit_tokens]

        # 3) 先处理新增/匹配的 token → 填充参考图
        filled = []
        filled_assets = []
        for tok in current_tokens:
            matched = self._match_asset_by_token(tok, assets)
            if not matched:
                continue
            if any(m is matched for m in filled_assets):
                continue
            # 旧图：用于资产换图/删图时安全清理（仅当不再被其它存活 token 引用才移除）
            old_img = self._asset_refs.get(tok)
            # 无论图是否新加入都记录映射：已有参考图的命中资产也要能高亮
            img = matched.get("image")
            if isinstance(img, str) and os.path.isfile(img):
                self._asset_refs[tok] = img
                if img not in self._ref_imgs:
                    if len(self._ref_imgs) >= _MAX_REF:
                        break
                    self._ref_imgs.append(img)
                    filled.append(tok)
                # 资产换图：旧图替换掉（该 token 已指向新图，旧的若无人再引用则清理）
                if old_img and old_img != img and old_img in self._ref_imgs:
                    still_used = any(
                        tt != tok and self._asset_refs.get(tt) == old_img
                        for tt in self._asset_refs)
                    if not still_used:
                        self._ref_imgs.remove(old_img)
                        filled.append(tok)
            else:
                self._asset_refs[tok] = ""
                # 资产图被移除/失效：token 仍在但已无有效图，清理旧图
                if old_img and old_img in self._ref_imgs:
                    still_used = any(
                        tt != tok and self._asset_refs.get(tt) == old_img
                        for tt in self._asset_refs)
                    if not still_used:
                        self._ref_imgs.remove(old_img)
                        filled.append(tok)
            filled_assets.append(matched)

        # 4) 处理消失的 token → 移除对应参考图（该图仍被其它现存活 token 引用则不删）
        alive_imgs = set()
        for tok in current_tokens:
            m = self._match_asset_by_token(tok, assets)
            if m:
                alive_imgs.add(str(m.get("image") or ""))
        gone_tokens = []
        for tok in list(self._asset_refs):
            if tok in current_tokens:
                continue
            img = self._asset_refs.pop(tok, None)
            # 仅当不再被任何活 token 引用时才真正移除（防止"@名"删除但"关键词名"仍在）
            if img and img in self._ref_imgs and img not in alive_imgs:
                self._ref_imgs.remove(img)
                gone_tokens.append(tok)
            self._remove_asset_highlight(tok)
            self._remove_asset_highlight("@%s" % tok)

        # 5) 同步高亮：显式 @ 与正文关键词命中的资产名（有参考图）一律高亮
        for tok in current_tokens:
            if tok in self._asset_refs:
                self._highlight_prompt_mentions(tok, tok in explicit_tokens)

        if filled or gone_tokens:
            self._update_ref_ui()
            if filled:
                self._log("已按提示词自动填充参考图：%s" % "、".join(filled), "info")
            if gone_tokens:
                self._log("已移除参考图：%s" % "、".join(gone_tokens), "info")

    def _keyword_tokens_from_prompt(self, prompt, assets, explicit_tokens):
        """扫描正文，返回与资产名相同的关键词 token（按首次出现顺序，去重）。

        规则：
        - 仅当资产名（或其主名）以连续文本出现在正文才自动匹配（如“陆知归/陆家老宅外村道/黑色签字笔”）；
        - 资产名短于 2 个字符不做正文自动匹配（避免误命中），仍可用 @ 显式引用；
        - 已显式 @ 引用的资产不再重复加入关键词列表；
        - 名称带“·日/·夜/｜”等修饰时，主名（分隔前部分）命中正文也视为提及。
        """
        if not prompt or not assets:
            return []
        # 台词（成对引号内容）内出现的人名/场景/道具不参与自动匹配，
        # 避免对话内容误触发参考图填充与高亮。仅匹配正文（非台词区）里的明文资产名。
        _rng_pat = re.compile(r'“[^”\n]*”|"[^"\n]*"|「[^」\n]*」|『[^』\n]*』|\'[^\'\n]*\'')
        d_ranges = [(m.start(), m.end()) for m in _rng_pat.finditer(prompt)]

        def _in_st(st):
            return any(s <= st < e for s, e in d_ranges)

        used_explicit = set()
        for tok in explicit_tokens:
            used_explicit.add(tok.lower())
        hits = []
        for a in assets:
            name = str(a.get("name") or "").strip()
            if len(name) < 2:
                continue
            low = name.lower()
            if low in used_explicit:
                continue
            # 候选词：完整名 + 主名（取第一个分隔符如 · ｜ | / 、 之前的较长片段）
            cands = [low]
            for sep in ("·", "｜", "|", "／", "/", "、", "（", "(", " ", "　"):
                if sep in low:
                    head = low.split(sep)[0].strip()
                    if len(head) >= 2:
                        cands.append(head)
                    break
            # 变体优先：正文里出现「资产名[服装词]」或「资产名【服装词】」时，产出同风格完整方括号
            # token（该 token 自带参考图变体）。token 采用正文里实际出现的括号风格，保证高亮 find() 命中。
            var_found = None
            var_idx = -1
            pat = re.compile(
                re.escape(name) + r'(?:\[(?P<va>[^\[\]]+)\]|\【(?P<vb>[^【】]+)\】)')
            for m in pat.finditer(prompt):
                if _in_st(m.start()):
                    continue   # 变体命中落在台词内，跳过
                if m.group("va") is not None:
                    varw = m.group("va").strip()
                    style, close = "[", "]"
                else:
                    varw = m.group("vb").strip()
                    style, close = "【", "】"
                if not re.search(r'[\u4e00-\u9fff]', varw):
                    continue
                i = m.start()
                if i >= 0 and (var_idx < 0 or i < var_idx) and not (i > 0 and prompt[i - 1] == "@"):
                    var_found = "%s%s%s%s" % (name, style, varw, close)
                    var_idx = i
            if var_found:
                hits.append((var_idx, var_found))
                continue
            idx = -1
            for c in cands:
                pos = 0
                while True:
                    i = prompt.lower().find(c, pos)
                    if i < 0:
                        break
                    if not _in_st(i):
                        break   # 找到一个正文（非台词区）命中
                    pos = i + 1
                if i >= 0 and (idx < 0 or i < idx):
                    idx = i
            if idx < 0:
                continue
            # 跳过该命中其实是被 @ 前缀包着的情况：向前看一位是否为 @
            if idx > 0 and prompt[idx - 1] == "@":
                continue
            hits.append((idx, name))
        hits.sort(key=lambda x: x[0])
        out = []
        for _idx, name in hits:
            if name not in out:
                out.append(name)
        return out

    @staticmethod
    def _split_variant_token(tok):
        """把「人物[服装]」或「人物【服装】」变体拆分为 (人物名, 服装词)。
        同时支持 ASCII 方括号「[]」与中文全角括号「【】」（开闭必须成对、同种括号）。
        服装词至少需含一个汉字，避免把「第[1]集」「第【1】集」等误判为变体。
        非变体返回 (原tok, None)。"""
        s = (tok or "").strip()
        m = re.match(r'^(?P<base>.+?)(?:\[(?P<va>[^\[\]]+)\]|\【(?P<vb>[^【】]+)\】)$', s)
        if not m:
            return s, None
        base = m.group("base").strip()
        var = (m.group("va") or m.group("vb") or "").strip()
        if not base or not var:
            return s, None
        if not re.search(r'[\u4e00-\u9fff]', var):
            return s, None
        return base, var

    @staticmethod
    def _match_variant_image(asset, var_word):
        """在人物的 variants 里找与 var_word 匹配的变体，返回该变体的图片路径或 None。
        匹配顺序：label 完全相等 > label 含 var_word > keywords 含 var_word。"""
        variants = asset.get("variants") or []
        if not variants:
            return None
        vw = (var_word or "").strip()

        def label_of(v):
            return str(v.get("label") or "").strip()

        # 1) label 完全相等
        for v in variants:
            if label_of(v) and label_of(v) == vw:
                img = v.get("image")
                if isinstance(img, str) and os.path.isfile(img):
                    return img
        # 2) label 包含 var_word
        for v in variants:
            if label_of(v) and vw in label_of(v):
                img = v.get("image")
                if isinstance(img, str) and os.path.isfile(img):
                    return img
        # 3) keywords 含 var_word
        for v in variants:
            for kw in (v.get("keywords") or []):
                if kw and vw in str(kw):
                    img = v.get("image")
                    if isinstance(img, str) and os.path.isfile(img):
                        return img
        return None

    @classmethod
    def _match_by_variant(cls, token, assets):
        """方括号变体匹配「人物[服装]」，按优先级：
        1) 人物资产的 variants 里找服装变体（推荐模型：服装变体挂在人物下）；
        2) 兼容独立存在的、名字恰为「人物[服装]」的资产卡 → 直接用它的图；
        3) 都没有 → 回落到人物默认图。"""
        base, var = cls._split_variant_token(token)
        if var is None:
            return None, None
        # 1) 纯名匹配定位人物资产（只取人物），再在其 variants 里找服装变体
        people = [a for a in assets if (a.get("type") or "") == "人物"]
        base_asset = cls._match_asset_by_token(base, people) if people else None
        if base_asset is not None:
            vimg = cls._match_variant_image(base_asset, var)
            if vimg:
                copy = dict(base_asset)
                copy["image"] = vimg
                copy["_variant_label"] = var
                return copy, var
        # 2) 兼容历史遗留：资产库里单独存在一张名为「人物[服装]」或「人物【服装】」的卡片
        #    （如「陆知归[西装]」/「陆知归【西装】」），方括号 token 优先指向它，而不是回落基座默认图。
        #    提示词里两种括号写法都可能出现，资产卡存储也可能只用其中一种，故两种都尝试。
        cand_names = [
            "%s[%s]" % (base, var),
            "%s【%s】" % (base, var),
        ]
        for full in cand_names:
            full_low = full.lower()
            for a in assets:
                nm = str(a.get("name") or "").strip()
                if nm.lower() == full_low:
                    img = a.get("image")
                    c2 = dict(a)
                    c2["_variant_label"] = var
                    if isinstance(img, str) and os.path.isfile(img):
                        return c2, var
                    return a, var   # 该卡暂无有效图，仍返回此资产（后续 isfile 判断决定是否填图）
        # 3) 回落到人物默认图
        if base_asset is not None:
            return base_asset, var
        return None, None

    @classmethod
    def _match_asset_by_token(cls, token, assets):
        """返回 asset dict 或 None。
        优先解析「人物[服装]」方括号变体；否则按 相等 > 名称含@标记 > @标记含名称 三级匹配。"""
        # 1) 方括号变体优先
        vr, var_word = cls._match_by_variant(token, assets)
        if vr is not None:
            return vr
        # 2) 纯名三级匹配
        t = token.lower()
        exact = [a for a in assets if a.get("name") and str(a["name"]).lower() == t]
        if exact:
            return exact[0]
        contains = [a for a in assets if a.get("name") and t in str(a["name"]).lower()]
        if contains:
            contains.sort(key=lambda a: len(str(a.get("name", ""))))
            return contains[0]
        sub = [a for a in assets if a.get("name")
               and str(a["name"]).strip() and str(a["name"]).lower() in t]
        if sub:
            sub.sort(key=lambda a: len(str(a.get("name", ""))), reverse=True)
            return sub[0]
        return None

    def _sync_ref_order(self, *_args):
        order = []
        for i in range(self.reflist.count()):
            item = self.reflist.item(i)
            p = item.data(Qt.UserRole)
            if p and p != "__ADD__":
                order.append(p)
        self._ref_imgs = order
        self._update_ref_ui()

    def _ref_image_name(self, path):
        """优先返回资产名（在 _asset_list 中按 image==path 反查 name）；
        找不到再用实际填充这张图的提示词 token（多变体取最长最具体）；都没有则回落到文件名。"""
        for a in getattr(self, "_asset_list", []) or []:
            if isinstance(a, dict) and a.get("image") == path and a.get("name"):
                return str(a["name"])
        best = ""
        for tok, p in self._asset_refs.items():
            if p == path and isinstance(tok, str) and len(tok) > len(best):
                best = tok
        return best or os.path.basename(path)

    def _thumb_widget(self, path, name=""):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        # 图片 + 右上角删除按钮
        pic = QLabel()
        pic.setFixedSize(46, 46)
        pm = QPixmap(path)
        if not pm.isNull():
            pic.setPixmap(pm.scaled(46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            pic.setText("图")
            pic.setStyleSheet("border:1px solid #94a3b8; color:#94a3b8; background:#e2e8f0;")
        pic.setAlignment(Qt.AlignCenter)
        if not pm.isNull():
            pic.setStyleSheet("border:1px solid #94a3b8; border-radius:6px; background:#e2e8f0;")
        pic.setProperty("ref_path", path)
        pic.setCursor(Qt.PointingHandCursor)
        pic.setToolTip(((name + " · ") if name else "") + "点击放大预览")
        pic.installEventFilter(self)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(pic, 0, Qt.AlignTop)
        xbtn = QPushButton("✕")
        xbtn.setFixedSize(16, 16)
        xbtn.setCursor(Qt.PointingHandCursor)
        xbtn.setToolTip("删除该参考图")
        xbtn.setStyleSheet(
            "QPushButton{color:#94a3b8; background:rgba(255,255,255,0.75); border:1px solid #cbd5e1;"
            " border-radius:8px; font-size:11px; font-weight:800; padding:0; margin:0;}"
            "QPushButton:hover{background:#ef4444; color:#fff; border-color:#ef4444;}")
        xbtn.clicked.connect(lambda checked=False, p=path: self._remove_image(p))
        row.addWidget(xbtn, 0, Qt.AlignTop)
        lay.addLayout(row)
        # 名称（人物/场景/道具 资产名，按提示词 token 反查），超宽省略
        raw = name or os.path.basename(path)
        nl = QLabel()
        nl.setAlignment(Qt.AlignCenter)
        nl.setFixedWidth(60)
        nl.setStyleSheet("color:#334155; font-size:11px; background:transparent;")
        nl.setText(nl.fontMetrics().elidedText(raw, Qt.ElideRight, 58))
        nl.setToolTip(raw)
        lay.addWidget(nl, 0, Qt.AlignCenter)
        return w

    def _add_widget(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        btn = QToolButton()
        btn.setText("+")
        btn.setFixedSize(52, 46)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("添加参考图片（可超5张，发送时自动合并为≤5）")
        btn.setStyleSheet(
            "QToolButton{font-size:22px; color:#3b82f6; background:#f8fafc; border:1.5px dashed #93c5fd;"
            " border-radius:8px; font-weight:700;}"
            "QToolButton:hover{background:#dbeafe; border-color:#3b82f6;}")
        btn.clicked.connect(self._show_add_menu)
        v.addWidget(btn, 0, Qt.AlignCenter)
        n = len(self._ref_imgs)
        cnt = QLabel("%d/5" % n if n <= 5 else "≥5:合并%d→5" % n)
        cnt.setStyleSheet("color:#94a3b8; font-size:9px; background:transparent;")
        cnt.setAlignment(Qt.AlignCenter)
        cnt.setToolTip("已选 %d 张参考图；发送时若超过 5 张，自动把超出部分拼接合并并在图上标注资产名，传给模型仍 ≤5 张" % n)
        v.addWidget(cnt, 0, Qt.AlignCenter)
        return w

    def _show_add_menu(self):
        m = QMenu(self)
        m.setStyleSheet(
            "QMenu{background:#ffffff; border:1.5px solid #93c5fd; border-radius:8px; padding:4px;}"
            "QMenu::item{padding:7px 22px 7px 12px; border-radius:6px; color:#1e293b; font-size:12px;}"
            "QMenu::item:selected{background:#dbeafe; color:#2563eb;}")
        a_as = m.addAction("🗂 从资产管理导入")
        a_up = m.addAction("📁 上传本地图片")
        btn = self.sender()
        if btn is not None:
            pos = btn.mapToGlobal(btn.rect().topRight() + QPoint(0, 4))
        else:
            pos = self.reflist.viewport().mapToGlobal(QPoint(0, 0))
        act = m.exec_(pos)
        if act == a_as:
            if self._on_import_asset:
                self._on_import_asset(self)
            else:
                QMessageBox.information(self, "提示", "尚未接入资产管理（未打开项目）")
        elif act == a_up:
            self._pick_images()

    def eventFilter(self, obj, event):
        # 监听提示词框按键
        if obj is self.prompt and event.type() == QEvent.KeyPress:
            key = event.key()
            # 输入法下 @ 可能以 text() 形式出现（key 为 2/其它），用文本兜底
            is_at = (key == Qt.Key_At) or (event.text() == '@')
            if is_at:
                # 放行：让 @ 正常上屏；稍后弹出资产选择列表
                QTimer.singleShot(0, self._delayed_show_at_picker)
                return False
            if key == Qt.Key_Escape and getattr(self, "_asset_picker", None) and self._asset_picker.isVisible():
                self._asset_picker.hide()
                self._asset_picker.deleteLater()
                self._asset_picker = None
                return True
            if key == Qt.Key_Return and getattr(self, "_asset_picker", None) and self._asset_picker.isVisible():
                self._select_asset_from_picker()
                return True
        # 监听资产选择列表：失焦 / 点击外部 / 窗口失活时直接关闭（不选也关）
        picker = getattr(self, "_asset_picker", None)
        if picker is not None and (obj is picker or obj is getattr(picker, "list", None)) \
                and event.type() in (QEvent.FocusOut, QEvent.WindowDeactivate):
            picker.hide()
            picker.deleteLater()
            if self._asset_picker is picker:
                self._asset_picker = None
            self.prompt.setFocus()
            return True
        # 监听资产选择列表按键（ESC 关闭 / 回车确认）
        picker = getattr(self, "_asset_picker", None)
        if picker is not None and (obj is picker or obj is getattr(picker, "list", None)) \
                and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Escape:
                picker.hide()
                picker.deleteLater()
                if self._asset_picker is picker:
                    self._asset_picker = None
                self.prompt.setFocus()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._select_asset_from_picker()
                return True
        # 原有逻辑：点击参考图缩略图放大预览
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton \
                and isinstance(obj, QLabel):
            p = obj.property("ref_path")
            if p:
                self._show_ref_preview(p)
                return True
        return super().eventFilter(obj, event)

    def _delayed_show_at_picker(self):
        """@ 字符上屏后弹出资产列表；仅当光标前确实是 @ 才弹。"""
        if not getattr(self, "_asset_list", []):
            return
        cur = self.prompt.textCursor()
        pos = cur.position()
        if pos <= 0:
            return
        if self.prompt.document().characterAt(pos - 1) != '@':
            return
        self._show_asset_picker('@')

    def _show_ref_preview(self, path):
        dlg = QDialog(self)
        dlg.setWindowTitle("参考图预览 - " + os.path.basename(path))
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
        dlg.resize(700, 500)
        lay = QVBoxLayout(dlg)
        img = QLabel()
        pix = QPixmap(path)
        if pix.isNull():
            img.setText("无法加载图片")
            img.setAlignment(Qt.AlignCenter)
            img.setStyleSheet("color:#94a3b8; font-size:14px;")
        else:
            img.setPixmap(pix.scaled(860, 620, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        img.setAlignment(Qt.AlignCenter)
        lay.addWidget(img)
        dlg.setModal(False)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _update_ref_ui(self):
        self.reflist.blockSignals(True)
        self.reflist.clear()
        for p in self._ref_imgs:
            nm = self._ref_image_name(p)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, p)
            item.setSizeHint(QSize(60, 90))
            item.setToolTip(nm)
            self.reflist.addItem(item)
            self.reflist.setItemWidget(item, self._thumb_widget(p, nm))
        add_item = QListWidgetItem()
        add_item.setData(Qt.UserRole, "__ADD__")
        add_item.setSizeHint(QSize(60, 90))
        self.reflist.addItem(add_item)
        self.reflist.setItemWidget(add_item, self._add_widget())
        self.reflist.blockSignals(False)
        self.state_changed.emit()   # 参考图增删/排序等变化 → 触发上层持久化

    def _toggle_merge(self):
        self._merge_selected = getattr(self, "_merge_selected", False)
        self._merge_selected = not self._merge_selected
        if self._merge_selected:
            self.merge_btn.setStyleSheet("""
                QPushButton{background:#2563eb;color:#fff;border-radius:13px;font-weight:bold;font-size:14px;}
                QPushButton:hover{background:#1d4ed8;}""")
            self.parent_widget and self.parent_widget._merge_selection_changed()
        else:
            self.merge_btn.setStyleSheet("")
            self.parent_widget and self.parent_widget._merge_selection_changed()

    def set_parent(self, parent):
        self.parent_widget = parent

    def merge_status_style(self, selected):
        """供外部同步选中状态样式。"""
        if selected:
            self.merge_btn.setStyleSheet("""
                QPushButton{background:#2563eb;color:#fff;border-radius:13px;font-weight:bold;font-size:14px;}
                QPushButton:hover{background:#1d4ed8;}""")
        else:
            self.merge_btn.setStyleSheet("")

    def _reuse(self):
        src = (self._get_sniffer() or "").strip()
        if not src:
            self.status.setText("暂无可用提示词，请先在「视频嗅探」页完成视频分析")
            self._log("暂无嗅探/分析结果可复用", "warn")
            return
        core = extract_core_prompt(src)
        self.prompt.setPlainText(core if core else src)
        self.status.setText("已复用嗅探分析提示词")
        self._log("已把视频分析提示词结果复用到生成区 %d" % (self.index + 1), "info")

    def preset(self, prompt, seconds, label=None):
        """按分镜一键预填：填入本段提示词与时长（秒）。"""
        cur = ""
        if prompt:
            self.prompt.setPlainText(prompt)
            if "\n" in prompt:
                # 长分镜提示词：随行数撑高，便于看到完整五方面内容
                lines = len(prompt.splitlines())
                h = min(240, 70 + lines * 13)
                self.prompt.setMinimumHeight(h)
                self.prompt.setMaximumHeight(h)
            else:
                self.prompt.setMinimumHeight(64)
                self.prompt.setMaximumHeight(64)
        if seconds is not None:
            cur = str(int(round(float(seconds))))
            items = [self.seconds.itemText(i)
                     for i in range(self.seconds.count())]
            if cur in items:
                self.seconds.setCurrentText(cur)
            elif items:
                near = min(items, key=lambda s: abs(int(s) - int(cur)))
                self.seconds.setCurrentText(near)
                cur = near
        if label:
            self.title_lbl.setText("🎬 %s · %ss" % (label, cur))
        self.status.setText("已按分镜预填提示词与时长")

    def export_state(self):
        """导出当前生成区的完整可持久化状态（重启/切剧集后恢复）。"""
        return {
            "prompt": self.prompt.toPlainText().strip(),
            "seconds": self.seconds.currentText(),
            "aspect": self.aspect.currentText(),
            "model": self._model,
            "negative": self.negative.text().strip(),
            "label": self._gen_label(),
            "refs": list(self._ref_imgs),
            "done_key": self._gen_done_key,
        }

    def import_state(self, st):
        """按导出的状态还原生成区：提示词、时长、标签、参考图。
        还原后主动触发一次关键词匹配（高亮/@同步），确保资产库变化后正确刷新。"""
        st = st or {}
        self.preset(st.get("prompt", ""), st.get("seconds"),
                    label=st.get("label") or None)
        try:
            if st.get("aspect") and self.aspect.findText(str(st["aspect"])) >= 0:
                self.aspect.setCurrentText(str(st["aspect"]))
            if st.get("negative"):
                self.negative.setText(str(st["negative"]))
        except Exception:
            pass
        self._ref_imgs = []
        for p in st.get("refs", []):
            if p and len(self._ref_imgs) < _MAX_REF:
                self._ref_imgs.append(p)
        self._asset_refs = {}
        self._update_ref_ui()
        # 用当前资产库重新匹配一次，填充高亮与参考图
        self._fill_assets_from_prompt()
        # 恢复"已生成"标记（重启/切剧集后一键生成仍可跳过已生成区）
        self._gen_done_key = st.get("done_key", "")

    def _gen_label(self):
        """从标题栏解析出生成区名称（去掉 🎬 与时长后缀）。"""
        try:
            t = self.title_lbl.text() or ""
            return t.split("·")[0].replace("🎬", "").strip()
        except Exception:
            return ""

    # ---------------- 历史（共享文件，各区独立展示） ----------------

    def _history_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(config.__file__)),
                            "agnes_video_history.json")

    def _load_history(self):
        try:
            if not os.path.exists(self._history_path()):
                return []
            with open(self._history_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_history(self, records):
        try:
            with open(self._history_path(), "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _insert_history(self, rec):
        recs = self._load_history()
        recs.insert(0, rec)
        self._save_history(recs)
        self._refresh_history()

    def _update_history_status(self, status, err="", prog="", url=""):
        recs = self._load_history()
        if not recs or not self._task:
            return
        vid = self._task.get("video_id")
        changed = False
        for r in recs:
            if r.get("video_id") == vid:
                if r.get("status") != status:
                    r["status"] = status
                    changed = True
                if err:
                    r["error"] = err
                    changed = True
                if url:
                    r["video_url"] = url
                    changed = True
                break
        # 只有关键字段真的变化才写盘，避免每 2 秒 tick 反复磁盘 IO
        if not changed:
            return
        self._save_history(recs)
        self._refresh_history()

    def _update_history_saved(self, path):
        recs = self._load_history()
        if not recs or not self._task:
            return
        vid = self._task.get("video_id")
        for r in recs:
            if r.get("video_id") == vid:
                r["saved_path"] = path
                break
        self._save_history(recs)
        self._refresh_history()

    _HIST_BADGE = {"completed": "✅ 完成", "processing": "⏳ 生成中",
                   "pending": "🕓 待处理", "failed": "❌ 失败"}

    def _history_text(self, r):
        st = r.get("status", "") or ""
        badge = self._HIST_BADGE.get(st, "·")
        ep = r.get("episode") or "-"
        prompt = (r.get("prompt") or "").replace("\n", " ").strip()[:34]
        seconds = r.get("seconds") or "-"
        aspect = r.get("aspect_ratio") or "-"
        t = r.get("created_at") or ""
        line = "%s  分镜%s · %ss %s · %s · %s" % (badge, ep, seconds, aspect, t, prompt)
        if r.get("error"):
            line += "\n    ⚠ " + str(r.get("error"))[:56]
        if r.get("saved_path"):
            line += "\n    💾 " + os.path.basename(r.get("saved_path"))
        return line

    def _refresh_history(self):
        pass

    def _clear_history(self):
        if not self.hist.count():
            return
        ret = QMessageBox.question(self, "清空历史记录",
                                   "确定清空全部生成历史记录吗？此操作不可恢复。")
        if ret != QMessageBox.Yes:
            return
        self._save_history([])
        self._refresh_history()
        self._log("已清空生成历史记录", "info")

    def _history_activated(self, item):
        pass

    # ---------------- 生成流程 ----------------

    def _plan_merge(self, paths):
        """把 >5 张参考图分成不超过 5 组（每组 1~3 张且至少 2 才拼），覆盖全部原图、
        单图尽量多。分组列表每个元素是该组包含的原图路径（长度 1 / 2 / 3）。
        算法：5 组每组分得若干张；为最大化单图数，把多出的 R 张增量尽量集中到最少组（每组最多加 2 张）。
        例：6 张→[单,单,单,单,二合一]；7 张→[单,单,单,单,三合一]；8 张→[单,单,单,三合一,二合一]。"""
        n = len(paths)
        if n <= 5:
            return [[p] for p in paths]
        # 最多 5 组，覆盖 n（n≤15）。先全部设想为单图共 5 组，R 张增量需并入某些组（每组最多再 +2）
        R = n - 5
        m = -(-R // 2)          # 需要的"多图组"数 = ceil(R/2)；其余组保持单图
        s = 5 - m               # 单图数量
        # 把 R 张增量尽量集中给前面的多图组（每组 0~2）
        d = [0] * m
        rem = R
        for i in range(m):
            space = 2 * (m - 1 - i)
            d[i] = min(2, max(0, rem - space))
            rem -= d[i]
        sizes = [1] * s + [1 + d[i] for i in range(m)]
        groups = []
        idx = 0
        for sz in sizes:
            groups.append(paths[idx:idx + sz])
            idx += sz
        return groups

    def _merge_composite(self, group, out_path):
        """把一组（2~3 张）参考图水平拼接成一张新图，并在每张子图下方用资产名标注，
        让模型能按名称把他们对应到剧本里出现的人物/场景/道具。
        读取失败的子图会被跳过，且其资产名一并跳过（按下标配对，避免错位）。"""
        pairs = []  # [(path, im), ...] 成功读取的（按原顺序）
        for p in group:
            im = QImage(p)
            if im.isNull():
                continue
            pairs.append((p, im.scaledToHeight(384, Qt.SmoothTransformation)))
        if not pairs:
            return None
        imgs = [im for _, im in pairs]
        # 标注参数按子图宽度比例自适应，保证模型清晰可读：
        #   字号 ≈ 子图宽 5.5%（最小 22px），标注条高 ≈ 子图宽 10%（最小 44px），黑字 #111。
        sub_w = min(im.width() for im in imgs)
        font_px = max(22, int(sub_w * 0.055))
        text_h = max(44, int(sub_w * 0.10))
        total_w = sum(im.width() for im in imgs)
        canvas = QImage(max(total_w, 1), imgs[0].height() + text_h, QImage.Format_ARGB32)
        canvas.fill(Qt.white)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)
        x = 0
        font = QFont()
        font.setPixelSize(font_px)
        painter.setFont(font)
        for path, im in pairs:
            painter.drawImage(x, 0, im)
            painter.setPen(QPen(QColor("#64748b"), 1.5))
            painter.drawRect(x, 0, im.width(), im.height())
            name = self._ref_image_name(path)
            # 不省略，直接全量居中；字号已足够大，正常宽度都能放得下
            painter.setPen(QColor("#111111"))
            painter.drawText(QRect(x + 2, im.height(), im.width() - 4, text_h),
                             Qt.AlignCenter, name)
            x += im.width()
        painter.end()
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            if canvas.save(out_path, "PNG"):
                return out_path
        except Exception:
            pass
        return None

    def _single_label(self, path, out_path):
        """给单张参考图在底部加一条资产名标注条（与原拼接图样式一致：白底 + 红字 + 灰蓝边），
        让模型也能按图上的名称把该参考图对应到剧本里的人物/场景/道具。
        成功返回带标注的 png 路径；读图失败返回 None。"""
        im = QImage(path)
        if im.isNull():
            return None
        # 与拼接图一致：字号 ≈ 图宽 5.5%（最小 22px），条高 ≈ 图宽 10%（最小 44px），黑字 #111。
        font_px = max(22, int(im.width() * 0.055))
        text_h = max(44, int(im.width() * 0.10))
        canvas = QImage(im.width(), im.height() + text_h, QImage.Format_ARGB32)
        canvas.fill(Qt.white)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.drawImage(0, 0, im)
        painter.setPen(QPen(QColor("#64748b"), 1.5))
        painter.drawRect(0, 0, im.width(), im.height())
        font = QFont()
        font.setPixelSize(font_px)
        painter.setFont(font)
        name = self._ref_image_name(path)
        painter.setPen(QColor("#111111"))
        painter.drawText(QRect(2, im.height(), im.width() - 4, text_h),
                         Qt.AlignCenter, name)
        painter.end()
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            if canvas.save(out_path, "PNG"):
                return out_path
        except Exception:
            pass
        return None

    def _refs_for_send(self, paths):
        """发送前把参考图整理为 ≤5 张，且每一张（无论单图还是拼接图）都带上资产名标注，方便模型识别。
        ≤5 张：逐张加标注条后发送（拼接逻辑不变，单图也加标注）；
        >5 张：把超出部分按最优算法拼接合并（拼接图本身已含标注），单图组同样加标注。
        拼接/标注图写入一次性的临时目录（_new_merge_dir），用完由调用方 _rmtree_merge_dir 清理，
        避免 %TEMP% 长期堆积。返回 (merged_paths, tmp_dir)。
        说明：本方法只做离屏 QImage/QPainter 操作（不触碰任何 QWidget），因此既可在 UI 线程
        调用，也可在 _RefPrepWorker 子线程里调用（P1 用它实现后台整理）。"""
        paths = [p for p in paths if p and os.path.exists(p)]
        groups = self._plan_merge(paths)
        tmp_dir = _new_merge_dir()
        merged = []
        gi = 0
        for g in groups:
            gi += 1
            tag = int(time.time() * 1000) % 1000000
            # 无法创建临时目录时退回原图，仍保证能发送
            base_dir = tmp_dir if tmp_dir else None
            if len(g) == 1:
                if base_dir:
                    out = os.path.join(base_dir, "ref_lbl_%d_%d_%d.png" % (
                        os.getpid(), gi, tag))
                    res = self._single_label(g[0], out)
                    merged.append(res or g[0])   # 加标注失败则退回原图
                else:
                    merged.append(g[0])
            else:
                if base_dir:
                    out = os.path.join(base_dir, "ref_merge_%d_%d_%d.png" % (
                        os.getpid(), gi, tag))
                    res = self._merge_composite(g, out)
                    if res:
                        merged.append(res)
                    else:
                        # 拼接失败：逐张加标注后全部保留（理论上不会触发 >5，因 5 组封顶）
                        for j, pp in enumerate(g):
                            o2 = os.path.join(base_dir, "ref_lbl_%d_%d_%de.png" % (
                                os.getpid(), gi, tag, j))
                            merged.append(self._single_label(pp, o2) or pp)
                else:
                    for pp in g:
                        merged.append(pp)
        # 若个别组退回导致仍可能 >5（理论只在合成失败时），再补一层截断保护
        return merged[:5], tmp_dir

    def _cleanup_ref_tmp_dir_safe(self, d):
        """跨线程安全清理临时目录（仅供 _RefPrepWorker 异常分支使用）。"""
        if d:
            _rmtree_merge_dir(d)

    def _cleanup_ref_tmp(self):
        """CreateTaskWorker 已把参考图（拼接/标注 png）读取完毕，清理本次一次性临时目录。"""
        d = getattr(self, "_ref_tmp_dir", None)
        if d:
            _rmtree_merge_dir(d)
        self._ref_tmp_dir = None

    def _start(self):
        if self._creating or (self._poll and self._poll.isRunning()):
            return
        prompt = self.prompt.toPlainText().strip()
        g = (self._get_global() or "").strip()
        if g:
            prompt = (g + "\n\n" + prompt) if prompt else g
        # 自动从资产库填充参考图（若提示词提及资产名称）
        self._fill_assets_from_prompt()
        if not prompt:
            QMessageBox.warning(self, "提示", "请先输入生成提示词（或填写全局提示词）")
            return
        key = getattr(self, "_key", None) or _read_api_key()
        if not key:
            QMessageBox.warning(self, "提示", "请先在「⚙ API 设置」配置 Agnes API Key")
            self._log("未配置 Agnes API Key", "warn")
            return
        # 模型统一从「AI 服务 → 视频生成」配置读取（解耦：生成区不再单独选择模型）
        try:
            self._model = config.AGNES_VIDEO.get("model") or "agnes-video-2.5-flash"
        except Exception:
            self._model = "agnes-video-2.5-flash"
        # 画幅：用本区自己选的画幅。
        # 全局画幅在「修改全局」时已批量同步到各区作为默认，之后各区仍可单独改，
        # 因此生成时不再用全局值覆盖本区。
        _aspect = self.aspect.currentText()
        if self._poll and self._poll.isRunning():
            self._poll.stop()
            self._poll.wait(3000)
        self._poll = None
        self._task = None
        self._creating = True
        self.go.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("正在整理参考图…")
        self.meta.setText("")
        ref = [p for p in self._ref_imgs if os.path.exists(p)]
        # 极简直发模式（参考 agnes-ai-studio）：裸 prompt + images 数组，不注入模板前缀。
        # 模型根据图片顺序与 prompt 内容自行匹配，更简单不易错。
        sent_prompt = (prompt or "").strip()
        self._last_sent_prompt = sent_prompt
        # 保留本次发送参数到后台线程里，准备完成后回主线程再创建任务
        self._pending_ref_paths = ref          # 原始参考图（未标注）
        self._pending_sent_prompt = sent_prompt
        self._pending_aspect = _aspect
        if ref:
            self.status.setText("正在整理参考图…")
            # P1：把耗时较长的「读图 + 缩放 + QPainter 标注/拼接 + 写 PNG」放到后台线程，
            # 避免点生成瞬间卡住 UI
            self._ref_prep = _RefPrepWorker(self, ref, parent=self)
            self._ref_prep.done.connect(self._on_ref_prepared)
            self._ref_prep.fail.connect(self._on_ref_prep_fail)
            self._ref_prep.start()
        else:
            # 无参考图时跳过整理，直接进入创建流程
            self._on_ref_prepared([], "")

    def _on_ref_prep_fail(self, err):
        self._creating = False
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("参考图整理失败")
        self._log("参考图整理失败: %s" % err, "error")
        _tk = self._task if isinstance(self._task, dict) else {}
        self._on_task("failed", {"task_id": _tk.get("task_id") or "",
                                 "error": str(err),
                                 "elapsed": _tk.get("elapsed")})
        self.generated.emit(self, False)

    def _on_ref_prepared(self, merged, tmp_dir):
        """后台参考图整理完成：清理本次一次性临时目录，然后真正创建任务。"""
        self._ref_tmp_dir = tmp_dir  # 由 _on_created / _on_create_failed 在任务读取完图片后清理
        self._launch_create(merged)

    def _launch_create(self, ref):
        """后台整理完成后在 UI 线程发起 CreateTaskWorker。"""
        key = getattr(self, "_key", None) or _read_api_key()
        cfg = config.AGNES_VIDEO
        try:
            self._create_worker = CreateTaskWorker(
                api_key=key,
                base_url=cfg.get("base_url") or config.AGNES_VIDEO_DEFAULT_BASE,
                prompt=self._pending_sent_prompt,
                seconds=int(self.seconds.currentText()),
                aspect=self._pending_aspect,
                negative_prompt=self.negative.text().strip(),
                image_urls=ref,
                model=self._model,
                parent=self)
            self._create_worker.done.connect(self._on_created)
            self._create_worker.failed.connect(self._on_create_failed)
            self._create_worker.start()
            n_ref = len(ref)
            n_orig = len(self._pending_ref_paths)
            if n_orig > n_ref:
                self._log("本次参考图 %d 张原图 → 已合并为 %d 张（带标注）发送，任务创建中…" % (n_orig, n_ref))
            else:
                self._log("本次参考图 %d 张（已加标注），任务创建中…" % n_ref)
            self.status.setText("正在创建任务…")
        except Exception as e:
            self._creating = False
            self.go.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status.setText("启动生成失败")
            self._log("启动生成任务出错: %s" % e, "error")

    def _on_created(self, res):
        self._creating = False
        self._cleanup_ref_tmp()
        video_id = res.get("video_id") or ""
        task_id = res.get("task_id") or ""
        if not video_id:
            self._on_create_failed("创建任务响应缺少 video_id：" + json.dumps(
                res.get("raw", {}), ensure_ascii=False)[:300])
            return
        self._task = res
        self.status.setText("任务已创建，等待生成…")
        self._log("Agnes 视频任务已创建 video_id=%s task_id=%s" % (video_id, task_id), "info")

        rec = {"task_id": task_id, "video_id": video_id,
               "prompt": getattr(self, "_last_sent_prompt", "") or self.prompt.toPlainText().strip(),
               "negative_prompt": self.negative.text().strip(),
               "seconds": self.seconds.currentText(),
               "aspect_ratio": self.aspect.currentText(),
               "status": "pending", "video_url": "", "saved_path": "",
               "episode": self.index + 1,
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._insert_history(rec)

        cfg = config.AGNES_VIDEO
        base_url = cfg.get("base_url") or config.AGNES_VIDEO_DEFAULT_BASE
        # TokenPlan 模式下视频生成耗时较长，创建后超过 60s 才轮询一次状态，避免高频拉取
        if getattr(config, "is_tokenplan_mode", lambda: False)():
            poll_interval = 60.0
        else:
            poll_interval = float(cfg.get("interval") or 2.0)
        # 轮询总超时：视频越长/画质越高耗时越久，给足缓冲；超时必须兜底停线程，避免僵尸轮询
        _secs = int(self.seconds.currentText() or 10)
        poll_timeout = max(1200, _secs * 120)
        self._poll = PollWorker(config.get_tokenplan_key_round_robin()
                                if getattr(config, "is_tokenplan_mode", lambda: False)()
                                else cfg.get("api_key", ""),
                                base_url, video_id,
                                interval=poll_interval,
                                model=self._model, parent=self,
                                started_at=rec.get("created_at") or None,
                                timeout_sec=poll_timeout)
        self._poll.progress.connect(self._on_progress)
        self._poll.finished_ok.connect(self._on_done)
        self._poll.failed.connect(self._on_fail)
        self._poll.start()
        self._on_task("created", {"task_id": task_id, "video_id": video_id,
                                  "prompt": getattr(self, "_last_sent_prompt", "") or self.prompt.toPlainText().strip(),
                                  "model": self._model, "episode": self.index + 1})

    def _on_create_failed(self, err):
        self._creating = False
        self._cleanup_ref_tmp()
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("创建任务失败")
        self._log("视频生成创建失败: %s" % err, "error")
        self._last_error = str(err)  # 供外部检测 429
        if self._task:
            self._update_history_status("failed", str(err))
        else:
            rec = {"task_id": "", "video_id": "",
                   "prompt": getattr(self, "_last_sent_prompt", "") or self.prompt.toPlainText().strip(),
                   "negative_prompt": self.negative.text().strip(),
                   "seconds": self.seconds.currentText(),
                   "aspect_ratio": self.aspect.currentText(),
                   "status": "failed", "video_url": "", "saved_path": "",
                   "error": str(err), "episode": self.index + 1,
                   "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._insert_history(rec)
        _tk = self._task if isinstance(self._task, dict) else {}
        self._on_task("failed", {"task_id": _tk.get("task_id") or "",
                                 "error": str(err),
                                 "elapsed": _tk.get("elapsed")})
        self.generated.emit(self, False)

    def _on_progress(self, st):
        prog = st.get("progress")
        # 进度值没变就完全跳过（不刷新状态文字/历史/任务面板），降低每 tick 无谓开销
        if getattr(self, "_last_prog", None) == prog:
            return
        self._last_prog = prog
        txt = "生成中…"
        if prog is not None:
            txt += " %s" % prog
        self.status.setText(txt)
        self._update_history_status("processing", "", prog)
        if self._task:
            self._on_task("progress", {"task_id": self._task.get("task_id") or "",
                                       "status": "processing", "progress": prog})

    def _on_done(self, st):
        url = st.get("video_url") or ""
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if not url:
            self._on_fail("任务完成但响应中未找到视频地址")
            return
        self._latest_url = url
        self._gen_done_key = self._gen_done_marker()  # 记录成功生成的参数快照，供批量跳过判断
        self.status.setText("✅ 生成完成")
        self.meta.setText(url[:80])
        self._update_history_status("completed", "", "", url)
        self._log("视频生成完成，开始保存到本地…", "ok")
        task_id = self._task.get("task_id") or "" if self._task else ""
        video_id = self._task.get("video_id") or "" if self._task else ""
        # 立即通知批量/任务面板生成已完成（不等待下载）
        self._on_task("completed", {"task_id": task_id, "video_id": video_id,
                                    "video_url": url,
                                    "local_file": "",
                                    "elapsed": st.get("elapsed")})
        self.generated.emit(self, True)
        # 后台异步保存视频（不阻塞 UI）
        self._save_thread = _SaveVideoThread(url, self._get_save_dir(),
                                             "分镜%d" % (self.index + 1), parent=self)
        self._save_thread.done.connect(self._on_save_done)
        self._save_thread.fail.connect(self._on_save_fail)
        self._save_thread.start()

    def _gen_done_marker(self):
        """当前参数快照：提示词+画幅+时长+模型。参数未变时与 _gen_done_key 相等即视为「已生成」。"""
        return "|".join([self.prompt.toPlainText().strip(),
                         self.aspect.currentText(),
                         self.seconds.currentText(),
                         self._model])

    def _on_fail(self, err):
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("生成失败")
        self._gen_done_key = ""  # 失败后清除已生成标记，下一轮批量允许重试
        self._log("视频生成失败: %s" % err, "error")
        self._update_history_status("failed", str(err))
        self._on_task("failed", {"task_id": self._task.get("task_id") or "" if self._task else "",
                                 "error": str(err),
                                 "elapsed": self._elapsed_for_history()})
        self.generated.emit(self, False)

    def _on_save_done(self, path):
        """后台下载完成：更新本地路径并刷新侧栏列表。"""
        self._latest_path = path
        self.meta.setText(os.path.basename(path))
        self._log("已保存到本地: %s" % path, "ok")
        self._on_saved(path)
        # 同步更新任务记录中的本地文件路径
        task_id = self._task.get("task_id") or "" if self._task else ""
        video_id = self._task.get("video_id") or "" if self._task else ""
        parent = getattr(self, "parent_widget", None)
        if parent and hasattr(parent, "_gentasks"):
            for key, rec in list(parent._gentasks.items()):
                if (key == task_id or rec.get("task_id") == task_id or
                        key == video_id or rec.get("video_id") == video_id):
                    rec["local_file"] = path
                    break
            parent._gen_tasks_render()
            parent._gen_files_refresh()
            # 持久化本地路径，避免重启后任务记录丢失导致无法播放
            try:
                if hasattr(parent, "_gen_tasks_save"):
                    parent._gen_tasks_save()
            except Exception:
                pass

    def _on_save_fail(self, err):
        self._log("自动保存本地失败: %s" % err, "error")
        import traceback
        self._log(traceback.format_exc(), "error")

    def _elapsed_for_history(self):
        """计算已耗费时间，供任务面板显示。"""
        if not getattr(self, "_task", None):
            return None
        started = getattr(self._task, "get", lambda k, default=None: default)("started_at", "")
        if not started:
            return None
        try:
            t0 = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
            s = int(time.time() - t0)
            if s < 60:
                return "%ds" % s
            m = s // 60
            return "%dm%02ds" % (m, s % 60)
        except Exception:
            return None

    def _stop(self):
        if self._poll and self._poll.isRunning():
            self._poll.stop()
            self._poll.wait(3000)
        self._creating = False
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("已停止")