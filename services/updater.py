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
    """Windows: create PowerShell script to replace files (supports UNC paths)."""
    ps_path = os.path.join(app_dir, "_updater.ps1")
    preserve_list = ", ".join(f'"{p}"' for p in PRESERVE_FOLDERS)

    script = f'''# VNV TTS Updater - PowerShell
$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$appDir = "{app_dir}"
$newDir = "{new_app_dir}"
$exeName = "{EXE_NAME}"
$preserve = @({preserve_list}, "_updater.ps1", "_update_tmp")

Write-Host "[VNV TTS Updater] Dang cap nhat..."
Write-Host "Doi ung dung dong..."

# Wait for exe to close
$maxWait = 30
$waited = 0
while ($waited -lt $maxWait) {{
    $proc = Get-Process -Name ($exeName -replace '\\.exe$','') -ErrorAction SilentlyContinue
    if (-not $proc) {{ break }}
    Start-Sleep -Seconds 1
    $waited++
}}
# Force kill if still running
$proc = Get-Process -Name ($exeName -replace '\\.exe$','') -ErrorAction SilentlyContinue
if ($proc) {{ $proc | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }}

Write-Host "Xoa file cu..."
# Delete old files (except preserved)
Get-ChildItem -Path $appDir -Force | Where-Object {{
    $preserve -notcontains $_.Name
}} | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Copy file moi..."
# Copy new files
Get-ChildItem -Path $newDir -Force | Copy-Item -Destination $appDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Khoi dong lai..."
$exePath = Join-Path $appDir $exeName
if (Test-Path $exePath) {{
    Start-Process -FilePath $exePath -WorkingDirectory $appDir
}} else {{
    Write-Host "Khong tim thay $exeName, vui long khoi dong thu cong."
    Read-Host "Nhan Enter de dong"
}}

Write-Host "Don dep..."
Start-Sleep -Seconds 2
Remove-Item -Path (Join-Path $appDir "_update_tmp") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
'''
    with open(ps_path, "w", encoding="utf-8") as f:
        f.write(script)

    # Launch PowerShell hidden (no console window flash)
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ["powershell", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", ps_path],
        creationflags=CREATE_NO_WINDOW,
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
