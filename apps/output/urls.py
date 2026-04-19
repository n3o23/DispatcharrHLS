from django.urls import path, re_path
from .views import m3u_endpoint, hls_m3u_endpoint, epg_endpoint, xc_get, xc_movie_stream, xc_series_stream
from core.views import stream_view

app_name = "output"

urlpatterns = [
    # Standard TS-proxy M3U
    re_path(r"^m3u(?:/(?P<profile_name>[^/]+))?/?$", m3u_endpoint, name="m3u_endpoint"),
    # HLS-output M3U (stream URLs point to HLS proxy)
    re_path(r"^m3u_hls(?:/(?P<profile_name>[^/]+))?/?$", hls_m3u_endpoint, name="hls_m3u_endpoint"),
    # EPG
    re_path(r"^epg(?:/(?P<profile_name>[^/]+))?/?$", epg_endpoint, name="epg_endpoint"),
    # Stream view
    re_path(r"^stream/(?P<channel_uuid>[0-9a-fA-F\-]+)/?$", stream_view, name="stream"),
]
