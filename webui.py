# -*- coding: utf-8 -*-
"""
CFBox 维护网页版 - 本地服务
============================
双击 网页版启动.bat 即可：自动打开浏览器，界面操作代替命令行。

流程（网页上点按钮）：
  1. 「开始优选」  -> 后台跑 拉源/筛选/测速/保底（预演，不改线上）
  2. 查看结果表格 -> 满意后
  3. 「更新部署」  -> 自动写 qinyu 配置 + 生成订阅文件（nodes_sub.txt）+ cfbb 粘贴内容
  4. 「一键上传 GitHub」-> 把订阅文件上传到 GitHub，cfbb 汇聚订阅自动生效
  5. Karing 更新「自己用」订阅 -> 连接新节点

地址: http://127.0.0.1:8899   （仅本机访问，端口可改 PORT）
"""

import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cfbox_maintain as cm

PORT = 8899
SETTINGS_FILE = os.path.join(BASE, "gh_settings.json")

DEFAULT_SETTINGS = {
    "gh_token": "",                 # GitHub Personal Access Token（repo 权限）
    "gh_repo": cm.GH["repo"],       # 仓库 owner/name
    "gh_file": cm.GH["file"],       # 订阅文件名
    "gh_branch": cm.GH["branch"],   # 分支
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with io.open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                d.setdefault(k, v)
            return d
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(d):
    with io.open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ==================== 任务状态 ====================

_lock = threading.Lock()
STATE = {
    "running": False,
    "logs": [],
    "result": None,     # run_maintenance 的 report 字典
    "last_error": None,
}


def _append_log(msg):
    with _lock:
        STATE["logs"].append(str(msg))
        if len(STATE["logs"]) > 2000:      # 防止日志无限增长
            STATE["logs"] = STATE["logs"][-1000:]


def start_optimize(min_kbps):
    """后台启动优选流程（预演模式，不写线上）"""
    with _lock:
        if STATE["running"]:
            return False
        STATE["running"] = True
        STATE["logs"] = []
        STATE["result"] = None
        STATE["last_error"] = None
    cm.LOG_HOOK = _append_log

    def worker():
        try:
            report = cm.run_maintenance(dry_run=True, min_kbps=min_kbps)
            with _lock:
                STATE["result"] = report
        except Exception as e:
            _append_log(f"✗ 任务异常: {repr(e)}")
            with _lock:
                STATE["last_error"] = repr(e)
        finally:
            with _lock:
                STATE["running"] = False
            cm.LOG_HOOK = None

    threading.Thread(target=worker, daemon=True).start()
    return True


def apply_deploy():
    """用最近一次优选结果更新部署（qinyu 自动 + 生成订阅文件 + cfbb 粘贴）"""
    with _lock:
        if STATE["running"]:
            return {"ok": False, "error": "任务仍在运行，请稍候"}
        report = STATE["result"]
    if not report or not report.get("top_nodes"):
        return {"ok": False, "error": "还没有优选结果，请先点「开始优选」"}

    cm.LOG_HOOK = _append_log
    try:
        nodes = [(ip, port, ms, kbps) for ip, port, ms, kbps, _src in report["top_nodes"]]
        _append_log("----- 更新部署 -----")
        ok_q = cm.update_qinyu(nodes)
        if ok_q:
            time.sleep(2)
            cm.verify_qinyu_sub(nodes)
        # 生成订阅文件（明文+base64，供 GitHub 一键上传 / cfbb 汇聚订阅）
        cm.gen_gh_sub(nodes)
        cm.gen_cfbb_paste(nodes)
        _append_log("完成！接下来：Karing 更新「自己用」订阅即可生效；或点「一键上传 GitHub」走汇聚订阅闭环。")
        return {"ok": True, "qinyu_ok": ok_q}
    except Exception as e:
        _append_log(f"✗ 更新失败: {repr(e)}")
        return {"ok": False, "error": repr(e)}
    finally:
        cm.LOG_HOOK = None


def upload_gh():
    """一键：用最近一次优选结果生成订阅并上传 GitHub"""
    with _lock:
        if STATE["running"]:
            return {"ok": False, "error": "任务仍在运行，请稍候"}
        report = STATE["result"]
    st = load_settings()
    token = st.get("gh_token") or ""
    if not token:
        return {"ok": False, "error": "请先在「GitHub 设置」里填写 Personal Access Token"}
    if not report or not report.get("top_nodes"):
        return {"ok": False, "error": "还没有优选结果，请先点「开始优选」"}

    cm.LOG_HOOK = _append_log
    try:
        nodes = [(ip, port, ms, kbps) for ip, port, ms, kbps, _src in report["top_nodes"]]
        plain = cm.gen_gh_sub(nodes)
        _append_log(f"上传 {st['gh_repo']}/{st['gh_file']} ...")
        sha = cm.gh_upload_file(token, st["gh_repo"], st["gh_file"], plain,
                                branch=st["gh_branch"],
                                message="Update CFBox subscription (auto from panel)")
        raw = "https://raw.githubusercontent.com/%s/%s/%s" % (st["gh_repo"], st["gh_branch"], st["gh_file"])
        _append_log(f"✓ GitHub 上传成功: {raw}")
        _append_log(f"  commit: {sha}")
        _append_log(f"  cfbb 汇聚订阅已指向该地址，Karing 更新「自己用」即生效")
        return {"ok": True, "raw": raw, "sha": sha}
    except Exception as e:
        _append_log(f"✗ GitHub 上传失败: {repr(e)}")
        return {"ok": False, "error": repr(e)}
    finally:
        cm.LOG_HOOK = None


def get_status():
    with _lock:
        r = STATE["result"]
        sub_info = None
        if r and r.get("top_nodes"):
            p = os.path.join(BASE, "nodes_sub.txt")
            if os.path.exists(p):
                sub_info = {
                    "exists": True,
                    "bytes": os.path.getsize(p),
                    "mtime": time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(p))),
                    "path": p,
                    "raw_url": cm.GH["raw_url"],
                    "cfbb_sub": cm.CFBB["sub_url"],
                }
        return {
            "running": STATE["running"],
            "logs": list(STATE["logs"]),
            "result": STATE["result"],
            "last_error": STATE["last_error"],
            "sub_info": sub_info,
        }


