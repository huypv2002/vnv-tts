"""
Multiple Voice Tab System for Qt MainWindow (11Labs0811.py).
This is a NEW file - does not modify any existing code.

Creates a QTabWidget with:
- Tab 1: "Text to Speech" - original single voice interface (existing widgets)
- Tab 2: "Multiple Voice" - clone with multiple voices using text-to-dialogue API
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, QThread, Signal, QThreadPool, QRunnable, QObject, QMutex, QMutexLocker
from PySide6.QtWidgets import QHeaderView
from typing import List, Optional, Callable, Dict
import os
import requests
import random
import time
import sys
import threading
from datetime import datetime

# Import find_ffmpeg from main app
try:
    # Try to import from parent module
    import sys
    import os
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    
    # Import find_ffmpeg from 11Labs0811.py
    import importlib.util
    spec = importlib.util.spec_from_file_location("main_app", os.path.join(parent_dir, "11Labs0811.py"))
    if spec and spec.loader:
        main_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main_app)
        find_ffmpeg = main_app.find_ffmpeg
    else:
        # Fallback: define simple find_ffmpeg
        def find_ffmpeg():
            import shutil
            return shutil.which("ffmpeg")
except Exception as e:
    print(f"⚠️ Could not import find_ffmpeg: {e}")
    # Fallback: define simple find_ffmpeg
    def find_ffmpeg():
        import shutil
        return shutil.which("ffmpeg")

# Import logging functions from main app
try:
    # Get app directory
    if getattr(sys, 'frozen', False):
        APP_DIR = os.path.dirname(sys.executable)
    else:
        APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    LOG_DIR = os.path.join(APP_DIR, "logtts")
    
    # Ensure log directory exists
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
except:
    LOG_DIR = None

def _log_to_file(text: str):
    """Log to file in logtts directory."""
    if not LOG_DIR:
        return
    
    try:
        # Get today's log file
        today = datetime.now().strftime("%Y%m%d")
        session_num = 1
        
        # Find existing log file for today
        while True:
            log_name = f"tts_{today}_{session_num}.log"
            log_path = os.path.join(LOG_DIR, log_name)
            
            # If file exists and is recent (modified in last hour), use it
            if os.path.exists(log_path):
                mtime = os.path.getmtime(log_path)
                if time.time() - mtime < 3600:  # 1 hour
                    break
                session_num += 1
            else:
                break
        
        # Write log
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {text}\n")
    except Exception as e:
        print(f"Log error: {e}")


# ========== VOICE CLEANUP WORKER (Background) ==========
class VoiceCleanupWorker(QThread):
    """
    Background worker để cleanup voice slots của key bị voice_limit.
    Chạy độc lập, không block quá trình gen audio.
    
    Flow:
    1. List voices của key
    2. Delete custom voices (không phải premade)
    3. Restore key về READY state trong DB
    """
    
    cleanup_done = Signal(str, bool, str)  # api_key, success, message
    
    def __init__(self, api_key: str, keys_pool, proxy_url: str = None):
        super().__init__()
        self.api_key = api_key
        self.keys_pool = keys_pool
        self.proxy_url = proxy_url
        self._stop = False
    
    def stop(self):
        self._stop = True
    
    def _log(self, msg: str):
        """Log message."""
        _log_to_file(f"[VoiceCleanup] {msg}")
        print(f"[VoiceCleanup] {msg}")
    
    def run(self):
        """Run cleanup in background."""
        self._log(f"🧹 Starting cleanup for key {self.api_key[:10]}...")
        
        try:
            # Step 1: List voices
            voices = self._list_voices()
            if self._stop:
                return
            
            if not voices:
                self._log(f"⚠️ No voices found for key {self.api_key[:10]}")
                self._restore_key()
                return
            
            # Step 2: Filter custom voices (not premade/default)
            custom_voices = []
            for v in voices:
                category = v.get("category", "").lower()
                if category not in ("premade", "default"):
                    custom_voices.append(v)
            
            self._log(f"📋 Found {len(voices)} voices, {len(custom_voices)} custom")
            
            if not custom_voices:
                self._log(f"⚠️ No custom voices to delete for key {self.api_key[:10]}")
                self._restore_key()
                return
            
            # Step 3: Delete custom voices
            deleted = 0
            for voice in custom_voices:
                if self._stop:
                    break
                
                voice_id = voice.get("voice_id", "")
                voice_name = voice.get("name", "Unknown")
                
                if self._delete_voice(voice_id):
                    deleted += 1
                    self._log(f"✅ Deleted voice: {voice_name} ({voice_id[:8]}...)")
                else:
                    self._log(f"❌ Failed to delete: {voice_name}")
                
                time.sleep(0.3)  # Rate limit
            
            self._log(f"🧹 Deleted {deleted}/{len(custom_voices)} custom voices for key {self.api_key[:10]}")
            
            # Step 4: Restore key to READY
            if deleted > 0:
                self._restore_key()
            
            self.cleanup_done.emit(self.api_key, True, f"Deleted {deleted} voices")
            
        except Exception as e:
            self._log(f"❌ Cleanup error: {e}")
            self.cleanup_done.emit(self.api_key, False, str(e))
    
    def _list_voices(self) -> list:
        """List all voices for this API key."""
        try:
            url = "https://api.elevenlabs.io/v1/voices"
            headers = {"xi-api-key": self.api_key}
            
            proxies = None
            if self.proxy_url:
                proxies = {"http": self.proxy_url, "https": self.proxy_url}
            
            response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                return data.get("voices", [])
            else:
                self._log(f"⚠️ List voices failed: {response.status_code}")
                return []
        except Exception as e:
            self._log(f"❌ List voices error: {e}")
            return []
    
    def _delete_voice(self, voice_id: str) -> bool:
        """Delete a voice by ID."""
        try:
            url = f"https://api.elevenlabs.io/v1/voices/{voice_id}"
            headers = {"xi-api-key": self.api_key}
            
            proxies = None
            if self.proxy_url:
                proxies = {"http": self.proxy_url, "https": self.proxy_url}
            
            response = requests.delete(url, headers=headers, proxies=proxies, timeout=30)
            
            return response.status_code in (200, 204)
        except Exception as e:
            self._log(f"❌ Delete voice error: {e}")
            return False
    
    def _restore_key(self):
        """Restore key to READY state in DB."""
        if self.keys_pool and hasattr(self.keys_pool, 'restore_key_after_voice_cleanup'):
            success = self.keys_pool.restore_key_after_voice_cleanup(self.api_key)
            if success:
                self._log(f"✅ Key {self.api_key[:10]}... restored to READY")
            else:
                self._log(f"⚠️ Key {self.api_key[:10]}... restore failed or skipped")
        else:
            self._log(f"⚠️ keys_pool doesn't support restore_key_after_voice_cleanup")


# ========== SIGNALS FOR BATCH WORKER ==========
class BatchWorkerSignals(QObject):
    """Signals for DialogueBatchWorker to communicate with main thread."""
    batch_done = Signal(int, bytes, bool, str)  # batch_idx, audio_bytes, success, error_msg
    batch_progress = Signal(int, str)  # batch_idx, status
    key_rotated = Signal(str)  # new_key_prefix
    voice_limit_hit = Signal(str, str)  # api_key, proxy_url - để main thread spawn cleanup worker


# ========== BATCH WORKER (QRunnable) ==========
class DialogueBatchWorker(QRunnable):
    """
    Worker to process a single batch in parallel.
    Uses QRunnable for QThreadPool compatibility.
    """
    
    def __init__(self, batch_idx: int, batch: list, keys_pool, proxy_getter: Callable,
                 proxy_service_db, signals: BatchWorkerSignals, stop_flag_ref: Callable,
                 key_lock: threading.Lock, stability: float = 0.5):
        super().__init__()
        self.batch_idx = batch_idx
        self.batch = batch  # List of {"text": str, "voice_id": str}
        self.keys_pool = keys_pool
        self.proxy_getter = proxy_getter
        self.proxy_service_db = proxy_service_db
        self.signals = signals
        self._stop_flag_ref = stop_flag_ref
        self._key_lock = key_lock  # Shared lock for key operations
        self.stability = stability  # 0, 0.5, or 1.0
        
        # Retry counters (per batch)
        self._key_retry_count = 0
        self._key_rotate_count = 0
        self._400_retry_count = 0
        
        # Acquired key (like TTS LineWorker)
        self._acquired_key = None
        
        self.setAutoDelete(False)
    
    def _log(self, msg: str):
        """Log message to file."""
        _log_to_file(f"[Batch {self.batch_idx + 1}] {msg}")
        print(f"[Batch {self.batch_idx + 1}] {msg}")
    
    def _should_stop(self) -> bool:
        """Check if stop was requested."""
        if self._stop_flag_ref and callable(self._stop_flag_ref):
            return self._stop_flag_ref()
        return False
    
    def _get_proxy(self) -> str:
        """Get proxy URL from proxy_getter."""
        if self.proxy_getter:
            try:
                return self.proxy_getter()
            except:
                pass
        return None
    
    def _get_api_key(self) -> str:
        """Acquire a unique API key for this batch (thread-safe)."""
        with self._key_lock:
            if self.keys_pool:
                # Calculate chars needed for this batch
                batch_chars = sum(len(inp.get("text", "")) for inp in self.batch)
                
                # Use acquire_key if available (like TTS LineWorker)
                if hasattr(self.keys_pool, 'acquire_key'):
                    batch_id = f"dialogue_batch_{self.batch_idx}"
                    key = self.keys_pool.acquire_key(
                        required_chars=batch_chars,
                        max_attempts=50,
                        timeout=30.0,
                        line_id=batch_id
                    )
                    if key:
                        self._log(f"🔑 Acquired key: {key[:8]}... for batch {self.batch_idx + 1}")
                    return key
                else:
                    # Fallback: use cur() for old KeyPool
                    return self.keys_pool.cur()
        return None
    
    def _rotate_key(self) -> str:
        """Rotate to next API key using acquire_key (thread-safe)."""
        with self._key_lock:
            if self.keys_pool:
                batch_chars = sum(len(inp.get("text", "")) for inp in self.batch)
                batch_id = f"dialogue_batch_{self.batch_idx}"
                
                # Use acquire_key if available (like TTS)
                if hasattr(self.keys_pool, 'acquire_key'):
                    new_key = self.keys_pool.acquire_key(
                        required_chars=batch_chars,
                        max_attempts=50,
                        timeout=30.0,
                        line_id=batch_id
                    )
                    if new_key:
                        self._log(f"🔄 Rotated to key: {new_key[:8]}...")
                        self.signals.key_rotated.emit(new_key[:8] + "...")
                    return new_key
                else:
                    # Fallback: use rotate() for old KeyPool
                    self.keys_pool.rotate()
                    new_key = self.keys_pool.cur()
                    if new_key:
                        self._log(f"🔄 Rotated to key: {new_key[:8]}...")
                        self.signals.key_rotated.emit(new_key[:8] + "...")
                    return new_key
        return None
    
    def _release_key(self, api_key: str, chars_used: int, success: bool, error_code: str = None):
        """Release key with result (thread-safe)."""
        with self._key_lock:
            if self.keys_pool and hasattr(self.keys_pool, 'release_key'):
                batch_id = f"dialogue_batch_{self.batch_idx}"
                self.keys_pool.release_key(
                    api_key, 
                    chars_used, 
                    success=success, 
                    error_code=error_code,
                    line_id=batch_id
                )
                self._log(f"🔓 Released key: {api_key[:8]}... (success={success})")
    
    def _mark_401(self, api_key: str):
        """Mark key as 401 (thread-safe)."""
        with self._key_lock:
            if self.keys_pool:
                batch_id = f"dialogue_batch_{self.batch_idx}"
                if hasattr(self.keys_pool, 'release_key'):
                    self.keys_pool.release_key(
                        api_key, 
                        0, 
                        success=False, 
                        error_code="401",
                        line_id=batch_id
                    )
                else:
                    self.keys_pool.mark_401(api_key)
    
    def _call_api(self, inputs: list, api_key: str, proxy_url: str) -> tuple:
        """
        Call text-to-dialogue API.
        Returns: (success, audio_bytes or error_message, should_rotate_key, error_code)
        """
        url = "https://api.elevenlabs.io/v1/text-to-dialogue"
        
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json"
        }
        
        payload = {
            "inputs": inputs,
            "model_id": "eleven_v3",
            "settings": {"stability": self.stability},
            "apply_text_normalization": "auto"
        }
        
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
            self._log(f"🌐 Using proxy: {proxy_url[:50]}...")
        else:
            self._log(f"⚠️ NO PROXY - calling API directly")
        
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                proxies=proxies,
                timeout=180
            )
            
            if response.status_code == 200:
                return True, response.content, False, None
            
            error_msg = f"Lỗi {response.status_code}"
            should_rotate = False
            error_code = str(response.status_code)
            
            self._log(f"❌ API Error {response.status_code}: {response.text[:500]}")
            
            try:
                detail = response.json().get("detail", {})
                if isinstance(detail, dict):
                    error_status = detail.get("status", "")
                    error_msg = detail.get("message", error_msg)[:100]
                    if error_status in ["quota_exceeded", "invalid_api_key"] or response.status_code == 401:
                        should_rotate = True
                elif isinstance(detail, str):
                    error_msg = detail[:100]
            except:
                pass
            
            return False, error_msg, should_rotate, error_code
            
        except requests.exceptions.Timeout:
            return False, "Request timeout", False, "timeout"
        except requests.exceptions.ConnectionError as e:
            error_msg = str(e).lower()
            if "connection aborted" in error_msg or "connection reset" in error_msg:
                return False, "Connection reset", False, "connection_reset"
            return False, f"Connection error: {str(e)[:50]}", False, "connection_error"
        except Exception as e:
            return False, str(e)[:100], False, "unknown"
    
    def run(self):
        """Process this batch with full retry logic (like TTS LineWorker)."""
        batch_chars = sum(len(inp.get("text", "")) for inp in self.batch)
        self._log(f"📦 Processing: {len(self.batch)} lines, {batch_chars:,} chars")
        self.signals.batch_progress.emit(self.batch_idx, "Processing...")
        
        # Acquire key ONCE at start (like TTS LineWorker)
        self._acquired_key = self._get_api_key()
        if not self._acquired_key:
            self._log("❌ No API key available at start")
            self.signals.batch_done.emit(self.batch_idx, b"", False, "No API key")
            return
        
        max_retries = 120
        
        try:
            for retry in range(max_retries):
                if self._should_stop():
                    self._log("⏹️ Stopped by user")
                    self.signals.batch_done.emit(self.batch_idx, b"", False, "Stopped")
                    return
                
                # Use the acquired key (don't acquire new one each retry)
                if not self._acquired_key:
                    self._log("❌ No API key available")
                    self.signals.batch_done.emit(self.batch_idx, b"", False, "No API key")
                    return
                
                proxy_url = self._get_proxy()
                
                try:
                    success, result, should_rotate, error_code = self._call_api(self.batch, self._acquired_key, proxy_url)
                    
                    if success:
                        audio_bytes = result
                        audio_size = len(audio_bytes) / 1024
                        self._log(f"✅ Success: {audio_size:.1f} KB")
                        
                        # Release key with success
                        self._release_key(self._acquired_key, batch_chars, success=True, error_code=None)
                        self._acquired_key = None
                        
                        self.signals.batch_done.emit(self.batch_idx, audio_bytes, True, "")
                        return
                    
                    # Handle errors
                    error_msg = str(result).lower()
                    
                    # === 429/403/503: Rate Limit ===
                    if error_code in ["429", "403", "503"]:
                        self._log(f"⚠️ Rate limit {error_code}")
                        self.signals.batch_progress.emit(self.batch_idx, f"Rate limit...")
                        
                        if self.proxy_service_db:
                            try:
                                switched = self.proxy_service_db.report_rate_limit()
                                if switched:
                                    self._log(f"✅ Switched proxy")
                                    time.sleep(1)
                                    continue
                                else:
                                    time.sleep(2)
                                    continue
                            except:
                                pass
                        
                        if retry < 30:
                            time.sleep(2)
                            continue
                        else:
                            # Release old key before rotating
                            self._release_key(self._acquired_key, 0, success=False, error_code=error_code)
                            self._acquired_key = self._rotate_key()
                            if not self._acquired_key:
                                self.signals.batch_done.emit(self.batch_idx, b"", False, "No keys after rate limit")
                                return
                            time.sleep(1)
                            continue
                    
                    # === 401/402/422: Invalid Key ===
                    elif error_code in ["401", "402", "422"]:
                        self._key_retry_count += 1
                        
                        if self._key_retry_count <= 1:
                            self._log(f"🔄 Key {error_code} retry {self._key_retry_count}/1...")
                            time.sleep(0.5)
                            continue
                        else:
                            self._key_retry_count = 0
                            self._key_rotate_count += 1
                            
                            if self._key_rotate_count >= 400:
                                self._release_key(self._acquired_key, 0, success=False, error_code=error_code)
                                self._acquired_key = None
                                self.signals.batch_done.emit(self.batch_idx, b"", False, "Rotated 400 keys")
                                return
                            
                            # Mark 401 and release old key before rotating
                            self._mark_401(self._acquired_key)
                            self._log(f"🔄 {error_code} - Rotating key ({self._key_rotate_count}/400)...")
                            self.signals.batch_progress.emit(self.batch_idx, f"Đổi key...")
                            
                            self._acquired_key = self._rotate_key()
                            if not self._acquired_key:
                                self.signals.batch_done.emit(self.batch_idx, b"", False, "No keys")
                                return
                            
                            time.sleep(1)
                            continue
                    
                    # === 400: Bad Request ===
                    elif error_code == "400":
                        self._log(f"⚠️ 400 ERROR: {result[:200]}")
                        
                        if "voice_limit" in error_msg or "maximum amount of custom voices" in error_msg:
                            # Save old key for cleanup
                            old_key = self._acquired_key
                            old_proxy = proxy_url
                            
                            # Mark key as voice_limit (will be DEAD in DB)
                            if self.keys_pool and hasattr(self.keys_pool, 'mark_voice_limit_reached'):
                                self.keys_pool.mark_voice_limit_reached(old_key)
                            
                            self._log(f"🔄 Voice limit reached for {old_key[:10]}... - rotating key")
                            
                            # Rotate to new key IMMEDIATELY (không chờ cleanup)
                            self._acquired_key = self._rotate_key()
                            if not self._acquired_key:
                                self.signals.batch_done.emit(self.batch_idx, b"", False, "No keys")
                                return
                            
                            # Emit signal để main thread spawn cleanup worker (background)
                            self.signals.voice_limit_hit.emit(old_key, old_proxy or "")
                            
                            time.sleep(1)
                            continue
                        
                        self._400_retry_count += 1
                        
                        if self._400_retry_count < 3:
                            time.sleep(1)
                            continue
                        
                        self._400_retry_count = 0
                        self._key_rotate_count += 1
                        
                        if self._key_rotate_count >= 500:
                            self._release_key(self._acquired_key, 0, success=False, error_code=error_code)
                            self._acquired_key = None
                            self.signals.batch_done.emit(self.batch_idx, b"", False, "Rotated 500 keys (400)")
                            return
                        
                        self._release_key(self._acquired_key, 0, success=False, error_code=error_code)
                        self._acquired_key = self._rotate_key()
                        if not self._acquired_key:
                            self.signals.batch_done.emit(self.batch_idx, b"", False, "No keys")
                            return
                        time.sleep(1)
                        continue
                    
                    # === Connection errors ===
                    elif error_code in ["connection_reset", "connection_error"]:
                        self._log(f"⚠️ Connection error - retrying...")
                        if self.proxy_service_db:
                            try:
                                self.proxy_service_db.get_current_proxy()
                            except:
                                pass
                        time.sleep(1)
                        continue
                    
                    # === Other errors ===
                    else:
                        if retry < max_retries - 1:
                            time.sleep(1)
                            continue
                        else:
                            self._release_key(self._acquired_key, 0, success=False, error_code=error_code)
                            self._acquired_key = None
                            self.signals.batch_done.emit(self.batch_idx, b"", False, f"Failed: {result}")
                            return
                
                except Exception as e:
                    self._log(f"❌ Exception: {e}")
                    if retry < max_retries - 1:
                        time.sleep(1)
                        continue
                    else:
                        self.signals.batch_done.emit(self.batch_idx, b"", False, f"Exception: {str(e)[:100]}")
                        return
            
            self.signals.batch_done.emit(self.batch_idx, b"", False, "Max retries exceeded")
            
        finally:
            # Always release key in finally block (like TTS LineWorker)
            if self._acquired_key:
                try:
                    self._release_key(self._acquired_key, 0, success=False, error_code=None)
                    self._acquired_key = None
                except:
                    pass


class DialogueTTSWorker(QThread):
    """
    Worker thread for text-to-dialogue TTS generation.
    Uses QThreadPool for PARALLEL batch processing.
    """
    
    progress = Signal(int, int, str)  # current, total, status
    line_done = Signal(int, str, float)  # line_index, output_file, duration
    batch_lines_done = Signal(int, int, bool)  # start_line_idx, end_line_idx, success - cập nhật UI từng batch
    finished = Signal(bool, str, str)  # success, message, output_file
    key_rotated = Signal(str)  # new_key_prefix (for logging)
    
    def __init__(self, keys_pool, inputs: list, output_dir: str, basename: str, 
                 proxy_getter: Callable = None, proxy_service_db = None, 
                 max_chars_per_batch: int = 1000, max_workers: int = 5,
                 stability: float = 0.5):
        super().__init__()
        self.keys_pool = keys_pool
        self.inputs = inputs
        self.output_dir = output_dir
        self.basename = basename
        self.proxy_getter = proxy_getter
        self.proxy_service_db = proxy_service_db
        self.max_chars_per_batch = max_chars_per_batch
        self.max_workers = max_workers
        self.stability = stability  # 0, 0.5, or 1.0
        self._stop = False
        self.output_file = ""
        
        # Thread-safe locks
        self._key_lock = threading.Lock()
        self._results_lock = threading.Lock()
        
        # Results storage (batch_idx -> audio_bytes)
        self._batch_results: Dict[int, bytes] = {}
        self._batch_errors: Dict[int, str] = {}
        self._completed_batches = 0
        self._total_batches = 0
        
        # Batch line mapping (batch_idx -> (start_line, end_line))
        self._batch_line_ranges: Dict[int, tuple] = {}
        
        # Active workers
        self._active_workers: List[DialogueBatchWorker] = []
        
        # Voice cleanup workers (background)
        self._cleanup_workers: List[VoiceCleanupWorker] = []
    
    def _log(self, msg: str):
        """Log message to file."""
        _log_to_file(f"[DialogueTTS] {msg}")
        print(f"[DialogueTTS] {msg}")
    
    def stop(self):
        self._stop = True
        # Stop all active workers
        for worker in self._active_workers:
            if hasattr(worker, '_stop'):
                worker._stop = True
        # Note: Cleanup workers continue running in background (không stop)
        # Vì chúng ta muốn key được restore sau khi cleanup xong
    
    def _should_stop(self) -> bool:
        return self._stop
    
    def _split_into_batches(self, inputs: list) -> list:
        """Split inputs into batches based on max_chars_per_batch."""
        batches = []
        current_batch = []
        current_chars = 0
        current_start_line = 0
        
        for i, inp in enumerate(inputs):
            text_len = len(inp.get("text", ""))
            
            if current_chars + text_len > self.max_chars_per_batch and current_batch:
                # Lưu line range cho batch này
                batch_idx = len(batches)
                self._batch_line_ranges[batch_idx] = (current_start_line, i - 1)
                
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
                current_start_line = i
            
            current_batch.append(inp)
            current_chars += text_len
        
        if current_batch:
            # Lưu line range cho batch cuối
            batch_idx = len(batches)
            self._batch_line_ranges[batch_idx] = (current_start_line, len(inputs) - 1)
            batches.append(current_batch)
        
        return batches
    
    def _on_batch_done(self, batch_idx: int, audio_bytes: bytes, success: bool, error_msg: str):
        """Handle batch completion (called from worker thread)."""
        with self._results_lock:
            self._completed_batches += 1
            
            if success and audio_bytes:
                self._batch_results[batch_idx] = audio_bytes
                self._log(f"✅ Batch {batch_idx + 1} completed: {len(audio_bytes) / 1024:.1f} KB")
            else:
                self._batch_errors[batch_idx] = error_msg
                self._log(f"❌ Batch {batch_idx + 1} failed: {error_msg}")
            
            # Update progress
            self.progress.emit(self._completed_batches, self._total_batches, 
                             f"Hoàn thành {self._completed_batches}/{self._total_batches} batch")
            
            # Emit batch_lines_done để cập nhật UI từng batch
            if batch_idx in self._batch_line_ranges:
                start_line, end_line = self._batch_line_ranges[batch_idx]
                self.batch_lines_done.emit(start_line, end_line, success)
    
    def _on_batch_progress(self, batch_idx: int, status: str):
        """Handle batch progress update."""
        self._log(f"📦 Batch {batch_idx + 1}: {status}")
    
    def _on_key_rotated(self, key_prefix: str):
        """Handle key rotation from worker."""
        self.key_rotated.emit(key_prefix)
    
    def _on_voice_limit_hit(self, api_key: str, proxy_url: str):
        """
        Handle voice_limit_reached error.
        Spawn background cleanup worker để xóa custom voices và restore key.
        """
        self._log(f"🧹 Voice limit hit for {api_key[:10]}... - spawning cleanup worker")
        
        # Create cleanup worker
        cleanup_worker = VoiceCleanupWorker(
            api_key=api_key,
            keys_pool=self.keys_pool,
            proxy_url=proxy_url if proxy_url else None
        )
        
        # Connect cleanup done signal
        cleanup_worker.cleanup_done.connect(self._on_cleanup_done, QtCore.Qt.QueuedConnection)
        
        # Track worker
        self._cleanup_workers.append(cleanup_worker)
        
        # Start cleanup in background (không block gen audio)
        cleanup_worker.start()
    
    def _on_cleanup_done(self, api_key: str, success: bool, message: str):
        """Handle cleanup worker completion."""
        if success:
            self._log(f"✅ Cleanup done for {api_key[:10]}...: {message}")
        else:
            self._log(f"⚠️ Cleanup failed for {api_key[:10]}...: {message}")
    
    def run(self):
        """Main worker thread execution with PARALLEL batch processing."""
        if not self.inputs:
            self._log("❌ No inputs provided")
            self.finished.emit(False, "Không có nội dung", "")
            return
        
        # Split into batches
        batches = self._split_into_batches(self.inputs)
        self._total_batches = len(batches)
        
        total_chars = sum(len(inp.get("text", "")) for batch in batches for inp in batch)
        self._log(f"📝 Starting PARALLEL dialogue generation: {len(self.inputs)} lines, {total_chars:,} chars → {self._total_batches} batch(es)")
        self._log(f"🚀 Using {min(self.max_workers, self._total_batches)} parallel workers")
        
        self.progress.emit(0, self._total_batches, f"Đang xử lý {self._total_batches} batch song song...")
        
        # Create thread pool
        pool = QThreadPool.globalInstance()
        pool.setMaxThreadCount(min(self.max_workers, self._total_batches))
        
        # Create signals object for workers
        signals = BatchWorkerSignals()
        signals.batch_done.connect(self._on_batch_done, QtCore.Qt.QueuedConnection)
        signals.batch_progress.connect(self._on_batch_progress, QtCore.Qt.QueuedConnection)
        signals.key_rotated.connect(self._on_key_rotated, QtCore.Qt.QueuedConnection)
        signals.voice_limit_hit.connect(self._on_voice_limit_hit, QtCore.Qt.QueuedConnection)
        
        # Keep strong reference to signals
        self._signals_ref = signals
        
        # Start all batch workers
        for batch_idx, batch in enumerate(batches):
            if self._stop:
                break
            
            worker = DialogueBatchWorker(
                batch_idx=batch_idx,
                batch=batch,
                keys_pool=self.keys_pool,
                proxy_getter=self.proxy_getter,
                proxy_service_db=self.proxy_service_db,
                signals=signals,
                stop_flag_ref=self._should_stop,
                key_lock=self._key_lock,
                stability=self.stability
            )
            
            self._active_workers.append(worker)
            pool.start(worker)
            
            self._log(f"🚀 Started batch {batch_idx + 1}/{self._total_batches}")
        
        # Wait for all batches to complete
        self._log(f"⏳ Waiting for {self._total_batches} batches to complete...")
        
        # Poll for completion (don't block UI)
        while True:
            if self._stop:
                self._log("⏹️ Stopped by user")
                pool.clear()  # Cancel pending workers
                self.finished.emit(False, "Đã dừng", "")
                return
            
            with self._results_lock:
                if self._completed_batches >= self._total_batches:
                    break
            
            time.sleep(0.1)  # Small sleep to avoid busy waiting
        
        # Check for errors
        if self._batch_errors:
            error_batches = list(self._batch_errors.keys())
            first_error = self._batch_errors[error_batches[0]]
            self._log(f"❌ {len(error_batches)} batch(es) failed. First error: {first_error}")
            self.finished.emit(False, f"Lỗi batch {error_batches[0] + 1}: {first_error}", "")
            return
        
        # Collect results in order
        all_audio_parts = []
        for batch_idx in range(self._total_batches):
            if batch_idx in self._batch_results:
                all_audio_parts.append(self._batch_results[batch_idx])
            else:
                self._log(f"❌ Missing result for batch {batch_idx + 1}")
                self.finished.emit(False, f"Thiếu kết quả batch {batch_idx + 1}", "")
                return
        
        # Combine all audio parts
        self._log(f"🔗 Combining {len(all_audio_parts)} audio part(s)...")
        self.progress.emit(self._total_batches, self._total_batches, "Đang ghép audio...")
        
        try:
            if len(all_audio_parts) == 1:
                self.output_file = os.path.join(self.output_dir, f"{self.basename}_dialogue.mp3")
                with open(self.output_file, 'wb') as f:
                    f.write(all_audio_parts[0])
                self._log(f"💾 Saved single batch: {os.path.basename(self.output_file)}")
            else:
                self._log(f"🔗 Concatenating {len(all_audio_parts)} batches with ffmpeg...")
                self.output_file = self._concat_audio_parts(all_audio_parts)
                self._log(f"💾 Saved concatenated file: {os.path.basename(self.output_file)}")
            
            if os.path.exists(self.output_file):
                file_size = os.path.getsize(self.output_file) / 1024
                self._log(f"✅ SUCCESS: {os.path.basename(self.output_file)} ({file_size:.1f} KB)")
                
                # Emit line_done for all lines
                processed_lines = 0
                for batch in batches:
                    for i in range(len(batch)):
                        self.line_done.emit(processed_lines + i, "", 0)
                    processed_lines += len(batch)
                
                self.finished.emit(True, f"Đã lưu: {os.path.basename(self.output_file)}", self.output_file)
            else:
                self._log("❌ Output file not found after save")
                self.finished.emit(False, "Không thể lưu file", "")
                
        except Exception as e:
            self._log(f"❌ Save error: {e}")
            self.finished.emit(False, f"Lỗi lưu file: {str(e)[:50]}", "")
    
    def _concat_audio_parts(self, audio_parts: list) -> str:
        """Concatenate multiple audio parts using ffmpeg."""
        import tempfile
        import subprocess
        from pathlib import Path
        
        # Find ffmpeg using imported function from main app
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("Không tìm thấy FFmpeg. Vui lòng cài FFmpeg hoặc đặt ffmpeg/ffmpeg.exe vào thư mục app")
        
        self._log(f"🔧 Using ffmpeg: {ffmpeg_bin}")
        
        # Save temp files
        temp_files = []
        temp_dir = tempfile.mkdtemp()
        
        try:
            for i, audio_bytes in enumerate(audio_parts):
                temp_path = os.path.join(temp_dir, f"part_{i}.mp3")
                with open(temp_path, 'wb') as f:
                    f.write(audio_bytes)
                temp_files.append(temp_path)
            
            # Create concat list file
            list_file = os.path.join(temp_dir, "concat_list.txt")
            with open(list_file, 'w', encoding='utf-8') as f:
                for temp_path in temp_files:
                    # Use forward slashes for cross-platform compatibility
                    temp_path_posix = Path(temp_path).as_posix()
                    f.write(f"file '{temp_path_posix}'\n")
            
            # Output file
            output_file = os.path.join(self.output_dir, f"{self.basename}_dialogue.mp3")
            
            # Run ffmpeg concat
            cmd = [
                str(ffmpeg_bin), "-y", "-f", "concat", "-safe", "0",
                "-i", list_file, "-c", "copy", output_file
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            
            if result.returncode != 0:
                error_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else "Unknown error"
                raise RuntimeError(f"FFmpeg concat failed: {error_msg[:200]}")
            
            return output_file
            
        finally:
            # Cleanup temp files
            import shutil
            try:
                shutil.rmtree(temp_dir)
            except:
                pass


def setup_tab_system(main_window) -> None:
    """
    Setup tab system for MainWindow.
    
    APPROACH: Instead of moving the existing central widget,
    we create a new tab widget and insert it ABOVE the existing content.
    The original content stays in Tab 1, Tab 2 is a new clone.
    
    Args:
        main_window: Qt MainWindow instance
    """
    print("🎭 [TAB_SYSTEM] Setting up tab system...")
    
    # === GET USER FEATURES ===
    # Lấy features từ DB để biết tab nào được phép hiển thị
    features = None
    try:
        from services.user_features_service import get_user_features, UserFeatures
        
        # Get user_id từ main_window (có thể là current_user_id hoặc _user_id)
        user_id = getattr(main_window, 'current_user_id', None) or getattr(main_window, '_user_id', None)
        
        if user_id:
            # Get DB client (có thể là supabase, auth_service, _db_client, hoặc _auth_client)
            db_client = (
                getattr(main_window, 'supabase', None) or 
                getattr(main_window, 'auth_service', None) or
                getattr(main_window, '_db_client', None) or 
                getattr(main_window, '_auth_client', None)
            )
            features = get_user_features(user_id, db_client)
            print(f"✅ [TAB_SYSTEM] Loaded features for user {user_id}")
        else:
            print("⚠️ [TAB_SYSTEM] No user_id, using default features")
            features = UserFeatures.default()
    except Exception as e:
        print(f"⚠️ [TAB_SYSTEM] Error loading features: {e}, using defaults")
        from services.user_features_service import UserFeatures
        features = UserFeatures.default()
    
    try:
        # Get the existing central widget
        old_central = main_window.centralWidget()
        if not old_central:
            print("❌ [TAB_SYSTEM] No central widget found")
            return
        
        # Get the existing layout from old central
        old_layout = old_central.layout()
        if not old_layout:
            print("❌ [TAB_SYSTEM] No layout in central widget")
            return

        # === Create Tab Widget ===
        tab_widget = QtWidgets.QTabWidget()
        tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                background: #e6e6e6;
            }
            QTabBar::tab {
                background: #d0d0d0;
                border: 1px solid #b0b0b0;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 10px 25px;
                margin-right: 3px;
                font-weight: bold;
                font-size: 11px;
            }
            QTabBar::tab:selected {
                background: #e6e6e6;
                border-bottom: 1px solid #e6e6e6;
            }
            QTabBar::tab:hover:!selected {
                background: #e0e0e0;
            }
        """)
        
        # Remove old_central from main_window but keep it alive
        main_window.takeCentralWidget()
        
        # === TAB 1: Giọng Trả Phí (JWT TTS) - ĐẦU TIÊN ===
        if features.tab_premium_voice:
            try:
                from ui.qt_tab_jwt_setup import add_jwt_tts_tab
                add_jwt_tts_tab(main_window, tab_widget, insert_index=0)
                print("   ✅ Tab 1: Giọng Trả Phí")
            except Exception as e:
                print(f"⚠️ [TAB_SYSTEM] Failed to load Giọng Trả Phí tab: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("   ⏭️ Tab: Giọng Trả Phí (disabled)")
        
        # === TAB 2: Chuyển văn bản (Text to Speech) ===
        if features.tab_text_to_speech:
            tab_widget.addTab(old_central, "🎤 Chuyển văn bản")
            print("   ✅ Tab 2: Chuyển văn bản")
        else:
            # Fallback: vẫn thêm tab chính nếu không có features
            tab_widget.addTab(old_central, "🎤 Chuyển văn bản")
        
        # === TAB 3: Multiple Voice (Đa giọng đọc) ===
        if features.tab_multiple_voice:
            try:
                tab_multi = MultipleVoiceTab(main_window)
                tab_widget.addTab(tab_multi, "💎 Đa giọng đọc")
                main_window._tab_multiple_voice = tab_multi
                print("   ✅ Tab: Đa giọng đọc")
            except Exception as e:
                print(f"⚠️ [TAB_SYSTEM] Failed to load Multiple Voice tab: {e}")
        else:
            print("   ⏭️ Tab: Đa giọng đọc (disabled)")
        
        # === TAB 4: Dubbing ===
        if features.tab_dubbing:
            try:
                from ui.qt_tab_dubbing import DubbingTab
                tab3 = DubbingTab(main_window)
                tab_widget.addTab(tab3, "🎬 Lồng Tiếng")
                main_window._tab_dubbing = tab3
                print("   ✅ Tab: Lồng Tiếng")
            except Exception as e:
                print(f"⚠️ [TAB_SYSTEM] Failed to load Dubbing tab: {e}")
        else:
            print("   ⏭️ Tab: Lồng Tiếng (disabled)")
        
        # === TAB 5: Sound Effects ===
        if features.tab_sound_effects:
            try:
                from ui.qt_tab_sound_effects import SoundEffectsTab
                tab4 = SoundEffectsTab(main_window)
                tab_widget.addTab(tab4, "🔊 Hiệu ứng âm thanh")
                main_window._tab_sound_effects = tab4
                print("   ✅ Tab: Hiệu ứng âm thanh")
            except Exception as e:
                print(f"⚠️ [TAB_SYSTEM] Failed to load Sound Effects tab: {e}")
        else:
            print("   ⏭️ Tab: Hiệu ứng âm thanh (disabled)")
        
        # === TAB 6: Speech-to-Text ===
        if features.tab_speech_to_text:
            try:
                from ui.qt_tab_speech_to_text import SpeechToTextTab
                tab5 = SpeechToTextTab(main_window)
                tab_widget.addTab(tab5, "🎙️ Audio thành văn bản")
                main_window._tab_speech_to_text = tab5
                print("   ✅ Tab: Audio thành văn bản")
            except Exception as e:
                print(f"⚠️ [TAB_SYSTEM] Failed to load Speech-to-Text tab: {e}")
        else:
            print("   ⏭️ Tab: Audio thành văn bản (disabled)")
        
        # === TAB 7: Voice Changer (Speech-to-Speech) ===
        if features.tab_voice_changer:
            try:
                from ui.qt_tab_voice_changer import VoiceChangerTab
                tab6 = VoiceChangerTab(main_window)
                tab_widget.addTab(tab6, "🔄 Thay đổi giọng nói")
                main_window._tab_voice_changer = tab6
                print("   ✅ Tab: Thay đổi giọng nói")
            except Exception as e:
                print(f"⚠️ [TAB_SYSTEM] Failed to load Voice Changer tab: {e}")
        else:
            print("   ⏭️ Tab: Thay đổi giọng nói (disabled)")
        
        # === Create new central widget containing tab_widget ===
        new_central = QtWidgets.QWidget()
        new_layout = QtWidgets.QVBoxLayout(new_central)
        new_layout.setContentsMargins(0, 0, 0, 0)
        new_layout.setSpacing(0)
        new_layout.addWidget(tab_widget)
        
        # Set new central widget
        main_window.setCentralWidget(new_central)
        
        # Store references
        main_window._tab_widget = tab_widget
        main_window._tab_single_voice = old_central
        main_window._user_features = features  # Store features for later use
        
        print("✅ [TAB_SYSTEM] Tab system setup complete")
        
    except Exception as e:
        print(f"❌ [TAB_SYSTEM] Setup failed: {e}")
        import traceback
        traceback.print_exc()


class MultipleVoiceTab(QtWidgets.QWidget):
    """
    Tab for Multiple Voice - uses text-to-dialogue API.
    """
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.tts_worker = None
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the multiple voice tab UI."""
        root = QtWidgets.QGridLayout(self)
        root.setHorizontalSpacing(8)
        root.setVerticalSpacing(6)
        
        # Apply same stylesheet as main window
        self.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #b0b0b0;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 5px;
                background-color: #e6e6e6;
            }
        """)
        
        # === VOICE MANAGER ===
        grp_voices = QtWidgets.QGroupBox("Quản lý Voice")
        vm_layout = QtWidgets.QVBoxLayout(grp_voices)
        
        # Voice count label
        self.lbl_voice_count = QtWidgets.QLabel("Chưa có voice nào")
        self.lbl_voice_count.setStyleSheet("font-size: 11px; color: #7f8c8d; padding: 3px;")
        vm_layout.addWidget(self.lbl_voice_count)
        
        # Voice list preview - tăng chiều cao
        self.lbl_voice_preview = QtWidgets.QLabel("")
        self.lbl_voice_preview.setStyleSheet("""
            font-size: 10px; color: #2c3e50; padding: 5px;
            background-color: #f5f5f5; border-radius: 3px;
        """)
        self.lbl_voice_preview.setWordWrap(True)
        self.lbl_voice_preview.setMinimumHeight(80)  # Tăng chiều cao tối thiểu
        self.lbl_voice_preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)  # Căn trên trái
        vm_layout.addWidget(self.lbl_voice_preview, 1)  # stretch factor = 1
        
        # Spacer để đẩy nút xuống cuối
        vm_layout.addStretch()
        
        # Manage button - ở cuối
        self.bt_manage_voices = QtWidgets.QPushButton("Quản lý Voice ID")
        self.bt_manage_voices.setCursor(Qt.PointingHandCursor)
        self.bt_manage_voices.clicked.connect(self._open_voice_manager)
        vm_layout.addWidget(self.bt_manage_voices)
        
        grp_voices.setMaximumWidth(300)
        grp_voices.setMinimumHeight(90)  # Tăng chiều cao tối thiểu của group
        
        # === VOICE SETTINGS (giống TTS tab nhưng chỉ Stability enabled) ===
        grp_set = QtWidgets.QGroupBox("")
        s = QtWidgets.QGridLayout(grp_set)
        s.setHorizontalSpacing(8)
        
        # Row 0: Change settings checkbox + Speaker Boost (disabled)
        self.cb_change = QtWidgets.QCheckBox("Change voice settings")
        self.cb_change.setChecked(True)
        self.cb_change.setEnabled(False)  # Disabled - Dialogue API không hỗ trợ
        self.cb_change.setToolTip("Dialogue API không hỗ trợ tùy chọn này")
        s.addWidget(self.cb_change, 0, 0, 1, 3)
        
        self.cb_boost = QtWidgets.QCheckBox("Speaker Boost")
        self.cb_boost.setChecked(False)
        self.cb_boost.setEnabled(False)  # Disabled - Dialogue API không hỗ trợ
        self.cb_boost.setToolTip("Dialogue API không hỗ trợ Speaker Boost")
        s.addWidget(self.cb_boost, 0, 3, 1, 2)
        
        # Row 1: Speed + Style (disabled)
        s.addWidget(QtWidgets.QLabel("Speed:"), 1, 0)
        self.sb_speed = QtWidgets.QDoubleSpinBox()
        self.sb_speed.setRange(0.5, 2.0)
        self.sb_speed.setValue(1.0)
        self.sb_speed.setSingleStep(0.05)
        self.sb_speed.setEnabled(False)  # Disabled - Dialogue API không hỗ trợ
        self.sb_speed.setToolTip("Dialogue API không hỗ trợ Speed")
        s.addWidget(self.sb_speed, 1, 1)
        
        s.addWidget(QtWidgets.QLabel("Style:"), 1, 2)
        self.sb_style = QtWidgets.QSpinBox()
        self.sb_style.setRange(0, 100)
        self.sb_style.setValue(0)
        self.sb_style.setSuffix(" %")
        self.sb_style.setEnabled(False)  # Disabled - Dialogue API không hỗ trợ
        self.sb_style.setToolTip("Dialogue API không hỗ trợ Style")
        s.addWidget(self.sb_style, 1, 3)
        
        # Row 2: Stability (ENABLED) + Similarity (disabled)
        s.addWidget(QtWidgets.QLabel("Stability:"), 2, 0)
        self.cb_stab = QtWidgets.QComboBox()
        self.cb_stab.addItems(["0%", "50%", "100%"])
        self.cb_stab.setCurrentIndex(1)  # Default 50%
        self.cb_stab.setToolTip("Dialogue API chỉ hỗ trợ 3 giá trị: 0%, 50%, 100%")
        s.addWidget(self.cb_stab, 2, 1)
        
        s.addWidget(QtWidgets.QLabel("Similarity:"), 2, 2)
        self.sb_sim = QtWidgets.QSpinBox()
        self.sb_sim.setRange(0, 100)
        self.sb_sim.setValue(75)
        self.sb_sim.setSuffix(" %")
        self.sb_sim.setEnabled(False)  # Disabled - Dialogue API không hỗ trợ
        self.sb_sim.setToolTip("Dialogue API không hỗ trợ Similarity")
        s.addWidget(self.sb_sim, 2, 3)
        
        # Buttons: Reset + Load
        self.bt_reset = QtWidgets.QPushButton("Reset")
        self.bt_reset.clicked.connect(self._reset_settings)
        s.addWidget(self.bt_reset, 1, 4)
        
        self.bt_load = QtWidgets.QPushButton("Load")
        self.bt_load.clicked.connect(self._load_credits)
        s.addWidget(self.bt_load, 2, 4)
        
        # Hidden sb_stab for compatibility (not used)
        self.sb_stab = QtWidgets.QSpinBox()
        self.sb_stab.setValue(50)
        self.sb_stab.setVisible(False)
        
        grp_set.setMaximumWidth(320)
        
        # === OPTIONS ===
        grp_opt = QtWidgets.QGroupBox("Tùy chọn")
        o = QtWidgets.QGridLayout(grp_opt)
        
        o.addWidget(QtWidgets.QLabel("Chế độ:"), 0, 0)
        self.cb_mode = QtWidgets.QComboBox()
        self.cb_mode.addItems([
            "Xen kẽ (1-2-1-2...)", 
            "Tuần tự (nửa đầu-sau)",
            "Theo tag [N]"
        ])
        self.cb_mode.setCurrentIndex(2)  # Default: Theo tag [N]
        self.cb_mode.setToolTip(
            "Cách phân chia voice cho các đoạn text:\n"
            "• Xen kẽ: Voice 1, 2, 1, 2...\n"
            "• Tuần tự: Nửa đầu Voice 1, nửa sau Voice 2...\n"
            "• Theo tag [N]: Dùng [1], [2]... ở đầu mỗi dòng để chỉ định voice"
        )
        self.cb_mode.currentIndexChanged.connect(self._on_mode_changed)  # Reload preview khi đổi mode
        o.addWidget(self.cb_mode, 0, 1, 1, 2)

        th = QtWidgets.QHBoxLayout()
        th.addWidget(QtWidgets.QLabel("Thread:"))
        self.sb_thread = QtWidgets.QSpinBox()
        self.sb_thread.setRange(5, 10)
        self.sb_thread.setValue(5)
        th.addWidget(self.sb_thread)
        th.addStretch(1)
        o.addLayout(th, 1, 0, 1, 3)
        
        self.bt_adv = QtWidgets.QPushButton("Cài đặt nâng cao")
        self.bt_adv.clicked.connect(self._open_advanced)
        o.addWidget(self.bt_adv, 2, 0, 1, 3)
        
        grp_opt.setMaximumWidth(220)
        
        # === TOP ROW ===
        top = QtWidgets.QHBoxLayout()
        top.addWidget(grp_voices, 3)
        top.addWidget(grp_set, 4)
        top.addWidget(grp_opt, 2)
        root.addLayout(top, 0, 0, 1, 2)
        
        # === BATCH JOB ===
        grp_b = QtWidgets.QGroupBox("Batch Job")
        b_layout = QtWidgets.QHBoxLayout(grp_b)
        
        left_widget = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 5, 0)
        
        path_row = QtWidgets.QHBoxLayout()
        lbl_path = QtWidgets.QLabel("Đường Dẫn:")
        lbl_path.setFixedWidth(60)
        path_row.addWidget(lbl_path)
        self.ed_folder = QtWidgets.QLineEdit()
        self.ed_folder.setPlaceholderText("Chọn file .txt hoặc thư mục...")
        path_row.addWidget(self.ed_folder)
        left_layout.addLayout(path_row)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(4)
        btn_row.addSpacing(60)
        
        self.bt_browse_file = QtWidgets.QPushButton("📄 File")
        self.bt_browse_folder = QtWidgets.QPushButton("📁 Folder")
        self.bt_browse_srt = QtWidgets.QPushButton("📜 SRT")
        self.bt_browse_file.clicked.connect(self._pick_file)
        self.bt_browse_folder.clicked.connect(self._pick_folder)
        self.bt_browse_srt.clicked.connect(self._pick_srt)
        btn_row.addWidget(self.bt_browse_file, 1)
        btn_row.addWidget(self.bt_browse_folder, 1)
        btn_row.addWidget(self.bt_browse_srt, 1)
        left_layout.addLayout(btn_row)
        
        result_row = QtWidgets.QHBoxLayout()
        result_row.addSpacing(60)
        
        self.cb_autosrt = QtWidgets.QCheckBox("Tự động tạo Srt")
        result_row.addWidget(self.cb_autosrt, 1)
        result_row.addStretch(1)
        left_layout.addLayout(result_row)
        left_layout.addStretch(1)
        
        # Right: Table
        right_widget = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 0, 0, 0)

        self.tbl_queue = QtWidgets.QTableWidget(0, 4)
        self.tbl_queue.setHorizontalHeaderLabels(["ID", "FileName", "Status", "Tiến độ"])
        hdr = self.tbl_queue.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(2, 100)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(3, 100)
        self.tbl_queue.verticalHeader().setVisible(False)
        small_font = QtGui.QFont()
        small_font.setPointSize(8)
        self.tbl_queue.setFont(small_font)
        self.tbl_queue.verticalHeader().setDefaultSectionSize(20)
        right_layout.addWidget(self.tbl_queue)
        
        b_layout.addWidget(left_widget, 35)
        b_layout.addWidget(right_widget, 65)
        grp_b.setFixedHeight(130)
        root.addWidget(grp_b, 1, 0, 1, 2)
        
        # === SUBTITLES ===
        grp_s = QtWidgets.QGroupBox("Subtitles")
        vs = QtWidgets.QVBoxLayout(grp_s)
        
        tb = QtWidgets.QHBoxLayout()
        self.bt_start = QtWidgets.QPushButton("Start")
        self.bt_stop = QtWidgets.QPushButton("Stop")
        self.bt_more = QtWidgets.QPushButton("📁 Output")
        self.bt_stop.setEnabled(False)
        self.bt_start.clicked.connect(self._start_generation)
        self.bt_stop.clicked.connect(self._stop_generation)
        self.bt_more.clicked.connect(self._open_output)
        for w in [self.bt_start, self.bt_stop, self.bt_more]:
            tb.addWidget(w)
        tb.addStretch(1)
        vs.addLayout(tb)

        self.tbl_sub = QtWidgets.QTableWidget(0, 5)
        self.tbl_sub.setHorizontalHeaderLabels(["ID", "Output", "Content", "Voice", "Status"])
        self.tbl_sub.verticalHeader().setVisible(False)
        self.tbl_sub.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_sub.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        hdr_sub = self.tbl_sub.horizontalHeader()
        hdr_sub.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(0, 40)
        hdr_sub.setSectionResizeMode(1, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(1, 120)
        hdr_sub.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr_sub.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(3, 100)
        hdr_sub.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tbl_sub.setColumnWidth(4, 80)
        vs.addWidget(self.tbl_sub)
        
        root.addWidget(grp_s, 2, 0, 1, 2)
        root.setRowStretch(2, 1)
        
        # Store state
        self.queue_paths = []
        self.is_running = False
        self.voices = []
        self.start_time = 0
        
        # Load saved config and voices
        QtCore.QTimer.singleShot(500, self._load_config)
        QtCore.QTimer.singleShot(600, self._load_voices_from_file)

    # ========== VOICE MANAGER ==========
    def _open_voice_manager(self):
        """Open Voice Manager dialog."""
        from ui.voice_manager_dialog import VoiceManagerDialog
        dialog = VoiceManagerDialog(self, main_window=self.main_window)
        dialog.voices_changed.connect(self._on_voices_changed)
        dialog.exec()
    
    def _on_voices_changed(self, voices: list):
        """Handle voices changed from Voice Manager."""
        self.voices = voices
        self._update_voice_preview()
    
    def _update_voice_preview(self):
        """Update voice count and preview labels."""
        count = len(self.voices)
        
        if count == 0:
            self.lbl_voice_count.setText("Chưa có voice nào")
            self.lbl_voice_count.setStyleSheet("font-size: 11px; color: #e74c3c; padding: 3px;")
            self.lbl_voice_preview.setText("Nhấn 'Quản lý Voice ID' để thêm")
        else:
            self.lbl_voice_count.setText(f"Đã có {count} voice(s)")
            self.lbl_voice_count.setStyleSheet("font-size: 11px; color: #27ae60; padding: 3px;")
            
            preview_lines = []
            for i, v in enumerate(self.voices[:3]):
                name = v.get("name", "")[:15]
                vid = v.get("voice_id", "")[:8]
                status = "✓" if v.get("tested") else ""
                preview_lines.append(f"{i+1}. {name} ({vid}...) {status}")
            
            if count > 3:
                preview_lines.append(f"... và {count - 3} voice khác")
            
            self.lbl_voice_preview.setText("\n".join(preview_lines))
    
    def _load_voices_from_file(self):
        """Load voices from dialogue_voices.json."""
        import json
        import sys
        
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        config_path = os.path.join(app_dir, "dialogue_voices.json")
        
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.voices = data.get("voices", [])
                    self._update_voice_preview()
                    print(f"✅ [MultiVoice] Loaded {len(self.voices)} voices")
            except Exception as e:
                print(f"❌ [MultiVoice] Error loading voices: {e}")
    
    # ========== FILE PICKING ==========
    def _pick_file(self):
        """Pick single file."""
        file, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chọn file", "",
            "Text files (*.txt);;SRT files (*.srt);;All files (*.*)"
        )
        if file:
            self.ed_folder.setText(file)
            self.queue_paths = [file]
            self._update_queue_table()
            self._load_subtitles_from_file(file)
    
    def _pick_folder(self):
        """Pick folder."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục")
        if folder:
            self.ed_folder.setText(folder)
            self.queue_paths = []
            for f in sorted(os.listdir(folder)):
                if f.lower().endswith('.txt'):
                    self.queue_paths.append(os.path.join(folder, f))
            self._update_queue_table()

    def _pick_srt(self):
        """Pick SRT file or folder."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục SRT")
        if folder:
            self.ed_folder.setText(folder)
            self.queue_paths = []
            for f in sorted(os.listdir(folder)):
                if f.lower().endswith('.srt'):
                    self.queue_paths.append(os.path.join(folder, f))
            self._update_queue_table()
    
    def _update_queue_table(self):
        """Update queue table."""
        self.tbl_queue.setRowCount(len(self.queue_paths))
        for i, path in enumerate(self.queue_paths):
            self.tbl_queue.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i + 1)))
            self.tbl_queue.setItem(i, 1, QtWidgets.QTableWidgetItem(os.path.basename(path)))
            self.tbl_queue.setItem(i, 2, QtWidgets.QTableWidgetItem("READY"))
            self.tbl_queue.setItem(i, 3, QtWidgets.QTableWidgetItem("0%"))
    
    def _on_mode_changed(self, index: int):
        """Reload preview khi user thay đổi mode."""
        # Reload preview nếu đã có file được chọn
        if self.queue_paths:
            self._load_subtitles_from_file(self.queue_paths[0])
    
    def _load_subtitles_from_file(self, filepath: str):
        """Load subtitles from file and assign voices."""
        try:
            lines = []
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
            
            num_voices = len(self.voices) if self.voices else 1
            mode = self.cb_mode.currentIndex()  # 0 = alternating, 1 = sequential, 2 = tag
            
            self.tbl_sub.setRowCount(len(lines))
            for i, line in enumerate(lines):
                # Parse tag và text
                voice_num, clean_text = self._parse_voice_tag(line, i, num_voices, mode)
                
                voice_label = f"Voice {voice_num}"
                if self.voices and voice_num <= len(self.voices):
                    v = self.voices[voice_num - 1]
                    voice_label = v.get("name", f"Voice {voice_num}")[:15]
                
                self.tbl_sub.setItem(i, 0, QtWidgets.QTableWidgetItem(str(i + 1)))
                self.tbl_sub.setItem(i, 1, QtWidgets.QTableWidgetItem(""))  # Output
                self.tbl_sub.setItem(i, 2, QtWidgets.QTableWidgetItem(clean_text[:80]))  # Content (không có tag)
                self.tbl_sub.setItem(i, 3, QtWidgets.QTableWidgetItem(voice_label))  # Voice
                
                status_item = QtWidgets.QTableWidgetItem("READY")
                status_item.setForeground(QtGui.QColor("#7f8c8d"))
                self.tbl_sub.setItem(i, 4, status_item)
                
        except Exception as e:
            print(f"❌ [MultiVoice] Error loading file: {e}")
    
    def _parse_voice_tag(self, line: str, line_idx: int, num_voices: int, mode: int) -> tuple:
        """
        Parse voice tag từ dòng text.
        
        Args:
            line: Dòng text gốc (có thể có tag [N] ở đầu)
            line_idx: Index của dòng (0-based)
            num_voices: Số lượng voices
            mode: 0 = alternating, 1 = sequential, 2 = tag
        
        Returns:
            (voice_num, clean_text): voice_num (1-based), text đã bỏ tag
        """
        import re
        
        # Mode 2: Theo tag [N]
        if mode == 2:
            # Tìm tag [N] ở đầu dòng (N là số từ 1 trở lên)
            match = re.match(r'^\[(\d+)\]\s*', line)
            if match:
                tag_num = int(match.group(1))
                clean_text = line[match.end():].strip()
                
                # Validate tag_num trong range
                if 1 <= tag_num <= num_voices:
                    return (tag_num, clean_text)
                else:
                    # Tag ngoài range → dùng voice 1 và giữ nguyên text
                    return (1, clean_text)
            else:
                # Không có tag → dùng voice 1 (fallback)
                return (1, line)
        
        # Mode 0: Alternating (xen kẽ)
        elif mode == 0:
            voice_num = (line_idx % num_voices) + 1
            return (voice_num, line)
        
        # Mode 1: Sequential (tuần tự)
        else:
            segment_size = max(1, (line_idx + num_voices) // num_voices)  # Chia đều
            # Tính lại cho chính xác hơn
            total_lines = line_idx + 1  # Giả sử đây là tổng số dòng tạm thời
            segment_size = max(1, total_lines // num_voices) if num_voices > 0 else 1
            voice_num = min(num_voices, (line_idx // max(1, segment_size)) + 1)
            return (voice_num, line)

    # ========== GENERATION ==========
    def _start_generation(self):
        """Start TTS generation with multiple voices using text-to-dialogue API."""
        
        # ========== CHECK SUBSCRIPTION trước khi bắt đầu ==========
        if hasattr(self.main_window, 'current_user_id') and self.main_window.current_user_id:
            if not self._check_subscription_active():
                # Load subscription info để hiển thị chi tiết lỗi
                info = self._load_subscription_info()
                
                error_msg = "Gói của bạn không hợp lệ!\n\n"
                
                if info.get('status') == 'no_subscription':
                    error_msg += "❌ Bạn chưa có gói nào.\n"
                elif info.get('status') == 'expired':
                    error_msg += f"❌ Gói '{info.get('type', '')}' đã hết hạn.\n"
                    if info.get('end_date'):
                        error_msg += f"📅 Ngày hết hạn: {info['end_date'][:10]}\n"
                
                count_chars = info.get('count_characters')
                if count_chars is not None and count_chars <= 0:
                    error_msg += "❌ Đã hết số ký tự trong gói.\n"
                
                error_msg += "\nVui lòng liên hệ admin để gia hạn."
                
                QtWidgets.QMessageBox.critical(
                    self,
                    "⛔ Subscription không hợp lệ",
                    error_msg
                )
                _log_to_file("[DialogueTTS] ⛔ Blocked - subscription không hợp lệ")
                return
            
            # Check credits còn đủ không
            if not self._check_subscription_credits():
                return
        
        # Validate
        if not self.queue_paths:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng chọn file hoặc thư mục!")
            return
        
        if not self.voices or len(self.voices) < 1:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Vui lòng thêm ít nhất 1 voice trong Voice Manager!")
            return
        
        voice_ids = [v.get("voice_id") for v in self.voices if v.get("voice_id")]
        if not voice_ids:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Không có voice ID hợp lệ!")
            return
        
        # Check API key
        if not hasattr(self.main_window, 'keys') or not self.main_window.keys.cur():
            QtWidgets.QMessageBox.warning(self, "Lỗi", "Chưa có API key! Load key trước.")
            return
        
        # Process first file
        filepath = self.queue_paths[0]
        
        # Read lines
        lines = []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Lỗi", f"Không đọc được file: {e}")
            return
        
        if not lines:
            QtWidgets.QMessageBox.warning(self, "Lỗi", "File không có nội dung!")
            return
        
        # Build inputs for API
        mode = self.cb_mode.currentIndex()  # 0 = alternating, 1 = sequential, 2 = tag
        num_voices = len(voice_ids)
        
        inputs = []
        for i, line in enumerate(lines):
            # Parse tag và lấy voice_num, clean_text
            voice_num, clean_text = self._parse_voice_tag(line, i, num_voices, mode)
            voice_idx = voice_num - 1  # Convert to 0-based index
            
            # Đảm bảo voice_idx trong range
            voice_idx = max(0, min(voice_idx, num_voices - 1))
            
            inputs.append({
                "text": clean_text,  # Dùng text đã bỏ tag
                "voice_id": voice_ids[voice_idx]
            })
        
        # Update UI - set all to PROCESSING
        self.is_running = True
        self.start_time = time.time()
        self.bt_start.setEnabled(False)
        self.bt_stop.setEnabled(True)
        
        for i in range(self.tbl_sub.rowCount()):
            status_item = QtWidgets.QTableWidgetItem("PROCESSING")
            status_item.setForeground(QtGui.QColor("#f39c12"))  # Orange
            self.tbl_sub.setItem(i, 4, status_item)
        
        # Update queue table
        self.tbl_queue.setItem(0, 2, QtWidgets.QTableWidgetItem("Processing"))
        
        # Get output directory
        output_dir = os.path.dirname(filepath)
        basename = os.path.splitext(os.path.basename(filepath))[0]
        
        # Proxy getter function
        def get_proxy():
            if hasattr(self.main_window, 'proxy_service_db') and self.main_window.proxy_service_db:
                return self.main_window.proxy_service_db.get_current_proxy()
            return None
        
        # ⚠️ Force rotate proxy BEFORE starting generation to ensure fresh proxy
        # This helps avoid unusual_activity errors from reused/flagged proxies
        if hasattr(self.main_window, 'proxy_service_db') and self.main_window.proxy_service_db:
            try:
                # Gọi force_refresh() để lấy IP mới từ proxyxoay API
                fresh_proxy = self.main_window.proxy_service_db.force_refresh()
                if fresh_proxy:
                    _log_to_file(f"[DialogueTTS] ✅ [Pre-gen] Fresh proxy: {fresh_proxy[:50]}...")
                else:
                    _log_to_file(f"[DialogueTTS] ⚠️ [Pre-gen] Could not refresh proxy")
            except Exception as e:
                _log_to_file(f"[DialogueTTS] ⚠️ Pre-gen proxy refresh failed: {e}")
        
        # Get stability from combo box
        stability = self._get_stability_value()
        
        # Start worker thread
        self.tts_worker = DialogueTTSWorker(
            keys_pool=self.main_window.keys,
            inputs=inputs,
            output_dir=output_dir,
            basename=basename,
            proxy_getter=get_proxy,
            proxy_service_db=getattr(self.main_window, 'proxy_service_db', None),
            max_chars_per_batch=1000,  # Giới hạn 1000 chars/batch
            max_workers=self.sb_thread.value(),  # Số luồng song song
            stability=stability
        )
        self.tts_worker.progress.connect(self._on_tts_progress)
        self.tts_worker.line_done.connect(self._on_line_done)
        self.tts_worker.batch_lines_done.connect(self._on_batch_lines_done)  # Cập nhật UI từng batch
        self.tts_worker.finished.connect(self._on_tts_finished)
        self.tts_worker.key_rotated.connect(self._on_key_rotated)
        self.tts_worker.start()
    
    def _on_tts_progress(self, current: int, total: int, status: str):
        """Handle TTS progress update."""
        # Update queue progress
        if total > 0:
            pct = int((current / total) * 100)
            self.tbl_queue.setItem(0, 3, QtWidgets.QTableWidgetItem(f"{pct}%"))
    
    def _on_line_done(self, line_idx: int, output_file: str, duration: float):
        """Handle individual line completion."""
        if line_idx < self.tbl_sub.rowCount():
            status_item = QtWidgets.QTableWidgetItem("DONE")
            status_item.setForeground(QtGui.QColor("#27ae60"))  # Green
            self.tbl_sub.setItem(line_idx, 4, status_item)
    
    def _on_batch_lines_done(self, start_line: int, end_line: int, success: bool):
        """Handle batch completion - cập nhật status từng dòng trong batch."""
        for line_idx in range(start_line, end_line + 1):
            if line_idx < self.tbl_sub.rowCount():
                if success:
                    status_item = QtWidgets.QTableWidgetItem("DONE")
                    status_item.setForeground(QtGui.QColor("#27ae60"))  # Green
                else:
                    status_item = QtWidgets.QTableWidgetItem("ERROR")
                    status_item.setForeground(QtGui.QColor("#e74c3c"))  # Red
                self.tbl_sub.setItem(line_idx, 4, status_item)
    
    def _on_key_rotated(self, key_prefix: str):
        """Handle key rotation notification."""
        print(f"🔑 [MultiVoice] Key rotated to: {key_prefix}")
    
    def _on_tts_finished(self, success: bool, message: str, output_file: str):
        """Handle TTS generation finished."""
        self.is_running = False
        self.bt_start.setEnabled(True)
        self.bt_stop.setEnabled(False)
        
        elapsed = int(time.time() - self.start_time)
        
        if success:
            self.tbl_queue.setItem(0, 2, QtWidgets.QTableWidgetItem("Completed"))
            self.tbl_queue.setItem(0, 3, QtWidgets.QTableWidgetItem("100%"))
            
            # Update all rows with output file
            output_basename = os.path.basename(output_file) if output_file else ""
            
            for i in range(self.tbl_sub.rowCount()):
                # Output column - show shared output file
                self.tbl_sub.setItem(i, 1, QtWidgets.QTableWidgetItem(output_basename))
                
                # Status - DONE with green color
                status_item = QtWidgets.QTableWidgetItem("DONE")
                status_item.setForeground(QtGui.QColor("#27ae60"))
                self.tbl_sub.setItem(i, 4, status_item)
            
            # Trừ credits sau khi gen thành công
            self._deduct_credits_for_dialogue()
            
            QtWidgets.QMessageBox.information(
                self, "Thành công", 
                f"Đã tạo file: {output_basename}\n"
                f"Thời gian xử lý: {elapsed}s"
            )
        else:
            self.tbl_queue.setItem(0, 2, QtWidgets.QTableWidgetItem("Error"))
            
            for i in range(self.tbl_sub.rowCount()):
                status_item = QtWidgets.QTableWidgetItem("ERROR")
                status_item.setForeground(QtGui.QColor("#e74c3c"))  # Red
                self.tbl_sub.setItem(i, 4, status_item)
            
            QtWidgets.QMessageBox.warning(self, "Lỗi", message)
    
    def _stop_generation(self):
        """Stop generation."""
        if self.tts_worker and self.tts_worker.isRunning():
            self.tts_worker.stop()
            self.tts_worker.wait(3000)  # Wait max 3 seconds
        
        self.is_running = False
        self.bt_stop.setEnabled(False)
        self.bt_start.setEnabled(True)
        
        # Update status to STOPPED
        for i in range(self.tbl_sub.rowCount()):
            current_status = self.tbl_sub.item(i, 4)
            if current_status and current_status.text() == "PROCESSING":
                status_item = QtWidgets.QTableWidgetItem("STOPPED")
                status_item.setForeground(QtGui.QColor("#95a5a6"))
                self.tbl_sub.setItem(i, 4, status_item)
    
    def _open_output(self):
        """Open output folder."""
        import subprocess
        import sys
        
        folder = os.path.dirname(self.queue_paths[0]) if self.queue_paths else ""
        if folder and os.path.exists(folder):
            if sys.platform == 'darwin':
                subprocess.call(['open', folder])
            elif sys.platform == 'win32':
                os.startfile(folder)
            else:
                subprocess.call(['xdg-open', folder])

    # ========== SETTINGS ==========
    def _reset_settings(self):
        """Reset voice settings to default."""
        self.cb_stab.setCurrentIndex(1)  # 50%
    
    def _get_stability_value(self) -> float:
        """Get stability value from combo box (0, 0.5, or 1.0)."""
        idx = self.cb_stab.currentIndex()
        return [0.0, 0.5, 1.0][idx]
    
    def _load_credits(self):
        """Load credits from main window."""
        if hasattr(self.main_window, 'check_credits_async'):
            self.main_window.check_credits_async()
    
    def _open_advanced(self):
        """Open advanced settings dialog."""
        if hasattr(self.main_window, 'open_adv'):
            self.main_window.open_adv()

    # ========== CONFIG SAVE/LOAD ==========
    def _get_config_path(self) -> str:
        """Get config file path."""
        import sys
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(app_dir, "multiple_voice_config.json")
    
    def _save_config(self):
        """Save voice settings to config file."""
        import json
        
        # Map mode index to name
        mode_names = ["alternating", "sequential", "tag"]
        mode_idx = self.cb_mode.currentIndex()
        mode_name = mode_names[mode_idx] if mode_idx < len(mode_names) else "alternating"
        
        config = {
            "enabled": True,
            "assignment_mode": mode_name,
            "settings": {
                "stability": self._get_stability_value(),
                "thread_count": self.sb_thread.value()
            }
        }
        
        try:
            config_path = self._get_config_path()
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ [MultiVoice] Error saving config: {e}")
    
    def _load_config(self):
        """Load voice settings from config file."""
        import json
        
        config_path = self._get_config_path()
        if not os.path.exists(config_path):
            return
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # Map mode name to index
            mode = config.get("assignment_mode", "alternating")
            mode_map = {"alternating": 0, "sequential": 1, "tag": 2}
            self.cb_mode.setCurrentIndex(mode_map.get(mode, 0))
            
            settings = config.get("settings", {})
            if "stability" in settings:
                stab = settings["stability"]
                # Map value to combo index: 0->0, 0.5->1, 1.0->2
                if stab <= 0.25:
                    self.cb_stab.setCurrentIndex(0)
                elif stab <= 0.75:
                    self.cb_stab.setCurrentIndex(1)
                else:
                    self.cb_stab.setCurrentIndex(2)
            if "thread_count" in settings:
                self.sb_thread.setValue(settings["thread_count"])
        except Exception as e:
            print(f"❌ [MultiVoice] Error loading config: {e}")

    # ========== SUBSCRIPTION CHECK ==========
    def _check_subscription_active(self) -> bool:
        """
        Check if user has active subscription.
        Kiểm tra:
        - is_active = True
        - end_date chưa hết hạn  
        - count_characters > 0 (nếu có)
        
        Returns: True nếu subscription còn hợp lệ
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                _log_to_file("[DialogueTTS] ⚠️ No user logged in")
                return False
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                _log_to_file("[DialogueTTS] ⚠️ No database connection")
                return False
            
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('is_active, end_date, count_characters, subscription_type')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                _log_to_file("[DialogueTTS] ❌ No active subscription found")
                return False
            
            sub = result.data[0]
            
            # Check if subscription has expired
            if sub.get('end_date'):
                from datetime import datetime, timezone
                try:
                    end_date_str = sub['end_date']
                    if end_date_str.endswith('Z'):
                        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    elif '+' in end_date_str or '-' in end_date_str[-6:]:
                        end_date = datetime.fromisoformat(end_date_str)
                    else:
                        end_date = datetime.fromisoformat(end_date_str)
                    
                    # Compare với UTC time
                    now = datetime.now(timezone.utc) if end_date.tzinfo else datetime.now()
                    if now > end_date:
                        _log_to_file(f"[DialogueTTS] ❌ Subscription expired: {end_date}")
                        return False
                except Exception as e:
                    _log_to_file(f"[DialogueTTS] ⚠️ Error parsing end_date: {e}")
            
            # Check character-based quota
            try:
                count_chars = sub.get('count_characters')
                if count_chars is not None and int(count_chars) <= 0:
                    _log_to_file("[DialogueTTS] ❌ No remaining characters (count_characters <= 0)")
                    return False
            except Exception:
                pass
            
            _log_to_file(f"[DialogueTTS] ✅ Subscription active: {sub.get('subscription_type', 'unknown')}")
            return True
            
        except Exception as e:
            _log_to_file(f"[DialogueTTS] ❌ Subscription check error: {e}")
            return False
    
    def _load_subscription_info(self) -> dict:
        """
        Load subscription info từ database.
        Returns dict với thông tin gói hoặc {} nếu lỗi.
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return {}
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return {}
            
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('*')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                return {'status': 'no_subscription'}
            
            sub = result.data[0]
            
            # Calculate days remaining
            days_remaining = None
            if sub.get('end_date'):
                from datetime import datetime, timezone
                try:
                    end_str = sub['end_date']
                    if end_str.endswith('Z'):
                        end_date = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    else:
                        end_date = datetime.fromisoformat(end_str)
                    
                    now = datetime.now(timezone.utc) if end_date.tzinfo else datetime.now()
                    days_remaining = max(0, (end_date - now).days)
                except:
                    pass
            
            return {
                'type': sub.get('subscription_type', 'unknown'),
                'end_date': sub.get('end_date'),
                'days_remaining': days_remaining,
                'is_active': sub.get('is_active', False),
                'count_characters': sub.get('count_characters'),
                'status': 'active' if sub.get('is_active') and (days_remaining is None or days_remaining > 0) else 'expired'
            }
        except Exception as e:
            _log_to_file(f"[DialogueTTS] ❌ Load subscription error: {e}")
            return {'error': str(e)}
    
    def _check_subscription_credits(self) -> bool:
        """
        Kiểm tra subscription credits trước khi bắt đầu TTS.
        Returns: True nếu còn credits, False nếu hết.
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return True  # Không có user_id, bỏ qua kiểm tra
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return True  # Không có supabase, bỏ qua kiểm tra
            
            # Get current subscription
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('id, count_characters, plan_name, subscription_type')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                return True  # Không có subscription, bỏ qua kiểm tra
            
            sub = result.data[0]
            current_chars = sub.get('count_characters')
            plan_name = sub.get('plan_name') or sub.get('subscription_type', 'Unknown')
            
            if current_chars is None:
                return True  # Không track characters, bỏ qua kiểm tra
            
            if current_chars <= 0:
                # Hết credits - hiện popup và không cho bắt đầu
                _log_to_file(f"[DialogueTTS] ❌ Hết credits! Gói {plan_name} còn {current_chars:,} ký tự")
                QtWidgets.QMessageBox.warning(
                    self, 
                    "Hết Credits Gói",
                    f"❌ Gói {plan_name} đã hết credits!\n\n"
                    f"Số ký tự còn lại: {current_chars:,}\n\n"
                    "Vui lòng nâng cấp gói hoặc liên hệ admin để nạp thêm."
                )
                return False
            
            # Còn credits - cho phép bắt đầu
            _log_to_file(f"[DialogueTTS] ✅ Gói {plan_name} còn {current_chars:,} ký tự")
            return True
            
        except Exception as e:
            _log_to_file(f"[DialogueTTS] ⚠️ Check credits error: {e}")
            return True  # Lỗi thì cho phép tiếp tục

    # ========== CREDIT DEDUCTION ==========
    def _deduct_credits_for_dialogue(self):
        """
        Trừ credits sau khi dialogue TTS thành công.
        Tính dựa trên tổng số ký tự đã gen.
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return
            
            # Tính tổng số ký tự từ các lines đã gen
            total_chars = 0
            if self.queue_paths:
                filepath = self.queue_paths[0]
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                # Remove voice tags if present
                                if line.startswith('[') and ']' in line:
                                    line = line[line.index(']')+1:].strip()
                                total_chars += len(line)
                except:
                    pass
            
            if total_chars <= 0:
                _log_to_file("[DialogueTTS] ⚠️ No characters to deduct")
                return
            
            # Nhân hệ số 1.2 (giống main app)
            chars_to_deduct = max(1, int(total_chars * 1.2))
            
            _log_to_file(f"[DialogueTTS] 📝 Deducting {chars_to_deduct:,} chars ({total_chars} raw * 1.2)")
            
            # Optimistic locking với retry
            max_retries = 3
            for attempt in range(max_retries):
                # Get current subscription
                result = self.main_window.supabase.table('user_subscriptions')\
                    .select('id, count_characters')\
                    .eq('user_id', self.main_window.current_user_id)\
                    .eq('is_active', True)\
                    .order('created_at', desc=True)\
                    .limit(1)\
                    .execute()
                
                if not result.data:
                    return
                
                sub = result.data[0]
                sub_id = sub['id']
                current_chars = sub.get('count_characters')
                
                if current_chars is None:
                    return  # No character limit tracking
                
                # Calculate new value
                new_chars = max(0, int(current_chars) - chars_to_deduct)
                
                # Update với optimistic locking
                update_result = self.main_window.supabase.table('user_subscriptions')\
                    .update({'count_characters': new_chars})\
                    .eq('id', sub_id)\
                    .eq('count_characters', current_chars)\
                    .execute()
                
                if update_result.data:
                    _log_to_file(f"[DialogueTTS] ✅ Deducted {chars_to_deduct:,} chars → {new_chars:,} remaining")
                    
                    # Update main window subscription label
                    if hasattr(self.main_window, '_update_subscription_label'):
                        from PySide6 import QtCore
                        QtCore.QTimer.singleShot(100, self.main_window._update_subscription_label)
                    
                    # Check if credits exhausted
                    if new_chars <= 0:
                        _log_to_file("[DialogueTTS] ❌ Credits exhausted after dialogue!")
                        QtWidgets.QMessageBox.warning(
                            self,
                            "Hết Credits",
                            "❌ Gói của bạn đã hết credits sau khi gen dialogue!\n\n"
                            "Vui lòng nâng cấp gói hoặc liên hệ admin."
                        )
                    return
                else:
                    # Conflict - retry
                    if attempt < max_retries - 1:
                        _log_to_file(f"[DialogueTTS] ⚠️ Retry {attempt + 1}/{max_retries} (conflict)")
                        import time
                        time.sleep(0.1)
                        continue
                    else:
                        _log_to_file(f"[DialogueTTS] ⚠️ Failed to deduct after {max_retries} retries")
                        return
                        
        except Exception as e:
            _log_to_file(f"[DialogueTTS] ⚠️ Deduct credits error: {e}")
