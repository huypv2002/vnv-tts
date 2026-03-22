"""
VPS TTS Server - FastAPI
Chạy trên Windows VPS, expose qua Cloudflare Tunnel
Xử lý TTS với ElevenLabs API + JWT accounts + Proxy rotation
"""
import os
import sys
import json
import time
import uuid
import asyncio
import threading
import tempfile
import shutil
from pathlib import Path
from typing import Optional, List, Dict
from contextlib import asynccontextmanager

import requests
import uvicorn
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
SERVER_API_KEY = os.environ.get("SERVER_API_KEY", "vps-tts-secret-2026")
D1_WORKER_URL  = os.environ.get("D1_WORKER_URL", "https://tts-api.kh431248.workers.dev")
D1_API_KEY     = os.environ.get("D1_API_KEY", "tts-d1-secret-key-2026")
OUTPUT_DIR     = Path(os.environ.get("OUTPUT_DIR", "./outputs"))
OUTPUT_DIR.mkdir(exist_ok=True)

# ── D1 helper ────────────────────────────────────────────────────────────────
def d1_query(table: str, method: str = "GET", filters: dict = None, body: dict = None):
    headers = {"X-API-Key": D1_API_KEY, "Content-Type": "application/json"}
    params = []
    if method == "GET":
        params.append("select=*")
        if filters:
            for k, v in filters.items():
                params.append(f"{k}={v}")
    url = f"{D1_WORKER_URL}/rest/v1/{table}"
    if params:
        url += "?" + "&".join(params)
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        elif method == "POST":
            r = requests.post(url, headers=headers, json=body, timeout=15)
        elif method == "PATCH":
            r = requests.patch(url, headers=headers, json=body, timeout=15)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as e:
        print(f"❌ D1 error: {e}")
        return None

def d1_rpc(func: str, params: dict = None):
    headers = {"X-API-Key": D1_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{D1_WORKER_URL}/rpc",
            headers=headers,
            json={"function": func, "params": params or {}},
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"❌ D1 RPC error: {e}")
        return None

# ── Job store (in-memory) ────────────────────────────────────────────────────
jobs: Dict[str, dict] = {}
job_lock = threading.Lock()

def create_job(job_id: str, total: int):
    with job_lock:
        jobs[job_id] = {
            "id": job_id, "status": "running",
            "total": total, "done": 0, "failed": 0,
            "chunks": {}, "logs": [], "output_file": None,
            "created_at": time.time()
        }

def update_job(job_id: str, **kwargs):
    with job_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)

def job_log(job_id: str, msg: str):
    with job_lock:
        if job_id in jobs:
            jobs[job_id]["logs"].append({"t": time.time(), "msg": msg})
            print(f"[{job_id[:8]}] {msg}")

# ── Proxy Pool ───────────────────────────────────────────────────────────────
PROXYXOAY_API = "https://proxyxoay.shop/api/get.php"

