import os
import sys
import shutil
import logging

logger = logging.getLogger('ffmpeg_utils')

def get_ffmpeg_path() -> str:
    """
    Resolve the path to the ffmpeg executable.
    Priority:
    1. Local directory (ffmpeg or ffmpeg.exe) - supports user provided binaries
    2. imageio-ffmpeg library
    3. System PATH
    """
    # 1. Check local directory (same dir as script or exe)
    base_dir = _get_base_dir()
    
    # Check for executable with or without .exe extension (Windows vs Mac/Linux)
    local_bins = ['ffmpeg', 'ffmpeg.exe']
    for binary_name in local_bins:
        local_path = os.path.join(base_dir, binary_name)
        if os.path.isfile(local_path) and os.access(local_path, os.X_OK):
            logger.info(f"Using local ffmpeg: {local_path}")
            return local_path

    # Also check one level up (common in development)
    parent_dir = os.path.dirname(base_dir)
    for binary_name in local_bins:
        parent_path = os.path.join(parent_dir, binary_name)
        if os.path.isfile(parent_path) and os.access(parent_path, os.X_OK):
            logger.info(f"Using parent directory ffmpeg: {parent_path}")
            return parent_path
            
    # Also check in 'app' folder if we are in a subfolder or parent
    app_path = os.path.join(parent_dir, 'app')
    for binary_name in local_bins:
        p = os.path.join(app_path, binary_name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
             logger.info(f"Using app directory ffmpeg: {p}")
             return p

    # 2. Try imageio_ffmpeg
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_exe and os.path.exists(ffmpeg_exe):
            logger.info(f"Using imageio-ffmpeg: {ffmpeg_exe}")
            return ffmpeg_exe
    except ImportError:
        logger.debug("imageio-ffmpeg not installed")
    except Exception as e:
        logger.warning(f"Error getting imageio-ffmpeg path: {e}")

    # 3. System PATH
    system_path = shutil.which('ffmpeg')
    if system_path:
        logger.info(f"Using system ffmpeg: {system_path}")
        return system_path
        
    # Fallback to just command name and hope for the best
    logger.warning("No ffmpeg found, defaulting to 'ffmpeg'")
    return 'ffmpeg'

def get_ffprobe_path() -> str:
    """
    Resolve the path to the ffprobe executable.
    Priority:
    1. Local directory
    2. System PATH
    """
    base_dir = _get_base_dir()
    
    local_bins = ['ffprobe', 'ffprobe.exe']
    for binary_name in local_bins:
        local_path = os.path.join(base_dir, binary_name)
        if os.path.isfile(local_path) and os.access(local_path, os.X_OK):
             logger.info(f"Using local ffprobe: {local_path}")
             return local_path

    # Check parent
    parent_dir = os.path.dirname(base_dir)
    for binary_name in local_bins:
        parent_path = os.path.join(parent_dir, binary_name)
        if os.path.isfile(parent_path) and os.access(parent_path, os.X_OK):
             logger.info(f"Using parent directory ffprobe: {parent_path}")
             return parent_path

    # Check app folder
    app_path = os.path.join(parent_dir, 'app')
    for binary_name in local_bins:
        p = os.path.join(app_path, binary_name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
             logger.info(f"Using app directory ffprobe: {p}")
             return p

    # System PATH
    system_path = shutil.which('ffprobe')
    if system_path:
         logger.info(f"Using system ffprobe: {system_path}")
         return system_path
         
    # Try to find it next to imageio-ffmpeg binary if possible (sometimes they come together? No, usually not)
    
    logger.warning("No ffprobe found, defaulting to 'ffprobe'")
    return 'ffprobe'

def _get_base_dir() -> str:
    """Get the base directory of the application (frozen or script)."""
    if hasattr(sys, 'frozen'):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
