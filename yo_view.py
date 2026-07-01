#!/usr/bin/env python3
"""
yo_view.py - view the YO live feed and record it at the same time.

Usage:
  python3 yo_view.py
  python3 yo_view.py --out session.mp4
  python3 yo_view.py --host 192.168.0.1

Join the device's "Yo2" WiFi first. The recording is written to the current
directory by default: ./yo_live_<timestamp>.mp4
Requires ffmpeg/ffprobe/ffplay: brew install ffmpeg
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime


DEFAULT_API_PORT = 12913
DEFAULT_RTSP_PORT = 554
DEFAULT_DEVICE_HOST = "192.168.0.1"  # observed YO v2 address when gateway lookup fails
DEFAULT_TIMEOUT = 6.0
STREAM_API_TIMEOUT = 0.5
RTSP_PROBE_TIMEOUT = 5
STREAM_SETTLE_SEC = 1.0
PROBE_RETRY_DELAY = 0.5
DEFAULT_STREAM_CYCLES = 8
PROBES_PER_CYCLE = 30

def log(message=""):
    print(message, file=sys.stderr, flush=True)


def die(message, code=1):
    print(f"\nERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def api_url(host, port, path):
    return f"http://{host}:{port}/{path.lstrip('/')}"


def http_get(url, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "yo_view/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return resp.status, body


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_gateway():
    try:
        out = subprocess.check_output(
            ["route", "-n", "get", "default"], stderr=subprocess.DEVNULL
        ).decode()
        match = re.search(r"gateway:\s*([0-9.]+)", out)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def device_host():
    """On Yo2 WiFi the device is the default gateway."""
    return default_gateway() or DEFAULT_DEVICE_HOST


def resolve_host(args):
    if args.host:
        log(f"Using device host from --host: {args.host}")
        return args.host

    host = device_host()
    log("Finding YO device on the local network...")
    log("Tip: your Mac should be joined to the device's 'Yo2' WiFi.")
    log(f"  Checking {host}:{args.api_port} ...")
    if not port_open(host, args.api_port):
        die(
            f"Could not reach {host}:{args.api_port}.\n"
            "Make sure you are on the 'Yo2' WiFi, wait a few seconds, then retry.\n"
            f"If the device IP differs, pass it explicitly: python3 yo_view.py --host {DEFAULT_DEVICE_HOST}"
        )
    try:
        status, _ = get_info(host, args.api_port)
        if status == 200:
            log(f"Found YO device at {host}")
            return host
    except Exception as exc:
        if args.verbose:
            log(f"    /getInfo failed on {host}: {exc}")

    die(
        f"Could not read /getInfo from {host}.\n"
        "Make sure you are on the 'Yo2' WiFi, wait a few seconds, then retry.\n"
        f"If you know the device IP, pass it explicitly: python3 yo_view.py --host {DEFAULT_DEVICE_HOST}"
    )


def pretty_body(body):
    body = (body or "").strip()
    if not body:
        return "  (empty response)"

    try:
        return json.dumps(json.loads(body), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        pass

    if "\n" in body:
        return "\n".join(f"  {line}" for line in body.splitlines())

    # Some firmware responses are compact key/value-ish strings.
    parts = [part.strip() for part in re.split(r"[&;]", body) if part.strip()]
    if len(parts) > 1:
        return "\n".join(f"  {part}" for part in parts)

    return f"  {body}"


def get_info(host, api_port):
    url = api_url(host, api_port, "getInfo")
    status, body = http_get(url, timeout=DEFAULT_TIMEOUT)
    return status, body


def print_device_info(host, api_port):
    url = api_url(host, api_port, "getInfo")
    log(f"\nReading device info: {url}")
    try:
        status, body = get_info(host, api_port)
    except urllib.error.URLError as exc:
        die(f"Could not read /getInfo from {host}: {exc}")
    except Exception as exc:
        die(f"Could not read /getInfo from {host}: {exc}")

    log(f"/getInfo HTTP {status}")
    print("\nDevice info:")
    print(pretty_body(body))
    return body


def require_tool(tool):
    try:
        subprocess.check_output([tool, "-version"], stderr=subprocess.STDOUT)
    except Exception:
        die(
            f"Missing {tool}. Install ffmpeg first:\n"
            "  brew install ffmpeg"
        )


def start_stream(host, api_port):
    url = api_url(host, api_port, "startStream")
    log(f"\nStarting stream: {url}")
    try:
        status, body = http_get(url, timeout=STREAM_API_TIMEOUT)
        log(f"/startStream HTTP {status}")
        if body.strip():
            log("Response:")
            log(pretty_body(body))
    except Exception as exc:
        log(f"/startStream did not return cleanly: {exc}")
        log("Continuing anyway; this device often starts streaming even when the HTTP call times out.")


def stop_stream(host, api_port):
    url = api_url(host, api_port, "stopStream")
    log(f"\nStopping stream: {url}")
    try:
        status, body = http_get(url, timeout=STREAM_API_TIMEOUT)
        log(f"/stopStream HTTP {status}")
        if body.strip():
            log("Response:")
            log(pretty_body(body))
    except Exception as exc:
        log(f"/stopStream did not return cleanly: {exc}")
        log("Continuing anyway; this device often stops streaming even when the HTTP call times out.")


def rtsp_url(host, rtsp_port, path):
    if path:
        return f"rtsp://{host}:{rtsp_port}/{path}"
    return f"rtsp://{host}:{rtsp_port}"


@dataclass
class ProbeResult:
    ok: bool
    timed_out: bool
    returncode: int | None
    stdout: str
    stderr: str


def probe_rtsp(url, timeout):
    cmd = [
        "ffprobe",
        "-rtsp_transport",
        "tcp",
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=codec_type,width,height,avg_frame_rate",
        "-of",
        "json",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired as exc:
        return ProbeResult(False, True, None, exc.stdout or "", exc.stderr or "")

    ok = proc.returncode == 0 and '"codec_type": "video"' in (proc.stdout or "")
    return ProbeResult(ok, False, proc.returncode, proc.stdout or "", proc.stderr or "")


def summarize_probe(result):
    if result.timed_out:
        return "timed out"
    if result.ok:
        try:
            data = json.loads(result.stdout)
            stream = (data.get("streams") or [{}])[0]
            size = ""
            if stream.get("width") and stream.get("height"):
                size = f" {stream['width']}x{stream['height']}"
            fps = stream.get("avg_frame_rate")
            fps_text = f" {fps} fps" if fps and fps != "0/0" else ""
            return f"video stream found{size}{fps_text}"
        except Exception:
            return "video stream found"
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    if detail:
        return detail[-1]
    return f"ffprobe exited {result.returncode}"


def find_rtsp_url(
    host,
    api_port,
    rtsp_port,
    probe_timeout,
    stream_cycles=DEFAULT_STREAM_CYCLES,
    probes_per_cycle=PROBES_PER_CYCLE,
):
    url = rtsp_url(host, rtsp_port, "")
    total_probes = stream_cycles * probes_per_cycle

    log("\nFinding live RTSP stream...")
    probe_num = 0
    for cycle in range(1, stream_cycles + 1):
        for probe in range(1, probes_per_cycle + 1):
            probe_num += 1
            log(f"  Probing {url} ({probe_num}/{total_probes})")
            result = probe_rtsp(url, probe_timeout)
            log(f"    {summarize_probe(result)}")
            if result.ok:
                return url
            if probe < probes_per_cycle:
                time.sleep(PROBE_RETRY_DELAY)

        if cycle < stream_cycles:
            log("  RTSP not ready; restarting stream...")
            stop_stream(host, api_port)
            start_stream(host, api_port)
            time.sleep(STREAM_SETTLE_SEC)

    die(
        "No RTSP stream answered.\n"
        f"Try opening VLC -> Open Network -> rtsp://{host}:{rtsp_port}\n"
        "If that also fails, power-cycle the YO, rejoin 'Yo2', wait 30 seconds, and retry."
    )


def default_output_path():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.abspath(f"yo_live_{stamp}.mp4")


def resolve_output_path(path):
    if path:
        out_path = os.path.abspath(os.path.expanduser(path))
    else:
        out_path = default_output_path()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    return out_path


def run_view_and_record(url, out_path):
    log(f"\nRecording to: {out_path}")
    log("Opening live view. Press q in the video window, or Ctrl-C here, to stop.")

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        "-fflags",
        "nobuffer",
        "-i",
        url,
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "tee",
        f"[movflags=+frag_keyframe+empty_moov+default_base_moof]{out_path}|[f=mpegts]pipe:1",
    ]
    ffplay_cmd = [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-framedrop",
        "-window_title",
        "YO live (recording)",
        "-i",
        "pipe:0",
    ]

    ffmpeg = ffplay = None
    try:
        ffmpeg = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE)
        ffplay = subprocess.Popen(ffplay_cmd, stdin=ffmpeg.stdout)
        ffmpeg.stdout.close()
        ffplay.wait()
    except KeyboardInterrupt:
        log("\nInterrupted; closing viewer and finalizing recording...")
    finally:
        for proc in (ffplay, ffmpeg):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        size_mb = os.path.getsize(out_path) / 1_048_576
        print(f"\nSaved recording: {out_path} ({size_mb:.1f} MB)")
    else:
        die(
            "No recording was written.\n"
            "The RTSP probe succeeded, but ffmpeg did not receive video data. "
            "Try running again after power-cycling the YO."
        )


def build_parser():
    parser = argparse.ArgumentParser(
        description="View the YO live stream and record it at the same time.",
        epilog=(
            "Quick start:\n"
            "  1. Join the YO device's 'Yo2' WiFi.\n"
            "  2. Run: python3 yo_view.py\n"
            "  3. Press q in the viewer to stop. The MP4 is saved in this directory.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", help="device IP; default is auto-detect")
    parser.add_argument("--out", help="output MP4; default ./yo_live_<timestamp>.mp4")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--rtsp-port", type=int, default=DEFAULT_RTSP_PORT)
    parser.add_argument("--probe-timeout", type=float, default=RTSP_PROBE_TIMEOUT)
    parser.add_argument(
        "--stream-cycles",
        type=int,
        default=DEFAULT_STREAM_CYCLES,
        help="stop/start cycles before giving up (default: %(default)s)",
    )
    parser.add_argument(
        "--probes-per-cycle",
        type=int,
        default=PROBES_PER_CYCLE,
        help="RTSP probes per cycle before restarting stream (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    for tool in ("ffmpeg", "ffprobe", "ffplay"):
        require_tool(tool)

    host = resolve_host(args)
    print_device_info(host, args.api_port)
    stop_stream(host, args.api_port)
    start_stream(host, args.api_port)
    time.sleep(STREAM_SETTLE_SEC)

    out_path = resolve_output_path(args.out)
    try:
        url = find_rtsp_url(
            host,
            args.api_port,
            args.rtsp_port,
            probe_timeout=args.probe_timeout,
            stream_cycles=args.stream_cycles,
            probes_per_cycle=args.probes_per_cycle,
        )
        log(f"\nUsing stream URL: {url}")
        run_view_and_record(url, out_path)
    finally:
        stop_stream(host, args.api_port)


if __name__ == "__main__":
    main()
