"""
Employee Attendance & Analytics API
Python / FastAPI / MongoDB
Complies with openapi.yaml and DATA_MODEL.md specifications.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env (override=False)
load_dotenv(override=False)

# MongoDB connection reading from standard and alias environment variables
mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL") or "mongodb://localhost:27017"
mongo_db = os.getenv("MONGO_DB") or os.getenv("DATABASE_NAME") or "attendance_db"


def clean_mongo_doc(doc: Any) -> Any:
    """Serialize BSON objects (Timestamps, ObjectIds) to standard JSON-compatible Python dicts."""
    return json.loads(json_util.dumps(doc))

client = MongoClient(mongo_uri, tz_aware=True)
db = client[mongo_db]

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Startup Index Management
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def parse_strict_epoch_millis(v: Any) -> Optional[int]:
    """Validate strict integer milliseconds. Reject strings, floats, booleans, and out-of-range."""
    if v is None:
        return None
    # Reject boolean (since type(True) is bool, subclass of int)
    if type(v) is not int:
        raise ValueError("Epoch milliseconds must be a strict integer")
    if v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
        raise ValueError(f"Epoch milliseconds out of range [{MIN_EPOCH_MS}, {MAX_EPOCH_MS}]")
    return v


def parse_valid_date(date_str: str) -> date:
    """Validate that date_str is a valid calendar date YYYY-MM-DD."""
    if not isinstance(date_str, str) or not re.match(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$", date_str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid date format: {date_str}, expected YYYY-MM-DD",
        )
    try:
        return date.fromisoformat(date_str)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid calendar date: {date_str}",
        )


def parse_valid_month(month_str: str) -> tuple[int, int]:
    """Validate that month_str is a valid month YYYY-MM."""
    if not isinstance(month_str, str) or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid month format: {month_str}, expected YYYY-MM",
        )
    y, m = map(int, month_str.split("-"))
    return y, m


def round_half_up(val: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """Arithmetic round-half-up (R4, R8)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("0." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def dt_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert integer epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def parse_ist_time(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start):
    a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = parse_ist_time(shift_end)
        punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        if punch_time < (end_h, end_m, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2 Late. Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    d = date.fromisoformat(date_str)
    start_h, start_m = parse_ist_time(shift_start)
    start_dt = datetime(d.year, d.month, d.day, start_h, start_m, 0, tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST).replace(microsecond=0)

    elapsed_seconds = (punch_in_ist - start_dt).total_seconds()
    if elapsed_seconds > 600:  # strictly more than 10:00 (600 seconds)
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4 Work hours. (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    p_in = punch_in_utc.replace(microsecond=0)
    p_out = punch_out_utc.replace(microsecond=0)
    diff_sec = Decimal(int((p_out - p_in).total_seconds()))
    hours = (diff_sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, date_str: str) -> int:
    """
    R3 Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    d = date.fromisoformat(date_str)
    end_h, end_m = parse_ist_time(shift_end)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        d = d + timedelta(days=1)
    end_dt = datetime(d.year, d.month, d.day, end_h, end_m, 0, tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST).replace(microsecond=0)

    diff_seconds = (punch_out_ist - end_dt).total_seconds()
    if diff_seconds >= 0:
        mins = int(diff_seconds // 60)
        return mins if mins >= 30 else 0
    return 0


def count_working_days(start_date_str: str, end_date_str: str, joined_on_str: str) -> int:
    """
    R7 Working days: Mon-Fri, no holiday calendar.
    For an employee who joined mid-month, working days are counted from joined_on.
    """
    d_start = max(date.fromisoformat(start_date_str), date.fromisoformat(joined_on_str))
    d_end = date.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    working_days = 0
    cur = d_start
    while cur <= d_end:
        if cur.weekday() < 5:  # Mon=0, ..., Fri=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document to AttendanceRecord response schema."""
    history_out = []
    for h in doc.get("history") or []:
        changes_out = {}
        for k, v in (h.get("changes") or {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = dt_to_epoch(f_val) if isinstance(f_val, datetime) else f_val
                t_val = dt_to_epoch(t_val) if isinstance(t_val, datetime) else t_val
            changes_out[k] = {"from": f_val, "to": t_val}

        at_val = h.get("at")
        at_epoch = dt_to_epoch(at_val) if isinstance(at_val, datetime) else at_val
        history_out.append({
            "at": at_epoch,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch(doc.get("punch_in")),
        "punch_out": dt_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out,
    }


def get_month_bounds(month_str: str) -> tuple[str, str]:
    """Return start date and end date strings for a month YYYY-MM."""
    year, month = parse_valid_month(month_str)
    start_date = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end_d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end_d = date(year, month + 1, 1) - timedelta(days=1)
    return start_date, end_d.isoformat()


# --------------------------------------------------------------------------- #
# Shared Pipeline & Query Builders (Used by Endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter
    return q


def build_employee_monthly_pipeline(
    emp_code: str,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


def build_department_summary_pipeline(
    start_date: str,
    end_date: str,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": ["$logs.late_minutes", 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(
    start_date: str,
    end_date: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
    from_dt: datetime,
    to_dt_next_day: datetime,
) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_next_day],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$", json_schema_extra={"example": "EMP0001"})
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on", mode="before")
    @classmethod
    def check_joined_on(cls, v: Any):
        if not isinstance(v, str):
            raise ValueError("joined_on must be a string")
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid calendar date")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: PresenceStatusType = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any):
        return parse_strict_epoch_millis(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any):
        return parse_strict_epoch_millis(v)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[StatusType] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", mode="before")
    @classmethod
    def check_punch_in(cls, v: Any):
        return parse_strict_epoch_millis(v)

    @field_validator("punch_out", mode="before")
    @classmethod
    def check_punch_out(cls, v: Any):
        return parse_strict_epoch_millis(v)


# --------------------------------------------------------------------------- #
# System & Employee Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"MongoDB unreachable: {str(e)}",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    """Create an employee. Unique emp_code enforced by unique database index."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch(now_utc),
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for d in cursor:
        items.append({
            "emp_code": d["emp_code"],
            "name": d["name"],
            "email": d["email"],
            "department": d["department"],
            "shift_start": d["shift_start"],
            "shift_end": d["shift_end"],
            "joined_on": d["joined_on"],
            "created_at": dt_to_epoch(d.get("created_at")),
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Race-safe via unique compound index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_to_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Closes most recent record whose punch_in <= punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_to_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find most recent record with punch_in <= punch_out_dt
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt, "$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in",
        )
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )
    if rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime(punch_out_dt, emp["shift_end"], emp["shift_start"], rec["date"])
    half_day = work_hours < 4.50

    # Atomic update to prevent simultaneous punch-out race condition
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record is already punched out",
        )

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc."""
    if date_from:
        parse_valid_date(date_from)
    if date_to:
        parse_valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )

    q = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)

    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's attendance record with audit history."""
    parse_valid_date(date_param)

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    new_status = body.status if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Supplying punch_in or punch_out when setting ABSENT or LEAVE is not allowed",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = epoch_to_dt(body.punch_in)
        else:
            new_punch_in = rec.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires punch_in",
            )

        # Check punch_in stays on the record's attendance date
        calculated_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calculated_date != date_param:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_to_dt(body.punch_out)
        else:
            new_punch_out = rec.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_end"], emp["shift_start"], date_param)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date_param)

    # Determine changes
    changes_mongo: Dict[str, Any] = {}
    candidate_changes = [
        ("status", rec.get("status"), new_status),
        ("punch_in", rec.get("punch_in"), new_punch_in),
        ("punch_out", rec.get("punch_out"), new_punch_out),
        ("work_hours", rec.get("work_hours"), new_work_hours),
        ("late_minutes", rec.get("late_minutes", 0), new_late_minutes),
        ("overtime_minutes", rec.get("overtime_minutes", 0), new_overtime_minutes),
        ("half_day", bool(rec.get("half_day", False)), new_half_day),
    ]

    for field_name, old_val, updated_val in candidate_changes:
        if old_val != updated_val:
            changes_mongo[field_name] = {"from": old_val, "to": updated_val}

    if not changes_mongo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request changes nothing",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    # Optimistic concurrency check to ensure history entries aren't lost
    # Handles both seeded records with missing history field and existing history arrays
    current_history = rec.get("history")
    current_history_len = len(current_history) if current_history is not None else 0

    filter_q: Dict[str, Any] = {"_id": rec["_id"]}
    if current_history_len == 0:
        filter_q["$or"] = [
            {"history": {"$exists": False}},
            {"history": {"$size": 0}},
        ]
    else:
        filter_q["history"] = {"$size": current_history_len}

    update_res = db.attendance_logs.find_one_and_update(
        filter_q,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not update_res:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent regularization detected",
        )

    return format_attendance_doc(update_res)


# --------------------------------------------------------------------------- #
# Analytics Aggregation Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(...),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    parse_valid_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    start_date, end_date = get_month_bounds(month)
    working_days = count_working_days(start_date, end_date, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        leave_days = int(row["leave_days"])
        late_count = int(row["late_count"])
        total_late_minutes = int(row["total_late_minutes"])
        total_overtime_minutes = int(row["total_overtime_minutes"])
        present_days = float(row["present_days"])
    else:
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0
        present_days = 0.0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = Query(None),
):
    """Department summary for a month via cross-collection aggregation."""
    parse_valid_month(month)
    start_date, end_date = get_month_bounds(month)
    pipeline = build_department_summary_pipeline(start_date, end_date, department)

    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        headcount = len(r["employees"])
        if headcount == 0:
            continue
        wh_list = r["work_hours_list"]
        avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2) if wh_list else None
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard for a month using window ranking ($setWindowFields)."""
    parse_valid_month(month)
    start_date, end_date = get_month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in rows:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Daily attendance trend with gap filling ($densify) and 7-day moving average."""
    d_from = parse_valid_date(from_date)
    d_to = parse_valid_date(to_date)

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department")

    if d_to < d_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be >= 'from' date",
        )
    if (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range exceeds 92 days",
        )

    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt_next_day = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)
    pipeline = build_department_trend_pipeline(department, from_date, to_date, from_dt, to_dt_next_day)

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in rows:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None,
            "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    """Return raw MongoDB explain output for an endpoint's main query with executionStats."""
    if endpoint == "attendance_list":
        if date_from:
            parse_valid_date(date_from)
        if date_to:
            parse_valid_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")

        q = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
        skip = ((page or 1) - 1) * (page_size or 20)
        sort_spec = [("date", -1), ("emp_code", 1)]
        raw_exp = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": dict(sort_spec),
                "skip": skip,
                "limit": page_size or 20,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_doc(raw_exp)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required",
            )
        parse_valid_month(month)
        start_date, end_date = get_month_bounds(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_doc(raw_exp)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        parse_valid_month(month)
        start_date, end_date = get_month_bounds(month)
        pipeline = build_department_summary_pipeline(start_date, end_date, department)
        raw_exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_mongo_doc(raw_exp)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        parse_valid_month(month)
        start_date, end_date = get_month_bounds(month)
        pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit or 10, department)
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_doc(raw_exp)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required",
            )
        d_from = parse_valid_date(from_date)
        d_to = parse_valid_date(to_date)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid date range",
            )
        from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
        to_dt_next_day = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)
        pipeline = build_department_trend_pipeline(department, from_date, to_date, from_dt, to_dt_next_day)
        raw_exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_doc(raw_exp)}

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown endpoint")
