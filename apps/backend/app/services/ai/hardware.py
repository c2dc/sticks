"""Machine memory detection and the model-fits-in-memory rule.

Uses ``nvidia-smi`` for the GPU and the standard library for RAM, so no extra
dependency is needed.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "MEMORY_HEADROOM_FACTOR",
    "GpuMemory",
    "MemoryFit",
    "classify_memory_fit",
    "read_gpu_memory",
    "read_ram_available_bytes",
]

MEMORY_HEADROOM_FACTOR = 1.2
"""Safety multiplier applied to the model file size before comparing it with free memory.

Ollama reports the size of the weights on disk (GGUF). At run time the model also needs
the attention cache (KV cache) for the context window and compute buffers, which grow
with the context length. A 20% margin covers those buffers for the default context of
the 7B-class models this project uses. It is a heuristic and is not measured per model.
Adjust the constant, not the call sites, if a model proves it wrong.
"""

_MIB = 1024 * 1024
_NVIDIA_SMI_TIMEOUT_SECONDS = 5.0


class MemoryFit(StrEnum):
    """Whether a local model fits in the machine memory."""

    FITS_GPU = "fits_gpu"
    FITS_RAM_ONLY = "fits_ram_only"
    TOO_LARGE = "too_large"
    MEMORY_UNKNOWN = "memory_unknown"


@dataclass(frozen=True)
class GpuMemory:
    """Memory of one GPU, in bytes."""

    total_bytes: int
    used_bytes: int

    @property
    def free_bytes(self) -> int:
        """Return the memory not in use."""
        return max(self.total_bytes - self.used_bytes, 0)


def read_gpu_memory() -> GpuMemory | None:
    """Return the memory of the GPU with the most free memory, or ``None``.

    ``None`` means ``nvidia-smi`` is missing, failed, timed out or printed
    something unreadable.
    """
    try:
        result = subprocess.run(
            [  # noqa: S607
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    best: GpuMemory | None = None
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            return None
        try:
            total, used = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if total < 0 or used < 0:
            return None
        gpu = GpuMemory(total_bytes=total * _MIB, used_bytes=used * _MIB)
        if best is None or gpu.free_bytes > best.free_bytes:
            best = gpu
    return best


def _read_ram_windows() -> int | None:
    """Return available physical memory on Windows through ``GlobalMemoryStatusEx``."""
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None
    return int(status.ullAvailPhys)


def _read_ram_linux() -> int | None:
    """Return ``MemAvailable`` from ``/proc/meminfo`` in bytes."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_ram_available_bytes() -> int | None:
    """Return available RAM in bytes, or ``None`` on an unsupported platform or failure."""
    try:
        if sys.platform == "win32":
            return _read_ram_windows()
        if sys.platform.startswith("linux"):
            return _read_ram_linux()
    except (OSError, AttributeError):
        return None
    return None


def classify_memory_fit(
    size_bytes: int | None,
    gpu_free_bytes: int | None,
    ram_free_bytes: int | None,
    loaded_in_vram: bool,
) -> MemoryFit:
    """Decide whether a model of ``size_bytes`` fits, in the order defined by the design."""
    if size_bytes is None or size_bytes <= 0:
        return MemoryFit.MEMORY_UNKNOWN
    if loaded_in_vram:
        # A loaded model already counts as used GPU memory, so it would look too big.
        return MemoryFit.FITS_GPU
    if gpu_free_bytes is None:
        return MemoryFit.MEMORY_UNKNOWN
    required = size_bytes * MEMORY_HEADROOM_FACTOR
    if required <= gpu_free_bytes:
        return MemoryFit.FITS_GPU
    if ram_free_bytes is None:
        # Without the RAM figure it cannot be said that the model does not fit.
        return MemoryFit.MEMORY_UNKNOWN
    if required <= ram_free_bytes:
        return MemoryFit.FITS_RAM_ONLY
    return MemoryFit.TOO_LARGE
