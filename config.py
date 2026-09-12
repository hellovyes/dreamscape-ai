"""
红果短视频下载器 - 配置模块
"""
import os
import sys
import json
import copy

# 配置/日志文件放在程序(exe或源码)所在目录，便于持久化
_EXE_DIR = (os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_EXE_DIR, "api_config.json")

# ---- 内置解析服务商预设 ----
# url_template 中的 {key} / {video_id} / {poster} 会被运行时替换。
# result_field 是 JSON 响应中"真实播放地址(m3u8/mp4)"所在字段的路径，用 . 分隔。
PROVIDER_PRESETS = {
    "custom": {
        "name": "自定义解析服务",
        "url_template": "",
        "method": "GET",
        "key_param": "key",
        "result_field": "data.play_url",
    },
    "hgduanju": {
        "name": "红果短剧(hgduanju.com)",
        "url_template": "https://api.hgduanju.com/video?apikey={key}&video_id={video_id}",
        "method": "GET",
        "key_param": "apikey",
        "result_field": "data.play_url",
    },
    "api52": {
        "name": "我爱API(52api.cn) 红果短剧",
        "url_template": "https://www.52api.cn/api/hg_duanju?key={key}&video_id={video_id}&type=video",
        "method": "GET",
        "key_param": "key",
        "result_field": "data.play_url",
    },
}

# 当前激活的解析服务配置（可被 Web 界面修改并持久化）
PARSE_PROVIDER = {
    "enabled": False,          # 是否启用第三方解析
    "provider": "custom",      # 服务商标识：custom / hgduanju / api52
    "key": "",                 # 你的 API Key
    "url_template": "",        # 自定义 API 地址模板（含 {video_id}）
    "method": "GET",           # GET / POST
    "key_param": "key",        # Key 对应的请求参数名
    "result_field": "data.play_url",  # 播放地址在响应 JSON 中的字段路径
}

# 兼容旧配置：简易 API Key（服务商 hgduanju）
API_BASE_URL = "https://api.hgduanju.com"
API_KEY = ""

# ---- GLM 视频分析服务（智谱 BigModel：GLM-5.3 / GLM-5.3-Flash）----
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_AI = {
    "enabled": False,
    "api_key": "",
    "model": "glm-5.3-flash",     # glm-5.3-flash（多模态推荐） / glm-5.3（旗舰）
    "base_url": GLM_BASE_URL,
}

_GLM_MODELS = ("glm-5.3-flash", "glm-5.3", "agnes-2.5-flash", "agnes-2.5-flash-cn")  # 支持的视频分析模型

# 各模型默认接口地址（选择模型时自动切换对应服务商 Base URL）
_PROVIDER_BASE_URLS = {
    "glm-5.3-flash": GLM_BASE_URL,
    "glm-5.3": GLM_BASE_URL,
    "agnes-2.5-flash": "https://apihub.agnes-ai.com/v1/chat/completions",
    "agnes-2.5-flash-cn": "https://api.agnes-ai.cn/v1/chat/completions",
    "agnes-3.0-flash": "https://apihub.agnes-ai.com/v1/chat/completions",
    "agnes-3.0-flash-cn": "https://api.agnes-ai.cn/v1/chat/completions",
}


def base_url_for(model):
    """返回指定模型的默认接口地址"""
    return _PROVIDER_BASE_URLS.get(model, GLM_BASE_URL)


def _clean_api_key(raw):
    """清洗 API Key：只保留可见 ASCII 字符，过滤中文/文档文字/控制符/空白。
    防止误粘贴的无关文本进入 Authorization 头导致 latin-1 编码错误。"""
    if not raw:
        return ""
    out = []
    for ch in str(raw):
        code = ord(ch)
        # 保留可见 ASCII（32-126）——API Key 只应含字母数字与少量符号
        if 33 <= code <= 126:
            out.append(ch)
    return "".join(out)