class ProxyPool:
    """
    Quản lý proxy list từ gateway.
    Hỗ trợ format:
    - "proxyxoay:KEY" → fetch IP từ proxyxoay.shop API (TTL 60s)
    - "user:pass@host:port" hoặc "host:port" → dùng trực tiếp
    """
    def __init__(self, proxy_list: List[str]):
        self._proxies = proxy_list  # raw strings từ gateway
        self._cache: Dict[str, tuple] = {}  # key → (url, expire_ts)
        self._lock = threading.Lock()
        self._idx = 0

    def _fetch_proxyxoay(self, api_key: str) -> Optional[str]:
        try:
            r = requests.get(
                f"{PROXYXOAY_API}?key={api_key}&nhamang=random&tinhthanh=random",
                timeout=10
            )
            data = r.json()
            if data.get("status") == 100:
                ph = data.get("proxyhttp", "")
                parts = ph.split(":")
                if len(parts) >= 4:
                    return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                elif len(parts) == 2:
                    return f"http://{parts[0]}:{parts[1]}"
            print(f"⚠️ Proxyxoay: {data.get('message','?')}")
        except Exception as e:
            print(f"❌ Proxyxoay fetch error: {e}")
        return None

    def _resolve(self, raw: str, force_refresh: bool = False) -> Optional[str]:
        """Resolve raw proxy string to usable URL."""
        if raw.startswith("proxyxoay:"):
            api_key = raw[len("proxyxoay:"):]
            with self._lock:
                cached = self._cache.get(api_key)
                if cached and not force_refresh and time.time() < cached[1]:
                    return cached[0]
            url = self._fetch_proxyxoay(api_key)
            if url:
                with self._lock:
                    self._cache[api_key] = (url, time.time() + 60)
                return url
            # fallback to stale cache
            with self._lock:
                cached = self._cache.get(api_key)
                return cached[0] if cached else None
        else:
            # Regular proxy string — parse to http://...
            p = raw.strip()
            if "://" in p:
                return p
            if "@" in p:
                return f"http://{p}"
            parts = p.split(":")
            if len(parts) == 4:
                return f"http://{parts[0]}:{parts[1]}@{parts[2]}:{parts[3]}"
            return f"http://{p}"

    def get(self, rotate: bool = False) -> Optional[str]:
        """Get current proxy URL. rotate=True → advance to next."""
        if not self._proxies:
            return None
        with self._lock:
            if rotate:
                self._idx = (self._idx + 1) % len(self._proxies)
            raw = self._proxies[self._idx % len(self._proxies)]
        return self._resolve(raw)

    def rotate_and_refresh(self) -> Optional[str]:
        """Switch to next proxy, force-refresh if proxyxoay."""
        if not self._proxies:
            return None
        with self._lock:
            self._idx = (self._idx + 1) % len(self._proxies)
            raw = self._proxies[self._idx % len(self._proxies)]
        return self._resolve(raw, force_refresh=True)

# ── ElevenLabs JWT TTS ───────────────────────────────────────────────────────
ELEVENLABS_BASE = "https://api.elevenlabs.io"
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys")

def refresh_jwt_token(refresh_token: str) -> Optional[str]:
    """Call Firebase token refresh endpoint. Returns new jwt_token or None."""
    if not refresh_token:
        return None
    try:
        r = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
            headers={
                "Content-Type": "application/json",
                "Referer": "https://elevenlabs.io",
                "Origin": "https://elevenlabs.io",
            },
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            new_token = data.get("id_token")
            if new_token:
                print(f"✅ JWT refreshed via Firebase (token len={len(new_token)})")
                return new_token
            print(f"❌ Firebase refresh: no id_token in response: {r.text[:200]}")
            return None
        print(f"❌ Firebase refresh failed {r.status_code}: {r.text[:300]}")
        return None
    except Exception as e:
        print(f"❌ Firebase refresh error: {e}")
        return None

def update_jwt_in_d1(email: str, new_jwt: str):
    """Update jwt_token in D1 for given email."""
    try:
        headers = {"X-API-Key": D1_API_KEY, "Content-Type": "application/json"}
        r = requests.patch(
            f"{D1_WORKER_URL}/rest/v1/jwt_accounts?email={email}",
            headers=headers,
            json={"jwt_token": new_jwt},
            timeout=10
        )
        if r.status_code == 200:
            print(f"✅ D1 JWT updated for {email[:25]}")
        else:
            print(f"⚠️ D1 JWT update failed {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"❌ D1 JWT update error: {e}")

def elevenlabs_tts_jwt(jwt_token: str, voice_id: str, text: str,
                        model_id: str = "eleven_turbo_v2_5",
                        stability: float = 0.5, similarity: float = 0.75,
                        proxy: str = None) -> tuple[Optional[bytes], str]:
    """Call ElevenLabs TTS with JWT token. Returns (audio_bytes, error_type)
    error_type: '' | 'expired' | 'rate_limit' | 'error'
    """
    # Sanitize token — strip whitespace/newlines that corrupt the header
    jwt_token = jwt_token.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if not jwt_token:
        return None, 'expired'

    url = f"{ELEVENLABS_BASE}/v1/text-to-speech/{voice_id}"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": stability, "similarity_boost": similarity}
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=60)
        if r.status_code == 200:
            return r.content, ''
        body = r.text[:300]
        print(f"❌ EL JWT {r.status_code}: {body}")
        if r.status_code in (401, 403):
            if "unusual_activity" in body or "unusual activity" in body.lower():
                return None, 'unusual_activity'
            return None, 'expired'
        if r.status_code == 429:
            return None, 'rate_limit'
        return None, 'error'
    except Exception as e:
        print(f"❌ EL JWT error: {e}")
        return None, 'error'

