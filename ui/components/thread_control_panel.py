from __future__ import annotations

import ttkbootstrap as tb
from tkinter import ttk
import threading
import logging


class ThreadControlPanel:
    """UI Panel for controlling multithreaded TTS processing"""

    def __init__(self, parent, callback=None) -> None:
        self.parent = parent
        self.callback = callback  # Callback to main window
        self.frame = tb.Frame(parent)

        # Setup logging
        self.logger = logging.getLogger('thread_control')

        # Create UI components
        self.setup_ui()

    def setup_ui(self):
        """Setup the thread control UI components"""
        # Title
        title_label = tb.Label(self.frame, text="Điều khiển đa luồng", font=("Arial", 10, "bold"))
        title_label.pack(anchor="w", pady=(0, 5))

        # Thread settings frame
        settings_frame = tb.Frame(self.frame)
        settings_frame.pack(fill="x", pady=(0, 10))

        # Max workers combobox
        workers_frame = tb.Frame(settings_frame)
        workers_frame.pack(fill="x", pady=(0, 5))

        workers_label = tb.Label(workers_frame, text="Số luồng tối đa:")
        workers_label.pack(side="left")

        # Create combobox with values 1-5
        self.max_workers_var = tb.StringVar(value="3")
        self.max_workers_combo = ttk.Combobox(
            workers_frame,
            textvariable=self.max_workers_var,
            values=["1", "2", "3", "4", "5"],
            state="readonly",
            width=10
        )
        self.max_workers_combo.pack(side="left", padx=(5, 0))

        # Bind change event
        self.max_workers_combo.bind("<<ComboboxSelected>>", self.on_workers_changed)

        # Thread info label
        self.info_label = tb.Label(workers_frame, text="(3 luồng = 1 Phase 1 + 2 Phase 2)",
                                 foreground="gray", font=("Arial", 8))
        self.info_label.pack(side="left", padx=(10, 0))

        # Control buttons frame
        controls_frame = tb.Frame(self.frame)
        controls_frame.pack(fill="x")

        # Start pipeline button
        self.start_btn = tb.Button(
            controls_frame,
            text="Khởi động pipeline",
            bootstyle="success",
            command=self.start_pipeline,
            width=15
        )
        self.start_btn.pack(side="left", padx=(0, 5))

        # Stop pipeline button
        self.stop_btn = tb.Button(
            controls_frame,
            text="Dừng pipeline",
            bootstyle="danger",
            command=self.stop_pipeline,
            width=15,
            state="disabled"
        )
        self.stop_btn.pack(side="left", padx=(0, 5))

        # Clear tasks button
        self.clear_btn = tb.Button(
            controls_frame,
            text="Xóa tasks",
            bootstyle="warning",
            command=self.clear_tasks,
            width=12
        )
        self.clear_btn.pack(side="left", padx=(0, 5))

        # Status frame
        status_frame = tb.LabelFrame(self.frame, text="Trạng thái pipeline")
        status_frame.pack(fill="x", pady=(10, 0))

        # Status indicators
        status_grid = tb.Frame(status_frame)
        status_grid.pack(fill="x", padx=10, pady=5)

        # Row 1: Queue sizes
        tb.Label(status_grid, text="Phase 1 Queue:").grid(row=0, column=0, sticky="w", pady=2)
        self.phase1_queue_label = tb.Label(status_grid, text="0", foreground="blue")
        self.phase1_queue_label.grid(row=0, column=1, sticky="w", padx=(5, 15), pady=2)

        tb.Label(status_grid, text="Phase 2 Queue:").grid(row=0, column=2, sticky="w", pady=2)
        self.phase2_queue_label = tb.Label(status_grid, text="0", foreground="blue")
        self.phase2_queue_label.grid(row=0, column=3, sticky="w", padx=(5, 0), pady=2)

        # Row 2: Task counts
        tb.Label(status_grid, text="Đã hoàn thành:").grid(row=1, column=0, sticky="w", pady=2)
        self.completed_label = tb.Label(status_grid, text="0", foreground="green")
        self.completed_label.grid(row=1, column=1, sticky="w", padx=(5, 15), pady=2)

        tb.Label(status_grid, text="Thất bại:").grid(row=1, column=2, sticky="w", pady=2)
        self.failed_label = tb.Label(status_grid, text="0", foreground="red")
        self.failed_label.grid(row=1, column=3, sticky="w", padx=(5, 0), pady=2)

        # Row 3: Pipeline status
        tb.Label(status_grid, text="Pipeline:").grid(row=2, column=0, sticky="w", pady=2)
        self.pipeline_status_label = tb.Label(status_grid, text="Đã dừng", foreground="gray")
        self.pipeline_status_label.grid(row=2, column=1, columnspan=3, sticky="w", padx=(5, 0), pady=2)

        # Progress bar
        self.progress_var = tb.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            status_frame,
            variable=self.progress_var,
            mode='indeterminate'
        )
        self.progress_bar.pack(fill="x", padx=10, pady=(5, 10))

        # Pipeline reference
        self.pipeline = None

    def on_workers_changed(self, event=None):
        """Handle max workers combobox change"""
        max_workers = int(self.max_workers_var.get())

        # Update info label
        phase1_workers = max(1, max_workers // 2)
        phase2_workers = max(1, max_workers - phase1_workers)
        self.info_label.config(text=f"({max_workers} luồng = {phase1_workers} Phase 1 + {phase2_workers} Phase 2)")

        self.logger.info(f"Max workers changed to: {max_workers}")

        # Notify callback
        if self.callback:
            self.callback('workers_changed', max_workers)

    def start_pipeline(self):
        """Start the multithreaded TTS pipeline"""
        max_workers = int(self.max_workers_var.get())

        # Disable start button, enable stop button
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        # Update status
        self.pipeline_status_label.config(text="Đang chạy...", foreground="green")
        self.progress_bar.start(10)  # Start animation

        self.logger.info(f"Starting pipeline with {max_workers} workers")

        # Notify callback to start pipeline
        if self.callback:
            self.callback('start_pipeline', max_workers)

    def stop_pipeline(self):
        """Stop the multithreaded TTS pipeline"""
        # Enable start button, disable stop button
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

        # Update status
        self.pipeline_status_label.config(text="Đang dừng...", foreground="orange")

        self.logger.info("Stopping pipeline")

        # Notify callback to stop pipeline
        if self.callback:
            self.callback('stop_pipeline', None)

    def clear_tasks(self):
        """Clear completed tasks"""
        self.logger.info("Clearing completed tasks")

        # Reset counters
        self.phase1_queue_label.config(text="0")
        self.phase2_queue_label.config(text="0")
        self.completed_label.config(text="0")
        self.failed_label.config(text="0")
        self.progress_var.set(0)

        # Notify callback
        if self.callback:
            self.callback('clear_tasks', None)

    def update_status(self, status_data: dict):
        """Update the status display with current pipeline statistics"""
        if 'phase1_queue' in status_data:
            self.phase1_queue_label.config(text=str(status_data['phase1_queue']))

        if 'phase2_queue' in status_data:
            self.phase2_queue_label.config(text=str(status_data['phase2_queue']))

        if 'completed' in status_data:
            self.completed_label.config(text=str(status_data['completed']))

        if 'failed' in status_data:
            self.failed_label.config(text=str(status_data['failed']))

        if 'pipeline_status' in status_data:
            status_text = status_data['pipeline_status']
            color = "green" if status_text == "Đang chạy..." else "gray"
            self.pipeline_status_label.config(text=status_text, foreground=color)

        # Update progress if total tasks is known
        if 'total_tasks' in status_data and 'completed' in status_data:
            total = status_data['total_tasks']
            completed = status_data['completed']
            if total > 0:
                progress = (completed / total) * 100
                self.progress_var.set(progress)

    def set_pipeline_running(self, is_running: bool):
        """Set the pipeline running status"""
        if is_running:
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.pipeline_status_label.config(text="Đang chạy...", foreground="green")
            self.progress_bar.start(10)
        else:
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.pipeline_status_label.config(text="Đã dừng", foreground="gray")
            self.progress_bar.stop()

    def get_max_workers(self) -> int:
        """Get the current max workers setting"""
        return int(self.max_workers_var.get())

    def pack(self, **kwargs):
        """Pack the frame"""
        self.frame.pack(**kwargs)

    def grid(self, **kwargs):
        """Grid the frame"""
        self.frame.grid(**kwargs)