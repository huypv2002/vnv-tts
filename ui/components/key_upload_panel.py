from __future__ import annotations

import tkinter as tk
from tkinter import filedialog
import ttkbootstrap as tb
from typing import Optional, Callable


class KeyUploadPanel:
    """Panel for users to upload their API key files"""
    
    def __init__(self, parent, on_upload_callback: Optional[Callable] = None):
        self.parent = parent
        self.on_upload_callback = on_upload_callback
        
        self.frame = tb.Labelframe(parent, text="API Key Management", padding=10)
        
        # Info section
        info_frame = tb.Frame(self.frame)
        info_frame.pack(fill="x", pady=(0, 10))
        
        info_text = (
            "API Key Management - Parallel Credit Checking\n"
            "✅ Default credits: 9,998 per unused key\n"
            "✅ Parallel processing: 20-30 threads simultaneously\n"
            "✅ Batch processing: 100 keys per batch\n"
            "✅ Background updates: Auto refresh every 5 minutes\n"
            "⚡ Performance: 2000-3000 keys supported"
        )
        
        info_label = tb.Label(
            info_frame,
            text=info_text,
            bootstyle="info",
            font=("Arial", 10),
            justify="left"
        )
        info_label.pack(anchor="w")
        
        # Upload section
        upload_frame = tb.Frame(self.frame)
        upload_frame.pack(fill="x", pady=(0, 10))
        
        # File selection
        file_frame = tb.Frame(upload_frame)
        file_frame.pack(fill="x", pady=(0, 5))
        
        tb.Label(file_frame, text="Key File:", font=("Arial", 10, "bold")).pack(side="left")
        
        self.file_path_var = tk.StringVar()
        self.file_entry = tb.Entry(
            file_frame,
            textvariable=self.file_path_var,
            state="readonly",
            width=50
        )
        self.file_entry.pack(side="left", fill="x", expand=True, padx=(10, 5))
        
        browse_btn = tb.Button(
            file_frame,
            text="Browse...",
            bootstyle="secondary-outline",
            command=self.browse_file,
            width=10
        )
        browse_btn.pack(side="right")
        
        # Upload button
        button_frame = tb.Frame(upload_frame)
        button_frame.pack(fill="x")
        
        self.upload_btn = tb.Button(
            button_frame,
            text="Upload API Keys",
            bootstyle="success",
            command=self.upload_file,
            state="disabled",
            width=20
        )
        self.upload_btn.pack(side="left")
        
        # Progress bar
        self.progress = tb.Progressbar(
            button_frame,
            mode="indeterminate",
            bootstyle="success"
        )
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))
        
        # Status section
        status_frame = tb.Frame(self.frame)
        status_frame.pack(fill="x")
        
        self.status_label = tb.Label(
            status_frame,
            text="No file selected",
            bootstyle="secondary",
            font=("Arial", 9)
        )
        self.status_label.pack(anchor="w")
        
        # Key file info section
        self.info_frame = tb.Frame(self.frame)
        self.info_frame.pack(fill="x", pady=(10, 0))
        
        self.refresh_key_info()
    
    def browse_file(self):
        """Open file dialog to select key file"""
        file_path = filedialog.askopenfilename(
            title="Select API Key File",
            filetypes=[
                ("Text files", "*.txt"),
                ("All files", "*.*")
            ],
            parent=self.parent
        )
        
        if file_path:
            self.file_path_var.set(file_path)
            self.validate_file(file_path)
    
    def validate_file(self, file_path: str):
        """Validate selected file"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            keys = [line.strip() for line in content.split('\n') if line.strip()]
            key_count = len(keys)
            
            if key_count == 0:
                self.status_label.config(
                    text="❌ File contains no valid API keys",
                    bootstyle="danger"
                )
                self.upload_btn.config(state="disabled")
                return
            
            if key_count > 2000:
                self.status_label.config(
                    text=f"❌ Too many keys ({key_count}). Maximum 2000 allowed.",
                    bootstyle="danger"
                )
                self.upload_btn.config(state="disabled")
                return
            
            # Validate key format (basic check)
            invalid_keys = 0
            for key in keys[:10]:  # Check first 10 keys
                if not key.startswith('sk_') or len(key) < 20:
                    invalid_keys += 1
            
            if invalid_keys > 0:
                self.status_label.config(
                    text=f"⚠️ {key_count} keys found, but some may be invalid format",
                    bootstyle="warning"
                )
            else:
                self.status_label.config(
                    text=f"✅ {key_count} valid API keys found",
                    bootstyle="success"
                )
            
            self.upload_btn.config(state="normal")
            
        except Exception as e:
            self.status_label.config(
                text=f"❌ Error reading file: {str(e)}",
                bootstyle="danger"
            )
            self.upload_btn.config(state="disabled")
    
    def upload_file(self):
        """Upload the selected file"""
        file_path = self.file_path_var.get()
        if not file_path:
            return
        
        # Show progress
        self.progress.start()
        self.upload_btn.config(state="disabled", text="Uploading...")
        self.status_label.config(text="Uploading keys...", bootstyle="info")
        
        # Call upload callback
        if self.on_upload_callback:
            try:
                success = self.on_upload_callback(file_path)
                
                if success:
                    self.status_label.config(
                        text="✅ Keys uploaded successfully!",
                        bootstyle="success"
                    )
                    self.file_path_var.set("")
                    self.refresh_key_info()
                else:
                    self.status_label.config(
                        text="❌ Upload failed. Check your subscription status.",
                        bootstyle="danger"
                    )
            except Exception as e:
                self.status_label.config(
                    text=f"❌ Upload error: {str(e)}",
                    bootstyle="danger"
                )
        
        # Hide progress
        self.progress.stop()
        self.upload_btn.config(state="normal", text="Upload API Keys")
    
    def refresh_key_info(self):
        """Refresh key file information display"""
        # Clear existing info widgets
        for widget in self.info_frame.winfo_children():
            widget.destroy()
        
        # This would be called from main window with actual data
        # For now, show placeholder
        tb.Label(
            self.info_frame,
            text="Key Files Status: Loading...",
            font=("Arial", 10, "bold")
        ).pack(anchor="w")
    
    def update_key_info(self, key_files_info: list):
        """Update key file information display"""
        # Clear existing info widgets
        for widget in self.info_frame.winfo_children():
            widget.destroy()
        
        if not key_files_info:
            tb.Label(
                self.info_frame,
                text="No key files uploaded yet",
                bootstyle="secondary"
            ).pack(anchor="w")
            return
        
        # Header
        tb.Label(
            self.info_frame,
            text="Your Key Files:",
            font=("Arial", 10, "bold")
        ).pack(anchor="w", pady=(0, 5))
        
        # Create table-like display
        for i, file_info in enumerate(key_files_info):
            file_frame = tb.Frame(self.info_frame)
            file_frame.pack(fill="x", pady=1)
            
            # File name and status
            name_text = f"📄 {file_info['file_name']}"
            status_text = f"({file_info['status']})"
            
            tb.Label(
                file_frame,
                text=name_text,
                font=("Arial", 9)
            ).pack(side="left")
            
            tb.Label(
                file_frame,
                text=status_text,
                font=("Arial", 9),
                bootstyle="info" if file_info['is_active'] else "secondary"
            ).pack(side="left", padx=(5, 0))
            
            # Key count info
            key_text = f"{file_info['remaining_keys']}/{file_info['total_keys']} keys remaining"
            tb.Label(
                file_frame,
                text=key_text,
                font=("Arial", 8),
                bootstyle="secondary"
            ).pack(side="right")
    
    def set_enabled(self, enabled: bool):
        """Enable/disable the upload panel"""
        state = "normal" if enabled else "disabled"
        self.upload_btn.config(state=state)
        
        if not enabled:
            self.status_label.config(
                text="Upload disabled - subscription required",
                bootstyle="warning"
            )