def elevenlabs_tts_apikey(api_key: str, voice_id: str, text: str,
                           model_id: str = "eleven_turbo_v2_5",
                           stability: float = 0.5, similarity: float = 0.75,
                           proxy: str = None) -> tuple[Optional[bytes], str]:
    """Call ElevenLabs TTS with API key. Returns (audio_bytes, error_type)"""
    url = f"{ELEVENLABS_BASE}/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": stability, "similarity_boost": similarity}
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.post(url, headers=headers, json=payload, proxies=proxies, timeout=60)
        if r.status_code == 200:
            return r.content, ''
        body = r.text[:300]
        print(f"❌ EL API key {r.status_code}: {body}")
        if r.status_code == 401:
            if "unusual_activity" in body or "unusual activity" in body.lower():
                return None, 'unusual_activity'
            return None, 'dead'
        if r.status_code == 429:
            return None, 'rate_limit'
        return None, 'error'
    except Exception as e:
        print(f"❌ EL API key error: {e}")
        return None, 'error'

# ── Text splitting ────────────────────────────────────────────────────────────
def split_text(text: str, max_chars: int = 300) -> List[str]:
    """Split text into chunks ≤ max_chars, respecting sentence boundaries"""
    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []
    
    chunks = []
    # Split by newlines first
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    
    for para in paragraphs:
        if len(para) <= max_chars:
            chunks.append(para)
        else:
            # Split by sentence endings
            import re
            sentences = re.split(r'(?<=[.!?。！？])\s+', para)
            current = ""
            for s in sentences:
                if len(current) + len(s) + 1 <= max_chars:
                    current = (current + " " + s).strip()
                else:
                    if current:
                        chunks.append(current)
                    current = s[:max_chars]
            if current:
                chunks.append(current)
    
    return [c for c in chunks if c.strip()]

def merge_mp3_files(files: List[str], output: str) -> bool:
    """Merge multiple mp3 files using binary concat (simple, no ffmpeg needed)"""
    try:
        with open(output, "wb") as out:
            for f in files:
                with open(f, "rb") as inp:
                    out.write(inp.read())
        return True
    except Exception as e:
        print(f"❌ Merge error: {e}")
        return False

