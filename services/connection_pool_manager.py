"""
Connection Pool Health Manager

Manages HTTP/2 connection pools and monitors their health.
Automatically detects and handles unhealthy connections.
"""

import logging
import time
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger('connection_pool')


@dataclass
class ConnectionStats:
    """Statistics for a single connection"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_time: float = 0.0
    last_used: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate (0-1)"""
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests
    
    @property
    def avg_response_time(self) -> float:
        """Average response time in seconds"""
        if self.successful_requests == 0:
            return 0.0
        return self.total_time / self.successful_requests
    
    @property
    def health_score(self) -> float:
        """
        Calculate health score (0-100).
        
        Factors:
        - Success rate (50% weight)
        - Response time (25% weight - faster is better)
        - Recent usage (15% weight - recent activity is good)
        - Consecutive failures (10% weight - penalize failures)
        """
        # Success rate component (0-50 points)
        success_component = self.success_rate * 50
        
        # Response time component (0-25 points, inverse of response time)
        # Good: <2s = 25 points, OK: 2-5s = 15 points, Bad: >5s = 5 points
        if self.avg_response_time < 2.0:
            time_component = 25
        elif self.avg_response_time < 5.0:
            time_component = 15
        else:
            time_component = 5
        
        # Recent usage component (0-15 points)
        # Good: <5min = 15 points, OK: 5-15min = 10 points, Bad: >15min = 0 points
        time_since_use = time.time() - self.last_used
        if time_since_use < 300:  # 5 minutes
            recency_component = 15
        elif time_since_use < 900:  # 15 minutes
            recency_component = 10
        else:
            recency_component = 0
        
        # Consecutive failures penalty (0-10 points)
        # No failures = 10 points, 1-2 failures = 5 points, 3+ failures = 0 points
        if self.consecutive_failures == 0:
            failure_component = 10
        elif self.consecutive_failures < 3:
            failure_component = 5
        else:
            failure_component = 0
        
        return success_component + time_component + recency_component + failure_component


