"""Real-thread integration test for :class:`FirstRunWizard` (M6 rewrite).

The unit-level wizard tests inject fake workers, which side-steps the
cross-thread dispatch path the production wizard uses. The original
M4-C version of this test pinned the InfoWorker probe (the public-server
wizard); the M6 Echos rewrite replaced that machinery, so this is the
consciously rewritten equivalent for the SAME contract class: page
actions reach a REAL worker on a REAL QThread via queued connections and
the GUI thread never blocks on the network.

Here a real :class:`EchosDiscoveryWorker` (browse injected, probes
served by the pinned :class:`FakeEchosFirmware` transport) lives on the
wizard's own thread; the Find page's auto-scan must round-trip into a
selectable, probe-confirmed row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread

from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.discovery import EchosDiscoveryWorker, _Candidate
from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.gui.dialogs.first_run_wizard import FirstRunWizard
from tests.core.echos_fake import FakeEchosFirmware


def test_wizard_scan_dispatches_through_real_worker_thread(
    qtbot: Any, tmp_path: Path
) -> None:
    """The Find page's scan runs on the wizard's worker thread (queued
    dispatch — rule 1): the probe-confirmed device lands back in the
    table and is selectable, and the browse demonstrably executed OFF
    the GUI thread."""
    fw = FakeEchosFirmware()
    browse_threads: list[QThread] = []

    async def browse() -> list[_Candidate]:
        browse_threads.append(QThread.currentThread())
        return [
            _Candidate(
                instance="ADS131M04-WebServer",
                hostname="echos.local",
                address="192.0.2.10",
                http_port=80,
                board="ESP32-S3",
            )
        ]

    def factory(address: str, http_port: int) -> EchosApiClient:
        return EchosApiClient(
            address, http_port, transport=fw.transport, get_retries=0, retry_delay_s=0.0
        )

    worker = EchosDiscoveryWorker(client_factory=factory, browse=browse)
    store = ConfigStore(
        RootConfig(app=AppConfig(), ui=UiConfig(), devices=[]),
        tmp_path / "config.yaml",
    )
    wizard = FirstRunWizard(store=store, discovery_worker=worker)
    qtbot.addWidget(wizard)
    wizard.restart()
    try:
        wizard.next()  # → Find page: auto-scan dispatches (queued)
        qtbot.waitUntil(lambda: wizard._find.selected_device() is not None, timeout=8000)
        device = wizard._find.selected_device()
        assert device is not None
        assert device.hostname == "echos.local"
        assert device.seedlink_port == 18000  # probed via the REAL client
        assert device.channels  # StationXML parsed on the worker
        assert browse_threads == [wizard._thread]
        assert wizard._thread is not QThread.currentThread()
    finally:
        wizard.done(0)
    assert not wizard._thread.isRunning()
