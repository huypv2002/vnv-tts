"""
JWT Accounts Tab - TTS using JWT Authentication
Tab mới cho mode chạy bằng tài khoản email/password

Features:
- Import file TXT chứa accounts (email|password)
- Hiển thị danh sách accounts với trạng thái
- Chạy TTS batch với account rotation
- Tích hợp proxy từ main app
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal, QObject, QRunnable, QThreadPool


def _get_app_dir() -> str:
    """Get application directory"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class JWTWorkerSignals(QObject):
    """Signals for JWT TTS worker"""
    progress = Signal(int, int, str)  # current, total, message
    line_done = Signal(int, bool, str)  # line_idx, success, message
    finished = Signal(int, int)  # success_count, fail_count
    log = Signal(str)


class JWTTTSWorker(QRunnable):
    """Worker để chạy TTS batch với JWT accounts"""
    
    def __init__(
        self,
        service,  # JWTTTSService
        lines: List[str],
        voice_id: str,
        model_id: str,
        output_dir: str,
        voice_settings: Dict,
    ):
        super().__init__()
        self.service = service
        self.lines = lines
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_dir = output_dir
        self.voice_settings = voice_settings
        
        self.signals = JWTWorkerSignals()
        self._stop = False
    
    def stop(self):
        self._stop = True
    
    def run(self):
        """Run TTS batch"""
        success_count = 0
        fail_count = 0
        total = len(self.lines)
        
        for idx, text in enumerate(self.lines):
            if self._stop:
                self.signals.log.emit("⏹️ Stopped by user")
                break
            
            # Skip empty lines
            text = text.strip()
            if not text:
                self.signals.line_done.emit(idx, True, "Empty line - skipped")
                success_count += 1
                continue
            
            # Progress
            self.signals.progress.emit(idx, total, f"Processing line {idx+1}/{total}...")
            
            # Output path
            output_path = os.path.join(self.output_dir, f"line_{idx+1:04d}.mp3")
            
            # Generate TTS
            try:
                result = self.service.generate_tts(
                    text=text,
                    voice_id=self.voice_id,
                    output_path=output_path,
                    model_id=self.model_id,
                    **self.voice_settings
                )
                
                if result.success:
                    success_count += 1
                    self.signals.line_done.emit(idx, True, f"OK: {result.audio_path}")
                else:
                    fail_count += 1
                    self.signals.line_done.emit(idx, False, f"Error: {result.error}")
                    
            except Exception as e:
                fail_count += 1
                self.signals.line_done.emit(idx, False, f"Exception: {e}")
            
            # Small delay
            if idx < total - 1 and not self._stop:
                time.sleep(0.3)
        
        # Done
        self.signals.progress.emit(total, total, f"Done: {success_count} success, {fail_count} failed")
        self.signals.finished.emit(success_count, fail_count)


