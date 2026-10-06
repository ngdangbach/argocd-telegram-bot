import os
import asyncio
import logging
import html
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from pydantic import BaseModel
import httpx

from database import (
    init_db,
    add_subscription,
    remove_subscription,
    list_subscriptions,
    get_subscribers_for_event,
    record_seen_namespace,
    get_cached_namespaces
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("argocd-telegram-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ARGOCD_URL = os.getenv("ARGOCD_URL", "https://argocd.internal.ih1.thinklabs.com.vn").rstrip("/")

if not TELEGRAM_BOT_TOKEN:
    logger.warning("WARNING: TELEGRAM_BOT_TOKEN is not set! Bot will not be able to interact with Telegram.")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# -------------------------------------------------------------
# Data models
# -------------------------------------------------------------
class ArgoWebhookPayload(BaseModel):
    app_name: str
    namespace: Optional[str] = "default"
    project: Optional[str] = "default"
    sync_status: Optional[str] = "Unknown"
    health_status: Optional[str] = "Unknown"
    phase: Optional[str] = ""
    revision: Optional[str] = ""
    message: Optional[str] = ""
    trigger: Optional[str] = ""
    pod_name: Optional[str] = None
    pod: Optional[str] = None
    pods: Optional[Any] = None


BOT_COMMANDS = [
    {"command": "sub", "description": "Đăng ký nhận thông báo (vd: /sub owlla-dev)"},
    {"command": "unsub", "description": "Hủy nhận thông báo namespace"},
    {"command": "list", "description": "Danh sách namespace đang theo dõi"},
    {"command": "namespaces", "description": "Xem danh sách namespace trên ArgoCD"},
    {"command": "ns", "description": "Xem nhanh danh sách namespace"},
    {"command": "myid", "description": "Xem Chat ID và Topic ID của nhóm"},
    {"command": "help", "description": "Xem hướng dẫn chi tiết"},
    {"command": "ping", "description": "Kiểm tra kết nối của bot"},
]

async def register_bot_commands():
    """Tự động đăng ký menu lệnh với Telegram để hiển thị khi gõ '/'"""
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"{TELEGRAM_API_BASE}/setMyCommands"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"commands": BOT_COMMANDS})
            if resp.status_code == 200:
                logger.info("Bot commands registered successfully with Telegram.")
            else:
                logger.warning(f"Failed to register commands: {resp.text}")
    except Exception as e:
        logger.error(f"Error registering bot commands: {e}")


# -------------------------------------------------------------
# Telegram API Helpers
# -------------------------------------------------------------
async def send_telegram_message(
    chat_id: int,
    text: str,
    thread_id: Optional[int] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: str = "HTML"
) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Telegram API error {resp.status_code} sending to {chat_id}: {resp.text}")
                return False
            return True
    except Exception as e:
        logger.error(f"Failed to send telegram message to {chat_id}: {e}")
        return False


async def get_argocd_namespaces() -> Dict[str, List[str]]:
    """Lấy danh sách các namespace và danh sách app từ K8s in-cluster API hoặc cache database."""
    token_file = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_file = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if os.path.exists(token_file):
        try:
            with open(token_file, "r") as f:
                token = f.read().strip()
            url = "https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"
            async with httpx.AsyncClient(verify=ca_file, timeout=6.0) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                if resp.status_code == 200:
                    data = resp.json()
                    result: Dict[str, List[str]] = {}
                    for item in data.get("items", []):
                        app_name = item.get("metadata", {}).get("name", "")
                        ns = (item.get("spec", {}).get("destination", {}).get("namespace") or "default").strip().lower()
                        result.setdefault(ns, []).append(app_name)
                        record_seen_namespace(ns, app_name)
                    return dict(sorted(result.items()))
        except Exception as e:
            logger.warning(f"Could not query K8s API directly: {e}")

    # Fallback: đọc từ database đã lưu lại từ các event
    cached = get_cached_namespaces()
    result = {}
    for row in cached:
        ns = row["namespace"]
        apps = [a for a in (row["app_names"] or "").split(",") if a]
        result[ns] = apps
    return result


