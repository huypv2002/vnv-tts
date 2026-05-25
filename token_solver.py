"""
Token Solver - Giải hCaptcha HSW bằng Camoufox
Logic từ elevenlabs_api_pool.py (đang chạy trên server)
"""
import asyncio
import json
import random
import re
import time
import warnings
from collections import deque

import httpx
import jwt
import tls_client
from camoufox.async_api import AsyncCamoufox

from proxy_pool import ProxyPool

warnings.filterwarnings("ignore")

# === OPTIMIZATION: hsw.js cache ===
_hsw_js_cache: dict[str, str] = {}

SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
HOST = "elevenlabs.io"
TOKEN_TTL = 100  # seconds — token hết hạn sau ~120s, dùng 100s cho an toàn


def get_hcaptcha_materials(proxy_http: str) -> tuple[str, str, dict]:
    """Lấy req_token, version, config qua HTTP."""
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*', 'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://newassets.hcaptcha.com',
        'referer': 'https://newassets.hcaptcha.com/',
        'sec-ch-ua': '"Chromium";"v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }
    if proxy_http:
        session.proxies = {'http': proxy_http, 'https': proxy_http}

    api_js = session.get('https://hcaptcha.com/1/api.js?render=explicit&onload=hcaptchaOnLoad').text
    versions = re.findall(r'v1/([A-Za-z0-9]+)/static', api_js)
    version = versions[1] if len(versions) > 1 else "unknown"

    config = session.post("https://api2.hcaptcha.com/checksiteconfig", params={
        'v': version, 'host': HOST, 'sitekey': SITEKEY, 'sc': '1', 'swa': '1', 'spst': '1',
    }).json()

    if 'c' not in config or 'req' not in config.get('c', {}):
        raise RuntimeError(f"checksiteconfig failed: {json.dumps(config)[:100]}")

    return config['c']['req'], version, config


async def solve_hsw(req_token: str, proxy_http: str, browser) -> str:
    """Giải HSW trong Camoufox browser page. Dùng cache cho hsw.js."""
    decoded = jwt.decode(req_token, options={"verify_signature": False})
    cache_key = decoded["l"]

    # Cache hsw.js (tiết kiệm 1-3s mỗi lần)
    if cache_key in _hsw_js_cache:
        hsw_js = _hsw_js_cache[cache_key]
    else:
        session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
        session.headers = {
            'accept': '*/*', 'accept-language': 'en-US,en;q=0.9',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        }
        if proxy_http:
            session.proxies = {'http': proxy_http, 'https': proxy_http}

        hsw_url = "https://newassets.hcaptcha.com" + cache_key + "/hsw.js"
        hsw_js = session.get(hsw_url).text
        if not hsw_js or "function" not in hsw_js:
            raise RuntimeError("hsw.js fetch returned invalid content")
        _hsw_js_cache[cache_key] = hsw_js
        print(f"  [cache] hsw.js cached ({len(hsw_js)//1024}KB, key={cache_key[:16]}...)", flush=True)

    page = await browser.new_page()
    try:
        await page.route(f"https://{HOST}/hsw", lambda r: r.fulfill(
            status=200, content_type="text/html",
            body="<html><head></head><body></body></html>"
        ))
        await page.goto(f"https://{HOST}/hsw", wait_until='domcontentloaded', timeout=10000)
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => false})")

        # Inject hsw.js
        injected = False
        try:
            await page.add_script_tag(content=hsw_js)
            await asyncio.sleep(0.2)
            if await page.evaluate("typeof hsw === 'function'"):
                injected = True
        except Exception:
            pass

        if not injected:
            try:
                await page.evaluate(f"""(function() {{
                    const s = document.createElement('script');
                    s.textContent = {json.dumps(hsw_js)};
                    document.head.appendChild(s);
                }})();""")
                await asyncio.sleep(0.2)
                if await page.evaluate("typeof hsw === 'function'"):
                    injected = True
            except Exception:
                pass

        if not injected:
            await page.evaluate(hsw_js)
            await asyncio.sleep(0.2)

        result = await page.evaluate("(req) => hsw(req)", req_token)
        return result
    finally:
        await page.close()


