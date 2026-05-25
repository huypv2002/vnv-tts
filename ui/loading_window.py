"""
Loading Window with Progress Bar

Hiển thị loading screen khi khởi động app:
- Validate API keys
- Load key pool
- Setup services
"""

import ttkbootstrap as tb
from tkinter import ttk
import threading
import time


class LoadingWindow:
    """
    Loading window với progress bar và status messages.
    
    Usage:
        loading = LoadingWindow(parent)
        loading.show("Loading API keys...")
        
        # Do work in background
        def work():
            for i in range(100):
                loading.update_progress(i, f"Validating key {i}/100...")
                time.sleep(0.1)
            loading.close()
        
        threading.Thread(target=work, daemon=True).start()
    """
    
    def __init__(self, parent=None):
        """
        Initialize loading window.
        
        Args:
            parent: Parent window (None = create standalone window)
        """
        self.parent = parent
        self.window = None
        self.progress_var = None
        self.status_label = None
        self.detail_label = None
        self.is_closed = False
    
    def show(self, title: str = "Loading...", message: str = "Please wait..."):
        """
        Show loading window.
        
        Args:
            title: Window title
            message: Initial message
        """
        # Create toplevel window
        if self.parent:
            self.window = tb.Toplevel(self.parent)
        else:
            self.window = tb.Window()
        
        self.window.title(title)
        self.window.geometry("500x250")
        self.window.resizable(False, False)
        
        # Center window
        self.window.update_idletasks()
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() // 2) - (width // 2)
        y = (self.window.winfo_screenheight() // 2) - (height // 2)
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        
        # Make modal
        self.window.transient(self.parent)
        self.window.grab_set()
        
        # Main container
        container = tb.Frame(self.window, padding=20)
        container.pack(fill="both", expand=True)
        
        # Title
        title_label = tb.Label(
            container,
            text=title,
            font=("Arial", 16, "bold"),
            bootstyle="primary"
        )
        title_label.pack(pady=(0, 10))
        
        # Status message
        self.status_label = tb.Label(
            container,
            text=message,
            font=("Arial", 11),
            wraplength=450
        )
        self.status_label.pack(pady=(0, 10))
        
        # Progress bar
        self.progress_var = tb.IntVar(value=0)
        progress_bar = ttk.Progressbar(
            container,
            variable=self.progress_var,
            maximum=100,
            mode='determinate',
            bootstyle="success-striped",
            length=450
        )
        progress_bar.pack(pady=(0, 10))
        
        # Percentage label
        self.percent_label = tb.Label(
            container,
            text="0%",
            font=("Arial", 10)
        )
        self.percent_label.pack()
        
        # Detail message (smaller font)
        self.detail_label = tb.Label(
            container,
            text="",
            font=("Arial", 9),
            bootstyle="secondary",
            wraplength=450
        )
        self.detail_label.pack(pady=(10, 0))
        
        self.is_closed = False
        
        # Update window
        self.window.update()
    
    def update_progress(self, percent: int, status: str = None, detail: str = None):
        """
        Update progress bar and messages.
        
        Args:
            percent: Progress percentage (0-100)
            status: Main status message (optional)
            detail: Detail message (optional)
        """
        if self.is_closed or not self.window:
            return
        
        try:
            # Update progress bar
            self.progress_var.set(min(100, max(0, percent)))
            self.percent_label.config(text=f"{percent}%")
            
            # Update status message
            if status:
                self.status_label.config(text=status)
            
            # Update detail message
            if detail:
                self.detail_label.config(text=detail)
            
            # Force update
            self.window.update_idletasks()
            self.window.update()
            
        except Exception as e:
            # Window might be closed
            pass
    
    def set_status(self, message: str):
        """Update status message only"""
        self.update_progress(self.progress_var.get(), status=message)
    
    def set_detail(self, message: str):
        """Update detail message only"""
        self.update_progress(self.progress_var.get(), detail=message)
    
    def close(self):
        """Close loading window"""
        if not self.is_closed and self.window:
            try:
                self.window.grab_release()
                self.window.destroy()
                self.is_closed = True
            except Exception:
                pass
    
    def __enter__(self):
        """Context manager support"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Auto-close on context exit"""
        self.close()


# Convenience function for simple loading
def show_loading_task(parent, title: str, message: str, task_func, *args, **kwargs):
    """
    Show loading window while executing task.
    
    Args:
        parent: Parent window
        title: Loading window title
        message: Loading message
        task_func: Function to execute (should call loading.update_progress())
        *args, **kwargs: Arguments for task_func
        
    Usage:
        def my_task(loading):
            for i in range(100):
                loading.update_progress(i, f"Processing {i}...")
                time.sleep(0.1)
        
        show_loading_task(root, "Processing", "Please wait...", my_task)
    """
    loading = LoadingWindow(parent)
    loading.show(title, message)
    
    def work():
        try:
            # Execute task with loading object
            task_func(loading, *args, **kwargs)
        finally:
            # Auto-close when done
            loading.close()
    
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    
    return loading


# Test
if __name__ == "__main__":
    import ttkbootstrap as tb
    
    root = tb.Window(themename="darkly")
    root.geometry("600x400")
    
    def test_loading():
        loading = LoadingWindow(root)
        loading.show("Initializing System", "Loading API keys...")
        
        def work():
            for i in range(101):
                status = f"Validating keys... ({i}/100)"
                detail = f"Checking key sk_test_{i:03d}..."
                loading.update_progress(i, status, detail)
                time.sleep(0.05)
            loading.close()
        
        threading.Thread(target=work, daemon=True).start()
    
    btn = tb.Button(root, text="Show Loading", command=test_loading, bootstyle="success")
    btn.pack(pady=50)
    
    root.mainloop()

