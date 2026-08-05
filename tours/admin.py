from django.contrib import admin
from .models import Tour, TourUpload


class TourUploadInline(admin.TabularInline):
    model = TourUpload
    extra = 0
    readonly_fields = ("source_type", "status", "created_at", "updated_at")
    fields = ("source_type", "status", "source_url", "original_zip_name", "created_at")


@admin.register(Tour)
class TourAdmin(admin.ModelAdmin):
    list_display = ("matterport_id", "name", "created_at")
    search_fields = ("matterport_id", "name")
    inlines = [TourUploadInline]


@admin.register(TourUpload)
class TourUploadAdmin(admin.ModelAdmin):
    list_display = ("tour", "source_type", "status", "created_at")
    list_filter = ("source_type", "status")
    search_fields = ("tour__matterport_id", "tour__name")