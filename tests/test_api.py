import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Use isolated test database for automated testing
os.environ["MONGO_URI"] = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")
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
    get_month_bounds,
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
    db.employees.update_one(
        {"emp_code": "EMP1001"},
        {"$set": {
            "emp_code": "EMP1001",
            "name": "Validation Test",
            "email": "val@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )

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
    # Impossible calendar date (Feb 31)
    res_feb = client.post("/employees", json={
        "emp_code": "EMP1002",
        "name": "Bad Date",
        "email": "baddate@example.com",
        "department": "Engineering",
        "joined_on": "2026-02-31"
    })
    assert res_feb.status_code == 422

    # Impossible date in query parameter (April 31)
    res_q = client.get("/attendance?date_from=2026-04-31")
    assert res_q.status_code == 422

    # Non-leap year Feb 29
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-02-29&to=2026-03-05")
    assert res_trend.status_code == 422


def test_strict_joined_on_formats():
    # Reject basic ISO format without hyphens
    res1 = client.post("/employees", json={
        "emp_code": "EMP8001",
        "name": "ISO Test",
        "email": "iso1@example.com",
        "department": "Engineering",
        "joined_on": "20260101"
    })
    assert res1.status_code == 422
    assert "joined_on" in res1.text

    # Reject week-date format
    res2 = client.post("/employees", json={
        "emp_code": "EMP8002",
        "name": "ISO Test",
        "email": "iso2@example.com",
        "department": "Engineering",
        "joined_on": "2026-W01-1"
    })
    assert res2.status_code == 422
    assert "joined_on" in res2.text

    # Reject year 0000
    res3 = client.post("/employees", json={
        "emp_code": "EMP8003",
        "name": "ISO Test",
        "email": "iso3@example.com",
        "department": "Engineering",
        "joined_on": "0000-01-01"
    })
    assert res3.status_code == 422
    assert "joined_on" in res3.text

    # Reject non-padded month/day
    res4 = client.post("/employees", json={
        "emp_code": "EMP8004",
        "name": "ISO Test",
        "email": "iso4@example.com",
        "department": "Engineering",
        "joined_on": "2026-1-1"
    })
    assert res4.status_code == 422
    assert "joined_on" in res4.text

    # Accept strictly valid YYYY-MM-DD
    db.employees.delete_many({"emp_code": "EMP8005"})
    res5 = client.post("/employees", json={
        "emp_code": "EMP8005",
        "name": "Valid Date",
        "email": "valid_iso@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res5.status_code == 201


def test_invalid_month_format_regression():
    # Year 0000 should return 422 Unprocessable Entity, not unhandled 500
    res_dept = client.get("/analytics/departments/summary?month=0000-01")
    assert res_dept.status_code == 422

    res_emp = client.get("/analytics/employees/EMP1001/monthly?month=0000-01")
    assert res_emp.status_code == 422

    res_late = client.get("/analytics/leaderboard/late?month=0000-01")
    assert res_late.status_code == 422

    res_exp = client.get("/admin/explain/department_summary?month=0000-01")
    assert res_exp.status_code == 422

    # Month 13 should return 422
    res_m13 = client.get("/analytics/departments/summary?month=2026-13")
    assert res_m13.status_code == 422

    # Month 00 should return 422
    res_m00 = client.get("/analytics/departments/summary?month=2026-00")
    assert res_m00.status_code == 422


def test_boundary_month_9999_12():
    # Direct function bounds calculation: must return 9999-12-01 to 9999-12-31 without ValueError
    start_d, end_d = get_month_bounds("9999-12")
    assert start_d == "9999-12-01"
    assert end_d == "9999-12-31"

    # API endpoints accepting valid boundary month 9999-12
    res_dept = client.get("/analytics/departments/summary?month=9999-12")
    assert res_dept.status_code == 200

    res_late = client.get("/analytics/leaderboard/late?month=9999-12")
    assert res_late.status_code == 200

    res_exp = client.get("/admin/explain/department_summary?month=9999-12")
    assert res_exp.status_code == 200




# --------------------------------------------------------------------------- #
# 2. Half-Up Rounding vs Banker's Rounding (Ties-To-Even)
# --------------------------------------------------------------------------- #
def test_half_up_rounding_vs_ties_to_even():
    from decimal import ROUND_HALF_EVEN

    # 2 decimals comparison (exact binary fraction 1/8):
    # 0.125 in half-up rounds to 0.13, while Python/banker's round(0.125, 2) produces 0.12
    assert round_half_up(0.125, 2) == 0.13
    assert round(0.125, 2) == 0.12  # Demonstrates python built-in ties-to-even behavior

    # 3 decimals comparison (exact binary fraction 1/16 = 0.0625):
    # 0.0625 in half-up rounds to 0.063, while Python round(0.0625, 3) produces 0.062
    assert round_half_up(0.0625, 3) == 0.063
    assert round(0.0625, 3) == 0.062

    # 4 decimals comparison (exact binary fraction 1/32 = 0.03125):
    # 0.03125 in half-up rounds to 0.0313, while Python round(0.03125, 4) produces 0.0312
    assert round_half_up(0.03125, 4) == 0.0313
    assert round(0.03125, 4) == 0.0312

    # Decimal comparison for 4 decimals:
    # 0.12345 in half-up rounds to 0.1235, while Decimal ROUND_HALF_EVEN produces 0.1234
    assert round_half_up(Decimal("0.12345"), 4) == 0.1235
    assert float(Decimal("0.12345").quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)) == 0.1234

    # Work hours R4: 9630 seconds = 2.675 hours -> rounds half-up to 2.68
    t_in = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 2, 40, 30, tzinfo=timezone.utc)
    assert compute_work_hours(t_in, t_out) == 2.68
    assert round((t_out - t_in).total_seconds() / 3600, 2) == 2.67  # Banker's rounding fails


# --------------------------------------------------------------------------- #
# 3. Real Concurrency Tests: Simultaneous HTTP Requests via Threads
# --------------------------------------------------------------------------- #
def test_real_concurrency_duplicate_punch_in():
    emp_code = "EMP2001"
    db.employees.update_one(
        {"emp_code": emp_code},
        {"$set": {
            "emp_code": emp_code,
            "name": "Concurrent Tester",
            "email": "concurrent@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-06"})
    ts_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)

    thread_count = 5
    barrier = threading.Barrier(thread_count)

    def worker():
        barrier.wait()
        return client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_in})

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = [pool.submit(worker) for _ in range(thread_count)]
        responses = [f.result() for f in futures]

    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1, f"Expected exactly one 201 Created, got {statuses}"
    assert statuses.count(409) == thread_count - 1, f"Expected remaining to be 409 Conflict, got {statuses}"


def test_real_concurrency_duplicate_punch_out():
    emp_code = "EMP2001"
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-07"})
    ts_in = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ts_out = int(datetime(2026, 7, 7, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)

    # First perform punch-in
    res_in = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_in})
    assert res_in.status_code == 201

    thread_count = 5
    barrier = threading.Barrier(thread_count)

    def worker():
        barrier.wait()
        return client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ts_out})

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = [pool.submit(worker) for _ in range(thread_count)]
        responses = [f.result() for f in futures]

    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 1, f"Expected exactly one 200 OK, got {statuses}"
    assert statuses.count(409) == thread_count - 1, f"Expected remaining to be 409 Conflict, got {statuses}"


