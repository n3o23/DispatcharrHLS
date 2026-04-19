import json
import logging
from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view, permission_classes
from apps.accounts.permissions import IsAdmin
from .server import ProxyServer

logger = logging.getLogger("hls_proxy")
proxy_server = ProxyServer()


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


@csrf_exempt
@require_http_methods(["GET"])
def stream_endpoint(request, channel_id):
    """Serve the HLS manifest for a channel."""
    content, status = proxy_server.stream_endpoint(channel_id, _client_ip(request))
    if status != 200:
        return JsonResponse({"error": content}, status=status)
    return HttpResponse(content, content_type="application/vnd.apple.mpegurl", status=200)


@csrf_exempt
@require_http_methods(["GET"])
def get_segment(request, channel_id, segment_name):
    """Serve a single MPEG-TS or fMP4 segment."""
    try:
        seq = int(segment_name.split(".")[0])
    except (ValueError, IndexError):
        return JsonResponse({"error": "Invalid segment name"}, status=400)

    data, status = proxy_server.get_segment(channel_id, seq, _client_ip(request))
    if status != 200 or data is None:
        return JsonResponse({"error": "Segment not found"}, status=status)

    return HttpResponse(data, content_type="video/MP2T", status=200)


@api_view(["POST"])
@permission_classes([IsAdmin])
def change_stream(request, channel_id):
    """Switch the stream URL for an active channel."""
    if channel_id not in proxy_server.stream_managers:
        return JsonResponse({"error": "Channel not found"}, status=404)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        data = request.data or {}
    new_url = data.get("url")
    if not new_url:
        return JsonResponse({"error": "No URL provided"}, status=400)
    changed = proxy_server.stream_managers[channel_id].update_url(new_url)
    return JsonResponse({
        "message": "Stream URL updated" if changed else "URL unchanged",
        "channel": channel_id,
        "url": new_url,
    })


@csrf_exempt
@require_http_methods(["POST"])
def initialize_stream(request, channel_id):
    """Initialize a new HLS channel."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    url = data.get("url")
    if not url:
        return JsonResponse({"error": "No URL provided"}, status=400)
    try:
        proxy_server.initialize_channel(url, channel_id)
        return JsonResponse({"message": "Stream initialized", "channel": channel_id, "url": url})
    except Exception as exc:
        logger.error(f"Failed to initialize channel {channel_id}: {exc}")
        return JsonResponse({"error": str(exc)}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def auto_stream(request, channel_uuid):
    """
    Auto-initializing HLS stream endpoint.
    Called by HLS-output M3U clients. On first request the channel is
    initialized from the channel's primary stream URL; subsequent
    requests serve the live manifest.
    """
    from apps.channels.models import Channel

    # Use channel UUID as the HLS channel_id
    channel_id = str(channel_uuid)

    # Auto-initialize if not running
    if channel_id not in proxy_server.stream_managers:
        try:
            channel = Channel.objects.select_related("stream_profile").get(uuid=channel_uuid)
            # Pick the first active stream URL
            stream = channel.streams.order_by("channelstream__order").first()
            if not stream or not stream.url:
                return JsonResponse({"error": "No stream URL configured"}, status=503)
            proxy_server.initialize_channel(stream.url, channel_id)
        except Channel.DoesNotExist:
            return JsonResponse({"error": "Channel not found"}, status=404)
        except Exception as exc:
            logger.error(f"Auto-init failed for {channel_id}: {exc}")
            return JsonResponse({"error": "Failed to initialize stream"}, status=503)

    content, status = proxy_server.stream_endpoint(channel_id, _client_ip(request))
    if status != 200:
        return JsonResponse({"error": content}, status=status)
    return HttpResponse(content, content_type="application/vnd.apple.mpegurl", status=200)
