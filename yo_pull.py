#!/usr/bin/env python3
"""
yo_pull.py — pull videos off a YO v2.0 WiFi sperm-test device onto a Mac.

The YO v2.0 testing device is a standalone Anyka-based Linux camera (an IP-camera /
DVR at heart). It broadcasts an SSID named "Yo2" and the companion app talks to it
over WiFi. There is NO USB data path (the USB port is power-only), so everything
here happens over the local network once your Mac is associated with the device.

Services exposed by the device (per public teardown of the firmware):
  * HTTP control API on TCP 12913  (/getInfo, /getClips, /startRecord, ...)
  * thttpd file server on TCP 80   (serves the webroot /mnt where clips land)
  * anonymous FTP on TCP 21        (ftpd -w /  -> rooted at filesystem '/')
  * RTSP live stream on TCP 554    (after /startStream)

Because the exact /getClips response shape and the precise clip subdirectory are
not documented, this tool tries several strategies and falls back gracefully:
  list/pull  -> ask the API, then fall back to walking FTP for *.mp4
  stream     -> kick /startStream and capture RTSP with ffmpeg

Stdlib only (urllib, ftplib, socket, subprocess). ffmpeg is only needed for
`stream`. Tested target: python3 that ships with macOS.

QUICK START
  1. Power on the YO v2.0 device.
  2. On your Mac, click the WiFi menu and join the network named "Yo2"
     (this disconnects you from normal internet while connected -- expected).
  3. Run:   python3 yo_pull.py setup      # checks the connection, finds the IP
  4. Then:  python3 yo_pull.py ftp-grab --out ~/Desktop/yo_videos
            python3 yo_pull.py view       # watch the live feed

  For the full walkthrough at any time:   python3 yo_pull.py guide

HOW DO I KNOW THE DEVICE'S IP ADDRESS?
  You usually don't need it -- the tool auto-detects it. When your Mac joins the
  "Yo2" WiFi, the DEVICE itself acts as the router, so its IP is whatever your Mac
  lists as the "Router" / default gateway. To see it yourself:
    GUI : System Settings -> WiFi -> (Yo2) Details... -> TCP/IP -> "Router"
    CLI : route -n get default | awk '/gateway/{print $2}'
  Pass it explicitly with --host if auto-detect ever fails.

Usage examples:
  python3 yo_pull.py setup                                    # connection doctor
  python3 yo_pull.py guide                                    # full instructions
  python3 yo_pull.py info
  python3 yo_pull.py list
  python3 yo_pull.py pull --all --out ~/Desktop/yo_videos
  python3 yo_pull.py pull clip_0003.mp4 --out ~/Desktop/yo_videos
  python3 yo_pull.py ftp-grab --out ~/Desktop/yo_videos      # robust brute pull
  python3 yo_pull.py view                                     # live view (ffplay)
  python3 yo_pull.py stream --duration 30 --out ~/Desktop/live.mp4
  python3 yo_pull.py --host 192.168.1.1 list                  # skip autodetect

This tool only talks to a device you own on your own local network. It reads back
your own recordings; it does not exploit or modify the device.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from ftplib import FTP, error_perm

DEFAULT_API_PORT = 12913
DEFAULT_HTTP_PORT = 80
DEFAULT_RTSP_PORT = 554
DEFAULT_FTP_PORT = 21
DEFAULT_TIMEOUT = 6.0

# Candidate device IPs to probe when --host is not given. When your Mac is joined
# to the "Yo2" SoftAP the device is the gateway; common SoftAP gateway addresses
# are tried plus whatever the OS reports as the default gateway.
CANDIDATE_HOSTS = [
    "192.168.1.1",
    "192.168.0.1",
    "192.168.4.1",
    "10.0.0.1",
    "192.168.43.1",
    "192.168.86.11",  # value seen in the device's U-Boot env during teardown
]

MP4_RE = re.compile(r"[\w./\-]+\.mp4", re.IGNORECASE)


SETUP_GUIDE = r"""
==============================================================================
 YO v2.0 WiFi device — setup & usage guide
==============================================================================