# ── Background TTS job ────────────────────────────────────────────────────────
def tts_one_chunk(chunk: str, voice_id: str, model_id: str,
                  jwt_accounts: List[dict], api_keys: List[str],
                  proxy_list: List[str], stability: float, similarity: float,
                  job_id: str, chunk_num: int) -> Optional[bytes]:
    """Try all JWT accounts (with auto-refresh) then API keys. Returns audio bytes or None.
    Proxy rotation: switch proxy on unusual_activity (free tier block), rate_limit.
    """
    pool = ProxyPool(proxy_list)

    # Try each JWT account — auto-refresh if expired
    for acc in jwt_accounts:
        jwt = acc.get("jwt_token") or acc.get("token")
        if not jwt:
            continue
        email = acc.get("email", "?")[:25]
        proxy = pool.get()
        audio, err_type = elevenlabs_tts_jwt(jwt, voice_id, chunk, model_id, stability, similarity, proxy)
        if audio:
            job_log(job_id, f"✅ Chunk {chunk_num} OK via JWT ({email})")
            return audio

        if err_type == 'unusual_activity':
            job_log(job_id, f"🚨 JWT unusual_activity ({email}), account bị block — skip, thử account tiếp theo...")
            continue  # Skip account này hoàn toàn, không retry với proxy mới

        if err_type == 'expired':
            job_log(job_id, f"⚠️ JWT expired: {email}, attempting refresh...")
            refresh_token = acc.get("refresh_token")
            new_jwt = refresh_jwt_token(refresh_token) if refresh_token else None
            if new_jwt:
                acc["jwt_token"] = new_jwt
                update_jwt_in_d1(acc.get("email", ""), new_jwt)
                proxy = pool.get()
                audio2, err2 = elevenlabs_tts_jwt(new_jwt, voice_id, chunk, model_id, stability, similarity, proxy)
                if audio2:
                    job_log(job_id, f"✅ Chunk {chunk_num} OK via refreshed JWT ({email})")
                    return audio2
                job_log(job_id, f"⚠️ Refreshed JWT still failed ({err2}): {email}")
            else:
                job_log(job_id, f"⚠️ JWT refresh failed for {email}, skipping...")
            continue

        if err_type == 'rate_limit':
            job_log(job_id, f"⚠️ Rate limit on {email}, rotating proxy...")
            pool.rotate_and_refresh()
            time.sleep(1)
            continue

        job_log(job_id, f"⚠️ JWT error ({err_type}) on {email}, trying next...")

    # Fallback: try each API key
    for key in api_keys:
        proxy = pool.get()
        audio, err_type = elevenlabs_tts_apikey(key, voice_id, chunk, model_id, stability, similarity, proxy)
        if audio:
            job_log(job_id, f"✅ Chunk {chunk_num} OK via API key ({key[:10]}...)")
            return audio

        if err_type == 'unusual_activity':
            job_log(job_id, f"🚨 API key unusual_activity ({key[:10]}...), rotating proxy...")
            proxy = pool.rotate_and_refresh()
            # Retry same key with new proxy
            audio2, err2 = elevenlabs_tts_apikey(key, voice_id, chunk, model_id, stability, similarity, proxy)
            if audio2:
                job_log(job_id, f"✅ Chunk {chunk_num} OK via API key+rotated proxy ({key[:10]}...)")
                return audio2
            job_log(job_id, f"⚠️ Still failed after proxy rotate ({err2}): {key[:10]}...")
            continue

        if err_type == 'dead':
            job_log(job_id, f"⚠️ API key dead: {key[:10]}..., trying next...")
            continue
        if err_type == 'rate_limit':
            job_log(job_id, f"⚠️ Rate limit on key {key[:10]}..., rotating proxy...")
            pool.rotate_and_refresh()
            time.sleep(2)
            continue
        job_log(job_id, f"⚠️ API key error ({err_type}): {key[:10]}..., trying next...")

    return None


