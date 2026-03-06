# -*- coding: utf-8 -*-
"""
VNV TTS Tool - Vietnamese Text-to-Speech
Standalone tool using Viettel TTS API
Liquid Glass UI inspired by Apple design
"""
from __future__ import annotations
import os
import sys
import json
import time
import threading
import subprocess
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

import requests
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QThreadPool, QRunnable, QObject, Signal, QTimer
from PySide6.QtWidgets import QHeaderView, QGraphicsDropShadowEffect

# ========== App Constants ==========
APP_NAME = "VNV_TTS"
APP_VERSION = "1.0.0"


def _get_app_dir() -> str:
    if "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except:
        return os.getcwd()


APP_DIR = _get_app_dir()
CFG_FILE = os.path.join(APP_DIR, f"{APP_NAME}_config.json")
LOGIN_TEMP_FILE = os.path.join(APP_DIR, "vnv_login_temp.json")
OUTPUT_DIR = os.path.join(APP_DIR, "outputs_vnv")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========== Import Services ==========
try:
    sys.path.insert(0, APP_DIR)
    from services.d1_client import D1Auth, D1Client
    from services.db_config import TTS_WORKER_URL
    from services.vnv_tts_client import (
        VNVTTSClient, VOICES, VOICE_MAP, TTSError, RateLimitError, StopRequested,
    )
    from services.version import VERSION
    from services.updater import UpdateChecker, UpdateDownloader, apply_update
    from services.update_dialog import UpdateDialog
    APP_VERSION = VERSION
    SERVICES_AVAILABLE = True
    print("✅ VNV Services loaded successfully")
except ImportError as e:
    print(f"⚠️ Services not available: {e}")
    SERVICES_AVAILABLE = False


# ========== Liquid Glass Stylesheet ==========
LIQUID_GLASS_STYLE = """
/* ===== LIQUID GLASS THEME - Apple Inspired ===== */

QMainWindow {
    background: qlineargradient(x1:0, y1:0, x2:0.3, y2:1,
        stop:0 #f0f0f5, stop:0.3 #e8eaf0, stop:0.7 #dde0e8, stop:1 #d5d8e2);
}

QGroupBox {
    background: rgba(255, 255, 255, 0.65);
    border: 1px solid rgba(255, 255, 255, 0.8);
    border-radius: 14px;
    margin-top: 14px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
    font-size: 12px;
    color: #1d1d1f;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 16px;
    padding: 2px 10px;
    background: rgba(255, 255, 255, 0.7);
    border-radius: 8px;
    color: #1d1d1f;
}

QLabel {
    color: #1d1d1f;
    font-size: 12px;
    background: transparent;
}

QLineEdit {
    background: rgba(255, 255, 255, 0.6);
    border: 1px solid rgba(0, 0, 0, 0.08);
    border-radius: 10px;
    padding: 8px 14px;
    color: #1d1d1f;
    font-size: 13px;
    selection-background-color: rgba(0, 122, 255, 0.3);
}
QLineEdit:focus {
    border: 1.5px solid rgba(0, 122, 255, 0.5);
    background: rgba(255, 255, 255, 0.8);
}
"""

LIQUID_GLASS_STYLE_2 = """
QComboBox {
    background: rgba(255, 255, 255, 0.6);
    border: 1px solid rgba(0, 0, 0, 0.08);
    border-radius: 10px;
    padding: 6px 12px;
    color: #1d1d1f;
    font-size: 12px;
    min-height: 24px;
}
QComboBox:hover {
    background: rgba(255, 255, 255, 0.8);
    border: 1px solid rgba(0, 122, 255, 0.3);
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox::down-arrow {
    image: none;
    border: none;
}
QComboBox QAbstractItemView {
    background: rgba(255, 255, 255, 0.95);
    border: 1px solid rgba(0, 0, 0, 0.1);
    border-radius: 10px;
    padding: 4px;
    selection-background-color: rgba(0, 122, 255, 0.15);
    selection-color: #007aff;
}

QPushButton {
    background: rgba(255, 255, 255, 0.55);
    border: 1px solid rgba(0, 0, 0, 0.06);
    border-radius: 10px;
    padding: 8px 18px;
    color: #1d1d1f;
    font-size: 12px;
    font-weight: 500;
}
QPushButton:hover {
    background: rgba(255, 255, 255, 0.85);
    border: 1px solid rgba(0, 122, 255, 0.25);
}
QPushButton:pressed {
    background: rgba(0, 122, 255, 0.12);
}
QPushButton:disabled {
    background: rgba(200, 200, 200, 0.3);
    color: #999;
}

QTableWidget {
    background: rgba(255, 255, 255, 0.5);
    border: 1px solid rgba(0, 0, 0, 0.06);
    border-radius: 12px;
    gridline-color: rgba(0, 0, 0, 0.05);
    font-size: 11px;
    selection-background-color: rgba(0, 122, 255, 0.12);
    selection-color: #1d1d1f;
}
QTableWidget::item {
    padding: 4px 8px;
    border-bottom: 1px solid rgba(0, 0, 0, 0.03);
}
QHeaderView::section {
    background: rgba(255, 255, 255, 0.7);
    border: none;
    border-bottom: 1px solid rgba(0, 0, 0, 0.08);
    padding: 6px 8px;
    font-weight: 600;
    font-size: 11px;
    color: #6e6e73;
}
"""

LIQUID_GLASS_STYLE_3 = """
QPlainTextEdit {
    background: rgba(255, 255, 255, 0.45);
    border: 1px solid rgba(0, 0, 0, 0.06);
    border-radius: 12px;
    padding: 8px;
    font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
    font-size: 11px;
    color: #1d1d1f;
}

QDoubleSpinBox, QSpinBox {
    background: rgba(255, 255, 255, 0.6);
    border: 1px solid rgba(0, 0, 0, 0.08);
    border-radius: 8px;
    padding: 4px 8px;
    color: #1d1d1f;
    font-size: 12px;
}

QCheckBox {
    color: #1d1d1f;
    font-size: 12px;
    spacing: 6px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1.5px solid rgba(0, 0, 0, 0.15);
    background: rgba(255, 255, 255, 0.6);
}
QCheckBox::indicator:checked {
    background: rgba(0, 122, 255, 0.85);
    border: 1.5px solid rgba(0, 122, 255, 0.9);
}

QProgressBar {
    background: rgba(0, 0, 0, 0.06);
    border: none;
    border-radius: 6px;
    height: 10px;
    text-align: center;
    font-size: 9px;
    color: #6e6e73;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #007aff, stop:1 #5ac8fa);
    border-radius: 6px;
}

QStatusBar {
    background: rgba(255, 255, 255, 0.5);
    border-top: 1px solid rgba(0, 0, 0, 0.06);
    font-size: 11px;
    color: #6e6e73;
}

QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: rgba(0, 0, 0, 0.15);
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(0, 0, 0, 0.25);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
"""


