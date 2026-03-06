# -*- coding: utf-8 -*-
"""
VNV TTS Tool - Auto Update System
Checks GitHub Releases for new versions, downloads and applies updates.
"""
from __future__ import annotations
import os
import sys
import json
import shutil
import subprocess
import zipfile
import tempfile
import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

# ============================================================
# CẤU HÌNH — SỬA CHO PHÙ HỢP PROJECT
# ============================================================
GITHUB_OWNER = "huypv2002"
GITHUB_REPO = "vnv-tts"
ASSET_NAME = "VNV-TTS-windows.zip"  # Tên file ZIP trong release
EXE_NAME = "vnv_tts_app.exe"        # Tên exe sau khi build
# ============================================================

# Folders to preserve during update
PRESERVE_FOLDERS = {"data", "output", "outputs_vnv", "_update_tmp", "vnv_login_temp.json", "VNV_TTS_config.json"}

logger = logging.getLogger("updater")


def _get_app_dir() -> str:
    if "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return os.getcwd()


def get_current_version() -> str:
    from services.version import VERSION
    return VERSION


def _parse_version(v: str) -> tuple:
    """Parse version string like '1.0.0' or 'v1.0.0' into tuple of ints."""
    v = v.strip().lstrip("v")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class UpdateChecker(QThread):
    """Check GitHub Releases for new version (background thread)."""
    result = Signal(bool, str, str, str, str)  # has_update, tag, download_url, notes, error

    def run(self):
        import requests as req
        try:
            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
            resp = req.get(url, timeout=10, headers={"Accept": "application/vnd.github.v3+json"})
            if resp.status_code == 404:
                self.result.emit(False, "", "", "", "")
                return
            resp.raise_for_status()
            data = resp.json()

            tag = data.get("tag_name", "")
            notes = data.get("body", "") or ""
            remote_ver = _parse_version(tag)
            local_ver = _parse_version(get_current_version())

            if remote_ver <= local_ver:
                self.result.emit(False, tag, "", notes, "")
                return

            # Find download URL
            download_url = ""
            for asset in data.get("assets", []):
                if asset["name"] == ASSET_NAME:
                    download_url = asset["browser_download_url"]
                    break

            if not download_url:
                # Fallback: use first zip asset
                for asset in data.get("assets", []):
                    if asset["name"].endswith(".zip"):
                        download_url = asset["browser_download_url"]
                        break

            if not download_url:
                self.result.emit(False, tag, "", notes, "No download asset found")
                return

            self.result.emit(True, tag, download_url, notes, "")
        except Exception as e:
            logger.error(f"Update check failed: {e}")
            self.result.emit(False, "", "", "", str(e))


class UpdateDownloader(QThread):
    """Download and extract update ZIP."""
    progress = Signal(int, int)  # downloaded, total
    finished = Signal(bool, str)  # success, path_or_error

    def __init__(self, download_url: str, parent=None):
        super().__init__(parent)
        self._url = download_url

    def run(self):
        import requests as req
        app_dir = _get_app_dir()
        tmp_dir = os.path.join(app_dir, "_update_tmp")

        try:
            # Clean old tmp
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            os.makedirs(tmp_dir, exist_ok=True)

            zip_path = os.path.join(tmp_dir, "update.zip")

            # Download with progress
            resp = req.get(self._url, stream=True, timeout=120)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            # Extract
            extract_dir = os.path.join(tmp_dir, "extracted")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)

            # Find the app folder (may be nested)
            new_app_dir = extract_dir
            # Check if there's a single subfolder containing the exe
            items = os.listdir(extract_dir)
            if len(items) == 1 and os.path.isdir(os.path.join(extract_dir, items[0])):
                candidate = os.path.join(extract_dir, items[0])
                # Check if exe exists in subfolder
                if os.path.exists(os.path.join(candidate, EXE_NAME)):
                    new_app_dir = candidate
                # Or check for vnv_tts_app.py (dev mode)
                elif os.path.exists(os.path.join(candidate, "vnv_tts_app.py")):
                    new_app_dir = candidate

            self.finished.emit(True, new_app_dir)
        except Exception as e:
            logger.error(f"Download failed: {e}")
            self.finished.emit(False, str(e))


