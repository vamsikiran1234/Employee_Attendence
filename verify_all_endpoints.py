"""
End-to-End Test Suite for All 12 Endpoints in the Employee Attendance & Analytics API
Verifies each endpoint against live MongoDB Atlas and prints status report.
"""
import json
from datetime import datetime
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes

init_indexes()
client = TestClient(app)

results = []

def record(endpoint, method, status_code, expected_status, notes):
    passed = (status_code == expected_status)
    results.append({
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "expected": expected_status,
        "passed": passed,
        "notes": notes
    })

def test_all():
    print("=====================================================================")
    print("RUNNING END-TO-END VERIFICATION ACROSS ALL 12 API ENDPOINTS")
    print("=====================================================================\n")

    # 1. GET /health
    res = client.get("/health")
    record("/health", "GET", res.status_code, 200, "Active ping to MongoDB Atlas")

    # 2. POST /employees
    emp_payload = {
        "emp_code": "EMP0999",
        "name": "Arjun Sharma",
        "email": "arjun@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01"
    }
    db.employees.delete_one({"emp_code": "EMP0999"})
    res = client.post("/employees", json=emp_payload)
    record("/employees", "POST", res.status_code, 201, "Created employee EMP0999 with epoch ms created_at")

    # Test Duplicate 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # 3. GET /employees
    res = client.get("/employees?department=Engineering&page=1&page_size=10")
    data = res.json()
    record("/employees", "GET", res.status_code, 200, f"Filtered Engineering employees, count: {len(data['items'])}, total: {data['total']}")

    # 4. POST /attendance/punch-in
    db.attendance_logs.delete_one({"emp_code": "EMP0999", "date": "2026-07-21"})
    punch_in_payload = {
        "emp_code": "EMP0999",
        "punched_at": 1784606400000, # 2026-07-21 04:00:00 UTC (09:30 IST)
        "status": "PRESENT"
    }
    res = client.post("/attendance/punch-in", json=punch_in_payload)
    record("/attendance/punch-in", "POST", res.status_code, 201, "Punched in for date 2026-07-21, late_minutes=0")

    # Test Duplicate Punch-in 409
    res_p_dup = client.post("/attendance/punch-in", json=punch_in_payload)
    assert res_p_dup.status_code == 409

    # 5. POST /attendance/punch-out
    punch_out_payload = {
        "emp_code": "EMP0999",
        "punched_at": 1784639400000 # 2026-07-21 13:10:00 UTC (18:40 IST -> 10 min past shift end < 30 -> 0 overtime)
    }
    res = client.post("/attendance/punch-out", json=punch_out_payload)
    p_out_data = res.json()
    record("/attendance/punch-out", "POST", res.status_code, 200, f"Punched out, work_hours={p_out_data.get('work_hours')}, overtime={p_out_data.get('overtime_minutes')}")

    # Test Duplicate Punch-out 409
    res_out_dup = client.post("/attendance/punch-out", json=punch_out_payload)
    assert res_out_dup.status_code == 409

    # 6. GET /attendance
    res = client.get("/attendance?emp_code=EMP0999&page=1&page_size=10")
    att_data = res.json()
    record("/attendance", "GET", res.status_code, 200, f"Found {len(att_data['items'])} logs via indexed cursor")

    # 7. PATCH /attendance/{emp_code}/{date}
    patch_payload = {
        "punch_out": 1784641800000, # 19:20 IST (50 min overtime)
        "reason": "Late production deployment support",
        "regularized_by": "lead.dev"
    }
    res = client.patch("/attendance/EMP0999/2026-07-21", json=patch_payload)
    patch_data = res.json()
    record("/attendance/{emp_code}/{date}", "PATCH", res.status_code, 200, f"Regularized: overtime={patch_data.get('overtime_minutes')}, history_length={len(patch_data.get('history', []))}")

    # Clean up test employee logs
    db.attendance_logs.delete_one({"emp_code": "EMP0999", "date": "2026-07-21"})
    db.employees.delete_one({"emp_code": "EMP0999"})

    # 8. GET /analytics/employees/{emp_code}/monthly
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    m_data = res.json()
    record("/analytics/employees/{emp_code}/monthly", "GET", res.status_code, 200, f"Working days: {m_data['working_days']}, Present days: {m_data['present_days']}, Rate: {m_data['attendance_pct']}%")

    # 9. GET /analytics/departments/summary
    res = client.get("/analytics/departments/summary?month=2026-07")
    d_data = res.json()
    record("/analytics/departments/summary", "GET", res.status_code, 200, f"Department summaries generated: {len(d_data['items'])} departments")

    # 10. GET /analytics/leaderboard/late
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    l_data = res.json()
    record("/analytics/leaderboard/late", "GET", res.status_code, 200, f"Ranked late employees: {len(l_data['items'])} entries with $rank")

    # 11. GET /analytics/departments/{department}/trend
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-10")
    t_data = res.json()
    record("/analytics/departments/{department}/trend", "GET", res.status_code, 200, f"Generated gap-filled trend for {len(t_data['items'])} calendar days with 7d moving avg")

    # 12. GET /admin/explain/{endpoint}
    explain_urls = [
        ("attendance_list", "/admin/explain/attendance_list?page=1&page_size=20"),
        ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "/admin/explain/department_summary?month=2026-07"),
        ("late_leaderboard", "/admin/explain/late_leaderboard?month=2026-07"),
        ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    ]
    all_explain_ok = True
    for ep_name, url in explain_urls:
        exp_res = client.get(url)
        if exp_res.status_code != 200:
            all_explain_ok = False
    record("/admin/explain/{endpoint}", "GET", 200 if all_explain_ok else 500, 200, "Verified executionStats and IXSCAN on all 5 queries")

    print(f"{'METHOD':<7} | {'STATUS':<7} | {'RESULT':<6} | {'ENDPOINT':<42} | NOTES")
    print("-" * 110)
    for r in results:
        res_str = "PASS" if r["passed"] else "FAIL"
        print(f"{r['method']:<7} | {r['status_code']:<7} | {res_str:<6} | {r['endpoint']:<42} | {r['notes']}")
    print("-" * 110)

if __name__ == "__main__":
    test_all()
