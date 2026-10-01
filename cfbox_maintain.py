# -*- coding: utf-8 -*-
"""
CFBox 一键维护脚本
=================
作用：速度变慢时，自动优选一批新的好节点，并更新部署配置。

流程：
  1. 从优选源拉取候选 IP 列表（bestcf 等）
  2. 并发 TCP 连通性 + 延迟筛选
  3. 用 xray 真实 VLESS+WS 隧道逐节点测速
  4. 选出最快的 TOP_N 节点
  5. 自动更新 qinyu（CFBox订阅）的 KV 配置
  6. 生成 cfbb（自己用）的粘贴内容并打开管理页，手动保存
  7. 输出报告

用法：
  python cfbox_maintain.py            # 正常执行（会更新线上配置）
  python cfbox_maintain.py --dry-run  # 只测速不出节点，不更新线上配置
  python cfbox_maintain.py --min-kbps 15000   # 自定义速度门槛（默认 10000 = 10Mbps）

依赖：python、curl.exe（Windows 自带）、xray.exe（已随脚本目录提供）
"""

import argparse
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
XRAY = os.path.join(BASE, "xray", "xray.exe")
CFG_TPL = os.path.join(BASE, "xray_test.json")
REPORT = os.path.join(BASE, "maintain_report.json")
CFBB_PASTE = os.path.join(BASE, "cfbb_nodes_to_paste.txt")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ==================== 配置区（按需修改） ====================

# 优选源列表（每行一个 IP:port 或 IP）
FETCH_SOURCES = [
    "https://bestcf.pages.dev/random-region/mix2.txt",    # 混合 VPS 池（约500个，含多端口）
    "https://bestcf.pages.dev/random-region/JP/100.txt",  # 日本随机池
    "https://bestcf.pages.dev/tiancheng3/jp.txt",         # 天诚优选 JP 专项
    "https://bestcf.pages.dev/gslege/JP.txt",             # Gslege 优选 JP 专项
]

# 保底节点（当前正在使用、实测过的好节点）：
# 每次维护时先测它们是否还活着，活着就保留，避免优选失败导致配置变差
BASELINE_NODES = [
    "92.113.124.38:443",
    "96.126.191.218:443",
    "45.32.250.130:443",
    "151.241.129.194:443",
    "45.8.173.250:443",
    "64.83.39.176:443",
    "137.220.225.56:443",
    "45.192.243.176:443",
]

# 最终下发节点数
TOP_N = 8
# 单连接测速最低门槛（kbps），低于此值不入选
MIN_KBPS = 10000
# TCP 筛选延迟上限（毫秒）
MAX_MS = 160
# TCP 并发数与超时
TCP_CONCURRENCY = 120
TCP_TIMEOUT = 4.0
# 测速并发数（同时起几个 xray）
SPEED_CONCURRENCY = 4
SPEED_BYTES = 20000000   # 20MB
SPEED_TIMEOUT = 25
SPEED_CANDIDATES = 20    # 测速候选数（TCP 筛选后按延迟取前 N）

# qinyu（CFBox订阅）——自动更新
QINYU = {
    "name": "qinyu（CFBox订阅）",
    "api_base": "https://qinyu.dongkunjian.ccwu.cc/81149ba1-7c54-4a79-b067-c74e273c01dc",
    "uuid": "81149ba1-7c54-4a79-b067-c74e273c01dc",
    "sni_host": "qinyu.dongkunjian.ccwu.cc",
    "ws_path": "/?ed=2048",
}

# cfbb（自己用）——API 不可靠，走"生成粘贴文本 + 打开管理页"手动保存
CFBB = {
    "name": "cfbb（自己用）",
    "admin_url": "https://cfbb.dongkunjian.ccwu.cc/admin",
    "uuid": "273e3ba1-ef63-47c1-88db-c368bf1e8736",
    "sni_host": "cfbb.dongkunjian.ccwu.cc",
}

# ==================== 工具函数 ====================

# 日志钩子：网页版会把日志实时推给浏览器（默认为 None 时直接打印）
LOG_HOOK = None


