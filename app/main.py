"""
Employee Attendance & Analytics API

Run from project root:
    uvicorn app.main:app --reload --port 8000

Environment:
    MONGO_URI=mongodb://localhost:27017
    MONGO_DB=attendance_db

All timestamps accepted/returned by the API are epoch milliseconds.
Attendance calendar dates and shift times use IST (UTC+05:30).
"""

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import ObjectId
from bson.timestamp import Timestamp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from datetime import date, datetime
from bson import ObjectId
from bson.timestamp import Timestamp
from bson.binary import Binary
from bson.decimal128 import Decimal128
from base64 import b64encode

# Do not override real environment variables with .env values.
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=10_000,
)
db = client[MONGO_DB]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# -------------------------- JSON-safe serialization --------------------------

def make_json_safe(value):
    if isinstance(value, dict):
        return {
            str(key): make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]

    if isinstance(value, Timestamp):
        return {
            "time": value.time,
            "inc": value.inc
        }

    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal128):
        return str(value.to_decimal())

    if isinstance(value, (Binary, bytes, bytearray, memoryview)):
        return {
            "binary_base64": b64encode(bytes(value)).decode("ascii")
        }

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    # Fallback for any other BSON or driver-specific value.
    return str(value)


# ------------------------------- Validation --------------------------------

def parse_calendar_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    if not 1 <= month <= 12:
        raise HTTPException(422, "month must be YYYY-MM")
    return year, month


def validate_epoch_ms(value: Optional[int], field_name: str = "punched_at") -> Optional[int]:
    if value is None:
        return None
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, f"{field_name} must be epoch milliseconds")
    return value


def epoch_ms_to_datetime(value: int) -> datetime:
    validate_epoch_ms(value)
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp()) * 1000


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def is_working_day(day: date) -> bool:
    return day.weekday() < 5


def shift_clock(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)



def attendance_date_for_punch(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    local = to_ist(punch_in)
    local_day = local.date()

    # If the shift crosses midnight, an early-morning punch
    # before shift_end belongs to the previous attendance date.
    if shift_end < shift_start and local.strftime("%H:%M") < shift_end:
        local_day -= timedelta(days=1)

    return local_day.isoformat()



def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quant = Decimal("1").scaleb(-places)
    result = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return float(result)


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_day: str) -> int:
    if punch_in is None:
        return 0
    start_day = parse_calendar_date(attendance_day)
    start = shift_clock(start_day, shift_start).astimezone(UTC)
    actual = punch_in.astimezone(UTC).replace(microsecond=0)
    elapsed_seconds = int((actual - start).total_seconds())
    # More than 10 minutes after start is late; minutes are measured from start.
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_overtime(punch_out: Optional[datetime], shift_start: str, shift_end: str, attendance_day: str) -> int:
    if punch_out is None:
        return 0
    day = parse_calendar_date(attendance_day)
    end = shift_clock(day, shift_end)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    elapsed_seconds = int(
        (punch_out.astimezone(UTC).replace(microsecond=0) - end.astimezone(UTC)).total_seconds()
    )
    minutes = max(0, elapsed_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int(
        (punch_out.astimezone(UTC).replace(microsecond=0) -
         punch_in.astimezone(UTC).replace(microsecond=0)).total_seconds()
    )
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_attendance_fields(record: dict, employee: dict) -> dict:
    punch_in = record.get("punch_in")
    punch_out = record.get("punch_out")
    status = record.get("status")
    if status not in PRESENCE_STATUSES:
        punch_in = None
        punch_out = None
    record["work_hours"] = compute_work_hours(punch_in, punch_out)
    record["late_minutes"] = (
        compute_late_minutes(punch_in, employee["shift_start"], record["date"])
        if status in PRESENCE_STATUSES else 0
    )
    record["overtime_minutes"] = (
        compute_overtime(punch_out, employee["shift_start"], employee["shift_end"], record["date"])
        if status in PRESENCE_STATUSES else 0
    )
    record["half_day"] = bool(
        record["work_hours"] is not None and record["work_hours"] < 4.50
    )
    return record


def serialize_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        doc[field] = datetime_to_epoch_ms(doc.get(field))
    history = []
    for entry in doc.get("history", []):
        item = dict(entry)
        item["at"] = datetime_to_epoch_ms(item.get("at"))
        changes = {}
        for key, value in item.get("changes", {}).items():
            changes[key] = datetime_to_epoch_ms(value) if key in {"punch_in", "punch_out"} else value
        item["changes"] = changes
        history.append(item)
    doc["history"] = history
    return doc


def serialize_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc.get("created_at"))
    return doc


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def get_attendance(emp_code: str, attendance_day: str) -> dict:
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_day})
    if not record:
        raise HTTPException(404, "attendance record not found")
    return record


