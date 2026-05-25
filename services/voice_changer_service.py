"""
Voice Changer Service - Speech-to-Speech API
Chuyển đổi giọng nói từ audio này sang giọng khác.

API: POST https://api.elevenlabs.io/v1/speech-to-speech/{voice_id}
Models hỗ trợ:
- eleven_english_sts_v2: Tiếng Anh
- eleven_multilingual_sts_v2: Đa ngôn ngữ
"""
from __future__ import annotations

import os
import json
import time
import requests
from typing import Optional, Dict, Tuple, Callable
from datetime import datetime


# Models hỗ trợ Speech-to-Speech (fixed từ API)
STS_MODELS = [
    ("eleven_multilingual_sts_v2", "Đa ngôn ngữ (Multilingual v2)"),
    ("eleven_english_sts_v2", "Tiếng Anh (English v2)"),
]

# Output formats
OUTPUT_FORMATS = [
    "mp3_44100_128",
    "mp3_44100_192",
    "mp3_44100_64",
    "mp3_22050_32",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_44100",
]


def _log(msg: str):
    """Log message với timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [VoiceChanger] {msg}")


class VoiceChangerService:
    """Service để gọi Speech-to-Speech API."""
    
    BASE_URL = "https://api.elevenlabs.io/v1/speech-to-speech"
    
    def __init__(self):
        pass
    
    def convert(
        self,
        api_key: str,
        voice_id: str,
        audio_path: str,
        output_path: str,
        model_id: str = "eleven_multilingual_sts_v2",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        use_speaker_boost: bool = False,
        remove_background_noise: bool = False,
        output_format: str = "mp3_44100_128",
        proxy: str = None,
        progress_callback: Callable[[int, str], None] = None,
    ) -> Tuple[bool, str]:
        """
        Chuyển đổi giọng nói từ audio file.
        
        Args:
            api_key: ElevenLabs API key
            voice_id: ID của voice đích
            audio_path: Đường dẫn file audio nguồn
            output_path: Đường dẫn file output
            model_id: Model ID (eleven_multilingual_sts_v2 hoặc eleven_english_sts_v2)
            stability: Độ ổn định (0.0 - 1.0)
            similarity_boost: Độ tương đồng (0.0 - 1.0)
            style: Style (0.0 - 1.0)
            use_speaker_boost: Tăng cường loa
            remove_background_noise: Loại bỏ noise
            output_format: Định dạng output
            proxy: Proxy URL (optional)
            progress_callback: Callback để báo tiến độ (percent, message)
            
        Returns:
            Tuple[bool, str]: (success, error_message hoặc output_path)
        """
        if not os.path.exists(audio_path):
            return False, f"File không tồn tại: {audio_path}"
        
        if progress_callback:
            progress_callback(10, "Đang chuẩn bị...")
        
        url = f"{self.BASE_URL}/{voice_id}"
        
        # Query params
        params = {
            "output_format": output_format,
        }
        
        # Headers
        headers = {
            "xi-api-key": api_key,
        }
        
        # Voice settings as JSON string
        voice_settings = {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        }
        
        # Proxy
        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}
            _log(f"Using proxy: {proxy[:50]}...")
        
        if progress_callback:
            progress_callback(20, "Đang upload audio...")
        
        try:
            # Open file for upload
            with open(audio_path, 'rb') as audio_file:
                files = {
                    'audio': (os.path.basename(audio_path), audio_file, 'audio/mpeg'),
                }
                
                data = {
                    'model_id': model_id,
                    'voice_settings': json.dumps(voice_settings),
                    'remove_background_noise': str(remove_background_noise).lower(),
                }
                
                _log(f"Converting: {os.path.basename(audio_path)} -> voice {voice_id[:10]}...")
                
                if progress_callback:
                    progress_callback(40, "Đang xử lý...")
                
                response = requests.post(
                    url,
                    headers=headers,
                    params=params,
                    files=files,
                    data=data,
                    proxies=proxies,
                    timeout=300,  # 5 minutes timeout
                )
            
            if progress_callback:
                progress_callback(80, "Đang lưu file...")
            
            if response.status_code == 200:
                # Save audio file
                os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
                with open(output_path, 'wb') as f:
                    f.write(response.content)
                
                _log(f"✅ Success: {output_path}")
                
                if progress_callback:
                    progress_callback(100, "Hoàn thành!")
                
                return True, output_path
            
            # Parse error
            error_msg = response.text[:500]
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
            
            _log(f"❌ Error {response.status_code}: {error_msg[:100]}")
            return False, f"Error {response.status_code}: {error_msg}"
            
        except requests.exceptions.Timeout:
            _log("❌ Timeout")
            return False, "Timeout - file quá lớn hoặc server không phản hồi"
        except requests.exceptions.ConnectionError as e:
            _log(f"❌ Connection error: {e}")
            return False, f"Lỗi kết nối: {e}"
        except Exception as e:
            _log(f"❌ Exception: {e}")
            return False, str(e)
    
    def get_error_type(self, error_msg: str) -> str:
        """Phân loại lỗi để xử lý key rotation."""
        error_lower = error_msg.lower()
        
        # Quota exceeded - cần đổi key
        if 'quota_exceeded' in error_lower or 'quota' in error_lower:
            return '401'  # Treat as key error to rotate
        if '401' in error_msg or 'invalid_api_key' in error_lower or 'unauthorized' in error_lower:
            return '401'
        if '402' in error_msg or 'payment' in error_lower:
            return '402'
        if '429' in error_msg or 'rate' in error_lower or 'too_many' in error_lower:
            return '429'
        if 'unusual_activity' in error_lower or 'unusual activity' in error_lower:
            return 'proxy'
        if '400' in error_msg:
            return '400'
        if '5' in error_msg[:3]:  # 5xx errors
            return 'server'
        if 'timeout' in error_lower or 'connection' in error_lower:
            return 'connection'
        
        return 'unknown'
