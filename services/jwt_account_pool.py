"""
JWT Account Pool - State Machine Architecture
Quản lý pool các ElevenLabs accounts (email/password) với JWT authentication

Nguyên tắc:
- Mỗi account có state: READY, TEMP_LOCK, EXHAUSTED, DEAD
- Auto refresh JWT khi hết hạn (1 giờ)
- Rotation khi account bị rate limit hoặc hết quota
- Không modify logic cũ - hoàn toàn độc lập
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Callable

import requests

logger = logging.getLogger("jwt_account_pool")


# Firebase API Key của ElevenLabs
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"


class AccountState(Enum):
    """Trạng thái của account"""
    READY = "READY"           # Sẵn sàng sử dụng
    TEMP_LOCK = "TEMP_LOCK"   # Bị rate limit tạm thời
    EXHAUSTED = "EXHAUSTED"   # Hết quota/credits
    DEAD = "DEAD"             # Login fail / account bị khóa


# Thời gian lock cho các loại lỗi
LOCK_DURATION = {
    "rate_limit": 60,      # 60 giây
    "network_error": 30,   # 30 giây
    "timeout": 30,         # 30 giây
    "unknown": 30,         # 30 giây
}


@dataclass
class JWTAccount:
    """Đại diện cho 1 account trong pool"""
    email: str
    password: str
    jwt_token: Optional[str] = None
    refresh_token: Optional[str] = None
    jwt_expires_at: int = 0
    state: AccountState = AccountState.READY
    locked_until: Optional[float] = None
    last_error: Optional[str] = None
    failure_count: int = 0
    success_count: int = 0
    last_used: Optional[float] = None
    total_chars_used: int = 0
    
    def is_jwt_valid(self) -> bool:
        """JWT còn hạn không (buffer 5 phút)"""
        if not self.jwt_token:
            return False
        return time.time() < (self.jwt_expires_at - 300)
    
    def is_usable(self) -> bool:
        """Account có thể dùng được không"""
        if self.state not in (AccountState.READY, AccountState.TEMP_LOCK):
            return False
        if self.state == AccountState.TEMP_LOCK:
            if self.locked_until and time.time() < self.locked_until:
                return False
        return True


class JWTAPIClient:
    """
    API Client cho ElevenLabs sử dụng JWT Bearer token
    Hỗ trợ các endpoints: user, voices, delete voice, TTS
    """
    
    BASE_URL = "https://api.us.elevenlabs.io"
    
    @staticmethod
    def _get_headers(jwt_token: str) -> Dict:
        """Get headers cho JWT request"""
        return {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://elevenlabs.io",
            "Referer": "https://elevenlabs.io/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        }
    
    @staticmethod
    def get_user_info(jwt_token: str, proxies: Dict = None) -> Optional[Dict]:
        """
        Lấy thông tin user và credits
        
        Returns: {subscription: {character_count, character_limit, ...}, ...}
        """
        url = f"{JWTAPIClient.BASE_URL}/v1/user"
        headers = JWTAPIClient._get_headers(jwt_token)
        
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ Get user info failed: {resp.status_code} - {resp.text[:200]}")
                return None
        except Exception as e:
            print(f"❌ Get user info error: {e}")
            return None
    
    @staticmethod
    def get_credits(jwt_token: str, proxies: Dict = None) -> Tuple[int, int]:
        """
        Lấy credits còn lại
        
        Returns: (remaining, limit)
        """
        user_info = JWTAPIClient.get_user_info(jwt_token, proxies)
        if not user_info:
            return (0, 0)
        
        sub = user_info.get("subscription", {})
        used = int(sub.get("character_count", 0) or 0)
        limit = int(sub.get("character_limit", 0) or 0)
        remaining = max(0, limit - used)
        
        return (remaining, limit)
    
    @staticmethod
    def list_voices(jwt_token: str, proxies: Dict = None) -> List[Dict]:
        """
        Lấy danh sách voices của account (v1)
        
        Returns: List of voice objects
        """
        url = f"{JWTAPIClient.BASE_URL}/v1/voices"
        headers = JWTAPIClient._get_headers(jwt_token)
        
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("voices", [])
            else:
                print(f"❌ List voices failed: {resp.status_code}")
                return []
        except Exception as e:
            print(f"❌ List voices error: {e}")
            return []
    
    @staticmethod
    def get_voice(jwt_token: str, voice_id: str, proxies: Dict = None) -> Optional[Dict]:
        """
        Lấy thông tin 1 voice cụ thể
        
        Returns: Voice object hoặc None
        """
        url = f"{JWTAPIClient.BASE_URL}/v1/voices/{voice_id}"
        headers = JWTAPIClient._get_headers(jwt_token)
        
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ Get voice failed: {resp.status_code}")
                return None
        except Exception as e:
            print(f"❌ Get voice error: {e}")
            return None
    
    @staticmethod
    def list_models(jwt_token: str, proxies: Dict = None) -> List[Dict]:
        """
        Lấy danh sách models
        
        Returns: List of model objects
        """
        url = f"{JWTAPIClient.BASE_URL}/v1/models"
        headers = JWTAPIClient._get_headers(jwt_token)
        
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ List models failed: {resp.status_code}")
                return []
        except Exception as e:
            print(f"❌ List models error: {e}")
            return []
    
    @staticmethod
    def search_voices(
        jwt_token: str, 
        search: str = "", 
        page_size: int = 100,
        sort: str = "name",
        sort_direction: str = "asc",
        next_page_token: str = None,
        proxies: Dict = None
    ) -> Dict:
        """
        Search voices trong library (v2 API)
        
        Args:
            search: Search query
            page_size: Số voices mỗi page (max 100)
            sort: Sort by (name, created_at, etc.)
            sort_direction: asc hoặc desc
            next_page_token: Token để lấy page tiếp theo
        
        Returns: {voices: [...], has_more: bool, next_page_token: str}
        """
        url = f"{JWTAPIClient.BASE_URL}/v2/voices"
        headers = JWTAPIClient._get_headers(jwt_token)
        
        params = {
            "page_size": page_size,
            "sort": sort,
            "sort_direction": sort_direction,
        }
        if search:
            params["search"] = search
        if next_page_token:
            params["next_page_token"] = next_page_token
        
        try:
            resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ Search voices failed: {resp.status_code}")
                return {"voices": [], "has_more": False}
        except Exception as e:
            print(f"❌ Search voices error: {e}")
            return {"voices": [], "has_more": False}
    
    @staticmethod
    def search_all_voices(jwt_token: str, search: str = "", proxies: Dict = None) -> List[Dict]:
        """
        Search tất cả voices (auto pagination)
        
        Returns: List of all matching voices
        """
        all_voices = []
        next_token = None
        max_pages = 10
        
        for _ in range(max_pages):
            result = JWTAPIClient.search_voices(
                jwt_token, 
                search=search, 
                next_page_token=next_token,
                proxies=proxies
            )
            
            voices = result.get("voices", [])
            all_voices.extend(voices)
            
            if not result.get("has_more"):
                break
            
            next_token = result.get("next_page_token")
            if not next_token:
                break
        
        return all_voices
    
    @staticmethod
    def search_shared_voices(
        jwt_token: str,
        search: str = "",
        page_size: int = 100,
        page: int = 0,
        proxies: Dict = None
    ) -> List[Dict]:
        """
        Search shared voices từ community (v1 API)
        
        Args:
            search: Search query
            page_size: Số voices mỗi page
            page: Page number (0-indexed)
        
        Returns: List of shared voice objects
        """
        url = f"{JWTAPIClient.BASE_URL}/v1/shared-voices"
        headers = JWTAPIClient._get_headers(jwt_token)
        
        params = {
            "page_size": page_size,
            "page": page,
        }
        if search:
            params["search"] = search
        
        try:
            resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("voices", [])
            else:
                print(f"❌ Search shared voices failed: {resp.status_code}")
                return []
        except Exception as e:
            print(f"❌ Search shared voices error: {e}")
            return []
    
    @staticmethod
    def search_all_shared_voices(jwt_token: str, search: str = "", proxies: Dict = None) -> List[Dict]:
        """
        Search tất cả shared voices (auto pagination)
        
        Returns: List of all matching shared voices
        """
        all_voices = []
        page = 0
        max_pages = 5
        page_size = 100
        
        while page < max_pages:
            voices = JWTAPIClient.search_shared_voices(
                jwt_token,
                search=search,
                page_size=page_size,
                page=page,
                proxies=proxies
            )
            
            all_voices.extend(voices)
            
            # Check if more pages
            if len(voices) < page_size:
                break
            
            page += 1
        
        return all_voices
    
    @staticmethod
    def delete_voice(jwt_token: str, voice_id: str, proxies: Dict = None) -> bool:
        """
        Xóa voice khỏi account
        
        Returns: True nếu thành công
        """
        url = f"{JWTAPIClient.BASE_URL}/v1/voices/{voice_id}"
        headers = JWTAPIClient._get_headers(jwt_token)
        # DELETE không cần Content-Type
        headers.pop("Content-Type", None)
        
        try:
            resp = requests.delete(url, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code in (200, 204):
                print(f"✅ Deleted voice: {voice_id}")
                return True
            else:
                print(f"❌ Delete voice failed: {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"❌ Delete voice error: {e}")
            return False
    
    @staticmethod
    def cleanup_custom_voices(jwt_token: str, proxies: Dict = None) -> int:
        """
        Xóa tất cả custom voices (không phải premade)
        
        Returns: Số voices đã xóa
        """
        voices = JWTAPIClient.list_voices(jwt_token, proxies)
        deleted = 0
        
        for voice in voices:
            category = voice.get("category", "").lower()
            # Chỉ xóa custom voices, giữ lại premade/default
            if category not in ("premade", "default"):
                voice_id = voice.get("voice_id")
                if voice_id and JWTAPIClient.delete_voice(jwt_token, voice_id, proxies):
                    deleted += 1
                    time.sleep(0.5)  # Rate limit
        
        return deleted
    
    @staticmethod
    def cleanup_library_voices(jwt_token: str, proxies: Dict = None) -> int:
        """
        Xóa tất cả library voices (voices được add từ Voice Library)
        Giữ lại premade voices
        
        Args:
            jwt_token: JWT token
            proxies: Proxy config
        
        Returns: Số voices đã xóa
        """
        voices = JWTAPIClient.list_voices(jwt_token, proxies)
        deleted = 0
        
        # Log để debug
        print(f"🔍 Found {len(voices)} voices in account")
        categories = {}
        deletable = []
        
        for voice in voices:
            voice_id = voice.get("voice_id", "")
            name = voice.get("name", "Unknown")[:15]
            category = voice.get("category", "").lower()
            sharing = voice.get("sharing")
            
            # Track categories
            categories[category] = categories.get(category, 0) + 1
            print(f"   📋 {name} ({voice_id[:8]}...) cat={category} sharing={sharing}")
            
            # Skip premade/default voices - không thể xóa
            if category in ("premade", "default"):
                continue
            
            # This is a library/custom voice - can delete
            deletable.append(voice)
        
        print(f"🔍 Voice categories: {categories}")
        print(f"🔍 Voices to delete (non-premade): {len(deletable)}")
        
        # Delete ALL library voices - không giữ lại gì
        for voice in deletable:
            voice_id = voice.get("voice_id")
            if voice_id and JWTAPIClient.delete_voice(jwt_token, voice_id, proxies):
                deleted += 1
                time.sleep(0.5)  # Rate limit
        
        print(f"🧹 Cleanup complete: deleted {deleted} voices")
        return deleted
    
    @staticmethod
    def get_voice_info(jwt_token: str, voice_id: str, proxies: Dict = None) -> Optional[Dict]:
        """
        Lấy thông tin voice từ Voice Library (shared voice)
        
        Thử nhiều endpoints:
        1. /v1/shared-voices?search={voice_id} - search shared voices
        2. /v1/voices/{voice_id} - cho voices đã có trong library
        
        Returns: Voice info dict hoặc None
        """
        headers = JWTAPIClient._get_headers(jwt_token)
        
        # Try shared-voices search endpoint first (for voices from Voice Library)
        try:
            url = f"{JWTAPIClient.BASE_URL}/v1/shared-voices"
            params = {
                "search": voice_id,
                "page_size": 30,
                "page": 0
            }
            resp = requests.get(url, headers=headers, params=params, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                voices = data.get("voices", [])
                # Find exact match by voice_id
                for voice in voices:
                    if voice.get("voice_id") == voice_id:
                        print(f"✅ Found voice in shared-voices: {voice.get('name')}")
                        return voice
                # If not found by exact match, return first result if any
                if voices:
                    print(f"⚠️ Voice not exact match, using first result: {voices[0].get('name')}")
                    return voices[0]
        except Exception as e:
            print(f"⚠️ Shared-voices search error: {e}")
        
        # Fallback to regular voices endpoint (for voices already in library)
        try:
            url = f"{JWTAPIClient.BASE_URL}/v1/voices/{voice_id}"
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ Get voice info failed: {resp.status_code}")
                return None
        except Exception as e:
            print(f"❌ Get voice info error: {e}")
            return None
    
    @staticmethod
    def add_voice_to_library(jwt_token: str, voice_id: str, proxies: Dict = None) -> Tuple[bool, str]:
        """
        Add voice từ Voice Library vào My Voices
        
        API endpoint: POST /v1/voices/add/{public_user_id}/{voice_id}
        Payload: {"new_name": "Voice Name"}
        
        Returns: (success, error_message)
        """
        try:
            headers = JWTAPIClient._get_headers(jwt_token)
            
            # Step 1: Get voice info to find public_owner_id
            print(f"🔍 Getting voice info for {voice_id[:8]}...")
            voice_info = JWTAPIClient.get_voice_info(jwt_token, voice_id, proxies)
            
            if not voice_info:
                # Try to add without owner info (might work for some voices)
                print(f"   ⚠️ Could not get voice info, trying direct add...")
                url = f"{JWTAPIClient.BASE_URL}/v1/voices/add/{voice_id}"
                resp = requests.post(url, json={}, headers=headers, proxies=proxies, timeout=30)
                if resp.status_code in (200, 201):
                    print(f"✅ Voice added to library (direct): {voice_id[:8]}...")
                    return (True, None)
                else:
                    error = resp.text[:200]
                    print(f"❌ Direct add failed: {resp.status_code} - {error}")
                    return (False, f"HTTP {resp.status_code}: {error}")
            
            # Extract voice info
            voice_name = voice_info.get("name", "Unknown Voice")
            # Try different field names for public owner ID
            public_owner_id = (
                voice_info.get("public_owner_id") or 
                voice_info.get("owner_id") or
                voice_info.get("creator_user_id")
            )
            
            print(f"   Voice: {voice_name}")
            print(f"   Owner ID: {public_owner_id[:16] if public_owner_id else 'None'}...")
            
            if not public_owner_id:
                # Fallback: try direct add
                print(f"   ⚠️ No owner ID found, trying direct add...")
                url = f"{JWTAPIClient.BASE_URL}/v1/voices/add/{voice_id}"
                resp = requests.post(url, json={}, headers=headers, proxies=proxies, timeout=30)
                if resp.status_code in (200, 201):
                    print(f"✅ Voice added to library (direct): {voice_name}")
                    return (True, None)
                else:
                    error = resp.text[:200]
                    print(f"❌ Direct add failed: {resp.status_code} - {error}")
                    return (False, f"HTTP {resp.status_code}: {error}")
            
            # Step 2: Add voice using correct endpoint format
            # POST /v1/voices/add/{public_user_id}/{voice_id}
            url = f"{JWTAPIClient.BASE_URL}/v1/voices/add/{public_owner_id}/{voice_id}"
            payload = {"new_name": voice_name}
            
            print(f"➕ Adding voice: {voice_name} ({voice_id[:8]}...)")
            print(f"   URL: /v1/voices/add/{public_owner_id[:8]}.../{voice_id[:8]}...")
            
            resp = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=30)
            
            if resp.status_code in (200, 201):
                print(f"✅ Voice added to library: {voice_name}")
                return (True, None)
            else:
                error = resp.text[:200]
                print(f"❌ Add voice failed: {resp.status_code} - {error}")
                return (False, f"HTTP {resp.status_code}: {error}")
                
        except Exception as e:
            print(f"❌ Add voice error: {e}")
            return (False, str(e))


class JWTAccountPool:
    """
    Pool quản lý JWT accounts với state machine
    
    Features:
    - Load accounts từ file TXT (email|password)
    - Auto login và refresh JWT
    - Rotation khi rate limit
    - Track usage per account
    """
    
    def __init__(self, log_fn: Callable = None):
        self._accounts: Dict[str, JWTAccount] = {}  # email -> JWTAccount
        self._lock = threading.RLock()
        self._log_fn = log_fn or print
        self._current_index = 0
        self._accounts_list: List[str] = []  # List emails để round-robin
        
        # Session tracking
        self._in_use: Set[str] = set()  # emails đang được sử dụng
        
        # Cache file
        self._cache_file = "jwt_accounts_cache.json"
        self._load_cache()
    
    def _log(self, msg: str):
        """Log message"""
        try:
            self._log_fn(msg)
        except:
            print(msg)
    
    # ------------------------------------------------------------------
    # CACHE: Lưu/load JWT tokens
    # ------------------------------------------------------------------
    def _load_cache(self):
        """Load JWT cache từ file"""
        if not os.path.exists(self._cache_file):
            return
        
        try:
            with open(self._cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for email, info in data.items():
                if email in self._accounts:
                    acc = self._accounts[email]
                    acc.jwt_token = info.get('jwt_token')
                    acc.refresh_token = info.get('refresh_token')
                    acc.jwt_expires_at = info.get('jwt_expires_at', 0)
                    
            self._log(f"📦 Loaded JWT cache for {len(data)} accounts")
        except Exception as e:
            self._log(f"⚠️ Failed to load JWT cache: {e}")
    
    def _save_cache(self):
        """Save JWT cache vào file"""
        try:
            data = {}
            with self._lock:
                for email, acc in self._accounts.items():
                    if acc.jwt_token:
                        data[email] = {
                            'jwt_token': acc.jwt_token,
                            'refresh_token': acc.refresh_token,
                            'jwt_expires_at': acc.jwt_expires_at,
                        }
            
            with open(self._cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self._log(f"⚠️ Failed to save JWT cache: {e}")
    
    # ------------------------------------------------------------------
    # LOAD: Load accounts từ file
    # ------------------------------------------------------------------
    def load_from_file(self, file_path: str) -> int:
        """
        Load accounts từ file TXT
        Format: email|password (mỗi dòng 1 account)
        
        Returns: số accounts loaded
        """
        if not os.path.exists(file_path):
            self._log(f"❌ File not found: {file_path}")
            return 0
        
        loaded = 0
        with self._lock:
            self._accounts.clear()
            self._accounts_list.clear()
            
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    
                    # Parse email|password
                    parts = line.split('|')
                    if len(parts) >= 2:
                        email = parts[0].strip()
                        password = parts[1].strip()
                        
                        if email and password:
                            acc = JWTAccount(email=email, password=password)
                            self._accounts[email] = acc
                            self._accounts_list.append(email)
                            loaded += 1
        
        # Load cache sau khi có accounts
        self._load_cache()
        
        self._log(f"✅ Loaded {loaded} JWT accounts from {os.path.basename(file_path)}")
        return loaded
    
    def load_from_list(self, accounts: List[Tuple[str, str]]) -> int:
        """
        Load accounts từ list
        
        Args:
            accounts: List of (email, password) tuples
        
        Returns: số accounts loaded
        """
        loaded = 0
        with self._lock:
            self._accounts.clear()
            self._accounts_list.clear()
            
            for email, password in accounts:
                if email and password:
                    acc = JWTAccount(email=email, password=password)
                    self._accounts[email] = acc
                    self._accounts_list.append(email)
                    loaded += 1
        
        self._load_cache()
        self._log(f"✅ Loaded {loaded} JWT accounts")
        return loaded
    
    # ------------------------------------------------------------------
    # AUTH: Login và refresh JWT
    # ------------------------------------------------------------------
    def _login(self, acc: JWTAccount, max_retries: int = 3) -> bool:
        """
        Login để lấy JWT token
        
        🔧 FIX: Thêm retry với delay khi gặp QUOTA_EXCEEDED từ Firebase
        
        Returns: True nếu thành công
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
                    acc.jwt_token = data.get('idToken')
                    acc.refresh_token = data.get('refreshToken')
                    expires_in = int(data.get('expiresIn', 3600))
                    acc.jwt_expires_at = int(time.time()) + expires_in
                    acc.state = AccountState.READY
                    acc.failure_count = 0
                    acc.last_error = None
                    
                    self._save_cache()
                    self._log(f"✅ Login OK: {acc.email}")
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
                            acc.last_error = f"Firebase QUOTA_EXCEEDED after {max_retries} retries"
                            acc.state = AccountState.TEMP_LOCK
                            acc.locked_until = time.time() + 60  # Lock 60s
                            self._log(f"⚠️ Firebase quota exceeded: {acc.email} - TEMP_LOCK 60s")
                            return False
                    
                    acc.last_error = msg
                    acc.failure_count += 1
                    
                    # Check nếu account bị khóa/invalid
                    if 'INVALID' in msg or 'DISABLED' in msg or 'NOT_FOUND' in msg:
                        acc.state = AccountState.DEAD
                        self._log(f"☠️ Account DEAD: {acc.email} - {msg}")
                    else:
                        acc.state = AccountState.TEMP_LOCK
                        acc.locked_until = time.time() + LOCK_DURATION['unknown']
                        self._log(f"⚠️ Login failed: {acc.email} - {msg}")
                    
                    return False
                    
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                acc.last_error = str(e)
                acc.failure_count += 1
                acc.state = AccountState.TEMP_LOCK
                acc.locked_until = time.time() + LOCK_DURATION['network_error']
                self._log(f"❌ Login error: {acc.email} - {e}")
                return False
        
        return False
    
    def _refresh_jwt(self, acc: JWTAccount) -> bool:
        """
        Refresh JWT token
        
        Returns: True nếu thành công
        """
        if not acc.refresh_token:
            return self._login(acc)
        
        self._log(f"🔄 Refreshing JWT: {acc.email}...")
        
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
                acc.jwt_token = data.get('id_token')
                acc.refresh_token = data.get('refresh_token')
                expires_in = int(data.get('expires_in', 3600))
                acc.jwt_expires_at = int(time.time()) + expires_in
                acc.state = AccountState.READY
                
                self._save_cache()
                self._log(f"✅ Refresh OK: {acc.email}")
                return True
            else:
                # Refresh fail -> try login
                self._log(f"⚠️ Refresh failed, trying login...")
                return self._login(acc)
                
        except Exception as e:
            self._log(f"⚠️ Refresh error: {e}, trying login...")
            return self._login(acc)
    
    def _ensure_jwt(self, acc: JWTAccount) -> bool:
        """
        Đảm bảo account có JWT valid
        
        Returns: True nếu có JWT valid
        """
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
    def get_account(self, excluded: Set[str] = None, allow_concurrent: bool = True) -> Optional[JWTAccount]:
        """
        Lấy 1 account READY có JWT valid
        
        Args:
            excluded: Set emails để exclude
            allow_concurrent: Cho phép dùng account đang in_use (default True cho JWT)
        
        Returns: JWTAccount hoặc None
        """
        excluded = excluded or set()
        
        with self._lock:
            if not self._accounts_list:
                return None
            
            # Unlock expired TEMP_LOCK
            self._unlock_expired()
            
            # Thử round-robin
            tried = set()
            
            while len(tried) < len(self._accounts_list):
                email = self._accounts_list[self._current_index]
                self._current_index = (self._current_index + 1) % len(self._accounts_list)
                
                if email in tried or email in excluded:
                    tried.add(email)
                    continue
                
                # Skip in_use accounts only if allow_concurrent is False
                if not allow_concurrent and email in self._in_use:
                    tried.add(email)
                    continue
                
                tried.add(email)
                acc = self._accounts.get(email)
                
                if not acc or not acc.is_usable():
                    continue
                
                # Ensure JWT valid
                if self._ensure_jwt(acc):
                    self._in_use.add(email)
                    acc.last_used = time.time()
                    self._log(f"✅ PICK account: {email}")
                    return acc
            
            self._log(f"❌ No usable account (total={len(self._accounts_list)}, excluded={len(excluded)}, in_use={len(self._in_use)})")
            return None
    
    def _unlock_expired(self):
        """Unlock các TEMP_LOCK đã hết hạn"""
        now = time.time()
        for acc in self._accounts.values():
            if acc.state == AccountState.TEMP_LOCK:
                if acc.locked_until and now >= acc.locked_until:
                    acc.state = AccountState.READY
                    acc.locked_until = None
                    self._log(f"🔓 Unlocked: {acc.email}")
    
    # ------------------------------------------------------------------
    # RELEASE: Trả account sau khi dùng
    # ------------------------------------------------------------------
    def release_account(self, email: str, success: bool, chars_used: int = 0, 
                       error_type: str = None, error_msg: str = None):
        """
        Release account sau khi sử dụng
        
        Args:
            email: Email của account
            success: True nếu gen thành công
            chars_used: Số ký tự đã gen
            error_type: Loại lỗi (rate_limit, auth_error, quota_exceeded, etc.)
            error_msg: Chi tiết lỗi
        """
        with self._lock:
            self._in_use.discard(email)
            
            acc = self._accounts.get(email)
            if not acc:
                return
            
            if success:
                acc.success_count += 1
                acc.failure_count = 0
                acc.total_chars_used += chars_used
                acc.state = AccountState.READY
                acc.last_error = None
                self._log(f"✅ Account success: {email} (+{chars_used} chars)")
            else:
                acc.failure_count += 1
                acc.last_error = error_msg or error_type
                
                if error_type:
                    error_type_lower = error_type.lower()
                    
                    if error_type_lower in ('auth_error', '401', 'invalid_token'):
                        # JWT hết hạn hoặc invalid -> thử refresh
                        acc.jwt_token = None  # Force re-login
                        acc.state = AccountState.TEMP_LOCK
                        acc.locked_until = time.time() + 5  # 5s để refresh
                        self._log(f"🔄 Account needs refresh: {email}")
                        
                    elif error_type_lower in ('quota_exceeded', 'insufficient_credits', '402'):
                        acc.state = AccountState.EXHAUSTED
                        self._log(f"💸 Account exhausted: {email}")
                        
                    elif error_type_lower in ('rate_limit', '429'):
                        acc.state = AccountState.TEMP_LOCK
                        acc.locked_until = time.time() + LOCK_DURATION['rate_limit']
                        self._log(f"🔒 Account rate limited: {email} ({LOCK_DURATION['rate_limit']}s)")
                        
                    elif error_type_lower in ('disabled', 'banned'):
                        acc.state = AccountState.DEAD
                        self._log(f"☠️ Account DEAD: {email}")
                        
                    else:
                        # Network error - DON'T lock account, just release it
                        # Network errors are usually proxy issues, not account issues
                        acc.state = AccountState.READY  # Keep READY, don't lock
                        self._log(f"⚠️ Account error: {email} - {error_type}")
    
    # ------------------------------------------------------------------
    # STATS: Thống kê
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict:
        """Lấy thống kê accounts"""
        with self._lock:
            stats = {
                'total': len(self._accounts),
                'ready': 0,
                'temp_lock': 0,
                'exhausted': 0,
                'dead': 0,
                'in_use': len(self._in_use),
                'total_chars': 0,
            }
            
            for acc in self._accounts.values():
                if acc.state == AccountState.READY:
                    stats['ready'] += 1
                elif acc.state == AccountState.TEMP_LOCK:
                    stats['temp_lock'] += 1
                elif acc.state == AccountState.EXHAUSTED:
                    stats['exhausted'] += 1
                elif acc.state == AccountState.DEAD:
                    stats['dead'] += 1
                
                stats['total_chars'] += acc.total_chars_used
            
            return stats
    
    def get_account_list(self) -> List[Dict]:
        """Lấy danh sách accounts với trạng thái"""
        with self._lock:
            result = []
            for email in self._accounts_list:
                acc = self._accounts[email]
                result.append({
                    'email': email,
                    'state': acc.state.value,
                    'success_count': acc.success_count,
                    'failure_count': acc.failure_count,
                    'total_chars': acc.total_chars_used,
                    'has_jwt': acc.jwt_token is not None,
                    'jwt_valid': acc.is_jwt_valid(),
                    'in_use': email in self._in_use,
                })
            return result
    
    def check_account_credits(self, email: str, proxies: Dict = None) -> Tuple[int, int]:
        """
        Check credits của account
        
        Returns: (remaining, limit)
        """
        with self._lock:
            acc = self._accounts.get(email)
            if not acc:
                return (0, 0)
        
        # Ensure JWT valid
        if not self._ensure_jwt(acc):
            return (0, 0)
        
        return JWTAPIClient.get_credits(acc.jwt_token, proxies)
    
    def cleanup_account_voices(self, email: str, proxies: Dict = None) -> int:
        """
        Cleanup ALL library voices của account (không phải premade).
        Dùng cleanup_library_voices() để xóa cả shared voices được add vào.
        
        Returns: Số voices đã xóa
        """
        with self._lock:
            acc = self._accounts.get(email)
            if not acc:
                return 0
        
        # Ensure JWT valid
        if not self._ensure_jwt(acc):
            return 0
        
        self._log(f"🧹 Cleaning up voices for {email}...")
        # 🔧 FIX: Dùng cleanup_library_voices thay vì cleanup_custom_voices
        # để xóa cả shared voices (như Linh, Kozy) được add vào library
        deleted = JWTAPIClient.cleanup_library_voices(acc.jwt_token, proxies)
        self._log(f"✅ Deleted {deleted} library voices from {email}")
        
        return deleted
    
    def get_all_accounts_credits(self, proxies: Dict = None) -> List[Dict]:
        """
        Check credits của tất cả accounts
        
        Returns: List of {email, remaining, limit, state}
        """
        results = []
        
        with self._lock:
            accounts_copy = list(self._accounts.items())
        
        for email, acc in accounts_copy:
            # Ensure JWT
            if not self._ensure_jwt(acc):
                results.append({
                    'email': email,
                    'remaining': 0,
                    'limit': 0,
                    'state': acc.state.value,
                    'error': 'JWT failed'
                })
                continue
            
            remaining, limit = JWTAPIClient.get_credits(acc.jwt_token, proxies)
            results.append({
                'email': email,
                'remaining': remaining,
                'limit': limit,
                'state': acc.state.value,
            })
            
            # Update state based on credits
            if remaining < 100:
                acc.state = AccountState.EXHAUSTED
                self._log(f"💸 Account {email} exhausted: {remaining} credits")
            
            time.sleep(0.3)  # Rate limit
        
        return results
    
    @property
    def total_accounts(self) -> int:
        """Tổng số accounts"""
        return len(self._accounts)
    
    @property
    def ready_accounts(self) -> int:
        """Số accounts READY"""
        with self._lock:
            return sum(1 for acc in self._accounts.values() 
                      if acc.state == AccountState.READY and acc not in self._in_use)


# ============== DEMO ==============
if __name__ == "__main__":
    print("="*60)
    print("🔑 JWT Account Pool Demo")
    print("="*60)
    
    pool = JWTAccountPool()
    
    # Test load từ file
    test_file = "jwt_accounts.txt"
    if os.path.exists(test_file):
        pool.load_from_file(test_file)
    else:
        # Demo với 1 account
        email = input("Email: ").strip()
        password = input("Password: ").strip()
        if email and password:
            pool.load_from_list([(email, password)])
    
    # Test get account
    acc = pool.get_account()
    if acc:
        print(f"\n✅ Got account: {acc.email}")
        print(f"   JWT: {acc.jwt_token[:50] if acc.jwt_token else 'None'}...")
        print(f"   Expires: {datetime.fromtimestamp(acc.jwt_expires_at)}")
        
        # Release
        pool.release_account(acc.email, success=True, chars_used=100)
    
    # Stats
    print(f"\n📊 Stats: {pool.get_stats()}")
