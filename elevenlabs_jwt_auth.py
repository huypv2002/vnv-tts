#!/usr/bin/env python3
"""
ElevenLabs JWT Auto-Refresh
Tự động lấy và refresh JWT token từ Firebase Auth

Usage:
    from elevenlabs_jwt_auth import ElevenLabsAuth
    
    auth = ElevenLabsAuth()
    jwt = auth.get_jwt(email, password)  # Login lần đầu
    jwt = auth.refresh_token()           # Refresh khi hết hạn
"""

import requests
import json
import time
import os
from datetime import datetime
from typing import Optional

# Firebase API Key của ElevenLabs (public, dùng cho client auth)
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"

# Endpoints
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"

# Cache file
JWT_CACHE_FILE = "jwt_cache.json"


class ElevenLabsAuth:
    """Quản lý JWT token cho ElevenLabs"""
    
    def __init__(self, cache_file: str = JWT_CACHE_FILE):
        self.cache_file = cache_file
        self.jwt_token: Optional[str] = None
        self.refresh_token_str: Optional[str] = None
        self.expires_at: int = 0
        self.email: Optional[str] = None
        
        # Load cache nếu có
        self._load_cache()
    
    def _load_cache(self):
        """Load JWT từ cache file"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                self.jwt_token = data.get('jwt_token')
                self.refresh_token_str = data.get('refresh_token')
                self.expires_at = data.get('expires_at', 0)
                self.email = data.get('email')
                print(f"📦 Loaded JWT cache for {self.email}")
            except Exception as e:
                print(f"⚠️ Failed to load cache: {e}")
    
    def _save_cache(self):
        """Save JWT vào cache file"""
        try:
            data = {
                'jwt_token': self.jwt_token,
                'refresh_token': self.refresh_token_str,
                'expires_at': self.expires_at,
                'email': self.email,
                'updated_at': datetime.now().isoformat()
            }
            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"💾 Saved JWT cache")
        except Exception as e:
            print(f"⚠️ Failed to save cache: {e}")
    
    def is_token_valid(self) -> bool:
        """Kiểm tra token còn hạn không (buffer 5 phút)"""
        if not self.jwt_token:
            return False
        # Buffer 5 phút trước khi hết hạn
        return time.time() < (self.expires_at - 300)
    
    def login(self, email: str, password: str) -> Optional[str]:
        """
        Login bằng email/password để lấy JWT token
        
        Returns:
            JWT token nếu thành công, None nếu thất bại
        """
        print(f"🔐 Logging in as {email}...")
        
        url = f"{FIREBASE_AUTH_URL}:signInWithPassword?key={FIREBASE_API_KEY}"
        
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://elevenlabs.io/",
            "Origin": "https://elevenlabs.io",
        }
        
        payload = {
            "email": email,
            "password": password,
            "returnSecureToken": True
        }
        
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                self.jwt_token = data.get('idToken')
                self.refresh_token_str = data.get('refreshToken')
                # expiresIn là số giây, mặc định 3600 (1 giờ)
                expires_in = int(data.get('expiresIn', 3600))
                self.expires_at = int(time.time()) + expires_in
                self.email = email
                
                self._save_cache()
                
                exp_time = datetime.fromtimestamp(self.expires_at)
                print(f"✅ Login successful!")
                print(f"   Token expires: {exp_time}")
                
                return self.jwt_token
            else:
                error = resp.json().get('error', {})
                msg = error.get('message', 'Unknown error')
                print(f"❌ Login failed: {msg}")
                return None
                
        except Exception as e:
            print(f"❌ Login error: {e}")
            return None
    
    def refresh_token(self) -> Optional[str]:
        """
        Refresh JWT token bằng refresh_token
        Nhanh hơn login vì không cần password
        
        Returns:
            JWT token mới nếu thành công
        """
        if not self.refresh_token_str:
            print("❌ No refresh token available. Need to login first.")
            return None
        
        print("🔄 Refreshing JWT token...")
        
        url = f"{FIREBASE_TOKEN_URL}?key={FIREBASE_API_KEY}"
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://elevenlabs.io/",
            "Origin": "https://elevenlabs.io",
        }
        
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token_str
        }
        
        try:
            resp = requests.post(url, data=payload, headers=headers, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                self.jwt_token = data.get('id_token')
                self.refresh_token_str = data.get('refresh_token')
                expires_in = int(data.get('expires_in', 3600))
                self.expires_at = int(time.time()) + expires_in
                
                self._save_cache()
                
                exp_time = datetime.fromtimestamp(self.expires_at)
                print(f"✅ Token refreshed!")
                print(f"   New expiry: {exp_time}")
                
                return self.jwt_token
            else:
                print(f"❌ Refresh failed: {resp.text[:200]}")
                return None
                
        except Exception as e:
            print(f"❌ Refresh error: {e}")
            return None
    
    def get_jwt(self, email: str = None, password: str = None) -> Optional[str]:
        """
        Lấy JWT token (auto refresh nếu cần)
        
        Args:
            email: Email (chỉ cần lần đầu hoặc khi refresh fail)
            password: Password (chỉ cần lần đầu hoặc khi refresh fail)
        
        Returns:
            Valid JWT token
        """
        # Nếu token còn hạn, trả về luôn
        if self.is_token_valid():
            print(f"✅ Using cached JWT (expires: {datetime.fromtimestamp(self.expires_at)})")
            return self.jwt_token
        
        # Thử refresh trước (nhanh hơn)
        if self.refresh_token_str:
            jwt = self.refresh_token()
            if jwt:
                return jwt
        
        # Nếu refresh fail, login lại
        if email and password:
            return self.login(email, password)
        
        print("❌ Token expired and no credentials provided")
        return None


# ============== DEMO ==============
def main():
    print("="*60)
    print("🔑 ElevenLabs JWT Auto-Auth")
    print("="*60)
    
    auth = ElevenLabsAuth()
    
    # Kiểm tra cache
    if auth.is_token_valid():
        print(f"\n✅ Cached token still valid!")
        print(f"   JWT: {auth.jwt_token[:50]}...")
        return
    
    # Nhập credentials
    email = input("\nEmail: ").strip()
    password = input("Password: ").strip()
    
    if not email or not password:
        print("❌ Need email and password!")
        return
    
    # Login
    jwt = auth.get_jwt(email, password)
    
    if jwt:
        print(f"\n🎫 JWT Token:")
        print(f"   {jwt[:80]}...")
        print(f"\n📋 Full token saved to {JWT_CACHE_FILE}")


if __name__ == "__main__":
    main()
