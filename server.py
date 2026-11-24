"""yaroPhotoSort — локальный сервер сортировки фото через LM Studio."""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import providers

try:
    from PIL import Image
except ImportError:
    print("Pillow не установлен. pip install pillow")
    raise

try:
    import pillow_heif  # noqa: F401
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:
    HEIF_OK = False


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
RESULTS_DIR = ROOT / "results"
CSV_PATH = RESULTS_DIR / "analysis.csv"
LOG_PATH = RESULTS_DIR / "run.log"
REVIEW_PATH = RESULTS_DIR / "review_queue.jsonl"
DUPES_PATH = RESULTS_DIR / "dupes.json"
JOURNAL_PATH = RESULTS_DIR / "journal.jsonl"
CURRENT_RUN = "manual"


# ────────────────────────────── журнал операций ──────────────────────────────

def journal_record(op: str, src: str, dest: str, run_id: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "op": op,
        "from": src,
        "to": dest,
        "run": run_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    with JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_journal() -> list[dict[str, Any]]:
    if not JOURNAL_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    with JOURNAL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def last_run_id() -> str:
    rows = load_journal()
    return rows[-1].get("run", "") if rows else ""


def last_run_ops() -> list[dict[str, Any]]:
    rid = last_run_id()
    if not rid:
        return []
    return [r for r in load_journal() if r.get("run") == rid]


def rewrite_journal(rows: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def do_move(src: Path, dest: Path, run_id: str | None = None) -> Path:
    """Перемещает файл, возвращает реальный путь назначения."""
    run_id = run_id or CURRENT_RUN
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.parent / f"{src.stem}_{int(time.time())}{src.suffix}"
    shutil.move(str(src), str(dest))
    journal_record("move", str(src), str(dest), run_id)
    return dest


def do_copy(src: Path, dest: Path, run_id: str | None = None) -> None:
    run_id = run_id or CURRENT_RUN
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.parent / f"{src.stem}_{int(time.time())}{src.suffix}"
    shutil.copy2(str(src), str(dest))
    journal_record("copy", str(src), str(dest), run_id)


def drop_dupes_groups_for_paths(paths: list[str]) -> int:
    """Убирает из dupes.json группы, где есть хоть один из этих файлов."""
    if not DUPES_PATH.exists() or not paths:
        return 0
    try:
        groups = json.loads(DUPES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    touched = set(paths)
    keep = []
    removed = 0
    for g in groups:
        gfiles = {str(f) for f in (g.get("files") or [])}
        if gfiles & touched:
            removed += 1
            continue
        keep.append(g)
    if removed:
        DUPES_PATH.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


def cleanup_empty_dirs(input_folder: str, paths: list[str]) -> list[str]:
    """Удаляет только пустые папки вдоль этих путей (не глубже input_folder)."""
    removed: list[str] = []
    base = Path(input_folder) if input_folder else None
    candidates: set[Path] = set()
    for raw in paths:
        d = Path(raw).parent
        while base and d != base and str(d).startswith(str(base)):
            candidates.add(d)
            d = d.parent
    for d in sorted(candidates, key=lambda x: len(str(x)), reverse=True):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                removed.append(str(d))
        except Exception:
            continue
    return removed


def undo_run(run_id: str) -> dict[str, Any]:
    """Разворачивает операции прогона в обратном порядке."""
    rows = load_journal()
    ops = [r for r in rows if r.get("run") == run_id]
    undone = 0
    errors: list[str] = []
    touched: list[str] = []
    for rec in reversed(ops):
        src = Path(str(rec.get("from") or ""))
        dest = Path(str(rec.get("to") or ""))
        op = rec.get("op")
        touched.append(str(src))
        touched.append(str(dest))
        try:
            if op == "move":
                if dest.is_file() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    if src.exists():
                        src = src.parent / f"{dest.stem}_undo_{int(time.time())}{dest.suffix}"
                    shutil.move(str(dest), str(src))
                undone += 1
            elif op == "copy":
                if dest.is_file():
                    dest.unlink()
                undone += 1
        except Exception as e:
            errors.append(f"{rec.get('from')}: {e}")
    rewrite_journal([r for r in rows if r.get("run") != run_id])
    cfg = load_config()
    removed = cleanup_empty_dirs(cfg.get("input_folder") or "", touched)
    # откат дубликатов: старые группы больше не отражают реальность
    if str(run_id).startswith("dupes"):
        try:
            DUPES_PATH.write_text("[]", encoding="utf-8")
        except Exception:
            pass
    else:
        drop_dupes_groups_for_paths(touched)
    return {
        "ok": not errors,
        "run": run_id,
        "undone": undone,
        "removed_dirs": removed,
        "errors": errors,
    }

LM_BASE = os.environ.get("LMSTUDIO_URL", "http://127.0.0.1:1234")


def resolve_port() -> int:
    env = os.environ.get("YAROPHOTO_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return int(cfg.get("port") or 8460)
    except Exception:
        pass
    return 8460


PORT = resolve_port()
ACTIVE_PORT_FILE = ROOT / "active_port.txt"


def find_free_port(preferred: int, span: int = 30) -> int:
    for port in range(preferred, min(preferred + span, 65535)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f"No free port in {preferred}..{preferred + span}")


def write_active_port(port: int) -> None:
    try:
        ACTIVE_PORT_FILE.write_text(str(port), encoding="utf-8")
    except Exception:
        pass


def clear_active_port() -> None:
    try:
        ACTIVE_PORT_FILE.unlink(missing_ok=True)
    except Exception:
        pass

app = FastAPI(title="yaroPhotoSort")
app.mount("/locales", StaticFiles(directory=str(ROOT / "locales")), name="locales")

# ────────────────────────────── конфиг ──────────────────────────────

CATEGORIES: list[dict[str, str]] = [
    {"key": "people", "name": "Люди / портреты"},
    {"key": "docs", "name": "Документы / сканы"},
    {"key": "screenshot", "name": "Скриншоты"},
    {"key": "food", "name": "Еда"},
    {"key": "nature", "name": "Пейзаж / природа"},
    {"key": "animals", "name": "Животные"},
    {"key": "transport", "name": "Транспорт"},
    {"key": "interior", "name": "Интерьер / вещи"},
    {"key": "other", "name": "Прочее"},
]

DEFAULT_CONFIG: dict[str, Any] = {
    "input_folder": "",
    "output_mode": "csv_only",          # csv_only | move_bad
    "review_mode": "auto",              # auto | ask  (mutually exclusive)
    "review_timing": "batch",           # batch | live (ask only; mutually exclusive)
    "bad_folder_name": "_rejects",
    "score_threshold": 2,               # score <= threshold → bad
    "quality_rules": {
        "blur": True,
        "exposure": True,
        "junk": True,
    },
    "formats": [".jpg", ".jpeg", ".png", ".webp"],
    "recursive": True,
    "resume": True,
    "dry_run": False,
    "categorization": False,
    "category_only": False,
    "events_enabled": False,
    "event_method": "time",             # time | similar | time_ai (mutually exclusive)
    "event_name": "date",               # date | ai (mutually exclusive)
    "event_base": "_event",
    "event_gap_hours": 3.0,
    "always_sort_screens_docs": False,
    "dupes_action": "mark",              # mark | group | reject_extra
    "dupes_keeper": "resolution",        # resolution | time
    "dupes_exact": True,
    "dupes_burst": True,
    "dupes_similar": False,
    "provider": "lmstudio",
    "model_name": "google/gemma-3-12b",
    "max_requests": 0,                   # 0 = без лимита
    "max_cost_usd": 0.0,                 # 0 = без лимита
    "categories": [
        {
            "key": c["key"],
            "name": c["name"],
            "enabled": True,
            "dest": "",
            "action": "copy",            # copy | move | skip
        }
        for c in CATEGORIES
    ],
    "add_reason_to_name": False,
    "preserve_structure": True,
    "max_image_side": 1280,
    "port": 8460,
    "ui_lang": "en",
    "ui_theme": "lab",
    "ui_tab": "sorting",
    "ui_accordion": {},
}


def load_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if k == "categories" and isinstance(v, list):
                    by_key = {c.get("key"): c for c in v if isinstance(c, dict)}
                    for c in cfg["categories"]:
                        if c["key"] in by_key:
                            c.update(by_key[c["key"]])
                elif k in cfg:
                    cfg[k] = v
        except Exception as e:
            log(f"config.json is broken, using defaults: {e}")
    # путь могли вставить в кавычках
    if isinstance(cfg.get("input_folder"), str):
        cfg["input_folder"] = cfg["input_folder"].strip().strip('"').strip("'").strip()
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


CONFIG = load_config()


# ────────────────────────────── лог ──────────────────────────────

LOG_LOCK = threading.Lock()
LOG_LINES: list[str] = []
MAX_LOG = 500


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    with LOG_LOCK:
        LOG_LINES.append(line)
        if len(LOG_LINES) > MAX_LOG:
            del LOG_LINES[: len(LOG_LINES) - MAX_LOG]
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ────────────────────────────── LM Studio ──────────────────────────────

_lm_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def lm_alive() -> bool:
    try:
        r = requests.get(f"{LM_BASE}/v1/models", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _get_json(url: str, timeout: float = 3.0) -> Any:
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        return None
    return r.json()


def lm_status() -> dict[str, Any]:
    """Собираем всё, что LM Studio готов отдать."""
    now = time.time()
    if _lm_cache["data"] is not None and now - _lm_cache["ts"] < 2.5:
        return _lm_cache["data"]

    info: dict[str, Any] = {
        "connected": False,
        "url": LM_BASE,
        "models": [],
        "active_model": None,
        "context_length": None,
        "quant": None,
        "params_b": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "gpu_name": None,
        "gpu_temp_c": None,
        "gpu_util_pct": None,
        "gpu_power_w": None,
        "ram_used_gb": None,
        "load_state": None,
        "raw": {},
        "speed_tps": _lm_cache.get("data", {}).get("speed_tps") if _lm_cache.get("data") else None,
        "sec_per_image": _lm_cache.get("data", {}).get("sec_per_image") if _lm_cache.get("data") else None,
    }

    try:
        models = _get_json(f"{LM_BASE}/v1/models")
        if models is None:
            raise RuntimeError("no /v1/models")
        info["connected"] = True
        data = models.get("data") or []
        info["models"] = [
            {
                "id": m.get("id"),
                "owned_by": m.get("owned_by"),
                "meta": m.get("meta") or m.get("info") or {},
            }
            for m in data
        ]
        if data:
            info["active_model"] = data[0].get("id")

        # LM Studio (новые версии) отдаёт /api/v0/models
        try:
            v0 = _get_json(f"{LM_BASE}/api/v0/models", timeout=2.5)
            if isinstance(v0, dict):
                info["raw"]["v0"] = v0
                loaded = v0.get("models") or v0.get("data") or []
                if isinstance(loaded, dict):
                    loaded = [loaded]
                if loaded and isinstance(loaded, list):
                    m0 = loaded[0]
                    if isinstance(m0, dict):
                        info["active_model"] = (
                            m0.get("id")
                            or m0.get("key")
                            or m0.get("displayName")
                            or info["active_model"]
                        )
                        for src in (m0, m0.get("architecture"), m0.get("modelInfo") or {}):
                            if not isinstance(src, dict):
                                continue
                            if info["context_length"] is None:
                                info["context_length"] = src.get(
                                    "contextLength"
                                ) or src.get("context_length") or src.get("max_context_length")
                            if info["quant"] is None:
                                info["quant"] = src.get("quantization") or src.get("quant")
                            if info["params_b"] is None:
                                info["params_b"] = (
                                    src.get("numParams")
                                    or src.get("params_b")
                                    or src.get("sizeLabel")
                                )
                        info["load_state"] = m0.get("state") or m0.get("load_state")
                        stats = m0.get("stats") or m0.get("memory") or {}
                        if isinstance(stats, dict):
                            info["vram_used_gb"] = (
                                stats.get("vramUsed")
                                or stats.get("gpuMemory")
                                or stats.get("vram_used")
                            )
                            info["vram_total_gb"] = stats.get("vramTotal") or stats.get(
                                "vram_total"
                            )
        except Exception:
            pass

        # контекст часто лежит в meta у /v1/models
        if info["context_length"] is None and info["models"]:
            meta = info["models"][0].get("meta") or {}
            info["context_length"] = meta.get("context_length") or meta.get("n_ctx")

    except Exception as e:
        info["error"] = str(e)

    # GPU: имя, температура, загрузка, потребление — через nvidia-smi
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            timeout=3,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip().splitlines()
        if out:
            parts = [x.strip() for x in out[0].split(",")]
            info["gpu_name"] = parts[0]
            if len(parts) > 1:
                try:
                    info["gpu_temp_c"] = int(float(parts[1]))
                except Exception:
                    pass
            if len(parts) > 2:
                try:
                    info["gpu_util_pct"] = int(float(parts[2]))
                except Exception:
                    pass
            if len(parts) > 3:
                try:
                    info["gpu_power_w"] = round(float(parts[3]), 1)
                except Exception:
                    pass
    except Exception:
        pass
    if info["vram_used_gb"] is None:
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                timeout=3,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
            ).strip()
            if out:
                used, total = out.splitlines()[0].split(",")[:2]
                info["vram_used_gb"] = round(float(used.strip()) / 1024, 2)
                info["vram_total_gb"] = round(float(total.strip()) / 1024, 2)
                info["raw"]["vram_source"] = "nvidia-smi"
        except Exception:
            pass

    info["ts"] = now
    _lm_cache["data"] = info
    _lm_cache["ts"] = now
    return info


# ────────────────────────────── файлы / форматы ──────────────────────────────

IMAGE_EXTS_ALL = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".heif",
}
# RAW — не жуём в первой версии
RAW_EXTS = {".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf"}


def iter_images(cfg: dict[str, Any]) -> list[Path]:
    root = Path(cfg["input_folder"])
    if not root.is_dir():
        raise FileNotFoundError(f"Папка не найдена: {root}")
    exts = {e.lower() for e in cfg.get("formats") or []}
    recursive = bool(cfg.get("recursive", True))
    result: list[Path] = []
    if recursive:
        walker = root.rglob("*")
    else:
        walker = root.glob("*")
    for p in walker:
        if not p.is_file():
            continue
        if p.suffix.lower() in exts:
            result.append(p)
        elif p.suffix.lower() in RAW_EXTS and p.suffix.lower() in exts:
            result.append(p)
    result.sort()
    return result


def count_files(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        files = iter_images(cfg)
        return {"ok": True, "count": len(files)}
    except Exception as e:
        return {"ok": False, "error": str(e), "count": 0}


# ────────────────────────────── события ──────────────────────────────

def photo_time(path: Path) -> datetime:
    try:
        img = Image.open(path)
        exif = img.getexif()
        raw = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
        if raw:
            raw = str(raw).strip().replace("/", ":")
            return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return datetime.now()


def looks_like_screenshot(path: Path, verdict_tags: list[str] | None = None) -> bool:
    tags = set(verdict_tags or [])
    if "screenshot" in tags or "docs" in tags:
        return True
    name = path.name.lower()
    return name.startswith("screenshot") or name.startswith("скрин")


def dhash(path: Path, hash_size: int = 8) -> int:
    img = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = list(img.getdata())
    bits = 0
    i = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = row * (hash_size + 1) + col
            if px[left] > px[left + 1]:
                bits |= 1 << i
            i += 1
    return bits


# ────────────────────────────── дубликаты ──────────────────────────────

DUPES_LOCK = threading.Lock()
DUPES_STATE: dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "groups": 0,
    "message": "",
}


def dupes_snapshot() -> dict[str, Any]:
    with DUPES_LOCK:
        return dict(DUPES_STATE)


def image_megapixels(path: Path) -> float:
    try:
        with Image.open(path) as img:
            w, h = img.size
            return w * h / 1_000_000
    except Exception:
        return 0.0


def file_md5(path: Path, chunk: int = 1024 * 1024) -> str:
    import hashlib

    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def find_exact_copies(files: list[Path]) -> list[list[Path]]:
    """Группы байт-в-байт одинаковых файлов (md5)."""
    by_hash: dict[str, list[Path]] = {}
    for p in files:
        try:
            key = file_md5(p)
        except Exception:
            continue
        by_hash.setdefault(key, []).append(p)
        with DUPES_LOCK:
            DUPES_STATE["done"] += 1
    return [g for g in by_hash.values() if len(g) > 1]


def find_duplicate_groups(files: list[Path], max_dist: int = 6) -> list[list[Path]]:
    """Группы почти одинаковых кадров по dHash + проверка размера кадра."""
    if max_dist <= 0:
        return []
    scored: list[tuple[Path, int]] = []
    for p in files:
        try:
            scored.append((p, dhash(p)))
        except Exception:
            continue
        with DUPES_LOCK:
            DUPES_STATE["done"] += 1

    scored.sort(key=lambda x: x[1])
    used = [False] * len(scored)
    groups: list[list[Path]] = []
    for i in range(len(scored)):
        if used[i]:
            continue
        group = [scored[i][0]]
        used[i] = True
        for j in range(i + 1, len(scored)):
            if used[j]:
                continue
            if bin(scored[i][1] ^ scored[j][1]).count("1") <= max_dist:
                group.append(scored[j][0])
                used[j] = True
        if len(group) > 1:
            groups.append(group)
    return groups


def confirm_same_scene(group: list[Path], cfg: dict[str, Any]) -> bool:
    """Две картинки → вопрос модели: одна сцена/объект (кот повернул голову)?"""
    if len(group) < 2:
        return False
    provider = cfg.get("provider") or "lmstudio"
    model = cfg.get("model_name") or providers.DEFAULT_MODELS.get(provider, "")
    try:
        data_a = prepare_image(group[0], 384)
        data_b = prepare_image(group[1], 384)
        prompt = (
            "Here are two photos. Answer ONLY yes or no. "
            "Do they show the same subject or scene (same cat, same place, same object), "
            "even if the pose or angle is slightly different?"
        )
        reply = providers.ask_model_multi(
            provider, model, prompt, [data_a, data_b], max_tokens=5
        )
        if reply.error:
            return True
        text = (reply.text or "").strip().lower()
        return text.startswith("y") or "да" in text[:5]
    except Exception:
        return True


def merge_groups(*lists: list[list[Path]]) -> list[list[Path]]:
    """Склейка групп с общими файлами."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for groups in lists:
        for g in groups:
            keys = [str(p) for p in g]
            for k in keys[1:]:
                union(keys[0], k)

    clusters: dict[str, list[Path]] = {}
    for k, p in parent.items():
        clusters.setdefault(find(k), []).append(Path(k))
    return [g for g in clusters.values() if len(g) > 1]


def pick_keeper(group: list[Path], mode: str) -> Path:
    if mode == "time":
        return max(group, key=lambda p: p.stat().st_mtime)
    return max(group, key=image_megapixels)


def run_dupes_scan(cfg: dict[str, Any]) -> None:
    global CURRENT_RUN
    CURRENT_RUN = f"dupes-{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    try:
        files = iter_images(cfg)
        with DUPES_LOCK:
            DUPES_STATE.update(
                running=True, done=0, total=len(files), groups=0,
                message="hashing",
            )
        groups: list[list[Path]] = []
        if cfg.get("dupes_exact"):
            exact = find_exact_copies(files)
            groups = merge_groups(groups, exact)
            with DUPES_LOCK:
                DUPES_STATE["message"] = f"exact copies: {len(exact)}"
            log(f"Duplicates exact: {len(exact)} groups")

        if cfg.get("dupes_burst"):
            burst = find_duplicate_groups(files, max_dist=2)
            groups = merge_groups(groups, burst)
            with DUPES_LOCK:
                DUPES_STATE["message"] = f"burst frames: {len(burst)}"
            log(f"Duplicates burst: {len(burst)} groups")

        if cfg.get("dupes_similar"):
            similar = find_duplicate_groups(files, max_dist=8)
            confirmed = []
            for g in similar:
                with DUPES_LOCK:
                    DUPES_STATE["message"] = "checking scene"
                if confirm_same_scene(g, cfg):
                    confirmed.append(g)
            groups = merge_groups(groups, confirmed)
            log(f"Duplicates similar: {len(confirmed)}/{len(similar)} groups after model check")
        action = cfg.get("dupes_action") or "mark"
        keeper_mode = cfg.get("dupes_keeper") or "resolution"
        input_folder = cfg.get("input_folder") or ""
        payload = []

        for gi, group in enumerate(groups, 1):
            keeper = pick_keeper(group, keeper_mode)
            entry = {
                "id": gi,
                "keeper": str(keeper),
                "keeper_name": keeper.name,
                "files": [str(p) for p in group],
                "names": [p.name for p in group],
            }
            payload.append(entry)

            if action == "group":
                dest_root = Path(input_folder) / "_dupes" / f"group_{gi:03d}"
                new_files = []
                for p in group:
                    if not p.is_file():
                        continue
                    dest = dest_root / p.name
                    real = do_move(p, dest)
                    new_files.append(str(real))
                    if str(p) == str(keeper):
                        entry["keeper"] = str(real)
                if new_files:
                    entry["files"] = new_files
                    entry["original_files"] = [str(x) for x in group]
                entry["action"] = f"group->{dest_root.name}"
            elif action == "reject_extra":
                rej = Path(input_folder) / (cfg.get("bad_folder_name") or "_rejects")
                rej.mkdir(parents=True, exist_ok=True)
                current = []
                for p in group:
                    if p == keeper or not p.is_file():
                        continue
                    dest = rej / p.name
                    real = do_move(p, dest)
                    current.append(str(real))
                if keeper.is_file():
                    current.insert(0, str(keeper))
                entry["files"] = current or [str(x) for x in group]
                entry["original_files"] = [str(x) for x in group]
                entry["action"] = "reject_extra"
            else:
                entry["action"] = "mark"

            with DUPES_LOCK:
                DUPES_STATE["groups"] = gi

        DUPES_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with DUPES_LOCK:
            DUPES_STATE.update(
                running=False,
                message=f"done: {len(groups)} groups",
            )
        log(f"Duplicates: {len(groups)} groups ({action})")
    except Exception as e:
        with DUPES_LOCK:
            DUPES_STATE.update(running=False, message=f"error: {e}")
        log(f"Duplicates error: {e}")


def assign_event_folders(files: list[Path], cfg: dict[str, Any]) -> dict[str, str]:
    """path -> имя папки события (без базы)."""
    method = cfg.get("event_method") or "time"
    if method == "similar":
        return _assign_by_similarity(files)
    return _assign_by_time(files, cfg)


def _assign_by_time(files: list[Path], cfg: dict[str, Any]) -> dict[str, str]:
    gap = float(cfg.get("event_gap_hours") or 3.0)
    timed = sorted(((photo_time(p), p) for p in files), key=lambda x: x[0])
    mapping: dict[str, str] = {}
    groups: list[list[tuple[datetime, Path]]] = []
    for t, p in timed:
        if not groups or (t - groups[-1][-1][0]).total_seconds() > gap * 3600:
            groups.append([])
        groups[-1].append((t, p))

    # имена event_DD.MM.YYYY, при повторе дня — суффикс
    day_counts: dict[str, int] = {}
    for group in groups:
        if not group:
            continue
        day = group[0][0].strftime("%d.%m.%Y")
        day_counts[day] = day_counts.get(day, 0) + 1
        name = f"event_{day}"
        if day_counts[day] > 1:
            name = f"event_{day}_{day_counts[day]}"
        for _, p in group:
            mapping[str(p)] = name
    return mapping


def _assign_by_similarity(files: list[Path], max_dist: int = 8) -> dict[str, str]:
    """Простые цепочки по dHash: похожие кадры в одну группу."""
    hashes: list[tuple[Path, int]] = []
    for p in files:
        try:
            hashes.append((p, dhash(p)))
        except Exception:
            hashes.append((p, 0))
    hashes.sort(key=lambda x: x[1])
    mapping: dict[str, str] = {}
    group_id = 0
    used = [False] * len(hashes)
    for i, (pi, hi) in enumerate(hashes):
        if used[i]:
            continue
        group_id += 1
        name = f"event_{group_id:03d}"
        mapping[str(pi)] = name
        used[i] = True
        for j in range(i + 1, len(hashes)):
            if used[j]:
                continue
            if bin(hi ^ hashes[j][1]).count("1") <= max_dist:
                mapping[str(hashes[j][0])] = name
                used[j] = True
    return mapping


def ask_event_name(sample: Path, lang: str, model: str) -> str:
    """Одно короткое имя события по одному фото группы."""
    prompt = (
        "Это фото из серии одного события. "
        "Придумай короткое имя папки для события (1-3 слова, без спецсимволов), "
        f"язык названия: {'русский' if lang == 'ru' else 'english'}. "
        "Только слово/фраза, без пояснений."
    )
    try:
        data_url = prepare_image(sample, 512)
        provider = "lmstudio"
        # имя события берём у текущего провайдера из конфига
        cfg = load_config()
        provider = cfg.get("provider") or "lmstudio"
        model = cfg.get("model_name") or providers.DEFAULT_MODELS.get(provider, "")
        reply = providers.ask_model(provider, model, prompt, data_url, max_tokens=20)
        if reply.error:
            return ""
        text = reply.text.strip()
        safe = re.sub(r'[<>:"/\\|?*]+', " ", text)
        safe = re.sub(r"\s+", " ", safe).strip()
        return safe[:40]
    except Exception:
        return ""


# ────────────────────────────── промпт / VLM ──────────────────────────────

QUALITY_LABELS = {
    "blur": "размытие",
    "exposure": "пересвет или недосвет",
    "junk": "мусорный/случайный кадр (руки, пол, потолок, случайный снимок)",
}


def build_prompt(cfg: dict[str, Any]) -> str:
    rules = cfg.get("quality_rules") or {}
    active = [QUALITY_LABELS[k] for k, on in rules.items() if on and k in QUALITY_LABELS]
    parts = [
        "Ты оцениваешь фотографию для ручной сортировки архива.",
    ]
    if active:
        parts.append(
            "Критерии плохого фото (quality=bad): " + "; ".join(active) + "."
        )
    else:
        parts.append("Оценку качества не используй — всегда quality=ok.")
    parts.append("Оценка score: 5 — отлично, 1 — полный брак.")

    if cfg.get("categorization"):
        keys = ", ".join(
            c["key"]
            for c in cfg.get("categories", [])
            if c.get("enabled") and c.get("action") != "skip"
        ) or "people, docs, screenshot, food, nature, animals, transport, interior, other"
        parts.append(
            "Содержимое снимка: выпиши от 1 до 3 тегов category_tags из списка: "
            f"{keys}. Самый важный — первым (например люди важнее пейзажа)."
        )
    else:
        keys = "people, docs, screenshot, food, nature, animals, transport, interior, other"

    if cfg.get("category_only") and cfg.get("categorization"):
        parts.append(
            "Оценивать качество НЕ нужно. Только определи содержимое снимка.\n"
            "Ответь СТРОГО одним JSON-объектом без markdown:\n"
            '{"category_tags":["'
            + (keys.split(", ")[0] if keys else "other")
            + '"],"category_confidence":0.0-1.0}'
        )
    else:
        parts.append(
            "Ответь СТРОГО одним JSON-объектом без markdown, без пояснений:\n"
            '{"quality":"ok|bad","quality_reason":"причина если bad иначе пусто",'
            '"score":1-5,'
            f'"category_tags":["{keys.split(", ")[0]}"],'
            '"category_confidence":0.0-1.0}'
        )
    return "\n".join(parts)


JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_verdict(text: str, score_threshold: int) -> dict[str, Any]:
    raw = (text or "").strip()
    valid = {c["key"] for c in CATEGORIES}

    def fail(reason: str) -> dict[str, Any]:
        return {
            "quality": "ok",
            "quality_reason": reason,
            "score": 3,
            "category": "other",
            "category_tags": ["other"],
            "category_confidence": 0.0,
            "raw": raw[:300],
        }

    m = JSON_RE.search(raw)
    if not m:
        return fail("no JSON in model reply")
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return fail("JSON parse error")
    # режим "только категории" — в ответе нет quality/score
    if "score" not in obj and "quality" not in obj:
        tags_raw = obj.get("category_tags")
        if not isinstance(tags_raw, list):
            tags_raw = [obj.get("category")] if obj.get("category") else []
        tags = []
        for tag in tags_raw:
            key = str(tag).lower().strip()
            if key in valid and key not in tags:
                tags.append(key)
        if not tags:
            tags = ["other"]
        try:
            conf = float(obj.get("category_confidence", 0.0))
        except Exception:
            conf = 0.0
        return {
            "quality": "ok",
            "quality_reason": "",
            "score": "",
            "category": tags[0],
            "category_tags": tags,
            "category_confidence": round(max(0.0, min(1.0, conf)), 3),
            "raw": raw[:300],
        }

    try:
        score = int(obj.get("score", 3))
    except Exception:
        score = 3
    score = max(1, min(5, score))
    q = str(obj.get("quality", "ok")).lower().strip()
    if q not in ("ok", "bad"):
        q = "bad" if score <= score_threshold else "ok"
    if score <= score_threshold:
        q = "bad"

    tags_raw = obj.get("category_tags")
    if not isinstance(tags_raw, list):
        single = obj.get("category")
        tags_raw = [single] if single else []
    tags: list[str] = []
    for tag in tags_raw:
        key = str(tag).lower().strip()
        if key in valid and key not in tags:
            tags.append(key)
    if not tags:
        tags = ["other"]

    try:
        conf = float(obj.get("category_confidence", 0.0))
    except Exception:
        conf = 0.0
    return {
        "quality": q,
        "quality_reason": str(obj.get("quality_reason", "") or ""),
        "score": score,
        "category": tags[0],
        "category_tags": tags,
        "category_confidence": round(max(0.0, min(1.0, conf)), 3),
        "raw": raw[:300],
    }


def prepare_image(path: Path, max_side: int) -> str:
    """Возвращает base64 data-url, готовый для vision-API."""
    img = Image.open(path)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def check_usage_limits(cfg: dict[str, Any]) -> str | None:
    """Возвращает сообщение о лимите или None. Для локальных провайдеров — нет."""
    provider = cfg.get("provider") or "lmstudio"
    if provider in providers.LOCAL_PROVIDERS:
        return None
    max_req = int(cfg.get("max_requests") or 0)
    max_cost = float(cfg.get("max_cost_usd") or 0.0)
    if max_req <= 0 and max_cost <= 0:
        return None
    stats = providers.load_stats()
    if max_req > 0 and int(stats.get("requests") or 0) >= max_req:
        return f"Request limit reached ({max_req})"
    if max_cost > 0 and float(stats.get("cost_usd") or 0.0) >= max_cost:
        return f"Cost limit reached (${max_cost})"
    return None


def ask_vlm(path: Path, cfg: dict[str, Any], model: str) -> dict[str, Any]:
    prompt = build_prompt(cfg)
    data_url = prepare_image(path, int(cfg.get("max_image_side") or 1280))
    provider = cfg.get("provider") or "lmstudio"
    reply = providers.ask_model(provider, model, prompt, data_url, max_tokens=220)
    if reply.error:
        raise RuntimeError(reply.error)
    verdict = parse_verdict(reply.text, int(cfg.get("score_threshold") or 2))
    verdict["sec"] = reply.sec
    verdict["tokens_out"] = reply.output_tokens
    return verdict


# ────────────────────────────── job ──────────────────────────────

@dataclass
class JobState:
    running: bool = False
    stop_requested: bool = False
    total: int = 0
    done: int = 0
    bad: int = 0
    ok: int = 0
    errors: int = 0
    current_file: str = ""
    started_at: str = ""
    finished_at: str = ""
    last_speed: Optional[float] = None  # сек/фото
    message: str = ""


JOB = JobState()
JOB_LOCK = threading.Lock()
JOB_THREAD: Optional[threading.Thread] = None


def job_snapshot() -> dict[str, Any]:
    with JOB_LOCK:
        pct = round(100 * JOB.done / JOB.total, 1) if JOB.total else 0.0
        eta = None
        if JOB.last_speed and JOB.total and JOB.done < JOB.total:
            eta_sec = JOB.last_speed * (JOB.total - JOB.done)
            eta = f"{int(eta_sec // 3600)}h {int((eta_sec % 3600) // 60)}m"
        return {
            "running": JOB.running,
            "stop_requested": JOB.stop_requested,
            "total": JOB.total,
            "done": JOB.done,
            "bad": JOB.bad,
            "ok": JOB.ok,
            "errors": JOB.errors,
            "percent": pct,
            "current_file": JOB.current_file,
            "started_at": JOB.started_at,
            "finished_at": JOB.finished_at,
            "eta": eta,
            "sec_per_image": JOB.last_speed,
            "message": JOB.message,
        }


CSV_HEADER = [
    "path",
    "filename",
    "quality",
    "quality_reason",
    "score",
    "category",
    "category_tags",
    "category_confidence",
    "action",
    "error",
    "sec",
    "ts",
]


def load_done_paths() -> set[str]:
    done: set[str] = set()
    if not CSV_PATH.exists():
        return done
    try:
        with CSV_PATH.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                p = row.get("path")
                if p:
                    done.add(p)
    except Exception:
        pass
    return done


def append_csv(row: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_HEADER})


def resolve_dest(dest: str | None, input_folder: str, category_key: str) -> Path:
    """Гибрид: пусто/относительно — внутри папки с фото; абсолютный — как есть."""
    raw = (dest or "").strip()
    if not raw:
        return Path(input_folder) / "_sorted" / category_key
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(input_folder) / raw


def needs_review(verdict: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """ask-режим: сомнительные и плохие не двигаем, кидаем в очередь."""
    if cfg.get("review_mode") != "ask":
        return False
    score = int(verdict.get("score") or 5)
    threshold = int(cfg.get("score_threshold") or 2)
    return verdict.get("quality") == "bad" or score <= threshold + 1


def enqueue_review(path: Path, verdict: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "path": str(path),
        "filename": path.name,
        "quality": verdict.get("quality"),
        "score": verdict.get("score"),
        "quality_reason": verdict.get("quality_reason"),
        "category_tags": verdict.get("category_tags") or [],
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    with REVIEW_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


# live-режим: задача ждёт решения по конкретному файлу
REVIEW_LOCK = threading.Lock()
REVIEW_EVENTS: dict[str, threading.Event] = {}
REVIEW_RESULTS: dict[str, str] = {}
REVIEW_PENDING: dict[str, dict[str, Any]] = {}


def wait_for_review_decision(path: str, item: dict[str, Any], timeout: float = 3600.0) -> str:
    ev = threading.Event()
    with REVIEW_LOCK:
        REVIEW_EVENTS[path] = ev
        REVIEW_PENDING[path] = item
    ev.wait(timeout)
    with REVIEW_LOCK:
        REVIEW_EVENTS.pop(path, None)
        REVIEW_PENDING.pop(path, None)
        return REVIEW_RESULTS.pop(path, "skip")


def signal_review_decision(path: str, decision: str) -> None:
    with REVIEW_LOCK:
        REVIEW_RESULTS[path] = decision
        ev = REVIEW_EVENTS.get(path)
    if ev:
        ev.set()


def apply_actions(
    path: Path,
    verdict: dict[str, Any],
    cfg: dict[str, Any],
    event_map: dict[str, str] | None = None,
) -> str:
    """Возвращает описание действия. Уважает dry_run."""
    actions: list[str] = []
    dry = bool(cfg.get("dry_run"))
    input_folder = cfg.get("input_folder") or ""

    if needs_review(verdict, cfg):
        # в ask-режиме файл остаётся на месте, пока человек не решил
        return "review"

    # события и категории взаимоисключающие: при событиях категории не работают
    use_events = bool(cfg.get("events_enabled"))
    use_categories = bool(cfg.get("categorization")) and not use_events

    if use_events and cfg.get("always_sort_screens_docs"):
        tags = verdict.get("category_tags") or []
        if "docs" in tags:
            tag = "docs"
        elif "screenshot" in tags or path.name.lower().startswith(("screenshot", "скрин")):
            tag = "screenshot"
        else:
            tag = ""
        if tag:
            dest_root = Path(input_folder) / "_sorted" / tag
            dest = dest_root / path.name
            actions.append(f"special->{tag}")
            if not dry and path.exists():
                do_move(path, dest)
            return ", ".join(actions)

    if use_events:
        ev = (event_map or {}).get(str(path))
        if ev:
            dest_root = Path(input_folder) / (cfg.get("event_base") or "_event") / ev
            dest = dest_root / path.name
            actions.append(f"event->{ev}")
            if not dry and path.exists():
                do_move(path, dest)
            return ", ".join(actions)

    if cfg.get("output_mode") == "move_bad" and verdict["quality"] == "bad":
        dest_root = Path(input_folder) / (cfg.get("bad_folder_name") or "_rejects")
        dest = dest_root / path.name
        if cfg.get("add_reason_to_name") and verdict.get("quality_reason"):
            safe = re.sub(r"[^\w\-]+", "_", verdict["quality_reason"])[:40]
            dest = dest_root / f"{path.stem}_bad_{safe}{path.suffix}"
        actions.append(f"move_bad->{dest.name}")
        if not dry:
            do_move(path, dest)

    if use_categories:
        tags = verdict.get("category_tags") or [verdict.get("category") or "other"]
        # порядок строк в categories = приоритет; файл уезжает только в первую совпавшую
        for c in cfg.get("categories") or []:
            if c.get("key") in tags and c.get("enabled") and c.get("action") not in (None, "skip"):
                key = str(c.get("key"))
                dest_root = resolve_dest(c.get("dest"), input_folder, key)
                dest = dest_root / path.name
                if cfg.get("preserve_structure"):
                    try:
                        rel = path.relative_to(input_folder)
                        dest = dest_root / rel
                    except Exception:
                        pass
                actions.append(f"{c.get('action')}->{key}")
                if not dry and path.exists():
                    if c.get("action") == "move":
                        do_move(path, dest)
                    else:
                        do_copy(path, dest)
                break

    return ", ".join(actions) if actions else "csv_only"


def job_worker(cfg: dict[str, Any]) -> None:
    global JOB, CURRENT_RUN
    CURRENT_RUN = f"run-{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    try:
        provider = cfg.get("provider") or "lmstudio"
        model = cfg.get("model_name") or providers.DEFAULT_MODELS.get(provider, "")
        if provider in providers.LOCAL_PROVIDERS:
            if provider == "lmstudio":
                st = lm_status()
                if not st.get("connected"):
                    with JOB_LOCK:
                        JOB.running = False
                        JOB.message = "LM Studio is not available or no model is loaded"
                    log("Start cancelled: LM Studio is not available.")
                    return
                if not model:
                    model = st.get("active_model") or model
            else:
                pst = providers.provider_status(provider, model)
                if not pst.get("ok"):
                    with JOB_LOCK:
                        JOB.running = False
                        JOB.message = f"{provider} is not available"
                    log(f"Start cancelled: {provider} is not available.")
                    return

        files = iter_images(cfg)
        skip = load_done_paths() if cfg.get("resume") else set()
        todo = [p for p in files if str(p) not in skip]

        with JOB_LOCK:
            JOB.running = True
            JOB.stop_requested = False
            JOB.total = len(todo)
            JOB.done = 0
            JOB.bad = 0
            JOB.ok = 0
            JOB.errors = 0
            JOB.started_at = datetime.now().isoformat(timespec="seconds")
            JOB.finished_at = ""
            JOB.message = f"Model: {model}"
            JOB.last_speed = None

        log(f"Started: {len(todo)} files (total {len(files)}, resume skipped {len(skip)}). Model: {model}")

        event_map: dict[str, str] = {}
        if cfg.get("events_enabled") and todo:
            event_map = assign_event_folders(todo, cfg)
            # ИИ-названия: один запрос на событие
            want_ai_name = cfg.get("event_name") == "ai" or cfg.get("event_method") == "time_ai"
            if want_ai_name:
                lang = str(cfg.get("ui_lang") or "en")
                samples: dict[str, Path] = {}
                for p in todo:
                    key = event_map.get(str(p))
                    if key and key not in samples:
                        samples[key] = p
                for key, sample in samples.items():
                    name = ask_event_name(sample, lang, model)
                    if name:
                        event_map = {k: (name if v == key else v) for k, v in event_map.items()}
            log(f"Events: {len(set(event_map.values()))} folders")

        speeds: list[float] = []
        for path in todo:
            with JOB_LOCK:
                if JOB.stop_requested:
                    JOB.message = "Stopped by user"
                    break
                JOB.current_file = str(path)

            limit_msg = check_usage_limits(cfg)
            if limit_msg:
                with JOB_LOCK:
                    JOB.message = limit_msg
                    JOB.running = False
                log(f"Stopped: {limit_msg}")
                break

            try:
                verdict = ask_vlm(path, cfg, model)
                if needs_review(verdict, cfg):
                    row = enqueue_review(path, verdict)
                    if cfg.get("review_timing") == "live":
                        with JOB_LOCK:
                            JOB.message = "Waiting for your decision"
                        decision = wait_for_review_decision(str(path), row)
                        if decision == "skip":
                            action = "review_skip"
                        else:
                            action = apply_review_decision(path, row, decision, load_config())
                            drop_from_review_queue(str(path))
                            log(f"live review {decision}: {path.name} ({action})")
                    else:
                        action = "review"
                else:
                    action = apply_actions(path, verdict, cfg, event_map)
                append_csv(
                    {
                        "path": str(path),
                        "filename": path.name,
                        "quality": verdict["quality"],
                        "quality_reason": verdict["quality_reason"],
                        "score": verdict["score"],
                        "category": verdict["category"],
                        "category_tags": "|".join(verdict.get("category_tags") or []),
                        "category_confidence": verdict["category_confidence"],
                        "action": action,
                        "error": "",
                        "sec": verdict.get("sec", ""),
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                speeds.append(verdict.get("sec") or 0)
                with JOB_LOCK:
                    JOB.done += 1
                    if verdict["quality"] == "bad":
                        JOB.bad += 1
                    else:
                        JOB.ok += 1
                    if speeds:
                        JOB.last_speed = round(sum(speeds[-20:]) / len(speeds[-20:]), 2)
                log(f"{verdict['quality']:3}  score={verdict['score']}  {path.name}  {verdict['quality_reason']}")
            except Exception as e:
                append_csv(
                    {
                        "path": str(path),
                        "filename": path.name,
                        "quality": "",
                        "quality_reason": "",
                        "score": "",
                        "category": "",
                        "category_tags": "",
                        "category_confidence": "",
                        "action": "",
                        "error": str(e),
                        "sec": "",
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                with JOB_LOCK:
                    JOB.done += 1
                    JOB.errors += 1
                log(f"ERR  {path.name}: {e}")

        with JOB_LOCK:
            JOB.running = False
            JOB.current_file = ""
            JOB.finished_at = datetime.now().isoformat(timespec="seconds")
            if not JOB.message or JOB.message.startswith("Model"):
                JOB.message = "Done"
        log("Done.")
    except Exception as e:
        log("Critical job error: " + str(e))
        log(traceback.format_exc())
        with JOB_LOCK:
            JOB.running = False
            JOB.message = f"Error: {e}"


# ────────────────────────────── диалог папки ──────────────────────────────

POWERSHELL_BROWSE = r"""
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Выбери папку с фото'
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }
"""


def browse_folder() -> str:
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-STA", "-Command", POWERSHELL_BROWSE],
            timeout=120,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip()
        return out
    except Exception as e:
        raise RuntimeError(f"Диалог не открылся: {e}")


# ────────────────────────────── API ──────────────────────────────

class ConfigModel(BaseModel):
    input_folder: str = ""
    output_mode: str = "csv_only"
    review_mode: str = "auto"
    review_timing: str = "batch"
    bad_folder_name: str = "_rejects"
    score_threshold: int = 2
    quality_rules: dict[str, bool] = {"blur": True, "exposure": True, "junk": True}
    formats: list[str] = [".jpg", ".jpeg", ".png", ".webp"]
    recursive: bool = True
    resume: bool = True
    dry_run: bool = False
    categorization: bool = False
    category_only: bool = False
    events_enabled: bool = False
    event_method: str = "time"
    event_name: str = "date"
    event_base: str = "_event"
    event_gap_hours: float = 3.0
    always_sort_screens_docs: bool = False
    dupes_action: str = "mark"
    dupes_keeper: str = "resolution"
    dupes_exact: bool = True
    dupes_burst: bool = True
    dupes_similar: bool = False
    provider: str = "lmstudio"
    model_name: str = "google/gemma-3-12b"
    max_requests: int = 0
    max_cost_usd: float = 0.0
    categories: list[dict[str, Any]] = []
    add_reason_to_name: bool = False
    preserve_structure: bool = True
    max_image_side: int = 1280
    port: int = 8460
    ui_lang: str = "en"
    ui_theme: str = "lab"
    ui_tab: str = "sorting"
    ui_accordion: dict[str, bool] = {}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/api/ping")
def ping() -> dict[str, Any]:
    return {"ok": True, "app": "yaroPhotoSort"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    global CONFIG
    CONFIG = load_config()
    return {
        "config": CONFIG,
        "categories_known": CATEGORIES,
        "heif_supported": HEIF_OK,
    }


@app.post("/api/config")
def set_config(body: ConfigModel) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    data = body.model_dump()
    for k, v in data.items():
        if k == "categories":
            by_key = {c.get("key"): c for c in v if isinstance(c, dict)}
            for c in cfg["categories"]:
                if c["key"] in by_key:
                    c.update(by_key[c["key"]])
        else:
            cfg[k] = v
    save_config(cfg)
    global CONFIG
    CONFIG = cfg
    return {"ok": True, "config": cfg}


@app.post("/api/config/reset")
def set_config_reset() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    save_config(cfg)
    global CONFIG
    CONFIG = cfg
    log("Config reset to defaults")
    return {"ok": True, "config": cfg}


@app.post("/api/browse-folder")
def api_browse() -> dict[str, Any]:
    try:
        path = browse_folder()
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"path": path}


@app.get("/api/scan")
def api_scan() -> dict[str, Any]:
    cfg = load_config()
    return count_files(cfg)


@app.get("/api/lmstudio")
def api_lmstudio() -> dict[str, Any]:
    return lm_status()


@app.get("/api/provider")
def api_provider() -> dict[str, Any]:
    cfg = load_config()
    provider = cfg.get("provider") or "lmstudio"
    model = cfg.get("model_name") or providers.DEFAULT_MODELS.get(provider, "")
    status = providers.provider_status(provider, model)
    if provider == "lmstudio":
        status.update(lm_status())
    return status


class ProviderKey(BaseModel):
    provider: str
    api_key: str = ""
    base_url: str = ""


@app.post("/api/provider/key")
def api_provider_key(body: ProviderKey) -> dict[str, Any]:
    saved = []
    if body.base_url.strip():
        saved.append(providers.save_base_url(body.provider, body.base_url.strip().rstrip("/")))
        log(f"Base URL saved to .env ({saved[-1]})")
    if body.provider in providers.LOCAL_PROVIDERS and not body.api_key.strip():
        return {"ok": True, "env": saved}
    if body.api_key.strip():
        if body.provider in providers.LOCAL_PROVIDERS:
            return {"ok": True, "env": saved}
        saved.append(providers.save_api_key(body.provider, body.api_key.strip()))
        log(f"API key saved to .env ({saved[-1]})")
    if not saved and not body.base_url.strip():
        raise HTTPException(400, "Empty key")
    return {"ok": True, "env": saved}


@app.post("/api/provider/test")
def api_provider_test() -> dict[str, Any]:
    cfg = load_config()
    provider = cfg.get("provider") or "lmstudio"
    model = cfg.get("model_name") or providers.DEFAULT_MODELS.get(provider, "")
    return providers.test_connection(provider, model)


@app.get("/api/usage")
def api_usage() -> dict[str, Any]:
    return {"ok": True, "usage": providers.load_stats()}


@app.post("/api/usage/reset")
def api_usage_reset() -> dict[str, Any]:
    stats = providers.reset_stats()
    log("Usage stats reset")
    return {"ok": True, "usage": stats}


@app.post("/api/start")
def api_start() -> dict[str, Any]:
    global JOB_THREAD
    with JOB_LOCK:
        if JOB.running:
            raise HTTPException(409, "A job is already running")
    cfg = load_config()
    if not cfg.get("input_folder"):
        raise HTTPException(400, "No folder selected")
    if not Path(cfg["input_folder"]).is_dir():
        raise HTTPException(400, f"Folder not found: {cfg['input_folder']}")
    if not lm_alive():
        raise HTTPException(400, "LM Studio is not available - start it and load a model")

    with JOB_LOCK:
        JOB.stop_requested = False
    JOB_THREAD = threading.Thread(target=job_worker, args=(cfg,), daemon=True)
    JOB_THREAD.start()
    return {"ok": True}


@app.post("/api/stop")
def api_stop() -> dict[str, Any]:
    with JOB_LOCK:
        if not JOB.running:
            return {"ok": True, "message": "Задача и так не запущена"}
        JOB.stop_requested = True
        JOB.message = "Stopping..."
    return {"ok": True}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    with LOG_LOCK:
        tail = LOG_LINES[-40:]
    with REVIEW_LOCK:
        pending = next(iter(REVIEW_PENDING.values()), None)
    return {
        "job": job_snapshot(),
        "log": tail,
        "csv_path": str(CSV_PATH),
        "review_pending": pending,
    }


def read_review_queue() -> list[dict[str, Any]]:
    if not REVIEW_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    with REVIEW_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def write_review_queue(rows: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with REVIEW_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


@app.get("/api/review")
def api_review_list() -> dict[str, Any]:
    rows = read_review_queue()
    for row in rows:
        row["exists"] = Path(row.get("path", "")).is_file()
    return {"ok": True, "count": len(rows), "items": rows}


class ReviewDecision(BaseModel):
    path: str
    decision: str  # keep | reject | skip


@app.post("/api/dupes/scan")
def api_dupes_scan() -> dict[str, Any]:
    with DUPES_LOCK:
        if DUPES_STATE.get("running"):
            raise HTTPException(409, "Duplicate scan is already running")
    cfg = load_config()
    if not cfg.get("input_folder"):
        raise HTTPException(400, "No folder selected")
    if not Path(cfg["input_folder"]).is_dir():
        raise HTTPException(400, f"Folder not found: {cfg['input_folder']}")
    threading.Thread(target=run_dupes_scan, args=(cfg,), daemon=True).start()
    return {"ok": True}


@app.get("/api/dupes/status")
def api_dupes_status() -> dict[str, Any]:
    return {"ok": True, "state": dupes_snapshot()}


@app.get("/api/dupes")
def api_dupes_list() -> dict[str, Any]:
    if not DUPES_PATH.exists():
        return {"ok": True, "count": 0, "groups": []}
    try:
        groups = json.loads(DUPES_PATH.read_text(encoding="utf-8"))
    except Exception:
        groups = []
    # живые группы: есть хотя бы два существующих файла
    live = []
    for g in groups:
        files = [f for f in (g.get("files") or []) if Path(str(f)).is_file()]
        if len(files) >= 2:
            g = dict(g)
            g["files"] = files
            live.append(g)
    return {"ok": True, "count": len(live), "groups": live}


@app.get("/api/dupes/image")
def api_dupes_image(path: str) -> FileResponse:
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


class DupeResolve(BaseModel):
    group_id: int
    keep_path: str
    others: str = "reject"  # reject | stay


class DupeSingle(BaseModel):
    path: str


@app.post("/api/dupes/delete-single")
def api_dupes_delete_single(body: DupeSingle) -> dict[str, Any]:
    global CURRENT_RUN
    CURRENT_RUN = f"dupes-{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    p = Path(body.path)
    if not p.is_file():
        raise HTTPException(404, "File not found")
    cfg = load_config()
    input_folder = cfg.get("input_folder") or ""
    rej = Path(input_folder) / (cfg.get("bad_folder_name") or "_rejects")
    do_move(p, rej / p.name)
    log(f"dupes single -> _rejects: {p.name}")
    return {"ok": True, "to": str(rej / p.name)}


def _find_original_for(moved_path: str) -> str | None:
    """Ищет в журнале исходный путь для файла, лежащего в _dupes."""
    for rec in reversed(load_journal()):
        if str(rec.get("to")) == moved_path and rec.get("op") == "move":
            return str(rec.get("from"))
    return None


@app.post("/api/dupes/resolve")
def api_dupes_resolve(body: DupeResolve) -> dict[str, Any]:
    global CURRENT_RUN
    CURRENT_RUN = f"dupes-{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    if not DUPES_PATH.exists():
        raise HTTPException(404, "No duplicates found")
    try:
        groups = json.loads(DUPES_PATH.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(500, "dupes.json is broken")
    group = next((g for g in groups if int(g.get("id") or 0) == body.group_id), None)
    if group is None:
        raise HTTPException(404, "Group not found")

    cfg = load_config()
    input_folder = cfg.get("input_folder") or ""
    rej = Path(input_folder) / (cfg.get("bad_folder_name") or "_rejects")

    keep = Path(body.keep_path)
    if not keep.is_file():
        raise HTTPException(404, "Keeper file not found")

    # keeper: вернуть из _dupes к источнику, если можем
    keeper_action = "keep"
    if "_dupes" in str(keep):
        original = _find_original_for(str(keep))
        if original and not Path(original).exists():
            do_move(keep, Path(original))
            keeper_action = f"restore->{original}"
        else:
            dest = Path(input_folder) / keep.name
            if not dest.exists():
                do_move(keep, dest)
                keeper_action = f"restore->{dest}"
            else:
                keeper_action = "keep"

    others_action = "stay"
    if body.others == "reject":
        rej.mkdir(parents=True, exist_ok=True)
        for raw in group.get("files") or []:
            p = Path(str(raw))
            if str(p) == str(keep) or not p.is_file():
                continue
            do_move(p, rej / p.name)
        others_action = "reject"

    group["resolved"] = {
        "keep": str(keep),
        "keeper_action": keeper_action,
        "others": others_action,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    DUPES_PATH.write_text(
        json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"dupes group {body.group_id} resolved: keep={keep.name} others={others_action}")
    return {"ok": True, "keeper_action": keeper_action, "others": others_action}


@app.get("/api/journal")
def api_journal(run: str = "") -> dict[str, Any]:
    if run:
        ops = [r for r in load_journal() if r.get("run") == run]
        return {"ok": True, "run": run, "count": len(ops), "ops": ops}
    rid = last_run_id()
    ops = last_run_ops()
    return {"ok": True, "run": rid, "count": len(ops), "ops": ops}


@app.get("/api/journal/runs")
def api_journal_runs() -> dict[str, Any]:
    rows = load_journal()
    groups: dict[str, dict[str, Any]] = {}
    file_to_runs: dict[str, set[str]] = {}
    for r in rows:
        rid = str(r.get("run") or "")
        g = groups.setdefault(
            rid,
            {"run": rid, "ts": r.get("ts"), "count": 0, "ops": 0, "files": set()},
        )
        g["ts"] = r.get("ts") or g["ts"]
        g["ops"] += 1
        for key in ("from", "to"):
            p = str(r.get(key) or "")
            if p:
                g["files"].add(p)
                file_to_runs.setdefault(p, set()).add(rid)

    # конфликт: файл задет более чем одним прогоном
    conflict_files = {p for p, runs in file_to_runs.items() if len(runs) > 1}

    out = []
    for rid, g in groups.items():
        touched = len(g["files"] & conflict_files)
        out.append(
            {
                "run": rid,
                "ts": g["ts"],
                "ops": g["ops"],
                "conflict_files": touched,
                "conflict": touched > 0,
            }
        )
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return {
        "ok": True,
        "count": len(out),
        "runs": out,
        "conflict_total": len(conflict_files),
    }


@app.get("/api/journal/image")
def api_journal_image(path: str) -> FileResponse:
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


class UndoOne(BaseModel):
    op: str
    from_path: str
    to_path: str


def undo_single(op: str, from_path: str, to_path: str) -> dict[str, Any]:
    src = Path(from_path)
    dest = Path(to_path)
    action = "skip"
    if op == "move":
        if dest.is_file() and not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                src = src.parent / f"{dest.stem}_undo_{int(time.time())}{dest.suffix}"
            shutil.move(str(dest), str(src))
            action = f"restored->{src}"
    elif op == "copy":
        if dest.is_file():
            dest.unlink()
            action = "copy-deleted"
    else:
        return {"ok": False, "action": "unknown-op"}

    cfg = load_config()
    cleanup_empty_dirs(cfg.get("input_folder") or "", [from_path, to_path])
    drop_dupes_groups_for_paths([from_path, to_path])
    return {"ok": True, "action": action}


def drop_journal_record(op: str, from_path: str, to_path: str) -> None:
    rows = load_journal()
    out: list[dict[str, Any]] = []
    dropped = False
    for r in rows:
        if (
            not dropped
            and r.get("op") == op
            and str(r.get("from")) == from_path
            and str(r.get("to")) == to_path
        ):
            dropped = True
            continue
        out.append(r)
    rewrite_journal(out)


@app.post("/api/journal/undo-one")
def api_journal_undo_one(body: UndoOne) -> dict[str, Any]:
    with JOB_LOCK:
        if JOB.running:
            raise HTTPException(409, "Stop the run first")
    result = undo_single(body.op, body.from_path, body.to_path)
    if result.get("ok"):
        drop_journal_record(body.op, body.from_path, body.to_path)
        log(f"undo one: {body.from_path} ({result.get('action')})")
    return result


class UndoRun(BaseModel):
    run: str = ""


@app.post("/api/journal/undo")
def api_journal_undo(body: UndoRun | None = None) -> dict[str, Any]:
    rid = body.run if body and body.run else last_run_id()
    if not rid:
        raise HTTPException(400, "Nothing to undo")
    with JOB_LOCK:
        if JOB.running:
            raise HTTPException(409, "Stop the run first")
    result = undo_run(rid)
    log(f"Undo run {rid}: {result.get('undone')} ops, {len(result.get('errors') or [])} errors")
    return result


@app.get("/api/review/image")
def api_review_image(path: str) -> FileResponse:
    rows = read_review_queue()
    allowed = {str(r.get("path")) for r in rows}
    if path not in allowed:
        raise HTTPException(403, "File is not in the review queue")
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


def apply_review_decision(path: Path, target: dict[str, Any], decision: str, cfg: dict[str, Any]) -> str:
    """keep | reject — файл двигается по решению человека."""
    input_folder = cfg.get("input_folder") or ""
    if decision == "reject":
        dest_root = Path(input_folder) / (cfg.get("bad_folder_name") or "_rejects")
        dest = dest_root / path.name
        if cfg.get("add_reason_to_name") and target.get("quality_reason"):
            safe = re.sub(r"[^\w\-]+", "_", str(target["quality_reason"]))[:40]
            dest = dest_root / f"{path.stem}_bad_{safe}{path.suffix}"
        if path.is_file():
            do_move(path, dest)
        return f"reject->{dest.name}"

    action = "keep"
    if cfg.get("categorization") and path.is_file():
        tags = target.get("category_tags") or []
        for c in cfg.get("categories") or []:
            if c.get("key") in tags and c.get("enabled") and c.get("action") not in (None, "skip"):
                key = str(c.get("key"))
                dest_root = resolve_dest(c.get("dest"), input_folder, key)
                dest = dest_root / path.name
                if cfg.get("preserve_structure"):
                    try:
                        dest = dest_root / path.relative_to(input_folder)
                    except Exception:
                        pass
                if c.get("action") == "move":
                    do_move(path, dest)
                else:
                    do_copy(path, dest)
                action = f"keep->{key}"
                break
    return action


def drop_from_review_queue(path: str) -> list[dict[str, Any]]:
    rows = [r for r in read_review_queue() if str(r.get("path")) != path]
    write_review_queue(rows)
    return rows


@app.post("/api/review/decide")
def api_review_decide(body: ReviewDecision) -> dict[str, Any]:
    global CURRENT_RUN
    CURRENT_RUN = f"review-{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    if body.decision not in ("keep", "reject", "skip"):
        raise HTTPException(400, "decision must be keep, reject or skip")
    rows = read_review_queue()
    target = next((r for r in rows if str(r.get("path")) == body.path), None)
    if target is None:
        # live-режим: файл мог быть ещё только в памяти ожидающего потока
        with REVIEW_LOCK:
            target = REVIEW_PENDING.get(body.path)
        if target is None:
            raise HTTPException(404, "Not in review queue")

    if body.decision == "skip":
        signal_review_decision(body.path, "skip")
        return {"ok": True, "decision": "skip", "count": len(rows)}

    cfg = load_config()
    path = Path(body.path)
    action = apply_review_decision(path, target, body.decision, cfg)
    rows = drop_from_review_queue(body.path)
    signal_review_decision(body.path, body.decision)
    log(f"review {body.decision}: {path.name} ({action})")
    return {"ok": True, "decision": body.decision, "action": action, "count": len(rows)}


@app.post("/api/shutdown")
def api_shutdown() -> dict[str, Any]:
    log("Shutdown requested from UI.")
    clear_active_port()
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True, "message": "Server is stopping"}


def run() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    port = find_free_port(PORT)
    if port != PORT:
        log(f"Port {PORT} is busy, using {port} instead")
    write_active_port(port)
    url = f"http://127.0.0.1:{port}"
    log(f"yaroPhotoSort server -> {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        clear_active_port()


if __name__ == "__main__":
    run()
