#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号「发布历史」查询工具
================================
- 走公众号后台网页会话（扫码登录），不依赖 OpenAPI 发布能力权限（48001 绕开）
- 登录/抓取用本机 Chromium（headless + Playwright），会话 cookies 本地保存
- 本地 Web UI（127.0.0.1）：模糊搜索（多关键词 AND + 高亮）、日期区间、排序、
  导出 CSV / Markdown、一键刷新
- 数据：自动分页拉取后台「发表记录」全部文章（标题/时间/摘要/作者/链接）

依赖（首次安装，本目录已装好则直接运行）:
    python3 -m venv .venv && .venv/bin/pip install playwright
    需要系统 Chromium:  /usr/bin/chromium 或 google-chrome

用法:
    ./wx_history            # 或 python3 wx_history.py [端口]，默认 8765

文件（本目录生成）:
    wx_state.json   后台会话（cookies，等同登录凭证，勿外传）
    articles.json   文章数据缓存
    debug.log       调试日志
"""

import base64
import html as _html_mod
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import http.server
import webbrowser
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "wx_state.json")
DATA_FILE = os.path.join(DIR, "articles.json")
LOG_FILE = os.path.join(DIR, "debug.log")
def _find_chromium():
    """跨平台找 Chromium/Chrome/Edge；找不到时返回 None（用 Playwright 自带内核）。"""
    names = ["chromium", "chromium-browser", "google-chrome", "chrome", "msedge"]
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    cands = []
    if os.name == "posix":
        cands.append("/usr/bin/chromium")
    else:
        cands += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None

CHROMIUM = _find_chromium()
BASE = "https://mp.weixin.qq.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
PAGE_SIZE = 20
MAX_RECORDS = 5000
LOGIN_TIMEOUT = 300

# ---------------------------------------------------------------- 日志

def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------- 全局状态

STATE = {
    "logged_in": False,
    "token": None,
    "login_state": "idle",        # idle | qrcode | waiting | scanning | done | error
    "login_msg": "",
    "qr_bytes": None,
    "fetching": False,
    "last_fetch": None,
    "fetch_msg": "",
}
LOCK = threading.RLock()

def set_state(**kw):
    with LOCK:
        STATE.update(kw)

# ---------------------------------------------------------------- Playwright 会话

def new_browser(pw, headless=True):
    return pw.chromium.launch(
        executable_path=CHROMIUM, headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"])

def new_context(pw, browser):
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1300, "height": 900})
    if os.path.exists(STATE_FILE):
        try:
            ctx.add_cookies(json.load(open(STATE_FILE, encoding="utf-8")).get("cookies", []))
        except Exception as e:
            log("读取已存会话失败: %s" % e)
    return ctx

def save_session(context, token=None):
    st = context.storage_state()
    if token:
        st["token"] = token
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.chmod(STATE_FILE, 0o600)
    except Exception as e:
        log("保存会话失败: %s" % e)

def extract_token(context):
    """登录后取后台 token。
    若会话 cookie 已完整（bizlogin 已执行过），直接访问 home 页从 URL/HTML 取；
    否则补做 bizlogin（POST）拿到带 token 的 redirect_url。"""
    def try_home():
        pg = context.new_page()
        try:
            pg.goto(BASE + "/cgi-bin/home?t=home/index&lang=zh_CN",
                    timeout=30000, wait_until="domcontentloaded")
            time.sleep(3)
            m = re.search(r"token=(\d+)", pg.url)
            if not m:
                m = re.search(r"token=(\d+)", pg.content()[:80000])
            return m.group(1) if m else None
        finally:
            pg.close()

    token = try_home()
    if token:
        return token
    # 补做 bizlogin：前端在 ask status=1 后的动作（必须从登录页 origin 内发起，
    # 否则服务端校验 referrer 失败）
    pg = context.new_page()
    try:
        pg.goto(BASE + "/", timeout=30000, wait_until="domcontentloaded")
        j = pg.evaluate("""async () => {
            const r = await fetch('/cgi-bin/bizlogin?action=login', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'userlang=zh_CN&redirect_url=&cookie_forbidden=0'
                     + '&cookie_cleaned=0&plugin_used=0&login_type=3'
            });
            return r.json();
        }""")
        redir = (j or {}).get("redirect_url") or ""
        ret = (j or {}).get("base_resp", {}).get("ret")
        log("bizlogin: ret=%s redirect=%s" % (ret, redir[:120]))
        m = re.search(r"token=(\d+)", redir)
        if m:
            # 跟随 redirect 让完整会话落地，再从 URL 复核 token
            pg.goto(BASE + redir if redir.startswith("/") else redir,
                    timeout=30000, wait_until="domcontentloaded")
            time.sleep(3)
            m2 = re.search(r"token=(\d+)", pg.url)
            return (m2 or m).group(1)
        return try_home()
    except Exception as e:
        log("bizlogin 失败: %s" % e)
        return try_home()
    finally:
        pg.close()

# ---------------------------------------------------------------- 登录流程

def _get_qrcode(context):
    """取当前登录二维码图片字节。"""
    pg = context.new_page()
    try:
        pg.goto(BASE + "/", timeout=30000, wait_until="domcontentloaded")
        try:
            pg.wait_for_selector('img[src*="scanloginqrcode"]', timeout=15000)
        except Exception:
            log("登录页未出现二维码 img: %s" % pg.content()[:300])
            return None
        img = pg.query_selector('img[src*="scanloginqrcode"]')
        src = img.get_attribute("src")
        url = src if src.startswith("http") else BASE + src
        resp = context.request.get(url, timeout=15000)
        if resp.status != 200 or not resp.body():
            log("二维码请求失败 status=%s" % resp.status)
            return None
        return resp.body()
    finally:
        pg.close()

def _ask(context):
    resp = context.request.get(BASE + "/cgi-bin/scanloginqrcode?action=ask",
                               timeout=15000)
    return resp.json() if resp.status == 200 else None

def _login_worker():
    from playwright.sync_api import sync_playwright
    set_state(login_state="qrcode", login_msg="正在生成二维码…")
    with sync_playwright() as pw:
        browser = new_browser(pw)
        try:
            context = new_context(pw, browser)
            deadline = time.time() + LOGIN_TIMEOUT
            confirmed = False
            while time.time() < deadline:
                qr = _get_qrcode(context)
                if not qr:
                    set_state(login_state="error",
                              login_msg="获取二维码失败，请点「重新获取」（见 debug.log）")
                    return
                set_state(login_state="waiting", login_msg="请用手机微信扫码…",
                         qr_bytes=qr)
                while time.time() < deadline:
                    time.sleep(2)
                    j = _ask(context)
                    if not j:
                        continue
                    st = j.get("status", 0)
                    if st == 1:                      # 已确认登录
                        confirmed = True
                        break
                    if st in (2, 3, 7):              # 过期/错误 → 换码
                        set_state(login_msg="二维码已刷新，请扫新码")
                        break
                    if st == 4:                      # 已扫码，等手机确认
                        set_state(login_state="scanning",
                                  login_msg="已扫码，请在手机上确认登录")
                    elif st == 5:
                        set_state(login_msg="该账号绑定 QQ 登录，请换普通扫码")
                        break
                if confirmed:
                    break
            if not confirmed:
                set_state(login_state="error", login_msg="登录超时，请重新获取二维码")
                return
            # 取 token 并保存会话
            token = extract_token(context)
            save_session(context, token)
            if not token:
                set_state(login_state="error",
                          login_msg="登录成功但未取到 token，请重试")
                return
            set_state(logged_in=True, token=token,
                      login_state="done", login_msg="登录成功，正在拉取发表记录…")
            log("登录成功 token=%s" % token)
            _fetch_all(token, context, pg_holder=[])
        finally:
            browser.close()

# ---------------------------------------------------------------- 拉取发表记录

def _parse_item(p):
    out = []
    try:
        info = p.get("publish_info") or {}
        ts = info.get("publish_time") or 0
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        news = (p.get("content") or {}).get("news_item") or []
        appmsgid = str(p.get("appmsgid") or "")
        for n in news:
            out.append({
                "date": dt, "ts": ts,
                "title": (n.get("title") or "").strip(),
                "digest": (n.get("digest") or "").strip(),
                "url": n.get("url") or "",
                "author": (n.get("author") or "").strip(),
                "appmsgid": appmsgid, "channel": "发表",
            })
        if not news and p.get("content"):
            raw = json.dumps(p["content"], ensure_ascii=False)[:200]
            out.append({"date": dt, "ts": ts, "title": "(未识别格式) " + raw,
                        "digest": "", "url": "", "author": "",
                        "appmsgid": appmsgid, "channel": "发表"})
    except Exception as e:
        log("解析发表单条失败: %s" % e)
    return out

def _parse_mass_item(it):
    """群发记录单条（订阅号常用通道）解析成文章条目。"""
    out = []
    try:
        ts = it.get("sent_time") or 0
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        msg = it.get("msg") or {}
        mt = msg.get("msgtype") or ""
        mid = str(it.get("msg_id") or it.get("mass_id") or it.get("idx") or "")
        if mt == "mpnews":
            for n in msg.get("mpnews", {}).get("news_item") or []:
                out.append({
                    "date": dt, "ts": ts,
                    "title": (n.get("title") or "").strip(),
                    "digest": (n.get("digest") or "").strip(),
                    "url": n.get("url") or n.get("link") or "",
                    "author": (n.get("author") or "").strip(),
                    "appmsgid": mid, "channel": "群发",
                })
        elif mt == "text":
            out.append({"date": dt, "ts": ts,
                        "title": (msg.get("text", {}).get("content") or "").strip()[:80],
                        "digest": "", "url": "", "author": "",
                        "appmsgid": mid, "channel": "群发"})
        else:
            out.append({"date": dt, "ts": ts, "title": "(%s 类型)" % (mt or "未知"),
                        "digest": "", "url": "", "author": "",
                        "appmsgid": mid, "channel": "群发"})
    except Exception as e:
        log("解析群发单条失败: %s" % e)
    return out

def _scrape_mass_ui(token, context):
    """兜底：驱动后台 UI 打开「已群发」页，捕获页面自身 XHR + DOM 表格。
    不依赖事先知道接口路径。返回 (articles, 捕获到的接口 url 列表)。"""
    captured = []
    def on_resp(resp):
        try:
            u = resp.url
            if "/cgi-bin/" in u and resp.status == 200:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" in ct or "text" in ct:
                    if "appmsgpublish" in u or "home" in u:
                        return
                    captured.append({"url": u,
                                     "body": resp.text()[:400000]})
        except Exception:
            pass
    pg = context.new_page()
    pg.on("response", on_resp)
    articles = []
    try:
        pg.goto(BASE + "/cgi-bin/home?t=home/index&lang=zh_CN&token=%s" % token,
                timeout=30000, wait_until="domcontentloaded")
        pg.wait_for_timeout(5000)
        # 先抓侧边栏全部后台链接，找「群发/消息记录」相关页面真实 URL
        links = pg.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({t: (a.innerText||'').trim().slice(0,20), h: a.href}))
                .filter(x => x.h.includes('/cgi-bin/') && x.t);
        }""")
        for l in links:
            log("侧边栏链接: [%s] %s" % (l["t"], l["h"][:120]))
        target = None
        for l in links:
            if ("群发" in l["t"] or "消息记录" in l["t"]
                    or "mass" in l["h"].lower()):
                target = l["h"]
                break
        if target:
            pg.goto(target if target.startswith("http") else BASE + target,
                    timeout=30000, wait_until="domcontentloaded")
            pg.wait_for_timeout(6000)
        else:
            # 没找到就点菜单文本
            clicked = pg.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('a,li,span,div'));
                for (const e of els) {
                    const t = (e.innerText || '').trim();
                    if ((t === '已群发消息' || t === '群发消息' || t === '消息记录')
                            && e.offsetParent !== null) {
                        (e.closest('a') || e).click();
                        return t;
                    }
                }
                return null;
            }""")
            log("群发页菜单点击: %s" % clicked)
            pg.wait_for_timeout(6000)
        # 1) 解析捕获到的 JSON
        for c in captured:
            try:
                j = json.loads(c["body"])
            except Exception:
                continue
            for key in ("item", "mass_message_list", "msg_list", "list",
                        "news_item", "publish_list"):
                items = j.get(key)
                if isinstance(items, list) and items:
                    for it in items:
                        if isinstance(it, dict):
                            articles.extend(_parse_mass_item(it))
        # 2) DOM 表格兜底
        if not articles:
            try:
                rows = pg.query_selector_all("table tr")
                for r in rows:
                    tds = r.query_selector_all("td")
                    if len(tds) >= 2:
                        title = tds[0].inner_text().strip()
                        if not title or "群发" in title[:4]:
                            continue
                        articles.append({"date": "", "ts": 0, "title": title[:120],
                                         "digest": "", "url": "", "author": "",
                                         "appmsgid": "dom", "channel": "群发(DOM)"})
            except Exception as e:
                log("DOM 抓取失败: %s" % e)
        try:
            with open(os.path.join(DIR, "captured_xhr.json"), "w",
                      encoding="utf-8") as f:
                json.dump(captured, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    except Exception as e:
        log("群发页驱动失败: %s" % e)
    finally:
        pg.close()
    urls = [c["url"].split("?")[0] for c in captured]
    log("群发 UI 捕获接口 %d 个, 解析文章 %d 篇: %s" %
        (len(captured), len(articles), urls[:8]))
    return articles, urls

def _clean_text(s):
    """去掉 HTML 实体（&nbsp; 等）并压缩空白。"""
    import re as _re
    s = _html_mod.unescape(s or "")
    return _re.sub(r"\s+", " ", s).strip()

def _parse_publish_js_data(data):
    """解析浏览器里的 publish_page 对象 → 文章条目列表。
    结构: publish_list[].{publish_info:dict(create_time...),
          appmsg_info:list[{title,content_url,appmsgid,author?}], view:dict}
    """
    out = []
    for p in data.get("publish_list") or []:
        info = p.get("publish_info") or {}
        if not isinstance(info, dict):
            try:
                import html as _h
                info = json.loads(_h.unescape(info) if isinstance(info, str) else json.dumps(info))
            except Exception:
                info = {}
        ts = info.get("create_time") or 0
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        ainfo = p.get("appmsg_info")
        if isinstance(ainfo, dict):
            ainfo = [ainfo]
        for a in ainfo or []:
            title = _clean_text(a.get("title"))
            if not title:
                continue  # 空标题条目（图片/链接等无标题项）跳过
            out.append({
                "date": dt, "ts": ts,
                "title": title,
                "digest": _clean_text(a.get("digest")),
                "url": a.get("content_url") or a.get("url") or "",
                "author": _clean_text(a.get("author")),
                "appmsgid": str(a.get("appmsgid") or p.get("msgid") or ""),
                "channel": "发表",
            })
    return out

def _fetch_all(token, context, pg_holder=None):
    """用已登录会话拉取「发表记录」页面（浏览器解析 publish_page）+ 群发通道。"""
    set_state(fetching=True, fetch_msg="拉取中（发表记录 + 群发记录）…")
    articles, seen = [], set()
    pub_n = mass_n = 0
    pub_total = mass_total = None
    pg = context.new_page()
    try:
        # --- 通道1：发表记录（页面 JS 全局变量 publish_page） ---
        begin, total, page_size = 0, None, 50
        for _ in range(300):
            url = (BASE + "/cgi-bin/appmsgpublish?sub=list&begin=%d&count=%d"
                   "&token=%s&lang=zh_CN" % (begin, page_size, token))
            pg.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                pg.wait_for_function("typeof publish_page !== 'undefined'",
                                     timeout=15000)
            except Exception:
                log("发表页未出现 publish_page (begin=%d)" % begin)
                break
            data = pg.evaluate("() => publish_page")
            if not data or not data.get("publish_list"):
                break
            total = data.get("total_count") or total
            parsed = _parse_publish_js_data(data)
            if begin == 0:
                log("发表页 total_count=%s 本页 %d 条" %
                    (data.get("total_count"), len(parsed)))
            for a in parsed:
                key = (a["appmsgid"], a["title"], a["date"])
                if key not in seen:
                    seen.add(key); articles.append(a); pub_n += 1
            begin += len(data["publish_list"])
            if total is not None and begin >= total:
                break
            if len(articles) >= MAX_RECORDS:
                break
            time.sleep(0.3)
        pub_total = total
        log("发表记录 %d 篇 (total_count=%s)" % (pub_n, total))

        # --- 通道2：群发记录 mass/getall（订阅号发文常用） ---
        mass_tpls = [
            "/cgi-bin/mass/getall?token=%s&lang=zh_CN&f=json&ajax=1&offset=%d&count=%d",
            "/cgi-bin/message/mass/getall?token=%s&lang=zh_CN&f=json&offset=%d&count=%d",
        ]
        for t in mass_tpls:
            off = 0
            got_any = False
            mj = {}
            for _ in range(300):
                url = BASE + (t % (token, off, PAGE_SIZE))
                resp = context.request.get(url, timeout=20000)
                if resp.status != 200:
                    break
                try:
                    mj = resp.json()
                except Exception:
                    break
                if mj.get("errcode") not in (0, None):
                    log("mass %s errcode=%s errmsg=%s" %
                        (t.split("?")[0], mj.get("errcode"), mj.get("errmsg")))
                    break
                items = mj.get("item") or []
                if items:
                    got_any = True
                mass_total = mj.get("total_count")
                for it in items:
                    for a in _parse_mass_item(it):
                        key = (a["appmsgid"], a["title"], a["date"])
                        if key not in seen:
                            seen.add(key); articles.append(a); mass_n += 1
                off += PAGE_SIZE
                nxt = mj.get("next_offset")
                if not items or (mass_total is not None and off >= mass_total) \
                        or (nxt is not None and off >= nxt):
                    break
                if len(articles) >= MAX_RECORDS:
                    break
                time.sleep(0.2)
            if got_any or mj.get("errcode") == 0:
                log("群发记录 %d 篇 (path=%s total=%s)" %
                    (mass_n, t.split("?")[0], mass_total))
                break
        # --- 通道3：UI 兜底（驱动后台页面，捕获真实接口/DOM） ---
        if pub_n == 0:
            try:
                ui_arts, ui_urls = _scrape_mass_ui(token, context)
                for a in ui_arts:
                    key = (a["appmsgid"], a["title"], a["date"])
                    if key not in seen:
                        seen.add(key); articles.append(a); mass_n += 1
            except Exception as e:
                log("UI 兜底抓取失败: %s" % e)
        # 若两条都为 0，给出明确提示
        if pub_n == 0 and mass_n == 0:
            log("各通道均为 0 篇，publish_count=%s mass_total=%s" % (pub_total, mass_total))

        articles.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        payload = {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "count": len(articles), "articles": articles}
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)
        set_state(fetching=False, last_fetch=payload["updated_at"],
                  fetch_msg="共 %d 篇（发表 %d + 群发 %d）" %
                  (len(articles), pub_n, mass_n))
        log("拉取完成 共 %d 篇" % len(articles))
    except Exception as e:
        set_state(fetching=False, fetch_msg="拉取失败: %s" % e)
        log("拉取失败: %s" % e)
    finally:
        try:
            pg.close()
        except Exception:
            pass

def _refresh_worker():
    from playwright.sync_api import sync_playwright
    with LOCK:
        token = STATE["token"]
    # 无内存 token 时尝试从会话文件恢复
    if not token and os.path.exists(STATE_FILE):
        try:
            token = json.load(open(STATE_FILE, encoding="utf-8")).get("token")
            STATE["token"] = token
        except Exception:
            pass
    if not token:
        set_state(logged_in=False, login_msg="会话不存在，请重新扫码")
        return
    set_state(fetching=True, fetch_msg="校验会话并重新拉取…")
    with sync_playwright() as pw:
        browser = new_browser(pw)
        try:
            context = new_context(pw, browser)
            # 用已存 token 直接调一个轻量接口校验有效性
            r = context.request.get(BASE +
                "/cgi-bin/appmsgpublish?token=%s&lang=zh_CN&f=json&ajax=1"
                "&action=list_ex&begin_index=0&offset=1" % token, timeout=20000)
            ok = False
            if r.status == 200:
                try:
                    j = r.json()
                    ok = "publish_page" in j or "errcode" not in j
                except Exception:
                    ok = False
            if not ok:
                set_state(fetching=False, logged_in=False,
                          fetch_msg="会话已过期，请重新扫码",
                          login_msg="会话已过期")
                log("token 校验失败: %s" % r.text()[:200])
                return
            save_session(context, token)
            set_state(logged_in=True)
            _fetch_all(token, context)
        finally:
            browser.close()

# ---------------------------------------------------------------- Web UI

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="version" content="20260913-1315">
<title>公众号发布历史</title>
<style>
:root{--bg:#0f1420;--panel:#171e2e;--line:#263049;--fg:#dbe4f5;--dim:#8291ad;
      --acc:#4f8cff;--mark:#3a2f10;--markfg:#ffd76a;--ok:#3ddc97;--err:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:20px;margin:8px 0 2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
input,select,button{font:inherit;color:var(--fg);background:#0d1220;
     border:1px solid var(--line);border-radius:7px;padding:7px 10px;outline:none}
input:focus,select:focus{border-color:var(--acc)}
#q{flex:1;min-width:220px}
button{cursor:pointer;background:#1c2740}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
.count{color:var(--dim);font-size:12px;margin:0 0 10px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:normal;font-size:12px;white-space:nowrap}
td.date{white-space:nowrap;color:var(--dim);font-size:12px}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
.digest{color:var(--dim);font-size:12px;max-width:420px}
mark{background:var(--mark);color:var(--markfg);border-radius:2px;padding:0 1px}
.login{max-width:460px;margin:40px auto;text-align:center}
.login .qr{width:280px;height:280px;margin:18px auto;display:block;
     background:#fff;border-radius:10px;padding:6px;object-fit:contain}
.status{margin-top:10px;min-height:22px}
.status.err{color:var(--err)}
.hidden{display:none}
.tip{color:var(--dim);font-size:12px;margin-top:14px;line-height:1.8}
.badge{display:inline-block;font-size:11px;border:1px solid var(--line);
       border-radius:20px;padding:1px 8px;color:var(--dim);margin-left:8px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid var(--line);
      border-top-color:var(--acc);border-radius:50%;animation:sp 1s linear infinite;
      vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <h1>公众号发布历史<span class="badge" id="meta"></span></h1>
  <div class="sub">数据来源：公众号后台「发表记录」 · <span id="fetchinfo"></span></div>

  <!-- 登录视图 -->
  <div class="panel login hidden" id="loginView">
    <h2 style="font-size:16px">登录微信公众平台</h2>
    <div class="sub" style="margin-bottom:4px">仅用于读取本账号发表记录，手机微信扫二维码并确认</div>
    <div id="qrBox">
      <button class="primary" id="btnLogin" style="padding:10px 26px;font-size:15px">获取登录二维码</button>
    </div>
    <div class="status" id="loginStatus"></div>
    <button id="btnStopLogin" style="margin-top:8px;background:#8b2e2e;border-color:#8b2e2e;color:#fff">关闭服务</button>
    <div class="tip">提示：会话有效期数小时；过期后点右上角「刷新数据」会要求重新扫码。</div>
  </div>

  <!-- 数据视图 -->
  <div id="dataView" class="hidden">
    <div class="panel">
      <div class="bar">
        <input id="q" placeholder="搜索标题/摘要/作者，空格分隔多关键词（需同时匹配），如：漏洞 红队">
        <button class="primary" id="btnSearch">搜索</button>
        <button id="btnClear">清空</button>
        <input type="date" id="from" title="起始日期">
        <input type="date" id="to" title="截止日期">
        <select id="sort">
          <option value="desc">时间 新→旧</option>
          <option value="asc">时间 旧→新</option>
        </select>
        <button id="btnRefresh">刷新数据</button>
        <button id="btnCsv">导出 CSV</button>
        <button id="btnMd">导出 Markdown</button>
        <button id="btnStop" style="margin-left:auto;background:#8b2e2e;border-color:#8b2e2e;color:#fff">关闭服务</button>
      </div>
      <div class="count" id="count"></div>
      <table>
        <thead><tr><th style="width:130px">发布时间</th><th>标题</th><th style="width:44%">摘要</th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
let ALL = [];
const $ = id => document.getElementById(id);
const esc = s => String(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function api(p,o){return (await fetch(p,o)).json()}
function hl(text,kws){let t=esc(text);
  for(const k of kws){if(!k)continue;
    const rk=k.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
    t=t.replace(new RegExp("("+rk+")","gi"),"<mark>$1</mark>");}
  return t}
function filtered(){
  const q=$("q").value.trim();const kws=q?q.split(/\s+/):[];
  const from=$("from").value,to=$("to").value,dir=$("sort").value;
  const list=ALL.filter(a=>{
    const hay=(a.title+" "+a.digest+" "+(a.author||"")).toLowerCase();
    for(const k of kws){if(!hay.includes(k.toLowerCase()))return false}
    const d=a.date.slice(0,10);
    if(from&&d<from)return false;
    if(to&&d>to)return false;
    return true});
  list.sort((a,b)=>dir==="asc"?a.ts-b.ts:b.ts-a.ts);
  return {list,kws};
}
function render(){
  const {list,kws}=filtered();
  $("count").textContent=`显示 ${list.length} / ${ALL.length} 篇`;
  $("rows").innerHTML=list.map(a=>`<tr>
    <td class="date">${esc(a.date)}</td>
    <td>${a.url?`<a href="${esc(a.url)}" target="_blank">${hl(a.title,kws)}</a>`:hl(a.title,kws)}</td>
    <td class="digest">${hl(a.digest,kws)}</td></tr>`).join("")
    ||`<tr><td colspan="3" style="color:var(--dim)">没有匹配的文章</td></tr>`;
}
function csvEsc(s){s=String(s||"");return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s}
function download(name,content,mime){
  const b=new Blob([content],{type:mime});const a=document.createElement("a");
  a.href=URL.createObjectURL(b);a.download=name;a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),5000);
}
function exportCsv(){
  const {list}=filtered();
  const L=["发布时间,标题,摘要,作者,链接"];
  for(const a of list)L.push([a.date,a.title,a.digest,a.author,a.url].map(csvEsc).join(","));
  download("wechat_published.csv","\ufeff"+L.join("\r\n"),"text/csv;charset=utf-8");
}
function exportMd(){
  const {list}=filtered();
  const L=["# 公众号已发布文章","",`共 ${list.length} 篇（导出时间 ${new Date().toLocaleString("zh-CN")}）`,""];
  for(const a of list)L.push(`- **${a.date}** [${a.title}](${a.url||"#"})${a.digest?` — ${a.digest}`:""}`);
  download("wechat_published.md",L.join("\n"),"text/markdown;charset=utf-8");
}
let pollTimer=null;
async function pollLogin(){
  const s=await api("/api/state");
  const el=$("loginStatus");
  if(s.logged_in){el.textContent="登录成功，正在拉取数据…";clearInterval(pollTimer);loadData();}
  else if(s.login_state==="error"){
    el.className="status err";
    el.textContent=s.login_msg||"登录失败";clearInterval(pollTimer);
    $("qrBox").innerHTML='<button class="primary" id="btnLogin2" style="padding:10px 26px">重新获取二维码</button>';
    $("btnLogin2").onclick=startLogin;
  } else {
    el.className="status";
    el.innerHTML=(s.fetching?'<span class="spin"></span>':"")+(s.login_msg||"等待扫码…");
    if(s.login_state==="qrcode"||s.login_msg.includes("扫码")){
      const hasImg=$("qrBox").querySelector("img");
      if(!hasImg&&s.login_state!=="qrcode"&&el._showedQr){/*already*/}
      if(!hasImg){$("qrBox").innerHTML=`<img class="qr" id="qrimg" src="/api/qrcode.png?r=${Date.now()}">`;}
      else {const img=$("qrimg"); if(img) img.src="/api/qrcode.png?r="+Date.now();}
    }
  }
}
async function startLogin(){
  clearInterval(pollTimer);
  $("loginStatus").className="status";
  $("loginStatus").textContent="正在生成二维码…";
  await api("/api/login_start",{method:"POST"});
  pollTimer=setInterval(pollLogin,2000);
  pollLogin();
}
async function loadData(){
  const d=await api("/api/articles");
  ALL=d.articles||[];
  $("meta").textContent=d.updated_at?`更新于 ${d.updated_at}`:"";
  $("fetchinfo").innerHTML=(d.fetching?'<span class="spin"></span>':"")+(d.fetch_msg||`${d.count} 篇`);
  $("loginView").classList.add("hidden");
  $("dataView").classList.remove("hidden");
  render();
  if(d.fetching)setTimeout(loadData,2000);
}
async function init(){
  const s=await api("/api/state");
  if(s.logged_in){await loadData();}
  else {$("loginView").classList.remove("hidden");startLogin();}
}
// JS 出任何错误都显示在页面上，避免"无声失败"
window.addEventListener("error",e=>{
  const el=$("loginStatus")||document.body;
  el.textContent="页面脚本错误："+(e.message||e.error);
  el.className="status err";
});
$("q").addEventListener("input",render);
$("btnSearch").onclick=render;
$("btnClear").onclick=()=>{$("q").value="";$("from").value="";$("to").value="";render();};
$("from").addEventListener("change",render);
$("to").addEventListener("change",render);
$("sort").addEventListener("change",render);
async function stopService(){
  if(!confirm("确定关闭服务吗？关闭后需重新启动才能再次使用。"))return;
  try{
    const r=await api("/api/stop",{method:"POST"});
    document.body.innerHTML="<div style='padding:60px;text-align:center;font-size:16px'>"+(r&&r.msg?"服务正在关闭…":"服务已关闭")+"</div>";
  }catch(e){
    alert("关闭请求失败："+e.message);
  }
}
$("btnCsv").onclick=exportCsv;
$("btnStop").onclick=stopService;
$("btnStopLogin").onclick=stopService;
$("btnMd").onclick=exportMd;
$("btnRefresh").onclick=async()=>{
  const r=await api("/api/refresh",{method:"POST"});
  if(!r.logged_in){
    $("dataView").classList.add("hidden");
    $("loginView").classList.remove("hidden");
    startLogin();
  } else setTimeout(loadData,1500);
};
init();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- HTTP 服务

# 由 main() 在创建服务器后赋值；/api/stop 用它优雅退出
HTTPD = None


def _stop_service():
    log("收到关闭服务请求，正在退出...")
    try:
        if HTTPD is not None:
            threading.Thread(target=HTTPD.shutdown, daemon=True).start()
        # 等待主线程退出 serve_forever；兜底强退（chromium 子进程是 daemon 线程管理的）
        import time
        time.sleep(2)
        os._exit(0)
    except Exception:
        os._exit(0)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "wx-history/2.0"

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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/":
                self._send(200, HTML, "text/html; charset=utf-8")
            elif p == "/api/state":
                with LOCK:
                    body = {k: STATE[k] for k in
                            ("logged_in", "login_state", "login_msg",
                             "fetching", "last_fetch", "fetch_msg")}
                self._json(body)
            elif p == "/api/qrcode.png":
                with LOCK:
                    qrb = STATE.get("qr_bytes")
                if not qrb:
                    self._json({"error": "no qrcode yet"}, 404)
                    return
                ctype = "image/jpeg" if qrb[:3] == b"\xff\xd8\xff" else "image/png"
                self._send(200, qrb, ctype)
            elif p == "/api/articles":
                if os.path.exists(DATA_FILE):
                    data = json.load(open(DATA_FILE, encoding="utf-8"))
                else:
                    data = {"updated_at": "", "count": 0, "articles": []}
                with LOCK:
                    data["fetching"] = STATE["fetching"]
                    data["fetch_msg"] = STATE["fetch_msg"]
                    data["updated_at"] = data.get("updated_at") or (STATE["last_fetch"] or "")
                self._json(data)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            log("handler 异常: %s" % e)
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/api/login_start":
                with LOCK:
                    if STATE["login_state"] == "waiting":
                        self._json({"ok": True, "msg": "已在等待扫码"})
                        return
                    if STATE["fetching"]:
                        self._json({"ok": False, "msg": "正在拉取数据"})
                        return
                threading.Thread(target=_login_worker, daemon=True).start()
                self._json({"ok": True})
            elif p == "/api/stop":
                # 先应答，再异步退出（给浏览器时间收到响应）
                self._json({"ok": True, "msg": "服务正在关闭"})
                threading.Thread(target=_stop_service, daemon=True).start()
            elif p == "/api/refresh":
                with LOCK:
                    if not STATE["logged_in"]:
                        self._json({"logged_in": False})
                        return
                    if STATE["fetching"]:
                        self._json({"logged_in": True, "fetching": True})
                        return
                threading.Thread(target=_refresh_worker, daemon=True).start()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            log("handler 异常: %s" % e)
            self._json({"error": str(e)}, 500)


def pick_port(prefer):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", prefer))
        s.close()
        return prefer
    except OSError:
        s.close()
        return 0


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    port = pick_port(port)
    if not port:
        print("端口 %d 已被占用：可能已有实例在运行（浏览器打开 http://127.0.0.1:%d），"
              "或换一个端口：./wx_history 9000" % (port, port))
        sys.exit(1)
    # 检查 playwright 是否可用
    try:
        import playwright  # noqa
    except ImportError:
        print("缺少 playwright，请先安装：")
        print("  python3 -m venv .venv && .venv/bin/pip install playwright")
        sys.exit(1)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    global HTTPD
    HTTPD = httpd
    if os.path.exists(STATE_FILE):
        # 有已存会话：后台自动校验并（无数据时）拉取
        threading.Thread(target=_refresh_worker, daemon=True).start()
    log("服务已启动: http://127.0.0.1:%d  (chromium=%s)" % (port, CHROMIUM))
    try:
        webbrowser.open("http://127.0.0.1:%d" % port)
    except Exception:
        pass
    print("=" * 58)
    print("  公众号发布历史查询工具")
    print("  浏览器打开: http://127.0.0.1:%d" % port)
    print("  首次使用：点「获取登录二维码」→ 手机微信扫码确认")
    print("  按 Ctrl+C 退出")
    print("=" * 58, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
