import re

from django.conf import settings
from django.db import models


def extract_matterport_id(url_or_id: str) -> str:
    """Mirrors matterport-dl.py's getPageId(): pulls the id out of a
    my.matterport.com/show/?m=XXXX url, or accepts a bare id."""
    raw = url_or_id.strip()
    candidate = raw.split("m=")[-1].split("&")[0]
    is_defurnished = len(candidate) == 25
    if not candidate.isalnum() or ((len(candidate) < 5 or len(candidate) > 15) and not is_defurnished):
        raise ValueError(
            f"Could not extract a valid Matterport model id from: {url_or_id!r}. "
            "Pass either the id itself (e.g. EGxFGTFyC9N) or a "
            "https://my.matterport.com/show/?m=... link."
        )
    return candidate


class Tour(models.Model):
    """Represents a single Matterport model, identified by its matterport_id.
    Can have multiple TourUpload attempts over time (via URL or ZIP)."""

    matterport_id = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.matterport_id

    @property
    def latest_upload(self):
        return self.uploads.order_by("-created_at").first()

    @property
    def local_url(self) -> str:
        return f"{settings.PLAYER_PUBLIC_BASE_URL}/tour/{self.matterport_id}/"


class TourUpload(models.Model):
    """Represents a single upload attempt for a Tour — either a Matterport
    URL download, or a ZIP file upload. tour is nullable because for ZIP
    uploads, the matterport_id (and thus which Tour this belongs to) isn't
    known until the ZIP is unpacked in the background task."""

    class SourceType(models.TextChoices):
        URL = "url", "Matterport URL"
        ZIP = "zip", "ZIP Upload"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DOWNLOADING = "downloading", "Downloading"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    tour = models.ForeignKey(
        Tour, on_delete=models.CASCADE, related_name="uploads", null=True, blank=True
    )
    source_type = models.CharField(max_length=10, choices=SourceType.choices)
    source_url = models.URLField(blank=True)
    original_zip_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    local_path = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        label = self.tour.matterport_id if self.tour else "(pending id)"
        return f"{label} [{self.source_type}] ({self.status})"