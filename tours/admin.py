from django.contrib import admin

from .models import MatterportTour


@admin.register(MatterportTour)
class MatterportTourAdmin(admin.ModelAdmin):
    list_display = ("matterport_id", "status", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("matterport_id", "source_url")
