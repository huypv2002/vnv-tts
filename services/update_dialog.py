# -*- coding: utf-8 -*-
"""
VNV TTS Tool - Update Dialog UI
"""
from __future__ import annotations
from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Signal


class UpdateDialog(QtWidgets.QDialog):
    """Modal dialog showing update info and download progress."""
    update_requested = Signal()

    def __init__(self, tag: str, notes: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cập nhật mới")
        self.setFixedSize(480, 380)
        self.setStyleSheet("""
            QDialog { background: #1a1d27; color: #e4e6f0; }
            QLabel { color: #e4e6f0; }
            QPushButton { padding: 10px 24px; border-radius: 8px; font-weight: 600; font-size: 13px; }
        """)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        # Header
        header = QtWidgets.QLabel(f"🎉 Phiên bản mới: {tag}")
        header.setStyleSheet("font-size: 18px; font-weight: 700; color: #6366f1;")
        layout.addWidget(header)

        # Release notes
        notes_box = QtWidgets.QPlainTextEdit()
        notes_box.setPlainText(notes or "Không có ghi chú.")
        notes_box.setReadOnly(True)
        notes_box.setStyleSheet("""
            QPlainTextEdit {
                background: #242836; border: 1px solid #2e3347; border-radius: 8px;
                padding: 10px; color: #e4e6f0; font-size: 12px;
            }
        """)
        notes_box.setMaximumHeight(160)
        layout.addWidget(notes_box)

        # Progress bar (hidden initially)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { background: #242836; border: 1px solid #2e3347; border-radius: 6px; height: 20px; text-align: center; color: #e4e6f0; }
            QProgressBar::chunk { background: #6366f1; border-radius: 6px; }
        """)
        layout.addWidget(self.progress_bar)

        # Status label
        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setStyleSheet("color: #8b8fa3; font-size: 12px;")
        self.lbl_status.setVisible(False)
        layout.addWidget(self.lbl_status)

        layout.addStretch()

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_later = QtWidgets.QPushButton("Để sau")
        self.btn_later.setStyleSheet("background: #2e3347; color: #e4e6f0; border: none;")
        self.btn_later.clicked.connect(self.reject)

        self.btn_update = QtWidgets.QPushButton("⬇️ Cập nhật ngay")
        self.btn_update.setStyleSheet("background: #6366f1; color: white; border: none;")
        self.btn_update.clicked.connect(self._on_update_click)

        self.btn_install = QtWidgets.QPushButton("🔄 Cài đặt & Khởi động lại")
        self.btn_install.setStyleSheet("background: #22c55e; color: white; border: none;")
        self.btn_install.setVisible(False)

        btn_row.addWidget(self.btn_later)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_update)
        btn_row.addWidget(self.btn_install)
        layout.addLayout(btn_row)

    def _on_update_click(self):
        self.update_requested.emit()

    def set_downloading(self, active: bool):
        self.progress_bar.setVisible(active)
        self.lbl_status.setVisible(active)
        self.btn_update.setEnabled(not active)
        self.btn_later.setEnabled(not active)
        if active:
            self.lbl_status.setText("⬇️ Đang tải...")
            self.progress_bar.setValue(0)

    def set_progress(self, downloaded: int, total: int):
        if total > 0:
            pct = int(downloaded / total * 100)
            self.progress_bar.setMaximum(100)
            self.progress_bar.setValue(pct)
            mb_down = downloaded / 1048576
            mb_total = total / 1048576
            self.lbl_status.setText(f"⬇️ {mb_down:.1f} / {mb_total:.1f} MB ({pct}%)")
        else:
            self.progress_bar.setMaximum(0)  # Indeterminate
            mb_down = downloaded / 1048576
            self.lbl_status.setText(f"⬇️ {mb_down:.1f} MB...")

    def set_ready_to_install(self):
        self.progress_bar.setValue(100)
        self.lbl_status.setText("✅ Tải xong! Sẵn sàng cài đặt.")
        self.lbl_status.setStyleSheet("color: #22c55e; font-size: 12px; font-weight: 600;")
        self.btn_update.setVisible(False)
        self.btn_install.setVisible(True)
        self.btn_later.setEnabled(True)

    def set_error(self, error_msg: str):
        self.lbl_status.setText(f"❌ Lỗi: {error_msg}")
        self.lbl_status.setStyleSheet("color: #ef4444; font-size: 12px;")
        self.btn_update.setEnabled(True)
        self.btn_later.setEnabled(True)
        self.progress_bar.setVisible(False)
