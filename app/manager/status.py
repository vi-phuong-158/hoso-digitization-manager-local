from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any


CHECKLIST_STATUSES = {"CO_TAI_LIEU", "KHONG_PHAT_SINH", "CHUA_XAC_DINH", "CAN_BO_SUNG"}
CASE_STATUSES = {"CHUA_XU_LY", "DANG_SO_HOA", "CHO_KIEM_TRA", "CAN_BO_SUNG", "HOAN_THANH"}
COMPLETED_CHECKLIST = {"CO_TAI_LIEU", "KHONG_PHAT_SINH"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def weight(priority: int | None) -> int:
    if priority == 1:
        return 3
    if priority == 2:
        return 2
    return 1


def checklist_for_case(conn, case_id: int) -> list[dict[str, Any]]:
    docs = {
        row["taxonomy_code"]: row["n"]
        for row in conn.execute(
            """SELECT taxonomy_code,COUNT(*) AS n FROM documents
               WHERE case_id=? AND is_present=1 AND parse_status='OK' AND taxonomy_code IS NOT NULL
               GROUP BY taxonomy_code""",
            (case_id,),
        ).fetchall()
    }
    # Existing pipeline manifests describe logical documents even when the
    # source PDF filename is intentionally opaque. Use validated taxonomy
    # entries as evidence, while retaining the filesystem fallback.
    try:
        pipeline_docs = {
            row["type_id"]: row["n"]
            for row in conn.execute(
                """SELECT type_id,COUNT(*) AS n FROM pipeline_documents
                   WHERE case_id=? AND type_id IS NOT NULL GROUP BY type_id""",
                (case_id,),
            ).fetchall()
        }
    except sqlite3.OperationalError:
        pipeline_docs = {}
    docs = {code: max(count, pipeline_docs.get(code, 0)) for code, count in docs.items()}
    for code, count in pipeline_docs.items():
        docs.setdefault(code, count)
    overrides = {
        row["taxonomy_code"]: row["status"]
        for row in conn.execute("SELECT taxonomy_code,status FROM checklist_overrides WHERE case_id=?", (case_id,)).fetchall()
    }
    rows = []
    for item in conn.execute("SELECT * FROM taxonomy_items WHERE active=1 ORDER BY priority,code").fetchall():
        code = item["code"]
        if docs.get(code, 0) > 0:
            status = "CO_TAI_LIEU"
        else:
            status = overrides.get(code, item["default_applicability"])
        rows.append({
            "code": code,
            "name": item["name"],
            "priority": item["priority"],
            "status": status,
            "document_count": docs.get(code, 0),
            "has_override": code in overrides,
        })
    return rows


def progress_percent(checklist: list[dict[str, Any]]) -> float:
    total = sum(weight(row["priority"]) for row in checklist)
    if total == 0:
        return 0.0
    completed = sum(weight(row["priority"]) for row in checklist if row["status"] in COMPLETED_CHECKLIST)
    return round(completed * 100.0 / total, 2)


def recompute_case(conn, case_id: int, at: str | None = None) -> dict[str, Any]:
    at = at or now()
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if case is None:
        raise ValueError("Không tìm thấy hồ sơ")
    if case["completed_at"]:
        changed = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE case_id=? AND is_present=1 AND last_hashed_at IS NOT NULL AND last_hashed_at>?",
            (case_id, case["completed_at"]),
        ).fetchone()["n"]
        if changed:
            _warning(conn, case_id, "CHANGED_AFTER_COMPLETION", "WARNING", "File nguồn thay đổi sau khi hồ sơ được đánh dấu hoàn thành.", at)

    checklist = checklist_for_case(conn, case_id)
    valid_docs = sum(row["document_count"] for row in checklist)
    review_pending = conn.execute(
        """SELECT COUNT(*) AS n FROM warnings WHERE case_id=? AND active=1
           AND warning_type IN ('REVIEW_PENDING','CAN_XAC_MINH')""",
        (case_id,),
    ).fetchone()["n"] > 0
    missing_p1 = sum(1 for row in checklist if row["priority"] == 1 and row["status"] == "CAN_BO_SUNG")
    has_can_bo_sung = any(row["status"] == "CAN_BO_SUNG" for row in checklist)
    if valid_docs == 0:
        auto = "CHUA_XU_LY"
    elif review_pending:
        auto = "DANG_SO_HOA"
    elif missing_p1 or has_can_bo_sung:
        auto = "CAN_BO_SUNG"
    else:
        auto = "CHO_KIEM_TRA"
    effective = case["manual_status"] or auto
    percent = progress_percent(checklist)
    warning_count = conn.execute("SELECT COUNT(*) AS n FROM warnings WHERE case_id=? AND active=1", (case_id,)).fetchone()["n"]
    conn.execute(
        """UPDATE cases SET auto_status=?,effective_status=?,progress_percent=?,document_count=?,warning_count=?,
           missing_priority1_count=?,last_scanned_at=COALESCE(last_scanned_at,?) WHERE id=?""",
        (auto, effective, percent, valid_docs, warning_count, missing_p1, at, case_id),
    )
    if case["effective_status"] != effective:
        conn.execute(
            "INSERT INTO case_history(case_id,event_type,from_status,to_status,detail,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, "AUTO_STATUS_CHANGED" if not case["manual_status"] else "MANUAL_STATUS_CHANGED", case["effective_status"], effective, "recompute", at),
        )
    return {"auto_status": auto, "effective_status": effective, "progress_percent": percent,
            "document_count": valid_docs, "warning_count": warning_count, "missing_priority1_count": missing_p1,
            "checklist": checklist}


