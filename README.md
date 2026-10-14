# Employee Attendance & Analytics API

A REST API built with Python, FastAPI, and MongoDB for employee attendance, punch-in/punch-out, manual corrections with audit history, and attendance analytics.

## Requirements

- Python 3.10 or newer
- MongoDB 6.0 or newer, or MongoDB Atlas
- PowerShell on Windows

## Setup

1. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Create a local `.env` file in the project root containing your own MongoDB settings:

   ```text
   MONGO_URI=your_mongodb_connection_string
   MONGO_DB=attendance_db
   ```

   Never commit `.env` or share database credentials.

4. Load the sample data if needed:

   ```powershell
   python sample_seed.py
   ```

5. Start the API:

   ```powershell
   python -m uvicorn app.main:app --port 8000 --reload
   ```

6. Open the interactive API documentation at http://127.0.0.1:8000/docs.

## Project files

- `app/main.py` — API implementation
- `requirements.txt` — Python dependencies
- `openapi.yaml` — API contract
- `DATA_MODEL.md` — MongoDB data model
- `REVIEW.md` — starter-code review
- `DECISIONS.md` — implementation decisions
- `sample_data/` and `sample_seed.py` — sample data and seeding utility

## Submission

Keep `.env`, virtual environments, caches, and credentials out of the repository. Submit the required source code, dependency file, review, decisions, and README.
