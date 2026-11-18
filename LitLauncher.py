#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LitLauncher — лаунчер Minecraft: Java Edition с мастером настройки и максимальной оптимизацией.

Что умеет:
  * Мастер первой настройки (ник, память, Java, оптимизация)
  * Главное меню: выбор версии, кнопка «Играть», настройки, лог
  * Скачивает версию, библиотеки и ресурсы напрямую с серверов Mojang
  * Сам скачивает нужную Java (Adoptium), если её нет
  * Оптимизированные JVM-флаги (G1GC, Xms=Xmx, pre-touch и т.д.)
  * Опционально: Fabric + Sodium, Lithium, FerriteCore, ModernFix, EntityCulling, ImmediatelyFast
  * Офлайн-режим (вход по нику)

Запуск:  python LitLauncher.py     (нужен Python 3.8+ с tkinter)
"""
import hashlib
import http.client
import json
import os
import platform
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
import ctypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:  # tkinter может отсутствовать (только для тестов ядра)
    tk = None

APP_NAME = "LitLauncher"
APP_VERSION = "2.0"

# Репозиторий GitHub с релизами лаунчера, формат "владелец/репозиторий".
# Репозиторий задан, проверка обновлений включена.
UPDATE_REPO = "Litlauncher/LitLauncher"
SETUP_ASSET = "LitLauncher-Setup.exe"
UA = "%s/%s" % (APP_NAME, APP_VERSION)

SYSTEM = platform.system()
OS_NAME = {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}.get(SYSTEM, "linux")
MACHINE = platform.machine().lower()
IS_ARM = MACHINE in ("arm64", "aarch64")
IS_64 = sys.maxsize > 2 ** 32
CREATE_NO_WINDOW = 0x08000000
ABOVE_NORMAL_PRIORITY = 0x00008000

if SYSTEM == "Windows":
    BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / ".litlauncher"
elif SYSTEM == "Darwin":
    BASE = Path.home() / "Library" / "Application Support" / "litlauncher"
else:
    BASE = Path.home() / ".litlauncher"

MC_DIR = BASE / "minecraft"
RUNTIME_DIR = BASE / "runtime"
CONFIG_PATH = BASE / "config.json"
MANIFEST_CACHE = BASE / "version_manifest.json"
LOG_DIR = BASE / "logs"

MANIFEST_URLS = [
    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
    "https://launchermeta.mojang.com/mc/game/version_manifest.json",
]
FABRIC_META = "https://meta.fabricmc.net/v2"
MODRINTH = "https://api.modrinth.com/v2"
FABRIC_API = "fabric-api"
OPT_MODS = [FABRIC_API, "sodium", "lithium", "ferrite-core", "modernfix", "entityculling", "immediatelyfast",
            "krypton", "c2me-fabric", "threadtweak", "very-many-players", "noisium", "badoptimizations",
            "dynamic-fps", "moreculling", "memoryleakfix", "clumps"]
MOD_TITLES = {"fabric-api": "Fabric API", "sodium": "Sodium", "lithium": "Lithium", "ferrite-core": "FerriteCore",
              "modernfix": "ModernFix", "entityculling": "EntityCulling", "immediatelyfast": "ImmediatelyFast",
              "krypton": "Krypton (сеть)", "c2me-fabric": "C2ME (чанки)", "threadtweak": "ThreadTweak",
              "very-many-players": "Very Many Players", "noisium": "Noisium", "badoptimizations": "BadOptimizations",
              "dynamic-fps": "Dynamic FPS", "moreculling": "More Culling", "memoryleakfix": "Memory Leak Fix",
              "clumps": "Clumps"}

DEFAULT_CONFIG = {
    "setup_done": False,
    "nickname": "Player",
    "accounts": [],
    "theme": "light",
    "version": "",
    "show_snapshots": False,
    "ram_mb": 4096,
    "java_path": "",
    "optimize_jvm": True,
    "fabric_mods": True,
    "fullscreen": False,
    "width": 1280,
    "height": 720,
    "extra_jvm": "",
    "proxy": "",
    "mods_auto_install": True,   # первый запуск: моды ставятся автоматически, 100%
    "mods_user_removed": False, # пользователь удалил моды — сами не перекачиваем
}

FEATURES = {"has_custom_resolution": True}

# Кооперативная отмена запуска (используется download_many и prepare_launch)
CANCEL_EVENT = threading.Event()


class LaunchCancelled(Exception):
    """Пользователь отменил запуск."""
    pass


def check_cancel():
    if CANCEL_EVENT.is_set():
        raise LaunchCancelled()


# ----------------------------------------------------------------------------
# Конфиг и системная информация
# ----------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    BASE.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")


def total_ram_mb():
    try:
        if SYSTEM == "Windows":
            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MemStatus()
            m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys // 1048576)
        if SYSTEM == "Linux":
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) // 1024
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"])
        return int(out) // 1048576
    except Exception:
        return 8192


def recommended_ram(total, with_mods=True):
    hi = 6144 if with_mods else 4096
    r = min(hi, int(total * 0.5))
    r = max(2048, r // 256 * 256)
    return int(min(r, total * 0.75 // 256 * 256))


# ----------------------------------------------------------------------------
# Сеть
# ----------------------------------------------------------------------------
SKIN_CACHE = {}


def _mojang_uuid(name):
    """Возвращает UUID (без дефисов) по нику или None."""
    try:
        url = "https://api.mojang.com/users/profiles/minecraft/%s" % urllib.parse.quote(name)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("id")
    except Exception:
        return None


def fetch_skin_head(name, size=64):
    """Скачивает голову скина с crafatar по UUID и возвращает bytes PNG (или None)."""
    key = (name.lower(), size)
    if key in SKIN_CACHE:
        return SKIN_CACHE[key]
    try:
        uid = _mojang_uuid(name)
        if not uid:
            SKIN_CACHE[key] = None
            return None
        url = "https://crafatar.com/avatars/%s?size=%d&overlay" % (uid, size)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
        SKIN_CACHE[key] = data
        return data
    except Exception:
        SKIN_CACHE[key] = None
        return None


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=30):
    return json.loads(http_get(url, timeout).decode("utf-8"))


def apply_proxy(url):
    """Прокси для всех запросов лаунчера (пусто = системные настройки)."""
    url = (url or "").strip()
    try:
        if url:
            if "://" not in url:
                url = "http://" + url
            handler = urllib.request.ProxyHandler({"http": url, "https": url})
            urllib.request.install_opener(urllib.request.build_opener(handler))
        else:
            urllib.request.install_opener(urllib.request.build_opener())
    except Exception:
        pass


def describe_error(e):
    s = str(e)
    low = s.lower()
    if isinstance(e, urllib.error.HTTPError):
        return "сервер ответил ошибкой HTTP %s" % e.code
    if "certificate" in low or "ssl" in low:
        return "ошибка сертификата SSL (на macOS запусти Install Certificates.command из папки Python)"
    if "timed out" in low or "timeout" in low:
        return "сервер не отвечает (таймаут)"
    if any(k in low for k in ("getaddrinfo", "name or service", "name resolution", "nodename", "temporary failure")):
        return "нет интернета или DNS не отвечает"
    if any(k in low for k in ("refused", "10061", "tunnel", "forbidden", "403", "reset", "10054")):
        return "соединение отклонено (файрвол, антивирус, прокси или блокировка провайдером)"
    return s[:120] or "неизвестная ошибка"


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_tls = threading.local()
_REDIRECTS = (301, 302, 303, 307, 308)


def _use_proxy(scheme, host):
    try:
        return bool(urllib.request.getproxies().get(scheme)) and not urllib.request.proxy_bypass(host)
    except Exception:
        return False


def _fast_fetch(url, tmp, cb, redirects=5):
    """Быстрая загрузка: соединение с сервером переиспользуется (keep-alive) в каждом потоке.
    Для тысяч мелких файлов игры это в разы быстрее, чем новое соединение на каждый файл."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or _use_proxy(u.scheme, u.netloc):
        raise OSError("fast path off")
    pool = getattr(_tls, "pool", None)
    if pool is None:
        pool = _tls.pool = {}
    key = (u.scheme, u.netloc)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    for attempt in (0, 1):
        conn = pool.get(key)
        if conn is None:
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            conn = pool[key] = cls(u.netloc, timeout=30)
        try:
            conn.request("GET", path, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            r = conn.getresponse()
            if r.status in _REDIRECTS and redirects > 0 and r.getheader("Location"):
                loc = urllib.parse.urljoin(url, r.getheader("Location"))
                r.read()
                return _fast_fetch(loc, tmp, cb, redirects - 1)
            if r.status != 200:
                r.read()
                raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
            total = int(r.getheader("Content-Length") or 0)
            done = 0
            with open(str(tmp), "wb") as f:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if cb:
                        cb(done, total)
            if total and done != total:
                raise http.client.IncompleteRead(b"", total - done)
            return
        except urllib.error.HTTPError:
            raise
        except (http.client.HTTPException, OSError):
            try:
                conn.close()
            except Exception:
                pass
            pool.pop(key, None)
            if attempt:
                raise


def _slow_fetch(url, tmp, cb):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(str(tmp), "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            check_cancel()
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if cb:
                cb(done, total)


def download(url, dest, sha1=None, size=None, cb=None, retries=2):
    dest = Path(dest)
    if dest.exists():
        try:
            if size is not None and dest.stat().st_size == size:
                return
            if sha1 and sha1_file(dest) == sha1:
                return
            if size is None and sha1 is None:
                return
        except OSError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = None
    for i in range(retries):
        try:
            if i == 0:
                try:
                    _fast_fetch(url, tmp, cb)
                except urllib.error.HTTPError:
                    raise
                except Exception:
                    _slow_fetch(url, tmp, cb)   # прокси, нестандартный сервер и т.п.
            else:
                _slow_fetch(url, tmp, cb)
            if sha1 and sha1_file(tmp) != sha1:
                raise IOError("sha1 не совпадает")
            os.replace(str(tmp), str(dest))
            return
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404, 410):
                break
            time.sleep(0.5 * (i + 1))
        except Exception as e:  # noqa
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError("Не удалось скачать %s: %s" % (url, last))


DOWNLOAD_WORKERS = 96


def download_many(tasks, progress, label):
    uniq = {}
    for t in tasks:
        uniq[str(t[1])] = t
    tasks = list(uniq.values())
    # сначала крупные файлы: так общая загрузка заканчивается без «хвоста» из одного долгого файла
    tasks.sort(key=lambda x: -(x[3] or 0))
    total = len(tasks)
    if not total:
        return
    done = [0]
    lock = threading.Lock()

    def work(t):
        check_cancel()
        download(t[0], t[1], t[2], t[3])
        with lock:
            done[0] += 1
            n = done[0]
        if n % 40 == 0 or n == total:
            progress(n / float(total), "%s: %d/%d" % (label, n, total))

    with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, total)) as ex:
        futs = [ex.submit(work, t) for t in tasks]
        try:
            for f in as_completed(futs):
                check_cancel()
                f.result()
        except Exception:
            for f in futs:
                f.cancel()
            raise


def version_tuple(v):
    nums = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in nums[:4]) or (0,)


def update_enabled():
    return bool(UPDATE_REPO) and "YOUR_GITHUB" not in UPDATE_REPO and "/" in UPDATE_REPO


