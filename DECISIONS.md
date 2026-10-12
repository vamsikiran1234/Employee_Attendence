# DECISIONS.md

1. **Indexes.**
   On `employees`: unique `{ emp_code: 1 }` guarantees employee key uniqueness and $O(1)$ lookups; compound `{ department: 1, joined_on: 1 }` speeds up department filtering and R9 headcount evaluations.
   On `attendance_logs`: unique compound `{ emp_code: 1, date: 1 }` enforces the natural key constraint (at most one record per employee per day), guarantees race-safe punch-ins, and makes `PATCH` lookups $O(1)$; `{ emp_code: 1, punch_in: -1 }` supports punch-out and monthly lookups; `{ date: -1, emp_code: 1 }` backs `GET /attendance` pagination without in-memory sorting; `{ date: 1, late_minutes: 1, emp_code: 1 }` backs the monthly late leaderboard.
   *Rejected*: A standalone index on `{ date: 1 }`, because compound indexes prefixed by `date` satisfy range queries via index prefixes without additional index storage and write amplification.

2. **Punch-in race.**
   Two concurrent punch-in requests arrive for the same employee and date. Both calculate attendance date as the same day. MongoDB's WiredTiger engine serializes the inserts on the unique compound index `{ emp_code: 1, date: 1 }`. Exactly one insert succeeds and returns HTTP 201. The second insert triggers a MongoDB `DuplicateKeyError` (code 11000). Our application explicitly catches this exception and returns HTTP 409 Conflict with `"already punched in for this date"`, ensuring zero race window, no duplicates, and no 500 errors.

3. **Ties.**
   When two employees tie on late minutes at `limit` (e.g., both rank 10 with `limit=10`), the leaderboard returns both employees (11 rows total). Our pipeline uses `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), and filters by `{"$match": {"rank": {"$lte": limit}}}` *after* ranking rather than using a naive `$limit` stage. Tied rows are consistently ordered by `emp_code` ascending.

4. **Headcount.**
   The department summary pipeline originates from the `employees` collection filtered by `joined_on <= end_of_month` (R9), and performs a `$lookup` against `attendance_logs` for that month. Employees with zero logs yield an empty logs array. The pipeline groups at the department level, summing 1 per employee for headcount, while calculating attendance and hours strictly over present records. Departments with zero headcount are filtered out.

5. **One thing you would change for 100x the data.**
   At 100x scale (~10 million records), real-time cross-collection aggregations and window operations would saturate database memory and disk I/O. I would introduce pre-aggregated daily/monthly rollup collections (or materialized views updated via MongoDB Change Streams on punch-out and regularization events). Analytics endpoints would query pre-computed rollups in $O(1)$ time rather than scanning millions of raw logs on every request.
