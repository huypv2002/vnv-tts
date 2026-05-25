"""
Auto Update Service — GitHub Releases
Check latest release từ GitHub, download exe, replace và restart.

Flow:
1. GET /repos/{owner}/{repo}/releases/latest → lấy tag_name, assets[].browser_download_url
2. So sánh version_code (tag v3.25 → 325) với CURRENT_VERSION_CODE
3. Download exe asset về cùng thư mục
4. Tạo bat/sh script: đợi app đóng → xóa exe cũ → rename exe mới → mở lại
5. Đóng app → script chạy
"""

from __future__ import annotations

import os
import sys
import re
import logging
import requests
import subprocess
import time
from dataclasses import dataclass
from typing import Optional, Dict, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# Config — CẬP NHẬT KHI RELEASE MỚI
# ============================================================
GITHUB_OWNER = "huypv2002"
GITHUB_REPO = "appTTs"
CURRENT_VERSION = "3.26"
CURRENT_VERSION_CODE = 326  # v3.26 → 326
EXE_NAME = "Elevenlabs_Unlimited.exe"


def _parse_version_code(tag: str) -> int:
    """Parse tag 'v3.25' → 325, 'v4.0' → 400, 'v3.25.1' → 3251"""
    tag = tag.lstrip("vV").strip()
    parts = re.findall(r'\d+', tag)
    if not parts:
        return 0
    if len(parts) == 1:
        return int(parts[0]) * 100
    if len(parts) == 2:
        return int(parts[0]) * 100 + int(parts[1])
    return int(parts[0]) * 1000 + int(parts[1]) * 10 + int(parts[2])


@dataclass
class UpdateInfo:
    tag: str
    version: str
    version_code: int
    download_url: str
    changelog: str
    is_mandatory: bool
    published_at: str
    file_size: int

    def is_newer_than(self, current_code: int) -> bool:
        return self.version_code > current_code


