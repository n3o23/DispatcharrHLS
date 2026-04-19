"""
Migration: add hardware-accelerated transcode stream profiles.

Adds locked read-only profiles for:
  - NVIDIA NVENC (h264_nvenc)
  - Intel QSV  (h264_qsv)
  - AMD/Intel VA-API (h264_vaapi)
  - Apple VideoToolbox (h264_videotoolbox)  -- macOS host passthrough
  - HEVC variants for NVENC, QSV, VA-API

All profiles pipe MPEG-TS to stdout so they slot into the existing
StreamProfile.build_command() / ts_proxy pipeline unchanged.
"""

from django.db import migrations


HW_PROFILES = [
    # ── NVIDIA NVENC ──────────────────────────────────────────────────────────
    {
        "name": "FFmpeg NVENC H.264",
        "command": "ffmpeg",
        "parameters": (
            "-hwaccel cuda -hwaccel_output_format cuda "
            "-user_agent {userAgent} -i {streamUrl} "
            "-c:v h264_nvenc -preset p4 -rc vbr -cq 23 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    {
        "name": "FFmpeg NVENC HEVC",
        "command": "ffmpeg",
        "parameters": (
            "-hwaccel cuda -hwaccel_output_format cuda "
            "-user_agent {userAgent} -i {streamUrl} "
            "-c:v hevc_nvenc -preset p4 -rc vbr -cq 28 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    # ── Intel QSV ─────────────────────────────────────────────────────────────
    {
        "name": "FFmpeg QSV H.264",
        "command": "ffmpeg",
        "parameters": (
            "-hwaccel qsv -hwaccel_output_format qsv "
            "-user_agent {userAgent} -i {streamUrl} "
            "-c:v h264_qsv -global_quality 23 -look_ahead 1 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    {
        "name": "FFmpeg QSV HEVC",
        "command": "ffmpeg",
        "parameters": (
            "-hwaccel qsv -hwaccel_output_format qsv "
            "-user_agent {userAgent} -i {streamUrl} "
            "-c:v hevc_qsv -global_quality 28 -look_ahead 1 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    # ── VA-API (Intel/AMD on Linux) ───────────────────────────────────────────
    {
        "name": "FFmpeg VA-API H.264",
        "command": "ffmpeg",
        "parameters": (
            "-hwaccel vaapi -hwaccel_device /dev/dri/renderD128 "
            "-hwaccel_output_format vaapi "
            "-user_agent {userAgent} -i {streamUrl} "
            "-vf 'format=nv12|vaapi,hwupload' "
            "-c:v h264_vaapi -qp 23 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    {
        "name": "FFmpeg VA-API HEVC",
        "command": "ffmpeg",
        "parameters": (
            "-hwaccel vaapi -hwaccel_device /dev/dri/renderD128 "
            "-hwaccel_output_format vaapi "
            "-user_agent {userAgent} -i {streamUrl} "
            "-vf 'format=nv12|vaapi,hwupload' "
            "-c:v hevc_vaapi -qp 28 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    # ── Apple VideoToolbox (macOS host) ──────────────────────────────────────
    {
        "name": "FFmpeg VideoToolbox H.264",
        "command": "ffmpeg",
        "parameters": (
            "-user_agent {userAgent} -i {streamUrl} "
            "-c:v h264_videotoolbox -q:v 65 "
            "-c:a copy -f mpegts pipe:1"
        ),
    },
    # ── Copy-only (stream remux, no transcode) ────────────────────────────────
    {
        "name": "FFmpeg Copy (Remux)",
        "command": "ffmpeg",
        "parameters": (
            "-user_agent {userAgent} -i {streamUrl} "
            "-c copy -f mpegts pipe:1"
        ),
    },
]


def add_hw_profiles(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")

    for p in HW_PROFILES:
        if not StreamProfile.objects.filter(name=p["name"]).exists():
            StreamProfile.objects.create(
                name=p["name"],
                command=p["command"],
                parameters=p["parameters"],
                locked=True,
                is_active=True,
            )


def remove_hw_profiles(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")
    StreamProfile.objects.filter(name__in=[p["name"] for p in HW_PROFILES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "022_default_user_limit_settings"),
    ]

    operations = [
        migrations.RunPython(add_hw_profiles, remove_hw_profiles),
    ]
