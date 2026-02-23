import discord
from discord import app_commands
import requests
import threading
import asyncio
import json
import time
from datetime import datetime, timezone

BOT_TOKEN = "MTQ3NTQ3MzA0MDc4NjU4NzY3OA.G1c9R5.UIE9f_XsoyzBpXomEfniCtQTu2y8IpgnMm54sY"
BASE_URL  = "https://altare.sh"

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# users[user_id] = { acc_name: Account }
users = {}


def log(name, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{name}] {msg}")


class Account:
    def __init__(self, user_id, acc_name, token, tenant_id, webhook, cfg):
        self.user_id            = user_id
        self.acc_name           = acc_name
        self.token              = token if token.startswith("Bearer ") else f"Bearer {token}"
        self.tenant_id          = tenant_id
        self.webhook            = webhook
        self.heartbeat_interval = cfg.get("heartbeat_interval", 30)
        self.stats_interval     = cfg.get("stats_interval", 60)
        self.notify_interval    = cfg.get("notify_interval_seconds", 10)
        self.bar_max            = 100000
        self.running            = False
        self.session_start      = None
        self.credits_start      = 0
        self.balance            = 0
        self.hb_ok              = 0
        self.hb_fail            = 0
        self.message_id         = None
        self.notify_count       = 0

    def h(self):
        h = {
            "Authorization": self.token,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Origin":        BASE_URL,
            "Referer":       f"{BASE_URL}/billing/rewards/afk",
            "User-Agent":    "Mozilla/5.0"
        }
        if self.tenant_id:
            h["altare-selected-tenant-id"] = self.tenant_id
        return h

    def detect_tenant(self):
        try:
            r = requests.get(f"{BASE_URL}/api/tenants", headers=self.h(), timeout=10)
            if r.status_code == 200:
                data  = r.json()
                items = data.get("items", data) if isinstance(data, dict) else data
                if items:
                    return items[0].get("id") or items[0].get("tenantId")
        except:
            pass
        return None

    def get_balance(self):
        try:
            r = requests.get(f"{BASE_URL}/api/tenants", headers=self.h(), timeout=10)
            if r.status_code == 200:
                items = r.json()
                items = items.get("items", items) if isinstance(items, dict) else items
                for item in items:
                    if item.get("id") == self.tenant_id:
                        cents = item.get("creditsCents")
                        return round(cents / 100, 4) if cents is not None else None
                if items:
                    cents = items[0].get("creditsCents")
                    return round(cents / 100, 4) if cents is not None else None
        except:
            pass
        return None

    def get_per_minute(self):
        try:
            r = requests.get(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards", headers=self.h(), timeout=10)
            if r.status_code == 200:
                data = r.json()
                afk  = data.get("afk") if isinstance(data.get("afk"), dict) else {}
                return afk.get("perMinute") or data.get("perMinute") or 0.35
        except:
            pass
        return 0.35

    def send_heartbeat(self):
        try:
            r = requests.post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/heartbeat",
                              headers=self.h(), json={}, timeout=10)
            return r.status_code in (200, 201, 204)
        except:
            return False

    def start_afk_api(self):
        try:
            r = requests.post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/start",
                              headers=self.h(), json={}, timeout=10)
            return r.status_code in (200, 201, 204)
        except:
            return False

    def stop_afk_api(self):
        try:
            requests.post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/stop",
                          headers=self.h(), json={}, timeout=10)
        except:
            pass

    def progress_bar(self, length=20):
        earned = max(self.balance - self.credits_start, 0)
        filled = min(int(earned / self.bar_max * length), length)
        return "█" * filled + "░" * (length - filled)

    def send_discord(self):
        if not self.webhook:
            return
        earned  = round(self.balance - self.credits_start, 4) if self.credits_start else 0
        elapsed = str(datetime.now() - self.session_start).split(".")[0] if self.session_start else "?"
        hb_rate = round(self.hb_ok / max(self.hb_ok + self.hb_fail, 1) * 100)
        per_min = self.get_per_minute()
        self.notify_count += 1

        payload = {
            "username": "Altare AFK",
            "embeds": [{
                "title": self.acc_name,
                "color": 0x00d4aa,
                "fields": [
                    {"name": "Số dư",       "value": f"```{self.balance:.4f} cr\n{self.progress_bar()} {earned:.4f}/{self.bar_max} cr```", "inline": False},
                    {"name": "Mỗi phút",    "value": f"```{per_min} cr/min```", "inline": True},
                    {"name": "Thời gian",   "value": f"```{elapsed}```",        "inline": True},
                    {"name": "Heartbeat",   "value": f"```OK: {self.hb_ok} | Fail: {self.hb_fail} | {hb_rate}%```", "inline": False},
                ],
                "footer": {"text": f"#{self.notify_count} | {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"},
                "timestamp": datetime.now(tz=timezone.utc).isoformat()
            }]
        }

        try:
            if self.message_id is None:
                r = requests.post(self.webhook + "?wait=true", json=payload, timeout=10)
                if r.status_code in (200, 204):
                    self.message_id = r.json().get("id")
            else:
                r = requests.patch(f"{self.webhook}/messages/{self.message_id}", json=payload, timeout=10)
                if r.status_code not in (200, 204):
                    self.message_id = None
        except:
            self.message_id = None

    def _heartbeat_loop(self):
        while self.running:
            if self.send_heartbeat():
                self.hb_ok += 1
            else:
                self.hb_fail += 1
                log(self.acc_name, f"heartbeat thất bại ({self.hb_fail})")
            time.sleep(self.heartbeat_interval)

    def _stats_loop(self):
        while self.running:
            bal = self.get_balance()
            if bal is not None:
                if not self.credits_start:
                    self.credits_start = bal
                self.balance = bal
                earned  = round(bal - self.credits_start, 4)
                elapsed = str(datetime.now() - self.session_start).split(".")[0]
                hb_rate = round(self.hb_ok / max(self.hb_ok + self.hb_fail, 1) * 100)
                log(self.acc_name, f"{bal:.4f} cr | +{earned:.4f} | {elapsed} | hb {hb_rate}%")
            time.sleep(self.stats_interval)

    def _notify_loop(self):
        time.sleep(3)
        while self.running:
            self.send_discord()
            time.sleep(self.notify_interval)

    def _sse_loop(self):
        raw  = self.token.replace("Bearer ", "")
        url  = f"https://api.altare.sh/subscribe?token={raw}"
        hdrs = {
            "Accept": "text/event-stream", "Cache-Control": "no-cache",
            "Authorization": self.token, "Origin": BASE_URL, "User-Agent": "Mozilla/5.0"
        }
        while self.running:
            try:
                with requests.get(url, headers=hdrs, stream=True, timeout=(15, None)) as r:
                    if r.status_code == 200:
                        for _ in r.iter_lines(chunk_size=1):
                            if not self.running:
                                break
                    else:
                        time.sleep(15)
            except:
                if self.running:
                    time.sleep(15)
            if self.running:
                time.sleep(5)

    def start(self):
        if not self.tenant_id:
            self.tenant_id = self.detect_tenant()
        if not self.tenant_id:
            return False, "Không tìm được tenant ID"
        if not self.start_afk_api():
            return False, "Gọi API start AFK thất bại"
        self.running       = True
        self.session_start = datetime.now()
        for fn in [self._sse_loop, self._heartbeat_loop, self._stats_loop, self._notify_loop]:
            threading.Thread(target=fn, daemon=True).start()
        log(self.acc_name, "đã bắt đầu")
        return True, "OK"

    def stop(self):
        self.running = False
        self.stop_afk_api()
        log(self.acc_name, "đã dừng")