WHAT THIS IS
  The YO v2.0 testing device is a small standalone WiFi camera (a Linux
  IP-camera/DVR internally). The phone app normally talks to it over WiFi.
  This tool does the same thing from your Mac: it lists, downloads, and plays
  the sperm videos the device records. The USB port on the device is
  POWER ONLY — there is no USB data transfer — so everything happens over WiFi.

ONE-TIME SETUP
  Requirements:
    - A Mac with Python 3 (the built-in /usr/bin/python3 is fine).
    - For live viewing / recording only: ffmpeg
        Install Homebrew if needed:  https://brew.sh
        Then:  brew install ffmpeg
      (Downloading saved clips does NOT need ffmpeg.)

  No Python packages to install — this tool uses the standard library only.

------------------------------------------------------------------------------
STEP 1 — Power on the device and connect your Mac to its WiFi
------------------------------------------------------------------------------
  1. Plug in / power on the YO v2.0 device and give it ~30 seconds to boot.
  2. On your Mac, click the WiFi icon in the menu bar.
  3. Join the network named "Yo2".
       - It may be open (no password). If it asks for one, check the device /
         its manual; some units use the device serial or a printed key.
       - macOS will warn there's no internet on this network — that's expected.
         The device IS the network. Stay on it while you transfer.
  4. (Optional) If you want internet at the same time, plug your Mac into
     ethernet / a dock and keep "Yo2" on WiFi. Then traffic to the device goes
     over WiFi and the internet goes over ethernet.

  When you're done pulling videos, just rejoin your normal WiFi.

------------------------------------------------------------------------------
STEP 2 — Find the device's IP address  (you usually DON'T have to)
------------------------------------------------------------------------------
  Easiest: don't. Run the tool with no --host and it auto-detects:
       python3 yo_pull.py info
  It looks at your Mac's default gateway (which, on the "Yo2" network, is the
  device itself) plus a list of common addresses, and probes each one.

  If auto-detect fails, find the IP manually — any of these work:

  A) System Settings (clicky way):
       System Settings -> WiFi -> (Yo2) Details... -> TCP/IP
       The "Router" address is the device. Use that as --host.

  B) Terminal one-liners (whichever you like):
       route -n get default | awk '/gateway/ {print $2}'
       ipconfig getoption en0 router        # en0 = your WiFi interface
       netstat -rn | awk '/^default/ {print $2; exit}'

       To confirm en0 is really your WiFi port:
       networksetup -listallhardwareports

  C) See every device on the little network (the YO is the only other one):
       arp -a
     Look for an entry on the same subnet as your Mac; that's the device.

  Then pass it explicitly, e.g.:
       python3 yo_pull.py --host 192.168.1.1 info

------------------------------------------------------------------------------
STEP 3 — Confirm you can reach it
------------------------------------------------------------------------------
       python3 yo_pull.py info
  You should get a /getInfo response from the device. If this works, you're set.

==============================================================================
 COMMANDS
==============================================================================
  info        Print the device's /getInfo (a quick "are we connected?" check).
                python3 yo_pull.py info

  list        List the recordings on the device. Tries the device's /getClips
              API first; if that's unhelpful, walks the device's FTP for *.mp4.
                python3 yo_pull.py list

  pull        Download specific clips (or all of them) to a folder. Tries HTTP
              first, falls back to FTP for anything it can't fetch that way.
                python3 yo_pull.py pull --all --out ~/Desktop/yo_videos
                python3 yo_pull.py pull clip_0003.mp4 --out ~/Desktop/yo_videos

  ftp-grab    Most robust download: ignore the API entirely, walk the device
              filesystem over FTP and pull EVERY .mp4. Use this if `pull`/`list`
              come up empty.
                python3 yo_pull.py ftp-grab --out ~/Desktop/yo_videos

  view        Watch the LIVE feed in real time AND record it to disk at the same
              time (needs ffmpeg + ffplay). One RTSP connection is teed to both a
              window and a file, so the camera isn't asked to serve two clients.
              Press q (or Ctrl-C) to stop; the recording stays valid even if you
              stop mid-stream.
                python3 yo_pull.py view
                python3 yo_pull.py view --out ~/Desktop/session.mp4
              Default recording path: ~/Desktop/yo_live_<timestamp>.mp4

  stream      Record the live feed to a file for N seconds, no window (needs ffmpeg).
                python3 yo_pull.py stream --duration 30 --out ~/Desktop/live.mp4

  setup       Print this guide.
                python3 yo_pull.py setup

  Global option:  --host <ip>   skip auto-detect and target a specific address.
  Add -v / --verbose for extra diagnostics (e.g. raw /getClips output).

