# Code Review

The following defects were identified in the original starter implementation in `app/main_backup.py`.

| # | Where (function / line) | What is wrong | How to notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Calculates the shift start by replacing the punch-in time's clock fields, which can mishandle dates, time zones, and punches before the shift start. Its calculation also does not implement the contract's exact elapsed-minute and grace-period rules reliably. | Test punches around the 10-minute grace boundary and across midnight. | Calculate the shift start from the attendance date and employee shift, compare instants consistently, and apply the specified grace-period rule. |
| 2 | `compute_work_hours` | Uses Python's built-in `round`, which does not guarantee the required half-up rounding behavior. | Test durations whose rounded value is exactly halfway between two hundredths. | Recompute duration from the stored punch timestamps and use the required half-up rounding. |
| 3 | `compute_overtime` | Counts every minute after shift end, without the required 30-minute minimum, and does not correctly model overnight shifts. | Test a punch-out less than 30 minutes after shift end and an overnight shift. | Calculate shift end using the correct attendance date and shift rules; report overtime only when the threshold is reached. |
| 4 | `create_employee` | Checks for duplicate employee codes before inserting, so simultaneous requests can race unless uniqueness is enforced by MongoDB. The timestamp is also created as a naive local datetime. | Send concurrent requests with the same employee code and inspect timestamp consistency. | Enforce employee-code uniqueness with a database index and use consistent UTC timestamps. |
| 5 | `list_employees` | Uses `page * page_size` as the skip amount, so page 1 skips the first page. The total count ignores the department filter, and pagination parameters are not bounded. | Request page 1 and compare totals with and without a department filter. | Use `(page - 1) * page_size`, count matching documents, and validate pagination limits. |
| 6 | `punch_in` | Does not handle an unknown employee safely, uses the server's local timezone for epoch conversion, and treats a timestamp of zero as if no timestamp were supplied. The duplicate check and insert are also vulnerable to races without a unique index. | Try an unknown employee, timestamp zero, and simultaneous duplicate punch-ins. | Validate the employee and timestamp, convert timestamps consistently, and rely on a unique employee/date index to enforce the one-record guarantee. |
| 7 | `list_attendance` | Loads every matching attendance record into application memory before sorting and paginating. It also lacks deterministic employee-code ordering for records sharing a date. | Test with a large collection and multiple employees having records on the same date. | Apply filtering, sorting, counting, skipping, and limiting in MongoDB, with a stable sort order. |
| 8 | Missing contract endpoints | The starter leaves punch-out, regularization, analytics, and query-plan explanation endpoints unimplemented. | Compare the running API with `openapi.yaml`. | Implement the required endpoints and their business rules. |

## Reviewed but not considered a defect

- Returning HTTP 201 for successful employee creation and punch-in is consistent with resource creation.
- Keeping attendance history as an array is consistent with the data model; the important requirement is to append correction entries rather than rewrite prior history.
- A unique MongoDB index is necessary for race-safe uniqueness; an application-level existence check alone is not sufficient.
