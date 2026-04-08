#!/usr/bin/env python3

import os
import sys
import time
import json
import platform
import random
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone, timedelta

import requests
from seleniumbase import SB

# ---------- 配置 ----------
BASE_URL = "https://client.falixnodes.net"
LOGIN_URL = f"{BASE_URL}/auth/login"
OUTPUT_DIR = Path("output/falix")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_RETRY = 3
AD_RETRY_LIMIT = 10  # Start 重试次数
CN_TZ = timezone(timedelta(hours=8))

screenshot_counter = {"count": 0}


def cn_time() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def is_linux() -> bool:
    return platform.system().lower() == "linux"


def email_to_filename(email: str) -> str:
    """将邮箱转换为安全的文件名"""
    if not email or "@" not in email:
        return "unknown"
    local, domain = email.split("@", 1)
    domain_short = domain.replace(".", "")[-2:] if domain else "xx"
    return f"{local[0]}_{domain_short}"


def shot(sb, name: str) -> str:
    """截图函数"""
    screenshot_counter["count"] += 1
    timestamp = datetime.now(CN_TZ).strftime('%H%M%S')
    clean_name = re.sub(r'[":><|*?\r\n/\\]', '', name)
    filename = f"{screenshot_counter['count']:03d}-{timestamp}-{clean_name}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        sb.save_screenshot(filepath)
    except Exception as e:
        print(f"[ERROR] 截图失败: {e}")
    return filepath


def mask_email_log(email: str) -> str:
    """仅用于日志输出的邮箱脱敏"""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@***{domain[-2:]}"


# ---------- Telegram 通知 ----------
def notify(ok: bool, email: str = "", summary: str = "", server_details: List[Dict] = None, screenshots: List[str] = None):
    """发送 Telegram 通知"""
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return
    
    try:
        text = f"{'✅ 成功' if ok else '❌ 失败'}\n"
        text += f"账号: {email}\n"
        text += f"信息: {summary}\n"
        
        if server_details:
            for detail in server_details:
                server_id = detail.get("id", "unknown")
                status = detail.get("status", "未知")
                text += f"服务器: {server_id}，{status}\n"
        
        text += f"时间: {cn_time()}\n"
        text += "\nFalixNodes Auto Restart"
        
        valid_screenshots = [s for s in (screenshots or []) if s and Path(s).exists()]
        
        if valid_screenshots:
            if len(valid_screenshots) == 1:
                with open(valid_screenshots[0], "rb") as f:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat_id, "caption": text},
                        files={"photo": f},
                        timeout=60
                    )
            else:
                media = []
                for idx, screenshot in enumerate(valid_screenshots[:10]):
                    if idx == 0:
                        media.append({
                            "type": "photo",
                            "media": f"attach://photo{idx}",
                            "caption": text
                        })
                    else:
                        media.append({
                            "type": "photo",
                            "media": f"attach://photo{idx}"
                        })
                
                files = {f"photo{idx}": open(screenshot, "rb") for idx, screenshot in enumerate(valid_screenshots[:10])}
                
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{token}/sendMediaGroup",
                        data={
                            "chat_id": chat_id,
                            "media": json.dumps(media)
                        },
                        files=files,
                        timeout=60
                    )
                finally:
                    for f in files.values():
                        f.close()
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=30
            )
    except Exception as e:
        print(f"[ERROR] TG通知失败: {e}")


# ---------- 解析账号 ----------
def parse_accounts() -> List[Dict]:
    raw = os.environ.get("FALIX", "")
    accounts = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "-----" not in line:
            continue
        email, pwd = line.split("-----", 1)
        accounts.append({"email": email.strip(), "password": pwd.strip()})
    return accounts


