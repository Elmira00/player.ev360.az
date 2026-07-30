import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "player_ev360.settings")

app = Celery("player_ev360")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
