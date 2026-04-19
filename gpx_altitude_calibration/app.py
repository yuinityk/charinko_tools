"""GPX viewer + annotation web app."""

import asyncio
import io
import json
import math
import time
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from flask import Flask, jsonify, render_template, request, send_file
import aiohttp


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def ns_tag(ns_uri: str, local: str) -> str:
    return f"{{{ns_uri}}}{local}" if ns_uri else local


GSI_API = "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php"
DEFAULT_CONCURRENCY = 10
BENCH_TARGET_SECS = 1.0  # プリフライト計測の目標時間（秒）
BENCH_WARMUP = 10        # ウォームアップ点数（速度推定用）

ANNOTATIONS_DIR = Path(__file__).parent / "annotations"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# GSI fetch
# ---------------------------------------------------------------------------

async def _fetch_one(
    session: aiohttp.ClientSession, sem: asyncio.Semaphore, lat: float, lon: float
) -> float | None:
    async with sem:
        try:
            async with session.get(
                GSI_API,
                params={"lat": lat, "lon": lon, "outtype": "JSON"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                d = await resp.json(content_type=None)
                e = d.get("elevation")
                if e is not None and e != "-----":
                    return float(e)
        except Exception:
            pass
    return None


async def fetch_terrain(
    points: list[tuple[float, float]], concurrency: int = DEFAULT_CONCURRENCY
) -> list[float | None]:
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession() as session:
        return list(
            await asyncio.gather(*[_fetch_one(session, sem, lat, lon) for lat, lon in points])
        )


# ---------------------------------------------------------------------------
# GPX parsing
# ---------------------------------------------------------------------------

def parse_gpx(content: bytes) -> list[tuple[float, float, float | None]]:
    root = ET.fromstring(content)
    ns_uri = root.tag[1 : root.tag.index("}")] if root.tag.startswith("{") else ""

    points: list[tuple[float, float, float | None]] = []
    for trkseg in root.findall(f".//{ns_tag(ns_uri, 'trkseg')}"):
        for pt in trkseg.findall(ns_tag(ns_uri, "trkpt")):
            lat, lon = float(pt.get("lat")), float(pt.get("lon"))
            e = pt.find(ns_tag(ns_uri, "ele"))
            ele = float(e.text) if e is not None and e.text else None
            points.append((lat, lon, ele))
    return points


def parse_gpx_files(files) -> list[tuple[float, float, float | None]]:
    """複数 GPX ファイルのトラックポイントを順に連結して返す。"""
    all_pts: list[tuple[float, float, float | None]] = []
    for f in files:
        all_pts.extend(parse_gpx(f.read()))
    return all_pts


def downsample(pts: list, target: int) -> list:
    if len(pts) <= target:
        return pts
    step = (len(pts) - 1) / (target - 1)
    result = [pts[round(i * step)] for i in range(target - 1)]
    result.append(pts[-1])
    return result


# ---------------------------------------------------------------------------
# Routes — analysis
# ---------------------------------------------------------------------------

@app.route("/preflight", methods=["POST"])
def preflight():
    """GPX点数を数え、APIスループットを実測して処理時間を推定する。"""
    files = request.files.getlist("gpx")
    if not files:
        return jsonify({"error": "GPXファイルが必要です"}), 400

    try:
        raw_pts = parse_gpx_files(files)
    except Exception as e:
        return jsonify({"error": f"GPX解析エラー: {e}"}), 400

    if not raw_pts:
        return jsonify({"error": "トラックポイントが見つかりません"}), 400

    total = len(raw_pts)
    coords = [(lat, lon) for lat, lon, _ in raw_pts]

    def evenly_sampled(n):
        step = max(1, total // n)
        return coords[::step][:n]

    # ── フェーズ1: BENCH_WARMUP 点で速度を粗く推定 ──────────────────
    warmup_n = min(BENCH_WARMUP, total)
    t0 = time.perf_counter()
    asyncio.run(fetch_terrain(evenly_sampled(warmup_n)))
    warmup_secs = max(time.perf_counter() - t0, 0.01)
    rate_warmup = warmup_n / warmup_secs

    # ── フェーズ2: 目標時間に合わせた追加計測 ───────────────────────
    extra_n = max(0, min(
        int(rate_warmup * BENCH_TARGET_SECS) - warmup_n,
        total - warmup_n,
    ))
    extra_secs = 0.0
    if extra_n > 0:
        t1 = time.perf_counter()
        asyncio.run(fetch_terrain(evenly_sampled(warmup_n + extra_n)[warmup_n:]))
        extra_secs = time.perf_counter() - t1

    bench_n   = warmup_n + extra_n
    elapsed   = warmup_secs + extra_secs
    rate      = bench_n / elapsed
    est_secs  = total / rate

    return jsonify({
        "total_points":     total,
        "bench_points":     bench_n,
        "bench_secs":       round(elapsed, 2),
        "rate_pts_per_sec": round(rate, 1),
        "est_secs_total":   round(est_secs),
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("gpx")
    if not files:
        return jsonify({"error": "GPXファイルが必要です"}), 400

    try:
        max_pts_str = request.form.get("max_points", "")
        max_pts = int(max_pts_str) if max_pts_str else None
    except ValueError as e:
        return jsonify({"error": f"パラメータエラー: {e}"}), 400

    # ファイルごとに個別解析してセグメント境界を記録する
    try:
        file_names = [Path(f.filename).stem for f in files]
        raw_pts_per_file = [parse_gpx(f.read()) for f in files]
    except Exception as e:
        return jsonify({"error": f"GPX解析エラー: {e}"}), 400

    raw_pts = [pt for pts in raw_pts_per_file for pt in pts]
    if not raw_pts:
        return jsonify({"error": "トラックポイントが見つかりません"}), 400

    # ファイルごとの先頭・末尾インデックス（結合前）
    file_start_orig: list[int] = []
    file_end_orig: list[int] = []
    n = 0
    for pts in raw_pts_per_file:
        file_start_orig.append(n)
        n += len(pts)
        file_end_orig.append(n - 1)

    original_count = len(raw_pts)
    if max_pts and max_pts < original_count:
        raw_pts = downsample(raw_pts, max_pts)
    sampled_count = len(raw_pts)

    coords    = [(lat, lon) for lat, lon, _ in raw_pts]
    orig_eles = [e if e is not None else 0.0 for _, _, e in raw_pts]

    terrain_raw = asyncio.run(fetch_terrain(coords))
    terrain = [t if t is not None else o for t, o in zip(terrain_raw, orig_eles)]

    cum_dist = [0.0]
    for i in range(1, len(coords)):
        cum_dist.append(cum_dist[-1] + haversine_m(*coords[i - 1], *coords[i]))

    points = [
        {
            "lat": lat,
            "lon": lon,
            "distance": round(d / 1000, 3),
            "ele_terrain": round(ter, 1),
        }
        for (lat, lon), d, ter in zip(coords, cum_dist, terrain)
    ]

    # ファイルごとの距離セグメントを計算
    def orig_idx_to_dist_km(orig_idx: int) -> float:
        if original_count <= 1:
            return round(cum_dist[-1] / 1000, 3) if cum_dist else 0.0
        si = min(round(orig_idx * (sampled_count - 1) / (original_count - 1)), sampled_count - 1)
        return round(cum_dist[si] / 1000, 3)

    file_segments = []
    for name, start_orig, end_orig in zip(file_names, file_start_orig, file_end_orig):
        start_km = orig_idx_to_dist_km(start_orig)
        end_km = orig_idx_to_dist_km(end_orig)
        file_segments.append({"name": name, "start_km": start_km, "end_km": end_km})

    return jsonify({
        "points":         points,
        "original_count": original_count,
        "sampled_count":  sampled_count,
        "file_segments":  file_segments,
    })


# ---------------------------------------------------------------------------
# Routes — annotations
# ---------------------------------------------------------------------------

@app.route("/annotations/<name>", methods=["GET"])
def get_annotation(name):
    ANNOTATIONS_DIR.mkdir(exist_ok=True)
    path = ANNOTATIONS_DIR / f"{name}.json"
    if not path.exists():
        return jsonify({"tunnels": [], "exists": False})
    with open(path, encoding="utf-8") as f:
        return jsonify({**json.load(f), "exists": True})


@app.route("/annotations/<name>", methods=["POST"])
def save_annotation(name):
    ANNOTATIONS_DIR.mkdir(exist_ok=True)
    payload = request.get_json(force=True)
    payload["saved_at"] = datetime.now().isoformat(timespec="seconds")
    path = ANNOTATIONS_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — export
# ---------------------------------------------------------------------------

@app.route("/export", methods=["POST"])
def export_gpx():
    payload = request.get_json(force=True)
    name   = payload.get("gpx_name", "calibrated")
    points = payload.get("points", [])

    if not points:
        return jsonify({"error": "ポイントデータが空です"}), 400

    safe = "".join(c for c in name if c.isalnum() or c in "-_").strip() or "calibrated"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"'
        ' creator="GPX Altitude Calibrator">',
        "  <trk>",
        f"    <name>{safe}</name>",
        "    <trkseg>",
    ]
    for p in points:
        lines.append(
            f'      <trkpt lat="{p["lat"]}" lon="{p["lon"]}">'
            f'<ele>{p["ele"]:.1f}</ele></trkpt>'
        )
    lines += ["    </trkseg>", "  </trk>", "</gpx>"]

    buf = io.BytesIO("\n".join(lines).encode("utf-8"))
    return send_file(
        buf,
        mimetype="application/gpx+xml",
        as_attachment=True,
        download_name=f"{safe}_calibrated.gpx",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
