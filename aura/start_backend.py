"""
Akansha Dedicated Backend Server Launcher
Monitors and ensures port 8000 is cleanly owned by Akansha FastAPI backend.
Automatically terminates conflicting background processes on port 8000.
"""
import os
import sys
import time
import subprocess
import uvicorn

def free_port_8000():
    try:
        # Find PIDs on port 8000 using netstat / powershell
        cmd = "powershell -Command \"(Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue).OwningProcess\""
        out = subprocess.check_output(cmd, shell=True, text=True).strip()
        pids = set(p.strip() for p in out.splitlines() if p.strip() and p.strip() != "0")
        current_pid = str(os.getpid())
        for pid in pids:
            if pid != current_pid:
                print(f"[Akansha Auto-Port-Guard] Terminating conflicting PID {pid} on port 8000...")
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
    except Exception as err:
        pass

if __name__ == "__main__":
    from backend.main import app
    print("[Akansha AI OS] Starting FastAPI backend on http://127.0.0.1:8000...")
    free_port_8000()
    time.sleep(1)
    
    while True:
        try:
            free_port_8000()
            uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
            break
        except Exception as e:
            print(f"[Akansha Auto-Port-Guard] Socket bind retry: {e}. Clearing port 8000 in 2s...")
            free_port_8000()
            time.sleep(2)
