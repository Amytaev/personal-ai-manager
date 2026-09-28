"""Builds the default ToolRegistry (AI Manager ТЗ v3 §10/§24-26) - the
one place every tool group (Study/Weather/VALORANT) is assembled into
a single registry for AIManager to use. Kept separate from tools/base.py
so ToolRegistry's own definition never has to import a concrete Agent/
StudyManager itself.
"""
from __future__ import annotations

from agents.study_manager.agent import StudyManager
from storage.database import Database
from tools.base import ToolRegistry
from tools.study import build_study_tools
from tools.valorant import build_valorant_tools
from tools.weather import build_weather_tools


def build_default_registry(db: Database, study_manager: StudyManager) -> ToolRegistry:
    """``db`` backs the Weather/VALORANT tools directly (they only ever
    read already-collected snapshot rows, spec §27/§84); ``study_manager``
    backs every Study Manager tool through its own public interface
    (spec §11). Both should be the SAME instances run.py already
    constructed and registered on the scheduler - this function never
    constructs its own copies."""
    registry = ToolRegistry()
    for tool in build_study_tools(study_manager):
        registry.register(tool)
    for tool in build_weather_tools(db):
        registry.register(tool)
    for tool in build_valorant_tools(db):
        registry.register(tool)
    return registry