# ========== Settings ==========
@dataclass
class AppSettings:
    last_voice_id: str = "1"
    speed: float = 1.0
    thread_count: int = 3
    last_folder: str = ""
    char_counter: int = 0

def load_settings() -> AppSettings:
    try:
        if os.path.exists(CFG_FILE):
            with open(CFG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return AppSettings(**{k: v for k, v in data.items() if k in AppSettings.__dataclass_fields__})
    except:
        pass
    return AppSettings()

def save_settings(s: AppSettings):
    try:
        with open(CFG_FILE, 'w', encoding='utf-8') as f:
            json.dump(s.__dict__, f, ensure_ascii=False, indent=2)
    except:
        pass


# ========== Text Processing (cloned from main app) ==========
def read_text_file(path: str) -> str:
    """Read text file with encoding detection"""
    for enc in ['utf-8', 'utf-8-sig', 'cp1252', 'latin-1']:
        try:
            with open(path, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""

def split_by_paragraphs(text: str, max_chars: int = 300) -> List[dict]:
    """Split text into paragraphs, then chunk large paragraphs"""
    lines = text.split('\n')
    paragraphs = []
    current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append('\n'.join(current))
                current = []
        else:
            current.append(stripped)
    if current:
        paragraphs.append('\n'.join(current))

    result = []
    for i, para in enumerate(paragraphs):
        if not para.strip():
            continue
        if len(para) <= max_chars:
            result.append({'index': len(result), 'text': para, 'chars': len(para)})
        else:
            # Split large paragraphs by sentences
            chunks = _split_large_text(para, max_chars)
            for chunk in chunks:
                if chunk.strip():
                    result.append({'index': len(result), 'text': chunk.strip(), 'chars': len(chunk.strip())})
    return result

def _split_large_text(text: str, max_chars: int) -> List[str]:
    """Split large text by sentence boundaries"""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) + 1 <= max_chars:
            current = (current + " " + sent).strip() if current else sent
        else:
            if current:
                chunks.append(current)
            if len(sent) > max_chars:
                # Force split very long sentences
                for i in range(0, len(sent), max_chars):
                    chunks.append(sent[i:i + max_chars])
                current = ""
            else:
                current = sent
    if current:
        chunks.append(current)
    return chunks

def collect_txt_files(folder: str) -> List[str]:
    """Collect all .txt files from folder"""
    files = []
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith('.txt'):
            files.append(os.path.join(folder, f))
    return files

def find_ffmpeg() -> Optional[str]:
    """Find ffmpeg binary"""
    # Check bundled
    for name in ['ffmpeg', 'ffmpeg.exe']:
        p = os.path.join(APP_DIR, name)
        if os.path.isfile(p):
            return p
    # Check PATH
    return shutil.which('ffmpeg')


# ========== Worker Signals ==========
class WorkerSignals(QObject):
    progress = Signal(int, int, int)  # row, current, total
    status = Signal(int, str)  # row, status_text
    line_done = Signal(int, str, str)  # row, status, output_path
    finished = Signal()
    error = Signal(str)
    log = Signal(str)
    chars_to_deduct = Signal(int)  # actual chars needing TTS (excluding cached)


# ========== TTS Worker ==========
class TTSWorker(QRunnable):
    """Background worker for TTS processing"""

    def __init__(self, paragraphs: List[dict], voice_id: str, speed: float,
                 output_dir: str, file_base: str, tts_client: VNVTTSClient,
                 thread_count: int = 3):
        super().__init__()
        self.paragraphs = paragraphs
        self.voice_id = voice_id
        self.speed = speed
        self.output_dir = output_dir
        self.file_base = file_base
        self.tts_client = tts_client
        self.thread_count = thread_count
        self.signals = WorkerSignals()
        self._stop = False
        self._lock = threading.Lock()
        self._completed = 0
        self._failed = 0

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.paragraphs)
        mini_dir = os.path.join(self.output_dir, f"{self.file_base}_tts_mini")
        os.makedirs(mini_dir, exist_ok=True)

        # Redirect TTS client log to signal (thread-safe)
        def _signal_log(msg):
            print(msg)
            try:
                self.signals.log.emit(msg)
            except RuntimeError:
                pass
        self.tts_client._log_fn = _signal_log

        self.signals.log.emit(f"🔄 Bắt đầu TTS: {total} đoạn, voice={self.voice_id}, speed={self.speed}, threads={self.thread_count}")

        # Pre-assign proxy keys cho mỗi thread slot
        # Mỗi thread sẽ tự assign khi chạy _process_single

        import concurrent.futures

        # Tạo queue các task cần làm
        pending = []
        for para in self.paragraphs:
            if self._stop:
                break
            idx = para['index']
            out_path = os.path.join(mini_dir, f"{idx:04d}.mp3")

            if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
                with self._lock:
                    self._completed += 1
                self.signals.status.emit(idx, "✅ Cached")
                self.signals.progress.emit(0, self._completed, total)
                continue
            pending.append((para, out_path))

        if not pending:
            self.signals.log.emit("✅ Tất cả đã cached, không cần TTS")
            self.signals.chars_to_deduct.emit(0)
            self._merge_mp3(mini_dir, total)
            self.signals.finished.emit()
            return

        # Tính tổng ký tự thực sự cần TTS (không cached)
        actual_chars = sum(len(p['text']) for p, _ in pending)
        self.signals.chars_to_deduct.emit(actual_chars)

        self.signals.log.emit(f"📋 {len(pending)} đoạn cần xử lý, {self._completed} đã cached")

        # Submit max 3 luồng song song — stagger chỉ 0.5s cho batch đầu
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_count) as executor:
            futures = {}
            for i, (para, out_path) in enumerate(pending):
                if self._stop:
                    break
                # Chỉ stagger batch đầu tiên (0, 0.5, 1.0s) để tránh burst
                stagger = min(i, self.thread_count - 1) * 0.5 if i < self.thread_count else 0
                future = executor.submit(self._process_single, para, out_path, stagger)
                futures[future] = para['index']

            for future in concurrent.futures.as_completed(futures):
                if self._stop:
                    break
                idx = futures[future]
                try:
                    future.result()
                except Exception as e:
                    self.signals.log.emit(f"❌ Đoạn {idx}: {e}")
                    self.signals.status.emit(idx, f"❌ {str(e)[:30]}")
                    with self._lock:
                        self._failed += 1
                    self.signals.progress.emit(0, self._completed + self._failed, total)

        if self._stop:
            self.signals.log.emit("⏹️ Đã dừng TTS")
            self.signals.finished.emit()
            return

        self.signals.log.emit(f"🔄 Ghép {self._completed} file MP3...")
        self._merge_mp3(mini_dir, total)
        self.signals.finished.emit()

    def _process_single(self, para: dict, out_path: str, initial_delay: float = 0):
        """Process a single paragraph — mỗi thread dùng proxy key riêng"""
        if self._stop:
            return
        idx = para['index']
        text = para['text']
        tid = threading.get_ident() % 10000

        # Stagger chỉ cho batch đầu
        if initial_delay > 0:
            time.sleep(initial_delay)
        if self._stop:
            return

        # Assign proxy key riêng cho thread này
        self.tts_client._proxy.assign_key_for_thread()

        self.signals.status.emit(idx, "🔄 Đang xử lý...")
        self.signals.log.emit(f"🧵 T{tid} → Đoạn {idx} ({len(text)} chars)")

        try:
            self.tts_client.synthesize(text, self.voice_id, self.speed, out_path)
            with self._lock:
                self._completed += 1
            self.signals.status.emit(idx, "✅ Xong")
            self.signals.progress.emit(0, self._completed, len(self.paragraphs))
            self.signals.log.emit(f"✅ T{tid} Đoạn {idx}: xong → {os.path.basename(out_path)}")
        except StopRequested:
            self.signals.status.emit(idx, "⏹️ Dừng")
            return
        except RateLimitError as e:
            self.signals.status.emit(idx, f"⚠️ 429 ({e.provider})")
            raise
        except Exception as e:
            self.signals.status.emit(idx, f"❌ Lỗi")
            raise
        finally:
            self.tts_client._proxy.release_key_for_thread()

    def _merge_mp3(self, mini_dir: str, total: int):
        """Merge individual MP3 files into final output"""
        ffmpeg = find_ffmpeg()
        mp3_files = []
        for i in range(total):
            p = os.path.join(mini_dir, f"{i:04d}.mp3")
            if os.path.exists(p) and os.path.getsize(p) > 100:
                mp3_files.append(p)

        if not mp3_files:
            self.signals.log.emit("⚠️ Không có file MP3 nào để ghép")
            return

        output_path = os.path.join(self.output_dir, f"{self.file_base}.mp3")

        if len(mp3_files) == 1:
            shutil.copy2(mp3_files[0], output_path)
            self.signals.log.emit(f"✅ Output: {output_path}")
            self.signals.line_done.emit(0, "✅ Hoàn thành", output_path)
            return

        if not ffmpeg:
            # Fallback: simple binary concatenation
            self.signals.log.emit("⚠️ FFmpeg không tìm thấy, ghép đơn giản...")
            with open(output_path, 'wb') as out:
                for f in mp3_files:
                    with open(f, 'rb') as inp:
                        out.write(inp.read())
            self.signals.log.emit(f"✅ Output: {output_path}")
            self.signals.line_done.emit(0, "✅ Hoàn thành", output_path)
            return

        # Use ffmpeg concat — escape single quotes in paths for concat list
        list_file = os.path.join(mini_dir, "concat_list.txt")
        with open(list_file, 'w', encoding='utf-8') as f:
            for mp3 in mp3_files:
                # Use absolute path and escape single quotes
                abs_path = os.path.abspath(mp3)
                escaped = abs_path.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        try:
            # Use re-encode instead of -c copy for mixed audio format compatibility
            cmd = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_file,
                   '-c:a', 'libmp3lame', '-b:a', '128k', '-loglevel', 'error', output_path]
            result = subprocess.run(cmd, capture_output=True, timeout=120,
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            # Check if output file was created successfully
            if result.returncode == 0 or (os.path.exists(output_path) and os.path.getsize(output_path) > 100):
                self.signals.log.emit(f"✅ Output: {output_path}")
                self.signals.line_done.emit(0, "✅ Hoàn thành", output_path)
            else:
                # Filter out version banner from stderr, only show actual errors
                stderr_text = result.stderr.decode(errors='replace').strip()
                err_lines = [l for l in stderr_text.splitlines()
                             if not l.startswith(('ffmpeg version', 'built with', 'configuration:', 'lib', '  '))]
                err_msg = '\n'.join(err_lines)[:200] if err_lines else f"returncode={result.returncode}"
                self.signals.log.emit(f"❌ FFmpeg error: {err_msg}")
                # Fallback to binary concat
                self.signals.log.emit("🔄 Thử ghép đơn giản...")
                with open(output_path, 'wb') as out:
                    for mp3f in mp3_files:
                        with open(mp3f, 'rb') as inp:
                            out.write(inp.read())
                self.signals.log.emit(f"✅ Output (fallback): {output_path}")
                self.signals.line_done.emit(0, "✅ Hoàn thành", output_path)
        except Exception as e:
            self.signals.log.emit(f"❌ FFmpeg exception: {e}")
            # Fallback to binary concat
            try:
                with open(output_path, 'wb') as out:
                    for mp3f in mp3_files:
                        with open(mp3f, 'rb') as inp:
                            out.write(inp.read())
                self.signals.log.emit(f"✅ Output (fallback): {output_path}")
                self.signals.line_done.emit(0, "✅ Hoàn thành", output_path)
            except Exception as e2:
                self.signals.log.emit(f"❌ Lỗi ghép: {e2}")
                self.signals.line_done.emit(0, "❌ Lỗi ghép", "")


# ========== Login Dialog - Liquid Glass ==========
class LoginDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, saved_username='', saved_password=''):
        super().__init__(parent)
        self.setWindowTitle("VNV TTS - Đăng nhập")
        self.setModal(True)
        self.setFixedSize(420, 500)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.login_result = None

        self.setStyleSheet("""
            QDialog {
                background: qlineargradient(x1:0, y1:0, x2:0.5, y2:1,
                    stop:0 rgba(245, 245, 250, 0.97),
                    stop:0.4 rgba(235, 238, 245, 0.95),
                    stop:1 rgba(220, 225, 240, 0.97));
                border-radius: 20px;
                border: 1px solid rgba(255, 255, 255, 0.6);
            }
            QLabel {
                color: #1d1d1f;
                font-size: 13px;
                background: transparent;
            }
            QLineEdit {
                background: rgba(255, 255, 255, 0.55);
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 12px;
                padding: 14px 18px;
                color: #1d1d1f;
                font-size: 14px;
                selection-background-color: rgba(0, 122, 255, 0.3);
            }
            QLineEdit:focus {
                border: 1.5px solid rgba(0, 122, 255, 0.5);
                background: rgba(255, 255, 255, 0.75);
            }
            QPushButton {
                border-radius: 12px;
                padding: 14px 30px;
                font-size: 14px;
                font-weight: 600;
            }
        """)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(40, 20, 40, 30)
        layout.setSpacing(0)

        # Close button
        close_row = QtWidgets.QHBoxLayout()
        close_row.addStretch()
        btn_close = QtWidgets.QPushButton("✕")
        btn_close.setFixedSize(32, 32)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet("""
            QPushButton { background: transparent; color: #999; border: none; font-size: 18px; }
            QPushButton:hover { color: #ff3b30; }
        """)
        btn_close.clicked.connect(self.reject)
        close_row.addWidget(btn_close)
        layout.addLayout(close_row)

        layout.addSpacing(15)

        # Icon
        icon_lbl = QtWidgets.QLabel("🇻🇳")
        icon_lbl.setStyleSheet("font-size: 52px;")
        icon_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_lbl)
        layout.addSpacing(8)

        # Title
        title = QtWidgets.QLabel("VNV TTS Tool")
        title.setStyleSheet("""
            font-size: 22px; font-weight: 700;
            color: #007aff; letter-spacing: 2px;
        """)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(4)

        subtitle = QtWidgets.QLabel("Vietnamese Text-to-Speech")
        subtitle.setStyleSheet("color: #86868b; font-size: 12px;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(30)

        # Username
        self.ed_user = QtWidgets.QLineEdit()
        self.ed_user.setPlaceholderText("👤  Tên đăng nhập")
        self.ed_user.setMinimumHeight(50)
        if saved_username:
            self.ed_user.setText(saved_username)
        layout.addWidget(self.ed_user)
        layout.addSpacing(14)

        # Password
        self.ed_pass = QtWidgets.QLineEdit()
        self.ed_pass.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ed_pass.setPlaceholderText("🔒  Mật khẩu")
        self.ed_pass.setMinimumHeight(50)
        if saved_password:
            self.ed_pass.setText(saved_password)
        layout.addWidget(self.ed_pass)
        layout.addSpacing(10)

        # Error label
        self.lbl_error = QtWidgets.QLabel("")
        self.lbl_error.setStyleSheet("color: #ff3b30; font-size: 12px;")
        self.lbl_error.setAlignment(Qt.AlignCenter)
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        layout.addWidget(self.lbl_error)
        layout.addSpacing(18)

        # Login button - glass blue
        self.btn_login = QtWidgets.QPushButton("ĐĂNG NHẬP")
        self.btn_login.setMinimumHeight(52)
        self.btn_login.setCursor(Qt.PointingHandCursor)
        self.btn_login.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(0, 122, 255, 0.85), stop:1 rgba(90, 200, 250, 0.85));
                color: white; border: none;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(0, 122, 255, 0.95), stop:1 rgba(90, 200, 250, 0.95));
            }
            QPushButton:pressed { background: rgba(0, 100, 220, 0.9); }
            QPushButton:disabled { background: rgba(180, 180, 180, 0.4); color: #999; }
        """)
        layout.addWidget(self.btn_login)
        layout.addSpacing(14)

        # Exit button
        self.btn_exit = QtWidgets.QPushButton("THOÁT")
        self.btn_exit.setMinimumHeight(44)
        self.btn_exit.setCursor(Qt.PointingHandCursor)
        self.btn_exit.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #86868b;
                border: 1.5px solid rgba(0, 0, 0, 0.1);
            }
            QPushButton:hover {
                background: rgba(255, 59, 48, 0.08);
                color: #ff3b30;
                border: 1.5px solid rgba(255, 59, 48, 0.3);
            }
        """)
        self.btn_exit.clicked.connect(self._exit_app)
        layout.addWidget(self.btn_exit)
        layout.addStretch()

        # Footer
        footer = QtWidgets.QLabel("© 2026 VNV TTS Tool")
        footer.setStyleSheet("color: #c7c7cc; font-size: 10px;")
        footer.setAlignment(Qt.AlignCenter)
        layout.addWidget(footer)

        # Connections
        self.btn_login.clicked.connect(self._do_login)
        self.ed_user.returnPressed.connect(lambda: self.ed_pass.setFocus())
        self.ed_pass.returnPressed.connect(self._do_login)
        self.center_on_screen()

    def _do_login(self):
        self.lbl_error.setVisible(False)
        username = self.ed_user.text().strip()
        password = self.ed_pass.text().strip()
        if not username or not password:
            self.lbl_error.setText("❌ Vui lòng nhập đầy đủ thông tin")
            self.lbl_error.setVisible(True)
            return

        self.btn_login.setEnabled(False)
        self.btn_login.setText("Đang đăng nhập...")
        QtWidgets.QApplication.processEvents()

        try:
            auth = D1Auth()
            user = auth.sign_in_custom_user_table(username, password)
            if not user:
                self.lbl_error.setText("❌ Sai tên đăng nhập hoặc mật khẩu")
                self.lbl_error.setVisible(True)
                self.btn_login.setEnabled(True)
                self.btn_login.setText("ĐĂNG NHẬP")
                return

            self.login_result = {'user': user, 'auth': auth, 'username': username}
            # Save credentials
            try:
                with open(LOGIN_TEMP_FILE, 'w', encoding='utf-8') as f:
                    json.dump({'username': username, 'password': password}, f)
            except:
                pass
            self.accept()
        except Exception as e:
            self.lbl_error.setText(f"❌ Lỗi kết nối: {str(e)[:50]}")
            self.lbl_error.setVisible(True)
            self.btn_login.setEnabled(True)
            self.btn_login.setText("ĐĂNG NHẬP")

    def center_on_screen(self):
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2)

    def _exit_app(self):
        self.reject()
        QtWidgets.QApplication.quit()
        sys.exit(0)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and hasattr(self, '_drag_pos'):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()