==============================================================================
 VIEWING LIVE — alternatives without this tool
==============================================================================
  The device only streams after it's told to. Trigger it, then point a player
  at its RTSP port (554). On this firmware the working URL is the path-less
  rtsp://<device-ip>:554 ; other builds may use a path, so a few are listed.

  VLC (no terminal):
    1. curl http://<device-ip>:12913/startStream
    2. VLC -> File -> Open Network -> rtsp://<device-ip>:554
       (if blank, try /live, /11, /12, /0, /stream)
    3. VLC -> Settings -> Input/Codecs -> Live555: set "RTSP over TCP".

  ffplay (terminal):
    ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay \
        rtsp://<device-ip>:554

  Note: expect a mostly pink/blank image unless a prepared slide is seated and
  in focus — the camera is just looking at its light source otherwise.

==============================================================================
 TROUBLESHOOTING
==============================================================================
  "Could not find the YO device"
    - Are you actually joined to the "Yo2" WiFi? (Not your home network.)
    - Wait for the device to finish booting, then retry.
    - Find the IP manually (Step 2) and pass --host.

  list / pull return nothing
    - Use `ftp-grab` — it's API-independent.
    - Re-run `list -v` to dump the raw /getClips body so the parser can be
      tuned to your firmware.

  view / stream can't connect
    - Make sure ffmpeg is installed (brew install ffmpeg).
    - Try VLC against rtsp://<device-ip>:554/live as a cross-check.

  Connection keeps dropping when you join "Yo2"
    - Some units briefly de-auth you. Re-join and wait a few seconds; using a
      wired connection for your normal internet (Step 1.4) avoids the tug-of-war.

  This tool only talks to YOUR device on YOUR local network and only reads back
  your own recordings.