def test_real_concurrency_duplicate_employee_create():
    emp_code = "EMP9999"
    db.employees.delete_many({"emp_code": emp_code})

    thread_count = 5
    barrier = threading.Barrier(thread_count)

    def worker():
        barrier.wait()
        return client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Race Tester",
            "email": "racetest@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = [pool.submit(worker) for _ in range(thread_count)]
        responses = [f.result() for f in futures]

    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1, f"Expected exactly one 201 Created, got {statuses}"
    assert statuses.count(409) == thread_count - 1, f"Expected remaining to be 409 Conflict, got {statuses}"


# --------------------------------------------------------------------------- #
# 4. Regularization & Legacy Record Safety
# --------------------------------------------------------------------------- #
def test_legacy_record_regularization():
    emp_code = "EMP3001"
    db.employees.update_one(
        {"emp_code": emp_code},
        {"$set": {
            "emp_code": emp_code,
            "name": "Legacy User",
            "email": "legacy@example.com",
            "department": "Support",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01",
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )

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
        # 'history' and 'half_day' intentionally omitted (legacy shape)
    })

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
# 5. Business Rules Unit Verification (R1 - R5)
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


def test_microsecond_truncation_to_whole_seconds():
    emp_code = "EMP_MICRO_1"
    db.employees.update_one(
        {"emp_code": emp_code},
        {"$set": {
            "emp_code": emp_code,
            "name": "Microsecond Emp",
            "email": "micro@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, 0, 0, 0, 456789, tzinfo=timezone.utc),
        }},
        upsert=True
    )
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-20"})
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-20",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 20, 4, 0, 0, 850000, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 20, 13, 0, 0, 123456, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [
            {
                "at": datetime(2026, 7, 20, 14, 0, 0, 999999, tzinfo=timezone.utc),
                "by": "admin",
                "reason": "microsecond audit test",
                "changes": {
                    "punch_in": {
                        "from": datetime(2026, 7, 20, 3, 59, 59, 500000, tzinfo=timezone.utc),
                        "to": datetime(2026, 7, 20, 4, 0, 0, 850000, tzinfo=timezone.utc),
                    }
                }
            }
        ]
    })

    # Fetch attendance list
    res = client.get(f"/attendance?emp_code={emp_code}&date_from=2026-07-20&date_to=2026-07-20")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    doc = items[0]

    # Verify all output epoch milliseconds are exact multiples of 1000 (ending in 000)
    assert doc["punch_in"] is not None and doc["punch_in"] % 1000 == 0
    assert doc["punch_out"] is not None and doc["punch_out"] % 1000 == 0
    hist = doc["history"][0]
    assert hist["at"] is not None and hist["at"] % 1000 == 0
    assert hist["changes"]["punch_in"]["from"] % 1000 == 0
    assert hist["changes"]["punch_in"]["to"] % 1000 == 0

    # Also check employee list created_at truncation
    emp_res = client.get("/employees?department=Engineering")
    assert emp_res.status_code == 200
    emp_match = next((e for e in emp_res.json()["items"] if e["emp_code"] == emp_code), None)
    assert emp_match is not None
    assert emp_match["created_at"] % 1000 == 0


