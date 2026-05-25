"""
Key Pool V2 - State Machine Architecture
Theo Plan_Key_Optimize.md

Nguyên tắc:
- DB là nguồn sự thật duy nhất
- Tool chỉ dùng, không suy luận
- Key đã bị loại (LOW_CREDIT, DEAD) -> KHÔNG BAO GIỜ QUAY LẠI
- TEMP_LOCK có thời gian chờ, tự unlock khi hết hạn
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

import requests
from requests.exceptions import RequestException

logger = logging.getLogger("key_pool_v2")


class KeyState(Enum):
    """4 trạng thái duy nhất của key"""
    READY = "READY"           # Đủ credit, dùng được
    LOW_CREDIT = "LOW_CREDIT" # Không đủ để gen -> VĨNH VIỄN
    TEMP_LOCK = "TEMP_LOCK"   # Lỗi tạm (rate, net) -> có thời gian chờ
    DEAD = "DEAD"             # Revoke / invalid -> VĨNH VIỄN


# Ngưỡng credit tối thiểu để coi là READY
MIN_CREDIT_THRESHOLD = 100

# Thời gian lock cho các loại lỗi
LOCK_DURATION = {
    "rate_limit": 60,      # 60 giây
    "network_error": 30,   # 30 giây
    "timeout": 30,         # 30 giây
    "unknown": 30,         # 30 giây
}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.replace(tzinfo=None).isoformat()


@dataclass
class KeyEntry:
    """Đại diện cho 1 key trong pool"""
    id: int
    api_key: str
    credit_remaining: int
    state: KeyState
    locked_until: Optional[datetime] = None
    last_error: Optional[str] = None
    failure_count: int = 0
    last_used: Optional[datetime] = None
    last_credit_check: Optional[datetime] = None
    
    @property
    def is_active(self) -> bool:
        """Backward compatible: key có active không (state = READY)"""
        return self.state == KeyState.READY
    
    def is_usable(self) -> bool:
        """Key có thể dùng được không?"""
        if self.state != KeyState.READY:
            return False
        if self.credit_remaining < MIN_CREDIT_THRESHOLD:
            return False
        return True
    
    def is_temp_locked(self) -> bool:
        """Key đang bị TEMP_LOCK?"""
        if self.state != KeyState.TEMP_LOCK:
            return False
        if self.locked_until and self.locked_until > _utcnow():
            return True
        return False


class KeyPoolV2:
    """
    Key Pool với State Machine Architecture
    
    Đặc điểm:
    - 1 query để pick key (không retry)
    - DB là nguồn sự thật duy nhất
    - Key bị loại vĩnh viễn (LOW_CREDIT, DEAD)
    - TEMP_LOCK tự unlock khi hết hạn
    """
    
    API_TIMEOUT = 5
    
    def __init__(self, supabase_client, user_id: int) -> None:
        self.supabase = supabase_client
        self.user_id = user_id
        self._lock = threading.RLock()
        self._cache: Dict[str, KeyEntry] = {}
        self._last_load: Optional[datetime] = None
        # 🔧 NEW: Session blacklist - track keys đã fail 401 trong session này
        self._session_blacklist: Set[str] = set()
    
    # ------------------------------------------------------------------
    # LOAD: Tải key từ DB
    # ------------------------------------------------------------------
    def load(self, load_all: bool = False, progress_callback=None) -> int:
        """
        Tải tất cả key READY từ DB
        Args:
            load_all: Nếu True, load tất cả key (kể cả không READY) - backward compatible
            progress_callback: Callback để báo tiến độ
        Returns: số key READY
        """
        if progress_callback:
            progress_callback(0, "Loading keys from database...")
        
        try:
            # Trước tiên, unlock các TEMP_LOCK đã hết hạn
            self._unlock_expired_temp_locks()
            
            # Lấy tất cả key của user
            query = (
                self.supabase.table("user_api_keys")
                .select("id, api_key, credit_remaining, key_state, locked_until, "
                       "last_error, failure_count, last_used, last_credit_check, is_active")
                .eq("user_id", self.user_id)
            )
            
            # Nếu không load_all, chỉ lấy key READY
            if not load_all:
                query = query.eq("key_state", "READY")
            
            result = query.execute()
            
            entries = {}
            ready_count = 0
            
            for row in (result.data or []):
                state_str = row.get("key_state", "READY")
                try:
                    state = KeyState(state_str)
                except ValueError:
                    state = KeyState.READY
                
                locked_until = None
                if row.get("locked_until"):
                    try:
                        locked_until = datetime.fromisoformat(
                            row["locked_until"].replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except:
                        pass
                
                entry = KeyEntry(
                    id=row["id"],
                    api_key=row["api_key"],
                    credit_remaining=row.get("credit_remaining", 0) or 0,
                    state=state,
                    locked_until=locked_until,
                    last_error=row.get("last_error"),
                    failure_count=row.get("failure_count", 0) or 0,
                )
                
                entries[entry.api_key] = entry
                if entry.is_usable():
                    ready_count += 1
            
            with self._lock:
                self._cache = entries
                self._last_load = _utcnow()
            
            logger.info(
                "KEY_POOL_V2_LOAD ✅ user=%s total=%s ready=%s",
                self.user_id, len(entries), ready_count
            )
            
            if progress_callback:
                progress_callback(100, f"Loaded {ready_count} ready keys")
            
            return ready_count
            
        except Exception as e:
            logger.error("KEY_POOL_V2_LOAD_ERROR: %s", e)
            if progress_callback:
                progress_callback(0, f"Error: {e}")
            return 0
    
    def _unlock_expired_temp_locks(self) -> int:
        """Unlock các TEMP_LOCK đã hết hạn trong DB"""
        try:
            result = (
                self.supabase.table("user_api_keys")
                .update({
                    "key_state": "READY",
                    "locked_until": None,
                    "failure_count": 0,
                    "updated_at": _iso(_utcnow())
                })
                .eq("user_id", self.user_id)
                .eq("key_state", "TEMP_LOCK")
                .lt("locked_until", _iso(_utcnow()))
                .execute()
            )
            count = len(result.data) if result.data else 0
            if count > 0:
                logger.info("Unlocked %s expired TEMP_LOCK keys", count)
            return count
        except Exception as e:
            logger.warning("Failed to unlock expired keys: %s", e)
            return 0
    
    # ------------------------------------------------------------------
    # PICK KEY: Lấy 1 key READY (1 query, 0 retry)
    # ------------------------------------------------------------------
    def get_key(self, required_credits: int = MIN_CREDIT_THRESHOLD, 
                excluded: Optional[Set[str]] = None) -> Optional[str]:
        """
        Lấy 1 key READY có đủ credit
        
        Nguyên tắc:
        - 1 query duy nhất
        - Không retry
        - Ưu tiên key có credit cao nhất
        - 🔧 NEW: Exclude session blacklist (keys đã fail 401 trong session này)
        
        Returns: api_key hoặc None
        """
        excluded = excluded or set()
        
        # 🔧 NEW: Merge với session blacklist
        with self._lock:
            all_excluded = excluded | self._session_blacklist
        
        # Unlock expired TEMP_LOCK trước
        self._unlock_expired_temp_locks()
        
        try:
            # Query trực tiếp từ DB - 1 query duy nhất
            query = (
                self.supabase.table("user_api_keys")
                .select("id, api_key, credit_remaining")
                .eq("user_id", self.user_id)
                .eq("key_state", "READY")
                .gte("credit_remaining", required_credits)
                .order("credit_remaining", desc=True)
                .limit(20)  # 🔧 FIX: Tăng lên 20 để có nhiều lựa chọn hơn khi exclude
            )
            
            result = query.execute()
            
            for row in (result.data or []):
                api_key = row["api_key"]
                if api_key not in all_excluded:
                    # Cập nhật cache
                    with self._lock:
                        if api_key in self._cache:
                            self._cache[api_key].credit_remaining = row["credit_remaining"]
                    
                    logger.debug(
                        "PICK_KEY ✅ key=%s credits=%s",
                        api_key[:10], row["credit_remaining"]
                    )
                    return api_key
            
            logger.warning("PICK_KEY ❌ No usable key found (excluded=%s, session_blacklist=%s)", 
                          len(excluded), len(self._session_blacklist))
            return None
            
        except Exception as e:
            logger.error("PICK_KEY_ERROR: %s", e)
            return None
    
    def get_any_active_key(self) -> Optional[str]:
        """Lấy bất kỳ key READY nào"""
        return self.get_key(MIN_CREDIT_THRESHOLD)
    
    # ------------------------------------------------------------------
    # UPDATE STATE: Cập nhật trạng thái sau khi dùng
    # ------------------------------------------------------------------
    def report_success(self, api_key: str, used_credits: int) -> None:
        """
        Báo cáo sử dụng thành công
        - Trừ credit
        - Nếu credit < ngưỡng -> LOW_CREDIT (vĩnh viễn)
        """
        try:
            # Lấy credit hiện tại
            result = (
                self.supabase.table("user_api_keys")
                .select("id, credit_remaining")
                .eq("api_key", api_key)
                .eq("user_id", self.user_id)
                .single()
                .execute()
            )
            
            if not result.data:
                return
            
            key_id = result.data["id"]
            current_credits = result.data["credit_remaining"] or 0
            new_credits = max(0, current_credits - used_credits)
            
            # Xác định state mới
            new_state = "READY"
            exhausted_at = None
            
            if new_credits < MIN_CREDIT_THRESHOLD:
                new_state = "LOW_CREDIT"
                exhausted_at = _iso(_utcnow())
                logger.info(
                    "KEY_LOW_CREDIT ⚠️ key=%s credits=%s -> LOW_CREDIT (vĩnh viễn)",
                    api_key[:10], new_credits
                )
            
            # Update DB
            update_data = {
                "credit_remaining": new_credits,
                "key_state": new_state,
                "last_used": _iso(_utcnow()),
                "updated_at": _iso(_utcnow()),
                "failure_count": 0,
                "last_error": None,
            }
            if exhausted_at:
                update_data["exhausted_at"] = exhausted_at
            
            self.supabase.table("user_api_keys")\
                .update(update_data)\
                .eq("id", key_id)\
                .execute()
            
            # Update cache
            with self._lock:
                if api_key in self._cache:
                    self._cache[api_key].credit_remaining = new_credits
                    self._cache[api_key].state = KeyState(new_state)
                    self._cache[api_key].failure_count = 0
                    self._cache[api_key].last_error = None
            
        except Exception as e:
            logger.error("REPORT_SUCCESS_ERROR: %s", e)
    
    def report_error(self, api_key: str, error_type: str, error_message: str = "") -> None:
        """
        Báo cáo lỗi
        
        error_type:
        - "auth_error", "401", "invalid_key" -> DEAD (vĩnh viễn)
        - "quota_exceeded", "insufficient_credits" -> LOW_CREDIT (vĩnh viễn)
        - "rate_limit", "429" -> TEMP_LOCK 60s
        - "network_error", "timeout" -> TEMP_LOCK 30s
        - "voice_limit" -> DEAD (vĩnh viễn)
        """
        try:
            result = (
                self.supabase.table("user_api_keys")
                .select("id, failure_count")
                .eq("api_key", api_key)
                .eq("user_id", self.user_id)
                .single()
                .execute()
            )
            
            if not result.data:
                return
            
            key_id = result.data["id"]
            failure_count = (result.data.get("failure_count") or 0) + 1
            
            # Xác định state và lock duration
            new_state = "READY"
            locked_until = None
            exhausted_at = None
            is_active = True
            
            error_type_lower = error_type.lower()
            
            if error_type_lower in ("auth_error", "401", "invalid_key", "voice_limit"):
                # DEAD - vĩnh viễn
                new_state = "DEAD"
                is_active = False
                exhausted_at = _iso(_utcnow())
                
                # 🔧 NEW: Thêm vào session blacklist để không pick lại trong session này
                with self._lock:
                    self._session_blacklist.add(api_key)
                
                logger.warning(
                    "KEY_DEAD ☠️ key=%s error=%s -> DEAD (vĩnh viễn)",
                    api_key[:10], error_type
                )
                
            elif error_type_lower in ("quota_exceeded", "insufficient_credits"):
                # LOW_CREDIT - vĩnh viễn
                new_state = "LOW_CREDIT"
                exhausted_at = _iso(_utcnow())
                logger.warning(
                    "KEY_LOW_CREDIT ⚠️ key=%s error=%s -> LOW_CREDIT (vĩnh viễn)",
                    api_key[:10], error_type
                )
                
            elif error_type_lower in ("rate_limit", "429"):
                # TEMP_LOCK 60s
                new_state = "TEMP_LOCK"
                lock_seconds = LOCK_DURATION.get("rate_limit", 60)
                locked_until = _iso(_utcnow() + timedelta(seconds=lock_seconds))
                logger.info(
                    "KEY_TEMP_LOCK 🔒 key=%s error=%s -> TEMP_LOCK %ss",
                    api_key[:10], error_type, lock_seconds
                )
                
            elif error_type_lower in ("network_error", "timeout"):
                # TEMP_LOCK 30s
                new_state = "TEMP_LOCK"
                lock_seconds = LOCK_DURATION.get("network_error", 30)
                locked_until = _iso(_utcnow() + timedelta(seconds=lock_seconds))
                logger.info(
                    "KEY_TEMP_LOCK 🔒 key=%s error=%s -> TEMP_LOCK %ss",
                    api_key[:10], error_type, lock_seconds
                )
                
            else:
                # Unknown error -> TEMP_LOCK 30s
                new_state = "TEMP_LOCK"
                lock_seconds = LOCK_DURATION.get("unknown", 30)
                locked_until = _iso(_utcnow() + timedelta(seconds=lock_seconds))
                logger.info(
                    "KEY_TEMP_LOCK 🔒 key=%s error=%s -> TEMP_LOCK %ss",
                    api_key[:10], error_type, lock_seconds
                )
            
            # Update DB
            update_data = {
                "key_state": new_state,
                "is_active": is_active,
                "locked_until": locked_until,
                "last_error": error_message[:500] if error_message else error_type,
                "failure_count": failure_count,
                "updated_at": _iso(_utcnow()),
            }
            if exhausted_at:
                update_data["exhausted_at"] = exhausted_at
            
            self.supabase.table("user_api_keys")\
                .update(update_data)\
                .eq("id", key_id)\
                .execute()
            
            # Update cache
            with self._lock:
                if api_key in self._cache:
                    self._cache[api_key].state = KeyState(new_state)
                    self._cache[api_key].failure_count = failure_count
                    self._cache[api_key].last_error = error_message or error_type
                    if locked_until:
                        self._cache[api_key].locked_until = datetime.fromisoformat(locked_until)
            
        except Exception as e:
            logger.error("REPORT_ERROR_ERROR: %s", e)
    
    # Alias methods cho backward compatibility
    def mark_exhausted(self, api_key: str) -> None:
        """Đánh dấu key hết credit"""
        self.report_error(api_key, "quota_exceeded", "Credit exhausted")
    
    def mark_invalid(self, api_key: str, reason: str = "Invalid key") -> None:
        """Đánh dấu key không hợp lệ"""
        self.report_error(api_key, "auth_error", reason)
    
    def mark_rate_limited(self, api_key: str) -> None:
        """Đánh dấu key bị rate limit"""
        self.report_error(api_key, "rate_limit", "Rate limited")
    
    def mark_voice_limit_reached(self, api_key: str) -> None:
        """Đánh dấu key đạt giới hạn voice"""
        self.report_error(api_key, "voice_limit", "Voice limit reached")
    
    def restore_key_after_voice_cleanup(self, api_key: str) -> bool:
        """
        Khôi phục key về READY sau khi đã cleanup voice slots.
        Chỉ restore nếu key đang ở state DEAD do voice_limit.
        
        Returns: True nếu restore thành công, False nếu không
        """
        try:
            # Kiểm tra key hiện tại
            result = (
                self.supabase.table("user_api_keys")
                .select("id, key_state, last_error, credit_remaining")
                .eq("api_key", api_key)
                .eq("user_id", self.user_id)
                .single()
                .execute()
            )
            
            if not result.data:
                logger.warning("RESTORE_KEY ❌ Key not found: %s", api_key[:10])
                return False
            
            key_id = result.data["id"]
            current_state = result.data.get("key_state", "")
            last_error = result.data.get("last_error", "") or ""
            credit_remaining = result.data.get("credit_remaining", 0) or 0
            
            # Chỉ restore nếu key bị DEAD do voice_limit
            if current_state != "DEAD":
                logger.info("RESTORE_KEY ⏭️ Key not DEAD, skip: %s (state=%s)", api_key[:10], current_state)
                return False
            
            if "voice" not in last_error.lower() and "voice_limit" not in last_error.lower():
                logger.info("RESTORE_KEY ⏭️ Key DEAD but not voice_limit: %s (error=%s)", api_key[:10], last_error)
                return False
            
            # Kiểm tra credit còn đủ không
            if credit_remaining < MIN_CREDIT_THRESHOLD:
                logger.warning("RESTORE_KEY ⚠️ Key has low credit: %s (credits=%s)", api_key[:10], credit_remaining)
                # Vẫn restore nhưng sẽ thành LOW_CREDIT
                new_state = "LOW_CREDIT"
            else:
                new_state = "READY"
            
            # Update DB - restore key
            update_data = {
                "key_state": new_state,
                "is_active": new_state == "READY",
                "locked_until": None,
                "last_error": None,
                "failure_count": 0,
                "exhausted_at": None,
                "updated_at": _iso(_utcnow()),
            }
            
            self.supabase.table("user_api_keys")\
                .update(update_data)\
                .eq("id", key_id)\
                .execute()
            
            # Update cache
            with self._lock:
                if api_key in self._cache:
                    self._cache[api_key].state = KeyState(new_state)
                    self._cache[api_key].failure_count = 0
                    self._cache[api_key].last_error = None
                    self._cache[api_key].locked_until = None
            
            logger.info(
                "RESTORE_KEY ✅ key=%s restored to %s (credits=%s)",
                api_key[:10], new_state, credit_remaining
            )
            return True
            
        except Exception as e:
            logger.error("RESTORE_KEY_ERROR: %s", e)
            return False
    
    def deduct(self, api_key: str, used_credits: int) -> None:
        """Trừ credit sau khi dùng thành công"""
        self.report_success(api_key, used_credits)
    
    # ------------------------------------------------------------------
    # STATS: Thống kê
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict[str, int]:
        """Lấy thống kê key theo state"""
        try:
            result = (
                self.supabase.table("user_api_keys")
                .select("key_state, credit_remaining")
                .eq("user_id", self.user_id)
                .execute()
            )
            
            stats = {
                "READY": 0,
                "LOW_CREDIT": 0,
                "TEMP_LOCK": 0,
                "DEAD": 0,
                "total": 0,
                "total_credits": 0,
            }
            
            for row in (result.data or []):
                state = row.get("key_state", "READY")
                credits = row.get("credit_remaining", 0) or 0
                
                stats["total"] += 1
                if state in stats:
                    stats[state] += 1
                if state == "READY":
                    stats["total_credits"] += credits
            
            return stats
            
        except Exception as e:
            logger.error("GET_STATS_ERROR: %s", e)
            return {}
    
    # ------------------------------------------------------------------
    # VALIDATE: Kiểm tra credit thực từ API
    # ------------------------------------------------------------------
    def validate_and_update_credits(self, api_key: str) -> Optional[int]:
        """
        Gọi ElevenLabs API để lấy credit thực
        Cập nhật DB và trả về credit còn lại
        """
        try:
            resp = requests.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": api_key},
                timeout=(3, self.API_TIMEOUT),
            )
            
            if resp.status_code == 401:
                self.report_error(api_key, "auth_error", "401 Unauthorized")
                return None
            
            if resp.status_code == 429:
                self.report_error(api_key, "rate_limit", "429 Rate Limited")
                return None
            
            if resp.status_code != 200:
                self.report_error(api_key, "unknown", f"HTTP {resp.status_code}")
                return None
            
            data = resp.json()
            subscription = data.get("subscription", {})
            used = int(subscription.get("character_count", 0) or 0)
            limit = int(subscription.get("character_limit", 0) or 0)
            remaining = max(0, limit - used)
            
            # Update DB với credit thực
            self.supabase.table("user_api_keys")\
                .update({
                    "credit_remaining": remaining,
                    "last_credit_check": _iso(_utcnow()),
                    "updated_at": _iso(_utcnow()),
                })\
                .eq("api_key", api_key)\
                .eq("user_id", self.user_id)\
                .execute()
            
            # Check voice limit
            voice_used = int(subscription.get("voice_slots_used", 0) or 0)
            voice_limit = int(subscription.get("voice_limit", 0) or 0)
            if voice_limit and voice_used >= voice_limit:
                self.report_error(api_key, "voice_limit", "Voice limit reached")
                return None
            
            # Check credit threshold
            if remaining < MIN_CREDIT_THRESHOLD:
                self.report_error(api_key, "insufficient_credits", f"Only {remaining} credits")
                return None
            
            logger.info("VALIDATE_CREDITS ✅ key=%s credits=%s", api_key[:10], remaining)
            return remaining
            
        except RequestException as e:
            self.report_error(api_key, "network_error", str(e))
            return None
        except Exception as e:
            logger.error("VALIDATE_CREDITS_ERROR: %s", e)
            return None
    
    def is_key_usable(self, api_key: str) -> bool:
        """Kiểm tra key có dùng được không (từ cache)"""
        with self._lock:
            entry = self._cache.get(api_key)
            if entry:
                return entry.is_usable()
        return False
    
    def get_key_state(self, api_key: str) -> Optional[KeyState]:
        """Lấy state của key (từ cache)"""
        with self._lock:
            entry = self._cache.get(api_key)
            if entry:
                return entry.state
        return None
    
    # ------------------------------------------------------------------
    # BACKWARD COMPATIBILITY: Các method để tương thích với LocalKeyPool cũ
    # ------------------------------------------------------------------
    
    @property
    def _keys(self) -> List[KeyEntry]:
        """Backward compatible: trả về list entries (giống LocalKeyPool cũ)"""
        with self._lock:
            return list(self._cache.values())
    
    @property
    def _by_api(self) -> Dict[str, KeyEntry]:
        """Backward compatible: trả về dict api_key -> entry"""
        with self._lock:
            return dict(self._cache)
    
    def sync_external_remaining(self, api_key: str, new_remaining: int) -> None:
        """
        Backward compatible: Cập nhật credit từ external source
        """
        try:
            self.supabase.table("user_api_keys")\
                .update({
                    "credit_remaining": max(0, new_remaining),
                    "updated_at": _iso(_utcnow()),
                })\
                .eq("api_key", api_key)\
                .eq("user_id", self.user_id)\
                .execute()
            
            # Check if should mark as LOW_CREDIT
            if new_remaining < MIN_CREDIT_THRESHOLD:
                self.report_error(api_key, "insufficient_credits", f"Only {new_remaining} credits")
            else:
                # Update cache
                with self._lock:
                    if api_key in self._cache:
                        self._cache[api_key].credit_remaining = new_remaining
                        
        except Exception as e:
            logger.error("SYNC_EXTERNAL_ERROR: %s", e)
    
    def pre_validate_keys(self, max_keys: int = 10, progress_callback=None) -> int:
        """
        Backward compatible: Validate top N keys bằng cách gọi API
        """
        validated = 0
        
        try:
            # Lấy top keys theo credit
            result = (
                self.supabase.table("user_api_keys")
                .select("api_key, credit_remaining")
                .eq("user_id", self.user_id)
                .eq("key_state", "READY")
                .order("credit_remaining", desc=True)
                .limit(max_keys)
                .execute()
            )
            
            keys_to_validate = result.data or []
            total = len(keys_to_validate)
            
            for idx, row in enumerate(keys_to_validate):
                api_key = row["api_key"]
                
                if progress_callback:
                    progress_callback(
                        50 + int((idx + 1) / max(1, total) * 45),
                        f"Validating key {idx + 1}/{total}...",
                        api_key[:10] + "..."
                    )
                
                # Validate bằng cách gọi API
                credits = self.validate_and_update_credits(api_key)
                if credits is not None and credits >= MIN_CREDIT_THRESHOLD:
                    validated += 1
            
            logger.info("PRE_VALIDATE ✅ validated=%s/%s", validated, total)
            return validated
            
        except Exception as e:
            logger.error("PRE_VALIDATE_ERROR: %s", e)
            return 0
    
    def flush_to_db(self) -> None:
        """Backward compatible: No-op vì V2 update DB ngay lập tức"""
        pass
    
    def sync_state_changes_immediate(self) -> None:
        """Backward compatible: No-op vì V2 update DB ngay lập tức"""
        pass
    
    def validate_api_key_has_credits(self, api_key: str, needed_chars: int, force_check: bool = False) -> bool:
        """
        Backward compatible: Kiểm tra key có đủ credit không
        """
        # Check từ cache trước
        with self._lock:
            entry = self._cache.get(api_key)
            if entry:
                if entry.state != KeyState.READY:
                    return False
                if entry.credit_remaining >= needed_chars + MIN_CREDIT_THRESHOLD and not force_check:
                    return True
        
        # Force check từ API
        if force_check:
            credits = self.validate_and_update_credits(api_key)
            return credits is not None and credits >= needed_chars + MIN_CREDIT_THRESHOLD
        
        return False

    
    # ------------------------------------------------------------------
    # BACKWARD COMPATIBILITY: Methods cho tts_service.py
    # ------------------------------------------------------------------
    
    def _get_real_credits_via_api(self, api_key: str) -> Optional[Tuple[int, dict]]:
        """
        Backward compatible: Gọi ElevenLabs API để lấy credit thực
        Returns: (remaining_credits, voice_info) hoặc None
        """
        try:
            resp = requests.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": api_key},
                timeout=(3, self.API_TIMEOUT),
            )
            
            if resp.status_code == 401:
                self.report_error(api_key, "auth_error", "401 Unauthorized")
                return None
            
            if resp.status_code == 429:
                self.report_error(api_key, "rate_limit", "429 Rate Limited")
                return None
            
            if resp.status_code != 200:
                logger.warning(
                    "REAL_CREDIT_API_ERROR - key=%s code=%s",
                    api_key[:10], resp.status_code
                )
                return None
            
            data = resp.json()
            subscription = data.get("subscription", {})
            used = int(subscription.get("character_count", 0) or 0)
            limit = int(subscription.get("character_limit", 0) or 0)
            remaining = max(0, limit - used)
            
            # Voice info
            voice_slots_used = int(subscription.get("voice_slots_used", 0) or 0)
            voice_limit = int(subscription.get("voice_limit", 0) or 0)
            voice_info = {
                "voice_slots_used": voice_slots_used,
                "voice_limit": voice_limit,
                "is_at_limit": bool(voice_limit and voice_slots_used >= voice_limit),
            }
            
            # Update DB với credit thực
            try:
                new_state = "READY"
                exhausted_at = None
                
                if remaining < MIN_CREDIT_THRESHOLD:
                    new_state = "LOW_CREDIT"
                    exhausted_at = _iso(_utcnow())
                
                update_data = {
                    "credit_remaining": remaining,
                    "key_state": new_state,
                    "last_credit_check": _iso(_utcnow()),
                    "updated_at": _iso(_utcnow()),
                }
                if exhausted_at:
                    update_data["exhausted_at"] = exhausted_at
                
                self.supabase.table("user_api_keys")\
                    .update(update_data)\
                    .eq("api_key", api_key)\
                    .eq("user_id", self.user_id)\
                    .execute()
                
                # Update cache
                with self._lock:
                    if api_key in self._cache:
                        self._cache[api_key].credit_remaining = remaining
                        self._cache[api_key].state = KeyState(new_state)
                        
            except Exception as e:
                logger.warning("UPDATE_CREDITS_DB_ERROR: %s", e)
            
            logger.info(
                "REAL_CREDIT_API_SUCCESS - key=%s remaining=%s",
                api_key[:10], remaining
            )
            return remaining, voice_info
            
        except RequestException as e:
            logger.warning("REAL_CREDIT_API_EXCEPTION - %s err=%s", api_key[:10], e)
            return None
        except Exception as e:
            logger.error("REAL_CREDIT_API_ERROR: %s", e)
            return None
    
    def mark_quota_exceeded(self, api_key: str) -> None:
        """Backward compatible: Đánh dấu key hết quota"""
        self.report_error(api_key, "quota_exceeded", "Quota exceeded")


# Alias cho backward compatibility
LocalKeyPool = KeyPoolV2
