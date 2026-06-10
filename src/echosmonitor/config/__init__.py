"""Typed configuration schema and loader."""

from __future__ import annotations

from echosmonitor.config.loader import load_config
from echosmonitor.config.schema import (
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
