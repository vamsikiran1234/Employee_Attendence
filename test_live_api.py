"""Live test suite testing all endpoints against connected MongoDB Atlas."""
import json
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes

# Ensure indexes are initialized on Atlas
init_indexes()

client = TestClient(app)

def run_live_tests():
    print("--- 1. Testing GET /health ---")
    res = client.get("/health")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    assert res.json() == {"status": "ok"}
    print("Health check PASSED: 200 OK")

    print("\n--- 2. Testing GET /employees ---")
    res = client.get("/employees")
    assert res.status_code == 200, res.text
    data = res.json()
    assert "items" in data and "total" in data
    assert data["total"] >= 6
    print(f"List employees PASSED: {len(data['items'])} items, total: {data['total']}")

    print("\n--- 3. Testing POST /employees Duplicate 409 ---")
    dup_body = {
        "emp_code": "EMP0001",
        "name": "Duplicate Tester",
        "email": "dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    }
    res = client.post("/employees", json=dup_body)
    assert res.status_code == 409, f"Expected 409 got {res.status_code}: {res.text}"
    print("Duplicate employee correctly rejected with 409 Conflict")

    print("\n--- 4. Testing GET /attendance ---")
    res = client.get("/attendance")
    assert res.status_code == 200, res.text
    data = res.json()
    assert "items" in data
    print(f"List attendance PASSED: {len(data['items'])} items, total: {data['total']}")

    print("\n--- 5. Testing GET /analytics/employees/EMP0001/monthly ---")
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200, res.text
    emp_monthly = res.json()
    print("Employee monthly response:", json.dumps(emp_monthly, indent=2))
    assert emp_monthly["emp_code"] == "EMP0001"
    assert emp_monthly["month"] == "2026-07"
    assert emp_monthly["working_days"] == 23

    print("\n--- 6. Testing GET /analytics/departments/summary ---")
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200, res.text
    dept_summary = res.json()
    print("Department summary response:", json.dumps(dept_summary, indent=2))
    assert "items" in dept_summary
    assert len(dept_summary["items"]) > 0

    print("\n--- 7. Testing GET /analytics/leaderboard/late ---")
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200, res.text
    leaderboard = res.json()
    print("Leaderboard response:", json.dumps(leaderboard, indent=2))
    assert "items" in leaderboard

    print("\n--- 8. Testing GET /analytics/departments/Engineering/trend ---")
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-15")
    assert res.status_code == 200, res.text
    trend = res.json()
    print(f"Department trend response: {len(trend['items'])} days")
    assert len(trend["items"]) == 15  # Exactly 15 calendar days from 07-01 to 07-15 inclusive

    print("\n--- 9. Testing GET /admin/explain for all 5 endpoints ---")
    endpoints_to_test = [
        ("attendance_list", "/admin/explain/attendance_list?page=1&page_size=20"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-15"),
    ]
    for ep_name, url in endpoints_to_test:
        res = client.get(url)
        assert res.status_code == 200, f"Explain failed for {ep_name}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep_name
        assert "collection" in data
        assert "explain" in data
        print(f"Explain {ep_name} on collection '{data['collection']}': PASSED (200 OK)")

    print("\n--- 10. Testing Attendance Lifecycle: Punch-in, Punch-out, Regularize ---")
    # Punch in for EMP0002 on a new day 2026-07-20
    test_punch_in = {
        "emp_code": "EMP0002",
        "punched_at": 1784520000000, # 2026-07-20 04:00:00 UTC (09:30 IST)
        "status": "PRESENT"
    }
    # Clean up any leftover test record
    db.attendance_logs.delete_one({"emp_code": "EMP0002", "date": "2026-07-20"})

    res = client.post("/attendance/punch-in", json=test_punch_in)
    assert res.status_code == 201, f"Punch in failed: {res.text}"
    p_in_doc = res.json()
    assert p_in_doc["emp_code"] == "EMP0002"
    assert p_in_doc["date"] == "2026-07-20"
    assert p_in_doc["late_minutes"] == 0
    print("Punch-in PASSED (201 Created)")

    # Concurrent/Duplicate punch-in must return 409
    res_dup = client.post("/attendance/punch-in", json=test_punch_in)
    assert res_dup.status_code == 409, f"Expected 409 on duplicate punch-in, got {res_dup.status_code}"
    print("Duplicate punch-in PASSED (409 Conflict)")

    # Punch-out at 18:35 IST (9 hours 5 minutes = 9.08 hours, 5 min overtime < 30 -> 0)
    test_punch_out = {
        "emp_code": "EMP0002",
        "punched_at": 1784552700000 # 2026-07-20 13:05:00 UTC (18:35 IST)
    }
    res_out = client.post("/attendance/punch-out", json=test_punch_out)
    assert res_out.status_code == 200, f"Punch out failed: {res_out.text}"
    p_out_doc = res_out.json()
    assert p_out_doc["work_hours"] == 9.08
    assert p_out_doc["half_day"] is False
    assert p_out_doc["overtime_minutes"] == 0
    print("Punch-out PASSED (200 OK, work_hours=9.08, half_day=False)")

    # Second punch-out must return 409
    res_out_dup = client.post("/attendance/punch-out", json=test_punch_out)
    assert res_out_dup.status_code == 409, f"Expected 409 on already punched out, got {res_out_dup.status_code}"
    print("Duplicate punch-out PASSED (409 Conflict)")

    # Regularize record (manager changes punch_out to 19:15 IST -> 45 min overtime)
    reg_body = {
        "punch_out": 1784555100000, # 19:15 IST
        "reason": "Manager approved extra overtime work",
        "regularized_by": "manager.rajesh"
    }
    res_patch = client.patch("/attendance/EMP0002/2026-07-20", json=reg_body)
    assert res_patch.status_code == 200, f"PATCH failed: {res_patch.text}"
    patched_doc = res_patch.json()
    assert patched_doc["overtime_minutes"] == 45
    assert len(patched_doc["history"]) == 1
    assert patched_doc["history"][0]["by"] == "manager.rajesh"
    print("PATCH /attendance regularize PASSED (200 OK, history entry appended)")

    # Clean up test log
    db.attendance_logs.delete_one({"emp_code": "EMP0002", "date": "2026-07-20"})

    print("\n=======================================================")
    print("ALL LIVE ENDPOINTS AND AGGREGATIONS PASSED ON ATLAS!")
    print("=======================================================")

if __name__ == "__main__":
    run_live_tests()
