import os
from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "instagram_saas",
    broker=REDIS_URL,
    backend=REDIS_URL
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,beat_schedule={
    "fetch-followers-hourly": {
        "task": "fetch_all_followers",
        "schedule": crontab(minute=0),
    },
    "process-automation-rules": {
        "task": "process_automation_rules",
        "schedule": crontab(minute="*/5"),
    },
}
)

celery_app.autodiscover_tasks(["app.tasks"])
