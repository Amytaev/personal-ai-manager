from __future__ import annotations

from agents.sso.parser import (
    build_umkd_path_by_folder_id,
    iter_umkd_leaf_folders,
    parse_courses,
    parse_materials,
    parse_schedule,
    pick_current_semester_id,
)


def test_pick_current_semester_id_prefers_the_flagged_one():
    # Real shape confirmed by scripts/sso_capture_data.py's Этап A capture
    # (2026-09-27): a single semester flagged isCurrentSemester: 1.
    semesters = [{"id": 85, "title": "Осень 2026-27", "isCurrentSemester": 1}]
    assert pick_current_semester_id(semesters) == 85


def test_pick_current_semester_id_falls_back_to_first_when_none_flagged():
    semesters = [{"id": 80, "title": "Old", "isCurrentSemester": 0}, {"id": 85, "title": "New"}]
    assert pick_current_semester_id(semesters) == 80


def test_pick_current_semester_id_returns_none_for_empty_list():
    assert pick_current_semester_id([]) is None


def test_parse_courses_extracts_real_shaped_fields():
    # Trimmed to the fields agents/sso/parser.py actually reads - full
    # shape confirmed against sso_capture/responses.jsonl's real
    # GetDesciplines response.
    raw = [
        {
            "code": "CSE5472",
            "title": "Основы научно-исследовательской работы студентов",
            "disciplineTypeTitle": "Обязательный",
            "cycleTitle": "Профильный",
            "lectureCredits": 1,
            "practiceCredits": 2,
            "labCredits": 0,
            "totalCredits": 3,
            "readingChairTitle": "Информационные системы",
            "description": "Курс нацелен на...",
        }
    ]
    courses = parse_courses(raw)
    assert len(courses) == 1
    assert courses[0]["code"] == "CSE5472"
    assert courses[0]["total_credits"] == 3
    assert courses[0]["reading_chair_title"] == "Информационные системы"


def test_parse_courses_skips_entries_missing_code_or_title():
    raw = [{"code": None, "title": "X"}, {"code": "Y", "title": None}, {"code": "Z", "title": "Zeta"}]
    courses = parse_courses(raw)
    assert [c["code"] for c in courses] == ["Z"]


def test_parse_courses_returns_empty_list_for_none_or_empty():
    assert parse_courses(None) == []
    assert parse_courses([]) == []


def _schedule_table_fixture() -> dict:
    # Trimmed real shape (one real lesson, one empty slot) - confirmed
    # against sso_capture/responses.jsonl's real GetTable response: the
    # CONTAINER's own "time" carries real title strings, the LESSON's
    # nested "time" has null titles in every real lesson captured.
    return {
        "columns": [
            {
                "title": "MONDAY_SHORT",
                "containers": [
                    {
                        "time": {"start": {"title": "8:55 "}, "end": {"title": "9:45"}},
                        "lessons": [
                            {
                                "classId": 222374,
                                "courseCode": "CSE5472",
                                "courseTitle": "Основы научно-исследовательской работы студентов",
                                "instructorName": "Сербин В.В.",
                                "roomTitle": "on line2",
                                "classType": 3,
                                "groupNumber": 2,
                                "studentsCount": 100,
                                "time": {"start": {"title": None}, "end": {"title": None}},
                            }
                        ],
                    },
                    {
                        "time": {"start": {"title": "10:00"}, "end": {"title": "10:50"}},
                        "lessons": [],
                    },
                ],
            }
        ]
    }


def test_parse_schedule_extracts_one_entry_per_real_lesson():
    entries = parse_schedule(_schedule_table_fixture())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["class_id"] == 222374
    assert entry["course_code"] == "CSE5472"
    assert entry["day_title"] == "MONDAY_SHORT"
    # Read from the CONTAINER's time, not the lesson's own (null in every
    # real capture) - trailing whitespace from the real "8:55 " value is
    # stripped.
    assert entry["start_time"] == "8:55"
    assert entry["end_time"] == "9:45"


def test_parse_schedule_skips_empty_time_slots():
    entries = parse_schedule(_schedule_table_fixture())
    # Only the slot with a real lesson produces a row - the empty 10:00
    # slot contributes nothing.
    assert all(e["start_time"] != "10:00" for e in entries)


def test_parse_schedule_returns_empty_list_for_none_or_empty():
    assert parse_schedule(None) == []
    assert parse_schedule({}) == []
    assert parse_schedule({"columns": []}) == []


