# Design Decisions

1. **Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employees, plus department/code and join-date/department indexes for employee filtering and headcount queries. On attendance, a unique `(emp_code, date)` index prevents duplicate daily records and handles simultaneous punch-ins. Additional date/employee and status/date/employee indexes support attendance filtering and sorting. I would consider a separate index for frequent punch-out lookups if query-plan measurements showed it was needed.

2. **Punch-in race.** Two identical requests can both pass an initial existence check. MongoDB's unique `(emp_code, date)` index allows only one insert. The successful request returns 201; the duplicate-key failure is converted to 409.

3. **Ties.** The leaderboard uses MongoDB `$rank` on total late minutes. If multiple employees tie at the cutoff rank, all employees tied at that rank are returned. This avoids arbitrarily excluding employees with equal totals.

4. **Headcount.** The department summary starts from employees and joins attendance data, rather than counting only attendance records. Employees who joined by the end of the month are therefore included even when they have no attendance logs.

5. **Scaling.** For 100 times more data, I would benchmark the aggregation pipelines with representative data, inspect `explain` plans, and add or adjust indexes based on measured bottlenecks.
