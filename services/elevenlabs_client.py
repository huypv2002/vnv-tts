# services/elevenlabs_client.py
"""
ElevenLabs API Client - migrated từ 11Labs0811.py ElevenClient.

Features:
- TTS synthesis với proxy support (LUÔN LUÔN dùng proxy)
- Session management với retry logic
- Data usage tracking
- Audio post-processing với FFmpeg
- Voice management
"""
from __future__ import annotations

import os
import time
import json
import shutil
import subprocess
from typing import Optional, Dict, List

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

ELEVEN_BASE = "https://api.elevenlabs.io"


class ElevenLabsClient:
    """
    Unified ElevenLabs API client với proxy support.
    
    🔧 QUAN TRỌNG: TẤT CẢ requests đều dùng proxy (unlimited bandwidth).
    """
    
    def __init__(self, key_pool, proxy_service, log_fn=None, settings=None):
        """
        Args:
            key_pool: KeyPoolManager hoặc LocalKeyPool
            proxy_service: ProxyService instance (simple version)
            log_fn: Logging callback function
            settings: Optional settings dict/object
        """
        self.keys = key_pool
        self.proxies = proxy_service
        self.log_fn = log_fn
        self.settings = settings
        
        # Create session (LUÔN dùng proxy)
        self.session = self._make_session()
        
        # Data usage tracking
        self.data_sent = 0
        self.data_recv = 0
    
    def log(self, msg: str):
        """Log message"""
        if self.log_fn:
            try:
                self.log_fn(msg)
            except:
                pass
        else:
            print(f"[ElevenLabs] {msg}")
    
    def _make_session(self) -> requests.Session:
        """Create requests session with retry logic"""
        s = requests.Session()
        s.trust_env = False
        
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "DELETE"])
        )
        
        adapter = HTTPAdapter(max_retries=retries, pool_maxsize=32)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        
        return s
    
    def refresh_session(self):
        """Recreate session to fix connection pool issues"""
        try:
            if self.session:
                self.session.close()
        except:
            pass
        
        self.session = self._make_session()
        self.log("🔄 Session refreshed")
    
    def _get_proxy_for_request(self) -> Optional[Dict[str, str]]:
        """Get proxy dict for requests library"""
        if not self.proxies:
            return None
        
        proxy_url = self.proxies.get_current_proxy()
        if not proxy_url:
            return None
        
        return {"http": proxy_url, "https": proxy_url}
    
    def _make_request(self, method: str, path: str, api_key: str = None, 
                     use_proxy: bool = True, **kwargs) -> requests.Response:
        """
        Generic API request với proxy support.
        
        Args:
            method: HTTP method (GET, POST, DELETE)
            path: API path (e.g., "/v1/voices")
            api_key: API key (nếu None, lấy từ key pool)
            use_proxy: Có dùng proxy không (default: True - LUÔN dùng proxy)
            **kwargs: Additional requests arguments
        
        Returns:
            requests.Response object
        """
        url = ELEVEN_BASE + path
        
        # Prepare headers
        headers = kwargs.pop("headers", {})
        headers["accept"] = "application/json"
        
        # Get API key
        if not api_key:
            if hasattr(self.keys, 'cur'):
                api_key = self.keys.cur()
            elif hasattr(self.keys, 'get_any_active_key'):
                api_key = self.keys.get_any_active_key()
        
        if not api_key:
            raise RuntimeError("No API key available")
        
        headers["xi-api-key"] = api_key
        kwargs["headers"] = headers
        
        # Add proxy (LUÔN LUÔN dùng proxy nếu có)
        if use_proxy:
            proxies = self._get_proxy_for_request()
            if proxies:
                kwargs["proxies"] = proxies
                # Mask password trong log
                proxy_url = proxies.get("http", "")
                if "@" in proxy_url:
                    parts = proxy_url.split("@")
                    masked = parts[0].split("://")[0] + "://***:***@" + parts[1]
                else:
                    masked = proxy_url
                self.log(f"[Request] {method} {path} via proxy {masked[:60]}...")
            else:
                self.log(f"⚠️ [Request] No proxy available, using DIRECT")
        
        # Track request size
        body = kwargs.get("data") or kwargs.get("json")
        sent_bytes = len(str(body).encode('utf-8')) if body else 0
        
        # Make request
        t0 = time.time()
        r = self.session.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
        dt = int((time.time() - t0) * 1000)
        
        # Track response size
        recv_bytes = len(r.content) if r.content else 0
        self.data_sent += sent_bytes
        self.data_recv += recv_bytes
        
        self.log(f"{method} {path} {r.status_code} {dt}ms | Sent: {sent_bytes/1024:.1f}KB Recv: {recv_bytes/1024:.1f}KB")
        
        # Handle errors
        if r.status_code == 400:
            try:
                err_json = r.json()
                detail = err_json.get("detail", err_json)
                if isinstance(detail, dict):
                    status = detail.get("status", "N/A")
                    message = detail.get("message", "N/A")
                    self.log(f"   [400 Response] status={status}, message={message[:200]}")
                else:
                    self.log(f"   [400 Response] {str(detail)[:250]}")
            except:
                self.log(f"   [400 Response] Raw: {r.text[:250] if r.text else 'empty'}")
        
        # Retry on certain errors
        if r.status_code in (401, 429, 403, 503):
            self.log(f"⚠️ Error {r.status_code} - retrying with rotation...")
            
            # Rotate key
            if hasattr(self.keys, 'rotate'):
                self.keys.rotate()
            
            # Rotate proxy
            if self.proxies and use_proxy:
                old_proxy = self.proxies.get_current_proxy()
                new_proxy = self.proxies.rotate_to_next()
                self.log(f"   Rotated proxy: {old_proxy[:30] if old_proxy else 'None'}... → {new_proxy[:30] if new_proxy else 'None'}...")
                if new_proxy:
                    kwargs["proxies"] = {"http": new_proxy, "https": new_proxy}
            
            time.sleep(1.0)
            
            # Retry request
            r = self.session.request(method, url, timeout=60, **kwargs)
            self.log(f"   Retry → {r.status_code}")
        
        r.raise_for_status()
        return r
    
    # ========== TTS Methods ==========
    
    def tts_synthesize(self, voice_id: str, text: str, model_id: str, 
                      voice_settings: dict, output_path: str, 
                      output_format: str = "mp3_44100_128",
                      language_code: str = None) -> bool:
        """
        Synthesize TTS với PROXY (unlimited bandwidth).
        
        Args:
            voice_id: Voice ID
            text: Text to synthesize
            model_id: Model ID
            voice_settings: Voice settings dict
            output_path: Output file path
            output_format: Output format (default: mp3_44100_128)
            language_code: Optional language code (vi, en, etc.)
        
        Returns:
            True if successful
        
        Raises:
            RuntimeError on failure
        """
        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        }
        
        # Add language_code if specified
        if language_code:
            payload["language_code"] = language_code
            self.log(f"🌐 Language: {language_code.upper()}")
        else:
            self.log(f"🌐 Language: Auto-detect")
        
        # Prepare request
        url_path = f"/v1/text-to-speech/{voice_id}?output_format={output_format}"
        full_url = ELEVEN_BASE + url_path
        
        headers = {
            "accept": "audio/mpeg",
            "Content-Type": "application/json"
        }
        
        # Get API key
        if hasattr(self.keys, 'cur'):
            api_key = self.keys.cur()
        else:
            api_key = self.keys.get_any_active_key()
        
        if not api_key:
            raise RuntimeError("No API key available")
        
        headers["xi-api-key"] = api_key
        
        # Get proxy (LUÔN LUÔN dùng proxy)
        proxies = self._get_proxy_for_request()
        if proxies:
            proxy_info = list(proxies.values())[0][:50]
            self.log(f"🌐 [TTS] Using proxy: {proxy_info}...")
        else:
            self.log(f"⚠️ [TTS] No proxy available - using DIRECT (not recommended)")
        
        # Track bytes
        payload_json = json.dumps(payload)
        sent_bytes = len(payload_json.encode('utf-8'))
        
        try:
            # Make request with streaming
            t0 = time.time()
            r = self.session.post(
                full_url,
                json=payload,
                headers=headers,
                proxies=proxies,
                timeout=120,
                stream=True
            )
            dt = int((time.time() - t0) * 1000)
            
            self.log(f"POST {url_path} {r.status_code} {dt}ms")
            
            # Handle retry on auth errors
            if r.status_code in (401, 429):
                self.log(f"⚠️ Error {r.status_code} - rotating and retrying...")
                
                # Rotate key
                if hasattr(self.keys, 'rotate'):
                    self.keys.rotate()
                    api_key = self.keys.cur()
                    headers["xi-api-key"] = api_key
                
                # Rotate proxy
                if self.proxies:
                    new_proxy_url = self.proxies.rotate_to_next()
                    if new_proxy_url:
                        proxies = {"http": new_proxy_url, "https": new_proxy_url}
                
                time.sleep(1.0)
                
                # Retry
                r = self.session.post(
                    full_url,
                    json=payload,
                    headers=headers,
                    proxies=proxies,
                    timeout=120,
                    stream=True
                )
                self.log(f"   Retry → {r.status_code}")
            
            # Handle 400 error
            if r.status_code == 400:
                raise RuntimeError(f"400 Client Error: {r.text}")
            
            r.raise_for_status()
            
            # Download audio to file
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            tmp = output_path + ".part"
            recv_bytes = 0
            
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        recv_bytes += len(chunk)
            
            os.replace(tmp, output_path)
            
            # Track data usage
            self.data_sent += sent_bytes
            self.data_recv += recv_bytes
            
            self.log(f"   → Downloaded: {recv_bytes/1024:.1f}KB ({recv_bytes} bytes)")
            
            # Post-process audio (optional FFmpeg processing)
            if hasattr(self, '_post_process_audio'):
                try:
                    self._post_process_audio(output_path, enable_processing=True)
                except Exception as e:
                    self.log(f"⚠️ Post-processing error: {e}")
            
            return True
        
        except Exception as e:
            self.log(f"❌ TTS error: {e}")
            raise
    
    def _post_process_audio(self, mp3_path: str, enable_processing: bool = True):
        """
        Post-process audio với FFmpeg (minimal - chỉ highpass filter).
        Migrated từ 11Labs0811.py
        """
        if not enable_processing:
            return
        
        # Find ffmpeg
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            # Try common paths
            common_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
                r"D:\ffmpeg\bin\ffmpeg.exe",
            ]
            for path in common_paths:
                if os.path.exists(path):
                    ffmpeg = path
                    break
        
        if not ffmpeg:
            self.log("⚠️ FFmpeg not found - skipping post-processing")
            return
        
        try:
            temp_out = mp3_path + ".processed.mp3"
            
            # Minimal filter: highpass=f=50 (cut rumble below 50Hz)
            filter_chain = "highpass=f=50"
            
            cmd = [
                ffmpeg,
                "-i", mp3_path,
                "-af", filter_chain,
                "-ar", "44100",
                "-b:a", "128k",
                "-y",
                temp_out
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if result.returncode == 0 and os.path.exists(temp_out):
                os.replace(temp_out, mp3_path)
                self.log(f"✨ Audio post-processed: highpass filter applied")
            else:
                self.log(f"⚠️ FFmpeg processing failed: {result.stderr[:200]}")
        
        except Exception as e:
            self.log(f"⚠️ Post-processing error: {e}")
    
    def log_data_summary(self):
        """Log data usage summary"""
        total_kb = (self.data_sent + self.data_recv) / 1024
        self.log(f"📊 Data Usage: Sent {self.data_sent/1024:.1f}KB | Recv {self.data_recv/1024:.1f}KB | Total: {total_kb:.1f}KB")
    
    # ========== Voice Management ==========
    
    def list_voices(self, api_key: str = None) -> List[dict]:
        """List all voices"""
        try:
            r = self._make_request("GET", "/v1/voices", api_key=api_key, use_proxy=True)
            if r.content:
                js = r.json()
                return js.get("voices", [])
        except Exception as e:
            self.log(f"List voices error: {e}")
        return []
    
    def get_voice(self, voice_id: str, api_key: str = None) -> Optional[dict]:
        """Get voice by ID"""
        try:
            r = self._make_request("GET", f"/v1/voices/{voice_id}", api_key=api_key, use_proxy=True)
            if r.content:
                return r.json()
        except Exception as e:
            self.log(f"Get voice error: {e}")
        return None
    
    def search_voices(self, query: str = "", voice_id: str = "", 
                     page_size: int = 100, max_pages: int = 10, 
                     api_key: str = None) -> List[dict]:
        """Search voices in library"""
        all_voices = []
        next_token = None
        pages_fetched = 0
        
        while pages_fetched < max_pages:
            params = {"page_size": page_size, "include_total_count": "true"}
            if voice_id:
                params["voice_ids"] = voice_id
            elif query:
                params["search"] = query
            if next_token:
                params["next_page_token"] = next_token
            
            try:
                r = self._make_request("GET", "/v2/voices", api_key=api_key, 
                                      use_proxy=True, params=params)
                if not r.content:
                    break
                
                js = r.json()
                voices = js.get("voices", [])
                all_voices.extend(voices)
                
                has_more = js.get("has_more", False)
                next_token = js.get("next_page_token")
                
                if not has_more or not next_token:
                    break
                pages_fetched += 1
            except Exception as e:
                self.log(f"Search voices error: {e}")
                break
        
        return all_voices
    
    def delete_voice(self, voice_id: str, api_key: str) -> Tuple[bool, str]:
        """Delete a voice"""
        try:
            r = self._make_request("DELETE", f"/v1/voices/{voice_id}", 
                                  api_key=api_key, use_proxy=True, timeout=30)
            if r.status_code in (200, 204):
                return True, "Deleted"
            return False, f"{r.status_code}: {r.text}"
        except Exception as e:
            return False, f"Error: {e}"
    
    def cleanup_voice_slots(self, api_key: str, max_voices: int = 2, 
                           protected_voice_ids: List[str] = None) -> int:
        """
        Clean up voice slots by deleting excess voices.
        
        Args:
            api_key: API key
            max_voices: Max voices to keep (default: 2)
            protected_voice_ids: List of voice IDs to protect from deletion
        
        Returns:
            Number of voices deleted
        """
        if protected_voice_ids is None:
            protected_voice_ids = []
        
        # Get all voices for this key
        voices = self.list_voices(api_key=api_key)
        
        # Filter deletable voices
        deletable = []
        for v in voices:
            vid = v.get("voice_id", "")
            category = v.get("category", "").lower()
            
            # Skip protected voices
            if vid in protected_voice_ids:
                continue
            
            # Skip premade/default voices
            if category in ("premade", "default"):
                continue
            
            deletable.append(vid)
        
        current_count = len(voices)
        if current_count <= max_voices:
            return 0
        
        to_delete = current_count - max_voices
        deleted = 0
        
        for vid in deletable[:to_delete]:
            ok, msg = self.delete_voice(vid, api_key)
            if ok:
                deleted += 1
                self.log(f"[Cleanup] Deleted voice {vid[:8]}... → slot freed")
            else:
                # Consider already deleted as success
                if "voice_does_not_exist" in msg or "404" in msg:
                    deleted += 1
                else:
                    self.log(f"[Cleanup] Delete error {vid[:8]}...: {msg}")
            
            time.sleep(0.3)  # Rate limit
        
        return deleted
    
    # ========== Subscription/Credits ==========
    
    def get_subscription(self, api_key: str) -> Optional[dict]:
        """Get subscription info for API key"""
        try:
            r = self._make_request("GET", "/v1/user/subscription", 
                                  api_key=api_key, use_proxy=True)
            return r.json()
        except Exception:
            return None
