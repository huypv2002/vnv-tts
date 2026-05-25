"""
PySide6 Auto Update Dialog
- Hiện popup khi có bản mới
- Download với progress bar
- Tự động đóng app → replace exe → restart
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, Signal, QObject

from services.update_service import UpdateInfo, UpdateService


class _DownloadSignals(QObject):
    progress = Signal(float)
    finished = Signal(str)   # path or ""
    error = Signal(str)


class QtUpdateDialog(QtWidgets.QDialog):
    """Modal dialog bắt buộc cập nhật khi có bản mới"""

    def __init__(self, parent, update_service: UpdateService, update_info: UpdateInfo):
        super().__init__(parent)
        self.update_service = update_service
        self.update_info = update_info
        self._download_path: Optional[str] = None
        self._signals = _DownloadSignals()

        self.setWindowTitle("Cập nhật ứng dụng")
        self.setFixedSize(560, 420)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setModal(True)

        self._build_ui()
        self._connect_signals()

    # ----------------------------------------------------------
    # UI
    # ----------------------------------------------------------
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(12)

        # Title
        title = QtWidgets.QLabel("🔄 Có bản cập nhật mới!")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #2196F3;")
        layout.addWidget(title)

        # Version info
        cur = self.update_service.get_current_version_info()
        info_text = (
            f"Version hiện tại:  {cur['version']}\n"
            f"Version mới:        {self.update_info.version}"
        )
        if self.update_info.file_size:
            size_mb = self.update_info.file_size / (1024 * 1024)
            info_text += f"  ({size_mb:.1f} MB)"

        lbl_info = QtWidgets.QLabel(info_text)
        lbl_info.setStyleSheet("font-size: 13px;")
        layout.addWidget(lbl_info)

        # Mandatory warning
        if self.update_info.is_mandatory:
            lbl_warn = QtWidgets.QLabel("⚠️ Bản cập nhật này là bắt buộc!")
            lbl_warn.setStyleSheet("font-size: 13px; font-weight: bold; color: #F44336;")
            layout.addWidget(lbl_warn)

        # Changelog
        if self.update_info.changelog:
            grp = QtWidgets.QGroupBox("Thay đổi")
            grp_layout = QtWidgets.QVBoxLayout(grp)
            txt = QtWidgets.QTextEdit()
            txt.setReadOnly(True)
            txt.setPlainText(self.update_info.changelog)
            txt.setMaximumHeight(140)
            grp_layout.addWidget(txt)
            layout.addWidget(grp)

        # Progress
        self._progress_frame = QtWidgets.QWidget()
        pf_layout = QtWidgets.QVBoxLayout(self._progress_frame)
        pf_layout.setContentsMargins(0, 0, 0, 0)
        self._lbl_progress = QtWidgets.QLabel("")
        self._lbl_progress.setStyleSheet("font-size: 12px;")
        pf_layout.addWidget(self._lbl_progress)
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        pf_layout.addWidget(self._progress_bar)
        self._progress_frame.setVisible(False)
        layout.addWidget(self._progress_frame)

        layout.addStretch()

        # Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self._btn_download = QtWidgets.QPushButton("Tải xuống và cài đặt")
        self._btn_download.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; font-size: 13px; "
            "font-weight: bold; padding: 8px 20px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #45a049; }"
            "QPushButton:disabled { background-color: #9E9E9E; }"
        )
        btn_layout.addWidget(self._btn_download)

        self._btn_later = QtWidgets.QPushButton("Để sau")
        self._btn_later.setStyleSheet("font-size: 13px; padding: 8px 16px;")
        if self.update_info.is_mandatory:
            self._btn_later.setEnabled(False)
        btn_layout.addWidget(self._btn_later)

        layout.addLayout(btn_layout)

    def _connect_signals(self):
        self._btn_download.clicked.connect(self._start_download)
        self._btn_later.clicked.connect(self.reject)
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_download_finished)
        self._signals.error.connect(self._on_download_error)

    # ----------------------------------------------------------
    # DOWNLOAD
    # ----------------------------------------------------------
    def _start_download(self):
        self._btn_download.setEnabled(False)
        self._btn_later.setEnabled(False)
        self._progress_frame.setVisible(True)
        self._lbl_progress.setText("🔄 Đang tải xuống...")
        self._progress_bar.setValue(0)

        t = threading.Thread(target=self._download_thread, daemon=True)
        t.start()

    def _download_thread(self):
        try:
            def cb(pct):
                self._signals.progress.emit(pct)

            path = self.update_service.download_update(
                self.update_info.download_url,
                progress_callback=cb,
            )
            if path:
                self._signals.finished.emit(path)
            else:
                self._signals.error.emit("Download failed")
        except Exception as e:
            self._signals.error.emit(str(e))

    # ----------------------------------------------------------
    # SLOTS
    # ----------------------------------------------------------
    @QtCore.Slot(float)
    def _on_progress(self, pct: float):
        self._progress_bar.setValue(int(pct))
        self._lbl_progress.setText(f"🔄 Đang tải xuống... {pct:.1f}%")

    @QtCore.Slot(str)
    def _on_download_finished(self, path: str):
        self._download_path = path
        self._progress_bar.setValue(100)
        self._lbl_progress.setText("✅ Tải xuống hoàn tất!")

        reply = QtWidgets.QMessageBox.question(
            self,
            "Cập nhật",
            "Tải xuống hoàn tất!\n\n"
            "Ứng dụng sẽ đóng lại, thay thế file exe,\n"
            "và tự động mở lại phiên bản mới.\n\n"
            "Tiếp tục?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )

        if reply == QtWidgets.QMessageBox.Yes:
            self._do_install()
        else:
            self._btn_download.setEnabled(True)
            self._btn_download.setText("Cài đặt ngay")
            self._btn_download.clicked.disconnect()
            self._btn_download.clicked.connect(self._do_install)
            if not self.update_info.is_mandatory:
                self._btn_later.setEnabled(True)

    @QtCore.Slot(str)
    def _on_download_error(self, msg: str):
        self._lbl_progress.setText(f"❌ Lỗi: {msg}")
        self._btn_download.setEnabled(True)
        if not self.update_info.is_mandatory:
            self._btn_later.setEnabled(True)

        QtWidgets.QMessageBox.critical(
            self, "Lỗi tải xuống",
            f"Không thể tải bản cập nhật.\n\n{msg}"
        )

    # ----------------------------------------------------------
    # INSTALL
    # ----------------------------------------------------------
    def _do_install(self):
        if not self._download_path:
            return

        ok = self.update_service.install_update(self._download_path)
        if ok:
            self._lbl_progress.setText("🔄 Đang cài đặt... Ứng dụng sẽ đóng.")
            # Đóng app sau 1.5s
            QtCore.QTimer.singleShot(1500, self._quit_app)
        else:
            QtWidgets.QMessageBox.critical(
                self, "Lỗi",
                f"Không thể cài đặt bản cập nhật.\n\n"
                f"File đã tải: {self._download_path}\n"
                f"Bạn có thể thay thế thủ công."
            )
            self._btn_download.setEnabled(True)

    def _quit_app(self):
        """Đóng toàn bộ app"""
        print("🔄 [UPDATE] Closing app for update...")
        QtWidgets.QApplication.instance().quit()

    # ----------------------------------------------------------
    # OVERRIDE close
    # ----------------------------------------------------------
    def closeEvent(self, event):
        if self.update_info.is_mandatory:
            QtWidgets.QMessageBox.warning(
                self, "Bắt buộc cập nhật",
                "Bạn phải cập nhật để tiếp tục sử dụng."
            )
            event.ignore()
        else:
            event.accept()

    def keyPressEvent(self, event):
        # Block Escape cho mandatory
        if event.key() == Qt.Key_Escape and self.update_info.is_mandatory:
            return
        super().keyPressEvent(event)
