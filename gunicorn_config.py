def post_fork(server, worker):
    """Restart APScheduler in worker process after gunicorn fork."""
    from app import scheduler
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
