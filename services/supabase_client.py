from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass
from typing import Optional

from supabase import create_client, Client
import requests


def _deobfuscate(obf: str) -> str:
    # Very light obfuscation to avoid plain-text strings in binary
    return base64.b64decode(obf.encode("utf-8")).decode("utf-8")


def _concat(parts: list[str]) -> str:
    return "".join(parts)


# NOTE: Values adapted from gg.py. Lightly obfuscated by splitting.
_URL_PARTS = [
    "https://",
    "qrieqjcplmnka",
    "puysnyf",
    ".supabase.co",
]

_ANON_PARTS = [
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.",
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFyaWVxamNwbG1ua2FwdXlzbnlmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTk1MDY2OTUsImV4cCI6MjA3NTA4MjY5NX0.",
    "nqhrBL8rRn94ABn5FYnUrM4_OVPoc7P79dz8RFC70bA",
]

def _get_url() -> str:
    return _concat(_URL_PARTS)

def _get_anon() -> str:
    return _concat(_ANON_PARTS)
_OBF_REMOTE_CFG = ""  # base64 of optional https://your-domain.tld/app-config.json


def _try_fetch_remote_config() -> Optional[tuple[str, str]]:
    if not _OBF_REMOTE_CFG:
        return None
    try:
        url = _deobfuscate(_OBF_REMOTE_CFG)
        resp = requests.get(url, timeout=10)
        if resp.ok:
            data = resp.json()
            su = data.get("supabase_url")
            ak = data.get("supabase_anon_key")
            if su and ak:
                return su, ak
    except Exception:
        return None
    return None


def get_runtime_config() -> tuple[str, str]:
    # Try remote config first, then fallback to local split-string join.
    remote = _try_fetch_remote_config()
    if remote:
        return remote
    return _get_url(), _get_anon()


@dataclass
class AuthSession:
    access_token: str
    refresh_token: str


class SupabaseAuth:
    """
    Supabase authentication with connection pooling (Phase 8).
    Singleton pattern ensures connection reuse across the application.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Singleton pattern to reuse connection"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        # Only initialize once
        if hasattr(self, '_initialized'):
            return
        
        url, anon = get_runtime_config()
        self._client: Client = create_client(url, anon)
        self._current_user: Optional[dict] = None
        self._initialized = True
        
        print("✅ Supabase connection pool initialized (singleton pattern)")

    @property
    def client(self) -> Client:
        return self._client
    
    @property
    def supabase(self) -> Client:
        """Alias for client to match expected interface"""
        return self._client

    def sign_in(self, email: str, password: str) -> Optional[AuthSession]:
        # Keep email login available if needed later
        resp = self._client.auth.sign_in_with_password({"email": email, "password": password})
        if resp and resp.session and resp.session.access_token and resp.session.refresh_token:
            return AuthSession(
                access_token=resp.session.access_token,
                refresh_token=resp.session.refresh_token,
            )
        return None

    def sign_in_custom_user_table(self, username: str, password: str) -> Optional[dict]:
        """Authenticate against 'users' table with plain password comparison.

        Returns the user record or None.
        """
        try:
            from services.db_retry_helper import safe_db_operation
            
            def _execute_query():
                return (
                    self._client.table("users")
                    .select("id, username, password, role")
                    .eq("username", username)
                    .single()
                    .execute()
                )
            
            result = safe_db_operation(_execute_query, max_retries=3, default_return=None)
            if not result or not result.data:
                return None
                
            user = result.data
            if password != user.get("password"):
                return None
            
            # Store current user info (without password)
            self._current_user = {
                'id': user['id'],
                'username': user['username'],
                'role': user['role']
            }
            
            return user
        except Exception as e:
            print(f"❌ Sign in error: {e}")
            return None

    def get_current_user(self) -> Optional[dict]:
        """Get currently logged in user information"""
        return self._current_user
    
    def sign_out(self) -> None:
        try:
            self._client.auth.sign_out()
            self._current_user = None  # Clear current user
        except Exception:
            pass


