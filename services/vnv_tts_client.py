"""
Vietnamese TTS Client - Gọi qua Cloudflare Worker Proxy
Fallback: khi bị 429, gọi trực tiếp qua proxy xoay (proxyxoay.shop)
"""
from __future__ import annotations
import os
import re
import sys
import time
import random
import threading
import subprocess
import shutil
import json
from typing import Optional, Callable, List
from dataclasses import dataclass

import requests

from .db_config import TTS_WORKER_URLS, TTS_API_KEY

MAX_CHARS_PER_REQUEST = 300

VIETTEL_API = 'https://viettelai.vn/tts/speech_synthesis'

# Proxy xoay keys — 5 keys, smart rotate
PROXY_KEYS = [
    'lbTCuoJHuauEPhwRjmODiD',
    'JvyqoRLudjFOdILWbjkorr',
    'tYSctPmRrvPluSqDUNLzkF',
    'jhcwQRhZXMrkthFZQirRnF',
    'OZecuBZuwBTiAAPGkuPLci',
]
PROXY_API = 'https://proxyxoay.shop/api/get.php'


@dataclass
class VoiceInfo:
    voice_id: str
    name: str
    gender: str
    region: str
    provider: str  # 'viettel'


VOICES = [
    VoiceInfo("hn-quynhanh", "Quỳnh Anh", "Female", "Northern", "viettel"),
    VoiceInfo("hn-thaochi", "Thảo Chi", "Female", "Northern", "viettel"),
    VoiceInfo("hn-thanhtung", "Thanh Tùng", "Male", "Northern", "viettel"),
    VoiceInfo("hn-namkhanh", "Nam Khánh", "Male", "Northern", "viettel"),
    VoiceInfo("hn-phuongtrang", "Phương Trang", "Female", "Northern", "viettel"),
    VoiceInfo("hn-thanhha", "Thanh Hà", "Female", "Northern", "viettel"),
    VoiceInfo("hn-thanhphuong", "Thanh Phương", "Female", "Northern", "viettel"),
    VoiceInfo("hn-tienquan", "Tiến Quân", "Male", "Northern", "viettel"),
    VoiceInfo("hue-maingoc", "Mai Ngọc", "Female", "Central", "viettel"),
    VoiceInfo("hue-baoquoc", "Bảo Quốc", "Male", "Central", "viettel"),
    VoiceInfo("hcm-diemmy", "Diễm My", "Female", "Southern", "viettel"),
    VoiceInfo("hcm-thuydung", "Thùy Dung", "Female", "Southern", "viettel"),
    VoiceInfo("hn-leyen", "Lê Yến", "Female", "Southern", "viettel"),
    VoiceInfo("hcm-phuongly", "Phương Ly", "Female", "Southern", "viettel"),
    VoiceInfo("hcm-minhquan", "Minh Quân", "Male", "Southern", "viettel"),
    VoiceInfo("hcm-thuyduyen", "Thùy Duyên", "Female", "Southern", "viettel"),
]

VOICE_MAP = {v.voice_id: v for v in VOICES}


class TTSError(Exception):
    pass


class RateLimitError(TTSError):
    def __init__(self, provider: str, cooldown: float):
        self.provider = provider
        self.cooldown = cooldown
        super().__init__(f"{provider} rate limited for {cooldown}s")


class StopRequested(TTSError):
    pass


