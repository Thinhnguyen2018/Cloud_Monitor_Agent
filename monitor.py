"""Standalone monitoring process — runs alongside gunicorn, no fork issues."""
import time, os, sys

# Ensure app module can be imported
sys.path.insert(0, os.path.dirname(__file__))

from app import (
    _run_secgroup_alerts,
    _run_health_alerts,
    _run_cpu_ram_alerts,
)

SECGROUP_INTERVAL = 60        # 1 minute (test)
HEALTH_INTERVAL   = 30 * 60  # 30 minutes
CPU_RAM_INTERVAL  = 5  * 60  # 5 minutes

def main():
    print("[MONITOR] Started")
    last_secgroup = 0
    last_health   = 0
    last_cpu_ram  = 0

    while True:
        now = time.time()

        if now - last_secgroup >= SECGROUP_INTERVAL:
            try:
                _run_secgroup_alerts()
                print("[MONITOR] secgroup check done")
            except Exception as e:
                print(f"[MONITOR] secgroup error: {e}")
            last_secgroup = now

        if now - last_health >= HEALTH_INTERVAL:
            try:
                _run_health_alerts()
                print("[MONITOR] health check done")
            except Exception as e:
                print(f"[MONITOR] health error: {e}")
            last_health = now

        if now - last_cpu_ram >= CPU_RAM_INTERVAL:
            try:
                _run_cpu_ram_alerts()
                print("[MONITOR] cpu/ram check done")
            except Exception as e:
                print(f"[MONITOR] cpu/ram error: {e}")
            last_cpu_ram = now

        time.sleep(10)

if __name__ == "__main__":
    main()
