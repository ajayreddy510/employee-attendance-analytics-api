import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Use isolated test database for automated testing
os.environ["MONGO_DB"] = "attendance_test_db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import (
    app,
    db,
    ensure_indexes,
    dt_to_epoch,
    round_half_up,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    get_attendance_date,
)

client = TestClient(app)

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    ensure_indexes()
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    # Cleanup after tests
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

# --------------------------------------------------------------------------- #
# 1. Validation Tests: Strict Epoch Millis & Calendar Dates
# --------------------------------------------------------------------------- #
def test_strict_epoch_millis_rejections():
    # Setup test employee
    db.employees.insert_one({
        "emp_code": "EMP1001",
        "name": "Validation Test",
        "email": "val@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc),
    })

    # Reject string timestamp
    res_str = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": "1783312500000"})
    assert res_str.status_code == 422

    # Reject float timestamp
    res_float = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1783312500000.5})
    assert res_float.status_code == 422

    # Reject boolean timestamp (True is subclass of int in Python)
    res_bool = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": True})
    assert res_bool.status_code == 422

    # Reject seconds-looking timestamp (< 100000000000)
    res_sec = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1783312500})
    assert res_sec.status_code == 422

    # Reject out-of-range timestamp (> 4102444800000)
    res_future = client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 5000000000000})
    assert res_future.status_code == 422

def test_invalid_calendar_dates_rejection():
    # Impossible date (Feb 31)
    res_feb = client.post("/employees", json={
        "emp_code": "EMP1002",
        "name": "Bad Date",
        "email": "baddate@example.com",
        "department": "Engineering",
        "joined_on": "2026-02-31"
    })
    assert res_feb.status_code == 422

    # Impossible date in query parameter
    res_q = client.get("/attendance?date_from=2026-04-31")
    assert res_q.status_code == 422

    # Impossible date in trend path/query
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-02-29&to=2026-03-05")
    assert res_trend.status_code == 422

# --------------------------------------------------------------------------- #
# 2. Concurrency & Unique Index Constraints
# --------------------------------------------------------------------------- #
def test_employee_uniqueness():
    db.employees.delete_many({"emp_code": "EMP2001"})
    res1 = client.post("/employees", json={
        "emp_code": "EMP2001",
        "name": "First Create",
        "email": "unique1@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert res1.status_code == 201

    # Duplicate must return 409
    res2 = client.post("/employees", json={
        "emp_code": "EMP2001",
        "name": "Duplicate Create",
        "email": "unique2@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert res2.status_code == 409

def test_punch_in_and_punch_out_concurrency():
    emp_code = "EMP2001"
    # Punch in 09:28 IST (03:58 UTC) on 2026-07-06
    ts_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-06"})

    res_in1 = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_in})
    assert res_in1.status_code == 201

    # Second punch in on same day must return 409
    res_in2 = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_in})
    assert res_in2.status_code == 409

    # Punch out at 18:35 IST (13:05 UTC)
    ts_out = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_out1 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ts_out})
    assert res_out1.status_code == 200

    # Second punch out for closed record must return 409
    res_out2 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ts_out})
    assert res_out2.status_code == 409

# --------------------------------------------------------------------------- #
# 3. Regularization & Legacy Record Safety
# --------------------------------------------------------------------------- #
def test_legacy_record_regularization():
    # Insert a legacy record with missing 'history' and 'half_day' fields (Sample 9 in DATA_MODEL.md)
    emp_code = "EMP3001"
    db.employees.delete_many({"emp_code": emp_code})
    db.employees.insert_one({
        "emp_code": emp_code,
        "name": "Legacy User",
        "email": "legacy@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
        "created_at": datetime.now(timezone.utc),
    })

    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-14"})
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
        "work_hours": 8.98,
        "late_minutes": 0,
        "overtime_minutes": 0,
        # 'history' and 'half_day' intentionally omitted
    })

    # Regularize legacy record
    res_patch = client.patch(f"/attendance/{emp_code}/2026-07-14", json={
        "reason": "Corrected biometric shift",
        "regularized_by": "hr_manager",
        "status": "WFH"
    })
    assert res_patch.status_code == 200, res_patch.text
    data = res_patch.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "hr_manager"
    assert data["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}

# --------------------------------------------------------------------------- #
# 4. Business Rules Unit Verification (R1 - R5)
# --------------------------------------------------------------------------- #
def test_rules_calculations():
    # R1 & R2: Late grace period
    # 09:40:00 -> 0
    t_094000 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(t_094000, "09:30", "2026-07-06") == 0

    # 09:40:01 -> 10
    t_094001 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(t_094001, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    t_101559 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(t_101559, "09:30", "2026-07-06") == 45

    # R1: Overnight shift (22:00 - 06:00). Punch-in 05:30 IST on July 7 belongs to July 6
    t_overnight = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)  # 05:30 IST
    assert get_attendance_date(t_overnight, "22:00", "06:00") == "2026-07-06"

    # R3: Overtime minimum 30 min threshold
    # 18:55 (25 min past 18:30) -> 0
    t_ot25 = datetime(2026, 7, 6, 13, 25, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_ot25, "18:30", "09:30", "2026-07-06") == 0

    # 19:10 (40 min past 18:30) -> 40
    t_ot40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_ot40, "18:30", "09:30", "2026-07-06") == 40

    # R4: Work hours round-half-up (not banker's rounding)
    # 9630 seconds = 2.675 hours -> rounds half-up to 2.68
    t_in = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 2, 40, 30, tzinfo=timezone.utc)
    assert compute_work_hours(t_in, t_out) == 2.68

# --------------------------------------------------------------------------- #
# 5. Explain Query Plans (IXSCAN without COLLSCAN)
# --------------------------------------------------------------------------- #
def test_all_explain_endpoints_use_ixscan():
    ensure_indexes()
    endpoints = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("employee_monthly", {"emp_code": "EMP2001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-10"}),
    ]
    for ep, params in endpoints:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        
        # Verify plan contains IXSCAN and NO COLLSCAN
        import json
        plan_str = json.dumps(data["explain"], default=str)
        assert "IXSCAN" in plan_str, f"{ep} explain plan missing IXSCAN"
        assert "COLLSCAN" not in plan_str, f"{ep} explain plan contains COLLSCAN"

if __name__ == "__main__":
    pytest.main(["-v", __file__])
