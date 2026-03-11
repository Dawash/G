"""
System Monitor — collects CPU/RAM/Disk/Network/GPU stats every 2 seconds.

Runs on a QThread and emits a statsUpdated signal with a dict payload
that the bridge forwards to the JS gauges in the Jarvis HUD.
"""

import logging
import time

from PyQt6.QtCore import QThread, pyqtSignal

try:
    import psutil
except ImportError:
    psutil = None

try:
    import GPUtil
except ImportError:
    GPUtil = None

logger = logging.getLogger(__name__)


class SystemMonitorThread(QThread):
    """Collects system stats and emits them every 2 seconds."""

    statsUpdated = pyqtSignal(dict)

    def __init__(self, interval: float = 2.0, parent=None):
        super().__init__(parent)
        self._interval = interval
        self._running = True
        self._prev_net = None
        self._prev_time = None

    def run(self):
        while self._running:
            try:
                stats = self._collect()
                self.statsUpdated.emit(stats)
            except Exception as e:
                logger.error(f"System monitor error: {e}")
            time.sleep(self._interval)

    def stop(self):
        self._running = False
        self.wait(3000)

    def _collect(self) -> dict:
        stats = {
            "cpu": 0.0,
            "ram": 0.0,
            "ram_used_gb": 0.0,
            "ram_total_gb": 0.0,
            "disk": 0.0,
            "disk_used_gb": 0.0,
            "disk_total_gb": 0.0,
            "gpu": 0.0,
            "gpu_name": "",
            "gpu_mem_used_mb": 0.0,
            "gpu_mem_total_mb": 0.0,
            "gpu_temp": 0,
            "net_up_kbs": 0.0,
            "net_down_kbs": 0.0,
            "cpu_freq_ghz": 0.0,
            "cpu_cores": 0,
            "uptime_hours": 0.0,
        }

        if not psutil:
            return stats

        # CPU
        stats["cpu"] = psutil.cpu_percent(interval=0.5)
        freq = psutil.cpu_freq()
        if freq:
            stats["cpu_freq_ghz"] = round(freq.current / 1000, 2)
        stats["cpu_cores"] = psutil.cpu_count(logical=True) or 0

        # RAM
        mem = psutil.virtual_memory()
        stats["ram"] = mem.percent
        stats["ram_used_gb"] = round(mem.used / (1024 ** 3), 1)
        stats["ram_total_gb"] = round(mem.total / (1024 ** 3), 1)

        # Disk (C:)
        try:
            disk = psutil.disk_usage("C:\\")
            stats["disk"] = disk.percent
            stats["disk_used_gb"] = round(disk.used / (1024 ** 3), 1)
            stats["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
        except Exception:
            pass

        # Network throughput
        try:
            net = psutil.net_io_counters()
            now = time.time()
            if self._prev_net and self._prev_time:
                dt = now - self._prev_time
                if dt > 0:
                    stats["net_up_kbs"] = round((net.bytes_sent - self._prev_net.bytes_sent) / dt / 1024, 1)
                    stats["net_down_kbs"] = round((net.bytes_recv - self._prev_net.bytes_recv) / dt / 1024, 1)
            self._prev_net = net
            self._prev_time = now
        except Exception:
            pass

        # Uptime
        try:
            stats["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        except Exception:
            pass

        # GPU (NVIDIA via GPUtil)
        if GPUtil:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    gpu = gpus[0]
                    stats["gpu"] = gpu.load * 100
                    stats["gpu_name"] = gpu.name
                    stats["gpu_mem_used_mb"] = round(gpu.memoryUsed, 0)
                    stats["gpu_mem_total_mb"] = round(gpu.memoryTotal, 0)
                    stats["gpu_temp"] = round(gpu.temperature, 0) if gpu.temperature else 0
            except Exception:
                pass

        return stats
