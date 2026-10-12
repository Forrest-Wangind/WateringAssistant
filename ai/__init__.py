"""AI 分析层：可插拔多模型 provider + 植物生长报告。"""
from ai.errors import AIError
from ai.plant_analyzer import AnalysisReport, PlantAnalyzer
from ai.providers import (
    PRESETS,
    AnthropicProvider,
    BaseProvider,
    MockProvider,
    OpenAICompatProvider,
    UnavailableProvider,
    build_provider,
    resolve_settings,
)

__all__ = [
    "AIError",
    "AnalysisReport",
    "PlantAnalyzer",
    "PRESETS",
    "AnthropicProvider",
    "BaseProvider",
    "MockProvider",
    "OpenAICompatProvider",
    "UnavailableProvider",
    "build_provider",
    "resolve_settings",
]
