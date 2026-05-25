"""
Worker-Key Allocator - STABLE MODE

Mỗi worker được phân bổ dedicated key pool để tránh conflict.
Prioritize stability over speed.
"""

import logging
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass
import time

logger = logging.getLogger('worker_key_allocator')


@dataclass
class WorkerKeyPool:
    """Dedicated key pool for one worker"""
    worker_id: int
    allocated_keys: List[str]  # API keys cho worker này
    current_key_index: int = 0  # Round-robin index
    failed_keys: set = None  # Keys đã fail
    
    def __post_init__(self):
        if self.failed_keys is None:
            self.failed_keys = set()
    
    def get_next_key(self) -> Optional[str]:
        """Get next available key (round-robin)"""
        if not self.allocated_keys:
            return None
        
        # Filter out failed keys
        available_keys = [k for k in self.allocated_keys if k not in self.failed_keys]
        
        if not available_keys:
            # All keys failed, reset and try again
            logger.warning(f"WORKER_{self.worker_id} - All keys failed, resetting")
            self.failed_keys.clear()
            available_keys = self.allocated_keys
        
        # Round-robin selection
        key = available_keys[self.current_key_index % len(available_keys)]
        self.current_key_index += 1
        
        return key
    
    def mark_key_failed(self, api_key: str):
        """Mark key as failed"""
        self.failed_keys.add(api_key)
        logger.warning(f"WORKER_{self.worker_id} - Key {api_key[:10]}... marked as failed")
    
    def reset_key(self, api_key: str):
        """Reset key (remove from failed list)"""
        self.failed_keys.discard(api_key)


class WorkerKeyAllocator:
    """
    Allocate dedicated key pools to workers for STABILITY.
    
    Each worker gets N keys (default 3) to avoid conflict.
    When worker needs key, it gets from its dedicated pool.
    """
    
    def __init__(self, num_workers: int, keys_per_worker: int = 3):
        """
        Initialize allocator.
        
        Args:
            num_workers: Number of workers (1-10)
            keys_per_worker: Keys per worker (default: 3)
        """
        self.num_workers = num_workers
        self.keys_per_worker = keys_per_worker
        self.worker_pools: Dict[int, WorkerKeyPool] = {}
        self.lock = threading.Lock()
        self._allocated = False
        
        logger.info(f"WorkerKeyAllocator initialized: {num_workers} workers, {keys_per_worker} keys each")
    
    def allocate_keys(self, available_keys: List[str]) -> bool:
        """
        Allocate keys to workers.
        
        Args:
            available_keys: List of API keys to distribute
            
        Returns:
            True if successful, False if not enough keys
        """
        total_needed = self.num_workers * self.keys_per_worker
        
        if len(available_keys) < total_needed:
            logger.error(
                f"ALLOCATION_FAILED - Need {total_needed} keys "
                f"({self.num_workers} workers × {self.keys_per_worker} keys), "
                f"but only {len(available_keys)} available"
            )
            return False
        
        with self.lock:
            # Distribute keys evenly to workers
            for worker_id in range(self.num_workers):
                start_idx = worker_id * self.keys_per_worker
                end_idx = start_idx + self.keys_per_worker
                worker_keys = available_keys[start_idx:end_idx]
                
                self.worker_pools[worker_id] = WorkerKeyPool(
                    worker_id=worker_id,
                    allocated_keys=worker_keys
                )
                
                key_previews = [k[:10] + "..." for k in worker_keys]
                logger.info(f"WORKER_{worker_id} allocated {len(worker_keys)} keys: {key_previews}")
            
            self._allocated = True
            logger.info(f"✅ ALLOCATION_SUCCESS - {total_needed} keys distributed to {self.num_workers} workers")
            
            return True
    
    def get_key_for_worker(self, worker_id: int) -> Optional[str]:
        """
        Get next key for specific worker.
        
        Args:
            worker_id: Worker ID (0 to num_workers-1)
            
        Returns:
            API key or None if worker has no available keys
        """
        if not self._allocated:
            logger.error("ALLOCATION_NOT_DONE - Call allocate_keys() first")
            return None
        
        if worker_id not in self.worker_pools:
            logger.error(f"INVALID_WORKER_ID - Worker {worker_id} not found")
            return None
        
        with self.lock:
            key = self.worker_pools[worker_id].get_next_key()
            if key:
                logger.debug(f"WORKER_{worker_id} - Using key {key[:10]}...")
            return key
    
    def mark_key_failed(self, worker_id: int, api_key: str):
        """Mark key as failed for this worker"""
        if worker_id in self.worker_pools:
            with self.lock:
                self.worker_pools[worker_id].mark_key_failed(api_key)
    
    def reset_key(self, worker_id: int, api_key: str):
        """Reset key (make it available again)"""
        if worker_id in self.worker_pools:
            with self.lock:
                self.worker_pools[worker_id].reset_key(api_key)
    
    def get_allocation_summary(self) -> Dict:
        """Get summary of key allocation"""
        with self.lock:
            total_keys = sum(len(pool.allocated_keys) for pool in self.worker_pools.values())
            total_failed = sum(len(pool.failed_keys) for pool in self.worker_pools.values())
            
            worker_stats = []
            for worker_id, pool in self.worker_pools.items():
                worker_stats.append({
                    'worker_id': worker_id,
                    'total_keys': len(pool.allocated_keys),
                    'failed_keys': len(pool.failed_keys),
                    'available_keys': len(pool.allocated_keys) - len(pool.failed_keys)
                })
            
            return {
                'num_workers': self.num_workers,
                'keys_per_worker': self.keys_per_worker,
                'total_keys': total_keys,
                'total_failed': total_failed,
                'total_available': total_keys - total_failed,
                'workers': worker_stats
            }
    
    def log_summary(self):
        """Log allocation summary"""
        summary = self.get_allocation_summary()
        logger.info(
            f"ALLOCATION_SUMMARY - {summary['num_workers']} workers, "
            f"{summary['total_keys']} keys total, "
            f"{summary['total_available']} available, "
            f"{summary['total_failed']} failed"
        )
        
        for worker in summary['workers']:
            logger.info(
                f"  WORKER_{worker['worker_id']}: {worker['available_keys']}/{worker['total_keys']} keys available"
            )


# Testing
if __name__ == "__main__":
    # Test with 5 workers, 3 keys each
    allocator = WorkerKeyAllocator(num_workers=5, keys_per_worker=3)
    
    # Generate test keys
    test_keys = [f"sk_test_{i:03d}" for i in range(20)]
    
    # Allocate
    if allocator.allocate_keys(test_keys):
        print("✅ Allocation successful")
        allocator.log_summary()
        
        # Test getting keys
        for worker_id in range(5):
            for i in range(3):
                key = allocator.get_key_for_worker(worker_id)
                print(f"Worker {worker_id}, Request {i}: {key}")
        
        # Test marking failed
        allocator.mark_key_failed(0, test_keys[0])
        allocator.log_summary()
    else:
        print("❌ Allocation failed")

