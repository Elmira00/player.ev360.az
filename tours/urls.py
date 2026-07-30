from django.urls import path
from . import views

urlpatterns = [
    path("", views.submit_view, name="submit"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("dashboard/status.json", views.tours_status_json, name="tours_status_json"),
    path("tour/<int:tour_id>/delete/", views.delete_tour_view, name="delete_tour"),
    path("tour/<int:tour_id>/edit/", views.edit_tour_view, name="edit_tour"),
    path("tour/<str:matterport_id>/", views.tour_proxy_view, name="tour_proxy"),
    path("tour/<str:matterport_id>/<path:subpath>", views.tour_proxy_view, name="tour_proxy_sub"),
    path("<path:subpath>", views.root_asset_proxy_view, name="root_asset_proxy"),  # catch-all, must stay LAST
]