def get_user_accs(uid):
    return users.get(uid, {})


def acc_choices(uid):
    return [
        app_commands.Choice(name=name, value=name)
        for name in get_user_accs(uid).keys()
    ]


@client.event
async def on_ready():
    await tree.sync()
    print(f"Bot sẵn sàng: {client.user}")


@tree.command(name="thêm", description="Gửi file JSON để thêm và chạy một tài khoản AFK mới")
async def cmd_them(interaction: discord.Interaction, file: discord.Attachment):
    uid = interaction.user.id
    if not file.filename.endswith(".json"):
        await interaction.response.send_message("Chỉ chấp nhận file `.json`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        cfg = json.loads(await file.read())
    except:
        await interaction.followup.send("File JSON không hợp lệ.", ephemeral=True)
        return

    token = cfg.get("token", "").strip()
    if not token:
        await interaction.followup.send("Thiếu `token` trong file JSON.", ephemeral=True)
        return

    acc_name = cfg.get("name", file.filename.replace(".json", "")).strip()

    if uid not in users:
        users[uid] = {}

    if acc_name in users[uid]:
        await interaction.followup.send(f"Tài khoản `{acc_name}` đang chạy rồi. Hãy đặt tên khác hoặc dừng cái cũ trước.", ephemeral=True)
        return

    acc = Account(uid, acc_name, token,
                  cfg.get("tenant_id", "").strip(),
                  cfg.get("discord_webhook", "").strip(), cfg)

    ok, msg = await asyncio.get_event_loop().run_in_executor(None, acc.start)
    if not ok:
        await interaction.followup.send(f"Lỗi: {msg}", ephemeral=True)
        return

    users[uid][acc_name] = acc
    await interaction.followup.send(
        f"Đã bắt đầu AFK cho **{acc_name}**!\n"
        f"Tenant: `{acc.tenant_id[:16]}...`\n"
        f"Heartbeat: `{acc.heartbeat_interval}s` | Stats: `{acc.stats_interval}s` | Notify: `{acc.notify_interval}s`\n"
        f"Dùng `/danh-sách` để xem tất cả tài khoản của bạn.",
        ephemeral=True
    )


@tree.command(name="dừng", description="Dừng và xoá một tài khoản AFK")
@app_commands.describe(tên="Tên tài khoản muốn dừng")
async def cmd_dung(interaction: discord.Interaction, tên: str):
    uid  = interaction.user.id
    accs = get_user_accs(uid)

    if tên not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tên}`.\nDùng `/danh-sách` để xem.", ephemeral=True)
        return

    accs[tên].stop()
    del accs[tên]
    if not accs:
        del users[uid]

    await interaction.response.send_message(f"Đã dừng và xoá tài khoản **{tên}**.", ephemeral=True)


@tree.command(name="danh-sách", description="Xem tất cả tài khoản AFK của bạn")
async def cmd_danh_sach(interaction: discord.Interaction):
    uid  = interaction.user.id
    accs = get_user_accs(uid)

    if not accs:
        await interaction.response.send_message("Bạn chưa có tài khoản AFK nào đang chạy.\nDùng `/thêm` để bắt đầu.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Tài khoản AFK của bạn ({len(accs)} acc)",
        color=0x00d4aa
    )

    for name, acc in accs.items():
        earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
        elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
        hb_rate = round(acc.hb_ok / max(acc.hb_ok + acc.hb_fail, 1) * 100)
        embed.add_field(
            name=name,
            value=(
                f"Số dư: `{acc.balance:.4f} cr` (+{earned:.4f})\n"
                f"Uptime: `{elapsed}` | Heartbeat: `{hb_rate}% OK`"
            ),
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="trạng-thái", description="Xem chi tiết một tài khoản AFK")
@app_commands.describe(tên="Tên tài khoản muốn xem")
async def cmd_trang_thai(interaction: discord.Interaction, tên: str):
    uid  = interaction.user.id
    accs = get_user_accs(uid)

    if tên not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tên}`.\nDùng `/danh-sách` để xem.", ephemeral=True)
        return

    acc     = accs[tên]
    earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
    elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
    hb_rate = round(acc.hb_ok / max(acc.hb_ok + acc.hb_fail, 1) * 100)

    embed = discord.Embed(title=tên, color=0x00d4aa)
    embed.add_field(name="Số dư",      value=f"`{acc.balance:.4f} cr`",   inline=True)
    embed.add_field(name="Kiếm được",  value=f"`+{earned:.4f} cr`",        inline=True)
    embed.add_field(name="Uptime",     value=f"`{elapsed}`",               inline=True)
    embed.add_field(name="Heartbeat",  value=f"`{hb_rate}% OK`",           inline=True)
    embed.add_field(name="HB OK/Fail", value=f"`{acc.hb_ok}/{acc.hb_fail}`", inline=True)
    embed.add_field(name="Tenant",     value=f"`{acc.tenant_id[:16]}...`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="cài-đặt", description="Chỉnh cài đặt cho một tài khoản AFK")
@app_commands.describe(
    tên="Tên tài khoản",
    heartbeat="Heartbeat mỗi N giây (tối thiểu 10)",
    stats="In log mỗi N giây (tối thiểu 10)",
    notify="Cập nhật Discord mỗi N giây (tối thiểu 5)",
    webhook="Đổi Discord webhook mới"
)
async def cmd_cai_dat(
    interaction: discord.Interaction,
    tên: str,
    heartbeat: int = None,
    stats: int     = None,
    notify: int    = None,
    webhook: str   = None
):
    uid  = interaction.user.id
    accs = get_user_accs(uid)

    if tên not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tên}`.", ephemeral=True)
        return

    acc, changes = accs[tên], []

    if heartbeat is not None:
        if heartbeat < 10:
            await interaction.response.send_message("Heartbeat tối thiểu 10 giây.", ephemeral=True)
            return
        acc.heartbeat_interval = heartbeat
        changes.append(f"heartbeat={heartbeat}s")

    if stats is not None:
        if stats < 10:
            await interaction.response.send_message("Stats tối thiểu 10 giây.", ephemeral=True)
            return
        acc.stats_interval = stats
        changes.append(f"stats={stats}s")

    if notify is not None:
        if notify < 5:
            await interaction.response.send_message("Notify tối thiểu 5 giây.", ephemeral=True)
            return
        acc.notify_interval = notify
        changes.append(f"notify={notify}s")

    if webhook is not None:
        acc.webhook    = webhook
        acc.message_id = None
        changes.append("webhook đã cập nhật")

    if not changes:
        await interaction.response.send_message("Không có thông số nào được thay đổi.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Đã cập nhật **{tên}**: {', '.join(changes)}", ephemeral=True)


@tree.command(name="khởi-động-lại", description="Khởi động lại phiên AFK của một tài khoản")
@app_commands.describe(tên="Tên tài khoản muốn restart")
async def cmd_restart(interaction: discord.Interaction, tên: str):
    uid  = interaction.user.id
    accs = get_user_accs(uid)

    if tên not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tên}`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    acc = accs[tên]
    acc.stop()
    time.sleep(2)

    acc.running = False
    acc.session_start = None
    acc.credits_start = 0
    acc.balance       = 0
    acc.hb_ok         = 0
    acc.hb_fail       = 0
    acc.message_id    = None
    acc.notify_count  = 0

    ok, msg = await asyncio.get_event_loop().run_in_executor(None, acc.start)
    if not ok:
        await interaction.followup.send(f"Restart thất bại: {msg}", ephemeral=True)
        return
    await interaction.followup.send(f"Đã khởi động lại **{tên}** thành công.", ephemeral=True)


