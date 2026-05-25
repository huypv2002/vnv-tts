"""
Dubbing Tab for Qt MainWindow (11Labs0811.py).
Dub video/audio sang ngôn ngữ khác sử dụng ElevenLabs Dubbing API.

Flow:
1. User chọn file MP3/MP4
2. Nếu MP3 → convert sang MP4 (ffmpeg + black video)
3. Upload lên ElevenLabs Dubbing API
4. Poll status mỗi 5s
5. Download kết quả khi hoàn thành
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, QThread, Signal, QThreadPool, QRunnable, QObject
from PySide6.QtWidgets import QHeaderView, QFileDialog, QMessageBox
from typing import Optional, Dict, List, Callable
import os
import json
import time
import requests
import subprocess
import uuid
import threading
import shutil
import tempfile
from datetime import datetime

# Language codes for dubbing (29 languages supported by ElevenLabs)
# Source: https://elevenlabs.io/docs/overview/capabilities/dubbing
LANGUAGES = {
    "auto": "Tự động phát hiện",
    "en": "English",
    "hi": "हिन्दी (Hindi)",
    "pt": "Português (Portuguese)",
    "zh": "中文 (Chinese)",
    "es": "Español (Spanish)",
    "fr": "Français (French)",
    "de": "Deutsch (German)",
    "ja": "日本語 (Japanese)",
    "ar": "العربية (Arabic)",
    "ru": "Русский (Russian)",
    "ko": "한국어 (Korean)",
    "id": "Bahasa Indonesia",
    "it": "Italiano (Italian)",
    "nl": "Nederlands (Dutch)",
    "tr": "Türkçe (Turkish)",
    "pl": "Polski (Polish)",
    "sv": "Svenska (Swedish)",
    "fil": "Filipino",
    "ms": "Bahasa Melayu (Malay)",
    "ro": "Română (Romanian)",
    "uk": "Українська (Ukrainian)",
    "el": "Ελληνικά (Greek)",
    "cs": "Čeština (Czech)",
    "da": "Dansk (Danish)",
    "fi": "Suomi (Finnish)",
    "bg": "Български (Bulgarian)",
    "hr": "Hrvatski (Croatian)",
    "sk": "Slovenčina (Slovak)",
    "ta": "தமிழ் (Tamil)",
    "vi": "Tiếng Việt",  # Note: Vietnamese may not be officially supported yet
}

# Target languages (không có auto)
TARGET_LANGUAGES = {k: v for k, v in LANGUAGES.items() if k != "auto"}


def _get_app_dir() -> str:
    """Get application directory."""
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _log(msg: str):
    """Log message."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [Dubbing] {msg}")


