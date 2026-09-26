"""Parse stud.satbayev.university / api.satbayev.university's real JSON
responses into normalized Course / ScheduleEntry / StudyMaterial rows
(Study Manager shape, per bro's ТЗ).

Written against REAL captured data, not a guessed shape -
scripts/sso_capture_data.py captured a real logged-in session's network
traffic (sso_capture/responses.jsonl, 2026-09-27) before this file was
written. The headline finding from that capture: unlike VALORANT's Stack
B (agents/valorant/parser.py's docstring - HTML-only, no JSON API) or
Teams' work API (agents/teams/agent.py's docstring - Bearer-token-only,
UI-driven), the SSO/student portal exposes a clean, versioned-looking
internal JSON API for everything this agent needs - no HTML parsing
anywhere in this module.

Confirmed real endpoints/shapes (see sso_capture/responses.jsonl for the
full capture):

- GET stud.satbayev.university/api/ScheduleTable/GetDesciplines
  ?semesterId=<id> -> list of discipline dicts (code, title,
  lectureCredits/practiceCredits/labCredits/totalCredits,
  readingChairTitle, description, disciplineTypeTitle, cycleTitle).
- GET stud.satbayev.university/api/ScheduleTable/GetTable
  ?semesterId=<id> -> {"columns": [{"title": "MONDAY_SHORT", ...,
  "containers": [{"time": {...}, "lessons": [...]}]}]}. Each container's
  OWN "time" field carries the real "8:55 "/"9:45"-style titles - a
  lesson's own nested "time" field has null titles in every real lesson
  captured, so this module reads the container's time, not the lesson's.
- GET api.satbayev.university/api/Umkd/GetFoldersForStudent -> a
  recursive tree: course node (title like "CSE4112 Администрирование
  систем и сетей") -> instructor node(s) -> leaf category folder(s)
  (title like "Лекции"/"Практика"/"Силлабус", real folder id). A node
  with an empty "nodes" list is a real, fetchable leaf folder; anything
  else is just a grouping label.
- GET api.satbayev.university/api/Umkd/GetFolderContent?folderId=<id>
  -> list of file dicts (fileId, fileName, fileCategoryTitle).

WHAT THIS MODULE DELIBERATELY DOES NOT DO, per explicit instruction
(2026-09-27):
- Never calls/parses api.satbayev.university/api/Umkd/Download - only
  file METADATA (name/category/id) is normalized here. Actually
  downloading the binary file is out of scope for this stage.
- Never touches api.satbayev.university/api/user/getuserinfo at all.
  That endpoint's real response (seen in the same capture) carries
  "iin" (the student's Kazakhstani national ID number) and "dob" (date
  of birth) - real, sensitive PII this project has no use for and must
  never store or log. Since SsoAgent has no reason to call that endpoint
  in the first place, there's no field to filter out here - it's simply
  never fetched.
"""
from __future__ import annotations

from typing import Any


def pick_current_semester_id(raw_semesters: list[dict]) -> int | None:
    """GetCurrentAndAvailableSemesters returns a list like
    ``[{"id": 85, "title": "Осень 2026-27", "isCurrentSemester": 1}]`` -
    picks the one flagged current, falling back to the first entry if
    none is flagged (defensive; every real capture so far has exactly
    one, flagged), or None if the list is empty."""
    if not raw_semesters:
        return None
    for semester in raw_semesters:
        if semester.get("isCurrentSemester"):
            return semester.get("id")
    return raw_semesters[0].get("id")


def parse_courses(raw_disciplines: list[dict]) -> list[dict]:
    """GetDesciplines's response -> normalized Course rows. Field names
    are ours (not a straight rename of the API's own casing), matching
    the Course model named in bro's ТЗ."""
    courses: list[dict] = []
    for raw in raw_disciplines or []:
        code = raw.get("code")
        title = raw.get("title")
        if not code or not title:
            # A real discipline always has both in every capture seen so
            # far - skip defensively rather than store a half-identified
            # row, same "skip the odd one out, don't crash the run"
            # discipline as agents/valorant/parser.py.
            continue
        courses.append(
            {
                "code": code,
                "title": title,
                "discipline_type_title": raw.get("disciplineTypeTitle"),
                "cycle_title": raw.get("cycleTitle"),
                "lecture_credits": raw.get("lectureCredits"),
                "practice_credits": raw.get("practiceCredits"),
                "lab_credits": raw.get("labCredits"),
                "total_credits": raw.get("totalCredits"),
                "reading_chair_title": raw.get("readingChairTitle"),
                "description": raw.get("description"),
            }
        )
    return courses


