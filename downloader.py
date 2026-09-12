"""
红果短视频下载器 - 核心下载模块
"""
import os
import re
import json
import logging
import urllib.parse
from datetime import datetime
import requests
from m3u8_downloader import M3U8Downloader
import config

logger = logging.getLogger(__name__)


class HongguoDownloader:
    """红果短视频下载器"""

    def __init__(self, api_key=None):
        self.api_key = api_key or config.API_KEY
        self.session = requests.Session()
        self.session.headers.update(config.DEFAULT_HEADERS)
        self.m3u8_downloader = M3U8Downloader(
            headers=config.DEFAULT_HEADERS,
            max_workers=config.MAX_CONCURRENT_DOWNLOADS,
            timeout=config.REQUEST_TIMEOUT
        )
        self._video_info_cache = {}

    def close(self):
        """清理资源"""
        self.m3u8_downloader.close()
        self.session.close()

    # 红果/番茄分享短链域名，需要跟随重定向解析
    SHARE_DOMAINS = (
        'novelquickapp.com', 'hongguo.com', 's.hongguo.com',
        've.17c.com', 'fanqienovel.com', 'hongguo.top',
    )

    # 视频 ID 的提取模式（优先 video_id / vid / video_series_id）
    _ID_PATTERNS = [
        r'video_id"?\s*[:=]\s*"?["\\]*(\d{15,})',
        r'video_series_id"?\s*[:=]\s*"?["\\]*(\d{15,})',
        r'vid"?\s*[:=]\s*"?["\\]*(\d{15,})',
        r'id"?\s*[:=]\s*"?["\\]*(\d{15,})',
        r'[?&]v=(\d{15,})',
        r'/(\d{15,})',
    ]

    def parse_url(self, url):
        """
        解析红果视频 URL，提取视频 ID

        支持的 URL 格式:
        - https://www.hongguo.com/watch?v=xxxxx
        - https://novelquickapp.com/s/xxxxx （红果/番茄分享短链）
        - 阿里/抖音分享短链
        - 纯视频 ID
        """
        if not url or not url.strip():
            return None

        url = url.strip()

        # 纯数字 ID
        if re.match(r'^\d+$', url):
            return url

        # 先从原始 URL 中提取 ID
        decoded = self._decode_query(url)
        extracted = self._extract_video_id(decoded)
        if extracted:
            return extracted

        # 短链接：跟随重定向到最终 URL 再解析
        if self._is_share_url(url):
            try:
                resp = self.session.get(url, allow_redirects=True, timeout=15,
                                        headers=config.DEFAULT_HEADERS)
                final_url = resp.url
                logger.info(f"短链重定向到: {final_url[:120]}...")

                decoded = self._decode_query(final_url)
                extracted = self._extract_video_id(decoded)
                if extracted:
                    return extracted

                # 兜底：从重定向 URL 常规提取
                for pattern in self._ID_PATTERNS:
                    match = re.search(pattern, final_url)
                    if match:
                        possible = match.group(1)
                        # 排除明显是 token/uid 的字段
                        if len(possible) >= 15 and possible.isdigit():
                            return possible
            except Exception as e:
                logger.warning(f"短链接解析失败: {e}")

        return None

    def _is_share_url(self, url):
        """判断是否为红果/番茄分享短链"""
        lower = url.lower()
        if '/s/' in lower:
            return True
        return any(domain in lower for domain in self.SHARE_DOMAINS)

    def _decode_query(self, url):
        """递归解码 URL 参数值，提取所有候选 ID 字段"""
        from urllib.parse import urlparse, parse_qs, unquote

        candidates = []
        # 顶层 query
        try:
            qs = parse_qs(urlparse(url).query)
            for key in ('zlink', 'url', 'share_url', 'link', 'redirect', 'data'):
                for val in qs.get(key, []):
                    candidates.append(val)
        except Exception:
            pass

        # 递归解编码并把所有 URL 片段纳入搜索文本
        searchable = url
        for _ in range(5):
            decoded = unquote(searchable)
            if decoded == searchable:
                break
            searchable = decoded
            candidates.append(decoded)

        return "\n".join(candidates) + "\n" + url

    def _extract_video_id(self, text):
        """从文本中提取视频 ID（15 位以上纯数字）"""
        for pattern in self._ID_PATTERNS:
            for match in re.finditer(pattern, text):
                candidate = match.group(1).split('\"')[0] if match.group(1) else ''
                if not candidate:
                    continue
                # 去掉可能残存的转义/引号/百分号
                candidate = re.sub(r'[\\"%]', '', candidate)
                if len(candidate) >= 15 and candidate.isdigit():
                    return candidate
        return None

    def _resolve_via_provider(self, video_id):
        """调用第三方解析服务，将视频 ID 解析为真实播放地址"""
        cfg = config.PARSE_PROVIDER
        if not cfg.get("enabled"):
            return None

        key = (cfg.get("key") or "").strip()
        if not key:
            logger.error("解析服务已启用但未填写 API Key")
            return None

        template = (cfg.get("url_template") or "").strip()
        if not template:
            logger.error("解析服务未配置 API 地址模板")
            return None

        method = (cfg.get("method") or "GET").upper()
        result_field = (cfg.get("result_field") or "data.play_url").strip()

        url = template.replace("{video_id}", video_id).replace("{key}", key)
        try:
            if method == "POST":
                resp = self.session.post(
                    url, data={cfg.get("key_param", "key"): key},
                    timeout=config.REQUEST_TIMEOUT
                )
            else:
                resp = self.session.get(url, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            play_url = self._nested_get(data, result_field)
            if play_url:
                logger.info(f"第三方解析成功: {str(play_url)[:80]}...")
                return play_url
            logger.error(f"第三方解析未返回播放地址，响应: {str(data)[:200]}")
        except Exception as e:
            logger.error(f"第三方解析失败: {e}")
        return None

    @staticmethod
    def _nested_get(obj, path):
        """按点路径从嵌套 JSON 取字段值"""
        cur = obj
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except Exception:
                    return None
            else:
                return None
            if cur is None:
                return None
        return cur if isinstance(cur, str) else None

    def get_video_info(self, video_id):
        """
        获取视频信息

        Returns:
            dict: 包含 title, description, author, download_url 等信息
        """
        if video_id in self._video_info_cache:
            return self._video_info_cache[video_id]

        info = {
            "id": video_id,
            "title": f"红果视频_{video_id}",
            "description": "",
            "author": "",
            "cover": "",
            "duration": 0,
            "download_url": None,
            "m3u8_url": None,
            "raw_data": None,
        }

        try:
            # 尝试通过 API 获取视频信息
            if self.api_key:
                api_url = f"{config.API_BASE_URL}/video"
                params = {
                    "apikey": self.api_key,
                    "video_id": video_id,
                }
                resp = self.session.get(api_url, params=params, timeout=config.REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                info["raw_data"] = data

                if data.get("code") == 200 and data.get("data"):
                    video_data = data["data"]
                    info["title"] = video_data.get("title", info["title"])
                    info["description"] = video_data.get("description", "")
                    info["author"] = video_data.get("author", "")
                    info["cover"] = video_data.get("cover", "")
                    info["duration"] = video_data.get("duration", 0)
                    info["download_url"] = video_data.get("play_url") or video_data.get("download_url")
                    info["m3u8_url"] = video_data.get("m3u8_url")
            else:
                # 无 API Key 时尝试直接请求
                info = self._fetch_via_web(video_id, info)

            self._video_info_cache[video_id] = info
            return info

        except Exception as e:
            logger.error(f"获取视频信息失败: {e}")
            return info

    def _fetch_via_web(self, video_id, info):
        """通过网页方式获取视频信息（备用方案）"""
        try:
            # 尝试通过红果分享页获取
            search_url = f"https://www.hongguo.com/search?keyword={urllib.parse.quote(video_id)}"
            resp = self.session.get(search_url, timeout=config.REQUEST_TIMEOUT)

            # 从页面中提取视频信息
            content = resp.text
            video_url_match = re.search(r'"playUrl"\s*:\s*"([^"]+)"', content)
            if video_url_match:
                info["download_url"] = video_url_match.group(1)

            m3u8_match = re.search(r'"m3u8"\s*:\s*"([^"]+)"', content)
            if m3u8_match:
                info["m3u8_url"] = m3u8_match.group(1)

            title_match = re.search(r'"title"\s*:\s*"([^"]+)"', content)
            if title_match:
                info["title"] = title_match.group(1)

        except Exception as e:
            logger.warning(f"网页方式获取失败: {e}")

        return info

    def download_video(self, video_id, output_dir=None, progress_callback=None, naming=None):
        """
        下载视频

        Args:
            video_id: 视频 ID
            output_dir: 输出目录
            progress_callback: 进度回调函数 (message, current, total)
            naming: 文件名命名规则（见 config.NAME_RULE），None 用配置默认值

        Returns:
            str: 下载文件的完整路径，失败返回 None
        """
        if output_dir is None:
            output_dir = config.DEFAULT_DOWNLOAD_PATH

        os.makedirs(output_dir, exist_ok=True)

        # 获取视频信息
        info = self.get_video_info(video_id)
        if not info:
            logger.error("无法获取视频信息")
            return None

        # 确定下载 URL（本地拿不到时尝试第三方解析服务）
        download_url = info.get("download_url") or info.get("m3u8_url")
        if not download_url:
            provider_url = self._resolve_via_provider(video_id)
            if provider_url:
                download_url = provider_url
                info["m3u8_url"] = provider_url
            else:
                enabled = config.PARSE_PROVIDER.get("enabled")
                logger.error(
                    "未能获取视频播放地址。"
                    + ("第三方解析也未返回地址，请检查 Key/接口配置或更换服务商。"
                       if enabled else
                       "该视频需经第三方解析才能取得播放地址：请在 Web 界面『API 解析设置』中启用并填写 Key。")
                )
                return None

        # 构建文件名（按命名规则）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", (info.get("title") or "").strip())
        safe_id = re.sub(r'[\\/:*?"<>|]', "_", str(video_id))
        rule = naming or getattr(config, "NAME_RULE", "title_ts")

        if rule == "title":
            base_name = safe_title or safe_id
        elif rule == "id_ts":
            base_name = f"{safe_id}_{timestamp}"
        elif rule == "ts":
            base_name = f"视频_{timestamp}"
        else:  # title_ts
            head = safe_title or safe_id
            base_name = f"{head}_{timestamp}"

        filename = f"{base_name}.mp4"
        output_path = os.path.join(output_dir, filename)

        # 下载视频
        try:
            if info.get("m3u8_url") or self._is_m3u8(download_url):
                # M3U8 流下载
                if progress_callback:
                    progress_callback("正在解析 M3U8 流...", 0, 100, os.path.basename(output_path))

                result = self.m3u8_downloader.download(
                    download_url, output_path,
                    progress_callback=progress_callback
                )
            else:
                # 直接 MP4 下载
                if progress_callback:
                    progress_callback("正在下载视频...", 0, 100, os.path.basename(output_path))

                result = self._download_direct(download_url, output_path, progress_callback)

            if result:
                logger.info(f"视频下载成功: {result}")
                return result
            else:
                logger.error("视频下载失败")
                return None

        except Exception as e:
            logger.error(f"下载异常: {e}")
            return None

    def _is_m3u8(self, url):
        """检测 URL 是否为 M3U8 流"""
        return ".m3u8" in url or url.endswith(".m3u8")

    def _download_direct(self, url, output_path, progress_callback=None):
        """直接下载 MP4 视频"""
        try:
            resp = self.session.get(url, stream=True, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()

            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        if progress_callback and total_size > 0:
                            percent = int(downloaded * 100 / total_size)
                            progress_callback(
                                f"下载中... {percent}%",
                                downloaded, total_size, os.path.basename(output_path)
                            )
                        elif progress_callback:
                            progress_callback(
                                f"下载中... {downloaded // 1024 // 1024}MB",
                                downloaded, 0, os.path.basename(output_path)
                            )

            logger.info(f"下载完成: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"直接下载失败: {e}")
            return None

    def batch_download(self, video_ids, output_dir=None, progress_callback=None):
        """
        批量下载视频

        Args:
            video_ids: 视频 ID 列表
            output_dir: 输出目录
            progress_callback: 进度回调函数 (message, current, total)

        Returns:
            list: 成功下载的文件路径列表
        """
        results = []
        total = len(video_ids)

        for i, video_id in enumerate(video_ids):
            if progress_callback:
                progress_callback(f"正在处理第 {i + 1}/{total} 个视频...", i, total, "")

            result = self.download_video(video_id, output_dir, progress_callback)
            if result:
                results.append(result)

        return results