def check_update(current=None, data=None):
    """Возвращает {version, url, page, notes, size} если на GitHub вышел релиз новее, иначе None."""
    if data is None:
        if not update_enabled():
            return None
        try:
            data = http_json("https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO, 12)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # релизов ещё нет, это не ошибка
                return None
            raise
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name") or ""
    if version_tuple(tag) <= version_tuple(current or APP_VERSION):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name", "").lower() == SETUP_ASSET.lower()), None)
    return {"version": tag.lstrip("vV"), "url": asset["browser_download_url"] if asset else "",
            "size": asset.get("size", 0) if asset else 0,
            "page": data.get("html_url") or "https://github.com/%s/releases" % UPDATE_REPO,
            "notes": (data.get("body") or "").strip()}


def download_update(info, progress):
    d = Path(tempfile.mkdtemp(prefix="litlauncher-update-"))
    dest = d / SETUP_ASSET
    download(info["url"], dest, cb=lambda done, total: progress(done / float(total or info.get("size") or 1)))
    return dest


def run_installer(path):
    """Открывает установщик нового лаунчера (окно мастера Setup)."""
    if SYSTEM == "Windows":
        os.startfile(str(path))  # noqa
    else:
        subprocess.Popen([str(path)])


def load_cached_manifest():
    try:
        m = json.loads(MANIFEST_CACHE.read_text("utf-8"))
        if m.get("versions"):
            return m
    except Exception:
        pass
    return None


def _vkey(s):
    return [(0, int(x)) if x.isdigit() else (1, x) for x in re.split(r"(\d+)", s) if x]


def local_versions():
    """Установленные ванильные версии (работают без интернета)."""
    out = []
    d = MC_DIR / "versions"
    if d.exists():
        for p in d.iterdir():
            j = p / (p.name + ".json")
            if j.exists() and (p / (p.name + ".jar")).exists():
                try:
                    if "inheritsFrom" in json.loads(j.read_text("utf-8")):
                        continue
                except Exception:
                    continue
                out.append(p.name)
    return sorted(out, key=_vkey, reverse=True)


def fetch_manifest(retries=2):
    """Список версий: сначала локальный кэш (мгновенно), затем 3 адреса Mojang."""
    BASE.mkdir(parents=True, exist_ok=True)
    # 1) Кэш — самый быстрый путь; он свежий, если обновлялся при последнем успехе.
    cached = None
    try:
        if MANIFEST_CACHE.exists() and (time.time() - MANIFEST_CACHE.stat().st_mtime) < 3600:
            cached = json.loads(MANIFEST_CACHE.read_text("utf-8"))
    except Exception:
        cached = None
    # 2) Параллельно проверяем удалённый манифест; если сеть медленная — отдаём кэш
    last = "неизвестная ошибка"
    deadline = time.time() + (12 if cached else 30)
    for attempt in range(retries):
        for url in MANIFEST_URLS:
            if time.time() > deadline:
                break
            try:
                m = http_json(url, timeout=8)
                if not m.get("versions"):
                    raise ValueError("пустой ответ сервера")
                MANIFEST_CACHE.write_text(json.dumps(m), "utf-8")
                return m
            except Exception as e:  # noqa
                last = describe_error(e)
        if cached is not None:
            return cached
        if attempt + 1 < retries:
            time.sleep(0.8)
    if cached is not None:
        return cached
    raise RuntimeError(last)


# ----------------------------------------------------------------------------
# Правила, библиотеки, версии
# ----------------------------------------------------------------------------
def _arch_ok(arch):
    if not arch:
        return True
    if arch == "x86":
        return not IS_64
    if arch in ("arm64", "aarch64"):
        return IS_ARM
    if arch in ("x86_64", "amd64"):
        return IS_64 and not IS_ARM
    return True


def rule_ok(rules):
    if not rules:
        return True
    allowed = False
    for r in rules:
        match = True
        o = r.get("os")
        if o:
            if "name" in o and o["name"] != OS_NAME:
                match = False
            if not _arch_ok(o.get("arch")):
                match = False
        for k, v in (r.get("features") or {}).items():
            if bool(FEATURES.get(k, False)) != bool(v):
                match = False
        if match:
            allowed = (r.get("action") == "allow")
    return allowed


def maven_path(name):
    parts = name.split(":")
    ext = "jar"
    if "@" in parts[-1]:
        parts[-1], ext = parts[-1].split("@")
    g, a, v = parts[0], parts[1], parts[2]
    cl = parts[3] if len(parts) > 3 else None
    fn = "%s-%s%s.%s" % (a, v, ("-" + cl) if cl else "", ext)
    return "%s/%s/%s/%s" % (g.replace(".", "/"), a, v, fn)


def _expected_native_classifier():
    tok = {"windows": "windows", "linux": "linux", "osx": "macos"}[OS_NAME]
    if IS_ARM:
        return "natives-%s-arm64" % tok
    if OS_NAME == "windows" and not IS_64:
        return "natives-windows-x86"
    return "natives-" + tok


def resolve_libraries(libs, libdir):
    """-> (classpath_files, download_tasks, natives_to_extract)"""
    cp, tasks, extract, seen = [], [], [], set()
    for lib in libs:
        if not rule_ok(lib.get("rules")):
            continue
        name = lib["name"]
        parts = name.split(":")
        key = ":".join(parts[:2]) + ((":" + parts[3]) if len(parts) > 3 else "")
        dl = lib.get("downloads")
        entries = []  # (artifact, on_classpath, extract)
        if "natives" in lib:
            cl = lib["natives"].get(OS_NAME)
            if cl:
                cl = cl.replace("${arch}", "64" if IS_64 else "32")
                if dl and cl in (dl.get("classifiers") or {}):
                    entries.append((dl["classifiers"][cl], False, True))
        elif dl and "artifact" in dl:
            is_nat = len(parts) > 3 and parts[3].startswith("natives-")
            if is_nat and parts[3] != _expected_native_classifier():
                continue
            entries.append((dl["artifact"], True, is_nat))
        elif not dl:
            base = lib.get("url", "https://libraries.minecraft.net/")
            p = maven_path(name)
            entries.append(({"path": p, "url": base.rstrip("/") + "/" + p,
                             "sha1": lib.get("sha1"), "size": lib.get("size")}, True, False))
        for art, on_cp, ext in entries:
            if on_cp and not ext:
                if key in seen:
                    continue
                seen.add(key)
            path = libdir / art.get("path", maven_path(name))
            tasks.append((art["url"], path, art.get("sha1"), art.get("size")))
            if on_cp:
                cp.append(path)
            if ext:
                extract.append(path)
    return cp, tasks, extract


def extract_natives(jars, dest):
    dest.mkdir(parents=True, exist_ok=True)
    for j in jars:
        try:
            with zipfile.ZipFile(str(j)) as z:
                for m in z.namelist():
                    if m.endswith("/") or m.upper().startswith("META-INF"):
                        continue
                    target = dest / os.path.basename(m)
                    if not target.exists():
                        with z.open(m) as s, open(str(target), "wb") as t:
                            shutil.copyfileobj(s, t)
        except Exception:
            pass


def merge_versions(child, parent):
    r = dict(parent)
    for k, v in child.items():
        if k not in ("libraries", "arguments", "inheritsFrom"):
            r[k] = v
    r["libraries"] = list(child.get("libraries", [])) + list(parent.get("libraries", []))
    pa, ca = parent.get("arguments"), child.get("arguments")
    if pa or ca:
        pa, ca = pa or {}, ca or {}
        r["arguments"] = {"game": list(pa.get("game", [])) + list(ca.get("game", [])),
                          "jvm": list(pa.get("jvm", [])) + list(ca.get("jvm", []))}
    r["jar_id"] = parent["id"]
    r["id"] = child["id"]
    return r


def get_fabric_profile(mc, mcdir):
    try:
        q = urllib.parse.quote(mc)
        loaders = http_json("%s/versions/loader/%s" % (FABRIC_META, q), 15)
        if not loaders:
            raise RuntimeError("нет загрузчика для этой версии")
        pick = next((l for l in loaders if l["loader"].get("stable")), loaders[0])
        lv = pick["loader"]["version"]
        profile = http_json("%s/versions/loader/%s/%s/profile/json" % (FABRIC_META, q, urllib.parse.quote(lv)), 15)
        fid = profile["id"]
        p = mcdir / "versions" / fid / (fid + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(profile), "utf-8")
        return fid, profile
    except Exception:
        vdir = mcdir / "versions"
        cands = []
        if vdir.exists():
            cands = sorted(vdir.glob("fabric-loader-*-%s" % mc), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in cands:
            j = p / (p.name + ".json")
            if j.exists():
                data = json.loads(j.read_text("utf-8"))
                return data["id"], data
        raise


MODS_REG = "lit_mods.json"


def load_mod_registry(mcdir):
    """Реестр модов, которые поставил лаунчер: {"mc": версия, "files": {slug: имя_файла}}."""
    try:
        reg = json.loads((mcdir / MODS_REG).read_text("utf-8"))
    except Exception:
        return {"mc": None, "files": {}}
    if "files" not in reg:  # старый формат: {slug: имя_файла}
        reg = {"mc": None, "files": {k: v for k, v in reg.items() if isinstance(v, str)}}
    reg.setdefault("mc", None)
    return reg


def save_mod_registry(mcdir, reg):
    try:
        (mcdir / MODS_REG).write_text(json.dumps(reg, indent=1), "utf-8")
    except OSError:
        pass


def clean_managed_mods(mcdir, log=None, keep_mc=None):
    """Удаляет моды, поставленные лаунчером. Чужие файлы в папке mods не трогает.
    Если keep_mc совпадает с версией в реестре, ничего не делает."""
    reg = load_mod_registry(mcdir)
    if keep_mc and reg.get("mc") == keep_mc:
        return 0
    removed = 0
    mods = mcdir / "mods"
    for slug, fn in list(reg["files"].items()):
        try:
            p = mods / fn
            if p.exists():
                p.unlink()
                removed += 1
        except OSError:
            pass
    if log and removed:
        log("Удалены старые моды (%d шт.), они были для версии %s" % (removed, reg.get("mc") or "?"))
    save_mod_registry(mcdir, {"mc": None, "files": {}})
    return removed


def install_mods(mcdir, mc, log, only_missing=False):
    """Ставит Fabric API и моды оптимизации под версию mc (параллельно).
    Если до этого стояли моды под другую версию, они сначала удаляются.
    only_missing=True — докачиваем только те, которых нет в реестре или на диске."""
    mods = mcdir / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    if not only_missing:
        clean_managed_mods(mcdir, log, keep_mc=mc)
    reg = load_mod_registry(mcdir)
    reg["mc"] = mc
    files = reg["files"]

    targets = list(OPT_MODS)
    if only_missing:
        targets = [s for s in OPT_MODS if not (files.get(s) and (mods / files[s]).exists())]

    # Скачиваем моды рекурсивно, включая required-зависимости (например Steal Chest для Fast Break).
    deps_seen = set()
    lock = threading.Lock()

    def fetch_project(slug):
        q = urllib.parse.urlencode({"game_versions": json.dumps([mc]), "loaders": json.dumps(["fabric"])})
        vs = http_json("%s/project/%s/version?%s" % (MODRINTH, slug, q), 15)
        if not vs:
            return None, None
        v = next((x for x in vs if x.get("version_type") == "release"), vs[0])
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        return v, f

    def fetch_one(slug):
        try:
            v, f = fetch_project(slug)
            if not f:
                return ("none", None, slug)
            if not (mods / f["filename"]).exists():
                download(f["url"], mods / f["filename"], (f.get("hashes") or {}).get("sha1"))
            # зависимост  
            dep_slugs = []
            for d in (v.get("dependencies") or []):
                pid = d.get("project_id")
                if not pid or d.get("dependency_type") != "required":
                    continue
                if pid in deps_seen:
                    continue
                with lock:
                    if pid in deps_seen:
                        continue
                    deps_seen.add(pid)
                dep_slugs.append(pid)
            return ("ok", f["filename"], dep_slugs)
        except urllib.error.HTTPError as e:
            return ("err", "HTTP %s" % e.code, [])
        except (urllib.error.URLError, OSError) as e:
            return ("net", describe_error(e), [])
        except Exception as e:  # noqa
            return ("err", str(e), [])

    if not targets:
        log("Все моды уже на месте — скачивать нечего.")
        return 0
    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
        results = list(ex.map(fetch_one, targets))

    # Рекурсивно докачиваем зависимости (BFS)
    dep_queue = []
    for slug, (status, val, deps) in zip(targets, results):
        dep_queue.extend(deps or [])
    depth = 0
    while dep_queue and depth < 3:
        depth += 1
        with ThreadPoolExecutor(max_workers=min(len(dep_queue), 6)) as ex:
            dep_results = list(ex.map(fetch_one, dep_queue))
        next_q = []
        for (status, val, deps) in dep_results:
            if status == "ok" and val:
                log("Зависимость: %s" % val)
            next_q.extend(deps or [])
        dep_queue = next_q

    net_warned = False
    for slug, (status, val, _deps) in zip(targets, results):
        title = MOD_TITLES.get(slug, slug)
        old = files.get(slug)
        if status == "ok":
            if old and old != val:
                try:
                    (mods / old).unlink()
                except OSError:
                    pass
            files[slug] = val
            log("Мод: %s (%s)" % (title, val))
        elif status == "none":
            if old:
                try:
                    (mods / old).unlink()
                except OSError:
                    pass
                files.pop(slug, None)
            log("%s: для %s версии нет, пропускаю" % (title, mc))
        elif status == "net":
            if not net_warned:
                log("Нет связи с Modrinth (%s), часть модов пропущена" % val)
                net_warned = True
        else:
            log("%s не установлен: %s" % (title, val))
    save_mod_registry(mcdir, reg)
    return len(files)


# ----------------------------------------------------------------------------
# Java
# ----------------------------------------------------------------------------
_java_cache = {}


def java_major(path):
    path = str(path)
    if path in _java_cache:
        return _java_cache[path]
    major = None
    try:
        kw = {"creationflags": CREATE_NO_WINDOW} if SYSTEM == "Windows" else {}
        r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL, **kw)
        m = re.search(r'version "(\d+)(?:\.(\d+))?', (r.stderr or "") + (r.stdout or ""))
        if m:
            major = int(m.group(1))
            if major == 1 and m.group(2):
                major = int(m.group(2))
    except Exception:
        pass
    _java_cache[path] = major
    return major


def find_java_bin(root):
    exe = "java.exe" if SYSTEM == "Windows" else "java"
    for dp, _dn, fn in os.walk(str(root)):
        if exe in fn and os.path.basename(dp) == "bin":
            return Path(dp) / exe
    return None


def java_candidates():
    c = []
    if RUNTIME_DIR.exists():
        for d in sorted(RUNTIME_DIR.glob("java*")):
            b = find_java_bin(d)
            if b:
                c.append(b)
    jh = os.environ.get("JAVA_HOME")
    exe = "java.exe" if SYSTEM == "Windows" else "java"
    if jh and (Path(jh) / "bin" / exe).exists():
        c.append(Path(jh) / "bin" / exe)
    w = shutil.which("java")
    if w:
        c.append(Path(w))
    globs = []
    if SYSTEM == "Windows":
        for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if pf:
                globs += [Path(pf).glob("Java/*/bin/java.exe"), Path(pf).glob("Eclipse Adoptium/*/bin/java.exe"),
                          Path(pf).glob("Microsoft/*/bin/java.exe"), Path(pf).glob("Zulu/*/bin/java.exe")]
    elif SYSTEM == "Darwin":
        globs += [Path("/Library/Java/JavaVirtualMachines").glob("*/Contents/Home/bin/java")]
    else:
        globs += [Path("/usr/lib/jvm").glob("*/bin/java")]
    for g in globs:
        c += list(g)
    return c


def lts_for(req):
    for lts in (8, 11, 17, 21, 25):
        if req <= lts:
            return lts
    return 25


def install_java(major, log, progress):
    os_ = {"windows": "windows", "linux": "linux", "osx": "mac"}[OS_NAME]
    arch = "aarch64" if IS_ARM else ("x64" if IS_64 else "x32")
    url = ("https://api.adoptium.net/v3/binary/latest/%d/ga/%s/%s/jre/hotspot/normal/eclipse"
           % (major, os_, arch))
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    archive = RUNTIME_DIR / ("tmp_java.zip" if SYSTEM == "Windows" else "tmp_java.tar.gz")
    if archive.exists():
        archive.unlink()
    log("Скачиваю Java %d (Adoptium)..." % major)
    download(url, archive, cb=lambda d, t: progress((d / float(t)) if t else 0.0,
                                                    "Java %d: %.0f МБ" % (major, d / 1048576.0)))
    dest = RUNTIME_DIR / ("java%d" % major)
    if dest.exists():
        shutil.rmtree(str(dest), ignore_errors=True)
    dest.mkdir(parents=True)
    if SYSTEM == "Windows":
        with zipfile.ZipFile(str(archive)) as z:
            z.extractall(str(dest))
    else:
        with tarfile.open(str(archive)) as t:
            try:
                t.extractall(str(dest), filter="data")
            except TypeError:
                t.extractall(str(dest))
    archive.unlink()
    b = find_java_bin(dest)
    if not b:
        raise RuntimeError("Не удалось найти java после установки")
    if SYSTEM != "Windows":
        os.chmod(str(b), 0o755)
    return b


def ensure_java(cfg, req, log, progress):
    ok = (lambda m: m == req) if req <= 8 else (lambda m: m >= req)
    p = (cfg.get("java_path") or "").strip()
    if p and Path(p).exists():
        m = java_major(p)
        if m and ok(m):
            return str(p)
        log("Java из настроек (версия %s) не подходит, нужна %d — ищу другую" % (m, req))
    # 1) Быстрый путь: кэш предыдущего поиска
    cached = getattr(ensure_java, "_cache", None)
    if cached is None:
        cached = ensure_java._cache = {}
    key = (req, IS_ARM, IS_64)
    if key in cached and Path(cached[key]).exists():
        return cached[key]
    # 2) Сканируем систему параллельно (быстрее, чем по одному)
    cands = list(java_candidates())
    found = []
    if cands:
        from concurrent.futures import ThreadPoolExecutor as _TP
        with _TP(max_workers=min(8, len(cands))) as ex:
            versions = list(ex.map(java_major, cands))
        for c, m in zip(cands, versions):
            if m and ok(m):
                found.append((m, c))
    if found:
        found.sort(key=lambda x: x[0])
        best = str(found[0][1])
        cached[key] = best
        return best
    path = str(install_java(lts_for(req), log, progress))
    cached[key] = path
    return path


# ----------------------------------------------------------------------------
# Оптимизация и сборка команды запуска
# ----------------------------------------------------------------------------
def jvm_opt_flags(optimize, ram, java_major_ver=None):
    if not optimize:
        return ["-Xms512M", "-Xmx%dM" % ram]
    # Общие флаги (не зависят от GC)
    common = [
        "-Xms%dM" % ram, "-Xmx%dM" % ram,
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+DisableExplicitGC",
        "-XX:+AlwaysPreTouch",
        "-XX:+ParallelRefProcEnabled",
        "-XX:+UseStringDeduplication",
        "-XX:+OptimizeStringConcat",
        "-XX:+UseCompressedOops",
        "-XX:+PerfDisableSharedMem",
        "-Dsun.java2d.opengl=true",
        "-Dsun.java2d.d3d=false",
        "-Dsun.java2d.noddraw=true",
        "-Dfml.ignoreInvalidMinecraftCertificates=true",
        "-Dfml.ignorePatchDiscrepancies=true",
        "-Dfile.encoding=UTF-8",
    ]
    # --- G1GC: клиентские настройки из официальной документации Minecraft ---
    # ZGC на клиенте заметно режет FPS, поэтому используем проверенный G1.
    return common + [
        "-XX:+UseG1GC",
        "-XX:MaxGCPauseMillis=50",
        "-XX:G1NewSizePercent=30",
        "-XX:G1MaxNewSizePercent=40",
        "-XX:G1HeapRegionSize=8M",
        "-XX:G1ReservePercent=20",
        "-XX:G1HeapWastePercent=5",
        "-XX:G1MixedGCCountTarget=4",
        "-XX:InitiatingHeapOccupancyPercent=15",
        "-XX:G1MixedGCLiveThresholdPercent=90",
        "-XX:G1RSetUpdatingPauseTimePercent=5",
        "-XX:SurvivorRatio=32",
        "-XX:MaxTenuringThreshold=1",
    ]


def offline_uuid(name):
    h = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return uuid.UUID(bytes=h, version=3).hex


def sub_vars(s, v):
    return re.sub(r"\$\{([^}]+)\}", lambda m: str(v.get(m.group(1), "")), s)


def parse_args(lst, v):
    out = []
    for a in lst:
        if isinstance(a, str):
            out.append(sub_vars(a, v))
        else:
            if not rule_ok(a.get("rules")):
                continue
            val = a["value"]
            for x in ([val] if isinstance(val, str) else val):
                out.append(sub_vars(x, v))
    return out


def build_command(cfg, data, java, mcdir, classpath, natives, assets_root, game_assets):
    name = (cfg.get("nickname") or "Player").strip() or "Player"
    v = {
        "auth_player_name": name, "version_name": data["id"], "game_directory": str(mcdir),
        "assets_root": str(assets_root), "assets_index_name": data["assetIndex"]["id"],
        "auth_uuid": offline_uuid(name), "auth_access_token": "0", "auth_session": "0",
        "auth_xuid": "0", "clientid": "0", "user_type": "legacy", "user_properties": "{}",
        "version_type": data.get("type", "release"), "natives_directory": str(natives),
        "launcher_name": APP_NAME, "launcher_version": APP_VERSION, "classpath": classpath,
        "library_directory": str(mcdir / "libraries"), "classpath_separator": os.pathsep,
        "game_assets": str(game_assets), "resolution_width": str(cfg["width"]),
        "resolution_height": str(cfg["height"]),
    }
    args = data.get("arguments") or {}
    jvm = parse_args(args.get("jvm", []), v)
    game = []
    if data.get("minecraftArguments"):
        game += [sub_vars(x, v) for x in data["minecraftArguments"].split()]
    game += parse_args(args.get("game", []), v)
    if not any(a.startswith("-Djava.library.path") for a in jvm):
        jvm.append("-Djava.library.path=" + str(natives))
    if "-cp" not in jvm and "-classpath" not in jvm:
        jvm += ["-cp", classpath]
    if "--width" not in game:
        game += ["--width", str(cfg["width"]), "--height", str(cfg["height"])]
    if cfg.get("fullscreen") and "--fullscreen" not in game:
        game.append("--fullscreen")
    ram = int(cfg["ram_mb"])
    extra = shlex.split(cfg.get("extra_jvm") or "")
    log4 = ["-Dlog4j2.formatMsgNoLookups=true"]
    try:
        jver = java_major(java)
    except Exception:
        jver = None
    return ([java] + jvm_opt_flags(cfg.get("optimize_jvm", True), ram, jver) + log4 + extra
            + jvm + [data["mainClass"]] + game)


def prepare_launch(cfg, log, progress):
    mcdir = MC_DIR
    mcdir.mkdir(parents=True, exist_ok=True)
    progress(0.0, "Получаю список версий...")
    try:
        manifest = fetch_manifest()
    except Exception as e:  # noqa
        manifest = load_cached_manifest()
        log("Нет связи с Mojang (%s), работаю офлайн" % e)
    vid = cfg.get("version") or (manifest["latest"]["release"] if manifest else "")
    if not vid:
        raise RuntimeError("Версия не выбрана, а список версий недоступен. Проверь интернет и нажми «Обновить».")
    vpath = mcdir / "versions" / vid / (vid + ".json")
    entry = next((x for x in manifest["versions"] if x["id"] == vid), None) if manifest else None
    if entry:
        try:
            download(entry["url"], vpath, sha1=entry.get("sha1"))
        except Exception:
            if not vpath.exists():
                raise
    if not vpath.exists():
        raise RuntimeError("Версия %s не установлена, а скачать её сейчас не получилось. Проверь интернет или VPN." % vid)
    vanilla = json.loads(vpath.read_text("utf-8"))
    vanilla["jar_id"] = vid
    data = vanilla
    used_fabric = False
    if cfg.get("fabric_mods"):
        try:
            progress(0.02, "Устанавливаю Fabric...")
            fid, fjson = get_fabric_profile(vid, mcdir)
            data = merge_versions(fjson, vanilla)
            used_fabric = True
            log("Fabric: " + fid)
        except Exception as e:
            log("Fabric недоступен для %s (%s) — запускаю ванильную версию" % (vid, e))

    req = (data.get("javaVersion") or {}).get("majorVersion", 8)
    java = ensure_java(cfg, req, log, progress)
    log("Java: %s" % java)

    tasks = []
    jar_id = data["jar_id"]
    client = vanilla["downloads"]["client"]
    jar = mcdir / "versions" / jar_id / (jar_id + ".jar")
    tasks.append((client["url"], jar, client.get("sha1"), client.get("size")))
    cp_files, ltasks, to_extract = resolve_libraries(data["libraries"], mcdir / "libraries")
    tasks += ltasks

    ai = data["assetIndex"]
    idx_path = mcdir / "assets" / "indexes" / (ai["id"] + ".json")
    download(ai["url"], idx_path, sha1=ai.get("sha1"))
    index = json.loads(idx_path.read_text("utf-8"))
    objects = index.get("objects", {})
    for name, o in objects.items():
        h = o["hash"]
        tasks.append(("https://resources.download.minecraft.net/%s/%s" % (h[:2], h),
                      mcdir / "assets" / "objects" / h[:2] / h, h, o.get("size")))
    progress(0.05, "Проверяю файлы игры...")
    download_many(tasks, progress, "Файлы игры")

    game_assets = mcdir / "assets"
    if index.get("map_to_resources") or index.get("virtual"):
        target = (mcdir / "resources") if index.get("map_to_resources") else (mcdir / "assets" / "virtual" / "legacy")
        for name, o in objects.items():
            src = mcdir / "assets" / "objects" / o["hash"][:2] / o["hash"]
            dst = target / name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(src), str(dst))
        game_assets = target

    natives = mcdir / "versions" / data["id"] / "natives"
    extract_natives(to_extract, natives)

    if used_fabric:
        auto = bool(cfg.get("mods_auto_install", True)) and not cfg.get("mods_user_removed", False)
        progress(1.0, "Fabric API и моды оптимизации...")
        if auto:
            # Первый запуск и последующие: гарантированно ставим ВСЁ (100%)
            install_mods(mcdir, vid, log)
            cfg["mods_auto_install"] = False
            cfg["mods_user_removed"] = False
            try:
                save_config(cfg)
            except Exception:
                pass
        else:
            # Пользователь удалил моды или отключил автоустановку — только докачиваем отсутствующие
            install_mods(mcdir, vid, log, only_missing=True)
    else:
        # без Fabric (выключен или для версии его нет): моды лаунчера не нужны, убираем
        clean_managed_mods(mcdir, log)

    classpath = os.pathsep.join([str(p) for p in cp_files] + [str(jar)])
    cmd = build_command(cfg, data, java, mcdir, classpath, natives, mcdir / "assets", game_assets)
    env = dict(os.environ)
    if cfg.get("optimize_jvm", True) and SYSTEM == "Linux":
        env.setdefault("__GL_THREADED_OPTIMIZATIONS", "1")
        env.setdefault("mesa_glthread", "true")
    progress(1.0, "Готово к запуску")
    return cmd, env, mcdir