def run_tts_job(job_id: str, chunks: List[str], voice_id: str, model_id: str,
                jwt_accounts: List[dict], api_keys: List[str],
                proxy_list: List[str], stability: float, similarity: float,
                user_id: int):
    """Run TTS job with 3 parallel workers"""
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    job_log(job_id, f"🚀 Starting: {len(chunks)} chunks, {len(jwt_accounts)} JWT accounts, {len(api_keys)} API keys, {len(proxy_list)} proxies")
    if proxy_list:
        job_log(job_id, f"🌐 Proxies: {[p[:30] for p in proxy_list]}")
    else:
        job_log(job_id, "⚠️ Không có proxy — chạy trực tiếp")

    # chunk_files dict: index → path (to preserve order)
    chunk_files: dict[int, str] = {}
    lock = threading.Lock()

    def process_chunk(i: int, chunk: str):
        if not chunk.strip():
            return
        job_log(job_id, f"🎤 Chunk {i+1}/{len(chunks)}: {len(chunk)} chars")
        audio = tts_one_chunk(
            chunk, voice_id, model_id,
            jwt_accounts, api_keys, proxy_list,
            stability, similarity, job_id, i + 1
        )
        with lock:
            if audio:
                chunk_path = str(job_dir / f"chunk_{i:04d}.mp3")
                with open(chunk_path, "wb") as f:
                    f.write(audio)
                chunk_files[i] = chunk_path
                jobs[job_id]["done"] += 1
            else:
                job_log(job_id, f"❌ Chunk {i+1} FAILED — all accounts/keys exhausted")
                jobs[job_id]["failed"] += 1

    # Sliding window: submit tối đa 3 chunks cùng lúc, khi 1 xong thì submit tiếp
    # Giống tool chính (TTSTaskRunner với max_workers=3)
    with ThreadPoolExecutor(max_workers=3) as executor:
        pending: dict = {}
        chunk_iter = iter(enumerate(chunks))

        # Seed ban đầu: submit tối đa 3 chunks
        for _ in range(3):
            try:
                i, chunk = next(chunk_iter)
                if chunk.strip():
                    fut = executor.submit(process_chunk, i, chunk)
                    pending[fut] = i
            except StopIteration:
                break

        # Sliding window: khi 1 future xong → submit chunk tiếp theo
        while pending:
            done_futures = as_completed(pending)
            fut = next(done_futures)
            try:
                fut.result()
            except Exception as e:
                job_log(job_id, f"❌ Worker error: {e}")
            del pending[fut]

            # Submit chunk tiếp theo nếu còn
            try:
                i, chunk = next(chunk_iter)
                if chunk.strip():
                    new_fut = executor.submit(process_chunk, i, chunk)
                    pending[new_fut] = i
            except StopIteration:
                pass

    # Merge in order
    ordered_files = [chunk_files[i] for i in sorted(chunk_files.keys())]
    if ordered_files:
        output_path = str(OUTPUT_DIR / f"{job_id}.mp3")
        if merge_mp3_files(ordered_files, output_path):
            update_job(job_id, status="done", output_file=f"{job_id}.mp3")
            job_log(job_id, f"✅ Done! Merged {len(ordered_files)} chunks → {job_id}.mp3")
        else:
            update_job(job_id, status="error")
            job_log(job_id, "❌ Merge failed")
    else:
        update_job(job_id, status="error")
        job_log(job_id, "❌ No chunks generated")

    # Cleanup chunk files
    shutil.rmtree(str(job_dir), ignore_errors=True)

    # Log usage to D1
    total_chars = sum(len(c) for c in chunks)
    d1_rpc("log_usage", {"user_id": user_id, "characters_used": total_chars, "voice_id": voice_id, "status": "success"})

# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("✅ VPS TTS Server started")
    yield
    print("🔄 Server shutting down...")

app = FastAPI(title="VPS TTS Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_key(x_api_key: str = Header(None)):
    if x_api_key != SERVER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Models ────────────────────────────────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_turbo_v2_5"
    stability: float = 0.5
    similarity: float = 0.75
    user_id: int
    jwt_accounts: List[dict] = []
    api_keys: List[str] = []
    proxy_list: List[str] = []

class TTSDirectRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_turbo_v2_5"
    jwt_token: Optional[str] = None
    api_key: Optional[str] = None
    proxy: Optional[str] = None

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "server": "vps-tts", "time": time.time()}

