"""
Database Configuration - Switch between Supabase and Cloudflare D1
"""

# Database backend: "supabase" or "d1"
DB_BACKEND = "d1"  # Changed to D1 after migration

# D1 Configuration
D1_WORKER_URL = "https://tts-api.kh431248.workers.dev"
D1_API_KEY = "tts-d1-secret-key-2026"


def get_db_client():
    """Get database client based on configuration"""
    if DB_BACKEND == "d1":
        from services.d1_client import D1Client
        return D1Client(D1_WORKER_URL, D1_API_KEY)
    else:
        from services.supabase_client import SupabaseAuth
        return SupabaseAuth().client


def get_auth_client():
    """Get auth client based on configuration"""
    if DB_BACKEND == "d1":
        from services.d1_client import D1Auth
        return D1Auth()
    else:
        from services.supabase_client import SupabaseAuth
        return SupabaseAuth()


# Export for easy import
__all__ = ['DB_BACKEND', 'get_db_client', 'get_auth_client']
