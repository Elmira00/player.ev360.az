import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt

from .models import MatterportTour, extract_matterport_id
from .serving import ensure_server_running, stop_server
from .tasks import download_matterport_tour

from django.views.decorators.http import require_POST


from django.contrib.auth.decorators import login_required
from django.db.models import Q
@login_required
def submit_view(request):
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        try:
            matterport_id = extract_matterport_id(url)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("submit")

        tour, created = MatterportTour.objects.get_or_create(
            matterport_id=matterport_id,
            defaults={"source_url": url},
        )

        if not created and tour.status == MatterportTour.Status.READY:
            messages.info(request, f"Already downloaded — link: {tour.local_url}")
        elif not created and tour.status in (
            MatterportTour.Status.DOWNLOADING,
            MatterportTour.Status.PENDING,
        ):
            messages.info(request, "This tour is already in progress — please wait for it to finish.")
        else:
            tour.source_url = url
            tour.status = MatterportTour.Status.PENDING
            tour.error_message = ""
            tour.save()
            download_matterport_tour.delay(tour.id)
            messages.success(request, f"Download started for {matterport_id}.")

        return redirect("dashboard")

    return render(request, "tours/submit.html")

@login_required
def dashboard_view(request):
    query = request.GET.get("q", "").strip()
    tours = MatterportTour.objects.all()

    if query:
        tours = tours.filter(
            Q(name__icontains=query) | Q(matterport_id__icontains=query)
        )

    has_active_tours = tours.filter(
        status__in=[MatterportTour.Status.PENDING, MatterportTour.Status.DOWNLOADING]
    ).exists()
    return render(
        request,
        "tours/dashboard.html",
        {"tours": tours, "has_active_tours": has_active_tours, "query": query},
    )


from django.views.decorators.clickjacking import xframe_options_exempt #### mark❌

@csrf_exempt
@xframe_options_exempt#### mark❌
def tour_proxy_view(request, matterport_id, subpath=""):
    tour = get_object_or_404(
        MatterportTour, matterport_id=matterport_id, status=MatterportTour.Status.READY
    )
    port = ensure_server_running(tour.matterport_id)

    query = request.META.get("QUERY_STRING", "")
    url = f"http://127.0.0.1:{port}/{subpath}"
    if subpath == "api/mp/accounts/graph":
        return HttpResponse('{"data": "empty"}', content_type="application/json")

    tour = get_object_or_404(
        MatterportTour, matterport_id=matterport_id, status=MatterportTour.Status.READY
    )
    port = ensure_server_running(tour.matterport_id)
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
                    content_type=resp.headers.get("Content-Type", "application/octet-stream"),
                )
        except urllib.error.HTTPError as e:
            return HttpResponse(e.read(), status=e.code)
        except Exception as e:
            last_error = e
            time.sleep(0.25)
    return HttpResponse(f"Tour server did not start in time: {last_error}", status=502)




import re

REFERER_TOUR_RE = re.compile(r"/tour/([A-Za-z0-9]+)/")


@csrf_exempt
def root_asset_proxy_view(request, subpath):
    """Catches absolute-path requests (e.g. /api/mp/models/graph) that
    showcase.js issues from site root instead of under /tour/<id>/, and
    routes them to the right tour's server using the Referer header."""
    referer = request.META.get("HTTP_REFERER", "")
    match = REFERER_TOUR_RE.search(referer)
    if not match:
        return HttpResponse("Cannot determine which tour this request belongs to", status=404)
    return tour_proxy_view(request, matterport_id=match.group(1), subpath=subpath)


@login_required
@require_POST
def delete_tour_view(request, tour_id):
    tour = get_object_or_404(MatterportTour, id=tour_id)

    stop_server(tour.matterport_id)

    if tour.local_path:
        local_path = Path(tour.local_path)
        if local_path.exists():
            try:
                shutil.rmtree(local_path)
                print(f"[delete_tour_view] Deleted folder: {local_path}")
            except OSError as e:
                messages.warning(request, f"Tour deleted from database, but could not fully delete files on disk: {e}")
                print(f"[delete_tour_view] Failed to delete folder {local_path}: {e}")

    tour.delete()

    messages.success(request, "Tour and its downloaded files were deleted.")
    return redirect("dashboard")


@login_required
def edit_tour_view(request, tour_id):
    tour = get_object_or_404(MatterportTour, id=tour_id)

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "retry":
            tour.status = MatterportTour.Status.PENDING
            tour.error_message = ""
            tour.save()
            download_matterport_tour.delay(tour.id)
            messages.success(request, "Re-download started.")
            return redirect("edit_tour", tour_id=tour.id)

        name = request.POST.get("name", "").strip()
        source_url = request.POST.get("source_url", "").strip()
        tour.name = name

        if source_url and source_url != tour.source_url:
            try:
                new_matterport_id = extract_matterport_id(source_url)
            except ValueError as e:
                messages.error(request, f"Invalid source URL: {e}")
                return render(request, "tours/edit_tour.html", {"tour": tour})

            if new_matterport_id != tour.matterport_id:
                duplicate = MatterportTour.objects.filter(
                    matterport_id=new_matterport_id
                ).exclude(id=tour.id).first()

                if duplicate:
                    messages.error(
                        request,
                        f"That Matterport model is already used by tour '{duplicate.name or duplicate.matterport_id}'. "
                        "Delete that tour first, or use a different URL.",
                    )
                    return render(request, "tours/edit_tour.html", {"tour": tour})

                stop_server(tour.matterport_id)

                if tour.local_path:
                    old_path = Path(tour.local_path)
                    if old_path.exists():
                        try:
                            shutil.rmtree(old_path)
                            print(f"[edit_tour_view] Deleted old folder: {old_path}")
                        except OSError as e:
                            messages.warning(request, f"Could not fully delete old files: {e}")
                            print(f"[edit_tour_view] Failed to delete old folder {old_path}: {e}")

                tour.matterport_id = new_matterport_id
                tour.local_path = ""
                tour.status = MatterportTour.Status.PENDING
                tour.error_message = ""
                tour.source_url = source_url
                tour.save()

                download_matterport_tour.delay(tour.id)
                messages.success(
                    request,
                    f"Tour re-pointed to {new_matterport_id}. Old files removed, download started.",
                )
                return redirect("dashboard")

            tour.source_url = source_url

        tour.save()
        messages.success(request, "Tour updated.")
        return redirect("dashboard")

    return render(request, "tours/edit_tour.html", {"tour": tour})


from django.http import JsonResponse
@login_required
def tours_status_json(request):
    """Returns current status/link data for all tours (or filtered by ?q=),
    used by the dashboard's JS polling to update badges without a full reload."""
    query = request.GET.get("q", "").strip()
    tours = MatterportTour.objects.all()

    if query:
        tours = tours.filter(
            Q(name__icontains=query) | Q(matterport_id__icontains=query)
        )

    data = []
    for tour in tours:
        data.append({
            "id": tour.id,
            "matterport_id": tour.matterport_id,
            "status": tour.status,
            "status_display": tour.get_status_display(),
            "local_url": tour.local_url if tour.status == MatterportTour.Status.READY else None,
            "error_message": tour.error_message if tour.status == MatterportTour.Status.FAILED else None,
        })

    has_active_tours = tours.filter(
        status__in=[MatterportTour.Status.PENDING, MatterportTour.Status.DOWNLOADING]
    ).exists()

    return JsonResponse({"tours": data, "has_active_tours": has_active_tours})