class ConnectionPoolManager:
    """
    Manages HTTP/2 connection pools with health monitoring.
    
    Features:
    - Track connection statistics
    - Monitor health scores
    - Automatic cleanup of stale connections
    - Thread-safe operations
    """
    
    def __init__(self, cleanup_interval: int = 300, stale_timeout: int = 1800):
        """
        Initialize connection pool manager.
        
        Args:
            cleanup_interval: Seconds between cleanup runs (default: 5 minutes)
            stale_timeout: Seconds before connection considered stale (default: 30 minutes)
        """
        self.stats: Dict[str, ConnectionStats] = {}
        self.lock = threading.RLock()
        self.cleanup_interval = cleanup_interval
        self.stale_timeout = stale_timeout
        self.last_cleanup = time.time()
        
        logger.info(f"ConnectionPoolManager initialized (cleanup={cleanup_interval}s, stale={stale_timeout}s)")
    
    def record_request(self, connection_id: str, success: bool, response_time: float, error: Optional[str] = None):
        """
        Record a request result.
        
        Args:
            connection_id: Unique identifier for connection (e.g., "proxy_host:port")
            success: Whether request succeeded
            response_time: Response time in seconds
            error: Error message if failed
        """
        with self.lock:
            if connection_id not in self.stats:
                self.stats[connection_id] = ConnectionStats()
            
            stats = self.stats[connection_id]
            stats.total_requests += 1
            stats.last_used = time.time()
            
            if success:
                stats.successful_requests += 1
                stats.total_time += response_time
                stats.consecutive_failures = 0
                logger.debug(f"CONN_{connection_id} - SUCCESS ({response_time:.2f}s)")
            else:
                stats.failed_requests += 1
                stats.consecutive_failures += 1
                stats.last_error = error
                logger.warning(f"CONN_{connection_id} - FAILED (consecutive={stats.consecutive_failures}): {error}")
            
            # Trigger cleanup if needed
            if time.time() - self.last_cleanup > self.cleanup_interval:
                self._cleanup_stale_connections()
    
    def get_health_score(self, connection_id: str) -> float:
        """
        Get health score for a connection.
        
        Returns:
            Health score 0-100 (higher is better), or 100 if no stats
        """
        with self.lock:
            if connection_id not in self.stats:
                return 100.0  # New connection, assume healthy
            return self.stats[connection_id].health_score
    
    def get_healthy_connections(self, min_score: float = 50.0) -> List[str]:
        """
        Get list of healthy connections.
        
        Args:
            min_score: Minimum health score required (default: 50)
            
        Returns:
            List of connection IDs sorted by health score (best first)
        """
        with self.lock:
            healthy = [
                (conn_id, stats.health_score)
                for conn_id, stats in self.stats.items()
                if stats.health_score >= min_score
            ]
            # Sort by health score descending
            healthy.sort(key=lambda x: x[1], reverse=True)
            return [conn_id for conn_id, _ in healthy]
    
    def is_connection_healthy(self, connection_id: str, min_score: float = 50.0) -> bool:
        """
        Check if connection is healthy.
        
        Args:
            connection_id: Connection to check
            min_score: Minimum health score required
            
        Returns:
            True if healthy, False otherwise
        """
        return self.get_health_score(connection_id) >= min_score
    
    def get_stats(self, connection_id: str) -> Optional[ConnectionStats]:
        """Get statistics for a connection"""
        with self.lock:
            return self.stats.get(connection_id)
    
    def get_all_stats(self) -> Dict[str, ConnectionStats]:
        """Get all connection statistics"""
        with self.lock:
            return self.stats.copy()
    
    def reset_stats(self, connection_id: str):
        """Reset statistics for a connection"""
        with self.lock:
            if connection_id in self.stats:
                del self.stats[connection_id]
                logger.info(f"CONN_{connection_id} - Stats reset")
    
    def _cleanup_stale_connections(self):
        """Remove stale connections (not used recently)"""
        with self.lock:
            now = time.time()
            stale_ids = [
                conn_id
                for conn_id, stats in self.stats.items()
                if now - stats.last_used > self.stale_timeout
            ]
            
            for conn_id in stale_ids:
                del self.stats[conn_id]
                logger.info(f"CONN_{conn_id} - Removed (stale)")
            
            if stale_ids:
                logger.info(f"CLEANUP - Removed {len(stale_ids)} stale connections")
            
            self.last_cleanup = now
    
    def get_summary(self) -> Dict:
        """
        Get summary of all connections.
        
        Returns:
            Dictionary with summary statistics
        """
        with self.lock:
            if not self.stats:
                return {
                    "total_connections": 0,
                    "healthy_connections": 0,
                    "unhealthy_connections": 0,
                    "avg_health_score": 0.0,
                    "total_requests": 0,
                    "total_successes": 0,
                    "total_failures": 0,
                    "overall_success_rate": 0.0
                }
            
            healthy_count = len(self.get_healthy_connections(min_score=50.0))
            total_requests = sum(s.total_requests for s in self.stats.values())
            total_successes = sum(s.successful_requests for s in self.stats.values())
            total_failures = sum(s.failed_requests for s in self.stats.values())
            avg_health = sum(s.health_score for s in self.stats.values()) / len(self.stats)
            
            return {
                "total_connections": len(self.stats),
                "healthy_connections": healthy_count,
                "unhealthy_connections": len(self.stats) - healthy_count,
                "avg_health_score": round(avg_health, 2),
                "total_requests": total_requests,
                "total_successes": total_successes,
                "total_failures": total_failures,
                "overall_success_rate": round(total_successes / total_requests, 3) if total_requests > 0 else 0.0
            }
    
    def log_summary(self):
        """Log summary to logger"""
        summary = self.get_summary()
        logger.info(f"CONNECTION_POOL_SUMMARY: {summary['total_connections']} connections, "
                   f"health={summary['avg_health_score']:.1f}, "
                   f"success_rate={summary['overall_success_rate']:.1%}")


# Global instance
_global_pool_manager: Optional[ConnectionPoolManager] = None
_manager_lock = threading.Lock()


def get_connection_pool_manager() -> ConnectionPoolManager:
    """Get global connection pool manager (singleton)"""
    global _global_pool_manager
    
    if _global_pool_manager is None:
        with _manager_lock:
            if _global_pool_manager is None:
                _global_pool_manager = ConnectionPoolManager()
    
    return _global_pool_manager


if __name__ == "__main__":
    # Simple test
    import random
    
    manager = ConnectionPoolManager(cleanup_interval=10, stale_timeout=30)
    
    # Simulate some connections
    connections = ["proxy1:8080", "proxy2:8080", "proxy3:8080"]
    
    print("Simulating requests...")
    for i in range(50):
        conn = random.choice(connections)
        success = random.random() > 0.2  # 80% success rate
        response_time = random.uniform(0.5, 3.0)
        error = None if success else "Connection timeout"
        
        manager.record_request(conn, success, response_time, error)
    
    print("\nConnection Summary:")
    summary = manager.get_summary()
    for key, value in summary.items():
        print(f"  {key}: {value}")
    
    print("\nHealthy Connections:")
    healthy = manager.get_healthy_connections(min_score=60.0)
    for conn_id in healthy:
        score = manager.get_health_score(conn_id)
        print(f"  {conn_id}: {score:.1f}")
    
    print("\nDetailed Stats:")
    for conn_id, stats in manager.get_all_stats().items():
        print(f"  {conn_id}:")
        print(f"    Requests: {stats.total_requests} (success={stats.successful_requests}, fail={stats.failed_requests})")
        print(f"    Success Rate: {stats.success_rate:.1%}")
        print(f"    Avg Response Time: {stats.avg_response_time:.2f}s")
        print(f"    Health Score: {stats.health_score:.1f}")
        print(f"    Consecutive Failures: {stats.consecutive_failures}")