def log(msg):
    if LOG_HOOK is not None:
        LOG_HOOK(msg)
    else:
        print(msg, flush=True)


def run(cmd, timeout=30):
    """执行命令并返回 (returncode, stdout)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def fetch_lists():
    """从优选源拉取候选 IP 列表"""
    ips = {}
    ua = ["-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0"]
    for url in FETCH_SOURCES:
        log(f"拉取优选源: {url}")
        rc, out = run(["curl.exe", "-k", "-s", "--max-time", "20", "-H", "User-Agent: Mozilla/5.0", url])
        if rc != 0 or not out.strip():
            log(f"  ✗ 失败: {rc}")
            continue
        count = 0
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            host = line.split("#", 1)[0].strip()
            # 解析 IP[:port]（兼容 IPv6 [ip]:port）
            if host.startswith("[") and "]" in host:
                ip = host[1:host.index("]")]
                rest = host[host.index("]") + 1:]
                port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else 443
            elif ":" in host:
                ip, ps = host.rsplit(":", 1)
                port = int(ps) if ps.isdigit() else 443
            else:
                ip, port = host, 443
            if port > 65535:
                continue
            ips[f"{ip}:{port}"] = (ip, port)
            count += 1
        log(f"  ✓ 解析到 {count} 个 IP")
    return list(ips.values())


def tcp_probe(ip_port):
    """TCP 连通性 + 延迟测试，返回 (ms) 或 None"""
    ip, port = ip_port
    t0 = time.time()
    try:
        with socket.create_connection((ip, port), timeout=TCP_TIMEOUT):
            return (time.time() - t0) * 1000
    except Exception:
        return None


def tcp_filter(candidates):
    """并发 TCP 筛选，返回 [(ip, port, ms)] 存活列表"""
    log(f"并发 TCP 筛选 {len(candidates)} 个候选...")
    results = []
    lock = threading.Lock()
    index = [0]
    done = [0]

    def worker():
        while True:
            with lock:
                if index[0] >= len(candidates):
                    return
                i = index[0]
                index[0] += 1
            ms = tcp_probe(candidates[i])
            with lock:
                done[0] += 1
                if done[0] % 50 == 0 or done[0] == len(candidates):
                    log(f"  进度 {done[0]}/{len(candidates)}")
            if ms is not None and ms <= MAX_MS:
                with lock:
                    results.append((candidates[i][0], candidates[i][1], ms))

    threads = [threading.Thread(target=worker) for _ in range(min(TCP_CONCURRENCY, len(candidates)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results.sort(key=lambda x: x[2])
    log(f"存活 {len(results)} 个（延迟 {MAX_MS}ms 内）")
    return results


def xray_speed(item, port_off, timeout=SPEED_TIMEOUT):
    """用 xray 起隧道测单个节点速度，返回 kbps"""
    ip, port, _ms = item
    cfg = (io.open(CFG_TPL, "r", encoding="utf-8").read()
           .replace("SERVER_IP", ip)
           .replace("SERVER_PORT", str(port))
           .replace("10809", str(10809 + port_off)))
    cfg_path = os.path.join(BASE, f"xr_m_{port_off}.json")
    with io.open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg)
    proc = subprocess.Popen([XRAY, "run", "-c", cfg_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.6)
        rc, out = run(["curl.exe", "-k", "-s", "-o", "NUL", "--max-time", str(timeout),
                       "-x", f"socks5h://127.0.0.1:{10809 + port_off}", "-w", "%{speed_download}",
                       f"https://speed.cloudflare.com/__down?bytes={SPEED_BYTES}"], timeout=timeout + 10)
        try:
            bps = float(out.strip())
            return bps * 8 / 1000
        except Exception:
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def speed_test(candidates, min_kbps):
    """并发测速，返回 [(ip, port, ms, kbps)] 按速度排序"""
    picks = candidates[:SPEED_CANDIDATES]
    log(f"xray 隧道测速 {len(picks)} 个候选（并发 {SPEED_CONCURRENCY}）...")
    results = []
    lock = threading.Lock()
    index = [0]
    done = [0]

    def worker():
        while True:
            with lock:
                if index[0] >= len(picks):
                    return
                i = index[0]
                index[0] += 1
            item = picks[i]
            kbps = xray_speed(item, i % 8)
            with lock:
                done[0] += 1
                results.append((item[0], item[1], item[2], kbps))
                log(f"  [{done[0]}/{len(picks)}] {item[0]}:{item[1]} -> {round(kbps)} kbps")

    threads = [threading.Thread(target=worker) for _ in range(SPEED_CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results.sort(key=lambda x: -x[3])
    good = [r for r in results if r[3] >= min_kbps]
    log(f"达标（>= {min_kbps} kbps）{len(good)} 个")
    return good


def deploy_nodes_text(nodes):
    """节点列表 -> 各种配置格式文本"""
    yx = ",".join(f"{ip}:{port}#日本-{ip}" for ip, port, _ms, _kbps in nodes)
    multi = "\n".join(f"{ip}:{port}#日本-{ip}" for ip, port, _ms, _kbps in nodes)
    return yx, multi


def get_config(base):
    """读取部署当前 KV 配置"""
    rc, out = run(["curl.exe", "-k", "-s", "--max-time", "20", base + "/api/config"])
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
        return data.get("config", data) if isinstance(data, dict) else None
    except Exception:
        return None


def update_qinyu(nodes):
    """更新 qinyu 的 KV：yx + subCustomIPs"""
    cfg = get_config(QINYU["api_base"])
    if cfg is None:
        log("✗ 读取 qinyu 当前配置失败，跳过自动更新")
        return False
    yx, multi = deploy_nodes_text(nodes)
    cfg["yx"] = yx
    cfg["subCustomIPs"] = multi
    payload = json.dumps(cfg, ensure_ascii=False)
    tmp = os.path.join(BASE, "qinyu_payload.json")
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    rc, out = run(["curl.exe", "-k", "-s", "--max-time", "30",
                   "-X", "POST", "-H", "Content-Type: application/json",
                   "--data-binary", f"@{tmp}",
                   QINYU["api_base"] + "/api/config"], timeout=40)
    ok = ("success" in out and "true" in out)
    log(f"{'✓' if ok else '✗'} qinyu 配置保存: {out[:120] if not ok else 'success'}")
    return ok


def verify_qinyu_sub(nodes):
    """拉取 qinyu 订阅，验证好节点是否下发"""
    rc, out = run(["curl.exe", "-k", "-s", "--max-time", "25", QINYU["api_base"] + "/sub"])
    if rc != 0 or not out.strip():
        log("✗ 订阅拉取失败，无法验证")
        return False
    import base64
    text = out
    # 若整块是 base64，解码后检查
    try:
        dec = base64.b64decode(text.strip()).decode("utf-8")
        if "vless" in dec or "trojan" in dec:
            text = dec
    except Exception:
        pass
    first = nodes[0][0]
    found = first in text
    log(f"{'✓' if found else '✗'} 订阅验证：{first} 是否已下发 -> {'是' if found else '否'}")
    return found


def gen_cfbb_paste(nodes):
    """生成 cfbb 手动粘贴文本并打开管理页"""
    _, multi = deploy_nodes_text(nodes)
    with io.open(CFBB_PASTE, "w", encoding="utf-8") as f:
        f.write(multi + "\n")
    log(f"✓ cfbb 粘贴内容已生成: {CFBB_PASTE}")
    log("  下一步（手动，约10秒）：")
    log("    1. 打开 " + CFBB["admin_url"])
    log("    2. 进入「⚡️ 优选订阅生成」→「自定义优选」输入框")
    log("    3. 清空后粘贴 cfbb_nodes_to_paste.txt 的内容，点「保存」")
    log("    4. 回到 Karing，更新「自己用」订阅即可")
    # 尝试打开管理页（仅 Windows）
    try:
        subprocess.Popen(["cmd", "/c", "start", "", CFBB["admin_url"]])
    except Exception:
        pass


def run_maintenance(dry_run, min_kbps=MIN_KBPS):
    """核心维护流程：优选 + 测速 + 保底 + 更新。

    返回 report 字典（网页版直接使用）：
      mode / min_kbps / baseline_alive / nodes[{ip,port,ms,kbps,source}]
      top_nodes: [(ip, port, ms, kbps, source), ...]
      nodes_text: {yx, multi} 下发文本
    """
    log("=" * 60)
    log("CFBox 一键维护")
    log("模式: " + ("预演（不更新）" if dry_run else "正式（会更新 qinyu 配置）"))
    log("=" * 60)

    # 0. 保底节点：先测现有好节点是否还活着
    baseline = []
    for s in BASELINE_NODES:
        m = re.match(r"^(\S+):(\d+)$", s.strip())
        if m:
            ip, port = m.group(1), int(m.group(2))
            ms = tcp_probe((ip, port))
            if ms is not None and ms <= MAX_MS:
                baseline.append((ip, port, ms))
    log(f"保底节点：{len(baseline)}/{len(BASELINE_NODES)} 个仍存活"
        + ("" if baseline else "（全部失联，将完全依赖新优选）"))

    # 1. 拉取候选
    candidates = fetch_lists()
    if not candidates and not baseline:
        log("✗ 没有拉到任何候选 IP，保底节点也全部失联，请检查网络或优选源")
        return None
    log(f"共 {len(candidates)} 个候选 IP")

    # 2. TCP 筛选
    alive = tcp_filter(candidates)
    if not alive and not baseline:
        log("✗ TCP 筛选后没有存活节点，保底节点也失联，无法继续")
        return None

    # 3. 真实测速（有候选才测）
    good = []
    if alive:
        good = speed_test(alive, min_kbps)
    # 保底节点直接视为可用（已在用中）
    baseline_full = [(ip, port, ms, min_kbps) for ip, port, ms in baseline]
    all_ok = baseline_full + good
    if not all_ok:
        log("✗ 没有可用节点（优选无达标、保底全部失联）")
        return None
    all_ok.sort(key=lambda x: -x[3])
    top = all_ok[:TOP_N]

    # 4. 报告
    log("\n===== 优选结果（TOP） =====")
    baseline_keys = {(b[0], b[1]) for b in baseline}
    top_nodes = []
    for i, (ip, port, ms, kbps) in enumerate(top, 1):
        src = "baseline" if (ip, port) in baseline_keys else "new"
        src_cn = "保底" if src == "baseline" else "新优选"
        log(f"  {i}. [{src_cn}] {ip}:{port}  延迟{round(ms)}ms  单连接{round(kbps)} kbps ({round(kbps/1000,1)} Mbps)")
        top_nodes.append((ip, port, round(ms), round(kbps), src))

    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "dry-run" if dry_run else "apply",
        "min_kbps": min_kbps,
        "baseline_alive": [f"{ip}:{port}" for ip, port, _ms in baseline],
        "nodes": [{"ip": ip, "port": port, "ms": ms, "kbps": kbps, "source": src}
                  for ip, port, ms, kbps, src in top_nodes],
        "top_nodes": top_nodes,
        "nodes_text": {"yx": None, "multi": None},
    }
    yx, multi = deploy_nodes_text(top)
    report["nodes_text"] = {"yx": yx, "multi": multi}

    with io.open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log(f"报告已保存: {REPORT}")

    # 5. 更新部署
    if dry_run:
        log("\n[预演模式] 不更新任何线上配置。")
        log("若正式运行将写入:")
        log("  yx = " + yx[:200] + "...")
        return report

    log("\n----- 更新部署 -----")
    ok_q = update_qinyu(top)
    if ok_q:
        time.sleep(2)
        verify_qinyu_sub(top)
    gen_cfbb_paste(top)
    log("\n完成！到 Karing 里更新「自己用」订阅即可生效。")
    return report


def main():
    parser = argparse.ArgumentParser(description="CFBox 一键维护")
    parser.add_argument("--dry-run", action="store_true", help="只测速，不更新线上配置")
    parser.add_argument("--min-kbps", type=int, default=MIN_KBPS, help="单连接速度门槛")
    args = parser.parse_args()
    run_maintenance(args.dry_run, args.min_kbps)


if __name__ == "__main__":
    main()
