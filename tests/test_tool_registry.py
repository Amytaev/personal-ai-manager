from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agents.study_manager.agent import StudyManager
from tools.base import Tool, ToolRegistry, ToolResult
from tools.registry import build_default_registry

_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ===========================================================================
# ToolRegistry core - allowlist enforcement, error boundary (spec §16/§46/§27)
# ===========================================================================


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error_not_an_exception():
    registry = ToolRegistry()
    result = await registry.execute("does_not_exist", {})
    assert result.status == "ERROR"
    assert result.error["code"] == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_execute_rejects_an_argument_not_in_the_schema():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="echoes",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=lambda text: text,
        )
    )
    result = await registry.execute("echo", {"text": "hi", "field": "some_internal_database_column"})
    assert result.status == "ERROR"
    assert result.error["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_execute_rejects_missing_required_argument():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="echoes",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=lambda text: text,
        )
    )
    result = await registry.execute("echo", {})
    assert result.status == "ERROR"
    assert result.error["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_execute_supports_sync_and_async_handlers():
    registry = ToolRegistry()
    registry.register(
        Tool(name="sync_one", description="d", parameters={"type": "object", "properties": {}, "required": []}, handler=lambda: 1)
    )

    async def _async_two():
        return 2

    registry.register(
        Tool(name="async_two", description="d", parameters={"type": "object", "properties": {}, "required": []}, handler=_async_two)
    )
    assert (await registry.execute("sync_one")).data == 1
    assert (await registry.execute("async_two")).data == 2


@pytest.mark.asyncio
async def test_execute_turns_a_handler_exception_into_an_error_result_never_raises():
    def _boom():
        raise RuntimeError("network is down")

    registry = ToolRegistry()
    registry.register(
        Tool(name="boom", description="d", parameters={"type": "object", "properties": {}, "required": []}, handler=_boom)
    )
    result = await registry.execute("boom")
    assert result.status == "ERROR"
    assert result.error["code"] == "RuntimeError"
    assert "network is down" in result.error["message"]


def test_register_rejects_a_duplicate_tool_name():
    registry = ToolRegistry()
    tool = Tool(name="dup", description="d", parameters={"type": "object", "properties": {}, "required": []}, handler=lambda: None)
    registry.register(tool)
    with pytest.raises(ValueError):
        registry.register(tool)


def test_tool_result_to_dict_matches_spec_27_wire_shape():
    ok = ToolResult.ok({"a": 1})
    assert ok.to_dict() == {"status": "OK", "data": {"a": 1}, "error": None}
    err = ToolResult.fail("SOURCE_UNAVAILABLE", "Teams is unavailable")
    assert err.to_dict() == {
        "status": "ERROR",
        "data": None,
        "error": {"code": "SOURCE_UNAVAILABLE", "message": "Teams is unavailable"},
    }


def test_anthropic_tools_builds_the_name_description_input_schema_shape():
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="get_x",
            description="Returns X.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: None,
        )
    )
    schemas = registry.anthropic_tools()
    assert schemas == [{"name": "get_x", "description": "Returns X.", "input_schema": {"type": "object", "properties": {}, "required": []}}]


# ===========================================================================
# Default registry - real Study/Weather/VALORANT tools (spec §24-26)
# ===========================================================================


def test_build_default_registry_registers_every_expected_tool(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    expected = {
        "get_dashboard", "get_upcoming_schedule", "get_upcoming_tasks", "get_overdue_tasks",
        "get_course_tasks", "get_course_materials", "get_checker_findings", "get_source_status",
        "refresh_study_data", "set_task_override", "set_activity_override", "clear_override",
        "get_overrides", "get_effective_task", "get_weather", "get_valorant_store",
    }
    assert expected.issubset(set(registry.names()))


@pytest.mark.asyncio
async def test_get_dashboard_tool_calls_through_to_study_manager(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_dashboard")
    assert result.status == "OK"
    assert "status" in result.data
    assert "source_status" in result.data


@pytest.mark.asyncio
async def test_set_task_override_tool_rejects_a_disallowed_field(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute(
        "set_task_override",
        {"task_id": "t1", "field": "some_internal_database_column", "value": "x"},
    )
    assert result.status == "ERROR"
    assert result.error["code"] == "OverrideValidationError"
    assert tmp_db.get_overrides() == []


@pytest.mark.asyncio
async def test_set_task_override_tool_stores_a_valid_override(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute(
        "set_task_override", {"task_id": "t1", "field": "academic_week", "value": 5}
    )
    assert result.status == "OK"
    assert sm.get_overrides()[0]["value"] == 5


@pytest.mark.asyncio
async def test_get_course_tasks_tool_reports_not_found_without_guessing(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_course_tasks", {"course_code": "XYZ9999"})
    assert result.status == "OK"
    assert result.data["match"] == "NOT_FOUND"
    assert result.data["tasks"] == []


@pytest.mark.asyncio
async def test_get_effective_task_tool_reports_not_found_for_unknown_task(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_effective_task", {"task_id": "does-not-exist"})
    assert result.status == "ERROR"
    assert result.error["code"] == "ToolNotFoundError"


@pytest.mark.asyncio
async def test_get_weather_tool_returns_not_found_before_any_snapshot(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_weather")
    assert result.status == "ERROR"
    assert result.error["code"] == "ToolNotFoundError"


@pytest.mark.asyncio
async def test_get_weather_tool_returns_the_latest_snapshot_with_freshness(tmp_db):
    tmp_db.record_agent_run("weather", "working", _iso(_NOW), _iso(_NOW), None)
    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO weather_snapshots (location, payload_json, created_at) VALUES (?, ?, ?)",
            ("Алматы", json.dumps({"location": "Алматы", "current": {"temperature": 20}}), _iso(_NOW)),
        )
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_weather")
    assert result.status == "OK"
    assert result.data["location"] == "Алматы"
    assert result.data["checked_at"] == _iso(_NOW)
    assert result.data["last_run_status"] == "working"


@pytest.mark.asyncio
async def test_get_valorant_store_tool_returns_not_found_before_any_snapshot(tmp_db):
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_valorant_store")
    assert result.status == "ERROR"
    assert result.error["code"] == "ToolNotFoundError"


@pytest.mark.asyncio
async def test_get_valorant_store_tool_never_reports_empty_items_as_a_real_empty_store(tmp_db):
    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO valorant_store (skins_json, checked_at) VALUES (?, ?)",
            (json.dumps({"items": [], "reset_in": None}), _iso(_NOW)),
        )
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_valorant_store")
    assert result.status == "ERROR"
    assert result.error["code"] == "StoreUnavailableError"


@pytest.mark.asyncio
async def test_get_valorant_store_tool_returns_real_items(tmp_db):
    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO valorant_store (skins_json, checked_at) VALUES (?, ?)",
            (json.dumps({"items": [{"uuid": "a", "name": "Skin", "price_vp": 1275}], "reset_in": "5 часов"}), _iso(_NOW)),
        )
    sm = StudyManager(db=tmp_db)
    registry = build_default_registry(tmp_db, sm)
    result = await registry.execute("get_valorant_store")
    assert result.status == "OK"
    assert result.data["items"][0]["name"] == "Skin"
    assert result.data["reset_in"] == "5 часов"