def load_glm_config():
    """从磁盘加载 GLM 分析服务配置"""
    global GLM_AI
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            glm = saved.get("glm", {}) or {}
            merged = copy.deepcopy(GLM_AI)
            merged.update({k: v for k, v in glm.items() if k in GLM_AI})
            if not merged.get("base_url"):
                merged["base_url"] = GLM_BASE_URL
            # 模型必须是已支持的合法值，否则回退默认，防止持久化的 false 等脏值生效
            if merged.get("model") not in _GLM_MODELS:
                merged["model"] = GLM_AI["model"]
            # API Key 清洗：过滤中文/文档文字等脏值，防 latin-1 编码崩溃
            merged["api_key"] = _clean_api_key(merged.get("api_key"))
            GLM_AI = merged
    except Exception:
        pass
    return GLM_AI


# ---- Agnes Video 2.5 Flash 视频生成服务 ----
AGNES_VIDEO_MODEL = "agnes-video-2.5-flash"
AGNES_VIDEO_DEFAULT_BASE = "https://apihub.agnes-ai.com/v1"   # 海外（默认，与视频分析 Agnes 海外站同域）
AGNES_VIDEO_CN_BASE = "https://api.agnes-ai.cn/v1"            # 中国站（与视频分析 Agnes 中国站同域）
# 服务区域预设：(id, 展示名)。切换区域时自动填充对应 Base URL，模型名保持不变，仅端点域名切换。
AGNES_VIDEO_REGIONS = (
    ("overseas", "海外（apihub.agnes-ai.com）"),
    ("cn", "中国（api.agnes-ai.cn）"),
)
AGNES_VIDEO_REGIONS_ID = [r[0] for r in AGNES_VIDEO_REGIONS]
AGNES_VIDEO = {
    "enabled": False,
    "api_key": "",
    "api_keys": [],          # 多 Key 列表（轮询用）
    "tokenplan_keys": [],    # Token Plan 多 Key 列表（每 Key 5 RPM，一键生成按 k*5 满速并发、60s 轮询）
    "tp_enabled": False,     # TokenPlan 模式总开关（需手动勾选启用）
    "base_url": AGNES_VIDEO_DEFAULT_BASE,
    "model": AGNES_VIDEO_MODEL,
    "region": "overseas",    # 服务区域：overseas(海外·apihub.agnes-ai.com) / cn(中国·api.agnes-ai.cn)
    "interval": 2.0,          # 轮询间隔（秒）
    "retry_wait_sec": 60,     # 限速重试等待秒数
    # 分站点 Key 存储：{region: {"api_keys": [...], "tokenplan_keys": [...]}}
    # 海外与中国站的 Key 不通用，各自独立保存；切换站点时输入框展示对应站点已填写的 Key。
    "keys_by_region": {},
}


def agnes_video_base_url(region=None):
    """按服务区域返回 Agnes Video 生成 Base URL（海外 apihub.agnes-ai.com / 中国 api.agnes-ai.cn）。
    region 为空时读取当前配置；无论哪个区域，模型名保持不变，仅切换端点域名。"""
    r = (region or AGNES_VIDEO.get("region") or "overseas").strip().lower()
    if r in ("cn", "china", "中国"):
        return AGNES_VIDEO_CN_BASE
    return AGNES_VIDEO_DEFAULT_BASE


def _region_norm(region):
    """归一化站点 id → 存储键（overseas / cn）。"""
    r = (region or "overseas").strip().lower()
    if r in ("cn", "china", "中国"):
        return "cn"
    return "overseas"


def agnes_video_region_keys(region=None):
    """返回某站点已保存的 {api_keys, tokenplan_keys}。
    优先读 keys_by_region；若该站点从未保存过且就是当前活动站点，回退当前平铺字段（兼容旧配置迁移）。"""
    r = _region_norm(region)
    rb = AGNES_VIDEO.get("keys_by_region") or {}
    obj = rb.get(r) or {}
    if not isinstance(obj, dict):
        obj = {}
    api = obj.get("api_keys") or []
    tp = obj.get("tokenplan_keys") or []
    # 兼容迁移：目标站点从未存过、且是当前 region，用平铺字段兜底一次
    cur = _region_norm(AGNES_VIDEO.get("region"))
    if (not api or not tp) and r == cur:
        flat_api = AGNES_VIDEO.get("api_keys") or []
        flat_tp = AGNES_VIDEO.get("tokenplan_keys") or []
        if not api and flat_api:
            api = list(flat_api)
        if not tp and flat_tp:
            tp = list(flat_tp)
    return {"api_keys": list(api), "tokenplan_keys": list(tp)}


