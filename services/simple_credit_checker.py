from __future__ import annotations

import asyncio
import json
import subprocess
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class SimpleCreditChecker:
    """Simple service to check credits for existing user_api_keys"""
    
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.credit_cache = {}
        self.cache_ttl = 300  # 5 minutes
    
    async def check_api_key_credits(self, api_key: str) -> Optional[Dict]:
        """Check credits for a single API key using curl"""
        try:
            cmd = [
                'curl', '-s',
                'https://api.elevenlabs.io/v1/user',
                '-H', f'xi-api-key: {api_key}'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            
            if result.returncode != 0:
                logger.error(f"Curl command failed: {result.stderr}")
                return None
            
            data = json.loads(result.stdout)
            
            if 'subscription' not in data:
                return None
            
            subscription = data['subscription']
            
            return {
                'credits_used': subscription.get('character_count', 0),
                'credits_limit': subscription.get('character_limit', 0),
                'credits_remaining': subscription.get('character_limit', 0) - subscription.get('character_count', 0),
                'is_valid': True,
                'tier': subscription.get('tier', 'free'),
                'status': subscription.get('status', 'unknown')
            }
            
        except Exception as e:
            logger.error(f"Error checking API key credits: {e}")
            return None
    
    async def update_user_credits(self, user_id: int) -> Dict:
        """Update credits for all user's API keys"""
        try:
            print(f"🔄 Updating credits for user {user_id}...")
            
            # Get user's API keys
            api_keys = self.supabase.table('user_api_keys')\
                .select('id, api_key, credit_remaining, last_used')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .execute()
            
            if not api_keys.data:
                print(f"No active API keys found for user {user_id}")
                return {'total_credits': 0, 'updated_keys': 0}
            
            print(f"Found {len(api_keys.data)} API keys to check")
            
            total_credits = 0
            updated_count = 0
            
            # Check credits for each key
            for key_data in api_keys.data:
                api_key = key_data['api_key']
                key_id = key_data['id']
                
                print(f"Checking key {key_id}: {api_key[:20]}...")
                
                # Check if we should skip this key (recently checked)
                last_used = key_data.get('last_used')
                if last_used:
                    try:
                        last_check = datetime.fromisoformat(last_used.replace('Z', '+00:00'))
                        if datetime.now() - last_check < timedelta(minutes=5):
                            # Use cached value
                            cached_credits = key_data.get('credit_remaining', 0)
                            if cached_credits is not None:
                                total_credits += cached_credits
                                print(f"Using cached credits for key {key_id}: {cached_credits}")
                                continue
                    except:
                        pass
                
                # Check credits from ElevenLabs
                credit_info = await self.check_api_key_credits(api_key)
                
                if credit_info and credit_info['is_valid']:
                    remaining = credit_info['credits_remaining']
                    total_credits += remaining
                    
                    # Update database
                    self.supabase.table('user_api_keys')\
                        .update({
                            'credit_remaining': remaining,
                            'credit_limit': credit_info['credits_limit'],
                            'last_used': datetime.now().isoformat(),
                            'is_active': remaining > 0  # Mark as inactive if no credits
                        })\
                        .eq('id', key_id)\
                        .execute()
                    
                    updated_count += 1
                    print(f"✅ Updated key {key_id}: {remaining:,} credits remaining")
                else:
                    print(f"❌ Failed to check key {key_id}")
                    
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)
            
            result = {
                'total_credits': total_credits,
                'updated_keys': updated_count,
                'total_keys': len(api_keys.data),
                'last_updated': datetime.now().isoformat()
            }
            
            print(f"✅ Credit update completed: {total_credits:,} total credits from {updated_count} keys")
            return result
            
        except Exception as e:
            logger.error(f"Error updating user credits: {e}")
            return {'error': str(e), 'total_credits': 0}
    
    async def get_user_total_credits(self, user_id: int, force_refresh: bool = False) -> Dict:
        """Get total credits for user (with optional force refresh)"""
        try:
            # Check cache first (unless force refresh)
            cache_key = f"user_credits_{user_id}"
            if not force_refresh and cache_key in self.credit_cache:
                cached_data, timestamp = self.credit_cache[cache_key]
                if datetime.now() - timestamp < timedelta(seconds=self.cache_ttl):
                    print(f"Using cached credits for user {user_id}: {cached_data['total_credits']:,}")
                    return cached_data
            
            # Get fresh data
            result = await self.update_user_credits(user_id)
            
            # Cache result
            if 'error' not in result:
                self.credit_cache[cache_key] = (result, datetime.now())
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting user total credits: {e}")
            return {'error': str(e), 'total_credits': 0}
    
    def clear_cache(self, user_id: int = None):
        """Clear credit cache for user or all users"""
        if user_id:
            cache_key = f"user_credits_{user_id}"
            if cache_key in self.credit_cache:
                del self.credit_cache[cache_key]
                print(f"Cleared cache for user {user_id}")
        else:
            self.credit_cache.clear()
            print("Cleared all credit cache")
    
    async def get_next_available_key(self, user_id: int) -> Optional[str]:
        """Get next available API key with credits"""
        try:
            # Get active keys with credits
            api_keys = self.supabase.table('user_api_keys')\
                .select('api_key, credit_remaining')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .gt('credit_remaining', 0)\
                .order('last_used', desc=False)\
                .limit(1)\
                .execute()
            
            if api_keys.data:
                key = api_keys.data[0]['api_key']
                
                # Update last_used
                self.supabase.table('user_api_keys')\
                    .update({'last_used': datetime.now().isoformat()})\
                    .eq('api_key', key)\
                    .execute()
                
                return key
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting next available key: {e}")
            return None
