"""
Centralized error handling logic for TTS service
Handles: quota_exceeded, HTTP 401, voice_limit, proxy errors
"""

class TTSErrorHandler:
    """Handle TTS errors with clear, predictable logic"""
    
    @staticmethod
    def should_mark_key_exhausted(returncode: int, error_msg: str, status: str = "") -> bool:
        """
        Determine if API key should be marked as exhausted
        Returns True if key is invalid/exhausted and should not be reused
        """
        # HTTP 401 = Unauthorized (invalid key)
        if returncode == 22 and "401" in error_msg:
            return True
        
        # Quota exceeded in JSON response
        if "quota" in status.lower() or "quota_exceeded" in status.lower():
            return True
        
        # Quota in error message
        if "quota" in error_msg.lower() and "exceeded" in error_msg.lower():
            return True
            
        # Invalid API key messages
        if any(keyword in error_msg.lower() for keyword in ["invalid api key", "unauthorized", "invalid_api_key"]):
            return True
        
        return False
    
    @staticmethod
    def should_retry_with_proxy(returncode: int, error_msg: str, status: str = "", current_stage: int = 0) -> bool:
        """
        Determine if request should be retried with proxy
        Returns True if error is network/VPN related and stage < 3
        """
        if current_stage >= 3:
            return False
        
        # Network/connection errors
        network_keywords = [
            'connection refused', 'connection reset', 'connection timeout',
            'network', 'dns', 'host unreachable', 'timeout', 'timed out',
            'curl: (6)', 'curl: (7)', 'curl: (28)', 'curl: (35)', 'curl: (56)',
            'ssl', 'certificate', 'handshake'
        ]
        
        if any(keyword in error_msg.lower() for keyword in network_keywords):
            return True
        
        # HTTP errors that indicate proxy/network issues
        if returncode == 22 and any(code in error_msg for code in ['502', '503', '504', '429']):
            return True
        
        # Unusual activity (ElevenLabs VPN detection)
        if any(keyword in error_msg.lower() for keyword in ['unusual activity', 'detected_unusual', 'vpn']):
            return True
        
        return False
    
    @staticmethod  
    def is_voice_error(status: str, error_msg: str) -> bool:
        """
        Check if error is related to voice ID (not key or network)
        Returns True if voice ID is invalid/unavailable
        """
        voice_keywords = ['voice_limit', 'voice not found', 'voice_not_found', 'invalid voice']
        return any(keyword in status.lower() or keyword in error_msg.lower() for keyword in voice_keywords)
    
    @staticmethod
    def get_error_type(returncode: int, error_msg: str, status: str = "", current_stage: int = 0) -> str:
        """
        Classify error and return appropriate error_type
        Returns: 'quota_exceeded', 'vpn_error', 'voice_error', or ''
        """
        # Check if key should be exhausted (triggers key rotation)
        if TTSErrorHandler.should_mark_key_exhausted(returncode, error_msg, status):
            return "quota_exceeded"
        
        # Check if it's a voice error (don't retry, don't rotate key)
        if TTSErrorHandler.is_voice_error(status, error_msg):
            return "voice_error"
        
        # Check if should retry with proxy
        if TTSErrorHandler.should_retry_with_proxy(returncode, error_msg, status, current_stage):
            return "vpn_error"
        
        # Unknown error - treat as vpn_error (don't rotate key)
        return "vpn_error"
