# Employee Attendance & Analytics API

A robust FastAPI and MongoDB backend service implementing employee attendance tracking, punch in/out with business rules, manual corrections with audit history, and high-performance analytics pipelines.

Built for the HROne Software Engineer Trainee technical assignment.

---

## Prerequisites
* Python 3.11+
* MongoDB 6.0+ (MongoDB Atlas or local MongoDB)

---

## Quickstart

1. **Clone repository and install dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Create a `.env` file (or set environment variables):
   ```env
   MONGO_URI=mongodb+srv://<username>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB=attendance_db
   ```
   *(Local Docker or local MongoDB instance e.g. `mongodb://localhost:27017` also works).*

3. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Run the API server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

---

## API Overview

* **System & Health:**
  * `GET /health`: Readiness probe (returns 200 when MongoDB responds, 503 if unreachable).
* **Employees:**
  * `POST /employees`: Create employee (enforces unique `emp_code`, pattern validations, and shift constraints).
  * `GET /employees`: Paginated employee list sorted by `emp_code` ascending with department filtering.
* **Attendance:**
  * `POST /attendance/punch-in`: Race-safe punch-in creating daily record adhering to R1 (IST timezone, overnight shifts) and R2 (10-minute grace period).
  * `POST /attendance/punch-out`: Closes record computing work hours (R4 round half-up), overtime (R3 threshold >= 30m), and half-day status (R5).
  * `GET /attendance`: Paginated attendance list sorted by date descending, emp_code ascending, backed by indexes.
  * `PATCH /attendance/{emp_code}/{date}`: Manual regularization recomputing derived fields and appending audit trail to `history`.
* **Analytics (MongoDB Aggregation Pipelines):**
  * `GET /analytics/employees/{emp_code}/monthly`: Monthly summary (working days from `joined_on`, present days Mon-Fri, attendance percentage).
  * `GET /analytics/departments/summary`: Department metrics with headcount including zero-log employees (R9) and average work hours.
  * `GET /analytics/leaderboard/late`: Late-comers competition ranking with ties handled via `$setWindowFields` (`$rank`).
  * `GET /analytics/departments/{department}/trend`: Daily series with gap filling (`$densify`) and 7-day moving average.
* **Admin:**
  * `GET /admin/explain/{endpoint}`: Raw MongoDB `executionStats` explain plan proving `IXSCAN` usage without collection scans.

---

## Key Files
* [`app/main.py`](app/main.py): Complete application implementation.
* [`tests/test_api.py`](tests/test_api.py): Comprehensive automated test suite.
* [`requirements.txt`](requirements.txt): Python dependencies.
* [`REVIEW.md`](REVIEW.md): Detailed defect review of the starter codebase.
* [`DECISIONS.md`](DECISIONS.md): Architectural decisions and indexing rationale.
* [`openapi.yaml`](openapi.yaml): API contract and business rule specifications.
* [`render.yaml`](render.yaml): Render Blueprint for automated web service deployment.

---

## Testing

Run the automated test suite using an isolated test database:
```bash
python -m pytest tests/test_api.py -v
```

---

## Deployment (Render)

The project includes a [`render.yaml`](render.yaml) blueprint for one-click deployment:

1. **Build Command:** `pip install -r requirements.txt`
2. **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. **Environment Variables:**
   * `MONGO_URI`: MongoDB Atlas connection string (configured securely as a secret).
   * `MONGO_DB`: `attendance_db`
   * `PYTHON_VERSION`: `3.11.9`