==============================================================================
"""


# --------------------------------------------------------------------------- #
# Networking helpers
# --------------------------------------------------------------------------- #
def _http_get(url, timeout=DEFAULT_TIMEOUT, binary=False):
    """GET a URL. Returns (status, bytes_or_text). Raises on transport error."""
    req = urllib.request.Request(url, headers={"User-Agent": "yo_pull/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        return resp.status, (data if binary else data.decode("utf-8", "replace"))


def _port_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_gateway():
    """Best-effort default-gateway lookup on macOS (and Linux fallback)."""
    try:
        out = subprocess.check_output(
            ["route", "-n", "get", "default"], stderr=subprocess.DEVNULL
        ).decode()
        m = re.search(r"gateway:\s*([0-9.]+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:  # Linux fallback, harmless on a Mac if 'ip' is absent
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], stderr=subprocess.DEVNULL
        ).decode()
        m = re.search(r"default via ([0-9.]+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def autodetect_host(api_port):
    """Find the device by probing /getInfo on candidate IPs. Returns host or None."""
    candidates = []
    gw = _default_gateway()
    if gw:
        candidates.append(gw)
    candidates.extend(h for h in CANDIDATE_HOSTS if h not in candidates)

    for host in candidates:
        if not _port_open(host, api_port, timeout=1.5):
            continue
        try:
            status, _ = _http_get(
                f"http://{host}:{api_port}/getInfo", timeout=3.0
            )
            if status == 200:
                return host
        except Exception:
            continue
    return None


def resolve_host(args):
    if args.host:
        return args.host
    print("Auto-detecting YO device (join the 'Yo2' WiFi first)...", file=sys.stderr)
    host = autodetect_host(args.api_port)
    if not host:
        sys.exit(
            "Could not find the YO device.\n"
            "  - Make sure your Mac is connected to the device's 'Yo2' WiFi.\n"
            "  - Or pass it explicitly:  --host <device-ip>\n"
            "    (find it via System Settings -> WiFi -> Details -> Router)"
        )
    print(f"Found device at {host}", file=sys.stderr)
    return host


# --------------------------------------------------------------------------- #
# API operations
# --------------------------------------------------------------------------- #
def api_url(host, port, path):
    return f"http://{host}:{port}/{path.lstrip('/')}"


def get_info(host, port):
    _, body = _http_get(api_url(host, port, "getInfo"))
    return body


def get_clips_raw(host, port):
    _, body = _http_get(api_url(host, port, "getClips"))
    return body


def parse_clip_names(raw):
    """Extract clip filenames/paths from a /getClips response of unknown shape.

    Handles: JSON list of strings, JSON list of dicts (name/file/path/url/clip),
    JSON dict wrapping such a list, or plain text. Falls back to scraping any
    *.mp4 tokens out of the raw body.
    """
    names = []

    def from_obj(obj):
        if isinstance(obj, str):
            if obj.strip():
                names.append(obj.strip())
        elif isinstance(obj, dict):
            for key in ("url", "path", "file", "filename", "name", "clip", "video"):
                if key in obj and isinstance(obj[key], str):
                    names.append(obj[key].strip())
                    return
            for v in obj.values():
                from_obj(v)
        elif isinstance(obj, list):
            for item in obj:
                from_obj(item)

    try:
        from_obj(json.loads(raw))
    except (ValueError, TypeError):
        pass

    if not names:
        names = MP4_RE.findall(raw)

    # de-dupe, preserve order
    seen, ordered = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
# Subdirectories under the webroot/filesystem where clips have been observed to
# land. service.sh recreates these at boot; exact name varies by firmware build.
CLIP_DIRS = ["", "CYC_DV/", "video_encode/", "tmp/", "video/", "record/"]


def _basename(name):
    return os.path.basename(name.rstrip("/")) or name


def download_clip_http(host, http_port, api_port, name, out_path):
    """Try to fetch one clip over HTTP. Returns True on success."""
    candidates = []
    if name.lower().startswith("http://") or name.lower().startswith("https://"):
        candidates.append(name)
    else:
        rel = name.lstrip("/")
        base = _basename(name)
        # If the name already includes a path, try it verbatim on both servers.
        for port in (http_port, api_port):
            candidates.append(f"http://{host}:{port}/{rel}")
        # Otherwise probe known clip dirs on the file server.
        for d in CLIP_DIRS:
            candidates.append(f"http://{host}:{http_port}/{d}{base}")

    for url in dict.fromkeys(candidates):  # de-dupe, keep order
        try:
            status, data = _http_get(url, binary=True, timeout=DEFAULT_TIMEOUT)
            if status == 200 and data and data[:1] != b"<":  # crude HTML guard
                with open(out_path, "wb") as fh:
                    fh.write(data)
                return True
        except Exception:
            continue
    return False


def ftp_walk_mp4(host, ftp_port, roots=("/mnt", "/")):
    """Walk the device's anonymous FTP tree and yield (remote_path, size)."""
    ftp = FTP()
    ftp.connect(host, ftp_port, timeout=DEFAULT_TIMEOUT)
    ftp.login()  # anonymous
    found = []
    visited = set()

    def walk(path, depth=0):
        if depth > 6 or path in visited:
            return
        visited.add(path)
        try:
            entries = ftp.mlsd(path)
            entries = list(entries)
            use_mlsd = True
        except (error_perm, Exception):
            use_mlsd = False
            entries = []
        if use_mlsd:
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                full = path.rstrip("/") + "/" + name
                if facts.get("type") == "dir":
                    walk(full, depth + 1)
                elif name.lower().endswith(".mp4"):
                    size = int(facts.get("size", "0") or 0)
                    found.append((full, size))
        else:
            # Fall back to NLST (no type info); recurse opportunistically.
            try:
                names = ftp.nlst(path)
            except error_perm:
                names = []
            for full in names:
                if full in (path, path + "/."):
                    continue
                if full.lower().endswith(".mp4"):
                    found.append((full, 0))
                elif "." not in os.path.basename(full):
                    walk(full, depth + 1)

    try:
        start = list(roots) if roots else ["/"]
        for r in start:
            walk(r)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return found


def ftp_download(host, ftp_port, remote_path, out_path):
    ftp = FTP()
    ftp.connect(host, ftp_port, timeout=DEFAULT_TIMEOUT)
    ftp.login()
    try:
        with open(out_path, "wb") as fh:
            ftp.retrbinary(f"RETR {remote_path}", fh.write)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return os.path.getsize(out_path) > 0


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_info(args):
    host = resolve_host(args)
    try:
        print(get_info(host, args.api_port))
    except Exception as e:
        sys.exit(f"getInfo failed: {e}")