# ========== Main Window ==========
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_user_id = None
        self.current_username = None
        self.supabase = None
        self.auth_service = None
        self._current_worker = None
        self._stop_requested = False
        self._queue_paths: List[str] = []
        self._queue_rows: List[int] = []

        # Login
        if SERVICES_AVAILABLE:
            if not self._do_login():
                QTimer.singleShot(0, QtWidgets.QApplication.instance().quit)
                return

        self.s = load_settings()
        self._stop_event = threading.Event()
        self.tts_client = VNVTTSClient(log_fn=print, stop_event=self._stop_event)
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)
        self._setup_ui()

    def _do_login(self) -> bool:
        saved = {}
        try:
            if os.path.exists(LOGIN_TEMP_FILE):
                with open(LOGIN_TEMP_FILE, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
        except:
            pass

        dlg = LoginDialog(self, saved.get('username', ''), saved.get('password', ''))
        if dlg.exec() != QtWidgets.QDialog.Accepted or not dlg.login_result:
            return False

        result = dlg.login_result
        self.auth_service = result['auth']
        self.supabase = result['auth'].supabase
        self.current_user_id = result['user']['id']
        self.current_username = result['user'].get('username', 'User')
        print(f"✅ Đăng nhập thành công: {self.current_username}")
        return True

    def _setup_ui(self):
        self.setWindowTitle(f"VNV TTS Tool v{APP_VERSION}")
        self.setFixedSize(960, 680)
        QtWidgets.QApplication.setStyle("Fusion")
        self.setStyleSheet(LIQUID_GLASS_STYLE + LIQUID_GLASS_STYLE_2 + LIQUID_GLASS_STYLE_3)

        cw = QtWidgets.QWidget()
        self.setCentralWidget(cw)
        root = QtWidgets.QVBoxLayout(cw)
        root.setContentsMargins(16, 12, 16, 8)
        root.setSpacing(10)

        # ===== Top Row: Voice + Options =====
        top_row = QtWidgets.QHBoxLayout()
        top_row.setSpacing(10)

        # Voice Group
        grp_voice = QtWidgets.QGroupBox("🎤 Giọng đọc")
        v_layout = QtWidgets.QGridLayout(grp_voice)
        v_layout.setSpacing(8)

        v_layout.addWidget(QtWidgets.QLabel("Voice:"), 0, 0)
        self.cb_voice = QtWidgets.QComboBox()
        self.cb_voice.setMinimumWidth(220)
        # Populate voices
        for voice in VOICES:
            label = f"{voice.name} ({voice.gender}, {voice.region}) [{voice.provider}]"
            self.cb_voice.addItem(label, userData=voice.voice_id)
        # Set saved voice
        for i in range(self.cb_voice.count()):
            if self.cb_voice.itemData(i) == self.s.last_voice_id:
                self.cb_voice.setCurrentIndex(i)
                break
        v_layout.addWidget(self.cb_voice, 0, 1, 1, 2)

        v_layout.addWidget(QtWidgets.QLabel("Speed:"), 1, 0)
        self.sb_speed = QtWidgets.QDoubleSpinBox()
        self.sb_speed.setRange(0.5, 2.0)
        self.sb_speed.setValue(self.s.speed)
        self.sb_speed.setSingleStep(0.1)
        v_layout.addWidget(self.sb_speed, 1, 1)

        # Thread count — max 3 luồng song song
        self.sb_thread = QtWidgets.QSpinBox()
        self.sb_thread.setRange(1, 5)
        self.sb_thread.setValue(3)
        self.sb_thread.setVisible(False)

        top_row.addWidget(grp_voice, 6)

        # Rate Limit Status + Proxy
        grp_status = QtWidgets.QGroupBox("📡 Trạng thái API")
        s_layout = QtWidgets.QVBoxLayout(grp_status)
        self.lbl_viettel_status = QtWidgets.QLabel("Viettel: ✅ Sẵn sàng")
        self.lbl_viettel_status.setStyleSheet("color: #34c759; font-weight: 600;")
        s_layout.addWidget(self.lbl_viettel_status)
        s_layout.addStretch()
        top_row.addWidget(grp_status, 3)

        root.addLayout(top_row)


        # ===== Batch Job Group =====
        grp_batch = QtWidgets.QGroupBox("📁 Batch Job")
        b_layout = QtWidgets.QHBoxLayout(grp_batch)

        # Left: path + buttons
        left_w = QtWidgets.QWidget()
        left_l = QtWidgets.QVBoxLayout(left_w)
        left_l.setContentsMargins(0, 0, 5, 0)

        path_row = QtWidgets.QHBoxLayout()
        path_row.addWidget(QtWidgets.QLabel("Đường dẫn:"))
        self.ed_path = QtWidgets.QLineEdit()
        self.ed_path.setPlaceholderText("Chọn file .txt hoặc thư mục...")
        path_row.addWidget(self.ed_path)
        left_l.addLayout(path_row)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(6)
        self.btn_file = QtWidgets.QPushButton("📄 File")
        self.btn_file.setToolTip("Chọn 1 file .txt")
        self.btn_folder = QtWidgets.QPushButton("📁 Folder")
        self.btn_folder.setToolTip("Chọn thư mục chứa file .txt")
        btn_row.addWidget(self.btn_file)
        btn_row.addWidget(self.btn_folder)
        btn_row.addStretch()

        self.lbl_result = QtWidgets.QLabel("Kết quả: 0/0")
        self.lbl_result.setStyleSheet("""
            border: 1px solid rgba(0,0,0,0.08);
            border-radius: 8px;
            padding: 4px 10px;
            background: rgba(255,255,255,0.5);
            font-weight: 500;
        """)
        btn_row.addWidget(self.lbl_result)
        left_l.addLayout(btn_row)
        left_l.addStretch()

        # Right: file queue table
        right_w = QtWidgets.QWidget()
        right_l = QtWidgets.QVBoxLayout(right_w)
        right_l.setContentsMargins(5, 0, 0, 0)

        self.tbl_queue = QtWidgets.QTableWidget(0, 4)
        self.tbl_queue.setHorizontalHeaderLabels(["#", "File", "Trạng thái", "Tiến độ"])
        hdr = self.tbl_queue.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(0, 35)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(2, 100)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_queue.setColumnWidth(3, 90)
        self.tbl_queue.verticalHeader().setVisible(False)
        self.tbl_queue.verticalHeader().setDefaultSectionSize(22)
        right_l.addWidget(self.tbl_queue)

        b_layout.addWidget(left_w, 35)
        b_layout.addWidget(right_w, 65)
        grp_batch.setFixedHeight(130)
        root.addWidget(grp_batch)

        # ===== Subtitles / Content Table =====
        grp_sub = QtWidgets.QGroupBox("📝 Nội dung")
        sub_layout = QtWidgets.QVBoxLayout(grp_sub)

        # Control buttons
        ctrl_row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("▶ Start")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(52, 199, 89, 0.75), stop:1 rgba(48, 209, 88, 0.75));
                color: white; font-weight: 700; border: none; padding: 10px 28px;
            }
            QPushButton:hover { background: rgba(52, 199, 89, 0.9); }
            QPushButton:disabled { background: rgba(180, 180, 180, 0.3); color: #999; }
        """)
        self.btn_stop = QtWidgets.QPushButton("⏹ Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(255, 59, 48, 0.7), stop:1 rgba(255, 69, 58, 0.7));
                color: white; font-weight: 700; border: none; padding: 10px 28px;
            }
            QPushButton:hover { background: rgba(255, 59, 48, 0.9); }
            QPushButton:disabled { background: rgba(180, 180, 180, 0.3); color: #999; }
        """)
        self.btn_output = QtWidgets.QPushButton("📁 Output")
        ctrl_row.addWidget(self.btn_start)
        ctrl_row.addWidget(self.btn_stop)
        ctrl_row.addWidget(self.btn_output)

        # Nút xem log → mở popup
        self.btn_log = QtWidgets.QPushButton("📋 Log")
        self.btn_log.clicked.connect(self._show_log_popup)
        ctrl_row.addWidget(self.btn_log)

        ctrl_row.addStretch()

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        ctrl_row.addWidget(self.progress_bar, 1)
        sub_layout.addLayout(ctrl_row)

        # Content table
        self.tbl_content = QtWidgets.QTableWidget(0, 5)
        self.tbl_content.setHorizontalHeaderLabels(["#", "Nội dung", "Ký tự", "Voice", "Trạng thái"])
        hdr2 = self.tbl_content.horizontalHeader()
        hdr2.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl_content.setColumnWidth(0, 40)
        hdr2.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr2.setSectionResizeMode(2, QHeaderView.Fixed)
        self.tbl_content.setColumnWidth(2, 55)
        hdr2.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_content.setColumnWidth(3, 90)
        hdr2.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tbl_content.setColumnWidth(4, 110)
        self.tbl_content.verticalHeader().setVisible(False)
        self.tbl_content.verticalHeader().setDefaultSectionSize(32)
        self.tbl_content.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_content.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tbl_content.setWordWrap(True)
        sub_layout.addWidget(self.tbl_content)
        root.addWidget(grp_sub, 1)

        # Log box ẩn (dữ liệu log lưu ở đây, hiển thị qua popup)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setVisible(False)

        # ===== Status Bar =====
        self.statusbar = QtWidgets.QStatusBar()
        self.setStatusBar(self.statusbar)
        username_text = self.current_username or 'Guest'
        self.lbl_user = QtWidgets.QLabel(f"👤 {username_text}")
        self.lbl_user.setStyleSheet("color: #007aff; font-weight: 600; padding: 0 8px;")
        self.lbl_sub_info = QtWidgets.QLabel("📦 Đang tải...")
        self.lbl_sub_info.setStyleSheet("color: #34c759; font-weight: 500; padding: 0 8px;")
        self.lbl_expires = QtWidgets.QLabel("")
        self.lbl_expires.setStyleSheet("color: #ff9500; font-weight: 500; padding: 0 8px;")
        self.statusbar.addPermanentWidget(self.lbl_user)
        self.statusbar.addPermanentWidget(self.lbl_sub_info)
        self.statusbar.addPermanentWidget(self.lbl_expires)

        # ===== Connect Signals =====
        self.btn_file.clicked.connect(self.pick_file)
        self.btn_folder.clicked.connect(self.pick_folder)
        self.btn_start.clicked.connect(self.start_processing)
        self.btn_stop.clicked.connect(self.stop_processing)
        self.btn_output.clicked.connect(self.open_output)
        self.cb_voice.currentIndexChanged.connect(self._save_settings)
        self.sb_speed.valueChanged.connect(self._save_settings)
        self.sb_thread.valueChanged.connect(self._save_settings)

        # Path change debounce
        self._path_timer = QTimer()
        self._path_timer.setSingleShot(True)
        self._path_timer.timeout.connect(self._load_files)
        self.ed_path.textChanged.connect(lambda: self._path_timer.start(500))

        # Rate limit status timer
        self._rate_timer = QTimer()
        self._rate_timer.timeout.connect(self._update_rate_status)
        self._rate_timer.start(2000)

        # Subscription state
        self._sub_remaining = 0
        self._sub_expires_at = None
        self._sub_days_left = None
        self._sub_plan_name = None
        self._refresh_subscription()

        # Refresh subscription every 30s
        self._sub_timer = QTimer()
        self._sub_timer.timeout.connect(self._refresh_subscription)
        self._sub_timer.start(30000)

        self.log(f"🚀 VNV TTS Tool v{APP_VERSION} - Sẵn sàng")
        if find_ffmpeg():
            self.log("✅ FFmpeg found")
        else:
            self.log("⚠️ FFmpeg không tìm thấy - ghép file sẽ dùng phương pháp đơn giản")

        # Auto-update check (delay 3s after startup)
        self._update_checker = None
        self._update_downloader = None
        self._update_dialog = None
        QTimer.singleShot(3000, self._check_for_updates)

    # ========== Methods ==========
    def log(self, text: str):
        print(text)
        self.log_box.appendPlainText(text)
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _show_log_popup(self):
        """Mở popup hiển thị log"""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("📋 Log")
        dlg.resize(700, 450)
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)

        txt = QtWidgets.QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(self.log_box.toPlainText())
        txt.setStyleSheet("""
            QPlainTextEdit {
                background: rgba(30, 30, 30, 0.95);
                color: #e0e0e0;
                font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
                font-size: 12px;
                border-radius: 10px;
                padding: 10px;
            }
        """)
        # Scroll to bottom
        sb = txt.verticalScrollBar()
        sb.setValue(sb.maximum())
        layout.addWidget(txt)

        btn_row = QtWidgets.QHBoxLayout()
        btn_clear = QtWidgets.QPushButton("🗑 Xóa log")
        btn_clear.clicked.connect(lambda: (self.log_box.clear(), txt.clear()))
        btn_close = QtWidgets.QPushButton("Đóng")
        btn_close.clicked.connect(dlg.close)
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        # Auto-update log khi popup đang mở
        timer = QTimer(dlg)
        timer.timeout.connect(lambda: (
            txt.setPlainText(self.log_box.toPlainText()),
            txt.verticalScrollBar().setValue(txt.verticalScrollBar().maximum())
        ))
        timer.start(1000)

        dlg.exec()

    def _save_settings(self):
        self.s.last_voice_id = self.cb_voice.currentData() or "1"
        self.s.speed = self.sb_speed.value()
        self.s.thread_count = self.sb_thread.value()
        save_settings(self.s)

    def _update_rate_status(self):
        """Update rate limit status display"""
        status = self.tts_client.get_rate_status()
        if status['viettel_limited']:
            wait = status['viettel_wait']
            self.lbl_viettel_status.setText(f"Viettel: ⚠️ 429 ({wait:.0f}s)")
            self.lbl_viettel_status.setStyleSheet("color: #ff9500; font-weight: 600;")
        else:
            self.lbl_viettel_status.setText("Viettel: ✅ Sẵn sàng")
            self.lbl_viettel_status.setStyleSheet("color: #34c759; font-weight: 600;")

    def _refresh_subscription(self):
        """Fetch subscription info from server and update UI"""
        if not self.supabase or not self.current_user_id:
            return
        try:
            result = self.supabase.rpc('get_user_subscription', {'user_id': self.current_user_id})
            if result and result.data and not result.error:
                data = result.data
                sub = data.get('subscription')
                if sub:
                    self._sub_remaining = data.get('remaining', 0)
                    self._sub_days_left = sub.get('days_left')
                    self._sub_expires_at = sub.get('expires_at')
                    self._sub_plan_name = sub.get('plan_display_name') or sub.get('plan_name', 'N/A')
                    self.lbl_sub_info.setText(
                        f"📦 {self._sub_plan_name} | 🔤 Còn: {self._sub_remaining:,} ký tự"
                    )
                    if self._sub_remaining <= 0:
                        self.lbl_sub_info.setStyleSheet("color: #ff3b30; font-weight: 600; padding: 0 8px;")
                    elif self._sub_remaining < 1000:
                        self.lbl_sub_info.setStyleSheet("color: #ff9500; font-weight: 500; padding: 0 8px;")
                    else:
                        self.lbl_sub_info.setStyleSheet("color: #34c759; font-weight: 500; padding: 0 8px;")

                    if self._sub_days_left is not None:
                        if self._sub_days_left <= 0:
                            self.lbl_expires.setText("⏰ Hết hạn!")
                            self.lbl_expires.setStyleSheet("color: #ff3b30; font-weight: 600; padding: 0 8px;")
                        elif self._sub_days_left <= 3:
                            self.lbl_expires.setText(f"⏰ Còn {self._sub_days_left} ngày")
                            self.lbl_expires.setStyleSheet("color: #ff9500; font-weight: 600; padding: 0 8px;")
                        else:
                            self.lbl_expires.setText(f"⏰ Còn {self._sub_days_left} ngày")
                            self.lbl_expires.setStyleSheet("color: #8e8e93; font-weight: 500; padding: 0 8px;")
                    else:
                        self.lbl_expires.setText("")
                else:
                    # Expired or no subscription
                    expired = data.get('expired', False)
                    self._sub_remaining = 0
                    self._sub_days_left = 0
                    if expired:
                        self.lbl_sub_info.setText("📦 Gói đã hết hạn!")
                    else:
                        self.lbl_sub_info.setText("📦 Chưa có gói")
                    self.lbl_sub_info.setStyleSheet("color: #ff3b30; font-weight: 600; padding: 0 8px;")
                    self.lbl_expires.setText("")
        except Exception as e:
            print(f"⚠️ Refresh subscription error: {e}")

    def _deduct_characters(self, char_count: int) -> bool:
        """Deduct characters from subscription. Returns True if success."""
        if not self.supabase or not self.current_user_id:
            return True  # No auth = no limit
        try:
            result = self.supabase.rpc('deduct_characters', {
                'user_id': self.current_user_id,
                'chars_used': char_count
            })
            if result and result.data:
                data = result.data
                if data.get('error'):
                    self.log(f"❌ {data['error']}")
                    if data.get('expired'):
                        self.log("⏰ Gói đã hết hạn, vui lòng gia hạn!")
                    elif 'Insufficient' in str(data.get('error', '')):
                        remaining = data.get('remaining', 0)
                        self.log(f"⚠️ Chỉ còn {remaining:,} ký tự, không đủ!")
                    return False
                # Update local remaining
                self._sub_remaining = data.get('remaining', 0)
                return True
            return False
        except Exception as e:
            print(f"⚠️ Deduct error: {e}")
            return True  # On error, allow (don't block user)

    def _log_usage(self, char_count: int, voice_id: str):
        """Log usage to server"""
        if not self.supabase or not self.current_user_id:
            return
        try:
            self.supabase.rpc('log_usage', {
                'user_id': self.current_user_id,
                'characters_used': char_count,
                'voice_id': voice_id,
                'status': 'success'
            })
        except Exception:
            pass

    # ========== Auto-Update ==========
    def _check_for_updates(self):
        """Check GitHub for new version."""
        if not SERVICES_AVAILABLE:
            return
        try:
            self._update_checker = UpdateChecker()
            self._update_checker.result.connect(self._on_update_checked)
            self._update_checker.start()
        except Exception as e:
            print(f"⚠️ Update check error: {e}")

    def _on_update_checked(self, has_update: bool, tag: str, url: str, notes: str, error: str):
        if error:
            print(f"⚠️ Update check: {error}")
            return
        if not has_update:
            return
        self.log(f"🔔 Phiên bản mới: {tag}")
        self._update_dialog = UpdateDialog(tag, notes, self)
        self._update_dialog.update_requested.connect(lambda: self._start_download(url))
        self._update_dialog.exec()

    def _start_download(self, url: str):
        if not self._update_dialog:
            return
        self._update_dialog.set_downloading(True)
        self._update_downloader = UpdateDownloader(url)
        self._update_downloader.progress.connect(self._update_dialog.set_progress)
        self._update_downloader.finished.connect(self._on_download_finished)
        self._update_downloader.start()

    def _on_download_finished(self, ok: bool, path_or_error: str):
        if not self._update_dialog:
            return
        if ok:
            self._update_dialog.set_ready_to_install()
            self._update_dialog.btn_install.clicked.connect(
                lambda: apply_update(path_or_error)
            )
        else:
            self._update_dialog.set_error(path_or_error)

    def pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Chọn file TXT", self.s.last_folder or "",
            "Text Files (*.txt);;All Files (*)")
        if path:
            self.ed_path.setText(path)
            self.s.last_folder = os.path.dirname(path)
            save_settings(self.s)

    def pick_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chọn thư mục", self.s.last_folder or "")
        if folder:
            self.ed_path.setText(folder)
            self.s.last_folder = folder
            save_settings(self.s)

    def open_output(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if sys.platform == 'darwin':
            subprocess.Popen(['open', OUTPUT_DIR])
        elif sys.platform == 'win32':
            os.startfile(OUTPUT_DIR)
        else:
            subprocess.Popen(['xdg-open', OUTPUT_DIR])

    def _load_files(self):
        """Load files from path into queue table"""
        path = self.ed_path.text().strip()
        if not path:
            return

        self._queue_paths = []
        self.tbl_queue.setRowCount(0)
        self.tbl_content.setRowCount(0)

        if os.path.isfile(path) and path.lower().endswith('.txt'):
            self._queue_paths = [path]
        elif os.path.isdir(path):
            self._queue_paths = collect_txt_files(path)
        else:
            self.log(f"⚠️ Đường dẫn không hợp lệ: {path}")
            return

        if not self._queue_paths:
            self.log("⚠️ Không tìm thấy file .txt nào")
            return

        self.tbl_queue.setRowCount(len(self._queue_paths))
        for i, fpath in enumerate(self._queue_paths):
            fname = os.path.basename(fpath)
            self.tbl_queue.setItem(i, 0, self._centered_item(str(i + 1)))
            self.tbl_queue.setItem(i, 1, QtWidgets.QTableWidgetItem(fname))
            self.tbl_queue.setItem(i, 2, self._centered_item("Chờ"))
            self.tbl_queue.setItem(i, 3, self._centered_item("0%"))

        self.log(f"📁 Đã tải {len(self._queue_paths)} file")
        self.lbl_result.setText(f"Kết quả: 0/{len(self._queue_paths)}")

        # Load first file content preview
        if self._queue_paths:
            self._preview_file(self._queue_paths[0])

    def _preview_file(self, fpath: str):
        """Preview file content in the content table"""
        text = read_text_file(fpath)
        if not text.strip():
            self.log(f"⚠️ File rỗng: {fpath}")
            return

        paragraphs = split_by_paragraphs(text)
        self.tbl_content.setRowCount(len(paragraphs))
        voice_name = self.cb_voice.currentText().split(" (")[0] if self.cb_voice.currentText() else "?"

        total_chars = 0
        for para in paragraphs:
            idx = para['index']
            total_chars += para['chars']
            self.tbl_content.setItem(idx, 0, self._centered_item(str(idx + 1)))
            # Truncate long text for display
            display_text = para['text'][:100] + "..." if len(para['text']) > 100 else para['text']
            self.tbl_content.setItem(idx, 1, QtWidgets.QTableWidgetItem(display_text))
            self.tbl_content.setItem(idx, 2, self._centered_item(str(para['chars'])))
            self.tbl_content.setItem(idx, 3, self._centered_item(voice_name))
            self.tbl_content.setItem(idx, 4, self._centered_item("Chờ"))

        self.log(f"📝 {os.path.basename(fpath)}: {len(paragraphs)} đoạn, {total_chars:,} ký tự")

    def _centered_item(self, text: str) -> QtWidgets.QTableWidgetItem:
        item = QtWidgets.QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        return item

    def start_processing(self):
        """Start TTS processing for all queued files"""
        if not self._queue_paths:
            self.log("⚠️ Chưa chọn file nào")
            return

        # Check subscription before starting
        self._refresh_subscription()
        if self._sub_remaining <= 0:
            self.log("❌ Không thể bắt đầu: hết ký tự hoặc gói đã hết hạn!")
            QtWidgets.QMessageBox.warning(
                self, "Hết ký tự",
                "Bạn đã hết ký tự hoặc gói đã hết hạn.\nVui lòng liên hệ admin để gia hạn."
            )
            return

        self._stop_requested = False
        self._stop_event.clear()
        self.tts_client = VNVTTSClient(log_fn=print, stop_event=self._stop_event)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._completed_files = 0
        self._current_file_idx = 0
        self._process_next_file()

    def _process_next_file(self):
        """Process the next file in queue"""
        if self._stop_requested or self._current_file_idx >= len(self._queue_paths):
            self._on_all_done()
            return

        fpath = self._queue_paths[self._current_file_idx]
        fname = os.path.basename(fpath)
        row = self._current_file_idx

        # Update queue table
        self.tbl_queue.setItem(row, 2, self._centered_item("🔄 Đang xử lý"))
        self.log(f"\n🔄 [{row + 1}/{len(self._queue_paths)}] Xử lý: {fname}")

        # Read and split text
        text = read_text_file(fpath)
        if not text.strip():
            self.log(f"⚠️ File rỗng, bỏ qua: {fname}")
            self.tbl_queue.setItem(row, 2, self._centered_item("⚠️ Rỗng"))
            self._current_file_idx += 1
            QTimer.singleShot(100, self._process_next_file)
            return

        # Check remaining chars before processing this file
        file_chars = len(text.strip())
        if self._sub_remaining <= 0:
            self.log(f"❌ Hết ký tự, dừng xử lý: {fname}")
            self.tbl_queue.setItem(row, 2, self._centered_item("❌ Hết ký tự"))
            self._on_all_done()
            return

        paragraphs = split_by_paragraphs(text)
        file_base = os.path.splitext(fname)[0]
        voice_id = self.cb_voice.currentData() or "1"
        speed = self.sb_speed.value()
        thread_count = max(self.sb_thread.value(), 3)

        # Update content table
        self._preview_file(fpath)
        self.progress_bar.setMaximum(len(paragraphs))
        self.progress_bar.setValue(0)

        # Store file info for logging
        self._current_file_chars = 0  # Will be set by chars_to_deduct signal
        self._current_voice_id = voice_id

        # Create worker
        worker = TTSWorker(paragraphs, voice_id, speed, OUTPUT_DIR, file_base,
                           self.tts_client, thread_count)
        self._current_worker = worker

        # Connect signals
        worker.signals.progress.connect(self._on_progress)
        worker.signals.status.connect(self._on_status)
        worker.signals.line_done.connect(self._on_line_done)
        worker.signals.finished.connect(self._on_file_finished)
        worker.signals.log.connect(self.log)
        worker.signals.error.connect(lambda msg: self.log(f"❌ {msg}"))
        worker.signals.chars_to_deduct.connect(self._on_chars_to_deduct)

        self.pool.start(worker)

    def _on_progress(self, row: int, current: int, total: int):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        if self._current_file_idx < self.tbl_queue.rowCount():
            pct = int(current / total * 100) if total > 0 else 0
            self.tbl_queue.setItem(self._current_file_idx, 3, self._centered_item(f"{pct}%"))

    def _on_status(self, idx: int, status: str):
        if idx < self.tbl_content.rowCount():
            self.tbl_content.setItem(idx, 4, self._centered_item(status))

    def _on_chars_to_deduct(self, char_count: int):
        """Called by worker after checking cache — deduct only actual TTS chars"""
        self._current_file_chars = char_count
        if char_count <= 0:
            self.log("✅ Cached hoàn toàn, không trừ ký tự")
            return
        if self._deduct_characters(char_count):
            self.log(f"💰 Trừ {char_count:,} ký tự (thực tế TTS), còn lại: {self._sub_remaining:,}")
        else:
            self.log(f"❌ Không đủ ký tự để trừ {char_count:,}")
            # Stop the worker
            if self._current_worker:
                self._current_worker.stop()
            self._stop_requested = True

    def _on_line_done(self, row: int, status: str, output_path: str):
        if output_path:
            self.log(f"✅ Output: {output_path}")

    def _on_file_finished(self):
        row = self._current_file_idx
        if row < self.tbl_queue.rowCount():
            self.tbl_queue.setItem(row, 2, self._centered_item("✅ Xong"))
            self.tbl_queue.setItem(row, 3, self._centered_item("100%"))

        self._completed_files += 1
        self.lbl_result.setText(f"Kết quả: {self._completed_files}/{len(self._queue_paths)}")

        # Log usage to server
        file_chars = getattr(self, '_current_file_chars', 0)
        voice_id = getattr(self, '_current_voice_id', '')
        if file_chars > 0:
            self._log_usage(file_chars, voice_id)

        # Refresh subscription display
        self._refresh_subscription()

        self._current_file_idx += 1
        QTimer.singleShot(500, self._process_next_file)

    def _on_all_done(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._current_worker = None
        if self._stop_requested:
            self.log("⏹️ Đã dừng xử lý")
        else:
            self.log(f"🎉 Hoàn thành tất cả {self._completed_files}/{len(self._queue_paths)} file")

    def stop_processing(self):
        self._stop_requested = True
        self._stop_event.set()  # Interrupt all sleeps
        self.tts_client.request_stop()
        if self._current_worker:
            self._current_worker.stop()
        self.btn_stop.setEnabled(False)
        self.log("⏹️ Đang dừng...")

    def closeEvent(self, event):
        self._stop_requested = True
        self._stop_event.set()  # Interrupt all sleeps in TTS client
        if self._current_worker:
            self._current_worker.stop()
        self.tts_client.request_stop()
        save_settings(self.s)
        event.accept()
        # Force kill - threads may be stuck in network I/O
        QTimer.singleShot(500, lambda: os._exit(0))


# ========== Entry Point ==========
def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("VNV TTS Tool")

    # Set app-wide font
    font = QtGui.QFont("SF Pro Display", 12)
    font.setStyleStrategy(QtGui.QFont.PreferAntialias)
    app.setFont(font)

    window = MainWindow()
    # Only show if login succeeded (window has centralWidget)
    if window.centralWidget():
        window.show()
        # Center on screen
        screen = app.primaryScreen().geometry()
        x = (screen.width() - window.width()) // 2
        y = (screen.height() - window.height()) // 2
        window.move(x, y)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