def cleanup_logs(keep=10):
    try:
        files = sorted(LOG_DIR.glob("game-*.log"))
        for f in files[:-keep]:
            f.unlink()
    except Exception:
        pass


def launch_process(cmd, env, mcdir, optimize):
    """Игра пишет в файл-лог: не зависит от окна лаунчера и не падает, если его закрыть."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_logs()
    logpath = LOG_DIR / ("game-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    kw = {}
    if SYSTEM == "Windows":
        flags = CREATE_NO_WINDOW
        if optimize:
            flags |= ABOVE_NORMAL_PRIORITY
        kw["creationflags"] = flags
    logf = open(str(logpath), "wb")
    try:
        proc = subprocess.Popen(cmd, cwd=str(mcdir), env=env, stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=subprocess.STDOUT, **kw)
    finally:
        logf.close()
    return proc, logpath


def tail_log(path, proc, emit):
    buf = b""
    with open(str(path), "rb") as f:
        while True:
            chunk = f.read(65536)
            if chunk:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    emit(ln.decode("utf-8", "replace").rstrip())
                continue
            if proc.poll() is not None:
                buf += f.read()
                for ln in buf.split(b"\n"):
                    if ln.strip():
                        emit(ln.decode("utf-8", "replace").rstrip())
                break
            time.sleep(0.25)


def open_folder(path):
    path = str(path)
    Path(path).mkdir(parents=True, exist_ok=True)
    if SYSTEM == "Windows":
        os.startfile(path)  # noqa
    elif SYSTEM == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
import base64
import math
import random
import struct
import zlib

try:
    import tkinter.font as tkfont
except Exception:  # noqa
    tkfont = None

FONT = "Segoe UI"

THEMES = {
    "light": {
        "BG": "#f5f6fa", "PANEL": "#ffffff", "PANEL2": "#f0f2f7", "LINE": "#e2e5ee",
        "HOVER": "#e8ebf3", "FG": "#14161c", "MUTED": "#6b7280", "DIM": "#9aa1ad",
        "ACCENT": "#7c4dff", "ACCENT_HI": "#9670ff", "ACCENT_LO": "#5e35d6",
        "ACCENT_FG": "#ffffff", "RED": "#e5484d", "GREEN": "#30a46c",
    },
    "dark": {
        "BG": "#0e1015", "PANEL": "#161922", "PANEL2": "#1e222e", "LINE": "#2a2f3e",
        "HOVER": "#262c3c", "FG": "#f2f4f8", "MUTED": "#8b93a7", "DIM": "#5c6478",
        "ACCENT": "#b6f26c", "ACCENT_HI": "#cbff8a", "ACCENT_LO": "#9bd94f",
        "ACCENT_FG": "#10140a", "RED": "#ff6b6b", "GREEN": "#4ade80",
    },
}
CURRENT_THEME = "light"


def apply_theme(name):
    global CURRENT_THEME
    global BG, PANEL, PANEL2, LINE, HOVER, FG, MUTED, DIM
    global ACCENT, ACCENT_HI, ACCENT_LO, ACCENT_FG, RED, GREEN
    CURRENT_THEME = name if name in THEMES else "light"
    t = THEMES[CURRENT_THEME]
    BG = t["BG"]
    PANEL = t["PANEL"]
    PANEL2 = t["PANEL2"]
    LINE = t["LINE"]
    HOVER = t["HOVER"]
    FG = t["FG"]
    MUTED = t["MUTED"]
    DIM = t["DIM"]
    ACCENT = t["ACCENT"]
    ACCENT_HI = t["ACCENT_HI"]
    ACCENT_LO = t["ACCENT_LO"]
    ACCENT_FG = t["ACCENT_FG"]
    RED = t["RED"]
    GREEN = t["GREEN"]


apply_theme("light")

_C = tk.Canvas if tk else object
_F = tk.Frame if tk else object


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(int(ra[i] + (rb[i] - ra[i]) * t) for i in range(3))


# ---- сглаженная графика без внешних библиотек: PNG с альфа-каналом, собранный вручную
_IMG = {}


def _png(w, h, rows):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _photo(key, w, h, rows):
    img = _IMG.get(key)
    if img is None:
        img = tk.PhotoImage(data=base64.b64encode(_png(w, h, rows)))
        _IMG[key] = img
    return img


def corner_img(r, fill, border, bw, quad):
    key = ("c", r, fill, border, bw, quad)
    if key in _IMG:
        return _IMG[key]
    cx, cy = {"tl": (r, r), "tr": (0, r), "bl": (r, 0), "br": (0, 0)}[quad]
    f = _rgb(fill)
    b = _rgb(border) if border else f
    ss = 4
    rows = []
    for y in range(r):
        row = bytearray()
        for x in range(r):
            outer = inner = 0
            for sy in range(ss):
                for sx in range(ss):
                    d = math.hypot(x + (sx + .5) / ss - cx, y + (sy + .5) / ss - cy)
                    if d <= r:
                        outer += 1
                    if d <= r - bw:
                        inner += 1
            if not outer:
                row += b"\x00\x00\x00\x00"
                continue
            col = [(f[i] * inner + b[i] * (outer - inner)) / float(outer) for i in range(3)]
            row += bytes([int(c) for c in col]) + bytes([int(255 * outer / float(ss * ss))])
        rows.append(bytes(row))
    return _photo(key, r, r, rows)


def disc_img(d, color):
    key = ("d", d, color)
    if key in _IMG:
        return _IMG[key]
    c = _rgb(color)
    ss = 4
    rad = d / 2.0
    rows = []
    for y in range(d):
        row = bytearray()
        for x in range(d):
            n = 0
            for sy in range(ss):
                for sx in range(ss):
                    if math.hypot(x + (sx + .5) / ss - rad, y + (sy + .5) / ss - rad) <= rad:
                        n += 1
            row += bytes(c) + bytes([int(255 * n / float(ss * ss))])
        rows.append(bytes(row))
    return _photo(key, d, d, rows)


def rrect(c, x1, y1, x2, y2, r, fill, border=None, bw=1, tags=()):
    """Скруглённый прямоугольник со сглаженными углами. x2, y2 — внешние границы."""
    w, h = x2 - x1, y2 - y1
    if w < 2 or h < 2:
        return
    r = max(1, min(int(r), w // 2, h // 2))
    bw = bw if border else 0
    bcol = border or fill
    if w - 2 * r > 0:
        c.create_rectangle(x1 + r, y1 + bw, x2 - r, y2 - bw, fill=fill, outline="", tags=tags)
    if h - 2 * r > 0:
        c.create_rectangle(x1 + bw, y1 + r, x2 - bw, y2 - r, fill=fill, outline="", tags=tags)
    if bw:
        if w - 2 * r > 0:
            c.create_rectangle(x1 + r, y1, x2 - r, y1 + bw, fill=bcol, outline="", tags=tags)
            c.create_rectangle(x1 + r, y2 - bw, x2 - r, y2, fill=bcol, outline="", tags=tags)
        if h - 2 * r > 0:
            c.create_rectangle(x1, y1 + r, x1 + bw, y2 - r, fill=bcol, outline="", tags=tags)
            c.create_rectangle(x2 - bw, y1 + r, x2, y2 - r, fill=bcol, outline="", tags=tags)
    for quad, (x, y) in (("tl", (x1, y1)), ("tr", (x2 - r, y1)), ("bl", (x1, y2 - r)), ("br", (x2 - r, y2 - r))):
        c.create_image(x, y, image=corner_img(r, fill, bcol, bw, quad), anchor="nw", tags=tags)


def lbl(parent, text, size=11, bold=False, color=FG, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", parent["bg"]), fg=color,
                    font=(FONT, size, "bold" if bold else "normal"), **kw)


def _valid_nick(n):
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", n))


def _fix_clipboard(entry):
    """Ctrl+C/V/X/A при русской раскладке в Windows (Tk по умолчанию их не ловит)."""
    if SYSTEM != "Windows":
        return

    def on_ctrl(e):
        k = e.keycode
        if k == 86:
            e.widget.event_generate("<<Paste>>")
        elif k == 67:
            e.widget.event_generate("<<Copy>>")
        elif k == 88:
            e.widget.event_generate("<<Cut>>")
        elif k == 65:
            e.widget.select_range(0, "end")
            e.widget.icursor("end")
        else:
            return None
        return "break"

    entry.bind("<Control-KeyPress>", on_ctrl)


def _make_logo_png(sz=64):
    s = sz / 512.0
    rects = [((140, 96, 250, 410), (255, 176, 32)), ((140, 300, 380, 410), (255, 176, 32)),
             ((290, 96, 380, 186), (255, 106, 31)), ((310, 70, 360, 120), (255, 211, 122))]
    rad = 112 * s
    rows = []
    for y in range(sz):
        row = bytearray()
        for x in range(sz):
            px, py = x + .5, y + .5
            ins = 16 * s <= px <= sz - 16 * s and 16 * s <= py <= sz - 16 * s
            if ins:
                for (cx, cy) in ((16 * s + rad, 16 * s + rad), (sz - 16 * s - rad, 16 * s + rad),
                                 (16 * s + rad, sz - 16 * s - rad), (sz - 16 * s - rad, sz - 16 * s - rad)):
                    if ((px < 16 * s + rad) == (cx < sz / 2.0)) and ((py < 16 * s + rad) == (cy < sz / 2.0)) \
                            and math.hypot(px - cx, py - cy) > rad and \
                            ((px < sz / 2.0) == (cx < sz / 2.0)) and ((py < sz / 2.0) == (cy < sz / 2.0)):
                        ins = False
            if not ins:
                row += b"\x00\x00\x00\x00"
                continue
            col = (19, 22, 31)
            for (x1, y1, x2, y2), cc in rects:
                if x1 * s <= px <= x2 * s and y1 * s <= py <= y2 * s:
                    col = cc
            row += bytes(col) + b"\xff"
        rows.append(bytes(row))
    return base64.b64encode(_png(sz, sz, rows))


# ----------------------------------------------------------------------------
# Виджеты
# ----------------------------------------------------------------------------
class RoundButton(_C):
    def __init__(self, parent, text, command, width=150, height=42, primary=False, size=11, radius=11):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0,
                         bd=0, cursor="hand2")
        self.w, self.h, self.radius = width, height, radius
        self.command, self.primary, self.size = command, primary, size
        self.text, self.enabled, self.hover, self.pressed = text, True, False, False
        self.bind("<Enter>", lambda e: self._st(hover=True))
        self.bind("<Leave>", lambda e: self._st(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._st(pressed=True))
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Configure>", self._on_resize)
        self.draw()

    def _on_resize(self, e):
        if (int(e.width), int(e.height)) != (int(self.w), int(self.h)):
            self.w, self.h = e.width, e.height
            self.draw()

    def _st(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def _click(self, e):
        was = self.pressed
        self._st(pressed=False)
        if was and self.enabled and 0 <= e.x <= self.w and 0 <= e.y <= self.h:
            self.command()

    def set(self, text=None, enabled=None, primary=None):
        if text is not None:
            self.text = text
        if primary is not None:
            self.primary = primary
        if enabled is not None:
            self.enabled = enabled
            self.config(cursor="hand2" if enabled else "arrow")
        self.draw()

    def draw(self):
        self.delete("all")
        try:
            w = max(int(self.winfo_width()), int(self.w))
            h = max(int(self.winfo_height()), int(self.h))
        except Exception:
            w, h = int(self.w), int(self.h)
        bd = None
        if not self.enabled:
            fill, fg, bd = "#eef0f2", DIM, LINE
        elif self.primary:
            fill = ACCENT_LO if self.pressed else (ACCENT_HI if self.hover else ACCENT)
            fg = ACCENT_FG
        else:
            fill = "#cfd6de" if self.pressed else (HOVER if self.hover else PANEL2)
            fg, bd = FG, (ACCENT if self.hover else LINE)
        rrect(self, 0, 0, w, h, self.radius, fill, bd, 1)
        self.create_text(w / 2.0, h / 2.0, text=self.text, fill=fg, font=(FONT, self.size, "bold"))


class Toggle(_F):
    def __init__(self, parent, text, var, sub=None, command=None, wrap=420):
        super().__init__(parent, bg=parent["bg"])
        self.var, self.command = var, command
        self.c = tk.Canvas(self, width=44, height=24, bg=parent["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.c.pack(side="left", anchor="n", pady=1)
        tf = tk.Frame(self, bg=parent["bg"])
        tf.pack(side="left", padx=12)
        self.l = tk.Label(tf, text=text, bg=parent["bg"], fg=FG, font=(FONT, 11), anchor="w", justify="left",
                          cursor="hand2", wraplength=wrap)
        self.l.pack(anchor="w")
        if sub:
            tk.Label(tf, text=sub, bg=parent["bg"], fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                     wraplength=wrap).pack(anchor="w")
        for w in (self.c, self.l):
            w.bind("<Button-1>", self._toggle)
        self._tid = var.trace_add("write", lambda *a: self.draw())
        self.bind("<Destroy>", self._destroyed)
        self.draw()

    def _destroyed(self, e):
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._tid)
            except Exception:
                pass

    def _toggle(self, _e=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def draw(self):
        try:
            on = bool(self.var.get())
            c = self.c
            c.delete("all")
            rrect(c, 0, 0, 44, 24, 12, ACCENT if on else "#2a3046")
            x = 23 if on else 3
            c.create_image(x, 3, image=disc_img(18, "#ffffff" if on else "#9aa3ba"), anchor="nw")
        except Exception:
            pass


class Slider(_C):
    PAD = 12

    def __init__(self, parent, var, lo, hi, step=256, height=30):
        super().__init__(parent, height=height, bg=parent["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.var, self.lo, self.hi, self.step = var, lo, hi, step
        self.active = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)
        self._tid = var.trace_add("write", lambda *a: self.draw())
        self.bind("<Destroy>", self._destroyed)

    def _destroyed(self, e):
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._tid)
            except Exception:
                pass

    def _span(self):
        return max(self.winfo_width() - 2 * self.PAD, 10)

    def _set_x(self, x):
        t = (x - self.PAD) / float(self._span())
        v = self.lo + t * (self.hi - self.lo)
        v = int(round(v / float(self.step)) * self.step)
        self.var.set(max(self.lo, min(self.hi, v)))

    def _press(self, e):
        self.active = True
        self._set_x(e.x)

    def _move(self, e):
        self._set_x(e.x)

    def _release(self, e):
        self.active = False
        self.draw()

    def _wheel(self, e):
        d = self.step if e.delta > 0 else -self.step
        self.var.set(max(self.lo, min(self.hi, int(self.var.get()) + d)))

    def draw(self):
        try:
            w = self.winfo_width()
            h = int(self.cget("height"))
            self.delete("all")
            if w < 40:
                return
            cy = h // 2
            frac = (self.var.get() - self.lo) / float(self.hi - self.lo)
            x = self.PAD + frac * self._span()
            rrect(self, self.PAD, cy - 3, w - self.PAD, cy + 3, 3, "#262c40")
            if x - self.PAD > 6:
                rrect(self, self.PAD, cy - 3, int(x), cy + 3, 3, ACCENT)
            if self.active:
                self.create_image(x - 15, cy - 15, image=disc_img(30, "#5a4318"), anchor="nw")
            self.create_image(x - 10, cy - 10, image=disc_img(20, "#ffffff"), anchor="nw")
        except Exception:
            pass


class Progress(_C):
    def __init__(self, parent, height=8):
        super().__init__(parent, height=height, bg=parent["bg"], highlightthickness=0, bd=0)
        self.value = 0.0
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, v):
        self.value = max(0.0, min(1.0, v))
        self.draw()

    def draw(self):
        w, h = self.winfo_width(), int(self.cget("height"))
        self.delete("all")
        if w < 8:
            return
        rrect(self, 0, 0, w, h, h // 2, "#1d2233")
        fw = int(w * self.value)
        if fw >= h:
            rrect(self, 0, 0, fw, h, h // 2, ACCENT)


class Field(_C):
    def __init__(self, parent, var, width=20, size=12, radius=9):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0)
        self.focus, self.radius, self.pad = False, radius, 12
        self.entry = tk.Entry(self, textvariable=var, width=width, bg=PANEL2, fg=FG, insertbackground=FG,
                              relief="flat", bd=0, font=(FONT, size), highlightthickness=0,
                              selectbackground=ACCENT, selectforeground=ACCENT_FG)
        _fix_clipboard(self.entry)
        self.h = self.entry.winfo_reqheight() + 16
        self.config(width=self.entry.winfo_reqwidth() + 2 * self.pad, height=self.h)
        self._win = self.create_window(self.pad, self.h // 2, window=self.entry, anchor="w")
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._focus(False))

    def _focus(self, v):
        self.focus = v
        self.draw()

    def draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        self.delete("bg")
        rrect(self, 0, 0, w, h, self.radius, PANEL2, ACCENT if self.focus else LINE, 1, tags=("bg",))
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=w - 2 * self.pad)
        self.coords(self._win, self.pad, h // 2)


class Card(_C):
    """Скруглённая карточка. Содержимое кладётся в card.body."""

    def __init__(self, parent, title=None, padx=24, pady=20, stretch=False, radius=14):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0, width=1, height=1)
        self.padx, self.pady, self.stretch, self.radius = padx, pady, stretch, radius
        self.body = tk.Frame(self, bg=PANEL)
        self._win = self.create_window(padx, pady, window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_canvas)
        if not stretch:
            self.body.bind("<Configure>", self._on_body)
        if title:
            lbl(self.body, title.upper(), 9, True, ACCENT).pack(anchor="w", pady=(0, 12))

    def _on_body(self, e):
        h = self.body.winfo_reqheight() + 2 * self.pady
        if int(float(self.cget("height"))) != h:
            self.config(height=h)

    def _on_canvas(self, e):
        w, h = e.width, e.height
        self.delete("bg")
        rrect(self, 0, 0, w, h, self.radius, PANEL, LINE, 1, tags=("bg",))
        self.tag_lower("bg")
        opts = {"width": max(10, w - 2 * self.padx)}
        if self.stretch:
            opts["height"] = max(10, h - 2 * self.pady)
        self.itemconfigure(self._win, **opts)


# ----------------------------------------------------------------------------
# Приложение
# ----------------------------------------------------------------------------
class App(tk.Tk if tk else object):
    TABS = (("play", "ИГРАТЬ"), ("settings", "НАСТРОЙКИ"), ("info", "ИНФОРМАЦИЯ"), ("about", "О ЛАУНЧЕРЕ"))

    def __init__(self):
        super().__init__()
        self.title("%s %s" % (APP_NAME, APP_VERSION))
        self.minsize(1000, 680)
        self.configure(bg=BG)
        # Стартовый размер — под содержимое, по центру экрана
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(max(1200, int(sw * 0.72)), sw - 80)
        h = min(max(800, int(sh * 0.78)), sh - 80)
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        try:
            self._icon = tk.PhotoImage(data=_make_logo_png(64))
            self.iconphoto(True, self._icon)
        except Exception:
            pass
        self.cfg = load_config()
        apply_theme(self.cfg.get("theme", "light"))
        self.q = queue.Queue()
        self.busy = False
        self.manifest = load_cached_manifest()
        self.update_info = None
        self.update_busy = False
        self.ver_state = "loading"      # loading | ok | error
        self.ver_error = ""
        self._ver_thread = False
        self.log_lines = []
        self.log_visible = False
        self.page = "play"
        self.total_ram = total_ram_mb()
        self._hero_job = None
        self._stars = []
        self._style()

        self.v_nick = tk.StringVar(value=self.cfg["nickname"])
        self.v_version = tk.StringVar(value=self.cfg["version"])
        self.v_snap = tk.BooleanVar(value=self.cfg["show_snapshots"])
        self.v_ram = tk.IntVar(value=self.cfg["ram_mb"])
        self.v_java = tk.StringVar(value=self.cfg["java_path"])
        self.v_opt = tk.BooleanVar(value=self.cfg["optimize_jvm"])
        self.v_fabric = tk.BooleanVar(value=self.cfg["fabric_mods"])
        self.v_full = tk.BooleanVar(value=self.cfg["fullscreen"])
        self.v_w = tk.StringVar(value=str(self.cfg["width"]))
        self.v_h = tk.StringVar(value=str(self.cfg["height"]))
        self.v_extra = tk.StringVar(value=self.cfg["extra_jvm"])
        self.v_proxy = tk.StringVar(value=self.cfg.get("proxy", ""))
        self.v_dark = tk.BooleanVar(value=(self.cfg.get("theme", "light") == "dark"))

        self.shell = tk.Frame(self, bg=BG)
        self.shell.pack(fill="both", expand=True)
        if self.cfg["setup_done"]:
            self.build_main()
        else:
            self.build_wizard(0)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(60, self.poll)
        self.reload_versions()
        self.check_updates(silent=True)

    # ---- общее
    def report_callback_exception(self, exc, val, tb):
        msg = "".join(traceback.format_exception(exc, val, tb))
        try:
            BASE.mkdir(parents=True, exist_ok=True)
            with open(str(BASE / "launcher-error.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + msg + "\n")
        except Exception:
            pass
        self.log_lines.append("Внутренняя ошибка интерфейса: %s" % val)

    def on_close(self):
        try:
            self.collect_cfg()
            save_config(self.cfg)
        except Exception:
            pass
        self.destroy()

    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Lit.TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=FG,
                    arrowcolor=ACCENT, bordercolor=LINE, lightcolor=PANEL2, darkcolor=PANEL2,
                    selectbackground=PANEL2, selectforeground=FG, padding=(12, 9), arrowsize=14,
                    relief="flat")
        s.map("Lit.TCombobox", fieldbackground=[("readonly", PANEL2), ("disabled", PANEL2)],
              foreground=[("readonly", FG), ("disabled", DIM)], bordercolor=[("focus", ACCENT)],
              background=[("active", HOVER)], arrowcolor=[("active", ACCENT_HI)])
        s.configure("Vertical.TScrollbar", troughcolor=PANEL, background=PANEL2, bordercolor=PANEL,
                    arrowcolor=MUTED, lightcolor=PANEL2, darkcolor=PANEL2, width=10)
        self.option_add("*TCombobox*Listbox.background", PANEL2)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ACCENT_FG)
        self.option_add("*TCombobox*Listbox.font", (FONT, 11))

    def _alive(self, name):
        w = getattr(self, name, None)
        try:
            return w is not None and bool(w.winfo_exists())
        except Exception:
            return False

    def clear(self):
        for w in self.shell.winfo_children():
            w.destroy()

    # ---- мастер первой настройки
    def build_wizard(self, step):
        self.clear()
        outer = tk.Frame(self.shell, bg=BG)
        outer.pack(fill="both", expand=True)
        card = Card(outer, padx=44, pady=34, stretch=True, radius=18)
        card.place(relx=0.5, rely=0.5, anchor="center", width=740, height=560)
        body = card.body

        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")
        lbl(top, "LIT", 18, True, ACCENT).pack(side="left")
        lbl(top, " LAUNCHER", 11, True, FG).pack(side="left", pady=(5, 0))
        dots = tk.Canvas(top, width=4 * 34, height=12, bg=PANEL, highlightthickness=0, bd=0)
        dots.pack(side="right")
        x = 4 * 34 - 4
        for i in range(3, -1, -1):
            wd = 28 if i == step else 10
            x -= wd
            rrect(dots, x, 1, x + wd, 11, 5, ACCENT if i <= step else "#2a3046")
            x -= 6
        lbl(top, "Шаг %d из 4" % (step + 1), 9, False, MUTED).pack(side="right", padx=14, pady=(3, 0))

        nav = tk.Frame(body, bg=PANEL)
        nav.pack(side="bottom", fill="x")
        inner = tk.Frame(body, bg=PANEL)
        inner.pack(fill="both", expand=True, pady=(26, 0))

        rec = recommended_ram(self.total_ram, self.v_fabric.get())
        if step == 0:
            lbl(inner, "Добро пожаловать", 28, True).pack(anchor="w")
            lbl(inner, "Придумай ник. Остальное лаунчер сделает сам: скачает игру и Java\n"
                       "и запустит Minecraft с максимальной оптимизацией.",
                11, False, MUTED, justify="left").pack(anchor="w", pady=(8, 30))
            lbl(inner, "НИК ИГРОКА", 9, True, MUTED).pack(anchor="w", pady=(0, 7))
            fld = Field(inner, self.v_nick, 26, 14)
            fld.pack(anchor="w")
            fld.entry.focus_set()
            fld.entry.bind("<Return>", lambda e: self._wiz_go(0, 1))
            lbl(inner, "3–16 символов: латиница, цифры и _", 9, False, MUTED).pack(anchor="w", pady=(9, 0))
        elif step == 1:
            lbl(inner, "Оперативная память", 28, True).pack(anchor="w")
            lbl(inner, "В системе %.1f ГБ, рекомендуем %.1f ГБ.\nБольше не значит быстрее: лишняя память даёт паузы сборщика мусора." %
                (self.total_ram / 1024.0, rec / 1024.0), 11, False, MUTED, justify="left").pack(anchor="w", pady=(8, 28))
            val = lbl(inner, "", 34, True, ACCENT)
            val.pack(anchor="w")
            self._ram_label(val)
            top_ram = max(2048, int(min(self.total_ram * 0.8, 24576)) // 256 * 256)
            Slider(inner, self.v_ram, 1024, top_ram, 256).pack(fill="x", pady=(10, 6))
            RoundButton(inner, "Подставить рекомендуемое", lambda: self.v_ram.set(rec), width=270, height=40,
                        size=10).pack(anchor="w", pady=8)
            if self.v_ram.get() == DEFAULT_CONFIG["ram_mb"] and not self.cfg["setup_done"]:
                self.v_ram.set(rec)
        elif step == 2:
            lbl(inner, "Оптимизация", 28, True).pack(anchor="w")
            lbl(inner, "Всё включено по умолчанию: это и есть «максимум FPS».", 11, False, MUTED).pack(anchor="w", pady=(8, 26))
            Toggle(inner, "Оптимизированные JVM-флаги", self.v_opt,
                   "G1GC, Xms = Xmx, pre-touch, повышенный приоритет процесса", wrap=600).pack(anchor="w", pady=9)
            Toggle(inner, "Fabric + моды оптимизации", self.v_fabric,
                   "Sodium, Lithium, FerriteCore, ModernFix, EntityCulling, ImmediatelyFast. "
                   "Если для версии модов нет, запустится обычная игра.", wrap=600).pack(anchor="w", pady=9)
            lbl(inner, "Подходящая Java найдётся или скачается автоматически.", 10, False, MUTED).pack(anchor="w", pady=(16, 0))
        else:
            lbl(inner, "Всё готово", 28, True).pack(anchor="w", pady=(0, 16))
            rows = [("Ник", self.v_nick.get()), ("Память", "%.2f ГБ" % (self.v_ram.get() / 1024.0)),
                    ("JVM-оптимизация", "включена" if self.v_opt.get() else "выключена"),
                    ("Fabric + моды", "включены" if self.v_fabric.get() else "выключены")]
            for k, v in rows:
                r = tk.Frame(inner, bg=PANEL)
                r.pack(fill="x", pady=4)
                lbl(r, k, 11, False, MUTED, width=18, anchor="w").pack(side="left")
                lbl(r, v, 11, True, FG).pack(side="left")
            lbl(inner, "Папка игры: %s" % MC_DIR, 9, False, MUTED, wraplength=620, justify="left").pack(anchor="w", pady=(20, 0))

        if step > 0:
            RoundButton(nav, "Назад", lambda: self._wiz_go(step, -1), width=120).pack(side="left")
        if step < 3:
            RoundButton(nav, "Далее", lambda: self._wiz_go(step, 1), width=160, primary=True).pack(side="right")
        else:
            RoundButton(nav, "Завершить", self._wiz_finish, width=180, primary=True).pack(side="right")

    def _ram_label(self, label):
        def upd(*_):
            try:
                label.config(text="%.2f ГБ" % (self.v_ram.get() / 1024.0))
            except Exception:
                pass
        tid = self.v_ram.trace_add("write", upd)
        label.bind("<Destroy>", lambda e: self._untrace(self.v_ram, tid) if e.widget is label else None)
        upd()

    def _untrace(self, var, tid):
        try:
            var.trace_remove("write", tid)
        except Exception:
            pass

    def _wiz_go(self, step, d):
        if step == 0 and d > 0 and not _valid_nick(self.v_nick.get().strip()):
            messagebox.showwarning("Ник", "Ник: 3–16 символов, латиница, цифры и _")
            return
        self.build_wizard(step + d)

    def _wiz_finish(self):
        self.cfg["setup_done"] = True
        self.collect_cfg()
        save_config(self.cfg)
        self.build_main()

    # ---- каркас главного окна (боковая навигация)
    def build_main(self):
        self.clear()
        self.upd_bar = None

        # ===== Современная шапка =====
        head = tk.Frame(self.shell, bg=PANEL, height=64)
        head.pack(fill="x")
        self.head_ref = head
        head.pack_propagate(False)
        accent_line = tk.Frame(head, bg=ACCENT, height=3)
        accent_line.pack(side="bottom", fill="x")
        logo = tk.Frame(head, bg=PANEL)
        logo.pack(side="left", padx=(26, 0))
        self._logo_cv = tk.Canvas(logo, width=34, height=34, bg=PANEL, highlightthickness=0, bd=0)
        self._logo_cv.pack(side="left", pady=15)
        rrect(self._logo_cv, 2, 2, 32, 32, 9, ACCENT)
        self._logo_cv.create_text(17, 17, text="L", fill=ACCENT_FG, font=(FONT, 17, "bold"))
        lbl(logo, "LIT", 17, True, FG, bg=PANEL).pack(side="left", padx=(10, 0))
        lbl(logo, "LAUNCHER", 9, True, MUTED, bg=PANEL).pack(side="left", padx=(4, 0), pady=(2, 0))
        badge = tk.Frame(head, bg=PANEL2)
        badge.pack(side="right", padx=(0, 26), pady=18)
        lbl(badge, "v%s" % APP_VERSION, 9, True, FG, bg=PANEL2).pack(padx=10, pady=4)
        lbl(head, "ОЗУ %.1f ГБ" % (self.total_ram / 1024.0), 9, True, MUTED, bg=PANEL).pack(side="right", padx=(0, 14))

        wrap = tk.Frame(self.shell, bg=BG)
        wrap.pack(fill="both", expand=True)

        # ===== Современный сайдбар =====
        side = tk.Frame(wrap, bg=PANEL, width=224)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(side, bg=PANEL, height=14).pack(fill="x")
        self.tabs = {}
        icons = {"play": "▶", "settings": "⚙", "info": "ℹ", "about": "★"}
        for key, title in self.TABS:
            row = tk.Frame(side, bg=PANEL, cursor="hand2", height=46)
            row.pack(fill="x", padx=12, pady=2)
            row.pack_propagate(False)
            bar = tk.Frame(row, bg=PANEL, width=4)
            bar.pack(side="left", fill="y")
            ic = tk.Label(row, text=icons.get(key, "•"), bg=PANEL, fg=MUTED, font=(FONT, 12, "bold"),
                          width=2, cursor="hand2")
            ic.pack(side="left", padx=(10, 0))
            l = tk.Label(row, text=title, bg=PANEL, fg=MUTED, font=(FONT, 11, "bold"),
                         anchor="w", padx=8, cursor="hand2")
            l.pack(side="left", fill="x", expand=True)
            for w in (row, l, ic):
                w.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            self.tabs[key] = (l, bar, row, ic)
        tk.Frame(side, bg=LINE, height=1).pack(side="bottom", fill="x")
        lbl(side, "Made by Nebobs", 8, False, DIM, bg=PANEL).pack(side="bottom", anchor="w", padx=20, pady=12)

        self.content = tk.Frame(wrap, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.show_page("play")
        if self.update_info:
            self.show_update_bar()

    def show_page(self, key):
        self.page = key
        for k, tup in self.tabs.items():
            l, bar, row, ic = tup
            active = (k == key)
            bgc = PANEL2 if active else PANEL
            fgc = ACCENT if active else MUTED
            row.config(bg=bgc)
            l.config(bg=bgc, fg=FG if active else MUTED)
            ic.config(bg=bgc, fg=fgc)
            bar.config(bg=ACCENT if active else PANEL)
        for w in self.content.winfo_children():
            w.destroy()
        {"play": self.page_play, "settings": self.page_settings, "info": self.page_info, "about": self.page_about}[key]()

    # ---- страница «Играть» (раскладка как в официальном лаунчере)
    def page_play(self):
        page = tk.Frame(self.content, bg=BG)
        page.pack(fill="both", expand=True)

        # ===== Левая часть: баннер/новости =====
        left = tk.Frame(page, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(28, 14), pady=26)

        hero_wrap = tk.Frame(left, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        hero_wrap.pack(fill="both", expand=True)
        self.hero = tk.Canvas(hero_wrap, bg=PANEL, highlightthickness=0, bd=0)
        self.hero.pack(fill="both", expand=True, padx=1, pady=1)
        self.hero.bind("<Configure>", self._hero_schedule)
        for v in (self.v_version, self.v_ram, self.v_opt, self.v_fabric):
            tid = v.trace_add("write", lambda *a: self._hero_schedule())
            self.hero.bind("<Destroy>", lambda e, v=v, tid=tid: self._untrace(v, tid), add="+")

        news = tk.Frame(left, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        news.pack(fill="x", pady=(14, 0))
        news_in = tk.Frame(news, bg=PANEL, padx=22, pady=18)
        news_in.pack(fill="x")
        tag = tk.Frame(news_in, bg=ACCENT)
        tag.pack(anchor="w")
        lbl(tag, "НОВОСТИ", 8, True, ACCENT_FG, bg=ACCENT).pack(padx=8, pady=2)
        lbl(news_in, "LitLauncher %s — стабильный запуск и максимум FPS" % APP_VERSION,
            14, True, FG).pack(anchor="w", pady=(10, 4))
        lbl(news_in, "G1GC/ZGC, авто-выбор Java, Fabric + Sodium. Всё готово к игре.",
            10, False, MUTED).pack(anchor="w")

        # ===== Правая часть: панель запуска =====
        right = tk.Frame(page, bg=PANEL, width=370, highlightthickness=1, highlightbackground=LINE)
        right.pack(side="right", fill="y", padx=(0, 26), pady=26)
        right.pack_propagate(False)
        right_in = tk.Frame(right, bg=PANEL, padx=22, pady=20)
        right_in.pack(fill="both", expand=True)

        lbl(right_in, "ВЕРСИЯ", 9, True, MUTED).pack(anchor="w")
        cap = tk.Frame(right_in, bg=PANEL)
        cap.pack(fill="x", pady=(6, 0))
        self.ver_info = tk.Label(cap, text="", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold"))
        self.ver_info.pack(side="right")
        self.ver_info.bind("<Button-1>", lambda e: self.reload_versions() if self.ver_state != "loading" else None)
        self.cb_version = ttk.Combobox(right_in, textvariable=self.v_version, state="readonly", width=22,
                                       font=(FONT, 12), style="Lit.TCombobox", height=12)
        self.cb_version.pack(anchor="w", fill="x", pady=(6, 16))
        self.cb_version.bind("<<ComboboxSelected>>", lambda e: (self.cb_version.selection_clear(), self._hero_schedule()))

        Toggle(right_in, "Снапшоты", self.v_snap, command=self.fill_versions).pack(anchor="w")

        self.play_btn = RoundButton(right_in, "▶  ИГРАТЬ", self.play, width=280, height=68, primary=True, size=18, radius=14)
        self.play_btn.pack(fill="x", pady=(22, 0))
        if self.busy:
            self.play_btn.set("ИДЁТ ЗАПУСК…", False)

        # Аккаунт внизу справа
        tk.Frame(right_in, bg=LINE, height=1).pack(fill="x", pady=(18, 10))
        lbl(right_in, "АККАУНТ", 9, True, MUTED).pack(anchor="w", pady=(0, 6))
        acc_btn = tk.Frame(right_in, bg=PANEL2, cursor="hand2", highlightthickness=1,
                           highlightbackground=LINE, highlightcolor=ACCENT)
        acc_btn.pack(fill="x")
        head_pad = tk.Frame(acc_btn, bg=PANEL2)
        head_pad.pack(fill="x", padx=10, pady=7)
        self._acc_avatar = tk.Canvas(head_pad, width=24, height=24, bg=PANEL2, highlightthickness=0, bd=0)
        self._acc_avatar.pack(side="left")
        self._acc_nick_lbl = tk.Label(head_pad, text=self.v_nick.get(), bg=PANEL2, fg=FG,
                                       font=(FONT, 11, "bold"), anchor="w")
        self._acc_nick_lbl.pack(side="left", padx=(8, 12), fill="x", expand=True)
        self._acc_arrow = tk.Label(head_pad, text="▾", bg=PANEL2, fg=ACCENT, font=(FONT, 11, "bold"))
        self._acc_arrow.pack(side="right")
        self._acc_btn = acc_btn
        self._acc_menu = None
        self._acc_head_pad = head_pad
        self._draw_acc_avatar()
        for w in (acc_btn, head_pad, self._acc_nick_lbl, self._acc_arrow, self._acc_avatar):
            w.bind("<Button-1>", lambda e: self.toggle_accounts())
        self.v_nick.trace_add("write", lambda *a: self._refresh_acc_header())

        # Статус и прогресс
        self.status = lbl(right_in, "Готов к запуску", 10, False, MUTED)
        self.status.pack(anchor="w", pady=(14, 4))
        self.bar = Progress(right_in)
        self.bar.pack(fill="x")
        self.log_toggle = tk.Label(right_in, text="Показать лог", bg=PANEL, fg=ACCENT, font=(FONT, 10, "bold"),
                                   cursor="hand2")
        self.log_toggle.pack(anchor="e", pady=(6, 0))
        self.log_toggle.bind("<Button-1>", lambda e: self.toggle_log())

        self.log_frame = tk.Frame(right_in, bg=PANEL2, height=160)
        self.log_frame.pack_propagate(False)
        self.log_box = tk.Text(self.log_frame, bg=PANEL2, fg="#2b3340", relief="flat", bd=0, height=6,
                               font=("Consolas", 9), wrap="word", padx=10, pady=8, insertbackground=FG,
                               selectbackground=ACCENT, selectforeground=ACCENT_FG)
        self.log_box.pack(fill="both", expand=True)
        if self.log_lines:
            self.log_box.insert("end", "\n".join(self.log_lines) + "\n")
            self.log_box.see("end")
        self.log_box.config(state="disabled")
        self.log_visible = False

        self.update_version_ui()
        self.fill_versions()
        if self.ver_state == "error" and not self.busy:
            self.set_status("Не удалось получить список версий: %s. Включи VPN или задай прокси в настройках." % self.ver_error, RED)
        self._twinkle()

    # ---- мультиаккаунт
    def _draw_acc_avatar(self):
        c = getattr(self, "_acc_avatar", None)
        if c is None:
            return
        try:
            c.delete("all")
        except Exception:
            return
        nick = self.v_nick.get().strip() or "Player"

        def done(data):
            if not data or not self._alive("_acc_avatar"):
                return
            try:
                img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
                if self._alive("_acc_avatar"):
                    self._acc_avatar.delete("all")
                    self._acc_avatar.create_image(0, 0, image=img, anchor="nw")
                    self._acc_avatar.image = img
            except Exception:
                pass

        def work():
            data = fetch_skin_head(nick, 24)
            if data:
                self.after(0, lambda d=data: done(d))
            else:
                self.after(0, lambda: self._draw_acc_fallback(c))

        try:
            self._draw_acc_fallback(c)
        except Exception:
            pass
        threading.Thread(target=work, daemon=True).start()

    def _draw_acc_fallback(self, c):
        try:
            c.delete("all")
            c.create_rectangle(0, 0, 28, 28, fill="#3a5fcd", outline="")
            c.create_rectangle(8, 4, 20, 10, fill="#d8b48a", outline="")
            c.create_rectangle(10, 12, 18, 16, fill="#e8c9a0", outline="")
            c.create_rectangle(6, 17, 22, 28, fill="#e8c9a0", outline="")
            c.create_rectangle(11, 6, 13, 8, fill="#1e1e1e", outline="")
            c.create_rectangle(15, 6, 17, 8, fill="#1e1e1e", outline="")
        except Exception:
            pass

    def _refresh_acc_header(self):
        try:
            if self._alive("_acc_nick_lbl"):
                self._acc_nick_lbl.config(text=self.v_nick.get())
            if self._alive("_acc_arrow") and getattr(self, "_acc_menu", None) is None:
                self._acc_arrow.config(text="▾")
            self._draw_acc_avatar()
        except Exception:
            pass

    def _close_acc_menu(self, _e=None):
        if getattr(self, "_acc_menu", None) is not None:
            try:
                self._acc_menu.destroy()
            except Exception:
                pass
            self._acc_menu = None
            if self._alive("_acc_arrow"):
                self._acc_arrow.config(text="▾")

    def toggle_accounts(self):
        if getattr(self, "_acc_menu", None) is not None:
            self._close_acc_menu()
            return
        if not self._alive("_acc_btn"):
            return
        accs = [a for a in (self.cfg.get("accounts") or []) if _valid_nick(a)]
        cur = self.v_nick.get().strip()
        if _valid_nick(cur) and cur not in accs:
            accs.insert(0, cur)
        menu = tk.Frame(self, bg=PANEL2, highlightthickness=1, highlightbackground=LINE)
        menu.place(in_=self._acc_btn, x=0, rely=1.0, y=6, anchor="nw", width=max(220, self._acc_btn.winfo_width()))
        self._acc_menu = menu
        self._acc_arrow.config(text="▴")

        list_box = tk.Frame(menu, bg=PANEL2)
        list_box.pack(fill="x", padx=4, pady=(4, 2))
        if not accs:
            lbl(list_box, "Нет сохранённых ников", 9, False, DIM).pack(anchor="w", padx=8, pady=6)
        for nick in accs:
            row = tk.Frame(list_box, bg=PANEL2, cursor="hand2")
            row.pack(fill="x", pady=1)
            av = tk.Canvas(row, width=22, height=22, bg=PANEL2, highlightthickness=0, bd=0)
            av.pack(side="left", padx=(6, 6), pady=4)
            av.create_rectangle(0, 0, 22, 22, fill="#3a5fcd", outline="")
            av.create_rectangle(6, 3, 16, 8, fill="#d8b48a", outline="")
            av.create_rectangle(4, 13, 18, 22, fill="#e8c9a0", outline="")

            def _set_av(data, canvas=av):
                if not data:
                    return
                try:
                    img = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
                    if canvas.winfo_exists():
                        canvas.delete("all")
                        canvas.create_image(0, 0, image=img, anchor="nw")
                        canvas.image = img
                except Exception:
                    pass

            def _load_av(n=nick, cb=_set_av):
                d = fetch_skin_head(n, 22)
                if d:
                    self.after(0, lambda x=d: cb(x))
            threading.Thread(target=_load_av, daemon=True).start()
            mark = "● " if nick == cur else "   "
            nl = tk.Label(row, text=mark + nick, bg=PANEL2, fg=ACCENT if nick == cur else FG,
                          font=(FONT, 10, "bold"), anchor="w")
            nl.pack(side="left", fill="x", expand=True, pady=6)
            del_btn = tk.Label(row, text="✕", bg=PANEL2, fg=DIM, font=(FONT, 9, "bold"), cursor="hand2")
            del_btn.pack(side="right", padx=8)
            del_btn.bind("<Button-1>", lambda e, n=nick: (self.delete_account(n), "break")[1])
            for w in (row, av, nl):
                w.bind("<Button-1>", lambda e, n=nick: self.select_account(n))
                w.bind("<Enter>", lambda e, r=row, l=nl: (r.config(bg=HOVER), l.config(bg=HOVER)))
                w.bind("<Leave>", lambda e, r=row, l=nl: (r.config(bg=PANEL2), l.config(bg=PANEL2)))

        tk.Frame(menu, bg=LINE, height=1).pack(fill="x", pady=(4, 0))
        add = tk.Frame(menu, bg=PANEL2, cursor="hand2")
        add.pack(fill="x", padx=4, pady=(2, 4))
        al = tk.Label(add, text="＋  Добавить ник", bg=PANEL2, fg=GREEN, font=(FONT, 10, "bold"), anchor="w")
        al.pack(fill="x", padx=8, pady=6)
        for w in (add, al):
            w.bind("<Button-1>", lambda e: self.open_add_account())
            w.bind("<Enter>", lambda e: (add.config(bg=HOVER), al.config(bg=HOVER)))
            w.bind("<Leave>", lambda e: (add.config(bg=PANEL2), al.config(bg=PANEL2)))
        self.bind("<Button-1>", self._acc_outside_click, add="+")
        # Открываем вверх, если снизу не хватает места
        try:
            self.update_idletasks()
            mw = max(220, self._acc_btn.winfo_width())
            menu.place_configure(width=mw)
            self.update_idletasks()
            mh = menu.winfo_reqheight()
            btn_top = self._acc_btn.winfo_rooty()
            screen_h = self.winfo_screenheight()
            if btn_top + mh + 12 > screen_h or (self.winfo_rooty() + self.winfo_height() - btn_top) < mh + 12:
                menu.place_configure(in_=self._acc_btn, x=0, rely=0.0, y=-6, anchor="sw", width=mw)
        except Exception:
            pass

    def _acc_outside_click(self, e):
        if getattr(self, "_acc_menu", None) is None:
            return
        w = e.widget
        try:
            while w is not None:
                if w is self._acc_menu or w is self._acc_btn:
                    return
                w = w.master
        except Exception:
            return
        self._close_acc_menu()

    def select_account(self, nick):
        self.v_nick.set(nick)
        self.cfg["nickname"] = nick
        save_config(self.cfg)
        self._close_acc_menu()
        self._draw_acc_avatar()

    def delete_account(self, nick):
        accs = [a for a in (self.cfg.get("accounts") or []) if a != nick]
        self.cfg["accounts"] = accs
        if self.v_nick.get().strip() == nick and accs:
            self.v_nick.set(accs[0])
            self.cfg["nickname"] = accs[0]
        save_config(self.cfg)
        self._close_acc_menu()
        self.toggle_accounts()

    def open_add_account(self):
        self._close_acc_menu()
        dlg = tk.Toplevel(self)
        dlg.title("Добавить ник")
        dlg.configure(bg=PANEL)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        body = tk.Frame(dlg, bg=PANEL, padx=26, pady=22)
        body.pack(fill="both", expand=True)
        lbl(body, "НОВЫЙ НИК", 9, True, MUTED).pack(anchor="w")
        var = tk.StringVar()
        fld = Field(body, var, 22, 13)
        fld.pack(anchor="w", pady=(8, 6))
        fld.entry.focus_set()
        err = lbl(body, "3–16 символов: латиница, цифры и _", 9, False, MUTED)
        err.pack(anchor="w")
        btns = tk.Frame(body, bg=PANEL)
        btns.pack(fill="x", pady=(16, 0))

        def do_add(_e=None):
            n = var.get().strip()
            if not _valid_nick(n):
                err.config(text="Некорректный ник", fg=RED)
                return
            accs = [a for a in (self.cfg.get("accounts") or []) if _valid_nick(a)]
            if n not in accs:
                accs.insert(0, n)
            self.cfg["accounts"] = accs
            self.cfg["nickname"] = n
            save_config(self.cfg)
            self.v_nick.set(n)
            dlg.destroy()
            self.toggle_accounts()

        RoundButton(btns, "Отмена", dlg.destroy, width=110).pack(side="right")
        RoundButton(btns, "Добавить", do_add, width=150, primary=True).pack(side="right", padx=(0, 8))
        fld.entry.bind("<Return>", do_add)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        try:
            dlg.update_idletasks()
            x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
            dlg.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def toggle_log(self, force=None):
        if not self._alive("log_frame"):
            return
        show = (not self.log_visible) if force is None else force
        if show == self.log_visible:
            return
        if show:
            self.log_frame.pack(side="bottom", fill="x", before=self.hero)
            self.log_box.see("end")
            self.log_toggle.config(text="Скрыть лог")
        else:
            self.log_frame.pack_forget()
            self.log_toggle.config(text="Показать лог")
        self.log_visible = show

    def set_status(self, text, color=MUTED):
        if self._alive("status"):
            self.status.config(text=text, fg=color)

    # ---- баннер
    def _hero_schedule(self, _e=None):
        if self._hero_job:
            try:
                self.after_cancel(self._hero_job)
            except Exception:
                pass
        self._hero_job = self.after(40, self.draw_hero)

    def draw_hero(self):
        self._hero_job = None
        if not self._alive("hero"):
            return
        c = self.hero
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 160)
        c.delete("all")
        top, mid, mid2, bot = "#0e0920", "#2b1650", "#7a2f6c", "#ff7d33"

        def sky(t):
            if t < 0.5:
                return _mix(top, mid, t / 0.5)
            if t < 0.8:
                return _mix(mid, mid2, (t - 0.5) / 0.3)
            return _mix(mid2, bot, (t - 0.8) / 0.2)

        horizon = h * 0.86
        step = 3
        for y in range(0, h, step):
            col = sky(min(1.0, y / horizon))
            c.create_rectangle(0, y, w, y + step, fill=col, outline=col)
        rng = random.Random(11)
        self._stars = []
        for i in range(80):
            x, y, s = rng.randrange(w), rng.randrange(max(1, int(h * 0.5))), rng.choice((2, 2, 3))
            col = _mix("#ffffff", "#b9a6ff", rng.random())
            self._stars.append((c.create_rectangle(x, y, x + s, y + s, fill=col, outline=col), col))
        # облака
        for cx, cy in ((int(w * 0.52), int(h * 0.20)), (int(w * 0.86), int(h * 0.30)), (int(w * 0.30), int(h * 0.46))):
            for dx, dy, ww, hh in ((0, 8, 80, 12), (14, 0, 46, 12), (28, 20, 56, 8)):
                c.create_rectangle(cx + dx, cy + dy, cx + dx + ww, cy + dy + hh, fill="#3d2466", outline="#3d2466")
        # солнце садится за горы
        sx, sy = int(w * 0.74), int(h * 0.66)
        for size, col in ((250, "#5a2a70"), (200, "#8a3a74"), (156, "#c8484f"), (116, "#ff7a3a"), (80, "#ffa14a"), (46, "#ffd37a")):
            c.create_rectangle(sx - size // 2, sy - size // 2, sx + size // 2, sy + size // 2, fill=col, outline=col)
        # горы: три слоя
        u = 28
        cols = w // u + 2
        layers = ((1.3, h * 0.36, h * 0.10, "#3a1b5e"), (3.7, h * 0.24, h * 0.09, "#241042"), (6.1, h * 0.13, h * 0.06, "#120a22"))
        for li, (seed, base, amp, color) in enumerate(layers):
            edge = _mix(color, "#ff9a5a", 0.16)
            for i in range(cols):
                v = (0.5 * math.sin(i * 0.23 + seed) + 0.3 * math.sin(i * 0.61 + seed * 2) + 0.2 * math.sin(i * 1.7 + seed * 3))
                hv = max(u * 2, int((base + amp * v) // u * u))
                c.create_rectangle(i * u, h - hv, i * u + u, h, fill=color, outline=color)
                c.create_rectangle(i * u, h - hv, i * u + u, h - hv + 4, fill=edge, outline=edge)
                if li == 2 and i % 3 == 1:
                    ty = h - hv
                    c.create_rectangle(i * u + 11, ty - 8, i * u + 17, ty, fill=color, outline=color)
                    c.create_rectangle(i * u + 6, ty - 22, i * u + 22, ty - 8, fill=color, outline=color)
                    c.create_rectangle(i * u + 10, ty - 34, i * u + 18, ty - 22, fill=color, outline=color)
        # заголовок
        ty = max(70, int(h * 0.24))
        c.create_text(62, ty + 3, anchor="w", text="LIT LAUNCHER", fill="#1a0a2a", font=(FONT, 44, "bold"))
        c.create_text(60, ty, anchor="w", text="LIT LAUNCHER", fill="#ffffff", font=(FONT, 44, "bold"))
        c.create_text(62, ty + 58, anchor="w", text="Максимум FPS. Минимум возни.", fill="#ffd9a8", font=(FONT, 15))
        # чипы с текущими настройками
        chips = []
        ver = self.v_version.get()
        chips.append("Minecraft %s" % ver if ver else "Версия не выбрана")
        try:
            chips.append("%.1f ГБ ОЗУ" % (self.v_ram.get() / 1024.0))
        except Exception:
            pass
        if self.v_opt.get():
            chips.append("G1GC")
        if self.v_fabric.get():
            chips.append("Fabric API + Sodium")
        fnt = tkfont.Font(family=FONT, size=10, weight="bold") if tkfont else None
        x, y = 60, ty + 104
        for chip in chips:
            wc = (fnt.measure(chip) if fnt else len(chip) * 8) + 30
            rrect(c, x, y, x + wc, y + 32, 10, "#22113a", "#6a4596", 1)
            c.create_text(x + wc / 2.0, y + 16, text=chip, fill="#efe4ff", font=(FONT, 10, "bold"))
            x += wc + 10

    def _twinkle(self):
        if not self._alive("hero"):
            return
        try:
            for item, col in random.sample(self._stars, min(6, len(self._stars))):
                dim = random.random() < 0.5
                c2 = _mix(col, "#2b1650", 0.7) if dim else col
                self.hero.itemconfigure(item, fill=c2, outline=c2)
        except Exception:
            pass
        self.after(550, self._twinkle)

    # ---- список версий
    def reload_versions(self):
        if self._ver_thread:
            return
        self._ver_thread = True
        self.ver_state = "loading"
        self.update_version_ui()
        threading.Thread(target=self._load_versions, daemon=True).start()

    def _load_versions(self):
        try:
            self.q.put(("versions", fetch_manifest()))
        except Exception as e:  # noqa
            self.q.put(("versions_error", str(e)))

    def update_version_ui(self):
        if not self._alive("ver_info"):
            return
        st = self.ver_state
        if st == "loading":
            self.ver_info.config(text="загрузка…", fg=MUTED, cursor="arrow")
        elif st == "error":
            self.ver_info.config(text=("из кэша · повторить" if self.manifest else "нет связи · повторить"),
                                 fg=RED, cursor="hand2")
        else:
            n = len(self.cb_version["values"]) if self._alive("cb_version") else 0
            self.ver_info.config(text="%d версий · обновить" % n, fg=MUTED, cursor="hand2")

    def version_ids(self):
        ids = []
        if self.manifest:
            snaps = self.v_snap.get()
            ids = [v["id"] for v in self.manifest["versions"] if snaps or v["type"] == "release"]
        for lv in local_versions():
            if lv not in ids:
                ids.append(lv)
        return ids

    def fill_versions(self):
        if not self._alive("cb_version"):
            return
        ids = self.version_ids()
        self.cb_version["values"] = ids
        cur = self.v_version.get()
        if cur not in ids:
            if self.manifest:
                self.v_version.set(self.manifest["latest"]["release"])
            else:
                self.v_version.set(ids[0] if ids else "")
        self.cb_version.config(state="readonly" if ids else "disabled")
        self.update_version_ui()
        self._hero_schedule()

    # ---- страница «Настройки»
    def page_settings(self):
        page = tk.Frame(self.content, bg=BG)
        page.pack(fill="both", expand=True)

        foot = tk.Frame(page, bg=PANEL)
        foot.pack(side="bottom", fill="x")
        tk.Frame(foot, bg=LINE, height=1).pack(fill="x")
        fr = tk.Frame(foot, bg=PANEL)
        fr.pack(fill="x", padx=34, pady=16)
        RoundButton(fr, "Сохранить", self.save_settings, width=170, primary=True).pack(side="right")
        RoundButton(fr, "Папка игры", lambda: open_folder(MC_DIR), width=150).pack(side="left")
        RoundButton(fr, "Мастер настройки", lambda: self.build_wizard(0), width=190).pack(side="left", padx=10)

        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=34, pady=(26, 20))
        lbl(body, "Настройки", 26, True, FG, bg=BG).pack(anchor="w", pady=(0, 16))
        cols = tk.Frame(body, bg=BG)
        cols.pack(fill="both", expand=True)
        cols.grid_columnconfigure(0, weight=1, uniform="c")
        cols.grid_columnconfigure(1, weight=1, uniform="c")
        cols.grid_rowconfigure(0, weight=1)

        left_wrap = tk.Frame(cols, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        left_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        lb = tk.Frame(left_wrap, bg=PANEL, padx=24, pady=20)
        lb.pack(fill="both", expand=True)
        lbl(lb, "ПРОИЗВОДИТЕЛЬНОСТЬ", 9, True, ACCENT, bg=PANEL).pack(anchor="w", pady=(0, 12))
        head = tk.Frame(lb, bg=PANEL)
        head.pack(fill="x")
        lbl(head, "Память (Xmx)", 11, False, FG).pack(side="left")
        val = lbl(head, "", 13, True, ACCENT)
        val.pack(side="right")
        self._ram_label(val)
        top_ram = max(2048, int(min(self.total_ram * 0.8, 24576)) // 256 * 256)
        Slider(lb, self.v_ram, 1024, top_ram, 256).pack(fill="x", pady=(8, 4))
        rec = recommended_ram(self.total_ram, self.v_fabric.get())
        br = tk.Frame(lb, bg=PANEL)
        br.pack(fill="x", pady=(6, 14))
        RoundButton(br, "Авто: %.1f ГБ" % (rec / 1024.0), lambda: self.v_ram.set(rec), width=140, height=34,
                    size=10, radius=9).pack(side="left")
        Toggle(lb, "Оптимизированные JVM-флаги", self.v_opt, "G1GC, pre-touch, высокий приоритет", wrap=330).pack(anchor="w", pady=8)
        Toggle(lb, "Fabric + моды оптимизации", self.v_fabric, "Fabric API, Sodium, Lithium, FerriteCore и др.", wrap=330).pack(anchor="w", pady=8)

        tk.Frame(lb, bg=LINE, height=1).pack(fill="x", pady=(14, 12))
        mh = tk.Frame(lb, bg=PANEL)
        mh.pack(fill="x")
        lbl(mh, "УСТАНОВЛЕННЫЕ МОДЫ", 9, True, MUTED).pack(side="left")
        RoundButton(mh, "Удалить моды", self.remove_mods, width=150, height=34, size=10,
                    radius=9).pack(side="right")
        reg = load_mod_registry(MC_DIR)
        lbl(mh, ("для Minecraft %s" % reg["mc"]) if reg.get("mc") else "нет", 9, True,
            ACCENT if reg.get("mc") else DIM).pack(side="right")
        self.mods_box = tk.Frame(lb, bg=PANEL)
        self.mods_box.pack(fill="x", pady=(8, 0))
        self.render_mods()
        self.install_mods_btn = RoundButton(lb, "Установить недостающие моды", self.install_missing_mods,
                                             height=40, primary=True, size=11, radius=10)
        self.install_mods_btn.pack(fill="x", pady=(12, 0))

        right_wrap = tk.Frame(cols, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        right_wrap.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        rb = tk.Frame(right_wrap, bg=PANEL, padx=24, pady=20)
        rb.pack(fill="both", expand=True)
        lbl(rb, "ИГРА", 9, True, ACCENT, bg=PANEL).pack(anchor="w", pady=(0, 12))
        lbl(rb, "Путь к Java (пусто = авто)", 10, False, MUTED).pack(anchor="w", pady=(0, 6))
        jr = tk.Frame(rb, bg=PANEL)
        jr.pack(fill="x")
        RoundButton(jr, "Обзор", lambda: self.v_java.set(filedialog.askopenfilename() or self.v_java.get()),
                    width=84, height=40, size=10, radius=9).pack(side="right", padx=(8, 0))
        Field(jr, self.v_java, 10, 11).pack(side="left", fill="x", expand=True)
        lbl(rb, "Разрешение окна", 10, False, MUTED).pack(anchor="w", pady=(16, 6))
        rr = tk.Frame(rb, bg=PANEL)
        rr.pack(anchor="w")
        Field(rr, self.v_w, 5, 11).pack(side="left")
        lbl(rr, "  ×  ", 11, False, MUTED).pack(side="left")
        Field(rr, self.v_h, 5, 11).pack(side="left")
        Toggle(rb, "Полный экран", self.v_full).pack(anchor="w", pady=(16, 0))
        lbl(rb, "Доп. JVM-аргументы", 10, False, MUTED).pack(anchor="w", pady=(16, 6))
        Field(rb, self.v_extra, 10, 11).pack(fill="x")

        tk.Frame(rb, bg=LINE, height=1).pack(fill="x", pady=(16, 12))
        lbl(rb, "ТЕМА ОФОРМЛЕНИЯ", 9, True, MUTED).pack(anchor="w", pady=(0, 6))
        Toggle(rb, "Тёмная тема", self.v_dark, "Светлая / тёмная — применяется сразу",
               command=self._toggle_theme).pack(anchor="w")
        lbl(rb, "Прокси (необязательно, если Mojang недоступен)", 10, False, MUTED).pack(anchor="w", pady=(16, 6))
        Field(rb, self.v_proxy, 10, 11).pack(fill="x")

    # ---- страница «Информация»
    def page_info(self):
        page = tk.Frame(self.content, bg=BG)
        page.pack(fill="both", expand=True, padx=34, pady=26)
        lbl(page, "Информация", 26, True, FG, bg=BG).pack(anchor="w", pady=(0, 16))

        card = tk.Frame(page, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="both", expand=True)

        # Градиентный баннер
        banner = tk.Canvas(card, height=170, bg=PANEL, highlightthickness=0, bd=0)
        banner.pack(fill="x")

        def _paint_banner(_e=None):
            w = max(banner.winfo_width(), 200)
            h = 170
            banner.delete("all")
            c1, c2 = "#0e0920", "#b6f26c"
            for y in range(0, h, 2):
                banner.create_rectangle(0, y, w, y + 2, fill=_mix(c1, c2, y / float(h)), outline="")
            for cx, cy, r, col in ((int(w * 0.15), 130, 90, "#1a0b33"),
                                    (int(w * 0.55), 40, 140, "#29125c"),
                                    (int(w * 0.9), 150, 120, "#1f6a3c")):
                banner.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline=col)
            banner.create_text(w // 2, 62, text="LIT LAUNCHER", fill="#ffffff",
                               font=(FONT, 30, "bold"))
            banner.create_text(w // 2, 106, text="Спасибо, что играешь с нами ♥", fill="#e6f7d0",
                               font=(FONT, 13, "bold"))
            banner.create_text(w // 2, 140, text="v%s  ·  Made by Nebobs" % APP_VERSION,
                               fill="#a6e96b", font=(FONT, 10, "bold"))
        banner.bind("<Configure>", lambda e: _paint_banner())
        self.after(50, _paint_banner)

        body = tk.Frame(card, bg=PANEL, padx=32, pady=26)
        body.pack(fill="both", expand=True)

        lbl(body, "Наш собственный SMP уже ждёт!", 18, True, FG, bg=PANEL).pack(anchor="w")
        lbl(body, "Заходи в наш Discord — там свой SMP, общение, ивенты и обновления лаунчера.",
            11, False, MUTED, bg=PANEL).pack(anchor="w", pady=(6, 18))

        def _open_discord():
            webbrowser.open("https://discord.gg/254UJYsFgN")
        dbtn = RoundButton(body, "Вступить в Discord →", _open_discord,
                           width=300, height=52, primary=True, size=13, radius=12)
        dbtn.pack(anchor="w")

        # Мини-статистика
        tk.Frame(body, bg=LINE, height=1).pack(fill="x", pady=(24, 18))
        stats = tk.Frame(body, bg=PANEL)
        stats.pack(fill="x")
        items = [
            ("Версия", APP_VERSION),
            ("Платформа", SYSTEM),
            ("ОЗУ ПК", "%.1f ГБ" % (self.total_ram / 1024.0)),
            ("Тема", "тёмная" if CURRENT_THEME == "dark" else "светлая"),
        ]
        for i, (k, v) in enumerate(items):
            box = tk.Frame(stats, bg=PANEL2, padx=16, pady=14)
            box.pack(side="left", expand=True, fill="x", padx=(0 if i == 0 else 10, 0))
            lbl(box, k.upper(), 8, True, MUTED, bg=PANEL2).pack(anchor="w")
            lbl(box, v, 14, True, ACCENT, bg=PANEL2).pack(anchor="w", pady=(4, 0))

        lbl(body, "С уважением, команда LitLauncher", 10, False, DIM,
            bg=PANEL).pack(anchor="w", pady=(24, 0))

    # ---- страница «О лаунчере»
    def page_about(self):
        page = tk.Frame(self.content, bg=BG)
        page.pack(fill="both", expand=True, padx=34, pady=26)
        lbl(page, "О лаунчере", 26, True, FG).pack(anchor="w", pady=(0, 16))
        card = tk.Frame(page, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=PANEL, padx=24, pady=20)
        inner.pack(fill="both", expand=True)
        lbl(inner, ("%s %s" % (APP_NAME, APP_VERSION)).upper(), 10, True, ACCENT, bg=PANEL).pack(anchor="w", pady=(0, 12))
        items = [
            ("G1GC-флаги", "Фиксированная куча (Xms = Xmx) и pre-touch: меньше фризов от сборщика мусора."),
            ("Fabric и моды", "Fabric API, Sodium, Lithium, FerriteCore, ModernFix, EntityCulling, ImmediatelyFast. При смене версии старые моды удаляются и ставятся под новую."),
            ("Система", "Повышенный приоритет процесса на Windows, threaded OpenGL на Linux."),
            ("Java", "Подходящая версия скачивается автоматически."),
            ("Офлайн", "Установленные версии запускаются без интернета."),
            ("Вход", "Офлайн-режим по нику. Серверы с проверкой лицензии не пустят."),
        ]
        for k, v in items:
            r = tk.Frame(inner, bg=PANEL)
            r.pack(fill="x", pady=6)
            lbl(r, k, 11, True, FG, width=16, anchor="w", bg=PANEL).pack(side="left", anchor="n")
            lbl(r, v, 11, False, MUTED, justify="left", wraplength=640, anchor="w", bg=PANEL).pack(side="left")
        row = tk.Frame(page, bg=BG)
        row.pack(anchor="w", pady=18)
        RoundButton(row, "Проверить обновления", lambda: self.check_updates(False), width=210, primary=True).pack(side="left")
        RoundButton(row, "Папка данных", lambda: open_folder(BASE), width=160).pack(side="left", padx=10)
        RoundButton(row, "Папка логов", lambda: open_folder(LOG_DIR), width=150).pack(side="left")
        lbl(page, "Данные: %s" % BASE, 9, False, MUTED, bg=BG).pack(anchor="w")

    # ---- обновление лаунчера
    def check_updates(self, silent=False):
        if not update_enabled():
            if not silent:
                messagebox.showinfo("Обновления", "Проверка обновлений выключена: в LitLauncher.py не задан репозиторий UPDATE_REPO.")
            return

        def work():
            try:
                self.q.put(("update", (check_update(), silent)))
            except Exception as e:  # noqa
                self.q.put(("update_err", (describe_error(e), silent)))
        threading.Thread(target=work, daemon=True).start()

    def show_update_bar(self):
        if not self._alive("head_ref") or not self.update_info:
            return
        if self._alive("upd_bar"):
            self.upd_bar.destroy()
        info = self.update_info
        bar = tk.Frame(self.shell, bg=ACCENT, height=46)
        bar.pack(fill="x", before=self.head_ref)
        bar.pack_propagate(False)
        self.upd_bar = bar
        lbl(bar, "●", 10, True, ACCENT_FG, bg=ACCENT).pack(side="left", padx=(30, 8))
        lbl(bar, "Вышла новая версия лаунчера %s (у тебя %s)" % (info["version"], APP_VERSION), 11, True,
            ACCENT_FG, bg=ACCENT).pack(side="left")
        self.upd_btn = RoundButton(bar, "Обновить", self.do_update, width=130, height=32, size=10, radius=9)
        self.upd_btn.config(bg=ACCENT)
        self.upd_btn.pack(side="right", padx=(8, 30), pady=7)
        what = tk.Label(bar, text="Что нового", bg=ACCENT, fg=ACCENT_FG, font=(FONT, 10, "underline"), cursor="hand2")
        what.pack(side="right")
        what.bind("<Button-1>", lambda e: webbrowser.open(info["page"]))

    def do_update(self):
        info = self.update_info
        if not info or self.update_busy:
            return
        if self.busy:
            messagebox.showinfo("Обновление", "Сначала закрой игру, потом обновляй лаунчер.")
            return
        if not info["url"] or SYSTEM != "Windows":
            webbrowser.open(info["page"])
            return
        self.update_busy = True
        self.upd_btn.set("Загрузка 0%", False)

        def work():
            try:
                path = download_update(info, lambda f: self.q.put(("update_prog", f)))
                self.q.put(("update_ready", path))
            except Exception as e:  # noqa
                self.q.put(("update_fail", describe_error(e)))
        threading.Thread(target=work, daemon=True).start()

    # ---- логика
    def collect_cfg(self):
        c = self.cfg
        c["nickname"] = self.v_nick.get().strip() or "Player"
        accs = [a for a in (c.get("accounts") or []) if _valid_nick(a)]
        nick = c["nickname"]
        if _valid_nick(nick) and nick not in accs:
            accs.insert(0, nick)
        c["accounts"] = accs
        c["version"] = self.v_version.get()
        c["show_snapshots"] = self.v_snap.get()
        c["ram_mb"] = int(self.v_ram.get())
        c["java_path"] = self.v_java.get().strip()
        c["optimize_jvm"] = self.v_opt.get()
        c["fabric_mods"] = self.v_fabric.get()
        c["fullscreen"] = self.v_full.get()
        try:
            c["width"] = max(320, int(self.v_w.get()))
            c["height"] = max(240, int(self.v_h.get()))
        except ValueError:
            c["width"], c["height"] = 1280, 720
        c["extra_jvm"] = self.v_extra.get().strip()
        c["proxy"] = self.v_proxy.get().strip()
        c["theme"] = "dark" if self.v_dark.get() else "light"

    def render_mods(self):
        for w in self.mods_box.winfo_children():
            w.destroy()
        files = load_mod_registry(MC_DIR)["files"]
        if not files:
            lbl(self.mods_box, "Появятся после первого запуска с Fabric.", 10, False, DIM).pack(anchor="w")
            return
        self.mods_box.grid_columnconfigure(0, weight=1, uniform="m")
        self.mods_box.grid_columnconfigure(1, weight=1, uniform="m")
        i = 0
        for slug in OPT_MODS:
            if slug in files:
                r = tk.Frame(self.mods_box, bg=PANEL)
                r.grid(row=i // 2, column=i % 2, sticky="w", pady=2)
                lbl(r, "●", 8, False, GREEN).pack(side="left")
                lbl(r, "  " + MOD_TITLES.get(slug, slug), 10, True, FG).pack(side="left")
                i += 1

    def remove_mods(self):
        n = clean_managed_mods(MC_DIR, None)
        self.cfg["mods_user_removed"] = True
        save_config(self.cfg)
        self.render_mods()
        messagebox.showinfo("Моды", "Удалено модов лаунчера: %d\nАвтоустановка отключена. "
                                     "Вернуть моды — кнопка «Установить недостающие»." % n)

    def install_missing_mods(self):
        if not self.v_fabric.get():
            messagebox.showinfo("Моды", "Сначала включите «Fabric + моды оптимизации».")
            return
        ver = (self.v_version.get() or self.cfg.get("version") or "").strip()
        if not ver:
            messagebox.showinfo("Моды", "Выберите версию Minecraft на вкладке «Играть».")
            return

        def work():
            try:
                install_mods(MC_DIR, ver, lambda m: self.q.put(("log", m)), only_missing=True)
                self.q.put(("mods_done", True))
            except Exception as e:  # noqa
                self.q.put(("mods_done", False))
                self.q.put(("log", "Ошибка установки модов: %s" % describe_error(e)))
        self.cfg["mods_user_removed"] = False
        save_config(self.cfg)
        self.set_status("Проверяю и докачиваю недостающие моды…")
        threading.Thread(target=work, daemon=True).start()

    def _toggle_theme(self):
        name = "dark" if self.v_dark.get() else "light"
        self.cfg["theme"] = name
        save_config(self.cfg)
        apply_theme(name)
        self._style()
        self.build_main()
        self.show_page("settings")

    def save_settings(self):
        old = self.cfg.get("proxy", "")
        self.collect_cfg()
        save_config(self.cfg)
        if self.cfg["proxy"] != old:
            apply_proxy(self.cfg["proxy"])
            self.reload_versions()
        messagebox.showinfo("Настройки", "Сохранено")

    def play(self):
        if self.busy:
            # Идёт запуск — кнопка сейчас работает как «Отменить»
            self.cancel_launch()
            return
        if not _valid_nick(self.v_nick.get().strip()):
            messagebox.showwarning("Ник", "Ник: 3–16 символов, латиница, цифры и _")
            return
        if not self.v_version.get():
            messagebox.showwarning("Версия", "Список версий не загружен. Проверь интернет или VPN и нажми «повторить» над списком.")
            return
        self.collect_cfg()
        save_config(self.cfg)
        CANCEL_EVENT.clear()
        self._game_proc = None
        self.busy = True
        self.set_status("Подготовка…", MUTED)
        self.play_btn.set("ОТМЕНИТЬ", True, primary=False)
        threading.Thread(target=self._worker, args=(dict(self.cfg),), daemon=True).start()

    def cancel_launch(self):
        if not self.busy:
            return
        CANCEL_EVENT.set()
        self.set_status("Отмена запуска…", RED)
        proc = getattr(self, "_game_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self._alive("play_btn"):
            self.play_btn.set("ОТМЕНА…", False, primary=False)

    def _worker(self, cfg):
        log = lambda s: self.q.put(("log", s))
        prog = lambda f, t: self.q.put(("progress", (f, t)))
        err = None
        try:
            cmd, env, mcdir = prepare_launch(cfg, log, prog)
            check_cancel()
            log("Запуск Minecraft…")
            proc, logpath = launch_process(cmd, env, mcdir, cfg.get("optimize_jvm", True))
            self._game_proc = proc
            if CANCEL_EVENT.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise LaunchCancelled()
            self.q.put(("playing", None))
            started = time.time()
            tail_log(logpath, proc, log)
            rc = proc.wait()
            log("Игра завершилась (код %s)" % rc)
            if CANCEL_EVENT.is_set():
                raise LaunchCancelled()
            if rc != 0 and time.time() - started < 40:
                err = "Игра закрылась с ошибкой (код %s). Подробности в логе." % rc
        except LaunchCancelled:
            log("Запуск отменён пользователем")
            self.q.put(("cancelled", None))
        except Exception as e:  # noqa
            log("ОШИБКА: %s" % e)
            err = "Ошибка: %s" % e
        finally:
            self._game_proc = None
            self.q.put(("idle", None))
            if err:
                self.q.put(("error", err))

    def _handle(self, kind, p):
        if kind == "versions":
            self._ver_thread = False
            self.manifest = p
            self.ver_state = "ok"
            self.fill_versions()
            if self.busy is False:
                self.set_status("Готов к запуску", MUTED)
        elif kind == "update":
            info, silent = p
            self.update_info = info
            if info:
                self.show_update_bar()
            elif not silent:
                messagebox.showinfo("Обновления", "У тебя последняя версия лаунчера (%s)." % APP_VERSION)
        elif kind == "update_err":
            if not p[1]:
                messagebox.showwarning("Обновления", "Не удалось проверить обновления: %s" % p[0])
        elif kind == "update_prog":
            if self._alive("upd_btn"):
                self.upd_btn.set("Загрузка %d%%" % int(min(1.0, p) * 100), False)
        elif kind == "update_ready":
            if self._alive("upd_btn"):
                self.upd_btn.set("Запуск Setup…", False)
            try:
                self.collect_cfg()
                save_config(self.cfg)
                run_installer(p)
                self.after(600, self.destroy)
            except Exception as e:  # noqa
                self.update_busy = False
                if self._alive("upd_btn"):
                    self.upd_btn.set("Обновить", True)
                messagebox.showerror("Обновление", "Не удалось открыть установщик: %s\nФайл: %s" % (e, p))
        elif kind == "update_fail":
            self.update_busy = False
            if self._alive("upd_btn"):
                self.upd_btn.set("Повторить", True)
            messagebox.showwarning("Обновление", "Не удалось скачать обновление: %s" % p)
        elif kind == "versions_error":
            self._ver_thread = False
            self.ver_state = "error"
            self.ver_error = p
            self.fill_versions()
            self.log_lines.append("Список версий не получен: %s" % p)
            if not self.busy:
                self.set_status("Не удалось получить список версий: %s. Включи VPN или задай прокси в настройках." % p, RED)
        elif kind == "log":
            self.log_lines.append(p)
            if len(self.log_lines) > 1500:
                del self.log_lines[:300]
            if self._alive("log_box"):
                self.log_box.config(state="normal")
                self.log_box.insert("end", p + "\n")
                self.log_box.see("end")
                self.log_box.config(state="disabled")
        elif kind == "progress":
            if self._alive("bar"):
                self.bar.set(p[0])
            self.set_status(p[1], MUTED)
        elif kind == "playing":
            if self._alive("play_btn"):
                self.play_btn.set("ИГРА ЗАПУЩЕНА", False)
            if self._alive("bar"):
                self.bar.set(0)
            self.set_status("Игра запущена", GREEN)
            # Ск  ываем окно лаунчера, пока игра работает
            try:
                self.withdraw()
            except Exception:
                pass
        elif kind == "cancelled":
            self.set_status("Запуск отменён", MUTED)
            if self._alive("log_toggle"):
                self.toggle_log(True)
        elif kind == "idle":
            self.busy = False
            if self._alive("play_btn"):
                self.play_btn.set("▶  ИГРАТЬ", True, primary=True)
            if self._alive("bar"):
                self.bar.set(0)
            self.set_status("Готов к запуску", MUTED)
            # Возвращаем окно лаунчера после выхода из игры
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except Exception:
                pass
        elif kind == "mods_done":
            if self._alive("mods_box"):
                self.render_mods()
            if p:
                self.set_status("Моды проверены — недостающие установлены", GREEN)
            else:
                self.set_status("Не удалось установить часть модов, смотри лог", RED)
        elif kind == "error":
            self.set_status(p, RED)
            self.toggle_log(True)

    def poll(self):
        for _ in range(300):
            try:
                kind, p = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(kind, p)
            except Exception:
                self.log_lines.append("Внутренняя ошибка: %s" % traceback.format_exc().splitlines()[-1])
        self.after(60, self.poll)


def main():
    if tk is None:
        print("Нужен tkinter. Linux: sudo apt install python3-tk")
        sys.exit(1)
    apply_proxy(load_config().get("proxy"))
    App().mainloop()


if __name__ == "__main__":
    main()
