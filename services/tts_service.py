from __future__ import annotations

import os
import sys
import uuid
import subprocess
import tempfile
import shutil
import json
import logging
import time
import threading
from datetime import datetime
from typing import List, Tuple, Optional
from enum import Enum

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError

from services.key_pool import LocalKeyPool, KeyState, _utcnow


# =========================
# Logging setup
# =========================
def setup_tts_logger():
    logger = logging.getLogger('tts_service')
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    try:
        if hasattr(sys, 'frozen') and hasattr(sys, '_MEIPASS'):
            exe_dir = os.path.dirname(sys.executable)
            log_dir = os.path.join(exe_dir, 'logs')
        else:
            log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    except Exception:
        log_dir = os.path.join(os.getcwd(), 'logs')

    try:
        os.makedirs(log_dir, exist_ok=True)
        print(f"📁 TTS Log directory: {log_dir}")
    except Exception:
        import tempfile as _temp
        log_dir = os.path.join(_temp.gettempdir(), 'tts_logs')
        os.makedirs(log_dir, exist_ok=True)
        print(f"📁 Using fallback TTS log dir: {log_dir}")

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(log_dir, f'tts_processing_{ts}.log')

    try:
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        print(f"✅ Logger file: {log_file}")
    except Exception:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        print("⚠️ Logger fallback to console")
    return logger


tts_logger = setup_tts_logger()


# =========================
# Configuration Constants (Phase 10)
# =========================
class TTSConfig:
    """Centralized configuration to replace magic numbers"""
    # Credit limits
    MAX_CREDITS_PER_REQUEST = 9998
    MAX_SAFE_CREDITS_PER_CHUNK = 4000  # 🔧 GIẢM XUỐNG 500 credits/chunk để fit keys nhỏ
    # Max chars per request/chunk - GIẢM XUỐNG để phù hợp với keys có ít credits
    DEFAULT_CHUNK_SIZE = 400  # 🔧 400 chars = ~400 credits (turbo) hoặc ~800 credits (standard)
    MAX_CHARS_PER_CHUNK = 3000  # 🔧 Giảm từ 4900 → 400 để fit keys nhỏ
    CREDIT_BUFFER_SMALL = 10      # For requests <= 50 chars
    CREDIT_BUFFER_MEDIUM = 0.20   # 20% for 51-200 chars
    CREDIT_BUFFER_LARGE = 0.15    # 15% for 201+ chars
    
    # Timeouts (seconds)
    CURL_CONNECT_TIMEOUT = 12
    # Read timeout for TTS calls (3 minutes)
    CURL_MAX_TIME = 180
    CURL_MAX_TIME_DOWNLOAD = 180
    API_REQUEST_TIMEOUT = 15
    
    # Retry settings
    MAX_KEY_ROTATION_ATTEMPTS = 3
    # Số lần retry cấp đoạn (paragraph-level).
    # 1 → tổng cộng 2 attempt (1 lần chính + 1 lần retry), giúp fail nhanh khi proxy bị block.
    MAX_PARAGRAPH_RETRIES = 1
    # Thời gian chờ giữa các lần retry đoạn – giữ thấp để phản hồi nhanh
    STAGE_RETRY_WAIT = 1  # seconds between stage retries
    KEY_VALIDATION_RETRIES = 2
    KEY_VALIDATION_RETRY_DELAY = 0.8  # seconds between validation attempts
    MAX_KEY_VALIDATION_SWITCHES = 5  # max times to swap keys per chunk when validation fails
    
    # Cache TTL
    VALIDATION_CACHE_TTL = 300  # 5 minutes
    PROXY_HEALTH_CACHE_TTL = 120  # 2 minutes
    
    # Circuit Breaker
    CIRCUIT_BREAKER_MAX_FAILURES = 5
    CIRCUIT_BREAKER_TIMEOUT = 60  # seconds
    
    # Parallel processing (OPTIMIZED)
    MAX_WORKERS = 5  # cap at 5 as requested
    DEFAULT_WORKERS = 3  # default 3 workers
    # Số key test song song khi tìm key mới. Để không vượt quá tổng 5 task, giới hạn = 1.
    PARALLEL_KEY_TEST_COUNT = 1
    
    # HTTP/2 Connection Pooling
    HTTP2_ENABLED = False  # Force plain HTTP requests (disable HTTP/2/pooling)
    CONNECTION_POOL_SIZE = 20  # Max connections per host
    CONNECTION_POOL_MAXSIZE = 30  # Total max connections
    NO_PROXY_STAGE1_RETRIES = 2  # Extra direct retries when proxy is unavailable
    
    # Async settings
    ASYNC_ENABLED = False  # DISABLED - Implementation incomplete (missing retry logic)
    ASYNC_TIMEOUT = 300  # Async operation timeout (5 minutes)
    
    # Text processing - OPTIMIZED for better TTS quality
    # Giữ sentence-level chunking nhưng cho phép đoạn dài hơn trước khi buộc phải tách nhỏ
    LONG_PARAGRAPH_THRESHOLD = 2500
    TARGET_CHUNK_SIZE = 4900
    
    # Concatenation safety limits
    CONCAT_BATCH_SIZE = 80  # Max number of inputs per ffmpeg concat call before batching


# =========================
# Error Hierarchy (Phase 9) - Based on ElevenLabs official errors
# =========================
class TTSError(Exception):
    """Base exception for TTS errors"""
    pass


