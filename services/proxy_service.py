# services/proxy_service.py
"""
Flexible proxy service - hỗ trợ nhiều hãng proxy.

Database table: users_token_proxy
- port: proxy string (format: username:password:hostname:port HOẶC proxyxoay key)
- reset_link: HTTP link để xoay IP (cho các hãng khác ngoài proxyxoay)
- token_proxy: label để phân biệt hãng/loại proxy (VD: "lunaproxy", "proxyxoay", etc.)

🔧 PHÂN LOẠI PROXY:
1. PROXYXOAY.SHOP:
   - token_proxy chứa "proxyxoay" HOẶC port là key 15-30 ký tự alphanumeric
   - Fetch proxy từ API proxyxoay.shop
   - TTL 60s
   
2. CÁC HÃNG KHÁC (LunaProxy, etc.):
   - Format: username:password:hostname:port
   - Xoay IP bằng reset_link (HTTP GET, check status code)
   - TTL 60s

🔧 RETRY LOGIC (v2 - Fast Failover):
- Gặp 401 "Unusual activity" → Switch proxy NGAY, không retry
- Shared blacklist: Track proxy bị 401 globally (dùng chung cho 50+ users)
- Round-robin với skip: Bỏ qua proxy đang bị blacklist
- Auto-unblock sau TTL (30-60s)
"""
from __future__ import annotations

import os
import time
import threading
import requests
import uuid
import re
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse


# ============== SHARED PROXY BLACKLIST ==============
# Global blacklist dùng chung cho tất cả ProxyService instances
# Key: proxy_identifier (api_key hoặc proxy_url hash)
# Value: (blocked_until_timestamp, reason)
_SHARED_BLACKLIST: Dict[str, Tuple[float, str]] = {}
_BLACKLIST_LOCK = threading.Lock()

# Blacklist TTL (seconds)
BLACKLIST_TTL_401 = 60  # 401 Unusual activity → block 60s
BLACKLIST_TTL_429 = 30  # 429 Rate limit → block 30s
BLACKLIST_TTL_NETWORK = 15  # Network error → block 15s


def _get_proxy_identifier(proxy: Dict) -> str:
    """Get unique identifier for proxy (dùng cho shared blacklist)"""
    if proxy.get("type") == "proxyxoay":
        return f"proxyxoay:{proxy.get('api_key', '')[:20]}"
    else:
        # Hash proxy URL để tránh lộ credentials
        url = proxy.get("proxy_url", "")
        return f"regular:{hash(url)}"


def _is_proxy_blacklisted(identifier: str) -> Tuple[bool, int]:
    """
    Check if proxy is in shared blacklist.
    
    Returns:
        Tuple[is_blacklisted, remaining_seconds]
    """
    with _BLACKLIST_LOCK:
        if identifier not in _SHARED_BLACKLIST:
            return False, 0
        
        blocked_until, reason = _SHARED_BLACKLIST[identifier]
        now = time.time()
        
        if now >= blocked_until:
            # Expired, remove from blacklist
            del _SHARED_BLACKLIST[identifier]
            return False, 0
        
        return True, int(blocked_until - now)


def _add_to_blacklist(identifier: str, ttl: int, reason: str):
    """Add proxy to shared blacklist"""
    with _BLACKLIST_LOCK:
        blocked_until = time.time() + ttl
        _SHARED_BLACKLIST[identifier] = (blocked_until, reason)
        print(f"🚫 [SharedBlacklist] Added {identifier[:30]}... for {ttl}s ({reason})")


def _remove_from_blacklist(identifier: str):
    """Remove proxy from shared blacklist"""
    with _BLACKLIST_LOCK:
        if identifier in _SHARED_BLACKLIST:
            del _SHARED_BLACKLIST[identifier]
            print(f"✅ [SharedBlacklist] Removed {identifier[:30]}...")


