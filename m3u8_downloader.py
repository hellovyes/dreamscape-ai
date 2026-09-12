"""
红果短视频下载器 - M3U8 流下载支持模块
"""
import os
import re
import threading
import logging
from urllib.parse import urljoin
import requests
import m3u8

logger = logging.getLogger(__name__)


class M3U8Downloader:
    """M3U8 流视频下载器"""

    def __init__(self, headers=None, max_workers=5, timeout=30):
        self.headers = headers or {}
        self.max_workers = max_workers
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(self.headers)
        self._stop_event = threading.Event()
        self._downloaded = 0
        self._total = 0

    def stop(self):
        """停止下载"""
        self._stop_event.set()

    def probe_all_m3u8(self, m3u8_urls):
        """探测多个 m3u8 URL，返回 (url, segments_count, total_duration_sec) 列表，按分片数降序排列。
        用于嗅探增强：从页面找到的多个候选 m3u8 中选出最长的那条（完整版而非预览版）。
        """
        import time
        results = []
        for url in m3u8_urls:
            try:
                obj = m3u8.load(url, timeout=self.timeout)
                if obj.playlists:
                    # master playlist → 沿最佳分辨率跟进
                    best = max(obj.playlists, key=lambda p: p.stream_info.bandwidth or 0)
                    sub_url = urljoin(url, best.uri)
                    obj = m3u8.load(sub_url, timeout=self.timeout)
                segs = obj.segments or []
                dur = sum(getattr(s, "duration", 0) or 0 for s in segs)
                results.append((url, len(segs), dur))
            except Exception:
                results.append((url, 0, 0))
        results.sort(key=lambda x: -x[1])
        return results

    def download(self, m3u8_url, output_path, progress_callback=None):
        """
        下载 M3U8 流视频

        Args:
            m3u8_url: M3U8 播放列表 URL
            output_path: 输出文件路径
            progress_callback: 进度回调函数 (message, current, total, filename)

        Returns:
            下载成功返回输出路径，失败返回 None
        """
        self._stop_event.clear()
        self._downloaded = 0
        self._total = 0

        try:
            # 解析 M3U8 列表
            m3u8_obj = m3u8.load(m3u8_url, timeout=self.timeout)

            if m3u8_obj.playlists:
                # 有多个清晰度，选择最高码率
                best_playlist = max(m3u8_obj.playlists, key=lambda p: p.stream_info.bandwidth or 0)
                media_playlist_url = urljoin(m3u8_url, best_playlist.uri)
                logger.info(f"选择清晰度: {best_playlist.stream_info.resolution or 'unknown'}")
            else:
                media_playlist_url = m3u8_url

            # 加载媒体播放列表
            media_obj = m3u8.load(media_playlist_url, timeout=self.timeout)
            segments = media_obj.segments

            if not segments:
                logger.error("未找到视频分段")
                return None

            # 处理加密
            key_info = media_obj.keys[0] if media_obj.keys else None

            # 获取输出目录
            output_dir = os.path.dirname(output_path)
            os.makedirs(output_dir, exist_ok=True)

            # 临时目录
            temp_dir = os.path.join(output_dir, ".tmp_hongguo")
            os.makedirs(temp_dir, exist_ok=True)

            self._total = len(segments)
            ts_files = []

            logger.info(f"开始下载 {len(segments)} 个视频分段...")

            # 串行下载（避免并发过多导致被封），每个分片最多重试 3 次
            for i, segment in enumerate(segments):
                if self._stop_event.is_set():
                    logger.info("下载已取消")
                    self._cleanup(temp_dir, ts_files)
                    return None

                seg_url = urljoin(media_playlist_url, segment.uri)
                ts_filename = f"seg_{i:05d}.ts"
                ts_path = os.path.join(temp_dir, ts_filename)
                ts_files.append(ts_path)

                content = None
                for attempt in range(1, 4):
                    if self._stop_event.is_set():
                        break
                    try:
                        resp = self._session.get(seg_url, timeout=self.timeout, stream=True)
                        if resp.status_code == 200 and resp.content:
                            content = resp.content
                            break
                        logger.warning(f"分段 {i} 下载失败: HTTP {resp.status_code}（第 {attempt} 次）")
                    except Exception as e:
                        logger.warning(f"分段 {i} 下载异常: {e}（第 {attempt} 次）")
                    time.sleep(1)
                if content is None:
                    logger.error(f"分段 {i} 连续 3 次下载失败，取消整个下载并清理临时文件")
                    self._cleanup(temp_dir, ts_files)
                    return None

                # 解密
                if key_info and key_info.method != "NONE":
                    content = self._decrypt_aes(content, key_info)

                with open(ts_path, "wb") as f:
                    f.write(content)
                self._downloaded += 1

                # 更新进度（统一回调签名：message, current, total, filename）
                if progress_callback:
                    progress = int(self._downloaded / max(self._total, 1) * 100)
                    progress_callback(f"下载分片 {ts_filename} ({progress}%)",
                                      self._downloaded, self._total, ts_filename)

            if self._stop_event.is_set():
                self._cleanup(temp_dir, ts_files)
                return None

            # 合并 TS 文件
            logger.info("合并视频分段...")
            with open(output_path, "wb") as outfile:
                for ts_path in ts_files:
                    if self._stop_event.is_set():
                        self._cleanup(temp_dir, ts_files)
                        return None
                    if os.path.exists(ts_path):
                        with open(ts_path, "rb") as infile:
                            outfile.write(infile.read())

            # 清理临时文件
            self._cleanup(temp_dir, ts_files)

            logger.info(f"下载完成: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"M3U8 下载失败: {e}")
            return None

    def _decrypt_aes(self, data, key_info):
        """AES-128-CBC 解密"""
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import unpad

            # 获取密钥
            key_url = urljoin(key_info.uri, key_info.key) if key_info.uri else key_info.uri
            key_resp = self._session.get(key_url, timeout=self.timeout)
            key = key_resp.content[:16]
            iv = bytes.fromhex(key_info.iv) if key_info.iv else key

            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(data)
            return unpad(decrypted, AES.block_size)
        except Exception as e:
            logger.warning(f"AES 解密失败，使用原始数据: {e}")
            return data

    def _cleanup(self, temp_dir, ts_files):
        """清理临时文件"""
        for ts_path in ts_files:
            try:
                if os.path.exists(ts_path):
                    os.remove(ts_path)
            except Exception:
                pass
        try:
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)
        except Exception:
            pass

    def close(self):
        """关闭会话"""
        self._session.close()
