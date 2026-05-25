from __future__ import annotations

import asyncio
import json
import subprocess
import concurrent.futures
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class OptimizedCreditChecker:
    """Optimized credit checker - only checks keys when necessary"""
    
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.credit_cache = {}
        self.cache_ttl = 3600  # 1 hour cache for unused keys
        self.used_key_cache_ttl = 300  # 5 minutes for used keys
        
        # Default credits for new/unused keys (ElevenLabs standard)
        self.DEFAULT_CREDITS = 9998
        
    def check_api_key_credits_sync(self, api_key: str) -> Optional[Dict]:
        """Synchronous version for thread pool execution"""
        import os
        try:
            cmd = [
                'curl', '-s',
                'https://api.elevenlabs.io/v1/user',
                '-H', f'xi-api-key: {api_key}'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
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
                'credits_limit': subscription.get('character_limit', 9998),
                'credits_remaining': subscription.get('character_limit', 9998) - subscription.get('character_count', 0),
                'is_valid': True,
                'tier': subscription.get('tier', 'free'),
                'status': subscription.get('status', 'unknown')
            }
            
        except Exception as e:
            logger.error(f"Error checking API key credits: {e}")
            return None
    
    async def check_api_key_credits(self, api_key: str) -> Optional[Dict]:
        """Async wrapper for backward compatibility"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.check_api_key_credits_sync, api_key)
    
    def _should_check_key(self, key_data: Dict) -> bool:
        """Determine if a key needs to be checked"""
        last_used = key_data.get('last_used')
        credit_remaining = key_data.get('credit_remaining')
        
        # Always check if no credit info
        if credit_remaining is None:
            return True
        
        # Don't check if key has never been used and has default credits
        if not last_used and credit_remaining >= self.DEFAULT_CREDITS * 0.9:  # 90% of default
            return False
        
        # Check used keys more frequently
        if last_used:
            try:
                last_check = datetime.fromisoformat(last_used.replace('Z', '+00:00'))
                # Check if last used within 24 hours
                if datetime.now() - last_check < timedelta(hours=24):
                    return True
            except:
                pass
        
        # Check keys with low credits
        if credit_remaining < 1000:
            return True
        
        # Skip checking for unused keys with high credits
        return False
    
    def _get_keys_to_check(self, api_keys: List[Dict], max_keys: int = 10) -> List[Dict]:
        """Get priority list of keys to check"""
        keys_to_check = []
        
        # Priority 1: Keys with no credit info
        for key in api_keys:
            if key.get('credit_remaining') is None:
                keys_to_check.append(key)
                if len(keys_to_check) >= max_keys:
                    break
        
        # Priority 2: Recently used keys
        if len(keys_to_check) < max_keys:
            recently_used = [k for k in api_keys if k.get('last_used') and self._should_check_key(k)]
            recently_used.sort(key=lambda x: x.get('last_used', ''), reverse=True)
            
            for key in recently_used:
                if key not in keys_to_check:
                    keys_to_check.append(key)
                    if len(keys_to_check) >= max_keys:
                        break
        
        # Priority 3: Keys with low credits
        if len(keys_to_check) < max_keys:
            low_credit_keys = [k for k in api_keys if k.get('credit_remaining', 0) < 1000]
            low_credit_keys.sort(key=lambda x: x.get('credit_remaining', 0))
            
            for key in low_credit_keys:
                if key not in keys_to_check:
                    keys_to_check.append(key)
                    if len(keys_to_check) >= max_keys:
                        break
        
        return keys_to_check
    
    async def get_user_total_credits_fast(self, user_id: int) -> Dict:
        """Fast credit calculation using smart estimation"""
        try:
            print(f"🚀 Fast credit check for user {user_id}...")
            
            # Get all user's API keys
            api_keys = self.supabase.table('user_api_keys')\
                .select('id, api_key, credit_remaining, credit_limit, last_used, is_active')\
                .eq('user_id', user_id)\
                .execute()
            
            if not api_keys.data:
                return {'total_credits': 0, 'active_keys': 0, 'method': 'no_keys'}
            
            total_keys = len(api_keys.data)
            active_keys = len([k for k in api_keys.data if k.get('is_active', True)])
            
            # Calculate estimated total credits
            estimated_credits = 0
            keys_with_data = 0
            keys_checked = 0
            
            # Get keys that need checking (max 10)
            keys_to_check = self._get_keys_to_check(api_keys.data, max_keys=10)
            
            print(f"📊 Total keys: {total_keys}, Need checking: {len(keys_to_check)}")
            
            # Check priority keys
            for key_data in keys_to_check:
                api_key = key_data['api_key']
                key_id = key_data['id']
                
                print(f"🔍 Checking priority key {key_id}: {api_key[:20]}...")
                
                credit_info = await self.check_api_key_credits(api_key)
                
                if credit_info and credit_info['is_valid']:
                    remaining = credit_info['credits_remaining']
                    estimated_credits += remaining
                    keys_checked += 1
                    
                    # Update database
                    self.supabase.table('user_api_keys')\
                        .update({
                            'credit_remaining': remaining,
                            'credit_limit': credit_info['credits_limit'],
                            'last_used': datetime.now().isoformat(),
                            'is_active': remaining > 0
                        })\
                        .eq('id', key_id)\
                        .execute()
                    
                    print(f"✅ Key {key_id}: {remaining:,} credits")
                else:
                    print(f"❌ Failed to check key {key_id}")
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.3)
            
            # Estimate credits for unchecked keys
            unchecked_keys = [k for k in api_keys.data if k not in keys_to_check]
            
            for key_data in unchecked_keys:
                credit_remaining = key_data.get('credit_remaining')
                last_used = key_data.get('last_used')
                
                if credit_remaining is not None:
                    # Use cached value
                    estimated_credits += credit_remaining
                    keys_with_data += 1
                elif not last_used:
                    # Assume default credits for unused keys
                    estimated_credits += self.DEFAULT_CREDITS
                    keys_with_data += 1
                    
                    # Update database with default (9998 credits)
                    self.supabase.table('user_api_keys')\
                        .update({
                            'credit_remaining': self.DEFAULT_CREDITS,
                            'credit_limit': self.DEFAULT_CREDITS
                        })\
                        .eq('id', key_data['id'])\
                        .execute()
            
            result = {
                'total_credits': estimated_credits,
                'active_keys': active_keys,
                'total_keys': total_keys,
                'keys_checked': keys_checked,
                'keys_estimated': len(unchecked_keys),
                'method': 'smart_estimation',
                'last_updated': datetime.now().isoformat()
            }
            
            print(f"🎯 Fast check completed: {estimated_credits:,} credits ({keys_checked} checked, {len(unchecked_keys)} estimated)")
            return result
            
        except Exception as e:
            logger.error(f"Error in fast credit check: {e}")
            return {'error': str(e), 'total_credits': 0}
    
    async def background_credit_refresh(self, user_id: int, max_keys_per_batch: int = 100, max_workers: int = 20) -> Dict:
        """Background refresh - check multiple batches of keys"""
        try:
            print(f"🔄 Background refresh for user {user_id} (max {max_keys_per_batch} keys per batch)...")
            
            # Get keys that haven't been checked recently
            api_keys = self.supabase.table('user_api_keys')\
                .select('id, api_key, credit_remaining, last_used')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .execute()
            
            if not api_keys.data:
                return {'updated': 0, 'message': 'No keys found'}
            
            # Find keys that need background refresh
            keys_to_refresh = []
            now = datetime.now()
            
            for key_data in api_keys.data:
                last_used = key_data.get('last_used')
                if last_used:
                    try:
                        last_check = datetime.fromisoformat(last_used.replace('Z', '+00:00'))
                        # Check keys that haven't been updated in 30 minutes
                        if now - last_check > timedelta(minutes=30):
                            keys_to_refresh.append(key_data)
                    except:
                        keys_to_refresh.append(key_data)
                else:
                    # Never checked keys (priority)
                    keys_to_refresh.append(key_data)
            
            if not keys_to_refresh:
                return {'updated': 0, 'message': 'All keys are up to date'}
            
            print(f"📊 Found {len(keys_to_refresh)} keys that need refresh")
            
            # Process in batches using ThreadPoolExecutor for true parallelism
            total_updated = 0
            batch_count = 0
            
            for i in range(0, len(keys_to_refresh), max_keys_per_batch):
                batch = keys_to_refresh[i:i + max_keys_per_batch]
                batch_count += 1
                
                print(f"🔄 Processing batch {batch_count}: {len(batch)} keys with {max_workers} workers")
                
                # Process batch with thread pool for true parallelism
                batch_updated = await self._process_key_batch_parallel(batch, max_workers)
                total_updated += batch_updated
                
                print(f"✅ Batch {batch_count} completed: {batch_updated}/{len(batch)} keys updated")
                
                # Small delay between batches to avoid overwhelming the API
                if i + max_keys_per_batch < len(keys_to_refresh):
                    await asyncio.sleep(2.0)  # Longer delay for large batches
            
            return {
                'updated': total_updated,
                'total_checked': len(keys_to_refresh),
                'batches_processed': batch_count,
                'message': f'Background updated {total_updated} keys in {batch_count} batches'
            }
            
        except Exception as e:
            logger.error(f"Error in background refresh: {e}")
            return {'error': str(e), 'updated': 0}
    
    async def _process_key_batch_parallel(self, key_batch: List[Dict], max_workers: int = 20) -> int:
        """Process a batch of keys using ThreadPoolExecutor for true parallelism"""
        try:
            loop = asyncio.get_event_loop()
            
            # Use ThreadPoolExecutor for true parallel processing
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks to thread pool
                future_to_key = {
                    executor.submit(self._check_and_update_key_sync, key_data): key_data 
                    for key_data in key_batch
                }
                
                updated_count = 0
                completed = 0
                
                # Process completed futures as they finish
                for future in concurrent.futures.as_completed(future_to_key):
                    completed += 1
                    key_data = future_to_key[future]
                    
                    try:
                        result = future.result()
                        if result:
                            updated_count += 1
                            if completed % 10 == 0:  # Progress update every 10 keys
                                print(f"  📈 Progress: {completed}/{len(key_batch)} keys processed")
                    except Exception as e:
                        logger.error(f"Error processing key {key_data.get('id', 'unknown')}: {e}")
                
                return updated_count
                
        except Exception as e:
            logger.error(f"Error in parallel batch processing: {e}")
            return 0
    
    async def _process_key_batch(self, key_batch: List[Dict]) -> int:
        """Legacy method - redirects to parallel version"""
        return await self._process_key_batch_parallel(key_batch, max_workers=10)
    
    def _check_and_update_key_sync(self, key_data: Dict) -> bool:
        """Synchronous version for thread pool execution"""
        try:
            api_key = key_data['api_key']
            key_id = key_data['id']
            
            # Check credits using synchronous method
            credit_info = self.check_api_key_credits_sync(api_key)
            
            if credit_info and credit_info['is_valid']:
                remaining = credit_info['credits_remaining']
                
                # Update database
                self.supabase.table('user_api_keys')\
                    .update({
                        'credit_remaining': remaining,
                        'last_used': datetime.now().isoformat(),
                        'is_active': remaining > 0
                    })\
                    .eq('id', key_id)\
                    .execute()
                
                print(f"✅ Updated key {key_id}: {remaining:,} credits")
                return True
            else:
                print(f"❌ Failed to check key {key_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error checking key {key_data.get('id', 'unknown')}: {e}")
            return False
    
    async def _check_and_update_key(self, key_data: Dict) -> bool:
        """Async wrapper for backward compatibility"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_and_update_key_sync, key_data)
    
    async def get_next_available_key(self, user_id: int) -> Optional[str]:
        """Get next available API key with credits"""
        try:
            # Get keys with credits, prioritize recently checked ones
            api_keys = self.supabase.table('user_api_keys')\
                .select('api_key, credit_remaining, last_used')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .order('credit_remaining', desc=True)\
                .limit(5)\
                .execute()
            
            if not api_keys.data:
                return None
            
            # Find best key
            for key_data in api_keys.data:
                credits = key_data.get('credit_remaining', 0)
                if credits > 100:  # Has enough credits
                    key = key_data['api_key']
                    
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
    
    def clear_cache(self, user_id: int = None):
        """Clear credit cache"""
        if user_id:
            cache_key = f"user_credits_{user_id}"
            if cache_key in self.credit_cache:
                del self.credit_cache[cache_key]
        else:
            self.credit_cache.clear()