def parse_schedule(raw_table: dict | None) -> list[dict]:
    """GetTable's response -> normalized ScheduleEntry rows, one per
    real lesson (a lesson-less time slot is skipped - most of a week's
    grid is empty). See this module's docstring for why the CONTAINER's
    "time" is used for start/end instead of the lesson's own (null in
    every real capture)."""
    if not raw_table:
        return []

    entries: list[dict] = []
    for column in raw_table.get("columns") or []:
        day_title = column.get("title")
        for container in column.get("containers") or []:
            time = container.get("time") or {}
            start_title = ((time.get("start") or {}).get("title") or "").strip() or None
            end_title = ((time.get("end") or {}).get("title") or "").strip() or None
            for lesson in container.get("lessons") or []:
                entries.append(
                    {
                        "class_id": lesson.get("classId"),
                        "course_code": lesson.get("courseCode"),
                        "course_title": lesson.get("courseTitle"),
                        "instructor_name": lesson.get("instructorName"),
                        "room_title": lesson.get("roomTitle"),
                        "class_type": lesson.get("classType"),
                        "day_title": day_title,
                        "start_time": start_title,
                        "end_time": end_title,
                        "group_number": lesson.get("groupNumber"),
                        "students_count": lesson.get("studentsCount"),
                    }
                )
    return entries


def _is_leaf(node: dict) -> bool:
    nodes = node.get("nodes")
    return not nodes


def iter_umkd_leaf_folders(raw_tree: list[dict]) -> list[dict]:
    """Walks GetFoldersForStudent's recursive course -> instructor ->
    category tree and returns every real, fetchable leaf folder (a node
    with no children of its own), each annotated with its ancestor path
    so a StudyMaterial row can carry course/instructor context without
    a second lookup. ``path`` is the list of ancestor node titles, e.g.
    ["CSE4112 Администрирование систем и сетей", "Майлыбаев Ерсайын
    Құрманбайұлы"] for a leaf titled "УМКД 2026-2027" - real shape
    confirmed in sso_capture/responses.jsonl.

    A node with an id of -1 is just a grouping label in every real
    capture seen (course/instructor nodes), never itself a real folder
    to call GetFolderContent on - only genuine leaves (their own real,
    positive-looking id and no children) are returned here."""
    leaves: list[dict] = []

    def _walk(nodes: list[dict], path: list[str]) -> None:
        for node in nodes or []:
            title = node.get("title")
            node_path = path + [title] if title else path
            if _is_leaf(node):
                folder_id = node.get("id")
                if folder_id is None or folder_id == -1:
                    # A leaf with no real id has nothing to fetch -
                    # skip rather than call GetFolderContent with a
                    # meaningless id.
                    continue
                leaves.append({"folder_id": folder_id, "path": node_path})
            else:
                _walk(node.get("nodes") or [], node_path)

    _walk(raw_tree or [], [])
    return leaves


def parse_materials(folder_id: int, raw_files: list[dict], path: list[str] | None = None) -> list[dict]:
    """GetFolderContent's response for one leaf folder -> normalized
    StudyMaterial rows. ``path`` (from iter_umkd_leaf_folders) is
    threaded through as course_title/instructor_name context, since the
    file listing itself carries no course/teacher info of its own.

    Deliberately does NOT call Umkd/Download or store any file content -
    see this module's docstring. ``fileData``/``clientFileData`` (always
    null in every real capture) are dropped rather than stored either
    way."""
    path = path or []
    course_title = path[0] if len(path) > 0 else None
    instructor_name = path[1] if len(path) > 1 else None

    materials: list[dict] = []
    for raw in raw_files or []:
        file_id = raw.get("fileId")
        file_name = raw.get("fileName")
        if file_id is None or not file_name:
            continue
        materials.append(
            {
                "file_id": file_id,
                "folder_id": folder_id,
                "file_name": file_name,
                "file_category_title": raw.get("fileCategoryTitle"),
                "course_title": course_title,
                "instructor_name": instructor_name,
            }
        )
    return materials


def build_umkd_path_by_folder_id(leaf_folders: list[dict]) -> dict[Any, list[str]]:
    """Convenience lookup used by SsoAgent: folder_id -> its ancestor
    path, so each GetFolderContent call's result can be parsed with the
    right course/instructor context without re-walking the tree."""
    return {leaf["folder_id"]: leaf["path"] for leaf in leaf_folders}
