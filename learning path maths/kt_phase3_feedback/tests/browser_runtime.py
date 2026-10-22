"""Shared Windows-resilient helpers for the Edge browser test fixtures."""
import time
from pathlib import Path


def read_devtools_port(profile, timeout=10.0):
    """Return the first line of Edge's DevToolsActivePort file.

    Edge can briefly keep the port file locked or absent while the
    process starts, so only FileNotFoundError/PermissionError retry.
    """
    port_file = Path(profile) / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            lines = port_file.read_text(
                encoding="utf-8").splitlines()
        except (FileNotFoundError, PermissionError) as error:
            last_error = error
        else:
            if lines:
                return lines[0]
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "DevToolsActivePort not readable under "
                f"{port_file}") from last_error
        time.sleep(.1)


def cleanup_browser_directory(tmp, timeout=5.0):
    """Call tmp.cleanup(), retrying only PermissionError until deadline.

    Windows may briefly retain child-process cache handles after the main
    Edge process exits, so the first cleanup attempt can hit a sharing
    violation that clears on retry. Other errors propagate immediately
    and a persistent PermissionError re-raises after the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            tmp.cleanup()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)
