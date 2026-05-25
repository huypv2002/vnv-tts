from __future__ import annotations

import os
import threading
import ttkbootstrap as tb
from ttkbootstrap.dialogs import Messagebox
from typing import Optional, Callable

from services.update_service import UpdateInfo, UpdateService


class UpdateDialog:
    """Dialog để hiển thị thông báo update và download"""
    
    def __init__(self, parent, update_service: UpdateService, update_info: UpdateInfo):
        self.parent = parent
        self.update_service = update_service
        self.update_info = update_info
        self.download_path: Optional[str] = None
        self.download_complete = False
        
        self.top = tb.Toplevel(parent)
        self.top.title("Cập nhật ứng dụng")
        self.top.resizable(False, False)
        self.top.geometry("600x450")
        self.center_window()
        
        # Make modal và đảm bảo ở trên cùng
        self.top.grab_set()
        self.top.focus_set()
        self.top.lift()
        self.top.attributes('-topmost', True)  # Đảm bảo luôn ở trên cùng
        self.top.after(100, lambda: self.top.attributes('-topmost', False))  # Tắt sau khi hiển thị để không block các dialog khác
        
        # Main container
        main_frame = tb.Frame(self.top, padding=30)
        main_frame.pack(fill="both", expand=True)
        
        # Title
        title_label = tb.Label(
            main_frame,
            text="🔄 Có bản cập nhật mới!",
            font=("Arial", 18, "bold"),
            bootstyle="primary"
        )
        title_label.pack(pady=(0, 20))
        
        # Version info
        info_frame = tb.Frame(main_frame)
        info_frame.pack(fill="x", pady=(0, 20))
        
        current_version = self.update_service.get_current_version_info()
        tb.Label(
            info_frame,
            text=f"Version hiện tại: {current_version['version']}",
            font=("Arial", 11)
        ).pack(anchor="w")
        
        tb.Label(
            info_frame,
            text=f"Version mới: {self.update_info.version}",
            font=("Arial", 11, "bold"),
            bootstyle="success"
        ).pack(anchor="w", pady=(5, 0))
        
        # Mandatory warning
        if self.update_info.is_mandatory:
            warning_label = tb.Label(
                info_frame,
                text="⚠️ Bản cập nhật này là bắt buộc!",
                font=("Arial", 11, "bold"),
                bootstyle="danger"
            )
            warning_label.pack(anchor="w", pady=(10, 0))
        
        # Changelog
        if self.update_info.changelog:
            changelog_frame = tb.Labelframe(main_frame, text="Thay đổi", padding=10)
            changelog_frame.pack(fill="both", expand=True, pady=(0, 20))
            
            changelog_text = tb.Text(changelog_frame, height=8, wrap="word", state="disabled")
            changelog_text.pack(fill="both", expand=True)
            changelog_text.config(state="normal")
            changelog_text.insert("1.0", self.update_info.changelog)
            changelog_text.config(state="disabled")
        
        # Progress bar (hidden initially)
        self.progress_frame = tb.Frame(main_frame)
        self.progress_frame.pack(fill="x", pady=(0, 10))
        
        self.progress_label = tb.Label(
            self.progress_frame,
            text="",
            font=("Arial", 10)
        )
        self.progress_label.pack(anchor="w", pady=(0, 5))
        
        self.progress_bar = tb.Progressbar(
            self.progress_frame,
            mode="determinate",
            length=540
        )
        self.progress_bar.pack(fill="x")
        self.progress_frame.pack_forget()  # Hide initially
        
        # Buttons
        button_frame = tb.Frame(main_frame)
        button_frame.pack(fill="x")
        
        if self.update_info.is_mandatory:
            # Mandatory update: chỉ có nút Download
            self.download_btn = tb.Button(
                button_frame,
                text="Tải xuống và cài đặt",
                bootstyle="success",
                width=25,
                command=self.start_download
            )
            self.download_btn.pack(side="left", padx=(0, 10))
            
            self.later_btn = tb.Button(
                button_frame,
                text="Sau",
                bootstyle="secondary-outline",
                width=15,
                command=self.close,
                state="disabled"  # Disabled for mandatory
            )
            self.later_btn.pack(side="left")
        else:
            # Optional update
            self.download_btn = tb.Button(
                button_frame,
                text="Tải xuống và cài đặt",
                bootstyle="success",
                width=25,
                command=self.start_download
            )
            self.download_btn.pack(side="left", padx=(0, 10))
            
            self.later_btn = tb.Button(
                button_frame,
                text="Để sau",
                bootstyle="secondary-outline",
                width=15,
                command=self.close
            )
            self.later_btn.pack(side="left")
        
        self.top.protocol("WM_DELETE_WINDOW", self.on_close)
    
    def center_window(self):
        """Căn giữa cửa sổ trên màn hình"""
        self.top.update_idletasks()
        width = 600
        height = 450
        
        screen_width = self.top.winfo_screenwidth()
        screen_height = self.top.winfo_screenheight()
        
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        
        self.top.geometry(f"{width}x{height}+{x}+{y}")
    
    def _ensure_on_top(self):
        """Đảm bảo dialog luôn ở trên cùng"""
        try:
            if self.top.winfo_exists():
                self.top.lift()
                self.top.focus_set()
                self.top.update()
        except:
            pass
    
    def _show_messagebox_safe(self, messagebox_func, *args, **kwargs):
        """Hiển thị Messagebox một cách an toàn với z-order đúng"""
        try:
            # Release grab tạm thời để Messagebox có thể hiển thị đúng
            self.top.grab_release()
            self._ensure_on_top()
            
            # Hiển thị Messagebox
            result = messagebox_func(*args, **kwargs)
            
            # Grab lại và đảm bảo dialog ở trên cùng
            self.top.grab_set()
            self._ensure_on_top()
            
            return result
        except Exception as e:
            print(f"❌ [UPDATE] Error showing messagebox: {e}")
            # Đảm bảo grab lại nếu có lỗi
            try:
                self.top.grab_set()
                self._ensure_on_top()
            except:
                pass
            return None
    
    def start_download(self):
        """Bắt đầu download update"""
        self.download_btn.config(state="disabled")
        if self.later_btn:
            self.later_btn.config(state="disabled")
        
        # Show progress bar
        self.progress_frame.pack(fill="x", pady=(0, 10))
        self.progress_label.config(text="Đang tải xuống...")
        self.progress_bar["value"] = 0
        self.top.update()
        
        # Download trong thread riêng để không block UI
        thread = threading.Thread(target=self._download_thread, daemon=True)
        thread.start()
    
    def _download_thread(self):
        """Thread để download update"""
        def progress_callback(percent: float):
            # Update progress bar từ main thread
            self.top.after(0, lambda: self._update_progress(percent))
        
        self.download_path = self.update_service.download_update(
            self.update_info.download_url,
            progress_callback=progress_callback
        )
        
        if self.download_path:
            self.download_complete = True
            self.top.after(0, self._on_download_complete)
        else:
            self.top.after(0, self._on_download_error)
    
    def _update_progress(self, percent: float):
        """Update progress bar"""
        try:
            if not self.top.winfo_exists():
                return
            self.progress_bar["value"] = percent
            self.progress_label.config(text=f"Đang tải xuống... {percent:.1f}%")
            self.top.update()
        except Exception as e:
            print(f"❌ [UPDATE] Error updating progress: {e}")
    
    def _on_download_complete(self):
        """Khi download hoàn tất"""
        try:
            # Kiểm tra widget còn tồn tại
            if not self.top.winfo_exists():
                return
            
            self.progress_label.config(text="✅ Tải xuống hoàn tất!")
            self.progress_bar["value"] = 100
            
            # Hỏi có muốn cập nhật ngay không (với z-order đúng)
            result = self._show_messagebox_safe(
                Messagebox.yesno,
                "Tải xuống hoàn tất!",
                "Bạn có muốn cập nhật ứng dụng ngay bây giờ?\n\n"
                "Ứng dụng sẽ được đóng để thay thế file exe.\n"
                "Sau đó ứng dụng sẽ tự động khởi động lại với phiên bản mới.",
                parent=self.top
            )
            
            if result == "Yes":
                # Replace exe
                if self.update_service.install_update(self.download_path):
                    self._show_messagebox_safe(
                        Messagebox.show_info,
                        "Đang cập nhật",
                        "Đang thay thế file ứng dụng...\n\n"
                        "Ứng dụng sẽ đóng trong giây lát.\n"
                        "Sau đó ứng dụng sẽ tự động khởi động lại với phiên bản mới.",
                        parent=self.top
                    )
                    # Đóng ứng dụng sau 2 giây để script replace file
                    self.top.after(2000, lambda: self._close_app())
                else:
                    self._show_messagebox_safe(
                        Messagebox.show_error,
                        "Lỗi",
                        "Không thể thay thế file ứng dụng.\n\n"
                        "Vui lòng đóng ứng dụng và thay thế thủ công:\n"
                        f"{self.download_path}",
                        parent=self.top
                    )
                    try:
                        if self.download_btn.winfo_exists():
                            self.download_btn.config(state="normal")
                    except:
                        pass
                    try:
                        if self.later_btn and self.later_btn.winfo_exists():
                            self.later_btn.config(state="normal")
                    except:
                        pass
            else:
                # Không cập nhật ngay
                self._show_messagebox_safe(
                    Messagebox.show_info,
                    "Đã tải xuống",
                    f"File đã được lưu tại:\n{self.download_path}\n\n"
                    "Bạn có thể cập nhật sau bằng cách:\n"
                    "1. Đóng ứng dụng\n"
                    "2. Thay thế file exe cũ bằng file mới",
                    parent=self.top
                )
                try:
                    if self.download_btn.winfo_exists():
                        self.download_btn.config(state="normal", text="Mở file đã tải")
                        self.download_btn.config(command=self._open_downloaded_file)
                except:
                    pass
                try:
                    if self.later_btn and self.later_btn.winfo_exists():
                        self.later_btn.config(state="normal")
                except:
                    pass
        except Exception as e:
            print(f"❌ [UPDATE] Error in _on_download_complete: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_download_error(self):
        """Khi download lỗi"""
        try:
            # Kiểm tra widget còn tồn tại trước khi config
            if not self.top.winfo_exists():
                return
            
            self.progress_label.config(text="❌ Lỗi khi tải xuống")
            # Hiển thị Messagebox với z-order đúng
            self._show_messagebox_safe(
                Messagebox.show_error,
                "Lỗi",
                "Không thể tải xuống bản cập nhật.\nVui lòng kiểm tra URL download trong database.\n\nLỗi: URL không hợp lệ hoặc không thể truy cập.",
                parent=self.top
            )
            
            # Kiểm tra widget còn tồn tại trước khi config
            try:
                if self.download_btn.winfo_exists():
                    self.download_btn.config(state="normal")
            except:
                pass
            
            try:
                if self.later_btn and self.later_btn.winfo_exists():
                    self.later_btn.config(state="normal")
            except:
                pass
        except Exception as e:
            print(f"❌ [UPDATE] Error in _on_download_error: {e}")
    
    def _open_downloaded_file(self):
        """Mở file đã download"""
        if self.download_path and os.path.exists(self.download_path):
            import subprocess
            import sys
            if sys.platform == "win32":
                os.startfile(self.download_path)
            else:
                subprocess.Popen(["xdg-open", self.download_path])
    
    def _close_app(self):
        """Đóng ứng dụng"""
        self.top.destroy()
        self.parent.quit()
    
    def on_close(self):
        """Xử lý khi đóng dialog"""
        if self.update_info.is_mandatory:
            # Không cho phép đóng nếu là mandatory update
            self._show_messagebox_safe(
                Messagebox.show_warning,
                "Cập nhật bắt buộc",
                "Bạn phải cập nhật để tiếp tục sử dụng ứng dụng.",
                parent=self.top
            )
        else:
            self.close()
    
    def close(self):
        """Đóng dialog"""
        self.top.grab_release()
        self.top.destroy()
    
    def show_modal(self):
        """Hiển thị dialog dạng modal"""
        self.top.wait_window()

