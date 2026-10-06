# ArgoCD Telegram Notification Bot (Namespace-based Subscription)

Bot tiếp nhận thông báo triển khai từ **ArgoCD Notifications** và tự động định tuyến (routing) tin nhắn đến các nhóm Telegram theo **Namespace** mà người dùng tự đăng ký (Self-Service).

---

## 🏗 Kiến trúc hoạt động

```
┌──────────────────────┐
│  ArgoCD Controller   │
│  (Trigger event)     │
└──────────┬───────────┘
           │ Webhook (POST /webhook/argocd)
           ▼
┌──────────────────────────────────────────────┐
│        argocd-telegram-bot Service           │
│  - FastAPI Webhook Receiver                  │
│  - SQLite: lưu mapping (chat_id, namespace)  │
│  - Telegram Polling (xử lý lệnh /sub, /unsub)│
└──────────┬───────────────────────────────────┘
           │ Gửi tin nhắn định tuyến theo namespace
           ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│   Group Team A   │  │   Group Team B   │  │   Group DevOps   │
│ (sub: owlla-dev) │  │(sub:medguard-dev)│  │    (sub: all)    │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

---

## 🚀 Hướng dẫn triển khai từng bước

### Bước 1: Tạo Telegram Bot
1. Mở Telegram, chat với **[@BotFather](https://t.me/BotFather)**.
2. Gõ `/newbot` và đặt tên cho bot (ví dụ: `Thinklabs ArgoCD Bot` - `thinklabs_ih1_argocd_bot`).
3. Lưu lại **HTTP API Token** (dạng `1234567890:ABCdefGHI...`).
4. *(Tùy chọn)* Tắt privacy mode nếu muốn bot đọc tin nhắn trong group mà không cần nhắc tên:
   - Chat với `@BotFather` -> `/setprivacy` -> Chọn bot -> Chọn `Disable`.

### Bước 2: Build & Push Docker Image
Chạy lệnh build container từ thư mục này:

```bash
cd c:\Thinklabs\DEVOPS\clusters\cluster-ih1\third-party\argocd-telegram-bot

# Build image
docker build -t ghcr.io/thinklabsdev/argocd-telegram-bot:latest .

# Đăng nhập GHCR và push (hoặc push lên Harbor nội bộ của ih1)
docker push ghcr.io/thinklabsdev/argocd-telegram-bot:latest
```

### Bước 3: Cấu hình Secret & Deploy lên Cluster `ih1`

1. Sửa file `manifests/secret.yaml` với token bot bạn vừa tạo:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: argocd-telegram-bot-secret
  namespace: argocd
type: Opaque
stringData:
  TELEGRAM_BOT_TOKEN: "1234567890:ABCdefGHI..." # Token của bạn
```

2. Áp dụng manifests vào cluster:
```bash
kubectl apply -k manifests/
```

3. Kiểm tra pod hoạt động:
```bash
kubectl get pods -n argocd -l app=argocd-telegram-bot
# Trạng thái 1/1 Running
```

### Bước 4: Kích hoạt Webhook trên ArgoCD Notifications
Áp dụng cấu hình patch webhook vào ConfigMap của ArgoCD:

```bash
kubectl apply -f argocd-notifications-patch.yaml
```

Khởi động lại notifications controller để nhận cấu hình mới:
```bash
kubectl rollout restart deployment argocd-notifications-controller -n argocd
```

---

## 💬 Hướng dẫn sử dụng cho Developer & DevOps

Sau khi thêm Bot vào Group Telegram hoặc Forum Topic:

| Lệnh | Ý nghĩa | Ví dụ |
|---|---|---|
| `/namespaces` (hoặc `/ns`) | Liệt kê tất cả các namespace đang có trên ArgoCD (kèm số app) | `/namespaces` |
| `/sub <namespace>` | Nhận toàn bộ thông báo (thành công + thất bại) của namespace | `/sub owlla-dev` |
| `/sub <namespace> failed` | **Chỉ** nhận thông báo khi deploy thất bại hoặc health degraded | `/sub owlla-dev failed` |
| `/sub all` | Nhận thông báo của **tất cả** các namespaces (Dành cho DevOps) | `/sub all` |
| `/unsub <namespace>` | Hủy nhận thông báo từ namespace đã chọn | `/unsub owlla-dev` |
| `/unsub all` | Hủy toàn bộ đăng ký trong nhóm/topic hiện tại | `/unsub all` |
| `/list` | Liệt kê danh sách namespace nhóm đang theo dõi | `/list` |
| `/myid` | Xem Chat ID và Thread ID (nếu dùng Topics) của nhóm | `/myid` |
| `/ping` | Kiểm tra trạng thái hoạt động của bot | `/ping` |

> 💡 **Hỗ trợ Forum Topics:** Nếu group Telegram của bạn bật tính năng Topics (Diễn đàn), khi gõ lệnh `/sub <namespace>` trong bất kỳ Topic nào, Bot sẽ ghi nhận chính xác Topic đó và chỉ gửi thông báo vào đúng Topic đó!

---

## 🧪 Kiểm tra thử nghiệm (Manual Test)

Bạn có thể test gửi trực tiếp event giả lập từ trong cluster hoặc mở port-forward trên máy cá nhân:

```bash
kubectl port-forward svc/argocd-telegram-bot -n argocd 8080:8080
```

### 1. Test tự động lấy Pod từ ArgoCD (Khuyên dùng)
Bot sẽ tự động truy vấn ArgoCD và Kubernetes để lấy đúng các Pod vừa được sync:
```bash
curl -X POST http://localhost:8080/webhook/argocd \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "ew-dev",
    "namespace": "ew-dev",
    "project": "default",
    "sync_status": "Synced",
    "health_status": "Healthy",
    "revision": "8a60ee1",
    "message": "Manual test trigger from local curl"
  }'
```

### 2. Test chỉ định cụ thể Pod Name
Nếu muốn chỉ định rõ pod cần hiển thị trong thông báo:
```bash
curl -X POST http://localhost:8080/webhook/argocd \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "ew-dev",
    "namespace": "ew-dev",
    "project": "default",
    "sync_status": "Synced",
    "health_status": "Healthy",
    "pod_name": "ew-webview-dev-559cc98866-htndz",
    "revision": "8a60ee1",
    "message": "Deploy update webview only"
  }'
```

### 3. Test thông báo lỗi Sync / Pod Degraded (🚨)
Kiểm tra thông báo cảnh báo đỏ khi triển khai thất bại:
```bash
curl -X POST http://localhost:8080/webhook/argocd \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "owlla-dev",
    "namespace": "owlla-dev",
    "project": "default",
    "sync_status": "Failed",
    "health_status": "Degraded",
    "revision": "a1b2c3d",
    "message": "Back-off restarting failed container backend-dev in pod backend-dev-6bc58b755c-hgs8p"
  }'
```

> 💡 **Kết quả:** Bot sẽ lập tức định tuyến gửi tin nhắn thông báo đến tất cả các Group Telegram hoặc Topic đã gõ lệnh `/sub <namespace>` tương ứng!
