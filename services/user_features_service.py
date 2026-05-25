"""
User Features Service - Quản lý các tính năng được phép sử dụng cho mỗi user
"""
from __future__ import annotations
from typing import Optional, Dict
from dataclasses import dataclass


@dataclass
class UserFeatures:
    """Data class chứa các feature flags cho user"""
    user_id: int
    tab_text_to_speech: bool = True      # 🎤 Chuyển văn bản (luôn bật)
    tab_premium_voice: bool = True       # 💎 Giọng Trả Phí
    tab_multiple_voice: bool = False     # 💎 Đa giọng đọc
    tab_dubbing: bool = False            # 🎬 Lồng Tiếng
    tab_sound_effects: bool = False      # 🔊 Hiệu ứng âm thanh
    tab_speech_to_text: bool = False     # 🎙️ Audio thành văn bản
    tab_voice_changer: bool = False      # 🔄 Thay đổi giọng nói
    
    @classmethod
    def from_dict(cls, data: dict) -> 'UserFeatures':
        """Create UserFeatures from dict (DB response)"""
        return cls(
            user_id=data.get('user_id', 0),
            tab_text_to_speech=bool(data.get('tab_text_to_speech', 1)),
            tab_premium_voice=bool(data.get('tab_premium_voice', 1)),
            tab_multiple_voice=bool(data.get('tab_multiple_voice', 0)),
            tab_dubbing=bool(data.get('tab_dubbing', 0)),
            tab_sound_effects=bool(data.get('tab_sound_effects', 0)),
            tab_speech_to_text=bool(data.get('tab_speech_to_text', 0)),
            tab_voice_changer=bool(data.get('tab_voice_changer', 0)),
        )
    
    @classmethod
    def default(cls, user_id: int = 0) -> 'UserFeatures':
        """Create default features (only TTS and Premium Voice enabled)"""
        return cls(
            user_id=user_id,
            tab_text_to_speech=True,
            tab_premium_voice=True,
        )


class UserFeaturesService:
    """Service để query và cache user features"""
    
    _instance = None
    _cache: Dict[int, UserFeatures] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._db_client = None
        print("✅ UserFeaturesService initialized")
    
    def set_db_client(self, client):
        """Set DB client (D1Client hoặc SupabaseAuth)"""
        self._db_client = client
    
    def get_features(self, user_id: int, force_refresh: bool = False) -> UserFeatures:
        """
        Get user features từ cache hoặc DB
        
        Args:
            user_id: ID của user
            force_refresh: True để bỏ qua cache và query DB
            
        Returns:
            UserFeatures object
        """
        # Check cache first
        if not force_refresh and user_id in self._cache:
            return self._cache[user_id]
        
        # Query from DB
        features = self._fetch_from_db(user_id)
        
        # Cache result
        self._cache[user_id] = features
        
        return features
    
    def _fetch_from_db(self, user_id: int) -> UserFeatures:
        """Fetch features từ DB"""
        if not self._db_client:
            print("⚠️ [UserFeatures] No DB client, using defaults")
            return UserFeatures.default(user_id)
        
        try:
            # Try D1 client first (has rpc method)
            if hasattr(self._db_client, 'rpc'):
                result = self._db_client.rpc('get_user_features', {'user_id': user_id})
                if result and result.data:
                    print(f"✅ [UserFeatures] Loaded from D1 for user {user_id}")
                    return UserFeatures.from_dict(result.data)
                elif result and result.error:
                    print(f"⚠️ [UserFeatures] D1 RPC error: {result.error}")
            
            # Try D1Auth client (has .client property)
            elif hasattr(self._db_client, 'client') and hasattr(self._db_client.client, 'rpc'):
                result = self._db_client.client.rpc('get_user_features', {'user_id': user_id})
                if result and result.data:
                    print(f"✅ [UserFeatures] Loaded from D1 (via auth) for user {user_id}")
                    return UserFeatures.from_dict(result.data)
            
            # Try Supabase client (has .client.table method)
            elif hasattr(self._db_client, 'client') and hasattr(self._db_client.client, 'table'):
                from services.db_retry_helper import safe_db_operation
                
                def _query():
                    return self._db_client.client.table('user_features').select('*').eq('user_id', user_id).single().execute()
                
                result = safe_db_operation(_query, max_retries=2, default_return=None)
                if result and result.data:
                    print(f"✅ [UserFeatures] Loaded from Supabase for user {user_id}")
                    return UserFeatures.from_dict(result.data)
                
                # Create default if not exists
                def _insert():
                    return self._db_client.client.table('user_features').insert({
                        'user_id': user_id,
                        'tab_text_to_speech': True,
                        'tab_premium_voice': True,
                    }).execute()
                
                safe_db_operation(_insert, max_retries=1, default_return=None)
                print(f"✅ [UserFeatures] Created default for user {user_id}")
                
        except Exception as e:
            print(f"⚠️ [UserFeatures] Error fetching: {e}")
        
        return UserFeatures.default(user_id)
    
    def clear_cache(self, user_id: Optional[int] = None):
        """Clear cache cho user hoặc tất cả"""
        if user_id:
            self._cache.pop(user_id, None)
        else:
            self._cache.clear()


# Singleton instance
_features_service: Optional[UserFeaturesService] = None


def get_features_service() -> UserFeaturesService:
    """Get singleton instance của UserFeaturesService"""
    global _features_service
    if _features_service is None:
        _features_service = UserFeaturesService()
    return _features_service


def get_user_features(user_id: int, db_client=None) -> UserFeatures:
    """
    Convenience function để get user features
    
    Args:
        user_id: ID của user
        db_client: Optional DB client (sẽ được set nếu chưa có)
        
    Returns:
        UserFeatures object
    """
    service = get_features_service()
    if db_client:
        service.set_db_client(db_client)
    return service.get_features(user_id)
