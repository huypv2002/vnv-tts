from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

import requests
from requests.exceptions import RequestException

logger = logging.getLogger("key_pool")


class KeyState(Enum):
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    INVALID = "invalid"
    VOICE_LIMIT_REACHED = "voice_limit_reached"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"


def _utcnow() -> datetime:
    return datetime.utcnow()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.replace(tzinfo=None).isoformat()


@dataclass
class KeyEntry:
    id: int
    api_key: str
    credit_remaining: int
    credit_limit: Optional[int]
    is_active: bool
    state: KeyState
    last_used: Optional[datetime] = None
    last_checked: Optional[datetime] = None
    key_file_id: Optional[int] = None
    active_row_id: Optional[int] = None
    failure_count: int = 0
    last_error: Optional[str] = None
    in_use: bool = False
    reserved_by: Optional[int] = None
    refresh_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def mark_active(self) -> None:
        self.state = KeyState.ACTIVE
        self.is_active = True

    def mark_exhausted(self) -> None:
        self.state = KeyState.EXHAUSTED
        self.is_active = False
        self.credit_remaining = 0

    def mark_invalid(self, reason: str) -> None:
        self.state = KeyState.INVALID
        self.is_active = False
        self.credit_remaining = 0
        self.last_error = reason

    def needs_refresh(self, ttl_seconds: int) -> bool:
        if not self.last_checked:
            return True
        return (_utcnow() - self.last_checked).total_seconds() >= ttl_seconds


