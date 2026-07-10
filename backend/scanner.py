"""
Ollama 集群扫描核心逻辑
改造自原始脚本: 支持动态主机列表 / 可中途停止 / 日志实时推送
"""
import requests
import re
import subprocess
import time
import json
import threading
from collections import defaultdict
from datetime import datetime

TIMEOUT = 120
QUICK_TEST_TIMEOUT = 20
CODE_RUN_TIMEOUT = 15

ADVANCED_TEST_CASES = [
    {
        "name": "Path Traversal Protection",
        "prompt": "write a Python function safe_read_file(base_dir, user_path) for a file manager panel. User passes relative path user_path, function should only allow reading files inside base_dir. If user_path tries to escape base_dir via ../, must raise PermissionError. For legal paths return file content as string. Return code only, no explanation, only import os.",
        "harness": """
import tempfile, os, shutil

tmpdir = tempfile.mkdtemp()
base = os.path.join(tmpdir, 'base')
os.makedirs(base)
outside = os.path.join(tmpdir, 'outside')
os.makedirs(outside)

with open(os.path.join(base, 'note.txt'), 'w') as f:
    f.write('hello inside')
with open(os.path.join(outside, 'secret.txt'), 'w') as f:
    f.write('TOP SECRET')

result1 = safe_read_file(base, 'note.txt')
assert result1 == 'hello inside', f"normal read failed: {result1!r}"

blocked = False
try:
    safe_read_file(base, '../outside/secret.txt')
except PermissionError:
    blocked = True
assert blocked, "SECURITY_FAIL: relative traversal NOT blocked!"

blocked2 = False
try:
    safe_read_file(base, os.path.join(outside, 'secret.txt'))
except PermissionError:
    blocked2 = True
assert blocked2, "SECURITY_FAIL: absolute traversal NOT blocked!"

shutil.rmtree(tmpdir)
print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
    {
        "name": "SiteManager Multi-method",
        "prompt": "Write Python class SiteManager for website panel. Methods: add_site(name,port)-ValueError if name/port exists. remove_site(name)-KeyError if not found. list_sites()-sorted list [(name,port),...]. get_port(name)-return port or KeyError. Return code only.",
        "harness": """
sm = SiteManager()
sm.add_site('blog', 8080)
sm.add_site('shop', 8081)
sm.add_site('api', 8082)

assert sm.list_sites() == [('api', 8082), ('blog', 8080), ('shop', 8081)]

try:
    sm.add_site('blog', 9999)
    assert False, "dup name not caught"
except ValueError:
    pass

try:
    sm.add_site('new', 8080)
    assert False, "dup port not caught"
except ValueError:
    pass

assert sm.get_port('shop') == 8081

sm.remove_site('api')
assert sm.list_sites() == [('blog', 8080), ('shop', 8081)]

try:
    sm.remove_site('notexist')
    assert False
except KeyError:
    pass

sm.add_site('api2', 8082)
assert sm.get_port('api2') == 8082
print("ALL_PASS")
""",
        "expected": "ALL_PASS",
    },
]


class ScanState:
    """扫描运行状态, 供 API 层轮询读取"""

    def __init__(self):
        self.lock = threading.Lock()
        self.results_lock = threading.Lock()
        self.running = False
        self.logs = []          # [{seq, ts, text}]
        self.results = None     # 最终 JSON 结果
        self.stop_event = threading.Event()
        self._seq = 0
        self.thread = None
        self.concurrency = 3
        self.active_hosts = set()   # 正在运行中的主机, 防止同一主机被重复并发运行
        self.active_hosts_lock = threading.Lock()

    def log(self, text):
        with self.lock:
            self._seq += 1
            self.logs.append({
                "seq": self._seq,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "text": text,
            })

    def get_logs_since(self, since):
        with self.lock:
            return [l for l in self.logs if l["seq"] > since]

    def reset(self):
        with self.lock:
            self.logs = []
            self.results = None
            self._seq = 0
        with self.active_hosts_lock:
            self.active_hosts = set()
        self.stop_event.clear()

    def request_stop(self):
        self.stop_event.set()

    def is_stopping(self):
        return self.stop_event.is_set()

    def try_acquire_host(self, host):
        """尝试独占运行某个主机, 已在运行中则返回 False (不允许重复并发)"""
        with self.active_hosts_lock:
            if host in self.active_hosts:
                return False
            self.active_hosts.add(host)
            return True

    def release_host(self, host):
        with self.active_hosts_lock:
            self.active_hosts.discard(host)


def discover_models(host):
    try:
        resp = requests.get(f"{host}/api/tags", timeout=10)
        data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
        return models, None
    except Exception as e:
        return [], str(e)


def quick_test(host, model):
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": "say ok", "stream": False},
            timeout=QUICK_TEST_TIMEOUT,
        )
        data = resp.json()
        if "error" in data:
            return False, data["error"]
        return True, None
    except Exception as e:
        return False, str(e)


def extract_code(text):
    patterns = [r"```python\s*(.*?)```", r"```\s*(.*?)```"]
    for pat in patterns:
        matches = re.findall(pat, text, re.DOTALL)
        if matches:
            return matches[0].strip()
    return text.strip()


def run_code(code, harness):
    full_code = code + "\n\n" + harness
    try:
        result = subprocess.run(
            ["python3", "-c", full_code],
            capture_output=True,
            text=True,
            timeout=CODE_RUN_TIMEOUT,
        )
        if result.returncode != 0:
            return None, (result.stdout + result.stderr).strip()[-400:]
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, "execution timeout"
    except Exception as e:
        return None, str(e)


def query_model(host, model, prompt):
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if "error" in data:
            return None, data["error"]
        return data.get("response", ""), None
    except Exception as e:
        return None, str(e)


def process_host(host, state: ScanState):
    """
    单个主机的完整流水线: 发现模型 -> 快速可用性测试 -> 高级测试
    在线程池的一个 worker 中运行, 日志加主机前缀以便在并发输出中区分
    """
    tag = f"[{host}]"
    host_result = {"models": [], "viability": {}, "advanced": {}}

    if state.is_stopping():
        return host_result

    state.log(f"{tag} 开始扫描 ...")
    models, err = discover_models(host)
    if err:
        state.log(f"{tag} 发现模型失败: {err}")
        return host_result
    if not models:
        state.log(f"{tag} 未发现模型")
        return host_result

    state.log(f"{tag} 发现 {len(models)} 个模型: {', '.join(models)}")
    host_result["models"] = models

    viable = {}
    for model in models:
        if state.is_stopping():
            state.log(f"{tag} 收到停止信号, 中断可用性测试")
            host_result["viability"] = viable
            return host_result
        ok, err = quick_test(host, model)
        viable[model] = ok
        state.log(f"{tag} {model}: {'可用' if ok else '不可用 - ' + str(err)[:80]}")
    host_result["viability"] = viable

    viable_models = [m for m, ok in viable.items() if ok]
    if not viable_models:
        state.log(f"{tag} 没有可用模型, 跳过高级测试")
        return host_result

    for model in viable_models:
        if state.is_stopping():
            state.log(f"{tag} 收到停止信号, 中断高级测试")
            break
        state.log(f"{tag} {model}: 开始高级测试")
        results = run_advanced_tests(host, model, state, tag=tag)
        host_result["advanced"][model] = results

    state.log(f"{tag} 扫描完成")
    return host_result


def run_advanced_tests(host, model, state, tag=""):
    results = []
    for case in ADVANCED_TEST_CASES:
        if state.is_stopping():
            state.log(f"{tag} 已收到停止信号, 中断高级测试")
            break
        start = time.time()
        response, err = query_model(host, model, case["prompt"])
        elapsed = time.time() - start

        if err:
            results.append((case["name"], "REQUEST_FAIL", err, elapsed))
            state.log(f"{tag}   [{case['name']}] REQUEST_FAIL ({elapsed:.1f}s) {err[:100]}")
            continue

        code = extract_code(response)
        output, run_err = run_code(code, case["harness"])

        if run_err:
            results.append((case["name"], "CODE_ERROR", run_err, elapsed))
            state.log(f"{tag}   [{case['name']}] CODE_ERROR ({elapsed:.1f}s) {run_err[:100]}")
            continue

        if output == case["expected"] or "ALL_PASS" in output:
            results.append((case["name"], "PASS", None, elapsed))
            state.log(f"{tag}   [{case['name']}] PASS ({elapsed:.1f}s)")
        else:
            results.append((case["name"], "WRONG_OUTPUT", output, elapsed))
            state.log(f"{tag}   [{case['name']}] WRONG_OUTPUT ({elapsed:.1f}s) {str(output)[:100]}")

    return results


def run_scan(hosts, state: ScanState, concurrency=3):
    """
    主扫描流程, 在后台线程中执行。
    使用线程池并发处理多个主机, 并发数由 concurrency (1-100) 控制。
    同一个主机不会被重复并发运行 (try_acquire_host 保证独占)。
    """
    import concurrent.futures

    state.reset()
    state.running = True
    state.concurrency = concurrency
    try:
        state.log("=" * 60)
        state.log(f"开始扫描 {len(hosts)} 个主机, 并发数: {concurrency}")
        state.log("=" * 60)

        all_results = {}  # host -> host_result

        def worker(host):
            if not state.try_acquire_host(host):
                state.log(f"[{host}] 已在运行中, 跳过重复任务")
                return host, None
            try:
                return host, process_host(host, state)
            finally:
                state.release_host(host)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(100, concurrency))) as pool:
            futures = {pool.submit(worker, host): host for host in hosts}
            for future in concurrent.futures.as_completed(futures):
                host, result = future.result()
                if result is not None:
                    with state.results_lock:
                        all_results[host] = result

        state.log("")
        state.log("=" * 60)
        state.log("最终结果")
        state.log("=" * 60)

        discovered = {h: r["models"] for h, r in all_results.items() if r.get("models")}
        viability = {}
        advanced = {}
        for h, r in all_results.items():
            for m, ok in r.get("viability", {}).items():
                viability[f"{h}|{m}"] = ok
            for m, tests in r.get("advanced", {}).items():
                advanced[f"{h}|{m}"] = tests

        passed_models = []
        failed_models = []
        for key, tests in advanced.items():
            host, model = key.split("|", 1)
            passed_tests = sum(1 for _, status, _, _ in tests if status == "PASS")
            total_tests = len(tests)
            model_key = f"{model} @ {host}"
            if total_tests > 0 and passed_tests == total_tests:
                passed_models.append(model_key)
            else:
                failed_models.append(model_key)

        if passed_models:
            state.log("全部通过的模型:")
            for m in passed_models:
                state.log(f"  ✔ {m}")
        if failed_models:
            state.log("存在失败项的模型:")
            for m in failed_models:
                state.log(f"  ✘ {m}")
        if not passed_models and not failed_models:
            state.log("没有模型进入高级测试阶段")

        state.results = {
            "discovered": discovered,
            "viability": viability,
            "advanced": {
                key: [
                    {"test": name, "status": status, "detail": (str(detail)[:300] if detail else None), "elapsed": elapsed}
                    for name, status, detail, elapsed in tests
                ]
                for key, tests in advanced.items()
            },
            "generated_at": datetime.now().isoformat(),
        }
        state.log("\n扫描完成" if not state.is_stopping() else "\n扫描已停止")
    except Exception as e:
        state.log(f"扫描过程中发生异常: {e}")
    finally:
        state.running = False


def start_scan_thread(hosts, state: ScanState, concurrency=3):
    if state.running:
        return False
    t = threading.Thread(target=run_scan, args=(hosts, state, concurrency), daemon=True)
    state.thread = t
    t.start()
    return True
