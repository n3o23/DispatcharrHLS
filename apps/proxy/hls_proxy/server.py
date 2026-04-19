"""
HLS Proxy Server - Full HLS Input Support
Handles:
- Standard .m3u8 media playlists
- Multi-variant (master) playlists → selects highest bandwidth rendition
- fMP4 (CMAF) segments in addition to MPEG-TS segments (Pluto, Disney+, etc.)
- EXT-X-DISCONTINUITY across ad breaks without freezing
- Proper absolute URL resolution for all segment/playlist URLs
"""

import requests
import threading
import logging
import m3u8
import time
from urllib.parse import urlparse, urljoin
from typing import Optional, Dict, List
from apps.proxy.config import HLSConfig as Config

logger = logging.getLogger("hls_proxy")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_url(base_url: str, uri: str) -> str:
    """Resolve a potentially-relative URI against the base playlist URL."""
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    return urljoin(base_url, uri)


def _is_master_playlist(manifest: m3u8.M3U8) -> bool:
    """Return True if this is a multi-variant (master) playlist."""
    return bool(manifest.playlists)


def _select_best_rendition(manifest: m3u8.M3U8, base_url: str) -> str:
    """Pick the highest-bandwidth variant from a master playlist."""
    best = max(manifest.playlists, key=lambda p: p.stream_info.bandwidth or 0)
    return _resolve_url(base_url, best.uri)


def verify_segment(data: bytes) -> dict:
    """
    Accept both MPEG-TS (0x47 sync) and fMP4 (ftyp/moof/mdat boxes) segments.
    Returns {'valid': True, 'format': 'ts'|'fmp4', 'size': int}
    """
    if len(data) < 4:
        return {"valid": False, "error": "Segment too short"}

    # fMP4: starts with a valid ISO BMFF box type
    fmp4_boxes = {b"ftyp", b"styp", b"moof", b"mdat", b"sidx", b"emsg"}
    if data[4:8] in fmp4_boxes:
        return {"valid": True, "format": "fmp4", "size": len(data)}

    # MPEG-TS: every 188-byte packet starts with 0x47
    if len(data) < 188:
        return {"valid": False, "error": "TS segment too short"}
    if data[0] != 0x47:
        return {"valid": False, "error": "Not TS (bad sync byte) and not fMP4"}
    if len(data) % 188 != 0:
        # Allow slightly misaligned segments (some servers pad)
        pass
    # Check first few packets only for speed
    for i in range(0, min(len(data), 188 * 10), 188):
        if data[i] != 0x47:
            return {"valid": False, "error": f"Bad sync at packet {i // 188}"}
    return {"valid": True, "format": "ts", "size": len(data)}


# ---------------------------------------------------------------------------
# Core classes
# ---------------------------------------------------------------------------

class StreamBuffer:
    """Thread-safe ring buffer for HLS segments keyed by sequence number."""

    def __init__(self):
        self.buffer: Dict[int, bytes] = {}
        self.lock = threading.Lock()

    def __getitem__(self, key: int) -> Optional[bytes]:
        return self.buffer.get(key)

    def __setitem__(self, key: int, value: bytes):
        self.buffer[key] = value
        if len(self.buffer) > Config.MAX_SEGMENTS:
            oldest = sorted(self.buffer.keys())[: -Config.MAX_SEGMENTS]
            for k in oldest:
                del self.buffer[k]

    def __contains__(self, key: int) -> bool:
        return key in self.buffer

    def keys(self) -> List[int]:
        return list(self.buffer.keys())


class ClientManager:
    """Tracks last-seen timestamps for connected clients."""

    def __init__(self):
        self.last_activity: Dict[str, float] = {}
        self.lock = threading.Lock()

    def record_activity(self, client_ip: str):
        with self.lock:
            if client_ip not in self.last_activity:
                logger.info(f"New HLS client: {client_ip}")
            self.last_activity[client_ip] = time.time()

    def cleanup_inactive(self, timeout: float) -> bool:
        now = time.time()
        with self.lock:
            active = {ip: t for ip, t in self.last_activity.items() if now - t < timeout}
            removed = set(self.last_activity) - set(active)
            for ip in removed:
                logger.info(f"HLS client {ip} timed out")
            self.last_activity = active
            return len(active) == 0