def _umkd_tree_fixture() -> list[dict]:
    # Trimmed real shape confirmed against sso_capture/responses.jsonl's
    # real GetFoldersForStudent response: course node -> instructor
    # node -> leaf category folder(s), grouping nodes carry id -1.
    return [
        {
            "id": -1,
            "title": "CSE4112 Администрирование систем и сетей",
            "nodes": [
                {
                    "id": -1,
                    "title": "Майлыбаев Ерсайын Құрманбайұлы",
                    "nodes": [
                        {"id": 398932, "title": "УМКД 2024-2025", "nodes": []},
                        {"id": 506481, "title": "УМКД 2025-2026", "nodes": []},
                    ],
                }
            ],
        },
        {
            "id": -1,
            "title": "CSE5472 Основы научно-исследовательской работы студентов",
            "nodes": [
                {
                    "id": -1,
                    "title": "Абдуллаева Асель Сейдуллаевна",
                    "nodes": [
                        {"id": 490059, "title": "Практика", "nodes": []},
                    ],
                }
            ],
        },
    ]


def test_iter_umkd_leaf_folders_finds_every_real_leaf():
    leaves = iter_umkd_leaf_folders(_umkd_tree_fixture())
    assert {leaf["folder_id"] for leaf in leaves} == {398932, 506481, 490059}


def test_iter_umkd_leaf_folders_annotates_the_full_ancestor_path():
    leaves = iter_umkd_leaf_folders(_umkd_tree_fixture())
    by_id = {leaf["folder_id"]: leaf for leaf in leaves}
    assert by_id[398932]["path"] == [
        "CSE4112 Администрирование систем и сетей",
        "Майлыбаев Ерсайын Құрманбайұлы",
        "УМКД 2024-2025",
    ]


def test_iter_umkd_leaf_folders_skips_leaves_with_no_real_id():
    tree = [{"id": -1, "title": "Course", "nodes": [{"id": -1, "title": "Empty group", "nodes": []}]}]
    assert iter_umkd_leaf_folders(tree) == []


def test_iter_umkd_leaf_folders_returns_empty_list_for_none_or_empty():
    assert iter_umkd_leaf_folders(None) == []
    assert iter_umkd_leaf_folders([]) == []


def test_build_umkd_path_by_folder_id_matches_iter_output():
    leaves = iter_umkd_leaf_folders(_umkd_tree_fixture())
    lookup = build_umkd_path_by_folder_id(leaves)
    assert lookup[490059] == [
        "CSE5472 Основы научно-исследовательской работы студентов",
        "Абдуллаева Асель Сейдуллаевна",
        "Практика",
    ]


def test_parse_materials_extracts_real_shaped_files_with_path_context():
    # Trimmed real shape confirmed against sso_capture/responses.jsonl's
    # real GetFolderContent response - fileData/clientFileData/createdDate
    # are always null in every real capture and are dropped either way.
    raw_files = [
        {
            "fileId": 398933,
            "fileName": "Лекция 1.docx",
            "fileCategoryId": 10,
            "fileCategoryTitle": "Лекции",
            "link": None,
            "folderId": 398932,
            "fileData": None,
            "createdDate": None,
            "clientFileData": None,
        }
    ]
    materials = parse_materials(
        398932,
        raw_files,
        path=["CSE4112 Администрирование систем и сетей", "Майлыбаев Ерсайын Құрманбайұлы", "УМКД 2024-2025"],
    )
    assert len(materials) == 1
    material = materials[0]
    assert material["file_id"] == 398933
    assert material["file_name"] == "Лекция 1.docx"
    assert material["file_category_title"] == "Лекции"
    assert material["course_title"] == "CSE4112 Администрирование систем и сетей"
    assert material["instructor_name"] == "Майлыбаев Ерсайын Құрманбайұлы"
    # Never stores the raw file payload/metadata beyond name+category -
    # no fileData/clientFileData/link key at all.
    assert "fileData" not in material
    assert "clientFileData" not in material
    assert "link" not in material


def test_parse_materials_skips_entries_missing_id_or_name():
    raw_files = [{"fileId": None, "fileName": "X"}, {"fileId": 1, "fileName": None}, {"fileId": 2, "fileName": "Y"}]
    materials = parse_materials(1, raw_files)
    assert [m["file_id"] for m in materials] == [2]


def test_parse_materials_returns_empty_list_for_none_or_empty():
    assert parse_materials(1, None) == []
    assert parse_materials(1, []) == []


def test_parse_materials_without_path_leaves_context_fields_none():
    materials = parse_materials(1, [{"fileId": 1, "fileName": "A.pdf"}])
    assert materials[0]["course_title"] is None
    assert materials[0]["instructor_name"] is None
