def post_fork(server, worker):
    """Restart APScheduler in worker process after gunicorn fork."""
    from app import scheduler, db_write_notification, get_all_customers
    # Write test notification to confirm post_fork ran
    try:
        customers = get_all_customers()
        for cust in customers:
            db_write_notification(cust["name"], "[DEBUG] post_fork ran", f"worker pid={worker.pid}", ntype="warning")
    except Exception as e:
        print(f"[GUNICORN] post_fork notify error: {e}")

    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:
        pass
    try:
        scheduler.start()
        print(f"[GUNICORN] Scheduler started in worker pid={worker.pid}")
    except Exception as e:
        print(f"[GUNICORN] Failed to start scheduler in worker: {e}")