async def get_pods_for_app(namespace: str, app_name: Optional[str] = None) -> List[str]:
    """Lấy danh sách các Pod đang chạy trong namespace của ứng dụng."""
    token_file = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_file = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if not os.path.exists(token_file) or not namespace:
        return []

    try:
        with open(token_file, "r") as f:
            token = f.read().strip()
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/pods"
        async with httpx.AsyncClient(verify=ca_file, timeout=4.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                data = resp.json()
                all_pods = []
                matched_pods = []
                app_clean = (app_name or "").lower().replace("-dev", "").replace("-prod", "").replace("-staging", "")

                for item in data.get("items", []):
                    p_name = item.get("metadata", {}).get("name", "")
                    if not p_name:
                        continue
                    phase = item.get("status", {}).get("phase", "")
                    if phase not in ["Running", "Pending", "CrashLoopBackOff"]:
                        continue

                    all_pods.append(p_name)
                    # Ưu tiên các pod có tên khớp với app_name
                    if app_name and (app_name.lower() in p_name.lower() or (app_clean and app_clean in p_name.lower())):
                        matched_pods.append(p_name)

                result = matched_pods if matched_pods else all_pods
                return sorted(result)
    except Exception as e:
        logger.warning(f"Error fetching pods for namespace {namespace}: {e}")
    return []


# -------------------------------------------------------------
# Telegram Bot Command Processing
# -------------------------------------------------------------
HELP_TEXT = """🤖 <b>ArgoCD Notification Bot (ih1)</b>

<b>Danh sách lệnh:</b>
• <code>/namespaces</code> (hoặc <code>/ns</code>) : Xem các namespace đang có trên ArgoCD.
• <code>/sub &lt;namespace&gt;</code> : Đăng ký nhận toàn bộ thông báo của namespace.
• <code>/sub &lt;namespace&gt; failed</code> : Chỉ nhận thông báo khi deploy thất bại/lỗi.
• <code>/sub all</code> : Nhận thông báo của <b>tất cả</b> namespaces (DevOps).
• <code>/unsub &lt;namespace&gt;</code> : Hủy nhận thông báo của namespace.
• <code>/unsub all</code> : Hủy toàn bộ đăng ký trong chat này.
• <code>/list</code> : Xem các namespace chat/topic này đang theo dõi.
• <code>/myid</code> : Xem Chat ID và Topic ID hiện tại.
• <code>/ping</code> : Kiểm tra bot còn hoạt động không.

<i>Ví dụ:</i>
<code>/namespaces</code>
<code>/sub owlla-dev</code>
<code>/sub medguard-dev failed</code>
"""

async def process_telegram_message(message: Dict[str, Any]):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_title = chat.get("title") or chat.get("username") or chat.get("first_name", "Unknown")
    thread_id = message.get("message_thread_id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text.startswith("/"):
        return

    # Tách command và args
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()  # Bỏ @bot_username nếu có
    args = parts[1:]

    if cmd in ["/start", "/help"]:
        await send_telegram_message(chat_id, HELP_TEXT, thread_id)

    elif cmd == "/ping":
        await send_telegram_message(chat_id, "🏓 <b>Pong!</b> ArgoCD Bot đang hoạt động bình thường.", thread_id)

    elif cmd == "/myid":
        info = f"🆔 <b>Thông tin Chat:</b>\n• Chat ID: <code>{chat_id}</code>\n• Chat Title: <b>{html.escape(str(chat_title))}</b>"
        if thread_id:
            info += f"\n• Topic ID (Thread): <code>{thread_id}</code>"
        await send_telegram_message(chat_id, info, thread_id)

    elif cmd == "/sub":
        if not args:
            await send_telegram_message(
                chat_id, 
                "⚠️ <b>Thiếu tên namespace!</b>\nCú pháp: <code>/sub &lt;namespace&gt; [failed]</code>\nVí dụ: <code>/sub owlla-dev</code>", 
                thread_id
            )
            return

        ns = args[0]
        level = "failed" if len(args) > 1 and args[1].lower() in ["failed", "fail", "error"] else "all"

        success = add_subscription(chat_id, thread_id, ns, level, chat_title)
        if success:
            level_desc = "chỉ khi có lỗi (failed/degraded)" if level == "failed" else "toàn bộ (thành công + thất bại)"
            topic_str = f" tại Topic <code>{thread_id}</code>" if thread_id else ""
            msg = (
                f"✅ <b>Đã đăng ký thành công!</b>\n"
                f"• Namespace: <code>{html.escape(ns)}</code>\n"
                f"• Chế độ: <i>{level_desc}</i>\n"
                f"• Nhóm/Chat: <b>{html.escape(str(chat_title))}</b>{topic_str}"
            )
        else:
            msg = "❌ Có lỗi xảy ra khi lưu đăng ký vào database. Vui lòng thử lại sau."
        await send_telegram_message(chat_id, msg, thread_id)

    elif cmd == "/unsub":
        if not args:
            await send_telegram_message(
                chat_id, 
                "⚠️ <b>Thiếu tên namespace cần hủy!</b>\nCú pháp: <code>/unsub &lt;namespace&gt;</code>\nVí dụ: <code>/unsub owlla-dev</code>", 
                thread_id
            )
            return

        ns = args[0]
        deleted_count = remove_subscription(chat_id, thread_id, ns)
        if deleted_count > 0:
            msg = f"🗑 <b>Đã hủy theo dõi</b> thành công cho namespace: <code>{html.escape(ns)}</code>"
        else:
            msg = f"ℹ️ Nhóm này chưa đăng ký nhận tin của namespace <code>{html.escape(ns)}</code>."
        await send_telegram_message(chat_id, msg, thread_id)

    elif cmd == "/list":
        subs = list_subscriptions(chat_id, thread_id)
        if not subs:
            topic_str = f" tại Topic {thread_id}" if thread_id else ""
            await send_telegram_message(
                chat_id, 
                f"📭 Hiện tại chat này{topic_str} chưa đăng ký nhận thông báo từ namespace nào.\nSử dụng <code>/sub &lt;namespace&gt;</code> để đăng ký.", 
                thread_id
            )
            return

        lines = [f"📋 <b>Danh sách namespace đang theo dõi:</b>"]
        for s in subs:
            level_badge = "🔴 Chỉ báo lỗi" if s["notify_level"] == "failed" else "🟢 Toàn bộ"
            lines.append(f"• <code>{html.escape(s['namespace'])}</code> ({level_badge})")
        
        await send_telegram_message(chat_id, "\n".join(lines), thread_id)

    elif cmd in ["/namespaces", "/ns"]:
        ns_map = await get_argocd_namespaces()
        if not ns_map:
            await send_telegram_message(
                chat_id,
                "ℹ️ <b>Chưa có dữ liệu namespace từ ArgoCD.</b>\n<i>(Khi có event sync đầu tiên hoặc cấp quyền đọc K8s, bot sẽ tự động cập nhật danh sách).</i>",
                thread_id
            )
            return

        lines = [f"🏷️ <b>Danh sách Namespaces trên ArgoCD ({len(ns_map)}):</b>\n"]
        for ns, apps in ns_map.items():
            if apps:
                app_count = len(apps)
                preview = ", ".join(apps[:3])
                if len(apps) > 3:
                    preview += f", +{len(apps)-3}..."
                lines.append(f"• <code>{html.escape(ns)}</code> ({app_count} apps: <i>{html.escape(preview)}</i>)")
            else:
                lines.append(f"• <code>{html.escape(ns)}</code>")

        lines.append("\n💡 <i>Gõ <code>/sub &lt;namespace&gt;</code> để đăng ký nhận thông báo.</i>")
        await send_telegram_message(chat_id, "\n".join(lines), thread_id)


# -------------------------------------------------------------
# Background Telegram Long-Polling Loop
# -------------------------------------------------------------
async def telegram_polling_worker():
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured, skipping Telegram polling worker.")
        return

    logger.info("Starting Telegram long-polling worker...")
    offset = 0
    while True:
        try:
            url = f"{TELEGRAM_API_BASE}/getUpdates"
            params = {"offset": offset, "timeout": 20, "allowed_updates": ["message"]}
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "message" in update:
                            await process_telegram_message(update["message"])
                elif resp.status_code == 409:
                    logger.warning("Conflict: another bot instance is polling with same token. Waiting 10s...")
                    await asyncio.sleep(10)
                else:
                    logger.warning(f"Polling HTTP {resp.status_code}: {resp.text}")
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            logger.info("Polling worker cancelled.")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(4)


# -------------------------------------------------------------
# Application Lifespan
# -------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    await register_bot_commands()
    polling_task = asyncio.create_task(telegram_polling_worker())
    yield
    # Shutdown
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="ArgoCD Telegram Bot", lifespan=lifespan)


# -------------------------------------------------------------
# Webhook Receiver from ArgoCD Notifications
# -------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhook/argocd", status_code=status.HTTP_200_OK)
async def handle_argocd_webhook(payload: ArgoWebhookPayload):
    app_name = payload.app_name or "unknown"
    namespace = (payload.namespace or "default").strip()
    project = payload.project or "default"
    sync_status = payload.sync_status or "Unknown"
    health_status = payload.health_status or "Unknown"
    revision = payload.revision or ""
    message = payload.message or ""
    phase = payload.phase or ""

    logger.info(f"Received ArgoCD event: app={app_name}, namespace={namespace}, sync={sync_status}, health={health_status}")

    # Ghi nhận namespace và app_name vào cache
    record_seen_namespace(namespace, app_name)

    # Xác định mức độ nghiêm trọng
    is_failed = False
    if (
        sync_status.lower() in ["failed", "unknown"] 
        or health_status.lower() in ["degraded", "missing"] 
        or phase.lower() in ["failed", "error"]
    ):
        is_failed = True

    # Biểu tượng trạng thái
    if is_failed:
        header_icon = "🚨"
        status_title = "DEPLOYMENT / SYNC THẤT BẠI"
    elif sync_status.lower() == "synced" and health_status.lower() == "healthy":
        header_icon = "🚀"
        status_title = "DEPLOYMENT THÀNH CÔNG"
    else:
        header_icon = "🔄"
        status_title = f"CẬP NHẬT TRẠNG THÁI ({sync_status})"

    # Lấy thông tin Pods từ payload hoặc tự động query K8s API
    pod_list = []
    if payload.pod_name:
        pod_list.append(str(payload.pod_name).strip())
    elif payload.pod:
        pod_list.append(str(payload.pod).strip())
    elif payload.pods:
        if isinstance(payload.pods, list):
            pod_list.extend([str(p).strip() for p in payload.pods if p])
        elif isinstance(payload.pods, str):
            pod_list.extend([p.strip() for p in payload.pods.split(",") if p.strip()])

    if not pod_list and namespace:
        pod_list = await get_pods_for_app(namespace, app_name)

    # Format message HTML
    short_rev = revision[:7] if len(revision) >= 7 else revision
    argo_app_url = f"{ARGOCD_URL}/applications/{app_name}"

    lines = [
        f"{header_icon} <b>ArgoCD ih1: {status_title}</b>",
        "",
        f"📦 <b>Ứng dụng:</b> <code>{html.escape(app_name)}</code>",
        f"🏷 <b>Namespace:</b> <code>{html.escape(namespace)}</code>",
    ]

    if pod_list:
        if len(pod_list) == 1:
            lines.append(f"🐳 <b>Pod:</b> <code>{html.escape(pod_list[0])}</code>")
        elif len(pod_list) <= 3:
            pods_formatted = ", ".join([f"<code>{html.escape(p)}</code>" for p in pod_list])
            lines.append(f"🐳 <b>Pods:</b> {pods_formatted}")
        else:
            pods_formatted = ", ".join([f"<code>{html.escape(p)}</code>" for p in pod_list[:3]])
            lines.append(f"🐳 <b>Pods:</b> {pods_formatted} <i>(+{len(pod_list)-3} pods)</i>")

    lines.extend([
        f"📁 <b>Project:</b> <code>{html.escape(project)}</code>",
        f"⚙️ <b>Sync:</b> <code>{html.escape(sync_status)}</code> | 🩺 <b>Health:</b> <code>{html.escape(health_status)}</code>"
    ])

    if short_rev:
        lines.append(f"🔖 <b>Commit:</b> <code>{html.escape(short_rev)}</code>")

    if message and message.strip() and message.strip().lower() != "none":
        clean_msg = message.strip()
        if len(clean_msg) > 300:
            clean_msg = clean_msg[:300] + "..."
        lines.append(f"📝 <b>Chi tiết:</b>\n<pre>{html.escape(clean_msg)}</pre>")

    msg_text = "\n".join(lines)

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "🌐 Mở trong ArgoCD", "url": argo_app_url}
            ]
        ]
    }

    # Tìm danh sách chat_id đã subscribe namespace này
    subscribers = get_subscribers_for_event(namespace, is_failed)
    if not subscribers:
        logger.info(f"No subscribers found for namespace '{namespace}' (is_failed={is_failed})")
        return {"status": "success", "sent_count": 0, "subscribers": 0}

    logger.info(f"Delivering notification to {len(subscribers)} subscribers for namespace '{namespace}'")
    sent_count = 0
    for sub in subscribers:
        ok = await send_telegram_message(
            chat_id=sub["chat_id"],
            text=msg_text,
            thread_id=sub["thread_id"],
            reply_markup=reply_markup
        )
        if ok:
            sent_count += 1

    return {"status": "success", "sent_count": sent_count, "subscribers": len(subscribers)}
