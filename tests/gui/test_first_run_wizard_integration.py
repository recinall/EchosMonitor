"""Real-thread integration test for :class:`FirstRunWizard` (M4 stage C).

The unit-level wizard tests stub the :class:`InfoWorker`, which side-
steps the cross-thread dispatch path that the production wizard uses.
Stage C's first draft called ``self._info_worker.requestId(...)``
directly on the GUI thread, blocking it for up to 30 s on an
unreachable host — code-reviewer caught the regression. The fix
routes the request through an internal ``_idRequested`` signal
connected to the worker's slot via ``Qt.ConnectionType.QueuedConnection``.

This test pins the contract end-to-end:

* A real :class:`InfoWorker` lives on a real ``QThread``.
* A real :class:`FakeSeedLinkServer` answers ``INFO ID``.
* The wizard's welcome page fires the queued-connection probe.
* We assert ``identityReceived`` actually fires for one of the two
  request_ids — proving the GUI thread did NOT block waiting for
  the slot to run synchronously.

A regression to direct-call dispatch would either time out
(``waitSignal`` deadline) or — worse — silently freeze the test
thread inside the slot body for up to 30 s.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QThread

from echosmonitor.config.schema import (
    AppConfig,
    RootConfig,
    UiConfig,
)
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.info_worker import InfoWorker
from echosmonitor.gui.dialogs.first_run_wizard import FirstRunWizard
from tests.core.fakes import (
    FakeSeedLinkServer,
    FakeSeedLinkServerConfig,
    FakeStation,
)
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401


@pytest.fixture
def make_fake_server(
    loop_thread: _LoopThread,  # noqa: F811
) -> Iterator[Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer]]:
    started: list[FakeSeedLinkServer] = []

    def _factory(cfg: FakeSeedLinkServerConfig) -> FakeSeedLinkServer:
        server = FakeSeedLinkServer(config=cfg)
        loop_thread.submit(server.start()).result(timeout=2.0)
        started.append(server)
        return server

    yield _factory

    for server in started:
        with contextlib.suppress(Exception):
            loop_thread.submit(server.stop()).result(timeout=3.0)


def test_wizard_probe_dispatches_through_real_worker_thread(
    qtbot: Any,
    tmp_path: Path,
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """The welcome page's probe runs on the InfoWorker thread, not the GUI thread.

    A direct ``requestId`` call from the GUI thread would block the
    test thread inside ``info.fetch`` for the full ``timeout_s = 30``;
    the queued-connection emit completes immediately and the actual
    fetch runs on the worker thread. We pin this by:

    * spinning up a real :class:`InfoWorker` on a real :class:`QThread`,
    * pointing both the GFZ and IRIS recommended hosts at the SAME
      fake server (the only INFO-capable test server we have),
    * showing the wizard so its welcome page fires the probe,
    * asserting ``identityReceived`` actually arrives — which is only
      possible if the slot dispatched on the worker thread and got a
      reply from the real (fake) server.
    """
    cfg = FakeSeedLinkServerConfig(
        stations=(FakeStation(network="GE", station="WLF", description="Wittensee"),),
    )
    server = make_fake_server(cfg)

    # Override the wizard's recommended-server constants so both
    # probes target our local fake. Patching the module-level
    # constants is cleaner than redefining them: a single import
    # path is the canonical source.
    import echosmonitor.gui.dialogs.first_run_wizard as wizard_mod

    original_gfz = wizard_mod._RECOMMENDED_GFZ
    original_iris = wizard_mod._RECOMMENDED_IRIS
    wizard_mod._RECOMMENDED_GFZ = (
        "fake-a",
        server.host,
        server.port,
        "GE",
        "WLF",
        "",
        "BHZ",
        "Fake A",
    )
    wizard_mod._RECOMMENDED_IRIS = (
        "fake-b",
        server.host,
        server.port,
        "GE",
        "WLF",
        "",
        "BHZ",
        "Fake B",
    )

    try:
        # Real InfoWorker on a real QThread.
        worker_thread = QThread()
        worker_thread.setObjectName("info-worker-test")
        worker = InfoWorker()
        worker.moveToThread(worker_thread)
        worker_thread.start()

        store_path = tmp_path / "config.yaml"
        store = ConfigStore(
            RootConfig(app=AppConfig(), ui=UiConfig(), devices=[]),
            store_path,
        )

        try:
            with qtbot.waitSignal(worker.identityReceived, timeout=10000) as blocker:
                wizard = FirstRunWizard(store=store, info_worker=worker)
                qtbot.addWidget(wizard)
                wizard.show()
                qtbot.waitExposed(wizard)
                # Do nothing else — the probe was kicked off in
                # initializePage and the queued emit has dispatched.
                # waitSignal blocks until the reply arrives.

            request_id, label, _identity = blocker.args
            # Either GFZ or IRIS request_id — both target the same fake.
            welcome = wizard.page(0)
            assert request_id in {welcome._gfz_request_id, welcome._iris_request_id}, (  # type: ignore[attr-defined]
                "identityReceived fired for an unrelated request_id"
            )
            assert label == f"{server.host}:{server.port}"
        finally:
            worker.stop()
            worker_thread.quit()
            assert worker_thread.wait(2000), "info worker thread did not join"
    finally:
        # Restore the constants so subsequent tests aren't affected.
        wizard_mod._RECOMMENDED_GFZ = original_gfz
        wizard_mod._RECOMMENDED_IRIS = original_iris