def _list_clips(host, args):
    """Return clip names via the API, or [] if the API is unhelpful."""
    try:
        raw = get_clips_raw(host, args.api_port)
    except Exception as e:
        print(f"(/getClips request failed: {e})", file=sys.stderr)
        return []
    names = parse_clip_names(raw)
    if not names:
        print("(/getClips returned no recognizable clips)", file=sys.stderr)
        if args.verbose:
            print("--- raw /getClips ---", file=sys.stderr)
            print(raw, file=sys.stderr)
    return names


def cmd_list(args):
    host = resolve_host(args)
    names = _list_clips(host, args)
    if names:
        for n in names:
            print(n)
        return
    # Fall back to FTP discovery so `list` is still useful.
    print("Falling back to FTP discovery...", file=sys.stderr)
    try:
        found = ftp_walk_mp4(host, args.ftp_port)
    except Exception as e:
        sys.exit(f"FTP discovery failed: {e}")
    if not found:
        sys.exit("No .mp4 clips found via API or FTP.")
    for path, size in found:
        sz = f"  ({size/1_048_576:.1f} MB)" if size else ""
        print(f"{path}{sz}")


def _ensure_outdir(out):
    out = os.path.expanduser(out)
    os.makedirs(out, exist_ok=True)
    return out


def cmd_pull(args):
    host = resolve_host(args)
    outdir = _ensure_outdir(args.out)

    names = _list_clips(host, args)
    selected = names
    if not args.all and args.clips:
        wanted = set(args.clips)
        selected = [n for n in names if _basename(n) in wanted or n in wanted]
        # allow pulling a name the API didn't list, by trying it directly
        for w in args.clips:
            if w not in selected and _basename(w) not in {_basename(s) for s in selected}:
                selected.append(w)
    elif not args.all and not args.clips:
        sys.exit("Specify clip name(s), or use --all.")

    got, failed = [], []

    if selected:
        for name in selected:
            out_path = os.path.join(outdir, _basename(name))
            ok = download_clip_http(host, args.http_port, args.api_port, name, out_path)
            if ok:
                got.append(out_path)
                print(f"  ok   {name} -> {out_path}")
            else:
                failed.append(name)
                print(f"  miss {name} (will try FTP)")

    # Anything we couldn't get over HTTP (or if the API listed nothing) -> FTP.
    if failed or (args.all and not selected):
        print("Resolving remaining clips over FTP...", file=sys.stderr)
        try:
            found = ftp_walk_mp4(host, args.ftp_port)
        except Exception as e:
            found = []
            print(f"FTP walk failed: {e}", file=sys.stderr)
        by_base = {}
        for path, size in found:
            by_base.setdefault(_basename(path), (path, size))

        targets = []
        if args.all and not selected:
            targets = [p for p, _ in found]
        else:
            for name in failed:
                hit = by_base.get(_basename(name))
                if hit:
                    targets.append(hit[0])

        for remote in targets:
            out_path = os.path.join(outdir, _basename(remote))
            try:
                if ftp_download(host, args.ftp_port, remote, out_path):
                    got.append(out_path)
                    print(f"  ok   {remote} -> {out_path}  (ftp)")
            except Exception as e:
                print(f"  fail {remote}: {e}")

    print(f"\nDone. {len(got)} file(s) saved to {outdir}")
    if not got:
        sys.exit("Nothing downloaded. Try `ftp-grab`, or re-run with --verbose.")


def cmd_ftp_grab(args):
    """Brute, API-independent: walk FTP and pull every .mp4."""
    host = resolve_host(args)
    outdir = _ensure_outdir(args.out)
    try:
        found = ftp_walk_mp4(host, args.ftp_port)
    except Exception as e:
        sys.exit(f"FTP walk failed: {e}")
    if not found:
        sys.exit("No .mp4 files found on the device over FTP.")
    print(f"Found {len(found)} clip(s).")
    got = 0
    for remote, size in found:
        out_path = os.path.join(outdir, _basename(remote))
        try:
            if ftp_download(host, args.ftp_port, remote, out_path):
                got += 1
                print(f"  ok   {remote} -> {out_path}")
        except Exception as e:
            print(f"  fail {remote}: {e}")
    print(f"\nDone. {got} file(s) saved to {outdir}")


