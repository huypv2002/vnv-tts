from __future__ import annotations

import json
import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class UserConfigService:
    """Service để quản lý cấu hình user trong file JSON local"""
    
    def __init__(self, supabase_client=None):
        # Không cần supabase nữa, nhưng giữ để tương thích với code cũ
        self.supabase = supabase_client
        # Determine correct path for config file
        import sys
        if getattr(sys, 'frozen', False):
            # If running as compiled exe (Nuitka/PyInstaller), use the executable's directory
            base_dir = os.path.dirname(sys.executable)
        else:
            # If running as script, use the project root (parent of services folder)
            base_dir = os.path.dirname(os.path.dirname(__file__))
            
        self.config_file = os.path.join(base_dir, 'user_config.json')
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Trả về config mặc định"""
        return {
            # Voice settings
            'voice_id': '',
            'voice_name': '',
            'model_name': 'eleven_turbo_v2_5',
            
            # Voice parameters
            'stability': 0.5,
            'similarity_boost': 0.75,
            'style': 0.0,
            'use_speaker_boost': False,
            
            # UI settings
            'loop_enabled': True,
            'auto_split_enabled': True,
            'auto_srt_enabled': False,
            
            # Proxy settings
            'proxy_mode': 'FREE',
            'thread_count': 5,  # Default: 5 workers
            
            # Advanced settings
            'pause_between_segments_enabled': False,
            'segment_gap_seconds': 1.3,
            'segment_count': 5,
            'srt_split_enabled': False,
            'per_char_enabled': False,
            'comma_pause': 0.3,
            'dot_pause': 0.5,
            'download_type': '1 <ORIGINAL>',
            'max_chars_per_line': 1000,
            
            # Last used folder - DISABLED
            'last_folder_path': '',
            
            # Timestamps
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }
    
    def save_user_config(self, user_id: int = None, config: Dict[str, Any] = None) -> bool:
        """Lưu cấu hình user vào file JSON local"""
        try:
            logger.info(f"SAVE_CONFIG_START - Saving to {self.config_file}")
            
            if not config:
                logger.warning("SAVE_CONFIG_EMPTY - No config provided")
                return False
            
            # Chuẩn bị data để lưu (bỏ user_id vì là local config)
            config_data = {
                # Voice settings
                'voice_id': config.get('voice_id', ''),
                'voice_name': config.get('voice_name', ''),
                'model_name': config.get('model_name', 'eleven_turbo_v2_5'),
                
                # Voice parameters
                'stability': float(config.get('stability', 0.5)),
                'similarity_boost': float(config.get('similarity_boost', 0.75)),
                'style': float(config.get('style', 0.0)),
                'use_speaker_boost': bool(config.get('use_speaker_boost', False)),
                
                # UI settings
                'loop_enabled': bool(config.get('loop_enabled', True)),
                'auto_split_enabled': bool(config.get('auto_split_enabled', True)),
                'auto_srt_enabled': bool(config.get('auto_srt_enabled', False)),
                
                # Proxy settings
                'proxy_mode': config.get('proxy_mode', 'FREE'),
                'thread_count': int(config.get('thread_count', 5)),
                
                # Advanced settings
                'pause_between_segments_enabled': bool(config.get('pause_between_segments_enabled', False)),
                'segment_gap_seconds': float(config.get('segment_gap_seconds', 1.3)),
                'segment_count': int(config.get('segment_count', 5)),
                'srt_split_enabled': bool(config.get('srt_split_enabled', False)),
                'per_char_enabled': bool(config.get('per_char_enabled', False)),
                'comma_pause': float(config.get('comma_pause', 0.3)),
                'dot_pause': float(config.get('dot_pause', 0.5)),
                'download_type': config.get('download_type', '1 <ORIGINAL>'),
                'max_chars_per_line': int(config.get('max_chars_per_line', 1000)),
                
                # Last used folder - DISABLED
                'last_folder_path': '',
                
                'updated_at': datetime.now().isoformat()
            }
            
            # Load existing config để giữ created_at nếu có
            existing = self.load_user_config()
            if existing and existing.get('created_at'):
                config_data['created_at'] = existing['created_at']
            else:
                config_data['created_at'] = datetime.now().isoformat()
            
            # Lưu vào file JSON
            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(config_data, f, indent=2, ensure_ascii=False)
                
                logger.info(f"SAVE_CONFIG_SUCCESS - Saved to {self.config_file}")
                print(f"✅ Config saved to {self.config_file}")
                return True
            except Exception as e:
                logger.error(f"SAVE_CONFIG_FILE_ERROR - {e}")
                print(f"❌ Error writing config file: {e}")
                return False
                
        except Exception as e:
            logger.error(f"SAVE_CONFIG_ERROR - Error: {str(e)}")
            print(f"❌ Error saving user config: {e}")
            return False
    
    def load_user_config(self, user_id: int = None) -> Optional[Dict[str, Any]]:
        """Load cấu hình user từ file JSON local"""
        try:
            logger.info(f"LOAD_CONFIG_START - Loading from {self.config_file}")
            
            # Kiểm tra file có tồn tại không
            if not os.path.exists(self.config_file):
                logger.info(f"LOAD_CONFIG_NOT_FOUND - File not found: {self.config_file}")
                return None
            
            # Đọc file JSON
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
            except json.JSONDecodeError as e:
                logger.error(f"LOAD_CONFIG_JSON_ERROR - Invalid JSON: {e}")
                print(f"❌ Error parsing config file: {e}")
                return None
            except Exception as e:
                logger.error(f"LOAD_CONFIG_FILE_ERROR - {e}")
                print(f"❌ Error reading config file: {e}")
                return None
            
            logger.info(f"LOAD_CONFIG_SUCCESS - Loaded from {self.config_file}")
            
            # Trả về config với các giá trị đã parse
            return {
                # Voice settings
                'voice_id': config_data.get('voice_id', ''),
                'voice_name': config_data.get('voice_name', ''),
                'model_name': config_data.get('model_name', 'eleven_turbo_v2_5'),
                
                # Voice parameters
                'stability': float(config_data.get('stability', 0.5)),
                'similarity_boost': float(config_data.get('similarity_boost', 0.75)),
                'style': float(config_data.get('style', 0.0)),
                'use_speaker_boost': bool(config_data.get('use_speaker_boost', False)),
                
                # UI settings
                'loop_enabled': bool(config_data.get('loop_enabled', True)),
                'auto_split_enabled': bool(config_data.get('auto_split_enabled', True)),
                'auto_srt_enabled': bool(config_data.get('auto_srt_enabled', False)),
                
                # Proxy settings
                'proxy_mode': config_data.get('proxy_mode', 'FREE'),
                'thread_count': int(config_data.get('thread_count', 5)),
                
                # Advanced settings
                'pause_between_segments_enabled': bool(config_data.get('pause_between_segments_enabled', False)),
                'segment_gap_seconds': float(config_data.get('segment_gap_seconds', 1.3)),
                'segment_count': int(config_data.get('segment_count', 5)),
                'srt_split_enabled': bool(config_data.get('srt_split_enabled', False)),
                'per_char_enabled': bool(config_data.get('per_char_enabled', False)),
                'comma_pause': float(config_data.get('comma_pause', 0.3)),
                'dot_pause': float(config_data.get('dot_pause', 0.5)),
                'download_type': config_data.get('download_type', '1 <ORIGINAL>'),
                'max_chars_per_line': int(config_data.get('max_chars_per_line', 1000)),
                
                # Last used folder - DISABLED
                'last_folder_path': '',
                
                # Timestamps
                'created_at': config_data.get('created_at'),
                'updated_at': config_data.get('updated_at')
            }
            
        except Exception as e:
            logger.error(f"LOAD_CONFIG_ERROR - Error: {str(e)}")
            print(f"❌ Error loading user config: {e}")
            return None
    
    def delete_user_config(self, user_id: int = None) -> bool:
        """Xóa cấu hình user (xóa file JSON)"""
        try:
            logger.info(f"DELETE_CONFIG_START - Deleting {self.config_file}")
            
            if os.path.exists(self.config_file):
                os.remove(self.config_file)
                logger.info(f"DELETE_CONFIG_SUCCESS - Deleted {self.config_file}")
                print(f"✅ Config file deleted: {self.config_file}")
                return True
            else:
                logger.info(f"DELETE_CONFIG_NOT_FOUND - File not found: {self.config_file}")
                return True  # Coi như đã xóa nếu file không tồn tại
            
        except Exception as e:
            logger.error(f"DELETE_CONFIG_ERROR - Error: {str(e)}")
            print(f"❌ Error deleting user config: {e}")
            return False