class ProxyRotator:
    """Smart proxy pool — 5 keys, thread lấy key nào có proxy sẵn trước.
    Khi proxy fail → invalidate → lấy key khác có sẵn hoặc chờ cooldown ngắn nhất."""

    def __init__(self, keys: list[str], log_fn=None):
        self._keys = keys
        self._lock = threading.Lock()
        self._log = log_fn or print
        # Cache proxy per key: {key: {http, expires_at, ip, ...}}
        self._cache: dict[str, dict] = {}
        # Cooldown tracker: {key: ready_at_timestamp}
        self._cooldowns: dict[str, float] = {}
        # Track which thread is using which key (for invalidate)
        self._thread_keys: dict[int, str] = {}

    def _fetch_proxy(self, key: str) -> Optional[dict]:
        """Gọi API lấy proxy cho 1 key. Return dict hoặc None."""
        try:
            resp = requests.get(PROXY_API, params={
                'key': key, 'nhamang': 'random', 'tinhthanh': '0'
            }, timeout=10)
            data = resp.json()

            if data.get('status') == 100:
                proxy_http = data.get('proxyhttp', '')
                if not proxy_http:
                    return None
                parts = proxy_http.split(':')
                if len(parts) < 2:
                    return None
                ip, port = parts[0], parts[1]
                user = parts[2] if len(parts) > 2 and parts[2] else ''
                pwd = parts[3] if len(parts) > 3 and parts[3] else ''
                proxy_url = f"http://{user}:{pwd}@{ip}:{port}" if user and pwd else f"http://{ip}:{port}"
                result = {
                    'http': proxy_url, 'https': proxy_url, 'ip': ip,
                    'expires_at': time.time() + 1700, 'key_prefix': key[:8],
                }
                with self._lock:
                    self._cache[key] = result
                    self._cooldowns.pop(key, None)
                self._log(f"🔄 Proxy mới: {ip} (key {key[:8]}...)")
                return result

            # Parse cooldown
            msg = data.get('message', '')
            m = re.search(r'(\d+)s', msg)
            if m:
                wait_secs = int(m.group(1))
                with self._lock:
                    self._cooldowns[key] = time.time() + wait_secs
            return None

        except Exception as e:
            self._log(f"⚠️ Lỗi lấy proxy: {e}")
            return None

    def get_proxy(self) -> Optional[dict]:
        """Lấy proxy tốt nhất: ưu tiên cached → key không cooldown → chờ key sớm nhất."""
        tid = threading.get_ident()
        now = time.time()

        # 1) Thử tất cả key có cache còn sống
        with self._lock:
            for key in self._keys:
                cached = self._cache.get(key)
                if cached and now < cached.get('expires_at', 0):
                    self._thread_keys[tid] = key
                    return cached

        # 2) Thử fetch từ key không đang cooldown
        with self._lock:
            available = [k for k in self._keys if self._cooldowns.get(k, 0) <= now]
        for key in available:
            result = self._fetch_proxy(key)
            if result:
                with self._lock:
                    self._thread_keys[tid] = key
                return result

        # 3) Tất cả key đang cooldown → chờ key sớm nhất
        with self._lock:
            if not self._cooldowns:
                return None
            soonest_key = min(self._cooldowns, key=self._cooldowns.get)
            wait = self._cooldowns[soonest_key] - now + 2  # +2s buffer

        if wait > 0:
            self._log(f"⏳ Tất cả key cooldown, chờ {wait:.0f}s (key {soonest_key[:8]}...)...")
            time.sleep(min(wait, 50))

        result = self._fetch_proxy(soonest_key)
        if result:
            with self._lock:
                self._thread_keys[tid] = soonest_key
        return result

    def invalidate_current_proxy(self):
        """Xóa cache proxy của thread hiện tại."""
        tid = threading.get_ident()
        with self._lock:
            key = self._thread_keys.get(tid)
            if key and key in self._cache:
                old_ip = self._cache[key].get('ip', '?')
                del self._cache[key]
                self._log(f"🗑️ Invalidate proxy {old_ip} (key {key[:8]}...)")

    def assign_key_for_thread(self) -> str:
        """Compat — giờ get_proxy tự assign."""
        tid = threading.get_ident()
        with self._lock:
            if tid in self._thread_keys:
                return self._thread_keys[tid]
        # Trigger get_proxy to assign
        self.get_proxy()
        with self._lock:
            return self._thread_keys.get(tid, self._keys[0])

    def release_key_for_thread(self):
        """Giải phóng key assignment."""
        tid = threading.get_ident()
        with self._lock:
            self._thread_keys.pop(tid, None)