def test_boundary_conditions_half_day_and_overnight_overtime():
    # R4 Half-Day Cutoff: 4.50 hours threshold
    # 4.49 hours = 16164 seconds
    t_in = datetime(2026, 7, 1, 4, 0, 0, tzinfo=timezone.utc)
    t_out_449 = datetime(2026, 7, 1, 8, 29, 24, tzinfo=timezone.utc)
    wh_449 = compute_work_hours(t_in, t_out_449)
    assert wh_449 == 4.49
    assert (wh_449 < 4.50) is True  # half_day is True

    # 4.50 hours = 16200 seconds
    t_out_450 = datetime(2026, 7, 1, 8, 30, 0, tzinfo=timezone.utc)
    wh_450 = compute_work_hours(t_in, t_out_450)
    assert wh_450 == 4.50
    assert (wh_450 < 4.50) is False  # half_day is False

    # Test via API punch-in and punch-out for half_day boundary
    emp_code = "EMP_HALF_DAY"
    db.employees.update_one(
        {"emp_code": emp_code},
        {"$set": {
            "emp_code": emp_code,
            "name": "Half Day Tester",
            "email": "halfday@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    # Day 1: 4.49 hours -> half_day True
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-21"})
    ts_in = dt_to_epoch(t_in)
    ts_out_449 = dt_to_epoch(t_out_449)
    assert client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ts_in}).status_code == 201
    res_out_1 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ts_out_449})
    assert res_out_1.status_code == 200
    assert res_out_1.json()["half_day"] is True
    assert res_out_1.json()["work_hours"] == 4.49

    # Day 2: 4.50 hours -> half_day False
    t_in_d2 = datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc)
    t_out_d2 = datetime(2026, 7, 22, 8, 30, 0, tzinfo=timezone.utc)
    db.attendance_logs.delete_many({"emp_code": emp_code, "date": "2026-07-22"})
    assert client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": dt_to_epoch(t_in_d2)}).status_code == 201
    res_out_2 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": dt_to_epoch(t_out_d2)})
    assert res_out_2.status_code == 200
    assert res_out_2.json()["half_day"] is False
    assert res_out_2.json()["work_hours"] == 4.50

    # R3 Overnight Overtime: shift 22:00 -> 06:00
    # Date: 2026-07-23 (shift start 22:00 IST on July 23, ends 06:00 IST on July 24)
    # 06:00 IST = 00:30 UTC on July 24
    # 29 min past shift_end: 06:29 IST = 00:59 UTC -> overtime is 0
    t_ot_29 = datetime(2026, 7, 24, 0, 59, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_ot_29, "06:00", "22:00", "2026-07-23") == 0

    # 30 min past shift_end: 06:30 IST = 01:00 UTC -> overtime is 30
    t_ot_30 = datetime(2026, 7, 24, 1, 0, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_ot_30, "06:00", "22:00", "2026-07-23") == 30

    # 31 min past shift_end: 06:31 IST = 01:01 UTC -> overtime is 31
    t_ot_31 = datetime(2026, 7, 24, 1, 1, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_ot_31, "06:00", "22:00", "2026-07-23") == 31



