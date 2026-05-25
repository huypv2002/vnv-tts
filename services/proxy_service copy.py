# services/proxy_service.py

from __future__ import annotations
import os, uuid, subprocess, requests
from typing import Optional, Dict

class ProxyService:
    """
    Khởi tạo kiểu bạn đang dùng:
      ProxyService(supabase=<supabase>, user_id=<int>)
    -> tự đọc bảng users_token_proxy: cột port, reset_link, token_proxy (optional)
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | str | None = None,
        username: str | None = None,
        password: str | None = None,
        rotate_url: str | None = None,
        supabase=None,
        user_id: int | None = None,
    ) -> None:
        self.supabase = supabase
        self.user_id = user_id
        self._proxy_row: Optional[Dict] = None   # raw row từ DB
        self._cfg: Optional[Dict[str, str]] = None  # {host,port,username,password}
        self.rotate_url: Optional[str] = rotate_url

        # Nếu truyền trực tiếp host/port/user/pass thì dùng luôn
        if host and port and username and password:
            self._cfg = {
                "host": str(host),
                "port": str(port),
                "username": str(username),
                "password": str(password),
            }

    # ---- internal helpers ----
    def _parse_port_field(self, s: str) -> Optional[Dict[str, str]]:
        # "sp07v2-03.proxygenz.com:37486:sp07v2-37486:MJMQT"
        parts = (s or "").split(":")
        if len(parts) < 4:
            return None
        return {
            "host": parts[0],
            "port": str(parts[1]),
            "username": parts[2],
            "password": parts[3],
        }

    # ---- DB load ----
    def _load_from_db(self) -> Optional[Dict]:
        if not (self.supabase and self.user_id):
            return None
        
        # Use safe database operation with retry logic
        def _execute_query():
            return (self.supabase.table("users_token_proxy")
                   .select("*")
                   .eq("user_id", self.user_id)
                   .limit(1)
                   .execute())
        
        try:
            from services.db_retry_helper import safe_db_operation
            res = safe_db_operation(_execute_query, max_retries=3, default_return=None)
            row = (res.data or [None])[0] if res else None
            self._proxy_row = row
            if row and not self._cfg:
                parsed = self._parse_port_field(row.get("port", ""))
                if parsed:
                    self._cfg = parsed
            # Ưu tiên rotate_url truyền trực tiếp; nếu chưa có thì dùng reset_link trong DB
            if row and not self.rotate_url:
                self.rotate_url = row.get("reset_link") or row.get("rotate_link") or row.get("rotate_url")
            return row
        except Exception as e:
            print(f"❌ load users_token_proxy error: {e}")
            self._proxy_row = None
            return None

    # ---- public API ----
    def get_proxy_config(self) -> Optional[Dict[str, str]]:
        if self._cfg:
            return dict(self._cfg)
        if self._load_from_db() and self._cfg:
            return dict(self._cfg)
        return None

    def get_token_proxy(self) -> Optional[str]:
        # Optional: nếu bạn muốn dùng token_proxy cho analytics/log/whatever
        if not self._proxy_row:
            self._load_from_db()
        return (self._proxy_row or {}).get("token_proxy")

    def rotate_proxy_via_php(self) -> str:
        if not self.rotate_url:
            self._load_from_db()
        if not self.rotate_url:
            raise RuntimeError("rotate_url/reset_link is not configured")

        # gọi cứng bằng curl để tránh requests bị noise môi trường
        url = f"{self.rotate_url}?t={uuid.uuid4().hex}"
        cmd = ["curl", "-sS", "-m", "12", "-A", "Mozilla/5.0", url]
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        if p.returncode != 0:
            raise RuntimeError(f"ROTATE_HTTP_FAIL rc={p.returncode} stderr={(p.stderr or '')[:160]}")
        return (p.stdout or "OK").strip()[:400]

    def probe_ip_via_proxy(self) -> Optional[str]:
        """Lấy IP hiện tại qua proxy (ipify), chống cache bằng query random."""
        cfg = self.get_proxy_config()
        if not cfg:
            return None
        cmd = [
            "curl", "-sS",
            f"https://api.ipify.org?t={uuid.uuid4().hex}",
            "-x", f"http://{cfg['host']}:{cfg['port']}",
            "--connect-timeout", "8", "--max-time", "12",
            "-H", "Cache-Control: no-cache", "-H", "Pragma: no-cache",
        ]
        if cfg.get("username"):
            cmd += ["--proxy-user", f"{cfg['username']}:{cfg['password']}"]
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        ip = (p.stdout or "").strip()
        if p.returncode == 0 and ip:
            return ip
        return None
