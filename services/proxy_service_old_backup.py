# services/proxy_service.py
from __future__ import annotations

import os
import uuid
import time
import threading
import subprocess
import sqlite3
import base64
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from urllib.parse import urlparse


@dataclass
class GatewayCfg:
    host: str
    port: str
    username: str
    password: str
    reset_link: Optional[str] = None
    token_proxy: Optional[str] = None

    # runtime
    last_ip: Optional[str] = None
    score: int = 0
    last_checked: float = 0.0
    last_rotated: float = 0.0           # lần gần nhất xoay IP
    rotating: bool = False              # tránh xoay trùng
    failed_rotates: int = 0             # backoff nếu reset fail
    
    # health tracking (for cache)
    health_status: str = "unknown"      # "healthy", "degraded", "unhealthy"
    success_count: int = 0              # consecutive successes
    failure_count: int = 0              # consecutive failures
    avg_latency_ms: float = 0.0         # average latency
    last_health_check: float = 0.0      # timestamp of last health check


class RotatingProxyKey:
    """
    Rotating proxy key from proxyxoay.shop - unlimited bandwidth.
    Fetches new proxy IP from API and caches it with TTL from API response.
    """
    PROXYXOAY_API = "https://proxyxoay.shop/api/get.php"
    
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self._current_proxy_url: Optional[str] = None
        self._proxy_expire_time: float = 0.0
        self._lock = threading.Lock()
        self._last_location: str = ""
        self._last_isp: str = ""
    
    def _fetch_new_proxy(self) -> Optional[str]:
        """Fetch new proxy from proxyxoay.shop API"""
        import requests
        import re
        
        try:
            url = f"{self.PROXYXOAY_API}?key={self.api_key}&nhamang=random&tinhthanh=random"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            
            if data.get("status") == 100:
                # Success - extract proxy
                proxy_http = data.get("proxyhttp", "")
                if proxy_http:
                    # Format: "ip:port::" hoặc "ip:port:user:pass"
                    parts = proxy_http.split(":")
                    if len(parts) >= 2:
                        ip, port = parts[0], parts[1]
                        # Check if has auth
                        if len(parts) >= 4 and parts[2] and parts[3]:
                            proxy_url = f"http://{parts[2]}:{parts[3]}@{ip}:{port}"
                        else:
                            proxy_url = f"http://{ip}:{port}"
                        
                        # Parse TTL from message (e.g., "proxy nay se die sau 1503s")
                        msg = data.get("message", "")
                        try:
                            match = re.search(r'(\d+)s', msg)
                            if match:
                                ttl = int(match.group(1))
                                self._proxy_expire_time = time.time() + ttl - 30  # 30s buffer
                            else:
                                self._proxy_expire_time = time.time() + 1200  # Default 20 min
                        except:
                            self._proxy_expire_time = time.time() + 1200
                        
                        # Extract location info
                        self._last_location = data.get("Vi Tri", "Unknown")
                        self._last_isp = data.get("Nha Mang", "Unknown")
                        
                        print(f"🔄 [RotatingProxy] New proxy: {ip}:{port} ({self._last_isp}/{self._last_location}), TTL: {ttl}s")
                        return proxy_url
            
            elif data.get("status") == 101:
                # Rate limit or invalid key
                msg = data.get("message", "")
                if "moi co the doi" in msg.lower() or "doi proxy" in msg.lower():
                    # Rate limit - keep current proxy, extend TTL
                    print(f"⏳ [RotatingProxy] Rate limit - keeping current proxy")
                    self._proxy_expire_time = time.time() + 10
                else:
                    print(f"❌ [RotatingProxy] Key invalid: {msg}")
            elif data.get("status") == 102:
                print(f"❌ [RotatingProxy] Key expired")
            else:
                print(f"❌ [RotatingProxy] API error: {data}")
        
        except Exception as e:
            print(f"❌ [RotatingProxy] Fetch error: {e}")
        
        return None
    
    def get_proxy_url(self, force_refresh: bool = False) -> Optional[str]:
        """
        Get proxy URL with TTL-based caching (sticky session).
        Only fetches new proxy when TTL expires or force_refresh=True.
        """
        with self._lock:
            now = time.time()
            
            # Return cached proxy if still valid
            if not force_refresh and self._current_proxy_url and now < self._proxy_expire_time:
                return self._current_proxy_url
            
            # Fetch new proxy
            new_proxy = self._fetch_new_proxy()
            if new_proxy:
                self._current_proxy_url = new_proxy
                return new_proxy
            
            # Fallback to current proxy if fetch failed
            return self._current_proxy_url
    
    def parse_to_cfg_dict(self, proxy_url: str) -> Optional[Dict[str, str]]:
        """Parse proxy URL to config dict compatible with GatewayCfg"""
        if not proxy_url:
            return None
        
        from urllib.parse import urlparse
        
        try:
            parsed = urlparse(proxy_url)
            return {
                "host": parsed.hostname or "",
                "port": str(parsed.port) if parsed.port else "",
                "username": parsed.username or "",
                "password": parsed.password or "",
                "token_proxy": "rotating_key"
            }
        except Exception:
            return None


