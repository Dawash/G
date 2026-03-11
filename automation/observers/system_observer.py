"""System state observer — CPU, RAM, battery, disk, network.

Reads system metrics via psutil. No side effects. Sub-second execution.
Replaces spawning PowerShell for simple system info queries.
"""

import logging
import time
from dataclasses import dataclass

from automation.observers.base import ObservationResult

logger = logging.getLogger(__name__)


@dataclass
class SystemInfo:
    """Structured system state."""
    cpu_percent: float = 0.0
    cpu_count: int = 0
    ram_total_gb: float = 0.0
    ram_used_gb: float = 0.0
    ram_percent: float = 0.0
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_percent: float = 0.0
    battery_percent: float = -1.0   # -1 = no battery
    battery_plugged: bool = False
    uptime_hours: float = 0.0
    hostname: str = ""
    ip_address: str = ""
    network_connected: bool = False

    def summary(self):
        """Human-readable one-liner."""
        parts = [
            f"CPU: {self.cpu_percent}%",
            f"RAM: {self.ram_used_gb:.1f}/{self.ram_total_gb:.1f} GB ({self.ram_percent}%)",
            f"Disk: {self.disk_free_gb:.1f} GB free ({self.disk_percent}% used)",
        ]
        if self.battery_percent >= 0:
            plug = "plugged in" if self.battery_plugged else "on battery"
            parts.append(f"Battery: {self.battery_percent:.0f}% ({plug})")
        if self.ip_address:
            parts.append(f"IP: {self.ip_address}")
        return " | ".join(parts)


class SystemObserver:
    """Reads system metrics via psutil. No side effects."""

    def __init__(self):
        self._last_info = None
        self._last_time = 0.0

    def get_system_info(self, cache_ttl=2.0):
        """Get comprehensive system info.

        Args:
            cache_ttl: Seconds to cache results (CPU% needs time to sample).

        Returns:
            SystemInfo
        """
        now = time.time()
        if self._last_info and (now - self._last_time) < cache_ttl:
            return self._last_info

        info = SystemInfo()

        try:
            import psutil
        except ImportError:
            return info

        try:
            # CPU
            info.cpu_percent = psutil.cpu_percent(interval=0.1)
            info.cpu_count = psutil.cpu_count()

            # RAM
            mem = psutil.virtual_memory()
            info.ram_total_gb = round(mem.total / (1024**3), 1)
            info.ram_used_gb = round(mem.used / (1024**3), 1)
            info.ram_percent = mem.percent

            # Disk
            import shutil
            disk = shutil.disk_usage("C:\\")
            info.disk_total_gb = round(disk.total / (1024**3), 1)
            info.disk_used_gb = round(disk.used / (1024**3), 1)
            info.disk_free_gb = round(disk.free / (1024**3), 1)
            info.disk_percent = round(100 * disk.used / disk.total, 1)

            # Battery
            bat = psutil.sensors_battery()
            if bat:
                info.battery_percent = bat.percent
                info.battery_plugged = bat.power_plugged

            # Uptime
            info.uptime_hours = round((time.time() - psutil.boot_time()) / 3600, 1)

            # Network & hostname
            import socket
            info.hostname = socket.gethostname()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(1)
                s.connect(("8.8.8.8", 80))
                info.ip_address = s.getsockname()[0]
                s.close()
                info.network_connected = True
            except Exception:
                info.network_connected = False

        except Exception as e:
            logger.debug(f"system_info error: {e}")

        self._last_info = info
        self._last_time = now
        return info

    def get_cpu_percent(self):
        return self.get_system_info().cpu_percent

    def get_ram_usage(self):
        info = self.get_system_info()
        return {"used_gb": info.ram_used_gb, "total_gb": info.ram_total_gb,
                "percent": info.ram_percent}

    def get_disk_usage(self, drive="C:"):
        info = self.get_system_info()
        return {"free_gb": info.disk_free_gb, "total_gb": info.disk_total_gb,
                "percent": info.disk_percent}

    def get_battery(self):
        info = self.get_system_info()
        if info.battery_percent < 0:
            return None
        return {"percent": info.battery_percent, "plugged": info.battery_plugged}

    def get_ip_address(self):
        return self.get_system_info().ip_address

    def is_network_connected(self):
        return self.get_system_info().network_connected

    def get_top_processes(self, n=10):
        """Get top N processes by memory usage."""
        try:
            import psutil
            procs = []
            for p in psutil.process_iter(["name", "pid", "memory_info", "cpu_percent"]):
                try:
                    mem = p.info.get("memory_info")
                    procs.append({
                        "name": p.info["name"],
                        "pid": p.info["pid"],
                        "memory_mb": round(mem.rss / 1048576, 1) if mem else 0,
                        "cpu_percent": p.info.get("cpu_percent", 0) or 0,
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            procs.sort(key=lambda x: x["memory_mb"], reverse=True)
            return procs[:n]
        except ImportError:
            return []

    def observe(self):
        """Full system snapshot.

        Returns:
            ObservationResult
        """
        info = self.get_system_info()
        data = {
            "cpu_percent": info.cpu_percent,
            "cpu_count": info.cpu_count,
            "ram": {"used_gb": info.ram_used_gb, "total_gb": info.ram_total_gb,
                    "percent": info.ram_percent},
            "disk": {"free_gb": info.disk_free_gb, "total_gb": info.disk_total_gb,
                     "percent": info.disk_percent},
            "battery": {"percent": info.battery_percent,
                       "plugged": info.battery_plugged}
                      if info.battery_percent >= 0 else None,
            "network": {"connected": info.network_connected,
                       "ip": info.ip_address},
            "uptime_hours": info.uptime_hours,
            "hostname": info.hostname,
            "summary": info.summary(),
        }

        return ObservationResult(
            domain="system",
            data=data,
            source="psutil",
            stale_after=5.0,
        )

    def answer_query(self, query):
        """Answer a system info query directly, avoiding PowerShell.

        Args:
            query: Normalized query like "disk space", "ram", "cpu", etc.

        Returns:
            str answer, or None if can't answer (fall through to terminal).
        """
        q = query.lower().strip()
        info = self.get_system_info()

        if any(k in q for k in ("disk", "storage", "space", "drive")):
            return (f"Disk C: {info.disk_used_gb:.1f} GB used / "
                    f"{info.disk_total_gb:.1f} GB total "
                    f"({info.disk_free_gb:.1f} GB free, {info.disk_percent}% used)")

        if any(k in q for k in ("ram", "memory")):
            return (f"RAM: {info.ram_used_gb:.1f} GB used / "
                    f"{info.ram_total_gb:.1f} GB total ({info.ram_percent}% used)")

        if "cpu" in q:
            return f"CPU: {info.cpu_percent}% usage across {info.cpu_count} cores"

        if "battery" in q:
            if info.battery_percent < 0:
                return "No battery detected (desktop PC)."
            plug = "plugged in" if info.battery_plugged else "on battery"
            return f"Battery: {info.battery_percent:.0f}% ({plug})"

        if any(k in q for k in ("ip", "network", "internet", "wifi", "connected")):
            if info.network_connected:
                return f"Connected. IP address: {info.ip_address}"
            return "Not connected to the internet."

        if any(k in q for k in ("uptime", "how long", "running")):
            return f"System uptime: {info.uptime_hours:.1f} hours"

        if any(k in q for k in ("hostname", "computer name", "pc name")):
            return f"Computer name: {info.hostname}"

        # Full summary for generic "system info" (avoid matching "git status" etc.)
        if any(k in q for k in ("system info", "system status", "system overview",
                                 "computer info", "pc status")):
            return info.summary()

        return None
