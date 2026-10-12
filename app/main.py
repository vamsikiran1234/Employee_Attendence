"""
Employee Attendance & Analytics API - Production Implementation
Contract: openapi.yaml (v2.0.0) | Data Model: DATA_MODEL.md | Rules: R1 - R10
"""
from __future__ import annotations

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Priority: real environment variables override local .env
load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

        # attendance_logs collection
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
    except Exception as e:
        # Do not crash startup if MongoDB is unreachable yet; health check handles readiness
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Pure Helper Functions (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Converts epoch milliseconds to UTC datetime truncated to whole seconds."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return truncate_to_seconds(dt)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts UTC BSON datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(val: Optional[float | Decimal], places: int = 2) -> Optional[float]:
    """R4 / R8: Precise half-up rounding (not banker's rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_time_str = punch_in_ist.strftime("%H:%M")
    ist_date = punch_in_ist.date()

    if shift_end <= shift_start:
        if ist_time_str < shift_end:
            return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def get_shift_instants(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    Returns UTC start and end datetimes for a shift given the attendance date (IST).
    For overnight shifts, shift_end falls on the next calendar day in IST.
    """
    att_date = datetime.fromisoformat(date_str).date()
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))

    start_ist = datetime(att_date.year, att_date.month, att_date.day, h_s, m_s, 0, tzinfo=IST)
    if shift_end <= shift_start:
        next_date = att_date + timedelta(days=1)
        end_ist = datetime(next_date.year, next_date.month, next_date.day, h_e, m_e, 0, tzinfo=IST)
    else:
        end_ist = datetime(att_date.year, att_date.month, att_date.day, h_e, m_e, 0, tzinfo=IST)

    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def compute_late_minutes(punch_in_utc: datetime, shift_start_utc: datetime) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start;
    then whole minutes (floor) since shift_start.
    """
    diff_sec = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    diff_sec = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def is_working_day(date_str: str) -> bool:
    """R7: Monday - Friday (weekday 0..4)."""
    d = datetime.fromisoformat(date_str).date()
    return d.weekday() < 5


def get_month_range(month_str: str) -> tuple[str, str, int]:
    """Returns (start_date, end_date, last_day_int) for YYYY-MM."""
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{last_day:02d}", last_day


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Count Mon-Fri calendar days in month.
    For mid-month joiners, counted from joined_on.
    """
    start_date_str, end_date_str, last_day = get_month_range(month_str)
    if joined_on_str > end_date_str:
        return 0

    eff_start_str = max(start_date_str, joined_on_str)
    curr = datetime.fromisoformat(eff_start_str).date()
    end_d = datetime.fromisoformat(end_date_str).date()

    count = 0
    while curr <= end_d:
        if curr.weekday() < 5:
            count += 1
        curr += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Pydantic Request / Response Schemas
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    created_at_ms = datetime_to_epoch_ms(doc.get("created_at"))
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_history_entry(h: dict) -> dict:
    at_val = h.get("at")
    if isinstance(at_val, datetime):
        at_val = datetime_to_epoch_ms(at_val)

    changes = {}
    for k, v in h.get("changes", {}).items():
        c_from = v.get("from")
        c_to = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(c_from, datetime):
                c_from = datetime_to_epoch_ms(c_from)
            if isinstance(c_to, datetime):
                c_to = datetime_to_epoch_ms(c_to)
        changes[k] = {"from": c_from, "to": c_to}

    return {
        "at": at_val,
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def serialize_attendance_record(doc: dict) -> dict:
    pi = doc.get("punch_in")
    if isinstance(pi, datetime):
        pi = datetime_to_epoch_ms(pi)

    po = doc.get("punch_out")
    if isinstance(po, datetime):
        po = datetime_to_epoch_ms(po)

    history = [serialize_history_entry(entry) for entry in doc.get("history", [])]

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": pi,
        "punch_out": po,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: 200 only when MongoDB answers; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Creates an employee. Race-safe unique index handling on emp_code."""
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees paginated, filtered by department, sorted by emp_code asc."""
    q: dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Creates attendance day record (R1, R2). Status: PRESENT, WFH, or ON_DUTY.
    Race-safe: unique constraint on (emp_code, date) ensures exactly one 201 and concurrent 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_utc = truncate_to_seconds(datetime.now(timezone.utc))

    att_date_str = determine_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    shift_start_utc, _ = get_shift_instants(att_date_str, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, shift_start_utc)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date_str,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work hours (R4), overtime (R3), and half day (R5).
    Two simultaneous punch-outs yield exactly one 200 and the other 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_utc = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record whose punch_in is <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")

    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    _, shift_end_utc = get_shift_instants(record["date"], emp["shift_start"], emp["shift_end"])

    w_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_min = compute_overtime(punch_out_utc, shift_end_utc)
    h_day = is_half_day(w_hours)

    # Atomic update ensuring race safety between simultaneous punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": w_hours,
                "overtime_minutes": ot_min,
                "half_day": h_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records backed by MongoDB index cursor operations."""
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort("date", DESCENDING).skip(skip).limit(page_size)
    items = [serialize_attendance_record(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and atomically appends entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Determine final values
    final_status = body.status if body.status is not None else record["status"]
    final_punch_in = (
        epoch_ms_to_datetime(body.punch_in)
        if body.punch_in is not None
        else record.get("punch_in")
    )
    final_punch_out = (
        epoch_ms_to_datetime(body.punch_out)
        if body.punch_out is not None
        else record.get("punch_out")
    )

    # Validation rules
    if final_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times supplied")
        final_punch_in = None
        final_punch_out = None

    if final_status in ("PRESENT", "WFH", "ON_DUTY"):
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    if final_punch_in is not None:
        calc_date = determine_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Cannot have punch_out without punch_in")
        if final_punch_out <= final_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Compute new derived values
    shift_start_utc, shift_end_utc = get_shift_instants(date, emp["shift_start"], emp["shift_end"])
    if final_status in ("ABSENT", "LEAVE"):
        final_late_min = 0
        final_work_hours = None
        final_ot_min = 0
        final_half_day = False
    else:
        final_late_min = compute_late_minutes(final_punch_in, shift_start_utc)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_ot_min = compute_overtime(final_punch_out, shift_end_utc)
            final_half_day = is_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_ot_min = 0
            final_half_day = False

    # Compute changes object
    changes: dict[str, dict[str, Any]] = {}

    def track_change(key: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes[key] = {"from": old_val, "to": new_val}

    track_change("status", record.get("status"), final_status)
    track_change("punch_in", record.get("punch_in"), final_punch_in)
    track_change("punch_out", record.get("punch_out"), final_punch_out)
    track_change("work_hours", record.get("work_hours"), final_work_hours)
    track_change("late_minutes", record.get("late_minutes", 0), final_late_min)
    track_change("overtime_minutes", record.get("overtime_minutes", 0), final_ot_min)
    track_change("half_day", bool(record.get("half_day", False)), final_half_day)

    if not changes:
        raise HTTPException(status_code=422, detail="Regularization request changes nothing")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking: check current history length / identity to avoid lost updates
    current_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(current_history)},
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_min,
                "overtime_minutes": final_ot_min,
                "half_day": final_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification detected on attendance record")

    return serialize_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start_d, end_d, _ = get_month_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_d, "$lte": end_d},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_ot = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    start_d, end_d, _ = get_month_range(month)
    match_stage: dict[str, Any] = {"joined_on": {"$lte": end_d}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_codes"]},
                                    {"$gte": ["$date", start_d]},
                                    {"$lte": ["$date", end_d]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list.work_hours"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month cross-collection aggregation."""
    pipeline = build_department_summary_pipeline(month, department)
    items = list(db.employees.aggregate(pipeline))

    for item in items:
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


def build_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict]:
    start_d, end_d, _ = get_month_range(month)
    pipeline: list[dict] = [
        {
            "$match": {
                "date": {"$gte": start_d, "$lte": end_d},
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
        {"$sort": {"total_late_minutes": -1, "emp_code": ASCENDING}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with competition ranking ($rank) and tie cutoff retention."""
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_date_str: str,
    to_date_str: str,
    joined_on_dates: list[str],
) -> list[dict]:
    from_dt = datetime.fromisoformat(from_date_str).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date_str).replace(tzinfo=timezone.utc)
    to_dt_plus_one = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date_str, "$lte": to_date_str}
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
                "_id": "$date",
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
            "$project": {
                "date_str": "$_id",
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_str": {"$literal": from_date_str},
                            "date_obj": {"$literal": from_dt},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date_obj",
                "date_str": {"$first": "$date_str"},
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_one],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_on_dates,
                            "as": "j",
                            "cond": {"$lte": ["$$j", {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend with database-level gap filling ($densify) and 7-day moving average."""
    from_d = datetime.fromisoformat(from_date).date()
    to_d = datetime.fromisoformat(to_date).date()

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")

    if (to_d - from_d).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    joined_on_dates = [e["joined_on"] for e in dept_emps if "joined_on" in e]
    pipeline = build_department_trend_pipeline(department, from_date, to_date, joined_on_dates)
    items = list(db.attendance_logs.aggregate(pipeline))

    for row in items:
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoint: Admin Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Runs MongoDB explain with executionStats verbosity for an endpoint's main query or pipeline.
    Validates required query parameters for each endpoint with 422 if missing.
    """
    valid_endpoints = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint: {endpoint}")

    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_output = db.attendance_logs.find(q).sort("date", DESCENDING).skip(skip).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="'emp_code' and 'month' are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_output = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="'month' is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        explain_output = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_output}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="'month' is required for late_leaderboard")
        pipeline = build_leaderboard_pipeline(month, limit or 10, department)
        explain_output = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'department', 'from', and 'to' are required for department_trend")
        dept_emps = list(db.employees.find({"department": department}, {"joined_on": 1}))
        joined_on_dates = [e["joined_on"] for e in dept_emps if "joined_on" in e]
        pipeline = build_department_trend_pipeline(department, from_date, to_date, joined_on_dates)
        explain_output = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_output}
