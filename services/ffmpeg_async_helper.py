"""
FFmpeg Async Helper - Prevents UI freezing during long concat operations

FEATURES:
- Non-blocking ffmpeg execution with progress callbacks
- Thread-safe operation with cancellation support
- Cross-platform (Windows + macOS)
- Automatic timeout handling
- Progress estimation based on file count
"""

import os
import sys
import subprocess
import threading
import time
import logging
from typing import Callable, Optional, List
from pathlib import Path

logger = logging.getLogger('ffmpeg_async')


class FFmpegAsyncConcatenator:
    """
    Async wrapper for ffmpeg concat operations to prevent UI freezing.
    
    Usage:
        concatenator = FFmpegAsyncConcatenator()
        concatenator.concat_async(
            ffmpeg_bin="ffmpeg",
            input_files=["file1.mp3", "file2.mp3"],
            output_file="output.mp3",
            progress_callback=lambda p, m: print(f"{p}% - {m}"),
            completion_callback=lambda success, msg: print(f"Done: {msg}")
        )
    """
    
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancelled = False
        self._lock = threading.Lock()
    
    def concat_async(
        self,
        ffmpeg_bin: str,
        input_files: List[str],
        output_file: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        completion_callback: Optional[Callable[[bool, str], None]] = None,
        timeout: int = 300
    ) -> None:
        """
        Start async concatenation in background thread.
        
        Args:
            ffmpeg_bin: Path to ffmpeg executable
            input_files: List of input file paths
            output_file: Output file path
            progress_callback: Called with (progress_percent, message)
            completion_callback: Called with (success, message) when done
            timeout: Max seconds to wait (default 5 minutes)
        """
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Another concat operation is already running")
        
        self._cancelled = False
        self._thread = threading.Thread(
            target=self._concat_worker,
            args=(ffmpeg_bin, input_files, output_file, progress_callback, completion_callback, timeout),
            daemon=True
        )
        self._thread.start()
    
    def cancel(self) -> None:
        """Cancel the running operation"""
        with self._lock:
            self._cancelled = True
            if self._process:
                try:
                    self._process.terminate()
                    # Give it 2 seconds to terminate gracefully
                    self._process.wait(timeout=2)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
    
    def is_running(self) -> bool:
        """Check if operation is still running"""
        return self._thread is not None and self._thread.is_alive()
    
    def _concat_worker(
        self,
        ffmpeg_bin: str,
        input_files: List[str],
        output_file: str,
        progress_callback: Optional[Callable[[int, str], None]],
        completion_callback: Optional[Callable[[bool, str], None]],
        timeout: int
    ) -> None:
        """Worker thread that runs ffmpeg"""
        try:
            # Report start
            if progress_callback:
                progress_callback(0, f"Starting concat of {len(input_files)} files...")
            
            # Create concat list file
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', encoding='utf-8') as f:
                for path in input_files:
                    abs_path = os.path.abspath(path)
                    normalized = abs_path.replace('\\', '/')
                    escaped = normalized.replace("'", "''")
                    f.write(f"file '{escaped}'\n")
                list_path = f.name
            
            if progress_callback:
                progress_callback(10, "Created concat list, starting ffmpeg...")
            
            # Build command
            cmd = [
                ffmpeg_bin, '-hide_banner', '-loglevel', 'warning',
                '-f', 'concat', '-safe', '0',
                '-i', list_path, '-c', 'copy', '-y', output_file
            ]
            
            # Start process
            with self._lock:
                if self._cancelled:
                    os.unlink(list_path)
                    if completion_callback:
                        completion_callback(False, "Cancelled before start")
                    return
                
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
            
            # Monitor progress with timeout
            start_time = time.time()
            last_progress = 10
            
            while True:
                # Check if cancelled
                with self._lock:
                    if self._cancelled:
                        self._process.terminate()
                        os.unlink(list_path)
                        if completion_callback:
                            completion_callback(False, "Cancelled by user")
                        return
                
                # Check if process finished
                retcode = self._process.poll()
                if retcode is not None:
                    break
                
                # Check timeout
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                    os.unlink(list_path)
                    if completion_callback:
                        completion_callback(False, f"Timeout after {timeout}s")
                    return
                
                # Update progress (estimate based on time)
                progress = min(90, int(10 + (elapsed / timeout) * 80))
                if progress > last_progress and progress_callback:
                    progress_callback(progress, f"Processing... ({int(elapsed)}s elapsed)")
                    last_progress = progress
                
                time.sleep(0.5)
            
            # Get output
            stdout, stderr = self._process.communicate()
            
            # Clean up list file
            try:
                os.unlink(list_path)
            except Exception:
                pass
            
            # Check result
            if retcode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 100:
                if progress_callback:
                    progress_callback(100, "Concat completed successfully!")
                if completion_callback:
                    completion_callback(True, f"Success: {output_file}")
            else:
                error_msg = stderr.decode('utf-8', errors='ignore') if stderr else "Unknown error"
                if completion_callback:
                    completion_callback(False, f"FFmpeg error: {error_msg[:200]}")
        
        except Exception as e:
            logger.exception("FFmpeg async concat error")
            if completion_callback:
                completion_callback(False, f"Exception: {str(e)}")
        
        finally:
            with self._lock:
                self._process = None


def concat_files_async(
    ffmpeg_bin: str,
    input_files: List[str],
    output_file: str,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    completion_callback: Optional[Callable[[bool, str], None]] = None,
    timeout: int = 300
) -> FFmpegAsyncConcatenator:
    """
    Convenience function to start async concat and return the concatenator object.
    
    Returns:
        FFmpegAsyncConcatenator instance (can be used to cancel operation)
    
    Example:
        def on_progress(percent, msg):
            print(f"Progress: {percent}% - {msg}")
        
        def on_complete(success, msg):
            print(f"Complete: {msg}")
        
        concatenator = concat_files_async(
            "ffmpeg",
            ["file1.mp3", "file2.mp3"],
            "output.mp3",
            on_progress,
            on_complete
        )
        
        # Later, if needed:
        # concatenator.cancel()
    """
    concatenator = FFmpegAsyncConcatenator()
    concatenator.concat_async(
        ffmpeg_bin, input_files, output_file,
        progress_callback, completion_callback, timeout
    )
    return concatenator
