# services/tts_canada_simple.py
"""
SIMPLIFIED TTS Service - BỎ LOGIC 2 PHASE.

Chỉ còn 1 PHASE duy nhất:
- POST request to /v1/text-to-speech/{voice_id}
- Stream download audio
- LUÔN LUÔN dùng proxy (unlimited bandwidth)

Migrated từ 11Labs0811.py tts_direct() logic.
"""
from __future__ import annotations

import os
import json
import time
import logging
from typing import Optional, Dict, Tuple

import requests
from requests.exceptions import RequestException

logger = logging.getLogger('tts_service')


def tts_direct_with_proxy(
    api_key: str,
    voice_id: str,
    text: str,
    model_id: str,
    voice_settings: dict,
    output_path: str,
    proxy_cfg: Optional[Dict] = None,
    output_format: str = "mp3_44100_128",
    language_code: str = None,
    timeout: int = 180
) -> Tuple[bool, str]:
    """
    TTS Direct - 1 PHASE DUY NHẤT với proxy.
    
    Migrated từ 11Labs0811.py ElevenClient.tts_direct()
    
    Args:
        api_key: ElevenLabs API key
        voice_id: Voice ID
        text: Text to synthesize
        model_id: Model ID (e.g., "eleven_turbo_v2_5")
        voice_settings: Voice settings dict
        output_path: Output MP3 file path
        proxy_cfg: Proxy config {host, port, username, password}
        output_format: Audio format (default: mp3_44100_128)
        language_code: Optional language code (vi, en, etc.)
        timeout: Request timeout in seconds
    
    Returns:
        Tuple[success: bool, error_message: str]
    """
    # Build payload
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }
    
    # Add language code if specified
    if language_code:
        payload["language_code"] = language_code
        logger.info(f"TTS_DIRECT - Language: {language_code.upper()}")
    
    # Build URL
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={output_format}"
    
    # Build headers
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"
    }
    
    # Build proxy dict for requests
    proxies = None
    if proxy_cfg:
        host = proxy_cfg.get("host")
        port = proxy_cfg.get("port")
        user = proxy_cfg.get("username")
        pwd = proxy_cfg.get("password")
        
        if host and port:
            if user:
                proxy_url = f"http://{user}:{pwd}@{host}:{port}"
            else:
                proxy_url = f"http://{host}:{port}"
            
            proxies = {"http": proxy_url, "https": proxy_url}
            
            # Log proxy (mask password)
            masked = f"{host}:{port}"
            if user:
                masked = f"***:***@{host}:{port}"
            logger.info(f"TTS_DIRECT - Using proxy: {masked}")
    else:
        logger.warning(f"TTS_DIRECT - No proxy config, using DIRECT (not recommended)")
    
    # Track bytes
    payload_json = json.dumps(payload)
    sent_bytes = len(payload_json.encode('utf-8'))
    
    try:
        # Make request
        t0 = time.time()
        logger.info(f"TTS_DIRECT - POST {url} (timeout={timeout}s)...")
        
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            stream=True  # Stream for large audio files
        )
        
        dt = int((time.time() - t0) * 1000)
        logger.info(f"TTS_DIRECT - Response: {resp.status_code} ({dt}ms)")
        
        # Handle errors
        if resp.status_code != 200:
            error_text = resp.text[:500] if resp.text else ""
            error_msg = f"HTTP {resp.status_code}: {error_text}"
            logger.error(f"TTS_DIRECT - Error: {error_msg}")
            
            # Parse specific errors
            if resp.status_code == 401:
                return False, "API_UNAUTHORIZED: Invalid or expired API key"
            elif resp.status_code == 429:
                return False, "API_RATE_LIMIT: Too many requests"
            elif resp.status_code == 400:
                # Check for specific 400 errors
                try:
                    err_json = resp.json()
                    detail = err_json.get("detail", {})
                    if isinstance(detail, dict):
                        status = detail.get("status", "")
                        # 🔧 FIX: Xử lý tất cả voice limit errors
                        if "voice_limit_reached" in status.lower():
                            return False, "API_VOICE_LIMIT_REACHED: Custom voice limit reached (3/3)"
                        elif "voice_add_edit_limit_reached" in status.lower():
                            return False, "API_VOICE_ADD_EDIT_LIMIT: Monthly voice add/edit limit reached"
                        elif "quota_exceeded" in status.lower() or "quota" in str(err_json).lower():
                            return False, "API_QUOTA_EXCEEDED: Insufficient credits"
                    # 🔧 FIX: Check trong toàn bộ error text
                    err_str = str(err_json).lower()
                    if "voice_limit_reached" in err_str:
                        return False, "API_VOICE_LIMIT_REACHED: Custom voice limit reached"
                    elif "voice_add_edit_limit" in err_str:
                        return False, "API_VOICE_ADD_EDIT_LIMIT: Monthly voice add/edit limit reached"
                except:
                    pass
                return False, f"API_BAD_REQUEST: {error_text[:200]}"
            else:
                return False, error_msg
        
        # Download audio to file
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        tmp = output_path + ".part"
        recv_bytes = 0
        
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    recv_bytes += len(chunk)
        
        # Move to final path
        os.replace(tmp, output_path)
        
        logger.info(f"TTS_DIRECT - ✅ Success: {recv_bytes/1024:.1f}KB downloaded → {output_path}")
        logger.info(f"TTS_DIRECT - Sent: {sent_bytes/1024:.1f}KB | Recv: {recv_bytes/1024:.1f}KB")
        
        return True, f"Success: {recv_bytes} bytes"
    
    except requests.Timeout as e:
        error_msg = f"TIMEOUT: Request timed out after {timeout}s - {e}"
        logger.error(f"TTS_DIRECT - {error_msg}")
        return False, error_msg
    
    except requests.ConnectionError as e:
        error_msg = f"CONNECTION_ERROR: Failed to connect - {e}"
        logger.error(f"TTS_DIRECT - {error_msg}")
        return False, error_msg
    
    except RequestException as e:
        error_msg = f"REQUEST_ERROR: {e}"
        logger.error(f"TTS_DIRECT - {error_msg}")
        return False, error_msg
    
    except Exception as e:
        error_msg = f"UNEXPECTED_ERROR: {e}"
        logger.error(f"TTS_DIRECT - {error_msg}")
        return False, error_msg


