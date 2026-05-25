#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests


APP_DIR = Path(__file__).resolve().parent
API_BASE = "https://api.elevenlabs.io/v1"
WEB_API_BASE = "https://api.elevenlabs.io/v1"
PROXYXOAY_API = "https://proxyxoay.shop/api/get.php"
DEFAULT_ACCOUNTS_FILE = APP_DIR / "accounts-test.txt"
DEFAULT_VOICES_FILE = APP_DIR / "dialogue_voices.json"
DEFAULT_OUTPUT_DIR = APP_DIR
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"


@dataclass
class Account:
    email: str
    password: str
    username: str
    api_key: str


def mask_secret(value: str, keep: int = 5) -> str:
    value = value.strip()
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def load_accounts(path: Path) -> list[Account]:
    accounts: list[Account] = []
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy accounts file: {path}")

    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 4:
            print(f"[skip] line {line_no}: không đủ 4 cột email|password|user|api_key")
            continue
        email, password, username, api_key = parts[:4]
        if not api_key.startswith("sk_"):
            print(f"[skip] line {line_no}: cột 4 không giống ElevenLabs API key")
            continue
        accounts.append(Account(email=email, password=password, username=username, api_key=api_key))

    if not accounts:
        raise RuntimeError(f"Không đọc được API key hợp lệ từ {path}")
    return accounts


def load_dialogue_voices(path: Path) -> list[str]:
    if not path.exists():
        return ["Yg7C1g7suzNt5TisIqkZ", "Yg7C1g7suzNt5TisIqkZ"]
    data = json.loads(path.read_text(encoding="utf-8"))
    voices = data.get("voices", []) if isinstance(data, dict) else []
    voice_ids = [
        str(item.get("voice_id", "")).strip()
        for item in voices
        if isinstance(item, dict) and str(item.get("voice_id", "")).strip()
    ]
    if len(voice_ids) >= 2:
        return voice_ids[:2]
    return (voice_ids + ["Yg7C1g7suzNt5TisIqkZ", "Yg7C1g7suzNt5TisIqkZ"])[:2]


def build_headers(api_key: str) -> dict[str, str]:
    return {
        "xi-api-key": api_key.strip(),
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://elevenlabs.io",
        "Referer": "https://elevenlabs.io/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        ),
    }


def build_web_headers(jwt_token: str) -> dict[str, str]:
    jwt_token = jwt_token.strip()
    if jwt_token.lower().startswith("bearer "):
        jwt_token = jwt_token[7:].strip()
    return {
        "accept": "*/*",
        "accept-language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
        "authorization": f"Bearer {jwt_token}",
        "content-type": "application/json",
        "origin": "https://elevenlabs.io",
        "priority": "u=1, i",
        "referer": "https://elevenlabs.io/",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }


def fetch_proxyxoay(proxy_key: str, nhamang: str = "random", tinhthanh: str = "0") -> str:
    response = requests.get(
        PROXYXOAY_API,
        params={
            "key": proxy_key.strip(),
            "nhamang": nhamang,
            "tinhthanh": tinhthanh,
            "whitelist": "",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") == 100:
        raw_proxy = str(data.get("proxyhttp", "")).strip().rstrip(":")
        parts = raw_proxy.split(":")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return f"http://{parts[0]}:{parts[1]}"
        raise RuntimeError(f"proxyhttp không hợp lệ: {data}")

    message = str(data.get("message", data))
    match = re.search(r"(\d+)s", message)
    cooldown = f" Thử lại sau khoảng {match.group(1)}s." if match else ""
    raise RuntimeError(f"ProxyXOAY lỗi status={data.get('status')}: {message}.{cooldown}")


def normalize_proxy_url(proxy: str) -> str:
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    parts = proxy.split(":")
    if len(parts) >= 4:
        host, port, username = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        return f"http://{username}:{password}@{host}:{port}"
    if len(parts) >= 2:
        return f"http://{parts[0]}:{parts[1]}"
    return proxy


def make_proxy_dict(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def login_web_jwt(email: str, password: str, proxy_url: str | None = None) -> tuple[str | None, str]:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://elevenlabs.io",
        "Referer": "https://elevenlabs.io/",
        "User-Agent": build_web_headers("placeholder")["user-agent"],
    }
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True,
    }
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=45,
            proxies=make_proxy_dict(proxy_url),
        )
    except requests.RequestException as exc:
        return None, f"Firebase login request lỗi: {exc}"

    if response.status_code != 200:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        return None, f"Firebase login HTTP {response.status_code}: {detail[:300]}"

    token = response.json().get("idToken")
    if not token:
        return None, "Firebase login OK nhưng response không có idToken."
    return str(token), "OK"


