"""
Isolated tool executor — runs tools in subprocess workers.

Spawns worker processes that execute tools in isolation. If a worker
crashes or times out, the main process continues normally.

Usage:
    executor = IsolatedToolExecutor()
    result = executor.execute("run_terminal", {"command": "dir"})
    executor.shutdown()
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_PYTHON = sys.executable
_WORKER_MODULE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "process_runner.py")


class _Worker:
    """A single worker subprocess."""

    def __init__(self):
        self.process = None
        self.busy = False
        self._read_lock = threading.Lock()

    def start(self):
        """Spawn the worker process."""
        try:
            self.process = subprocess.Popen(
                [_PYTHON, _WORKER_MODULE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            # Wait for ready signal
            ready_line = self._read_line(timeout=10)
            if ready_line and ready_line.get("status") == "ready":
                logger.debug("Worker started and ready")
                return True
            else:
                logger.warning(f"Worker did not send ready signal: {ready_line}")
                self.kill()
                return False
        except Exception as e:
            logger.error(f"Failed to start worker: {e}")
            return False

    def send(self, request, timeout=30):
        """Send a request and wait for response with timeout."""
        if not self.process or self.process.poll() is not None:
            return {"ok": False, "error": "Worker not running"}

        self.busy = True
        try:
            # Write request
            line = json.dumps(request) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()

            # Read response with timeout (using a thread since Windows pipes don't support select)
            result = self._read_line(timeout=timeout)
            if result is None:
                # Timeout — kill the worker
                logger.warning(f"Worker timed out after {timeout}s, killing")
                self.kill()
                return {"ok": False, "error": f"Tool timed out after {timeout}s"}

            return result
        except (BrokenPipeError, OSError) as e:
            logger.warning(f"Worker pipe error: {e}")
            self.kill()
            return {"ok": False, "error": f"Worker crashed: {e}"}
        finally:
            self.busy = False

    def _read_line(self, timeout=30):
        """Read one line from stdout with timeout (Windows-safe)."""
        result = [None]
        error = [None]

        def _reader():
            try:
                line = self.process.stdout.readline()
                if line:
                    result[0] = json.loads(line.strip())
            except Exception as e:
                error[0] = e

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            # Timeout
            return None

        if error[0]:
            raise error[0]

        return result[0]

    def is_alive(self):
        return self.process is not None and self.process.poll() is None

    def kill(self):
        """Kill the worker process."""
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None
        self.busy = False


class IsolatedToolExecutor:
    """Executes tools in subprocess workers with crash isolation.

    Maintains a small pool of workers. Risky tools are sent to workers;
    if a worker crashes, it's replaced and the main process continues.
    """

    def __init__(self, max_workers=2, default_timeout=30):
        self._max_workers = max_workers
        self._default_timeout = default_timeout
        self._workers = []
        self._lock = threading.Lock()
        self._started = False

    def _ensure_workers(self):
        """Ensure at least one worker is running."""
        if self._started:
            # Replace dead workers
            self._workers = [w for w in self._workers if w.is_alive()]

        while len(self._workers) < self._max_workers:
            w = _Worker()
            if w.start():
                self._workers.append(w)
            else:
                break  # Can't start workers

        self._started = True

    def _get_worker(self):
        """Get an available (non-busy) worker."""
        with self._lock:
            self._ensure_workers()

            # Find a free worker
            for w in self._workers:
                if not w.busy and w.is_alive():
                    return w

            # All busy — try to spawn another if under limit
            if len(self._workers) < self._max_workers:
                w = _Worker()
                if w.start():
                    self._workers.append(w)
                    return w

            # Wait for any worker to become free (round-robin first available)
            for w in self._workers:
                if w.is_alive():
                    return w  # It'll block in send() until the current task finishes

            # No workers at all — spawn one
            w = _Worker()
            if w.start():
                self._workers.append(w)
                return w

            return None

    def execute(self, tool_name, arguments, timeout=None):
        """Execute a tool in an isolated worker process.

        Args:
            tool_name: Name of the tool to execute
            arguments: Dict of tool arguments
            timeout: Seconds to wait (default: self._default_timeout)

        Returns:
            str result or error message. Never raises.
        """
        timeout = timeout or self._default_timeout

        worker = self._get_worker()
        if not worker:
            return f"Error: Could not start tool worker for {tool_name}"

        request = {
            "id": uuid.uuid4().hex[:12],
            "tool": tool_name,
            "args": arguments,
        }

        response = worker.send(request, timeout=timeout)

        if response.get("ok"):
            return response.get("result", "Done")
        else:
            error = response.get("error", "Unknown error")
            logger.warning(f"Isolated tool {tool_name} failed: {error}")

            # If worker died, remove it so a fresh one is spawned next time
            if not worker.is_alive():
                with self._lock:
                    if worker in self._workers:
                        self._workers.remove(worker)

            return f"Error: {error}"

    def shutdown(self):
        """Kill all worker processes."""
        with self._lock:
            for w in self._workers:
                w.kill()
            self._workers.clear()
            self._started = False
        logger.info("Isolated executor shut down")

    @property
    def worker_count(self):
        return len([w for w in self._workers if w.is_alive()])