def agnes_video_store_region_keys(region, api_keys, tokenplan_keys):
    """把某站点填写的普通 Keys 与 TokenPlan Keys 写入 keys_by_region（内存）。"""
    r = _region_norm(region)
    rb = dict(AGNES_VIDEO.get("keys_by_region") or {})
    rb[r] = {"api_keys": list(api_keys or []), "tokenplan_keys": list(tokenplan_keys or [])}
    AGNES_VIDEO["keys_by_region"] = rb

# 视频生成：可选时长（秒），2~12 选填（覆盖分镜文件的 2s/3s/5s…）
VIDEO_SECONDS = [str(i) for i in range(2, 13)]

# 视频生成：可选 Agnes 视频模型（仅 Agnes 系）
VIDEO_GEN_MODELS = ["agnes-video-2.5-flash", "agnes-video-2.5"]


def load_agnes_video_config():
    """从磁盘加载 Agnes 视频生成服务配置（并入 api_config.json 的 agnes_video 键）"""
    global AGNES_VIDEO
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            ag = saved.get("agnes_video", {}) or {}
            merged = copy.deepcopy(AGNES_VIDEO)
            merged.update({k: v for k, v in ag.items() if k in AGNES_VIDEO})
            # region 归一化：未知值回退默认（海外），防止脏值生效
            if str(merged.get("region") or "overseas").strip().lower() not in AGNES_VIDEO_REGIONS_ID:
                merged["region"] = AGNES_VIDEO["region"]
            if not merged.get("base_url"):
                merged["base_url"] = AGNES_VIDEO_DEFAULT_BASE
            AGNES_VIDEO = merged
    except Exception:
        pass
    return AGNES_VIDEO


def save_agnes_video_config(cfg):
    """保存 Agnes 视频生成服务配置到磁盘（并入 api_config.json）"""
    global AGNES_VIDEO
    merged = copy.deepcopy(AGNES_VIDEO)
    for k, v in cfg.items():
        if k in AGNES_VIDEO:
            merged[k] = v
    # region 归一化：未知值回退海外
    if str(merged.get("region") or "overseas").strip().lower() not in AGNES_VIDEO_REGIONS_ID:
        merged["region"] = "overseas"
    if not merged.get("base_url"):
        merged["base_url"] = AGNES_VIDEO_DEFAULT_BASE
    if merged.get("api_key"):
        merged["enabled"] = True
    AGNES_VIDEO = merged
    try:
        existing = {}
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
        existing["agnes_video"] = merged
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return AGNES_VIDEO


def save_glm_config(cfg):
    """保存 GLM 分析服务配置到磁盘（并入 api_config.json）"""
    global GLM_AI
    cfg = dict(cfg)
    if "api_key" in cfg:
        cfg["api_key"] = _clean_api_key(cfg.get("api_key"))
    merged = copy.deepcopy(GLM_AI)
    for k, v in cfg.items():
        if k in GLM_AI:
            merged[k] = v
    if not merged.get("base_url"):
        merged["base_url"] = GLM_BASE_URL
    GLM_AI = merged
    try:
        existing = {}
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
        existing["glm"] = merged
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return GLM_AI


# ---- 本地分镜：分段模式 + 段头格式预设（每行一条大白话，类似 API Keys 多行填写）----
SEGMENT_SPLIT_MODE = "hdr"                    # "hdr"=按段头格式 / "ai"=AI 分段（LLM）
SEGMENT_HEADER_PRESETS = ["镜头数字", "Scene编号", "分镜", "视频编号", "Shot数字"]


