"""
JWT TTS Service - Text-to-Speech using JWT Authentication
Hoàn toàn độc lập với tts_service.py (API key mode)

Features:
- Sử dụng JWT Bearer token thay vì API key
- Auto refresh JWT khi hết hạn
- Account rotation khi rate limit
- Tích hợp proxy service
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests

# Import JWT Account Pool
from services.jwt_account_pool import JWTAccountPool, JWTAccount, AccountState


# ElevenLabs API endpoints
ELEVEN_BASE_US = "https://api.us.elevenlabs.io"
ELEVEN_BASE = "https://api.elevenlabs.io"

# 🔧 v2: Max retries for network errors (timeout, connection error)
MAX_NETWORK_RETRIES = 3


@dataclass
class JWTTTSResult:
    """Kết quả TTS"""
    success: bool
    audio_path: Optional[str] = None
    chars_used: int = 0
    error: Optional[str] = None
    account_email: Optional[str] = None


class JWTTTSService:
    """
    TTS Service sử dụng JWT Authentication
    
    Workflow:
    1. Load accounts từ file TXT (email|password)
    2. Pick account có JWT valid
    3. Gọi API với Bearer token
    4. Rotate account khi rate limit
    """
    
    def __init__(self, log_fn: Callable = None, proxy_fn: Callable = None, proxy_service=None):
        """
        Args:
            log_fn: Function để log messages
            proxy_fn: Function trả về proxy dict cho requests (optional)
            proxy_service: ProxyService instance để rotate proxy khi cần (optional)
        """
        self._log_fn = log_fn or print
        self._proxy_fn = proxy_fn  # () -> Optional[dict]
        self._proxy_service = proxy_service  # ProxyService instance
        
        # Account pool
        self.account_pool = JWTAccountPool(log_fn=self._log)
        
        # Session với retry
        self._session = self._create_session()
        
        # Stats
        self._total_chars = 0
        self._total_requests = 0
        self._failed_requests = 0
    
    def _log(self, msg: str):
        """Log message"""
        try:
            self._log_fn(msg)
        except:
            print(msg)
    
    def _create_session(self) -> requests.Session:
        """Tạo session với retry config"""
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter
        
        session = requests.Session()
        session.trust_env = False
        
        retries = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"])
        )
        
        adapter = HTTPAdapter(max_retries=retries, pool_maxsize=10)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        return session
    
    def _get_proxy(self) -> Optional[dict]:
        """Lấy proxy từ proxy_fn nếu có"""
        if self._proxy_fn:
            try:
                return self._proxy_fn()
            except:
                pass
        return None
    
    def set_proxy_service(self, proxy_service):
        """
        Set ProxyService instance để có thể rotate proxy khi cần.
        
        Args:
            proxy_service: ProxyService instance
        """
        self._proxy_service = proxy_service
    
    def _rotate_proxy_on_unusual_activity(self) -> bool:
        """
        Rotate proxy khi gặp 401 "Unusual activity detected".
        
        Returns:
            True nếu đã rotate thành công
        """
        if self._proxy_service:
            try:
                switched, new_url = self._proxy_service.report_unusual_activity()
                if switched:
                    self._log(f"🔄 Proxy rotated due to unusual activity → {new_url[:40] if new_url else 'None'}...")
                    return True
            except Exception as e:
                self._log(f"⚠️ Failed to rotate proxy: {e}")
        return False
    
    def _rotate_proxy_on_401(self, error_msg: str = "") -> bool:
        """
        🔧 v2 NEW: Rotate proxy khi gặp BẤT KỲ lỗi 401 nào.
        Sử dụng fast failover - switch ngay, không retry.
        
        Returns:
            True nếu đã rotate thành công
        """
        if self._proxy_service:
            try:
                switched, new_url = self._proxy_service.report_401_error(error_msg)
                if switched:
                    self._log(f"🔄 Proxy fast-failover on 401 → {new_url[:40] if new_url else 'None'}...")
                    return True
            except Exception as e:
                self._log(f"⚠️ Failed to rotate proxy: {e}")
        return False
    
    # ------------------------------------------------------------------
    # LOAD ACCOUNTS
    # ------------------------------------------------------------------
    def load_accounts_from_file(self, file_path: str) -> int:
        """
        Load accounts từ file TXT
        Format: email|password (mỗi dòng 1 account)
        
        Returns: số accounts loaded
        """
        return self.account_pool.load_from_file(file_path)
    
    def load_accounts_from_list(self, accounts: List[Tuple[str, str]]) -> int:
        """
        Load accounts từ list
        
        Args:
            accounts: List of (email, password) tuples
        """
        return self.account_pool.load_from_list(accounts)
    
    def load_accounts_from_d1(self, user_id: int) -> int:
        """
        Load accounts từ D1 database
        
        Args:
            user_id: User ID để lấy accounts
        
        Returns: số accounts loaded
        """
        try:
            from services.d1_jwt_account_pool import D1JWTAccountPool
            
            # Replace account_pool với D1-backed pool
            self.account_pool = D1JWTAccountPool(user_id=user_id, log_fn=self._log)
            count = self.account_pool.load_accounts()
            
            self._log(f"✅ Loaded {count} JWT accounts from D1 for user {user_id}")
            return count
            
        except Exception as e:
            self._log(f"❌ Failed to load accounts from D1: {e}")
            return 0
    
    # ------------------------------------------------------------------
    # TTS API CALLS
    # ------------------------------------------------------------------
    def _is_voice_slots_full_error(self, error: str) -> bool:
        """Check if error is voice slots full (3/3)"""
        if not error:
            return False
        error_lower = error.lower()
        return ('voice_limit_reached' in error_lower or 
                'maximum amount of custom voices' in error_lower or
                'custom voice limit' in error_lower)
    
    def _handle_voice_slots_full(self, account: JWTAccount, voice_id: str, retry_count: int = 0) -> bool:
        """
        Handle voice slots full error:
        1. Cleanup library voices (keep premade)
        2. Add target voice to library
        
        Args:
            account: JWT account
            voice_id: Voice ID to add
            retry_count: Current retry count
        
        Returns: True if handled successfully
        """
        from services.jwt_account_pool import JWTAPIClient
        
        max_retries = 2
        if retry_count >= max_retries:
            self._log(f"❌ Voice slots full - max retries ({max_retries}) reached")
            return False
        
        self._log(f"🧹 Voice slots full for {account.email} (retry {retry_count + 1}/{max_retries}) - cleaning up...")
        
        proxies = self._get_proxy()
        
        # Step 1: Cleanup ALL library voices (keep only premade)
        deleted = JWTAPIClient.cleanup_library_voices(account.jwt_token, proxies)
        self._log(f"🧹 Cleanup result: deleted {deleted} voices")
        
        # Step 2: Add target voice to library
        self._log(f"➕ Adding voice {voice_id[:8]}... to library...")
        success, error = JWTAPIClient.add_voice_to_library(account.jwt_token, voice_id, proxies)
        
        if success:
            self._log(f"✅ Voice added to library, retrying TTS...")
            return True
        else:
            self._log(f"❌ Failed to add voice: {error}")
            return False
    
    def generate_tts(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        model_id: str = "eleven_multilingual_v2",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        use_speaker_boost: bool = False,
        speed: float = 1.0,
    ) -> JWTTTSResult:
        """
        Generate TTS audio with automatic voice slots handling
        
        Args:
            text: Text to convert
            voice_id: ElevenLabs voice ID
            output_path: Path to save MP3
            model_id: Model ID (default: eleven_multilingual_v2)
            stability: Voice stability (0-1)
            similarity_boost: Similarity boost (0-1)
            style: Style exaggeration (0-1)
            use_speaker_boost: Enable speaker boost
            speed: Speech speed (0.5-2.0)
        
        Returns: JWTTTSResult
        """
        chars = len(text)
        self._total_requests += 1
        
        # Get account - CRITICAL: allow_concurrent=False để tránh 429 "Too many concurrent requests"
        # Free tier ElevenLabs chỉ cho phép 2 concurrent requests per account
        account = self.account_pool.get_account(allow_concurrent=False)
        if not account:
            self._failed_requests += 1
            return JWTTTSResult(
                success=False,
                error="No available account",
                chars_used=0
            )
        
        # Track retry for voice slots full
        voice_slots_retry = 0
        max_voice_slots_retries = 2
        
        while True:
            try:
                result = self._call_tts_api(
                    account=account,
                    text=text,
                    voice_id=voice_id,
                    output_path=output_path,
                    model_id=model_id,
                    stability=stability,
                    similarity_boost=similarity_boost,
                    style=style,
                    use_speaker_boost=use_speaker_boost,
                    speed=speed,
                )
                
                if result.success:
                    self._total_chars += chars
                    self.account_pool.release_account(
                        account.email, 
                        success=True, 
                        chars_used=chars
                    )
                    result.account_email = account.email
                    return result
                
                # Check for voice slots full error
                if self._is_voice_slots_full_error(result.error):
                    error_type = self._parse_error_type(result.error)
                    self._log(f"🔍 Error type: {error_type} (from: {result.error[:80]}...)")
                    
                    if voice_slots_retry < max_voice_slots_retries:
                        # Try to handle voice slots full
                        if self._handle_voice_slots_full(account, voice_id, voice_slots_retry):
                            voice_slots_retry += 1
                            # Report success to release account temporarily
                            self.account_pool.release_account(account.email, success=True, chars_used=0)
                            # Get same or new account for retry
                            account = self.account_pool.get_account(allow_concurrent=False)
                            if not account:
                                return JWTTTSResult(success=False, error="No available account after voice cleanup")
                            continue  # Retry TTS
                        else:
                            # Cleanup failed, try next account
                            voice_slots_retry += 1
                            self.account_pool.release_account(account.email, success=False, error_type="voice_slots_full", error_msg=result.error)
                            account = self.account_pool.get_account(allow_concurrent=False)
                            if not account:
                                return JWTTTSResult(success=False, error="No available account after voice cleanup failed")
                            continue
                    else:
                        # 🔧 FIX: Đã hết retry cho account này, switch sang account khác và reset retry counter
                        self._log(f"⚠️ Voice slots retry exhausted for {account.email}, switching account...")
                        self.account_pool.release_account(account.email, success=False, error_type="voice_slots_full", error_msg=result.error)
                        account = self.account_pool.get_account(allow_concurrent=False)
                        if not account:
                            return JWTTTSResult(success=False, error="No available account after max voice slots retries")
                        voice_slots_retry = 0  # Reset retry counter for new account
                        continue
                
                # Other errors - release and return
                self._failed_requests += 1
                error_type = self._parse_error_type(result.error)
                self.account_pool.release_account(
                    account.email,
                    success=False,
                    error_type=error_type,
                    error_msg=result.error
                )
                result.account_email = account.email
                return result
                
            except Exception as e:
                self._failed_requests += 1
                self.account_pool.release_account(
                    account.email,
                    success=False,
                    error_type="unknown",
                    error_msg=str(e)
                )
                return JWTTTSResult(
                    success=False,
                    error=str(e),
                    account_email=account.email
                )
    
    def _call_tts_api(
        self,
        account: JWTAccount,
        text: str,
        voice_id: str,
        output_path: str,
        model_id: str,
        stability: float,
        similarity_boost: float,
        style: float,
        use_speaker_boost: bool,
        speed: float,
        _retry_count: int = 0,  # 🔧 v2: Track retry count
    ) -> JWTTTSResult:
        """
        Gọi ElevenLabs TTS API với JWT token
        
        Supports:
        - eleven_v3: POST /v1/text-to-dialogue/stream (voice_id in body)
        - Other models: POST /v1/text-to-speech/{voice_id} (voice_id in URL)
        
        🔧 v2: Auto-retry với proxy mới khi gặp network error (max 3 retries)
        """
        # Determine endpoint and payload based on model
        is_v3 = model_id == "eleven_v3"
        
        if is_v3:
            # V3 uses text-to-dialogue endpoint
            url = f"{ELEVEN_BASE}/v1/text-to-dialogue/stream"
            payload = {
                "inputs": [
                    {
                        "text": text,
                        "voice_id": voice_id
                    }
                ],
                "model_id": model_id,
                "settings": {
                    "stability": stability,
                    "similarity_boost": similarity_boost,
                }
            }
            # V3 doesn't support style/speaker_boost in same way
            if speed != 1.0:
                payload["settings"]["speed"] = speed
        else:
            # V2.5 and other models use text-to-speech endpoint
            url = f"{ELEVEN_BASE}/v1/text-to-speech/{voice_id}"
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": stability,
                    "similarity_boost": similarity_boost,
                    "style": style,
                    "use_speaker_boost": use_speaker_boost,
                }
            }
            if speed != 1.0:
                payload["speed"] = speed
        
        headers = {
            "Authorization": f"Bearer {account.jwt_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": "https://elevenlabs.io",
            "Referer": "https://elevenlabs.io/",
        }
        
        # Get proxy
        proxies = self._get_proxy()
        
        try:
            endpoint_type = "dialogue" if is_v3 else "tts"
            self._log(f"🎤 TTS ({endpoint_type}): {len(text)} chars → {voice_id[:8]}... (account: {account.email})")
            
            resp = self._session.post(
                url,
                headers=headers,
                json=payload,
                proxies=proxies,
                timeout=60
            )
            
            if resp.status_code == 200:
                # Save audio
                with open(output_path, 'wb') as f:
                    f.write(resp.content)
                
                size_kb = len(resp.content) / 1024
                self._log(f"✅ TTS OK: {size_kb:.1f}KB → {os.path.basename(output_path)}")
                
                # Report success to proxy service
                if self._proxy_service:
                    try:
                        self._proxy_service.report_success()
                    except:
                        pass
                
                return JWTTTSResult(
                    success=True,
                    audio_path=output_path,
                    chars_used=len(text)
                )
            else:
                error_msg = f"HTTP {resp.status_code}"
                try:
                    error_data = resp.json()
                    if isinstance(error_data, dict):
                        detail = error_data.get('detail', error_data)
                        if isinstance(detail, dict):
                            error_msg = f"{resp.status_code}: {detail.get('message', detail)}"
                        else:
                            error_msg = f"{resp.status_code}: {detail}"
                except:
                    error_msg = f"{resp.status_code}: {resp.text[:200]}"
                
                self._log(f"❌ TTS Error: {error_msg}")
                
                # 🔧 v2: Fast failover cho TẤT CẢ lỗi 401 (không chỉ unusual activity)
                if resp.status_code == 401:
                    self._log(f"🚨 401 Error detected - fast failover...")
                    self._rotate_proxy_on_401(error_msg)
                
                return JWTTTSResult(success=False, error=error_msg)
                
        except requests.exceptions.Timeout:
            # 🔧 v2: Rotate proxy và retry on timeout
            if _retry_count < MAX_NETWORK_RETRIES:
                self._rotate_proxy_on_network_error("timeout")
                self._log(f"⏱️ Timeout - retry {_retry_count + 1}/{MAX_NETWORK_RETRIES} với proxy mới...")
                import time
                time.sleep(1)  # Small delay before retry
                return self._call_tts_api(
                    account=account, text=text, voice_id=voice_id, output_path=output_path,
                    model_id=model_id, stability=stability, similarity_boost=similarity_boost,
                    style=style, use_speaker_boost=use_speaker_boost, speed=speed,
                    _retry_count=_retry_count + 1
                )
            return JWTTTSResult(success=False, error="timeout")
        except requests.exceptions.ConnectionError as e:
            # 🔧 v2: Rotate proxy và retry on connection error
            if _retry_count < MAX_NETWORK_RETRIES:
                self._rotate_proxy_on_network_error(f"connection_error: {e}")
                self._log(f"🔌 Connection error - retry {_retry_count + 1}/{MAX_NETWORK_RETRIES} với proxy mới...")
                import time
                time.sleep(1)  # Small delay before retry
                return self._call_tts_api(
                    account=account, text=text, voice_id=voice_id, output_path=output_path,
                    model_id=model_id, stability=stability, similarity_boost=similarity_boost,
                    style=style, use_speaker_boost=use_speaker_boost, speed=speed,
                    _retry_count=_retry_count + 1
                )
            return JWTTTSResult(success=False, error=f"network_error: {e}")
        except Exception as e:
            return JWTTTSResult(success=False, error=str(e))
    
    def _rotate_proxy_on_network_error(self, error_msg: str = "") -> bool:
        """
        🔧 v2 NEW: Rotate proxy khi gặp network error (timeout, connection error).
        
        Returns:
            True nếu đã rotate thành công
        """
        if self._proxy_service:
            try:
                # Report failure để switch sang proxy khác
                switched = self._proxy_service.report_failure(is_rate_limited=False)
                if switched:
                    new_proxy = self._proxy_service.get_current_proxy()
                    self._log(f"🔄 Proxy rotated on network error ({error_msg[:30]}...) → {new_proxy[:40] if new_proxy else 'None'}...")
                    return True
                else:
                    # Single proxy - force refresh
                    new_proxy = self._proxy_service.force_refresh(wait_for_cooldown=False)
                    if new_proxy:
                        self._log(f"🔄 Proxy refreshed on network error → {new_proxy[:40]}...")
                        return True
            except Exception as e:
                self._log(f"⚠️ Failed to rotate proxy on network error: {e}")
        return False
    
    def _parse_error_type(self, error: str) -> str:
        """Parse error message to error type"""
        if not error:
            return "unknown"
        
        error_lower = error.lower()
        
        # 🔧 Check voice slots full FIRST (400 error with specific message)
        if "maximum amount of custom voices" in error_lower or "custom voice limit" in error_lower:
            return "voice_slots_full"
        
        if "401" in error or "unauthorized" in error_lower:
            if "unusual activity" in error_lower:
                return "unusual_activity"
            return "auth_error"
        elif "402" in error or "payment" in error_lower or "quota" in error_lower or "insufficient" in error_lower:
            # 🔧 Handle "insufficient credits" error → mark as EXHAUSTED
            return "quota_exceeded"
        elif "429" in error or "rate" in error_lower or "concurrent" in error_lower:
            return "rate_limit"
        elif "timeout" in error_lower:
            return "timeout"
        elif "network" in error_lower or "connection" in error_lower:
            return "network_error"
        else:
            return "unknown"
    
    # ------------------------------------------------------------------
    # BATCH PROCESSING
    # ------------------------------------------------------------------
    def generate_batch(
        self,
        items: List[Tuple[str, str]],  # List of (text, output_path)
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        progress_callback: Callable = None,
        stop_flag: Callable = None,
        **voice_settings
    ) -> List[JWTTTSResult]:
        """
        Generate TTS cho nhiều items
        
        Args:
            items: List of (text, output_path) tuples
            voice_id: Voice ID
            model_id: Model ID
            progress_callback: (current, total, message) -> None
            stop_flag: () -> bool, return True to stop
            **voice_settings: stability, similarity_boost, etc.
        
        Returns: List of results
        """
        results = []
        total = len(items)
        
        for idx, (text, output_path) in enumerate(items):
            # Check stop
            if stop_flag and stop_flag():
                self._log("⏹️ Batch stopped by user")
                break
            
            # Progress
            if progress_callback:
                progress_callback(idx, total, f"Processing {idx+1}/{total}...")
            
            # Generate
            result = self.generate_tts(
                text=text,
                voice_id=voice_id,
                output_path=output_path,
                model_id=model_id,
                **voice_settings
            )
            results.append(result)
            
            # Small delay between requests
            if idx < total - 1:
                time.sleep(0.5)
        
        # Final progress
        if progress_callback:
            success_count = sum(1 for r in results if r.success)
            progress_callback(total, total, f"Done: {success_count}/{total} success")
        
        return results
    
    # ------------------------------------------------------------------
    # STATS
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict:
        """Lấy thống kê"""
        account_stats = self.account_pool.get_stats()
        return {
            **account_stats,
            'total_chars_generated': self._total_chars,
            'total_requests': self._total_requests,
            'failed_requests': self._failed_requests,
            'success_rate': (self._total_requests - self._failed_requests) / max(1, self._total_requests) * 100
        }


# ============== DEMO ==============
if __name__ == "__main__":
    print("="*60)
    print("🎤 JWT TTS Service Demo")
    print("="*60)
    
    service = JWTTTSService()
    
    # Load accounts
    accounts_file = "jwt_accounts.txt"
    if os.path.exists(accounts_file):
        count = service.load_accounts_from_file(accounts_file)
        print(f"✅ Loaded {count} accounts")
    else:
        # Demo với 1 account
        email = input("Email: ").strip()
        password = input("Password: ").strip()
        if email and password:
            service.load_accounts_from_list([(email, password)])
    
    # Test TTS
    test_text = "Xin chào, đây là bài test text to speech bằng JWT authentication."
    voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel
    output_path = "test_jwt_tts.mp3"
    
    result = service.generate_tts(
        text=test_text,
        voice_id=voice_id,
        output_path=output_path
    )
    
    if result.success:
        print(f"\n✅ Success! Audio saved to: {result.audio_path}")
        print(f"   Chars used: {result.chars_used}")
        print(f"   Account: {result.account_email}")
    else:
        print(f"\n❌ Failed: {result.error}")
    
    # Stats
    print(f"\n📊 Stats: {service.get_stats()}")
