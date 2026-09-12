# -*- coding: utf-8 -*-
"""本地 MITM 抓包代理：抓取第三方客户端/网页播放视频时的真实 m3u8 / mp4 / flv 地址。

原理：
- 内置一个 HTTP 代理服务器（默认 127.0.0.1:8899）。把第三方客户端/浏览器的手动代理
  或系统代理指向它即可。
- 对 http 请求直接转发并解析 URL；对 https（CONNECT）做中间人：按目标域名现场签发
  由自建 CA 签名的证书来完成 TLS 解密，然后读取解密后的 HTTP 请求，抓到 .m3u8/.mp4/.flv
  的完整地址回调给上层，再原样转发给上游服务器，保证视频照常播放。
- CA 只为“抓包”而加载，需一次性加入系统受信根证书库（CurrentUser，一般无需管理员）。

依赖：cryptography（签名证书）、ssl、socket。
"""
import os
import re
import ssl
import socket
import base64
import shutil
import socket as _socket
import threading
import tempfile
import subprocess

from urllib.parse import urlparse
from ipaddress import IPv4Address, IPv6Address

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend as _backend

_B = _backend()

MEDIA_RE = re.compile(r'\.(m3u8|mp4|flv)(\?|$)', re.I)
WANTED_RE = re.compile(r'\.m3u8(\?|$)', re.I)   # 播放列表最有用
_CTX_UP = None


def _ensure_ctx_up():
    global _CTX_UP
    if _CTX_UP is None:
        _CTX_UP = ssl.create_default_context()
        # 上游不校验证书（抓包场景下避免因中间人/过期证书失败；仅用于中转）
        _CTX_UP.check_hostname = False
        _CTX_UP.verify_mode = ssl.CERT_NONE
    return _CTX_UP


# ---------------- CA 与按域名签发的证书 ----------------
def ensure_ca(ca_key_path, ca_cert_path):
    """不存在则生成一张自签根 CA，返回 (key_pem, cert_pem)。"""
    if os.path.exists(ca_key_path) and os.path.exists(ca_cert_path):
        with open(ca_key_path, "rb") as f:
            key_pem = f.read()
        with open(ca_cert_path, "rb") as f:
            cert_pem = f.read()
        return key_pem, cert_pem
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=_B)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "HuGuo MITM CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HuGuo Capture"),
    ])
    now = _Stamp()
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now.add(-86400).to_dt())
            .not_valid_after(now.add(315360000).to_dt())
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_encipherment=True, key_cert_sign=True,
                key_agreement=False, content_commitment=False, data_encipherment=False,
                crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256(), _B))
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    try:
        with open(ca_key_path, "wb") as f:
            f.write(key_pem)
        with open(ca_cert_path, "wb") as f:
            f.write(cert_pem)
    except Exception:
        pass
    return key_pem, cert_pem


class _Stamp:
    """秒级时间戳包装，便于构造有效日期（cryptography 46 使用 datetime）。"""
    def __init__(self, ts=None):
        import time as _t
        self.v = ts if ts is not None else int(_t.time())

    def add(self, seconds):
        return _Stamp(self.v + seconds)

    def to_dt(self):
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self.v, _dt.timezone.utc)


class _CertCache:
    def __init__(self, ca_key, ca_cert):
        self._key = serialization.load_pem_private_key(ca_key, password=None, backend=_B)
        self._cert = x509.load_pem_x509_certificate(ca_cert, _B)
        self._lock = threading.Lock()
        self._map = {}
        # 用一张预置的 localhost 证书 + 按域名动态生成
        self._map_cache = {}

    def for_host(self, host):
        with self._lock:
            c = self._map.get(host)
            if c:
                return c
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HuGuo Capture")])
        now = _Stamp()
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(self._cert.issuer)
                .public_key(self._key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now.add(-2 * 86400).to_dt())
                .not_valid_after(now.add(365 * 86400).to_dt())
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
                .add_extension(x509.SubjectAlternativeName(
                    [x509.DNSName(host)] if not _is_ip(host) else
                    [x509.IPAddress(IPv4Address(host))]),
                    critical=False)
                .sign(self._key, hashes.SHA256(), _B))
        pem = cert.public_bytes(serialization.Encoding.PEM)
        key_rsa = self._key
        key_pem = key_rsa.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption())
        with self._lock:
            self._map.setdefault(host, (pem, key_pem))
        return pem, key_pem


def _is_ip(host):
    try:
        IPv4Address(host)
        return True
    except Exception:
        pass
    try:
        IPv6Address(host)
        return True
    except Exception:
        return False


