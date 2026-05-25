from __future__ import annotations

import ttkbootstrap as tb
from ttkbootstrap.tableview import Tableview


class SubtitlesPanel:
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent, style='Gray.TFrame')
        self._subtitle_rows = []
        self._row_index_by_id = {}
        self._row_status_by_id = {}
        
        # Single Bordered Frame
        main_card = tb.Labelframe(self.frame, text="Phụ đề", padding=10, style='Bordered.TLabelframe')
        main_card.pack(fill="both", expand=True)
        
        # Status Label
        self.status_label = tb.Label(
            main_card, 
            text="Trạng thái: Chờ",
            bootstyle="secondary",
            style="TLabel"
        )
        self.status_label.pack(fill="x", pady=(0, 10))
        
        # Main content table
        self.content_table = Tableview(
            master=main_card,
            coldata=[
                {"text": "ID", "width": 40, "stretch": False},
                {"text": "Đầu ra", "width": 80, "stretch": False},
                {"text": "Thời gian", "width": 100, "stretch": False},
                {"text": "Nội dung", "stretch": True},
                {"text": "Giọng #", "width": 80, "stretch": False},
                {"text": "Trạng thái", "width": 100, "stretch": False}
            ],
            rowdata=[],
            paginated=False,
            searchable=False,
            bootstyle="light",
            height=8,
            stripecolor=("#ffffff", "#f0f0f0")
        )
        self.content_table.pack(fill="both", expand=True, pady=(0, 10))
        
        # File path entry removed as per user request
        # self.path_entry = tb.Entry(main_card, width=80) 
        # self.path_entry.pack(fill="x")

    def clear(self) -> None:
        try:
            self._subtitle_rows.clear()
            tv = self._tv
            if tv is not None:
                tv.delete(*tv.get_children(""))
            self.status_label.config(text="Subtitles")
        except Exception as e:
            print(f"❌ Error clearing subtitles: {e}")


    def add_subtitle_row(self, row_id: str, output: str, timing: str, content: str, voice_num: str, status: str) -> None:
        """Add row WITHOUT rebuilding; preserve scroll."""
        try:
            new_row = [row_id, output, timing, content, voice_num, status]
            idx = len(self._subtitle_rows)
            self._subtitle_rows.append(new_row)
            self._row_index_by_id[row_id] = idx
            self._row_status_by_id[row_id] = status

            tv = self._tv
            if tv is None:
                # lần build đầu tiên thì vẫn phải build
                self.content_table.build_table_data(
                    coldata=[
                        {"text": "Id", "width": 40, "sortable": False},
                        {"text": "Output", "stretch": True, "sortable": False},
                        {"text": "Timing", "stretch": True, "sortable": False},
                        {"text": "Content", "stretch": True, "sortable": False},
                        {"text": "Voice #", "stretch": True, "sortable": False},
                        {"text": "Status", "stretch": True, "sortable": False},
                    ],
                    rowdata=self._subtitle_rows
                )
            else:
                self._preserve_scroll(lambda: tv.insert("", "end", values=new_row))
            print(f"➕ Added subtitle row: {row_id}")
        except Exception as e:
            print(f"❌ Error adding subtitle row: {e}")
            import traceback; traceback.print_exc()



    def update_subtitle_status(self, row_id: str, status: str, output: str = "", timing: str = "") -> None:
        """
        Update 1 dòng:
        - Không rebuild
        - Giữ yview/selection
        - Chặn chuyển trạng thái ngược/đã có
        - Thực thi trong main thread để tránh race (after)
        - Cập nhật timing nếu được cung cấp
        """
        def _do_update():
            try:
                if row_id not in self._row_index_by_id:
                    print(f"❌ Row {row_id} not found (index map)")
                    return

                idx = self._row_index_by_id[row_id]
                if idx >= len(self._subtitle_rows):
                    print(f"❌ Row index out of range for {row_id}")
                    return

                row = self._subtitle_rows[idx]
                current_status = self._row_status_by_id.get(row_id, row[5])

                # --- GUARD: bỏ các update trùng/ngoặt ---
                if status == current_status and not output and not timing:
                    # Không làm gì nếu không thay đổi gì
                    # print(f"ℹ️ Row {row_id}: status unchanged ({status})")
                    return

                # Không cho DONE -> Processing / Queued, và Processing -> Queued
                # Cho phép các status chi tiết: Verifying, Check Key, Synthesizing, Uploading
                illegal_back = {
                    ("DONE", "Processing"),
                    ("DONE", "Queued"),
                    ("Processing", "Queued"),
                }
                # Cho phép chuyển từ Processing sang các status chi tiết
                detail_statuses = {"Verifying", "Check Key", "Synthesizing", "Downloading", "Uploading"}
                if (current_status, status) in illegal_back and status not in detail_statuses:
                    print(f"⛔ Ignore illegal transition {current_status} → {status} for {row_id}")
                    return

                # Cập nhật dữ liệu bộ nhớ
                old_status = current_status
                if status:  # Chỉ cập nhật status nếu có giá trị
                    row[5] = status
                    self._row_status_by_id[row_id] = status
                if output:  # Cập nhật output (index 1)
                    row[1] = output
                if timing:  # Cập nhật timing (index 2)
                    row[2] = timing
                    print(f"⏱️ Updated timing for row {row_id}: {timing}")

                tv = self._tv
                if tv is None:
                    # Fallback – chỉ dùng nếu Tableview chưa init hoàn chỉnh
                    print("ℹ️ Fallback build_table_data vì không có Treeview.view (nên hạn chế)")
                    self.content_table.build_table_data(
                        coldata=[
                            {"text": "Id", "width": 40, "sortable": False},
                            {"text": "Output", "stretch": True, "sortable": False},
                            {"text": "Timing", "stretch": True, "sortable": False},
                            {"text": "Content", "stretch": True, "sortable": False},
                            {"text": "Voice #", "stretch": True, "sortable": False},
                            {"text": "Status", "stretch": True, "sortable": False},
                        ],
                        rowdata=self._subtitle_rows
                    )
                    return

                # Update in-place 1 item
                def _apply_item():
                    iid = self._find_iid_by_row_id(row_id)
                    if iid is None:
                        # nếu item bị mất do rebuild ở chỗ khác, insert lại
                        tv.insert("", "end", values=row)
                    else:
                        tv.item(iid, values=row)

                self._preserve_scroll(_apply_item)
                if status:
                    print(f"✅ Updated row {row_id}: {old_status} → {status}")
            except Exception as e:
                print(f"❌ Error updating subtitle status: {e}")
                import traceback; traceback.print_exc()

        # BẢO ĐẢM CHẠY TRONG MAIN THREAD
        try:
            self.frame.after(0, _do_update)
        except Exception:
            _do_update()

    
    def update_stats(self, done: int, processing: int, total: int, elapsed: int = 0) -> None:
        """Update the statistics label"""
        self.status_label.config(text=f"Subtitles (Done: {done} Processing: {processing} Total: {total}) Elapsed: {elapsed}s")

    # --- helpers for Treeview inside Tableview ---
    # --- helpers for Treeview inside Tableview ---
    @property
    def _tv(self):
        return getattr(self.content_table, "view", None)

    def _find_iid_by_row_id(self, row_id: str):
        tv = self._tv
        if tv is None:
            return None
        for iid in tv.get_children(""):
            vals = tv.item(iid, "values")
            if vals and str(vals[0]) == str(row_id):
                return iid
        return None

    def _preserve_scroll(self, fn):
        """
        Chạy fn() và giữ nguyên yview + selection sau update.
        """
        tv = self._tv
        if tv is None:
            fn()
            return
        y0 = tv.yview()
        sel = tv.selection()
        fn()
        try:
            if y0:
                tv.yview_moveto(y0[0])
            if sel:
                tv.selection_set(sel)
        except Exception:
            pass