from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class UserService:
    """Service for managing user information and account details"""
    
    def __init__(self, supabase_client):
        self.supabase = supabase_client
    
    def get_user_account_info(self, user_id: int) -> Dict:
        """
        Get comprehensive user account information
        Returns: Complete user profile with subscription, credits, etc.
        """
        try:
            # Get basic user info
            user_info = self._get_user_basic_info(user_id)
            if not user_info:
                return {'error': 'User not found'}
            
            # Get subscription info
            subscription_info = self._get_user_subscription(user_id)
            
            # Get credit summary
            credit_info = self._get_user_credits(user_id)
            
            # Get API key stats
            api_key_stats = self._get_api_key_stats(user_id)
            
            # Get usage stats
            usage_stats = self._get_usage_stats(user_id)
            
            # Combine all information
            account_info = {
                'user': user_info,
                'subscription': subscription_info,
                'credits': credit_info,
                'api_keys': api_key_stats,
                'usage': usage_stats,
                'last_updated': datetime.now().isoformat()
            }
            
            return account_info
            
        except Exception as e:
            logger.error(f"Error getting user account info: {e}")
            return {'error': str(e)}
    
    def _get_user_basic_info(self, user_id: int) -> Optional[Dict]:
        """Get basic user information with retry logic"""
        from services.db_retry_helper import safe_db_operation
        import time
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                def _query():
                    return (
                        self.supabase.table('users')
                        .select('id, username, role, login_count, device_id, last_device_change')
                        .eq('id', user_id)
                        .execute()
                    )
                
                result = safe_db_operation(_query, max_retries=2, default_return=None)
                
                if result and result.data:
                    return result.data[0]
                
                # Nếu không có data và chưa hết retry, đợi một chút rồi thử lại
                if attempt < max_retries - 1:
                    wait_time = 0.5 * (attempt + 1)  # 0.5s, 1s, 1.5s
                    logger.warning(f"User {user_id} not found, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                
                return None
                
            except Exception as e:
                logger.error(f"Error getting user basic info (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return None
        
        return None
    
    def _get_user_subscription(self, user_id: int) -> Dict:
        """Get user subscription information"""
        try:
            # ✅ OPTIMIZED: Chỉ select các fields cần thiết thay vì *
            result = self.supabase.table('user_subscriptions')\
                .select('subscription_type, start_date, end_date, is_active, max_videos_per_day, max_videos_per_month, features_allowed, count_characters')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .limit(1)\
                .execute()
            
            if result.data:
                sub = result.data[0]
                
                # Calculate days remaining
                days_remaining = None
                if sub['end_date']:
                    end_date = datetime.fromisoformat(sub['end_date'].replace('Z', '+00:00'))
                    days_remaining = max(0, (end_date - datetime.now()).days)
                
                return {
                    'type': sub['subscription_type'],
                    'start_date': sub['start_date'],
                    'end_date': sub['end_date'],
                    'days_remaining': days_remaining,
                    'is_active': sub['is_active'],
                    'max_videos_per_day': sub['max_videos_per_day'],
                    'max_videos_per_month': sub['max_videos_per_month'],
                    'features_allowed': sub['features_allowed'],
                    # New: character-based quota for this subscription
                    'count_characters': sub.get('count_characters'),
                    'status': 'Active' if sub['is_active'] and (not days_remaining or days_remaining > 0) else 'Expired'
                }
            else:
                return {
                    'type': 'free',
                    'status': 'No active subscription',
                    'is_active': False
                }
                
        except Exception as e:
            logger.error(f"Error getting user subscription: {e}")
            return {'error': str(e)}
    
    def _get_user_credits(self, user_id: int) -> Dict:
        """Get user credit information from user_api_keys table"""
        try:
            # Get credits from user_api_keys table
            result = self.supabase.table('user_api_keys')\
                .select('credit_remaining, credit_limit, last_used')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .execute()
            
            if result.data:
                total_credits = 0
                for key_data in result.data:
                    remaining = key_data.get('credit_remaining', 0)
                    if remaining is not None:
                        total_credits += remaining
                
                # Get the most recent last_used as last_updated
                last_updated = None
                for key_data in result.data:
                    if key_data.get('last_used'):
                        if not last_updated or key_data['last_used'] > last_updated:
                            last_updated = key_data['last_used']
                
                return {
                    'total_credits': total_credits,
                    'last_updated': last_updated,
                    'next_refresh': None
                }
            else:
                return {
                    'total_credits': 0,
                    'last_updated': None,
                    'next_refresh': None
                }
                
        except Exception as e:
            logger.error(f"Error getting user credits: {e}")
            return {'total_credits': 0, 'error': str(e)}
    
    def _get_api_key_stats(self, user_id: int) -> Dict:
        """Get API key statistics from existing user_api_keys table"""
        try:
            # Get API keys from existing table
            api_keys_result = self.supabase.table('user_api_keys')\
                .select('id, api_key, is_active, credit_remaining, credit_limit')\
                .eq('user_id', user_id)\
                .execute()
            
            if not api_keys_result.data:
                return {
                    'total_keys_available': 0,
                    'active_keys': 0,
                    'exhausted_keys': 0,
                    'total_credits': 0
                }
            
            total_keys = len(api_keys_result.data)
            active_keys = len([k for k in api_keys_result.data if k.get('is_active', True)])
            exhausted_keys = total_keys - active_keys
            
            # Calculate total credits from all keys
            total_credits = 0
            for key in api_keys_result.data:
                if key.get('is_active', True):
                    remaining = key.get('credit_remaining', 0)
                    if remaining is not None:
                        total_credits += remaining
            
            return {
                'total_keys_available': total_keys,
                'active_keys': active_keys,
                'exhausted_keys': exhausted_keys,
                'total_credits': total_credits,
                'keys_loaded': active_keys,
                'keys_remaining': 0  # All keys are already "loaded" in this table
            }
            
        except Exception as e:
            logger.error(f"Error getting API key stats: {e}")
            return {'error': str(e)}
    
    def _get_usage_stats(self, user_id: int) -> Dict:
        """Get usage statistics"""
        try:
            # Get today's usage
            today = datetime.now().date()
            today_result = self.supabase.table('user_usage_logs')\
                .select('videos_generated')\
                .eq('user_id', user_id)\
                .eq('usage_date', today.isoformat())\
                .execute()
            
            today_videos = sum(log['videos_generated'] for log in today_result.data)
            
            # Get this month's usage
            month_start = today.replace(day=1)
            month_result = self.supabase.table('user_usage_logs')\
                .select('videos_generated')\
                .eq('user_id', user_id)\
                .gte('usage_date', month_start.isoformat())\
                .execute()
            
            month_videos = sum(log['videos_generated'] for log in month_result.data)
            
            # Get total usage
            total_result = self.supabase.table('user_usage_logs')\
                .select('videos_generated')\
                .eq('user_id', user_id)\
                .execute()
            
            total_videos = sum(log['videos_generated'] for log in total_result.data)
            
            return {
                'videos_today': today_videos,
                'videos_this_month': month_videos,
                'videos_total': total_videos,
                'last_activity': self._get_last_activity(user_id)
            }
            
        except Exception as e:
            logger.error(f"Error getting usage stats: {e}")
            return {'error': str(e)}
    
    def _get_last_activity(self, user_id: int) -> Optional[str]:
        """Get user's last activity timestamp"""
        try:
            result = self.supabase.table('user_sessions')\
                .select('last_activity')\
                .eq('user_id', user_id)\
                .order('last_activity', desc=True)\
                .limit(1)\
                .execute()
            
            if result.data:
                return result.data[0]['last_activity']
            return None
            
        except Exception as e:
            logger.error(f"Error getting last activity: {e}")
            return None
    
    def update_user_activity(self, user_id: int) -> bool:
        """Update user's last activity timestamp"""
        try:
            # Update session activity
            self.supabase.table('user_sessions')\
                .update({'last_activity': datetime.now().isoformat()})\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .execute()
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating user activity: {e}")
            return False
    
    def log_video_generation(self, user_id: int, feature_used: str = 'tts_generation') -> bool:
        """Log video generation activity"""
        try:
            today = datetime.now().date()
            
            # Check if there's already a log for today
            existing = self.supabase.table('user_usage_logs')\
                .select('id, videos_generated')\
                .eq('user_id', user_id)\
                .eq('usage_date', today.isoformat())\
                .execute()
            
            if existing.data:
                # Update existing log
                log_id = existing.data[0]['id']
                current_count = existing.data[0]['videos_generated']
                
                self.supabase.table('user_usage_logs')\
                    .update({'videos_generated': current_count + 1})\
                    .eq('id', log_id)\
                    .execute()
            else:
                # Create new log
                self.supabase.table('user_usage_logs')\
                    .insert({
                        'user_id': user_id,
                        'usage_date': today.isoformat(),
                        'videos_generated': 1,
                        'feature_used': feature_used
                    })\
                    .execute()
            
            return True
            
        except Exception as e:
            logger.error(f"Error logging video generation: {e}")
            return False
