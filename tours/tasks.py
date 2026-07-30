import os
import subprocess

from celery import shared_task
from django.conf import settings

from .models import MatterportTour
import re

@shared_task(bind=True)
def download_matterport_tour(self, tour_id: int):
    tour = MatterportTour.objects.get(id=tour_id)
    tour.status = MatterportTour.Status.DOWNLOADING
    tour.celery_task_id = self.request.id or ""
    tour.error_message = ""
    tour.save(update_fields=["status", "celery_task_id", "error_message", "updated_at"])

    try:
        subprocess.run(
            [settings.MATTERPORT_DL_PYTHON, "matterport-dl.py", tour.source_url],
            cwd=settings.MATTERPORT_DL_DIR,
            check=True,
            timeout=1800,
            capture_output=True,
            text=True,
        )
        expected_dir = os.path.join(settings.MATTERPORT_DOWNLOADS_DIR, tour.matterport_id)
        if not os.path.isdir(expected_dir):
            raise RuntimeError(
                f"matterport-dl.py exited without error but expected output dir "
                f"was not found: {expected_dir}"
            )
        tour.local_path = expected_dir
        tour.status = MatterportTour.Status.READY

        if not tour.name:
            tour.name = fetch_tour_name(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)

    except subprocess.CalledProcessError as e:
        tour.status = MatterportTour.Status.FAILED
        tour.error_message = e.stderr[-4000:] if e.stderr else str(e)
    except Exception as e:
        tour.status = MatterportTour.Status.FAILED
        tour.error_message = str(e)

    tour.save()



import os
import json 
def fetch_tour_name(matterport_id, download_root):
    """Read the tour's display name from the already-downloaded GraphQL response."""
    json_path = os.path.join(
        download_root, matterport_id, "api", "mp", "models", "graph_GetModelDetails.json"
    )
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("data", {}).get("model", {}).get("name", "")
        return name.strip()[:255] if name else ""
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(f"[fetch_tour_name] couldn't read name for {matterport_id}: {e}")
        return ""