def apply_update(new_app_dir: str):
    """Create update script and restart app."""
    app_dir = _get_app_dir()

    if sys.platform == "win32":
        _apply_update_windows(app_dir, new_app_dir)
    else:
        _apply_update_unix(app_dir, new_app_dir)


def _apply_update_windows(app_dir: str, new_app_dir: str):
    """Windows: create batch script to replace files."""
    bat_path = os.path.join(app_dir, "_updater.bat")
    preserve = " ".join(f'"{p}"' for p in PRESERVE_FOLDERS)

    script = f'''@echo off
chcp 65001 >nul
echo [VNV TTS Updater] Đang cập nhật...
echo Đợi ứng dụng đóng...

:wait_loop
tasklist /FI "IMAGENAME eq {EXE_NAME}" 2>NUL | find /I "{EXE_NAME}" >NUL
if %ERRORLEVEL%==0 (
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

echo Xóa file cũ...
for /f "delims=" %%i in ('dir /b /a-d "{app_dir}"') do (
    set "skip=0"
    for %%p in ({preserve} "_updater.bat" "_update_tmp") do (
        if /I "%%i"=="%%p" set "skip=1"
    )
    if "!skip!"=="0" del /q "{app_dir}\\%%i" 2>nul
)

for /f "delims=" %%d in ('dir /b /ad "{app_dir}"') do (
    set "skip=0"
    for %%p in ({preserve} "_update_tmp") do (
        if /I "%%d"=="%%p" set "skip=1"
    )
    if "!skip!"=="0" rmdir /s /q "{app_dir}\\%%d" 2>nul
)

echo Copy file mới...
xcopy /s /e /y /q "{new_app_dir}\\*" "{app_dir}\\" >nul

echo Khởi động lại...
if exist "{app_dir}\\{EXE_NAME}" (
    start "" "{app_dir}\\{EXE_NAME}"
) else (
    echo Không tìm thấy {EXE_NAME}, vui lòng khởi động thủ công.
    pause
)

echo Dọn dẹp...
rmdir /s /q "{app_dir}\\_update_tmp" 2>nul
del "%~f0"
'''
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(script)

    # Launch batch with CREATE_NEW_CONSOLE
    CREATE_NEW_CONSOLE = 0x00000010
    subprocess.Popen(
        [bat_path],
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )
    os._exit(0)


def _apply_update_unix(app_dir: str, new_app_dir: str):
    """macOS/Linux: create shell script to replace files."""
    sh_path = os.path.join(app_dir, "_updater.sh")
    preserve_conditions = " ".join(
        f'-not -name "{p}"' for p in PRESERVE_FOLDERS
    )

    script = f'''#!/bin/bash
echo "[VNV TTS Updater] Đang cập nhật..."
sleep 2

# Xóa file cũ (giữ lại folders cần thiết)
cd "{app_dir}"
for item in *; do
    case "$item" in
        {"|".join(PRESERVE_FOLDERS)}|_updater.sh|_update_tmp) continue ;;
        *) rm -rf "$item" ;;
    esac
done

# Copy file mới
cp -rf "{new_app_dir}/"* "{app_dir}/"

# Dọn dẹp
rm -rf "{app_dir}/_update_tmp"

echo "Cập nhật xong! Vui lòng khởi động lại ứng dụng."
rm -f "{sh_path}"
'''
    with open(sh_path, "w") as f:
        f.write(script)
    os.chmod(sh_path, 0o755)

    subprocess.Popen(
        ["/bin/bash", sh_path],
        start_new_session=True,
        close_fds=True,
    )
    os._exit(0)
