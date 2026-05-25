"""
D1 JWT Account Pool - State Machine Architecture
Quản lý pool các ElevenLabs accounts (email/password) với JWT authentication
Lưu trữ trong Cloudflare D1 database

Nguyên tắc:
- Mỗi account có state: READY, TEMP_LOCK, EXHAUSTED, DEAD
- Auto refresh JWT khi hết hạn (1 giờ)
- Rotation khi account bị rate limit hoặc hết quota
- DB là nguồn sự thật duy nhất (single source of truth)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests

from services.d1_client import D1Client

logger = logging.getLogger("d1_jwt_account_pool")

# Firebase API Key của ElevenLabs
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"


class AccountState(Enum):
    """Trạng thái của account"""
    READY = "READY"
    TEMP_LOCK = "TEMP_LOCK"
    EXHAUSTED = "EXHAUSTED"
    DEAD = "DEAD"


@dataclass
class JWTAccountInfo:
    """Account info từ database"""
    id: int
    email: str
    password: str
    state: str
    jwt_token: Optional[str] = None
    refresh_token: Optional[str] = None
    jwt_expires_at: int = 0
    failure_count: int = 0
    success_count: int = 0
    total_chars_used: int = 0
    
    def is_jwt_valid(self) -> bool:
        """JWT còn hạn không (buffer 5 phút)"""
        if not self.jwt_token:
            return False
        return time.time() < (self.jwt_expires_at - 300)


class D1JWTAccountPool:
    """
    Pool quản lý JWT accounts với D1 database backend
    
    Features:
    - Load accounts từ D1 database
    - Auto login và refresh JWT
    - Rotation khi rate limit
    - Track usage per account
    - Sync state với database
    """
    
    def __init__(self, user_id: int, log_fn: Callable = None):
        self._user_id = user_id
        self._d1 = D1Client()
        self._lock = threading.RLock()
        self._log_fn = log_fn or print
        
        # Local cache
        self._accounts: Dict[str, JWTAccountInfo] = {}
        self._in_use: Set[str] = set()
        
    def _log(self, msg: str):
        """Log message"""
        try:
            self._log_fn(msg)
        except:
            print(msg)
    
    # ------------------------------------------------------------------
    # LOAD: Load accounts từ D1
    # ------------------------------------------------------------------
    def load_accounts(self) -> int:
        """
        Load accounts từ D1 database
        
        Returns: số accounts loaded
        """
        try:
            result = self._d1.rpc("get_jwt_accounts", {"user_id": self._user_id})
            
            # D1Response has .data attribute, not .get()
            if result.error:
                self._log(f"D1 error: {result.error}")
                return 0
            
            accounts = result.data or []
            
            with self._lock:
                self._accounts.clear()
                for acc in accounts:
                    info = JWTAccountInfo(
                        id=acc["id"],
                        email=acc["email"],
                        password=acc.get("password", ""),
                        state=acc.get("state", "READY"),
                        jwt_token=acc.get("jwt_token"),
                        refresh_token=acc.get("refresh_token"),
                        jwt_expires_at=acc.get("jwt_expires_at", 0) or 0,
                        failure_count=acc.get("failure_count", 0) or 0,
                        success_count=acc.get("success_count", 0) or 0,
                        total_chars_used=acc.get("total_chars_used", 0) or 0,
                    )
                    self._accounts[acc["email"]] = info
            
            self._log(f"Loaded {len(accounts)} JWT accounts from D1")
            return len(accounts)
            
        except Exception as e:
            self._log(f"Failed to load JWT accounts: {e}")
            return 0
    
    def add_account(self, email: str, password: str) -> bool:
        """Add single account to D1"""
        try:
            result = self._d1.rpc("add_jwt_account", {
                "user_id": self._user_id,
                "email": email,
                "password": password
            })
            
            if result.error:
                self._log(f"Add account failed: {result.error}")
                return False
            
            # Reload to sync
            self.load_accounts()
            return True
            
        except Exception as e:
            self._log(f"Add account error: {e}")
            return False
    
    def add_accounts_from_file(self, file_path: str) -> Tuple[int, int]:
        """
        Import accounts từ file TXT
        Format: email|password (mỗi dòng 1 account)
        
        Returns: (added, skipped)
        """
        import os
        if not os.path.exists(file_path):
            self._log(f"File not found: {file_path}")
            return (0, 0)
        
        accounts = []
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('|')
                if len(parts) >= 2:
                    accounts.append({
                        "email": parts[0].strip(),
                        "password": parts[1].strip()
                    })
        
        if not accounts:
            return (0, 0)
        
        try:
            result = self._d1.rpc("batch_add_jwt_accounts", {
                "user_id": self._user_id,
                "accounts": accounts
            })
            
            if result.error:
                self._log(f"Import error: {result.error}")
                return (0, 0)
            
            data = result.data or {}
            added = data.get("added", 0) if isinstance(data, dict) else 0
            skipped = data.get("skipped", 0) if isinstance(data, dict) else 0
            
            self._log(f"Imported {added} accounts, skipped {skipped} duplicates")
            self.load_accounts()
            return (added, skipped)
            
        except Exception as e:
            self._log(f"Import accounts error: {e}")
            return (0, 0)
    
    # ------------------------------------------------------------------
    # AUTH: Login và refresh JWT
    # ------------------------------------------------------------------
    def _login(self, acc: JWTAccountInfo, max_retries: int = 3) -> bool:
        """Login để lấy JWT token
        
        🔧 FIX: Thêm retry với delay khi gặp QUOTA_EXCEEDED từ Firebase
        """
        self._log(f"🔐 Logging in: {acc.email}...")
        
        url = f"{FIREBASE_AUTH_URL}:signInWithPassword?key={FIREBASE_API_KEY}"
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://elevenlabs.io/",
            "Origin": "https://elevenlabs.io",
        }
        payload = {
            "email": acc.email,
            "password": acc.password,
            "returnSecureToken": True
        }
        
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                
                if resp.status_code == 200:
                    data = resp.json()
                    jwt_token = data.get('idToken')
                    refresh_token = data.get('refreshToken')
                    expires_in = int(data.get('expiresIn', 3600))
                    jwt_expires_at = int(time.time()) + expires_in
                    
                    # Update local cache
                    acc.jwt_token = jwt_token
                    acc.refresh_token = refresh_token
                    acc.jwt_expires_at = jwt_expires_at
                    acc.state = "READY"
                    
                    # Sync to D1
                    self._d1.rpc("update_jwt_token", {
                        "email": acc.email,
                        "jwt_token": jwt_token,
                        "refresh_token": refresh_token,
                        "jwt_expires_at": jwt_expires_at
                    })
                    
                    self._log(f"✅ Login OK: {acc.email}")
                    
                    # 🔧 SKIP auto-check credits - will detect on TTS error instead
                    # This speeds up login significantly
                    
                    return True
                else:
                    error = resp.json().get('error', {})
                    msg = error.get('message', 'Unknown error')
                    
                    # 🔧 FIX: Nếu QUOTA_EXCEEDED, đợi và retry
                    if 'QUOTA_EXCEEDED' in msg:
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 3  # 3s, 6s, 9s
                            self._log(f"⏳ Firebase quota exceeded, đợi {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                            time.sleep(wait_time)
                            continue
                        else:
                            # Hết retry - TEMP_LOCK account để thử lại sau
                            self._report_error(acc.email, "rate_limit", f"Firebase QUOTA_EXCEEDED after {max_retries} retries")
                            self._log(f"⚠️ Firebase quota exceeded: {acc.email} - TEMP_LOCK")
                            return False
                    
                    # Check nếu account bị khóa/invalid
                    if 'INVALID' in msg or 'DISABLED' in msg or 'NOT_FOUND' in msg:
                        self._report_error(acc.email, "invalid", msg)
                        self._log(f"☠️ Account DEAD: {acc.email} - {msg}")
                    else:
                        self._report_error(acc.email, "auth_error", msg)
                        self._log(f"❌ Login failed: {acc.email} - {msg}")
                    
                    return False
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                self._report_error(acc.email, "network", str(e))
                self._log(f"❌ Login error: {acc.email} - {e}")
                return False
        
        return False
    
    def _auto_check_credits(self, acc: JWTAccountInfo):
        """
        🔧 Auto check credits sau khi login thành công
        Chỉ log thông tin, KHÔNG mark EXHAUSTED (chờ gen fail mới mark)
        """
        try:
            url = "https://api.elevenlabs.io/v1/user"
            headers = {
                "Authorization": f"Bearer {acc.jwt_token}",
                "Accept": "application/json",
            }
            
            resp = requests.get(url, headers=headers, timeout=15)
            
            if resp.status_code == 200:
                data = resp.json()
                sub = data.get("subscription", {})
                used = int(sub.get("character_count", 0) or 0)
                limit = int(sub.get("character_limit", 0) or 0)
                remaining = max(0, limit - used)
                
                # Update to D1 (chỉ để tracking, không dùng để pick)
                self._d1.rpc("update_jwt_credits", {
                    "email": acc.email,
                    "credit_remaining": remaining,
                    "credit_limit": limit
                })
                
                self._log(f"💰 {acc.email}: {remaining}/{limit} credits")
                
                # 🔧 KHÔNG auto mark EXHAUSTED nữa - chờ gen fail mới mark
                # Vì có thể credits đã reset nhưng chưa sync
                    
        except Exception as e:
            # Không fail login nếu check credits lỗi
            self._log(f"⚠️ Auto-check credits failed: {e}")
    def _refresh_jwt(self, acc: JWTAccountInfo) -> bool:
        """Refresh JWT token"""
        if not acc.refresh_token:
            return self._login(acc)
        
        self._log(f"Refreshing JWT: {acc.email}...")
        
        url = f"{FIREBASE_TOKEN_URL}?key={FIREBASE_API_KEY}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://elevenlabs.io/",
            "Origin": "https://elevenlabs.io",
        }
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": acc.refresh_token
        }
        
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                jwt_token = data.get('id_token')
                refresh_token = data.get('refresh_token')
                expires_in = int(data.get('expires_in', 3600))
                jwt_expires_at = int(time.time()) + expires_in
                
                # Update local cache
                acc.jwt_token = jwt_token
                acc.refresh_token = refresh_token
                acc.jwt_expires_at = jwt_expires_at
                acc.state = "READY"
                
                # Sync to D1
                self._d1.rpc("update_jwt_token", {
                    "email": acc.email,
                    "jwt_token": jwt_token,
                    "refresh_token": refresh_token,
                    "jwt_expires_at": jwt_expires_at
                })
                
                self._log(f"Refresh OK: {acc.email}")
                return True
            else:
                # Refresh fail -> try login
                self._log(f"Refresh failed, trying login...")
                return self._login(acc)
                
        except Exception as e:
            self._log(f"Refresh error: {e}, trying login...")
            return self._login(acc)
    
    def _ensure_jwt(self, acc: JWTAccountInfo) -> bool:
        """Đảm bảo account có JWT valid"""
        if acc.is_jwt_valid():
            return True
        
        # Thử refresh trước (nhanh hơn)
        if acc.refresh_token:
            if self._refresh_jwt(acc):
                return True
        
        # Login nếu refresh fail
        return self._login(acc)
    
    # ------------------------------------------------------------------
    # PICK: Lấy account để sử dụng
    # ------------------------------------------------------------------
    def get_account(self, excluded: Set[str] = None, allow_concurrent: bool = True) -> Optional[JWTAccountInfo]:
        """
        Lấy 1 account READY có JWT valid
        
        Args:
            excluded: Set emails để exclude
            allow_concurrent: Nếu False, exclude accounts đang in_use
        
        Returns: JWTAccountInfo hoặc None
        """
        excluded = excluded or set()
        email = None
        acc = None
        need_retry = False
        
        # 🔧 FIX: Lock toàn bộ quá trình pick để tránh race condition
        with self._lock:
            # Nếu không cho phép concurrent, exclude accounts đang in_use
            if not allow_concurrent:
                excluded_list = list(excluded | self._in_use)
            else:
                excluded_list = list(excluded)
            
            try:
                # Pick từ D1 (đã có round-robin logic)
                result = self._d1.rpc("pick_jwt_account", {
                    "user_id": self._user_id,
                    "excluded_emails": excluded_list
                })
                
                if result.error:
                    self._log(f"Pick account error: {result.error}")
                    return None
                
                data = result.data
                if not data:
                    self._log(f"No usable account (excluded={len(excluded_list)})")
                    return None
                
                email = data["email"]
                
                # Double-check: nếu account đã bị pick bởi thread khác trong lúc chờ D1
                if not allow_concurrent and email in self._in_use:
                    self._log(f"⚠️ Account {email} already in use, retrying...")
                    need_retry = True
                else:
                    # Add to in_use IMMEDIATELY (trong lock)
                    self._in_use.add(email)
                    
                    # Get from local cache or create new
                    if email not in self._accounts:
                        self._accounts[email] = JWTAccountInfo(
                            id=data["id"],
                            email=email,
                            password=data.get("password", ""),
                            state=data.get("state", "READY"),
                            jwt_token=data.get("jwt_token"),
                            refresh_token=data.get("refresh_token"),
                            jwt_expires_at=data.get("jwt_expires_at", 0) or 0,
                        )
                    else:
                        # Update from DB
                        existing = self._accounts[email]
                        existing.jwt_token = data.get("jwt_token")
                        existing.refresh_token = data.get("refresh_token")
                        existing.jwt_expires_at = data.get("jwt_expires_at", 0) or 0
                        existing.password = data.get("password", existing.password)
                    
                    acc = self._accounts[email]
                    
            except Exception as e:
                self._log(f"Get account error: {e}")
                return None
        
        # Nếu account đã in_use, retry với excluded (ngoài lock)
        if need_retry and email:
            return self.get_account(excluded | {email}, allow_concurrent)
        
        if not acc:
            return None
        
        # Ensure JWT valid (ngoài lock vì có thể mất thời gian)
        if self._ensure_jwt(acc):
            self._log(f"PICK account: {email}")
            return acc
        else:
            # Login failed, remove from in_use and try next
            with self._lock:
                self._in_use.discard(email)
            return self.get_account(excluded | {email}, allow_concurrent)
    
    # ------------------------------------------------------------------
    # RELEASE: Trả account sau khi dùng
    # ------------------------------------------------------------------
    def release_account(self, email: str, success: bool, chars_used: int = 0,
                       error_type: str = None, error_msg: str = None):
        """
        Release account sau khi sử dụng
        """
        with self._lock:
            self._in_use.discard(email)
        
        if success:
            self._report_success(email, chars_used)
        else:
            self._report_error(email, error_type, error_msg)
    
    def _report_success(self, email: str, chars_used: int):
        """Report success to D1"""
        try:
            self._d1.rpc("report_jwt_success", {
                "email": email,
                "chars_used": chars_used
            })
            
            # Update local cache
            with self._lock:
                if email in self._accounts:
                    acc = self._accounts[email]
                    acc.success_count += 1
                    acc.failure_count = 0
                    acc.total_chars_used += chars_used
                    acc.state = "READY"
            
            self._log(f"Account success: {email} (+{chars_used} chars)")
            
        except Exception as e:
            self._log(f"Report success error: {e}")
    
    def _report_error(self, email: str, error_type: str, error_msg: str = None, lock_seconds: int = 60):
        """Report error to D1"""
        try:
            self._d1.rpc("report_jwt_error", {
                "email": email,
                "error_type": error_type,
                "error_message": error_msg or error_type,
                "lock_seconds": lock_seconds
            })
            
            # Update local cache
            with self._lock:
                if email in self._accounts:
                    acc = self._accounts[email]
                    acc.failure_count += 1
                    
                    if error_type in ('rate_limit', '429'):
                        acc.state = "TEMP_LOCK"
                    elif error_type in ('quota_exceeded', '402', 'insufficient_credits'):
                        acc.state = "EXHAUSTED"
                        self._log(f"⚠️ {email} EXHAUSTED - will auto-reset on next billing cycle")
                    elif error_type in ('disabled', 'banned', 'invalid'):
                        acc.state = "DEAD"
            
            self._log(f"Account error: {email} - {error_type}")
            
        except Exception as e:
            self._log(f"Report error failed: {e}")
    
    # ------------------------------------------------------------------
    # STATS: Thống kê
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict:
        """Lấy thống kê accounts từ D1"""
        try:
            result = self._d1.rpc("get_jwt_accounts_stats", {"user_id": self._user_id})
            
            if result.error:
                self._log(f"Get stats error: {result.error}")
                return {
                    'total': 0, 'ready': 0, 'temp_lock': 0,
                    'exhausted': 0, 'dead': 0, 'in_use': 0, 'total_chars': 0
                }
            
            data = result.data or {}
            
            return {
                'total': data.get('total', 0) or 0,
                'ready': data.get('ready', 0) or 0,
                'temp_lock': data.get('temp_lock', 0) or 0,
                'exhausted': data.get('exhausted', 0) or 0,
                'dead': data.get('dead', 0) or 0,
                'in_use': len(self._in_use),
                'total_chars': data.get('total_chars', 0) or 0,
            }
        except Exception as e:
            self._log(f"Get stats error: {e}")
            return {
                'total': 0, 'ready': 0, 'temp_lock': 0,
                'exhausted': 0, 'dead': 0, 'in_use': 0, 'total_chars': 0
            }
    
    def load_from_file(self, file_path: str) -> int:
        """Load accounts từ file TXT và thêm vào D1
        
        Format file: email:password hoặc email|password mỗi dòng
        
        Returns:
            Số accounts đã thêm thành công
        """
        if not os.path.exists(file_path):
            self._log(f"❌ File không tồn tại: {file_path}")
            return 0
        
        accounts = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    
                    # Parse email:password hoặc email|password
                    if ':' in line:
                        parts = line.split(':', 1)
                    elif '|' in line:
                        parts = line.split('|', 1)
                    else:
                        continue
                    
                    if len(parts) == 2:
                        email, password = parts[0].strip(), parts[1].strip()
                        if email and password:
                            accounts.append({'email': email, 'password': password})
            
            if not accounts:
                self._log("⚠️ Không tìm thấy accounts trong file")
                return 0
            
            self._log(f"📂 Đọc được {len(accounts)} accounts từ file")
            
            # Batch add vào D1
            result = self._d1.rpc("batch_add_jwt_accounts", {
                "user_id": self._user_id,
                "accounts": accounts
            })
            
            if result.error:
                self._log(f"❌ Batch add error: {result.error}")
                return 0
            
            data = result.data or {}
            added = data.get('added', 0)
            skipped = data.get('skipped', 0)
            
            self._log(f"✅ Đã thêm {added} accounts vào D1 (bỏ qua {skipped} trùng)")
            return added
            
        except Exception as e:
            self._log(f"❌ Load from file error: {e}")
            return 0
    
    def get_account_list(self) -> List[Dict]:
        """Lấy danh sách accounts với trạng thái"""
        try:
            result = self._d1.rpc("get_jwt_accounts", {"user_id": self._user_id})
            
            if result.error:
                self._log(f"Get account list error: {result.error}")
                return []
            
            accounts = result.data or []
            
            return [{
                'id': acc['id'],
                'email': acc['email'],
                'state': acc.get('state', 'READY'),
                'success_count': acc.get('success_count', 0) or 0,
                'failure_count': acc.get('failure_count', 0) or 0,
                'total_chars': acc.get('total_chars_used', 0) or 0,
                'has_jwt': bool(acc.get('jwt_token')),
                'jwt_valid': (acc.get('jwt_expires_at', 0) or 0) > time.time(),
                'in_use': acc['email'] in self._in_use,
                'last_used': acc.get('last_used'),
            } for acc in accounts]
            
        except Exception as e:
            self._log(f"Get account list error: {e}")
            return []
    
    def reset_accounts(self, states: List[str] = None) -> int:
        """Reset accounts về READY"""
        states = states or ['TEMP_LOCK', 'EXHAUSTED']
        try:
            result = self._d1.rpc("batch_reset_jwt_accounts", {
                "user_id": self._user_id,
                "states": states
            })
            
            if result.error:
                self._log(f"Reset accounts error: {result.error}")
                return 0
            
            data = result.data or {}
            reset_count = data.get("reset", 0) if isinstance(data, dict) else 0
            self._log(f"Reset {reset_count} accounts")
            self.load_accounts()
            return reset_count
        except Exception as e:
            self._log(f"Reset accounts error: {e}")
            return 0

    # ------------------------------------------------------------------
    # CREDIT CHECK: Check và update credits từ ElevenLabs API
    # ------------------------------------------------------------------
    def check_account_credits(self, email: str, proxies: Dict = None) -> Tuple[int, int]:
        """
        Check credits của account từ ElevenLabs API
        
        Args:
            email: Email của account
            proxies: Proxy dict cho requests
        
        Returns: (remaining, limit)
        """
        with self._lock:
            acc = self._accounts.get(email)
            if not acc:
                return (0, 0)
        
        # Ensure JWT valid
        if not self._ensure_jwt(acc):
            return (0, 0)
        
        try:
            # Call ElevenLabs API
            url = "https://api.elevenlabs.io/v1/user"
            headers = {
                "Authorization": f"Bearer {acc.jwt_token}",
                "Accept": "application/json",
            }
            
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                sub = data.get("subscription", {})
                used = int(sub.get("character_count", 0) or 0)
                limit = int(sub.get("character_limit", 0) or 0)
                remaining = max(0, limit - used)
                
                # Update to D1
                self._update_credits(email, remaining, limit)
                
                self._log(f"💰 {email}: {remaining}/{limit} credits remaining")
                return (remaining, limit)
            else:
                self._log(f"❌ Check credits failed: {resp.status_code}")
                return (0, 0)
                
        except Exception as e:
            self._log(f"❌ Check credits error: {e}")
            return (0, 0)
    
    def _update_credits(self, email: str, credit_remaining: int, credit_limit: int):
        """Update credits to D1"""
        try:
            self._d1.rpc("update_jwt_credits", {
                "email": email,
                "credit_remaining": credit_remaining,
                "credit_limit": credit_limit
            })
        except Exception as e:
            self._log(f"Update credits error: {e}")
    
    def check_all_accounts_credits(self, proxies: Dict = None) -> List[Dict]:
        """
        Check credits của tất cả accounts
        
        Returns: List of {email, remaining, limit, state}
        """
        results = []
        
        with self._lock:
            emails = list(self._accounts.keys())
        
        for email in emails:
            remaining, limit = self.check_account_credits(email, proxies)
            
            # Determine state based on credits
            state = "READY"
            if remaining <= 0:
                state = "EXHAUSTED"
            elif remaining < 100:
                state = "LOW_CREDIT"
            
            results.append({
                "email": email,
                "remaining": remaining,
                "limit": limit,
                "state": state
            })
            
            # Small delay to avoid rate limit
            time.sleep(0.5)
        
        return results
    
    def sync_credits_from_api(self, proxies: Dict = None) -> int:
        """
        Sync credits từ ElevenLabs API cho tất cả accounts
        Tự động mark EXHAUSTED nếu hết credits
        
        Returns: Số accounts đã sync
        """
        results = self.check_all_accounts_credits(proxies)
        
        exhausted_count = 0
        for r in results:
            if r["state"] == "EXHAUSTED":
                # Mark as EXHAUSTED in D1
                self._report_error(r["email"], "quota_exceeded", "No credits remaining")
                exhausted_count += 1
        
        self._log(f"✅ Synced credits for {len(results)} accounts ({exhausted_count} exhausted)")
        
        # Reload accounts to get updated state
        self.load_accounts()
        
        return len(results)

    # ------------------------------------------------------------------
    # VOICE SLOTS TRACKING: Free Tier limits (3 slots, 54 edits/month)
    # ------------------------------------------------------------------
    def get_voice_slots_info(self, email: str) -> Dict:
        """
        Get voice slots info for account
        
        Returns: {
            email, voice_slots_used, voice_edits_count,
            can_add_voice, can_edit_voice,
            slots_remaining, edits_remaining
        }
        """
        try:
            result = self._d1.rpc("get_voice_slots_info", {"email": email})
            
            if result.error:
                self._log(f"Get voice slots info error: {result.error}")
                return {
                    "email": email,
                    "voice_slots_used": 0,
                    "voice_edits_count": 0,
                    "can_add_voice": True,
                    "can_edit_voice": True,
                    "slots_remaining": 3,
                    "edits_remaining": 54
                }
            
            return result.data or {}
            
        except Exception as e:
            self._log(f"Get voice slots info error: {e}")
            return {}
    
    def can_add_voice(self, email: str) -> Tuple[bool, str]:
        """
        Check if account can add a new voice
        
        Returns: (can_add, reason)
        """
        try:
            result = self._d1.rpc("can_edit_voice", {
                "email": email,
                "operation": "add"
            })
            
            if result.error:
                return (False, result.error)
            
            data = result.data or {}
            can_proceed = data.get("can_proceed", False)
            reason = data.get("reason")
            
            if not can_proceed:
                if reason == "slots_full":
                    return (False, "Voice slots full (max 3)")
                elif reason == "edits_exhausted":
                    return (False, "Voice edits exhausted (max 54/month)")
                return (False, reason or "Unknown")
            
            return (True, None)
            
        except Exception as e:
            self._log(f"Can add voice check error: {e}")
            return (False, str(e))
    
    def can_delete_voice(self, email: str) -> Tuple[bool, str]:
        """
        Check if account can delete a voice
        
        Returns: (can_delete, reason)
        """
        try:
            result = self._d1.rpc("can_edit_voice", {
                "email": email,
                "operation": "delete"
            })
            
            if result.error:
                return (False, result.error)
            
            data = result.data or {}
            can_proceed = data.get("can_proceed", False)
            reason = data.get("reason")
            
            if not can_proceed:
                if reason == "edits_exhausted":
                    return (False, "Voice edits exhausted (max 54/month)")
                return (False, reason or "Unknown")
            
            return (True, None)
            
        except Exception as e:
            self._log(f"Can delete voice check error: {e}")
            return (False, str(e))
    
    def record_voice_add(self, email: str) -> bool:
        """
        Record that a voice was added to account
        Increments slots_used and edits_count
        
        Returns: True if successful
        """
        try:
            result = self._d1.rpc("record_voice_add", {"email": email})
            
            if result.error:
                self._log(f"❌ Record voice add failed: {result.error}")
                return False
            
            data = result.data or {}
            slots_remaining = result.get("slots_remaining", "?")
            edits_remaining = result.get("edits_remaining", "?")
            self._log(f"✅ Voice added: {email} (slots: {slots_remaining}/3, edits: {edits_remaining}/54)")
            return True
            
        except Exception as e:
            self._log(f"Record voice add error: {e}")
            return False
    
    def record_voice_delete(self, email: str) -> bool:
        """
        Record that a voice was deleted from account
        Decrements slots_used, increments edits_count
        
        Returns: True if successful
        """
        try:
            result = self._d1.rpc("record_voice_delete", {"email": email})
            
            if result.error:
                self._log(f"❌ Record voice delete failed: {result.error}")
                return False
            
            data = result.data or {}
            slots_remaining = data.get("slots_remaining", "?")
            edits_remaining = data.get("edits_remaining", "?")
            self._log(f"✅ Voice deleted: {email} (slots: {slots_remaining}/3, edits: {edits_remaining}/54)")
            return True
            
        except Exception as e:
            self._log(f"Record voice delete error: {e}")
            return False
    
    def sync_voice_slots(self, email: str, actual_slots_used: int) -> bool:
        """
        Sync voice slots count from ElevenLabs API
        Call this after listing voices to get accurate count
        
        Returns: True if successful
        """
        try:
            result = self._d1.rpc("sync_voice_slots", {
                "email": email,
                "actual_slots_used": actual_slots_used
            })
            
            if result.error:
                self._log(f"Sync voice slots error: {result.error}")
                return False
            
            self._log(f"✅ Synced voice slots: {email} = {actual_slots_used}/3")
            return True
            
        except Exception as e:
            self._log(f"Sync voice slots error: {e}")
            return False


    # ------------------------------------------------------------------
    # VOICE MANAGEMENT: Add/Delete voices with limit tracking
    # ------------------------------------------------------------------
    def add_voice_to_account(self, email: str, voice_id: str, voice_name: str = None, 
                             proxies: Dict = None) -> Tuple[bool, str]:
        """
        Add a voice to account's library (from shared voices)
        Checks limits before adding
        
        Returns: (success, error_message)
        """
        # Check if can add
        can_add, reason = self.can_add_voice(email)
        if not can_add:
            return (False, reason)
        
        # Get account JWT
        with self._lock:
            acc = self._accounts.get(email)
            if not acc:
                return (False, "Account not found")
        
        if not self._ensure_jwt(acc):
            return (False, "JWT authentication failed")
        
        # Add voice via API
        try:
            from services.jwt_account_pool import JWTAPIClient
            
            url = f"{JWTAPIClient.BASE_URL}/v1/voices/add/{voice_id}"
            headers = JWTAPIClient._get_headers(acc.jwt_token)
            
            payload = {}
            if voice_name:
                payload["name"] = voice_name
            
            resp = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=30)
            
            if resp.status_code in (200, 201):
                # Record the add
                self.record_voice_add(email)
                self._log(f"✅ Added voice {voice_id} to {email}")
                return (True, None)
            else:
                error = resp.text[:200]
                self._log(f"❌ Add voice failed: {resp.status_code} - {error}")
                return (False, f"API error: {resp.status_code}")
                
        except Exception as e:
            self._log(f"❌ Add voice error: {e}")
            return (False, str(e))
    
    def delete_voice_from_account(self, email: str, voice_id: str, 
                                   proxies: Dict = None) -> Tuple[bool, str]:
        """
        Delete a voice from account
        Checks edit limit before deleting
        
        Returns: (success, error_message)
        """
        # Check if can delete
        can_delete, reason = self.can_delete_voice(email)
        if not can_delete:
            return (False, reason)
        
        # Get account JWT
        with self._lock:
            acc = self._accounts.get(email)
            if not acc:
                return (False, "Account not found")
        
        if not self._ensure_jwt(acc):
            return (False, "JWT authentication failed")
        
        # Delete voice via API
        try:
            from services.jwt_account_pool import JWTAPIClient
            
            success = JWTAPIClient.delete_voice(acc.jwt_token, voice_id, proxies)
            
            if success:
                # Record the delete
                self.record_voice_delete(email)
                self._log(f"✅ Deleted voice {voice_id} from {email}")
                return (True, None)
            else:
                return (False, "Delete API failed")
                
        except Exception as e:
            self._log(f"❌ Delete voice error: {e}")
            return (False, str(e))
    
    def cleanup_account_voices(self, email: str, proxies: Dict = None) -> int:
        """
        Cleanup ALL library voices của account (không phải premade).
        Wrapper cho cleanup_custom_voices() để tương thích với JWTAccountPool interface.
        
        Returns: Số voices đã xóa (-1 nếu account hết edits)
        """
        deleted, error = self.cleanup_custom_voices(email, proxies)
        
        # Return -1 để signal rằng account này đã hết edits
        if error and "exhausted" in error.lower():
            return -1
        
        return deleted
    
    def cleanup_custom_voices(self, email: str, proxies: Dict = None) -> Tuple[int, str]:
        """
        Cleanup all custom voices from account
        Respects monthly edit limit (54 edits/month)
        
        Returns: (deleted_count, error_message)
        """
        # Get account JWT
        with self._lock:
            acc = self._accounts.get(email)
            if not acc:
                return (0, "Account not found")
        
        if not self._ensure_jwt(acc):
            return (0, "JWT authentication failed")
        
        # Get voice slots info to check remaining edits
        slots_info = self.get_voice_slots_info(email)
        edits_remaining = slots_info.get("edits_remaining", 0)
        
        if edits_remaining <= 0:
            return (0, "Voice edits exhausted (max 54/month)")
        
        # List voices
        try:
            from services.jwt_account_pool import JWTAPIClient
            
            voices = JWTAPIClient.list_voices(acc.jwt_token, proxies)
            custom_voices = [v for v in voices if v.get("category", "").lower() not in ("premade", "default")]
            
            if not custom_voices:
                self._log(f"No custom voices to cleanup for {email}")
                return (0, None)
            
            # Limit deletions to remaining edits
            voices_to_delete = custom_voices[:edits_remaining]
            
            self._log(f"🧹 Cleaning up {len(voices_to_delete)} voices for {email} (max {edits_remaining} edits remaining)")
            
            deleted = 0
            for voice in voices_to_delete:
                voice_id = voice.get("voice_id")
                if not voice_id:
                    continue
                
                # Check again before each delete
                can_delete, reason = self.can_delete_voice(email)
                if not can_delete:
                    self._log(f"⚠️ Stopped cleanup: {reason}")
                    break
                
                if JWTAPIClient.delete_voice(acc.jwt_token, voice_id, proxies):
                    self.record_voice_delete(email)
                    deleted += 1
                    time.sleep(0.5)  # Rate limit
            
            self._log(f"✅ Deleted {deleted} custom voices from {email}")
            
            # Sync actual slots count
            remaining_voices = JWTAPIClient.list_voices(acc.jwt_token, proxies)
            custom_count = len([v for v in remaining_voices if v.get("category", "").lower() not in ("premade", "default")])
            self.sync_voice_slots(email, custom_count)
            
            return (deleted, None)
            
        except Exception as e:
            self._log(f"❌ Cleanup voices error: {e}")
            return (0, str(e))
    
    def sync_all_voice_slots(self, proxies: Dict = None) -> int:
        """
        Sync voice slots count for all accounts from ElevenLabs API
        
        Returns: Number of accounts synced
        """
        synced = 0
        
        with self._lock:
            emails = list(self._accounts.keys())
        
        for email in emails:
            try:
                with self._lock:
                    acc = self._accounts.get(email)
                    if not acc:
                        continue
                
                if not self._ensure_jwt(acc):
                    continue
                
                from services.jwt_account_pool import JWTAPIClient
                
                voices = JWTAPIClient.list_voices(acc.jwt_token, proxies)
                custom_count = len([v for v in voices if v.get("category", "").lower() not in ("premade", "default")])
                
                self.sync_voice_slots(email, custom_count)
                synced += 1
                
                time.sleep(0.5)  # Rate limit
                
            except Exception as e:
                self._log(f"⚠️ Sync voice slots failed for {email}: {e}")
        
        self._log(f"✅ Synced voice slots for {synced} accounts")
        return synced
