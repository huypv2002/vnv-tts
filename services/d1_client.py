"""
Cloudflare D1 Client - Drop-in replacement for Supabase client
Provides same interface: .table().select().eq().execute()
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
import requests


# Configuration - Update these values after deploying Worker
D1_WORKER_URL = "https://tts-api.kh431248.workers.dev"
D1_API_KEY = "tts-d1-secret-key-2026"


@dataclass
class D1Response:
    """Response wrapper to match Supabase response format"""
    data: Union[Dict, List, None]
    count: Optional[int] = None
    error: Optional[str] = None


class QueryBuilder:
    """
    Query builder that mimics Supabase's fluent interface.
    Example: client.table("users").select("*").eq("id", 1).execute()
    """
    
    def __init__(self, client: 'D1Client', table: str):
        self._client = client
        self._table = table
        self._select_cols = "*"
        self._filters: List[tuple] = []
        self._limit: Optional[int] = None
        self._offset: Optional[int] = None
        self._order: Optional[str] = None
        self._single = False
        self._body: Optional[Dict] = None
        self._method = "GET"
    
    def select(self, columns: str = "*") -> 'QueryBuilder':
        """Select specific columns"""
        self._select_cols = columns
        self._method = "GET"
        return self
    
    def insert(self, data: Dict) -> 'QueryBuilder':
        """Insert a new record"""
        self._body = data
        self._method = "POST"
        return self
    
    def update(self, data: Dict) -> 'QueryBuilder':
        """Update records matching filters"""
        self._body = data
        self._method = "PATCH"
        return self
    
    def delete(self) -> 'QueryBuilder':
        """Delete records matching filters"""
        self._method = "DELETE"
        return self
    
    def eq(self, column: str, value: Any) -> 'QueryBuilder':
        """Filter: column = value"""
        self._filters.append((column, "eq", value))
        return self
    
    def neq(self, column: str, value: Any) -> 'QueryBuilder':
        """Filter: column != value"""
        self._filters.append((column, "neq", value))
        return self
    
    def gt(self, column: str, value: Any) -> 'QueryBuilder':
        """Filter: column > value"""
        self._filters.append((column, "gt", value))
        return self
    
    def gte(self, column: str, value: Any) -> 'QueryBuilder':
        """Filter: column >= value"""
        self._filters.append((column, "gte", value))
        return self
    
    def lt(self, column: str, value: Any) -> 'QueryBuilder':
        """Filter: column < value"""
        self._filters.append((column, "lt", value))
        return self
    
    def lte(self, column: str, value: Any) -> 'QueryBuilder':
        """Filter: column <= value"""
        self._filters.append((column, "lte", value))
        return self
    
    def like(self, column: str, pattern: str) -> 'QueryBuilder':
        """Filter: column LIKE pattern"""
        self._filters.append((column, "like", pattern))
        return self
    
    def ilike(self, column: str, pattern: str) -> 'QueryBuilder':
        """Filter: column ILIKE pattern (case insensitive)"""
        self._filters.append((column, "ilike", pattern))
        return self
    
    def is_(self, column: str, value: str) -> 'QueryBuilder':
        """Filter: column IS NULL / IS NOT NULL"""
        self._filters.append((column, "is", value))
        return self
    
    def in_(self, column: str, values: List) -> 'QueryBuilder':
        """Filter: column IN (values)"""
        self._filters.append((column, "in", ",".join(str(v) for v in values)))
        return self
    
    def order(self, column: str, desc: bool = False) -> 'QueryBuilder':
        """Order by column"""
        self._order = f"{column}.{'desc' if desc else 'asc'}"
        return self
    
    def limit(self, count: int) -> 'QueryBuilder':
        """Limit number of results"""
        self._limit = count
        return self
    
    def offset(self, count: int) -> 'QueryBuilder':
        """Offset results (for pagination)"""
        self._offset = count
        return self
    
    def single(self) -> 'QueryBuilder':
        """Expect single result"""
        self._single = True
        self._limit = 1
        return self
    
    def _build_url(self) -> str:
        """Build the request URL with query params"""
        url = f"{self._client._base_url}/rest/v1/{self._table}"
        params = []
        
        if self._method == "GET":
            params.append(f"select={self._select_cols}")
        
        for col, op, val in self._filters:
            # Convert boolean to integer for SQLite compatibility
            if isinstance(val, bool):
                val = 1 if val else 0
            
            if op == "eq":
                params.append(f"{col}={val}")
            else:
                params.append(f"{col}.{op}={val}")
        
        if self._limit:
            params.append(f"limit={self._limit}")
        if self._offset:
            params.append(f"offset={self._offset}")
        if self._order:
            params.append(f"order={self._order}")
        
        if params:
            url += "?" + "&".join(params)
        
        return url
    
    def execute(self) -> D1Response:
        """Execute the query and return response"""
        url = self._build_url()
        headers = {
            "X-API-Key": self._client._api_key,
            "Content-Type": "application/json",
        }
        
        try:
            if self._method == "GET":
                resp = requests.get(url, headers=headers, timeout=30)
            elif self._method == "POST":
                resp = requests.post(url, headers=headers, json=self._body, timeout=30)
            elif self._method == "PATCH":
                resp = requests.patch(url, headers=headers, json=self._body, timeout=30)
            elif self._method == "DELETE":
                resp = requests.delete(url, headers=headers, timeout=30)
            else:
                return D1Response(data=None, error=f"Unknown method: {self._method}")
            
            if not resp.ok:
                return D1Response(data=None, error=f"HTTP {resp.status_code}: {resp.text}")
            
            result = resp.json()
            data = result.get("data")
            
            # Handle single() - return first item or None
            if self._single:
                if isinstance(data, list):
                    data = data[0] if data else None
            
            return D1Response(data=data, count=result.get("count"))
            
        except requests.exceptions.Timeout:
            return D1Response(data=None, error="Request timeout")
        except requests.exceptions.RequestException as e:
            return D1Response(data=None, error=str(e))
        except Exception as e:
            return D1Response(data=None, error=str(e))


class D1Client:
    """
    D1 Database client with Supabase-compatible interface.
    Singleton pattern for connection reuse.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, base_url: str = None, api_key: str = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, base_url: str = None, api_key: str = None):
        if hasattr(self, '_initialized'):
            return
        
        self._base_url = (base_url or D1_WORKER_URL).rstrip('/')
        self._api_key = api_key or D1_API_KEY
        self._initialized = True
        
        print("✅ D1 client initialized (singleton pattern)")
    
    def table(self, name: str) -> QueryBuilder:
        """Start a query on a table"""
        return QueryBuilder(self, name)
    
    def rpc(self, function: str, params: Dict = None) -> D1Response:
        """Call an RPC function"""
        url = f"{self._base_url}/rpc"
        headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }
        body = {
            "function": function,
            "params": params or {},
        }
        
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            
            if not resp.ok:
                return D1Response(data=None, error=f"HTTP {resp.status_code}: {resp.text}")
            
            result = resp.json()
            return D1Response(data=result.get("data"))
            
        except Exception as e:
            return D1Response(data=None, error=str(e))
    
    def health_check(self) -> bool:
        """Check if the D1 worker is healthy"""
        try:
            url = f"{self._base_url}/health"
            headers = {"X-API-Key": self._api_key}
            resp = requests.get(url, headers=headers, timeout=10)
            return resp.ok
        except:
            return False


class D1Auth:
    """
    D1 Authentication - replacement for SupabaseAuth.
    Provides same interface for sign_in_custom_user_table.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self._client = D1Client()
        self._current_user: Optional[Dict] = None
        self._initialized = True
        
        print("✅ D1 Auth initialized")
    
    @property
    def client(self) -> D1Client:
        return self._client
    
    @property
    def supabase(self) -> D1Client:
        """Alias for compatibility"""
        return self._client
    
    def sign_in_custom_user_table(self, username: str, password: str) -> Optional[Dict]:
        """Authenticate against 'users' table"""
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
            
            self._current_user = {
                'id': user['id'],
                'username': user['username'],
                'role': user['role']
            }
            
            return user
            
        except Exception as e:
            print(f"❌ D1 Sign in error: {e}")
            return None
    
    def get_current_user(self) -> Optional[Dict]:
        """Get currently logged in user"""
        return self._current_user
    
    def sign_out(self) -> None:
        """Sign out current user"""
        self._current_user = None


# Convenience function to get D1 client (matches Supabase pattern)
def get_d1_client() -> D1Client:
    """Get singleton D1 client instance"""
    return D1Client()


def get_d1_auth() -> D1Auth:
    """Get singleton D1 auth instance"""
    return D1Auth()