class APIError(TTSError):
    """ElevenLabs API errors (400/401/403/429)"""
    def __init__(self, message: str, status_code: int = None, error_code: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code  # ElevenLabs error code


class MaxCharacterLimitExceededError(APIError):
    """400 - max_character_limit_exceeded"""
    def __init__(self, message: str = "Too many characters in single request"):
        super().__init__(message, status_code=400, error_code="max_character_limit_exceeded")


class InvalidAPIKeyError(APIError):
    """401 - invalid_api_key"""
    def __init__(self, message: str = "Invalid API key"):
        super().__init__(message, status_code=401, error_code="invalid_api_key")


class QuotaExceededError(APIError):
    """401 - quota_exceeded"""
    def __init__(self, message: str = "Insufficient quota"):
        super().__init__(message, status_code=401, error_code="quota_exceeded")


class KeyValidationError(TTSError):
    """Raised when API key credit validation fails repeatedly."""
    def __init__(self, api_key: str, message: str = "API key validation failed"):
        super().__init__(message)
        self.api_key = api_key


class VoiceNotFoundError(APIError):
    """401 - voice_not_found"""
    def __init__(self, message: str = "Voice ID not found"):
        super().__init__(message, status_code=401, error_code="voice_not_found")


class OnlyForCreatorPlusError(APIError):
    """403 - only_for_creator+"""
    def __init__(self, message: str = "Professional voices require Creator+ plan"):
        super().__init__(message, status_code=403, error_code="only_for_creator+")


class TooManyConcurrentRequestsError(APIError):
    """429 - too_many_concurrent_requests"""
    def __init__(self, message: str = "Exceeded concurrency limit"):
        super().__init__(message, status_code=429, error_code="too_many_concurrent_requests")


class SystemBusyError(APIError):
    """429 - system_busy"""
    def __init__(self, message: str = "ElevenLabs system busy, retry with backoff"):
        super().__init__(message, status_code=429, error_code="system_busy")


class VoiceLimitReachedError(APIError):
    """Custom voice limit reached (not in official docs but exists in practice)"""
    def __init__(self, message: str = "Custom voice limit reached"):
        super().__init__(message, status_code=400, error_code="voice_limit_reached")


class NetworkError(TTSError):
    """Network/connection errors"""
    pass


class ProxyError(NetworkError):
    """Proxy connection errors"""
    def __init__(self, message: str, proxy_host: str = None):
        super().__init__(message)
        self.proxy_host = proxy_host


class CircuitBreakerOpenError(TTSError):
    """Circuit breaker is open"""
    pass


# =========================
# Circuit Breaker Pattern (Phase 2)
# =========================
class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Prevent infinite retry loops when all keys fail"""
    
    def __init__(self, max_failures: int = None, timeout: int = None):
        self.max_failures = max_failures or TTSConfig.CIRCUIT_BREAKER_MAX_FAILURES
        self.timeout = timeout or TTSConfig.CIRCUIT_BREAKER_TIMEOUT
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
        self._lock = threading.Lock()
    
    def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        with self._lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    tts_logger.info("CIRCUIT_BREAKER: HALF_OPEN - Testing recovery")
                else:
                    wait_time = self.timeout - (time.time() - self.last_failure_time)
                    raise CircuitBreakerOpenError(f"Circuit breaker OPEN: wait {wait_time:.0f}s")
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e
    
    def _on_success(self):
        with self._lock:
            self.failure_count = 0
            self.state = CircuitState.CLOSED
    
    def _on_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.max_failures:
                self.state = CircuitState.OPEN
                tts_logger.error(f"CIRCUIT_BREAKER: OPEN - {self.failure_count} failures")
    
    def _should_attempt_reset(self) -> bool:
        if self.last_failure_time is None:
            return True
        return (time.time() - self.last_failure_time) >= self.timeout
    
    def reset(self):
        with self._lock:
            self.failure_count = 0
            self.state = CircuitState.CLOSED
            self.last_failure_time = None


# =========================
# Retry Strategy (Phase 7)
# =========================
class RetryStrategy:
    """Exponential backoff with jitter"""
    
    def __init__(self, max_retries: int = 3, backoff_factor: float = 2.0, 
                 initial_delay: float = 1.0, max_delay: float = 30.0):
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.initial_delay = initial_delay
        self.max_delay = max_delay
    
    def execute(self, func, *args, on_retry=None, **kwargs):
        import random
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries:
                    delay = min(self.initial_delay * (self.backoff_factor ** attempt), self.max_delay)
                    jitter = delay * random.uniform(-0.2, 0.2)
                    delay = max(0.1, delay + jitter)
                    
                    tts_logger.warning(f"RETRY {attempt + 1}/{self.max_retries} - Error: {str(e)[:100]}, wait {delay:.2f}s")
                    
                    if on_retry:
                        try:
                            on_retry(attempt + 1, delay, e)
                        except Exception:
                            pass
                    time.sleep(delay)
        
        raise last_exception


# =========================
# Text Processing (Phase 6)
# =========================
class TextSanitizer:
    """
    Sanitize text for ElevenLabs TTS.
    SIMPLIFIED: Remove all problematic characters that cause issues.
    
    Characters removed:
    - { } < > [ ] - These cause low quality speech
    - SSML tags are NO LONGER supported (they cause reading errors)
    
    For v3 models: Preserve emotion tags like [angry], [whisper] etc.
    """
    
    @staticmethod
    def sanitize(text: str, skip_for_non_ascii: bool = False, model_name: str = None) -> str:
        """
        Simple and reliable text sanitization for all languages.
        Removes problematic special characters that ElevenLabs can't handle.
        """
        if not text:
            return ""
        
        import re
        
        # Check if model is v3 (supports emotion tags)
        is_v3_model = model_name and 'v3' in model_name.lower()
        
        # Step 1: For v3 models, protect emotion tags first
        emotion_placeholders = []
        if is_v3_model:
            def replace_emotion(match):
                tag_content = match.group(1).lower().strip()
                # Only protect known emotion tags
                known_emotions = ['angry', 'whisper', 'sad', 'happy', 'excited', 'calm', 'serious']
                if tag_content in known_emotions:
                    placeholder = f"__EMOTION_{len(emotion_placeholders)}__"
                    emotion_placeholders.append(match.group(0))
                    return placeholder
                return match.group(0)  # Keep unknown tags for removal
            
            text = re.sub(r'\[([^\]]+)\]', replace_emotion, text)
        
        # Step 2: Remove ALL problematic characters
        # Complete list of characters that cause issues with ElevenLabs
        problematic_chars = [
            '{', '}',     # Curly braces
            '<', '>',     # Angle brackets (SSML removed)
            '[', ']',     # Square brackets (except v3 emotion tags already protected)
        ]
        
        for char in problematic_chars:
            text = text.replace(char, '')
        
        # Step 3: Restore emotion tags for v3 models
        if is_v3_model:
            for i, emotion_tag in enumerate(emotion_placeholders):
                placeholder = f"__EMOTION_{i}__"
                text = text.replace(placeholder, emotion_tag)
        
        # Step 4: Normalize whitespace (collapse multiple spaces to single space)
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        
        tts_logger.debug(f"SANITIZE - Cleaned text, kept {len(emotion_placeholders)} emotion tags (v3={is_v3_model}), length: {len(text)}")
        return text


class TextChunker:
    """Smart text chunking"""
    
    @staticmethod
    def split_paragraphs(content: str) -> List[str]:
        import re
        text = (content or "").encode('utf-8', errors='ignore').decode('utf-8')
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        
        # CRITICAL: Handle single long line (no paragraph breaks)
        if '\n\n' not in text and len(text) > TTSConfig.MAX_CHARS_PER_CHUNK:
            # Single long line - apply smart chunking directly
            tts_logger.info(f"SINGLE_LONG_LINE detected - {len(text)} chars, applying smart chunking...")
            chunks = TextChunker._chunk_long_paragraph(text, max_chunk_size=TTSConfig.MAX_CHARS_PER_CHUNK)
            tts_logger.info(f"SINGLE_LONG_LINE chunked - {len(text)} chars → {len(chunks)} chunks")
            return chunks
        
        # Normal paragraph splitting (multiple paragraphs separated by \n\n)
        raw_parts = re.split(r"\n\s*\n+", text)
        parts = [p.strip() for p in raw_parts if p and p.strip()]
        
        # OPTIMIZATION: Combine small paragraphs up to MAX_CHARS_PER_CHUNK (4900 chars)
        # This reduces number of TTS requests and improves efficiency
        final_parts = []
        current_chunk = ""
        
        for part in parts:
            # If single paragraph exceeds limit, chunk it first
            if len(part) > TTSConfig.MAX_CHARS_PER_CHUNK:
                # Save current chunk if exists
                if current_chunk:
                    final_parts.append(current_chunk.strip())
                    current_chunk = ""
                
                # Chunk the long paragraph
                chunks = TextChunker._chunk_long_paragraph(part, max_chunk_size=TTSConfig.MAX_CHARS_PER_CHUNK)
                final_parts.extend(chunks)
                tts_logger.info(f"PARAGRAPH_CHUNKED - {len(part)} chars → {len(chunks)} chunks")
            else:
                # Try to combine with current chunk
                test_chunk = current_chunk + "\n\n" + part if current_chunk else part
                
                if len(test_chunk) <= TTSConfig.MAX_CHARS_PER_CHUNK:
                    # Can combine - add to current chunk
                    current_chunk = test_chunk
                else:
                    # Can't combine - save current chunk and start new one
                    if current_chunk:
                        final_parts.append(current_chunk.strip())
                    current_chunk = part
        
        # Don't forget the last chunk
        if current_chunk:
            final_parts.append(current_chunk.strip())
        
        # Debug logging for chunking analysis
        tts_logger.info(f"SPLIT_PARAGRAPHS - Total input: {len(text)} chars → {len(final_parts)} final parts")
        for i, part in enumerate(final_parts, 1):
            tts_logger.info(f"  Part {i}: {len(part)} chars - '{part[:80]}...'")
        
        return final_parts
    
    @staticmethod
    def _chunk_long_paragraph(text: str, max_chunk_size: int = TTSConfig.MAX_CHARS_PER_CHUNK) -> List[str]:
        """
        SMART chunking for long paragraphs - break at sentence boundaries.
        
        Strategy:
        1. Split by sentences (ending with . ! ?)
        2. Group sentences up to max_chunk_size characters
        3. Break at sentence boundary, not mid-sentence
        4. Each chunk becomes separate TTS request with own request_id
        """
        import re
        
        # Split into sentences at . ! ? followed by space or end of string
        sentences = re.split(r'(?<=[\.!?])\s+', text)
        
        # If no sentence breaks found, split by words to ensure chunking
        if len(sentences) == 1 and len(text) > max_chunk_size:
            tts_logger.warning(f"NO_SENTENCE_BREAKS - Splitting by words instead (text too long: {len(text)} chars)")
            words = text.split()
            chunks = []
            current_chunk = []
            current_length = 0
            
            for word in words:
                word_with_space = (" " if current_chunk else "") + word
                new_length = current_length + len(word_with_space)
                
                if new_length > max_chunk_size and current_chunk:
                    chunks.append(" ".join(current_chunk))
                    current_chunk = [word]
                    current_length = len(word)
                else:
                    current_chunk.append(word)
                    current_length = new_length
            
            if current_chunk:
                chunks.append(" ".join(current_chunk))
        else:
            # Normal sentence-based chunking
            chunks = []
            current_chunk = []
            current_length = 0
            
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                
                # Calculate length if we add this sentence
                separator = " " if current_chunk else ""
                sentence_with_sep = separator + sentence
                new_length = current_length + len(sentence_with_sep)
                
                # If adding this sentence exceeds limit and we have content, start new chunk
                if new_length > max_chunk_size and current_chunk:
                    # Finish current chunk
                    chunks.append(" ".join(current_chunk).strip())
                    current_chunk = [sentence]
                    current_length = len(sentence)
                else:
                    # Add to current chunk (even if a single long sentence > max_chunk_size).
                    # We'll hard-split any oversize chunks in a second pass below.
                    current_chunk.append(sentence)
                    current_length = new_length
            
            # Add final chunk if exists
            if current_chunk:
                chunks.append(" ".join(current_chunk).strip())
        
        # Second pass: HARD LIMIT to max_chunk_size for any oversize chunks
        final_chunks: List[str] = []
        for ch in chunks:
            ch = ch.strip()
            if not ch:
                continue
            
            if len(ch) <= max_chunk_size:
                final_chunks.append(ch)
                continue
            
            # Prefer word-based splitting when spaces exist
            if " " in ch:
                words = ch.split()
                buf = []
                cur_len = 0
                for w in words:
                    add_len = len(w) + (1 if buf else 0)
                    if cur_len + add_len > max_chunk_size and buf:
                        final_chunks.append(" ".join(buf))
                        buf = [w]
                        cur_len = len(w)
                    elif len(w) > max_chunk_size:
                        # Single word too long: hard split by characters
                        if buf:
                            final_chunks.append(" ".join(buf))
                            buf = []
                            cur_len = 0
                        for i in range(0, len(w), max_chunk_size):
                            final_chunks.append(w[i:i + max_chunk_size])
                        cur_len = 0
                    else:
                        buf.append(w)
                        cur_len += add_len
                if buf:
                    final_chunks.append(" ".join(buf))
            else:
                # No spaces (e.g. CJK text) → hard split by characters
                for i in range(0, len(ch), max_chunk_size):
                    final_chunks.append(ch[i:i + max_chunk_size])

        # Log chunking info
        tts_logger.info(f"SMART_CHUNKING - Original: {len(text)} chars → {len(final_chunks)} chunks (max {max_chunk_size} chars each)")
        for i, chunk in enumerate(final_chunks, 1):
            tts_logger.info(f"  Chunk {i}: {len(chunk)} chars - '{chunk[:50]}...'")
        
        return final_chunks
    
    @staticmethod
    def chunk_by_credits(model_name: str, text: str, max_credits: int = None, 
                        per_request_char_limit: int = None) -> List[str]:
        if not text:
            return []
        
        max_credits = max_credits or TTSConfig.MAX_CREDITS_PER_REQUEST
        per_request_char_limit = per_request_char_limit or TTSConfig.DEFAULT_CHUNK_SIZE
        
        factor = credit_factor(model_name)
        
        # CRITICAL: Limit to 4900 chars maximum per chunk
        # This ensures chunks stay well below 8000 credits even with 20% buffer
        # For turbo model (0.5): 4900 chars = 2450 credits base + 490 buffer = 2940 total
        # For standard model (1.0): 4900 chars = 4900 credits base + 980 buffer = 5880 total
        MAX_CHARS_PER_CHUNK = TTSConfig.MAX_CHARS_PER_CHUNK
        
        # Calculate max chars based on credits (with 20% buffer reserve)
        base_credits_without_buffer = int(max_credits / 1.20)  # Reserve 20% for buffer
        max_chars_by_credit = int(base_credits_without_buffer / max(factor, 0.0001))
        
        # Use the minimum of: credit-based limit, per_request_char_limit, and 4900 chars
        chunk_char_limit = max(1, min(max_chars_by_credit, per_request_char_limit, MAX_CHARS_PER_CHUNK))
        
        words = text.split()
        chunks, buf, current_len = [], [], 0
        
        for w in words:
            add_len = len(w) + (1 if buf else 0)
            if current_len + add_len > chunk_char_limit:
                if buf:
                    chunks.append(" ".join(buf))
                buf, current_len = [w], len(w)
            else:
                buf.append(w)
                current_len += add_len
        
        if buf:
            chunks.append(" ".join(buf))
        
        return chunks


# =========================
# Credit helpers
# =========================
def credit_factor(model_name: str) -> float:
    name = (model_name or "").lower()
    half_rate = any(tag in name for tag in ["flash", "turbo"]) or name in ["eleven_v2_5_flash", "eleven_v2_flash"]
    return 0.5 if half_rate else 1.0


def estimate_credits_for_text(model_name: str, text: str) -> int:
    """Estimate credits with 1% safety buffer"""
    char_count = len(text or "")
    factor = credit_factor(model_name)
    estimated = int(char_count * factor)
    # Add 1% buffer for safety
    buffer = max(1, int(estimated * 0.01))
    return estimated + buffer


# =========================
# Main class
# =========================
class TTSTaskRunner:
    """
    3-stage flow cho MỖI ĐOẠN:
      Stage 1: NO proxy
      Stage 2: nếu fail -> ENABLE proxy (hostname gateway); nếu vẫn fail -> WAIT 60s + rotate via PHP
      Stage 3: call lại với proxy sau khi rotate

    Chạy song song tối đa 3 đoạn (cửa sổ trượt 3). Khi 1 đoạn xong mới xếp đoạn tiếp theo.
    Không chuyển sang file txt khác nếu file hiện tại chưa hoàn thành (dù còn slot trống).
    """

    def __init__(self, supabase_client, user_id: int, output_dir: str, max_workers: int = None,
                 advanced_settings: dict = None, proxy_service=None, credit_tracker=None) -> None:
        import threading
        self.supabase = supabase_client
        self.user_id = user_id
        self.output_dir = output_dir
        
        # OPTIMIZED: Dynamic max_workers with validation
        if max_workers is None:
            max_workers = TTSConfig.DEFAULT_WORKERS  # Default: 5
        elif max_workers < 1:
            tts_logger.warning(f"max_workers={max_workers} too low, using minimum=1")
            max_workers = 1
        elif max_workers > TTSConfig.MAX_WORKERS:
            tts_logger.warning(f"max_workers={max_workers} exceeds MAX_WORKERS={TTSConfig.MAX_WORKERS}, capping")
            max_workers = TTSConfig.MAX_WORKERS
        os.makedirs(output_dir, exist_ok=True)

        self.credit_tracker = credit_tracker
        self.pool = LocalKeyPool(supabase_client, user_id, accurate_tracker=credit_tracker)
        
        # CRITICAL: Load ALL keys (active + inactive) at initialization for maximum availability
        # This loads 1000+ keys into memory once to avoid repeated Supabase queries
        tts_logger.info(f"KEY_POOL_INIT - Loading ALL keys from database for user {user_id}...")
        self.pool.load(load_all=True)  # Load ALL keys including inactive ones
        tts_logger.info(f"KEY_POOL_INIT_COMPLETE - All keys loaded and ready")
        
        # CRITICAL: Pre-validate top 10 keys to ensure they have real credits
        validated_count = self.pool.pre_validate_keys(max_keys=10)
        tts_logger.info(f"KEY_POOL_VALIDATION - {validated_count} keys validated with real credits")

        # Store validated max_workers (already validated above)
        self.max_workers = max_workers
        self.advanced_settings = advanced_settings or {}
        self.proxy_service = proxy_service
        
        # HTTP/2 Connection Pool Manager (NEW)
        self.http_session = self._create_http2_session() if TTSConfig.HTTP2_ENABLED else None
        
        # Connection Pool Health Manager (NEW)
        try:
            from services.connection_pool_manager import get_connection_pool_manager
            self.pool_health_manager = get_connection_pool_manager()
            tts_logger.info("Connection Pool Health Manager: ENABLED")
        except ImportError:
            self.pool_health_manager = None
            tts_logger.warning("Connection Pool Health Manager: NOT AVAILABLE")

        self.should_stop = False

        # Locks cho an toàn đa luồng
        self.pool_lock = threading.Lock()
        self.callback_lock = threading.Lock()
        
        # Circuit Breaker to prevent infinite loops (Phase 2)
        self.circuit_breaker = CircuitBreaker()
        
        # Retry strategy for operations (Phase 7)
        self.retry_strategy = RetryStrategy(max_retries=3, backoff_factor=2.0)
        
        # TEMPORARY KEY BLACKLIST - Keys that got 401 error are blacklisted for 5 minutes
        # Format: {api_key: expiry_timestamp}
        self._key_blacklist: Dict[str, float] = {}
        self._key_blacklist_lock = threading.Lock()
        self._key_blacklist_ttl = 300  # 5 minutes in seconds
        
        # Use refactored stage executors (Phase 4) - set to True to use new clean code
        self.use_refactored_stages = True  # Toggle: True = new clean code (54 lines), False = original (800 lines)
        
        # Cache for silent audio files to avoid recreating them
        self._silent_file_cache = {}  # {duration: filepath}
        
        # Log initialization
        tts_logger.info(f"TTSTaskRunner initialized - user_id={user_id}, max_workers={self.max_workers}")
        tts_logger.info(f"Circuit Breaker: max_failures={self.circuit_breaker.max_failures}, timeout={self.circuit_breaker.timeout}s")
        tts_logger.info(f"Stage Executors: {'REFACTORED (optimized)' if self.use_refactored_stages else 'ORIGINAL (legacy)'}")
        
        # Log proxy status for debugging
        self._log_proxy_status()
        
        # Log HTTP client mode
        if TTSConfig.HTTP2_ENABLED:
            tts_logger.info(f"HTTP/2 Connection Pooling: ENABLED (pool_size={TTSConfig.CONNECTION_POOL_SIZE})")
        else:
            tts_logger.info("HTTP/2 Connection Pooling: DISABLED (using plain requests)")

    def _sync_pool_after_credit_tracker(self, api_key: str, tracker_result) -> None:
        """
        Align LocalKeyPool cache with AccurateCreditTracker updates so exhausted keys
        are immediately marked inactive in-memory.
        """
        try:
            new_remaining = None
            if isinstance(tracker_result, dict):
                new_remaining = tracker_result.get("new_credits")
            elif isinstance(tracker_result, (tuple, list)) and len(tracker_result) >= 2:
                new_remaining = tracker_result[1]

            if new_remaining is None:
                return

            with self.pool_lock:
                self.pool.sync_external_remaining(api_key, new_remaining)
        except Exception as exc:
            tts_logger.debug(f"POOL_SYNC_SKIP - Could not sync remaining credits: {exc}")
    
    def _log_proxy_status(self) -> None:
        """Log proxy service status for debugging"""
        try:
            if not self.proxy_service:
                tts_logger.info(f"PROXY_STATUS - User {self.user_id}: No proxy service configured")
                return
            
            gateways = self.proxy_service.get_all_gateways()
            if not gateways:
                tts_logger.warning(f"PROXY_STATUS - User {self.user_id}: Proxy service exists but no gateways loaded")
                tts_logger.warning("PROXY_DEBUG - Check: 1) users_token_proxy table has data for this user 2) Proxy service loaded correctly")
            else:
                tts_logger.info(f"PROXY_STATUS - User {self.user_id}: {len(gateways)} gateways available")
                for i, gw in enumerate(gateways):
                    tts_logger.info(f"PROXY_GATEWAY_{i+1} - {gw.host}:{gw.port} (user: {gw.username})")
                    
        except Exception as e:
            tts_logger.error(f"PROXY_STATUS_ERROR - User {self.user_id}: {e}")
    
    def _create_http2_session(self):
        """
        Create HTTP/2 session with connection pooling.
        
        Benefits:
        - Reuse connections (reduce handshake overhead)
        - HTTP/2 multiplexing (multiple requests per connection)
        - Connection pool management
        """
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            
            # Create session
            session = requests.Session()
            
            # Configure retry strategy
            retry_strategy = Retry(
                total=2,  # Max retries
                status_forcelist=[429, 500, 502, 503, 504],  # Retry on these status codes
                backoff_factor=1,  # Wait 1s, 2s, 4s between retries
                raise_on_status=False
            )
            
            # Create adapter with connection pooling
            adapter = HTTPAdapter(
                pool_connections=TTSConfig.CONNECTION_POOL_SIZE,  # Max number of connection pools
                pool_maxsize=TTSConfig.CONNECTION_POOL_MAXSIZE,   # Max connections per pool
                max_retries=retry_strategy,
                pool_block=False  # Don't block when pool is full
            )
            
            # Mount adapter for both http and https
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            
            tts_logger.info("HTTP/2 session created with connection pooling")
            return session
            
        except ImportError as e:
            tts_logger.warning(f"Failed to create HTTP/2 session (requests not available): {e}")
            return None
        except Exception as e:
            tts_logger.error(f"Failed to create HTTP/2 session: {e}")
            return None
    
    def _prepare_utf8_json(self, payload: dict) -> str:
        """Prepare JSON payload with proper UTF-8 encoding for Vietnamese text and curl compatibility"""
        try:
            # Check if text contains Vietnamese characters
            text_preview = payload.get('text', '')
            if len(text_preview) > 100:
                text_preview = text_preview[:100] + "..."
            
            vietnamese_chars = any(ord(c) > 127 for c in text_preview)
            if vietnamese_chars:
                tts_logger.info(f"UTF8_TEXT detected Vietnamese characters in: {text_preview}")
                # Use ASCII-safe encoding for Vietnamese text to avoid curl parsing errors
                json_str = json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
                tts_logger.info(f"UTF8_JSON using ASCII-safe encoding for Vietnamese text")
            else:
                # English text - can use UTF-8 safely
                json_str = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                tts_logger.info(f"UTF8_JSON using UTF-8 encoding for English text")
            
            return json_str
            
        except Exception as e:
            tts_logger.error(f"UTF8_JSON_ERROR: {e}")
            # Always fallback to ASCII-safe encoding
            return json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
    
    def _check_api_error_response(self, file_head: bytes, stage_name: str) -> str:
        """
        Check API error response and map to proper error codes (Phase 9).
        Based on official ElevenLabs error documentation.
        """
        try:
            text_content = file_head.decode('utf-8', errors='ignore')
            text_lower = text_content.lower()
            
            # Map to official ElevenLabs error codes (ORDER MATTERS - specific first)
            
            # 400 errors
            if 'max_character_limit_exceeded' in text_lower:
                return "API_MAX_CHARACTER_LIMIT_EXCEEDED"
            elif ('voice_limit_reached' in text_lower or
                  'maximum amount of custom voices' in text_lower):
                return "API_VOICE_LIMIT_REACHED"
            elif 'voice_not_found' in text_lower or ('voice' in text_lower and 'not found' in text_lower):
                return "API_VOICE_NOT_FOUND"
            elif ('"status":400' in text_lower or 'bad request' in text_lower):
                return "API_BAD_REQUEST"
            
            # 401 errors
            elif 'invalid_api_key' in text_lower or ('invalid' in text_lower and 'api' in text_lower and 'key' in text_lower):
                return "API_INVALID_API_KEY"
            elif ('quota_exceeded' in text_lower or 'exceeds your quota' in text_lower):
                return f"API_QUOTA_EXCEEDED: {text_content[:300].strip()}"
            elif ('unauthorized' in text_lower or '"status":401' in text_lower):
                return "API_UNAUTHORIZED"
            
            # 403 errors
            elif 'only_for_creator' in text_lower:
                return "API_ONLY_FOR_CREATOR_PLUS"
            elif ('"status":403' in text_lower or 'forbidden' in text_lower):
                return "API_FORBIDDEN"
            
            # 429 errors
            elif 'too_many_concurrent_requests' in text_lower:
                return "API_TOO_MANY_CONCURRENT"
            elif 'system_busy' in text_lower:
                return "API_SYSTEM_BUSY"
            elif ('rate limit' in text_lower or '"status":429' in text_lower):
                return "API_RATE_LIMIT"
            
            # Other errors
            elif ('"status":404' in text_lower or 'not found' in text_lower):
                return "API_NOT_FOUND"
            elif ('"status":451' in text_lower or 'legal' in text_lower):
                return "API_LEGAL_BLOCK"
            elif ('proxy authentication' in text_lower or '"status":407' in text_lower):
                return "PROXY_AUTH_REQUIRED"
            elif ('unusual activity' in text_lower):
                return "API_UNUSUAL_ACTIVITY"
            elif ('"detail"' in text_lower or '"error"' in text_lower or '"message"' in text_lower):
                preview = text_content[:200].strip()
                return f"API_ERROR: {preview}"
            else:
                preview = text_content[:100].strip()
                return f"NON_MP3_RESPONSE: {preview}"
                
        except Exception:
            return "BINARY_NON_MP3"
        
    def _format_proxy_cfg(self, proxy_cfg: dict) -> str:
        """Định dạng proxy để log (ẩn mật khẩu)."""
        if not proxy_cfg:
            return "None"
        host = proxy_cfg.get("host")
        port = proxy_cfg.get("port")
        user = proxy_cfg.get("username")
        return f"{host}:{port}, user={user or 'None'}, pass=****"

    def _probe_proxy_ip(self, proxy_cfg: dict, label: str = "") -> Optional[str]:
        """Probe proxy IP using requests instead of curl."""
        import uuid
        try:
            if not proxy_cfg:
                tts_logger.info(f"{label} PROXY_PROBE skip: no proxy_cfg")
                return None
            host, port = proxy_cfg.get("host"), proxy_cfg.get("port")
            user, pwd  = proxy_cfg.get("username"), proxy_cfg.get("password")
            if not (host and port):
                tts_logger.warning(f"{label} PROXY_PROBE invalid cfg: {proxy_cfg}")
                return None

            rnd = uuid.uuid4().hex
            url = f"https://api.ipify.org?format=text&r={rnd}"

            if user:
                proxy_url = f"http://{user}:{pwd}@{host}:{port}"
            else:
                proxy_url = f"http://{host}:{port}"

            proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }


            try:
                resp = requests.get(
                    url,
                    proxies=proxies,
                    timeout=(8, 12),
                    headers={
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
            except RequestException as e:
                tts_logger.error(f"{label} PROXY_PROBE EXC {e}")
                return None

            ip_out = (resp.text or "").strip() if resp.status_code == 200 else ""
            if ip_out:
                tts_logger.info(f"{label} PROXY_PROBE OK ip={ip_out} cfg={self._format_proxy_cfg(proxy_cfg)}")
                return ip_out
            else:
                tts_logger.error(f"{label} PROXY_PROBE FAIL http={resp.status_code} body={(resp.text or '')[:180]} cfg={self._format_proxy_cfg(proxy_cfg)}")
                return None
        except Exception as e:
            tts_logger.error(f"{label} PROXY_PROBE UNEXPECTED {e}")
            return None

    # ---------- utilities (refactored - Phase 6) ----------
    def split_paragraphs(self, content: str) -> List[str]:
        """Delegate to TextChunker for optimized paragraph splitting"""
        return TextChunker.split_paragraphs(content)

    def sanitize_text(self, text: str, model_name: str = None) -> str:
        """Delegate to TextSanitizer - removes {,},<,>,[,] only. For v3 models, preserves emotion tags."""
        return TextSanitizer.sanitize(text, skip_for_non_ascii=False, model_name=model_name)

    def _validate_key_with_retry(self, api_key: str, needed_chars: int, force_check: bool = True,
                                 max_attempts: Optional[int] = None, retry_delay: Optional[float] = None) -> bool:
        """
        Validate API key credits with retry to handle transient API/network issues.
        Returns True if validation succeeds within allowed attempts; otherwise False.
        """
        attempts = max_attempts or TTSConfig.KEY_VALIDATION_RETRIES
        delay = TTSConfig.KEY_VALIDATION_RETRY_DELAY if retry_delay is None else retry_delay
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                if self.validate_api_key_has_credits(api_key, needed_chars, force_check=force_check):
                    if attempt > 1:
                        tts_logger.info(
                            f"KEY_VALIDATE_RETRY_SUCCESS - Key {api_key[:10]}... attempt {attempt}/{attempts}"
                        )
                    return True
                else:
                    tts_logger.warning(
                        f"KEY_VALIDATE_RETRY_FAIL - Key {api_key[:10]}... insufficient credits "
                        f"(attempt {attempt}/{attempts})"
                    )
            except Exception as exc:
                last_error = exc
                tts_logger.warning(
                    f"KEY_VALIDATE_RETRY_EXCEPTION - Key {api_key[:10]}... attempt {attempt}/{attempts} error: {exc}"
                )

            if attempt < attempts:
                try:
                    time.sleep(delay)
                except Exception:
                    pass

        if last_error:
            tts_logger.error(
                f"KEY_VALIDATE_RETRY_GIVEUP - Key {api_key[:10]}... failed after {attempts} attempts "
                f"(last_error={last_error})"
            )
        else:
            tts_logger.error(
                f"KEY_VALIDATE_RETRY_GIVEUP - Key {api_key[:10]}... insufficient credits after {attempts} attempts"
            )
        return False

    def chunk_text_by_credits(self, model_name: str, text: str, max_credits: int = 9998,
                               per_request_char_limit: int = 3000) -> List[str]:
        """Delegate to TextChunker for optimized credit-based chunking"""
        return TextChunker.chunk_by_credits(model_name, text, max_credits, per_request_char_limit)

    def add_ssml_breaks(self, text: str) -> str:
        """
        DISABLED: SSML break injection causes ElevenLabs to read breaks as text.
        
        ElevenLabs naturally pauses at punctuation (.,!?;:) so SSML breaks are not needed.
        For longer pauses between segments, use 'pause_between_segments' in advanced settings
        which inserts silent audio during concatenation - this is more reliable.
        
        Previous issues:
        - SSML breaks were being read as text by ElevenLabs
        - Complex regex patterns caused issues with different languages
        - Duplicate punctuation was being added
        
        Solution: Just return original text without modification.
        """
        # DISABLED - Just return original text
        # If per_char_enabled is True, log a warning that it's disabled
        if self.advanced_settings.get('per_char_enabled'):
            tts_logger.warning("SSML_BREAKS_DISABLED - 'per_char' feature is disabled due to ElevenLabs compatibility issues. Use 'pause_between_segments' instead.")
        
        return text

    def validate_api_key_has_credits(self, api_key: str, needed_chars: int, force_check: bool = False) -> bool:
        """
        STRICT: Credit validation using REAL-TIME API check.
        
        🔧 FIX: LUÔN LUÔN check API thực tế, KHÔNG dựa vào cache.
        Cache có thể outdated → gây quota_exceeded.
        
        Args:
            api_key: API key to validate
            needed_chars: Characters needed for request
            force_check: IGNORED - always check API
        
        Returns:
            True if key has REAL sufficient credits from API
        """
        try:
            # ALWAYS check REAL credits from API
            with self.pool_lock:
                api_result = self.pool._get_real_credits_via_api(api_key)
            
            if api_result is None:
                tts_logger.error(f"CREDIT_CHECK_API_FAIL - {api_key[:10]}... API call failed")
                return False
            
            real_credits, voice_limit_info = api_result
            
            # Calculate buffer (20% for large requests)
            if needed_chars <= 50:
                buffer = 25
            elif needed_chars <= 200:
                buffer = max(40, int(needed_chars * 0.25))
            else:
                buffer = max(80, int(needed_chars * 0.20))
            
            total_needed = needed_chars + buffer
            
            # Check if sufficient
            has_credits = real_credits >= total_needed
            
            tts_logger.info(
                f"CREDIT_CHECK_REAL - Key {api_key[:10]}... has {real_credits:,} credits, "
                f"needs {total_needed:,} (text={needed_chars}, buffer={buffer}) → {'✅ OK' if has_credits else '❌ INSUFFICIENT'}"
            )
            
            # Update cache in pool
            if has_credits:
                with self.pool_lock:
                    # Update local cache để không check lại lần sau
                    entry = self.pool._by_api.get(api_key)
                    if entry:
                        entry.credit_remaining = real_credits
                        entry.last_checked = _utcnow()
            
            return has_credits
        
        except Exception as e:
            tts_logger.error(f"CREDIT_CHECK_EXCEPTION - {api_key[:10]}... error: {e}")
            return False

    def validate_voice_id(self, api_key: str, voice_id: str) -> bool:
        """
        Gọi thử TTS ngắn với voice_id để xác minh access với UTF-8 text.
        """
        try:
            
            test_payload = {
                "text": "Xin chào - Hello",  # Test both Vietnamese and English
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            }
            
            url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}'
            headers = {
                "xi-api-key": api_key,
                "Content-Type": "application/json"
            }
            
            response = requests.post(url, json=test_payload, headers=headers, timeout=(5, 15))
            
            # Check if response is valid (200 OK and has content)
            if response.status_code == 200:
                # Consume a small amount to verify it's MP3
                chunk = response.raw.read(512)
                if chunk and self._is_valid_mp3_head(chunk):
                    return True
            return False
        except RequestException:
            return False
        except Exception:
            return False

    # ---------- HTTP requests helpers (replaced curl) ----------
    def _make_http_request(self, url: str, api_key: str, payload_json: str, out_path: str,
                          proxy: Optional[dict] = None, timeout: Optional[tuple] = None,
                          connect_timeout: Optional[int] = None) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Make HTTP POST request using requests library (replaced curl).
        
        Args:
            url: Target URL
            api_key: ElevenLabs API key
            payload_json: JSON payload as string
            out_path: Output file path for MP3
            proxy: Optional proxy dict with host, port, username, password
            timeout: Optional (connect_timeout, read_timeout) tuple
            connect_timeout: Optional connection timeout in seconds
            
        Returns:
            Tuple[success: bool, error_message: Optional[str], response_bytes: Optional[bytes]]
            - success: True if request succeeded and file is valid MP3
            - error_message: Error description if failed
            - response_bytes: Response content (first 512 bytes for validation)
        """
        
        # Ensure output directory exists
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
        except Exception:
            pass
        
        # Prepare headers
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json"
        }
        
        # Parse JSON payload
        try:
            import json
            payload_dict = json.loads(payload_json)
        except Exception as e:
            return (False, f"JSON_PARSE_ERROR: {e}", None)
        
        # Prepare proxy configuration (same style as check_live.py)
        proxies = None
        if proxy and proxy.get("host") and proxy.get("port"):
            proxy_url = f"http://{proxy['host']}:{proxy['port']}"
            if proxy.get("username"):
                # Proxy with authentication
                proxy_url = f"http://{proxy['username']}:{proxy.get('password', '')}@{proxy['host']}:{proxy['port']}"
            # Use proxy for both HTTP and HTTPS, giống check_live.py
            proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }
        
        # Set timeouts
        if timeout:
            request_timeout = timeout
        elif connect_timeout:
            request_timeout = (connect_timeout, TTSConfig.CURL_MAX_TIME)
        else:
            request_timeout = (TTSConfig.CURL_CONNECT_TIMEOUT, TTSConfig.CURL_MAX_TIME)
        
        # Log request (hide sensitive data)
        proxy_info = f"via {proxy['host']}:{proxy['port']}" if proxy else "direct"
        tts_logger.info(f"HTTP_REQUEST - POST {url} {proxy_info} (timeout={request_timeout})")
        
        try:
            # Make POST request
            response = requests.post(
                url,
                json=payload_dict,
                headers=headers,
                proxies=proxies,
                timeout=request_timeout,
            )
            
            # Check status code
            if response.status_code != 200:
                error_text = response.text[:200] if response.text else ""
                return (False, f"HTTP_{response.status_code}: {error_text}", None)
            
            # Write response to file
            try:
                with open(out_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            except Exception as e:
                return (False, f"FILE_WRITE_ERROR: {e}", None)
            
            # Validate file exists and has content
            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                return (False, "EMPTY_FILE", None)
            
            # Read first 512 bytes for validation
            try:
                with open(out_path, 'rb') as f:
                    file_head = f.read(512)
            except Exception as e:
                return (False, f"FILE_READ_ERROR: {e}", None)
            
            # Validate MP3 header
            if not self._is_valid_mp3_head(file_head):
                return (False, "INVALID_MP3", file_head)
            
            tts_logger.info(f"HTTP_REQUEST_SUCCESS - {os.path.getsize(out_path)} bytes written to {out_path}")
            return (True, None, file_head)
            
        except Timeout as e:
            return (False, f"TIMEOUT: {e}", None)
        except ConnectionError as e:
            return (False, f"CONNECTION_ERROR: {e}", None)
        except RequestException as e:
            return (False, f"REQUEST_ERROR: {e}", None)
        except Exception as e:
            return (False, f"UNEXPECTED_ERROR: {e}", None)
    
    
    def _get_fresh_api_key_for_canada_isp(self, current_key: str, payload_json: str) -> Optional[str]:
        """
        Get fresh API key for canada_isp retry when quota exceeded.
        
        Strategy:
        1. Mark current key as exhausted
        2. Get fresh key with sufficient credits
        3. Validate fresh key has enough credits for the request
        """
        try:
            # Mark current key as exhausted
            with self.pool_lock:
                self.pool.mark_exhausted(current_key)
                self.pool.sync_state_changes_immediate()
            
            # Extract text length from payload_json
            try:
                import json
                payload = json.loads(payload_json)
                text = payload.get('text', '')
                needed_chars = len(text)
            except Exception:
                needed_chars = 200  # Fallback estimate
            
            # Get fresh key with sufficient credits
            fresh_key = self._get_fresh_api_key(needed_chars, exclude_keys={current_key})
            
            if fresh_key:
                # Validate fresh key has real credits
                api_result = self.pool._get_real_credits_via_api(fresh_key)
                if api_result is not None:
                    real_credits, voice_limit_info = api_result
                    if real_credits >= needed_chars + 50:  # Buffer
                        tts_logger.info(f"CANADA_ISP_FRESH_KEY - {fresh_key[:10]}... has {real_credits} credits")
                        return fresh_key
                    else:
                        tts_logger.warning(f"CANADA_ISP_FRESH_KEY_INSUFFICIENT - {fresh_key[:10]}... has {real_credits} credits (need {needed_chars + 50})")
                        return None
                else:
                    tts_logger.warning(f"CANADA_ISP_FRESH_KEY_API_FAILED - {fresh_key[:10]}... API check failed")
                    return None
            else:
                tts_logger.error("CANADA_ISP_FRESH_KEY - No fresh key available")
                return None
                
        except Exception as e:
            tts_logger.error(f"CANADA_ISP_FRESH_KEY_ERROR: {e}")
            return None
    
    # ---------- KEY BLACKLIST METHODS ----------
    def _blacklist_key(self, api_key: str, reason: str = "401") -> None:
        """Blacklist a key temporarily (5 minutes) to prevent it from being used."""
        import time
        with self._key_blacklist_lock:
            expiry = time.time() + self._key_blacklist_ttl
            self._key_blacklist[api_key] = expiry
            tts_logger.warning(f"KEY_BLACKLISTED - {api_key[:10]}... reason={reason}, expires in {self._key_blacklist_ttl}s")
    
    def _is_key_blacklisted(self, api_key: str) -> bool:
        """Check if a key is currently blacklisted."""
        import time
        with self._key_blacklist_lock:
            if api_key not in self._key_blacklist:
                return False
            expiry = self._key_blacklist[api_key]
            if time.time() >= expiry:
                # Expired, remove from blacklist
                del self._key_blacklist[api_key]
                return False
            return True
    
    def _cleanup_blacklist(self) -> None:
        """Remove expired keys from blacklist."""
        import time
        with self._key_blacklist_lock:
            now = time.time()
            expired = [k for k, exp in self._key_blacklist.items() if now >= exp]
            for k in expired:
                del self._key_blacklist[k]
            if expired:
                tts_logger.info(f"BLACKLIST_CLEANUP - Removed {len(expired)} expired keys")

    def _is_valid_mp3_head(self, first_bytes: bytes) -> bool:
        if first_bytes.startswith(b'ID3'):
            return True
        syncs = (b'\xff\xfb', b'\xff\xfa', b'\xff\xf3', b'\xff\xf2')
        return any(first_bytes[i:i+2] in syncs for i in range(min(100, len(first_bytes)-1)))

    # ---------- helper methods ----------
    def _get_fresh_api_key(self, needed_chars: int, exclude_keys: set = None) -> Optional[str]:
        """
        Lấy API key mới **nhanh** từ pool, ưu tiên key có nhiều credits nhất.
        
        - Không còn vòng lặp validate/phải gọi API nhiều lần.
        - Dựa hoàn toàn vào `LocalKeyPool.get_key` (đã sort theo credits giảm dần).
        - Chỉ skip các key đã nằm trong `exclude_keys` hoặc không usable.
        - NEW: Also skip keys in temporary blacklist (401 errors).
        """
        exclude_keys = exclude_keys or set()
        
        # Periodically cleanup expired blacklist entries
        self._cleanup_blacklist()
        
        # Bộ key đang dùng song song (để tránh trùng)
        concurrent_in_use = getattr(self, "_concurrent_in_use_keys", set())
        
        # Add blacklisted keys to exclude set
        with self._key_blacklist_lock:
            blacklisted = set(self._key_blacklist.keys())
        
        tts_logger.info(
            f"FRESH_KEY_REQUEST - needed={needed_chars} chars, "
            f"excluding={len(exclude_keys)} keys, blacklisted={len(blacklisted)}, concurrent_in_use={len(concurrent_in_use)}"
        )

        # Giới hạn vòng lặp bằng số key hiện có để tránh vòng lặp vô hạn,
        # nhưng **không** giới hạn theo số lần 401 ở phía trên.
        try:
            total_keys = len(getattr(self.pool, "_keys", [])) or 50
        except Exception:
            total_keys = 50

        attempts = 0
        while attempts < total_keys:
            attempts += 1
            with self.pool_lock:
                api_key = self.pool.get_key(
                    needed_chars,
                    excluded=exclude_keys.union(concurrent_in_use).union(blacklisted),
                ) or self.pool.get_any_active_key()

            if not api_key:
                tts_logger.warning("FRESH_KEY_POOL - No key available from pool")
                break

            if api_key in exclude_keys:
                continue
            
            # CRITICAL: Skip blacklisted keys (401 errors)
            if self._is_key_blacklisted(api_key):
                tts_logger.debug(f"FRESH_KEY_SKIP_BLACKLISTED - {api_key[:10]}... is blacklisted, skipping")
                exclude_keys.add(api_key)
                continue

            # Nếu key đang usable thì dùng luôn, không gọi validate thêm lần nữa
            try:
                if hasattr(self.pool, "is_key_usable") and not self.pool.is_key_usable(api_key):
                    exclude_keys.add(api_key)
                    continue
            except Exception:
                # Nếu check lỗi thì vẫn thử dùng để tránh chặn luồng
                pass

            tts_logger.info(f"FRESH_KEY_SUCCESS - {api_key[:10]}... selected on attempt {attempts}")
            return api_key
                        
        tts_logger.error(f"FRESH_KEY_EXHAUSTED - Tried {len(exclude_keys)} excluded keys, {len(blacklisted)} blacklisted, no usable key found")
        self._log_pool_status()
        return None
    
    def _log_pool_status(self) -> None:
        """Log current key pool status with state breakdown (Phase 1)"""
        try:
            with self.pool_lock:
                keys = getattr(self.pool, '_keys', [])
                total_keys = len(keys)
                
                # Count by state
                state_counts = {}
                for k in keys:
                    state = getattr(k, 'state', KeyState.ACTIVE)
                    state_name = state.value if isinstance(state, KeyState) else str(state)
                    state_counts[state_name] = state_counts.get(state_name, 0) + 1
                
                active_keys = len([k for k in keys if getattr(k, 'is_active', True)])
                
                tts_logger.info(
                    f"KEY_POOL_STATUS - Total: {total_keys}, Active: {active_keys}, "
                    f"States: {state_counts}"
                )
                
                if active_keys == 0:
                    tts_logger.error("KEY_POOL_CRITICAL - No active keys remaining!")
                    tts_logger.error("SOLUTION: 1) Add more API keys 2) Check key credits 3) Wait for quota reset")
                elif active_keys <= 2:
                    tts_logger.warning(f"KEY_POOL_LOW - Only {active_keys} active keys remaining")
                    
        except Exception as e:
            tts_logger.error(f"KEY_POOL_STATUS_ERROR - {e}")

    # ===== SIMPLIFIED: Single-phase TTS with proxy (bỏ logic 3-stage phức tạp) =====
    
    def _call_tts_simple_with_proxy(self, api_key: str, voice_id: str, payload: dict, 
                                     out_path: str, on_status_update=None) -> str:
        """
        SIMPLIFIED TTS call - CHỈ 1 PHASE với proxy (unlimited bandwidth).
        
        Bỏ hết logic:
        - Stage 1 (no proxy)
        - Stage 2 (with proxy)  
        - Stage 3 (failover)
        - Canada 2-phase (generate + download)
        - Token proxy types (canada, canada_isp, vps, etc.)
        
        CHỈ CÒN:
        - 1 POST request to /v1/text-to-speech/{voice_id}
        - LUÔN LUÔN dùng proxy (từ database users_token_proxy)
        - Retry với proxy rotation on failure
        
        Migrated từ 11Labs0811.py tts_direct() logic.
        """
        from services import tts_service_canada
        
        # Extract text from payload
        text = payload.get("text", "")
        model_id = payload.get("model_id", "")
        voice_settings = payload.get("voice_settings", {})
        language_code = payload.get("language_code")
        
        # Status update
        if on_status_update:
            try:
                on_status_update("synthesizing", "Synthesizing audio with proxy...")
            except:
                pass
        
        # Call simplified TTS (tự động dùng proxy từ proxy_service)
        success, message = tts_service_canada.tts_synthesize_simple(
            api_key=api_key,
            voice_id=voice_id,
            text=text,
            model_id=model_id,
            voice_settings=voice_settings,
            output_path=out_path,
            proxy_service=self.proxy_service,
            language_code=language_code,
            max_retries=3
        )
        
        if not success:
            tts_logger.error(f"TTS_SIMPLE_FAILED - {message}")
            raise RuntimeError(f"TTS synthesis failed: {message}")
        
        tts_logger.info(f"TTS_SIMPLE_SUCCESS - {message}")
        return api_key  # Return API key used
    
    # ===================================================================
    # DEPRECATED METHODS - OLD 3-STAGE & 2-PHASE LOGIC
    # Kept for reference only. DO NOT USE.
    # Use _call_tts_simple_with_proxy() instead.
    # ===================================================================
    
    def _execute_stage1_no_proxy(self, url: str, api_key: str, payload_json: str, 
                                  out_path: str, used_keys: set) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Stage 1: Try without proxy.
        
        CRITICAL: Validate key credits BEFORE attempting Stage 1 to prevent quota_exceeded.
        OPTIMIZED: Try HTTP/2 session first (faster), fallback to curl if unavailable.
        Returns: (success, used_api_key, error_type)
        """
        current_key = api_key
        
        # CRITICAL: Validate key has real credits BEFORE Stage 1
        needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
        
        # Get real credits from key pool (uses real-time API validation)
        with self.pool_lock:
            api_result = self.pool._get_real_credits_via_api(current_key)
        
        if api_result is not None:
            real_credits, voice_limit_info = api_result
            # Calculate buffer
            if needed_chars <= 50:
                buffer = 25
            elif needed_chars <= 100:
                buffer = max(30, int(needed_chars * 0.30))
            elif needed_chars <= 200:
                buffer = max(40, int(needed_chars * 0.25))
            elif needed_chars <= 500:
                buffer = max(60, int(needed_chars * 0.20))
            else:
                buffer = max(80, int(needed_chars * 0.15))
            
            total_needed = needed_chars + buffer
            
            if real_credits < total_needed:
                # Exhaust key ONLY when its real credits are truly 0
                if real_credits == 0:
                    tts_logger.error(
                        f"STAGE1_PRE_CHECK - Key {current_key[:10]}... has 0 credits, marking exhausted"
                    )
                    try:
                        with self.pool_lock:
                            self.pool.mark_exhausted(current_key)
                            # Sync immediately so other threads don't reuse this zero-credit key
                            self.pool.sync_state_changes_immediate()
                    except Exception:
                        pass
                    return (False, current_key, "API_QUOTA_EXCEEDED: Pre-check failed")

                # Credits > 0 nhưng không đủ cho request này -> skip, KHÔNG đánh exhausted
                tts_logger.warning(
                    f"STAGE1_PRE_CHECK - Key {current_key[:10]}... has {real_credits} credits but needs {total_needed} - Skipping (not exhausted)"
                )
                return (False, current_key, "INSUFFICIENT_CREDITS: Pre-check failed")
        else:
            tts_logger.warning(
                f"STAGE1_PRE_CHECK - Failed to get real credits for key {current_key[:10]}..., proceeding with caution"
            )
        
        tts_logger.info(f"STAGE1 start (no proxy) with key {current_key[:10]}...")
        
        # OPTIMIZATION: Try HTTP/2 first (faster with connection pooling)
        if TTSConfig.HTTP2_ENABLED and self.http_session:
            try:
                import json
                payload_dict = json.loads(payload_json)
                tts_logger.debug("STAGE1 - Trying HTTP/2 connection pooling")
                
                if self._http2_tts_request(url, current_key, payload_dict, out_path, proxy=None, timeout=180):
                    tts_logger.info("STAGE1 success via HTTP/2")
                    return (True, current_key, None)
                else:
                    tts_logger.debug("STAGE1 - HTTP/2 failed, fallback to curl")
            except Exception as e:
                tts_logger.debug(f"STAGE1 - HTTP/2 exception, fallback to curl: {e}")
        
        # Fallback to HTTP request
        success, error_msg, file_head = self._make_http_request(
            url, current_key, payload_json, out_path, proxy=None
        )
        
        # Check if succeeded
        if success and file_head:
            tts_logger.info("STAGE1 success")
            return (True, current_key, None)
        elif file_head:
            # Check for API errors
            error_info = self._check_api_error_response(file_head, "STAGE1")
            tts_logger.error(f"STAGE1 API error: {error_info}")
            
            # Handle specific errors immediately
            if error_info == "API_VOICE_LIMIT_REACHED":
                fresh_key = self._handle_voice_limit_error(current_key, "STAGE1", 1000)
                if fresh_key and fresh_key != current_key:
                    current_key = fresh_key
                    used_keys.add(fresh_key)
                    # Immediate retry
                    success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                        url, current_key, payload_json, out_path, proxy=None
                    )
                    if success_retry and file_head_retry and self._is_valid_mp3_head(file_head_retry):
                        tts_logger.info("STAGE1 - VOICE_LIMIT - IMMEDIATE SUCCESS!")
                        return (True, current_key, None)
            
            try:
                os.unlink(out_path)
            except Exception:
                pass
            return (False, current_key, error_info)
        
        # Stage 1 failed
        tts_logger.error(f"STAGE1 fail: {error_msg}")
        try:
            os.unlink(out_path)
        except Exception:
            pass
        
        return (False, current_key, f"STAGE1_NETWORK_ERROR: {error_msg}")
    
    def _execute_stage2_with_specific_proxy(self, url: str, current_key: str, payload_json: str,
                                            out_path: str, used_keys: set, proxy_cfg: dict,
                                            on_status_update=None, proxy_url: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Execute Stage 2 với proxy cụ thể (dùng cho random proxy per segment).
        Returns: (success, used_api_key, error_type)
        proxy_url: URL gốc của proxy (để trả về queue sau khi dùng)
        """
        if not proxy_cfg:
            return (False, current_key, "NO_PROXY_CFG")
        
        # Lấy token_proxy từ proxy_cfg nếu có, hoặc từ GatewayCfg nếu được pass
        proxy_type = proxy_cfg.get("token_proxy", "standard").lower() if isinstance(proxy_cfg, dict) else "standard"
        tts_logger.info(f"STAGE2_SPECIFIC_PROXY - Using proxy: {proxy_cfg.get('host')}:{proxy_cfg.get('port')} (type: {proxy_type})")
        
        # Tạo GatewayCfg từ proxy_cfg để tương thích với các method khác
        from services.proxy_service import GatewayCfg
        gw = GatewayCfg(
            host=proxy_cfg.get("host", ""),
            port=str(proxy_cfg.get("port", "")),
            username=proxy_cfg.get("username", ""),
            password=proxy_cfg.get("password", ""),
            token_proxy=proxy_type
        )
        
        # Thử với Canada proxy (2-phase)
        if proxy_type == 'canada':
            success, used_key, is_401, is_blocked, is_timeout = self._try_canada_proxy(
                url, current_key, payload_json, out_path, proxy_cfg, gw, on_status_update=on_status_update
            )
            if success:
                # Trả proxy về queue sau khi thành công (giống check_live.py)
                if proxy_url and self.proxy_service and hasattr(self.proxy_service, 'return_proxy_url'):
                    try:
                        self.proxy_service.return_proxy_url(proxy_url)
                        tts_logger.debug(f"STAGE2_SPECIFIC_PROXY-CANADA_PROXY_RETURNED - Returned proxy_url='{proxy_url}' to queue")
                    except Exception:
                        pass
                return (True, used_key, None)
            elif is_401 or is_blocked or is_timeout:
                # 401/blocked/timeout đều cần XOAY PROXY
                reason = "401" if is_401 else ("blocked" if is_blocked else "timeout")
                tts_logger.warning(f"STAGE2_SPECIFIC_PROXY-CANADA {reason} - Rotating to next proxy...")
                
                # CRITICAL: Trả proxy về queue ngay cả khi có 401 (vì 401 có thể do key bị block, không phải proxy)
                # Chỉ KHÔNG trả về nếu proxy bị blocked (unusual_activity)
                if proxy_url and not is_blocked and self.proxy_service and hasattr(self.proxy_service, 'return_proxy_url'):
                    try:
                        self.proxy_service.return_proxy_url(proxy_url)
                        tts_logger.debug(f"STAGE2_SPECIFIC_PROXY-CANADA_PROXY_RETURNED_ON_{reason.upper()} - Returned proxy_url='{proxy_url}' to queue")
                    except Exception:
                        pass
                
                # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                if self.proxy_service:
                    next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                    if next_gw:
                        tts_logger.info(f"STAGE2_SPECIFIC_PROXY-CANADA_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                        proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                        
                        # Retry với proxy mới
                        success_retry, used_key_retry, is_401_retry, is_blocked_retry, is_timeout_retry = self._try_canada_proxy(
                            url, current_key, payload_json, out_path, proxy_cfg_next, next_gw, on_status_update=on_status_update
                        )
                        if success_retry:
                            tts_logger.info(f"STAGE2_SPECIFIC_PROXY-CANADA_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                            return (True, used_key_retry, None)
                        elif is_401_retry or is_blocked_retry or is_timeout_retry:
                            tts_logger.warning(f"STAGE2_SPECIFIC_PROXY-CANADA_ROTATE failed - {next_gw.host}:{next_gw.port}, will continue rotating")
                
                return (False, used_key, "PROXY_401_OR_BLOCKED_OR_TIMEOUT")
            # Trả proxy về queue nếu không phải lỗi fatal
            if proxy_url and self.proxy_service and hasattr(self.proxy_service, 'return_proxy_url'):
                try:
                    self.proxy_service.return_proxy_url(proxy_url)
                    tts_logger.debug(f"STAGE2_SPECIFIC_PROXY-CANADA_PROXY_RETURNED_ON_FAIL - Returned proxy_url='{proxy_url}' to queue")
                except Exception:
                    pass
            return (False, used_key, "CANADA_PROXY_FAILED")
        
        # Thử với Canada ISP proxy (single-phase)
        elif proxy_type == 'canada_isp':
            success, used_key, is_401 = self._try_canada_isp_proxy(url, current_key, payload_json, out_path, proxy_cfg, gw)
            if success:
                return (True, used_key, None)
            elif is_401:
                tts_logger.warning(f"STAGE2_SPECIFIC_PROXY-CANADA_ISP 401 - Rotating to next proxy...")
                
                # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                if self.proxy_service:
                    next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                    if next_gw:
                        tts_logger.info(f"STAGE2_SPECIFIC_PROXY-CANADA_ISP_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                        proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                        
                        # Retry với proxy mới
                        success_retry, used_key_retry, is_401_retry = self._try_canada_isp_proxy(
                            url, current_key, payload_json, out_path, proxy_cfg_next, next_gw
                        )
                        if success_retry:
                            tts_logger.info(f"STAGE2_SPECIFIC_PROXY-CANADA_ISP_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                            return (True, used_key_retry, None)
                        elif is_401_retry:
                            tts_logger.warning(f"STAGE2_SPECIFIC_PROXY-CANADA_ISP_ROTATE 401 - {next_gw.host}:{next_gw.port} also returned 401, will continue rotating")
                
                return (False, used_key, "PROXY_401")
            return (False, used_key, "CANADA_ISP_PROXY_FAILED")
        
        # Standard proxy (single-phase)
        else:
            if on_status_update:
                try:
                    on_status_update("downloading", f"Downloading via {proxy_type} proxy...")
                except Exception:
                    pass
            
            success, error_msg, file_head = self._make_http_request(
                url, current_key, payload_json, out_path, proxy=proxy_cfg
            )
            
            if success and file_head:
                tts_logger.info(f"STAGE2_SPECIFIC_PROXY success {proxy_cfg.get('host')}:{proxy_cfg.get('port')}")
                return (True, current_key, None)
            elif file_head:
                error_info = self._check_api_error_response(file_head, f"STAGE2_SPECIFIC_PROXY-{proxy_cfg.get('host')}:{proxy_cfg.get('port')}")
                tts_logger.error(f"STAGE2_SPECIFIC_PROXY API error: {error_info}")
                
                # Check for 401 error
                is_401_error = False
                if error_info == "API_UNAUTHORIZED" or "401" in error_info or "unauthorized" in error_info.lower():
                    is_401_error = True
                
                if is_401_error:
                    tts_logger.warning(f"STAGE2_SPECIFIC_PROXY 401 - Rotating to next proxy...")
                    
                    # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                    if self.proxy_service:
                        next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                        if next_gw:
                            tts_logger.info(f"STAGE2_SPECIFIC_PROXY_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                            proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                            
                            # Retry với proxy mới
                            if on_status_update:
                                try:
                                    on_status_update("downloading", f"Retrying with rotated proxy {next_gw.host}:{next_gw.port}...")
                                except Exception:
                                    pass
                            success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                                url, current_key, payload_json, out_path, proxy=proxy_cfg_next
                            )
                            if success_retry and file_head_retry:
                                tts_logger.info(f"STAGE2_SPECIFIC_PROXY_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                                return (True, current_key, None)
                            elif file_head_retry:
                                error_info_retry = self._check_api_error_response(file_head_retry, f"STAGE2_SPECIFIC_PROXY_ROTATE-{next_gw.host}:{next_gw.port}")
                                if error_info_retry == "API_UNAUTHORIZED" or "401" in error_info_retry:
                                    tts_logger.warning(f"STAGE2_SPECIFIC_PROXY_ROTATE 401 - {next_gw.host}:{next_gw.port} also returned 401, will continue rotating")
                            try:
                                os.unlink(out_path)
                            except Exception:
                                pass
                
                try:
                    os.unlink(out_path)
                except Exception:
                    pass
                return (False, current_key, error_info)
            
            try:
                os.unlink(out_path)
            except Exception:
                pass
            
            return (False, current_key, f"STAGE2_SPECIFIC_PROXY_FAILED: {error_msg}")
    
    def _execute_stage2_with_proxy(self, url: str, current_key: str, payload_json: str,
                                    out_path: str, used_keys: set, used_proxies: set = None, on_status_update=None) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Stage 2: Try with proxy gateways in priority order.
        Mỗi request dùng proxy khác nhau, không retry cùng proxy.
        Returns: (success, used_api_key, error_type)
        """
        if used_proxies is None:
            used_proxies = set()
        
        if not self.proxy_service:
            tts_logger.error("STAGE2_SKIP - No proxy service configured")
            return (False, current_key, "NO_PROXY_SERVICE")
        
        gateways = self.proxy_service.get_all_gateways()
        if not gateways:
            tts_logger.error("STAGE2_SKIP - No proxy gateways available")
            return (False, current_key, "NO_PROXY_GATEWAYS")
        
        # SIMPLIFIED: Always use all proxies, no "used_proxies" tracking
        # IP rotation is done via reset link, not by switching proxies
        available_gateways = gateways
        
        # CHỈ sử dụng proxy từ users_token_proxy (không dùng user_proxy_pool nữa)
        # Tách riêng các loại proxy từ users_token_proxy
        canada_gateways = []
        other_gateways = []
        
        for gw in gateways:
            token_proxy = getattr(gw, 'token_proxy', '').lower()
            if token_proxy == 'canada':
                canada_gateways.append(gw)
            else:
                other_gateways.append(gw)
        
        # Thứ tự ưu tiên: canada_gateways -> other_gateways
        # ƯU TIÊN: Thử canada proxy từ users_token_proxy trước (2-phase flow)
        for gw in canada_gateways:
            proxy_id = f"{gw.host}:{gw.port}"
            if proxy_id in used_proxies:
                continue  # Skip proxy đã dùng
            
            proxy_type = getattr(gw, 'token_proxy', 'unknown').lower()
            proxy_cfg = self.proxy_service.as_proxy_dict(gw)
            
            # Mark proxy as used
            # used_proxies.add(proxy_id)  # DISABLED: Only use IP rotation via reset link
            
            # Chỉ thử 1 lần, không retry cùng proxy
            tts_logger.info(f"STAGE2 trying {gw.host}:{gw.port} ({proxy_type}) with key {current_key[:10]}...")

            # CRITICAL: Validate key has real credits BEFORE each attempt
            needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))

            with self.pool_lock:
                api_result = self.pool._get_real_credits_via_api(current_key)

            if api_result is not None:
                real_credits, voice_limit_info = api_result
                # Calculate buffer
                if needed_chars <= 50:
                    buffer = 25
                elif needed_chars <= 100:
                    buffer = max(30, int(needed_chars * 0.30))
                elif needed_chars <= 200:
                    buffer = max(40, int(needed_chars * 0.25))
                elif needed_chars <= 500:
                    buffer = max(60, int(needed_chars * 0.20))
                else:
                    buffer = max(80, int(needed_chars * 0.15))

                total_needed = needed_chars + buffer

                if real_credits < total_needed:
                    # Không đánh exhausted: chỉ bỏ qua key này cho request hiện tại
                    tts_logger.warning(f"STAGE2_PRE_CHECK - Key {current_key[:10]}... has {real_credits} credits but needs {total_needed} - Skipping (no exhausted mark)")

                    # Lấy key mới, không đánh exhausted
                    used_keys.add(current_key)
                    fresh_key = self._get_fresh_api_key(needed_chars, exclude_keys=used_keys)
                    if fresh_key and fresh_key != current_key:
                        tts_logger.info(f"STAGE2_PRE_CHECK - Got new key {fresh_key[:10]}... (replacing low-credit key)")
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                    else:
                        tts_logger.error(f"STAGE2_PRE_CHECK - No fresh key available, skipping this proxy")
                        continue  # Skip this proxy, try next
                else:
                    tts_logger.info(f"STAGE2_PRE_CHECK ✅ - Key {current_key[:10]}... has {real_credits} credits (need {total_needed})")
                
            # Try Canada proxy (2-phase flow) - chỉ thử 1 lần
            success, used_key, is_401, is_blocked, is_timeout = self._try_canada_proxy(
                url, current_key, payload_json, out_path, proxy_cfg, gw, on_status_update=on_status_update
            )
            if success:
                tts_logger.info(f"STAGE2-CANADA SUCCESS {gw.host}:{gw.port}")
                return (True, used_key, None)
            elif is_401 or is_blocked or is_timeout:
                # 401/blocked/timeout đều cần XOAY PROXY
                reason = "401" if is_401 else ("blocked" if is_blocked else "timeout")
                tts_logger.warning(f"STAGE2-CANADA {reason} - Rotating to next proxy...")
                
                # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                if next_gw:
                    tts_logger.info(f"STAGE2-CANADA_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                    proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                    
                    # Retry với proxy mới
                    success_retry, used_key_retry, is_401_retry, is_blocked_retry, is_timeout_retry = self._try_canada_proxy(
                        url, current_key, payload_json, out_path, proxy_cfg_next, next_gw, on_status_update=on_status_update
                    )
                    if success_retry:
                        tts_logger.info(f"STAGE2-CANADA_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                        return (True, used_key_retry, None)
                    elif is_401_retry or is_blocked_retry or is_timeout_retry:
                        tts_logger.warning(f"STAGE2-CANADA_ROTATE failed - {next_gw.host}:{next_gw.port}, will continue rotating")
                
                continue  # Không retry cùng proxy, thử proxy tiếp theo trong loop
            else:
                tts_logger.warning(f"STAGE2-CANADA FAILED {gw.host}:{gw.port}, trying next proxy...")
                continue  # Không retry cùng proxy, thử proxy tiếp theo
        
        # Now try other proxy types (Canada ISP, Standard, VPS)
        for gw in other_gateways:
            proxy_id = f"{gw.host}:{gw.port}"
            if proxy_id in used_proxies:
                continue  # Skip proxy đã dùng
            
            # Mark proxy as used
            # used_proxies.add(proxy_id)  # DISABLED: Only use IP rotation via reset link
            proxy_type = getattr(gw, 'token_proxy', 'unknown').lower()
            proxy_cfg = self.proxy_service.as_proxy_dict(gw)
            
            # CRITICAL: Validate key has real credits BEFORE Stage 2
            needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
            
            with self.pool_lock:
                api_result = self.pool._get_real_credits_via_api(current_key)
            
            if api_result is not None:
                real_credits, voice_limit_info = api_result
                # Calculate buffer
                if needed_chars <= 50:
                    buffer = 25
                elif needed_chars <= 100:
                    buffer = max(30, int(needed_chars * 0.30))
                elif needed_chars <= 200:
                    buffer = max(40, int(needed_chars * 0.25))
                elif needed_chars <= 500:
                    buffer = max(60, int(needed_chars * 0.20))
                else:
                    buffer = max(80, int(needed_chars * 0.15))
                
                total_needed = needed_chars + buffer
                
                if real_credits < total_needed:
                    # Không đánh exhausted: chỉ bỏ qua key này cho request hiện tại
                    tts_logger.warning(f"STAGE2_PRE_CHECK - Key {current_key[:10]}... has {real_credits} credits but needs {total_needed} - Skipping {gw.host}:{gw.port} (no exhausted mark)")
                    continue  # Skip this gateway, try next
                else:
                    tts_logger.info(f"STAGE2_PRE_CHECK ✅ - Key {current_key[:10]}... has {real_credits} credits (need {total_needed})")
            
            # Try with this gateway
            tts_logger.info(f"STAGE2 trying {gw.host}:{gw.port} ({proxy_type}) with key {current_key[:10]}...")
            
            # Check for 401 error in response
            is_401_error = False
            
            # Special handling for Canada ISP proxy (single-phase, HIGHEST priority)
            if proxy_type == 'canada_isp':
                success, used_key, is_401 = self._try_canada_isp_proxy(url, current_key, payload_json, out_path, proxy_cfg, gw)
                if success:
                    return (True, used_key, None)
                elif is_401:
                    tts_logger.warning(f"STAGE2-CANADA_ISP 401 - Rotating to next proxy...")
                    
                    # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                    next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                    if next_gw:
                        tts_logger.info(f"STAGE2-CANADA_ISP_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                        proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                        
                        # Retry với proxy mới
                        success_retry, used_key_retry, is_401_retry = self._try_canada_isp_proxy(
                            url, current_key, payload_json, out_path, proxy_cfg_next, next_gw
                        )
                        if success_retry:
                            tts_logger.info(f"STAGE2-CANADA_ISP_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                            return (True, used_key_retry, None)
                        elif is_401_retry:
                            tts_logger.warning(f"STAGE2-CANADA_ISP_ROTATE 401 - {next_gw.host}:{next_gw.port} also returned 401, will continue rotating")
                continue
            
            # Special handling for Canada proxy (2-phase flow)
            if proxy_type == 'canada':
                success, used_key, is_401, is_blocked, is_timeout = self._try_canada_proxy(url, current_key, payload_json, out_path, proxy_cfg, gw, on_status_update=on_status_update)
                if success:
                    return (True, used_key, None)
                elif is_401 or is_blocked or is_timeout:
                    # 401/blocked/timeout đều cần XOAY PROXY
                    reason = "401" if is_401 else ("blocked" if is_blocked else "timeout")
                    tts_logger.warning(f"STAGE2-CANADA {reason} - Rotating to next proxy...")
                    
                    # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                    next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                    if next_gw:
                        tts_logger.info(f"STAGE2-CANADA_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                        proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                        
                        # Retry với proxy mới
                        success_retry, used_key_retry, is_401_retry, is_blocked_retry, is_timeout_retry = self._try_canada_proxy(
                            url, current_key, payload_json, out_path, proxy_cfg_next, next_gw, on_status_update=on_status_update
                        )
                        if success_retry:
                            tts_logger.info(f"STAGE2-CANADA_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                            return (True, used_key_retry, None)
                        elif is_401_retry or is_blocked_retry or is_timeout_retry:
                            tts_logger.warning(f"STAGE2-CANADA_ROTATE failed - {next_gw.host}:{next_gw.port}, will continue rotating")
                continue
            
            # Standard proxy attempt
            if on_status_update:
                try:
                    on_status_update("downloading", f"Downloading via {proxy_type} proxy...")
                except Exception:
                    pass
            success, error_msg, file_head = self._make_http_request(
                url, current_key, payload_json, out_path, proxy=proxy_cfg
            )
            
            # Check for 401 error
            is_401_error = False
            if success and file_head:
                tts_logger.info(f"STAGE2 success {gw.host}:{gw.port}")
                return (True, current_key, None)
            elif file_head:
                error_info = self._check_api_error_response(file_head, f"STAGE2-{gw.host}:{gw.port}")
                tts_logger.error(f"STAGE2 {gw.host}:{gw.port} API error: {error_info}")
                
                # Check for 401 in response
                if error_info == "API_UNAUTHORIZED" or "401" in error_info or "unauthorized" in error_info.lower():
                    is_401_error = True
                
                # Handle voice limit immediately
                if error_info == "API_VOICE_LIMIT_REACHED":
                    fresh_key = self._handle_voice_limit_error(current_key, f"STAGE2-{gw.host}:{gw.port}", 1000)
                    if fresh_key and fresh_key != current_key:
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                        # Immediate retry on same proxy
                        if on_status_update:
                            try:
                                on_status_update("downloading", "Downloading audio (retry)...")
                            except Exception:
                                pass
                        success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                            url, current_key, payload_json, out_path, proxy=proxy_cfg
                        )
                        if success_retry and file_head_retry:
                            tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT - SUCCESS!")
                            return (True, current_key, None)
            
            # Check for 401 in error message
            if error_msg and ("401" in error_msg or "unauthorized" in error_msg.lower()):
                is_401_error = True
            
            # If 401 error, rotate to next proxy (xoay vòng)
            if is_401_error:
                tts_logger.warning(f"STAGE2-STANDARD 401 - Rotating to next proxy...")
                
                # ROTATE PROXY: Lấy proxy tiếp theo từ danh sách proxy của user hiện tại (xoay vòng)
                next_gw = self.proxy_service.get_next_proxy_for_rotation(gw)
                if next_gw:
                    tts_logger.info(f"STAGE2-STANDARD_ROTATE - Rotating from {gw.host}:{gw.port} to {next_gw.host}:{next_gw.port}")
                    proxy_cfg_next = self.proxy_service.as_proxy_dict(next_gw)
                    
                    # Retry với proxy mới
                    if on_status_update:
                        try:
                            on_status_update("downloading", f"Retrying with rotated proxy {next_gw.host}:{next_gw.port}...")
                        except Exception:
                            pass
                    success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                        url, current_key, payload_json, out_path, proxy=proxy_cfg_next
                    )
                    if success_retry and file_head_retry:
                        tts_logger.info(f"STAGE2-STANDARD_ROTATE SUCCESS - {next_gw.host}:{next_gw.port}")
                        return (True, current_key, None)
                    elif file_head_retry:
                        error_info_retry = self._check_api_error_response(file_head_retry, f"STAGE2-STANDARD_ROTATE-{next_gw.host}:{next_gw.port}")
                        if error_info_retry == "API_UNAUTHORIZED" or "401" in error_info_retry:
                            tts_logger.warning(f"STAGE2-STANDARD_ROTATE 401 - {next_gw.host}:{next_gw.port} also returned 401, will continue rotating")
                        try:
                            os.unlink(out_path)
                        except Exception:
                            pass
            
            try:
                os.unlink(out_path)
            except Exception:
                pass
        
        return (False, current_key, "STAGE2_ALL_PROXIES_FAILED")
    
    def _order_gateways_by_priority(self, gateways: list) -> list:
        """
        Order gateways by priority: canada -> canada_isp -> standard -> vps
        
        CHỈ sử dụng proxy từ users_token_proxy (không dùng user_proxy_pool nữa)
        
        canada: 2-phase flow with request_id matching (high priority, most reliable)
        canada_isp: Single-phase call (fast, but less precise matching)
        standard: Normal proxy (medium priority)
        vps: VPS proxy (lowest priority)
        """
        ordered = []
        # Priority 1: canada (2-phase flow with request_id, most reliable)
        ordered.extend([g for g in gateways if getattr(g, 'token_proxy', '').lower() == 'canada'])
        # Priority 2: canada_isp (single-phase, fast but less precise)
        ordered.extend([g for g in gateways if getattr(g, 'token_proxy', '').lower() == 'canada_isp'])
        # Priority 3: standard proxies
        ordered.extend([g for g in gateways if getattr(g, 'token_proxy', '').lower() not in ['vps', 'canada', 'canada_isp']])
        # Priority 4: vps (lowest)
        ordered.extend([g for g in gateways if getattr(g, 'token_proxy', '').lower() == 'vps'])
        return ordered
    
    def _try_canada_isp_proxy(self, url: str, api_key: str, payload_json: str, out_path: str,
                              proxy_cfg: dict, gw) -> Tuple[bool, str, bool]:
        """
        Try Canada ISP proxy with SINGLE-PHASE flow (NOT 2-phase like regular canada).
        Returns: (success, used_key, is_401)
        """
        """
        Try Canada ISP proxy with SINGLE-PHASE flow (NOT 2-phase like regular canada).
        
        canada_isp proxies:
        - Use proxy for both create AND download in ONE phase
        - Faster than canada 2-phase (no history lookup)
        - Higher priority than regular canada proxies
        - More stable (single call = less points of failure)
        - OPTIMIZED: Extended timeouts for single-phase calls
        """
        tts_logger.info(f"STAGE2-CANADA_ISP single-phase {gw.host}:{gw.port} with key {api_key[:10]}...")
        
        try:
            # Make HTTP request with OPTIMIZED timeouts for canada_isp (single-phase needs more time)
            # Extended timeout: connect=25s, read=180s (3 phút)
            success, error_msg, file_head = self._make_http_request(
                url, api_key, payload_json, out_path, 
                proxy=proxy_cfg,
                timeout=(25, 180),  # Extended timeout for single-phase (3 phút)
                connect_timeout=25
            )
            
            # Check for 401 error
            is_401 = False
            if success and file_head:
                file_size = os.path.getsize(out_path)
                tts_logger.info(f"STAGE2-CANADA_ISP ✅ {gw.host}:{gw.port} (single-phase, {file_size} bytes)")
                return (True, api_key, False)
            elif file_head:
                # Check for API errors
                error_info = self._check_api_error_response(file_head, f"STAGE2-CANADA_ISP-{gw.host}:{gw.port}")
                
                # Check for 401
                if error_info == "API_UNAUTHORIZED" or "401" in error_info or "unauthorized" in error_info.lower():
                    is_401 = True
                
                # Handle specific errors with appropriate logging level
                if error_info == "API_UNUSUAL_ACTIVITY":
                    tts_logger.warning(f"STAGE2-CANADA_ISP Unusual Activity detected {gw.host}:{gw.port} - IP may be rate limited")
                else:
                    tts_logger.error(f"STAGE2-CANADA_ISP API error {gw.host}:{gw.port}: {error_info}")
            
            # Handle timeout errors with retry
            if error_msg and ("TIMEOUT" in error_msg or "CONNECTION_ERROR" in error_msg):
                tts_logger.warning(f"STAGE2-CANADA_ISP timeout {gw.host}:{gw.port} - {error_msg}, retrying with extended timeout...")
                
                # Retry with even longer timeout: connect=45s, read=180s (3 phút)
                success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                    url, api_key, payload_json, out_path,
                    proxy=proxy_cfg,
                    timeout=(45, 180),  # Extended retry timeout (3 phút)
                    connect_timeout=45
                )
                
                if success_retry and file_head_retry:
                    file_size = os.path.getsize(out_path)
                    tts_logger.info(f"STAGE2-CANADA_ISP ✅ {gw.host}:{gw.port} (retry success, {file_size} bytes)")
                    return (True, api_key, False)
                
                # Check for 401 in retry
                if file_head_retry:
                    error_info_retry = self._check_api_error_response(file_head_retry, f"STAGE2-CANADA_ISP-RETRY-{gw.host}:{gw.port}")
                    if error_info_retry == "API_UNAUTHORIZED" or "401" in error_info_retry:
                        is_401 = True
                tts_logger.error(f"STAGE2-CANADA_ISP ❌ {gw.host}:{gw.port} - retry failed: {error_msg_retry}")
                return (False, api_key, is_401)
            
            # Handle quota exceeded with key rotation
            if file_head:
                error_info = self._check_api_error_response(file_head, f"STAGE2-CANADA_ISP-{gw.host}:{gw.port}")
                if "quota_exceeded" in error_info.lower():
                    tts_logger.warning(f"STAGE2-CANADA_ISP quota exceeded {gw.host}:{gw.port}, trying key rotation...")
                    
                    # Try to get fresh key and retry canada_isp
                    fresh_key = self._get_fresh_api_key_for_canada_isp(api_key, payload_json)
                    if fresh_key and fresh_key != api_key:
                        tts_logger.info(f"STAGE2-CANADA_ISP retry with fresh key {fresh_key[:10]}...")
                        
                        # Retry with fresh key
                        success_fresh, error_msg_fresh, file_head_fresh = self._make_http_request(
                            url, fresh_key, payload_json, out_path,
                            proxy=proxy_cfg,
                            timeout=(25, 180),
                            connect_timeout=25
                        )
                        
                        if success_fresh and file_head_fresh:
                            file_size = os.path.getsize(out_path)
                            tts_logger.info(f"STAGE2-CANADA_ISP ✅ {gw.host}:{gw.port} (fresh key success, {file_size} bytes)")
                            return (True, fresh_key, False)
                        
                        # Check for 401 in fresh key retry
                        if file_head_fresh:
                            error_info_fresh = self._check_api_error_response(file_head_fresh, f"STAGE2-CANADA_ISP-FRESH-{gw.host}:{gw.port}")
                            if error_info_fresh == "API_UNAUTHORIZED" or "401" in error_info_fresh:
                                is_401 = True
                        tts_logger.error(f"STAGE2-CANADA_ISP ❌ {gw.host}:{gw.port} - fresh key failed: {error_msg_fresh}")
                    else:
                        tts_logger.error(f"STAGE2-CANADA_ISP ❌ {gw.host}:{gw.port} - no fresh key available")
            
            # Check for 401 in error message
            if error_msg and ("401" in error_msg or "unauthorized" in error_msg.lower()):
                is_401 = True
            tts_logger.error(f"STAGE2-CANADA_ISP ❌ {gw.host}:{gw.port} - {error_msg}")
            return (False, api_key, is_401)
            
        except Exception as e:
            tts_logger.error(f"STAGE2-CANADA_ISP exception {gw.host}:{gw.port}: {e}")
            return (False, api_key, False)

    def _try_canada_proxy(self, url: str, api_key: str, payload_json: str, out_path: str,
                          proxy_cfg: dict, gw, on_status_update=None) -> Tuple[bool, str, bool, bool, bool]:
        """
        Try Canada proxy with OPTIMIZED 2-phase flow + fast fail strategy.
        Returns: (success, used_key, is_401, is_blocked_proxy, is_timeout)
        """
        """
        Try Canada proxy with OPTIMIZED 2-phase flow + fast fail strategy.
        
        Strategy:
        1. Try Canada 2-phase flow (optimized - reduced retries from 3→2)
        2. If timeout/SSL error → Skip fallback (fail fast)
        3. Only fallback on specific recoverable errors
        
        Performance improvements:
        - Reduced retries in Canada flow
        - Skip expensive fallback on timeouts
        - Better error classification
        """
        try:
            from services import tts_service_canada as ca
            tts_logger.info(f"STAGE2-CANADA 2-phase {gw.host}:{gw.port}")
            
            # Extract voice_id from URL
            voice_id = url.split('/')[-1]
            
            # Optimized: Reduce retries from 3→2 for faster failure
            ok, err = ca.canada_generate_then_download(api_key, voice_id, payload_json, proxy_cfg, out_path, retries=2)
            
            # Check for 401 error / proxy block / timeout
            is_401 = False
            is_blocked = False
            is_timeout = False
            
            # QUAN TRỌNG: Phân biệt TIMEOUT vs 401
            # Timeout cần XOAY PROXY, 401 cần XOAY KEY
            if err and ("TIMEOUT_ERROR" in err or "timed out" in err.lower() or "Read timed out" in err):
                is_timeout = True
                tts_logger.warning(f"STAGE2-CANADA TIMEOUT - Proxy {gw.host}:{gw.port} timed out, will rotate proxy (not key)")
                
                # TRIGGER ROTATION IMMEDIATELY if we have the proxy service
                if self.proxy_service and hasattr(gw, 'token_proxy') and getattr(gw, 'token_proxy', '').lower() == 'canada':
                    tts_logger.info(f"STAGE2-CANADA TRIGGERING ROTATION for timed-out proxy {gw.host}:{gw.port}")
                    # Force rotate via dedicated thread or direct call?
                    # Since we're in a worker thread, we can call rotation directly but non-blocking preferred
                    # Let's mark it as rotated so the auto-rotator picks it up or manually trigger
                    try:
                        # Mark last_rotated to way back to force auto-rotation check or use reset link
                        if hasattr(gw, 'reset_link') and gw.reset_link:
                            # Use CENTRALIZED reset - only 1 call even if multiple threads timeout
                            was_reset, msg = self.proxy_service.trigger_reset_smart(gw)
                            tts_logger.info(f"STAGE2-CANADA RESET: {msg}")
                    except Exception as e:
                        tts_logger.warning(f"STAGE2-CANADA ROTATION ERROR: {e}")
            elif err and ("401" in err or "unauthorized" in err.lower() or "invalid_api_key" in err.lower()):
                # Bất kỳ lỗi 401/unauthorized nào cũng được coi là lỗi KEY
                is_401 = True
                tts_logger.error(f"STAGE2-CANADA 401 - Proxy {gw.host}:{gw.port} returned 401/unauthorized for key {api_key[:10]}...")
                # CRITICAL: Blacklist this key to prevent reusing it (5 minutes)
                self._blacklist_key(api_key, reason="401_CANADA")
            
            # CRITICAL: Check for QUOTA_EXCEEDED - blacklist key
            if err and "QUOTA_EXCEEDED" in err:
                tts_logger.error(f"STAGE2-CANADA QUOTA_EXCEEDED - Key {api_key[:10]}... out of credits, blacklisting")
                self._blacklist_key(api_key, reason="QUOTA_EXCEEDED")
                return (False, api_key, is_401, is_blocked, is_timeout)
            
            # CRITICAL: Check for INSUFFICIENT_CREDITS - blacklist key
            if err and ("INSUFFICIENT_CREDITS" in err or "CREDIT_CHECK_FAILED" in err or "CREDIT_CHECK_EXCEPTION" in err):
                tts_logger.error(
                    f"STAGE2-CANADA INSUFFICIENT_CREDITS - Key {api_key[:10]}... insufficient credits ({err}), blacklisting"
                )
                self._blacklist_key(api_key, reason="INSUFFICIENT_CREDITS")
                return (False, api_key, is_401, is_blocked, is_timeout)
            
            # CRITICAL: Check for VOICE_LIMIT_REACHED and mark key as voice_limit_reached
            # Note: Voice deletion is handled automatically in canada_generate_then_download
            # If it still fails after deletion, we should rotate to a different key
            if err and "VOICE_LIMIT_REACHED" in err:
                tts_logger.error(f"STAGE2-CANADA VOICE_LIMIT_REACHED - Key {api_key[:10]}... hit voice limit, blacklisting")
                self._blacklist_key(api_key, reason="VOICE_LIMIT_REACHED")
                try:
                    with self.pool_lock:
                        self.pool.mark_voice_limit_reached(api_key)
                        self.pool.sync_state_changes_immediate()
                except Exception:
                    pass
                return (False, api_key, is_401, is_blocked, is_timeout)  # Rotate to a different key
            
            # CRITICAL: Handle ElevenLabs VPN/abuse detection (detected_unusual_activity)
            # Trước đây chỉ coi là lỗi PROXY. Yêu cầu mới: coi như 401 để ĐỔI KEY LUÔN.
            if err and ("detected_unusual_activity" in err.lower() or "UNUSUAL_ACTIVITY" in err):
                is_blocked = True
                is_401 = True  # xử lý như 401 để upper-layer rotate key
                tts_logger.error(
                    f"STAGE2-CANADA UNUSUAL_ACTIVITY - Proxy {gw.host}:{gw.port} blocked by ElevenLabs abuse detector "
                    f"for key {api_key[:10]}.... Treating as 401 to rotate API key."
                )
                self._blacklist_key(api_key, reason="UNUSUAL_ACTIVITY")
                return (False, api_key, is_401, is_blocked, is_timeout)
            
            # TIMEOUT: Cần xoay proxy, không xoay key
            if is_timeout:
                tts_logger.warning(f"STAGE2-CANADA TIMEOUT - {gw.host}:{gw.port} timed out, returning is_timeout=True to rotate proxy")
                return (False, api_key, is_401, is_blocked, is_timeout)
            
            if ok:
                with open(out_path, "rb") as f:
                    if self._is_valid_mp3_head(f.read(512)):
                        tts_logger.info(f"STAGE2-CANADA ✅ {gw.host}:{gw.port}")
                        return (True, api_key, False, False, False)
            
            # OPTIMIZED: Skip expensive fallback for SSL errors (fail fast)
            # Timeout errors (28) are retried with longer timeout in canada_generate_then_download
            # So we don't mark them as unrecoverable here - let the retry logic handle it
            unrecoverable_errors = [
                "(35)",  # SSL/TLS error
                "schannel: failed",
                "SSL/TLS connection failed"
            ]
            
            # Only mark SSL errors as unrecoverable
            # Timeout errors (28) are handled by retry logic with increasing timeout
            if any(e in err for e in unrecoverable_errors):
                tts_logger.warning(
                    f"STAGE2-CANADA ❌ unrecoverable SSL error {gw.host}:{gw.port} - skipping Canada proxy (no curl fallback)"
                )
                return (False, api_key, is_401, is_blocked, is_timeout)
            
            # For all other errors, fail fast WITHOUT curl fallback to avoid extra curl calls
            tts_logger.error(f"STAGE2-CANADA ❌ {gw.host}:{gw.port} - {err[:200]} (no curl fallback)")
            return (False, api_key, is_401, is_blocked, is_timeout)
        except Exception as e:
            tts_logger.error(f"STAGE2-CANADA exception {gw.host}:{gw.port}: {e}")
            return (False, api_key, False, False, False)

    # Canada proxy 2-phase logic removed - now works like normal proxy

    # Canada proxy helper methods removed - no longer needed

    def _check_api_key_quota(self, api_key: str, needed_chars: int) -> bool:
        """Check if API key has sufficient quota before making TTS call by calling ElevenLabs API directly"""
        try:
            # Call ElevenLabs API directly to get real quota
            real_credits = self._get_real_quota_from_api(api_key)
            if real_credits == -1:
                tts_logger.warning(f"API_KEY_QUOTA_CHECK - Failed to get credits for key {api_key[:10]}...")
                return False
            
            # Add buffer for safety (100-200 credits as requested)
            buffer = 150  # Middle of 100-200 range
            total_needed = needed_chars + buffer
            
            is_sufficient = real_credits >= total_needed
            
            tts_logger.info(f"API_KEY_QUOTA_CHECK - Key {api_key[:10]}... has {real_credits} credits, needs {needed_chars}+{buffer}={total_needed}, sufficient: {is_sufficient}")
            
            return is_sufficient
            
        except Exception as e:
            tts_logger.error(f"API_KEY_QUOTA_CHECK_ERROR - {e}")
            return False

    def _get_real_quota_from_api(self, api_key: str) -> int:
        """Get real quota from ElevenLabs API directly using requests"""
        try:
            
            headers = {"xi-api-key": api_key}
            url = "https://api.elevenlabs.io/v1/user"
            
            response = requests.get(url, headers=headers, timeout=(5, 10))
            
            if response.status_code != 200:
                tts_logger.error(f"REAL_QUOTA_API_ERROR - HTTP {response.status_code}: {response.text[:200]}")
                return -1
            
            try:
                user_data = response.json()
                subscription = user_data.get('subscription', {})
                character_count = subscription.get('character_count', 0)  # Used credits
                character_limit = subscription.get('character_limit', 0)  # Total limit
                
                remaining_credits = character_limit - character_count
                
                tts_logger.info(f"REAL_QUOTA_API - Used: {character_count}, Limit: {character_limit}, Remaining: {remaining_credits}")
                
                return remaining_credits
                
            except (json.JSONDecodeError, ValueError) as e:
                tts_logger.error(f"REAL_QUOTA_API_JSON_ERROR - {e}")
                return -1
                
        except RequestException as e:
            tts_logger.error(f"REAL_QUOTA_API_REQUEST_ERROR - {e}")
            return -1
        except Exception as e:
            tts_logger.error(f"REAL_QUOTA_API_ERROR - {e}")
            return -1

    def _find_valid_key_parallel(self, url: str, payload_json: str, needed_chars: int, 
                                used_keys: set, max_parallel: int = 1) -> Optional[str]:
        """
        Find a valid key using parallel testing of multiple keys simultaneously.
        Returns the first valid key found, or None if no valid keys available.
        """
        import concurrent.futures

        # Đảm bảo không bao giờ tạo nhiều thread hơn self.max_workers
        max_parallel = max(1, min(max_parallel, self.max_workers))
        import threading
        
        def test_single_key(api_key: str) -> Optional[str]:
            """Test a single key and return it if valid, None otherwise"""
            try:
                # Quick credit check first
                if not self.validate_api_key_has_credits(api_key, needed_chars):
                    tts_logger.warning(f"PARALLEL_KEY_TEST - Key {api_key[:10]}... insufficient credits")
                    return None
                
                # Test with actual TTS call
                test_out_path = os.path.join(self.output_dir, f"test_{uuid.uuid4()}.mp3")
                success, error_msg, file_head = self._make_http_request(
                    url, api_key, payload_json, test_out_path, proxy=None
                )
                
                if success and file_head:
                    tts_logger.info(f"PARALLEL_KEY_TEST - Key {api_key[:10]}... VALID!")
                    return api_key
                elif file_head:
                    # Check for quota_exceeded
                    error_type = self._check_api_error_response(file_head, f"PARALLEL_TEST-{api_key[:10]}")
                    if error_type and error_type.startswith("API_QUOTA_EXCEEDED"):
                        tts_logger.warning(f"PARALLEL_KEY_TEST - Key {api_key[:10]}... quota_exceeded")
                        try:
                            with self.pool_lock:
                                self.pool.mark_exhausted(api_key)
                        except Exception:
                            pass
                
                # Cleanup test file
                try:
                    os.unlink(test_out_path)
                except Exception:
                    pass
                    
                return None
                
            except Exception as e:
                tts_logger.error(f"PARALLEL_KEY_TEST_ERROR - Key {api_key[:10]}... failed: {e}")
                return None
        
        # Collect candidate keys
        candidate_keys = []
        attempts = 0
        max_attempts = 10  # Prevent infinite loop
        
        while len(candidate_keys) < max_parallel and attempts < max_attempts:
            attempts += 1
            try:
                fresh_key = self._get_fresh_api_key(needed_chars, exclude_keys=used_keys)
                if fresh_key and fresh_key not in used_keys and fresh_key not in candidate_keys:
                    candidate_keys.append(fresh_key)
                    used_keys.add(fresh_key)
                else:
                    break  # No more keys available
            except Exception as e:
                tts_logger.error(f"PARALLEL_KEY_COLLECTION_ERROR: {e}")
                break
        
        if not candidate_keys:
            tts_logger.warning("PARALLEL_KEY_SEARCH - No candidate keys found")
            return None
        
        tts_logger.info(f"PARALLEL_KEY_SEARCH - Testing {len(candidate_keys)} keys in parallel: {[k[:10]+'...' for k in candidate_keys]}")
        
        # Nếu max_parallel == 1 thì test tuần tự để tránh tạo thêm thread
        if max_parallel == 1:
            for key in candidate_keys:
                try:
                    if test_single_key(key):
                        tts_logger.info(f"PARALLEL_KEY_SEARCH - Found valid key {key[:10]}... (sequential)")
                        return key
                except Exception as e:
                    tts_logger.error(f"PARALLEL_KEY_TEST_EXCEPTION - Key {key[:10]}... error: {e}")
        else:
            # Test keys in parallel (chỉ dùng khi max_parallel > 1 và vẫn ≤ self.max_workers)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="key_test") as executor:
                future_to_key = {executor.submit(test_single_key, key): key for key in candidate_keys}
                
                for future in concurrent.futures.as_completed(future_to_key):
                    key = future_to_key[future]
                    try:
                        result = future.result()
                        if result:
                            # Found valid key! Cancel remaining tasks and return
                            tts_logger.info(f"PARALLEL_KEY_SEARCH - Found valid key {result[:10]}... first!")
                            for f in future_to_key:
                                if f != future:
                                    f.cancel()
                            return result
                    except Exception as e:
                        tts_logger.error(f"PARALLEL_KEY_TEST_EXCEPTION - Key {key[:10]}... error: {e}")
        
        tts_logger.warning("PARALLEL_KEY_SEARCH - No valid keys found in parallel testing")
        return None

    def _mark_proxy_inactive(self, gw) -> None:
        """
        Mark proxy as inactive in database when it returns 401 or is blocked.
        CHỈ sử dụng proxy từ users_token_proxy (không dùng user_proxy_pool nữa).
        Log warning khi proxy từ users_token_proxy trả về 401.
        """
        if not (self.proxy_service and self.proxy_service.supabase):
            return
        
        try:
            # CHỈ sử dụng proxy từ users_token_proxy - log warning khi proxy trả về 401
            tts_logger.warning(f"PROXY_401_USER_PROXY - User proxy {gw.host}:{gw.port} returned 401 (from users_token_proxy)")
        except Exception as e:
            tts_logger.error(f"PROXY_401_MARK_INACTIVE_EXCEPTION - {e}")

    def _handle_401_error(self, api_key: str, stage_name: str) -> Optional[str]:
        """Handle 401 error using proper state management (Phase 1)"""
        tts_logger.error(f"{stage_name} - 401 ERROR - Key {api_key[:10]}... invalid, rotating key")
        
        # Mark key as INVALID (not just exhausted)
        try:
            with self.pool_lock:
                self.pool.mark_invalid(api_key, reason="401 unauthorized")
        except Exception:
            pass
            
        # Get fresh key
        return self._get_fresh_api_key(1000, exclude_keys={api_key})
    
    def _handle_quota_exceeded_error(self, api_key: str, stage_name: str, needed_chars: int) -> Optional[str]:
        """Handle quota_exceeded error using proper state (Phase 1)"""
        tts_logger.error(f"{stage_name} - QUOTA_EXCEEDED - Key {api_key[:10]}... out of credits, rotating key")
        
        # Mark with QUOTA_EXCEEDED state
        try:
            with self.pool_lock:
                self.pool.mark_quota_exceeded(api_key)
        except Exception:
            pass
            
        # Get fresh key with enough credits
        return self._get_fresh_api_key(needed_chars, exclude_keys={api_key})
    
    def _handle_voice_limit_error(self, api_key: str, stage_name: str, needed_chars: int) -> Optional[str]:
        """Handle voice_limit_reached error using proper state (Phase 1)"""
        tts_logger.error(f"{stage_name} - VOICE_LIMIT_REACHED - Key {api_key[:10]}... hit custom voice limit, rotating key")
        
        # Mark with VOICE_LIMIT_REACHED state
        try:
            with self.pool_lock:
                self.pool.mark_voice_limit_reached(api_key)
            tts_logger.info(f"{stage_name} - VOICE_LIMIT - Key {api_key[:10]}... marked as voice_limit_reached")
        except Exception as e:
            tts_logger.error(f"{stage_name} - VOICE_LIMIT - Failed to mark key: {e}")
            
        # Force reload pool to get fresh keys
        try:
            with self.pool_lock:
                self.pool.load()  # Reload from database
            tts_logger.info(f"{stage_name} - VOICE_LIMIT - Pool reloaded from database")
        except Exception as e:
            tts_logger.error(f"{stage_name} - VOICE_LIMIT - Failed to reload pool: {e}")
            
        # Get fresh key that may have different voice limits
        fresh_key = self._get_fresh_api_key(needed_chars, exclude_keys={api_key})
        if fresh_key:
            tts_logger.info(f"{stage_name} - VOICE_LIMIT - Got fresh key {fresh_key[:10]}...")
        else:
            tts_logger.error(f"{stage_name} - VOICE_LIMIT - No fresh key available")
        return fresh_key

    # ---------- core 3-stage (REFACTORED - Phase 4) ----------
    
    def _http2_tts_request(self, url: str, api_key: str, payload: dict, output_path: str, 
                           proxy: Optional[dict] = None, timeout: int = 180) -> bool:
        """
        Make TTS request using HTTP/2 session with connection pooling.
        
        This is FASTER than curl for multiple requests because:
        - Reuses connections (no handshake overhead)
        - HTTP/2 multiplexing
        - Connection pool management
        
        Returns:
            True if successful, False otherwise
        """
        if not self.http_session:
            tts_logger.debug("HTTP/2 session not available, fallback to curl")
            return False
        
        try:
            headers = {
                "xi-api-key": api_key,
                "Content-Type": "application/json"
            }
            
            # Configure proxy if provided
            proxies = None
            if proxy and proxy.get("host") and proxy.get("port"):
                proxy_url = f"http://{proxy['host']}:{proxy['port']}"
                if proxy.get("username"):
                    auth = f"{proxy['username']}:{proxy.get('password', '')}"
                    proxy_url = f"http://{auth}@{proxy['host']}:{proxy['port']}"
                proxies = {
                    "http": proxy_url,
                }
            
            # Make request with connection pooling
            tts_logger.debug(f"HTTP/2 request to {url} (timeout={timeout}s)")
            response = self.http_session.post(
                url,
                json=payload,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
            )
            
            # Check status
            if response.status_code != 200:
                tts_logger.error(f"HTTP/2 request failed: {response.status_code} - {response.text[:200]}")
                return False
            
            # Write response to file
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # Validate file
            if not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
                tts_logger.error(f"HTTP/2 request produced invalid file")
                return False
            
            # Validate MP3 header
            with open(output_path, 'rb') as f:
                if not self._is_valid_mp3_head(f.read(512)):
                    tts_logger.error(f"HTTP/2 request produced invalid MP3")
                    return False
            
            tts_logger.debug(f"HTTP/2 request successful ({os.path.getsize(output_path)} bytes)")
            return True
            
        except Exception as e:
            tts_logger.error(f"HTTP/2 request exception: {e}")
            return False
    
    # DEPRECATED: Use _call_tts_simple_with_proxy() instead
    def _call_tts_1stage_2phase_with_proxy(self, api_key: str, voice_id: str, payload: dict, out_path: str, 
                                          proxy_cfg: Optional[dict] = None, on_status_update=None,
                                          proxy_url: Optional[str] = None) -> str:
        """
        NEW: 1-stage TTS call with 2-phase proxy flow với proxy cụ thể.
        Nếu proxy_cfg được chỉ định, dùng proxy đó; nếu không, dùng proxy_service.
        proxy_url: URL gốc của proxy (để trả về queue sau khi dùng)
        """
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload_json = self._prepare_utf8_json(payload)
        self._current_payload = payload
        
        current_key = api_key
        used_keys = {api_key}
        
        # Nếu có proxy_cfg, dùng trực tiếp proxy đó
        proxy_url_used = proxy_url  # Ưu tiên dùng proxy_url được truyền vào
        if proxy_cfg:
            tts_logger.info(f"1STAGE_2PHASE_WITH_PROXY - Using specified proxy: {proxy_cfg.get('host')}:{proxy_cfg.get('port')}")
            # Nếu không có proxy_url được truyền vào, build từ proxy_cfg
            if not proxy_url_used and proxy_cfg.get('host') and proxy_cfg.get('port'):
                host = proxy_cfg.get('host')
                port = proxy_cfg.get('port')
                username = proxy_cfg.get('username', '')
                password = proxy_cfg.get('password', '')
                if username:
                    proxy_url_used = f"http://{username}:{password}@{host}:{port}"
                else:
                    proxy_url_used = f"http://{host}:{port}"
            # Thử với proxy được chỉ định
            success, used_key, error = self._execute_stage2_with_specific_proxy(
                url, current_key, payload_json, out_path, used_keys, proxy_cfg, 
                on_status_update=on_status_update, proxy_url=proxy_url_used
            )
            if success:
                tts_logger.info("1STAGE_2PHASE_WITH_PROXY - SUCCESS")
                return used_key

            # Nếu lỗi liên quan 401 / unauthorized / proxy bị block → ĐỔI KEY LUÔN
            # CRITICAL: KHÔNG đổi key nếu lỗi là TIMEOUT (chỉ cần đổi IP)
            is_401_like_error = False
            is_timeout_error = False
            if isinstance(error, str):
                lower_err = error.lower()
                # Check for TIMEOUT first - take priority
                if "timeout" in lower_err or "timed out" in lower_err:
                    is_timeout_error = True
                    tts_logger.info(f"1STAGE_2PHASE_WITH_PROXY - TIMEOUT detected, will rotate IP (not key)")
                elif (
                    error in ("PROXY_401_OR_BLOCKED", "PROXY_401")
                    or "401" in error
                    or "unauthorized" in lower_err
                    or "invalid_api_key" in lower_err
                    or "detected_unusual_activity" in lower_err
                ):
                    is_401_like_error = True

            # Only rotate key if it's a genuine 401 error (NOT timeout)
            if is_401_like_error and not is_timeout_error:
                fresh_key = self._handle_401_error(current_key, "1STAGE_2PHASE_WITH_PROXY")
                if fresh_key and fresh_key not in used_keys:
                    tts_logger.info(
                        f"1STAGE_2PHASE_WITH_PROXY - 401 detected, rotated key "
                        f"{current_key[:10]}... → {fresh_key[:10]}..."
                    )
                    current_key = fresh_key
                    used_keys.add(fresh_key)

            # Nếu fail, retry với proxy khác từ pool (dùng current_key mới nếu đã rotate)
            tts_logger.warning("1STAGE_2PHASE_WITH_PROXY - Specified proxy failed, trying proxy queue from pool")
            if self.proxy_service and hasattr(self.proxy_service, "get_next_proxy_url"):
                proxy_url = self.proxy_service.get_next_proxy_url()
                if proxy_url:
                    proxy_cfg = None
                    if hasattr(self.proxy_service, "parse_proxy_url_to_cfg"):
                        proxy_cfg = self.proxy_service.parse_proxy_url_to_cfg(proxy_url)
                    if proxy_cfg:
                        success, used_key, error = self._execute_stage2_with_specific_proxy(
                            url, current_key, payload_json, out_path, used_keys, proxy_cfg, 
                            on_status_update=on_status_update, proxy_url=proxy_url
                        )
                        # _execute_stage2_with_specific_proxy đã tự động trả proxy về queue
                        if success:
                            return used_key
        
        # Fallback: dùng proxy_service như cũ (yêu cầu proxy_queue đã init)
        if not self.proxy_service:
            tts_logger.error("1STAGE_2PHASE_SKIP - No proxy service configured")
            raise RuntimeError("No proxy service available - 1-stage 2-phase requires proxy")
        
        # === Phase 1: Try with proxy (Canada priority) ===
        if on_status_update:
            try:
                on_status_update("synthesizing", "Phase 1: Trying with proxy...")
            except Exception:
                pass
        
        if on_status_update:
            try:
                on_status_update("downloading", "Downloading audio...")
            except Exception:
                pass
        
        used_proxies = set()
        success, used_key, error = self._execute_stage2_with_proxy(url, current_key, payload_json, out_path, used_keys, used_proxies, on_status_update=on_status_update)
        if success:
            tts_logger.info("1STAGE_2PHASE - Phase 1 SUCCESS")
            return used_key
        
        current_key = used_key
        
        # === Phase 2: Retry with different proxy/key ===
        if on_status_update:
            try:
                on_status_update("synthesizing", "Phase 2: Retrying with different proxy...")
            except Exception:
                pass
        
        if on_status_update:
            try:
                on_status_update("downloading", "Downloading audio...")
            except Exception:
                pass
        
        tts_logger.warning("1STAGE_2PHASE - Phase 1 failed, trying Phase 2")
        success, used_key, error = self._execute_stage2_with_proxy(url, current_key, payload_json, out_path, used_keys, used_proxies, on_status_update=on_status_update)
        if success:
            tts_logger.info("1STAGE_2PHASE - Phase 2 SUCCESS")
            return used_key
        
        # All attempts failed
        tts_logger.error(f"1STAGE_2PHASE_FAILURE - Both phases failed, used {len(used_keys)} keys")
        raise RuntimeError(
            f"All TTS attempts failed after 1-stage 2-phase retry. "
            f"Last error: {error}, Keys used: {len(used_keys)}"
        )
    
    # DEPRECATED: Use _call_tts_simple_with_proxy() instead
    def _call_tts_1stage_2phase(self, api_key: str, voice_id: str, payload: dict, out_path: str, 
                                 on_status_update=None) -> str:
        """
        NEW: 1-stage TTS call with 2-phase proxy flow (bỏ stage 1 no proxy).
        Chỉ dùng proxy với 2 phase để tăng tốc và đơn giản hóa.
        """
        return self._call_tts_1stage_2phase_with_proxy(api_key, voice_id, payload, out_path, 
                                                       proxy_cfg=None, on_status_update=on_status_update)
    
    # DEPRECATED: Use _call_tts_simple_with_proxy() instead
    def _call_tts_3stage_refactored(self, api_key: str, voice_id: str, payload: dict, out_path: str) -> str:
        """
        Refactored 3-stage TTS call using Stage Executors (Phase 4A-C).
        Much cleaner and easier to maintain than original 800-line method.
        """
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload_json = self._prepare_utf8_json(payload)
        self._current_payload = payload
        
        current_key = api_key
        used_keys = {api_key}
        
        # === Stage 1: No Proxy ===
        success, used_key, error = self._execute_stage1_no_proxy(url, current_key, payload_json, out_path, used_keys)
        if success:
            return used_key
        
        current_key = used_key  # May have been rotated
        
        # === Stage 2: With Proxy ===
        if self.proxy_service:
            success, used_key, error = self._execute_stage2_with_proxy(url, current_key, payload_json, out_path, used_keys)
            if success:
                return used_key
            current_key = used_key
        else:
            tts_logger.error("STAGE2_SKIP - No proxy service, failed at Stage 1")
            raise RuntimeError(f"Stage 1 failed and no proxy available: {error}")
        
        # === Stage 3: Retry Stage 1 → Stage 2 ===
        tts_logger.warning("ALL_STAGES_FAILED - Retrying: Stage 1 → Stage 2")
        
        # Retry Stage 1
        success, used_key, error = self._execute_stage1_no_proxy(url, current_key, payload_json, out_path, used_keys)
        if success:
            tts_logger.info("RETRY-STAGE1 success")
            return used_key
        
        # Retry Stage 2
        if self.proxy_service:
            success, used_key, error = self._execute_stage2_with_proxy(url, current_key, payload_json, out_path, used_keys)
            if success:
                tts_logger.info("RETRY-STAGE2 success")
                return used_key
        
        # All attempts failed
        tts_logger.error(f"FINAL_FAILURE - All stages exhausted, used {len(used_keys)} keys")
        raise RuntimeError(
            f"All TTS attempts failed after 3-stage retry. "
            f"Last error: {error}, Keys used: {len(used_keys)}"
        )
    
    # DEPRECATED: Use _call_tts_simple_with_proxy() instead
    def _call_tts_3stage(self, api_key: str, voice_id: str, payload: dict, out_path: str) -> str:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        # Use proper UTF-8 JSON encoding for Vietnamese text
        payload_json = self._prepare_utf8_json(payload)
        
        # Store payload and URL for error handling and Canada proxy
        self._current_payload = payload
        # Store URL for reference (no longer needed for Canada 2-phase)

        # ===== Stage 1 — NO PROXY =====
        current_key = api_key
        used_keys = {api_key}  # Track used keys to avoid repeating
        
        tts_logger.info(f"STAGE1 start (no proxy) with key {current_key[:10]}...")
        success, error_msg, file_head = self._make_http_request(
            url, current_key, payload_json, out_path, proxy=None
        )
        
        if success and file_head:
            tts_logger.info("STAGE1 success")
            return current_key
        elif file_head:
            # Check if it's a JSON error response AND HANDLE IMMEDIATELY
            error_info = self._check_api_error_response(file_head, "STAGE1")
            tts_logger.error(f"STAGE1 API error: {error_info}")
            
            # IMMEDIATE handling for voice_limit_reached
            if error_info == "API_VOICE_LIMIT_REACHED":
                tts_logger.error(f"STAGE1 - VOICE_LIMIT_REACHED - IMMEDIATE ROTATION of key {current_key[:10]}...")
                
                # Mark key as voice_limit_reached (KHÔNG phải exhausted vì không phải hết credits)
                try:
                    with self.pool_lock:
                        self.pool.mark_voice_limit_reached(current_key)
                    tts_logger.info(f"STAGE1 - VOICE_LIMIT - Key {current_key[:10]}... marked as voice_limit_reached")
                except Exception as e:
                    tts_logger.error(f"STAGE1 - VOICE_LIMIT - Failed to mark key voice_limit_reached: {e}")
                
                needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                fresh_key = self._handle_voice_limit_error(current_key, "STAGE1", needed_chars)
                if fresh_key and fresh_key != current_key:
                    current_key = fresh_key
                    used_keys.add(fresh_key)
                    tts_logger.info(f"STAGE1 - VOICE_LIMIT - IMMEDIATE ROTATED to fresh key {current_key[:10]}...")
                    
                    # IMMEDIATE retry with fresh key
                    tts_logger.info(f"STAGE1 - VOICE_LIMIT - IMMEDIATE RETRY with fresh key {current_key[:10]}...")
                    success_immediate, error_msg_immediate, file_head_immediate = self._make_http_request(
                        url, current_key, payload_json, out_path, proxy=None
                    )
                    
                    if success_immediate and file_head_immediate:
                        tts_logger.info("STAGE1 - VOICE_LIMIT - IMMEDIATE SUCCESS!")
                        return current_key
                else:
                    tts_logger.error(f"STAGE1 - VOICE_LIMIT - No fresh key available, stopping")
                    raise RuntimeError("No fresh key available for voice limit rotation")
            try: os.unlink(out_path)
            except Exception: pass
        
        # Handle Stage 1 failure - check error message and response content  
        need_key_rotation = False
        response_error_type = None
        
        # Check error message for 401
        if error_msg and ("401" in error_msg or "unauthorized" in error_msg.lower()):
            need_key_rotation = True
        
        # Check response content for new logic
        if file_head:
            try:
                response_error_type = self._check_api_error_response(file_head, "STAGE1")
                
                if response_error_type == "API_UNAUTHORIZED":
                    need_key_rotation = True
                    tts_logger.error(f"STAGE1 detected 401 in response content")
                elif response_error_type in ("API_BAD_REQUEST", "API_NOT_FOUND"):
                    # Fail fast: invalid voice_id or model → don't retry, stop early
                    raise RuntimeError(f"Bad request or not found: {response_error_type}")
                elif response_error_type in ("API_FORBIDDEN", "API_LEGAL_BLOCK"):
                    # Likely IP blocked → move to proxy stages
                    pass
                elif response_error_type and response_error_type.startswith("API_QUOTA_EXCEEDED"):
                    # Handle quota exceeded - ROTATE to fresh key immediately, DON'T use exhausted key in Stage 2
                    tts_logger.error(f"STAGE1 - QUOTA_EXCEEDED - Key {current_key[:10]}... out of credits, marking exhausted")
                    try:
                        with self.pool_lock:
                            self.pool.mark_exhausted(current_key)
                    except Exception:
                        pass
                    
                    needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                    
                    # Try to find valid key using (at most) 1 concurrent test to không vượt quá tổng 5 task
                    valid_key = self._find_valid_key_parallel(
                        url, payload_json, needed_chars, used_keys, max_parallel=TTSConfig.PARALLEL_KEY_TEST_COUNT
                    )
                    if valid_key:
                        tts_logger.info(f"STAGE1 - QUOTA_EXCEEDED - Found valid key {valid_key[:10]}... will retry Stage 1 with this key!")
                        current_key = valid_key  # CRITICAL: Update current_key
                        used_keys.add(valid_key)
                        need_key_rotation = True  # Trigger Stage 1 retry with fresh key
                    else:
                        tts_logger.error(f"STAGE1 - QUOTA_EXCEEDED - No valid keys found, cannot proceed")
                        # CRITICAL: RETURN ERROR - don't proceed to Stage 2 with exhausted key
                        return ""  # Empty string indicates fatal error
                elif response_error_type == "API_VOICE_LIMIT_REACHED":
                    # Handle voice limit reached - rotate to different API key immediately
                    tts_logger.error(f"STAGE1 - VOICE_LIMIT_REACHED - Rotating key {current_key[:10]}...")
                    needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                    fresh_key = self._handle_voice_limit_error(current_key, "STAGE1", needed_chars)
                    if fresh_key and fresh_key != current_key:
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                        need_key_rotation = True
                        tts_logger.info(f"STAGE1 - VOICE_LIMIT - Rotated to fresh key {current_key[:10]}...")
                elif response_error_type == "API_RATE_LIMIT":
                    # Backoff with jitter then continue to Stage 2 proxy path
                    import random, time as _t
                    backoff = random.uniform(0.5, 1.5)
                    tts_logger.info(f"STAGE1 rate limited - sleeping {backoff:.2f}s before proxy fallback")
                    _t.sleep(backoff)
            except Exception:
                pass
        
        if need_key_rotation:
            # Skip 401 handling if we already handled specific errors
            already_rotated = (response_error_type and (
                response_error_type.startswith("API_QUOTA_EXCEEDED") or 
                response_error_type == "API_VOICE_LIMIT_REACHED"
            ))
            
            if not already_rotated:
                fresh_key = self._handle_401_error(current_key, "STAGE1")
                if fresh_key and fresh_key != current_key:
                    current_key = fresh_key  
                    used_keys.add(fresh_key)
                    tts_logger.info(f"STAGE1 - 401 - Rotated to fresh key {current_key[:10]}...")
            
            # Retry with fresh key (multiple attempts until we find working key or run out)
            max_key_attempts = 3  # Try up to 3 different keys
            attempt = 0
            
            while current_key != api_key and attempt < max_key_attempts:
                attempt += 1
                tts_logger.info(f"STAGE1 retry attempt {attempt}/{max_key_attempts} with fresh key {current_key[:10]}...")
                
                # Retry Stage 1 with fresh key
                success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                    url, current_key, payload_json, out_path, proxy=None
                )
                
                if success_retry and file_head_retry:
                    tts_logger.info(f"STAGE1 retry success with key {current_key[:10]}...")
                    return current_key
                elif file_head_retry:
                    # Check if new key also has same error
                    error_type = self._check_api_error_response(file_head_retry, f"STAGE1-RETRY-{attempt}")
                    if error_type == "API_VOICE_LIMIT_REACHED":
                        tts_logger.error(f"STAGE1-RETRY-{attempt} - Key {current_key[:10]}... also has voice limit, trying next key")
                        # Get another fresh key
                        next_fresh_key = self._handle_voice_limit_error(current_key, f"STAGE1-RETRY-{attempt}", needed_chars)
                        if next_fresh_key and next_fresh_key not in used_keys:
                            current_key = next_fresh_key
                            used_keys.add(next_fresh_key)
                        else:
                            break  # No more fresh keys available
                    else:
                        break  # Different error, stop retrying
                    try: os.unlink(out_path)
                    except Exception: pass
                else:
                    break  # Network/HTTP error, stop retrying
        
        tts_logger.error(f"STAGE1 fail: {error_msg}")

        if not self.proxy_service:
            tts_logger.error("STAGE2_SKIP - No proxy service configured, only Stage 1 attempted")
            raise RuntimeError(f"Stage 1 failed (rc={res1.returncode}) and no proxy service available for fallback")

        # sắp xếp gateway tốt nhất hiện tại
        active_gw = self.proxy_service.choose_best_gateway() or self.proxy_service.get_current_gateway()
        gateways = self.proxy_service.get_all_gateways()
        if not gateways:
            tts_logger.error("STAGE2_SKIP - No proxy gateways available, only Stage 1 attempted")
            tts_logger.error("PROXY_HELP - Please check: 1) User has proxy config in database 2) Proxy service loaded correctly")
            raise RuntimeError(f"Stage 1 failed and no proxy gateways available for fallback")

        # Sort gateways by priority: Canada -> Canada ISP -> Standard -> VPS
        ordered = []
        
        # Priority 1: Canada proxies (2-phase flow with request_id, HIGHEST priority)
        canada_gateways = [g for g in gateways if getattr(g, 'token_proxy', '').lower() == 'canada']
        ordered.extend(canada_gateways)
        
        # Priority 2: Canada ISP proxies (single-phase, less precise)
        canada_isp_gateways = [g for g in gateways if getattr(g, 'token_proxy', '').lower() == 'canada_isp']
        ordered.extend(canada_isp_gateways)
        
        # Priority 3: Standard proxies
        standard_gateways = [g for g in gateways if getattr(g, 'token_proxy', '').lower() not in ['vps', 'canada', 'canada_isp']]
        ordered.extend(standard_gateways)
        
        # Priority 4: VPS proxies (lowest priority)
        vps_gateways = [g for g in gateways if getattr(g, 'token_proxy', '').lower() == 'vps']
        ordered.extend(vps_gateways)
        
        # Log the priority order for debugging
        tts_logger.info(f"PROXY_PRIORITY_ORDER: {len(ordered)} gateways")
        for i, gw in enumerate(ordered):
            proxy_type = getattr(gw, 'token_proxy', 'unknown').lower()
            tts_logger.info(f"  {i+1}. {gw.host}:{gw.port} ({proxy_type})")

        # Special handling for VPS proxies: single attempt, no rotation in Stage 2 & skip Stage 3
        try:
            token_proxy = getattr(active_gw, 'token_proxy', '') if active_gw else ''
            is_vps_proxy = isinstance(token_proxy, str) and token_proxy.lower() == 'vps'
            is_canada_proxy = isinstance(token_proxy, str) and token_proxy.lower() == 'canada'
        except Exception:
            is_vps_proxy = False
            is_canada_proxy = False

        if is_vps_proxy and active_gw:
            proxy_cfg = self.proxy_service.as_proxy_dict(active_gw)
            self._probe_proxy_ip(proxy_cfg, label=f"STAGE2-VPS pre-call {active_gw.host}:{active_gw.port}")
            tts_logger.info(f"STAGE2-VPS single try {active_gw.host}:{active_gw.port} with key {current_key[:10]}...")
            success_vps, error_msg_vps, file_head_vps = self._make_http_request(
                url, current_key, payload_json, out_path, proxy=proxy_cfg
            )

            if success_vps and file_head_vps:
                tts_logger.info(f"STAGE2-VPS success {active_gw.host}:{active_gw.port}")
                return current_key
            elif file_head_vps:
                error_info = self._check_api_error_response(file_head_vps, f"STAGE2-VPS-{active_gw.host}:{active_gw.port}")
                tts_logger.error(f"STAGE2-VPS API error: {error_info}")
                try: os.unlink(out_path)
                except Exception: pass

            tts_logger.error(f"STAGE2-VPS fail: {error_msg_vps}")
            # VPS policy: single attempt only, continue to next proxy (Canada)
            tts_logger.info(f"STAGE2-VPS single attempt failed, continuing to next proxy...")

        # Canada proxies: use 2-phase flow (generate via proxy, then download via no proxy)

        # ===== Stage 2 — Thử từng gateway với key rotation =====
        last_rc, last_err = None, None
        for gw in ordered:
            # Skip VPS proxy in Stage 2 loop - already handled separately above
            if getattr(gw, 'token_proxy', '').lower() == 'vps':
                tts_logger.info(f"STAGE2 skip VPS proxy {gw.host}:{gw.port} - already handled separately")
                continue
                
            proxy_cfg = self.proxy_service.as_proxy_dict(gw)
            self._probe_proxy_ip(proxy_cfg, label=f"STAGE2 pre-call {gw.host}:{gw.port}")
            
            # For Canada ISP proxy, use single-phase flow (FASTEST)
            if getattr(gw, 'token_proxy', '').lower() == 'canada_isp':
                try:
                    tts_logger.info(f"STAGE2-CANADA_ISP single-phase {gw.host}:{gw.port} with key {current_key[:10]}...")
                    success_isp, error_msg_isp, file_head_isp = self._make_http_request(
                        url, current_key, payload_json, out_path, proxy=proxy_cfg
                    )
                    
                    if success_isp and file_head_isp:
                        tts_logger.info(f"STAGE2-CANADA_ISP ✅ {gw.host}:{gw.port}")
                        return current_key
                    elif file_head_isp:
                        error_info = self._check_api_error_response(file_head_isp, f"STAGE2-CANADA_ISP-{gw.host}:{gw.port}")
                        
                        # Handle specific errors with appropriate logging level
                        if error_info == "API_UNUSUAL_ACTIVITY":
                            tts_logger.warning(f"STAGE2-CANADA_ISP Unusual Activity detected {gw.host}:{gw.port} - IP may be rate limited, trying next proxy")
                        else:
                            tts_logger.error(f"STAGE2-CANADA_ISP API error {gw.host}:{gw.port}: {error_info}")
                        
                        try: os.unlink(out_path)
                        except Exception: pass
                    else:
                        tts_logger.error(f"STAGE2-CANADA_ISP ❌ {gw.host}:{gw.port} - {error_msg_isp}")
                    
                    # Continue to next gateway on failure
                    last_rc, last_err = None, error_msg_isp
                    continue
                except Exception as e:
                    tts_logger.error(f"STAGE2-CANADA_ISP EXC {gw.host}:{gw.port}: {e}")
                    last_rc, last_err = 1, str(e)
                    continue
            
            # For Canada proxy, use 2-phase specialized flow (same as test script)
            if getattr(gw, 'token_proxy', '').lower() == 'canada':
                try:
                    tts_logger.info(f"STAGE2-CANADA importing tts_service_canada module...")
                    from services import tts_service_canada as ca
                    tts_logger.info(f"STAGE2-CANADA import OK, starting 2-phase flow {gw.host}:{gw.port} with key {current_key[:10]}...")
                    # Use same optimized flow as test script: bandwidth optimization + proper retries
                    ok2, err2 = ca.canada_generate_then_download(current_key, voice_id, payload_json, proxy_cfg, out_path, retries=3)
                    tts_logger.info(f"STAGE2-CANADA 2-phase completed: ok={ok2}, err='{err2[:200]}'")
                    if ok2:
                        # Validate MP3 head quickly
                        with open(out_path, "rb") as f:
                            file_head = f.read(512)
                        if self._is_valid_mp3_head(file_head):
                            tts_logger.info(f"STAGE2-CANADA success {gw.host}:{gw.port} (Phase1: Range+max-filesize, Phase2: no-proxy download)")
                            return current_key
                        else:
                            tts_logger.error(f"STAGE2-CANADA NON_MP3 after download")
                            try: os.unlink(out_path)
                            except Exception: pass
                    else:
                        tts_logger.error(f"STAGE2-CANADA fail {gw.host}:{gw.port} - {err2[:300]}")
                    # continue to next gateway on failure
                    last_rc, last_err = 22, err2
                    continue
                except ImportError as e:
                    tts_logger.error(f"STAGE2-CANADA IMPORT_ERROR {gw.host}:{gw.port}: {e}")
                    last_rc, last_err = 1, f"Import error: {e}"
                    continue
                except Exception as e:
                    tts_logger.error(f"STAGE2-CANADA EXC {gw.host}:{gw.port}: {e}")
                    last_rc, last_err = 1, str(e)
                    continue

            # Non-Canada: try simple single-call via proxy
            tts_logger.info(f"STAGE2 trying {gw.host}:{gw.port} with key {current_key[:10]}...")
            success, error_msg, file_head = self._make_http_request(
                url, current_key, payload_json, out_path, proxy=proxy_cfg
            )
            
            if success and file_head:
                tts_logger.info(f"STAGE2 success {gw.host}:{gw.port}")
                return current_key
            elif file_head:
                # Check if it's a JSON error response AND HANDLE IMMEDIATELY
                error_info = self._check_api_error_response(file_head, f"STAGE2-{gw.host}:{gw.port}")
                tts_logger.error(f"STAGE2 {gw.host}:{gw.port} API error: {error_info}")
                
                # IMMEDIATE handling for voice_limit_reached  
                if error_info == "API_VOICE_LIMIT_REACHED":
                    tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT_REACHED - IMMEDIATE ROTATION of key {current_key[:10]}...")
                    needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                    fresh_key = self._handle_voice_limit_error(current_key, f"STAGE2-{gw.host}:{gw.port}", needed_chars)
                    if fresh_key and fresh_key != current_key:
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                        tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT - IMMEDIATE ROTATED to fresh key {current_key[:10]}...")
                        
                        # IMMEDIATE retry with fresh key on this proxy
                        tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT - IMMEDIATE RETRY with fresh key {current_key[:10]}...")
                        success_immediate, error_msg_immediate, file_head_immediate = self._make_http_request(
                            url, current_key, payload_json, out_path, proxy=proxy_cfg
                        )
                        
                        if success_immediate and file_head_immediate:
                            tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT - IMMEDIATE SUCCESS!")
                            return current_key
                try: os.unlink(out_path)
                except Exception: pass
            
            # Handle 401 error - check error message and response content
            need_key_rotation = False
            skip_gateway = False
            response_error_type = None
            
            # Check error message for 401
            if error_msg and ("401" in error_msg or "unauthorized" in error_msg.lower()):
                need_key_rotation = True
            # Check for proxy auth / DNS / TLS issues in error message
            if error_msg and ('proxy authentication required' in error_msg.lower() or '407' in error_msg):
                skip_gateway = True
                tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - PROXY_AUTH_REQUIRED")
            if error_msg and ('could not resolve host' in error_msg.lower() or 'ssl' in error_msg.lower() or 'connection' in error_msg.lower()):
                skip_gateway = True
                tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - NETWORK_DNS_TLS_ERROR: {error_msg[:120]}")
            
            # Check response content for errors and handle key rotation
            if file_head:
                try:
                    response_error_type = self._check_api_error_response(file_head, f"STAGE2-{gw.host}:{gw.port}")
                    
                    if response_error_type == "API_UNAUTHORIZED":
                        need_key_rotation = True
                        tts_logger.error(f"STAGE2 {gw.host}:{gw.port} detected 401 in response content")
                    elif response_error_type and response_error_type.startswith("API_QUOTA_EXCEEDED"):
                        # Không đánh exhausted: chỉ bỏ key này khỏi request hiện tại, lấy key khác
                        tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - QUOTA_EXCEEDED - Key {current_key[:10]}... out of credits, skipping (no exhausted mark)")
                        
                        needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                        used_keys.add(current_key)
                        fresh_key = self._get_fresh_api_key(needed_chars, exclude_keys=used_keys)
                        if fresh_key and fresh_key not in used_keys:
                            current_key = fresh_key
                            used_keys.add(fresh_key)
                            
                            # IMMEDIATE retry with fresh key on SAME PROXY
                            tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - QUOTA_EXCEEDED - IMMEDIATE RETRY with fresh key {current_key[:10]}...")
                            success_immediate, error_msg_immediate, file_head_immediate = self._make_http_request(
                                url, current_key, payload_json, out_path, proxy=proxy_cfg
                            )
                            
                            if success_immediate and file_head_immediate:
                                tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - QUOTA_EXCEEDED - IMMEDIATE SUCCESS!")
                                return current_key
                            try: os.unlink(out_path)
                            except Exception: pass
                            
                            # Nếu retry vẫn fail, bỏ qua proxy này
                            last_rc, last_err = None, error_msg_immediate
                            tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - QUOTA_EXCEEDED RETRY failed: {error_msg_immediate}")
                        else:
                            tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - QUOTA_EXCEEDED - No fresh key available, skip proxy")
                    elif response_error_type == "API_VOICE_LIMIT_REACHED":
                        # Handle voice limit reached - rotate to different API key immediately
                        tts_logger.error(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT_REACHED - Rotating key {current_key[:10]}...")
                        needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                        fresh_key = self._handle_voice_limit_error(current_key, f"STAGE2-{gw.host}:{gw.port}", needed_chars)
                        if fresh_key and fresh_key != current_key and fresh_key not in used_keys:
                            current_key = fresh_key
                            used_keys.add(fresh_key)
                            need_key_rotation = True
                            tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - VOICE_LIMIT - Rotated to fresh key {current_key[:10]}...")
                    elif response_error_type == "API_RATE_LIMIT":
                        # Light backoff + retry on same gateway once, then move on
                        import random, time as _t
                        backoff = random.uniform(0.5, 1.5)
                        tts_logger.info(f"STAGE2 {gw.host}:{gw.port} - RATE_LIMIT - sleeping {backoff:.2f}s then retry once")
                        _t.sleep(backoff)
                    elif response_error_type in ("API_FORBIDDEN", "API_LEGAL_BLOCK"):
                        # IP-based block → try next gateway (no key rotation here)
                        skip_gateway = True
                except Exception:
                    pass
            
            if skip_gateway:
                last_rc, last_err = None, error_msg
                tts_logger.error(f"STAGE2 skip gateway {gw.host}:{gw.port} due to proxy/network/IP block")
                continue

            if need_key_rotation:
                # Skip 401 retry if we already handled specific errors
                if not (response_error_type and (
                    response_error_type.startswith("API_QUOTA_EXCEEDED") or 
                    response_error_type == "API_VOICE_LIMIT_REACHED"
                )):
                    fresh_key = self._handle_401_error(current_key, f"STAGE2-{gw.host}:{gw.port}")
                    if fresh_key and fresh_key not in used_keys:
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                
                # Retry with fresh key (multiple attempts for voice_limit_reached)
                max_key_attempts = 2 if response_error_type == "API_VOICE_LIMIT_REACHED" else 1
                attempt = 0
                
                while current_key != api_key and attempt < max_key_attempts:
                    attempt += 1
                    tts_logger.info(f"STAGE2 retry attempt {attempt}/{max_key_attempts} {gw.host}:{gw.port} with fresh key {current_key[:10]}...")
                    success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                        url, current_key, payload_json, out_path, proxy=proxy_cfg
                    )
                    
                    if success_retry and file_head_retry:
                        tts_logger.info(f"STAGE2 retry success {gw.host}:{gw.port} with key {current_key[:10]}...")
                        return current_key
                    elif file_head_retry:
                        # Check if new key also has same error
                        error_type = self._check_api_error_response(file_head_retry, f"STAGE2-RETRY-{attempt}-{gw.host}:{gw.port}")
                        if error_type == "API_VOICE_LIMIT_REACHED":
                            tts_logger.error(f"STAGE2-RETRY-{attempt} {gw.host}:{gw.port} - Key {current_key[:10]}... also has voice limit, trying next key")
                            # Get another fresh key
                            needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                            next_fresh_key = self._handle_voice_limit_error(current_key, f"STAGE2-RETRY-{attempt}-{gw.host}:{gw.port}", needed_chars)
                            if next_fresh_key and next_fresh_key not in used_keys:
                                current_key = next_fresh_key
                                used_keys.add(next_fresh_key)
                            else:
                                break  # No more fresh keys available
                        else:
                            break  # Different error, stop retrying
                        try: os.unlink(out_path)
                        except Exception: pass
                    else:
                        break  # Network/HTTP error, stop retrying
            
            last_rc, last_err = None, error_msg
            tts_logger.error(f"STAGE2 fail {gw.host}:{gw.port}: {error_msg}")

        # ===== Stage 3 — Failover vòng cuối với key rotation =====
        # If VPS, Stage 3 is skipped entirely (handled above)
        # thử lại nhanh theo round-robin khác thứ tự để "bắt" IP vừa đổi
        alt_list = list(reversed(ordered))
        stage3_last_rc = None
        
        for gw in alt_list:
            proxy_cfg = self.proxy_service.as_proxy_dict(gw)
            self._probe_proxy_ip(proxy_cfg, label=f"STAGE3 pre-call {gw.host}:{gw.port}")
            
            # Try with current key first
            tts_logger.info(f"STAGE3 trying {gw.host}:{gw.port} with key {current_key[:10]}...")
            success, error_msg, file_head = self._make_http_request(
                url, current_key, payload_json, out_path, proxy=proxy_cfg
            )
            
            if success and file_head:
                tts_logger.info(f"STAGE3 success {gw.host}:{gw.port}")
                return current_key
            elif file_head:
                # Check if it's a JSON error response AND HANDLE IMMEDIATELY
                error_info = self._check_api_error_response(file_head, f"STAGE3-{gw.host}:{gw.port}")
                tts_logger.error(f"STAGE3 {gw.host}:{gw.port} API error: {error_info}")
                
                # IMMEDIATE handling for voice_limit_reached  
                if error_info == "API_VOICE_LIMIT_REACHED":
                    tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - VOICE_LIMIT_REACHED - IMMEDIATE ROTATION of key {current_key[:10]}...")
                    needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                    fresh_key = self._handle_voice_limit_error(current_key, f"STAGE3-{gw.host}:{gw.port}", needed_chars)
                    if fresh_key and fresh_key != current_key:
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                        tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - VOICE_LIMIT - IMMEDIATE ROTATED to fresh key {current_key[:10]}...")
                        
                        # IMMEDIATE retry with fresh key on this proxy
                        tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - VOICE_LIMIT - IMMEDIATE RETRY with fresh key {current_key[:10]}...")
                        success_immediate, error_msg_immediate, file_head_immediate = self._make_http_request(
                            url, current_key, payload_json, out_path, proxy=proxy_cfg
                        )
                        
                        if success_immediate and file_head_immediate:
                            tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - VOICE_LIMIT - IMMEDIATE SUCCESS!")
                            return current_key
                try: os.unlink(out_path)
                except Exception: pass
            
            # Handle 401 error - check error message and response content
            need_key_rotation = False
            skip_gateway = False
            response_error_type = None
            
            # Check error message for 401
            if error_msg and ("401" in error_msg or "unauthorized" in error_msg.lower()):
                need_key_rotation = True
            if error_msg and ('proxy authentication required' in error_msg.lower() or '407' in error_msg):
                skip_gateway = True
                tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - PROXY_AUTH_REQUIRED")
            if error_msg and ('could not resolve host' in error_msg.lower() or 'ssl' in error_msg.lower() or 'connection' in error_msg.lower()):
                skip_gateway = True
                tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - NETWORK_DNS_TLS_ERROR: {error_msg[:120]}")
            
            # Check response content for errors and handle key rotation  
            if file_head:
                try:
                    response_error_type = self._check_api_error_response(file_head, f"STAGE3-{gw.host}:{gw.port}")
                    
                    if response_error_type == "API_UNAUTHORIZED":
                        need_key_rotation = True
                        tts_logger.error(f"STAGE3 {gw.host}:{gw.port} detected 401 in response content")
                    elif response_error_type and response_error_type.startswith("API_QUOTA_EXCEEDED"):
                        # Handle quota exceeded - mark current key exhausted and rotate to fresh key with credits
                        tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - QUOTA_EXCEEDED - Key {current_key[:10]}... out of credits, marking exhausted")
                        try:
                            with self.pool_lock:
                                self.pool.mark_exhausted(current_key)
                        except Exception:
                            pass
                        
                        needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                        fresh_key = self._handle_quota_exceeded_error(current_key, f"STAGE3-{gw.host}:{gw.port}", needed_chars)
                        if fresh_key and fresh_key not in used_keys:
                            current_key = fresh_key
                            used_keys.add(fresh_key)
                            
                            # IMMEDIATE retry with fresh key on SAME PROXY
                            tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - QUOTA_EXCEEDED - IMMEDIATE RETRY with fresh key {current_key[:10]}...")
                            success_immediate, error_msg_immediate, file_head_immediate = self._make_http_request(
                                url, current_key, payload_json, out_path, proxy=proxy_cfg
                            )
                            
                            if success_immediate and file_head_immediate:
                                tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - QUOTA_EXCEEDED - IMMEDIATE SUCCESS!")
                                return current_key
                            elif file_head_immediate:
                                # Check if new key also has quota exceeded
                                error_type = self._check_api_error_response(file_head_immediate, f"STAGE3-QUOTA-RETRY-{gw.host}:{gw.port}")
                                if error_type and error_type.startswith("API_QUOTA_EXCEEDED"):
                                    tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - QUOTA_EXCEEDED RETRY - Fresh key {current_key[:10]}... also exhausted")
                                    # Mark this key as exhausted too and continue to next proxy
                                    try:
                                        with self.pool_lock:
                                            self.pool.mark_exhausted(current_key)
                                    except Exception:
                                        pass
                                else:
                                    tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - QUOTA_EXCEEDED RETRY - Different error: {error_type}")
                            try: os.unlink(out_path)
                            except Exception: pass
                            
                            # If retry failed, skip to next proxy with fresh key
                            stage3_last_rc = None
                            tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - QUOTA_EXCEEDED RETRY failed: {error_msg_immediate}")
                            # Skip to next gateway - this will be handled by the outer loop naturally
                    elif response_error_type == "API_VOICE_LIMIT_REACHED":
                        # Handle voice limit reached - rotate to different API key immediately
                        tts_logger.error(f"STAGE3 {gw.host}:{gw.port} - VOICE_LIMIT_REACHED - Rotating key {current_key[:10]}...")
                        needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                        fresh_key = self._handle_voice_limit_error(current_key, f"STAGE3-{gw.host}:{gw.port}", needed_chars)
                        if fresh_key and fresh_key != current_key and fresh_key not in used_keys:
                            current_key = fresh_key
                            used_keys.add(fresh_key)
                            need_key_rotation = True
                            tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - VOICE_LIMIT - Rotated to fresh key {current_key[:10]}...")
                    elif response_error_type == "API_RATE_LIMIT":
                        # Light backoff + retry on same gateway once, then move on
                        import random, time as _t
                        backoff = random.uniform(0.5, 1.5)
                        tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - RATE_LIMIT - sleeping {backoff:.2f}s then continue")
                        _t.sleep(backoff)
                    elif response_error_type in ("API_FORBIDDEN", "API_LEGAL_BLOCK"):
                        # IP-based block → try next gateway
                        skip_gateway = True
                except Exception:
                    pass
        
        if skip_gateway:
            stage3_last_rc = None
            tts_logger.error(f"STAGE3 skip gateway {gw.host}:{gw.port} due to proxy/network/IP block")

            if need_key_rotation:
                # Skip 401 retry if we already handled specific errors
                already_rotated = (response_error_type and (
                    response_error_type.startswith("API_QUOTA_EXCEEDED") or 
                    response_error_type == "API_VOICE_LIMIT_REACHED"
                ))
                
                if not already_rotated:
                    fresh_key = self._handle_401_error(current_key, f"STAGE3-{gw.host}:{gw.port}")
                    if fresh_key and fresh_key != current_key and fresh_key not in used_keys:
                        current_key = fresh_key
                        used_keys.add(fresh_key)
                        tts_logger.info(f"STAGE3 {gw.host}:{gw.port} - 401 - Rotated to fresh key {current_key[:10]}...")
                
                # Retry with fresh key (multiple attempts for voice_limit_reached)
                max_key_attempts = 2 if response_error_type == "API_VOICE_LIMIT_REACHED" else 1
                attempt = 0
                
                while current_key != api_key and attempt < max_key_attempts:
                    attempt += 1
                    tts_logger.info(f"STAGE3 retry attempt {attempt}/{max_key_attempts} {gw.host}:{gw.port} with fresh key {current_key[:10]}...")
                    success_retry, error_msg_retry, file_head_retry = self._make_http_request(
                        url, current_key, payload_json, out_path, proxy=proxy_cfg
                    )
                    
                    if success_retry and file_head_retry:
                        tts_logger.info(f"STAGE3 retry success {gw.host}:{gw.port} with key {current_key[:10]}...")
                        return current_key
                    elif file_head_retry:
                        # Check if new key also has same error
                        error_type = self._check_api_error_response(file_head_retry, f"STAGE3-RETRY-{attempt}-{gw.host}:{gw.port}")
                        if error_type == "API_VOICE_LIMIT_REACHED":
                            tts_logger.error(f"STAGE3-RETRY-{attempt} {gw.host}:{gw.port} - Key {current_key[:10]}... also has voice limit, trying next key")
                            # Get another fresh key
                            needed_chars = estimate_credits_for_text(self._current_payload.get('model_id', ''), self._current_payload.get('text', ''))
                            next_fresh_key = self._handle_voice_limit_error(current_key, f"STAGE3-RETRY-{attempt}-{gw.host}:{gw.port}", needed_chars)
                            if next_fresh_key and next_fresh_key not in used_keys:
                                current_key = next_fresh_key
                                used_keys.add(next_fresh_key)
                            else:
                                break  # No more fresh keys available
                        else:
                            break  # Different error, stop retrying
                        try: os.unlink(out_path)
                        except Exception: pass
                    else:
                        break  # Network/HTTP error, stop retrying
            
            stage3_last_rc = None
            tts_logger.error(f"STAGE3 fail {gw.host}:{gw.port}: {error_msg}")

        # All 3 stages failed - now retry: Stage 1 → Stage 2 → Kết thúc
        tts_logger.warning(f"ALL_3_STAGES_FAILED - Retrying: Stage 1 → Stage 2 → Kết thúc")
        
        # ===== RETRY Stage 1 =====
        tts_logger.info(f"RETRY-STAGE1 start (no proxy) with key {current_key[:10]}...")
        success_retry, error_msg_retry, file_head_retry = self._make_http_request(
            url, current_key, payload_json, out_path, proxy=None
        )
        
        if success_retry and file_head_retry:
            tts_logger.info(f"RETRY-STAGE1 success")
            return current_key
        elif file_head_retry:
            error_info = self._check_api_error_response(file_head_retry, "RETRY-STAGE1")
            tts_logger.error(f"RETRY-STAGE1 API error: {error_info}")
            try: os.unlink(out_path)
            except Exception: pass
        
        tts_logger.error(f"RETRY-STAGE1 fail: {error_msg_retry}")
        
        # ===== RETRY Stage 2 =====
        tts_logger.info(f"RETRY-STAGE2 start with first available proxy")
        
        # Get fresh proxy list for retry
        retry_gateways = self.proxy_service.get_all_gateways()
        if not retry_gateways:
            tts_logger.error("RETRY-STAGE2_SKIP - No proxy gateways available")
        else:
            # Try first available proxy
            retry_gw = retry_gateways[0]
            retry_proxy_cfg = self.proxy_service.as_proxy_dict(retry_gw)
            self._probe_proxy_ip(retry_proxy_cfg, label=f"RETRY-STAGE2 pre-call {retry_gw.host}:{retry_gw.port}")
            
            tts_logger.info(f"RETRY-STAGE2 trying {retry_gw.host}:{retry_gw.port} with key {current_key[:10]}...")
            success_retry2, error_msg_retry2, file_head_retry2 = self._make_http_request(
                url, current_key, payload_json, out_path, proxy=retry_proxy_cfg
            )
            
            if success_retry2 and file_head_retry2:
                tts_logger.info(f"RETRY-STAGE2 success {retry_gw.host}:{retry_gw.port}")
                return current_key
            elif file_head_retry2:
                error_info = self._check_api_error_response(file_head_retry2, f"RETRY-STAGE2-{retry_gw.host}:{retry_gw.port}")
                tts_logger.error(f"RETRY-STAGE2 API error: {error_info}")
                try: os.unlink(out_path)
                except Exception: pass
            
            tts_logger.error(f"RETRY-STAGE2 fail {retry_gw.host}:{retry_gw.port}: {error_msg_retry2}")
        
        # Final failure after retry
        tts_logger.error(f"FINAL_FAILURE - All attempts exhausted: Stage 1 → Stage 2 → Stage 3 → Stage 1 → Stage 2")
        tts_logger.error(f"FAILURE_DETAILS - Text length: {len(self._current_payload.get('text', ''))} chars, Voice ID: {voice_id}")
        tts_logger.error(f"USED_KEYS - {len(used_keys)} different API keys: {[k[:10]+'...' for k in used_keys]}")
        
        raise RuntimeError(
            f"All attempts failed: Stage 1 → Stage 2 → Stage 3 → Stage 1 → Stage 2. "
            f"Possible causes: voice_limit_reached on all keys, invalid voice_id, or network issues"
        )



    # ---------- public API ----------
    def _synthesize_file_with_proxy(self, api_key: str, voice_id: str, model_name: str, text: str,
                                    voice_settings: Optional[dict] = None, stop_callback=None,
                                    on_status_update=None, proxy_cfg: Optional[dict] = None,
                                    proxy_url: Optional[str] = None) -> str:
        """
        SIMPLIFIED: Synthesize với proxy cụ thể - CHỈ 1 PHASE.
        
        🔧 ĐƠN GIẢN HÓA - Bỏ logic 2-phase, chỉ 1 POST request với proxy.
        """
        if stop_callback and stop_callback():
            raise RuntimeError("Stop requested")

        # Sanitize text
        clean_text = self.sanitize_text(text, model_name=model_name)
        clean_text = self.add_ssml_breaks(clean_text)

        # Build payload
        payload = {
            "text": clean_text,
            "model_id": model_name,
            "optimize_streaming_latency": "0",
            "output_format": "mp3_22050_32",
        }
        if voice_settings:
            payload["voice_settings"] = {
                "stability": float(voice_settings.get("stability", 0.0)),
                "similarity_boost": float(voice_settings.get("similarity_boost", 1.0)),
                "style": float(voice_settings.get("style", 0.0)),
                "use_speaker_boost": bool(voice_settings.get("use_speaker_boost", False)),
                "speed": max(0.7, min(1.2, float(voice_settings.get("speed", 1.0)))),
            }

        needed_chars = estimate_credits_for_text(model_name, clean_text)
        
        # Validate credits
        tts_logger.info(f"SYNTHESIZE_PRE_CHECK - Key {api_key[:10]}... needs {needed_chars} chars")
        if not self._validate_key_with_retry(api_key, needed_chars, force_check=True):
            tts_logger.error(f"SYNTHESIZE_CREDIT_FAIL - Key {api_key[:10]}... insufficient credits")
            raise KeyValidationError(api_key, f"Insufficient credits: {api_key[:10]}...")

        out_path = os.path.join(self.output_dir, f"{uuid.uuid4()}.mp3")
        
        # 🔧 NEW: Use SIMPLIFIED single-phase TTS
        # Nếu có proxy_cfg được chỉ định, tạo temp ProxyService với proxy đó
        temp_proxy_service = None
        if proxy_cfg:
            # Tạo temp service với proxy này
            class TempProxyWrapper:
                def __init__(self, cfg):
                    self.cfg = cfg
                def get_current_proxy(self):
                    host = self.cfg.get("host")
                    port = self.cfg.get("port")
                    user = self.cfg.get("username")
                    pwd = self.cfg.get("password")
                    if host and port:
                        if user:
                            return f"http://{user}:{pwd}@{host}:{port}"
                        return f"http://{host}:{port}"
                    return None
                def rotate_to_next(self):
                    return self.get_current_proxy()
            
            temp_proxy_service = TempProxyWrapper(proxy_cfg)
        else:
            temp_proxy_service = self.proxy_service
        
        # Call simplified TTS
        from services import tts_service_canada
        success, message = tts_service_canada.tts_synthesize_simple(
            api_key=api_key,
            voice_id=voice_id,
            text=clean_text,
            model_id=model_name,
            voice_settings=payload.get("voice_settings", {}),
            output_path=out_path,
            proxy_service=temp_proxy_service,
            max_retries=3
        )
        
        if not success:
            raise RuntimeError(f"TTS synthesis failed: {message}")
        
        used_key = api_key

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("Output file missing after success path")

        # Status: Uploading
        if on_status_update:
            try:
                on_status_update("uploading", "Finalizing audio file...")
            except Exception:
                pass

        # Credit tracking
        if self.credit_tracker:
            usage = self.credit_tracker.use_credits(used_key, model_name, text,
                                                    endpoint="text_to_speech",
                                                    success=True,
                                                    response_data=f"File: {out_path}")
            self._sync_pool_after_credit_tracker(used_key, usage)
        else:
            with self.pool_lock:
                self.pool.deduct(used_key, needed_chars)
        
        tts_logger.info(f"SYNTHESIZE_SUCCESS - Used key {used_key[:10]}... for {len(clean_text)} chars")
        return out_path

    def synthesize_file(self, api_key: str, voice_id: str, model_name: str, text: str,
                        voice_settings: Optional[dict] = None, stop_callback=None,
                        on_status_update=None) -> str:
        """
        SIMPLIFIED: Synthesize text to speech - CHỈ 1 PHASE với proxy.
        
        🔧 ĐƠN GIẢN HÓA:
        - Bỏ hết logic 3-stage (Stage 1/2/3)
        - Bỏ logic 2-phase (Canada generate + download)
        - Bỏ token_proxy types (canada, canada_isp, vps, etc.)
        
        CHỈ CÒN:
        - 1 POST request với proxy
        - Auto retry với proxy rotation
        - Migrated từ 11Labs0811.py tts_direct()
        """
        if stop_callback and stop_callback():
            raise RuntimeError("Stop requested")

        # Status: Verifying
        if on_status_update:
            try:
                on_status_update("verify", "Verifying API key...")
            except Exception:
                pass

        # Sanitize text
        clean_text = self.sanitize_text(text, model_name=model_name)
        clean_text = self.add_ssml_breaks(clean_text)

        # Build payload
        payload = {
            "text": clean_text,
            "model_id": model_name,
            "optimize_streaming_latency": "0",
            "output_format": "mp3_22050_32",
        }
        if voice_settings:
            payload["voice_settings"] = {
                "stability": float(voice_settings.get("stability", 0.0)),
                "similarity_boost": float(voice_settings.get("similarity_boost", 1.0)),
                "style": float(voice_settings.get("style", 0.0)),
                "use_speaker_boost": bool(voice_settings.get("use_speaker_boost", False)),
                "speed": max(0.7, min(1.2, float(voice_settings.get("speed", 1.0)))),
            }

        needed_chars = estimate_credits_for_text(model_name, clean_text)
        
        # Status: Checking key
        if on_status_update:
            try:
                on_status_update("check_key", f"Checking key credits ({needed_chars} chars needed)...")
            except Exception:
                pass
        
        # Validate credits
        tts_logger.info(f"SYNTHESIZE_PRE_CHECK - Key {api_key[:10]}... needs {needed_chars} chars")
        if not self._validate_key_with_retry(api_key, needed_chars, force_check=True):
            tts_logger.error(f"SYNTHESIZE_CREDIT_FAIL - Key {api_key[:10]}... insufficient credits")
            raise KeyValidationError(api_key, f"API key insufficient credits: {api_key[:10]}...")

        out_path = os.path.join(self.output_dir, f"{uuid.uuid4()}.mp3")
        
        # Status: Synthesizing
        if on_status_update:
            try:
                on_status_update("synthesizing", "Synthesizing audio...")
            except Exception:
                pass
        
        # 🔧 NEW: Use SIMPLIFIED single-phase TTS with proxy
        used_key = self._call_tts_simple_with_proxy(api_key, voice_id, payload, out_path, on_status_update=on_status_update)

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("Output file missing after success path")

        # Status: Uploading (if needed)
        if on_status_update:
            try:
                on_status_update("uploading", "Finalizing audio file...")
            except Exception:
                pass

        # Use the actual key that was successful for credit tracking
        if self.credit_tracker:
            usage = self.credit_tracker.use_credits(used_key, model_name, text,
                                                    endpoint="text_to_speech",
                                                    success=True,
                                                    response_data=f"File: {out_path}")
            self._sync_pool_after_credit_tracker(used_key, usage)
        else:
            with self.pool_lock:
                self.pool.deduct(used_key, needed_chars)
        
        tts_logger.info(f"SYNTHESIZE_SUCCESS - Used key {used_key[:10]}... for {len(clean_text)} chars")
        return out_path

    # ---------- paragraph worker (1 đoạn) ----------
    def _process_single_paragraph(self, idx: int, total: int, para: str,
                              voice_id: str, model_name: str,
                              voice_settings: Optional[dict],
                              on_paragraph_start, on_paragraph_done, on_paragraph_error,
                              stop_callback, on_status_update=None,
                              assigned_proxy: Optional[dict] = None,
                              first_batch_used_proxies: Optional[set] = None,
                              first_batch_proxy_lock: Optional[threading.Lock] = None) -> Tuple[int, Optional[str]]:
        """
        Xử lý MỘT đoạn với retry VÔ HẠN:
        - Mỗi segment text dùng 1 proxy ngẫu nhiên (random từ Supabase)
        - Mỗi segment text dùng 1 key riêng (ưu tiên credits cao nhất, tránh trùng lặp)
        - chunk theo limit → synthesize từng chunk (tuần tự)
        - nếu BẤT KỲ chunk nào fail: hủy file tạm và retry LẠI TOÀN BỘ đoạn (đổi key / rotate proxy)
        - RETRY VÔ HẠN cho đến khi hoàn thành (bắt buộc phải hoàn thành)
        """
        import concurrent.futures
        if stop_callback and stop_callback():
            tts_logger.info(f"PARA{idx}: stop requested before start")
            return (idx, None)

        # callback start (chỉ gọi 1 lần cho đoạn này)
        if on_paragraph_start:
            try:
                with self.callback_lock:
                    on_paragraph_start(idx, para)
            except Exception as e:
                # Don't fail paragraph if UI callback fails
                tts_logger.warning(f"PARA{idx} - UI callback error (ignored): {e}")

        # Track keys đã dùng cho segment này (để tránh trùng lặp)
        segment_used_keys = set()
        
        # RETRY VÔ HẠN - không có giới hạn attempt
        attempt = 0

        while True:  # Retry vô hạn
            attempt += 1
            try:
                # CRITICAL: Chunk paragraph if it exceeds safe limits
                # Check both character count (4900) and credits (8000) to ensure safety
                MAX_CHARS_PER_CHUNK = TTSConfig.MAX_CHARS_PER_CHUNK
                para_credits = estimate_credits_for_text(model_name, para)
                
                if len(para) > MAX_CHARS_PER_CHUNK or para_credits > TTSConfig.MAX_SAFE_CREDITS_PER_CHUNK:
                    # Paragraph too large - chunk it by credits (will respect 4900 chars limit)
                    tts_logger.info(f"PARA{idx} - Paragraph too large ({len(para)} chars, {para_credits} credits), chunking...")
                    chunks = TextChunker.chunk_by_credits(
                        model_name, 
                        para, 
                        max_credits=TTSConfig.MAX_SAFE_CREDITS_PER_CHUNK,
                        per_request_char_limit=MAX_CHARS_PER_CHUNK  # Enforce 4900 chars limit
                    )
                    tts_logger.info(f"PARA{idx} - Chunked into {len(chunks)} chunks (max {MAX_CHARS_PER_CHUNK} chars, {TTSConfig.MAX_SAFE_CREDITS_PER_CHUNK} credits each)")
                else:
                    # Paragraph is small enough - process as single unit
                    chunks = [para]
                    tts_logger.info(f"PARA{idx} - Processing paragraph: {len(para)} chars ({para_credits} credits)")
                
                if not chunks:
                    raise RuntimeError("No chunks produced")

                chunk_files: List[str] = []

                for chunk_idx, chunk in enumerate(chunks, start=1):
                    if stop_callback and stop_callback():
                        raise RuntimeError("Stop requested")

                    needed = estimate_credits_for_text(model_name, chunk)
                    
                    tts_logger.info(f"PARA{idx}_CHUNK{chunk_idx}/{len(chunks)} - Processing {len(chunk)} chars: '{chunk[:50]}...'")
                    tts_logger.info(f"PARA{idx}_CHUNK{chunk_idx}_NEEDED - Need {needed} credits for this chunk")

                    # ===== MỖI CHUNK DÙNG 1 PROXY TỪ QUEUE (check_live-style) =====
                    # Batch đầu tiên: dùng proxy đã được assign (nếu có)
                    # Các batch sau: lấy proxy_url từ queue của ProxyService
                    proxy_cfg = None
                    proxy_url_used = None  # Lưu proxy_url để trả về queue sau khi dùng (giống check_live.py)
                    
                    if assigned_proxy:
                        # Batch đầu tiên: dùng proxy đã được assign
                        proxy_cfg = assigned_proxy.copy()  # Copy để tránh modify
                        proxy_key = f"{proxy_cfg.get('host')}:{proxy_cfg.get('port')}"
                        # Build proxy_url từ proxy_cfg để có thể trả về queue
                        host = proxy_cfg.get('host')
                        port = proxy_cfg.get('port')
                        username = proxy_cfg.get('username', '')
                        password = proxy_cfg.get('password', '')
                        if username:
                            proxy_url_used = f"http://{username}:{password}@{host}:{port}"
                        else:
                            proxy_url_used = f"http://{host}:{port}"
                        tts_logger.info(
                            f"PARA{idx}_CHUNK{chunk_idx}_ASSIGNED_PROXY - Using assigned proxy: "
                            f"{proxy_key} (type: {proxy_cfg.get('token_proxy', 'canada')})"
                        )
                        
                        # Track proxy đã dùng trong batch đầu tiên
                        if first_batch_used_proxies is not None and first_batch_proxy_lock is not None:
                            with first_batch_proxy_lock:
                                first_batch_used_proxies.add(proxy_key)
                    elif self.proxy_service and hasattr(self.proxy_service, 'get_next_proxy_url'):
                        # Các batch sau: lấy proxy_url từ queue (giống check_live.py)
                        proxy_url_used = self.proxy_service.get_next_proxy_url()
                        if proxy_url_used:
                            parsed_cfg = None
                            if hasattr(self.proxy_service, 'parse_proxy_url_to_cfg'):
                                parsed_cfg = self.proxy_service.parse_proxy_url_to_cfg(proxy_url_used)
                            if parsed_cfg:
                                proxy_cfg = parsed_cfg
                                # Gắn type mặc định 'canada' cho logging/logic downstream
                                proxy_cfg.setdefault("token_proxy", "canada")
                                proxy_key = f"{proxy_cfg.get('host')}:{proxy_cfg.get('port')}"
                                tts_logger.info(
                                    f"PARA{idx}_CHUNK{chunk_idx}_QUEUE_PROXY - Using proxy_url='{proxy_url_used}' "
                                    f"→ {proxy_key} (type: {proxy_cfg.get('token_proxy')})"
                                )
                            else:
                                tts_logger.warning(
                                    f"PARA{idx}_CHUNK{chunk_idx}_QUEUE_PROXY_PARSE_FAIL - Could not parse proxy_url='{proxy_url_used}'"
                                )
                        else:
                            tts_logger.warning(
                                f"PARA{idx}_CHUNK{chunk_idx}_NO_QUEUE_PROXY - No proxy_url available from queue, will use direct"
                            )
                    
                    exclude_keys_chunk = set()
                    key_switch_attempts = 0

                    while True:
                        api_key = None
                        reserved_now = False

                        if stop_callback and stop_callback():
                            raise RuntimeError("Stop requested")

                        tts_logger.info(f"PARA{idx}_CHUNK{chunk_idx}_GET_KEY_START - Starting to get API key (switch #{key_switch_attempts + 1})...")
                        
                        # ===== LẤY KEY THEO THỨ TỰ CREDITS TỪ LỚN XUỐNG BÉ, TRÁNH TRÙNG LẶP =====
                        # Exclude: keys đã dùng trong segment này + keys đang dùng bởi threads khác
                        excluded_for_segment = segment_used_keys.union(getattr(self, "_concurrent_in_use_keys", set()))
                        
                        # Try up to 50 keys để tìm key có credits cao nhất chưa dùng
                        for key_attempt in range(50):
                            tts_logger.debug(f"PARA{idx}_CHUNK{chunk_idx}_KEY_ATTEMPT_{key_attempt+1}/50 - Getting key from pool (credits DESC)...")
                            
                            try:
                                with self.pool_lock:
                                    tts_logger.debug(f"PARA{idx}_CHUNK{chunk_idx}_POOL_LOCK_ACQUIRED - Attempt {key_attempt+1}")
                                    # Get key với excluded = keys đã dùng trong segment + keys đang dùng
                                    candidate_key = self.pool.get_key(needed, excluded=excluded_for_segment.union(exclude_keys_chunk))
                                    if not candidate_key:
                                        # Nếu không có key nào, thử get_any_active_key nhưng vẫn exclude keys đã dùng
                                        all_keys = self.pool.get_any_active_key()
                                        if all_keys and all_keys not in excluded_for_segment.union(exclude_keys_chunk):
                                            candidate_key = all_keys
                                    tts_logger.debug(f"PARA{idx}_CHUNK{chunk_idx}_POOL_LOCK_RELEASED - Got candidate: {candidate_key[:10] if candidate_key else 'None'}...")
                            except Exception as e:
                                tts_logger.error(f"PARA{idx}_CHUNK{chunk_idx}_POOL_GET_ERROR - Error getting key: {e}")
                                import traceback
                                tts_logger.error(f"PARA{idx}_CHUNK{chunk_idx}_POOL_GET_TRACEBACK - {traceback.format_exc()}")
                                continue
                            
                            # If pool exhausted, reload once every 10 attempts
                            if not candidate_key and key_attempt % 10 == 0 and key_attempt > 0:
                                tts_logger.info(f"CHUNK_{chunk_idx} - Attempt {key_attempt}: reloading pool...")
                                try:
                                    with self.pool_lock:
                                        self.pool.load()
                                except Exception as e:
                                    tts_logger.error(f"CHUNK_{chunk_idx}_RELOAD_ERROR - {e}")
                                continue
                            
                            if not candidate_key:
                                tts_logger.debug(f"CHUNK_{chunk_idx}_NO_CANDIDATE - No candidate key at attempt {key_attempt+1}")
                                continue
                            
                            if candidate_key in exclude_keys_chunk:
                                tts_logger.debug(f"CHUNK_{chunk_idx}_EXCLUDED - Key {candidate_key[:10]}... already excluded")
                                continue
                            
                            # Reserve candidate key to avoid other threads picking it
                            reserved_now = False
                            try:
                                with self._concurrent_lock:
                                    if candidate_key not in self._concurrent_in_use_keys:
                                        self._concurrent_in_use_keys.add(candidate_key)
                                        reserved_now = True
                                    else:
                                        # Someone else took it; try another
                                        continue
                            except Exception:
                                pass
                            
                            tts_logger.info(f"CHUNK_{chunk_idx}_VALIDATING - Validating key {candidate_key[:10]}... (attempt {key_attempt+1})")
                            
                            is_valid = self._validate_key_with_retry(candidate_key, needed, force_check=True)
                            tts_logger.info(f"CHUNK_{chunk_idx}_VALIDATION_RESULT - Key {candidate_key[:10]}... valid={is_valid}")
                            
                            if is_valid:
                                api_key = candidate_key
                                # Track key đã dùng cho segment này
                                segment_used_keys.add(api_key)
                                tts_logger.info(f"CHUNK_{chunk_idx} - Found {api_key[:10]}... (attempt {key_attempt+1}, credits priority)")
                                break
                            
                            # Validation failed after retries -> mark invalid (không phải hết credits)
                            tts_logger.warning(f"CHUNK_{chunk_idx}_INVALID - Key {candidate_key[:10]}... invalid after retries, marking invalid")
                            with self.pool_lock:
                                self.pool.mark_invalid(candidate_key, "Validation failed after retries")
                            exclude_keys_chunk.add(candidate_key)
                            if reserved_now:
                                try:
                                    with self._concurrent_lock:
                                        self._concurrent_in_use_keys.discard(candidate_key)
                                except Exception:
                                    pass
                        
                        if not api_key:
                            tts_logger.error(f"CHUNK_{chunk_idx} - No valid key (tried {len(exclude_keys_chunk)} keys)")
                            raise RuntimeError(f"No valid key (tried {len(exclude_keys_chunk)} keys)")
                        
                        tts_logger.info(f"PARA{idx}_CHUNK{chunk_idx}_KEY_FOUND - Using key {api_key[:10]}... for synthesis (unique for segment)")

                        try:
                            # ===== CALL TTS VỚI PROXY TỪ QUEUE (NẾU CÓ) =====
                            if proxy_cfg:
                                # Dùng proxy đã được gán/được lấy từ queue
                                # Truyền proxy_url để _synthesize_file_with_proxy có thể trả về queue
                                out = self._synthesize_file_with_proxy(
                                    api_key,
                                    voice_id,
                                    model_name,
                                    chunk,
                                    voice_settings=voice_settings,
                                    stop_callback=stop_callback,
                                    on_status_update=on_status_update,
                                    proxy_cfg=proxy_cfg,
                                    proxy_url=proxy_url_used,  # Truyền proxy_url để trả về queue
                                )
                            else:
                                # Fallback: dùng synthesize_file bình thường (tự chọn proxy hoặc không dùng proxy)
                                out = self.synthesize_file(
                                    api_key,
                                    voice_id,
                                    model_name,
                                    chunk,
                                    voice_settings=voice_settings,
                                    stop_callback=stop_callback,
                                    on_status_update=on_status_update,
                                )
                            
                            chunk_files.append(out)
                            
                            # CRITICAL: Trả proxy về queue sau khi dùng thành công (giống check_live.py)
                            # Chỉ trả về nếu không phải lỗi 401/unusual_activity (proxy bị block)
                            if proxy_url_used and self.proxy_service and hasattr(self.proxy_service, 'return_proxy_url'):
                                try:
                                    self.proxy_service.return_proxy_url(proxy_url_used)
                                    tts_logger.debug(f"PARA{idx}_CHUNK{chunk_idx}_PROXY_RETURNED - Returned proxy_url='{proxy_url_used}' to queue")
                                except Exception as e:
                                    tts_logger.warning(f"PARA{idx}_CHUNK{chunk_idx}_PROXY_RETURN_ERROR - Failed to return proxy: {e}")
                            
                            tts_logger.info(f"PARA{idx}_CHUNK{chunk_idx}/{len(chunks)} - SUCCESS: {out}")
                            break  # chunk completed successfully
                        except KeyValidationError as key_err:
                            key_switch_attempts += 1
                            failing_key = key_err.api_key or api_key
                            tts_logger.warning(
                                f"PARA{idx}_CHUNK{chunk_idx}_KEY_VALIDATION_FAIL - {failing_key[:10]}... "
                                f"failed during synth (switch #{key_switch_attempts}): {key_err}"
                            )
                            
                            # CRITICAL: Trả proxy về queue (KeyValidationError không phải lỗi proxy)
                            if proxy_url_used and self.proxy_service and hasattr(self.proxy_service, 'return_proxy_url'):
                                try:
                                    self.proxy_service.return_proxy_url(proxy_url_used)
                                    tts_logger.debug(f"PARA{idx}_CHUNK{chunk_idx}_PROXY_RETURNED_ON_KEY_ERROR - Returned proxy_url='{proxy_url_used}' to queue")
                                except Exception as e:
                                    tts_logger.warning(f"PARA{idx}_CHUNK{chunk_idx}_PROXY_RETURN_ERROR - Failed to return proxy: {e}")
                            
                            exclude_keys_chunk.add(failing_key)
                            segment_used_keys.discard(failing_key)  # Remove from segment used keys
                            with self.pool_lock:
                                # KeyValidationError không phải do hết credits -> mark invalid
                                self.pool.mark_invalid(failing_key, f"KeyValidationError: {str(key_err)[:100]}")
                            
                            # Không giới hạn key switches - retry vô hạn
                            # continue while-loop to pick another key
                            continue
                        except Exception as synth_err:
                            # Bất kỳ lỗi nào khác cũng retry với key khác
                            key_switch_attempts += 1
                            failing_key = api_key
                            tts_logger.warning(
                                f"PARA{idx}_CHUNK{chunk_idx}_SYNTH_ERROR - {failing_key[:10]}... "
                                f"failed during synth (switch #{key_switch_attempts}): {synth_err}"
                            )
                            
                            # CRITICAL: Trả proxy về queue ngay cả khi có 401 (vì 401 có thể do key bị block, không phải proxy)
                            # Chỉ KHÔNG trả về nếu proxy bị blocked (unusual_activity)
                            err_str = str(synth_err).lower() if synth_err else ""
                            is_fatal_proxy_error = (
                                "detected_unusual_activity" in err_str or
                                "proxy_blocked" in err_str
                            )
                            if proxy_url_used and not is_fatal_proxy_error and self.proxy_service and hasattr(self.proxy_service, 'return_proxy_url'):
                                try:
                                    self.proxy_service.return_proxy_url(proxy_url_used)
                                    tts_logger.debug(f"PARA{idx}_CHUNK{chunk_idx}_PROXY_RETURNED_ON_ERROR - Returned proxy_url='{proxy_url_used}' to queue (non-fatal error)")
                                except Exception as e:
                                    tts_logger.warning(f"PARA{idx}_CHUNK{chunk_idx}_PROXY_RETURN_ERROR - Failed to return proxy: {e}")
                            
                            exclude_keys_chunk.add(failing_key)
                            segment_used_keys.discard(failing_key)  # Remove from segment used keys
                            # Retry với key khác
                            continue
                        finally:
                            if reserved_now and api_key:
                                try:
                                    with self._concurrent_lock:
                                        self._concurrent_in_use_keys.discard(api_key)
                                except Exception:
                                    pass

                    # Credit deduction is handled inside synthesize_file
                    # Each chunk gets its own request_id for precise tracking

                # ----- Hợp nhất/copy ra file đoạn sau khi TOÀN BỘ chunk ok -----
                paragraph_folder = os.path.join(self.output_dir, "paragraph_tmp")
                os.makedirs(paragraph_folder, exist_ok=True)
                final_para_path = os.path.join(paragraph_folder, f"{idx}.mp3")

                # Concatenate chunks into final paragraph file
                if len(chunk_files) == 1:
                    # Single chunk - just copy
                    shutil.copy2(chunk_files[0], final_para_path)
                    tts_logger.info(f"PARA{idx} - Single chunk copied to {final_para_path}")
                    # Clean up chunk
                    try:
                        os.unlink(chunk_files[0])
                    except Exception:
                        pass
                else:
                    # Multiple chunks - concatenate with ffmpeg
                    tts_logger.info(f"PARA{idx} - Concatenating {len(chunk_files)} chunks into single paragraph")
                    
                    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8') as f:
                        for i, cf in enumerate(chunk_files, 1):
                            f.write(f"file '{os.path.abspath(cf).replace('\\', '/')}'\n")
                            tts_logger.info(f"PARA{idx}_CONCAT - Chunk {i}: {cf}")
                        lst = f.name
                    
                    cmd = ['ffmpeg.exe', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
                        '-i', lst, '-c', 'copy', '-y', final_para_path]
                    
                    pr = subprocess.run(cmd, capture_output=True, text=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    
                    try:
                        os.unlink(lst)
                    except Exception:
                        pass
                    
                    if pr.returncode != 0:
                        # Clean up chunks if FFmpeg fails
                        for cf in chunk_files:
                            try:
                                os.unlink(cf)
                            except Exception:
                                pass
                        raise RuntimeError(f"ffmpeg concat failed: {pr.stderr or 'Unknown'}")
                    
                    # Clean up individual chunk files after successful concat
                    for cf in chunk_files:
                        try:
                            os.unlink(cf)
                        except Exception:
                            pass
                    
                    tts_logger.info(f"PARA{idx} - Successfully concatenated {len(chunk_files)} chunks into {final_para_path}")

                # callback done khi cả đoạn đã OK
                if on_paragraph_done:
                    try:
                        with self.callback_lock:
                            on_paragraph_done(idx, final_para_path)
                    except Exception as e:
                        # Don't fail paragraph if UI callback fails
                        tts_logger.warning(f"PARA{idx} - UI callback done error (ignored): {e}")

                return (idx, final_para_path)

            except Exception as e:
                err_msg = str(e)
                tts_logger.error(f"PARAGRAPH_{idx}_ATTEMPT_{attempt}_ERROR: {err_msg}")

                # dọn mọi file tạm của attempt này (nếu có thư mục paragraph_tmp thì để attempt sau ghi đè)
                try:
                    tmp_folder = os.path.join(self.output_dir, "paragraph_tmp")
                    # không xóa cả thư mục để tránh race, chỉ bỏ file mang tên idx.mp3 nếu có
                    tmp_file = os.path.join(tmp_folder, f"{idx}.mp3")
                    if os.path.exists(tmp_file):
                        os.unlink(tmp_file)
                except Exception:
                    pass

                # RETRY VÔ HẠN - không bao giờ return None
                attempt += 1
                tts_logger.warning(f"PARAGRAPH_{idx}_ATTEMPT_{attempt}_FAILED - Retrying vô hạn... Error: {err_msg}")
                
                # Thử "cứu" trước khi retry:
                # 1) Reload key pool để lấy keys mới
                try:
                    with self.pool_lock:
                        self.pool.load()
                    tts_logger.info(f"PARA{idx}_RETRY - Reloaded key pool")
                except Exception as e2:
                    tts_logger.warning(f"PARA{idx}_RELOAD_ERROR - {e2}")
                
                # 2) Sleep để backend ổn định
                time.sleep(TTSConfig.STAGE_RETRY_WAIT)
                tts_logger.info(f"PARAGRAPH_{idx} retrying attempt {attempt} (vô hạn) ...")
                
                # Clear segment_used_keys để có thể dùng lại keys sau retry
                segment_used_keys.clear()
                continue


    # ---------- run_for_texts: đa luồng với cửa sổ trượt 3 ----------
    def run_for_texts(self, voice_id: str, model_name: str, paragraphs: List[str], mini_dir: str,
                      basename: str, voice_settings: dict | None = None,
                      on_paragraph_start=None, on_paragraph_done=None, on_paragraph_error=None,
                      stop_callback=None, on_status_update=None) -> List[Tuple[str, str]]:
        """
        SEQUENTIAL PROCESSING: Xử lý nối đuôi nhau (không đa luồng) để đảm bảo chắc chắn có request_id.
          - Xử lý từng paragraph một, đợi xong mới chuyển sang paragraph tiếp theo
          - Đảm bảo 100% chính xác mapping request_id với history_item_id
          - Tránh conflict khi nhiều request cùng lúc
          - Nếu có đoạn lỗi → retry ở cuối
        Kết quả được ghi ra folder đích theo thứ tự đoạn (1.mp3, 2.mp3, ...).
        """
        # OPTIMIZATION: Try async processing if enabled
        if TTSConfig.ASYNC_ENABLED:
            try:
                return self._run_for_texts_async(
                    voice_id, model_name, paragraphs, mini_dir, basename,
                    voice_settings, on_paragraph_start, on_paragraph_done,
                    on_paragraph_error, stop_callback, on_status_update
                )
            except Exception as e:
                tts_logger.warning(f"ASYNC_PROCESSING_FAILED - Fallback to threading: {e}")
        
        # Fallback to threading (original implementation)
        return self._run_for_texts_threading(
            voice_id, model_name, paragraphs, mini_dir, basename,
            voice_settings, on_paragraph_start, on_paragraph_done,
            on_paragraph_error, stop_callback, on_status_update
        )
    
    def _run_for_texts_async(self, voice_id: str, model_name: str, paragraphs: List[str], mini_dir: str,
                             basename: str, voice_settings: dict | None = None,
                             on_paragraph_start=None, on_paragraph_done=None, on_paragraph_error=None,
                             stop_callback=None, on_status_update=None) -> List[Tuple[str, str]]:
        """
        Async-based paragraph processing using asyncio.
        
        This is MORE EFFICIENT than threading for I/O-bound tasks:
        - 10x lower memory overhead per task
        - Better scalability (can handle 10+ concurrent tasks vs 3-5 threads)
        - Faster task switching (cooperative vs preemptive)
        """
        try:
            from services.async_tts_processor import run_async_processing
            
            tts_logger.info(f"ASYNC_PROCESSING - Starting with max_workers={self.max_workers}")
            
            # Use async processor with sliding window
            async_results = run_async_processing(
                self._process_single_paragraph,
                paragraphs,
                max_workers=self.max_workers,
                use_sliding_window=True
            )
            
            # Process results (same as threading version)
            outputs: List[Tuple[str, str]] = []
            failed_paragraphs: List[int] = []
            dst_folder = os.path.join(mini_dir, basename)
            os.makedirs(dst_folder, exist_ok=True)
            
            for idx_res, path in async_results:
                if path:
                    final_para = os.path.join(dst_folder, f"{idx_res}.mp3")
                    shutil.copy2(path, final_para)
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
                    outputs.append((f"p{idx_res}", final_para))
                else:
                    failed_paragraphs.append(idx_res)
            
            # DISABLED ASYNC - Retry logic not implemented yet
            # Raise error to force fallback to threading
            if failed_paragraphs:
                raise RuntimeError(f"ASYNC implementation incomplete - {len(failed_paragraphs)} paragraphs failed")
            
        except ImportError as e:
            tts_logger.warning(f"ASYNC module not available, fallback to threading: {e}")
            raise
        except Exception as e:
            tts_logger.error(f"ASYNC processing error: {e}")
            raise
    
    def _run_for_texts_threading(self, voice_id: str, model_name: str, paragraphs: List[str], mini_dir: str,
                                basename: str, voice_settings: dict | None = None,
                                on_paragraph_start=None, on_paragraph_done=None, on_paragraph_error=None,
                                stop_callback=None, on_status_update=None) -> List[Tuple[str, str]]:
        """
        MULTI-THREAD MODEL: Xử lý song song với cửa sổ trượt (default 3, max 5).
        - Mỗi worker dùng API key khác nhau (tránh trùng key giữa các luồng)
        - Kết quả vẫn được ghi theo thứ tự đoạn (1.mp3, 2.mp3, ...)
        """
        outputs: List[Tuple[str, str]] = []
        failed_paragraphs: List[int] = []  # Track failed paragraph indices
        dst_folder = os.path.join(mini_dir, basename)
        os.makedirs(dst_folder, exist_ok=True)
        tts_logger.info(f"FOLDER_CREATED - {dst_folder}")

        total = len(paragraphs)
        if total == 0:
            return outputs

        # Concurrency control: unique key usage across workers
        if not hasattr(self, "_concurrent_in_use_keys"):
            self._concurrent_in_use_keys = set()
        if not hasattr(self, "_concurrent_lock"):
            import threading
            self._concurrent_lock = threading.Lock()

        from concurrent.futures import ThreadPoolExecutor, as_completed

        tts_logger.info(f"MULTI_PROCESSING - Processing {total} paragraphs with max_workers={self.max_workers}")

        # Thread-safe set để track proxies đã dùng trong batch đầu tiên
        import threading
        first_batch_used_proxies = set()
        first_batch_proxy_lock = threading.Lock()

        def task_wrapper(p_idx: int, assigned_proxy: Optional[dict] = None) -> Tuple[int, Optional[str]]:
            """Process a single paragraph with full retry logic."""
            try:
                return self._process_single_paragraph(
                    p_idx, total, paragraphs[p_idx - 1],
                    voice_id, model_name, voice_settings,
                    on_paragraph_start, on_paragraph_done, on_paragraph_error,
                    stop_callback, on_status_update,
                    assigned_proxy=assigned_proxy,  # Pass proxy được assign cho batch đầu tiên
                    first_batch_used_proxies=first_batch_used_proxies if p_idx <= min(2, total) else None,
                    first_batch_proxy_lock=first_batch_proxy_lock if p_idx <= min(2, total) else None
                )
            except Exception as exc:
                tts_logger.error(f"MULTI_TASK_EXCEPTION - Paragraph {p_idx} error: {exc}")
                return (p_idx, None)

        # ===== BATCHED EXECUTION (STRICT QUEUE) =====
        # Chạy theo lô: mỗi lô tối đa self.max_workers đoạn.
        # Ví dụ max_workers=5:
        #   - Batch 1: đoạn 1..5 (kể cả retry) chạy xong hoàn toàn
        #   - Sau khi 1..5 xong mới bắt đầu Batch 2: 6..10, v.v.
        outputs_map: Dict[int, str] = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            batch_size = self.max_workers
            current = 1
            is_first_batch = True  # Track batch đầu tiên để warm-up

            while current <= total:
                if stop_callback and stop_callback():
                    tts_logger.info("MULTI_STOP - Stop requested before batch submission")
                    break

                # Giảm concurrency cho batch đầu tiên để tránh abuse detection
                # Batch đầu: chỉ submit 1-2 paragraphs để "warm up" proxy
                # Các batch sau: dùng full max_workers
                if is_first_batch:
                    # Batch đầu tiên: chỉ submit 2 paragraphs đầu tiên để warm-up
                    first_batch_size = min(2, total)
                    batch_end = min(total, current + first_batch_size - 1)
                    tts_logger.info(f"FIRST_BATCH_WARMUP - Reducing concurrency to {first_batch_size} paragraphs for warm-up")
                else:
                    batch_end = min(total, current + batch_size - 1)
                
                batch_indices = list(range(current, batch_end + 1))
                tts_logger.info(f"MULTI_BATCH - Submitting paragraphs {batch_indices[0]}..{batch_indices[-1]} (batch size={len(batch_indices)})")

                # BATCH ĐẦU TIÊN: Pre-assign proxies để đảm bảo mỗi segment dùng proxy khác nhau
                # Mỗi paragraph trong batch đầu tiên sẽ được assign 1 proxy riêng
                # Proxy này sẽ được dùng cho tất cả chunks trong paragraph đó
                assigned_proxies = {}
                used_proxy_keys = set()  # Track proxy keys đã assign để tránh trùng lặp
                if is_first_batch and self.proxy_service and hasattr(self.proxy_service, 'get_next_proxy_url'):
                    tts_logger.info(f"FIRST_BATCH_PROXY_ASSIGNMENT - Pre-assigning proxies from queue for {len(batch_indices)} paragraphs...")
                    for idx in batch_indices:
                        proxy_url = self.proxy_service.get_next_proxy_url()
                        if not proxy_url:
                            tts_logger.warning(
                                f"FIRST_BATCH_PROXY_FAIL - No proxy_url available for paragraph {idx}, "
                                f"will fall back to queue per chunk"
                            )
                            continue
                        parsed_cfg = None
                        if hasattr(self.proxy_service, 'parse_proxy_url_to_cfg'):
                            parsed_cfg = self.proxy_service.parse_proxy_url_to_cfg(proxy_url)
                        if not parsed_cfg:
                            tts_logger.warning(
                                f"FIRST_BATCH_PROXY_PARSE_FAIL - Could not parse proxy_url='{proxy_url}' "
                                f"for paragraph {idx}"
                            )
                            continue
                        proxy_cfg = parsed_cfg
                        proxy_cfg.setdefault("token_proxy", "canada")
                        proxy_key = f"{proxy_cfg.get('host')}:{proxy_cfg.get('port')}"
                        assigned_proxies[idx] = proxy_cfg
                        used_proxy_keys.add(proxy_key)
                        tts_logger.info(
                            f"FIRST_BATCH_PROXY_ASSIGNED - Paragraph {idx} → {proxy_key} "
                            f"(type: {proxy_cfg.get('token_proxy')}) from proxy_url='{proxy_url}'"
                        )

                # WARM-UP DELAY cho batch đầu tiên để tránh abuse detection
                # Khi restart app, nhiều requests đồng thời từ cùng proxy → trigger abuse detector
                # Delay này giúp proxy "warm up" và tránh burst requests
                if is_first_batch:
                    warmup_delay = 3.0  # 3s warm-up cho batch đầu tiên
                    tts_logger.info(f"WARMUP_DELAY - Waiting {warmup_delay}s before first batch to avoid abuse detection...")
                    try:
                        import time as _warmup_time
                        _warmup_time.sleep(warmup_delay)
                    except Exception:
                        pass
                    is_first_batch = False

                in_flight = {}
                # Submit toàn bộ batch với proxy đã assign (nếu có)
                for idx in batch_indices:
                    if stop_callback and stop_callback():
                        tts_logger.info("MULTI_STOP - Stop requested during batch submission")
                        break
                    assigned_proxy = assigned_proxies.get(idx)  # None nếu không phải batch đầu tiên
                    fut = executor.submit(task_wrapper, idx, assigned_proxy)
                    in_flight[fut] = idx

                    # Stagger start times giữa các đoạn trong cùng batch 2–3s
                    # Batch đầu tiên: tăng stagger delay lên 3-5s để tránh burst
                    try:
                        import random, time as _stagger_time
                        if current == 1:  # Batch đầu tiên
                            delay_s = random.uniform(3.0, 5.0)  # Delay dài hơn cho batch đầu
                        else:
                            delay_s = random.uniform(2.0, 3.0)  # Delay bình thường
                        tts_logger.info(f"STAGGER_START - Waiting {delay_s:.2f}s before launching next prompt")
                        _stagger_time.sleep(delay_s)
                    except Exception:
                        pass

                # Chờ cả batch hoàn thành rồi mới chuyển sang batch tiếp theo
                batch_success = []  # các đoạn trong batch này có mp3 thành công
                for fut in as_completed(list(in_flight.keys()), timeout=None):
                    idx_res, path = fut.result()
                    in_flight.pop(fut, None)

                    if path:
                        final_para = os.path.join(dst_folder, f"{idx_res}.mp3")
                        try:
                            shutil.copy2(path, final_para)
                            try:
                                os.unlink(path)
                            except Exception:
                                pass
                            outputs_map[idx_res] = final_para
                            outputs.append((f"p{idx_res}", final_para))
                            tts_logger.info(f"MULTI_SUCCESS - Paragraph {idx_res}/{total} completed")
                            batch_success.append(idx_res)
                        except Exception as e:
                            tts_logger.error(f"MULTI_COPY_ERROR - Paragraph {idx_res}: {e}")
                            failed_paragraphs.append(idx_res)
                    else:
                        failed_paragraphs.append(idx_res)

                # Nếu batch hiện tại KHÔNG có đoạn nào success (không có file mp3),
                # thì dừng, KHÔNG chạy nối đuôi batch tiếp theo (6,7,8,...).
                if not batch_success:
                    tts_logger.error(
                        f"MULTI_BATCH_ABORT - No successful paragraphs in batch {batch_indices[0]}..{batch_indices[-1]},"
                        " stopping further batches to avoid wasting keys/proxy."
                    )
                    break

                # Chuyển sang batch tiếp theo (chỉ khi batch hiện tại có ít nhất 1 success)
                current = batch_end + 1

        # ===== RETRY FAILED PARAGRAPHS (AGGRESSIVE) =====
        if failed_paragraphs:
            failed_count = len(failed_paragraphs)
            tts_logger.warning(f"RETRY_PHASE - {failed_count} paragraphs failed, starting AGGRESSIVE retry...")
            
            # Notify user about retry phase
            if on_paragraph_error:
                try:
                    with self.callback_lock:
                        on_paragraph_error(-1, f"Retrying {failed_count} failed paragraphs...")
                except Exception as e:
                    tts_logger.warning(f"RETRY_PHASE - UI callback error (ignored): {e}")
            
            # CRITICAL: Reload key pool from database before retry
            tts_logger.info(f"RETRY_PHASE - Reloading key pool from database...")
            with self.pool_lock:
                self.pool.load()
            
            # Retry failed paragraphs with up to 2 attempts per paragraph
            retry_success_count = 0
            still_failing = list(failed_paragraphs)  # Copy list
            
            for retry_round in range(1, 3):  # 2 rounds of retries
                if not still_failing:
                    break
                    
                tts_logger.info(f"RETRY_ROUND_{retry_round} - {len(still_failing)} paragraphs to retry")
                round_success = []
                
                for idx in still_failing:
                    if stop_callback and stop_callback():
                        tts_logger.info(f"RETRY_ROUND_{retry_round} - Stop requested")
                        break
                        
                    tts_logger.info(f"RETRY_ROUND_{retry_round} - Paragraph {idx}/{total}")
                    
                    try:
                        # Process single paragraph with fresh retry
                        idx_res, path = self._process_single_paragraph(
                            idx, total, paragraphs[idx-1],
                            voice_id, model_name, voice_settings,
                            on_paragraph_start, on_paragraph_done, on_paragraph_error,
                            stop_callback, on_status_update
                        )
                        
                        if path:
                            # Success! Copy to destination
                            final_para = os.path.join(dst_folder, f"{idx_res}.mp3")
                            shutil.copy2(path, final_para)
                            try:
                                os.unlink(path)
                            except Exception:
                                pass
                            outputs.append((f"p{idx_res}", final_para))
                            round_success.append(idx)
                            retry_success_count += 1
                            tts_logger.info(f"RETRY_ROUND_{retry_round} - Paragraph {idx} SUCCESS ✅")
                        else:
                            tts_logger.warning(f"RETRY_ROUND_{retry_round} - Paragraph {idx} still failing")
                            
                    except Exception as e:
                        tts_logger.error(f"RETRY_ROUND_{retry_round} - Paragraph {idx} exception: {e}")
                
                # Remove successful paragraphs from retry list
                for idx in round_success:
                    still_failing.remove(idx)
                
                # If still have failures and more rounds to go, reload pool and wait
                if still_failing and retry_round < 2:
                    tts_logger.info(f"RETRY_ROUND_{retry_round} - Waiting 3s before next round...")
                    time.sleep(3)
                    with self.pool_lock:
                        self.pool.load()
            
            # Final retry summary
            if still_failing:
                error_details = f"RETRY_FINAL - {len(still_failing)} paragraphs PERMANENTLY FAILED: {still_failing}"
                tts_logger.error(error_details)
                tts_logger.error(f"❌ CRITICAL - These segments will be MISSING from final output!")
                tts_logger.error(f"❌ File will NOT be created due to missing segments: {still_failing}")
                
                if on_paragraph_error:
                    try:
                        with self.callback_lock:
                            on_paragraph_error(-2, f"❌ {len(still_failing)} paragraphs failed: {still_failing}")
                    except Exception as e:
                        tts_logger.warning(f"RETRY_FINAL - UI callback error (ignored): {e}")
                
                # CRITICAL: Raise error to prevent concat with missing segments
                raise RuntimeError(f"MISSING_PARAGRAPHS - {len(still_failing)} segments failed: {still_failing}")
            else:
                tts_logger.info(f"RETRY_FINAL - ✅ ALL {failed_count} paragraphs recovered successfully!")
                if on_paragraph_done:
                    try:
                        with self.callback_lock:
                            on_paragraph_done(-1, f"✅ All {failed_count} paragraphs recovered")
                    except Exception as e:
                        tts_logger.warning(f"RETRY_FINAL - UI callback error (ignored): {e}")

        # sau khi xong toàn bộ đoạn của file hiện tại, trả outputs
        return outputs

    # ---------- concat utility ----------
    def concat_pieces(self, ffmpeg_bin: str, folder: str, output_path: str, advanced_settings: dict = None) -> None:
        """
        Concatenate MP3 files with STRICT validation to prevent missing segments.
        Enhanced to support:
        - pause between segments based on advanced settings
        - batched concatenation when there are many parts (to avoid ffmpeg / OS limits)

        Args:
            ffmpeg_bin: Path to ffmpeg executable
            folder: Folder containing mp3 files to concatenate (các đoạn của 1 file txt)
            output_path: Output file path (file tổng cho txt đó)
            advanced_settings: Dictionary with pause settings:
                - pause_between_segments_enabled: bool
                - segment_gap_seconds: float (pause duration)
                - segment_count: int (number of segments per group)
        """
        import os
        import tempfile as temp
        import shutil

        if not os.path.exists(folder):
            raise RuntimeError(f"Folder not found: {folder}")

        # Lấy danh sách mp3 con: 1.mp3, 2.mp3, ..., N.mp3
        # Chỉ lấy các file có tên là số (bỏ qua output_merged.mp3, etc.)
        def is_numeric_filename(filename):
            try:
                int(os.path.splitext(filename)[0])
                return True
            except ValueError:
                return False
        
        files = sorted(
            [f for f in os.listdir(folder) if f.lower().endswith(".mp3") and is_numeric_filename(f)],
            key=lambda x: int(os.path.splitext(x)[0]),
        )

        if not files:
            raise RuntimeError("No mp3 parts to concat")

        # --- VALIDATION: đủ file, không bị rỗng / hỏng ---
        expected_indices = set(range(1, len(files) + 1))
        actual_indices = set()

        for name in files:
            try:
                idx = int(os.path.splitext(name)[0])
                actual_indices.add(idx)
            except ValueError:
                tts_logger.warning(f"CONCAT_SKIP_INVALID_FILENAME - {name}")

        missing_indices = expected_indices - actual_indices
        if missing_indices:
            missing_list = sorted(missing_indices)
            error_msg = (
                f"MISSING_MP3_SEGMENTS - Expected {len(files)} files (1-{len(files)}), "
                f"but missing {len(missing_list)} segments: {missing_list}. "
                f"Found files: {sorted(actual_indices)}"
            )
            tts_logger.error(error_msg)
            raise RuntimeError(error_msg)

        invalid_files = []
        for name in files:
            full_path = os.path.join(folder, name)
            if os.path.getsize(full_path) < 100:  # MP3 hợp lệ phải đủ lớn
                invalid_files.append(name)

        if invalid_files:
            error_msg = f"CORRUPT_MP3_SEGMENTS - {len(invalid_files)} files too small: {invalid_files}"
            tts_logger.error(error_msg)
            raise RuntimeError(error_msg)

        total_segments = len(files)
        tts_logger.info(f"CONCAT_VALIDATION_OK - {total_segments} segments ready to concat")

        # --- CHIẾN LƯỢC GHÉP: nhỏ thì ghép 1 lần, lớn thì ghép theo batch ---
        # Ngưỡng an toàn cho 1 lần ffmpeg concat (tùy bạn chỉnh 50/80/100)
        MAX_FILES_PER_FFMPEG_CONCAT = 80

        # 1) Trường hợp ít file: dùng logic cũ, không cần batch
        if total_segments <= MAX_FILES_PER_FFMPEG_CONCAT:
            if (
                advanced_settings
                and advanced_settings.get("pause_between_segments_enabled", False)
                and total_segments > 1
            ):
                segment_gap_seconds = advanced_settings.get("segment_gap_seconds", 1.3)
                segment_count = advanced_settings.get("segment_count", 2)

                tts_logger.info(
                    f"CONCAT_PAUSE_ENABLED - Gap: {segment_gap_seconds}s, "
                    f"Group size: {segment_count}"
                )
                self._concat_with_pauses(
                    ffmpeg_bin,
                    folder,
                    files,
                    output_path,
                    segment_gap_seconds,
                    segment_count,
                )
            else:
                self._concat_simple(ffmpeg_bin, folder, files, output_path)
            return

        # 2) Trường hợp nhiều file: ghép theo batch nhỏ rồi ghép các batch
        tts_logger.info(
            f"CONCAT_BATCH_MODE - {total_segments} segments, "
            f"using batches of {MAX_FILES_PER_FFMPEG_CONCAT}"
        )

        temp_dir = temp.mkdtemp(prefix="tts_concat_batch_")
        batch_files = []

        try:
            # 2.1) Ghép từng batch nhỏ thành file mp3 trung gian (copy mode)
            for batch_index, start in enumerate(
                range(0, total_segments, MAX_FILES_PER_FFMPEG_CONCAT), start=1
            ):
                batch_segment_names = files[start : start + MAX_FILES_PER_FFMPEG_CONCAT]
                batch_output_name = f"batch_{batch_index:03d}.mp3"
                batch_output_path = os.path.join(temp_dir, batch_output_name)

                tts_logger.info(
                    f"CONCAT_BATCH_PART - Batch {batch_index}: "
                    f"segments {start + 1}-{start + len(batch_segment_names)} "
                    f"-> {batch_output_name}"
                )

                # Trong từng batch, vẫn tôn trọng cài đặt pause nếu có
                if (
                    advanced_settings
                    and advanced_settings.get("pause_between_segments_enabled", False)
                    and len(batch_segment_names) > 1
                ):
                    segment_gap_seconds = advanced_settings.get("segment_gap_seconds", 1.3)
                    segment_count = advanced_settings.get("segment_count", 2)

                    self._concat_with_pauses(
                        ffmpeg_bin,
                        folder,
                        batch_segment_names,
                        batch_output_path,
                        segment_gap_seconds,
                        segment_count,
                    )
                else:
                    self._concat_simple(
                        ffmpeg_bin,
                        folder,
                        batch_segment_names,
                        batch_output_path,
                    )

                if not os.path.exists(batch_output_path) or os.path.getsize(batch_output_path) < 100:
                    raise RuntimeError(
                        f"CONCAT_BATCH_ERROR - Intermediate batch file invalid: {batch_output_path}"
                    )
                batch_files.append(batch_output_name)

            # 2.2) Ghép các batch mp3 trung gian lại thành output cuối (copy mode, không thêm pause lần nữa)
            tts_logger.info(
                f"CONCAT_BATCH_FINAL - Merging {len(batch_files)} batch files into final output {output_path}"
            )
            self._concat_simple(ffmpeg_bin, temp_dir, batch_files, output_path)

        finally:
            # 2.3) Xoá folder tạm chứa batch
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                tts_logger.warning(f"CONCAT_BATCH_CLEANUP_ERROR - {e}")
    def _get_or_create_silent_file(self, ffmpeg_bin: str, duration_seconds: float) -> str:
        """
        Get or create a cached silent audio file for the specified duration.
        Returns the file path to the silent audio file.
        """
        # Round duration to avoid too many cache entries (cache with 0.1s precision)
        cache_key = round(duration_seconds, 1)
        
        # Check cache first
        if cache_key in self._silent_file_cache:
            cached_file = self._silent_file_cache[cache_key]
            if os.path.exists(cached_file):
                tts_logger.info(f"SILENT_CACHE_HIT - Using cached silent file for {duration_seconds}s")
                return cached_file
            else:
                # Remove invalid cache entry
                del self._silent_file_cache[cache_key]
        
        # Create new silent file
        import tempfile as temp
        temp_silent = temp.NamedTemporaryFile(delete=False, suffix='.mp3')
        silent_file = temp_silent.name
        temp_silent.close()
        
        tts_logger.info(f"SILENT_CREATE - Creating new silent file for {duration_seconds}s")
        
        # Generate silent audio file using ffmpeg
        # Using 44.1kHz, mono, 128k bitrate to match typical audio files
        silent_cmd = [
            ffmpeg_bin, '-hide_banner', '-loglevel', 'warning',
            '-f', 'lavfi', '-i', f'anullsrc=channel_layout=mono:sample_rate=44100',
            '-t', str(duration_seconds), '-c:a', 'libmp3lame', '-b:a', '128k', '-y', silent_file
        ]
        
        silent_result = subprocess.run(silent_cmd, capture_output=True, text=True,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        
        if silent_result.returncode != 0:
            raise RuntimeError(f"Failed to create silent file: {silent_result.stderr}")
        
        if not os.path.exists(silent_file) or os.path.getsize(silent_file) < 100:
            raise RuntimeError("Created silent file is invalid or too small")
        
        # Cache the file
        self._silent_file_cache[cache_key] = silent_file
        tts_logger.info(f"SILENT_CACHE_STORE - Cached silent file for {duration_seconds}s")
        
        return silent_file

    def _concat_with_pauses(self, ffmpeg_bin: str, folder: str, files: list, output_path: str, 
                           segment_gap_seconds: float, segment_count: int) -> None:
        """
        Concatenate files with pauses every 'segment_count' files.
        Example: If 4 files and segment_count=2, pause pattern is: [file1, pause, file2, pause, file3, pause, file4]
        Uses cached silent files for efficiency.
        """
        import tempfile as temp
        
        try:
            silent_file = self._get_or_create_silent_file(ffmpeg_bin, segment_gap_seconds)
            sequence: list[str] = []
            for i, name in enumerate(files, 1):
                sequence.append(os.path.join(folder, name))
                if i % segment_count == 0 and i < len(files):
                    sequence.append(silent_file)
                    tts_logger.info(f"CONCAT_PAUSE_INSERTED - After segment {i}")
            
            tts_logger.info(f"CONCAT_WITH_PAUSES - Total sequence length including pauses: {len(sequence)}")
            self._concat_paths_with_batching(ffmpeg_bin, sequence, output_path)
            tts_logger.info(f"CONCAT_PAUSES_SUCCESS - Successfully concatenated {len(files)} files with pauses")
        except Exception as e:
            tts_logger.warning(f"CONCAT_PAUSES_ERROR - {e}, falling back to simple concat")
            self._concat_simple(ffmpeg_bin, folder, files, output_path)
    
    def _concat_simple(self, ffmpeg_bin: str, folder: str, files: list, output_path: str) -> None:
        """Original simple concatenation without pauses (with batching for large inputs)"""
        paths = [os.path.join(folder, name) for name in files]
        self._concat_paths_with_batching(ffmpeg_bin, paths, output_path)
    
    def _concat_paths_with_batching(self, ffmpeg_bin: str, paths: list[str], output_path: str) -> None:
        """
        Concatenate a list of file paths using ffmpeg while batching inputs to avoid
        command-line limits on Windows. Works recursively by combining chunks until
        a single output file remains.
        """
        if not paths:
            raise RuntimeError("No input files provided for concatenation")
        
        batch_size = max(2, getattr(TTSConfig, 'CONCAT_BATCH_SIZE', 80))
        abs_paths = [os.path.abspath(p) for p in paths]
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        
        def run_concat(input_paths: list[str], target: str) -> None:
            if len(input_paths) == 1:
                source = input_paths[0]
                if os.path.abspath(source) == os.path.abspath(target):
                    return
                shutil.copy2(source, target)
                return
            
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8') as f:
                for path in input_paths:
                    # Normalize path for ffmpeg concat list:
                    # - use forward slashes and absolute paths
                    # - escape single quotes by doubling them
                    abs_path = os.path.abspath(path)
                    normalized = abs_path.replace('\\', '/')
                    # For ffmpeg concat, we need to escape single quotes by replacing ' with ''
                    escaped = normalized.replace("'", "''")
                    line = f"file '{escaped}'\n"
                    f.write(line)
                    tts_logger.debug(f"CONCAT_LIST_ENTRY - {line.strip()}")
                list_path = f.name
                tts_logger.debug(f"CONCAT_LIST_FILE - {list_path}")
            
            cmd = [
                ffmpeg_bin, '-hide_banner', '-loglevel', 'warning', '-f', 'concat', '-safe', '0',
                '-i', list_path, '-c', 'copy', '-y', target
            ]
            tts_logger.debug(f"CONCAT_CMD - {' '.join(cmd)}")
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            try:
                os.unlink(list_path)
            except Exception:
                pass
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {result.stderr or 'Unknown'}")
        
        if len(abs_paths) <= batch_size:
            run_concat(abs_paths, output_path)
            return
        
        tts_logger.info(f"CONCAT_BATCHING - Splitting {len(abs_paths)} inputs into batches of {batch_size}")
        temp_outputs: list[str] = []
        try:
            for start in range(0, len(abs_paths), batch_size):
                chunk = abs_paths[start:start + batch_size]
                fd, temp_path = tempfile.mkstemp(suffix='.mp3')
                os.close(fd)
                temp_outputs.append(temp_path)
                run_concat(chunk, temp_path)
                tts_logger.debug(f"CONCAT_BATCH_CREATED - {temp_path} from {len(chunk)} files")
            
            self._concat_paths_with_batching(ffmpeg_bin, temp_outputs, output_path)
        finally:
            for temp_path in temp_outputs:
                if os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                        tts_logger.debug(f"CONCAT_TEMP_CLEANUP - Removed {temp_path}")
                    except Exception as cleanup_err:
                        tts_logger.warning(f"CONCAT_TEMP_CLEANUP_ERROR - {cleanup_err}")

    # ---------- cleanup ----------
    def finalize(self) -> None:
        self.pool.flush_to_db()
        
        # Cleanup cached silent files
        try:
            for cache_key, silent_file_path in self._silent_file_cache.items():
                if os.path.exists(silent_file_path):
                    try:
                        os.unlink(silent_file_path)
                        tts_logger.info(f"SILENT_CACHE_CLEANUP - Removed cached file for {cache_key}s")
                    except Exception as e:
                        tts_logger.warning(f"SILENT_CACHE_CLEANUP_ERROR - {e}")
            self._silent_file_cache.clear()
        except Exception as e:
            tts_logger.error(f"SILENT_CACHE_CLEANUP_ERROR {e}")
        
        # Cleanup output directory
        try:
            if os.path.exists(self.output_dir):
                for f in os.listdir(self.output_dir):
                    fp = os.path.join(self.output_dir, f)
                    try:
                        os.unlink(fp)
                    except Exception:
                        pass
        except Exception as e:
            tts_logger.error(f"CLEANUP_ERROR {e}")
            
    # trong class TTSTaskRunner

    def _rotate_and_wait_for_new_ip(self, old_ip: str, wait_timeout_sec: int = 90, poll_every_sec: int = 5) -> str | None:
        """
        Gọi proxy_service.rotate_proxy_via_php() rồi poll ipify qua proxy
        cho đến khi IP != old_ip hoặc hết thời gian chờ.
        Trả về new_ip (khác old_ip) nếu OK, None nếu không đổi.
        """
        if not self.proxy_service:
            return None

        # 1) Gọi rotate link
        try:
            rotate_res = self.proxy_service.rotate_proxy_via_php()
            tts_logger.info(f"ROTATE_PHP result: {rotate_res[:160]}")
        except Exception as e:
            tts_logger.error(f"ROTATE_PHP error: {e}")
            return None

        # 2) Poll ip mới
        import time
        deadline = time.time() + max(5, int(wait_timeout_sec))
        last_ip = None
        while time.time() < deadline:
            new_ip = self.proxy_service.probe_ip_via_proxy()
            if new_ip:
                tts_logger.info(f"ROTATE polling-ip via proxy: {new_ip}")
                last_ip = new_ip
                if old_ip and new_ip != old_ip:
                    return new_ip
            time.sleep(max(1, int(poll_every_sec)))
        # Nếu không lấy được hoặc không đổi
        return None