class VNVTTSClient:
    """Vietnamese TTS Client — CF Worker + proxy fallback"""

    _worker_index = 0
    _worker_lock = threading.Lock()
    _MIN_GAP = 0.5
    _last_request_time = 0.0

    def __init__(self, log_fn: Optional[Callable] = None,
                 stop_event: Optional[threading.Event] = None, **kwargs):
        self._log_fn = log_fn or print
        self._stop_event = stop_event or threading.Event()
        self._worker_urls = [u.rstrip('/') for u in TTS_WORKER_URLS]
        self._api_key = TTS_API_KEY
        self._proxy = ProxyRotator(PROXY_KEYS, log_fn=self._log_fn)

    def _next_worker_url(self) -> str:
        with VNVTTSClient._worker_lock:
            url = self._worker_urls[VNVTTSClient._worker_index % len(self._worker_urls)]
            VNVTTSClient._worker_index += 1
            return url

    def log(self, msg: str):
        self._log_fn(msg)

    def request_stop(self):
        self._stop_event.set()

    def _sleep(self, seconds: float):
        if self._stop_event.wait(timeout=seconds):
            raise StopRequested("TTS stopped by user")

    def _ensure_gap(self):
        """Đảm bảo gap tối thiểu giữa requests"""
        with VNVTTSClient._worker_lock:
            now = time.time()
            elapsed = now - VNVTTSClient._last_request_time
            gap = VNVTTSClient._MIN_GAP - elapsed
        if gap > 0:
            self._sleep(gap)
        with VNVTTSClient._worker_lock:
            VNVTTSClient._last_request_time = time.time()

    def _call_via_curl(self, text: str, voice_id: str, speed: float,
                        output_path: str, proxy_url: str) -> bool:
        """Gọi Viettel TTS qua curl + proxy HTTP"""
        payload = json.dumps({
            'text': text, 'voice': voice_id, 'speed': speed,
            'tts_return_option': 2, 'without_filter': False
        })
        cmd = [
            'curl', '-s', '-x', proxy_url,
            '-X', 'POST', VIETTEL_API,
            '-H', 'Content-Type: application/json',
            '-H', 'Origin: https://viettelai.vn',
            '-H', 'Referer: https://viettelai.vn/tts',
            '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            '-d', payload,
            '-o', output_path,
            '--connect-timeout', '15',
            '-m', '30',
            '-w', '%{http_code}',
        ]
        try:
            kwargs = {}
            if sys.platform == 'win32':
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd, capture_output=True, timeout=35, text=True, **kwargs)
            status_code = result.stdout.strip()
            if status_code == '200' and os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                return True
            elif status_code == '429':
                return False
            else:
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False
        except Exception as e:
            self.log(f"⚠️ curl error: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False


    def _call_via_proxy(self, text: str, voice_id: str, speed: float,
                         output_path: str) -> bool:
        """Gọi Viettel TTS trực tiếp qua proxy xoay bằng curl"""
        max_proxy_tries = 5
        for i in range(max_proxy_tries):
            if self._stop_event.is_set():
                raise StopRequested()
            proxy_dict = self._proxy.get_proxy()
            if not proxy_dict:
                self._sleep(1)
                continue
            proxy_url = proxy_dict['http'].replace('http://', '')  # curl -x cần ip:port
            ip = proxy_dict.get('ip', '?')
            ok = self._call_via_curl(text, voice_id, speed, output_path, proxy_url)
            if ok:
                self.log(f"✅ Proxy OK ({ip})")
                return True
            else:
                self.log(f"⚠️ Proxy {ip} fail, invalidate cache...")
                self._proxy.invalidate_current_proxy()
            self._sleep(0.5)
        raise TTSError(f"Proxy failed sau {max_proxy_tries} lần thử")

    def _call_worker(self, text: str, voice_id: str, speed: float, output_path: str) -> bool:
        """Gọi CF Worker, nếu 429 → fallback qua proxy xoay"""
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['x-api-key'] = self._api_key

        payload = {'text': text, 'voice': voice_id, 'speed': speed}

        for attempt in range(2):
            if self._stop_event.is_set():
                raise StopRequested()

            self._ensure_gap()
            worker_url = self._next_worker_url()
            worker_name = worker_url.split('//')[1].split('.')[0]

            try:
                resp = requests.post(f"{worker_url}/tts", json=payload,
                                     headers=headers, timeout=30)

                if resp.status_code == 200:
                    content_type = resp.headers.get('content-type', '')
                    if 'audio' in content_type or 'octet-stream' in content_type:
                        with open(output_path, 'wb') as f:
                            f.write(resp.content)
                        if os.path.getsize(output_path) > 100:
                            return True
                        os.remove(output_path)

                if resp.status_code == 429:
                    self.log(f"⚠️ 429 [{worker_name}] → fallback proxy...")
                    return self._call_via_proxy(text, voice_id, speed, output_path)

                if resp.status_code == 502:
                    self.log(f"⚠️ 502 [{worker_name}], thử worker khác...")
                    continue

                try:
                    err = resp.json().get('error', f'HTTP {resp.status_code}')
                except Exception:
                    err = f'HTTP {resp.status_code}'
                raise TTSError(err)

            except StopRequested:
                raise
            except TTSError:
                raise
            except requests.exceptions.RequestException as e:
                self.log(f"⚠️ Network error [{worker_name}]: {e}")
                if attempt == 0:
                    self._sleep(1)
                    continue
                raise TTSError(f"Network error: {e}")

        self.log(f"⚠️ Workers failed → fallback proxy...")
        return self._call_via_proxy(text, voice_id, speed, output_path)

    def _split_text_for_api(self, text: str) -> list[str]:
        """Split text into chunks <= MAX_CHARS_PER_REQUEST at sentence boundaries"""
        if len(text) <= MAX_CHARS_PER_REQUEST:
            return [text]

        sentences = re.split(r'(?<=[.!?。，,;；])\s*', text)
        chunks = []
        current = ""
        for sent in sentences:
            if not sent.strip():
                continue
            if len(current) + len(sent) + 1 <= MAX_CHARS_PER_REQUEST:
                current = (current + " " + sent).strip() if current else sent
            else:
                if current:
                    chunks.append(current)
                if len(sent) > MAX_CHARS_PER_REQUEST:
                    words = sent.split()
                    sub = ""
                    for w in words:
                        if len(sub) + len(w) + 1 <= MAX_CHARS_PER_REQUEST:
                            sub = (sub + " " + w).strip() if sub else w
                        else:
                            if sub:
                                chunks.append(sub)
                            if len(w) > MAX_CHARS_PER_REQUEST:
                                for i in range(0, len(w), MAX_CHARS_PER_REQUEST):
                                    chunks.append(w[i:i + MAX_CHARS_PER_REQUEST])
                                sub = ""
                            else:
                                sub = w
                    if sub:
                        current = sub
                    else:
                        current = ""
                else:
                    current = sent
        if current:
            chunks.append(current)
        return [c for c in chunks if c.strip()]

    def synthesize(self, text: str, voice_id: str, speed: float,
                   output_path: str, max_retries: int = 3) -> bool:
        """Synthesize text → audio qua Viettel TTS + proxy"""
        chunks = self._split_text_for_api(text)

        if len(chunks) == 1:
            return self._call_via_proxy(chunks[0], voice_id, speed, output_path)

        self.log(f"📝 Text {len(text)} chars → {len(chunks)} chunks")
        temp_dir = output_path + "_chunks"
        os.makedirs(temp_dir, exist_ok=True)
        chunk_files = []

        try:
            for i, chunk in enumerate(chunks):
                if self._stop_event.is_set():
                    raise StopRequested()
                chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}.mp3")
                self._call_via_proxy(chunk, voice_id, speed, chunk_path)
                chunk_files.append(chunk_path)
            self._concat_audio(chunk_files, output_path)
            return True
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    def _concat_audio(self, chunk_files: list[str], output_path: str):
        if len(chunk_files) == 1:
            shutil.copy2(chunk_files[0], output_path)
            return
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for name in ['ffmpeg', 'ffmpeg.exe']:
                p = os.path.join(app_dir, name)
                if os.path.isfile(p):
                    ffmpeg = p
                    break
        if ffmpeg:
            list_file = output_path + ".concat.txt"
            try:
                with open(list_file, 'w', encoding='utf-8') as f:
                    for cf in chunk_files:
                        escaped = os.path.abspath(cf).replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")
                cmd = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_file,
                       '-c:a', 'libmp3lame', '-b:a', '128k', '-loglevel', 'error', output_path]
                result = subprocess.run(cmd, capture_output=True, timeout=60,
                                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                if result.returncode == 0 or (os.path.exists(output_path) and os.path.getsize(output_path) > 100):
                    return
            finally:
                try:
                    os.remove(list_file)
                except Exception:
                    pass
        with open(output_path, 'wb') as out:
            for cf in chunk_files:
                with open(cf, 'rb') as inp:
                    out.write(inp.read())

    def get_rate_status(self) -> dict:
        return {
            'viettel_limited': False, 'viettel_wait': 0,
            'proxy': {'enabled': True, 'active': True, 'ttl': 0, 'ip': ''},
        }