# ========== AUDIO SPLITTER ==========
class AudioSplitter:
    """Split audio/video into chunks using ffmpeg."""
    
    def __init__(self, ffmpeg_path: str = None):
        self.ffmpeg = ffmpeg_path or self._find_ffmpeg()
        self.ffprobe = self.ffmpeg.replace('ffmpeg', 'ffprobe') if self.ffmpeg else None
    
    def _find_ffmpeg(self) -> Optional[str]:
        """Find ffmpeg binary."""
        app_dir = _get_app_dir()
        for name in ['ffmpeg', 'ffmpeg.exe']:
            path = os.path.join(app_dir, name)
            if os.path.exists(path):
                return path
        return shutil.which('ffmpeg')
    
    def get_duration(self, file_path: str) -> float:
        """Get file duration in seconds using ffprobe."""
        if not self.ffprobe:
            return 0
        
        try:
            result = subprocess.run(
                [self.ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', file_path],
                capture_output=True, text=True, timeout=30
            )
            return float(result.stdout.strip())
        except Exception as e:
            _log(f"Get duration error: {e}")
            return 0
    
    def split(self, input_path: str, chunk_duration: int = 300, 
              output_dir: str = None) -> List[str]:
        """
        Split file into chunks.
        
        Args:
            input_path: Path to input file
            chunk_duration: Duration per chunk in seconds (default 5 min)
            output_dir: Output directory for chunks
            
        Returns:
            List of chunk file paths
        """
        if not self.ffmpeg:
            _log("ffmpeg not found!")
            return []
        
        # Create output directory
        if not output_dir:
            output_dir = os.path.join(os.path.dirname(input_path), '_chunks')
        os.makedirs(output_dir, exist_ok=True)
        
        # Get file extension
        ext = os.path.splitext(input_path)[1].lower()
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        
        # Split using ffmpeg segment
        output_pattern = os.path.join(output_dir, f"{base_name}_chunk_%03d{ext}")
        
        try:
            cmd = [
                self.ffmpeg, '-y',
                '-i', input_path,
                '-f', 'segment',
                '-segment_time', str(chunk_duration),
                '-c', 'copy',
                '-reset_timestamps', '1',
                output_pattern
            ]
            
            _log(f"Splitting: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode != 0:
                _log(f"Split error: {result.stderr}")
                return []
            
            # Find generated chunks
            chunks = []
            for i in range(1000):  # Max 1000 chunks
                chunk_path = os.path.join(output_dir, f"{base_name}_chunk_{i:03d}{ext}")
                if os.path.exists(chunk_path):
                    chunks.append(chunk_path)
                else:
                    break
            
            _log(f"Split into {len(chunks)} chunks")
            return chunks
            
        except Exception as e:
            _log(f"Split error: {e}")
            return []


# ========== CHUNK WORKER SIGNALS ==========
class ChunkWorkerSignals(QObject):
    """Signals for ChunkDubbingWorker."""
    chunk_progress = Signal(int, str, int)  # chunk_idx, status, percent
    chunk_done = Signal(int, bool, str, str)  # chunk_idx, success, output_path, error
    key_rotated = Signal(int, str)  # chunk_idx, new_key_prefix


# ========== CHUNK DUBBING WORKER ==========
class ChunkDubbingWorker(QRunnable):
    """
    Worker to dub a single chunk.
    Acquires its own key from pool.
    """
    
    def __init__(self, chunk_idx: int, chunk_path: str, source_lang: str, target_lang: str,
                 keys_pool, proxy_getter: Callable, signals: ChunkWorkerSignals,
                 key_lock: threading.Lock, stop_flag_ref: Callable,
                 num_speakers: int = 0, watermark: bool = True,
                 drop_background: bool = False, highest_res: bool = False,
                 proxy_rotator: Callable = None, export_mp4: bool = True,
                 remove_watermark: bool = False, original_video_path: str = None):
        super().__init__()
        self.chunk_idx = chunk_idx
        self.chunk_path = chunk_path
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.keys_pool = keys_pool
        self.proxy_getter = proxy_getter
        self.proxy_rotator = proxy_rotator  # Function to force rotate proxy
        self.signals = signals
        self._key_lock = key_lock
        self._stop_flag_ref = stop_flag_ref
        self.num_speakers = num_speakers
        self.watermark = watermark
        self.drop_background = drop_background
        self.highest_res = highest_res
        self.export_mp4 = export_mp4  # 🔧 NEW: Flag to keep MP4 file
        self.remove_watermark = remove_watermark  # 🔧 NEW: Flag to remove watermark
        self.original_video_path = original_video_path  # 🔧 NEW: Original video for watermark removal
        
        self._current_key = None
        self._key_rotate_count = 0
        self._max_key_rotates = 30
        self.dubbing_id = None
        
        self.setAutoDelete(False)
    
    def _log(self, msg: str):
        _log(f"[Chunk {self.chunk_idx + 1}] {msg}")
    
    def _should_stop(self) -> bool:
        if self._stop_flag_ref and callable(self._stop_flag_ref):
            return self._stop_flag_ref()
        return False
    
    def _get_api_key(self) -> str:
        """Acquire API key from pool."""
        with self._key_lock:
            if self.keys_pool and hasattr(self.keys_pool, 'cur'):
                return self.keys_pool.cur()
        return None
    
    def _rotate_key(self) -> str:
        """Rotate to next API key."""
        with self._key_lock:
            if self.keys_pool and hasattr(self.keys_pool, 'rotate'):
                self.keys_pool.rotate()
            return self._get_api_key()
    
    def _get_proxy(self) -> str:
        if self.proxy_getter and callable(self.proxy_getter):
            try:
                return self.proxy_getter()
            except:
                pass
        return None
    
    def run(self):
        """Dub this chunk."""
        self._log(f"Starting: {os.path.basename(self.chunk_path)}")
        self.signals.chunk_progress.emit(self.chunk_idx, "uploading", 10)
        
        # Get initial key
        self._current_key = self._get_api_key()
        if not self._current_key:
            self.signals.chunk_done.emit(self.chunk_idx, False, "", "No API key")
            return
        
        # Create dubbing with retry
        result = self._create_dubbing_with_retry()
        if not result.get('success'):
            self.signals.chunk_done.emit(self.chunk_idx, False, "", result.get('error', 'Upload failed'))
            return
        
        self.dubbing_id = result.get('dubbing_id')
        self._log(f"Created dubbing: {self.dubbing_id}")
        
        # Poll status
        self.signals.chunk_progress.emit(self.chunk_idx, "dubbing", 30)
        poll_count = 0
        max_polls = 180  # 15 minutes max for a chunk
        
        while not self._should_stop() and poll_count < max_polls:
            status_result = self._get_status()
            
            if status_result.get('status') == 'dubbed':
                # Download result
                self.signals.chunk_progress.emit(self.chunk_idx, "downloading", 90)
                output_path = self._download_result()
                
                if output_path:
                    self.signals.chunk_progress.emit(self.chunk_idx, "done", 100)
                    self.signals.chunk_done.emit(self.chunk_idx, True, output_path, "")
                else:
                    self.signals.chunk_done.emit(self.chunk_idx, False, "", "Download failed")
                return
            
            elif status_result.get('status') == 'failed':
                self.signals.chunk_done.emit(self.chunk_idx, False, "", "Dubbing failed")
                return
            
            # Update progress
            progress = min(30 + (poll_count * 55 // max_polls), 85)
            self.signals.chunk_progress.emit(self.chunk_idx, "dubbing", progress)
            
            time.sleep(5)
            poll_count += 1
        
        if self._should_stop():
            self.signals.chunk_done.emit(self.chunk_idx, False, "", "Stopped")
        else:
            self.signals.chunk_done.emit(self.chunk_idx, False, "", "Timeout")
    
    def _create_dubbing_with_retry(self) -> Dict:
        """Create dubbing with retry on key errors and connection errors."""
        for retry in range(self._max_key_rotates):
            if self._should_stop():
                return {'success': False, 'error': 'Stopped'}
            
            if not self._current_key:
                return {'success': False, 'error': 'No API key'}
            
            result = self._create_dubbing()
            
            if result.get('success'):
                return result
            
            error = result.get('error', '')
            error_code = result.get('error_code', '')
            
            # Connection errors - retry with delay
            if error_code == 'exception':
                error_lower = error.lower()
                if any(x in error_lower for x in ['connection', 'timeout', 'reset', 'refused', 'network', 'socket', 'ssl', 'eof']):
                    self._log(f"⚠️ Connection error, retrying ({retry + 1}/{self._max_key_rotates})...")
                    time.sleep(5)
                    continue
            
            # ⚠️ CRITICAL: Check unusual_activity FIRST - it's a PROXY issue, NOT key issue!
            # Even if error_code is 401, if message contains unusual_activity, it's proxy problem
            if 'unusual_activity' in error.lower():
                self._log(f"⚠️ [Chunk {self.chunk_idx + 1}] Unusual activity - PROXY issue, rotating proxy...")
                # Force proxy rotation
                if self.proxy_rotator and callable(self.proxy_rotator):
                    try:
                        self.proxy_rotator()
                        new_proxy = self._get_proxy()
                        self._log(f"✅ [Proxy rotated] Got: {new_proxy[:50] if new_proxy else 'None'}...")
                    except Exception as e:
                        self._log(f"⚠️ Proxy rotation error: {e}")
                time.sleep(3)
                continue
            
            # Key errors - rotate (but NOT for unusual_activity which was handled above)
            if error_code in ['401', '402', '422'] or 'invalid_api_key' in error.lower():
                self._log(f"Key error {error_code}, rotating...")
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                self.signals.key_rotated.emit(self.chunk_idx, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(1)
                continue
            
            # Rate limit / concurrent limit - đổi key
            if error_code == '429' or 'too_many_concurrent' in error.lower():
                self._log(f"🔄 Rate limit / concurrent limit (429), rotating key...")
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                self.signals.key_rotated.emit(self.chunk_idx, self._current_key[:8] + "..." if self._current_key else "")
                time.sleep(2)
                continue
            
            if 'quota_exceeded' in error.lower():
                self._log("Quota exceeded, rotating key...")
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                time.sleep(1)
                continue
            
            # Server errors (5xx) - retry
            if error_code.startswith('5'):
                self._log(f"⚠️ Server error {error_code}, retrying...")
                time.sleep(5)
                continue
            
            return result
        
        return {'success': False, 'error': 'Max retries exceeded'}
    
    def _create_dubbing(self) -> Dict:
        """Create dubbing project via API."""
        url = "https://api.elevenlabs.io/v1/dubbing"
        headers = {"xi-api-key": self._current_key}
        
        proxy_url = self._get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        
        if proxy_url:
            self._log(f"🌐 Using proxy: {proxy_url[:50]}...")
        else:
            self._log(f"⚠️ NO PROXY - may trigger unusual_activity")
        
        try:
            with open(self.chunk_path, 'rb') as f:
                files = {'file': (os.path.basename(self.chunk_path), f, 'video/mp4')}
                data = {
                    'target_lang': self.target_lang,
                    'source_lang': self.source_lang,
                    'num_speakers': str(self.num_speakers),
                    'watermark': 'true' if self.watermark else 'false',
                    'drop_background_audio': 'true' if self.drop_background else 'false',
                    'highest_resolution': 'true' if self.highest_res else 'false',
                }
                
                response = requests.post(url, headers=headers, files=files, data=data,
                                        proxies=proxies, timeout=300)
            
            if response.status_code == 200:
                result = response.json()
                return {'success': True, 'dubbing_id': result.get('dubbing_id')}
            else:
                error_code = str(response.status_code)
                error_msg = response.text[:200]
                try:
                    detail = response.json().get('detail', {})
                    if isinstance(detail, dict):
                        error_msg = detail.get('message', error_msg)
                    elif isinstance(detail, str):
                        error_msg = detail
                except:
                    pass
                self._log(f"API Error {error_code}: {error_msg[:100]}")
                return {'success': False, 'error': error_msg, 'error_code': error_code}
                
        except Exception as e:
            self._log(f"Exception: {e}")
            return {'success': False, 'error': str(e), 'error_code': 'exception'}
    
    def _get_status(self) -> Dict:
        """Get dubbing status."""
        if not self.dubbing_id:
            return {'status': 'failed'}
        
        url = f"https://api.elevenlabs.io/v1/dubbing/{self.dubbing_id}"
        headers = {"xi-api-key": self._current_key}
        
        proxy_url = self._get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        
        try:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            if response.status_code == 200:
                return response.json()
            return {'status': 'unknown'}
        except:
            return {'status': 'unknown'}
    
    def _download_result(self) -> Optional[str]:
        """Download dubbed result and extract MP3.
        
        🔧 NEW: Nếu export_mp4=True, giữ cả MP4 và MP3. Nếu False, chỉ giữ MP3.
        """
        if not self.dubbing_id:
            return None
        
        url = f"https://api.elevenlabs.io/v1/dubbing/{self.dubbing_id}/audio/{self.target_lang}"
        headers = {"xi-api-key": self._current_key}
        
        proxy_url = self._get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        
        try:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=300, stream=True)
            
            if response.status_code == 200:
                # Save MP4 to same directory as chunk
                output_dir = os.path.dirname(self.chunk_path)
                base_name = os.path.splitext(os.path.basename(self.chunk_path))[0]
                mp4_path = os.path.join(output_dir, f"{base_name}_dubbed.mp4")
                
                with open(mp4_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                self._log(f"Downloaded MP4: {mp4_path}")
                
                # Extract MP3 from MP4
                mp3_path = self._extract_mp3_from_mp4(mp4_path)
                
                if mp3_path:
                    # 🔧 NEW: Chỉ xóa MP4 nếu export_mp4=False
                    if not self.export_mp4:
                        try:
                            os.remove(mp4_path)
                            self._log(f"Deleted MP4: {mp4_path}")
                        except Exception as e:
                            self._log(f"Failed to delete MP4: {e}")
                    else:
                        self._log(f"Kept MP4: {mp4_path}")
                    
                    return mp3_path
                
                return mp4_path  # Fallback if extraction fails
            return None
        except Exception as e:
            self._log(f"Download error: {e}")
            return None
    
    def _extract_mp3_from_mp4(self, mp4_path: str) -> Optional[str]:
        """Extract MP3 audio from MP4 file using ffmpeg."""
        splitter = AudioSplitter()
        if not splitter.ffmpeg:
            self._log("ffmpeg not found, cannot extract MP3")
            return None
        
        # Output path: same name but .mp3 extension
        mp3_path = os.path.splitext(mp4_path)[0] + ".mp3"
        
        try:
            cmd = [
                splitter.ffmpeg, '-y',
                '-i', mp4_path,
                '-vn',  # No video
                '-acodec', 'libmp3lame',
                '-q:a', '2',  # High quality
                mp3_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode == 0 and os.path.exists(mp3_path):
                self._log(f"Extracted MP3: {mp3_path}")
                return mp3_path
            else:
                self._log(f"Extract MP3 failed: {result.stderr}")
                return None
                
        except Exception as e:
            self._log(f"Extract MP3 error: {e}")
            return None
    
    def _remove_watermark(self, original_mp4: str, dubbed_mp3: str) -> Optional[str]:
        """Remove watermark by replacing audio track."""
        splitter = AudioSplitter()
        if not splitter.ffmpeg:
            self._log("ffmpeg not found, cannot remove watermark")
            return None
        
        base_name = os.path.splitext(dubbed_mp3)[0]
        output_mp4 = f"{base_name}_no_watermark.mp4"
        
        try:
            cmd = [
                splitter.ffmpeg, '-y',
                '-i', original_mp4,
                '-i', dubbed_mp3,
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-shortest',
                output_mp4
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0 and os.path.exists(output_mp4):
                self._log(f"✅ Watermark removed: {output_mp4}")
                return output_mp4
            else:
                self._log(f"❌ Watermark removal failed: {result.stderr}")
                return None
                
        except Exception as e:
            self._log(f"❌ Watermark removal error: {e}")
            return None


# ========== PARALLEL DUBBING MANAGER ==========
class ParallelDubbingManager(QThread):
    """
    Orchestrates parallel dubbing of chunks.
    Split → Convert → Parallel Dub → Extract → Merge
    """
    
    overall_progress = Signal(int, int, str)  # completed, total, status
    chunk_status = Signal(int, str, int)  # chunk_idx, status, percent
    finished = Signal(bool, str, str)  # success, message, output_path
    
    def __init__(self, input_path: str, source_lang: str, target_lang: str,
                 keys_pool, proxy_getter: Callable, output_dir: str = None,
                 chunk_duration: int = 300, max_workers: int = 5,
                 num_speakers: int = 0, watermark: bool = True,
                 drop_background: bool = False, highest_res: bool = False,
                 proxy_rotator: Callable = None, export_mp4: bool = True):
        super().__init__()
        self.input_path = input_path
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.keys_pool = keys_pool
        self.proxy_getter = proxy_getter
        self.proxy_rotator = proxy_rotator  # Function to force rotate proxy
        self.output_dir = output_dir or os.path.dirname(input_path)
        self.chunk_duration = chunk_duration
        self.max_workers = max_workers
        self.num_speakers = num_speakers
        self.watermark = watermark
        self.drop_background = drop_background
        self.highest_res = highest_res
        self.export_mp4 = export_mp4  # 🔧 NEW: Flag to keep MP4 file
        
        self._stop = False
        self._key_lock = threading.Lock()
        self._results_lock = threading.Lock()
        
        # Results
        self._chunk_results: Dict[int, str] = {}  # idx -> output_path
        self._chunk_errors: Dict[int, str] = {}
        self._completed_count = 0
        self._total_chunks = 0
        
        # Temp directory
        self._temp_dir = None
        self._chunks: List[str] = []
        self._mp4_chunks: List[str] = []
    
    def stop(self):
        self._stop = True
    
    def _should_stop(self) -> bool:
        return self._stop
    
    def run(self):
        """Main workflow."""
        try:
            splitter = AudioSplitter()
            
            # Step 1: Get duration and check if split needed
            duration = splitter.get_duration(self.input_path)
            _log(f"File duration: {duration:.1f}s ({duration/60:.1f} min)")
            
            if duration <= self.chunk_duration:
                # File is small, no need to split - use regular dubbing
                _log("File is small, using single dubbing")
                self._single_dub()
                return
            
            # Step 2: Create temp directory
            base_name = os.path.splitext(os.path.basename(self.input_path))[0]
            self._temp_dir = os.path.join(self.output_dir, f"_dub_temp_{base_name}")
            os.makedirs(self._temp_dir, exist_ok=True)
            
            # Step 3: Split file
            self.overall_progress.emit(0, 100, "Đang chia file...")
            self._chunks = splitter.split(self.input_path, self.chunk_duration, self._temp_dir)
            
            if not self._chunks:
                self.finished.emit(False, "Không thể chia file", "")
                return
            
            self._total_chunks = len(self._chunks)
            _log(f"Split into {self._total_chunks} chunks")
            
            # Step 4: Convert MP3 chunks to MP4
            self.overall_progress.emit(5, 100, "Đang chuẩn bị...")
            self._mp4_chunks = self._convert_chunks_to_mp4()
            
            if not self._mp4_chunks or self._should_stop():
                self.finished.emit(False, "Không thể convert chunks", "")
                return
            
            # Step 5: Parallel dubbing
            self.overall_progress.emit(15, 100, "Đang dubbing...")
            success = self._parallel_dub()
            
            if not success or self._should_stop():
                self.finished.emit(False, "Dubbing thất bại", "")
                return
            
            # Step 6: Extract audio and merge
            self.overall_progress.emit(90, 100, "Đang ghép file...")
            output_path = self._extract_and_merge()
            
            if not output_path:
                self.finished.emit(False, "Không thể ghép file", "")
                return
            
            # Step 7: Cleanup
            self._cleanup()
            
            self.overall_progress.emit(100, 100, "Hoàn thành!")
            self.finished.emit(True, "Thành công", output_path)
            
        except Exception as e:
            _log(f"ParallelDubbing error: {e}")
            self.finished.emit(False, str(e), "")
    
    def _single_dub(self):
        """Use regular dubbing for small files."""
        # This will be handled by the regular DubbingWorker
        self.finished.emit(False, "USE_SINGLE_DUB", self.input_path)
    
    def _convert_chunks_to_mp4(self) -> List[str]:
        """Convert MP3 chunks to MP4 (with black video)."""
        mp4_chunks = []
        splitter = AudioSplitter()
        
        for i, chunk_path in enumerate(self._chunks):
            if self._should_stop():
                break
            
            ext = os.path.splitext(chunk_path)[1].lower()
            
            if ext == '.mp4':
                # Already MP4
                mp4_chunks.append(chunk_path)
                continue
            
            # Convert MP3 to MP4
            duration = splitter.get_duration(chunk_path)
            mp4_path = chunk_path.replace(ext, '.mp4')
            
            try:
                cmd = [
                    splitter.ffmpeg, '-y',
                    '-f', 'lavfi', '-i', f'color=c=black:s=640x360:d={duration}',
                    '-i', chunk_path,
                    '-c:v', 'libx264', '-c:a', 'aac',
                    '-shortest',
                    mp4_path
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                
                if result.returncode == 0 and os.path.exists(mp4_path):
                    mp4_chunks.append(mp4_path)
                    _log(f"Converted chunk {i+1}/{len(self._chunks)}")
                else:
                    _log(f"Convert chunk {i+1} failed")
                    return []
                    
            except Exception as e:
                _log(f"Convert error: {e}")
                return []
        
        return mp4_chunks
    
    def _parallel_dub(self) -> bool:
        """Dub chunks in parallel using QThreadPool."""
        pool = QThreadPool.globalInstance()
        pool.setMaxThreadCount(self.max_workers)
        
        # Create signals object
        signals = ChunkWorkerSignals()
        signals.chunk_progress.connect(self._on_chunk_progress)
        signals.chunk_done.connect(self._on_chunk_done)
        signals.key_rotated.connect(self._on_key_rotated)
        
        # Create workers
        workers = []
        for idx, chunk_path in enumerate(self._mp4_chunks):
            if self._should_stop():
                break
            
            worker = ChunkDubbingWorker(
                chunk_idx=idx,
                chunk_path=chunk_path,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                keys_pool=self.keys_pool,
                proxy_getter=self.proxy_getter,
                signals=signals,
                key_lock=self._key_lock,
                stop_flag_ref=self._should_stop,
                num_speakers=self.num_speakers,
                watermark=self.watermark,
                drop_background=self.drop_background,
                highest_res=self.highest_res,
                proxy_rotator=self.proxy_rotator,
                export_mp4=self.export_mp4  # 🔧 NEW: Pass export_mp4 flag
            )
            workers.append(worker)
            pool.start(worker)
        
        # Wait for all workers
        pool.waitForDone()
        
        # Check results
        return len(self._chunk_errors) == 0 and len(self._chunk_results) == self._total_chunks
    
    def _on_chunk_progress(self, chunk_idx: int, status: str, percent: int):
        """Handle chunk progress update."""
        self.chunk_status.emit(chunk_idx, status, percent)
        
        # Update overall progress (15-90% range for dubbing)
        if self._total_chunks > 0:
            base_progress = 15
            dub_range = 75  # 15% to 90%
            chunk_contribution = dub_range / self._total_chunks
            overall = base_progress + (self._completed_count * chunk_contribution) + (percent / 100 * chunk_contribution / self._total_chunks)
            self.overall_progress.emit(int(overall), 100, f"Đang dubbing {self._completed_count + 1}/{self._total_chunks}...")
    
    def _on_chunk_done(self, chunk_idx: int, success: bool, output_path: str, error: str):
        """Handle chunk completion."""
        with self._results_lock:
            if success:
                self._chunk_results[chunk_idx] = output_path
                _log(f"Chunk {chunk_idx + 1} done: {output_path}")
            else:
                self._chunk_errors[chunk_idx] = error
                _log(f"Chunk {chunk_idx + 1} failed: {error}")
            
            self._completed_count += 1
    
    def _on_key_rotated(self, chunk_idx: int, new_key: str):
        """Handle key rotation."""
        _log(f"Chunk {chunk_idx + 1} rotated to key: {new_key}")
    
    def _extract_and_merge(self) -> Optional[str]:
        """Merge dubbed audio files (MP3s from chunks)."""
        splitter = AudioSplitter()
        
        # Sort results by chunk index
        sorted_results = sorted(self._chunk_results.items(), key=lambda x: x[0])
        
        # Collect MP3 files - ChunkDubbingWorker now returns MP3 directly
        mp3_files = []
        for idx, file_path in sorted_results:
            # Check if it's already MP3 or needs extraction
            if file_path.endswith('.mp3'):
                mp3_files.append(file_path)
            elif file_path.endswith('.mp4'):
                # Extract MP3 from MP4 (fallback case)
                mp3_path = file_path.replace('.mp4', '.mp3')
                
                try:
                    cmd = [
                        splitter.ffmpeg, '-y',
                        '-i', file_path,
                        '-vn',
                        '-acodec', 'libmp3lame',
                        '-q:a', '2',
                        mp3_path
                    ]
                    
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    
                    if result.returncode == 0 and os.path.exists(mp3_path):
                        mp3_files.append(mp3_path)
                        # Delete MP4 after extraction
                        try:
                            os.remove(file_path)
                            _log(f"Deleted MP4: {file_path}")
                        except:
                            pass
                    else:
                        _log(f"Extract audio failed for chunk {idx}")
                        return None
                        
                except Exception as e:
                    _log(f"Extract error: {e}")
                    return None
            else:
                # Unknown format, try to use as-is
                mp3_files.append(file_path)
        
        if not mp3_files:
            _log("No MP3 files to merge")
            return None
        
        # Create concat file list
        list_path = os.path.join(self._temp_dir, 'concat_list.txt')
        with open(list_path, 'w', encoding='utf-8') as f:
            for mp3_path in mp3_files:
                # Use relative path or escape special chars
                f.write(f"file '{mp3_path}'\n")
        
        # Merge
        base_name = os.path.splitext(os.path.basename(self.input_path))[0]
        if base_name.endswith('_converted'):
            base_name = base_name[:-10]
        output_path = os.path.join(self.output_dir, f"{base_name}_dubbed_{self.target_lang}.mp3")
        
        try:
            cmd = [
                splitter.ffmpeg, '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_path,
                '-c', 'copy',
                output_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0 and os.path.exists(output_path):
                _log(f"Merged to: {output_path}")
                return output_path
            else:
                _log(f"Merge failed: {result.stderr}")
                return None
                
        except Exception as e:
            _log(f"Merge error: {e}")
            return None
    
    def _cleanup(self):
        """Remove temporary files."""
        if self._temp_dir and os.path.exists(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir)
                _log(f"Cleaned up: {self._temp_dir}")
            except Exception as e:
                _log(f"Cleanup error: {e}")


# ========== DUBBING WORKER ==========
class DubbingWorker(QThread):
    """
    Worker thread for dubbing a single file.
    Handles: upload → poll status → download result
    With key pool integration for error handling and rotation.
    """
    
    progress = Signal(str, str, int)  # job_id, status, percent
    status_update = Signal(str, str)  # job_id, status_text
    finished = Signal(str, bool, str, str)  # job_id, success, output_path, error_msg
    key_error = Signal(str, str, str)  # job_id, api_key, error_type (401, quota_exceeded, etc.)
    
    def __init__(self, job_id: str, file_path: str, source_lang: str, target_lang: str,
                 keys_pool, proxy_getter=None, output_dir: str = None,
                 num_speakers: int = 0, watermark: bool = True, 
                 drop_background: bool = False, highest_res: bool = False,
                 proxy_rotator=None, export_mp4: bool = True, remove_watermark: bool = False):
        super().__init__()
        self.job_id = job_id
        self.file_path = file_path
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.keys_pool = keys_pool  # Key pool for rotation
        self.proxy_getter = proxy_getter  # Function to get current proxy
        self.proxy_rotator = proxy_rotator  # Function to force rotate proxy
        self.output_dir = output_dir or os.path.dirname(file_path)
        self.num_speakers = num_speakers
        self.watermark = watermark
        self.drop_background = drop_background
        self.highest_res = highest_res
        self.export_mp4 = export_mp4  # 🔧 NEW: Flag to keep MP4 file
        self.remove_watermark = remove_watermark  # 🔧 NEW: Flag to remove watermark
        
        self._stop = False
        self.dubbing_id = None
        self._current_key = None
        self._key_rotate_count = 0
        self._max_key_rotates = 50
    
    def stop(self):
        self._stop = True
    
    def _get_api_key(self) -> str:
        """Get current API key from pool."""
        if self.keys_pool:
            if hasattr(self.keys_pool, 'cur'):
                return self.keys_pool.cur()
        return None
    
    def _rotate_key(self) -> str:
        """Rotate to next API key."""
        if self.keys_pool:
            if hasattr(self.keys_pool, 'rotate'):
                self.keys_pool.rotate()
            return self._get_api_key()
        return None
    
    def _mark_key_error(self, api_key: str, error_type: str):
        """Mark key as having error (401, quota_exceeded, etc.)."""
        if self.keys_pool and api_key:
            if hasattr(self.keys_pool, 'mark_401') and error_type == "401":
                self.keys_pool.mark_401(api_key)
            self.key_error.emit(self.job_id, api_key[:8] + "...", error_type)
    
    def _get_proxy(self) -> str:
        """Get current proxy URL."""
        if self.proxy_getter and callable(self.proxy_getter):
            try:
                return self.proxy_getter()
            except:
                pass
        return None
    
    def run(self):
        try:
            # Get initial API key
            self._current_key = self._get_api_key()
            if not self._current_key:
                self.finished.emit(self.job_id, False, "", "Không có API key!")
                return
            
            # Step 1: Create dubbing project (with retry on key errors)
            self.status_update.emit(self.job_id, "Đang upload...")
            self.progress.emit(self.job_id, "uploading", 10)
            
            result = self._create_dubbing_with_retry()
            if not result.get('success'):
                self.finished.emit(self.job_id, False, "", result.get('error', 'Unknown error'))
                return
            
            self.dubbing_id = result.get('dubbing_id')
            expected_duration = result.get('expected_duration_sec', 0)
            _log(f"Created dubbing: {self.dubbing_id}, expected: {expected_duration}s")
            
            # Step 2: Poll status
            self.status_update.emit(self.job_id, "Đang xử lý...")
            self.progress.emit(self.job_id, "dubbing", 20)
            
            poll_count = 0
            max_polls = 360  # 30 minutes max (5s interval)
            
            while not self._stop and poll_count < max_polls:
                status_result = self._get_status()
                if not status_result.get('success'):
                    _log(f"Status check failed: {status_result.get('error')}")
                    time.sleep(5)
                    poll_count += 1
                    continue
                
                status = status_result.get('status', '')
                _log(f"Status: {status}")
                
                if status == 'dubbed':
                    # Success - download result
                    self.status_update.emit(self.job_id, "Đang tải xuống...")
                    self.progress.emit(self.job_id, "downloading", 90)
                    
                    output_path = self._download_result()
                    if output_path:
                        self.progress.emit(self.job_id, "done", 100)
                        self.finished.emit(self.job_id, True, output_path, "")
                    else:
                        self.finished.emit(self.job_id, False, "", "Download failed")
                    return
                
                elif status == 'failed':
                    error = status_result.get('error', 'Dubbing failed')
                    self.finished.emit(self.job_id, False, "", error)
                    return
                
                # Update progress (estimate based on poll count)
                progress_pct = min(20 + (poll_count * 70 // max_polls), 85)
                self.progress.emit(self.job_id, "dubbing", progress_pct)
                self.status_update.emit(self.job_id, f"Đang xử lý... ({status})")
                
                time.sleep(5)
                poll_count += 1
            
            if self._stop:
                self.finished.emit(self.job_id, False, "", "Stopped by user")
            else:
                self.finished.emit(self.job_id, False, "", "Timeout - dubbing took too long")
                
        except Exception as e:
            _log(f"Worker error: {e}")
            self.finished.emit(self.job_id, False, "", str(e))
    
    def _create_dubbing_with_retry(self) -> Dict:
        """Create dubbing project with retry on key errors and connection errors."""
        max_retries = self._max_key_rotates
        
        for retry in range(max_retries):
            if self._stop:
                return {'success': False, 'error': 'Stopped by user'}
            
            if not self._current_key:
                return {'success': False, 'error': 'Không có API key!'}
            
            result = self._create_dubbing()
            
            if result.get('success'):
                return result
            
            # Check for key errors
            error = result.get('error', '')
            error_code = result.get('error_code', '')
            
            # Connection errors - retry with delay
            if error_code == 'exception':
                error_lower = error.lower()
                if any(x in error_lower for x in ['connection', 'timeout', 'reset', 'refused', 'network', 'socket', 'ssl', 'eof']):
                    _log(f"⚠️ Connection error, retrying ({retry + 1}/{max_retries})...")
                    self.status_update.emit(self.job_id, f"Lỗi kết nối, thử lại ({retry + 1})...")
                    time.sleep(5)
                    continue
            
            # ⚠️ CRITICAL: Check unusual_activity FIRST - it's a PROXY issue, NOT key issue!
            # Even if error_code is 401, if message contains unusual_activity, it's proxy problem
            if 'unusual_activity' in error.lower():
                _log(f"⚠️ Unusual activity detected - PROXY issue, NOT key issue! Rotating proxy...")
                self.status_update.emit(self.job_id, "Lỗi proxy, đang xoay proxy...")
                # Force proxy rotation
                if self.proxy_rotator and callable(self.proxy_rotator):
                    try:
                        self.proxy_rotator()
                        new_proxy = self._get_proxy()
                        _log(f"✅ [Proxy rotated] Got: {new_proxy[:50] if new_proxy else 'None'}...")
                    except Exception as e:
                        _log(f"⚠️ Proxy rotation error: {e}")
                time.sleep(3)
                continue
            
            # Key errors - rotate (but NOT for unusual_activity which was handled above)
            if error_code in ['401', '402', '422'] or 'invalid_api_key' in error.lower():
                _log(f"🔄 Key error {error_code}, rotating... ({self._key_rotate_count + 1}/{max_retries})")
                self._mark_key_error(self._current_key, error_code or '401')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                self.status_update.emit(self.job_id, f"Đổi key... ({self._key_rotate_count})")
                time.sleep(1)
                continue
            
            elif error_code == '429' or 'rate' in error.lower() or 'too_many_concurrent' in error.lower():
                # 429 hoặc too_many_concurrent_requests - đổi key
                _log(f"🔄 Rate limit / concurrent limit (429), rotating key...")
                self._mark_key_error(self._current_key, '429')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                self.status_update.emit(self.job_id, f"Đổi key (429)... ({self._key_rotate_count})")
                time.sleep(2)
                continue
            
            elif 'quota_exceeded' in error.lower():
                _log(f"🔄 Quota exceeded, rotating key...")
                self._mark_key_error(self._current_key, 'quota_exceeded')
                self._key_rotate_count += 1
                self._current_key = self._rotate_key()
                self.status_update.emit(self.job_id, f"Đổi key... ({self._key_rotate_count})")
                time.sleep(1)
                continue
            
            # Server errors (5xx) - retry
            elif error_code.startswith('5'):
                _log(f"⚠️ Server error {error_code}, retrying...")
                self.status_update.emit(self.job_id, f"Lỗi server, thử lại...")
                time.sleep(5)
                continue
            
            # Other errors - don't retry
            return result
        
        return {'success': False, 'error': f'Đã thử {max_retries} lần, không thành công'}
    
    def _create_dubbing(self) -> Dict:
        """Create dubbing project via API."""
        url = "https://api.elevenlabs.io/v1/dubbing"
        
        headers = {
            "xi-api-key": self._current_key
        }
        
        proxy_url = self._get_proxy()
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
            _log(f"🌐 Using proxy: {proxy_url[:50]}...")
        else:
            _log(f"⚠️ NO PROXY - calling API directly (may trigger unusual_activity)")
        
        try:
            with open(self.file_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(self.file_path), f, 'video/mp4')
                }
                
                data = {
                    'target_lang': self.target_lang,
                    'source_lang': self.source_lang,
                    'num_speakers': str(self.num_speakers),
                    'watermark': 'true' if self.watermark else 'false',
                    'drop_background_audio': 'true' if self.drop_background else 'false',
                    'highest_resolution': 'true' if self.highest_res else 'false',
                }
                
                _log(f"Creating dubbing: {data}")
                
                response = requests.post(
                    url, 
                    headers=headers, 
                    files=files, 
                    data=data,
                    proxies=proxies,
                    timeout=300  # 5 minutes for upload
                )
            
            _log(f"Create response: {response.status_code} - {response.text[:500]}")
            
            if response.status_code == 200:
                result = response.json()
                return {
                    'success': True,
                    'dubbing_id': result.get('dubbing_id'),
                    'expected_duration_sec': result.get('expected_duration_sec')
                }
            else:
                # Parse error details
                error_code = str(response.status_code)
                error_msg = response.text[:200]
                
                try:
                    detail = response.json().get('detail', {})
                    if isinstance(detail, dict):
                        error_msg = detail.get('message', error_msg)
                        status = detail.get('status', '')
                        if status:
                            error_msg = f"{status}: {error_msg}"
                    elif isinstance(detail, str):
                        error_msg = detail
                except:
                    pass
                
                return {
                    'success': False,
                    'error': error_msg,
                    'error_code': error_code
                }
                
        except Exception as e:
            return {'success': False, 'error': str(e), 'error_code': 'exception'}
    
    def _get_status(self) -> Dict:
        """Get dubbing status."""
        if not self.dubbing_id:
            return {'success': False, 'error': 'No dubbing_id'}
        
        url = f"https://api.elevenlabs.io/v1/dubbing/{self.dubbing_id}"
        headers = {"xi-api-key": self._current_key}
        
        proxy_url = self._get_proxy()
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        
        try:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                return {
                    'success': True,
                    'status': result.get('status'),
                    'error': result.get('error')
                }
            else:
                return {'success': False, 'error': f"Status API Error: {response.status_code}"}
                
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def _download_result(self) -> Optional[str]:
        """Download dubbed audio and extract MP3.
        
        🔧 NEW: Nếu export_mp4=True, giữ cả MP4 và MP3. Nếu False, chỉ giữ MP3.
        """
        if not self.dubbing_id:
            return None
        
        url = f"https://api.elevenlabs.io/v1/dubbing/{self.dubbing_id}/audio/{self.target_lang}"
        headers = {"xi-api-key": self._current_key}
        
        proxy_url = self._get_proxy()
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        
        try:
            response = requests.get(url, headers=headers, proxies=proxies, timeout=300, stream=True)
            
            if response.status_code == 200:
                # Generate output filename
                base_name = os.path.splitext(os.path.basename(self.file_path))[0]
                # Remove _converted suffix if present
                if base_name.endswith('_converted'):
                    base_name = base_name[:-10]
                
                mp4_name = f"{base_name}_dubbed_{self.target_lang}.mp4"
                mp4_path = os.path.join(self.output_dir, mp4_name)
                
                with open(mp4_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                _log(f"Downloaded MP4: {mp4_path}")
                
                # Extract MP3 from MP4
                mp3_path = self._extract_mp3_from_mp4(mp4_path)
                if mp3_path:
                    _log(f"Extracted MP3: {mp3_path}")
                    
                    # 🔧 NEW: Remove watermark if enabled and input is MP4
                    final_output = mp3_path
                    if self.remove_watermark and self.file_path.lower().endswith('.mp4'):
                        _log("🎯 Removing watermark by replacing audio track...")
                        no_wm_mp4 = self._remove_watermark(self.file_path, mp3_path)
                        if no_wm_mp4:
                            _log(f"✅ Watermark removed: {no_wm_mp4}")
                            final_output = no_wm_mp4
                            # Delete watermarked MP4 from API
                            try:
                                os.remove(mp4_path)
                                _log(f"Deleted watermarked MP4: {mp4_path}")
                            except Exception as e:
                                _log(f"Failed to delete watermarked MP4: {e}")
                        else:
                            _log("⚠️ Watermark removal failed, keeping original outputs")
                    
                    # 🔧 Chỉ xóa MP4 nếu export_mp4=False (và không remove watermark)
                    elif not self.export_mp4:
                        try:
                            os.remove(mp4_path)
                            _log(f"Deleted MP4: {mp4_path}")
                        except Exception as e:
                            _log(f"Failed to delete MP4: {e}")
                    else:
                        _log(f"Kept MP4: {mp4_path}")
                    
                    return final_output
                
                return mp4_path  # Fallback to MP4 if extraction fails
            else:
                _log(f"Download failed: {response.status_code}")
                return None
                
        except Exception as e:
            _log(f"Download error: {e}")
            return None
    
    def _extract_mp3_from_mp4(self, mp4_path: str) -> Optional[str]:
        """Extract MP3 audio from MP4 file using ffmpeg."""
        ffmpeg_bin = self._find_ffmpeg()
        if not ffmpeg_bin:
            _log("ffmpeg not found, cannot extract MP3")
            return None
        
        # Output path: same name but .mp3 extension
        mp3_path = os.path.splitext(mp4_path)[0] + ".mp3"
        
        try:
            cmd = [
                ffmpeg_bin, '-y',
                '-i', mp4_path,
                '-vn',  # No video
                '-acodec', 'libmp3lame',
                '-q:a', '2',  # High quality
                mp3_path
            ]
            
            _log(f"Extracting MP3: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0 and os.path.exists(mp3_path):
                _log(f"Extracted MP3: {mp3_path}")
                return mp3_path
            else:
                _log(f"Extract MP3 failed: {result.stderr}")
                return None
                
        except Exception as e:
            _log(f"Extract MP3 error: {e}")
            return None
    
    def _remove_watermark(self, original_mp4: str, dubbed_mp3: str) -> Optional[str]:
        """
        Remove watermark by replacing audio track of original MP4 with dubbed MP3.
        
        🔧 WATERMARK REMOVAL TECHNIQUE:
        - ElevenLabs free tier adds watermark to audio track
        - We replace original video's audio with dubbed audio (no watermark)
        - Use ffmpeg copy mode for video (fast, no re-encoding)
        
        Args:
            original_mp4: Path to original MP4 file (input)
            dubbed_mp3: Path to dubbed MP3 audio
        
        Returns:
            Path to output MP4 (no watermark) or None if failed
        """
        ffmpeg_bin = self._find_ffmpeg()
        if not ffmpeg_bin:
            _log("ffmpeg not found, cannot remove watermark")
            return None
        
        # Output path: add _no_watermark suffix
        base_name = os.path.splitext(dubbed_mp3)[0]  # Remove .mp3
        output_mp4 = f"{base_name}_no_watermark.mp4"
        
        try:
            # ffmpeg command:
            # -i original.mp4 (video source)
            # -i dubbed.mp3 (audio source)
            # -c:v copy (copy video without re-encoding - FAST)
            # -c:a aac (encode audio to AAC for MP4 compatibility)
            # -map 0:v:0 (use video from first input)
            # -map 1:a:0 (use audio from second input)
            # -shortest (match shortest stream duration)
            cmd = [
                ffmpeg_bin, '-y',
                '-i', original_mp4,  # Video source
                '-i', dubbed_mp3,    # Audio source
                '-c:v', 'copy',      # Copy video (no re-encode)
                '-c:a', 'aac',       # Encode audio to AAC
                '-b:a', '192k',      # Audio bitrate
                '-map', '0:v:0',     # Map video from input 0
                '-map', '1:a:0',     # Map audio from input 1
                '-shortest',         # Match shortest duration
                output_mp4
            ]
            
            _log(f"Removing watermark: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0 and os.path.exists(output_mp4):
                _log(f"✅ Watermark removed: {output_mp4}")
                return output_mp4
            else:
                _log(f"❌ Watermark removal failed: {result.stderr}")
                return None
                
        except Exception as e:
            _log(f"❌ Watermark removal error: {e}")
            return None
    
    def _find_ffmpeg(self) -> Optional[str]:
        """Find ffmpeg binary."""
        import shutil
        
        # Check in app directory
        app_dir = _get_app_dir()
        for name in ['ffmpeg', 'ffmpeg.exe']:
            path = os.path.join(app_dir, name)
            if os.path.exists(path):
                return path
        
        # Check in PATH
        return shutil.which('ffmpeg')


# ========== DUBBING TAB UI ==========
class DubbingTab(QtWidgets.QWidget):
    """
    Tab for Dubbing - uses ElevenLabs Dubbing API.
    """
    
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.jobs: List[Dict] = []  # List of dubbing jobs
        self.workers: Dict[str, DubbingWorker] = {}  # Active workers
        self._setup_ui()
        self._load_jobs()
    
    def _setup_ui(self):
        """Setup the dubbing tab UI."""
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)
        
        # ========== TOP ROW: Input + Options ==========
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(10)
        
        # === INPUT SECTION ===
        grp_input = QtWidgets.QGroupBox("Input")
        input_layout = QtWidgets.QVBoxLayout(grp_input)
        input_layout.setSpacing(5)
        
        # File path
        file_row = QtWidgets.QHBoxLayout()
        self.txt_file = QtWidgets.QLineEdit()
        self.txt_file.setPlaceholderText("Chọn file video/audio...")
        self.txt_file.setReadOnly(True)
        
        self.btn_browse = QtWidgets.QPushButton("📁 Chọn File")
        self.btn_browse.clicked.connect(self._browse_file)
        
        file_row.addWidget(self.txt_file, 1)
        file_row.addWidget(self.btn_browse)
        input_layout.addLayout(file_row)
        
        # Convert button (for MP3)
        self.btn_convert = QtWidgets.QPushButton("🔄 Convert MP3 → MP4")
        self.btn_convert.setToolTip("Chuyển file MP3 sang MP4 (video đen) để dub với free tier")
        self.btn_convert.clicked.connect(self._convert_mp3_to_mp4)
        self.btn_convert.setEnabled(False)
        input_layout.addWidget(self.btn_convert)
        
        # File info
        self.lbl_file_info = QtWidgets.QLabel("")
        self.lbl_file_info.setStyleSheet("color: #666; font-size: 10px;")
        input_layout.addWidget(self.lbl_file_info)
        
        input_layout.addStretch()
        grp_input.setMinimumWidth(350)
        top_row.addWidget(grp_input)
        
        # === OPTIONS SECTION ===
        grp_options = QtWidgets.QGroupBox("Tùy chọn")
        opt_layout = QtWidgets.QGridLayout(grp_options)
        opt_layout.setHorizontalSpacing(10)
        opt_layout.setVerticalSpacing(5)
        
        # Source language
        opt_layout.addWidget(QtWidgets.QLabel("Ngôn ngữ gốc:"), 0, 0)
        self.cmb_source = QtWidgets.QComboBox()
        for code, name in LANGUAGES.items():
            self.cmb_source.addItem(f"{name} ({code})", code)
        self.cmb_source.setCurrentIndex(0)  # auto
        opt_layout.addWidget(self.cmb_source, 0, 1)
        
        # Target language
        opt_layout.addWidget(QtWidgets.QLabel("Ngôn ngữ đích:"), 1, 0)
        self.cmb_target = QtWidgets.QComboBox()
        for code, name in TARGET_LANGUAGES.items():
            self.cmb_target.addItem(f"{name} ({code})", code)
        self.cmb_target.setCurrentIndex(0)  # vi
        opt_layout.addWidget(self.cmb_target, 1, 1)
        
        # Num speakers
        opt_layout.addWidget(QtWidgets.QLabel("Số người nói:"), 2, 0)
        self.spn_speakers = QtWidgets.QSpinBox()
        self.spn_speakers.setRange(0, 10)
        self.spn_speakers.setValue(0)
        self.spn_speakers.setToolTip("0 = tự động phát hiện")
        opt_layout.addWidget(self.spn_speakers, 2, 1)
        
        # Checkboxes
        self.chk_watermark = QtWidgets.QCheckBox("Xóa Watermark (chỉ MP4)")
        self.chk_watermark.setChecked(True)  # Mặc định tick
        self.chk_watermark.setToolTip("Thay thế audio track của video gốc bằng audio đã dubbing để xóa watermark.\nChỉ hoạt động khi input là MP4.")
        self.chk_watermark.setEnabled(False)  # Sẽ enable khi chọn MP4
        opt_layout.addWidget(self.chk_watermark, 3, 0, 1, 2)
        
        self.chk_drop_bg = QtWidgets.QCheckBox("Bỏ nhạc nền")
        self.chk_drop_bg.setToolTip("Bỏ background audio, phù hợp cho speech/monologue")
        opt_layout.addWidget(self.chk_drop_bg, 4, 0, 1, 2)
        
        self.chk_high_res = QtWidgets.QCheckBox("Chất lượng cao")
        self.chk_high_res.setToolTip("Sử dụng độ phân giải cao nhất")
        opt_layout.addWidget(self.chk_high_res, 5, 0, 1, 2)
        
        # 🔧 NEW: Checkbox xuất MP4
        self.chk_export_mp4 = QtWidgets.QCheckBox("Xuất MP4 (video)")
        self.chk_export_mp4.setChecked(True)  # Mặc định tick
        self.chk_export_mp4.setToolTip("Xuất cả file MP4 (video) và MP3 (audio). Nếu bỏ tick chỉ xuất MP3")
        opt_layout.addWidget(self.chk_export_mp4, 6, 0, 1, 2)
        
        # Parallel dubbing options (hidden from user - auto-enabled for large files)
        # Số workers mặc định = 5
        self._max_workers = 5
        self._chunk_duration = 300  # 5 minutes
        
        grp_options.setMinimumWidth(250)
        top_row.addWidget(grp_options)
        
        top_row.addStretch()
        root.addLayout(top_row)
        
        # ========== JOB TABLE ==========
        grp_jobs = QtWidgets.QGroupBox("Danh sách công việc")
        jobs_layout = QtWidgets.QVBoxLayout(grp_jobs)
        
        self.tbl_jobs = QtWidgets.QTableWidget(0, 6)
        self.tbl_jobs.setHorizontalHeaderLabels([
            "#", "File", "Ngôn ngữ đích", "Trạng thái", "Tiến độ", "Hành động"
        ])
        self.tbl_jobs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_jobs.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tbl_jobs.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_jobs.verticalHeader().setVisible(False)
        self.tbl_jobs.setAlternatingRowColors(True)
        
        # Column widths
        hdr = self.tbl_jobs.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(2, 100)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(3, 120)
        hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(4, 80)
        hdr.setSectionResizeMode(5, QHeaderView.Fixed)
        self.tbl_jobs.setColumnWidth(5, 80)
        
        jobs_layout.addWidget(self.tbl_jobs)
        root.addWidget(grp_jobs, 1)  # stretch
        
        # ========== BOTTOM BUTTONS ==========
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(10)
        
        self.btn_start = QtWidgets.QPushButton("▶ Bắt đầu Dubbing")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #2ecc71; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_start.clicked.connect(self._start_dubbing)
        btn_row.addWidget(self.btn_start)
        
        self.btn_stop = QtWidgets.QPushButton("⏹ Dừng")
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #c0392b; }
            QPushButton:disabled { background-color: #95a5a6; }
        """)
        self.btn_stop.clicked.connect(self._stop_dubbing)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_stop)
        
        self.btn_output = QtWidgets.QPushButton("📁 Thư mục Output")
        self.btn_output.clicked.connect(self._open_output_folder)
        btn_row.addWidget(self.btn_output)
        
        self.btn_clear = QtWidgets.QPushButton("🗑 Xóa hoàn thành")
        self.btn_clear.clicked.connect(self._clear_completed)
        btn_row.addWidget(self.btn_clear)
        
        btn_row.addStretch()
        
        # Status label
        self.lbl_status = QtWidgets.QLabel("Sẵn sàng")
        self.lbl_status.setStyleSheet("color: #666;")
        btn_row.addWidget(self.lbl_status)
        
        root.addLayout(btn_row)
    
    # ========== FILE HANDLING ==========
    def _browse_file(self):
        """Browse for video/audio file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn file video/audio",
            "",
            "Media Files (*.mp4 *.mp3 *.wav *.m4a *.webm *.mkv);;All Files (*.*)"
        )
        
        if file_path:
            self.txt_file.setText(file_path)
            self._update_file_info(file_path)
            # Clear jobs table when importing new file
            self._clear_all_jobs()
    
    def _update_file_info(self, file_path: str):
        """Update file info label and enable/disable watermark removal."""
        if not os.path.exists(file_path):
            self.lbl_file_info.setText("❌ File không tồn tại")
            self.btn_convert.setEnabled(False)
            self.chk_watermark.setEnabled(False)
            return
        
        file_size = os.path.getsize(file_path) / (1024 * 1024)  # MB
        ext = os.path.splitext(file_path)[1].lower()
        
        info = f"📄 {os.path.basename(file_path)} ({file_size:.1f} MB)"
        
        # 🔧 NEW: Enable convert button for MP3
        if ext == '.mp3':
            self.btn_convert.setEnabled(True)
            self.chk_watermark.setEnabled(False)  # MP3 không có video track
            self.chk_watermark.setChecked(False)
            info += " | ⚠️ MP3: Không thể xóa watermark (cần MP4)"
        # 🔧 NEW: Enable watermark removal for MP4
        elif ext == '.mp4':
            self.btn_convert.setEnabled(False)
            self.chk_watermark.setEnabled(True)
            self.chk_watermark.setChecked(True)  # Mặc định tick cho MP4
            info += " | ✅ MP4: Có thể xóa watermark"
        else:
            self.btn_convert.setEnabled(False)
            self.chk_watermark.setEnabled(False)
            self.chk_watermark.setChecked(False)
        
        self.lbl_file_info.setText(info)
    
    def _convert_mp3_to_mp4(self):
        """Convert MP3 to MP4 with black video."""
        mp3_path = self.txt_file.text()
        if not mp3_path or not mp3_path.lower().endswith('.mp3'):
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn file MP3!")
            return
        
        # Find ffmpeg
        ffmpeg_bin = self._find_ffmpeg()
        if not ffmpeg_bin:
            QMessageBox.warning(self, "Lỗi", "Không tìm thấy ffmpeg!")
            return
        
        # Get duration
        ffprobe_bin = ffmpeg_bin.replace('ffmpeg', 'ffprobe')
        try:
            result = subprocess.run(
                [ffprobe_bin, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', mp3_path],
                capture_output=True, text=True, timeout=30
            )
            duration = float(result.stdout.strip())
        except Exception as e:
            _log(f"ffprobe error: {e}")
            duration = 300  # Default 5 minutes
        
        # Output path
        mp4_path = mp3_path.replace('.mp3', '_converted.mp4')
        
        # Convert
        self.lbl_status.setText("Đang convert MP3 → MP4...")
        QtWidgets.QApplication.processEvents()
        
        try:
            cmd = [
                ffmpeg_bin, '-y',
                '-f', 'lavfi', '-i', f'color=c=black:s=640x360:d={duration}',
                '-i', mp3_path,
                '-c:v', 'libx264', '-c:a', 'aac',
                '-shortest',
                mp4_path
            ]
            
            _log(f"Converting: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            
            if result.returncode == 0 and os.path.exists(mp4_path):
                self.txt_file.setText(mp4_path)
                self._update_file_info(mp4_path)
                self.lbl_status.setText(f"✅ Đã convert: {os.path.basename(mp4_path)}")
                QMessageBox.information(self, "Thành công", f"Đã convert sang:\n{mp4_path}")
            else:
                _log(f"Convert failed: {result.stderr}")
                self.lbl_status.setText("❌ Convert thất bại")
                QMessageBox.warning(self, "Lỗi", f"Convert thất bại:\n{result.stderr[:200]}")
                
        except Exception as e:
            _log(f"Convert error: {e}")
            self.lbl_status.setText("❌ Convert lỗi")
            QMessageBox.warning(self, "Lỗi", f"Convert lỗi: {e}")
    
    def _find_ffmpeg(self) -> Optional[str]:
        """Find ffmpeg binary."""
        import shutil
        
        # Check in app directory
        app_dir = _get_app_dir()
        for name in ['ffmpeg', 'ffmpeg.exe']:
            path = os.path.join(app_dir, name)
            if os.path.exists(path):
                return path
        
        # Check in PATH
        return shutil.which('ffmpeg')

    
    # ========== DUBBING ACTIONS ==========
    def _start_dubbing(self):
        """Start dubbing the selected file."""
        file_path = self.txt_file.text()
        if not file_path or not os.path.exists(file_path):
            QMessageBox.warning(self, "Lỗi", "Vui lòng chọn file!")
            return
        
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
                
                QMessageBox.critical(
                    self,
                    "⛔ Subscription không hợp lệ",
                    error_msg
                )
                _log("⛔ Blocked - subscription không hợp lệ")
                return
            
            # Check credits còn đủ không
            if not self._check_subscription_credits():
                return
        
        # Check if keys pool is available
        keys_pool = self._get_keys_pool()
        if not keys_pool or not keys_pool.cur():
            QMessageBox.warning(self, "Lỗi", "Không có API key! Vui lòng load key trước.")
            return
        
        # Get options
        source_lang = self.cmb_source.currentData()
        target_lang = self.cmb_target.currentData()
        num_speakers = self.spn_speakers.value()
        watermark = self.chk_watermark.isChecked()
        drop_bg = self.chk_drop_bg.isChecked()
        high_res = self.chk_high_res.isChecked()
        
        # Check file duration for parallel dubbing
        splitter = AudioSplitter()
        duration = splitter.get_duration(file_path)
        use_parallel = duration > self._chunk_duration  # > 5 minutes
        
        # Proxy getter function (closure to capture main_window)
        main_window = self.main_window
        def get_proxy():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                proxy_url = main_window.proxy_service_db.get_current_proxy()
                _log(f"Got proxy: {proxy_url[:50] if proxy_url else 'None'}...")
                return proxy_url
            _log("⚠️ No proxy service available")
            return None
        
        # Proxy rotator function (closure to capture main_window) - gọi force_refresh để lấy IP mới
        def rotate_proxy():
            if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
                fresh_proxy = main_window.proxy_service_db.force_refresh()
                _log(f"✅ [Proxy rotated via force_refresh()] Got: {fresh_proxy[:50] if fresh_proxy else 'None'}...")
        
        # ⚠️ Force rotate proxy BEFORE starting dubbing to ensure fresh proxy
        # This helps avoid unusual_activity errors from reused/flagged proxies
        if main_window and hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
            try:
                # Gọi force_refresh() để lấy IP mới từ proxyxoay API
                fresh_proxy = main_window.proxy_service_db.force_refresh()
                if fresh_proxy:
                    _log(f"✅ [Pre-dub] Fresh proxy: {fresh_proxy[:50]}...")
                else:
                    _log(f"⚠️ [Pre-dub] Could not refresh proxy")
            except Exception as e:
                _log(f"⚠️ Pre-dub proxy refresh failed: {e}")
        
        # Create job
        job_id = str(uuid.uuid4())[:8]
        job = {
            'id': job_id,
            'file_path': file_path,
            'file_name': os.path.basename(file_path),
            'source_lang': source_lang,
            'target_lang': target_lang,
            'status': 'pending',
            'progress': 0,
            'output_path': None,
            'error': None,
            'created_at': datetime.now().isoformat(),
            'parallel': use_parallel
        }
        
        self.jobs.append(job)
        self._refresh_table()
        self._save_jobs()
        
        if use_parallel:
            # Use ParallelDubbingManager for large files
            _log(f"Using parallel dubbing for {duration/60:.1f} min file")
            self.lbl_status.setText(f"Đang xử lý file lớn ({duration/60:.0f} phút)...")
            
            worker = ParallelDubbingManager(
                input_path=file_path,
                source_lang=source_lang,
                target_lang=target_lang,
                keys_pool=keys_pool,
                proxy_getter=get_proxy,
                output_dir=os.path.dirname(file_path),
                chunk_duration=self._chunk_duration,
                max_workers=self._max_workers,
                num_speakers=num_speakers,
                watermark=watermark,
                drop_background=drop_bg,
                highest_res=high_res,
                proxy_rotator=rotate_proxy,
                export_mp4=self.chk_export_mp4.isChecked()  # 🔧 NEW: Pass export_mp4 flag
            )
            
            worker.overall_progress.connect(lambda c, t, s: self._on_parallel_progress(job_id, c, t, s))
            worker.finished.connect(lambda ok, msg, path: self._on_parallel_finished(job_id, ok, msg, path))
            
            self.workers[job_id] = worker
            worker.start()
        else:
            # Use regular DubbingWorker for small files
            worker = DubbingWorker(
                job_id=job_id,
                file_path=file_path,
                source_lang=source_lang,
                target_lang=target_lang,
                keys_pool=keys_pool,
                proxy_getter=get_proxy,
                output_dir=os.path.dirname(file_path),
                num_speakers=num_speakers,
                watermark=watermark,
                drop_background=drop_bg,
                highest_res=high_res,
                proxy_rotator=rotate_proxy,
                export_mp4=self.chk_export_mp4.isChecked(),  # 🔧 NEW: Pass export_mp4 flag
                remove_watermark=self.chk_watermark.isChecked()  # 🔧 NEW: Pass remove_watermark flag
            )
            
            worker.progress.connect(self._on_progress)
            worker.status_update.connect(self._on_status_update)
            worker.finished.connect(self._on_finished)
            worker.key_error.connect(self._on_key_error)
            
            self.workers[job_id] = worker
            worker.start()
        
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.lbl_status.setText(f"Đang xử lý: {os.path.basename(file_path)}")
    
    def _on_parallel_progress(self, job_id: str, completed: int, total: int, status: str):
        """Handle parallel dubbing progress."""
        for job in self.jobs:
            if job['id'] == job_id:
                job['progress'] = completed
                job['status'] = 'dubbing'
                break
        self._refresh_table()
        self.lbl_status.setText(status)
    
    def _on_parallel_finished(self, job_id: str, success: bool, message: str, output_path: str):
        """Handle parallel dubbing finished."""
        # Check if it's a signal to use single dub
        if message == "USE_SINGLE_DUB":
            # File is small, redirect to regular dubbing
            # This shouldn't happen as we check duration before
            return
        
        for job in self.jobs:
            if job['id'] == job_id:
                if success:
                    job['status'] = 'done'
                    job['progress'] = 100
                    job['output_path'] = output_path
                    # Trừ credits sau khi dub thành công
                    self._deduct_credits_for_dubbing(output_path, job.get('file_path'))
                else:
                    job['status'] = 'failed'
                    job['error'] = message
                break
        
        self._refresh_table()
        self._save_jobs()
        
        # Remove worker
        if job_id in self.workers:
            del self.workers[job_id]
        
        # Update UI
        if not self.workers:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
        
        if success:
            self.lbl_status.setText(f"✅ Hoàn thành: {os.path.basename(output_path)}")
        else:
            self.lbl_status.setText(f"❌ Lỗi: {message[:50]}")
    
    def _on_key_error(self, job_id: str, api_key: str, error_type: str):
        """Handle key error signal from worker."""
        _log(f"Key error: {api_key} - {error_type}")
        self.lbl_status.setText(f"⚠️ Key lỗi, đang đổi key...")
    
    def _stop_dubbing(self):
        """Stop all running workers."""
        for job_id, worker in self.workers.items():
            if worker.isRunning():
                worker.stop()
                worker.wait(5000)
        
        self.workers.clear()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText("Đã dừng")
    
    def _on_progress(self, job_id: str, status: str, percent: int):
        """Handle progress update."""
        for job in self.jobs:
            if job['id'] == job_id:
                job['status'] = status
                job['progress'] = percent
                break
        self._refresh_table()
    
    def _on_status_update(self, job_id: str, status_text: str):
        """Handle status text update."""
        self.lbl_status.setText(status_text)
    
    def _on_finished(self, job_id: str, success: bool, output_path: str, error_msg: str):
        """Handle worker finished."""
        for job in self.jobs:
            if job['id'] == job_id:
                if success:
                    job['status'] = 'done'
                    job['progress'] = 100
                    job['output_path'] = output_path
                    # Trừ credits sau khi dub thành công
                    self._deduct_credits_for_dubbing(output_path, job.get('file_path'))
                else:
                    job['status'] = 'failed'
                    job['error'] = error_msg
                break
        
        self._refresh_table()
        self._save_jobs()
        
        # Remove worker
        if job_id in self.workers:
            del self.workers[job_id]
        
        # Update UI
        if not self.workers:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
        
        if success:
            self.lbl_status.setText(f"✅ Hoàn thành: {os.path.basename(output_path)}")
        else:
            self.lbl_status.setText(f"❌ Lỗi: {error_msg[:50]}")
    
    # ========== TABLE MANAGEMENT ==========
    def _refresh_table(self):
        """Refresh job table."""
        self.tbl_jobs.setRowCount(len(self.jobs))
        
        for i, job in enumerate(self.jobs):
            # #
            item_idx = QtWidgets.QTableWidgetItem(str(i + 1))
            item_idx.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 0, item_idx)
            
            # File
            self.tbl_jobs.setItem(i, 1, QtWidgets.QTableWidgetItem(job.get('file_name', '')))
            
            # Target lang
            target = job.get('target_lang', '')
            target_name = TARGET_LANGUAGES.get(target, target)
            item_lang = QtWidgets.QTableWidgetItem(f"{target_name}")
            item_lang.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 2, item_lang)
            
            # Status
            status = job.get('status', '')
            status_map = {
                'pending': '⏳ Chờ',
                'uploading': '📤 Upload',
                'dubbing': '🔄 Đang dub',
                'downloading': '📥 Tải xuống',
                'done': '✅ Hoàn thành',
                'failed': '❌ Lỗi'
            }
            item_status = QtWidgets.QTableWidgetItem(status_map.get(status, status))
            item_status.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 3, item_status)
            
            # Progress
            progress = job.get('progress', 0)
            item_progress = QtWidgets.QTableWidgetItem(f"{progress}%")
            item_progress.setTextAlignment(Qt.AlignCenter)
            self.tbl_jobs.setItem(i, 4, item_progress)
            
            # Action buttons container
            action_widget = QtWidgets.QWidget()
            action_layout = QtWidgets.QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            action_layout.setSpacing(2)
            
            # Open folder button (if completed)
            if job.get('output_path') and os.path.exists(job.get('output_path', '')):
                btn_open = QtWidgets.QPushButton("📂")
                btn_open.setToolTip("Mở thư mục chứa file")
                btn_open.setFixedSize(28, 24)
                btn_open.clicked.connect(lambda checked, p=job['output_path']: self._open_file_location(p))
                action_layout.addWidget(btn_open)
            
            # Delete button (always show, except when running)
            job_status = job.get('status', '')
            if job_status not in ('uploading', 'dubbing', 'downloading'):
                btn_del = QtWidgets.QPushButton("🗑")
                btn_del.setToolTip("Xóa")
                btn_del.setFixedSize(28, 24)
                btn_del.clicked.connect(lambda checked, jid=job['id']: self._delete_job(jid))
                action_layout.addWidget(btn_del)
            
            action_layout.addStretch()
            self.tbl_jobs.setCellWidget(i, 5, action_widget)
    
    def _open_file_location(self, file_path: str):
        """Open file location in explorer."""
        import subprocess
        import sys
        
        folder = os.path.dirname(file_path)
        if sys.platform == 'darwin':
            subprocess.run(['open', folder])
        elif sys.platform == 'win32':
            subprocess.run(['explorer', folder])
        else:
            subprocess.run(['xdg-open', folder])
    
    def _open_output_folder(self):
        """Open output folder."""
        file_path = self.txt_file.text()
        if file_path and os.path.exists(file_path):
            self._open_file_location(file_path)
        else:
            QMessageBox.information(self, "Thông báo", "Chưa chọn file nào")
    
    def _clear_completed(self):
        """Clear completed jobs from list."""
        self.jobs = [j for j in self.jobs if j.get('status') not in ('done', 'failed')]
        self._refresh_table()
        self._save_jobs()
    
    def _clear_all_jobs(self):
        """Clear all jobs from list (when importing new file)."""
        # Stop any running workers first
        for worker in list(self.workers.values()):
            if worker.isRunning():
                worker.stop()
                worker.wait(2000)
        self.workers.clear()
        
        # Clear jobs
        self.jobs = []
        self._refresh_table()
        self._save_jobs()
        _log("Cleared all jobs")
    
    def _delete_job(self, job_id: str):
        """Delete a specific job by ID."""
        # Check if job is running
        if job_id in self.workers:
            worker = self.workers[job_id]
            if worker.isRunning():
                QMessageBox.warning(self, "Lỗi", "Không thể xóa job đang chạy!")
                return
            del self.workers[job_id]
        
        # Remove from jobs list
        self.jobs = [j for j in self.jobs if j.get('id') != job_id]
        self._refresh_table()
        self._save_jobs()
    
    # ========== JOB PERSISTENCE ==========
    def _get_jobs_path(self) -> str:
        """Get path to jobs JSON file."""
        return os.path.join(_get_app_dir(), 'dubbing_jobs.json')
    
    def _load_jobs(self):
        """Load jobs from file."""
        path = self._get_jobs_path()
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.jobs = data.get('jobs', [])
                    _log(f"Loaded {len(self.jobs)} jobs")
            except Exception as e:
                _log(f"Load jobs error: {e}")
                self.jobs = []
        self._refresh_table()
    
    def _save_jobs(self):
        """Save jobs to file."""
        path = self._get_jobs_path()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'jobs': self.jobs}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log(f"Save jobs error: {e}")
    
    # ========== API KEY & PROXY ==========
    def _get_keys_pool(self):
        """Get keys pool from main window."""
        if self.main_window and hasattr(self.main_window, 'keys'):
            return self.main_window.keys
        return None
    
    def _get_api_key(self) -> Optional[str]:
        """Get API key from main window."""
        keys_pool = self._get_keys_pool()
        if keys_pool:
            return keys_pool.cur()
        return None
    
    def _get_proxy(self) -> Optional[str]:
        """Get proxy URL from main window."""
        if self.main_window and hasattr(self.main_window, 'proxy_service_db'):
            proxy_service = self.main_window.proxy_service_db
            if proxy_service:
                proxy_url = proxy_service.get_current_proxy()
                _log(f"Got proxy from service: {proxy_url[:50] if proxy_url else 'None'}...")
                return proxy_url
            else:
                _log("⚠️ proxy_service_db is None")
        else:
            _log("⚠️ main_window has no proxy_service_db")
        return None

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
                _log("⚠️ No user logged in")
                return False
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                _log("⚠️ No database connection")
                return False
            
            result = self.main_window.supabase.table('user_subscriptions')\
                .select('is_active, end_date, count_characters, subscription_type')\
                .eq('user_id', self.main_window.current_user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if not result.data:
                _log("❌ No active subscription found")
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
                        _log(f"❌ Subscription expired: {end_date}")
                        return False
                except Exception as e:
                    _log(f"⚠️ Error parsing end_date: {e}")
            
            # Check character-based quota
            try:
                count_chars = sub.get('count_characters')
                if count_chars is not None and int(count_chars) <= 0:
                    _log("❌ No remaining characters (count_characters <= 0)")
                    return False
            except Exception:
                pass
            
            _log(f"✅ Subscription active: {sub.get('subscription_type', 'unknown')}")
            return True
            
        except Exception as e:
            _log(f"❌ Subscription check error: {e}")
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
            _log(f"❌ Load subscription error: {e}")
            return {'error': str(e)}
    
    def _check_subscription_credits(self) -> bool:
        """
        Kiểm tra subscription credits trước khi bắt đầu Dubbing.
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
                _log(f"❌ Hết credits! Gói {plan_name} còn {current_chars:,} ký tự")
                QMessageBox.warning(
                    self, 
                    "Hết Credits Gói",
                    f"❌ Gói {plan_name} đã hết credits!\n\n"
                    f"Số ký tự còn lại: {current_chars:,}\n\n"
                    "Vui lòng nâng cấp gói hoặc liên hệ admin để nạp thêm."
                )
                return False
            
            # Còn credits - cho phép bắt đầu
            _log(f"✅ Gói {plan_name} còn {current_chars:,} ký tự")
            return True
            
        except Exception as e:
            _log(f"⚠️ Check credits error: {e}")
            return True  # Lỗi thì cho phép tiếp tục

    # ========== CREDIT DEDUCTION ==========
    def _deduct_credits_for_dubbing(self, output_path: str, input_path: str = None):
        """
        Trừ credits sau khi dubbing thành công.
        
        ElevenLabs Dubbing tính credits theo phút audio:
        - Khoảng 2000 credits/phút (dựa trên ~9000 credits cho 4.5 phút)
        - Tương đương ~33 credits/giây
        
        Công thức: duration_seconds * 33
        """
        try:
            if not hasattr(self.main_window, 'current_user_id') or not self.main_window.current_user_id:
                return
            
            if not hasattr(self.main_window, 'supabase') or not self.main_window.supabase:
                return
            
            # Get duration of output file
            splitter = AudioSplitter()
            duration = 0
            
            if output_path and os.path.exists(output_path):
                duration = splitter.get_duration(output_path)
            elif input_path and os.path.exists(input_path):
                duration = splitter.get_duration(input_path)
            
            if duration <= 0:
                _log("⚠️ Cannot get duration for credit deduction")
                return
            
            # Tính credits: ~33 credits/giây (2000 credits/phút)
            # Thêm 20% margin để đảm bảo lời
            credits_per_second = 33
            chars_to_deduct = int(duration * credits_per_second * 1.2)
            
            if chars_to_deduct <= 0:
                return
            
            _log(f"📝 Deducting {chars_to_deduct:,} credits for {duration:.1f}s dubbing (~{duration/60:.1f} min)")
            
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
                    _log(f"✅ Deducted {chars_to_deduct:,} credits → {new_chars:,} remaining")
                    
                    # Update main window subscription label
                    if hasattr(self.main_window, '_update_subscription_label'):
                        from PySide6 import QtCore
                        QtCore.QTimer.singleShot(100, self.main_window._update_subscription_label)
                    
                    # Check if credits exhausted
                    if new_chars <= 0:
                        _log("❌ Credits exhausted after dubbing!")
                        QMessageBox.warning(
                            self,
                            "Hết Credits",
                            "❌ Gói của bạn đã hết credits sau khi dubbing!\n\n"
                            "Vui lòng nâng cấp gói hoặc liên hệ admin."
                        )
                    return
                else:
                    # Conflict - retry
                    if attempt < max_retries - 1:
                        _log(f"⚠️ Retry {attempt + 1}/{max_retries} (conflict)")
                        time.sleep(0.1)
                        continue
                    else:
                        _log(f"⚠️ Failed to deduct after {max_retries} retries")
                        return
                        
        except Exception as e:
            _log(f"⚠️ Deduct credits error: {e}")
