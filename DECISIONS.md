# DECISIONS.md

1. **Indexes.**
   We created:
   * `employees`: unique `{"emp_code": 1}` for identity and duplicate prevention; compound `{"department": 1, "joined_on": 1}` for department filtering and headcount rollups.
   * `attendance_logs`: unique compound `{"emp_code": 1, "date": 1}` to strictly enforce one record per employee-day and eliminate punch-in race conditions; compound `{"date": -1, "emp_code": 1}` to back `/attendance` list pagination and date-range filters; `{"date": 1, "late_minutes": 1}` to power the late leaderboard.
   * *Considered and rejected:* An index on `work_hours`, because derived values are aggregated across date ranges rather than filtered or sorted independently.

2. **Punch-in race.**
   Both requests validate the employee and execute `insert_one` with the same `(emp_code, date)`. MongoDB's unique index `{"emp_code": 1, "date": 1}` enforces atomicity at the storage engine level. Exactly one insert commits (returning 201 Created). The other triggers a `DuplicateKeyError`, which our route catches and converts into HTTP 409 Conflict.

3. **Ties.**
   The aggregation uses `$setWindowFields` with standard competition ranking (`$rank: {}`), followed by `$match: {"rank": {"$lte": limit}}`. When two employees tie at the cutoff rank, both are returned (the response can contain more than `limit` rows). Rows are deterministically sorted by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
   The pipeline originates from `employees` (filtering `joined_on <= end_of_month`), performing a `$lookup` into `attendance_logs`. We unwind with `preserveNullAndEmptyArrays: true` and group by department using `{"$addToSet": "$emp_code"}`. Employees with zero logs remain in the employee set and are accurately counted in headcount.

5. **One thing you would change for 100x data.**
   At 100x data volume, we would pre-aggregate daily and monthly departmental metrics into rollup summary collections during punch-out/regularization (or via asynchronous background workers), eliminating dynamic cross-collection `$lookup` and `$densify` calculations at read time.