def load_segment_split_config():
    """从磁盘加载分段模式与段头格式预设（并入 api_config.json 的 segment_split 键）"""
    global SEGMENT_SPLIT_MODE, SEGMENT_HEADER_PRESETS
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            seg = saved.get("segment_split", {}) or {}
            if seg.get("mode") in ("hdr", "ai"):
                SEGMENT_SPLIT_MODE = seg["mode"]
            if seg.get("headers") is not None:
                hs = [str(t).strip() for t in seg["headers"] if str(t).strip()]
                SEGMENT_HEADER_PRESETS = hs
    except Exception:
        pass
    return SEGMENT_SPLIT_MODE


def save_segment_split_config(cfg):
    """保存分段模式与段头格式预设到磁盘（并入 api_config.json）"""
    global SEGMENT_SPLIT_MODE, SEGMENT_HEADER_PRESETS
    if cfg.get("mode") in ("hdr", "ai"):
        SEGMENT_SPLIT_MODE = cfg["mode"]
    if "headers" in cfg:
        SEGMENT_HEADER_PRESETS = [str(t).strip() for t in cfg["headers"] if str(t).strip()]
    merged = {"mode": SEGMENT_SPLIT_MODE, "headers": SEGMENT_HEADER_PRESETS}
    try:
        existing = {}
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
        existing["segment_split"] = merged
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return merged


# ---- 资产生成图服务（OpenAI 兼容 /images/generations：智谱 CogView / Agnes / 任意兼容端点）----
IMAGE_GEN_DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4/images/generations"
IMAGE_GEN_PRESETS = {
    "glm": {
        "name": "智谱 CogView（推荐）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/images/generations",
        "model": "cogview-3-flash",
    },
    "agnes": {
        "name": "Agnes 图像模型",
        "base_url": "https://apihub.agnes-ai.com/v1/images/generations",
        "model": "agnes-image-2.5-flash",
    },
    "custom": {
        "name": "自定义 OpenAI 兼容服务",
        "base_url": "",
        "model": "",
    },
}
IMAGE_GEN_SIZES = ["1024x1024", "512x512", "768x512", "512x768", "768x1024", "1024x768"]
IMAGE_GEN = {
    "enabled": False,
    "api_key": "",
    "base_url": IMAGE_GEN_DEFAULT_BASE,
    "model": "cogview-3-flash",
    "size": "1024x1024",
    "max_concurrent": 3,      # 资产生成图最大并发数
}


def load_image_gen_config():
    """从磁盘加载资产生成图服务配置（并入 api_config.json 的 image_gen 键）"""
    global IMAGE_GEN
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            ig = saved.get("image_gen", {}) or {}
            merged = copy.deepcopy(IMAGE_GEN)
            merged.update({k: v for k, v in ig.items() if k in IMAGE_GEN})
            if not merged.get("base_url"):
                merged["base_url"] = IMAGE_GEN_DEFAULT_BASE
            if not merged.get("model"):
                merged["model"] = IMAGE_GEN["model"]
            IMAGE_GEN = merged
    except Exception:
        pass
    return IMAGE_GEN


def save_image_gen_config(cfg):
    """保存资产生成图服务配置到磁盘（并入 api_config.json）"""
    global IMAGE_GEN
    merged = copy.deepcopy(IMAGE_GEN)
    for k, v in cfg.items():
        if k in IMAGE_GEN:
            merged[k] = v
    if not merged.get("base_url"):
        merged["base_url"] = IMAGE_GEN_DEFAULT_BASE
    if not merged.get("model"):
        merged["model"] = IMAGE_GEN["model"]
    if merged.get("api_key"):
        merged["enabled"] = True
    IMAGE_GEN = merged
    try:
        existing = {}
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        if not isinstance(existing, dict):
            existing = {}
        existing["image_gen"] = merged
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return IMAGE_GEN


def mask_api_key(key):
    """展示用脱敏：test123456 → test******456"""
    if not key:
        return ""
    if len(key) <= 8:
        return key[:3] + "******"
    return key[:4] + "******" + key[-4:]


# 多 Key 轮询索引（线程安全）
import threading as _threading
_agnes_key_lock = _threading.Lock()
_agnes_key_idx = 0