class StreamFetcher:
    """
    Downloads HLS playlists and segments continuously.

    Key improvements over the original:
    - Resolves master playlists automatically
    - Uses urljoin for all segment URLs (fixes Pluto's CDN paths)
    - Accepts fMP4 segments (skips TS-only verify for ad segments)
    - Skips EXT-X-DISCONTINUITY gaps without stalling
    - Tracks downloaded segment URIs to avoid double-downloads
    """

    def __init__(self, manager: "StreamManager", buffer: StreamBuffer):
        self.manager = manager
        self.buffer = buffer
        self.stream_url = manager.current_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": manager.user_agent,
            "Connection": "keep-alive",
        })
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=2, pool_maxsize=4, max_retries=2
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self._downloaded_uris: set = set()
        self._last_request_time: float = 0

    # ------------------------------------------------------------------
    def _get(self, url: str, stream: bool = False, timeout: int = 15) -> requests.Response:
        """Rate-limited GET with a small floor to be polite to CDNs."""
        gap = self._last_request_time + 0.05 - time.time()
        if gap > 0:
            time.sleep(gap)
        resp = self.session.get(url, stream=stream, timeout=timeout,
                                allow_redirects=True)
        self._last_request_time = time.time()
        resp.raise_for_status()
        return resp

    def _fetch_playlist(self, url: str):
        """Download and parse a playlist; follow master→media if needed."""
        resp = self._get(url)
        final_url = resp.url            # after redirects
        pl = m3u8.loads(resp.text)

        if _is_master_playlist(pl):
            media_url = _select_best_rendition(pl, final_url)
            logger.info(f"Master playlist → selected rendition: {media_url}")
            resp2 = self._get(media_url)
            return m3u8.loads(resp2.text), resp2.url

        return pl, final_url

    def _fetch_segment(self, url: str) -> Optional[bytes]:
        try:
            resp = self._get(url, timeout=20)
            data = resp.content
            v = verify_segment(data)
            if v["valid"]:
                return data
            logger.warning(f"Segment validation failed ({v.get('error')}): {url}")
            return None
        except Exception as exc:
            logger.error(f"Segment download error {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    def fetch_loop(self):
        """Main fetch loop: runs until manager.running is False."""
        retry_delay = 1.0
        max_retry_delay = 16.0

        while self.manager.running:
            try:
                pl, base_url = self._fetch_playlist(self.manager.current_url)

                if pl.target_duration:
                    self.manager.target_duration = float(pl.target_duration)
                if pl.version:
                    self.manager.manifest_version = pl.version

                if not pl.segments:
                    time.sleep(1)
                    continue

                # --- Initial buffering pass ---
                if self.manager.initial_buffering:
                    self._do_initial_buffer(pl, base_url)
                    retry_delay = 1.0
                    continue

                # --- Steady-state: grab newest unseen segments ---
                new_segments = [
                    s for s in pl.segments
                    if s.uri not in self._downloaded_uris
                ]

                if not new_segments:
                    # Nothing new; wait half a target-duration
                    time.sleep(max(1.0, self.manager.target_duration * 0.5))
                    continue

                for seg in new_segments:
                    seg_url = _resolve_url(base_url, seg.uri)
                    data = self._fetch_segment(seg_url)
                    if data is None:
                        continue

                    with self.buffer.lock:
                        seq = self.manager.next_sequence
                        # Mark discontinuity if playlist says so
                        if seg.discontinuity:
                            self.manager.source_changes.add(seq)
                            logger.debug(f"Discontinuity at seq {seq} (ad break)")
                        self.buffer[seq] = data
                        self.manager.segment_durations[seq] = float(seg.duration or 6.0)
                        self.manager.buffered_sequences.add(seq)
                        self.manager.next_sequence += 1

                    self._downloaded_uris.add(seg.uri)
                    # Prevent the set growing without bound
                    if len(self._downloaded_uris) > 500:
                        self._downloaded_uris = set(list(self._downloaded_uris)[-200:])

                retry_delay = 1.0
                # Poll at ~half segment duration to stay close to live
                time.sleep(max(0.5, self.manager.target_duration * 0.4))

            except requests.HTTPError as exc:
                logger.error(f"HTTP {exc.response.status_code} fetching playlist")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)
            except Exception as exc:
                logger.error(f"Fetch loop error: {exc}")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    def _do_initial_buffer(self, pl: m3u8.M3U8, base_url: str):
        """Fill the initial buffer with the last N seconds of the playlist."""
        target_secs = Config.INITIAL_BUFFER_SECONDS
        segments_to_get: list = []
        accumulated = 0.0

        for seg in reversed(pl.segments):
            accumulated += float(seg.duration or 6.0)
            segments_to_get.append(seg)
            if accumulated >= target_secs or len(segments_to_get) >= Config.MAX_INITIAL_SEGMENTS:
                break

        segments_to_get.reverse()
        fetched = 0

        for seg in segments_to_get:
            seg_url = _resolve_url(base_url, seg.uri)
            data = self._fetch_segment(seg_url)
            if data is None:
                continue
            with self.buffer.lock:
                seq = self.manager.next_sequence
                self.buffer[seq] = data
                self.manager.segment_durations[seq] = float(seg.duration or 6.0)
                self.manager.buffered_sequences.add(seq)
                self.manager.next_sequence += 1
            self._downloaded_uris.add(seg.uri)
            fetched += 1

        if fetched > 0:
            self.manager.initial_buffering = False
            self.manager.buffer_ready.set()
            logger.info(f"Initial HLS buffer ready: {fetched} segments")
        else:
            logger.warning("Initial buffer got 0 segments, will retry")
            time.sleep(2)


