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
    "bad_folder_name": "на_удаление",
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

    # VRAM: пробуем через nvidia-smi, если LM Studio не отдал
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


def ask_vlm(path: Path, cfg: dict[str, Any], model: str) -> dict[str, Any]:
    prompt = build_prompt(cfg)
    data_url = prepare_image(path, int(cfg.get("max_image_side") or 1280))
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 220,
    }
    t0 = time.time()
    r = requests.post(
        f"{LM_BASE}/v1/chat/completions",
        json=payload,
        timeout=120,
    )
    dt = time.time() - t0
    if r.status_code != 200:
        raise RuntimeError(f"LM Studio {r.status_code}: {r.text[:200]}")
    data = r.json()
    content = (
        (data.get("choices") or [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    usage = data.get("usage") or {}
    verdict = parse_verdict(content, int(cfg.get("score_threshold") or 2))
    verdict["sec"] = round(dt, 2)
    verdict["tokens_out"] = usage.get("completion_tokens")
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


def apply_actions(path: Path, verdict: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Возвращает описание действия. Уважает dry_run."""
    actions: list[str] = []
    dry = bool(cfg.get("dry_run"))
    input_folder = cfg.get("input_folder") or ""

    if cfg.get("output_mode") == "move_bad" and verdict["quality"] == "bad":
        dest_root = Path(input_folder) / (cfg.get("bad_folder_name") or "на_удаление")
        dest = dest_root / path.name
        if cfg.get("add_reason_to_name") and verdict.get("quality_reason"):
            safe = re.sub(r"[^\w\-]+", "_", verdict["quality_reason"])[:40]
            dest = dest_root / f"{path.stem}_bad_{safe}{path.suffix}"
        actions.append(f"move_bad->{dest.name}")
        if not dry:
            dest_root.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest_root / f"{path.stem}_{int(time.time())}{path.suffix}"
            shutil.move(str(path), str(dest))

    if cfg.get("categorization"):
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
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest = dest.parent / f"{path.stem}_{int(time.time())}{path.suffix}"
                    if c.get("action") == "move":
                        shutil.move(str(path), str(dest))
                    else:
                        shutil.copy2(str(path), str(dest))
                break

    return ", ".join(actions) if actions else "csv_only"


def job_worker(cfg: dict[str, Any]) -> None:
    global JOB
    try:
        st = lm_status()
        model = st.get("active_model")
        if not st.get("connected") or not model:
            with JOB_LOCK:
                JOB.running = False
                JOB.message = "LM Studio is not available or no model is loaded"
            log("Start cancelled: LM Studio is not available.")
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

        speeds: list[float] = []
        for path in todo:
            with JOB_LOCK:
                if JOB.stop_requested:
                    JOB.message = "Stopped by user"
                    break
                JOB.current_file = str(path)

            try:
                verdict = ask_vlm(path, cfg, model)
                action = apply_actions(path, verdict, cfg)
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
    bad_folder_name: str = "на_удаление"
    score_threshold: int = 2
    quality_rules: dict[str, bool] = {"blur": True, "exposure": True, "junk": True}
    formats: list[str] = [".jpg", ".jpeg", ".png", ".webp"]
    recursive: bool = True
    resume: bool = True
    dry_run: bool = False
    categorization: bool = False
    categories: list[dict[str, Any]] = []
    add_reason_to_name: bool = False
    preserve_structure: bool = True
    max_image_side: int = 1280
    port: int = 8460


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
    return {"job": job_snapshot(), "log": tail, "csv_path": str(CSV_PATH)}


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