def tts_synthesize_simple(
    api_key: str,
    voice_id: str,
    text: str,
    model_id: str,
    voice_settings: dict,
    output_path: str,
    proxy_service,  # ProxyService instance
    output_format: str = "mp3_44100_128",
    language_code: str = None,
    max_retries: int = 10  # 🔧 Tăng lên 10 vì có nhiều proxy key
) -> Tuple[bool, str]:
    """
    Simplified TTS synthesis với auto proxy rotation on failure.
    
    🔧 FIX: Khi TIMEOUT/CONNECTION_ERROR → đổi proxy key ngay lập tức, không chờ.
    Có nhiều proxy key nên cứ xoay cho đến khi thành công.
    
    Args:
        api_key: ElevenLabs API key
        voice_id: Voice ID
        text: Text to synthesize
        model_id: Model ID
        voice_settings: Voice settings
        output_path: Output file path
        proxy_service: ProxyService instance (simple version)
        output_format: Audio format
        language_code: Optional language code
        max_retries: Max retry attempts (default 10 vì có nhiều proxy)
    
    Returns:
        Tuple[success: bool, message: str]
    """
    last_error = ""
    
    for attempt in range(max_retries):
        # Get current proxy
        proxy_url = None
        proxy_cfg = None
        
        if proxy_service:
            proxy_url = proxy_service.get_current_proxy()
            if proxy_url:
                # Parse to config dict
                from urllib.parse import urlparse
                try:
                    parsed = urlparse(proxy_url)
                    proxy_cfg = {
                        "host": parsed.hostname or "",
                        "port": str(parsed.port) if parsed.port else "",
                        "username": parsed.username or "",
                        "password": parsed.password or ""
                    }
                    logger.info(f"TTS_SIMPLE - Attempt {attempt + 1}/{max_retries} using proxy: {parsed.hostname}:{parsed.port}")
                except:
                    proxy_cfg = None
        
        # Call TTS với timeout ngắn hơn (60s) để fail fast và đổi proxy nhanh
        success, error = tts_direct_with_proxy(
            api_key=api_key,
            voice_id=voice_id,
            text=text,
            model_id=model_id,
            voice_settings=voice_settings,
            output_path=output_path,
            proxy_cfg=proxy_cfg,
            output_format=output_format,
            language_code=language_code,
            timeout=60  # 🔧 Giảm từ 180s xuống 60s để fail fast
        )
        
        if success:
            # 🔧 Report success để proxy service biết proxy này OK
            if proxy_service:
                proxy_service.report_success()
            logger.info(f"TTS_SIMPLE - ✅ Success on attempt {attempt + 1}/{max_retries}")
            return True, error
        
        # Failed - log and rotate proxy immediately
        last_error = error
        logger.warning(f"TTS_SIMPLE - Attempt {attempt + 1}/{max_retries} failed: {error}")
        
        # 🔧 FIX: TIMEOUT/CONNECTION → Đổi proxy NGAY LẬP TỨC, không chờ
        if "TIMEOUT" in error or "CONNECTION" in error or "timed out" in error.lower():
            if proxy_service:
                logger.info(f"TTS_SIMPLE - 🔄 TIMEOUT/CONNECTION → Đổi proxy ngay!")
                proxy_service.report_failure(is_rate_limited=False)
                # Không chờ, retry ngay với proxy mới
            continue  # Retry ngay lập tức
        
        # 429 Rate limit → Đổi proxy ngay
        elif "429" in error or "RATE_LIMIT" in error:
            if proxy_service:
                logger.info(f"TTS_SIMPLE - 🔄 429 Rate Limit → Đổi proxy ngay!")
                proxy_service.report_failure(is_rate_limited=True)
            continue  # Retry ngay lập tức
        
        # API key error - don't retry with same key
        # 🔧 FIX: Thêm voice_limit và voice_add_edit_limit - đây là lỗi API KEY, không phải proxy
        elif "401" in error or "UNAUTHORIZED" in error or "QUOTA_EXCEEDED" in error:
            logger.error(f"TTS_SIMPLE - API key error, stopping retries: {error}")
            break
        
        # 🔧 NEW: Voice limit errors - cần đổi API KEY (return để caller xử lý)
        elif "voice_limit" in error.lower() or "voice_add_edit_limit" in error.lower():
            logger.error(f"TTS_SIMPLE - Voice limit error (cần đổi API KEY): {error}")
            # Return với error message đặc biệt để caller biết cần đổi API key
            return False, f"API_KEY_VOICE_LIMIT: {error}"
        
        # Other errors - đổi proxy và retry
        else:
            if proxy_service:
                logger.info(f"TTS_SIMPLE - 🔄 Other error → Đổi proxy!")
                proxy_service.report_failure(is_rate_limited=False)
            continue
    
    # All retries failed
    logger.error(f"TTS_SIMPLE - ❌ All {max_retries} attempts failed: {last_error}")
    return False, last_error
