from __future__ import annotations

import os
import json
import subprocess
import requests
import time
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union
import logging

logger = logging.getLogger(__name__)


class DubbingService:
    """
    Service for ElevenLabs Dubbing API operations
    
    Note: This service can use Canada proxy to avoid "unusual activity" detection.
    Proxy usage is configurable via use_proxy parameter.
    """
    
    def __init__(self, supabase_client=None, user_id: Optional[int] = None, use_proxy: bool = True):
        self.supabase = supabase_client
        self.user_id = user_id
        self.base_url = "https://api.elevenlabs.io/v1"
        self.dubbing_cache = {}  # Cache for dubbing status
        self.cache_ttl = 60  # 1 minute cache
        self.use_proxy = use_proxy
        
        if use_proxy:
            print(f"🇨🇦 [DUBBING] Proxy ENABLED - Will use Canada proxy for anti-detection")
        else:
            # Disable proxy at environment level for this service
            self._disable_proxy_environment()
    
    def _disable_proxy_environment(self) -> None:
        """Disable proxy at environment variable level"""
        try:
            # Clear proxy environment variables
            proxy_vars = ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 
                         'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy']
            
            for var in proxy_vars:
                if var in os.environ:
                    original_value = os.environ[var]
                    print(f"🚫 [DUBBING] Clearing proxy env var: {var}={original_value}")
                    del os.environ[var]
            
            # Set no_proxy to disable all proxies for this process
            os.environ['NO_PROXY'] = '*'
            os.environ['no_proxy'] = '*'
            
            print(f"🚫 [DUBBING] All proxy environment variables cleared")
            print(f"🚫 [DUBBING] Set NO_PROXY=* to disable all proxies")
            
        except Exception as e:
            print(f"❌ [DUBBING] Error disabling proxy environment: {e}")
    
    def _parse_watermark_error(self, error_msg: str) -> Optional[str]:
        """Parse watermark-specific errors and provide user-friendly messages"""
        try:
            # Try to parse JSON error
            error_data = json.loads(error_msg)
            detail = error_data.get('detail', {})
            
            if isinstance(detail, dict):
                status = detail.get('status', '')
                message = detail.get('message', '')
                
                if status == 'watermark_not_allowed':
                    if 'audio input' in message.lower():
                        return (
                            "❌ Watermark không được hỗ trợ cho file audio.\n"
                            "💡 Giải pháp: Sử dụng file video hoặc tắt watermark (chỉ dành cho Creator+)"
                        )
                    elif 'Creator+' in message:
                        return (
                            "❌ Tắt watermark chỉ dành cho người dùng Creator+.\n"
                            "💡 Giải pháp: Sử dụng file video với watermark hoặc upgrade tài khoản"
                        )
                elif status == 'detected_unusual_activity':
                    return (
                        "❌ ElevenLabs phát hiện hoạt động bất thường và tắt Free Tier.\n"
                        "🇨🇦 Đang dùng Canada proxy để tránh phát hiện.\n"
                        "💡 Nếu vẫn lỗi: Thử API key khác hoặc upgrade tài khoản Paid Plan."
                    )
                
            return f"API Error: {message}" if message else None
            
        except Exception:
            # Not JSON or parsing failed
            if 'watermark_not_allowed' in error_msg.lower():
                return (
                    "❌ Lỗi cài đặt watermark.\n"
                    "💡 Thử: File video + watermark ON hoặc upgrade tài khoản Creator+"
                )
            elif 'unusual activity' in error_msg.lower() or 'detected_unusual_activity' in error_msg.lower():
                return (
                    "❌ ElevenLabs phát hiện hoạt động bất thường.\n"
                    "🇨🇦 Đang dùng Canada proxy để tránh phát hiện.\n"
                    "💡 Thử API key khác hoặc chờ một lúc rồi thử lại."
                )
            
            return None
    
    def test_connection(self, api_key: str) -> Dict:
        """Test connection to ElevenLabs API without proxy"""
        try:
            print(f"🧪 [DUBBING TEST] Testing connection to ElevenLabs API...")
            
            # Simple test call to user endpoint
            url = "https://api.elevenlabs.io/v1/user"
            headers = {'xi-api-key': api_key}
            
            proxy_config = None if self.use_proxy else {'http': None, 'https': None}
            proxy_status = "🇨🇦 CANADA PROXY" if self.use_proxy else "🚫 NO PROXY"
            print(f"🧪 [DUBBING TEST] Using {proxy_status}")
            
            response = requests.get(
                url, 
                headers=headers, 
                timeout=10,
                proxies=proxy_config
            )
            
            print(f"🧪 [DUBBING TEST] Response status: {response.status_code}")
            print(f"🧪 [DUBBING TEST] Response: {response.text}")
            
            if response.status_code == 200:
                return {'success': True, 'message': 'Connection successful'}
            else:
                return {'success': False, 'error': f'HTTP {response.status_code}: {response.text}'}
                
        except Exception as e:
            print(f"❌ [DUBBING TEST] Connection failed: {e}")
            return {'success': False, 'error': str(e)}
    
    async def create_dubbing(self, api_key: str, **params) -> Dict:
        """
        Create a new dubbing project
        
        Args:
            api_key: ElevenLabs API key
            **params: Dubbing parameters (file, name, source_lang, target_lang, etc.)
        
        Returns:
            Dict with dubbing_id and expected_duration_sec
        """
        try:
            url = f"{self.base_url}/dubbing"
            headers = {
                'xi-api-key': api_key
            }
            
            # Prepare multipart form data
            files = {}
            data = {}
            
            # Handle file upload with proper content-type
            if 'file_path' in params and params['file_path']:
                file_path = params['file_path']
                if os.path.exists(file_path):
                    content_type = self._get_content_type(file_path)
                    print(f"📁 [FILE UPLOAD] Path: {file_path}")
                    print(f"📁 [FILE UPLOAD] Content-Type: {content_type}")
                    print(f"📁 [FILE UPLOAD] Size: {os.path.getsize(file_path)} bytes")
                    
                    files['file'] = (
                        os.path.basename(file_path),
                        open(file_path, 'rb'),
                        content_type
                    )
            
            # Handle other file types if needed
            for file_param in ['csv_file', 'foreground_audio_file', 'background_audio_file']:
                if file_param in params and params[file_param]:
                    file_path = params[file_param]
                    if os.path.exists(file_path):
                        files[file_param] = (
                            os.path.basename(file_path),
                            open(file_path, 'rb'),
                            self._get_content_type(file_path)
                        )
            
            # Smart parameter handling
            text_params = [
                'name', 'source_url', 'source_lang', 'target_lang', 'target_accent',
                'num_speakers', 'start_time', 'end_time', 'highest_resolution',
                'drop_background_audio', 'use_profanity_filter', 'dubbing_studio',
                'disable_voice_cloning', 'mode', 'csv_fps'
            ]
            
            # Add basic parameters
            for param in text_params:
                if param in params and params[param] is not None:
                    if isinstance(params[param], bool):
                        data[param] = str(params[param]).lower()
                    else:
                        data[param] = str(params[param])
            
            # Handle watermark intelligently
            if 'watermark' in params:
                watermark_value = params['watermark']
                
                # Check if we have video file
                has_video_file = files and 'file' in files
                is_video_type = False
                
                if has_video_file:
                    file_path = params.get('file_path', '')
                    content_type = self._get_content_type(file_path)
                    is_video_type = content_type.startswith('video/')
                
                print(f"🏷️ [WATERMARK] Has file: {has_video_file}, Is video: {is_video_type}, Watermark requested: {watermark_value}")
                
                # Only add watermark parameter for video files
                if is_video_type:
                    data['watermark'] = str(watermark_value).lower()
                    print(f"🏷️ [WATERMARK] Added watermark={watermark_value} for video file")
                else:
                    print(f"🏷️ [WATERMARK] Skipped watermark parameter for audio file (avoid API error)")
            
            print(f"📋 [FINAL PARAMS] Data to send: {data}")
            
            logger.info(f"DUBBING_CREATE_START - URL: {url}")
            logger.info(f"DUBBING_PARAMS - Data: {data}")
            logger.info(f"DUBBING_HEADERS - Headers: {headers}")
            logger.info(f"DUBBING_FILES - Files: {list(files.keys())}")
            
            print(f"🌐 [DUBBING API] POST {url}")
            print(f"📋 [DUBBING API] Data: {data}")
            print(f"🔑 [DUBBING API] API Key: {api_key[:20]}...")
            print(f"📁 [DUBBING API] Files: {list(files.keys())}")
            
            # Configure proxy based on setting
            proxy_config = None if self.use_proxy else {'http': None, 'https': None}
            proxy_status = "🇨🇦 CANADA PROXY ENABLED" if self.use_proxy else "🚫 PROXY DISABLED - Direct connection"
            print(f"{proxy_status}")
            
            # Make request with or without proxy
            response = requests.post(
                url, 
                headers=headers, 
                files=files, 
                data=data,
                timeout=120,  # 2 minutes timeout for file upload
                proxies=proxy_config
            )
            
            # Close file handles
            for file_handle in files.values():
                if hasattr(file_handle[1], 'close'):
                    file_handle[1].close()
            
            print(f"📊 [DUBBING API] Response Status: {response.status_code}")
            print(f"📄 [DUBBING API] Response Headers: {dict(response.headers)}")
            print(f"📝 [DUBBING API] Response Text: {response.text}")
            
            logger.info(f"DUBBING_CREATE_RESPONSE - Status: {response.status_code}")
            logger.info(f"DUBBING_CREATE_RESPONSE_TEXT - {response.text}")
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"DUBBING_CREATE_SUCCESS - Result: {result}")
                
                # Store in database if available
                if self.supabase and self.user_id:
                    await self._store_dubbing_project(result, params)
                
                return {
                    'success': True,
                    'dubbing_id': result.get('dubbing_id'),
                    'expected_duration_sec': result.get('expected_duration_sec'),
                    'message': 'Dubbing project created successfully'
                }
            else:
                # Parse error response for specific watermark issues
                error_msg = response.text
                specific_error = self._parse_watermark_error(error_msg)
                
                full_error = f"API Error {response.status_code}: {error_msg}"
                logger.error(f"DUBBING_CREATE_ERROR - {full_error}")
                
                return {
                    'success': False,
                    'error': specific_error or full_error,
                    'error_code': response.status_code,
                    'raw_error': error_msg
                }
                
        except Exception as e:
            logger.error(f"DUBBING_CREATE_EXCEPTION - {str(e)}")
            return {
                'success': False,
                'error': f"Exception: {str(e)}"
            }
    
    async def get_dubbing_status(self, api_key: str, dubbing_id: str) -> Dict:
        """
        Get dubbing project status
        
        Args:
            api_key: ElevenLabs API key
            dubbing_id: Dubbing project ID
        
        Returns:
            Dict with dubbing metadata and status
        """
        try:
            # Check cache first
            cache_key = f"dubbing_{dubbing_id}"
            if cache_key in self.dubbing_cache:
                cached_data, timestamp = self.dubbing_cache[cache_key]
                if datetime.now() - timestamp < timedelta(seconds=self.cache_ttl):
                    return cached_data
            
            url = f"{self.base_url}/dubbing/{dubbing_id}"
            headers = {
                'xi-api-key': api_key
            }
            
            logger.info(f"DUBBING_STATUS_REQUEST - URL: {url}")
            
            print(f"🌐 [DUBBING STATUS] GET {url}")
            print(f"🔑 [DUBBING STATUS] API Key: {api_key[:20]}...")
            
            proxy_config = None if self.use_proxy else {'http': None, 'https': None}
            proxy_status = "🇨🇦 CANADA PROXY ENABLED" if self.use_proxy else "🚫 PROXY DISABLED - Direct connection"
            print(f"{proxy_status}")
            
            response = requests.get(url, headers=headers, timeout=30, proxies=proxy_config)
            
            print(f"📊 [DUBBING STATUS] Response Status: {response.status_code}")
            print(f"📝 [DUBBING STATUS] Response Text: {response.text}")
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"DUBBING_STATUS_SUCCESS - Status: {result.get('status')}")
                
                # Cache successful response
                self.dubbing_cache[cache_key] = (result, datetime.now())
                
                # Update database if available
                if self.supabase and self.user_id:
                    await self._update_dubbing_status(dubbing_id, result)
                
                return {
                    'success': True,
                    'dubbing_id': result.get('dubbing_id'),
                    'name': result.get('name'),
                    'status': result.get('status'),
                    'target_languages': result.get('target_languages', []),
                    'created_at': result.get('created_at'),
                    'editable': result.get('editable', False),
                    'error': result.get('error'),
                    'media_metadata': result.get('media_metadata')
                }
            else:
                error_msg = f"API Error {response.status_code}: {response.text}"
                logger.error(f"DUBBING_STATUS_ERROR - {error_msg}")
                return {
                    'success': False,
                    'error': error_msg
                }
                
        except Exception as e:
            logger.error(f"DUBBING_STATUS_EXCEPTION - {str(e)}")
            return {
                'success': False,
                'error': f"Exception: {str(e)}"
            }
    
    async def download_dubbed_audio(self, api_key: str, dubbing_id: str, 
                                  language_code: str, output_path: str) -> Dict:
        """
        Download dubbed audio for specific language
        
        Args:
            api_key: ElevenLabs API key
            dubbing_id: Dubbing project ID
            language_code: Target language code
            output_path: Local path to save the audio file
        
        Returns:
            Dict with success status and file path
        """
        try:
            url = f"{self.base_url}/dubbing/{dubbing_id}/audio/{language_code}"
            headers = {
                'xi-api-key': api_key
            }
            
            logger.info(f"DUBBING_DOWNLOAD_REQUEST - URL: {url}")
            
            print(f"🌐 [DUBBING DOWNLOAD] GET {url}")
            print(f"🔑 [DUBBING DOWNLOAD] API Key: {api_key[:20]}...")
            print(f"📁 [DUBBING DOWNLOAD] Output: {output_path}")
            
            proxy_config = None if self.use_proxy else {'http': None, 'https': None}
            proxy_status = "🇨🇦 CANADA PROXY ENABLED" if self.use_proxy else "🚫 PROXY DISABLED - Direct connection"
            print(f"{proxy_status}")
            
            response = requests.get(url, headers=headers, timeout=300, stream=True, proxies=proxy_config)
            
            print(f"📊 [DUBBING DOWNLOAD] Response Status: {response.status_code}")
            print(f"📄 [DUBBING DOWNLOAD] Content-Type: {response.headers.get('content-type', 'Unknown')}")
            print(f"📏 [DUBBING DOWNLOAD] Content-Length: {response.headers.get('content-length', 'Unknown')}")
            
            if response.status_code == 200:
                # Ensure output directory exists
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                
                # Download file in chunks
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                logger.info(f"DUBBING_DOWNLOAD_SUCCESS - File saved: {output_path}")
                
                return {
                    'success': True,
                    'file_path': output_path,
                    'file_size': os.path.getsize(output_path),
                    'message': f'Downloaded dubbed audio for {language_code}'
                }
            else:
                error_msg = f"Download Error {response.status_code}: {response.text}"
                logger.error(f"DUBBING_DOWNLOAD_ERROR - {error_msg}")
                return {
                    'success': False,
                    'error': error_msg
                }
                
        except Exception as e:
            logger.error(f"DUBBING_DOWNLOAD_EXCEPTION - {str(e)}")
            return {
                'success': False,
                'error': f"Exception: {str(e)}"
            }
    
    async def list_user_dubbing_projects(self) -> List[Dict]:
        """
        List user's dubbing projects from database
        
        Returns:
            List of dubbing projects
        """
        try:
            if not self.supabase or not self.user_id:
                return []
            
            result = self.supabase.table('user_dubbing_projects')\
                .select('*')\
                .eq('user_id', self.user_id)\
                .order('created_at', desc=True)\
                .execute()
            
            return result.data if result.data else []
            
        except Exception as e:
            logger.error(f"LIST_DUBBING_PROJECTS_ERROR - {str(e)}")
            return []
    
    async def _store_dubbing_project(self, dubbing_result: Dict, params: Dict) -> None:
        """Store dubbing project in database"""
        try:
            if not self.supabase or not self.user_id:
                return
            
            project_data = {
                'user_id': self.user_id,
                'dubbing_id': dubbing_result.get('dubbing_id'),
                'project_name': params.get('name', ''),
                'source_lang': params.get('source_lang', ''),
                'target_lang': params.get('target_lang', ''),
                'expected_duration_sec': dubbing_result.get('expected_duration_sec', 0),
                'status': 'created',
                'parameters': json.dumps(params),
                'created_at': datetime.now().isoformat()
            }
            
            self.supabase.table('user_dubbing_projects').insert(project_data).execute()
            logger.info(f"DUBBING_PROJECT_STORED - ID: {dubbing_result.get('dubbing_id')}")
            
        except Exception as e:
            logger.error(f"STORE_DUBBING_PROJECT_ERROR - {str(e)}")
    
    async def _update_dubbing_status(self, dubbing_id: str, status_data: Dict) -> None:
        """Update dubbing project status in database"""
        try:
            if not self.supabase or not self.user_id:
                return
            
            update_data = {
                'status': status_data.get('status', 'unknown'),
                'updated_at': datetime.now().isoformat()
            }
            
            # Add error if present
            if status_data.get('error'):
                update_data['error_message'] = status_data['error']
            
            self.supabase.table('user_dubbing_projects')\
                .update(update_data)\
                .eq('user_id', self.user_id)\
                .eq('dubbing_id', dubbing_id)\
                .execute()
            
            logger.info(f"DUBBING_STATUS_UPDATED - ID: {dubbing_id}, Status: {status_data.get('status')}")
            
        except Exception as e:
            logger.error(f"UPDATE_DUBBING_STATUS_ERROR - {str(e)}")
    
    def _get_content_type(self, file_path: str) -> str:
        """Get content type for file with comprehensive MIME type detection"""
        ext = os.path.splitext(file_path)[1].lower()
        
        video_types = {
            '.mp4': 'video/mp4',
            '.avi': 'video/avi',
            '.mov': 'video/quicktime',
            '.mkv': 'video/x-matroska',
            '.wmv': 'video/x-ms-wmv',
            '.flv': 'video/x-flv',
            '.webm': 'video/webm',
            '.m4v': 'video/x-m4v'
        }
        
        audio_types = {
            '.mp3': 'audio/mpeg',
            '.wav': 'audio/wav',
            '.flac': 'audio/flac',
            '.aac': 'audio/aac',
            '.ogg': 'audio/ogg',
            '.m4a': 'audio/mp4',
            '.wma': 'audio/x-ms-wma'
        }
        
        content_type = video_types.get(ext) or audio_types.get(ext)
        
        if not content_type:
            # Try to detect from file content
            try:
                import mimetypes
                content_type, _ = mimetypes.guess_type(file_path)
            except:
                pass
        
        # Final fallback
        final_type = content_type or 'application/octet-stream'
        print(f"📁 [CONTENT-TYPE] File: {os.path.basename(file_path)} → {final_type}")
        
        return final_type
    
    async def get_next_api_key(self) -> Optional[str]:
        """Get next available API key for dubbing"""
        try:
            if not self.supabase or not self.user_id:
                return None
            
            # Get active keys for user
            result = self.supabase.table('active_api_keys')\
                .select('api_key')\
                .eq('user_id', self.user_id)\
                .eq('is_exhausted', False)\
                .limit(1)\
                .execute()
            
            if result.data:
                return result.data[0]['api_key']
            
            return None
            
        except Exception as e:
            logger.error(f"GET_API_KEY_ERROR - {str(e)}")
            return None
    
    async def monitor_dubbing_progress(self, api_key: str, dubbing_id: str, 
                                     callback=None, check_interval: int = 30) -> Dict:
        """
        Monitor dubbing progress until completion
        
        Args:
            api_key: ElevenLabs API key
            dubbing_id: Dubbing project ID
            callback: Optional callback function to call on status updates
            check_interval: Seconds between status checks
        
        Returns:
            Final status dict
        """
        try:
            logger.info(f"DUBBING_MONITOR_START - ID: {dubbing_id}")
            
            max_attempts = 120  # 1 hour max with 30-second intervals
            attempts = 0
            
            while attempts < max_attempts:
                status_result = await self.get_dubbing_status(api_key, dubbing_id)
                
                if not status_result.get('success'):
                    logger.error(f"DUBBING_MONITOR_ERROR - {status_result.get('error')}")
                    return status_result
                
                status = status_result.get('status', '').lower()
                logger.info(f"DUBBING_MONITOR_STATUS - ID: {dubbing_id}, Status: {status}, Attempt: {attempts + 1}")
                
                # Call callback if provided
                if callback:
                    try:
                        callback(status_result)
                    except Exception as e:
                        logger.error(f"DUBBING_MONITOR_CALLBACK_ERROR - {str(e)}")
                
                # Check if completed
                if status in ['completed', 'successful', 'done']:
                    logger.info(f"DUBBING_MONITOR_COMPLETED - ID: {dubbing_id}")
                    return status_result
                
                # Check if failed
                if status in ['failed', 'error']:
                    error_msg = status_result.get('error', 'Dubbing failed')
                    logger.error(f"DUBBING_MONITOR_FAILED - ID: {dubbing_id}, Error: {error_msg}")
                    return {
                        'success': False,
                        'error': f"Dubbing failed: {error_msg}"
                    }
                
                attempts += 1
                
                # Wait before next check
                if attempts < max_attempts:
                    await self._async_sleep(check_interval)
            
            # Timeout
            logger.warning(f"DUBBING_MONITOR_TIMEOUT - ID: {dubbing_id}")
            return {
                'success': False,
                'error': f"Monitoring timeout after {max_attempts * check_interval} seconds"
            }
            
        except Exception as e:
            logger.error(f"DUBBING_MONITOR_EXCEPTION - {str(e)}")
            return {
                'success': False,
                'error': f"Monitoring exception: {str(e)}"
            }
    
    async def _async_sleep(self, seconds: int) -> None:
        """Async sleep helper"""
        import asyncio
        await asyncio.sleep(seconds)