# Common RTSP url paths for Anyka / generic DVR firmware. The correct one varies
# by build, so we probe these in order and use the first that returns a stream.
# "" (no path, rtsp://host:554) is the one this firmware actually serves, so it's
# tried first; the rest stay as fallbacks for other builds.
RTSP_PATHS = ["", "live", "11", "12", "0", "1", "stream", "video", "ch0", "h264"]


def _start_stream(host, api_port):
    try:
        _http_get(api_url(host, api_port, "startStream"), timeout=5.0)
        print("Asked device to start streaming.", file=sys.stderr)
    except Exception as e:
        print(f"(/startStream call failed, trying RTSP anyway: {e})", file=sys.stderr)


def _stop_stream(host, api_port):
    try:
        _http_get(api_url(host, api_port, "stopStream"), timeout=5.0)
    except Exception:
        pass


def _probe_rtsp(url, timeout=6.0):
    """Return True if ffprobe sees a video stream at this RTSP url."""
    cmd = [
        "ffprobe", "-rtsp_transport", "tcp",
        "-v", "error",
        "-select_streams", "v",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        url,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=timeout, text=True
        )
        return "video" in (out.stdout or "")
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def _find_rtsp_url(host, rtsp_port, timeout=6.0):
    """Probe candidate paths and return the first working rtsp:// url, or None."""
    for rp in RTSP_PATHS:
        url = f"rtsp://{host}:{rtsp_port}/{rp}".rstrip("/")
        print(f"Probing {url} ...", file=sys.stderr)
        if _probe_rtsp(url, timeout=timeout):
            print(f"Live stream found at {url}", file=sys.stderr)
            return url
    return None


def cmd_view(args):
    """Watch the live RTSP feed AND record it to disk at the same time.

    A single ffmpeg process pulls one RTSP connection (the cheap camera only likes
    one client), tees it to a file with stream-copy, and pipes a copy to ffplay for
    real-time viewing. Quit with q in the window (or Ctrl-C); the recording is a
    fragmented mp4 so it stays valid even if you stop mid-stream.
    """
    host = resolve_host(args)
    for tool in ("ffmpeg", "ffplay", "ffprobe"):
        if not _have_ffmpeg(tool=tool):
            sys.exit(
                f"Need {tool} (ships with ffmpeg).  Install with:  brew install ffmpeg\n"
                "Or view manually in VLC -> Open Network -> "
                f"rtsp://{host}:{args.rtsp_port}"
            )

    _start_stream(host, args.api_port)
    url = _find_rtsp_url(host, args.rtsp_port)
    if not url:
        _stop_stream(host, args.api_port)
        sys.exit(
            "No RTSP stream answered on any common path.\n"
            "Try VLC against rtsp://%s:%d, or re-run with --verbose."
            % (host, args.rtsp_port)
        )

    if args.out:
        out_path = os.path.expanduser(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.expanduser(f"~/Desktop/yo_live_{stamp}.mp4")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    print(f"Live view + recording to:\n  {out_path}", file=sys.stderr)
    print("Press q in the video window (or Ctrl-C here) to stop.", file=sys.stderr)

    # ffmpeg: one input -> tee [file (fragmented mp4) | mpegts to stdout]
    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp", "-fflags", "nobuffer",
        "-i", url,
        "-map", "0", "-c", "copy",
        "-f", "tee",
        f"[movflags=+frag_keyframe+empty_moov+default_base_moof]{out_path}"
        f"|[f=mpegts]pipe:1",
    ]
    # ffplay: read the mpegts copy from stdin, low latency
    ffplay_cmd = [
        "ffplay", "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-flags", "low_delay", "-framedrop",
        "-window_title", "YO live (recording)",
        "-i", "pipe:0",
    ]

    ff = pf = None
    try:
        ff = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE)
        pf = subprocess.Popen(ffplay_cmd, stdin=ff.stdout)
        ff.stdout.close()  # let ffmpeg receive SIGPIPE when ffplay quits
        pf.wait()          # blocks until you close the player window
    except KeyboardInterrupt:
        pass
    finally:
        for p in (pf, ff):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        _stop_stream(host, args.api_port)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        size_mb = os.path.getsize(out_path) / 1_048_576
        print(f"\nSaved recording: {out_path}  ({size_mb:.1f} MB)")
    else:
        print("\n(no recording was written)", file=sys.stderr)