class JWTAccountsTab(QtWidgets.QWidget):
    """
    Tab để chạy TTS với JWT accounts
    """
    
    def __init__(self, parent=None, log_fn: Callable = None, proxy_fn: Callable = None):
        super().__init__(parent)
        self._parent = parent
        self._log_fn = log_fn or print
        self._proxy_fn = proxy_fn
        
        # Service (lazy init)
        self._service = None
        
        # Worker
        self._worker = None
        self._pool = QThreadPool.globalInstance()
        
        # Data
        self._lines: List[str] = []
        self._accounts_file: str = ""
        
        self._setup_ui()
    
    def _log(self, msg: str):
        """Log message"""
        try:
            self._log_fn(msg)
        except:
            print(msg)
        
        # Also update log text
        if hasattr(self, 'txt_log'):
            self.txt_log.appendPlainText(msg)
    
    def _get_service(self):
        """Lazy init service"""
        if self._service is None:
            from services.jwt_tts_service import JWTTTSService
            self._service = JWTTTSService(
                log_fn=self._log,
                proxy_fn=self._proxy_fn
            )
        return self._service
    
    def _setup_ui(self):
        """Setup UI"""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        
        # === Header ===
        header = QtWidgets.QLabel("🔑 JWT Account Mode - TTS bằng tài khoản ElevenLabs")
        header.setStyleSheet("font-size: 14px; font-weight: bold; color: #e94560;")
        layout.addWidget(header)
        
        # === Accounts Section ===
        grp_accounts = QtWidgets.QGroupBox("Accounts")
        acc_layout = QtWidgets.QHBoxLayout(grp_accounts)
        
        self.btn_load_accounts = QtWidgets.QPushButton("📂 Load Accounts...")
        self.btn_load_accounts.clicked.connect(self._load_accounts)
        acc_layout.addWidget(self.btn_load_accounts)
        
        # 🔧 v2: Thêm input để dán path trực tiếp
        self.ed_accounts_path = QtWidgets.QLineEdit()
        self.ed_accounts_path.setPlaceholderText("Hoặc dán path file accounts vào đây...")
        self.ed_accounts_path.returnPressed.connect(self._load_accounts_from_path)
        acc_layout.addWidget(self.ed_accounts_path, 1)
        
        self.btn_load_path = QtWidgets.QPushButton("📥")
        self.btn_load_path.setFixedWidth(35)
        self.btn_load_path.setToolTip("Load từ path đã nhập")
        self.btn_load_path.clicked.connect(self._load_accounts_from_path)
        acc_layout.addWidget(self.btn_load_path)
        
        self.lbl_accounts = QtWidgets.QLabel("Chưa load accounts")
        self.lbl_accounts.setStyleSheet("color: #888;")
        acc_layout.addWidget(self.lbl_accounts, 1)
        
        self.btn_refresh = QtWidgets.QPushButton("🔄")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setToolTip("Refresh account status")
        self.btn_refresh.clicked.connect(self._refresh_accounts)
        acc_layout.addWidget(self.btn_refresh)
        
        self.btn_check_credits = QtWidgets.QPushButton("💰 Check Credits")
        self.btn_check_credits.setToolTip("Check credits của tất cả accounts")
        self.btn_check_credits.clicked.connect(self._check_all_credits)
        acc_layout.addWidget(self.btn_check_credits)
        
        self.btn_cleanup = QtWidgets.QPushButton("🧹 Cleanup Voices")
        self.btn_cleanup.setToolTip("Xóa custom voices để giải phóng slots")
        self.btn_cleanup.clicked.connect(self._cleanup_voices)
        acc_layout.addWidget(self.btn_cleanup)
        
        layout.addWidget(grp_accounts)
        
        # === Input Section ===
        grp_input = QtWidgets.QGroupBox("Input Text")
        input_layout = QtWidgets.QVBoxLayout(grp_input)
        
        # Buttons row
        btn_row = QtWidgets.QHBoxLayout()
        
        self.btn_load_txt = QtWidgets.QPushButton("📄 Load TXT File...")
        self.btn_load_txt.clicked.connect(self._load_txt_file)
        btn_row.addWidget(self.btn_load_txt)
        
        # 🔧 v2: Thêm input để dán path file TXT
        self.ed_txt_path = QtWidgets.QLineEdit()
        self.ed_txt_path.setPlaceholderText("Hoặc dán path file TXT...")
        self.ed_txt_path.setMinimumWidth(200)
        self.ed_txt_path.returnPressed.connect(self._load_txt_from_path)
        btn_row.addWidget(self.ed_txt_path, 1)
        
        self.btn_load_txt_path = QtWidgets.QPushButton("📥")
        self.btn_load_txt_path.setFixedWidth(35)
        self.btn_load_txt_path.setToolTip("Load từ path đã nhập")
        self.btn_load_txt_path.clicked.connect(self._load_txt_from_path)
        btn_row.addWidget(self.btn_load_txt_path)
        
        self.btn_paste = QtWidgets.QPushButton("📋 Paste Text")
        self.btn_paste.clicked.connect(self._paste_text)
        btn_row.addWidget(self.btn_paste)
        
        self.btn_clear = QtWidgets.QPushButton("🗑️ Clear")
        self.btn_clear.clicked.connect(self._clear_text)
        btn_row.addWidget(self.btn_clear)
        
        btn_row.addStretch()
        
        self.lbl_lines = QtWidgets.QLabel("0 lines")
        btn_row.addWidget(self.lbl_lines)
        
        input_layout.addLayout(btn_row)
        
        # Text preview
        self.txt_input = QtWidgets.QPlainTextEdit()
        self.txt_input.setPlaceholderText("Paste text here or load from file...\nMỗi dòng sẽ được gen thành 1 file MP3")
        self.txt_input.setMaximumHeight(150)
        self.txt_input.textChanged.connect(self._on_text_changed)
        input_layout.addWidget(self.txt_input)
        
        layout.addWidget(grp_input)
        
        # === Voice Settings ===
        grp_voice = QtWidgets.QGroupBox("Voice Settings")
        voice_layout = QtWidgets.QGridLayout(grp_voice)
        
        # Voice ID
        voice_layout.addWidget(QtWidgets.QLabel("Voice ID:"), 0, 0)
        self.ed_voice_id = QtWidgets.QLineEdit()
        self.ed_voice_id.setPlaceholderText("VD: 21m00Tcm4TlvDq8ikWAM")
        self.ed_voice_id.setText("21m00Tcm4TlvDq8ikWAM")  # Rachel default
        voice_layout.addWidget(self.ed_voice_id, 0, 1)
        
        # Model
        voice_layout.addWidget(QtWidgets.QLabel("Model:"), 0, 2)
        self.cb_model = QtWidgets.QComboBox()
        self.cb_model.addItems([
            "eleven_multilingual_v2",
            "eleven_turbo_v2_5",
            "eleven_turbo_v2",
            "eleven_monolingual_v1",
        ])
        voice_layout.addWidget(self.cb_model, 0, 3)
        
        # Stability
        voice_layout.addWidget(QtWidgets.QLabel("Stability:"), 1, 0)
        self.sb_stability = QtWidgets.QDoubleSpinBox()
        self.sb_stability.setRange(0, 1)
        self.sb_stability.setValue(0.5)
        self.sb_stability.setSingleStep(0.1)
        voice_layout.addWidget(self.sb_stability, 1, 1)
        
        # Similarity
        voice_layout.addWidget(QtWidgets.QLabel("Similarity:"), 1, 2)
        self.sb_similarity = QtWidgets.QDoubleSpinBox()
        self.sb_similarity.setRange(0, 1)
        self.sb_similarity.setValue(0.75)
        self.sb_similarity.setSingleStep(0.1)
        voice_layout.addWidget(self.sb_similarity, 1, 3)
        
        # Speed
        voice_layout.addWidget(QtWidgets.QLabel("Speed:"), 2, 0)
        self.sb_speed = QtWidgets.QDoubleSpinBox()
        self.sb_speed.setRange(0.5, 2.0)
        self.sb_speed.setValue(1.0)
        self.sb_speed.setSingleStep(0.1)
        voice_layout.addWidget(self.sb_speed, 2, 1)
        
        # Output dir
        voice_layout.addWidget(QtWidgets.QLabel("Output:"), 2, 2)
        self.ed_output = QtWidgets.QLineEdit()
        self.ed_output.setPlaceholderText("Output directory")
        self.ed_output.setText(os.path.join(_get_app_dir(), "outputs", "jwt_tts"))
        voice_layout.addWidget(self.ed_output, 2, 3)
        
        layout.addWidget(grp_voice)
        
        # === Control Buttons ===
        ctrl_layout = QtWidgets.QHBoxLayout()
        
        self.btn_start = QtWidgets.QPushButton("▶️ START")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:disabled {
                background-color: #6c757d;
            }
        """)
        self.btn_start.clicked.connect(self._start_tts)
        ctrl_layout.addWidget(self.btn_start)
        
        self.btn_stop = QtWidgets.QPushButton("⏹️ STOP")
        self.btn_stop.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
            QPushButton:disabled {
                background-color: #6c757d;
            }
        """)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_tts)
        ctrl_layout.addWidget(self.btn_stop)
        
        ctrl_layout.addStretch()
        
        # Progress
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMinimumWidth(200)
        ctrl_layout.addWidget(self.progress)
        
        self.lbl_status = QtWidgets.QLabel("Ready")
        ctrl_layout.addWidget(self.lbl_status)
        
        layout.addLayout(ctrl_layout)
        
        # === Log ===
        grp_log = QtWidgets.QGroupBox("Log")
        log_layout = QtWidgets.QVBoxLayout(grp_log)
        
        self.txt_log = QtWidgets.QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumHeight(150)
        self.txt_log.setStyleSheet("font-family: monospace; font-size: 11px;")
        log_layout.addWidget(self.txt_log)
        
        layout.addWidget(grp_log)
        
        # Stretch
        layout.addStretch()
    
    # ------------------------------------------------------------------
    # ACTIONS
    # ------------------------------------------------------------------
    def _load_accounts(self):
        """Load accounts từ file TXT"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file accounts (email|password)",
            "",
            "Text Files (*.txt);;All Files (*)"
        )
        
        if not path:
            return
        
        service = self._get_service()
        count = service.load_accounts_from_file(path)
        
        if count > 0:
            self._accounts_file = path
            self.lbl_accounts.setText(f"✅ {count} accounts từ {os.path.basename(path)}")
            self.lbl_accounts.setStyleSheet("color: #28a745;")
            self._log(f"✅ Loaded {count} accounts from {path}")
        else:
            self.lbl_accounts.setText("❌ Không load được accounts")
            self.lbl_accounts.setStyleSheet("color: #dc3545;")
            self._log(f"❌ Failed to load accounts from {path}")
    
    def _load_accounts_from_path(self):
        """🔧 v2: Load accounts từ path được dán vào input"""
        path = self.ed_accounts_path.text().strip()
        
        # Xử lý path có quotes
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path.startswith("'") and path.endswith("'"):
            path = path[1:-1]
        
        if not path:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Vui lòng nhập path file accounts!"
            )
            return
        
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                f"File không tồn tại:\n{path}"
            )
            return
        
        service = self._get_service()
        count = service.load_accounts_from_file(path)
        
        if count > 0:
            self._accounts_file = path
            self.lbl_accounts.setText(f"✅ {count} accounts từ {os.path.basename(path)}")
            self.lbl_accounts.setStyleSheet("color: #28a745;")
            self._log(f"✅ Loaded {count} accounts from {path}")
            self.ed_accounts_path.clear()
        else:
            self.lbl_accounts.setText("❌ Không load được accounts")
            self.lbl_accounts.setStyleSheet("color: #dc3545;")
            self._log(f"❌ Failed to load accounts from {path}")
    
    def _refresh_accounts(self):
        """Refresh account status"""
        if not self._service:
            return
        
        stats = self._service.get_stats()
        self.lbl_accounts.setText(
            f"✅ {stats['ready']} ready / {stats['total']} total | "
            f"🔒 {stats['temp_lock']} locked | 💸 {stats['exhausted']} exhausted"
        )
        self._log(f"📊 Account stats: {stats}")
    
    def _check_all_credits(self):
        """Check credits của tất cả accounts"""
        if not self._service:
            QtWidgets.QMessageBox.warning(self, "Warning", "Chưa load accounts!")
            return
        
        self._log("💰 Checking credits for all accounts...")
        self.btn_check_credits.setEnabled(False)
        self.btn_check_credits.setText("Checking...")
        QtWidgets.QApplication.processEvents()
        
        try:
            # Get proxy
            proxies = None
            if self._proxy_fn:
                proxies = self._proxy_fn()
            
            results = self._service.account_pool.get_all_accounts_credits(proxies)
            
            total_remaining = 0
            for r in results:
                self._log(f"   {r['email']}: {r['remaining']:,} / {r['limit']:,} ({r['state']})")
                total_remaining += r['remaining']
            
            self._log(f"💰 Total remaining credits: {total_remaining:,}")
            
            # Update label
            ready = sum(1 for r in results if r['remaining'] >= 100)
            self.lbl_accounts.setText(
                f"✅ {ready} ready / {len(results)} total | 💰 {total_remaining:,} credits"
            )
            
        except Exception as e:
            self._log(f"❌ Check credits error: {e}")
        finally:
            self.btn_check_credits.setEnabled(True)
            self.btn_check_credits.setText("💰 Check Credits")
    
    def _cleanup_voices(self):
        """Cleanup custom voices của tất cả accounts"""
        if not self._service:
            QtWidgets.QMessageBox.warning(self, "Warning", "Chưa load accounts!")
            return
        
        reply = QtWidgets.QMessageBox.question(
            self, "Confirm",
            "Bạn có chắc muốn xóa tất cả custom voices?\n"
            "Điều này sẽ giải phóng voice slots nhưng không thể hoàn tác.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        
        if reply != QtWidgets.QMessageBox.Yes:
            return
        
        self._log("🧹 Cleaning up voices for all accounts...")
        self.btn_cleanup.setEnabled(False)
        self.btn_cleanup.setText("Cleaning...")
        QtWidgets.QApplication.processEvents()
        
        try:
            # Get proxy
            proxies = None
            if self._proxy_fn:
                proxies = self._proxy_fn()
            
            total_deleted = 0
            for acc_info in self._service.account_pool.get_account_list():
                email = acc_info['email']
                deleted = self._service.account_pool.cleanup_account_voices(email, proxies)
                total_deleted += deleted
                if deleted > 0:
                    self._log(f"   {email}: deleted {deleted} voices")
            
            self._log(f"🧹 Total deleted: {total_deleted} voices")
            QtWidgets.QMessageBox.information(
                self, "Done",
                f"Đã xóa {total_deleted} custom voices!"
            )
            
        except Exception as e:
            self._log(f"❌ Cleanup error: {e}")
        finally:
            self.btn_cleanup.setEnabled(True)
            self.btn_cleanup.setText("🧹 Cleanup Voices")
    
    def _load_txt_file(self):
        """Load text từ file TXT"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file text",
            "",
            "Text Files (*.txt);;All Files (*)"
        )
        
        if not path:
            return
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
            self.txt_input.setPlainText(text)
            self._log(f"📄 Loaded text from {path}")
        except Exception as e:
            self._log(f"❌ Error loading file: {e}")
    
    def _load_txt_from_path(self):
        """🔧 v2: Load text từ path được dán vào input"""
        path = self.ed_txt_path.text().strip()
        
        # Xử lý path có quotes
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path.startswith("'") and path.endswith("'"):
            path = path[1:-1]
        
        if not path:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Vui lòng nhập path file TXT!"
            )
            return
        
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                f"File không tồn tại:\n{path}"
            )
            return
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
            self.txt_input.setPlainText(text)
            self._log(f"📄 Loaded text from {path}")
            self.ed_txt_path.clear()
        except Exception as e:
            self._log(f"❌ Error loading file: {e}")
            QtWidgets.QMessageBox.warning(
                self, "Error",
                f"Không thể đọc file:\n{e}"
            )
    
    def _paste_text(self):
        """Paste từ clipboard"""
        clipboard = QtWidgets.QApplication.clipboard()
        text = clipboard.text()
        if text:
            self.txt_input.setPlainText(text)
            self._log(f"📋 Pasted {len(text)} characters")
    
    def _clear_text(self):
        """Clear text"""
        self.txt_input.clear()
        self._lines = []
        self.lbl_lines.setText("0 lines")
    
    def _on_text_changed(self):
        """Update line count"""
        text = self.txt_input.toPlainText()
        lines = [l for l in text.split('\n') if l.strip()]
        self._lines = lines
        self.lbl_lines.setText(f"{len(lines)} lines")
    
    def _start_tts(self):
        """Start TTS processing"""
        # Validate
        if not self._service or self._service.account_pool.total_accounts == 0:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Chưa load accounts! Vui lòng load file accounts trước."
            )
            return
        
        if not self._lines:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Chưa có text! Vui lòng load hoặc paste text."
            )
            return
        
        voice_id = self.ed_voice_id.text().strip()
        if not voice_id:
            QtWidgets.QMessageBox.warning(
                self, "Warning",
                "Chưa nhập Voice ID!"
            )
            return
        
        # Create output dir
        output_dir = self.ed_output.text().strip()
        if not output_dir:
            output_dir = os.path.join(_get_app_dir(), "outputs", "jwt_tts")
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Voice settings
        voice_settings = {
            'stability': self.sb_stability.value(),
            'similarity_boost': self.sb_similarity.value(),
            'speed': self.sb_speed.value(),
        }
        
        # Create worker
        self._worker = JWTTTSWorker(
            service=self._service,
            lines=self._lines,
            voice_id=voice_id,
            model_id=self.cb_model.currentText(),
            output_dir=output_dir,
            voice_settings=voice_settings,
        )
        
        # Connect signals
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.line_done.connect(self._on_line_done)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.signals.log.connect(self._log)
        
        # Update UI
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setMaximum(len(self._lines))
        self.progress.setValue(0)
        self.lbl_status.setText("Processing...")
        
        # Start
        self._pool.start(self._worker)
        self._log(f"▶️ Started TTS: {len(self._lines)} lines")
    
    def _stop_tts(self):
        """Stop TTS processing"""
        if self._worker:
            self._worker.stop()
            self._log("⏹️ Stopping...")
    
    def _on_progress(self, current: int, total: int, message: str):
        """Handle progress update"""
        self.progress.setValue(current)
        self.lbl_status.setText(message)
    
    def _on_line_done(self, idx: int, success: bool, message: str):
        """Handle line completion"""
        status = "✅" if success else "❌"
        self._log(f"{status} Line {idx+1}: {message}")
    
    def _on_finished(self, success_count: int, fail_count: int):
        """Handle completion"""
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText(f"Done: {success_count} success, {fail_count} failed")
        self._log(f"🏁 Finished: {success_count} success, {fail_count} failed")
        
        # Show stats
        if self._service:
            stats = self._service.get_stats()
            self._log(f"📊 Total chars generated: {stats['total_chars_generated']:,}")
        
        # Refresh accounts
        self._refresh_accounts()


# ============== DEMO ==============
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    
    tab = JWTAccountsTab()
    tab.setWindowTitle("JWT Accounts Tab Demo")
    tab.resize(800, 600)
    tab.show()
    
    sys.exit(app.exec())
