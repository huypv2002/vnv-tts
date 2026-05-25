from __future__ import annotations

import base64
import gzip
import hashlib
from datetime import datetime, timedelta
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)


class KeyFileManager:
    """Service for managing API key files upload and storage"""
    
    def __init__(self, supabase_client):
        self.supabase = supabase_client
    
    def upload_key_file(self, user_id: int, file_path: str, file_name: str) -> Dict:
        """
        Upload and process API key file for user
        Returns: {success: bool, message: str, file_id: int}
        """
        try:
            # Check user subscription status
            if not self._check_user_subscription(user_id):
                return {
                    'success': False,
                    'message': 'User subscription expired or inactive',
                    'file_id': None
                }
            
            # Read and validate file
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            keys = [line.strip() for line in content.split('\n') if line.strip()]
            
            if not keys:
                return {
                    'success': False,
                    'message': 'File contains no valid API keys',
                    'file_id': None
                }
            
            if len(keys) > 2000:
                return {
                    'success': False,
                    'message': 'File contains too many keys (max 2000)',
                    'file_id': None
                }
            
            # Encrypt and compress content
            encrypted_content = self._encrypt_content(content)
            
            # Generate file hash
            file_hash = hashlib.md5(content.encode()).hexdigest()
            
            # Check if file already exists
            existing = self.supabase.table('user_key_files')\
                .select('id')\
                .eq('user_id', user_id)\
                .eq('file_hash', file_hash)\
                .execute()
            
            if existing.data:
                return {
                    'success': False,
                    'message': 'File already uploaded',
                    'file_id': existing.data[0]['id']
                }
            
            # Get user subscription expiry
            expires_at = self._get_user_subscription_expiry(user_id)
            
            # Insert file record
            result = self.supabase.table('user_key_files')\
                .insert({
                    'user_id': user_id,
                    'file_name': file_name,
                    'total_keys': len(keys),
                    'active_keys': 0,
                    'file_hash': file_hash,
                    'encrypted_content': encrypted_content,
                    'expires_at': expires_at.isoformat() if expires_at else None,
                    'is_active': True
                })\
                .execute()
            
            if result.data:
                file_id = result.data[0]['id']
                logger.info(f"Successfully uploaded key file for user {user_id}: {len(keys)} keys")
                
                return {
                    'success': True,
                    'message': f'Successfully uploaded {len(keys)} API keys',
                    'file_id': file_id
                }
            else:
                return {
                    'success': False,
                    'message': 'Failed to save file to database',
                    'file_id': None
                }
                
        except Exception as e:
            logger.error(f"Error uploading key file: {e}")
            return {
                'success': False,
                'message': f'Error uploading file: {str(e)}',
                'file_id': None
            }
    
    def _encrypt_content(self, content: str) -> str:
        """Encrypt and compress file content"""
        try:
            # Compress first
            compressed = gzip.compress(content.encode('utf-8'))
            
            # Base64 encode (simple encryption - you can add proper encryption here)
            encrypted = base64.b64encode(compressed).decode('ascii')
            
            return encrypted
            
        except Exception as e:
            logger.error(f"Error encrypting content: {e}")
            raise
    
    def _check_user_subscription(self, user_id: int) -> bool:
        """Check if user has active subscription"""
        try:
            subscription = self.supabase.table('user_subscriptions')\
                .select('is_active, end_date')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .execute()
            
            if not subscription.data:
                return False
            
            sub = subscription.data[0]
            
            # Check if subscription is still valid
            if sub['end_date']:
                end_date = datetime.fromisoformat(sub['end_date'].replace('Z', '+00:00'))
                if datetime.now() > end_date:
                    # Mark subscription as inactive
                    self.supabase.table('user_subscriptions')\
                        .update({'is_active': False})\
                        .eq('user_id', user_id)\
                        .execute()
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking user subscription: {e}")
            return False
    
    def _get_user_subscription_expiry(self, user_id: int) -> Optional[datetime]:
        """Get user subscription expiry date"""
        try:
            subscription = self.supabase.table('user_subscriptions')\
                .select('end_date')\
                .eq('user_id', user_id)\
                .eq('is_active', True)\
                .execute()
            
            if subscription.data and subscription.data[0]['end_date']:
                return datetime.fromisoformat(subscription.data[0]['end_date'].replace('Z', '+00:00'))
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting subscription expiry: {e}")
            return None
    
    def cleanup_expired_files(self) -> int:
        """Clean up expired key files and their active keys"""
        try:
            # Get expired files
            expired_files = self.supabase.table('user_key_files')\
                .select('id, user_id')\
                .eq('is_active', True)\
                .lt('expires_at', datetime.now().isoformat())\
                .execute()
            
            if not expired_files.data:
                return 0
            
            expired_count = 0
            
            for file_data in expired_files.data:
                file_id = file_data['id']
                
                # Delete active keys for this file
                self.supabase.table('active_api_keys')\
                    .delete()\
                    .eq('key_file_id', file_id)\
                    .execute()
                
                # Mark file as inactive
                self.supabase.table('user_key_files')\
                    .update({'is_active': False})\
                    .eq('id', file_id)\
                    .execute()
                
                expired_count += 1
                logger.info(f"Cleaned up expired key file {file_id} for user {file_data['user_id']}")
            
            return expired_count
            
        except Exception as e:
            logger.error(f"Error cleaning up expired files: {e}")
            return 0
    
    def get_user_key_files_info(self, user_id: int) -> List[Dict]:
        """Get information about user's key files"""
        try:
            files = self.supabase.table('user_key_files')\
                .select('id, file_name, total_keys, active_keys, created_at, expires_at, is_active')\
                .eq('user_id', user_id)\
                .order('created_at', desc=True)\
                .execute()
            
            result = []
            for file_data in files.data:
                result.append({
                    'id': file_data['id'],
                    'file_name': file_data['file_name'],
                    'total_keys': file_data['total_keys'],
                    'active_keys': file_data['active_keys'],
                    'remaining_keys': file_data['total_keys'] - file_data['active_keys'],
                    'created_at': file_data['created_at'],
                    'expires_at': file_data['expires_at'],
                    'is_active': file_data['is_active'],
                    'status': self._get_file_status(file_data)
                })
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting user key files info: {e}")
            return []
    
    def _get_file_status(self, file_data: Dict) -> str:
        """Get human readable status for key file"""
        if not file_data['is_active']:
            return 'Inactive'
        
        if file_data['expires_at']:
            expires_at = datetime.fromisoformat(file_data['expires_at'].replace('Z', '+00:00'))
            if datetime.now() > expires_at:
                return 'Expired'
        
        remaining = file_data['total_keys'] - file_data['active_keys']
        if remaining <= 0:
            return 'Exhausted'
        
        return f'Active ({remaining} keys remaining)'
