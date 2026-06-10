"""Typed configuration schema and loader."""

from __future__ import annotations

from seedlink_dashboard.config.loader import load_config
from seedlink_dashboard.config.schema import (
    AiConfig,
    AppConfig,
    ArchiveConfig,
    BandpassStage,
    DecimationStage,
    DetrendStage,
    DeviceConfig,
    DspStageConfig,
    HighpassStage,
    LowpassStage,
    NotchStage,
    ReconnectConfig,
    RootConfig,
    StaLtaStage,
    StreamSelectorConfig,
    TaperStage,
    UiConfig,
)

__all__ = [
    "AiConfig",
    "AppConfig",
    "ArchiveConfig",
    "BandpassStage",
    "DecimationStage",
    "DetrendStage",
    "DeviceConfig",
    "DspStageConfig",
    "HighpassStage",
    "LowpassStage",
    "NotchStage",
    "ReconnectConfig",
    "RootConfig",
    "StaLtaStage",
    "StreamSelectorConfig",
    "TaperStage",
    "UiConfig",
    "load_config",
]