def build_sample_inputs(voice_ids: Iterable[str]) -> list[dict[str, str]]:
    voice_a, voice_b = list(voice_ids)[:2]
    return [
        {
            "text": "[calmly] Chào anh, hôm nay mình thử tạo một đoạn hội thoại ngắn nhé.",
            "voice_id": voice_a,
        },
        {
            "text": "[warmly] Được thôi. Nếu đoạn này xuất ra MP3 thì endpoint text to dialogue đã chạy ổn.",
            "voice_id": voice_b,
        },
    ]


def save_error(output_file: Path, status_code: int, text: str) -> Path:
    error_path = output_file.with_suffix(".error.json")
    try:
        payload = json.loads(text)
    except Exception:
        payload = {"raw": text}
    error_path.write_text(
        json.dumps({"status_code": status_code, "response": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return error_path


def text_to_dialogue_stream(
    api_key: str,
    inputs: list[dict[str, str]],
    output_file: Path,
    proxy_url: str | None = None,
    model_id: str = "eleven_v3",
    stability: float = 0.5,
    output_format: str = "mp3_44100_128",
) -> tuple[bool, str]:
    url = f"{API_BASE}/text-to-dialogue/stream"
    payload = {
        "inputs": inputs,
        "model_id": model_id,
        "settings": {"stability": stability},
    }
    params = {"output_format": output_format}
    proxies = make_proxy_dict(proxy_url)

    try:
        response = requests.post(
            url,
            params=params,
            headers=build_headers(api_key),
            json=payload,
            timeout=180,
            stream=True,
            proxies=proxies,
        )
    except requests.RequestException as exc:
        return False, f"Request lỗi: {exc}"

    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        error_path = save_error(output_file, response.status_code, response.text)
        return False, f"HTTP {response.status_code}; đã lưu lỗi: {error_path}"

    output_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_file.with_suffix(output_file.suffix + ".part")
    total = 0
    with tmp_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                handle.write(chunk)
                total += len(chunk)

    if total == 0:
        tmp_path.unlink(missing_ok=True)
        return False, "Response 200 nhưng không có audio bytes."

    if "json" in content_type.lower():
        text = tmp_path.read_text(encoding="utf-8", errors="replace")
        tmp_path.unlink(missing_ok=True)
        error_path = save_error(output_file, response.status_code, text)
        return False, f"Response là JSON, không phải audio; đã lưu: {error_path}"

    tmp_path.replace(output_file)
    return True, f"Đã lưu MP3: {output_file} ({total:,} bytes)"


def text_to_dialogue_stream_web(
    jwt_token: str,
    hcaptcha_token: str,
    inputs: list[dict[str, str]],
    output_file: Path,
    proxy_url: str | None = None,
    model_id: str = "eleven_v3",
    stability: float = 0.5,
) -> tuple[bool, str]:
    url = f"{WEB_API_BASE}/text-to-dialogue/stream?"
    payload = {
        "inputs": inputs,
        "model_id": model_id,
        "settings": {"stability": stability},
        "hcaptcha_token": hcaptcha_token.strip(),
    }
    try:
        response = requests.post(
            url,
            headers=build_web_headers(jwt_token),
            json=payload,
            timeout=180,
            stream=True,
            proxies=make_proxy_dict(proxy_url),
        )
    except requests.RequestException as exc:
        return False, f"Request lỗi: {exc}"

    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        error_path = save_error(output_file, response.status_code, response.text)
        return False, f"HTTP {response.status_code}; đã lưu lỗi: {error_path}"

    output_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_file.with_suffix(output_file.suffix + ".part")
    total = 0
    with tmp_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                handle.write(chunk)
                total += len(chunk)

    if total == 0:
        tmp_path.unlink(missing_ok=True)
        return False, "Response 200 nhưng không có audio bytes."

    if "json" in content_type.lower():
        text = tmp_path.read_text(encoding="utf-8", errors="replace")
        tmp_path.unlink(missing_ok=True)
        error_path = save_error(output_file, response.status_code, text)
        return False, f"Response là JSON, không phải audio; đã lưu: {error_path}"

    tmp_path.replace(output_file)
    return True, f"Đã lưu MP3: {output_file} ({total:,} bytes)"


def get_hcaptcha_token_from_args(args: argparse.Namespace) -> str:
    if args.hcaptcha_token:
        return args.hcaptcha_token.strip()
    if args.hcaptcha_file:
        return Path(args.hcaptcha_file).expanduser().read_text(encoding="utf-8").strip()
    env_token = os.getenv("ELEVENLABS_HCAPTCHA_TOKEN", "").strip()
    if env_token:
        return env_token
    if args.no_auto_hcaptcha:
        return ""

    from services.hcaptcha_token_provider import get_elevenlabs_hcaptcha_token

    print("         lấy hcaptcha token bằng Chrome headless...")
    token = get_elevenlabs_hcaptcha_token(
        behaviour_type="text_to_dialogue",
        navigation_timeout=30.0,
        headless=not args.show_hcaptcha_browser,
    )
    print(f"         hcaptcha OK: {mask_secret(token, keep=12)}")
    return token


def load_web_request_from_curl(path: str) -> tuple[str, str, dict[str, object]]:
    text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    jwt_match = re.search(r"-H\s+['\"]authorization:\s*Bearer\s+([^'\"]+)['\"]", text, re.IGNORECASE)
    payload_match = re.search(r"--data-raw\s+(['\"])(.*?)\1", text, re.DOTALL)
    if not jwt_match:
        raise RuntimeError("Không tìm thấy header authorization: Bearer trong curl file.")
    if not payload_match:
        raise RuntimeError("Không tìm thấy --data-raw JSON trong curl file.")

    payload_raw = payload_match.group(2)
    payload_raw = payload_raw.replace("\\'", "'")
    payload = json.loads(payload_raw)
    hcaptcha_token = str(payload.get("hcaptcha_token", "")).strip()
    if not hcaptcha_token:
        raise RuntimeError("Curl payload không có hcaptcha_token.")
    return jwt_match.group(1).strip(), hcaptcha_token, payload


def list_account_voice_ids(api_key: str, proxy_url: str | None = None) -> list[str]:
    try:
        response = requests.get(
            f"{API_BASE}/voices",
            headers={
                "xi-api-key": api_key.strip(),
                "Accept": "application/json",
                "User-Agent": build_headers(api_key)["User-Agent"],
            },
            timeout=45,
            proxies=make_proxy_dict(proxy_url),
        )
        if response.status_code != 200:
            return []
        data = response.json()
    except Exception:
        return []

    voices = data.get("voices", []) if isinstance(data, dict) else []
    result: list[str] = []
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        voice_id = str(voice.get("voice_id", "")).strip()
        if voice_id:
            result.append(voice_id)
        if len(result) >= 2:
            break
    return result


def run_test(args: argparse.Namespace) -> int:
    curl_jwt = ""
    curl_hcaptcha = ""
    curl_payload: dict[str, object] = {}
    if args.curl_file:
        curl_jwt, curl_hcaptcha, curl_payload = load_web_request_from_curl(args.curl_file)

    accounts = load_accounts(Path(args.accounts).expanduser())
    if args.shuffle:
        random.shuffle(accounts)

    proxy_url = None
    if not args.no_proxy:
        if args.proxy:
            proxy_url = normalize_proxy_url(args.proxy)
            print(f"[proxy] Static: {proxy_url}")
        else:
            proxy_key = args.proxy_key or os.getenv("PROXYXOAY_KEY", "").strip()
            if not proxy_key:
                raise RuntimeError("Thiếu proxy. Truyền --proxy, --proxy-key hoặc set PROXYXOAY_KEY.")
            print("[proxy] Đang lấy proxyxoay...")
            proxy_url = fetch_proxyxoay(proxy_key)
            print(f"[proxy] OK: {proxy_url}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = Path(args.output).expanduser() if args.output else DEFAULT_OUTPUT_DIR / f"dialogue_test_{timestamp}.mp3"

    start = max(0, args.start_index)
    end = min(len(accounts), start + max(1, args.max_accounts))
    print(f"[accounts] Loaded {len(accounts)} account(s); test range {start + 1}-{end}")

    last_message = ""
    for index, account in enumerate(accounts[start:end], start=start + 1):
        print(f"[try {index}] email={account.email} key={mask_secret(account.api_key)}")
        if args.voice_a:
            voice_ids = [args.voice_a, args.voice_b or args.voice_a]
        elif curl_payload.get("inputs"):
            voice_ids = [
                str(item.get("voice_id", ""))
                for item in curl_payload.get("inputs", [])
                if isinstance(item, dict) and item.get("voice_id")
            ]
            if len(voice_ids) == 1:
                voice_ids.append(voice_ids[0])
            elif len(voice_ids) < 1:
                voice_ids = load_dialogue_voices(DEFAULT_VOICES_FILE)
        elif args.mode == "api":
            voice_ids = list_account_voice_ids(account.api_key, proxy_url) or load_dialogue_voices(DEFAULT_VOICES_FILE)
        else:
            voice_ids = load_dialogue_voices(DEFAULT_VOICES_FILE)
        print(f"         voices={voice_ids[0]} / {voice_ids[1]}")
        inputs = curl_payload.get("inputs") if curl_payload.get("inputs") else build_sample_inputs(voice_ids)
        if args.mode == "web":
            hcaptcha_token = curl_hcaptcha or get_hcaptcha_token_from_args(args)
            if not hcaptcha_token:
                raise RuntimeError(
                    "Mode web cần hcaptcha token từ request browser. "
                    "Truyền --hcaptcha-token, --hcaptcha-file hoặc env ELEVENLABS_HCAPTCHA_TOKEN."
                )
            jwt_token = curl_jwt or args.jwt_token.strip()
            if not jwt_token:
                print("         login JWT bằng email/password trong accounts-test.txt...")
                jwt_token, login_msg = login_web_jwt(account.email, account.password, None)
                if not jwt_token:
                    print(f"         {login_msg}")
                    last_message = login_msg
                    continue
            ok, message = text_to_dialogue_stream_web(
                jwt_token=jwt_token,
                hcaptcha_token=hcaptcha_token,
                inputs=inputs,
                output_file=output_file,
                proxy_url=proxy_url,
                model_id=str(curl_payload.get("model_id", args.model_id)),
                stability=float((curl_payload.get("settings") or {}).get("stability", args.stability)) if isinstance(curl_payload.get("settings"), dict) else args.stability,
            )
        else:
            ok, message = text_to_dialogue_stream(
                api_key=account.api_key,
                inputs=inputs,
                output_file=output_file,
                proxy_url=proxy_url,
                model_id=args.model_id,
                stability=args.stability,
                output_format=args.output_format,
            )
        print(f"         {message}")
        last_message = message
        if ok:
            return 0

    print(f"[failed] Không có account nào chạy thành công. Last: {last_message}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Test ElevenLabs Text-to-Dialogue bằng accounts-test.txt")
    parser.add_argument("--mode", choices=("web", "api"), default="web", help="web=JWT+hcaptcha giống browser; api=xi-api-key")
    parser.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS_FILE), help="File email|password|user|api_key")
    parser.add_argument("--proxy-key", default="", help="ProxyXOAY key. Có thể dùng env PROXYXOAY_KEY.")
    parser.add_argument("--proxy", default="", help="Proxy tĩnh. Hỗ trợ ip:port:user:pass hoặc URL http://user:pass@ip:port")
    parser.add_argument("--no-proxy", action="store_true", help="Không dùng proxy")
    parser.add_argument("--max-accounts", type=int, default=5, help="Số account/API key tối đa để thử")
    parser.add_argument("--start-index", type=int, default=0, help="Index 0-based trong accounts file")
    parser.add_argument("--shuffle", action="store_true", help="Random thứ tự account trước khi thử")
    parser.add_argument("--voice-a", default="", help="Voice ID speaker A")
    parser.add_argument("--voice-b", default="", help="Voice ID speaker B")
    parser.add_argument("--model-id", default="eleven_v3")
    parser.add_argument("--stability", type=float, default=0.5)
    parser.add_argument("--output-format", default="mp3_44100_128")
    parser.add_argument("--output", default="", help="File MP3 output")
    parser.add_argument("--jwt-token", default="", help="JWT Bearer token lấy từ web. Nếu bỏ trống sẽ login bằng accounts file.")
    parser.add_argument("--hcaptcha-token", default="", help="hcaptcha_token lấy từ request web")
    parser.add_argument("--hcaptcha-file", default="", help="File text chứa hcaptcha_token")
    parser.add_argument("--no-auto-hcaptcha", action="store_true", help="Không tự lấy hCaptcha bằng Chrome headless")
    parser.add_argument("--show-hcaptcha-browser", action="store_true", help="Mở Chrome có UI khi lấy hCaptcha thay vì headless")
    parser.add_argument("--curl-file", default="", help="File chứa nguyên curl request web để tự tách JWT/hcaptcha/payload")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_test(args)
    except KeyboardInterrupt:
        print("\n[stop] User interrupted")
        return 130
    except Exception as exc:
        print(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