@tree.command(name="tổng-quan", description="[Admin] Xem tất cả acc của tất cả người dùng")
async def cmd_tong_quan(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    total = sum(len(accs) for accs in users.values())
    if total == 0:
        await interaction.response.send_message("Không có tài khoản nào đang chạy.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Tổng quan — {total} acc đang chạy", color=0x00d4aa)

    for uid, accs in users.items():
        lines = []
        for name, acc in accs.items():
            earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
            elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
            lines.append(f"`{name}` — {acc.balance:.4f} cr (+{earned:.4f}) — {elapsed}")
        embed.add_field(name=f"<@{uid}>", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="dừng-admin", description="[Admin] Dừng một tài khoản cụ thể của bất kỳ user nào")
@app_commands.describe(user="User cần dừng", tên="Tên tài khoản cần dừng")
async def cmd_dung_admin(interaction: discord.Interaction, user: discord.Member, tên: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    uid  = user.id
    accs = get_user_accs(uid)

    if tên not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tên}` của {user.display_name}.", ephemeral=True)
        return

    accs[tên].stop()
    del accs[tên]
    if not accs:
        del users[uid]

    await interaction.response.send_message(
        f"Đã dừng tài khoản **{tên}** của {user.display_name}.", ephemeral=True)


@tree.command(name="trợ-giúp", description="Hướng dẫn sử dụng bot")
async def cmd_tro_giup(interaction: discord.Interaction):
    embed = discord.Embed(title="Altare AFK Bot — Hướng dẫn", color=0x00d4aa)

    embed.add_field(name="Bước 1 — Tạo file config.json", value=(
        "```json\n"
        "{\n"
        '  "name": "Tên tài khoản",\n'
        '  "token": "Bearer eyJ...",\n'
        '  "tenant_id": "",\n'
        '  "discord_webhook": "https://discord.com/api/webhooks/...",\n'
        '  "heartbeat_interval": 30,\n'
        '  "stats_interval": 60,\n'
        '  "notify_interval_seconds": 10\n'
        "}\n```"
        "Trường `tenant_id` có thể để trống, bot tự tìm.\n"
        "Trường `name` dùng để phân biệt nhiều tài khoản."
    ), inline=False)

    embed.add_field(name="Bước 2 — Lấy token", value=(
        "1. Mở `altare.sh`, đăng nhập\n"
        "2. Nhấn `F12` → tab `Network`\n"
        "3. Click bất kỳ request nào tới `altare.sh`\n"
        "4. Tìm header `Authorization` → copy giá trị `Bearer eyJ...`\n"
        "5. Dán vào trường `token` trong file JSON"
    ), inline=False)

    embed.add_field(name="Lệnh cá nhân", value=(
        "`/thêm` + đính kèm file JSON — Thêm và chạy một tài khoản AFK mới\n"
        "`/dừng [tên]` — Dừng và xoá một tài khoản\n"
        "`/danh-sách` — Xem tất cả tài khoản đang chạy\n"
        "`/trạng-thái [tên]` — Xem chi tiết một tài khoản\n"
        "`/khởi-động-lại [tên]` — Restart một tài khoản\n"
        "`/cài-đặt [tên]` — Chỉnh heartbeat, stats, notify, webhook"
    ), inline=False)

    embed.add_field(name="Lệnh Admin", value=(
        "`/tổng-quan` — Xem tất cả acc của tất cả người dùng\n"
        "`/dừng-admin @user [tên]` — Dừng một acc cụ thể của bất kỳ ai"
    ), inline=False)

    embed.add_field(name="Lưu ý", value=(
        "— Mỗi người có thể chạy nhiều tài khoản cùng lúc, mỗi file JSON là một tài khoản\n"
        "— Trường `name` trong JSON là tên dùng cho các lệnh quản lý\n"
        "— Token hết hạn phải lấy lại từ DevTools và `/thêm` lại\n"
        "— Mọi reply chỉ hiển thị riêng với bạn"
    ), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


client.run(BOT_TOKEN)