# ==================== HTTP 处理 ====================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            html = io.open(os.path.join(BASE, "index.html"), "r", encoding="utf-8").read()
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(200, json.dumps(get_status(), ensure_ascii=False))
        elif path == "/api/settings":
            st = load_settings()
            st = dict(st)
            if st.get("gh_token"):
                st["gh_token"] = st["gh_token"][:4] + "****" + st["gh_token"][-4:]
            self._send(200, json.dumps(st, ensure_ascii=False))
        elif path == "/api/cfbb":
            p = os.path.join(BASE, "cfbb_nodes_to_paste.txt")
            content = io.open(p, "r", encoding="utf-8").read() if os.path.exists(p) else ""
            self._send(200, json.dumps({"content": content, "exists": bool(content.strip())}, ensure_ascii=False))
        elif path == "/api/upload-check":
            p = os.path.join(BASE, "cfbb_nodes_to_paste.txt")
            ready = os.path.exists(p) and os.path.getsize(p) > 10
            self._send(200, json.dumps({"ready": ready, "file": p if ready else None}, ensure_ascii=False))
        elif path == "/api/health":
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?")[0]
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            data = {}
        if path == "/api/start":
            ok = start_optimize(int(data.get("min_kbps") or cm.MIN_KBPS))
            self._send(200, json.dumps({"ok": ok}))
        elif path == "/api/apply":
            self._send(200, json.dumps(apply_deploy(), ensure_ascii=False))
        elif path == "/api/upload-gh":
            self._send(200, json.dumps(upload_gh(), ensure_ascii=False))
        elif path == "/api/settings":
            st = load_settings()
            if "gh_token" in data:
                st["gh_token"] = str(data["gh_token"]).strip()
            if "gh_repo" in data:
                st["gh_repo"] = str(data["gh_repo"]).strip() or cm.GH["repo"]
            if "gh_file" in data:
                st["gh_file"] = str(data["gh_file"]).strip() or cm.GH["file"]
            if "gh_branch" in data:
                st["gh_branch"] = str(data["gh_branch"]).strip() or cm.GH["branch"]
            save_settings(st)
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CFBox 维护面板已启动: http://127.0.0.1:{PORT}")
    print("关闭本窗口即停止服务。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