# ---------------------------------------------------------------------------
# StreamManager
# ---------------------------------------------------------------------------

class StreamManager:
    """Coordinates fetch thread, buffer state, and stream switching."""

    def __init__(self, initial_url: str, channel_id: str, user_agent: Optional[str] = None):
        self.current_url = initial_url
        self.channel_id = channel_id
        self.user_agent = user_agent or Config.DEFAULT_USER_AGENT
        self.running = True

        # Sequence tracking
        self.next_sequence = 0
        self.buffered_sequences: set = set()
        self.segment_durations: Dict[int, float] = {}
        self.source_changes: set = set()

        # Manifest metadata
        self.target_duration = 6.0
        self.manifest_version = 3

        # Buffering state
        self.initial_buffering = True
        self.buffer_ready = threading.Event()

        # Threading
        self.fetch_thread: Optional[threading.Thread] = None
        self.url_changed = threading.Event()

        # Client / cleanup bookkeeping
        self.client_manager: Optional[ClientManager] = None
        self.proxy_server = None
        self.first_client_connected = False
        self.cleanup_started = False
        self.cleanup_running = False
        self.cleanup_thread: Optional[threading.Thread] = None

        logger.info(f"StreamManager created for channel {channel_id}")

    def update_url(self, new_url: str) -> bool:
        if new_url == self.current_url:
            return False
        logger.info(f"Stream switch → {new_url}")
        self.current_url = new_url
        # Mark next sequence as discontinuity
        self.source_changes.add(self.next_sequence)
        self.url_changed.set()
        return True

    def enable_cleanup(self):
        if not self.first_client_connected:
            self.first_client_connected = True

    def start(self):
        if not self.fetch_thread or not self.fetch_thread.is_alive():
            self.running = True
            self.fetch_thread = threading.Thread(
                target=self._fetch_loop, daemon=True, name=f"HLS-{self.channel_id}"
            )
            self.fetch_thread.start()

    def _fetch_loop(self):
        while self.running:
            try:
                buf = StreamBuffer()
                # Share the buffer reference back to proxy_server
                if self.proxy_server and self.channel_id in self.proxy_server.stream_buffers:
                    buf = self.proxy_server.stream_buffers[self.channel_id]
                fetcher = StreamFetcher(self, buf)
                fetcher.fetch_loop()
            except Exception as exc:
                logger.error(f"Fetch thread error: {exc}")
                time.sleep(5)
            self.url_changed.clear()

    def stop(self):
        self.running = False
        self.cleanup_running = False
        self.url_changed.set()
        if self.fetch_thread and self.fetch_thread.is_alive():
            self.fetch_thread.join(timeout=5)

    def start_cleanup_thread(self):
        if self.cleanup_started:
            return
        self.cleanup_started = True
        self.cleanup_running = True

        def loop():
            deadline = time.time() + Config.INITIAL_CONNECTION_WINDOW
            while self.cleanup_running and time.time() < deadline:
                if self.first_client_connected:
                    break
                time.sleep(1)

            if not self.first_client_connected:
                logger.info(f"Channel {self.channel_id}: no clients in window, shutting down")
                if self.proxy_server:
                    self.proxy_server.stop_channel(self.channel_id)
                return

            while self.cleanup_running and self.running:
                timeout = self.target_duration * Config.CLIENT_TIMEOUT_FACTOR
                if self.client_manager and self.client_manager.cleanup_inactive(timeout):
                    logger.info(f"Channel {self.channel_id}: all clients gone, shutting down")
                    if self.proxy_server:
                        self.proxy_server.stop_channel(self.channel_id)
                    break
                time.sleep(Config.CLIENT_CLEANUP_INTERVAL)

        self.cleanup_thread = threading.Thread(
            target=loop, daemon=True, name=f"HLS-cleanup-{self.channel_id}"
        )
        self.cleanup_thread.start()