class LocalKeyPool:
    """
    Fast credit-aware key pool:
      - keeps cached credits in sync with Supabase `active_api_keys`
      - uses local bookkeeping for deductions
      - only calls ElevenLabs when strictly necessary
    """

    CREDIT_BUFFER = 80
    REFRESH_TTL = 90
    RELOAD_INTERVAL = 120
    API_TIMEOUT = 5

    def __init__(self, supabase_client, user_id: int, accurate_tracker=None) -> None:
        self.supabase = supabase_client
        self.user_id = user_id
        self.accurate_tracker = accurate_tracker

        self._lock = threading.RLock()
        self._keys: List[KeyEntry] = []
        self._by_api: Dict[str, KeyEntry] = {}
        self._needs_sort = True
        self._sorted: List[KeyEntry] = []
        self._last_reload: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self, load_all: bool = False, progress_callback=None) -> None:
        if progress_callback:
            progress_callback(0, "Loading keys...")
        
        try:
            user_keys = (
                self.supabase.table("user_api_keys")
                .select(
                    "id",
                    "api_key",
                    "credit_remaining",
                    "credit_limit",
                    "is_active",
                    "last_used",
                    "updated_at",
                )
                .eq("user_id", self.user_id)
                .execute()
            )
        except Exception as exc:
            logger.error("KEY_POOL_LOAD_ERROR - %s", exc, exc_info=True)
            with self._lock:
                self._keys = []
                self._by_api = {}
                self._needs_sort = True
            if progress_callback:
                progress_callback(0, f"❌ Error loading keys: {exc}")
            return

        try:
            cache_rows = (
                self.supabase.table("active_api_keys")
                .select(
                    "api_key",
                    "cached_credits",
                    "is_exhausted",
                    "last_credit_check",
                    "last_used",
                    "key_file_id",
                )
                .eq("user_id", self.user_id)
                .execute()
            )
        except Exception:
            cache_rows = type("RowResult", (), {"data": []})()

        cache_map = {row["api_key"]: row for row in (cache_rows.data or [])}

        entries: List[KeyEntry] = []
        for row in (user_keys.data or []):
            api_key = row["api_key"]
            cache_row = cache_map.get(api_key)

            cached = cache_row.get("cached_credits") if cache_row else None
            credit_remaining = cached if cached is not None else row.get("credit_remaining") or 0

            entry = KeyEntry(
                id=row["id"],
                api_key=api_key,
                credit_remaining=max(0, int(credit_remaining)),
                credit_limit=row.get("credit_limit"),
                is_active=bool(row.get("is_active", True)),
                state=KeyState.ACTIVE if row.get("is_active", True) else KeyState.EXHAUSTED,
                last_used=_parse_ts(cache_row.get("last_used") if cache_row else row.get("last_used")),
                last_checked=_parse_ts(cache_row.get("last_credit_check") if cache_row else row.get("updated_at")),
                key_file_id=cache_row.get("key_file_id") if cache_row else None,
            )

            if cache_row and cache_row.get("is_exhausted"):
                entry.mark_exhausted()

            entries.append(entry)

        with self._lock:
            self._keys = entries
            self._by_api = {entry.api_key: entry for entry in entries}
            self._needs_sort = True
            self._last_reload = _utcnow()

        logger.info(
            "KEY_POOL_LOAD ✅ user=%s keys=%s total_credits=%s",
            self.user_id,
            len(entries),
            sum(e.credit_remaining for e in entries),
        )

        if progress_callback:
            progress_callback(90, f"Loaded {len(entries)} keys")

    def _reload_if_needed(self) -> None:
        with self._lock:
            if self._last_reload and (_utcnow() - self._last_reload) < timedelta(seconds=self.RELOAD_INTERVAL):
                return
        self.load()

    # ------------------------------------------------------------------
    # Key selection
    # ------------------------------------------------------------------
    def _ensure_sorted(self) -> None:
        """
        Sort keys by credits (highest first) to ensure keys with most credits are selected first.
        Secondary sort by last_checked (most recently checked first) for better cache utilization.
        """
        if not self._needs_sort:
            return
        # CRITICAL: Sort by credit_remaining DESCENDING (highest credits first)
        # Secondary sort by last_checked DESCENDING (most recently checked first)
        self._sorted = sorted(
            [k for k in self._keys if k.is_active and k.state == KeyState.ACTIVE],
            key=lambda k: (k.credit_remaining, k.last_checked or datetime.min),
            reverse=True,  # DESCENDING: highest credits first
        )
        self._needs_sort = False

    def _reserve(self, entry: KeyEntry) -> None:
        entry.in_use = True
        entry.reserved_by = threading.get_ident()
        entry.last_used = _utcnow()

    def _release(self, entry: KeyEntry) -> None:
        entry.in_use = False
        entry.reserved_by = None
        self._needs_sort = True

    def get_key(self, required_credits: int, excluded: Optional[Set[str]] = None) -> Optional[str]:
        """
        Get a key with sufficient credits, prioritizing keys with HIGHEST credits first.
        
        Args:
            required_credits: Minimum credits needed
            excluded: Set of API keys to exclude
            
        Returns:
            API key string or None if no suitable key found
        """
        excluded = excluded or set()
        reload_attempted = False

        while True:
            with self._lock:
                # CRITICAL: Ensure keys are sorted by credits DESCENDING (highest first)
                self._ensure_sorted()
                
                # Filter candidates: not excluded, not in use, and ACTIVE state
                candidates = [
                    entry
                    for entry in self._sorted
                    if entry.api_key not in excluded and not entry.in_use and entry.state == KeyState.ACTIVE
                ]
                
                if not candidates:
                    if reload_attempted:
                        logger.warning("GET_KEY - No usable keys (after reload)")
                        return None
                    reload_attempted = True
                    self.load()
                    continue

                # CRITICAL: Always pick the HIGHEST-credit key to maximize usage of strongest keys
                # candidates is already sorted by credit_remaining DESC via _ensure_sorted()
                entry = candidates[0]
                self._reserve(entry)

            try:
                if entry.credit_remaining >= required_credits + self.CREDIT_BUFFER and not entry.needs_refresh(self.REFRESH_TTL):
                    return entry.api_key

                if self._refresh_entry(entry, required_credits):
                    # Sau khi refresh, chỉ exhausted nếu credits = 0 hoặc không đủ cho request
                    if entry.credit_remaining == 0:
                        self.mark_exhausted(entry.api_key)
                        excluded.add(entry.api_key)
                    elif entry.credit_remaining >= required_credits + self.CREDIT_BUFFER:
                        return entry.api_key
                    else:
                        # Credits > 0 nhưng không đủ cho request này -> không exhausted, chỉ skip
                        excluded.add(entry.api_key)
                else:
                    # Refresh failed -> không exhausted, chỉ skip
                    excluded.add(entry.api_key)
            finally:
                with self._lock:
                    if entry.api_key in excluded or entry.state != KeyState.ACTIVE:
                        if entry.in_use:
                            self._release(entry)

    def get_any_active_key(self) -> Optional[str]:
        return self.get_key(self.CREDIT_BUFFER, set())

    # ------------------------------------------------------------------
    # Refresh & validation
    # ------------------------------------------------------------------
    def _refresh_entry(self, entry: KeyEntry, required: int) -> bool:
        if not entry.refresh_lock.acquire(blocking=False):
            # Another thread is refreshing; wait briefly for it to finish.
            while entry.refresh_lock.locked():
                time.sleep(0.05)
            return True

        try:
            data = self._get_real_credits_via_api(entry.api_key)
            if data is None:
                entry.last_checked = _utcnow()
                return False
            
            remaining, voice_info = data
            voice_at_limit = bool(voice_info and voice_info.get("is_at_limit"))

            with self._lock:
                entry.credit_remaining = max(0, int(remaining))
                entry.last_checked = _utcnow()
                if voice_at_limit:
                    entry.state = KeyState.VOICE_LIMIT_REACHED
                    entry.is_active = False
                elif entry.credit_remaining == 0:
                    # CHỈ đánh dấu exhausted khi credits thực sự = 0
                    entry.mark_exhausted()
                else:
                    entry.mark_active()
                self._needs_sort = True
                self._persist_entry(entry)
            return True
        finally:
            entry.refresh_lock.release()

    def validate_api_key_has_credits(self, api_key: str, needed_chars: int, force_check: bool = False) -> bool:
        entry = self._by_api.get(api_key)
        if not entry or not entry.is_active or entry.state not in {KeyState.ACTIVE, KeyState.RATE_LIMITED}:
            return False

        if entry.credit_remaining >= needed_chars + self.CREDIT_BUFFER and not force_check:
            return True

        if self._refresh_entry(entry, needed_chars):
            return entry.credit_remaining >= needed_chars + self.CREDIT_BUFFER

        return False

    # ------------------------------------------------------------------
    # State mutations
    # ------------------------------------------------------------------
    def deduct(self, api_key: str, used_credits: int) -> None:
        entry = self._by_api.get(api_key)
        if not entry:
                return
                
        with entry.refresh_lock:
            with self._lock:
                previous = entry.credit_remaining
                entry.credit_remaining = max(0, previous - max(0, used_credits))
                entry.last_used = _utcnow()
                if entry.credit_remaining == 0:
                    # CHỈ đánh dấu exhausted khi credits thực sự = 0
                    entry.mark_exhausted()
                self._needs_sort = True
                self._persist_entry(entry, used_credits=used_credits)
                if entry.in_use:
                    self._release(entry)

    def sync_external_remaining(self, api_key: str, new_remaining: int) -> None:
        """
        Update local cache after credits are deducted elsewhere (e.g., AccurateCreditTracker).
        Keeps in-memory state and Supabase cache aligned so exhausted keys are not reused.
        """
        entry = self._by_api.get(api_key)
        if not entry:
            return
        with entry.refresh_lock:
            with self._lock:
                entry.credit_remaining = max(0, int(new_remaining or 0))
                entry.last_used = _utcnow()
                if entry.credit_remaining == 0:
                    entry.mark_exhausted()
                else:
                    entry.mark_active()
                self._needs_sort = True
                self._persist_entry(entry)
                if entry.in_use:
                    self._release(entry)

    def mark_exhausted(self, api_key: str) -> None:
        entry = self._by_api.get(api_key)
        if not entry:
            return
        with self._lock:
            entry.mark_exhausted()
            self._persist_entry(entry)
            if entry.in_use:
                self._release(entry)
    
    def mark_invalid(self, api_key: str, reason: str = "401 unauthorized") -> None:
        entry = self._by_api.get(api_key)
        if not entry:
            return
        with self._lock:
            entry.mark_invalid(reason)
            self._persist_entry(entry)
            if entry.in_use:
                self._release(entry)
    
    def mark_voice_limit_reached(self, api_key: str) -> None:
        entry = self._by_api.get(api_key)
        if not entry:
            return
        with self._lock:
            entry.state = KeyState.VOICE_LIMIT_REACHED
            entry.is_active = False
            entry.credit_remaining = 0
            self._persist_entry(entry)
            if entry.in_use:
                self._release(entry)
    
    def mark_quota_exceeded(self, api_key: str) -> None:
        entry = self._by_api.get(api_key)
        if not entry:
            return
        with self._lock:
            entry.state = KeyState.QUOTA_EXCEEDED
            entry.is_active = False
            entry.credit_remaining = 0
            self._persist_entry(entry)
            if entry.in_use:
                self._release(entry)
    
    def mark_rate_limited(self, api_key: str) -> None:
        entry = self._by_api.get(api_key)
        if not entry:
            return
        with self._lock:
            entry.state = KeyState.RATE_LIMITED
            entry.is_active = True
            self._persist_entry(entry)
    
    def is_key_usable(self, api_key: str) -> bool:
        entry = self._by_api.get(api_key)
        if not entry:
            return False
        return entry.is_active and entry.state in {KeyState.ACTIVE, KeyState.RATE_LIMITED}
    
    def get_key_state(self, api_key: str) -> Optional[KeyState]:
        entry = self._by_api.get(api_key)
        return entry.state if entry else None

    def sync_state_changes_immediate(self) -> None:
        # Compatibility no-op (updates happen immediately).
        return

    def flush_to_db(self) -> None:
        # Compatibility no-op (updates happen immediately).
        return

    def pre_validate_keys(self, max_keys: int = 10, progress_callback=None) -> int:
        with self._lock:
            candidates = sorted(
                [k for k in self._keys if k.is_active],
                key=lambda k: k.credit_remaining,
                reverse=True,
            )[:max_keys]

        validated = 0
        for idx, entry in enumerate(candidates, start=1):
            if progress_callback:
                progress_callback(
                    50 + int(idx / max(1, len(candidates)) * 45),
                    f"Validating key {idx}/{len(candidates)}...",
                    entry.api_key[:10] + "...",
                )
            if self._refresh_entry(entry, self.CREDIT_BUFFER):
                if entry.state == KeyState.ACTIVE:
                    validated += 1
        return validated

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _persist_entry(self, entry: KeyEntry, used_credits: Optional[int] = None) -> None:
        payload = {
            "credit_remaining": entry.credit_remaining,
            "is_active": entry.is_active,
            "last_used": _iso(entry.last_used) or _iso(_utcnow()),
            "updated_at": _iso(_utcnow()),
        }
        try:
            self.supabase.table("user_api_keys")\
                .update(payload)\
                .eq("id", entry.id)\
                .eq("user_id", self.user_id)\
                .execute()
        except Exception as exc:
            logger.error("DB_UPDATE_KEY_ERROR - id=%s err=%s", entry.id, exc)

        cache_payload = {
            "user_id": self.user_id,
            "api_key": entry.api_key,
            "cached_credits": entry.credit_remaining,
            "is_exhausted": entry.state in {KeyState.EXHAUSTED, KeyState.QUOTA_EXCEEDED, KeyState.VOICE_LIMIT_REACHED},
            "last_credit_check": _iso(entry.last_checked) or _iso(_utcnow()),
            "last_used": _iso(entry.last_used) or _iso(_utcnow()),
        }
        if entry.key_file_id:
            cache_payload["key_file_id"] = entry.key_file_id
        try:
            self.supabase.table("active_api_keys").upsert(
                cache_payload,
                on_conflict="api_key",
            ).execute()
        except Exception as exc:
            logger.warning("ACTIVE_API_KEYS_UPSERT_FAILED - api_key=%s err=%s", entry.api_key[:10], exc)

    # ------------------------------------------------------------------
    # External API
    # ------------------------------------------------------------------
    def _get_real_credits_via_api(self, api_key: str) -> Optional[Tuple[int, dict]]:
        try:
            resp = requests.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": api_key},
                timeout=(3, self.API_TIMEOUT),
            )
        except RequestException as exc:
            logger.warning("REAL_CREDIT_API_EXCEPTION - %s err=%s", api_key[:10], exc)
            return None

        if resp.status_code != 200:
            logger.warning(
                "REAL_CREDIT_API_ERROR - key=%s code=%s body=%s",
                api_key[:10],
                resp.status_code,
                (resp.text or "").strip()[:120],
            )
            return None

        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            logger.warning("REAL_CREDIT_API_PARSE_ERROR - key=%s", api_key[:10])
            return None

        subscription = data.get("subscription") or {}
        used = int(subscription.get("character_count", 0) or 0)
        limit = int(subscription.get("character_limit", 0) or 0)
        remaining = max(0, limit - used)

        voice_slots_used = int(subscription.get("voice_slots_used", 0) or 0)
        voice_limit = int(subscription.get("voice_limit", 0) or 0)
        voice_info = {
            "voice_slots_used": voice_slots_used,
            "voice_limit": voice_limit,
            "is_at_limit": bool(voice_limit and voice_slots_used >= voice_limit),
        }

        logger.info(
            "REAL_CREDIT_API_SUCCESS - key=%s remaining=%s voice=%s",
            api_key[:10],
            remaining,
            voice_info,
        )
        return remaining, voice_info

    def _auto_clean_voices_when_at_limit(self, api_key: str) -> bool:
        # Auto cleaning removed for simplicity; keep compatibility return False.
        return False

