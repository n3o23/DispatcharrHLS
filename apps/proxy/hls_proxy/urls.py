from django.urls import path
from . import views

app_name = "hls_proxy"

urlpatterns = [
    # Auto-initializing HLS output stream (used by hls_m3u_endpoint)
    path("stream/<str:channel_uuid>", views.auto_stream, name="auto_stream"),
    # Manual init + management
    path("initialize/<str:channel_id>", views.initialize_stream, name="initialize"),
    path("manifest/<str:channel_id>", views.stream_endpoint, name="stream"),
    # Segments — channel_id scoped to avoid cross-channel collision
    path("segments/<str:channel_id>/<path:segment_name>", views.get_segment, name="segment"),
    path("change_stream/<str:channel_id>", views.change_stream, name="change_stream"),
]
