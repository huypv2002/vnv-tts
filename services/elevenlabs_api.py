from __future__ import annotations

import asyncio
import json
import subprocess
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class ElevenLabsAPI:
    """Service for managing ElevenLabs API keys and credits"""
    
    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self.credit_cache = {}  # In-memory cache for credits
        self.cache_ttl = 300  # 5 minutes cache
    
    async def check_api_key_credits(self, api_key: str) -> Optional[Dict]:
        """
        Check credits for a single API key using curl
        Returns: {credits_used, credits_limit, is_valid}
        """
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
    
    async def get_user_total_credits(self, user_id: int) -> Dict:
        """
        Get total credits for user by checking all active keys
        Uses smart caching to avoid too many API calls
        """
        try:
            # Check cache first
            cache_key = f"user_credits_{user_id}"
            if cache_key in self.credit_cache:
                cached_data, timestamp = self.credit_cache[cache_key]
                if datetime.now() - timestamp < timedelta(seconds=self.cache_ttl):
                    return cached_data
            
            # Get active keys for user
            active_keys = self.supabase.table('active_api_keys')\
                .select('api_key, cached_credits, last_credit_check')\
                .eq('user_id', user_id)\
                .eq('is_exhausted', False)\
                .execute()
            
            # If no active keys, try to load some from files
            if not active_keys.data:
                print(f"No active keys found for user {user_id}, trying to load from files...")
                await self._load_initial_keys(user_id)
                
                # Try again after loading
                active_keys = self.supabase.table('active_api_keys')\
                    .select('api_key, cached_credits, last_credit_check')\
                    .eq('user_id', user_id)\
                    .eq('is_exhausted', False)\
                    .execute()
            
            if not active_keys.data:
                print(f"Still no active keys for user {user_id}")
                return {'total_credits': 0, 'active_keys': 0, 'last_updated': datetime.now()}
            
            total_credits = 0
            keys_to_update = []
            
            # Check which keys need credit refresh
            for key_data in active_keys.data:
                last_check = key_data.get('last_credit_check')
                if not last_check or self._should_refresh_credits(last_check):
                    keys_to_update.append(key_data['api_key'])
                else:
                    # Use cached credits
                    total_credits += key_data.get('cached_credits', 0)
            
            # Batch check credits for keys that need update
            if keys_to_update:
                credit_results = await self._batch_check_credits(keys_to_update)
                
                for api_key, credits_info in credit_results.items():
                    if credits_info and credits_info['is_valid']:
                        remaining = credits_info['credits_remaining']
                        total_credits += remaining
                        
                        # Update cache in database
                        self.supabase.table('active_api_keys')\
                            .update({
                                'cached_credits': remaining,
                                'last_credit_check': datetime.now().isoformat(),
                                'is_exhausted': remaining <= 0
                            })\
                            .eq('api_key', api_key)\
                            .execute()
            
            result = {
                'total_credits': total_credits,
                'active_keys': len(active_keys.data),
                'last_updated': datetime.now()
            }
            
            # Update user credit summary
            self.supabase.table('user_credit_summary')\
                .upsert({
                    'user_id': user_id,
                    'total_credits': total_credits,
                    'last_updated': datetime.now().isoformat(),
                    'next_refresh': (datetime.now() + timedelta(seconds=self.cache_ttl)).isoformat()
                })\
                .execute()
            
            # Cache result
            self.credit_cache[cache_key] = (result, datetime.now())
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting user total credits: {e}")
            return {'total_credits': 0, 'active_keys': 0, 'error': str(e)}
    
    async def _batch_check_credits(self, api_keys: List[str]) -> Dict[str, Optional[Dict]]:
        """Check credits for multiple API keys concurrently"""
        tasks = []
        for api_key in api_keys:
            task = asyncio.create_task(self.check_api_key_credits(api_key))
            tasks.append((api_key, task))
        
        results = {}
        for api_key, task in tasks:
            try:
                result = await task
                results[api_key] = result
            except Exception as e:
                logger.error(f"Error checking credits for key {api_key[:10]}...: {e}")
                results[api_key] = None
        
        return results
    
    def _should_refresh_credits(self, last_check: str) -> bool:
        """Check if credits should be refreshed based on last check time"""
        try:
            last_check_time = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
            return datetime.now() - last_check_time > timedelta(seconds=self.cache_ttl)
        except:
            return True
    
    async def get_next_available_key(self, user_id: int) -> Optional[str]:
        """
        Get next available API key for user
        Automatically loads from file if no active keys available
        """
        try:
            # First, try to get an active key with credits
            active_key = self.supabase.table('active_api_keys')\
                .select('api_key')\
                .eq('user_id', user_id)\
                .eq('is_exhausted', False)\
                .order('last_used', desc=False)\
                .limit(1)\
                .execute()
            
            if active_key.data:
                key = active_key.data[0]['api_key']
                # Update last_used
                self.supabase.table('active_api_keys')\
                    .update({'last_used': datetime.now().isoformat()})\
                    .eq('api_key', key)\
                    .execute()
                return key
            
            # No active keys, try to load from file
            return await self._load_key_from_file(user_id)
            
        except Exception as e:
            logger.error(f"Error getting next available key: {e}")
            return None
    
    async def _load_key_from_file(self, user_id: int) -> Optional[str]:
        """Load next available key from encrypted file storage"""
        try:
            # Get user's key files that are still valid
            key_files = self.supabase.table('user_key_files')\
                .select('*')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .gt('expires_at', datetime.now().isoformat())\
                .gt('active_keys', 0)\
                .execute()
            
            if not key_files.data:
                return None
            
            # Try to load keys from first available file
            key_file = key_files.data[0]
            
            # Decrypt and parse file content
            keys = self._decrypt_key_file(key_file['encrypted_content'])
            
            if not keys:
                return None
            
            # Get already loaded keys to avoid duplicates
            existing_keys = self.supabase.table('active_api_keys')\
                .select('api_key')\
                .eq('key_file_id', key_file['id'])\
                .execute()
            
            existing_set = {k['api_key'] for k in existing_keys.data}
            
            # Find first unused key
            for key in keys:
                if key not in existing_set:
                    # Add to active keys
                    self.supabase.table('active_api_keys')\
                        .insert({
                            'user_id': user_id,
                            'key_file_id': key_file['id'],
                            'api_key': key,
                            'last_used': datetime.now().isoformat()
                        })\
                        .execute()
                    
                    # Update file stats
                    self.supabase.table('user_key_files')\
                        .update({'active_keys': key_file['active_keys'] + 1})\
                        .eq('id', key_file['id'])\
                        .execute()
                    
                    return key
            
            return None
            
        except Exception as e:
            logger.error(f"Error loading key from file: {e}")
            return None
    
    def _decrypt_key_file(self, encrypted_content: str) -> List[str]:
        """Decrypt and parse key file content"""
        try:
            # Simple base64 decode for now - you can implement proper encryption
            import base64
            import gzip
            
            # Decode base64
            compressed_data = base64.b64decode(encrypted_content)
            
            # Decompress
            decompressed_data = gzip.decompress(compressed_data)
            
            # Parse as text file
            content = decompressed_data.decode('utf-8')
            keys = [line.strip() for line in content.split('\n') if line.strip()]
            
            return keys
            
        except Exception as e:
            logger.error(f"Error decrypting key file: {e}")
            return []
    
    async def _load_initial_keys(self, user_id: int, count: int = 5) -> int:
        """Load initial keys from files for a user"""
        try:
            # Get user's key files
            key_files = self.supabase.table('user_key_files')\
                .select('*')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .gt('expires_at', datetime.now().isoformat())\
                .execute()
            
            if not key_files.data:
                print(f"No active key files found for user {user_id}")
                return 0
            
            loaded_count = 0
            
            for key_file in key_files.data:
                if loaded_count >= count:
                    break
                
                # Decrypt and get keys
                keys = self._decrypt_key_file(key_file['encrypted_content'])
                
                if not keys:
                    continue
                
                # Get already loaded keys to avoid duplicates
                existing_keys = self.supabase.table('active_api_keys')\
                    .select('api_key')\
                    .eq('key_file_id', key_file['id'])\
                    .execute()
                
                existing_set = {k['api_key'] for k in existing_keys.data}
                
                # Load new keys
                for key in keys:
                    if loaded_count >= count:
                        break
                        
                    if key not in existing_set:
                        # Add to active keys
                        self.supabase.table('active_api_keys')\
                            .insert({
                                'user_id': user_id,
                                'key_file_id': key_file['id'],
                                'api_key': key,
                                'cached_credits': 0,  # Will be checked later
                                'is_exhausted': False,
                                'last_used': datetime.now().isoformat()
                            })\
                            .execute()
                        
                        loaded_count += 1
                        print(f"Loaded key {loaded_count}: {key[:20]}...")
                
                # Update file stats
                if loaded_count > 0:
                    current_active = key_file.get('active_keys', 0)
                    self.supabase.table('user_key_files')\
                        .update({'active_keys': current_active + loaded_count})\
                        .eq('id', key_file['id'])\
                        .execute()
            
            print(f"Loaded {loaded_count} initial keys for user {user_id}")
            return loaded_count
            
        except Exception as e:
            logger.error(f"Error loading initial keys: {e}")
            return 0
    
    # ===== Phase 3A: Centralized API Client Methods =====
    
    def get_user_info_sync(self, api_key: str) -> Optional[Dict]:
        """
        Synchronous version of user info check for TTS service.
        Returns: {character_count, character_limit, remaining, tier, status}
        """
        try:
            cmd = [
                'curl', '-sS',
                'https://api.elevenlabs.io/v1/user',
                '-H', f'xi-api-key: {api_key}',
                '--connect-timeout', '10',
                '--max-time', '15'
            ]
            
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            if result.returncode != 0:
                logger.error(f"User info API failed: {result.stderr[:200]}")
                return None
            
            data = json.loads(result.stdout)
            subscription = data.get('subscription', {})
            
            return {
                'character_count': subscription.get('character_count', 0),
                'character_limit': subscription.get('character_limit', 0),
                'remaining': subscription.get('character_limit', 0) - subscription.get('character_count', 0),
                'tier': subscription.get('tier', 'free'),
                'status': subscription.get('status', 'active'),
                'is_valid': True
            }
            
        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return None
    
    def validate_voice_id_sync(self, api_key: str, voice_id: str) -> bool:
        """
        Validate if voice_id is accessible with this API key.
        Makes a minimal test TTS call to verify access.
        """
        try:
            test_payload = {
                "text": "Test",
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            }
            
            payload_json = json.dumps(test_payload, ensure_ascii=True, separators=(',', ':'))
            url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}'
            output_path = 'NUL' if os.name == 'nt' else '/dev/null'
            
            cmd = [
                'curl', '-sS',
                '-X', 'POST',
                '-H', f'xi-api-key: {api_key}',
                '-H', 'Content-Type: application/json',
                '-d', payload_json,
                '-o', output_path,
                '--max-time', '15',
                url
            ]
            
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=20,
                encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            return result.returncode == 0
            
        except Exception as e:
            logger.error(f"Error validating voice_id: {e}")
            return False
    
    def parse_api_error(self, response_text: str) -> Dict:
        """
        Parse API error response and return structured error info.
        Based on official ElevenLabs error codes.
        """
        try:
            text_lower = response_text.lower()
            
            # Try to parse as JSON first
            try:
                error_data = json.loads(response_text)
                error_code = error_data.get('status') or error_data.get('error_code')
                error_message = error_data.get('message') or error_data.get('detail')
                
                return {
                    'error_code': error_code,
                    'message': error_message,
                    'raw_response': response_text[:500]
                }
            except json.JSONDecodeError:
                pass
            
            # Fallback to pattern matching
            if 'max_character_limit_exceeded' in text_lower:
                return {'error_code': 'max_character_limit_exceeded', 'status': 400}
            elif 'invalid_api_key' in text_lower:
                return {'error_code': 'invalid_api_key', 'status': 401}
            elif 'quota_exceeded' in text_lower:
                return {'error_code': 'quota_exceeded', 'status': 401}
            elif 'voice_not_found' in text_lower:
                return {'error_code': 'voice_not_found', 'status': 401}
            elif 'only_for_creator' in text_lower:
                return {'error_code': 'only_for_creator+', 'status': 403}
            elif 'too_many_concurrent_requests' in text_lower:
                return {'error_code': 'too_many_concurrent_requests', 'status': 429}
            elif 'system_busy' in text_lower:
                return {'error_code': 'system_busy', 'status': 429}
            elif 'voice_limit_reached' in text_lower:
                return {'error_code': 'voice_limit_reached', 'status': 400}
            else:
                return {'error_code': 'unknown', 'message': response_text[:200]}
                
        except Exception as e:
            logger.error(f"Error parsing API error: {e}")
            return {'error_code': 'parse_error', 'message': str(e)}