def get_api_key_round_robin():
    """从 api_keys 列表轮询获取下一个 Key；单 Key 时直接返回。"""
    global _agnes_key_idx
    keys = AGNES_VIDEO.get("api_keys") or []
    if not keys:
        return AGNES_VIDEO.get("api_key", "") or ""
    with _agnes_key_lock:
        idx = _agnes_key_idx % len(keys)
        _agnes_key_idx += 1
    return keys[idx]


def get_api_key_count():
    """返回当前配置的 Key 数量。"""
    keys = AGNES_VIDEO.get("api_keys") or []
    primary = AGNES_VIDEO.get("api_key", "")
    if keys:
        return len(keys)
    return 1 if primary else 0


# Token Plan 多 Key 轮询索引（线程安全）——独立于普通 Key 池
_tp_key_lock = _threading.Lock()
_tp_key_idx = 0


def is_tokenplan_mode():
    """是否配置了 Token Plan Key（视频生成 5 RPM/Key，一键生成按 k*5 并发、60s 轮询）"""
    return bool(AGNES_VIDEO.get("tp_enabled")) and bool(AGNES_VIDEO.get("tokenplan_keys"))


def get_tokenplan_key_count():
    """返回 Token Plan Key 数量。"""
    return len(AGNES_VIDEO.get("tokenplan_keys") or [])


def get_tokenplan_key_round_robin():
    """从 tokenplan_keys 列表轮询获取下一个 Key；仅一个时直接返回。"""
    global _tp_key_idx
    keys = AGNES_VIDEO.get("tokenplan_keys") or []
    if not keys:
        return AGNES_VIDEO.get("api_key", "") or ""
    with _tp_key_lock:
        idx = _tp_key_idx % len(keys)
        _tp_key_idx += 1
    return keys[idx] if len(keys) > 1 else keys[0]


# ---- 持久化（存到程序目录下 api_config.json）----
def load_api_config():
    """从磁盘加载解析服务配置，覆盖默认值"""
    global PARSE_PROVIDER, API_KEY
    try:
        if os.path.exists(_CONFIG_FILE):
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = copy.deepcopy(PARSE_PROVIDER)
            merged.update({k: v for k, v in saved.items() if k in PARSE_PROVIDER})
            PARSE_PROVIDER = merged
            API_KEY = PARSE_PROVIDER.get("key", "")
    except Exception:
        pass
    return PARSE_PROVIDER


def save_api_config(cfg):
    """保存解析服务配置到磁盘"""
    global PARSE_PROVIDER, API_KEY
    merged = copy.deepcopy(PARSE_PROVIDER)
    for k, v in cfg.items():
        if k in PARSE_PROVIDER:
            merged[k] = v
    # 启用服务商预设时，自动套用其地址模板与字段映射
    prov_key = merged.get("provider", "custom")
    preset = PROVIDER_PRESETS.get(prov_key)
    if preset:
        if not cfg.get("url_template"):
            merged["url_template"] = preset["url_template"]
        if not cfg.get("result_field"):
            merged["result_field"] = preset["result_field"]
        if not cfg.get("method"):
            merged["method"] = preset["method"]
        if not cfg.get("key_param"):
            merged["key_param"] = preset["key_param"]
    PARSE_PROVIDER = merged
    API_KEY = merged.get("key", "")
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(PARSE_PROVIDER, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return PARSE_PROVIDER

# 请求头配置
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.hongguo.com/",
}

# 下载配置
MAX_CONCURRENT_DOWNLOADS = 5  # 最大并发下载数
REQUEST_TIMEOUT = 30  # 请求超时(秒)
RETRY_COUNT = 3  # 重试次数
RETRY_DELAY = 2  # 重试间隔(秒)

# 默认下载路径
DEFAULT_DOWNLOAD_PATH = os.path.join(os.path.expanduser("~"), "Downloads", "红果视频")

# 保存文件名命名规则 (naming rule)
#   title_ts : 视频标题_时间戳  （解析不到标题时回退为 视频ID_时间戳）
#   title    : 视频标题
#   id_ts    : 视频ID_时间戳
#   ts       : 视频_时间戳
NAME_RULE = "title_ts"

# 应用配置
APP_NAME = "红果短视频下载器"
APP_VERSION = "1.1.1"
