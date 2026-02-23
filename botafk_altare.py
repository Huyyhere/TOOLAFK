import discord
from discord import app_commands
import requests
import threading
import asyncio
import json
import time
import sqlite3
import os
from getpass import getpass
from datetime import datetime, timezone

def _clear():
    os.system("cls" if os.name == "nt" else "clear")

BOT_TOKEN = getpass("Nhập token bot Discord: ").strip()
_clear()
print("Token đã nhận. Đang khởi động bot...")

BASE_URL = "https://altare.sh"
MAX_ACC  = 10


intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

runtime = {}


def db():
    conn = sqlite3.connect("afk.db")
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                user_id   INTEGER,
                name      TEXT,
                config    TEXT,
                PRIMARY KEY (user_id, name)
            )
        """)

def db_save(user_id, name, cfg):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounts VALUES (?, ?, ?)",
            (user_id, name, json.dumps(cfg))
        )

def db_delete(user_id, name):
    with db() as conn:
        conn.execute("DELETE FROM accounts WHERE user_id=? AND name=?", (user_id, name))

def db_load_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchall()

def db_count_user(user_id):
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (user_id,)).fetchone()[0]



class Account:
    def __init__(self, user_id, name, cfg):
        self.user_id            = user_id
        self.name               = name
        self.token              = cfg["token"] if cfg["token"].startswith("Bearer ") else f"Bearer {cfg['token']}"
        self.tenant_id          = cfg.get("tenant_id", "").strip()
        self.webhook            = cfg.get("discord_webhook", "").strip()
        self.heartbeat_interval = cfg.get("heartbeat_interval", 30)
        self.stats_interval     = cfg.get("stats_interval", 60)
        self.notify_interval    = cfg.get("notify_interval_seconds", 10)
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

    def fetch_balance(self):
        try:
            r = requests.get(f"{BASE_URL}/api/tenants", headers=self.h(), timeout=10)
            if r.status_code == 200:
                items = r.json()
                items = items.get("items", items) if isinstance(items, dict) else items
                for item in items:
                    if item.get("id") == self.tenant_id:
                        c = item.get("creditsCents")
                        return round(c / 100, 4) if c is not None else None
                if items:
                    c = items[0].get("creditsCents")
                    return round(c / 100, 4) if c is not None else None
        except:
            pass
        return None

    def fetch_per_minute(self):
        try:
            r = requests.get(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards", headers=self.h(), timeout=10)
            if r.status_code == 200:
                data = r.json()
                afk  = data.get("afk") if isinstance(data.get("afk"), dict) else {}
                return afk.get("perMinute") or data.get("perMinute") or 0.35
        except:
            pass
        return 0.35

    def heartbeat(self):
        try:
            r = requests.post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/heartbeat",
                              headers=self.h(), json={}, timeout=10)
            return r.status_code in (200, 201, 204)
        except:
            return False

    def api_start(self):
        try:
            r = requests.post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/start",
                              headers=self.h(), json={}, timeout=10)
            return r.status_code in (200, 201, 204)
        except:
            return False

    def api_stop(self):
        try:
            requests.post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/stop",
                          headers=self.h(), json={}, timeout=10)
        except:
            pass

    def push_discord(self):
        if not self.webhook:
            return
        earned  = round(self.balance - self.credits_start, 4) if self.credits_start else 0
        elapsed = str(datetime.now() - self.session_start).split(".")[0] if self.session_start else "?"
        hb_rate = round(self.hb_ok / max(self.hb_ok + self.hb_fail, 1) * 100)
        per_min = self.fetch_per_minute()
        self.notify_count += 1

        payload = {
            "username": "Altare AFK",
            "embeds": [{
                "title": self.name,
                "color": 0x00d4aa,
                "fields": [
                    {"name": "Số dư",     "value": f"```{self.balance:.4f} cr```",                                          "inline": True},
                    {"name": "Kiếm được", "value": f"```+{earned:.4f} cr```",                                                "inline": True},
                    {"name": "Mỗi phút",  "value": f"```{per_min} cr/min```",                                                "inline": True},
                    {"name": "Uptime",    "value": f"```{elapsed}```",                                                       "inline": True},
                    {"name": "Heartbeat", "value": f"```OK: {self.hb_ok} | Fail: {self.hb_fail} | {hb_rate}%```",           "inline": True},
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

    def _loop_heartbeat(self):
        while self.running:
            if self.heartbeat():
                self.hb_ok += 1
            else:
                self.hb_fail += 1
                print(f"[{self.name}] heartbeat thất bại ({self.hb_fail})")
            time.sleep(self.heartbeat_interval)

    def _loop_stats(self):
        while self.running:
            bal = self.fetch_balance()
            if bal is not None:
                if not self.credits_start:
                    self.credits_start = bal
                self.balance = bal
                earned  = round(bal - self.credits_start, 4)
                elapsed = str(datetime.now() - self.session_start).split(".")[0]
                hb_rate = round(self.hb_ok / max(self.hb_ok + self.hb_fail, 1) * 100)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.name}] {bal:.4f} cr | +{earned:.4f} | {elapsed} | hb {hb_rate}%")
            time.sleep(self.stats_interval)

    def _loop_notify(self):
        time.sleep(3)
        while self.running:
            self.push_discord()
            time.sleep(self.notify_interval)

    def _loop_sse(self):
        raw  = self.token.replace("Bearer ", "")
        url  = f"https://api.altare.sh/subscribe?token={raw}"
        hdrs = {"Accept": "text/event-stream", "Cache-Control": "no-cache",
                "Authorization": self.token, "Origin": BASE_URL, "User-Agent": "Mozilla/5.0"}
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
            return False, "Không tìm được tenant ID. Kiểm tra lại token."
        if not self.api_start():
            return False, "Gọi API start AFK thất bại."
        self.running       = True
        self.session_start = datetime.now()
        for fn in [self._loop_sse, self._loop_heartbeat, self._loop_stats, self._loop_notify]:
            threading.Thread(target=fn, daemon=True).start()
        print(f"[{self.name}] đã bắt đầu")
        return True, "OK"

    def stop(self):
        self.running = False
        self.api_stop()
        print(f"[{self.name}] đã dừng")



async def autocomplete_acc(interaction: discord.Interaction, current: str):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})
    return [
        app_commands.Choice(name=name, value=name)
        for name in accs if current.lower() in name.lower()
    ][:25]



@client.event
async def on_ready():
    db_init()

    with db() as conn:
        rows = conn.execute("SELECT * FROM accounts").fetchall()
    for row in rows:
        uid  = row["user_id"]
        name = row["name"]
        cfg  = json.loads(row["config"])
        acc  = Account(uid, name, cfg)
        ok, _ = acc.start()
        if ok:
            runtime.setdefault(uid, {})[name] = acc
            print(f"[Khôi phục] {name} (user {uid})")

    await tree.sync()
    print(f"Bot sẵn sàng: {client.user} | Đã khôi phục {len(rows)} tài khoản")



@tree.command(name="thêm", description="Gửi file JSON để thêm tài khoản AFK (tối đa 10 tài khoản)")
async def cmd_them(interaction: discord.Interaction, file: discord.Attachment):
    uid = interaction.user.id

    if db_count_user(uid) >= MAX_ACC:
        await interaction.response.send_message(
            f"Bạn đã đạt tối đa {MAX_ACC} tài khoản. Xoá bớt trước khi thêm mới.", ephemeral=True)
        return

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
        await interaction.followup.send("Thiếu trường `token` trong file JSON.", ephemeral=True)
        return

    name = cfg.get("name", "").strip() or file.filename.replace(".json", "")

    if name in runtime.get(uid, {}):
        await interaction.followup.send(
            f"Tài khoản `{name}` đang chạy rồi. Đặt tên khác hoặc xoá cái cũ trước.", ephemeral=True)
        return

    acc    = Account(uid, name, cfg)
    ok, msg = await asyncio.get_event_loop().run_in_executor(None, acc.start)

    if not ok:
        await interaction.followup.send(f"Lỗi: {msg}", ephemeral=True)
        return

    runtime.setdefault(uid, {})[name] = acc
    db_save(uid, name, cfg)

    await interaction.followup.send(
        f"Đã thêm và bắt đầu AFK cho **{name}**!\n"
        f"Tenant: `{acc.tenant_id[:20]}...`\n"
        f"Còn lại: `{MAX_ACC - db_count_user(uid)}/{MAX_ACC}` slot trống.",
        ephemeral=True
    )



@tree.command(name="xóa", description="Dừng và xoá một tài khoản AFK")
@app_commands.describe(tài_khoản="Chọn tài khoản muốn xoá")
@app_commands.autocomplete(tài_khoản=autocomplete_acc)
async def cmd_xoa(interaction: discord.Interaction, tài_khoản: str):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})

    if tài_khoản not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tài_khoản}`.", ephemeral=True)
        return

    accs[tài_khoản].stop()
    del accs[tài_khoản]
    if not accs:
        del runtime[uid]

    db_delete(uid, tài_khoản)
    await interaction.response.send_message(
        f"Đã dừng và xoá tài khoản **{tài_khoản}**.", ephemeral=True)



@tree.command(name="danh-sách", description="Xem tất cả tài khoản AFK của bạn")
async def cmd_danh_sach(interaction: discord.Interaction):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})

    if not accs:
        await interaction.response.send_message(
            "Bạn chưa có tài khoản AFK nào.\nDùng `/thêm` để bắt đầu.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Tài khoản của bạn — {len(accs)}/{MAX_ACC}",
        color=0x00d4aa
    )
    for name, acc in accs.items():
        earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
        elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
        hb_rate = round(acc.hb_ok / max(acc.hb_ok + acc.hb_fail, 1) * 100)
        embed.add_field(
            name=name,
            value=f"`{acc.balance:.4f} cr` (+{earned:.4f}) | Uptime: `{elapsed}` | HB: `{hb_rate}%`",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)



@tree.command(name="trạng-thái", description="Xem chi tiết một tài khoản AFK")
@app_commands.describe(tài_khoản="Chọn tài khoản muốn xem")
@app_commands.autocomplete(tài_khoản=autocomplete_acc)
async def cmd_trang_thai(interaction: discord.Interaction, tài_khoản: str):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})

    if tài_khoản not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy tài khoản `{tài_khoản}`.", ephemeral=True)
        return

    acc     = accs[tài_khoản]
    earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
    elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
    hb_rate = round(acc.hb_ok / max(acc.hb_ok + acc.hb_fail, 1) * 100)
    per_min = await asyncio.get_event_loop().run_in_executor(None, acc.fetch_per_minute)

    embed = discord.Embed(title=tài_khoản, color=0x00d4aa)
    embed.add_field(name="Số dư",      value=f"`{acc.balance:.4f} cr`",    inline=True)
    embed.add_field(name="Kiếm được",  value=f"`+{earned:.4f} cr`",         inline=True)
    embed.add_field(name="Mỗi phút",   value=f"`{per_min} cr/min`",         inline=True)
    embed.add_field(name="Uptime",     value=f"`{elapsed}`",                inline=True)
    embed.add_field(name="Heartbeat",  value=f"`{hb_rate}% OK`",            inline=True)
    embed.add_field(name="HB OK/Fail", value=f"`{acc.hb_ok}/{acc.hb_fail}`", inline=True)
    embed.add_field(name="Tenant",     value=f"`{acc.tenant_id}`",          inline=False)
    embed.set_footer(text=f"Cập nhật lúc {datetime.now().strftime('%H:%M:%S')}")

    await interaction.response.send_message(embed=embed, ephemeral=True)



@tree.command(name="trợ-giúp", description="Hướng dẫn sử dụng bot")
async def cmd_tro_giup(interaction: discord.Interaction):
    embed = discord.Embed(title="Altare AFK Bot — Hướng dẫn", color=0x00d4aa)

    embed.add_field(name="Bước 1 — Tạo file config.json", value=(
        "```json\n{\n"
        '  "name": "Tên tài khoản",\n'
        '  "token": "Bearer eyJ...",\n'
        '  "tenant_id": "",\n'
        '  "discord_webhook": "https://discord.com/api/webhooks/...",\n'
        '  "heartbeat_interval": 30,\n'
        '  "stats_interval": 60,\n'
        '  "notify_interval_seconds": 10\n'
        "}\n```"
        "Trường `tenant_id` để trống, bot tự tìm.\n"
        "Trường `name` dùng để phân biệt khi có nhiều acc. Nếu không điền, bot lấy tên file."
    ), inline=False)

    embed.add_field(name="Bước 2 — Lấy token", value=(
        "1. Mở `altare.sh`, đăng nhập\n"
        "2. Nhấn `F12` → tab **Network**\n"
        "3. Click bất kỳ request nào tới `altare.sh`\n"
        "4. Tìm header **Authorization** → copy giá trị `Bearer eyJ...`\n"
        "5. Dán vào trường `token` trong file JSON"
    ), inline=False)

    embed.add_field(name="Các lệnh", value=(
        "`/thêm` — Gửi kèm file JSON, thêm tài khoản AFK mới (tối đa 10)\n"
        "`/xóa` — Dừng và xoá tài khoản (có gợi ý chọn)\n"
        "`/danh-sách` — Xem tất cả tài khoản đang chạy\n"
        "`/trạng-thái` — Xem chi tiết một tài khoản (có gợi ý chọn)\n"
        "`/trợ-giúp` — Hiện hướng dẫn này"
    ), inline=False)

    embed.add_field(name="Lưu ý", value=(
        "— Mỗi người tối đa **10 tài khoản**\n"
        "— Tài khoản được lưu vào database, tự khôi phục khi bot restart\n"
        "— Token hết hạn: xoá tài khoản cũ, tạo file JSON mới với token mới rồi `/thêm` lại\n"
        "— Mọi reply chỉ hiển thị riêng với bạn"
    ), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


client.run(BOT_TOKEN)