# ---------------- 代理服务器 ----------------
class CapturingProxy:
    """抓包代理：单独一个监听 socket，线程处理每个连接。"""

    def __init__(self, host="127.0.0.1", port=8899, ca_key=None, ca_cert=None,
                 on_media=None, on_log=None):
        self.host = host
        self.port = port
        self.on_media = on_media or (lambda u: None)
        self.on_log = on_log or (lambda s: None)
        self._certs = _CertCache(ca_key, ca_cert)
        self._srv = None
        self._threads = []
        self._running = False
        self._conn_count = 0
        self._media_count = 0
        self._last_error = ""

    @property
    def status(self):
        return {
            "running": self._running,
            "port": self.port,
            "connections": self._conn_count,
            "media_found": self._media_count,
            "last_error": self._last_error,
        }

    # ---- 生命周期 ----
    def start(self):
        if self._running:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(128)
            s.settimeout(0.5)
            self._srv = s
            self.port = s.getsockname()[1]
            self._running = True
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self._threads.append(t)
            self.on_log(f"抓包代理已启动：{self.host}:{self.port}")
            return True
        except Exception as e:
            self._last_error = str(e)
            self.on_log(f"抓包代理启动失败：{e}")
            return False

    def stop(self):
        self._running = False
        # 代理退出时若系统代理仍指向本代理，强制恢复，避免网络状态残留
        if self._sys_proxy_on:
            try:
                set_system_proxy(False, self.port)
            except Exception:
                pass
            self._sys_proxy_on = False
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass

    def _loop(self):
        while self._running:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            self._conn_count += 1
            if self._conn_count <= 20:
                self.on_log(f"[代理] 收到第 {self._conn_count} 个连接")
            t = threading.Thread(target=self._handle, args=(c,), daemon=True)
            t.start()
            self._threads.append(t)

    # ---- 主分发 ----
    def _handle(self, c, addr=None):
        peer = ""
        try:
            peer = c.getpeername()[0] if addr is None else addr[0]
        except Exception:
            pass
        try:
            c.settimeout(15)
            first = self._readline_raw(c)
            if not first:
                self._safe_close(c)
                return
            parts = first.split(b" ")
            if len(parts) < 2:
                self._safe_close(c)
                return
            method = parts[0].upper()
            target = parts[1].decode("utf-8", "ignore")
            if method == b"CONNECT":
                self._handle_connect(c, first, target)
            else:
                self._handle_http(c, first, method, target)
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            self.on_log(f"连接处理异常({peer}): {self._last_error}")
        finally:
            self._safe_close(c)

    def _log_safe(self, s):
        try:
            self.on_log(s)
        except Exception:
            pass

    def _drain_connect_headers(self, sock):
        """读掉 CONNECT 请求头直到空行，避免残留字节污染随后的 TLS 握手。"""
        buf = b""
        while not (buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n")):
            try:
                b = sock.recv(1)
            except Exception:
                break
            if not b:
                break
            buf += b

    @staticmethod
    def _readline_raw(sock):
        """读取一行（到 \\r\\n），逐字节读确保不越界消费后续头。"""
        data = bytearray()
        while True:
            try:
                b = sock.recv(1)
            except Exception:
                return b""
            if not b:
                return bytes(data)
            data += b
            if data.endswith(b"\r\n") or len(data) > 65536:
                return bytes(data)

    def _handle_http(self, c, first, method, target):
        """非 CONNECT 的明文 http 代理请求：解析绝对 URL。"""
        # 攒齐请求头（含可能的 Content-Length 体），便于完整转发
        rest = self._read_headers(c)
        if not target.startswith("http://"):
            self._safe_close(c)
            return
        u = urlparse(target)
        host = u.hostname
        port = u.port or 80
        self._note(u.geturl(), host, port)
        # 转发
        upstream = self._connect_up(host, port, target)
        if upstream is None:
            self._safe_close(c)
            return
        try:
            upstream.sendall(first + rest)
            self._splice(c, upstream, read_from_upstream_first=True)
        except Exception:
            pass
        self._safe_close(upstream)
        self._safe_close(c)

    def _handle_connect(self, c, first, target):
        # 先把 CONNECT 的其余请求头(到空行)读完，避免残留 ASCII 头污染后续 TLS 握手
        self._drain_connect_headers(c)
        try:
            host_s, _, port_s = target.partition(":")
        except Exception:
            port_s = "443"
        port = int(port_s or "443")
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        # 取 SNI 作为域名（先用 CONNECT 目标，再考虑 SNI）
        sni = host_s.strip()
        cert_pem, key_pem = self._certs.for_host(sni)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cf = kf = ""
        try:
            fd, cf = tempfile.mkstemp(suffix=".crt")
            os.write(fd, cert_pem); os.close(fd)
            fd, kf = tempfile.mkstemp(suffix=".key")
            os.write(fd, key_pem); os.close(fd)
            ctx.load_cert_chain(certfile=cf, keyfile=kf)
            tls = ctx.wrap_socket(c, server_side=True)
        except Exception as e:
            self._log_safe(f"TLS 握手失败({sni}): {e}")
            self._safe_close(c)
            return
        finally:
            for p in (cf, kf):
                try:
                    if p:
                        os.unlink(p)
                except Exception:
                    pass
        try:
            self._serve_tls(tls, sni)
        except Exception as e:
            self._log_safe(f"TLS 转发异常({sni}): {e}")
        self._safe_close(tls)

    def _serve_tls(self, tls, host):
        """在已解密的 TLS 连接上读取明文 HTTP 请求，抓 URL 并转发。"""
        head = self._read_request(tls)
        if not head:
            return
        first, sep, rest = head.partition(b"\r\n")
        parts = first.split(b" ")
        if len(parts) < 2:
            return
        raw_path = parts[1].decode("utf-8", "ignore")
        full = "https://" + host + ("/"+raw_path.lstrip("/") if not raw_path.startswith("/") else raw_path)
        self._note(full, host, 443)
        # 转发到上游
        ctx = _ensure_ctx_up()
        try:
            upstream = socket.create_connection((host, 443), timeout=15)
            tup = ctx.wrap_socket(upstream, server_hostname=host)
        except Exception:
            return
        try:
            tup.sendall(head)
            self._splice(tls, tup, read_from_upstream_first=True)
        except Exception:
            pass
        self._safe_close(tup)

    def _note(self, full_url, host, port):
        p = urlparse(full_url)
        if p.scheme in ("http", "https"):
            if MEDIA_RE.search(p.path):
                self._media_count += 1
                self.on_media(full_url)
                self.on_log(f"捕获媒体(第{self._media_count}条): {p.path[-60:]}")

    # ---- 转发辅助 ----
    def _read_headers(self, sock):
        data = bytearray()
        while not data.endswith(b"\r\n\r\n"):
            try:
                b = sock.recv(1)
            except Exception:
                break
            if not b:
                break
            data += b
        # 读 body（若有 Content-Length）
        head = bytes(data)
        cl = 0
        for ln in head.split(b"\r\n")[1:]:
            if ln.lower().startswith(b"content-length:"):
                try:
                    cl = int(ln.split(b":", 1)[1].strip())
                except Exception:
                    cl = 0
                break
        while len(data) < len(head) + cl:
            try:
                d = sock.recv(65536)
            except Exception:
                break
            if not d:
                break
            data += d
        return bytes(data)

    def _read_request(self, sock):
        return self._read_headers(sock)

    def _connect_up(self, host, port, target):
        try:
            return socket.create_connection((host, port), timeout=15)
        except Exception:
            return None

    def _splice(self, down, up, read_from_upstream_first=True):
        """双向字节中转，直到任一端关闭。"""
        done = threading.Event()

        def fwd(src, dst):
            try:
                while True:
                    d = src.recv(65536)
                    if not d:
                        break
                    dst.sendall(d)
            except Exception:
                pass
            done.set()

        t1 = threading.Thread(target=fwd, args=(down, up), daemon=True)
        t2 = threading.Thread(target=fwd, args=(up, down), daemon=True)
        t1.start(); t2.start()
        done.wait(timeout=300)
        try:
            down.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            up.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    @staticmethod
    def _safe_close(s):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


# ---------------- CA 信任安装 / 系统代理 ----------------
def install_ca(ca_cert_pem, log=None):
    """把自签 CA 加入 Windows 当前用户受信根证书库（一般无需管理员）。"""
    log = log or (lambda s: None)
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".cer")
        with os.fdopen(fd, "wb") as f:
            f.write(ca_cert_pem)
        r = subprocess.run(["certutil", "-user", "-addstore", "-f", "Root", tmp],
                           capture_output=True, text=True)
        if r.returncode == 0:
            log("CA 已加入当前用户受信根证书库")
            return True
        log("certutil 失败：" + (r.stderr or r.stdout or "")[-300:])
        return False
    except Exception as e:
        log("安装 CA 失败：" + str(e))
        return False
    finally:
        try:
            if tmp:
                os.unlink(tmp)
        except Exception:
            pass


def set_system_proxy(enabled, port=8899, log=None):
    """开关 Windows 系统代理（HKCU Internet Settings）。"""
    import ctypes
    import winreg
    log = log or (lambda s: None)
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                                 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"127.0.0.1:{port}")
            winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
        else:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        # 通知 WinInet 刷新
        INTERNET_OPTION_SETTINGS_CHANGED = 39
        INTERNET_OPTION_REFRESH = 37
        ctypes.windll.Wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        ctypes.windll.Wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
        log(("已开启系统代理（全系统走抓包代理）" if enabled else "已关闭系统代理"))
        return True
    except Exception as e:
        log("设置系统代理失败：" + str(e))
        return False