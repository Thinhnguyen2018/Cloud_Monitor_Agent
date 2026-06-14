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
    except Exception as e:
        print(f"[GUNICORN] Failed to start scheduler in worker: {e}")