def cmd_stream(args):
    """Start the live RTSP stream and record it to a file with ffmpeg."""
    host = resolve_host(args)
    if not _have_ffmpeg() or not _have_ffmpeg(tool="ffprobe"):
        sys.exit("ffmpeg/ffprobe not found. Install it (e.g. `brew install ffmpeg`).")

    _start_stream(host, args.api_port)
    url = _find_rtsp_url(host, args.rtsp_port)
    if not url:
        _stop_stream(host, args.api_port)
        sys.exit("Could not find an RTSP stream on any common path.")

    out_path = os.path.expanduser(args.out)
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-t", str(args.duration),
        "-c", "copy",
        out_path,
    ]
    rc = subprocess.call(cmd)
    _stop_stream(host, args.api_port)

    if rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        print(f"\nSaved {out_path}")
    else:
        sys.exit("Recording failed.")


def _have_ffmpeg(tool="ffmpeg"):
    try:
        subprocess.check_output([tool, "-version"], stderr=subprocess.STDOUT)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_setup(args):
    """Print the full setup & usage guide."""
    print(SETUP_GUIDE)


def build_parser():
    p = argparse.ArgumentParser(
        description="Pull videos off a YO v2.0 WiFi sperm-test device. "
                    "Run `python3 yo_pull.py setup` for the full setup guide.",
        epilog="Quick start:\n"
               "  1. Join the device's 'Yo2' WiFi network.\n"
               "  2. python3 yo_pull.py info        # confirm connection\n"
               "  3. python3 yo_pull.py list        # see recordings\n"
               "  4. python3 yo_pull.py ftp-grab --out ~/Desktop/yo_videos\n"
               "  5. python3 yo_pull.py view        # watch live (needs ffmpeg)\n"
               "\nFull guide & how to find the device IP:  python3 yo_pull.py setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", help="device IP (default: auto-detect on the Yo2 network)")
    p.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    p.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    p.add_argument("--ftp-port", type=int, default=DEFAULT_FTP_PORT)
    p.add_argument("--rtsp-port", type=int, default=DEFAULT_RTSP_PORT)
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="print /getInfo").set_defaults(func=cmd_info)
    sub.add_parser("list", help="list clips on the device").set_defaults(func=cmd_list)
    pp = sub.add_parser("pull", help="download clips (API + FTP fallback)")
    pp.add_argument("clips", nargs="*", help="clip filename(s) to pull")
    pp.add_argument("--all", action="store_true", help="pull every clip")
    pp.add_argument("--out", default="./yo_videos", help="output directory")
    pp.set_defaults(func=cmd_pull)

    fg = sub.add_parser("ftp-grab", help="brute-pull every .mp4 over FTP")
    fg.add_argument("--out", default="./yo_videos", help="output directory")
    fg.set_defaults(func=cmd_ftp_grab)

    st = sub.add_parser("stream", help="record the live RTSP feed (needs ffmpeg)")
    st.add_argument("--duration", type=int, default=30, help="seconds to record")
    st.add_argument("--out", default="./yo_live.mp4", help="output file")
    st.set_defaults(func=cmd_stream)

    vw = sub.add_parser(
        "view", help="watch the live feed AND record it (needs ffmpeg+ffplay)")
    vw.add_argument(
        "--out", default=None,
        help="recording path (default: ~/Desktop/yo_live_<timestamp>.mp4)")
    vw.set_defaults(func=cmd_view)

    sub.add_parser("setup", help="print the full setup & usage guide").set_defaults(
        func=cmd_setup
    )

    return p


def main(argv=None):
    parser = build_parser()
    # With no arguments, show the setup guide rather than a bare usage error.
    if argv is None and len(sys.argv) == 1:
        print(SETUP_GUIDE)
        return
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")


if __name__ == "__main__":
    main()
