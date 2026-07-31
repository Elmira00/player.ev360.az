import os
import subprocess
from celery import shared_task
from django.conf import settings
from .models import MatterportTour
import re


def rewrite_cdn_urls(matterport_id: str, download_root: str) -> None:
    """matterport-dl.py doesn't rewrite static.matterport.com/webgl-vendors/
    URLs to local paths, so the browser makes a direct cross-origin request
    that gets blocked by CORS on our production domain. This patches the
    downloaded showcase.js / showcase.modified.js in place so those assets
    load through our own /webgl-vendors/ proxy instead."""
    tour_dir = os.path.join(download_root, matterport_id)
    targets = [
        os.path.join(tour_dir, "js", "showcase.js"),
        os.path.join(tour_dir, "js", "showcase.modified.js"),
    ]

    for path in targets:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = content.replace(
                "https://static.matterport.com/webgl-vendors/",
                "/webgl-vendors/",
            )

            if new_content != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"[rewrite_cdn_urls] Patched CDN URLs in: {path}")
        except OSError as e:
            print(f"[rewrite_cdn_urls] Could not patch {path}: {e}")


def _tour_actually_downloaded(expected_dir: str) -> bool:
    """matterport-dl.py sometimes exits with a non-zero code after fully
    downloading the main tour, because it then tries to fetch a *secondary*
    optional asset (e.g. the defurnished view, or extra plugin data) which
    can fail (e.g. rate limited). The core tour is still usable in that case,
    so check for the essential file rather than trusting the exit code alone."""
    return os.path.isfile(os.path.join(expected_dir, "index.html"))


@shared_task(bind=True)
def download_matterport_tour(self, tour_id: int):
    tour = MatterportTour.objects.get(id=tour_id)
    tour.status = MatterportTour.Status.DOWNLOADING
    tour.celery_task_id = self.request.id or ""
    tour.error_message = ""
    tour.save(update_fields=["status", "celery_task_id", "error_message", "updated_at"])

    expected_dir = os.path.join(settings.MATTERPORT_DOWNLOADS_DIR, tour.matterport_id)

    try:
        subprocess.run(
            [settings.MATTERPORT_DL_PYTHON, "matterport-dl.py", tour.source_url],
            cwd=settings.MATTERPORT_DL_DIR,
            check=True,
            timeout=1800,
            capture_output=True,
            text=True,
        )

        if not os.path.isdir(expected_dir):
            raise RuntimeError(
                f"matterport-dl.py exited without error but expected output dir "
                f"was not found: {expected_dir}"
            )

        rewrite_cdn_urls(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)

        tour.local_path = expected_dir
        tour.status = MatterportTour.Status.READY
        if not tour.name:
            tour.name = fetch_tour_name(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)

    except subprocess.CalledProcessError as e:
        # Check if the CORE tour actually finished downloading before the
        # script crashed on a secondary/optional asset (common cause: a
        # defurnished-view fetch or extra plugin getting rate-limited AFTER
        # the main tour is already fully saved).
        if _tour_actually_downloaded(expected_dir):
            print(
                f"[download_matterport_tour] matterport-dl.py exited with an error, "
                f"but the core tour files exist at {expected_dir} — treating as READY."
            )
            rewrite_cdn_urls(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
            tour.local_path = expected_dir
            tour.status = MatterportTour.Status.READY
            tour.error_message = (
                "Note: a secondary asset (e.g. defurnished view) failed to download, "
                "but the main tour is complete and viewable."
            )
            if not tour.name:
                tour.name = fetch_tour_name(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
        else:
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
