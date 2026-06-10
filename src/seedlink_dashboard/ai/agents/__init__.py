"""Concrete AI agents + the engagement registry (M9).

The registry is the single extension point the engagement UI reads: to add
a new agent type (a classifier, an anomaly detector) you register a factory
here and it appears in the "Engage agent…" dialog — no change to
:class:`~seedlink_dashboard.core.ai_engine.AIEngine`.

Factories are imported lazily so this module — and the app — load without
the optional ``ai`` extra (torch / seisbench). ``available_agents`` reports
which registered agents can actually run in the current environment.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from seedlink_dashboard.ai.base import AIAgent

# Registry of agent factories keyed by a stable id. Each factory takes the
# engagement parameters (model/weights/thresholds/device) as kwargs and
# returns a constructed (but not yet warmed) AIAgent.
AgentFactory = Callable[..., "AIAgent"]


def _construct(cls: type, kwargs: dict[str, object]) -> AIAgent:
    """Construct ``cls`` passing only the kwargs its ``__init__`` accepts.

    ``main_window`` calls every factory uniformly with the union of the
    selected agent's :meth:`~seedlink_dashboard.ai.base.AIAgent.engage_params`
    values plus ``device=``; agents with narrower signatures (the heuristic
    classifier takes no ``device``) must not crash on the extras. Filtering
    via :func:`inspect.signature` keeps construction robust without per-agent
    branching in the caller. Torch-free: reading ``__init__``'s signature
    never imports torch.
    """
    import inspect

    accepted = set(inspect.signature(cls).parameters)
    filtered = {k: v for k, v in kwargs.items() if k in accepted}
    return cls(**filtered)  # type: ignore[no-any-return]


def _make_seisbench_picker(**kwargs: object) -> AIAgent:
    from seedlink_dashboard.ai.agents.seisbench_picker import SeisBenchPicker

    return _construct(SeisBenchPicker, kwargs)


def _make_seisbench_detector(**kwargs: object) -> AIAgent:
    from seedlink_dashboard.ai.agents.seisbench_detector import SeisBenchDetector

    return _construct(SeisBenchDetector, kwargs)


def _make_heuristic_classifier(**kwargs: object) -> AIAgent:
    from seedlink_dashboard.ai.agents.heuristic_classifier import HeuristicClassifier

    return _construct(HeuristicClassifier, kwargs)


def _make_autoencoder_anomaly(**kwargs: object) -> AIAgent:
    from seedlink_dashboard.ai.agents.autoencoder_anomaly import AutoencoderAnomaly

    return _construct(AutoencoderAnomaly, kwargs)


AGENTS: dict[str, AgentFactory] = {
    "seisbench_picker": _make_seisbench_picker,
    "seisbench_detector": _make_seisbench_detector,
    # M10 Stage C — the two learning agents (fit-then-infer).
    "heuristic_classifier": _make_heuristic_classifier,
    "autoencoder_anomaly": _make_autoencoder_anomaly,
}


def seisbench_available() -> bool:
    """True if the ``ai`` extra (seisbench + torch) is importable."""
    import importlib.util

    return (
        importlib.util.find_spec("seisbench") is not None
        and importlib.util.find_spec("torch") is not None
    )


def torch_available() -> bool:
    """True if ``torch`` alone is importable (the autoencoder's only need).

    The autoencoder needs torch but NOT seisbench, so it gates on this rather
    than the heavier :func:`seisbench_available`.
    """
    import importlib.util

    return importlib.util.find_spec("torch") is not None


def available_agents() -> dict[str, bool]:
    """Map each registered agent id to whether it can run right now."""
    sb = seisbench_available()
    return {
        "seisbench_picker": sb,
        "seisbench_detector": sb,
        # The heuristic classifier is torch-free → always available.
        "heuristic_classifier": True,
        # The autoencoder needs torch (not seisbench).
        "autoencoder_anomaly": torch_available(),
    }