# ---------- 处理 Cookie 弹窗 ----------
def handle_cookie_consent(sb) -> bool:
    """处理 Cookie 同意弹窗"""
    try:
        selectors = [
            "#accept-choices",
            "div.sn-b-def.sn-blue",
        ]
        
        for selector in selectors:
            try:
                if sb.is_element_visible(selector, timeout=3):
                    sb.click(selector)
                    print("[INFO] Cookie 弹窗已处理")
                    time.sleep(2)
                    return True
            except:
                continue
        
        try:
            elements = sb.find_elements("div.sn-b-def, button")
            for elem in elements:
                text = elem.text.lower()
                if "accept" in text and ("all" in text or "visit" in text):
                    elem.click()
                    print("[INFO] Cookie 弹窗已处理")
                    time.sleep(2)
                    return True
        except:
            pass
            
        return False
    except Exception as e:
        print(f"[WARN] Cookie 处理异常: {e}")
        return False


# ---------- Turnstile 处理 ----------
def handle_turnstile(sb, timeout=30) -> bool:
    """主动触发 Turnstile 验证并等待完成"""
    start = time.time()
    last_click = 0
    
    while time.time() - start < timeout:
        try:
            val = sb.execute_script("""
                var el = document.querySelector("input[name='cf-turnstile-response']");
                return el ? el.value : "";
            """)
            if val and len(val) > 20:
                print("[INFO] Turnstile 验证完成")
                return True
        except:
            pass

        now = time.time()
        if now - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = now
            except:
                pass

        time.sleep(1)

    print("[WARN] Turnstile 超时")
    return False


# ---------- 处理广告弹窗 ----------
def handle_ad_modal(sb, server_id: str) -> bool:
    """检测广告弹窗，如果存在则刷新"""
    try:
        if sb.is_element_visible("#adModal", timeout=2):
            print("[WARN] 检测到广告弹窗")
            shot(sb, f"ad-{server_id[:8]}")
            sb.refresh()
            time.sleep(3)
            return True
    except:
        pass
    return False


# ---------- 获取控制台页面的服务器状态 ----------
def get_console_status(sb) -> str:
    """从控制台页面获取服务器状态（#csb-status-text）"""
    try:
        status_elem = sb.find_element("#csb-status-text", timeout=5)
        status = status_elem.text.strip().lower()
        return status
    except Exception as e:
        print(f"[WARN] 获取控制台状态失败: {e}")
        return "unknown"


def is_offline(status: str) -> bool:
    """判断是否需要重启：offline 或 unknown 都需要重启"""
    status_lower = status.lower()
    return "offline" in status_lower or "unknown" in status_lower


# ---------- 从页面解析服务器列表 ----------
def fetch_servers_from_page(sb, email: str) -> Tuple[List[Dict], str]:
    """从首页解析服务器列表，只获取ID和名称"""
    email_safe = email_to_filename(email)
    
    sb.open(BASE_URL)
    time.sleep(3)
    handle_cookie_consent(sb)
    
    last_shot = shot(sb, f"homepage-{email_safe}")
    
    try:
        sb.wait_for_element_visible(".servers-container", timeout=10)
        print("[INFO] 服务器列表容器已加载")
        last_shot = shot(sb, f"servers-loaded-{email_safe}")
    except:
        print("[ERROR] 服务器列表加载超时")
        last_shot = shot(sb, f"timeout-{email_safe}")
        return [], last_shot

    servers = []
    try:
        rows = sb.find_elements("a.server-row-link")
        print(f"[INFO] 找到 {len(rows)} 个服务器")
        
        for idx, row in enumerate(rows):
            try:
                href = row.get_attribute("href")
                if not href or "/server/" not in href:
                    continue
                
                server_id = href.split("/server/")[1].split("/")[0]
                
                # 获取名称
                name = f"Server-{server_id[:4]}"
                try:
                    name_elem = row.find_element("tag name", "h5")
                    if name_elem and name_elem.text.strip():
                        name = name_elem.text.strip()
                except:
                    pass
                
                # 修改：不打印完整ID
                print(f"[INFO] 服务器 {idx+1}: {name}")
                
                servers.append({
                    "id": server_id,
                    "name": name
                })
                
            except Exception as e:
                print(f"[ERROR] 解析第 {idx+1} 行失败: {e}")
                continue
                
    except Exception as e:
        print(f"[ERROR] 查找服务器行失败: {e}")

    last_shot = shot(sb, f"parsed-{len(servers)}servers-{email_safe}")
    print(f"[INFO] 成功解析 {len(servers)} 个服务器")
    return servers, last_shot