@app.get("/debug/test_key")
def debug_test_key(x_api_key: str = Header(None)):
    """Test 1 API key directly against ElevenLabs, return raw response"""
    verify_key(x_api_key)
    rows = d1_query("user_api_keys", filters={"key_state": "READY", "is_active": "1"})
    if not rows:
        return {"error": "no keys in D1"}
    key = rows[0].get("api_key", "")
    url = f"{ELEVENLABS_BASE}/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
    headers = {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    payload = {"text": "Hello", "model_id": "eleven_turbo_v2_5", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        return {"status": r.status_code, "key_prefix": key[:12], "body": r.text[:500]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/refresh")
def debug_refresh(x_api_key: str = Header(None)):
    """Test JWT refresh from VPS directly — shows exact Firebase response"""
    verify_key(x_api_key)
    rows = d1_query("jwt_accounts", filters={"state": "READY"})
    if not rows:
        return {"error": "no jwt accounts in D1"}
    acc = rows[0]
    email = acc.get("email", "")
    refresh_token = acc.get("refresh_token", "")
    jwt_token = acc.get("jwt_token", "")

    # Test current JWT first
    tts_url = f"{ELEVENLABS_BASE}/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
    tts_headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json", "Accept": "audio/mpeg"}
    tts_payload = {"text": "Hi", "model_id": "eleven_turbo_v2_5", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    try:
        tr = requests.post(tts_url, headers=tts_headers, json=tts_payload, timeout=20)
        tts_result = {"status": tr.status_code, "body": tr.text[:200]}
    except Exception as e:
        tts_result = {"error": str(e)}

    # Try Firebase refresh
    try:
        fr = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
            headers={"Content-Type": "application/json", "Referer": "https://elevenlabs.io", "Origin": "https://elevenlabs.io"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15
        )
        fb_result = {"status": fr.status_code, "body": fr.text[:400]}
        if fr.status_code == 200:
            new_jwt = fr.json().get("id_token", "")
            fb_result["new_jwt_len"] = len(new_jwt)
            fb_result["new_jwt_prefix"] = new_jwt[:50]
    except Exception as e:
        fb_result = {"error": str(e)}

    return {
        "email": email,
        "jwt_len": len(jwt_token),
        "refresh_token_len": len(refresh_token),
        "tts_with_current_jwt": tts_result,
        "firebase_refresh": fb_result,
    }

@app.post("/tts/start")
def tts_start(req: TTSRequest, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    
    chunks = split_text(req.text, max_chars=300)
    if not chunks:
        raise HTTPException(400, "Empty text")
    
    job_id = str(uuid.uuid4())
    create_job(job_id, len(chunks))
    
    # Dùng threading.Thread thay vì BackgroundTasks để ThreadPoolExecutor bên trong
    # có thể spawn 3 worker threads thực sự song song, không bị block bởi event loop
    t = threading.Thread(
        target=run_tts_job,
        args=(job_id, chunks, req.voice_id, req.model_id,
              req.jwt_accounts, req.api_keys, req.proxy_list,
              req.stability, req.similarity, req.user_id),
        daemon=True
    )
    t.start()
    
    return {"job_id": job_id, "total_chunks": len(chunks), "status": "started"}

@app.get("/tts/status/{job_id}")
def tts_status(job_id: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    with job_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "id": job["id"],
        "status": job["status"],
        "total": job["total"],
        "done": job["done"],
        "failed": job["failed"],
        "progress": round(job["done"] / max(job["total"], 1) * 100),
        "logs": job["logs"][-20:],  # last 20 logs
        "output_file": job.get("output_file"),
    }

@app.get("/tts/download/{filename}")
def tts_download(filename: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), media_type="audio/mpeg", filename=filename)

@app.post("/tts/direct")
def tts_direct(req: TTSDirectRequest, x_api_key: str = Header(None)):
    """Single chunk TTS - returns audio directly"""
    verify_key(x_api_key)
    
    if len(req.text) > 500:
        raise HTTPException(400, "Text too long for direct TTS (max 500 chars)")
    
    audio = None
    if req.jwt_token:
        audio, _ = elevenlabs_tts_jwt(req.jwt_token, req.voice_id, req.text,
                                       req.model_id, proxy=req.proxy)
    elif req.api_key:
        audio, _ = elevenlabs_tts_apikey(req.api_key, req.voice_id, req.text,
                                          req.model_id, proxy=req.proxy)

    if not audio:
        raise HTTPException(502, "TTS generation failed")
    
    return StreamingResponse(iter([audio]), media_type="audio/mpeg")

@app.delete("/tts/job/{job_id}")
def tts_delete_job(job_id: str, x_api_key: str = Header(None)):
    verify_key(x_api_key)
    with job_lock:
        jobs.pop(job_id, None)
    # Delete output file
    for f in OUTPUT_DIR.glob(f"{job_id}*"):
        f.unlink(missing_ok=True)
    return {"deleted": True}

@app.get("/voices")
def list_voices(jwt_token: str = None, api_key: str = None,
                x_api_key: str = Header(None)):
    """List available ElevenLabs voices"""
    verify_key(x_api_key)
    headers = {}
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
    elif api_key:
        headers["xi-api-key"] = api_key
    else:
        raise HTTPException(400, "Need jwt_token or api_key")
    
    try:
        r = requests.get(f"{ELEVENLABS_BASE}/v1/voices", headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(502, str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    print(f"🚀 Starting VPS TTS Server on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
