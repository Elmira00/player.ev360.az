import mimetypes
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from django.contrib import messages
from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from .models import Tour, TourUpload, extract_matterport_id
from .serving import ensure_server_running, stop_server
from .tasks import download_matterport_tour, process_zip_upload


def _safe_rmtree(path, attempts=5, delay=1.0):
    """Windows sometimes still holds a file handle open briefly after a
    subprocess is killed (antivirus scanning, deep nested paths, etc.),
    causing rmtree to fail with WinError 145. Retry a few times with a
    short delay instead of failing immediately."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    return False





def _resolve_content_type(subpath, resp):
    """The local matterport-dl server sometimes mislabels static assets
    (e.g. serves a .js file as application/octet-stream). Browsers with
    strict MIME-type checking will refuse to *execute* a <script> whose
    Content-Type isn't a JS MIME type, silently breaking the tour with no
    error beyond a "Refused to execute script" console line. Guess from
    the file extension first — mimetypes gets common web asset types
    (.js, .css, .wasm, .json, etc.) right regardless of what the local
    server sent — and only fall back to the server's own header, then a
    generic default, if the extension is unrecognized (e.g. an
    extensionless persisted-query endpoint)."""
    guessed_type, _ = mimetypes.guess_type(subpath)
    return guessed_type or resp.headers.get("Content-Type", "application/octet-stream")


def _resolve_local_content_type(path):
    """Same extension-first MIME guessing as _resolve_content_type, but for
    files we're reading straight off disk (no upstream response headers to
    fall back on)."""
    guessed_type, _ = mimetypes.guess_type(path)
    return guessed_type or "application/octet-stream"


_PLUGIN_SUBPATH_RE = re.compile(r"^showcase-sdk/plugins/published/([^/]+)/([^/]+)/(.+)$")


def _resolve_local_plugin_path(download_root, matterport_id, subpath):
    """showcase.js sometimes requests a bare plugin version (e.g.
    'compass/1.0.14/plugin.json') while matterport-dl actually downloads it
    under a suffixed folder name (e.g. 'compass/1.0.14-1/'). That mismatch
    causes both the local server AND the static.matterport.com CDN fallback
    to 404 on the exact requested path, which stalls the plugin loader and
    the tour never leaves LOADING. Scan the plugin's folder on disk for a
    version directory that matches or starts with the requested version
    before falling through to the CDN fallback."""
    m = _PLUGIN_SUBPATH_RE.match(subpath)
    if not m:
        return None
    plugin, version, rest = m.groups()
    plugin_dir = os.path.join(
        download_root, matterport_id, "showcase-sdk", "plugins", "published", plugin
    )
    if not os.path.isdir(plugin_dir):
        return None
    for entry in os.listdir(plugin_dir):
        if entry == version or entry.startswith(version + "-"):
            candidate = os.path.join(plugin_dir, entry, rest)
            if os.path.isfile(candidate):
                return candidate
    return None


_local_port_cache = {}


@login_required
def submit_view(request):
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        try:
            matterport_id = extract_matterport_id(url)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("submit")

        tour, created = Tour.objects.get_or_create(matterport_id=matterport_id)

        latest = tour.latest_upload
        if latest and latest.status in (TourUpload.Status.DOWNLOADING, TourUpload.Status.PENDING):
            messages.info(request, "This tour is already in progress — please wait for it to finish.")
        else:
            # No restriction on re-submitting an already-READY tour — this
            # creates a new TourUpload row (history preserved), and the
            # dashboard always shows only the latest one (by created_at).
            upload = TourUpload.objects.create(
                tour=tour,
                source_type=TourUpload.SourceType.URL,
                source_url=url,
                status=TourUpload.Status.PENDING,
            )
            download_matterport_tour.delay(upload.id)
            messages.success(request, f"Download started for {matterport_id}.")

        return redirect("dashboard")

    return render(request, "tours/submit.html")


@login_required
def submit_zip_view(request):
    if request.method != "POST":
        return redirect("submit")

    zip_file = request.FILES.get("zip_file")
    if not zip_file:
        messages.error(request, "No ZIP file was uploaded.")
        return redirect("submit")

    if not zip_file.name.lower().endswith(".zip"):
        messages.error(request, "Please upload a .zip file.")
        return redirect("submit")

    upload_dir = os.path.join(settings.BASE_DIR, "tmp_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    temp_zip_path = os.path.join(upload_dir, f"{uuid.uuid4().hex}.zip")

    with open(temp_zip_path, "wb+") as dest:
        for chunk in zip_file.chunks():
            dest.write(chunk)

    # tour is left null — process_zip_upload will link it once the zip is
    # unpacked and the matterport_id is known.
    upload = TourUpload.objects.create(
        tour=None,
        source_type=TourUpload.SourceType.ZIP,
        original_zip_name=zip_file.name,
        status=TourUpload.Status.PENDING,
    )

    process_zip_upload.delay(upload.id, temp_zip_path)
    messages.success(request, "ZIP uploaded — processing in the background.")
    return redirect("dashboard")


@login_required
def dashboard_view(request):
    query = request.GET.get("q", "").strip()
    tours = Tour.objects.all()

    if query:
        tours = tours.filter(
            Q(name__icontains=query) | Q(matterport_id__icontains=query)
        )

    tour_rows = []
    has_active = False
    for tour in tours:
        latest = tour.latest_upload
        if latest is None:
            continue
        tour_rows.append({"tour": tour, "upload": latest})
        if latest.status in (TourUpload.Status.PENDING, TourUpload.Status.DOWNLOADING):
            has_active = True

    pending_zip_uploads = TourUpload.objects.filter(tour__isnull=True).exclude(
        status=TourUpload.Status.READY
    )
    if pending_zip_uploads.exists():
        has_active = True

    return render(
        request,
        "tours/dashboard.html",
        {
            "tour_rows": tour_rows,
            "pending_zip_uploads": pending_zip_uploads,
            "has_active_tours": has_active,
            "query": query,
        },
    )


@login_required
def tours_status_json(request):
    query = request.GET.get("q", "").strip()
    tours = Tour.objects.all()

    if query:
        tours = tours.filter(
            Q(name__icontains=query) | Q(matterport_id__icontains=query)
        )

    data = []
    has_active = False
    for tour in tours:
        latest = tour.latest_upload
        if latest is None:
            continue
        data.append({
            "tour_id": tour.id,
            "matterport_id": tour.matterport_id,
            "status": latest.status,
            "status_display": latest.get_status_display(),
            "local_url": tour.local_url if latest.status == TourUpload.Status.READY else None,
            "error_message": latest.error_message if latest.status == TourUpload.Status.FAILED else None,
        })
        if latest.status in (TourUpload.Status.PENDING, TourUpload.Status.DOWNLOADING):
            has_active = True

    if TourUpload.objects.filter(tour__isnull=True).exclude(status=TourUpload.Status.READY).exists():
        has_active = True

    return JsonResponse({"tours": data, "has_active_tours": has_active})


@csrf_exempt
def tour_proxy_view(request, matterport_id, subpath=""):
    tour = get_object_or_404(Tour, matterport_id=matterport_id)
    latest = tour.latest_upload
    if not latest or latest.status != TourUpload.Status.READY:
        return HttpResponse("Tour not ready", status=404)

    query = request.META.get("QUERY_STRING", "")

    # Short-circuit persisted GraphQL queries the local matterport-dl server
    # has no prefetched file for. Forwarding these just gets the connection
    # dropped, which the retry loop below then hammers 20x (~5-6s) before
    # giving up with a 502 — one of the two "freeze" symptoms users hit.
    # matterport-dl.py's fixed prefetch list doesn't cover every query
    # showcase.js can fire, so this list may need more entries as new gaps
    # turn up (confirmed missing on disk before adding each entry here).
    if subpath == "api/mp/accounts/graph":
        return HttpResponse('{"data": "empty"}', content_type="application/json")

    if subpath == "api/mp/models/graph" and "GetModelAssets" in query:
        cdn_url = f"https://my.matterport.com/api/mp/models/graph?{query}"
        try:
            cdn_req = urllib.request.Request(cdn_url, method="GET")
            with urllib.request.urlopen(cdn_req, timeout=10) as cdn_resp:
                return HttpResponse(
                    cdn_resp.read(),
                    status=cdn_resp.status,
                    content_type="application/json",
                )
        except Exception as e:
            print(f"[tour_proxy_view] GetModelAssets live fetch failed, falling back to empty stub: {e}")
            return HttpResponse(
                '{"data": {"model": {"assets": {"__typename": "AssetsGroup"}}}}',
                content_type="application/json",
            )
    if subpath == "api/mp/models/graph" and "GetSweeps" in query:
        cdn_url = f"https://my.matterport.com/api/mp/models/graph?{query}"
        try:
            cdn_req = urllib.request.Request(cdn_url, method="GET")
            with urllib.request.urlopen(cdn_req, timeout=10) as cdn_resp:
                return HttpResponse(
                    cdn_resp.read(),
                    status=cdn_resp.status,
                    content_type="application/json",
                )
        except Exception as e:
            print(f"[tour_proxy_view] GetSweeps live fetch failed, falling back to empty stub: {e}")
            return HttpResponse(
                '{"data": {"model": {"sweeps": []}}}',
                content_type="application/json",
            )

    # showcase.js sometimes requests a bare plugin version (e.g.
    # showcase-sdk/plugins/published/compass/1.0.14/plugin.json) while
    # matterport-dl actually saved it under a suffixed folder name (e.g.
    # 1.0.14-1). That mismatch 404s both locally and against the CDN
    # fallback below, stalling the plugin loader forever (confirmed: no
    # canvas ever gets created when this happens — the tour just sits on
    # LOADING). Check for a matching version-prefixed folder on disk before
    # falling through to the local server / CDN.
    if subpath.startswith("showcase-sdk/plugins/published/"):
        local_file = _resolve_local_plugin_path(
            settings.MATTERPORT_DOWNLOADS_DIR, tour.matterport_id, subpath
        )
        if local_file:
            with open(local_file, "rb") as f:
                return HttpResponse(
                    f.read(),
                    content_type=_resolve_local_content_type(local_file),
                )

    port = _local_port_cache.get(tour.matterport_id)
    if port is None:
        port = ensure_server_running(tour.matterport_id)
        print(port)
        _local_port_cache[tour.matterport_id] = port

    url = f"http://127.0.0.1:{port}/{subpath}"
    if query:
        url += f"?{query}"

    body = request.body if request.method == "POST" else None
    req = urllib.request.Request(url, data=body, method=request.method)
    if "Content-Type" in request.headers:
        req.add_header("Content-Type", request.headers["Content-Type"])

    last_error = None
    for _ in range(20):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return HttpResponse(
                    resp.read(),
                    status=resp.status,
                    content_type=_resolve_content_type(subpath, resp),
                )
        except urllib.error.HTTPError as e:
            # Stock Matterport SDK plugins (e.g. showcase-sdk/plugins/...)
            # aren't always bundled by matterport-dl. These are generic,
            # not tour-specific, so it's safe to fetch the real file from
            # Matterport's CDN instead of failing the plugin loader —
            # an unhandled 404 here stalls loadApplication permanently
            # (confirmed: no canvas ever gets created when this happens).
            if e.code == 404 and subpath.startswith("showcase-sdk/"):
                cdn_url = f"https://static.matterport.com/{subpath}"
                if query:
                    cdn_url += f"?{query}"
                try:
                    cdn_req = urllib.request.Request(cdn_url, method="GET")
                    with urllib.request.urlopen(cdn_req, timeout=10) as cdn_resp:
                        return HttpResponse(
                            cdn_resp.read(),
                            status=cdn_resp.status,
                            content_type=_resolve_content_type(subpath, cdn_resp),
                        )
                except Exception:
                    pass  # fall through to returning the original 404
            return HttpResponse(e.read(), status=e.code)
        except Exception as e:
            last_error = e
            # A cached port may be stale (server restarted, port reused by
            # something else) — drop it and force a fresh lookup next loop.
            _local_port_cache.pop(tour.matterport_id, None)
            port = ensure_server_running(tour.matterport_id)
            _local_port_cache[tour.matterport_id] = port
            url = f"http://127.0.0.1:{port}/{subpath}"
            if query:
                url += f"?{query}"
            req = urllib.request.Request(url, data=body, method=request.method)
            if "Content-Type" in request.headers:
                req.add_header("Content-Type", request.headers["Content-Type"])
            time.sleep(0.25)

    return HttpResponse(f"Tour server did not start in time: {last_error}", status=502)


REFERER_TOUR_RE = re.compile(r"/tour/([A-Za-z0-9]+)/")


@csrf_exempt
def root_asset_proxy_view(request, subpath):
    referer = request.META.get("HTTP_REFERER", "")
    match = REFERER_TOUR_RE.search(referer)
    if not match:
        return HttpResponse("Cannot determine which tour this request belongs to", status=404)
    return tour_proxy_view(request, matterport_id=match.group(1), subpath=subpath)


@csrf_exempt
def matterport_cdn_proxy_view(request, subpath):
    query = request.META.get("QUERY_STRING", "")
    url = f"https://static.matterport.com/{subpath}"
    if query:
        url += f"?{query}"

    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return HttpResponse(
                resp.read(),
                status=resp.status,
                content_type=_resolve_content_type(subpath, resp),
            )
    except urllib.error.HTTPError as e:
        return HttpResponse(e.read(), status=e.code)
    except Exception as e:
        return HttpResponse(f"Failed to fetch CDN asset: {e}", status=502)


@login_required
@require_POST
def delete_tour_view(request, tour_id):
    tour = get_object_or_404(Tour, id=tour_id)

    stop_server(tour.matterport_id)
    _local_port_cache.pop(tour.matterport_id, None)

    candidate_path = os.path.join(settings.MATTERPORT_DOWNLOADS_DIR, tour.matterport_id)
    local_path = Path(candidate_path)
    if local_path.exists():
        try:
            _safe_rmtree(local_path)
            print(f"[delete_tour_view] Deleted folder: {local_path}")
        except OSError as e:
            messages.warning(request, f"Tour deleted from database, but could not fully delete files on disk: {e}")
            print(f"[delete_tour_view] Failed to delete folder {local_path}: {e}")

    tour.delete()  # cascades to all TourUpload rows too

    messages.success(request, "Tour, all its upload history, and downloaded files were deleted.")
    return redirect("dashboard")


@login_required
def edit_tour_view(request, tour_id):
    tour = get_object_or_404(Tour, id=tour_id)
    latest = tour.latest_upload

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "retry":
            if latest and latest.source_type == TourUpload.SourceType.URL:
                new_upload = TourUpload.objects.create(
                    tour=tour,
                    source_type=TourUpload.SourceType.URL,
                    source_url=latest.source_url,
                    status=TourUpload.Status.PENDING,
                )
                download_matterport_tour.delay(new_upload.id)
                messages.success(request, "Re-download started.")
            return redirect("edit_tour", tour_id=tour.id)

        name = request.POST.get("name", "").strip()
        source_url = request.POST.get("source_url", "").strip()
        tour.name = name

        if source_url and latest and source_url != latest.source_url:
            try:
                new_matterport_id = extract_matterport_id(source_url)
            except ValueError as e:
                messages.error(request, f"Invalid source URL: {e}")
                return render(request, "tours/edit_tour.html", {"tour": tour, "latest": latest})

            if new_matterport_id != tour.matterport_id:
                duplicate = Tour.objects.filter(
                    matterport_id=new_matterport_id
                ).exclude(id=tour.id).first()

                if duplicate:
                    messages.error(
                        request,
                        f"That Matterport model is already used by tour '{duplicate.name or duplicate.matterport_id}'. "
                        "Delete that tour first, or use a different URL.",
                    )
                    return render(request, "tours/edit_tour.html", {"tour": tour, "latest": latest})

                stop_server(tour.matterport_id)
                _local_port_cache.pop(tour.matterport_id, None)

                old_path = Path(os.path.join(settings.MATTERPORT_DOWNLOADS_DIR, tour.matterport_id))
                if old_path.exists():
                    try:
                        _safe_rmtree(old_path)
                        print(f"[edit_tour_view] Deleted old folder: {old_path}")
                    except OSError as e:
                        messages.warning(request, f"Could not fully delete old files: {e}")

                tour.matterport_id = new_matterport_id
                tour.save()

                new_upload = TourUpload.objects.create(
                    tour=tour,
                    source_type=TourUpload.SourceType.URL,
                    source_url=source_url,
                    status=TourUpload.Status.PENDING,
                )
                download_matterport_tour.delay(new_upload.id)
                messages.success(
                    request,
                    f"Tour re-pointed to {new_matterport_id}. Old files removed, download started.",
                )
                return redirect("dashboard")

        tour.save()
        messages.success(request, "Tour updated.")
        return redirect("dashboard")

    return render(request, "tours/edit_tour.html", {"tour": tour, "latest": latest})