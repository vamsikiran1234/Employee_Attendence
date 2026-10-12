# Employee Attendance & Analytics API

A FastAPI and MongoDB service for tracking employee attendance, punch-in/out business rules, audit regularization, and analytics aggregation pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Local instance or MongoDB Atlas free tier)

## Setup & Running

1. **Clone repository & activate virtual environment:**
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .venv\Scripts\Activate.ps1
   # Linux / macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure MongoDB Connection:**
   Create a local `.env` file (or set environment variables):
   ```env
   MONGO_URI=mongodb+srv://<username>:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB=attendance_db
   ```
   *(Note: Real environment variables take precedence over `.env`)*

4. **Seed sample data (optional):**
   ```bash
   python sample_seed.py
   ```

5. **Start application:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

6. **Interactive API Documentation:**
   Open [http://localhost:8000/docs](http://localhost:8000/docs) in your browser.

## Test Suite
Run the test suite verifying business rules R1–R10:
```bash
python test_app.py
```

## Structure
- `app/main.py`: Complete single-file application containing all endpoints, models, helpers, and aggregation pipelines.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect audit of starter code and resolutions (Part A).
- `DECISIONS.md`: Architectural decisions and answers to the five evaluation prompts (Part C).
- `INTERVIEW_GUIDE.md`: Comprehensive walkthrough guide for the technical interview.