class ProxyService:
    """
    Khởi tạo:
        ProxyService(supabase=<supabase>, user_id=<int>)
      → tự load tất cả hàng trong bảng users_token_proxy cho user_id
    Hoặc:
        ProxyService(host, port, username, password, rotate_url)
      → dùng 1 gateway truyền trực tiếp.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | str | None = None,
        username: str | None = None,
        password: str | None = None,
        rotate_url: str | None = None,
        supabase=None,
        user_id: int | None = None,
        rotating_proxy_key: str | None = None,  # NEW: Support for rotating proxy key
    ) -> None:
        self.supabase = supabase
        self.user_id = user_id

        self._gateways: List[GatewayCfg] = []
        # Local SQLite-backed proxy queue (for check_live-style rotation)
        self._proxy_queue: "queue.Queue[str]" | None = None
        self._local_pool_initialized: bool = False
        self._idx: int = 0

        # auto-rotator
        self._rotator_thread: Optional[threading.Thread] = None
        self._rotator_stop = threading.Event()
        self._lock = threading.RLock()
        self._rotate_every = 60
        self._gateway_cooldown = 55       # tối thiểu giữa 2 lần rotate cùng 1 gateway
        self._jitter_min = 5
        self._jitter_max = 15
        
        # health check cache
        self._health_check_ttl = 120  # 2 minutes cache for health status
        self._health_check_enabled = True
        
        # CENTRALIZED reset link manager - prevents duplicate calls
        self._last_global_reset_time = 0.0  # Last time reset link was called (for any gateway)
        self._reset_cooldown = 5  # Minimum seconds between reset calls
        self._reset_in_progress = False  # Lock to prevent concurrent reset calls
        
        # NEW: Rotating proxy key support (proxyxoay.shop)
        self._rotating_proxy: Optional[RotatingProxyKey] = None
        if rotating_proxy_key:
            self._rotating_proxy = RotatingProxyKey(rotating_proxy_key)
            print(f"✅ [ProxyService] Rotating proxy key initialized: {rotating_proxy_key[:10]}...")

        if host and port and username and password:
            self._gateways = [
                GatewayCfg(
                    host=str(host),
                    port=str(port),
                    username=str(username),
                    password=str(password),
                    reset_link=rotate_url,
                )
            ]

    # ----------------- Rotating Proxy Key Methods -----------------
    @staticmethod
    def _is_rotating_proxy_key(text: str) -> bool:
        """
        Detect if text is a rotating proxy key from proxyxoay.shop.
        Key characteristics: 15-30 alphanumeric chars, no special chars (: @ .)
        """
        text = text.strip()
        if len(text) >= 15 and len(text) <= 30:
            if ':' not in text and '@' not in text and '.' not in text:
                if text.isalnum():
                    return True
        return False
    
    def set_rotating_key(self, api_key: str) -> None:
        """Set rotating proxy key (proxyxoay.shop)"""
        with self._lock:
            self._rotating_proxy = RotatingProxyKey(api_key)
            print(f"✅ [ProxyService] Rotating proxy key set: {api_key[:10]}...")
    
    def has_rotating_key(self) -> bool:
        """Check if rotating proxy key is configured"""
        return self._rotating_proxy is not None
    
    def get_rotating_proxy_url(self, force_refresh: bool = False) -> Optional[str]:
        """
        Get proxy URL from rotating key with TTL-based caching.
        Returns: proxy_url (http://ip:port or http://user:pass@ip:port)
        """
        if not self._rotating_proxy:
            return None
        return self._rotating_proxy.get_proxy_url(force_refresh)
    
    def parse_proxy_string_auto(self, proxy_text: str) -> Tuple[bool, Optional[str], List[str]]:
        """
        Auto-detect proxy type from text and parse accordingly.
        
        Returns:
            (is_rotating_key, rotating_key_if_any, static_proxy_list)
        """
        lines = [x.strip() for x in proxy_text.splitlines() if x.strip()]
        
        # Single line that looks like rotating key
        if len(lines) == 1 and self._is_rotating_proxy_key(lines[0]):
            return (True, lines[0], [])
        
        # Multiple lines or non-rotating format - treat as static proxy list
        return (False, None, lines)
    
    # ----------------- SQLite helper methods -----------------
    def _get_sqlite_path(self) -> str:
        """
        Local encrypted proxy cache path.
        One DB for all users; user_id field separates pools.
        """
        base_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        os.makedirs(base_dir, exist_ok=True)
        return os.path.join(base_dir, "proxy_pool.db")

    def _get_enc_key(self) -> bytes:
        """
        Derive simple XOR key from user_id (lightweight obfuscation, not strong crypto).
        """
        uid = self.user_id if self.user_id is not None else 0
        raw = f"proxy_pool_user_{uid}_salt"
        return hashlib.sha256(raw.encode("utf-8")).digest()

    def _encrypt(self, text: str) -> str:
        if text is None:
            return ""
        data = text.encode("utf-8")
        key = self._get_enc_key()
        xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
        return base64.b64encode(xored).decode("ascii")

    def _decrypt(self, enc: str) -> str:
        if not enc:
            return ""
        try:
            data = base64.b64decode(enc.encode("ascii"))
            key = self._get_enc_key()
            plain = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
            return plain.decode("utf-8", errors="ignore")
        except Exception:
            # Fallback: return original if decode fails
            return enc

    def _ensure_sqlite(self) -> sqlite3.Connection:
        """
        Ensure SQLite DB and table exist.
        """
        db_path = self._get_sqlite_path()
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_proxy_pool_local (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                proxy_enc TEXT NOT NULL,
                token_proxy TEXT,
                is_active INTEGER DEFAULT 1,
                last_used_at REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_proxy_user ON user_proxy_pool_local(user_id)"
        )
        conn.commit()
        return conn

    def _load_local_proxies(self) -> List[GatewayCfg]:
        """
        Load proxies for this user from local SQLite (if any).
        """
        try:
            conn = self._ensure_sqlite()
            uid = self.user_id if self.user_id is not None else 0
            cur = conn.execute(
                "SELECT proxy_enc, token_proxy FROM user_proxy_pool_local WHERE user_id=? AND is_active=1",
                (uid,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            print(f"❌ PROXY_LOCAL_LOAD_ERROR - {exc}")
            return []

        gws: List[GatewayCfg] = []
        print(f"🔍 PROXY_LOCAL_LOAD - Found {len(rows)} proxies in SQLite for user_id={self.user_id}")
        for idx, (proxy_enc, token_proxy) in enumerate(rows, 1):
            raw_url = self._decrypt(proxy_enc).strip()
            if not raw_url:
                continue
            parsed = self._parse_port_field(raw_url)
            if not parsed:
                print(f"⚠️ PROXY_LOCAL_ROW_{idx}_SKIP - Invalid proxy format after decrypt: '{raw_url}'")
                continue
            gw = GatewayCfg(
                host=parsed["host"],
                port=parsed["port"],
                username=parsed["username"],
                password=parsed["password"],
                reset_link=None,
                token_proxy=token_proxy,
            )
            gws.append(gw)
        return gws

    def _save_local_proxies(self, gateways: List[GatewayCfg]) -> None:
        """
        Save list of GatewayCfg to local SQLite for this user (overwrite existing).
        """
        try:
            conn = self._ensure_sqlite()
            uid = self.user_id if self.user_id is not None else 0
            conn.execute(
                "DELETE FROM user_proxy_pool_local WHERE user_id=?",
                (uid,),
            )
            for gw in gateways:
                if gw.username:
                    proxy_url = f"http://{gw.username}:{gw.password}@{gw.host}:{gw.port}"
                else:
                    proxy_url = f"http://{gw.host}:{gw.port}"
                proxy_enc = self._encrypt(proxy_url)
                conn.execute(
                    """
                    INSERT INTO user_proxy_pool_local (user_id, proxy_enc, token_proxy, is_active, last_used_at)
                    VALUES (?, ?, ?, 1, strftime('%s','now'))
                    """,
                    (uid, proxy_enc, gw.token_proxy or None),
                )
            conn.commit()
            conn.close()
            print(f"✅ PROXY_LOCAL_SAVE - Saved {len(gateways)} proxies to SQLite for user_id={self.user_id}")
        except Exception as exc:
            print(f"❌ PROXY_LOCAL_SAVE_ERROR - {exc}")

    def _init_local_pool(self) -> None:
        """
        Initialize local proxy pool + in-memory queue.
        Priority:
          1. Load from local SQLite (encrypted)
          2. If empty, fetch from Supabase user_proxy_pool (global shared),
             then persist to SQLite.
        """
        if self._local_pool_initialized:
            return

        import queue as _queue

        # Step 1: try local SQLite
        local_gws = self._load_local_proxies()
        if not local_gws:
            # Step 2: fetch from Supabase user_proxy_pool (global)
            pool_gws = self._load_from_pool_table()
            if pool_gws:
                self._save_local_proxies(pool_gws)
                local_gws = pool_gws

        with self._lock:
            self._gateways = local_gws
            self._idx = 0 if self._gateways else self._idx
            # Build queue of proxy URLs like check_live.py
            self._proxy_queue = _queue.Queue()
            for gw in self._gateways:
                if gw.username:
                    proxy_url = f"http://{gw.username}:{gw.password}@{gw.host}:{gw.port}"
                else:
                    proxy_url = f"http://{gw.host}:{gw.port}"
                self._proxy_queue.put(proxy_url)
            self._local_pool_initialized = True
            print(f"✅ PROXY_LOCAL_POOL_INIT - Initialized {len(self._gateways)} gateways and queue size {self._proxy_queue.qsize() if self._proxy_queue else 0}")

    # ----------------- load & parse DB -----------------
    def _parse_port_field(self, s: str) -> Optional[Dict[str, str]]:
        """
        Parse proxy port field from database.
        
        Supported formats:
        1. URL với scheme + auth:
           - "http://user:pass@host:port"
           - "https://user:pass@host:port"
        2. Chuỗi 4 phần với dấu ':':
           - "host:port:username:password"
           - "username:password:host:port"
        3. Định dạng đặc biệt với username có dấu ':':
           - "username-with-colons:password:host:port"
           - Logic: tìm port ở cuối, host ở trước port, phần còn lại là username:password
        """
        raw = (s or "").strip()
        print(f"🔍 PROXY_PARSE - Raw string: '{raw}'")

        # --- Format 1: full URL http://user:pass@host:port ---
        if "://" in raw:
            try:
                parsed = urlparse(raw)
                host = parsed.hostname or ""
                port = parsed.port
                username = parsed.username or ""
                password = parsed.password or ""

                if not host or port is None:
                    raise ValueError("Missing host or port in proxy URL")

                result = {
                    "host": host.strip(),
                    "port": str(port).strip(),
                    "username": username.strip(),
                    "password": password.strip(),
                }
                print("✅ PROXY_PARSE_SUCCESS - URL format detected: http(s)://user:pass@host:port")
                print(f"✅ PROXY_PARSE_SUCCESS - Host: {result['host']}, Port: {result['port']}, User: {result['username']}")
                return result
            except Exception as e:
                print(f"❌ PROXY_PARSE_URL_ERROR - {e}")
                # fall through to legacy formats

        # --- Colon-separated formats ---
        # Tìm port ở cuối cùng (phần số hợp lệ)
        parts = raw.split(":")
        print(f"🔍 PROXY_PARSE - Parts after split: {parts} (count: {len(parts)})")
        
        if len(parts) < 3:
            print(f"❌ PROXY_PARSE_ERROR - Expected at least 3 parts, got {len(parts)} parts")
            print(f"❌ PROXY_PARSE_ERROR - Supported formats:")
            print(f"   1. 'hostname:port:username:password'")
            print(f"   2. 'username:password:hostname:port'")
            print(f"   3. 'http://user:pass@host:port'")
            print(f"   4. 'username-with-colons:password:host:port'")
            return None
        
        # Tìm port (phần cuối cùng là số hợp lệ)
        port_idx = None
        port_value = None
        for i in range(len(parts) - 1, -1, -1):  # Tìm từ cuối lên
            try:
                port_num = int(parts[i].strip())
                if 0 < port_num <= 65535:
                    port_idx = i
                    port_value = str(port_num)
                    break
            except ValueError:
                continue
        
        if port_idx is None:
            print(f"❌ PROXY_PARSE_ERROR - No valid port number found in: {parts}")
            return None
        
        # Xác định format dựa trên vị trí port
        if port_idx == len(parts) - 1:
            # Port ở cuối: có thể là "username:password:host:port" hoặc "username-with-colons:password:host:port"
            # Host là phần ngay trước port
            if port_idx < 1:
                print(f"❌ PROXY_PARSE_ERROR - Not enough parts for host (port at index {port_idx})")
                return None
            
            host = parts[port_idx - 1].strip()
            # Phần còn lại (từ đầu đến port_idx-2) là username:password
            # Tách phần cuối cùng trước host là password, phần còn lại là username
            if port_idx >= 2:
                password = parts[port_idx - 2].strip()
                username = ":".join(parts[:port_idx - 2]).strip() if port_idx > 2 else ""
                if not username:
                    # Nếu không có username (port_idx == 2), thì phần đầu là username, phần thứ 2 là password
                    if port_idx == 2:
                        username = parts[0].strip()
                        password = parts[1].strip()
                else:
                    # Username có thể chứa dấu ':', nên cần join lại
                    pass
            else:
                print(f"❌ PROXY_PARSE_ERROR - Not enough parts for username:password (port at index {port_idx})")
                return None
            
            result = {
                "host": host,
                "port": port_value,
                "username": username,
                "password": password,
            }
            print("✅ PROXY_PARSE_SUCCESS - Format detected: user:pass:host:port (with possible colons in username)")
        elif port_idx == 1:
            # Format: "host:port:username:password"
            if len(parts) < 4:
                print(f"❌ PROXY_PARSE_ERROR - Expected 4 parts for host:port:user:pass format, got {len(parts)}")
                return None
            result = {
                "host": parts[0].strip(),
                "port": port_value,
                "username": parts[2].strip(),
                "password": parts[3].strip() if len(parts) > 3 else "",
            }
            print("✅ PROXY_PARSE_SUCCESS - Format detected: host:port:user:pass")
        else:
            # Port ở giữa - không hỗ trợ
            print(f"❌ PROXY_PARSE_ERROR - Port at unexpected position {port_idx}, cannot determine format")
            return None
        
        print(f"✅ PROXY_PARSE_SUCCESS - Host: {result['host']}, Port: {result['port']}, User: {result['username']}")
        return result

    def _load_all_from_db(self) -> List[GatewayCfg]:
        if not (self.supabase and self.user_id):
            return self._gateways

        # Use safe database operation with retry logic
        def _execute_query():
            return (
                self.supabase.table("users_token_proxy")
                .select("*")
                .eq("user_id", self.user_id)
                .execute()
            )
        
        try:
            from services.db_retry_helper import safe_db_operation
            res = safe_db_operation(_execute_query, max_retries=3, default_return=None)
            rows = res.data if res else []
        except Exception as e:
            print(f"❌ load users_token_proxy error: {e}")
            rows = []

        gws: List[GatewayCfg] = []
        print(f"🔍 PROXY_DB_LOAD - Found {len(rows)} proxy rows from users_token_proxy for user_id={self.user_id}")
        
        for idx, r in enumerate(rows, 1):
            print(f"🔍 PROXY_ROW_{idx} - Raw data: id={r.get('id')}, token_proxy={r.get('token_proxy')}, port='{r.get('port')}'")
            
            parsed = self._parse_port_field(r.get("port", ""))
            if not parsed:
                print(f"⚠️ PROXY_ROW_{idx}_SKIP - Invalid port format: '{r.get('port')}'")
                print(f"⚠️ PROXY_ROW_{idx}_HELP - Expected format: 'hostname:port:username:password' (4 parts separated by colon)")
                continue
            
            gw = GatewayCfg(
                host=parsed["host"],
                port=parsed["port"],
                username=parsed["username"],
                password=parsed["password"],
                reset_link=r.get("reset_link") or r.get("rotate_link") or r.get("rotate_url"),
                token_proxy=r.get("token_proxy"),
            )
            gws.append(gw)
            print(f"✅ PROXY_ROW_{idx}_LOADED - {gw.host}:{gw.port} (type: {gw.token_proxy})")

        # ƯU TIÊN: gateways từ bảng users_token_proxy trước
        with self._lock:
            # chỉ cache proxy của riêng user; pool sẽ lấy ngẫu nhiên mỗi lần gọi
            self._gateways = gws
            self._idx = 0 if self._gateways else self._idx

        return self._gateways

    def _load_from_pool_table(self) -> List[GatewayCfg]:
        """
        Lấy NGẪU NHIÊN 1 proxy active từ bảng dùng chung user_proxy_pool.
        Không load toàn bộ 10k proxies để tránh overhead.
        """
        if not (self.supabase and self.user_id):
            return []

        try:
            from services.db_retry_helper import safe_db_operation
        except Exception:
            safe_db_operation = None  # fallback nếu helper không tồn tại

        def _execute_query():
            # Bảng dùng chung: user_proxy_pool
            # Cột: proxy_url (chuỗi kiểu host:port:user:pass), is_active, created_at, last_used_at
            # Luôn lọc is_active = true; tạm thời ưu tiên bản ghi mới nhất (id desc)
            return (
                self.supabase.table("user_proxy_pool")
                .select("proxy_url, is_active")
                .eq("is_active", True)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )

        try:
            if safe_db_operation:
                res = safe_db_operation(_execute_query, max_retries=3, default_return=None)
                rows = res.data if res else []
            else:
                res = _execute_query()
                rows = res.data if res else []
        except Exception as e:
            print(f"❌ load user_proxy_pool error: {e}")
            rows = []

        pool_gws: List[GatewayCfg] = []
        print(f"🔍 PROXY_POOL_DB_LOAD - Random fetch got {len(rows)} rows from user_proxy_pool (global shared)")

        for idx, r in enumerate(rows, 1):
            raw_url = (r.get("proxy_url") or "").strip()
            if not raw_url:
                continue

            print(f"🔍 PROXY_POOL_ROW_{idx} - proxy_url='{raw_url}' token_proxy={r.get('token_proxy')}")
            parsed = self._parse_port_field(raw_url)
            if not parsed:
                print(f"⚠️ PROXY_POOL_ROW_{idx}_SKIP - Invalid proxy_url format: '{raw_url}'")
                continue

            # user_proxy_pool không có cột token_proxy; mặc định coi là 'canada'
            gw = GatewayCfg(
                host=parsed["host"],
                port=parsed["port"],
                username=parsed["username"],
                password=parsed["password"],
                reset_link=None,
                token_proxy="canada",
            )
            pool_gws.append(gw)
            print(f"✅ PROXY_POOL_ROW_{idx}_LOADED - {gw.host}:{gw.port} (type: {gw.token_proxy})")

        return pool_gws

    # ----------------- public getters -----------------
    def get_all_gateways(self) -> List[GatewayCfg]:
        """
        Trả về danh sách proxy cho user hiện tại.
        Ưu tiên pool local SQLite (đã sync từ Supabase user_proxy_pool).
        """
        # Lazy init local pool (SQLite + Supabase)
        self._init_local_pool()
        return self._gateways
    
    def get_random_proxy_from_db(self) -> Optional[GatewayCfg]:
        """
        Lấy NGẪU NHIÊN 1 proxy từ users_token_proxy cho user hiện tại.
        CHỈ sử dụng proxy từ users_token_proxy (không dùng user_proxy_pool nữa).
        
        Returns:
            GatewayCfg nếu tìm thấy, None nếu không có proxy nào
        """
        # Lấy tất cả proxy từ users_token_proxy
        gws = self.get_all_gateways()
        if not gws:
            print("⚠️ No proxies found in users_token_proxy for user")
            return None
        
        # Random chọn 1 proxy từ danh sách
        import random
        random_gw = random.choice(gws)
        print(f"✅ RANDOM_PROXY_LOADED - {random_gw.host}:{random_gw.port} (type: {random_gw.token_proxy})")
        return random_gw
    
    def get_next_proxy_for_rotation(self, current_proxy: Optional[GatewayCfg] = None) -> Optional[GatewayCfg]:
        """
        Lấy proxy tiếp theo để rotate khi gặp lỗi 401 (xoay vòng round-robin).
        Chỉ lấy proxy từ users_token_proxy của user hiện tại.
        Nếu hết proxy thì quay lại đầu danh sách và tiếp tục xoay vòng.
        
        Args:
            current_proxy: Proxy hiện tại đang dùng (sẽ tìm proxy tiếp theo sau nó)
            
        Returns:
            GatewayCfg tiếp theo trong danh sách, None nếu không có proxy nào
        """
        gws = self.get_all_gateways()
        if not gws:
            print("⚠️ ROTATE_PROXY - No proxies found in users_token_proxy for user")
            return None
        
        # Nếu chỉ có 1 proxy, trả về chính nó (không thể xoay)
        if len(gws) == 1:
            print(f"✅ ROTATE_PROXY - Only 1 proxy available, returning same: {gws[0].host}:{gws[0].port}")
            return gws[0]
        
        # Tìm vị trí của current_proxy trong danh sách
        if current_proxy:
            current_key = f"{current_proxy.host}:{current_proxy.port}"
            current_idx = -1
            for i, gw in enumerate(gws):
                gw_key = f"{gw.host}:{gw.port}"
                if gw_key == current_key:
                    current_idx = i
                    break
            
            # Nếu tìm thấy current_proxy, lấy proxy tiếp theo (xoay vòng)
            if current_idx >= 0:
                next_idx = (current_idx + 1) % len(gws)  # Xoay vòng: nếu hết thì quay lại đầu
                next_gw = gws[next_idx]
                print(f"✅ ROTATE_PROXY - Rotating from {current_proxy.host}:{current_proxy.port} to {next_gw.host}:{next_gw.port} (round-robin {current_idx+1}/{len(gws)} → {next_idx+1}/{len(gws)})")
                return next_gw
            else:
                # Không tìm thấy current_proxy trong danh sách, trả về proxy đầu tiên
                print(f"✅ ROTATE_PROXY - Current proxy not found in list, returning first: {gws[0].host}:{gws[0].port}")
                return gws[0]
        else:
            # Không có current_proxy, trả về proxy đầu tiên
            print(f"✅ ROTATE_PROXY - No current proxy, returning first: {gws[0].host}:{gws[0].port}")
            return gws[0]

    def get_current_gateway(self) -> Optional[GatewayCfg]:
        gws = self.get_all_gateways()
        if not gws:
            return None
        with self._lock:
            self._idx %= len(gws)
            return gws[self._idx]

    def advance_round_robin(self) -> Optional[GatewayCfg]:
        gws = self.get_all_gateways()
        if not gws:
            return None
        with self._lock:
            self._idx = (self._idx + 1) % len(gws)
            return gws[self._idx]

    def as_proxy_dict(self, gw: GatewayCfg | None) -> Optional[Dict[str, str]]:
        if not gw:
            return None
        return {
            "host": gw.host,
            "port": gw.port,
            "username": gw.username,
            "password": gw.password,
        }

    def parse_proxy_url_to_cfg(self, proxy_url: str) -> Optional[Dict[str, str]]:
        """
        Chuyển 1 proxy_url dạng:
          - "http://user:pass@host:port"
          - "host:port:user:pass"
        thành dict {host, port, username, password} để dùng cho TTS.
        """
        raw = (proxy_url or "").strip()
        if not raw:
            return None
        parsed = self._parse_port_field(raw)
        return parsed

    # ----------------- Unified Proxy Access (Rotating + Database) -----------------
    def get_proxy_for_request(self, prefer_rotating: bool = True) -> Optional[Dict[str, str]]:
        """
        Get proxy for a request - automatically chooses rotating key or database proxy.
        
        Priority:
        1. Rotating proxy key (if available and prefer_rotating=True) - unlimited bandwidth
        2. Database proxy (fallback)
        
        Returns:
            Dict with {host, port, username, password, token_proxy} or None
        """
        # Priority 1: Rotating proxy key (unlimited bandwidth)
        if prefer_rotating and self._rotating_proxy:
            proxy_url = self.get_rotating_proxy_url(force_refresh=False)
            if proxy_url:
                cfg = self._rotating_proxy.parse_to_cfg_dict(proxy_url)
                if cfg:
                    return cfg
        
        # Priority 2: Database proxy (fallback)
        gw = self.get_current_gateway()
        if gw:
            return self.as_proxy_dict(gw)
        
        # Priority 3: Try queue-based proxy (check_live style)
        proxy_url = self.get_next_proxy_url()
        if proxy_url:
            cfg = self.parse_proxy_url_to_cfg(proxy_url)
            if cfg:
                return cfg
        
        return None
    
    def get_proxy_url_for_requests(self, prefer_rotating: bool = True) -> Optional[str]:
        """
        Get proxy URL in format ready for requests library.
        
        Returns:
            "http://user:pass@host:port" or "http://host:port" or None
        """
        # Rotating key has priority
        if prefer_rotating and self._rotating_proxy:
            return self.get_rotating_proxy_url(force_refresh=False)
        
        # Fallback to database/queue proxy
        cfg = self.get_proxy_for_request(prefer_rotating=False)
        if not cfg:
            return None
        
        host = cfg.get("host")
        port = cfg.get("port")
        user = cfg.get("username")
        pwd = cfg.get("password")
        
        if not (host and port):
            return None
        
        if user:
            return f"http://{user}:{pwd}@{host}:{port}"
        return f"http://{host}:{port}"
    
    # --------- check_live-style queue API (proxy_url) ----------
    def get_next_proxy_url(self) -> Optional[str]:
        """
        Lấy 1 proxy_url dạng http://user:pass@host:port từ queue local (giống check_live.py).
        Dùng cho mỗi request; sau khi dùng xong, hãy gọi return_proxy_url().
        """
        self._init_local_pool()
        if not self._proxy_queue:
            print(f"⚠️ GET_NEXT_PROXY_URL - Queue is None")
            return None
        try:
            proxy_url = self._proxy_queue.get_nowait()
            queue_size = self._proxy_queue.qsize()
            print(f"✅ GET_NEXT_PROXY_URL - Got proxy_url='{proxy_url[:50]}...', queue_size={queue_size}")
            return proxy_url
        except Exception as e:
            queue_size = self._proxy_queue.qsize() if self._proxy_queue else 0
            print(f"⚠️ GET_NEXT_PROXY_URL - Queue empty or error: {e}, queue_size={queue_size}")
            return None

    def return_proxy_url(self, proxy_url: Optional[str]) -> None:
        """
        Trả proxy_url lại queue để tái sử dụng (giống check_live.py).
        Nếu proxy gặp lỗi nặng (401/unusual_activity) thì KHÔNG nên gọi hàm này.
        """
        if not proxy_url:
            print(f"⚠️ RETURN_PROXY_URL - proxy_url is None/empty")
            return
        if not self._proxy_queue:
            print(f"⚠️ RETURN_PROXY_URL - Queue is None")
            return
        try:
            self._proxy_queue.put_nowait(proxy_url)
            queue_size = self._proxy_queue.qsize()
            print(f"✅ RETURN_PROXY_URL - Returned proxy_url='{proxy_url[:50]}...', queue_size={queue_size}")
        except Exception as e:
            queue_size = self._proxy_queue.qsize() if self._proxy_queue else 0
            print(f"⚠️ RETURN_PROXY_URL - Queue full or error: {e}, queue_size={queue_size}")

    # Back-compat
    def get_proxy_config(self) -> Optional[Dict[str, str]]:
        return self.as_proxy_dict(self.get_current_gateway())

    def get_token_proxy(self) -> Optional[str]:
        cur = self.get_current_gateway()
        return cur.token_proxy if cur else None
    
    def is_canada_proxy(self, gw: GatewayCfg) -> bool:
        """Check if gateway is Canada proxy (auto-rotating)"""
        return hasattr(gw, 'token_proxy') and getattr(gw, 'token_proxy', '').lower() == 'canada'
    
    def get_canada_proxies(self) -> List[GatewayCfg]:
        """Get all Canada proxies"""
        return [gw for gw in self.get_all_gateways() if self.is_canada_proxy(gw)]
    
    def get_standard_proxies(self) -> List[GatewayCfg]:
        """Get all non-Canada proxies"""
        return [gw for gw in self.get_all_gateways() if not self.is_canada_proxy(gw)]
    
    def should_rotate_canada_proxy(self, gw: GatewayCfg) -> bool:
        """Check if Canada proxy should be rotated (based on 60s timer)"""
        if not self.is_canada_proxy(gw):
            return False
        
        now = time.time()
        time_since_last = now - gw.last_rotated
        
        # Canada proxies auto-rotate every 60 seconds
        return time_since_last >= 60
    
    def wait_for_canada_rotation(self, gw: GatewayCfg, timeout: int = 70) -> Optional[str]:
        """Wait for Canada proxy to auto-rotate and return new IP"""
        if not self.is_canada_proxy(gw):
            return None
            
        old_ip = self.probe_ip(gw, label="canada-pre-wait")
        print(f"🇨🇦 Canada proxy {gw.host}:{gw.port} - Waiting for auto-rotation from IP {old_ip}")
        
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)  # Check every 5 seconds
            new_ip = self.probe_ip(gw, label="canada-rotation-poll")
            
            if new_ip and new_ip != old_ip:
                print(f"🇨🇦 Canada proxy auto-rotation detected: {old_ip} → {new_ip}")
                gw.last_ip = new_ip
                gw.last_rotated = time.time()
                return new_ip
                
        print(f"🇨🇦 Canada proxy rotation timeout - IP still {old_ip}")
        return None

    # ----------------- probe & rotate with health tracking -----------------
    def probe_ip(self, gw: GatewayCfg, label: str = "", force: bool = False) -> Optional[str]:
        """
        Probe IP with health check caching (2min TTL).
        Set force=True to bypass cache.
        """
        now = time.time()
        
        # Check if health check is cached (unless force)
        if not force and self._health_check_enabled:
            age = now - gw.last_health_check
            if age < self._health_check_ttl and gw.health_status in ["healthy", "degraded"]:
                # Use cached result
                print(f"🟢 PROXY_HEALTH_CACHED {label} {gw.host}:{gw.port} - {gw.health_status} (age={age:.0f}s)")
                return gw.last_ip
        
        # Perform actual health check via HTTP request (no curl)
        url = f"https://api.ipify.org?format=text&r={uuid.uuid4().hex}"

        if gw.username:
            proxy_url = f"http://{gw.username}:{gw.password}@{gw.host}:{gw.port}"
        else:
            proxy_url = f"http://{gw.host}:{gw.port}"

        proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }

        import requests
        from requests.exceptions import RequestException

        start_time = time.time()
        try:
            resp = requests.get(
                url,
                proxies=proxies,
                timeout=(20, 30),
                headers={
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            latency_ms = (time.time() - start_time) * 1000
            ip = (resp.text or "").strip() if resp.status_code == 200 else ""
            ok = bool(ip)
        except RequestException as exc:
            latency_ms = (time.time() - start_time) * 1000
            ip = ""
            ok = False
            print(f"🔴 PROXY_HEALTH_HTTP_EXC {label} {gw.host}:{gw.port} - {exc}")
        
        # Update health tracking
        gw.last_checked = now
        gw.last_health_check = now
        
        if ok:
            gw.last_ip = ip
            gw.score = min(gw.score + 1, 10)
            gw.success_count += 1
            gw.failure_count = 0  # Reset failure count on success
            
            # Update average latency (exponential moving average)
            if gw.avg_latency_ms == 0:
                gw.avg_latency_ms = latency_ms
            else:
                gw.avg_latency_ms = (gw.avg_latency_ms * 0.7) + (latency_ms * 0.3)
            
            # Determine health status based on latency and success rate
            if latency_ms < 500 and gw.success_count >= 3:
                gw.health_status = "healthy"
            elif latency_ms < 1500:
                gw.health_status = "degraded"
            else:
                gw.health_status = "unhealthy"
            
            print(f"🟢 PROXY_HEALTH_OK {label} {gw.host}:{gw.port} - ip={ip}, latency={latency_ms:.0f}ms, status={gw.health_status}")
        else:
            gw.score = max(gw.score - 2, -10)
            gw.failure_count += 1
            gw.success_count = 0  # Reset success count on failure
            
            # Mark as unhealthy after 2 consecutive failures
            if gw.failure_count >= 2:
                gw.health_status = "unhealthy"
            else:
                gw.health_status = "degraded"
            
            print(f"🔴 PROXY_HEALTH_FAIL {label} {gw.host}:{gw.port} - failures={gw.failure_count}, status={gw.health_status}")
        
        return ip if ok else None

    def rotate_gateway(self, gw: GatewayCfg) -> bool:
        if not gw.reset_link:
            return False
        url = f"{gw.reset_link}?t={uuid.uuid4().hex}"
        import requests
        from requests.exceptions import RequestException
        try:
            resp = requests.get(
                url,
                timeout=12,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            return resp.status_code == 200
        except RequestException:
            return False

    def trigger_reset_smart(self, gw: GatewayCfg) -> tuple[bool, str]:
        """
        CENTRALIZED reset link call - prevents duplicate calls.
        Returns: (was_reset_called, message)
        
        Logic:
        - If reset was called within cooldown period: skip, return (False, "COOLDOWN")
        - If another thread is already calling: skip, return (False, "IN_PROGRESS")  
        - Otherwise: call reset, wait 5s, return (True, "RESET_OK")
        """
        now = time.time()
        
        with self._lock:
            # Check if reset was recently called
            time_since_last = now - self._last_global_reset_time
            if time_since_last < self._reset_cooldown:
                remaining = self._reset_cooldown - time_since_last
                print(f"🔄 RESET_SKIP - Cooldown active, {remaining:.1f}s remaining")
                return (False, "COOLDOWN")
            
            # Check if another thread is already resetting
            if self._reset_in_progress:
                print(f"🔄 RESET_SKIP - Another thread already calling reset")
                return (False, "IN_PROGRESS")
            
            # Mark as in progress
            self._reset_in_progress = True
        
        try:
            # Call reset link
            print(f"🔄 RESET_CALLING - {gw.host}:{gw.port}")
            ok = self.rotate_gateway(gw)
            
            if ok:
                print(f"✅ RESET_SUCCESS - Waiting 5s for IP change...")
                time.sleep(5)  # Mandatory wait for IP to change
                
                # Probe new IP
                new_ip = self.probe_ip(gw, label="post-reset", force=True)
                if new_ip:
                    print(f"✅ RESET_NEW_IP - {new_ip}")
                    gw.last_ip = new_ip
                
                with self._lock:
                    self._last_global_reset_time = time.time()
                    gw.last_rotated = time.time()
                
                return (True, "RESET_OK")
            else:
                print(f"❌ RESET_FAILED - Reset link call failed")
                return (False, "RESET_FAILED")
        finally:
            with self._lock:
                self._reset_in_progress = False

    def wait_for_new_ip(
        self,
        gw: GatewayCfg,
        old_ip: str | None,
        wait_timeout_sec: int = 20,
        poll_every_sec: int = 3,
    ) -> Optional[str]:
        end = time.time() + max(5, int(wait_timeout_sec))
        last_seen = None
        while time.time() < end:
            new_ip = self.probe_ip(gw, label="poll", force=True)  # Force bypass cache
            if new_ip:
                last_seen = new_ip
                if old_ip and new_ip != old_ip:
                    return new_ip
            time.sleep(max(1, int(poll_every_sec)))
        return last_seen

    def choose_best_gateway(self) -> Optional[GatewayCfg]:
        """
        Choose best gateway based on health status, score, and latency.
        Priority: healthy > degraded > unhealthy
        Within same health tier: lower latency first
        """
        gws = self.get_all_gateways()
        if not gws:
            return None
        
        # Probe all gateways if needed (use cache if available)
        for gw in gws:
            self.probe_ip(gw, label="init", force=False)
        
        # Smart sorting: health_status (healthy first), then latency (low first), then score (high first)
        def sort_key(gw: GatewayCfg):
            # Health priority: healthy=3, degraded=2, unhealthy=1, unknown=0
            health_priority = {
                "healthy": 3,
                "degraded": 2,
                "unhealthy": 1,
                "unknown": 0
            }.get(gw.health_status, 0)
            
            # Latency penalty (lower is better)
            latency_score = -gw.avg_latency_ms if gw.avg_latency_ms > 0 else -9999
            
            # Return tuple for sorting (higher values first)
            return (health_priority, latency_score, gw.score)
        
        gws.sort(key=sort_key, reverse=True)
        
        # Log selection
        if gws:
            best = gws[0]
            print(f"🎯 BEST_GATEWAY_SELECTED: {best.host}:{best.port} (health={best.health_status}, latency={best.avg_latency_ms:.0f}ms, score={best.score})")
            with self._lock:
                # reset vòng tròn nhưng không cache pool proxy
                self._idx = 0
            return best
        
        return None

    # ----------------- auto-rotator (mỗi phút) -----------------
    def start_auto_rotate(self, rotate_every: int = 60) -> None:
        """Bật thread xoay proxy ~ mỗi phút (có jitter), round-robin qua các gateway."""
        with self._lock:
            self._rotate_every = max(20, int(rotate_every))  # không cho <20s
            if self._rotator_thread and self._rotator_thread.is_alive():
                return
            self._rotator_stop.clear()
            self._rotator_thread = threading.Thread(target=self._rotator_loop, name="proxy_rotator", daemon=True)
            self._rotator_thread.start()
        
        # Log proxy types for monitoring
        canada_count = len(self.get_canada_proxies())
        standard_count = len(self.get_standard_proxies())
        print(f"🔄 Auto-rotation started: {canada_count} Canada proxies (60s auto), {standard_count} standard proxies (reset link)")
        
        if canada_count > 0:
            print("🇨🇦 Canada proxies will auto-rotate every 60 seconds (no reset link needed)")
        if standard_count > 0:
            print("🌐 Standard proxies will use reset links for rotation")

    def stop_auto_rotate(self) -> None:
        with self._lock:
            if self._rotator_thread and self._rotator_thread.is_alive():
                self._rotator_stop.set()
        # không join để không block UI; nếu cần, có thể join với timeout nhỏ

    def _rotator_loop(self) -> None:
        import random
        while not self._rotator_stop.is_set():
            try:
                gws = self.get_all_gateways()
                if not gws:
                    # không có gateway → chờ rồi thử load lại
                    time.sleep(5)
                    self._load_all_from_db()
                    continue

                # chọn gateway theo round-robin nhưng có tôn trọng cooldown
                with self._lock:
                    gw = self.advance_round_robin()
                    if not gw:
                        time.sleep(5)
                        continue
                    now = time.time()
                    
                    # Check cooldown - khác nhau cho Canada vs standard proxy
                    is_canada = hasattr(gw, 'token_proxy') and getattr(gw, 'token_proxy', '').lower() == 'canada'
                    if is_canada:
                        if gw.reset_link:
                            cooldown_time = 6  # Minimum 5s as requested -> set 6s for safety
                        else:
                            cooldown_time = 60 # Auto rotate every 60s if no link
                    else:
                        cooldown_time = self._gateway_cooldown
                    
                    if (now - gw.last_rotated) < cooldown_time or gw.rotating:
                        # gateway này còn cooldown → thử gateway kế
                        gw2 = self.advance_round_robin()
                        if gw2:
                            # Check cooldown of next gateway too
                            is_canada2 = hasattr(gw2, 'token_proxy') and getattr(gw2, 'token_proxy', '').lower() == 'canada'
                            cooldown_time2 = 60 if is_canada2 else self._gateway_cooldown
                            if (now - gw2.last_rotated) < cooldown_time2:
                                # Skip this round if both are in cooldown
                                time.sleep(5)
                                continue
                            gw = gw2

                    gw.rotating = True

                old_ip = gw.last_ip or self.probe_ip(gw, label="pre-rotate")
                
                # Check if this is Canada proxy
                is_canada = hasattr(gw, 'token_proxy') and getattr(gw, 'token_proxy', '').lower() == 'canada'
                
                if is_canada:
                    # Canada proxy - check if reset link is available
                    if gw.reset_link:
                        print(f"🇨🇦 Canada proxy {gw.host}:{gw.port} - Has reset link, attempting FORCE ROTATION")
                        ok = self.rotate_gateway(gw)
                        if ok:
                            print(f"🇨🇦 Canada proxy reset requested successfully")
                        else:
                            print(f"⚠️ Canada proxy reset request failed")
                    else:
                        print(f"🇨🇦 Canada proxy {gw.host}:{gw.port} - Auto rotation (no reset link available)")
                    
                    # Wait for IP change (either from force rotate or auto rotate)
                    new_ip = self.wait_for_new_ip(gw, old_ip=old_ip, wait_timeout_sec=75, poll_every_sec=5)
                    
                    with self._lock:
                        gw.last_rotated = time.time()
                        gw.rotating = False
                        if new_ip and (not old_ip or new_ip != old_ip):
                            gw.failed_rotates = 0
                            print(f"🇨🇦 Canada proxy rotation success: {old_ip} → {new_ip}")
                        else:
                            gw.failed_rotates += 1
                            print(f"🇨🇦 Canada proxy rotation no change: IP still {new_ip or old_ip}")
                else:
                    # Standard proxy - use reset link
                    ok = self.rotate_gateway(gw)
                    if ok:
                        new_ip = self.wait_for_new_ip(gw, old_ip=old_ip, wait_timeout_sec=20, poll_every_sec=3)
                        with self._lock:
                            gw.last_rotated = time.time()
                            gw.rotating = False
                            gw.failed_rotates = 0 if new_ip and (not old_ip or new_ip != old_ip) else gw.failed_rotates
                    else:
                        with self._lock:
                            gw.rotating = False
                            gw.failed_rotates += 1
                            # backoff nhẹ nếu lỗi liên tục
                            if gw.failed_rotates >= 3:
                                gw.last_rotated = time.time() + 30  # delay thêm 30s

            except Exception as e:
                # swallow và tiếp tục vòng
                print(f"⚠️ proxy rotator error: {e}")

            # ngủ ~rotate_every (+ jitter)
            jitter = random.randint(self._jitter_min, self._jitter_max)
            sleep_sec = max(10, self._rotate_every + jitter)
            # cho phép dừng sớm
            for _ in range(sleep_sec):
                if self._rotator_stop.is_set():
                    break
                time.sleep(1)