# ---------- 检查并重启单个服务器 ----------
def check_and_restart_server(sb, server_id: str, server_name: str) -> Tuple[bool, str, str]:
    """检查服务器状态，如果需要则重启"""
    console_url = f"{BASE_URL}/server/{server_id}/console"
    print(f"[INFO] 检查服务器: {server_name}")
    
    server_id_short = server_id[:8]
    last_shot = ""

    for attempt in range(AD_RETRY_LIMIT):
        sb.open(console_url)
        
        # 修改：添加随机延时 0-5秒
        delay = random.uniform(0, 5)
        print(f"[INFO] 随机延时 {delay:.1f}s")
        time.sleep(delay)
        
        handle_cookie_consent(sb)
        
        last_shot = shot(sb, f"console-{server_id_short}-try{attempt+1}")

        # 获取当前状态
        current_status = get_console_status(sb)
        print(f"[INFO] {server_name} 当前状态: [{current_status}]")
        
        # 如果状态正常
        if not is_offline(current_status):
            print(f"[INFO] {server_name} 状态正常")
            last_shot = shot(sb, f"ok-{server_id_short}")
            return True, f"在线 ({current_status})", last_shot

        # 需要重启
        if attempt == 0:  # 只在第一次打印
            print(f"[INFO] {server_name} 需要重启")
        
        # 点击 Start
        try:
            last_shot = shot(sb, f"before-start-{server_id_short}")
            sb.click("#startbutton", timeout=5)
            print(f"[INFO] 已点击 Start 按钮 (尝试 {attempt+1}/{AD_RETRY_LIMIT})")
            time.sleep(5)
            last_shot = shot(sb, f"after-start-{server_id_short}")
        except Exception as e:
            print(f"[WARN] 点击 Start 失败: {e}")
            last_shot = shot(sb, f"click-fail-{server_id_short}")
            pass

        # 处理广告
        if handle_ad_modal(sb, server_id):
            continue

        # 再次检查状态
        new_status = get_console_status(sb)
        print(f"[INFO] {server_name} 启动后状态: [{new_status}]")
        
        # 只要不是 offline/unknown 就算成功
        if not is_offline(new_status):
            print(f"[INFO] {server_name} 重启成功")
            last_shot = shot(sb, f"success-{server_id_short}")
            return True, f"重启成功 ({new_status})", last_shot
        
        # 继续重试前等待一下
        time.sleep(3)

    print(f"[ERROR] {server_name} 重启失败（达到重试上限）")
    last_shot = shot(sb, f"fail-{server_id_short}")
    return False, "重启失败", last_shot


