from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional, Dict
import math
from services.tts_service import estimate_credits_for_text
from services.db_retry_helper import safe_db_operation


class AccurateCreditTracker:
    """
    Accurate credit tracking system that logs every usage to database.
    Ensures credits are tracked precisely for billing purposes.
    """
    
    def __init__(self, supabase_client, user_id: int):
        self.supabase = supabase_client
        self.user_id = user_id
        self._lock = threading.Lock()
    
    def use_credits(self, api_key: str, model_name: str, text: str, 
                   endpoint: str = "text_to_speech", success: bool = True,
                   response_data: str = None, error_message: str = None) -> dict:
        """
        Record usage and update database immediately.
        Returns:
            dict: {
                "credits_used": estimated ElevenLabs credits used (for key tracking),
                "new_credits": credit_remaining after deduction (None if unknown)
            }
        Note:
            - Key credits: dùng estimate_credits_for_text (có factor 0.5 cho turbo/flash).
            - Subscription characters: luôn trừ đúng theo số ký tự (len(text)), không nhân 0.5.
        """
        # Credits used cho API key bookkeeping (giữ nguyên theo ElevenLabs)
        credits_used = estimate_credits_for_text(model_name, text)
        # Characters used cho quota gói:
        # - chars_raw = số ký tự thực tế
        # - billed_chars = chars_raw * 1.2 (làm tròn lên) để lời ~20%
        chars_raw = len(text or "")
        billed_chars = max(1, int(math.ceil(chars_raw * 1.2)))
        
        with self._lock:
            try:
                # Get api_key_id
                def _get_key():
                    return (
                        self.supabase.table('user_api_keys')
                        .select('id, credit_remaining, credit_limit')
                        .eq('api_key', api_key)
                        .eq('user_id', self.user_id)
                        .execute()
                    )

                key_result = safe_db_operation(_get_key, max_retries=3, default_return=None)
                
                if not key_result or not key_result.data:
                    print(f"❌ API key not found in database")
                    return {"credits_used": credits_used, "new_credits": None}
                
                key_data = key_result.data[0]
                api_key_id = key_data['id']
                current_credits = key_data.get('credit_remaining', 0) or 0
                
                # Update credit_remaining in user_api_keys
                new_credits = max(0, current_credits - credits_used)
                
                def _update_key():
                    return (
                        self.supabase.table('user_api_keys')
                        .update({
                            'credit_remaining': new_credits,
                            'last_used': datetime.now().isoformat(),
                            'updated_at': datetime.now().isoformat(),
                            'is_active': new_credits > 0
                        })
                        .eq('id', api_key_id)
                        .execute()
                    )

                safe_db_operation(_update_key, max_retries=3, default_return=None)
                
                print(f"💳 Updated key {api_key_id}: {current_credits:,} → {new_credits:,} (-{credits_used:,})")
                
                # Log to api_usage_logs for audit trail
                def _insert_log():
                    return (
                        self.supabase.table('api_usage_logs')
                        .insert({
                            'user_id': self.user_id,
                            'api_key_id': api_key_id,
                            'api_provider': 'elevenlabs',
                            'endpoint': endpoint,
                            'request_data': f"Model: {model_name}, Text length: {len(text)}",
                            'response_status': 200 if success else 500,
                            'response_data': response_data[:500] if response_data else None,
                            'error_message': error_message[:500] if error_message else None,
                            'credit_used': credits_used,
                            'processing_time_ms': 0,
                            'created_at': datetime.now().isoformat()
                        })
                        .execute()
                    )

                safe_db_operation(_insert_log, max_retries=3, default_return=None)
                
                print(f"📝 Logged usage to api_usage_logs")
                
                # Update user_credit_summary for quick total lookup (per-key credits)
                self._update_credit_summary()

                # Also deduct from user_subscriptions.count_characters (character-based quota, with 20% margin)
                self._deduct_subscription_characters(billed_chars)
                
                return {"credits_used": credits_used, "new_credits": new_credits}
                
            except Exception as e:
                print(f"❌ Error tracking credits: {e}")
                return {"credits_used": credits_used, "new_credits": None}
    
    def _update_credit_summary(self) -> None:
        """Update or create user_credit_summary with current total"""
        try:
            # Calculate total credits from all active keys
            def _get_keys():
                return (
                    self.supabase.table('user_api_keys')
                    .select('credit_remaining')
                    .eq('user_id', self.user_id)
                    .eq('is_active', True)
                    .execute()
                )

            keys_result = safe_db_operation(_get_keys, max_retries=3, default_return=None)

            if not keys_result or not keys_result.data:
                return
            
            total_credits = sum(k.get('credit_remaining', 0) or 0 for k in keys_result.data)
            
            # Upsert to user_credit_summary
            def _upsert_summary():
                return (
                    self.supabase.table('user_credit_summary')
                    .upsert({
                        'user_id': self.user_id,
                        'total_credits': total_credits,
                        'last_updated': datetime.now().isoformat()
                    })
                    .execute()
                )

            safe_db_operation(_upsert_summary, max_retries=3, default_return=None)
            
            print(f"📊 Updated credit summary: {total_credits:,} total credits")
            
        except Exception as e:
            print(f"❌ Error updating credit summary: {e}")

    def _deduct_subscription_characters(self, used_chars: int) -> None:
        """
        Deduct characters from the active subscription's count_characters field.
        This is the primary billing counter (characters remaining in the user's plan).
        """
        try:
            if used_chars <= 0:
                return

            def _get_sub():
                return (
                    self.supabase.table('user_subscriptions')
                    .select('id, count_characters')
                    .eq('user_id', self.user_id)
                    .eq('is_active', True)
                    .order('created_at', desc=True)
                    .limit(1)
                    .execute()
                )

            sub_result = safe_db_operation(_get_sub, max_retries=3, default_return=None)
            if not sub_result or not sub_result.data:
                return

            sub = sub_result.data[0]
            sub_id = sub['id']
            current_chars = sub.get('count_characters')
            if current_chars is None:
                # If not initialized, do not try to infer a starting value here.
                return

            new_chars = max(0, int(current_chars) - int(used_chars))

            def _update_sub():
                return (
                    self.supabase.table('user_subscriptions')
                    .update({
                        'count_characters': new_chars,
                        'updated_at': datetime.now().isoformat()
                    })
                    .eq('id', sub_id)
                    .execute()
                )

            safe_db_operation(_update_sub, max_retries=3, default_return=None)

            print(f"💬 Updated subscription characters: {current_chars:,} → {new_chars:,} (-{used_chars:,})")

        except Exception as e:
            print(f"❌ Error deducting subscription characters: {e}")
    
    def get_current_total_credits(self) -> int:
        """Get current total credits from database (accurate, real-time)"""
        try:
            # Sum all active keys
            def _get_keys():
                return (
                    self.supabase.table('user_api_keys')
                    .select('credit_remaining')
                    .eq('user_id', self.user_id)
                    .eq('is_active', True)
                    .execute()
                )

            keys_result = safe_db_operation(_get_keys, max_retries=3, default_return=None)

            if not keys_result or not keys_result.data:
                return 0
            
            total = sum(k.get('credit_remaining', 0) or 0 for k in keys_result.data)
            return total
            
        except Exception as e:
            print(f"❌ Error getting total credits: {e}")
            return 0

    def get_remaining_characters(self) -> int:
        """
        Get remaining characters in the current active subscription.
        If no active subscription or count_characters is NULL, returns 0.
        """
        try:
            def _get_sub():
                return (
                    self.supabase.table('user_subscriptions')
                    .select('count_characters')
                    .eq('user_id', self.user_id)
                    .eq('is_active', True)
                    .order('created_at', desc=True)
                    .limit(1)
                    .execute()
                )

            sub_result = safe_db_operation(_get_sub, max_retries=3, default_return=None)
            if not sub_result or not sub_result.data:
                return 0

            sub = sub_result.data[0]
            value = sub.get('count_characters')
            return int(value) if value is not None else 0

        except Exception as e:
            print(f"❌ Error getting remaining characters: {e}")
            return 0
    
    def get_usage_report(self, days: int = 7) -> Dict:
        """Get usage report for last N days"""
        try:
            from datetime import timedelta
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
            
            # Get usage logs
            logs = self.supabase.table('api_usage_logs')\
                .select('credit_used, created_at, endpoint')\
                .eq('user_id', self.user_id)\
                .gte('created_at', start_date)\
                .execute()
            
            total_used = sum(log.get('credit_used', 0) for log in logs.data)
            total_requests = len(logs.data)
            
            return {
                'days': days,
                'total_credits_used': total_used,
                'total_requests': total_requests,
                'average_per_request': total_used // max(1, total_requests),
                'logs_count': len(logs.data)
            }
            
        except Exception as e:
            print(f"❌ Error getting usage report: {e}")
            return {}

