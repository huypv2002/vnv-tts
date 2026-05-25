from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
import socket
from pathlib import Path
from typing import Any

import websockets


ELEVENLABS_HCAPTCHA_SITEKEY = "8e58fe8c-1a48-4f94-88ae-8e90b586a192"
ELEVENLABS_ORIGIN_URL = "https://elevenlabs.io/"
DEFAULT_DEBUG_PORT = 9229

MACOS_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

LINUX_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def _resolve_chrome_executable() -> str:
    for path in MACOS_CHROME_PATHS:
        if Path(path).exists():
            return path

    for name in LINUX_CHROME_CANDIDATES:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    raise RuntimeError(
        "Không tìm thấy Chrome/Chromium. "
        "Hãy cài Google Chrome hoặc truyền đường dẫn riêng vào helper này."
    )


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


class _ChromeCdpClient:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self._message_id = 0
        self._socket: websockets.ClientConnection | None = None

    async def __aenter__(self) -> "_ChromeCdpClient":
        self._socket = await websockets.connect(self.websocket_url, max_size=None)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._socket is not None:
            await self._socket.close()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if self._socket is None:
            raise RuntimeError("CDP socket chưa được mở.")

        self._message_id += 1
        payload: dict[str, Any] = {
            "id": self._message_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = session_id

        await self._socket.send(json.dumps(payload))

        while True:
            response = json.loads(await self._socket.recv())
            if response.get("id") == self._message_id:
                if "error" in response:
                    raise RuntimeError(
                        f"CDP error calling {method}: {response['error']}"
                    )
                return response


async def _get_hcaptcha_token_async(
    behaviour_type: str,
    chrome_executable: str,
    debug_port: int,
    navigation_timeout: float,
    headless: bool,
) -> str:
    user_data_dir = tempfile.mkdtemp(prefix="elevenlabs-hcaptcha-")
    cmd = [
        chrome_executable,
        f"--remote-debugging-port={debug_port}",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,900",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]
    if headless:
        cmd.insert(2, "--headless=new")

    chrome_process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        debugger_url = None
        deadline = time.time() + navigation_timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{debug_port}/json/version",
                    timeout=1,
                ) as response:
                    debugger_url = json.load(response)["webSocketDebuggerUrl"]
                    break
            except Exception:
                await asyncio.sleep(0.2)

        if not debugger_url:
            raise RuntimeError("Không mở được Chrome DevTools Protocol.")

        async with _ChromeCdpClient(debugger_url) as client:
            target_id = (
                await client.send("Target.createTarget", {"url": "about:blank"})
            )["result"]["targetId"]
            session_id = (
                await client.send(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
            )["result"]["sessionId"]

            await client.send("Page.enable", session_id=session_id)
            await client.send("Runtime.enable", session_id=session_id)
            await client.send(
                "Page.navigate",
                {"url": ELEVENLABS_ORIGIN_URL},
                session_id=session_id,
            )

            await asyncio.sleep(6)

            expression = f"""
            (async () => {{
              if (!window.hcaptcha) {{
                await new Promise((resolve, reject) => {{
                  const oldScript = document.querySelector('script[data-elevenlabs-hcaptcha]');
                  if (oldScript) {{
                    oldScript.remove();
                  }}
                  const script = document.createElement('script');
                  script.src = 'https://js.hcaptcha.com/1/api.js?render=explicit&hl=vi';
                  script.async = true;
                  script.defer = true;
                  script.dataset.elevenlabsHcaptcha = '1';
                  script.onload = () => resolve();
                  script.onerror = () => reject(new Error('load hcaptcha script failed'));
                  document.head.appendChild(script);
                }});
              }}

              await new Promise((resolve, reject) => {{
                let retries = 0;
                const timer = setInterval(() => {{
                  if (window.hcaptcha) {{
                    clearInterval(timer);
                    resolve();
                    return;
                  }}
                  retries += 1;
                  if (retries > 120) {{
                    clearInterval(timer);
                    reject(new Error("hcaptcha script failed to load"));
                  }}
                }}, 100);
              }});

              const container = document.createElement("div");
              container.style.position = "absolute";
              container.style.left = "-999999px";
              document.body.appendChild(container);

              const token = await new Promise((resolve, reject) => {{
                const widgetId = window.hcaptcha.render(container, {{
                  sitekey: "{ELEVENLABS_HCAPTCHA_SITEKEY}",
                  size: "invisible",
                  callback: (value) => resolve(value),
                  "close-callback": () => reject(new Error("hcaptcha challenge closed")),
                  "error-callback": (error) => reject(new Error("hcaptcha failed: " + error))
                }});

                try {{
                  window.hcaptcha.execute(widgetId, {{ async: true }});
                }} catch (error) {{
                  reject(error);
                }}
              }});

              return {{
                token,
                behaviourType: "{behaviour_type}"
              }};
            }})()
            """

            result = await client.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            )

            value = result["result"]["result"].get("value")
            if not isinstance(value, dict) or not value.get("token"):
                raise RuntimeError("Không lấy được hcaptcha token từ Chrome.")
            return str(value["token"])
    finally:
        chrome_process.terminate()
        try:
            chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_process.kill()
            chrome_process.wait(timeout=5)


def get_elevenlabs_hcaptcha_token(
    behaviour_type: str = "voice_preview",
    chrome_executable: str | None = None,
    debug_port: int | None = None,
    navigation_timeout: float = 15.0,
    headless: bool = True,
) -> str:
    """
    Lấy hCaptcha token mới từ browser thật để gọi anonymous ElevenLabs preview.

    ElevenLabs hiện render invisible hCaptcha trên trang marketing và dùng token
    đó cho các request anonymous. Helper này tái hiện đúng flow đó trong Chrome.
    """
    resolved_chrome = chrome_executable or _resolve_chrome_executable()
    resolved_port = debug_port if debug_port is not None else _reserve_free_port()
    return asyncio.run(
        _get_hcaptcha_token_async(
            behaviour_type=behaviour_type,
            chrome_executable=resolved_chrome,
            debug_port=resolved_port,
            navigation_timeout=navigation_timeout,
            headless=headless,
        )
    )
