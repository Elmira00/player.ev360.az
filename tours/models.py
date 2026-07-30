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


class MatterportTour(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DOWNLOADING = "downloading", "Downloading"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    matterport_id = models.CharField(max_length=32, unique=True, db_index=True)
    name= models.CharField(max_length=255, blank=True, default="")
    source_url = models.URLField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    local_path = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.matterport_id} ({self.status})"

    # @property
    # def local_url(self) -> str:
    #     """Placeholder local link. Not guaranteed to resolve yet — serving
    #     (proxying to a per-tour matterport-dl server) is a later step."""
    #     return f"{settings.PLAYER_PUBLIC_BASE_URL}/tour/{self.matterport_id}/"

    @property
    def local_url(self) -> str:
        """Real, working link to view this tour — resolves via our proxy."""
        return f"{settings.PLAYER_PUBLIC_BASE_URL}/tour/{self.matterport_id}/"