class UpdateService:
    """Check & install updates từ GitHub Releases"""

    def __init__(self):
        self.owner = GITHUB_OWNER
        self.repo = GITHUB_REPO
        self.current_version = CURRENT_VERSION
        self.current_version_code = CURRENT_VERSION_CODE

    # ----------------------------------------------------------
    # CHECK
    # ----------------------------------------------------------
    def check_for_updates(self) -> Optional[UpdateInfo]:
        """Check GitHub Releases API cho bản mới nhất"""
        try:
            url = f"https://api.github.com/repos/{self.owner}/{self.repo}/releases/latest"
            print(f"🔍 [UPDATE] Checking: {url}")

            resp = requests.get(url, timeout=15, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 404:
                print("⚠️ [UPDATE] No releases found")
                return None
            resp.raise_for_status()

            data = resp.json()
            tag = data.get("tag_name", "")
            version_code = _parse_version_code(tag)
            version_str = tag.lstrip("vV")

            # Tìm asset .exe
            download_url = ""
            file_size = 0
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                if name.lower().endswith(".exe"):
                    download_url = asset.get("browser_download_url", "")
                    file_size = asset.get("size", 0)
                    break

            if not download_url:
                print("⚠️ [UPDATE] No .exe asset found in release")
                return None

            # Changelog từ body
            changelog = data.get("body", "") or ""
            # Mandatory nếu có [mandatory] trong body
            is_mandatory = "[mandatory]" in changelog.lower()

            info = UpdateInfo(
                tag=tag,
                version=version_str,
                version_code=version_code,
                download_url=download_url,
                changelog=changelog.replace("[mandatory]", "").replace("[MANDATORY]", "").strip(),
                is_mandatory=is_mandatory,
                published_at=data.get("published_at", ""),
                file_size=file_size,
            )

            print(f"📦 [UPDATE] Latest: {info.version} (code={info.version_code}), current={self.current_version_code}")

            if info.is_newer_than(self.current_version_code):
                size_mb = info.file_size / (1024 * 1024) if info.file_size else 0
                print(f"🆕 [UPDATE] New version available: {info.version} ({size_mb:.1f} MB)")
                return info

            print(f"✅ [UPDATE] Already on latest: {self.current_version}")
            return None

        except Exception as e:
            print(f"❌ [UPDATE] Check error: {e}")
            return None

    # ----------------------------------------------------------
    # DOWNLOAD
    # ----------------------------------------------------------
    def download_update(self, download_url: str, progress_callback: Optional[Callable] = None) -> Optional[str]:
        """Download exe mới về cùng thư mục với app"""
        try:
            print(f"🔽 [UPDATE] Downloading: {download_url}")

            app_dir = self._get_app_dir()
            tmp_path = os.path.join(app_dir, f"{EXE_NAME}.update.tmp")

            resp = requests.get(download_url, stream=True, timeout=60)
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total > 0:
                            progress_callback(downloaded / total * 100)

            size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
            print(f"✅ [UPDATE] Downloaded: {tmp_path} ({size_mb:.1f} MB)")
            return tmp_path

        except Exception as e:
            print(f"❌ [UPDATE] Download error: {e}")
            # Cleanup
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass
            return None

    # ----------------------------------------------------------
    # INSTALL (replace exe + restart)
    # ----------------------------------------------------------
    def install_update(self, update_file: str) -> bool:
        """Tạo script replace exe, đóng app, restart"""
        try:
            if not os.path.exists(update_file):
                print(f"❌ [UPDATE] File not found: {update_file}")
                return False

            app_dir = self._get_app_dir()
            is_frozen = getattr(sys, 'frozen', False)

            if is_frozen:
                current_exe = sys.executable
            else:
                # Dev mode — chỉ thông báo
                print(f"ℹ️ [UPDATE] Dev mode — file downloaded: {update_file}")
                return True

            current_name = os.path.basename(current_exe)
            target_path = os.path.join(app_dir, current_name)

            if sys.platform == "win32":
                return self._install_windows(app_dir, current_exe, target_path, update_file)
            else:
                return self._install_unix(app_dir, current_exe, target_path, update_file)

        except Exception as e:
            print(f"❌ [UPDATE] Install error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _install_windows(self, app_dir, current_exe, target_path, update_file) -> bool:
        """Windows: tạo bat script để replace exe"""
        script_path = os.path.join(app_dir, "_update.bat")
        current_name = os.path.basename(current_exe)

        bat = f'''@echo off
chcp 65001 >nul
echo Đang cập nhật...
REM Đợi app đóng
:wait
tasklist /FI "PID eq %1" 2>NUL | find /I "%1" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait
)
timeout /t 2 /nobreak >nul

REM Xóa exe cũ
del /F /Q "{target_path}" >nul 2>&1

REM Đổi tên file mới
move /Y "{update_file}" "{target_path}" >nul 2>&1

REM Mở app mới
start "" "{target_path}"

REM Xóa script
del /F /Q "%~f0" >nul 2>&1
'''
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(bat)

        pid = os.getpid()
        subprocess.Popen(
            [script_path, str(pid)],
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        print(f"✅ [UPDATE] Bat script launched (PID={pid})")
        return True

    def _install_unix(self, app_dir, current_exe, target_path, update_file) -> bool:
        """macOS/Linux: tạo sh script để replace"""
        script_path = os.path.join(app_dir, "_update.sh")
        pid = os.getpid()

        sh = f'''#!/bin/bash
echo "Đang cập nhật..."
# Đợi app đóng
while kill -0 {pid} 2>/dev/null; do sleep 1; done
sleep 2

# Xóa cũ, đổi tên mới
rm -f "{target_path}" 2>/dev/null
mv "{update_file}" "{target_path}" 2>/dev/null
chmod +x "{target_path}" 2>/dev/null

# Mở app mới
"{target_path}" &

# Xóa script
rm -f "$0" 2>/dev/null
'''
        with open(script_path, "w") as f:
            f.write(sh)
        os.chmod(script_path, 0o755)

        subprocess.Popen([script_path], start_new_session=True)
        print(f"✅ [UPDATE] Shell script launched (PID={pid})")
        return True

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------
    def _get_app_dir(self) -> str:
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def get_current_version_info(self) -> Dict:
        return {
            "version": self.current_version,
            "version_code": self.current_version_code,
        }
