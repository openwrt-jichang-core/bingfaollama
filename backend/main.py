import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scanner import ScanState, start_scan_thread

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
HOSTS_FILE = DATA_DIR / "hosts.json"
RESULTS_FILE = DATA_DIR / "scan_results.json"

app = FastAPI(title="Ollama Cluster Scanner")
state = ScanState()


def load_hosts():
    """
    主机记录格式: {"url": str, "enabled": bool, "favorite": bool}
    自动兼容旧版本(纯字符串列表)数据, 迁移为新格式。
    """
    if not HOSTS_FILE.exists():
        return []
    try:
        raw = json.loads(HOSTS_FILE.read_text())
    except Exception:
        return []

    migrated = False
    hosts = []
    for item in raw:
        if isinstance(item, str):
            hosts.append({"url": item, "enabled": True, "favorite": False})
            migrated = True
        else:
            hosts.append({
                "url": item.get("url"),
                "enabled": item.get("enabled", True),
                "favorite": item.get("favorite", False),
            })
    if migrated:
        save_hosts(hosts)
    return hosts


def save_hosts(hosts):
    # 收藏的排在前面, 其余保持原有相对顺序
    ordered = sorted(hosts, key=lambda h: not h.get("favorite", False))
    HOSTS_FILE.write_text(json.dumps(ordered, indent=2, ensure_ascii=False))


class HostIn(BaseModel):
    url: str


class HostPatch(BaseModel):
    url: str
    enabled: bool | None = None
    favorite: bool | None = None


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise HTTPException(status_code=400, detail="地址不能为空")
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    return url


def find_host(hosts, url):
    for h in hosts:
        if h["url"] == url:
            return h
    return None


@app.get("/api/hosts")
def get_hosts():
    return load_hosts()


@app.post("/api/hosts")
def add_host(host: HostIn):
    url = normalize_url(host.url)
    hosts = load_hosts()
    if find_host(hosts, url):
        raise HTTPException(status_code=400, detail="该地址已存在")
    hosts.append({"url": url, "enabled": True, "favorite": False})
    save_hosts(hosts)
    return load_hosts()


@app.patch("/api/hosts")
def patch_host(patch: HostPatch):
    url = normalize_url(patch.url)
    hosts = load_hosts()
    h = find_host(hosts, url)
    if not h:
        raise HTTPException(status_code=404, detail="未找到该地址")
    if patch.enabled is not None:
        h["enabled"] = patch.enabled
    if patch.favorite is not None:
        h["favorite"] = patch.favorite
    save_hosts(hosts)
    return load_hosts()


@app.delete("/api/hosts")
def delete_host(host: HostIn):
    url = normalize_url(host.url)
    hosts = load_hosts()
    h = find_host(hosts, url)
    if not h:
        raise HTTPException(status_code=404, detail="未找到该地址")
    hosts.remove(h)
    save_hosts(hosts)
    return load_hosts()


class ScanStartIn(BaseModel):
    concurrency: int = 3


@app.post("/api/scan/start")
def scan_start(body: ScanStartIn = ScanStartIn()):
    hosts = [h["url"] for h in load_hosts() if h.get("enabled", True)]
    if not hosts:
        raise HTTPException(status_code=400, detail="请先添加并启用至少一个主机地址")
    if state.running:
        raise HTTPException(status_code=409, detail="扫描已在进行中")
    concurrency = max(1, min(100, body.concurrency))
    ok = start_scan_thread(hosts, state, concurrency=concurrency)
    if not ok:
        raise HTTPException(status_code=409, detail="扫描已在进行中")
    return {"status": "started", "hosts": hosts, "concurrency": concurrency}


@app.post("/api/scan/stop")
def scan_stop():
    if not state.running:
        return {"status": "not_running"}
    state.request_stop()
    return {"status": "stopping"}


@app.get("/api/scan/status")
def scan_status(since: int = 0):
    logs = state.get_logs_since(since)
    results = state.results
    if results is not None:
        try:
            RESULTS_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        except Exception:
            pass
    return JSONResponse({
        "running": state.running,
        "logs": logs,
        "results": results,
    })


@app.get("/api/scan/results")
def scan_results():
    if RESULTS_FILE.exists():
        return JSONResponse(json.loads(RESULTS_FILE.read_text()))
    return JSONResponse({})


static_dir = Path(__file__).parent.parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