# --------------------------------------------------------------------------- #
# 6. Edge Cases: Trend with No Records, Weekend Records, Moving Average
# --------------------------------------------------------------------------- #
def test_trend_date_range_with_no_attendance_logs():
    dept = "EdgeDept"
    db.employees.update_one(
        {"emp_code": "EMP_EDGE_1"},
        {"$set": {
            "emp_code": "EMP_EDGE_1",
            "name": "Edge User",
            "email": "edge@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    # Ensure zero attendance logs exist in August 2026
    db.attendance_logs.delete_many({"emp_code": "EMP_EDGE_1"})

    # Query 5 days: 2026-08-01 (Sat) to 2026-08-05 (Wed)
    res = client.get(f"/analytics/departments/{dept}/trend?from=2026-08-01&to=2026-08-05")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["department"] == dept
    items = data["items"]
    assert len(items) == 5, f"Expected exactly 5 rows inclusive, got {len(items)}"

    expected_dates = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
    assert [it["date"] for it in items] == expected_dates

    # 2026-08-01 (Sat) and 2026-08-02 (Sun) are weekends
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    assert items[1]["is_working_day"] is False
    assert items[1]["attendance_rate"] is None
    assert items[1]["moving_avg_7d"] is None

    # 2026-08-03 (Mon) is a weekday with headcount=1, present_count=0 -> rate=0.0, moving_avg=0.0
    assert items[2]["is_working_day"] is True
    assert items[2]["headcount"] == 1
    assert items[2]["present_count"] == 0.0
    assert items[2]["late_count"] == 0
    assert items[2]["attendance_rate"] == 0.0
    assert items[2]["moving_avg_7d"] == 0.0


def test_department_summary_with_employees_no_logs():
    dept = "NoLogDept"
    db.employees.update_one(
        {"emp_code": "EMP_NOLOG_1"},
        {"$set": {
            "emp_code": "EMP_NOLOG_1", "name": "No Log 1", "email": "nolog1@example.com",
            "department": dept, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    db.employees.update_one(
        {"emp_code": "EMP_NOLOG_2"},
        {"$set": {
            "emp_code": "EMP_NOLOG_2", "name": "No Log 2", "email": "nolog2@example.com",
            "department": dept, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP_NOLOG_1", "EMP_NOLOG_2"]}})

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    dept_item = next((item for item in data["items"] if item["department"] == dept), None)
    assert dept_item is not None
    assert dept_item["headcount"] == 2
    assert dept_item["present_days"] == 0.0
    assert dept_item["avg_work_hours"] is None
    assert dept_item["late_count"] == 0
    assert dept_item["leave_count"] == 0
    assert dept_item["on_duty_count"] == 0


def test_weekend_records_and_moving_avg_window():
    emp_code = "EMP_WKND"
    dept = "WeekendDept"
    db.employees.update_one(
        {"emp_code": emp_code},
        {"$set": {
            "emp_code": emp_code, "name": "Weekend User", "email": "wknd@example.com",
            "department": dept, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        }},
        upsert=True
    )
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Saturday 2026-07-11 punch (late by 15 min, overtime 45 min)
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-11",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 11, 4, 15, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 11, 13, 45, tzinfo=timezone.utc),
        "work_hours": 9.5,
        "late_minutes": 15,
        "overtime_minutes": 45,
        "half_day": False,
        "history": [],
    })
    # Monday 2026-07-13 punch
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-07-13",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    })

    # 1. Verify Monthly Analytics: R7 weekend records do NOT count towards present_days,
    # but DO add to late_minutes and overtime_minutes
    res_monthly = client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-07")
    assert res_monthly.status_code == 200
    monthly_data = res_monthly.json()
    assert monthly_data["present_days"] == 1.0  # Only Monday counted, Saturday excluded
    assert monthly_data["total_late_minutes"] == 15  # Weekend lateness included
    assert monthly_data["total_overtime_minutes"] == 45  # Weekend overtime included

    # 2. Verify Department Trend: moving average window begins inside requested range
    # Saturday (2026-07-11) to Tuesday (2026-07-14)
    res_trend = client.get(f"/analytics/departments/{dept}/trend?from=2026-07-11&to=2026-07-14")
    assert res_trend.status_code == 200
    trend_items = res_trend.json()["items"]
    assert len(trend_items) == 4

    # Saturday: weekend -> rate is None, moving_avg is None
    assert trend_items[0]["date"] == "2026-07-11"
    assert trend_items[0]["is_working_day"] is False
    assert trend_items[0]["present_count"] == 1.0  # presence-status records on any weekday or weekend
    assert trend_items[0]["attendance_rate"] is None
    assert trend_items[0]["moving_avg_7d"] is None

    # Sunday: weekend -> rate is None, moving_avg is None
    assert trend_items[1]["date"] == "2026-07-12"
    assert trend_items[1]["attendance_rate"] is None
    assert trend_items[1]["moving_avg_7d"] is None

    # Monday: working day, 1 present / 1 headcount = 1.0 -> moving_avg of [1.0] = 1.0
    assert trend_items[2]["date"] == "2026-07-13"
    assert trend_items[2]["is_working_day"] is True
    assert trend_items[2]["attendance_rate"] == 1.0
    assert trend_items[2]["moving_avg_7d"] == 1.0

    # Tuesday: working day, 0 present / 1 headcount = 0.0 -> moving_avg of [1.0, 0.0] = 0.5
    assert trend_items[3]["date"] == "2026-07-14"
    assert trend_items[3]["is_working_day"] is True
    assert trend_items[3]["attendance_rate"] == 0.0
    assert trend_items[3]["moving_avg_7d"] == 0.5


# --------------------------------------------------------------------------- #
# 7. Explain Winning Execution Plans: IXSCAN and NO COLLSCAN
# --------------------------------------------------------------------------- #
def test_all_explain_endpoints_use_ixscan():
    ensure_indexes()

    # Ensure seed records exist so index optimizer evaluates index scan
    db.employees.update_one(
        {"emp_code": "EMP2001"},
        {"$set": {
            "emp_code": "EMP2001", "name": "Explain Emp", "email": "exp@emp.com",
            "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)
        }},
        upsert=True
    )
    db.attendance_logs.update_one(
        {"emp_code": "EMP2001", "date": "2026-07-06"},
        {"$set": {
            "emp_code": "EMP2001", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
            "work_hours": 9.0, "late_minutes": 20, "overtime_minutes": 0, "half_day": False
        }},
        upsert=True
    )

    def extract_winning_stages(explain_data: dict) -> list[str]:
        found = []
        def recurse(node):
            if isinstance(node, dict):
                if "stage" in node:
                    found.append(node["stage"])
                for val in node.values():
                    recurse(val)
            elif isinstance(node, list):
                for item in node:
                    recurse(item)

        stages = explain_data.get("stages", [])
        if stages and "$cursor" in stages[0]:
            cursor_part = stages[0]["$cursor"]
            winning = cursor_part.get("queryPlanner", {}).get("winningPlan", {})
        else:
            winning = explain_data.get("queryPlanner", {}).get("winningPlan", {})

        recurse(winning)
        return found

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

        winning_stages = extract_winning_stages(data["explain"])
        assert "IXSCAN" in winning_stages, f"{ep} winning plan missing IXSCAN. Stages: {winning_stages}"
        assert "COLLSCAN" not in winning_stages, f"{ep} winning plan contains COLLSCAN. Stages: {winning_stages}"


if __name__ == "__main__":
    pytest.main(["-v", __file__])
