from __future__ import annotations

import ttkbootstrap as tb
from tkinter import ttk


class ControlsPanel:
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent)
        
        # All controls in one compact row
        controls_row = tb.Frame(self.frame)
        controls_row.pack(fill="x")
        
        # Main buttons: Start, Stop, ...
        start_btn = tb.Button(controls_row, text="Bắt đầu", bootstyle="success", width=10, command=self.on_generate)
        start_btn.pack(side="left", padx=(0, 3))
        
        stop_btn = tb.Button(controls_row, text="Dừng", bootstyle="danger", width=10, command=self.on_stop)
        stop_btn.pack(side="left", padx=(0, 3))
        
        # Menu button (...)
        menu_btn = tb.Menubutton(controls_row, text="...", width=3, bootstyle="secondary-outline")
        menu_btn.pack(side="left", padx=(0, 0))
        
        # Create menu
        self.menu = tb.Menu(menu_btn)
        menu_btn["menu"] = self.menu
        self.menu.add_command(label="Srt sang TTS", command=self.on_import_srt)
        self.menu.add_command(label="Tạo SRT", command=self.on_create_srt)
        
        # Store reference to create_srt_btn for enabling/disabling (for compatibility with main_window)
        # We'll use a dummy button that we can enable/disable, but the actual control is the menu
        self.create_srt_btn = tb.Button(controls_row, text="", width=0)
        self.create_srt_btn.pack_forget()  # Hide it
        self._create_srt_menu_index = 1  # Index of "Tạo srt" in menu
    
    def _set_create_srt_enabled(self, enabled: bool) -> None:
        """Enable/disable the 'Tạo srt' menu item"""
        try:
            if enabled:
                self.menu.entryconfig(self._create_srt_menu_index, state="normal")
            else:
                self.menu.entryconfig(self._create_srt_menu_index, state="disabled")
        except Exception as e:
            print(f"❌ Error setting menu item state: {e}")

    def on_generate(self) -> None:
        # Delegate to main window which has services and user_id
        root = self.frame.winfo_toplevel()
        from ui.main_window import MainWindow
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win and hasattr(main_win, "start_generate_txt_batch"):
            main_win.start_generate_txt_batch()
    
    def on_stop(self) -> None:
        # Stop running batch
        root = self.frame.winfo_toplevel()
        from ui.main_window import MainWindow
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win and hasattr(main_win, "stop_generation"):
            main_win.stop_generation()
    
    def on_import_srt(self) -> None:
        # Import SRT folder
        root = self.frame.winfo_toplevel()
        from ui.main_window import MainWindow
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win and hasattr(main_win, "import_srt_folder"):
            main_win.import_srt_folder()
    
    def on_create_srt(self) -> None:
        # Create SRT from existing audio files
        root = self.frame.winfo_toplevel()
        from ui.main_window import MainWindow
        main_win: MainWindow = getattr(root, "_main_window_ref", None)
        if main_win and hasattr(main_win, "create_srt_for_selected"):
            main_win.create_srt_for_selected()
    