def submit_captcha(hsw_token: str, version: str, config: dict, proxy_http: str) -> str:
    """Submit HSW → lấy hcaptcha pass UUID."""
    session = tls_client.Session(client_identifier="chrome_130", random_tls_extension_order=True)
    session.headers = {
        'accept': '*/*', 'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://newassets.hcaptcha.com', 'referer': 'https://newassets.hcaptcha.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    }
    if proxy_http:
        session.proxies = {'http': proxy_http, 'https': proxy_http}

    motion = {
        "st": int(time.time() * 1000), "dct": int(time.time() * 1000),
        "mm": [[random.randint(100, 800), random.randint(100, 600), random.randint(10, 500)] for _ in range(3)],
    }
    data = {
        'v': version, 'sitekey': SITEKEY, 'host': HOST, 'hl': 'en',
        'motionData': json.dumps(motion), 'n': hsw_token, 'c': json.dumps(config['c']),
    }
    resp = session.post(f"https://api2.hcaptcha.com/getcaptcha/{SITEKEY}", data=data)
    result = resp.json()

    if 'generated_pass_UUID' in result:
        return result['generated_pass_UUID']
    if 'tasklist' in result:
        raise RuntimeError("image_challenge")
    raise RuntimeError(f"getcaptcha failed: {json.dumps(result)[:100]}")


class TokenPool:
    """
    Pre-solve hCaptcha tokens liên tục trong background.
    Mỗi token = (hcaptcha_token, proxy_dict, solved_at)
    """

    def __init__(self, proxy_pool: ProxyPool, target_size: int = 5, max_solvers: int = 5):
        self.proxy_pool = proxy_pool
        self.target_size = target_size
        self.max_solvers = max_solvers
        self._tokens = deque()  # (hcaptcha_token, proxy, solved_at)
        self._lock = asyncio.Lock()
        self._solving = 0
        self._total_solved = 0
        self._total_expired = 0
        self._total_served = 0
        self._total_failed = 0
        self._running = False
        self._solver_tasks = []
        # Callback cho UI log
        self._log_callback = None

    def set_log_callback(self, callback):
        """Set callback function(msg: str) để log ra UI."""
        self._log_callback = callback

    def _log(self, msg: str):
        if self._log_callback:
            self._log_callback(msg)
        else:
            print(msg, flush=True)

    @property
    def available(self) -> int:
        """Số token còn hợp lệ trong pool."""
        now = time.time()
        return sum(1 for _, _, t in self._tokens if now - t < TOKEN_TTL)

    @property
    def solving_count(self) -> int:
        return self._solving

    @property
    def stats(self) -> dict:
        return {
            "pool_size": self.available,
            "pool_target": self.target_size,
            "total_solved": self._total_solved,
            "total_served": self._total_served,
            "total_expired": self._total_expired,
            "total_failed": self._total_failed,
            "solving_now": self._solving,
        }

    async def get_token(self, timeout: float = 90.0) -> tuple[str, dict]:
        """Lấy token đã solve sẵn. Chờ tối đa timeout giây."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with self._lock:
                while self._tokens:
                    token, proxy, solved_at = self._tokens.popleft()
                    if time.time() - solved_at < TOKEN_TTL:
                        self._total_served += 1
                        age = time.time() - solved_at
                        self._log(f"[pool] ✓ served token (age={age:.0f}s, còn={len(self._tokens)})")
                        return token, proxy
                    else:
                        self._total_expired += 1
                        self._log(f"[pool] ⏰ token hết hạn, bỏ")
            await asyncio.sleep(1)
        raise RuntimeError("Token pool trống — timeout chờ token")

    async def start(self):
        """Bắt đầu solve token liên tục."""
        self._running = True
        num_workers = self.max_solvers
        self._log(f"[pool] Khởi động {num_workers} solver (target={self.target_size})")
        self._solver_tasks = [
            asyncio.create_task(self._solver_loop(i))
            for i in range(num_workers)
        ]

    async def stop(self):
        """Dừng solve."""
        self._running = False
        for task in self._solver_tasks:
            task.cancel()
        self._solver_tasks = []
        self._log("[pool] Đã dừng tất cả solver")

    async def restart_solvers(self):
        """Restart solvers khi thêm/xóa proxy key — tự động cập nhật số worker theo key count."""
        self.max_solvers = max(1, self.proxy_pool.key_count)  # Dynamic: sync với số key
        await self.stop()
        await self.start()

    async def _solve_single_token(self, proxy: dict, browser) -> str:
        """Solve 1 token với proxy và browser đã có."""
        req_token, version, config = await asyncio.to_thread(
            get_hcaptcha_materials, proxy["http"]
        )
        hsw_token = await solve_hsw(req_token, proxy["http"], browser)
        hcaptcha_token = await asyncio.to_thread(
            submit_captcha, hsw_token, version, config, proxy["http"]
        )
        return hcaptcha_token

    async def _solver_loop(self, worker_id: int):
        """Liên tục solve token.
        
        OPTIMIZATIONS:
        - hsw.js cache (tránh re-download 944KB)
        - Batch 5-7 token/IP (thay vì 1)
        - Browser alive xuyên suốt (ko đóng/mở)
        - Stagger dàn đều 5 workers
        - Skip stagger nếu pool rỗng (urgent)
        - Pre-fetch proxy + browser trong cooldown
        """
        # Stagger dàn đều worker — multiplier giảm dần theo số worker
        # Với 5 worker: 0, 3, 6, 9, 12s
        # Với 20 worker: 0, 0.75, 1.5, ... , 14.25s
        # Với 100 worker: 0, 0.15, 0.3, ... , 14.85s
        # Luôn giới hạn stagger tối đa 15s để worker cuối không chờ quá lâu
        stagger_mult = min(3.0, 15.0 / max(1, self.max_solvers - 1)) if self.max_solvers > 1 else 0
        stagger = worker_id * stagger_mult
        if self.available > 0:
            await asyncio.sleep(stagger)
        else:
            await asyncio.sleep(0.1)

        browser = None
        proxy = None
        t_ip_acquired = 0
        TOKEN_SOLVE_TIMEOUT = 75

        while self._running:
            try:
                if self.available >= self.target_size:
                    await asyncio.sleep(2)
                    continue

                self._solving += 1
                self._log(f"[solver-{worker_id}] Yêu cầu proxy...")

                # Fetch proxy (1 lần cho mỗi batch)
                if not proxy:
                    proxy = await self.proxy_pool.get_proxy_for_solve()
                    t_ip_acquired = time.time()

                num_tokens = random.randint(5, 7)  # TĂNG batch: 5-7
                self._log(f"[solver-{worker_id}] IP {proxy['raw']}. Giải {num_tokens} token...")

                # Launch/reuse browser
                if not browser:
                    browser = await AsyncCamoufox(headless=True, os='windows', proxy={'server': proxy["http"]}).start()

                # Solve parallel
                tasks = [
                    asyncio.wait_for(
                        self._solve_single_token(proxy, browser),
                        timeout=TOKEN_SOLVE_TIMEOUT,
                    )
                    for _ in range(num_tokens)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                success_count = 0
                for res in results:
                    if isinstance(res, Exception):
                        self._total_failed += 1
                        self._log(f"[solver-{worker_id}] ✗ {str(res)[:80]}")
                    else:
                        success_count += 1
                        self._total_solved += 1
                        async with self._lock:
                            self._tokens.append((res, proxy, time.time()))

                self._solving -= 1
                self._log(f"[solver-{worker_id}] ✓ {success_count}/{num_tokens} solve xong (pool={self.available})")

                # Pre-fetch proxy mới + browser trong cooldown
                elapsed = time.time() - t_ip_acquired
                wait_time = max(0.0, 60.0 - elapsed)
                if wait_time > 0 and self._running:
                    self._log(f"[solver-{worker_id}] cooldown {wait_time:.1f}s...")
                    
                    pre_fetch_at = max(3.0, wait_time * 0.4)
                    await asyncio.sleep(pre_fetch_at)
                    
                    if self._running and self.available < self.target_size:
                        self._log(f"[solver-{worker_id}] pre-fetch proxy...")
                        try:
                            new_proxy = await self.proxy_pool.get_proxy_for_solve()
                            new_browser = await AsyncCamoufox(headless=True, os='windows', proxy={'server': new_proxy["http"]}).start()
                            if browser:
                                try: await browser.stop()
                                except: pass
                            browser = new_browser
                            proxy = new_proxy
                        except Exception as e:
                            self._log(f"[solver-{worker_id}] pre-fetch lỗi: {str(e)[:50]}")
                    
                    remaining = max(0.0, 60.0 - (time.time() - t_ip_acquired))
                    if remaining > 0 and self._running:
                        await asyncio.sleep(remaining)
                        if self.available >= self.target_size:
                            self._log(f"[solver-{worker_id}] pool đầy, bỏ qua")
                            proxy = None
                            if browser:
                                try: await browser.stop()
                                except: pass
                                browser = None
                            continue
                else:
                    proxy = None
                    if browser:
                        try: await browser.stop()
                        except: pass
                        browser = None

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._solving -= 1
                self._total_failed += 1
                self._log(f"[solver-{worker_id}] ✗ lỗi: {str(e)[:80]}")
                proxy = None
                if browser:
                    try: await browser.stop()
                    except: pass
                    browser = None
                await asyncio.sleep(5)

        # Cleanup
        if browser:
            try: await browser.stop()
            except: pass