# --------------------------------- Models ----------------------------------

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeCreate(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("time must be HH:MM in 24-hour format")
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def strict_epoch(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("punched_at must be an integer epoch-millisecond value")
        return value

    @field_validator("punched_at")
    @classmethod
    def epoch_range(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutRequest(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def strict_epoch(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("punched_at must be an integer epoch-millisecond value")
        return value

    @field_validator("punched_at")
    @classmethod
    def epoch_range(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeRequest(StrictModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def strict_epoch(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("punch values must be integer epoch milliseconds")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def epoch_range(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punch values must be epoch milliseconds")
        return value


# ------------------------------- Indexes -----------------------------------

def create_indexes() -> None:
    """Idempotently create indexes used by uniqueness, filters, and analytics."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_employee",
    )


@app.on_event("startup")
def startup_create_indexes():
    create_indexes()


# -------------------------------- Endpoints --------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(x) for x in cursor],
        "total": total, "page": page, "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee(body.emp_code)
    punched = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    attendance_day = attendance_date_for_punch(punched, emp["shift_start"], emp["shift_end"])
    record = {
        "emp_code": body.emp_code, "date": attendance_day, "status": body.status,
        "punch_in": punched, "punch_out": None, "work_hours": None,
        "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
    }
    derive_attendance_fields(record, emp)
    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance record already exists for this employee and date")
    return serialize_attendance(record)



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee(body.emp_code)

    punched = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc_seconds()
    )

    # Find the most recent punch-in at or before this timestamp.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        # A future punch-in exists, but the requested punch-out
        # is not after it.
        future_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$gt": punched},
            },
            sort=[("punch_in", 1)],
        )

        if future_record is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )

        raise HTTPException(
            status_code=404,
            detail="attendance record not found for punch-out",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="attendance record already punched out",
        )

    punch_in_time = record["punch_in"]

    duration_seconds = (punched - punch_in_time).total_seconds()

    if duration_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in",
        )

    if duration_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punch duration cannot exceed 24 hours",
        )

    record["punch_out"] = punched
    derive_attendance_fields(record, emp)

    # Atomically close the record only if it is still open.
    result = db.attendance_logs.update_one(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_in": punch_in_time,
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": record["punch_out"],
                "work_hours": record["work_hours"],
                "late_minutes": record["late_minutes"],
                "overtime_minutes": record["overtime_minutes"],
                "half_day": record["half_day"],
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="attendance record was already punched out",
        )

    return serialize_attendance(record)



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            parse_calendar_date(date_from, "date_from")
            query["date"]["$gte"] = date_from
        if date_to:
            parse_calendar_date(date_to, "date_to")
            query["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(x) for x in cursor],
        "total": total, "page": page, "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{attendance_day}")
def regularize_attendance(emp_code: str, attendance_day: str, body: RegularizeRequest):
    parse_calendar_date(attendance_day)
    emp = get_employee(emp_code)
    record = get_attendance(emp_code, attendance_day)

    new_status = body.status if body.status is not None else record["status"]
    new_in = (
        epoch_ms_to_datetime(body.punch_in)
        if body.punch_in is not None else record.get("punch_in")
    )
    new_out = (
        epoch_ms_to_datetime(body.punch_out)
        if body.punch_out is not None else record.get("punch_out")
    )


    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot include punch times",
            )
        new_in, new_out = None, None
    elif new_in is None:
        raise HTTPException(
            status_code=422,
            detail="presence statuses require punch_in",
        )

    if new_in is not None:
        punch_in_local = to_ist(new_in)
    if punch_in_local.date().isoformat() != attendance_day:
        raise HTTPException(
            status_code=422,
            detail="punch_in must be on the attendance date",
        )

    if new_in is not None and new_out is not None:
        duration = (new_out - new_in).total_seconds()

        if duration <= 0:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )

    if duration > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punch duration cannot exceed 24 hours",
        )


    updated = dict(record)
    updated["status"] = new_status
    updated["punch_in"] = new_in
    updated["punch_out"] = new_out
    derive_attendance_fields(updated, emp)

    changes = {}
    for key in (
        "status", "punch_in", "punch_out", "work_hours",
        "late_minutes", "overtime_minutes", "half_day",
    ):
        before = record.get(key)
        after = updated.get(key)
        if before != after:
            changes[key] = after


        if not changes:
            raise HTTPException(
            status_code=422,
            detail="No changes to apply",
        )


    history_entry = {
        "at": now_utc_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    db.attendance_logs.update_one(
        {"emp_code": emp_code, "date": attendance_day},
        {
            "$set": {
                "status": updated["status"],
                "punch_in": updated["punch_in"],
                "punch_out": updated["punch_out"],
                "work_hours": updated["work_hours"],
                "late_minutes": updated["late_minutes"],
                "overtime_minutes": updated["overtime_minutes"],
                "half_day": updated["half_day"],
            },
            "$push": {"history": history_entry},
        },
    )
    updated["history"] = list(record.get("history", [])) + [history_entry]
    return serialize_attendance(updated)


# ------------------------------- Analytics ---------------------------------

def month_bounds(month: str) -> tuple[date, date]:
    year, mon = parse_month(month)
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first, last


def working_days_between(start: date, end: date) -> int:
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if is_working_day(start + timedelta(days=i)))


def all_month_attendance(emp_code: str, month: str) -> list[dict]:
    first, last = month_bounds(month)
    return list(db.attendance_logs.find({
        "emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}
    }))



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)

    joined_on = date.fromisoformat(emp["joined_on"])
    start = max(first, joined_on)

    working_days = (
        working_days_between(start, last)
        if start <= last
        else 0
    )

    # Aggregate attendance metrics inside MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
            }
        },
        {
            "$set": {
                "_attendance_date": {
                    "$dateFromString": {"dateString": "$date"}
                },
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_overtime": {"$ifNull": ["$overtime_minutes", 0]},
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
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": "$_attendance_date"
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$_late", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_overtime"},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))

    metrics = result[0] if result else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = metrics["present_days"]
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(metrics["leave_days"]),
        "late_count": int(metrics["late_count"]),
        "total_late_minutes": int(metrics["total_late_minutes"]),
        "total_overtime_minutes": int(metrics["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }




@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    first, last = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "joined_on": {"$lte": last.isoformat()}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            first.isoformat(),
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            last.isoformat(),
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_is_working_day": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$eq": [
                                            "$attendance._is_working_day",
                                            True,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            "$attendance.half_day",
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$attendance.work_hours",
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$attendance.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$attendance.work_hours",
                                            None,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$attendance.late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$attendance.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$attendance.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        count = row["work_hours_count"]
        average = (
            round_half_up(row["work_hours_sum"] / count, 2)
            if count > 0
            else None
        )

        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": average,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=100),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
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
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_calendar_date(from_date, "from")
    end = parse_calendar_date(to_date, "to")

    if start > end:
        raise HTTPException(
            status_code=422,
            detail="from must be on or before to",
        )

    if (end - start).days > 91:
        raise HTTPException(
            status_code=422,
            detail="trend range cannot exceed 92 calendar days",
        )

    # A department must exist, even if its employees joined later.
    department_exists = db.employees.find_one(
        {"department": department},
        {"_id": 0, "emp_code": 1, "department": 1}
    )

    if department_exists is None:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department}' not found"
        )

    start_iso = start.isoformat()
    end_iso = end.isoformat()
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())

    start_date_expr = {
        "$dateFromString": {"dateString": start_iso}
    }
    end_date_expr = {
        "$dateFromString": {"dateString": end_iso}
    }

    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": {
                            "$dateFromString": {
                                "dateString": "$joined_on"
                            }
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "employees": 1,
                "day": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": start_date_expr,
                                                "endDate": end_date_expr,
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_date_expr,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$day"},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$set": {
                "day_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                    }
                },
                "active_codes": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$employees",
                                "as": "employee",
                                "cond": {
                                    "$lte": [
                                        "$$employee.joined_on",
                                        "$day",
                                    ]
                                },
                            }
                        },
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$active_codes"},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$day"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day_string": "$day_string",
                    "codes": "$active_codes",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day_string"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "daily_records",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$reduce": {
                        "input": "$daily_records",
                        "initialValue": 0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$$this.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$$this.half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$daily_records",
                            "as": "record",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$record.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$day_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving_avg = row.get("moving_avg_7d")

        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": (
                round_half_up(rate, 4) if rate is not None else None
            ),
            "moving_avg_7d": (
                round_half_up(moving_avg, 4)
                if moving_avg is not None
                else None
            ),
        })

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": items,
    }



# -------------------------- Query-plan diagnostics -------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    try:
        if endpoint == "attendance_list":
            query = {}
            if emp_code:
                query["emp_code"] = emp_code
            if status:
                if status not in ALL_STATUSES:
                    raise HTTPException(422, "status must be PRESENT, WFH, ON_DUTY, ABSENT, or LEAVE")
                query["status"] = status
            if date_from or date_to:
                query["date"] = {}
                if date_from:
                    parse_calendar_date(date_from, "date_from")
                    query["date"]["$gte"] = date_from
                if date_to:
                    parse_calendar_date(date_to, "date_to")
                    query["date"]["$lte"] = date_to
                if date_from and date_to and date_from > date_to:
                    raise HTTPException(422, "date_from must be on or before date_to")
            plan = db.command("explain", {
                "find": "attendance_logs", "filter": query,
                "sort": {"date": -1, "emp_code": 1}, "limit": 20,
            }, verbosity="executionStats")

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(422, "emp_code and month are required")
            first, last = month_bounds(month)
            plan = db.command("explain", {
                "find": "attendance_logs",
                "filter": {
                    "emp_code": emp_code,
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                },
            }, verbosity="executionStats")

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required")
            first, last = month_bounds(month)
            plan = db.command("explain", {
                "aggregate": "attendance_logs",
                "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
                "cursor": {},
            }, verbosity="executionStats")


        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required")

            first, last = month_bounds(month)

            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": first.isoformat(),
                            "$lte": last.isoformat(),
                        },
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
                    "$sort": {
                        "total_late_minutes": -1,
                        "late_count": -1,
                        "_id": 1,
                    }
                },
                {"$limit": 10},
            ]

            plan = db.command(
                "explain",
                {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                verbosity="executionStats",
            )


        else:  # department_trend
            if not department or not from_date or not to_date:
                raise HTTPException(422, "department, from and to are required")
            start = parse_calendar_date(from_date, "from")
            end = parse_calendar_date(to_date, "to")
            if start > end:
                raise HTTPException(422, "from must be on or before to")
            if (end - start).days > 366:
                raise HTTPException(422, "trend range cannot exceed 367 calendar days")
            codes = [
                e["emp_code"]
                for e in db.employees.find(
                    {"department": department, "joined_on": {"$lte": end.isoformat()}},
                    {"_id": 0, "emp_code": 1},
                )
            ]
            plan = db.command("explain", {
                "find": "attendance_logs",
                "filter": {
                    "emp_code": {"$in": codes},
                    "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                },
            }, verbosity="executionStats")

        return make_json_safe(plan)

    except HTTPException:
        raise
    except PyMongoError as exc:
        # Avoid leaking connection strings or internal configuration in API responses.
        raise HTTPException(503, f"MongoDB explain failed: {exc.__class__.__name__}") from exc
