from __future__ import annotations

import ttkbootstrap as tb
from ttkbootstrap.tableview import Tableview
import re
import os


class BatchPanel:
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent, style='Gray.TFrame')
        self.selected_folder = ""
        self.txt_files = []  # list[str]
        self.current_file_type = "txt"  # "txt" or "srt"
        self._on_folder_selected_callback = None
        self._coldata = [
            {"text": "ID", "width": 40},
            {"text": "Tên File", "stretch": True},
            {"text": "Trạng thái", "stretch": True},
            {"text": "Tiến độ", "stretch": True}
        ]
        
        # Main container: Horizontal split
        main_container = tb.Frame(self.frame, style='Gray.TFrame')
        main_container.pack(fill="both", expand=True)
        
        # --- Left Column: Controls (Bordered) ---
        # Wrapping in a Frame with Bordered style - Fixed Width 300 to match VoicePanel
        left_col = tb.Frame(main_container, style='Bordered.TFrame', padding=10, width=300)
        left_col.pack(side="left", fill="y", padx=(0, 10))
        left_col.pack_propagate(False)
        
        # Path Section
        path_row = tb.Frame(left_col, style='Gray.TFrame')
        path_row.pack(fill="x", pady=(0, 5))
        tb.Label(path_row, text="Nguồn:").pack(side="left")
        self.folder_entry = tb.Entry(path_row)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        # Buttons Row
        btn_row = tb.Frame(left_col, style='Gray.TFrame')
        btn_row.pack(fill="x", pady=(0, 10))
        tb.Button(btn_row, text="Chọn File", width=8, bootstyle="secondary-outline", command=self.browse_file).pack(side="left", padx=(0,5))
        tb.Button(btn_row, text="Thư mục", width=8, bootstyle="secondary-outline", command=self.browse_folder).pack(side="left", padx=(0,5))
        tb.Button(btn_row, text="Thư mục SRT", width=10, bootstyle="secondary-outline", command=self.browse_srt).pack(side="left")
        
        # Settings & Status
        middle_row = tb.Frame(left_col, style='Gray.TFrame')
        middle_row.pack(fill="x", pady=(0, 10))
        
        self.auto_srt_var = tb.BooleanVar()
        tb.Checkbutton(middle_row, text="Tự tạo SRT", variable=self.auto_srt_var, style='TCheckbutton').pack(side="left")
        
        self.result_label = tb.Label(middle_row, text="Kết quả: 0/0", bootstyle="inverse-info")
        self.result_label.pack(side="right")
        
        # Controls (Start/Stop) - pushed to bottom
        controls_container = tb.Frame(left_col, style='Gray.TFrame')
        controls_container.pack(fill="x", side="bottom")
        from ui.components.controls_panel import ControlsPanel
        self.controls_panel = ControlsPanel(controls_container)
        self.controls_panel.frame.pack(fill="x")

        # --- Right Column: Table ---
        right_col = tb.Frame(main_container, style='Gray.TFrame')
        right_col.pack(side="right", fill="both", expand=True)
        
        self.file_table = Tableview(
            master=right_col,
            coldata=self._coldata,
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="light",
            stripecolor=("#ffffff", "#f0f0f0") # Slight contrast steps for readability
        )
        self.file_table.pack(fill="both", expand=True)
        
        # Bind selection event
        self.file_table.bind("<<TreeviewSelect>>", self.on_table_select)

    def _natural_sort_key(self, text: str) -> list:
        """
        Natural sort key function for sorting filenames.
        Converts text to a list of strings and numbers for proper natural sorting.
        Example: "file10.txt" -> ["file", 10, ".txt"]
        """
        def convert(text_part):
            return int(text_part) if text_part.isdigit() else text_part.lower()
        
        return [convert(c) for c in re.split(r'(\d+)', text)]

    def browse_file(self) -> None:
        """Browse for single file"""
        from tkinter import filedialog
        file = filedialog.askopenfilename(
            title="Chọn file .txt hoặc .srt",
            filetypes=[("Text files", "*.txt"), ("SRT files", "*.srt"), ("All files", "*.*")]
        )
        if not file:
            return
        import os
        self.selected_folder = os.path.dirname(file)
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, self.selected_folder)
        self.txt_files = [file]
        if file.lower().endswith(".srt"):
            self.current_file_type = "srt"
        else:
            self.current_file_type = "txt"
        # Reload table
        rows = []
        for idx, path in enumerate(self.txt_files, start=1):
            fname = os.path.basename(path)
            rows.append([str(idx), fname, "READY", "0%"])
        self.file_table.build_table_data(coldata=self._coldata, rowdata=rows)
        
        # Trigger callback
        if hasattr(self, '_on_folder_selected_callback') and self._on_folder_selected_callback:
            try:
                self._on_folder_selected_callback(self.txt_files, self.current_file_type)
            except Exception as e:
                print(f"❌ Error in folder selected callback: {e}")
    
    def browse_srt(self) -> None:
        """Browse for SRT folder"""
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Chọn thư mục chứa file .srt")
        if not folder:
            return
        import os
        self.selected_folder = folder
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, folder)
        # Scan only SRT files
        self.txt_files = []
        for name in os.listdir(folder):
            if name.lower().endswith(".srt"):
                self.txt_files.append(os.path.join(folder, name))
        
        # Sort files using natural sort
        self.txt_files.sort(key=lambda path: self._natural_sort_key(os.path.basename(path)))
        self.current_file_type = "srt"
        
        # Reload table
        rows = []
        for idx, path in enumerate(self.txt_files, start=1):
            fname = os.path.basename(path)
            rows.append([str(idx), fname, "READY", "0%"])
        self.file_table.build_table_data(coldata=self._coldata, rowdata=rows)
        
        # Trigger callback
        if hasattr(self, '_on_folder_selected_callback') and self._on_folder_selected_callback:
            try:
                self._on_folder_selected_callback(self.txt_files, self.current_file_type)
            except Exception as e:
                print(f"❌ Error in folder selected callback: {e}")
    
    def browse_folder(self) -> None:
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Chọn thư mục chứa .txt hoặc .srt")
        if not folder:
            return
        import os
        self.selected_folder = folder
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, folder)
        # Scan txt and srt files
        self.txt_files = []
        txt_count = 0
        srt_count = 0
        for name in os.listdir(folder):
            if name.lower().endswith(".txt"):
                self.txt_files.append(os.path.join(folder, name))
                txt_count += 1
            elif name.lower().endswith(".srt"):
                self.txt_files.append(os.path.join(folder, name))
                srt_count += 1
        
        # Sort files using natural sort (file1.txt, file2.txt, file10.txt instead of file1.txt, file10.txt, file2.txt)
        self.txt_files.sort(key=lambda path: self._natural_sort_key(os.path.basename(path)))
        
        # Determine file type based on what's found
        if srt_count > 0 and txt_count == 0:
            self.current_file_type = "srt"
        elif txt_count > 0 and srt_count == 0:
            self.current_file_type = "txt"
        elif txt_count > 0 and srt_count > 0:
            # Mixed files - default to txt
            self.current_file_type = "txt"
        else:
            self.current_file_type = "txt"
        # Reload table
        rows = []
        for idx, path in enumerate(self.txt_files, start=1):
            fname = os.path.basename(path)
            rows.append([str(idx), fname, "READY", "0%"])
        self.file_table.build_table_data(coldata=self._coldata, rowdata=rows)
        
        # Trigger callback to load subtitles preview
        if hasattr(self, '_on_folder_selected_callback') and self._on_folder_selected_callback:
            try:
                self._on_folder_selected_callback(self.txt_files, self.current_file_type)
            except Exception as e:
                print(f"❌ Error in folder selected callback: {e}")

    # External API to update a row status
    def update_status(self, index: int, status: str, progress: str = None) -> None:
        """
        Cập nhật cột Status và Tiến độ cho hàng (index 0-based).
        Sửa theo đúng API của ttkbootstrap.Tableview:
        - get_rows() trả về list TableRow -> cần lấy row.values
        - Chuyển về rowdata (list[list]) rồi build lại.
        """
        try:
            # Bảo vệ chỉ số
            rows = self.file_table.get_rows()  # list[TableRow]
            total = len(rows)
            if index < 0 or index >= total:
                print(f"[BatchPanel] Invalid row index: {index}")
                return

            # Map status hiển thị với emoji
            status_map = {
                "Queued": "READY",
                "Processing": "Processing", 
                "Running": "Processing",
                "DONE": "Completed",
                "Completed": "Completed",
                "Error": "Error",
                "Skipped": "Skipped",
            }
            display_status = status_map.get(status, status)

            # Chuyển TableRow -> list values để chỉnh sửa
            new_rowdata = []
            for r in rows:
                vals = list(r.values)  # copy để có thể chỉnh
                # Đảm bảo có đủ 4 cột (ID, FileName, Status, Tiến độ)
                while len(vals) < 4:
                    vals.append("0%")
                new_rowdata.append(vals)

            # Cập nhật cột Status (cột thứ 3 => index 2)
            new_rowdata[index][2] = display_status
            # Cập nhật cột Tiến độ (cột thứ 4 => index 3) nếu có
            if progress is not None:
                new_rowdata[index][3] = progress

            # Build lại bảng để phản ánh thay đổi
            self.file_table.build_table_data(coldata=self._coldata, rowdata=new_rowdata)

            print(f"[BatchPanel] Row {index + 1} status -> {display_status}, progress -> {progress or 'N/A'}")
        except Exception as e:
            print(f"[BatchPanel] Error updating status: {e}")


    def get_selected_index(self) -> int:
        try:
            # Get selected items from tableview
            selection = self.file_table.selection()
            if not selection:
                return -1
            
            selected_iid = selection[0]
            item_data = self.file_table.item(selected_iid)
            if 'values' in item_data and item_data['values']:
                try:
                    row_id = int(item_data['values'][0])
                    return row_id - 1
                except:
                    pass
            try:
                if selected_iid.startswith('I'):
                    return int(selected_iid[1:]) - 1
            except:
                pass
            return -1
        except Exception as e:
            print(f"[BatchPanel] get_selected_index error: {e}")
            return -1

    def get_selected_file(self) -> str:
        idx = self.get_selected_index()
        if idx < 0 or idx >= len(self.txt_files):
            return ""
        return self.txt_files[idx]

    def get_all_files(self) -> list[str]:
        return list(self.txt_files)
    
    def get_current_file_type(self) -> str:
        """Get current file type: 'txt' or 'srt'"""
        return self.current_file_type
    
    def set_on_folder_selected_callback(self, callback):
        """Set callback function to be called when folder is selected"""
        self._on_folder_selected_callback = callback
    
    def open_output_folder(self) -> None:
        """Open the selected folder in file explorer"""
        try:
            import os
            import subprocess
            import sys
            from ttkbootstrap.dialogs import Messagebox
            
            if not self.selected_folder or not os.path.exists(self.selected_folder):
                Messagebox.show_warning("Chưa chọn thư mục hoặc thư mục không tồn tại!\nVui lòng chọn thư mục TXT trước.")
                return
            
            # Open folder based on OS
            if os.name == 'nt':  # Windows
                os.startfile(self.selected_folder)
            elif os.name == 'posix':  # macOS and Linux
                subprocess.call(['open' if sys.platform == 'darwin' else 'xdg-open', self.selected_folder])
            
            print(f"📁 Opened output folder: {self.selected_folder}")
            
        except Exception as e:
            print(f"❌ Error opening output folder: {e}")
            from ttkbootstrap.dialogs import Messagebox
            Messagebox.show_error(f"Không thể mở thư mục: {e}")
    
    def on_table_select(self, event=None):
        # optional debug
        try:
            selected_iid = self.file_table.focus()
            if hasattr(self.file_table, 'selection'):
                sel = self.file_table.selection()
                # print(f"[BatchPanel] selection: {sel}, focus: {selected_iid}")
        except Exception as e:
            print(f"[BatchPanel] selection debug error: {e}")
