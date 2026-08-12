import time
import os
import shutil
import subprocess
import zipfile
import tempfile
import glob
from celery import shared_task
from django.conf import settings

def _safe_rmtree(path, attempts=5, delay=1.0):
    """Windows can hold a file handle open briefly after a subprocess is
    terminated, causing rmtree to fail with WinError 32/145. Retry with a
    short delay instead of failing immediately — same issue _safe_rmtree
    in views.py handles for the same reason."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    return False

from .models import Tour, TourUpload
from .serving import stop_server

def rewrite_cdn_urls(matterport_id: str, download_root: str) -> None:
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
    if not os.path.isfile(os.path.join(expected_dir, "index.html")):
        return False
    mesh_tiles_dirs = glob.glob(
        os.path.join(expected_dir, "models", "*", "assets", "mesh_tiles")
    )
    if not mesh_tiles_dirs:
        return False
    mesh_tiles_ok = False
    for d in mesh_tiles_dirs:
        if not os.path.isdir(d):
            continue
        for _root, _dirs, files in os.walk(d):
            if files:
                mesh_tiles_ok = True
                break
        if mesh_tiles_ok:
            break
    if not mesh_tiles_ok:
        return False



    return True

def fetch_tour_name(matterport_id, download_root):
    import json
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

@shared_task(bind=True)
def download_matterport_tour(self, upload_id: int):
    upload = TourUpload.objects.get(id=upload_id)
    tour = upload.tour

    upload.status = TourUpload.Status.DOWNLOADING
    upload.celery_task_id = self.request.id or ""
    upload.error_message = ""
    upload.save(update_fields=["status", "celery_task_id", "error_message", "updated_at"])

    expected_dir = os.path.join(settings.MATTERPORT_DOWNLOADS_DIR, tour.matterport_id)

    try:
        subprocess.run(
            [
                settings.MATTERPORT_DL_PYTHON, 
                "matterport-dl.py", 
                "--proxy", "socks5://127.0.0.1:9050", "--no-generate-tile-mesh-crops",
                upload.source_url
            ],
            cwd=settings.MATTERPORT_DL_DIR,
            check=True,
            timeout=7200,
            capture_output=True,
            text=True,
        )

        if not os.path.isdir(expected_dir):
            raise RuntimeError(
                f"matterport-dl.py exited without error but expected output dir "
                f"was not found: {expected_dir}"
            )

        if not _tour_actually_downloaded(expected_dir):
            raise RuntimeError(
                f"Download incomplete (mesh_tiles/sweep tiles missing "
                f"or real 429s found)"
            )

        rewrite_cdn_urls(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
        rewrite_graph_json_cdn_urls(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
        strip_dangling_defurnish_views(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)

        upload.local_path = expected_dir
        upload.status = TourUpload.Status.READY
        if not tour.name:
            tour.name = fetch_tour_name(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
            tour.save(update_fields=["name"])

    except subprocess.CalledProcessError as e:
        if _tour_actually_downloaded(expected_dir):
            print(
                f"[download_matterport_tour] matterport-dl.py exited with an error, "
                f"but the core tour files exist at {expected_dir} — treating as READY."
            )
            rewrite_cdn_urls(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
            rewrite_graph_json_cdn_urls(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
            strip_dangling_defurnish_views(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
            upload.local_path = expected_dir
            upload.status = TourUpload.Status.READY
            upload.error_message = (
                "Note: a secondary asset (e.g. defurnished view) failed to download, "
                "but the main tour is complete and viewable."
            )
            if not tour.name:
                tour.name = fetch_tour_name(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
                tour.save(update_fields=["name"])
        else:
            upload.status = TourUpload.Status.FAILED
            upload.error_message = e.stderr[-4000:] if e.stderr else str(e)

    except Exception as e:
        upload.status = TourUpload.Status.FAILED
        upload.error_message = str(e)

    upload.save()

@shared_task(bind=True)
def process_zip_upload(self, upload_id: int, zip_path: str):
    upload = TourUpload.objects.get(id=upload_id)
    upload.status = TourUpload.Status.DOWNLOADING
    upload.celery_task_id = self.request.id or ""
    upload.error_message = ""
    upload.save(update_fields=["status", "celery_task_id", "error_message", "updated_at"])

    extract_dir = None
    try:
        extract_dir = tempfile.mkdtemp(prefix="tour_zip_extract_")

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        entries = [
            e for e in os.listdir(extract_dir)
            if os.path.isdir(os.path.join(extract_dir, e))
        ]

        if len(entries) != 1:
            raise ValueError(
                f"Expected exactly one top-level folder in the ZIP, found {len(entries)}: {entries}"
            )

        matterport_id = entries[0]
        source_folder = os.path.join(extract_dir, matterport_id)

        if not os.path.isfile(os.path.join(source_folder, "index.html")):
            raise ValueError(f"'{matterport_id}' folder doesn't look like a valid tour.")
            
        tour, _ = Tour.objects.get_or_create(matterport_id=matterport_id)
        upload.tour = tour
        upload.save(update_fields=["tour"])

        target_dir = os.path.join(settings.MATTERPORT_DOWNLOADS_DIR, matterport_id)

        stop_server(matterport_id)

        if os.path.exists(target_dir):
            _safe_rmtree(target_dir)

        shutil.move(source_folder, target_dir)

        rewrite_cdn_urls(matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
        rewrite_graph_json_cdn_urls(matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
        strip_dangling_defurnish_views(tour.matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)

        upload.local_path = target_dir
        upload.status = TourUpload.Status.READY
        if not tour.name:
            tour.name = fetch_tour_name(matterport_id, settings.MATTERPORT_DOWNLOADS_DIR)
            tour.save(update_fields=["name"])

    except Exception as e:
        upload.status = TourUpload.Status.FAILED
        upload.error_message = str(e)

    finally:
        if extract_dir and os.path.isdir(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        if os.path.isfile(zip_path):
            os.remove(zip_path)

    upload.save()

import json
import re

def strip_dangling_defurnish_views(matterport_id: str, download_root: str) -> None:
    tour_dir = os.path.join(download_root, matterport_id)
    targets = [
        os.path.join(tour_dir, "api", "mp", "models", "graph_GetModelDetails.json"),
        os.path.join(tour_dir, "api", "mp", "models", "graph_GetModelDetails.modified.json"),
    ]

    for path in targets:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            model = data.get("data", {}).get("model", {})
            defurnish_views = model.get("defurnishViews") or []
            if not defurnish_views:
                continue

            kept = []
            for view in defurnish_views:
                view_id = view.get("model", {}).get("id", "")
                found = False
                if view_id:
                    for root, _dirs, files in os.walk(tour_dir):
                        if any(view_id in fn for fn in files) or view_id in root:
                            found = True
                            break
                if found:
                    kept.append(view)
                else:
                    print(f"[strip_dangling_defurnish_views] Removing dangling defurnishViews entry {view_id}")

            if len(kept) != len(defurnish_views):
                model["defurnishViews"] = kept
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[strip_dangling_defurnish_views] Could not process {path}: {e}")

import glob
def rewrite_graph_json_cdn_urls(matterport_id: str, download_root: str) -> None:
    tour_dir = os.path.join(download_root, matterport_id)
    models_dir = os.path.join(tour_dir, "api", "mp", "models")
    if not os.path.isdir(models_dir):
        return

    cdn_pattern = re.compile(
        r'https://cdn-2\.matterport\.com/(models/[0-9a-fA-F]+/assets/[^"?]*)(\?[^"]*)?'
    )

    for path in glob.glob(os.path.join(models_dir, "graph_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            new_content, count = cdn_pattern.subn(
                rf"/tour/{matterport_id}/\1", content
            )

            if count:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"[rewrite_graph_json_cdn_urls] Patched {count} CDN asset URL(s) in: {path}")
        except OSError as e:
            print(f"[rewrite_graph_json_cdn_urls] Could not patch {path}: {e}")