# ---------------------------------------------------------------------------
# ProxyServer
# ---------------------------------------------------------------------------

class ProxyServer:
    """Registry of active HLS channels."""

    def __init__(self, user_agent: Optional[str] = None):
        self.stream_managers: Dict[str, StreamManager] = {}
        self.stream_buffers: Dict[str, StreamBuffer] = {}
        self.client_managers: Dict[str, ClientManager] = {}
        self.user_agent = user_agent or Config.DEFAULT_USER_AGENT

    def initialize_channel(self, url: str, channel_id: str) -> None:
        if channel_id in self.stream_managers:
            self.stop_channel(channel_id)

        buf = StreamBuffer()
        cm = ClientManager()
        sm = StreamManager(url, channel_id, user_agent=self.user_agent)
        sm.client_manager = cm
        sm.proxy_server = self

        self.stream_buffers[channel_id] = buf
        self.client_managers[channel_id] = cm
        self.stream_managers[channel_id] = sm

        # Start fetcher
        fetcher = StreamFetcher(sm, buf)
        t = threading.Thread(
            target=fetcher.fetch_loop, daemon=True, name=f"HLS-fetch-{channel_id}"
        )
        t.start()

        sm.start_cleanup_thread()
        logger.info(f"Initialized HLS channel {channel_id} → {url}")

    def stop_channel(self, channel_id: str) -> None:
        sm = self.stream_managers.get(channel_id)
        if sm:
            try:
                sm.stop()
            except Exception as exc:
                logger.error(f"Error stopping channel {channel_id}: {exc}")
            finally:
                self._cleanup_channel(channel_id)

    def _cleanup_channel(self, channel_id: str):
        for d in (self.stream_managers, self.stream_buffers, self.client_managers):
            d.pop(channel_id, None)

    def shutdown(self):
        for cid in list(self.stream_managers):
            self.stop_channel(cid)

    # ---- Endpoint helpers (called from views) ----

    def stream_endpoint(self, channel_id: str, client_ip: str = "unknown"):
        """Build and return an HLS manifest for the given channel."""
        sm = self.stream_managers.get(channel_id)
        if not sm:
            return "Channel not found", 404

        # Wait for initial buffer
        if not sm.buffer_ready.wait(Config.BUFFER_READY_TIMEOUT):
            return "Initial buffer not ready", 503

        sm.enable_cleanup()
        if cm := self.client_managers.get(channel_id):
            cm.record_activity(client_ip)

        buf = self.stream_buffers.get(channel_id)
        if not buf:
            return "Channel not found", 404

        with buf.lock:
            available = sorted(buf.keys())

        if not available:
            return "No segments available", 503

        max_seq = available[-1]
        min_seq = max(available[0], max_seq - Config.WINDOW_SIZE + 1)
        window = [s for s in available if min_seq <= s <= max_seq]

        lines = [
            "#EXTM3U",
            f"#EXT-X-VERSION:{sm.manifest_version}",
            f"#EXT-X-TARGETDURATION:{int(sm.target_duration)}",
            f"#EXT-X-MEDIA-SEQUENCE:{min_seq}",
        ]
        for seq in window:
            if seq in sm.source_changes:
                lines.append("#EXT-X-DISCONTINUITY")
            dur = sm.segment_durations.get(seq, sm.target_duration)
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"/proxy/hls/segments/{channel_id}/{seq}.ts")

        return "\n".join(lines), 200

    def get_segment(self, channel_id: str, seq: int, client_ip: str = "unknown"):
        """Serve a single segment by sequence number."""
        if channel_id not in self.stream_managers:
            return None, 404
        if cm := self.client_managers.get(channel_id):
            cm.record_activity(client_ip)
        buf = self.stream_buffers.get(channel_id)
        if not buf:
            return None, 404
        with buf.lock:
            data = buf[seq]
        if data is None:
            return None, 404
        return data, 200
