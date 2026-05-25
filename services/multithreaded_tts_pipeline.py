"""
Multithreaded TTS Pipeline System
Producer-Consumer pattern for Phase 1 and Phase 2 processing
"""

import threading
import queue
import time
import logging
from typing import List, Dict, Optional, Tuple, Callable
from dataclasses import dataclass
from enum import Enum
import uuid

from services.tts_service_canada import canada_generate_then_download


class TaskStatus(Enum):
    PENDING = "pending"
    PHASE1_PROCESSING = "phase1_processing"
    PHASE1_COMPLETED = "phase1_completed"
    PHASE2_PROCESSING = "phase2_processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TTSTask:
    """Represents a single TTS task"""
    id: str
    text: str
    voice_id: str
    api_key: str
    payload_json: str
    proxy_cfg: dict
    output_path: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = None
    phase1_completed_at: float = None
    completed_at: float = None
    history_item_id: Optional[str] = None
    error_info: Optional[str] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()


class MultithreadedTTSPipeline:
    """
    Producer-Consumer Pipeline for TTS Processing:
    - Phase 1: Generate TTS via Canada proxy (Producer)
    - Phase 2: Download audio (Consumer)
    - Pipeline flow: Dong 1 Phase 1 → Dong 2 Phase 1 + Dong 1 Phase 2 (song song)
    """

    def __init__(self, max_workers: int = 5, callback: Optional[Callable] = None):
        self.max_workers = max_workers
        self.callback = callback  # UI callback function

        # Queues for pipeline
        self.phase1_queue = queue.Queue(maxsize=max_workers * 2)  # Limit queue size
        self.phase2_queue = queue.Queue()
        self.completed_tasks = {}

        # Thread pools
        self.phase1_threads = []
        self.phase2_threads = []

        # Control flags
        self.is_running = False
        self.should_stop = False

        # Statistics
        self.stats = {
            'total_tasks': 0,
            'phase1_completed': 0,
            'phase2_completed': 0,
            'failed_tasks': 0,
            'start_time': None,
            'end_time': None
        }

        # Setup logging
        self.logger = logging.getLogger('multithreaded_tts')

        # Thread safety
        self.stats_lock = threading.Lock()
        self.tasks_lock = threading.Lock()

    def add_task(self, text: str, voice_id: str, api_key: str, payload_json: str,
                proxy_cfg: dict, output_path: str) -> str:
        """Add a new TTS task to the pipeline"""
        self.logger.info(f"PIPELINE_ADD_TASK_START - text_len={len(text)}, voice_id={voice_id}")

        task_id = str(uuid.uuid4())
        self.logger.info(f"PIPELINE_ADD_TASK_GENERATED_ID - {task_id}")

        task = TTSTask(
            id=task_id,
            text=text,
            voice_id=voice_id,
            api_key=api_key,
            payload_json=payload_json,
            proxy_cfg=proxy_cfg,
            output_path=output_path
        )

        with self.tasks_lock:
            self.completed_tasks[task_id] = task
            self.logger.info(f"PIPELINE_ADD_TASK_STORED - {task_id} in completed_tasks dict")

        if not self.is_running:
            self.logger.error(f"PIPELINE_ADD_TASK_ERROR - Pipeline is not running!")
            return None

        try:
            self.logger.info(f"PIPELINE_ADD_TASK_PUTTING_TO_QUEUE - {task_id}")
            self.phase1_queue.put(task, timeout=1.0)
            with self.stats_lock:
                self.stats['total_tasks'] += 1
            self.logger.info(f"PIPELINE_ADD_TASK_SUCCESS - Added task {task_id} to Phase 1 queue (total: {self.stats['total_tasks']})")
            return task_id
        except queue.Full:
            self.logger.error(f"PIPELINE_ADD_TASK_QUEUE_FULL - Phase 1 queue is full, cannot add task {task_id}")
            return None
        except Exception as e:
            self.logger.error(f"PIPELINE_ADD_TASK_ERROR - Unexpected error adding task {task_id}: {e}")
            return None

    def start(self):
        """Start the pipeline processing"""
        if self.is_running:
            self.logger.warning("Pipeline is already running")
            return

        self.is_running = True
        self.should_stop = False
        self.stats['start_time'] = time.time()

        # Start Phase 1 workers (TTS Generation)
        phase1_workers = max(1, self.max_workers // 2)  # Use half workers for Phase 1
        for i in range(phase1_workers):
            thread = threading.Thread(
                target=self._phase1_worker,
                name=f"Phase1-Worker-{i+1}",
                daemon=True
            )
            thread.start()
            self.phase1_threads.append(thread)

        # Start Phase 2 workers (Download)
        phase2_workers = max(1, self.max_workers - phase1_workers)  # Remaining workers for Phase 2
        for i in range(phase2_workers):
            thread = threading.Thread(
                target=self._phase2_worker,
                name=f"Phase2-Worker-{i+1}",
                daemon=True
            )
            thread.start()
            self.phase2_threads.append(thread)

        # Start monitoring thread
        monitor_thread = threading.Thread(
            target=self._monitor_pipeline,
            name="Pipeline-Monitor",
            daemon=True
        )
        monitor_thread.start()

        self.logger.info(f"Pipeline started with {phase1_workers} Phase 1 workers and {phase2_workers} Phase 2 workers")

    def stop(self):
        """Stop the pipeline processing"""
        self.should_stop = True
        self.is_running = False

        # Wait for threads to finish
        for thread in self.phase1_threads + self.phase2_threads:
            if thread.is_alive():
                thread.join(timeout=2.0)

        self.stats['end_time'] = time.time()
        self.logger.info("Pipeline stopped")

    def _phase1_worker(self):
        """Phase 1 worker: TTS Generation via Canada proxy"""
        self.logger.info(f"Phase 1 worker {threading.current_thread().name} started")

        while not self.should_stop:
            try:
                # Get task from Phase 1 queue
                task = self.phase1_queue.get(timeout=1.0)

                with self.tasks_lock:
                    if self.completed_tasks[task.id].status != TaskStatus.PENDING:
                        self.phase1_queue.task_done()
                        continue

                # Update task status
                with self.tasks_lock:
                    self.completed_tasks[task.id].status = TaskStatus.PHASE1_PROCESSING

                self.logger.info(f"Phase 1 processing task {task.id}: {task.text[:50]}...")

                # Notify UI callback that Phase 1 is starting
                if self.callback:
                    self.callback('phase1_started', task.id, None)

                # Execute Phase 1: Canada TTS Generation
                start_time = time.time()
                success, error_info = canada_generate_then_download(
                    api_key=task.api_key,
                    voice_id=task.voice_id,
                    payload_json=task.payload_json,
                    proxy_cfg=task.proxy_cfg,
                    output_path=task.output_path
                )
                phase1_duration = time.time() - start_time

                if success:
                    # Phase 1 completed successfully - move to Phase 2 queue
                    with self.tasks_lock:
                        self.completed_tasks[task.id].status = TaskStatus.PHASE1_COMPLETED
                        self.completed_tasks[task.id].phase1_completed_at = time.time()

                    self.phase2_queue.put(task)

                    with self.stats_lock:
                        self.stats['phase1_completed'] += 1

                    self.logger.info(f"Phase 1 completed for task {task.id} in {phase1_duration:.2f}s")

                    # Notify UI callback
                    if self.callback:
                        self.callback('phase1_completed', task.id, task.text)

                else:
                    # Phase 1 failed
                    with self.tasks_lock:
                        self.completed_tasks[task.id].status = TaskStatus.FAILED
                        self.completed_tasks[task.id].error_info = error_info

                    with self.stats_lock:
                        self.stats['failed_tasks'] += 1

                    self.logger.error(f"Phase 1 failed for task {task.id}: {error_info}")

                    # Notify UI callback
                    if self.callback:
                        self.callback('phase1_failed', task.id, error_info)

                self.phase1_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Phase 1 worker error: {e}")
                continue

        self.logger.info(f"Phase 1 worker {threading.current_thread().name} stopped")

    def _phase2_worker(self):
        """Phase 2 worker: Download completion"""
        self.logger.info(f"Phase 2 worker {threading.current_thread().name} started")

        while not self.should_stop:
            try:
                # Get task from Phase 2 queue
                task = self.phase2_queue.get(timeout=1.0)

                self.logger.info(f"Phase 2 processing task {task.id}")

                # Notify UI callback that Phase 2 is starting
                if self.callback:
                    self.callback('phase2_started', task.id, None)

                # Phase 2 completion (the download was already done in Phase 1 with direct download)
                # This is mainly for tracking and cleanup

                with self.tasks_lock:
                    self.completed_tasks[task.id].status = TaskStatus.COMPLETED
                    self.completed_tasks[task.id].completed_at = time.time()

                with self.stats_lock:
                    self.stats['phase2_completed'] += 1

                total_time = time.time() - task.created_at
                self.logger.info(f"Task {task.id} completed in {total_time:.2f}s")

                # Notify UI callback
                if self.callback:
                    self.callback('completed', task.id, task.output_path)

                self.phase2_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Phase 2 worker error: {e}")
                continue

        self.logger.info(f"Phase 2 worker {threading.current_thread().name} stopped")

    def _monitor_pipeline(self):
        """Monitor pipeline status and statistics"""
        while not self.should_stop:
            try:
                # Log current status
                with self.stats_lock:
                    phase1_qsize = self.phase1_queue.qsize()
                    phase2_qsize = self.phase2_queue.qsize()
                    total_completed = self.stats['phase2_completed']
                    total_failed = self.stats['failed_tasks']

                self.logger.info(f"Pipeline Status - Phase1 Queue: {phase1_qsize}, Phase2 Queue: {phase2_qsize}, "
                               f"Completed: {total_completed}, Failed: {total_failed}")

                # Notify UI callback with status update
                if self.callback:
                    self.callback('status_update', None, {
                        'phase1_queue': phase1_qsize,
                        'phase2_queue': phase2_qsize,
                        'completed': total_completed,
                        'failed': total_failed
                    })

                time.sleep(5.0)  # Report every 5 seconds

            except Exception as e:
                self.logger.error(f"Monitor error: {e}")
                time.sleep(5.0)

    def get_task_status(self, task_id: str) -> Optional[TTSTask]:
        """Get status of a specific task"""
        with self.tasks_lock:
            return self.completed_tasks.get(task_id)

    def get_statistics(self) -> Dict:
        """Get pipeline statistics"""
        with self.stats_lock:
            stats = self.stats.copy()
            stats['is_running'] = self.is_running
            stats['phase1_queue_size'] = self.phase1_queue.qsize()
            stats['phase2_queue_size'] = self.phase2_queue.qsize()

            if stats['start_time'] and stats['end_time']:
                stats['total_duration'] = stats['end_time'] - stats['start_time']
            elif stats['start_time']:
                stats['total_duration'] = time.time() - stats['start_time']

            return stats

    def get_all_tasks(self) -> List[TTSTask]:
        """Get all tasks with their current status"""
        with self.tasks_lock:
            return list(self.completed_tasks.values())


# Utility function to create and manage pipeline instance
_pipeline_instance: Optional[MultithreadedTTSPipeline] = None

def get_tts_pipeline(max_workers: int = 5, callback: Optional[Callable] = None) -> MultithreadedTTSPipeline:
    """Get or create TTS pipeline instance"""
    global _pipeline_instance

    if _pipeline_instance is None or not _pipeline_instance.is_running:
        _pipeline_instance = MultithreadedTTSPipeline(max_workers=max_workers, callback=callback)

    return _pipeline_instance

def stop_tts_pipeline():
    """Stop the current TTS pipeline"""
    global _pipeline_instance

    if _pipeline_instance and _pipeline_instance.is_running:
        _pipeline_instance.stop()
        _pipeline_instance = None