class ProxyService:
    """
    Flexible proxy service hỗ trợ nhiều hãng proxy.
    
    Features:
    - Load proxy từ database users_token_proxy
    - Parse nhiều format proxy string
    - Hỗ trợ proxyxoay.shop (API fetch)
    - Hỗ trợ các hãng khác (reset_link HTTP)
    - Retry logic 5-10 lần trước khi chuyển proxy
    - Round-robin rotation
    """
    
    # Constants
    PROXY_TTL = 60  # 60s - TTL cho tất cả proxy
    MAX_RETRY_BEFORE_SWITCH_SINGLE = 3  # Khi chỉ có 1 proxy - retry 3 lần trước khi fail
    MAX_RETRY_BEFORE_SWITCH_MULTI = 1   # Khi có nhiều proxy - switch ngay sau 1 lần fail
    MAX_RETRY_BEFORE_SWITCH = 1  # 🔧 Backward compatible - dùng cho code cũ
    PROXYXOAY_API = "https://proxyxoay.shop/api/get.php"
    
    # Anti-detection settings
    MIN_REQUEST_INTERVAL = 0.5  # Minimum 0.5s giữa các request để tránh bị detect
    BLOCKED_COOLDOWN = 5  # Cooldown 5s khi bị chặn trước khi retry
    
    def __init__(self, supabase=None, user_id: int = None):
        self.supabase = supabase
        self.user_id = user_id

        # Proxy list
        self._proxies: List[Dict] = []
        self._current_index: int = 0
        self._lock = threading.Lock()
        
        # Retry tracking per proxy
        self._fail_counts: Dict[int, int] = {}  # proxy_index -> fail_count
        self._blocked_until: Dict[int, float] = {}  # proxy_index -> timestamp when unblocked
        self._last_request_time: float = 0  # Last request timestamp for rate limiting
        
        # Cache for rotating proxies (proxyxoay or reset_link based)
        self._proxy_cache: Dict[int, Tuple[str, float]] = {}  # proxy_index -> (proxy_url, expire_time)
        
        # Health tracking
        self._health_cache: Dict[str, Tuple[bool, float]] = {}
        self._health_ttl: int = 120
        
        # Load from database on init
        if self.supabase and self.user_id:
            self.load_from_database()
    
    def load_from_database(self) -> int:
        """
        Load proxies từ database users_token_proxy.
        
        Returns:
            Số proxy đã load
        """
        if not (self.supabase and self.user_id):
            print("⚠️ [ProxyService] No supabase or user_id - cannot load from DB")
            return 0
        
        try:
            from services.db_retry_helper import safe_db_operation
            
            def _query():
                return (
                    self.supabase.table("users_token_proxy")
                    .select("port, reset_link, token_proxy")
                    .eq("user_id", self.user_id)
                    .execute()
                )
            
            result = safe_db_operation(_query, max_retries=3, default_return=None)
            rows = result.data if result else []
            print(f"🔍 [ProxyService] DB query returned {len(rows)} row(s)")
        except Exception as e:
            print(f"❌ [ProxyService] DB load error: {e}")
            import traceback
            traceback.print_exc()
            rows = []
        
        proxies = []
        for row in rows:
            print(f"   📦 Processing row: token_proxy='{row.get('token_proxy')}', port='{row.get('port', '')[:30]}...'")
            port_str = (row.get("port") or "").strip().replace('\n', '').replace('\r', '')
            token_proxy = (row.get("token_proxy") or "").strip().lower()
            reset_link = (row.get("reset_link") or "").strip()
            
            if not port_str:
                continue
            
            # Phân loại proxy
            proxy_type = self._detect_proxy_type(port_str, token_proxy)
            
            if proxy_type == "proxyxoay":
                # Proxyxoay - sử dụng API
                print(f"🔑 [ProxyService] Detected PROXYXOAY: {port_str[:15]}...")
                proxies.append({
                    "proxy_url": None,  # Will be fetched from API
                    "reset_link": "",
                    "label": token_proxy or "proxyxoay",
                    "original": port_str,
                    "type": "proxyxoay",
                    "api_key": port_str,
                    "expired": False,  # Will be checked later
                    "expiry_date": None
                })
            else:
                # Các hãng khác - parse proxy string
                proxy_url = self._parse_proxy_string(port_str)
                if proxy_url:
                    print(f"🌐 [ProxyService] Detected REGULAR proxy: {proxy_url[:40]}... ({token_proxy or 'unknown'})")
                    proxies.append({
                        "proxy_url": proxy_url,
                        "reset_link": reset_link,
                        "label": token_proxy or "regular",
                        "original": port_str,
                        "type": "regular",
                        "expired": False,
                        "expiry_date": None
                    })

        with self._lock:
            self._proxies = proxies
            self._current_index = 0
            self._fail_counts = {i: 0 for i in range(len(proxies))}
            self._blocked_until = {i: 0 for i in range(len(proxies))}
            self._proxy_cache = {}
        
        print(f"✅ [ProxyService] Loaded {len(proxies)} proxy(ies) for user {self.user_id}")
        for i, p in enumerate(proxies, 1):
            if p["type"] == "proxyxoay":
                print(f"   {i}. [Proxyxoay] Key: {p['api_key'][:10]}... (TTL {self.PROXY_TTL}s)")
            else:
                reset_info = "✓ reset_link" if p["reset_link"] else "✗ no reset"
                print(f"   {i}. [{p['label']}] {p['proxy_url'][:40]}... ({reset_info})")
        
        return len(proxies)
    
    def _detect_proxy_type(self, port_str: str, token_proxy: str) -> str:
        """
        Detect proxy type:
        - "proxyxoay": token_proxy chứa "proxyxoay" HOẶC port là key alphanumeric 15-30 chars
        - "regular": các trường hợp còn lại
        """
        # Check token_proxy first
        if "proxyxoay" in token_proxy:
            return "proxyxoay"
        
        # Check if port is proxyxoay key (15-30 alphanumeric, no special chars)
        clean = port_str.strip()
        if 15 <= len(clean) <= 30:
            if ':' not in clean and '@' not in clean and '.' not in clean:
                if clean.isalnum():
                    return "proxyxoay"
        
        return "regular"
    
    def _parse_proxy_string(self, proxy_str: str) -> Optional[str]:
        """
        Parse proxy string sang format chuẩn: http://[user:pass@]host:port
        
        Hỗ trợ format:
        - hostname:port
        - hostname:port:username:password
        - username:password:hostname:port
        - username:password@hostname:port
        - http://hostname:port
        - http://username:password@hostname:port
        """
        p = proxy_str.strip().replace('\n', '').replace('\r', '')
        if not p:
            return None
        
        # Đã có scheme
        if "://" in p:
            return p
        
        # Format: user:pass@host:port
        if "@" in p:
            auth_part, host_part = p.rsplit("@", 1)
            if ":" in auth_part and ":" in host_part:
                return f"http://{auth_part}@{host_part}"
        
        parts = [x.strip() for x in p.split(":")]
        
        if len(parts) == 2:
            host, port = parts
            return f"http://{host}:{port}"
        
        elif len(parts) == 4:
            # Check username:password:hostname:port (phổ biến hơn)
            try:
                port_val = int(parts[3])
                if 1 <= port_val <= 65535 and '.' in parts[2]:
                    user, passwd, host, port = parts
                    return f"http://{user}:{passwd}@{host}:{port}"
            except ValueError:
                pass
            
            # Check hostname:port:username:password
            try:
                port_val = int(parts[1])
                if 1 <= port_val <= 65535 and '.' in parts[0]:
                    host, port, user, passwd = parts
                    return f"http://{user}:{passwd}@{host}:{port}"
            except ValueError:
                pass
            
            # Fallback
            user, passwd, host, port = parts
            return f"http://{user}:{passwd}@{host}:{port}"
        
        elif len(parts) >= 5:
            # Complex format
            try:
                port_val = int(parts[-1])
                if 1 <= port_val <= 65535 and '.' in parts[-2]:
                    port = parts[-1]
                    host = parts[-2]
                    user = parts[0]
                    passwd = ":".join(parts[1:-2])
                    return f"http://{user}:{passwd}@{host}:{port}"
            except ValueError:
                pass
            
            return f"http://{p}"
        else:
            return f"http://{p}"
    
    def _fetch_proxyxoay(self, api_key: str, wait_for_cooldown: bool = False) -> Optional[str]:
        """
        Fetch proxy từ proxyxoay.shop API.
        
        Args:
            wait_for_cooldown: Nếu True, đợi khi gặp cooldown (CHỈ DÙNG TRONG WORKER THREAD)
                              Nếu False (default), return None ngay để không block UI
        
        Returns:
            proxy_url or None
        """
        max_retries = 2 if wait_for_cooldown else 1
        
        for attempt in range(max_retries):
            try:
                url = f"{self.PROXYXOAY_API}?key={api_key}&nhamang=random&tinhthanh=random"
                resp = requests.get(url, timeout=10)
                
                if resp.status_code != 200:
                    print(f"❌ [Proxyxoay] API HTTP {resp.status_code}")
                    return None
                
                data = resp.json()
                
                if data.get("status") == 100:
                    proxy_http = data.get("proxyhttp", "")
                    if proxy_http:
                        parts = proxy_http.split(":")
                        if len(parts) >= 2:
                            ip, port = parts[0], parts[1]
                            
                            if len(parts) >= 4 and parts[2] and parts[3]:
                                proxy_url = f"http://{parts[2]}:{parts[3]}@{ip}:{port}"
                            else:
                                proxy_url = f"http://{ip}:{port}"
                            
                            location = data.get("Vi Tri", "")
                            isp = data.get("Nha Mang", "")
                            info_str = f" ({isp}/{location})" if isp or location else ""
                            
                            print(f"✅ [Proxyxoay] Got: {ip}:{port}{info_str}")
                            return proxy_url
                
                msg = data.get('message', 'Unknown error')
                print(f"❌ [Proxyxoay] API error: {msg}")
                
                # 🔧 FIX: Chỉ đợi cooldown nếu được yêu cầu VÀ đang trong worker thread
                if wait_for_cooldown and ("moi co the" in msg.lower() or "còn" in msg.lower() or "giây" in msg.lower() or "s " in msg.lower()):
                    # Parse số giây từ message (VD: "Còn 33s mới có thể đổi proxy")
                    import re
                    match = re.search(r'(\d+)\s*s', msg.lower())
                    if match:
                        wait_seconds = min(int(match.group(1)) + 2, 35)  # Cap at 35s
                    else:
                        wait_seconds = 35  # Default 35s
                    
                    if attempt < max_retries - 1:
                        print(f"⏳ [Proxyxoay] Cooldown {wait_seconds}s - đợi và retry...")
                        time.sleep(wait_seconds)
                        continue
                
                return None
            
            except Exception as e:
                print(f"❌ [Proxyxoay] Fetch error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None
        
        return None
    
    def _call_reset_link(self, reset_link: str) -> bool:
        """
        Gọi reset_link để xoay IP (cho các hãng khác ngoài proxyxoay).
        
        Returns:
            True nếu thành công (HTTP 2xx), False otherwise
        """
        if not reset_link:
            return False
        
        try:
            # Add random param to avoid cache
            sep = "&" if "?" in reset_link else "?"
            url = f"{reset_link}{sep}t={uuid.uuid4().hex}"
            
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            
            # Check status code - 2xx là thành công
            success = 200 <= resp.status_code < 300
            
            if success:
                print(f"✅ [ProxyService] Reset link OK (HTTP {resp.status_code})")
            else:
                print(f"❌ [ProxyService] Reset link failed (HTTP {resp.status_code})")
            
            return success
        except Exception as e:
            print(f"❌ [ProxyService] Reset link error: {e}")
            return False
    
    def get_current_proxy(self) -> Optional[str]:
        """
        Get current proxy URL.
        
        - Proxyxoay: Fetch từ API với TTL caching
        - Regular: Return proxy_url trực tiếp (có thể gọi reset_link nếu cần)
        - Auto-skip blocked proxies (local + shared blacklist)
        
        🔧 v2: Check shared blacklist trước để tận dụng thông tin từ các users khác
        """
        with self._lock:
            if not self._proxies:
                return None
            
            # Nếu chỉ có 1 proxy - luôn trả về nó (không block)
            if len(self._proxies) == 1:
                proxy = self._proxies[0]
                if proxy["type"] == "proxyxoay":
                    return self._get_proxyxoay_url(0, proxy)
                else:
                    return self._get_regular_url(0, proxy)
            
            # Có nhiều proxy - tìm proxy không bị block (local + shared)
            attempts = 0
            
            while attempts < len(self._proxies):
                idx = self._current_index % len(self._proxies)
                proxy = self._proxies[idx]
                
                # 🔧 v2: Check SHARED blacklist trước
                identifier = _get_proxy_identifier(proxy)
                is_blacklisted, remaining = _is_proxy_blacklisted(identifier)
                if is_blacklisted:
                    print(f"⏳ [ProxyService] Proxy #{idx+1} in SHARED blacklist ({remaining}s), skipping...")
                    self._current_index = (self._current_index + 1) % len(self._proxies)
                    attempts += 1
                    continue
                
                # Check LOCAL block
                blocked_until = self._blocked_until.get(idx, 0)
                if time.time() < blocked_until:
                    remaining = int(blocked_until - time.time())
                    print(f"⏳ [ProxyService] Proxy #{idx+1} locally blocked ({remaining}s), skipping...")
                    self._current_index = (self._current_index + 1) % len(self._proxies)
                    attempts += 1
                    continue
                
                if proxy["type"] == "proxyxoay":
                    result = self._get_proxyxoay_url(idx, proxy)
                else:
                    result = self._get_regular_url(idx, proxy)
                
                if result:
                    return result
                
                # Proxy failed to get URL, try next
                self._current_index = (self._current_index + 1) % len(self._proxies)
                attempts += 1
            
            # All proxies blocked - find one with shortest remaining time
            print(f"⚠️ [ProxyService] All proxies blocked, finding shortest wait...")
            min_wait = float('inf')
            best_idx = 0
            now = time.time()
            
            for i, p in enumerate(self._proxies):
                identifier = _get_proxy_identifier(p)
                is_bl, bl_remaining = _is_proxy_blacklisted(identifier)
                local_remaining = max(0, self._blocked_until.get(i, 0) - now)
                total_wait = max(bl_remaining, local_remaining)
                
                if total_wait < min_wait:
                    min_wait = total_wait
                    best_idx = i
            
            self._current_index = best_idx
            proxy = self._proxies[best_idx]
            
            if proxy["type"] == "proxyxoay":
                return self._get_proxyxoay_url(best_idx, proxy)
            else:
                return self._get_regular_url(best_idx, proxy)
    
    def _get_proxyxoay_url(self, idx: int, proxy: Dict) -> Optional[str]:
        """Get proxy URL for proxyxoay type"""
        api_key = proxy.get("api_key")
        
        # Check cache
        cached = self._proxy_cache.get(idx)
        if cached:
            proxy_url, expire_time = cached
            if time.time() < expire_time:
                return proxy_url
        
        # Fetch new - không đợi cooldown khi get_current_proxy (để không block)
        new_url = self._fetch_proxyxoay(api_key, wait_for_cooldown=False)
        if new_url:
            self._proxy_cache[idx] = (new_url, time.time() + self.PROXY_TTL)
            return new_url
        
        # Fallback to cache if available
        if cached:
            return cached[0]
        
        return None
    
    def _get_regular_url(self, idx: int, proxy: Dict) -> Optional[str]:
        """Get proxy URL for regular type (các hãng khác)"""
        return proxy["proxy_url"]
    
    def _get_proxy_for_key(self, key_idx: int, wait_for_cooldown: bool = False) -> Optional[str]:
        """
        🔧 NEW: Lấy proxy URL cho một proxy key cụ thể (dùng cho multi-worker).
        
        Mỗi worker sẽ dùng một proxy key riêng để tránh xung đột.
        VD: Worker 1 dùng proxy key 0, Worker 2 dùng proxy key 1, ...
        
        Args:
            key_idx: Index của proxy key (0-based)
            wait_for_cooldown: Nếu True, đợi khi gặp cooldown từ proxyxoay
        
        Returns:
            Proxy URL hoặc None nếu không có
        """
        with self._lock:
            if not self._proxies:
                return None
            
            # Wrap index nếu vượt quá số proxy
            idx = key_idx % len(self._proxies)
            proxy = self._proxies[idx]
        
        # Lấy proxy URL (ngoài lock để không block các worker khác)
        if proxy["type"] == "proxyxoay":
            api_key = proxy.get("api_key")
            
            # Check cache trước
            cached = self._proxy_cache.get(idx)
            if cached:
                proxy_url, expire_time = cached
                if time.time() < expire_time:
                    return proxy_url
            
            # Fetch IP mới từ API
            new_url = self._fetch_proxyxoay(api_key, wait_for_cooldown=wait_for_cooldown)
            if new_url:
                with self._lock:
                    self._proxy_cache[idx] = (new_url, time.time() + self.PROXY_TTL)
                return new_url
            
            # Fallback to cache nếu có
            if cached:
                return cached[0]
            
            return None
        else:
            # Regular proxy - return URL trực tiếp
            return proxy.get("proxy_url")
    
    def report_failure(self, is_rate_limited: bool = False) -> bool:
        """
        Report proxy failure. Tăng fail count.
        
        🔧 FIX: Khi có NHIỀU proxy → SWITCH NGAY LẬP TỨC, không refresh.
        Vì có nhiều proxy key nên cứ xoay cho nhanh.
        
        Args:
            is_rate_limited: True nếu lỗi là 429/rate limit - sẽ block proxy lâu hơn
        
        Returns:
            True nếu đã chuyển sang proxy khác, False nếu còn retry
        """
        with self._lock:
            if not self._proxies:
                return False
            
            idx = self._current_index % len(self._proxies)
            self._fail_counts[idx] = self._fail_counts.get(idx, 0) + 1
            fail_count = self._fail_counts[idx]
            
            proxy = self._proxies[idx]
            
            print(f"⚠️ [ProxyService] Failure #{fail_count} for proxy [{proxy['label']}] (total: {len(self._proxies)} proxies)")
            
            # === CHỈ CÓ 1 PROXY - REFRESH IP ===
            if len(self._proxies) == 1:
                if is_rate_limited:
                    print(f"🔄 [ProxyService] Single proxy rate-limited, force refreshing IP...")
                    self._force_refresh_single_proxy(idx, proxy)
                    return False  # Không switch, chỉ refresh
                else:
                    # Lỗi thường - thử refresh IP
                    print(f"🔄 [ProxyService] Single proxy failed, refreshing IP...")
                    self._force_refresh_single_proxy(idx, proxy)
                    return False
            
            # === NHIỀU PROXY - SWITCH NGAY LẬP TỨC ===
            # 🔧 FIX: Không cần đếm fail_count, có nhiều proxy thì SWITCH NGAY
            block_duration = 10 if not is_rate_limited else 20  # 🔧 FIX: Giảm block time 15s→10s, 30s→20s
            self._blocked_until[idx] = time.time() + block_duration
            print(f"🔀 [ProxyService] SWITCH NGAY → blocking proxy #{idx+1} for {block_duration}s")
            
            return self._switch_to_next()
    
    def _force_refresh_single_proxy(self, idx: int, proxy: Dict) -> bool:
        """
        Force refresh IP cho proxy (dùng khi chỉ có 1 proxy hoặc cần refresh).
        
        Returns:
            True nếu refresh thành công
        """
        if proxy["type"] == "proxyxoay":
            # Proxyxoay - force fetch IP mới từ API
            print(f"🔄 [ProxyService] Force refresh proxyxoay...")
            api_key = proxy.get("api_key")
            
            # 🔧 FIX: Không đợi cooldown để không block - return cached proxy
            new_url = self._fetch_proxyxoay(api_key, wait_for_cooldown=False)
            if new_url:
                self._proxy_cache[idx] = (new_url, time.time() + self.PROXY_TTL)
                print(f"✅ [ProxyService] Got new IP from proxyxoay")
                return True
            
            # 🔧 FIX: Nếu đang cooldown, vẫn return True để tiếp tục dùng cached proxy
            cached = self._proxy_cache.get(idx)
            if cached:
                print(f"⚠️ [ProxyService] Proxyxoay cooldown - using cached proxy")
                return True
            
            print(f"⚠️ [ProxyService] Proxyxoay refresh failed, no cache")
            return False
        else:
            # Regular proxy - gọi reset_link để xoay IP
            if proxy["reset_link"]:
                print(f"🔄 [ProxyService] Calling reset_link to rotate IP...")
                success = self._call_reset_link(proxy["reset_link"])
                if success:
                    print(f"✅ [ProxyService] Reset link called, waiting for IP change...")
                    time.sleep(3)  # Wait for IP change
                    return True
                else:
                    print(f"⚠️ [ProxyService] Reset link failed")
                    return False
            else:
                print(f"⚠️ [ProxyService] No reset_link available for this proxy")
                return False
    
    def report_rate_limit(self) -> bool:
        """
        Report 429/rate limit error. 
        - Nếu có nhiều proxy: Block current proxy và chuyển ngay.
        - Nếu chỉ có 1 proxy: Force refresh IP và chờ.
        
        Returns:
            True nếu đã chuyển proxy, False nếu chỉ refresh (1 proxy)
        """
        return self.report_failure(is_rate_limited=True)
    
    def report_unusual_activity(self) -> Tuple[bool, Optional[str]]:
        """
        🔧 v2: Report 401 "Unusual activity detected" error từ ElevenLabs.
        
        ElevenLabs Free Tier rất nhạy với proxy/VPN detection.
        Khi gặp lỗi này, cần rotate sang proxy IP khác NGAY LẬP TỨC.
        
        🔧 FAST FAILOVER Strategy:
        - Add proxy vào SHARED blacklist (dùng chung cho 50+ users)
        - Switch sang proxy khác NGAY, không retry
        - Nếu chỉ có 1 proxy: Force refresh IP
        
        Returns:
            Tuple[switched, new_proxy_url]:
            - switched: True nếu đã switch/refresh proxy
            - new_proxy_url: URL của proxy mới (hoặc None nếu không có)
        """
        print(f"🚨 [ProxyService] Unusual activity detected - FAST FAILOVER...")
        
        with self._lock:
            if not self._proxies:
                return False, None
            
            idx = self._current_index % len(self._proxies)
            proxy = self._proxies[idx]
            
            # 🔧 v2: Add to SHARED blacklist (benefit all 50+ users)
            identifier = _get_proxy_identifier(proxy)
            _add_to_blacklist(identifier, BLACKLIST_TTL_401, "401_unusual_activity")
            
            # === CHỈ CÓ 1 PROXY - FORCE REFRESH IP ===
            if len(self._proxies) == 1:
                print(f"🔄 [ProxyService] Single proxy - force refreshing IP...")
                
                if proxy["type"] == "proxyxoay":
                    # Clear cache để force fetch IP mới
                    self._proxy_cache.pop(idx, None)
                    api_key = proxy.get("api_key")
                    
                    # Release lock trước khi gọi network
                    self._lock.release()
                    try:
                        new_url = self._fetch_proxyxoay(api_key, wait_for_cooldown=True)
                        if new_url:
                            with self._lock:
                                self._proxy_cache[idx] = (new_url, time.time() + self.PROXY_TTL)
                            # Remove from blacklist since we got new IP
                            _remove_from_blacklist(identifier)
                            print(f"✅ [ProxyService] Got new IP from proxyxoay: {new_url[:40]}...")
                            return True, new_url
                    finally:
                        self._lock.acquire()
                    
                    # Fallback to cached if available
                    cached = self._proxy_cache.get(idx)
                    if cached:
                        return True, cached[0]
                    return False, None
                else:
                    # Regular proxy - call reset_link
                    reset_link = proxy.get("reset_link")
                    if reset_link:
                        self._lock.release()
                        try:
                            success = self._call_reset_link(reset_link)
                            if success:
                                time.sleep(3)  # Wait for IP change
                                # Remove from blacklist since IP changed
                                _remove_from_blacklist(identifier)
                                print(f"✅ [ProxyService] Reset link called, IP should be changed")
                                return True, proxy.get("proxy_url")
                        finally:
                            self._lock.acquire()
                    return False, proxy.get("proxy_url")
            
            # === NHIỀU PROXY - SWITCH NGAY LẬP TỨC ===
            # 🔧 v2: Không cần block local nữa vì đã có shared blacklist
            print(f"🔀 [ProxyService] FAST SWITCH from proxy #{idx+1} (in shared blacklist for {BLACKLIST_TTL_401}s)")
            
            # Switch to next proxy
            self._switch_to_next()
            new_idx = self._current_index
        
        # Get new proxy URL (outside lock)
        new_url = self.get_current_proxy()
        if new_url:
            print(f"✅ [ProxyService] Switched to proxy #{new_idx+1}: {new_url[:40]}...")
        
        return True, new_url
    
    def report_401_error(self, error_msg: str = "") -> Tuple[bool, Optional[str]]:
        """
        🔧 v2 NEW: Report any 401 error - fast failover to next proxy.
        
        Gọi method này khi gặp BẤT KỲ lỗi 401 nào từ proxy.
        Sẽ add proxy vào shared blacklist và switch ngay.
        
        Args:
            error_msg: Error message để log
        
        Returns:
            Tuple[switched, new_proxy_url]
        """
        print(f"🚨 [ProxyService] 401 Error: {error_msg[:50]}... - FAST FAILOVER")
        
        # Check if it's unusual activity
        if "unusual" in error_msg.lower():
            return self.report_unusual_activity()
        
        with self._lock:
            if not self._proxies:
                return False, None
            
            idx = self._current_index % len(self._proxies)
            proxy = self._proxies[idx]
            
            # Add to shared blacklist với TTL ngắn hơn (30s cho 401 thường)
            identifier = _get_proxy_identifier(proxy)
            _add_to_blacklist(identifier, 30, f"401_error")
            
            if len(self._proxies) == 1:
                # Single proxy - try refresh
                return self._force_refresh_single_proxy_unlocked(idx, proxy)
            
            # Multiple proxies - switch immediately
            self._switch_to_next()
            new_idx = self._current_index
        
        new_url = self.get_current_proxy()
        return True, new_url
    
    def _force_refresh_single_proxy_unlocked(self, idx: int, proxy: Dict) -> Tuple[bool, Optional[str]]:
        """Force refresh single proxy (called with lock held, will release)"""
        identifier = _get_proxy_identifier(proxy)
        
        if proxy["type"] == "proxyxoay":
            self._proxy_cache.pop(idx, None)
            api_key = proxy.get("api_key")
            
            self._lock.release()
            try:
                new_url = self._fetch_proxyxoay(api_key, wait_for_cooldown=True)
                if new_url:
                    with self._lock:
                        self._proxy_cache[idx] = (new_url, time.time() + self.PROXY_TTL)
                    _remove_from_blacklist(identifier)
                    return True, new_url
            finally:
                self._lock.acquire()
            
            cached = self._proxy_cache.get(idx)
            return (True, cached[0]) if cached else (False, None)
        else:
            reset_link = proxy.get("reset_link")
            if reset_link:
                self._lock.release()
                try:
                    if self._call_reset_link(reset_link):
                        time.sleep(3)
                        _remove_from_blacklist(identifier)
                        return True, proxy.get("proxy_url")
                finally:
                    self._lock.acquire()
            return False, proxy.get("proxy_url")
    
    def _switch_to_next(self) -> bool:
        """Switch to random proxy (internal, called with lock held)"""
        if len(self._proxies) <= 1:
            # Reset fail count và thử lại
            self._fail_counts[0] = 0
            return False
        
        import random
        old_idx = self._current_index
        
        # 🔧 FIX: Chọn proxy NGẪU NHIÊN thay vì theo thứ tự
        # Loại bỏ proxy hiện tại và các proxy đang bị block
        available_indices = []
        now = time.time()
        for i in range(len(self._proxies)):
            if i == old_idx:
                continue  # Skip proxy hiện tại
            if self._blocked_until.get(i, 0) > now:
                continue  # Skip proxy đang bị block
            available_indices.append(i)
        
        if available_indices:
            self._current_index = random.choice(available_indices)
        else:
            # 🔧 FIX: Nếu tất cả đều bị block, chọn proxy có block time ngắn nhất
            other_indices = [i for i in range(len(self._proxies)) if i != old_idx]
            if other_indices:
                # Chọn proxy có block time ngắn nhất
                min_block_idx = min(other_indices, key=lambda i: self._blocked_until.get(i, 0))
                remaining = max(0, self._blocked_until.get(min_block_idx, 0) - now)
                if remaining > 0:
                    print(f"⏳ [ProxyService] Proxy #{min_block_idx+1} blocked for {int(remaining)}s more, trying next...")
                self._current_index = min_block_idx
            else:
                self._current_index = (old_idx + 1) % len(self._proxies)
        
        # Reset fail count cho proxy mới
        self._fail_counts[self._current_index] = 0
        
        new_proxy = self._proxies[self._current_index]
        print(f"✅ [ProxyService] Switched from #{old_idx+1} to #{self._current_index+1} [{new_proxy['label']}] (random)")
        
        return True
    
    def wait_for_rate_limit(self) -> None:
        """
        Wait để đảm bảo không gửi request quá nhanh (anti-detection).
        Gọi trước mỗi request.
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            
            if elapsed < self.MIN_REQUEST_INTERVAL:
                wait_time = self.MIN_REQUEST_INTERVAL - elapsed
                time.sleep(wait_time)
            
            self._last_request_time = time.time()
    
    def get_blocked_status(self) -> Dict[int, int]:
        """
        Get blocked status của tất cả proxy.
        
        Returns:
            Dict[proxy_index, remaining_seconds]
        """
        with self._lock:
            now = time.time()
            result = {}
            for idx, blocked_until in self._blocked_until.items():
                if blocked_until > now:
                    result[idx] = int(blocked_until - now)
            return result
    
    def report_success(self):
        """Report proxy success. Reset fail count."""
        with self._lock:
            if not self._proxies:
                return
            
            idx = self._current_index % len(self._proxies)
            if self._fail_counts.get(idx, 0) > 0:
                self._fail_counts[idx] = 0
                print(f"✅ [ProxyService] Reset fail count for proxy #{idx+1}")
    
    def rotate_to_next(self) -> Optional[str]:
        """
        Force rotate to next proxy (manual rotation).
        
        Returns:
            New proxy URL or None
        """
        with self._lock:
            if not self._proxies:
                return None
            
            self._switch_to_next()
            
        return self.get_current_proxy()
    
    def force_refresh(self, wait_for_cooldown: bool = False) -> Optional[str]:
        """
        Force refresh current proxy (proxyxoay: new API call, regular: call reset_link).
        
        Args:
            wait_for_cooldown: Nếu True, đợi khi gặp cooldown từ proxyxoay (CHỈ DÙNG TRONG WORKER)
                              Nếu False (default), return cached proxy để không block UI
        
        Returns:
            Refreshed proxy URL or None
        """
        # 🔧 FIX: Lấy thông tin proxy trước, release lock, rồi mới gọi network
        with self._lock:
            if not self._proxies:
                return None
            
            idx = self._current_index % len(self._proxies)
            proxy = self._proxies[idx].copy()  # Copy để tránh race condition
            cached = self._proxy_cache.get(idx)
        
        print(f"🔄 [ProxyService] Force refresh proxy [{proxy['label']}]...")
        
        if proxy["type"] == "proxyxoay":
            api_key = proxy.get("api_key")
            # 🔧 FIX: Gọi network NGOÀI lock để không block các worker khác
            new_url = self._fetch_proxyxoay(api_key, wait_for_cooldown=wait_for_cooldown)
            if new_url:
                with self._lock:
                    self._proxy_cache[idx] = (new_url, time.time() + self.PROXY_TTL)
                return new_url
            else:
                # 🔧 FIX: Nếu không lấy được IP mới, return cached proxy
                if cached:
                    print(f"⚠️ [ProxyService] Using cached proxy (cooldown)")
                    return cached[0]
                else:
                    print(f"⚠️ [ProxyService] No cached proxy available")
                    return None
        else:
            if proxy.get("reset_link"):
                success = self._call_reset_link(proxy["reset_link"])
                if success:
                    time.sleep(3)  # Wait for IP change
            return proxy.get("proxy_url")
        
        return self.get_current_proxy()
    
    def get_current_reset_link(self) -> Optional[str]:
        """Get reset link for current proxy (regular type only)"""
        with self._lock:
            if not self._proxies:
                return None
            proxy = self._proxies[self._current_index % len(self._proxies)]
            return proxy.get("reset_link") or None
    
    def call_reset_link(self, proxy_url: str = None) -> bool:
        """Call reset link cho current proxy"""
        reset_link = self.get_current_reset_link()
        if reset_link:
            return self._call_reset_link(reset_link)
        return False
    
    def check_proxy_health(self, proxy_url: str = None, force: bool = False) -> bool:
        """Check if proxy is healthy"""
        if proxy_url is None:
            proxy_url = self.get_current_proxy()
        
        if not proxy_url:
            return False
        
        # Check cache
        if not force:
            cached = self._health_cache.get(proxy_url)
            if cached:
                is_healthy, timestamp = cached
                if time.time() - timestamp < self._health_ttl:
                    return is_healthy
        
        # Health check
        try:
            test_url = f"https://api.ipify.org?format=text&r={uuid.uuid4().hex}"
            proxies = {"http": proxy_url, "https": proxy_url}
            
            resp = requests.get(test_url, proxies=proxies, timeout=(10, 15))
            is_healthy = resp.status_code == 200 and bool(resp.text.strip())
            
            self._health_cache[proxy_url] = (is_healthy, time.time())
            
            if is_healthy:
                print(f"🟢 [ProxyService] Healthy: {proxy_url[:40]}... → IP: {resp.text.strip()}")
            else:
                print(f"🔴 [ProxyService] Unhealthy: {proxy_url[:40]}...")
            
            return is_healthy
        except Exception as e:
            print(f"🔴 [ProxyService] Health check error: {e}")
            self._health_cache[proxy_url] = (False, time.time())
            return False
    
    def to_requests_dict(self, proxy_url: str = None) -> Optional[Dict[str, str]]:
        """Convert to requests proxies dict"""
        if proxy_url is None:
            proxy_url = self.get_current_proxy()
        
        if not proxy_url:
            return None
        
        return {"http": proxy_url, "https": proxy_url}
    
    def get_proxy_info(self) -> Dict:
        """Get current proxy info"""
        with self._lock:
            if not self._proxies:
                return {"total": 0, "current": None}
            
            idx = self._current_index % len(self._proxies)
            current = self._proxies[idx]
            
            return {
                "total": len(self._proxies),
                "current_index": idx,
                "current_type": current["type"],
                "current_label": current["label"],
                "fail_count": self._fail_counts.get(idx, 0),
                "has_reset_link": bool(current.get("reset_link"))
            }
    
    def start_auto_rotate(self, rotate_every: int = 60) -> None:
        """Compatibility method - no-op"""
        print(f"ℹ️ [ProxyService] Auto-rotation managed by TTL ({self.PROXY_TTL}s) and retry logic")

    def stop_auto_rotate(self) -> None:
        """Compatibility method - no-op"""
        pass
    
    def check_proxyxoay_expiry(self, remove_expired: bool = True) -> Tuple[bool, str, Optional[str]]:
        """
        🔧 NEW: Kiểm tra tất cả proxyxoay key còn hạn hay không.
        
        API: GET https://proxyxoay.shop/api/get.php?key=<KEY>
        
        Status codes:
        - 100: Thành công - có proxy và thông tin hết hạn
        - 101: Thành công - rate limit (đợi X giây mới đổi được)
        - 102: Key không tồn tại hoặc hết hạn
        
        Args:
            remove_expired: Nếu True, sẽ loại bỏ key hết hạn khỏi danh sách proxy
        
        Returns:
            Tuple[has_valid, message, expired_date]:
            - has_valid: True nếu còn ít nhất 1 key hợp lệ
            - message: Thông báo chi tiết
            - expired_date: Ngày hết hạn gần nhất (nếu có key còn hạn)
        """
        with self._lock:
            if not self._proxies:
                return True, "Không có proxy", None
            
            # Lọc các proxy loại proxyxoay với index
            proxyxoay_indices = []
            for i, p in enumerate(self._proxies):
                if p.get("type") == "proxyxoay" and p.get("api_key"):
                    proxyxoay_indices.append((i, p["api_key"]))
            
            if not proxyxoay_indices:
                return True, "Không có proxyxoay key", None
        
        # Check từng key
        expired_indices = []
        valid_keys = []
        
        print(f"🔍 [ProxyService] Checking {len(proxyxoay_indices)} proxyxoay key(s)...")
        
        for idx, api_key in proxyxoay_indices:
            try:
                # Sử dụng API get.php để check (trả về proxy + thông tin hết hạn)
                url = f"{self.PROXYXOAY_API}?key={api_key}&nhamang=random&tinhthanh=random"
                resp = requests.get(url, timeout=10)
                
                if resp.status_code != 200:
                    print(f"   ⚠️ Key #{idx+1} API error: HTTP {resp.status_code}")
                    continue
                
                data = resp.json()
                status = data.get("status")
                
                if status == 100 or status == 101:
                    # Key còn hạn (100 = OK, 101 = rate limit nhưng key vẫn valid)
                    expiry_date = data.get("Token expiration date", "Unknown")
                    message = data.get("message", "")
                    
                    valid_keys.append({
                        "key": api_key[:10] + "...",
                        "full_key": api_key,
                        "expired": expiry_date,
                        "index": idx
                    })
                    
                    # Cập nhật thông tin expiry
                    with self._lock:
                        if idx < len(self._proxies):
                            self._proxies[idx]["expired"] = False
                            self._proxies[idx]["expiry_date"] = expiry_date
                    
                    if status == 100:
                        print(f"   ✅ Key #{idx+1} {api_key[:10]}... OK - hết hạn: {expiry_date}")
                    else:
                        print(f"   ✅ Key #{idx+1} {api_key[:10]}... OK (rate limit) - hết hạn: {expiry_date}")
                    
                elif status == 102:
                    # Key hết hạn hoặc không tồn tại
                    error_msg = data.get("message", "key khong ton tai hoac het han")
                    expired_indices.append({
                        "key": api_key[:10] + "...",
                        "full_key": api_key,
                        "error": error_msg,
                        "index": idx
                    })
                    
                    # Đánh dấu key hết hạn
                    with self._lock:
                        if idx < len(self._proxies):
                            self._proxies[idx]["expired"] = True
                    
                    print(f"   ❌ Key #{idx+1} {api_key[:10]}... HẾT HẠN: {error_msg}")
                else:
                    print(f"   ⚠️ Key #{idx+1} Unknown status: {status} - {data}")
                    
            except Exception as e:
                print(f"   ⚠️ Key #{idx+1} Check error: {e}")
        
        # Loại bỏ key hết hạn nếu được yêu cầu
        if remove_expired and expired_indices:
            with self._lock:
                # Đánh dấu key hết hạn bằng cách block vĩnh viễn
                for exp_info in expired_indices:
                    idx = exp_info["index"]
                    # Block key hết hạn 999999 giây (vĩnh viễn)
                    self._blocked_until[idx] = time.time() + 999999
                    print(f"🚫 [ProxyService] Blocked expired key #{idx+1} permanently")
                
                # Nếu key hiện tại bị hết hạn, chuyển sang key khác
                expired_idx_list = [e["index"] for e in expired_indices]
                if self._current_index in expired_idx_list:
                    # Tìm key còn hạn đầu tiên
                    for vk in valid_keys:
                        self._current_index = vk["index"]
                        print(f"🔄 [ProxyService] Switched to valid key #{vk['index']+1}")
                        break
        
        # Tổng kết
        total_keys = len(proxyxoay_indices)
        valid_count = len(valid_keys)
        expired_count = len(expired_indices)
        
        if expired_count > 0 and valid_count == 0:
            # Tất cả key đều hết hạn
            expired_info = ", ".join([f"{k['key']}" for k in expired_indices])
            return False, f"❌ TẤT CẢ {expired_count} proxy key đã hết hạn: {expired_info}", None
        
        if expired_count > 0 and valid_count > 0:
            # Một số key hết hạn, một số còn hạn
            expired_info = ", ".join([f"{k['key']}" for k in expired_indices])
            earliest_expiry = min([k["expired"] for k in valid_keys])
            return True, f"⚠️ {expired_count}/{total_keys} key hết hạn ({expired_info}). Còn {valid_count} key hoạt động (hết hạn sớm nhất: {earliest_expiry})", earliest_expiry
        
        if valid_keys:
            # Tất cả key còn hạn
            earliest_expiry = min([k["expired"] for k in valid_keys])
            return True, f"✅ Tất cả {valid_count} proxy key còn hạn (sớm nhất: {earliest_expiry})", earliest_expiry
        
        return True, "Không có thông tin", None
    
    def get_valid_proxy_count(self) -> int:
        """Đếm số proxy còn hoạt động (không bị block/expired)"""
        with self._lock:
            now = time.time()
            count = 0
            for i, p in enumerate(self._proxies):
                if not p.get("expired", False) and self._blocked_until.get(i, 0) < now:
                    count += 1
            return count
    
    def get_proxy_status(self) -> List[Dict]:
        """Lấy trạng thái của tất cả proxy"""
        with self._lock:
            now = time.time()
            result = []
            for i, p in enumerate(self._proxies):
                blocked_until = self._blocked_until.get(i, 0)
                is_blocked = blocked_until > now
                is_current = i == self._current_index
                
                status = {
                    "index": i,
                    "type": p.get("type"),
                    "key": p.get("api_key", "")[:10] + "..." if p.get("api_key") else p.get("proxy_url", "")[:30],
                    "expired": p.get("expired", False),
                    "expiry_date": p.get("expiry_date"),
                    "blocked": is_blocked,
                    "blocked_remaining": int(blocked_until - now) if is_blocked else 0,
                    "fail_count": self._fail_counts.get(i, 0),
                    "current": is_current
                }
                result.append(status)
            return result


# Backward compatibility alias
ProxyManager = ProxyService
