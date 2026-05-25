"""
JWT TTS Tab Setup - Helper to add JWT TTS tab to main window
"""

from typing import Callable
from PySide6 import QtWidgets


def add_jwt_tts_tab(main_window, tab_widget: QtWidgets.QTabWidget, insert_index: int = 1):
    """
    Add JWT TTS tab to the tab widget.
    
    Args:
        main_window: Main window instance (for log_fn and proxy_fn)
        tab_widget: QTabWidget to add tab to
        insert_index: Position to insert tab (default: 1, after first tab)
    """
    print("💎 [JWT_TAB] Setting up Giọng Trả Phí tab...")
    
    try:
        from ui.qt_tab_jwt_tts import JWTTTSTab
        
        # Get log function from main window
        log_fn = None
        if hasattr(main_window, 'log'):
            log_fn = main_window.log
        elif hasattr(main_window, '_log'):
            log_fn = main_window._log
        else:
            log_fn = print
        
        # Get proxy function from main window
        proxy_fn = None
        proxy_service = None
        if hasattr(main_window, 'proxy_service_db') and main_window.proxy_service_db:
            proxy_service = main_window.proxy_service_db  # 🔧 NEW: Pass proxy_service for rotation
            def _get_proxy():
                try:
                    proxy_url = main_window.proxy_service_db.get_current_proxy()
                    if proxy_url:
                        return {"http": proxy_url, "https": proxy_url}
                except:
                    pass
                return None
            proxy_fn = _get_proxy
        elif hasattr(main_window, '_get_proxy'):
            proxy_fn = main_window._get_proxy
        
        # Get user_id from main window
        user_id = None
        if hasattr(main_window, 'current_user_id'):
            user_id = main_window.current_user_id
        
        # Create tab with user_id for auto-loading D1 accounts
        # 🔧 NEW: Pass proxy_service for rotation on 401 "Unusual activity"
        tab = JWTTTSTab(parent=main_window, log_fn=log_fn, proxy_fn=proxy_fn, user_id=user_id, proxy_service=proxy_service)
        
        # Insert at specified position
        tab_widget.insertTab(insert_index, tab, "💎 Giọng Trả Phí")
        
        # Store reference on main window
        main_window._tab_jwt_tts = tab
        
        print(f"✅ [JWT_TAB] Tab added at position {insert_index}")
        if proxy_service:
            print(f"   🔄 ProxyService linked for auto-rotation on unusual activity")
        
    except Exception as e:
        print(f"❌ [JWT_TAB] Failed to setup: {e}")
        import traceback
        traceback.print_exc()
