"""
VNV TTS Tool - Cloudflare D1 Client
Cloned from main app, points to separate VNV database
"""
from __future__ import annotations
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
import requests

from .db_config import D1_WORKER_URL, D1_API_KEY


@dataclass
class D1Response:
    """Response wrapper to match Supabase response format"""
    data: Union[Dict, List, None]
    count: Optional[int] = None
    error: Optional[str] = None


class QueryBuilder:
    def __init__(self, client: 'D1Client', table: str):
        self._client = client
        self._table = table
        self._method = 'GET'
        self._select_cols = '*'
        self._filters: List[Dict] = []
        self._order_col = None
        self._order_desc = False
        self._limit_val = None
        self._offset_val = None
        self._single = False
        self._body = None

    def select(self, columns: str = "*") -> 'QueryBuilder':
        self._select_cols = columns
        return self

    def insert(self, data: Dict) -> 'QueryBuilder':
        self._method = 'POST'
        self._body = data
        return self

    def update(self, data: Dict) -> 'QueryBuilder':
        self._method = 'PATCH'
        self._body = data
        return self

    def delete(self) -> 'QueryBuilder':
        self._method = 'DELETE'
        return self

    def eq(self, column: str, value: Any) -> 'QueryBuilder':
        self._filters.append({'column': column, 'op': 'eq', 'value': str(value)})
        return self

    def neq(self, column: str, value: Any) -> 'QueryBuilder':
        self._filters.append({'column': column, 'op': 'neq', 'value': str(value)})
        return self

    def gt(self, column: str, value: Any) -> 'QueryBuilder':
        self._filters.append({'column': column, 'op': 'gt', 'value': str(value)})
        return self

    def gte(self, column: str, value: Any) -> 'QueryBuilder':
        self._filters.append({'column': column, 'op': 'gte', 'value': str(value)})
        return self

    def lt(self, column: str, value: Any) -> 'QueryBuilder':
        self._filters.append({'column': column, 'op': 'lt', 'value': str(value)})
        return self

    def lte(self, column: str, value: Any) -> 'QueryBuilder':
        self._filters.append({'column': column, 'op': 'lte', 'value': str(value)})
        return self

    def order(self, column: str, desc: bool = False) -> 'QueryBuilder':
        self._order_col = column
        self._order_desc = desc
        return self

    def limit(self, count: int) -> 'QueryBuilder':
        self._limit_val = count
        return self

    def single(self) -> 'QueryBuilder':
        self._single = True
        return self

    def _build_url(self) -> str:
        url = f"{self._client._base_url}/rest/v1/{self._table}"
        params = []
        params.append(f"select={self._select_cols}")
        for f in self._filters:
            params.append(f"{f['column']}={f['op']}.{f['value']}")
        if self._order_col:
            direction = 'desc' if self._order_desc else 'asc'
            params.append(f"order={self._order_col}.{direction}")
        if self._limit_val is not None:
            params.append(f"limit={self._limit_val}")
        if self._single:
            params.append("single=true")
        if params:
            url += '?' + '&'.join(params)
        return url

    def execute(self) -> D1Response:
        url = self._build_url()
        headers = {
            'Content-Type': 'application/json',
            'x-api-key': self._client._api_key,
        }
        try:
            if self._method == 'GET':
                resp = requests.get(url, headers=headers, timeout=15)
            elif self._method == 'POST':
                resp = requests.post(url, headers=headers, json=self._body, timeout=15)
            elif self._method in ('PATCH', 'PUT'):
                resp = requests.patch(url, headers=headers, json=self._body, timeout=15)
            elif self._method == 'DELETE':
                resp = requests.delete(url, headers=headers, timeout=15)
            else:
                return D1Response(data=None, error=f"Unknown method: {self._method}")

            if resp.status_code >= 400:
                return D1Response(data=None, error=f"HTTP {resp.status_code}: {resp.text}")

            data = resp.json()
            return D1Response(data=data)
        except Exception as e:
            return D1Response(data=None, error=str(e))


class D1Client:
    """Singleton D1 client for VNV TTS Tool"""
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
        print("✅ VNV D1 client initialized")

    def table(self, name: str) -> QueryBuilder:
        return QueryBuilder(self, name)

    def rpc(self, function: str, params: Dict = None) -> D1Response:
        url = f"{self._base_url}/rpc"
        headers = {'Content-Type': 'application/json', 'x-api-key': self._api_key}
        try:
            resp = requests.post(url, headers=headers, json={'function': function, 'params': params or {}}, timeout=15)
            if resp.status_code >= 400:
                return D1Response(data=None, error=f"HTTP {resp.status_code}: {resp.text}")
            return D1Response(data=resp.json())
        except Exception as e:
            return D1Response(data=None, error=str(e))

    def health_check(self) -> bool:
        try:
            resp = requests.get(f"{self._base_url}/health",
                                headers={'x-api-key': self._api_key}, timeout=10)
            return resp.ok
        except:
            return False


class D1Auth:
    """Authentication service for VNV TTS Tool"""
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
        print("✅ VNV D1 Auth initialized")

    @property
    def client(self) -> D1Client:
        return self._client

    @property
    def supabase(self) -> D1Client:
        return self._client

    def sign_in_custom_user_table(self, username: str, password: str) -> Optional[Dict]:
        """Authenticate against 'users' table"""
        try:
            result = self._client.table("users") \
                .select("id, username, password, role") \
                .eq("username", username) \
                .single() \
                .execute()

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
            print(f"❌ VNV Sign in error: {e}")
            return None

    def get_current_user(self) -> Optional[Dict]:
        return self._current_user

    def sign_out(self) -> None:
        self._current_user = None