# ---------- 登录并重启 ----------
def login_and_restart(email: str, password: str, proxy: Optional[str]) -> Dict:
    """登录并检查所有服务器"""
    result = {
        "success": False,
        "email": email,
        "servers_checked": 0,
        "servers_restarted": 0,
        "message": "",
        "server_details": [],
        "screenshots": []
    }
    email_log = mask_email_log(email)
    email_safe = email_to_filename(email)

    print("\n" + "=" * 60)
    print(f"[INFO] 账号: {email_log}")
    print("=" * 60)

    with SB(uc=True, test=True, locale="en", proxy=proxy, headed=not is_linux()) as sb:

        # ---------- 登录 ----------
        logged_in = False
        for attempt in range(MAX_RETRY):
            print(f"[INFO] 登录尝试 {attempt + 1}/{MAX_RETRY}")
            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10.0)
            time.sleep(3)
            handle_cookie_consent(sb)
            
            login_shot = shot(sb, f"login-{email_safe}-try{attempt+1}")

            if "/auth/login" not in sb.get_current_url():
                print("[INFO] 已处于登录状态")
                logged_in = True
                break

            try:
                sb.type("#email-address", email, timeout=5)
                sb.type("#password", password)
                print("[INFO] 登录表单已填写")
            except Exception as e:
                print(f"[ERROR] 填写表单失败: {e}")
                continue

            handle_turnstile(sb)

            try:
                sb.click("button[type='submit']", timeout=5)
                print("[INFO] 登录表单已提交")
                time.sleep(5)
            except Exception as e:
                print(f"[ERROR] 提交表单失败: {e}")
                continue

            if "/auth/login" not in sb.get_current_url():
                logged_in = True
                print("[INFO] 登录成功")
                break

        if not logged_in:
            result["message"] = "登录失败"
            result["screenshots"] = [shot(sb, f"login-fail-{email_safe}")]
            return result

        # ---------- 获取服务器列表 ----------
        servers, list_shot = fetch_servers_from_page(sb, email)
        
        if not servers:
            result["message"] = "未找到服务器"
            result["screenshots"] = [list_shot]
            return result

        # ---------- 逐个检查并重启服务器 ----------
        result["servers_checked"] = len(servers)
        restarted = 0
        
        for idx, svr in enumerate(servers, 1):
            print(f"\n[INFO] === 检查服务器 {idx}/{len(servers)} ===")
            ok, status_desc, server_shot = check_and_restart_server(sb, svr["id"], svr["name"])
            
            result["server_details"].append({
                "id": svr["id"],
                "status": status_desc
            })
            
            result["screenshots"].append(server_shot)
            
            if "重启成功" in status_desc:
                restarted += 1

        result["success"] = True
        result["servers_restarted"] = restarted
        result["message"] = f"检查 {len(servers)} 个服务器，重启 {restarted} 个"
        
        return result


# ---------- 主函数 ----------
def main():
    proxy = os.environ.get("PROXY_SERVER")
    display = None

    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            print("[INFO] 虚拟显示已启动")
        except Exception as e:
            print(f"[ERROR] 虚拟显示启动失败: {e}")
            sys.exit(1)

    accounts = parse_accounts()
    if not accounts:
        sys.exit("[ERROR] 未找到账号配置")

    print(f"[INFO] 共加载 {len(accounts)} 个账号")

    results = []
    for idx, acc in enumerate(accounts, 1):
        print(f"\n{'#' * 60}")
        print(f"# 账号 {idx}/{len(accounts)}")
        print(f"{'#' * 60}")
        
        res = login_and_restart(acc["email"], acc["password"], proxy)
        results.append(res)

        notify(
            ok=res["success"],
            email=res["email"],
            summary=res["message"],
            server_details=res.get("server_details", []),
            screenshots=res.get("screenshots", [])
        )

        if idx < len(accounts):
            delay = random.randint(10, 30)
            print(f"[INFO] 等待 {delay}s 后处理下一个账号...")
            time.sleep(delay)

    print("\n" + "=" * 60)
    print("[INFO] 全部任务完成")
    print("=" * 60)
    success_cnt = sum(1 for r in results if r["success"])
    total_checked = sum(r.get("servers_checked", 0) for r in results)
    total_restart = sum(r.get("servers_restarted", 0) for r in results)
    print(f"[INFO] 成功账号: {success_cnt}/{len(results)}")
    print(f"[INFO] 检查服务器: {total_checked} 个")
    print(f"[INFO] 重启服务器: {total_restart} 个")
    print(f"[INFO] 总截图数: {screenshot_counter['count']} 张")

    if display:
        display.stop()

    sys.exit(0 if success_cnt == len(results) else 1)


if __name__ == "__main__":
    main()
