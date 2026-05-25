"""
Database retry helper for handling connection issues.
"""
import time
import logging
from typing import Callable, Any, Optional

logger = logging.getLogger(__name__)


def db_retry(max_retries: int = 3, backoff_base: float = 1.0, 
             connection_errors: tuple = None) -> Callable:
    """
    Decorator for database operations with retry logic.
    
    Args:
        max_retries: Maximum number of retry attempts
        backoff_base: Base time for exponential backoff
        connection_errors: Tuple of error types to retry on
    """
    if connection_errors is None:
        connection_errors = ('server disconnected', 'connection', 'timeout', 'network')
    
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs) -> Any:
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    error_msg = str(e).lower()
                    
                    # Check if this is a connection-related error
                    is_connection_error = any(
                        error_type in error_msg for error_type in connection_errors
                    )
                    
                    if attempt < max_retries - 1 and is_connection_error:
                        backoff_time = backoff_base * (2 ** attempt)  # Exponential backoff
                        logger.warning(
                            f"Database connection issue (attempt {attempt + 1}/{max_retries}): {e}"
                        )
                        time.sleep(backoff_time)
                        continue
                    else:
                        # Not a connection error or final attempt
                        break
            
            # Re-raise the last error if all retries failed
            logger.error(f"Database operation failed after {max_retries} attempts: {last_error}")
            raise last_error
        
        return wrapper
    return decorator


def safe_db_operation(operation: Callable, max_retries: int = 3, 
                     default_return: Any = None, *args, **kwargs) -> Any:
    """
    Safely execute a database operation with retry logic.
    
    Args:
        operation: Database operation function to execute
        max_retries: Maximum number of retry attempts
        default_return: Value to return if all retries fail
        *args, **kwargs: Arguments to pass to the operation
    
    Returns:
        Result of operation or default_return if all retries fail
    """
    last_error = None
    connection_errors = ('server disconnected', 'connection', 'timeout', 'network')
    
    for attempt in range(max_retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            
            # Check if this is a connection-related error
            is_connection_error = any(
                error_type in error_msg for error_type in connection_errors
            )
            
            if attempt < max_retries - 1 and is_connection_error:
                backoff_time = 1.0 * (2 ** attempt)  # 1s, 2s, 4s
                logger.warning(
                    f"Database connection issue (attempt {attempt + 1}/{max_retries}): {e}"
                )
                time.sleep(backoff_time)
                continue
            else:
                # Not a connection error or final attempt
                break
    
    # Log error and return default
    logger.error(f"Database operation failed after {max_retries} attempts: {last_error}")
    return default_return