def set_checklist_override(conn, case_id: int, code: str, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in CHECKLIST_STATUSES:
        raise ValueError(f"Trạng thái checklist không hợp lệ: {status}")
    if conn.execute("SELECT code FROM taxonomy_items WHERE code=? AND active=1", (code,)).fetchone() is None:
        raise ValueError(f"Mã taxonomy không hợp lệ: {code}")
    at = now()
    old = conn.execute("SELECT status FROM checklist_overrides WHERE case_id=? AND taxonomy_code=?", (case_id, code)).fetchone()
    conn.execute(
        """INSERT INTO checklist_overrides(case_id,taxonomy_code,status,note,updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(case_id,taxonomy_code) DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at""",
        (case_id, code, status, note, at),
    )
    conn.execute(
        "INSERT INTO case_history(case_id,event_type,detail,created_at) VALUES(?,?,?,?)",
        (case_id, "CHECKLIST_OVERRIDE", f"{code}: {old['status'] if old else None} -> {status}", at),
    )
    return recompute_case(conn, case_id, at)


def set_manual_status(conn, case_id: int, status: str) -> dict[str, Any]:
    if status not in CASE_STATUSES - {"HOAN_THANH"}:
        raise ValueError("Dùng endpoint hoàn thành để đặt HOAN_THANH")
    case = conn.execute("SELECT effective_status FROM cases WHERE id=?", (case_id,)).fetchone()
    if case is None:
        raise ValueError("Không tìm thấy hồ sơ")
    at = now()
    conn.execute("UPDATE cases SET manual_status=?,effective_status=? WHERE id=?", (status, status, case_id))
    conn.execute("INSERT INTO case_history(case_id,event_type,from_status,to_status,created_at) VALUES(?,?,?,?,?)", (case_id, "MANUAL_STATUS_CHANGED", case["effective_status"], status, at))
    return recompute_case(conn, case_id, at)


def mark_complete(conn, case_id: int, reviewed_by: str | None = None) -> dict[str, Any]:
    case = conn.execute("SELECT effective_status FROM cases WHERE id=?", (case_id,)).fetchone()
    if case is None:
        raise ValueError("Không tìm thấy hồ sơ")
    at = now()
    conn.execute("UPDATE cases SET manual_status='HOAN_THANH',effective_status='HOAN_THANH',completed_at=?,reviewed_by=? WHERE id=?", (at, reviewed_by, case_id))
    conn.execute("INSERT INTO case_history(case_id,event_type,from_status,to_status,detail,created_at) VALUES(?,?,?,?,?,?)", (case_id, "MARK_COMPLETED", case["effective_status"], "HOAN_THANH", reviewed_by, at))
    return recompute_case(conn, case_id, at)


def reopen(conn, case_id: int) -> dict[str, Any]:
    case = conn.execute("SELECT effective_status FROM cases WHERE id=?", (case_id,)).fetchone()
    if case is None:
        raise ValueError("Không tìm thấy hồ sơ")
    at = now()
    conn.execute("UPDATE cases SET manual_status=NULL,effective_status='CHUA_XU_LY',completed_at=NULL,reviewed_by=NULL WHERE id=?", (case_id,))
    conn.execute("INSERT INTO case_history(case_id,event_type,from_status,to_status,created_at) VALUES(?,?,?,?,?)", (case_id, "REOPENED", case["effective_status"], "CHUA_XU_LY", at))
    return recompute_case(conn, case_id, at)


def update_note(conn, case_id: int, note: str | None) -> None:
    at = now()
    conn.execute("UPDATE cases SET note=? WHERE id=?", (note, case_id))
    conn.execute("INSERT INTO case_history(case_id,event_type,detail,created_at) VALUES(?,?,?,?)", (case_id, "NOTE_UPDATED", note, at))


def _warning(conn, case_id: int, kind: str, severity: str, message: str, at: str) -> None:
    fingerprint = f"{case_id}:case:{kind}:{message}"
    conn.execute(
        """INSERT INTO warnings(case_id,warning_type,severity,message,active,fingerprint,created_at,updated_at)
           VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET active=1,updated_at=excluded.updated_at""",
        (case_id, kind, severity, message, fingerprint, at, at),
    )

