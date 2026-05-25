from __future__ import annotations

import ttkbootstrap as tb
from ttkbootstrap.dialogs import Messagebox
import json
import os
from cryptography.fernet import Fernet
import base64


class LoginDialog:
    def __init__(self, master, auth) -> None:
        self.master = master
        self._auth = auth
        self._result = False
        self._password_visible = False
        self._remember_password = False
        
        # Load saved credentials
        self._load_saved_credentials()

        self.top = tb.Toplevel(master)
        self.top.title("Đăng nhập - Elevenlabs Auto TTS Subtitles")
        self.top.resizable(True, True) # Enable resizing
        
        # Set min size
        self.top.minsize(400, 300)
        
        # Căn giữa màn hình
        self.center_window()
        
        # Main container scrolling
        # Use ScrolledFrame to handle small screens
        from ttkbootstrap.scrolled import ScrolledFrame
        self.main_scroll = ScrolledFrame(self.top, padding=40, autohide=True)
        self.main_scroll.pack(fill="both", expand=True)
        
        # Content frame inside ScrolledFrame
        main_frame = self.main_scroll
        
        # Logo/Title section
        title_frame = tb.Frame(main_frame)
        title_frame.pack(fill="x", pady=(0, 30))
        
        title_label = tb.Label(
            title_frame, 
            text="ELEVENLABS AUTO TTS", 
            font=("Arial", 24, "bold"),
            bootstyle="primary"
        )
        title_label.pack()
        
        subtitle_label = tb.Label(
            title_frame,
            text="Đăng nhập để tiếp tục sử dụng app",
            font=("Arial", 11)
        )
        subtitle_label.pack(pady=(5, 0))
        
        # Form section
        form_frame = tb.Frame(main_frame)
        form_frame.pack(fill="x", pady=(0, 30))
        
        # Username field
        tb.Label(form_frame, text="Tên đăng nhập", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 5))
        self.username = tb.Entry(form_frame, font=("Arial", 11), width=35)
        self.username.pack(fill="x", pady=(0, 15), ipady=6)
        
        # Password field
        tb.Label(form_frame, text="Mật khẩu", font=("Arial", 10, "bold")).pack(anchor="w", pady=(0, 5))
        
        password_frame = tb.Frame(form_frame)
        password_frame.pack(fill="x", pady=(0, 20))
        
        self.password = tb.Entry(password_frame, show="*", font=("Arial", 11))
        self.password.pack(side="left", fill="x", expand=True, ipady=6)
        
        # Toggle password visibility button
        self.toggle_btn = tb.Button(
            password_frame,
            text="👁",
            width=3,
            bootstyle="secondary-outline",
            command=self.toggle_password_visibility
        )
        self.toggle_btn.pack(side="right", padx=(5, 0))
        
        # Remember password checkbox
        self.remember_var = tb.BooleanVar(value=self._remember_password)
        remember_check = tb.Checkbutton(
            form_frame,
            text="Ghi nhớ mật khẩu",
            variable=self.remember_var,
            bootstyle="success-round-toggle"
        )
        remember_check.pack(anchor="w", pady=(0, 15))
        
        # Buttons section
        button_frame = tb.Frame(main_frame)
        button_frame.pack(fill="x")
        
        # Main login button - full width
        login_btn = tb.Button(
            button_frame,
            text="ĐĂNG NHẬP",
            bootstyle="success",
            command=self.try_login,
            width=25
        )
        login_btn.pack(fill="x", pady=(0, 10), ipady=10)
        
        # Cancel button centered
        cancel_btn = tb.Button(
            button_frame,
            text="Hủy",
            bootstyle="secondary-outline",
            command=self.close,
            width=15
        )
        cancel_btn.pack(pady=(10, 0))

        self.top.bind("<Return>", lambda e: self.try_login())
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        
        # Auto-fill saved credentials
        if hasattr(self, '_saved_username') and self._saved_username:
            self.username.insert(0, self._saved_username)
        if hasattr(self, '_saved_password') and self._saved_password:
            self.password.insert(0, self._saved_password)
        
        # Focus vào username field
        self.username.focus_set()
    
    def _get_credentials_file(self):
        """Get path to credentials file"""
        app_data = os.path.join(os.environ.get('APPDATA', ''), 'audio', 'audio_app')
        os.makedirs(app_data, exist_ok=True)
        return os.path.join(app_data, 'login_credentials.json')
    
    def _get_encryption_key(self):
        """Get or create encryption key"""
        key_file = os.path.join(os.environ.get('APPDATA', ''), 'audio', 'audio_app', 'login_key.key')
        if os.path.exists(key_file):
            with open(key_file, 'rb') as f:
                return f.read()
        else:
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(key_file), exist_ok=True)
            with open(key_file, 'wb') as f:
                f.write(key)
            return key
    
    def _load_saved_credentials(self):
        """Load saved credentials from file"""
        try:
            cred_file = self._get_credentials_file()
            if os.path.exists(cred_file):
                with open(cred_file, 'r') as f:
                    data = json.load(f)
                
                key = self._get_encryption_key()
                fernet = Fernet(key)
                
                encrypted_password = data.get('password', '')
                if encrypted_password:
                    decrypted_password = fernet.decrypt(encrypted_password.encode()).decode()
                    self._saved_username = data.get('username', '')
                    self._saved_password = decrypted_password
                    self._remember_password = True
                else:
                    self._saved_username = ''
                    self._saved_password = ''
                    self._remember_password = False
            else:
                self._saved_username = ''
                self._saved_password = ''
                self._remember_password = False
        except Exception:
            self._saved_username = ''
            self._saved_password = ''
            self._remember_password = False
    
    def _save_credentials(self, username, password):
        """Save credentials to file"""
        try:
            key = self._get_encryption_key()
            fernet = Fernet(key)
            
            encrypted_password = fernet.encrypt(password.encode()).decode()
            
            data = {
                'username': username,
                'password': encrypted_password
            }
            
            cred_file = self._get_credentials_file()
            with open(cred_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Error saving credentials: {e}")
    
    def _clear_saved_credentials(self):
        """Clear saved credentials"""
        try:
            cred_file = self._get_credentials_file()
            if os.path.exists(cred_file):
                os.remove(cred_file)
        except Exception as e:
            print(f"Error clearing credentials: {e}")
    
    def center_window(self):
        """Căn giữa cửa sổ trên màn hình với kích thước thông minh"""
        self.top.update_idletasks()
        
        # Desired size
        target_width = 500
        target_height = 450
        
        # Lấy kích thước màn hình
        screen_width = self.top.winfo_screenwidth()
        screen_height = self.top.winfo_screenheight()
        
        # Clamp height to 90% of screen height
        final_height = min(target_height, int(screen_height * 0.9))
        
        # Tính toán vị trí căn giữa
        x = (screen_width - target_width) // 2
        y = (screen_height - final_height) // 2
        
        self.top.geometry(f"{target_width}x{final_height}+{x}+{y}")
    
    def toggle_password_visibility(self):
        """Chuyển đổi hiển thị/ẩn mật khẩu"""
        if self._password_visible:
            self.password.config(show="*")
            self.toggle_btn.config(text="👁")
            self._password_visible = False
        else:
            self.password.config(show="")
            self.toggle_btn.config(text="🙈")
            self._password_visible = True

    def show_modal(self) -> bool:
        self.top.grab_set()
        self.master.wait_window(self.top)
        return self._result

    def close(self) -> None:
        self._result = False
        self.top.destroy()

    def bypass_login(self) -> None:
        """Bypass login for development/testing purposes"""
        # Set a dummy user for bypass mode
        self._auth._current_user = {
            'id': 3,  # Changed to match your user ID
            'username': 'mavanhuy30',
            'role': 'admin'
        }
        self._result = True
        self.top.destroy()

    def try_login(self) -> None:
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            self._show_error_popup("Vui lòng nhập đầy đủ thông tin đăng nhập", "Thiếu thông tin")
            return
        
        # Disable login button during authentication
        login_btn = None
        for widget in self.top.winfo_children():
            if isinstance(widget, tb.Frame):
                for child in widget.winfo_children():
                    if isinstance(child, tb.Frame):
                        for btn in child.winfo_children():
                            if isinstance(btn, tb.Button) and btn.cget("text") == "ĐĂNG NHẬP":
                                login_btn = btn
                                break
        
        if login_btn:
            login_btn.config(text="Đang đăng nhập...", state="disabled")
            self.top.update()
        
        try:
            user = self._auth.sign_in_custom_user_table(username, password)
            if user is None:
                self._show_error_popup("Đăng nhập thất bại. Vui lòng kiểm tra lại thông tin.", "Lỗi đăng nhập")
                if login_btn:
                    login_btn.config(text="ĐĂNG NHẬP", state="normal")
                return
            
            # Handle remember password
            if self.remember_var.get():
                self._save_credentials(username, password)
            else:
                self._clear_saved_credentials()
            
            # Login successful - user info is automatically stored in auth service
            self._result = True
            self.top.destroy()
            
        except Exception as e:
            self._show_error_popup(f"Có lỗi xảy ra: {str(e)}", "Lỗi")
            if login_btn:
                login_btn.config(text="ĐĂNG NHẬP", state="normal")
    
    def _show_error_popup(self, message, title):
        """Show error popup that can be closed properly"""
        error_window = tb.Toplevel(self.top)
        error_window.title(title)
        error_window.resizable(False, False)
        error_window.geometry("400x150")
        
        # Center the error window
        error_window.update_idletasks()
        x = (error_window.winfo_screenwidth() // 2) - (400 // 2)
        y = (error_window.winfo_screenheight() // 2) - (150 // 2)
        error_window.geometry(f"400x150+{x}+{y}")
        
        # Make it modal
        error_window.grab_set()
        error_window.focus_set()
        
        # Content
        main_frame = tb.Frame(error_window, padding=20)
        main_frame.pack(fill="both", expand=True)
        
        # Error icon and message
        icon_label = tb.Label(main_frame, text="⚠️", font=("Arial", 24))
        icon_label.pack(pady=(0, 10))
        
        message_label = tb.Label(main_frame, text=message, font=("Arial", 11), wraplength=350)
        message_label.pack(pady=(0, 20))
        
        # OK button
        ok_btn = tb.Button(main_frame, text="OK", bootstyle="primary", width=10,
                          command=lambda: self._close_error_popup(error_window))
        ok_btn.pack()
        
        # Bind Enter key to close
        error_window.bind("<Return>", lambda e: self._close_error_popup(error_window))
        error_window.bind("<Escape>", lambda e: self._close_error_popup(error_window))
        
        # Focus on OK button
        ok_btn.focus_set()
    
    def _close_error_popup(self, error_window):
        """Close error popup properly"""
        error_window.grab_release()
        error_window.destroy()


