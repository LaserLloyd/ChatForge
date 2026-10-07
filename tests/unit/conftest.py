"""Unit-test defaults: the machine running the tests is not the machine ChatForge targets.

The Linux shell code (``desktop.xutil``), XDG autostart and the Linux-without-an-NPU device fallback depend
on the host, which on a Linux CI runner would change what the Windows-oriented tests see. So
all three are switched off for every test; the tests that cover them switch them back on (see
``test_linux_*.py``).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _host_independent(monkeypatch):
    from chatforge import autostart
    from chatforge.desktop import xutil
    from chatforge.runtime import manager

    monkeypatch.setattr(xutil, "IS_LINUX", False)
    monkeypatch.setattr(manager, "npu_unavailable_on_host", lambda *_a, **_k: False)
    monkeypatch.setattr(autostart, "_is_linux", lambda: False)  # never write ~/.config
