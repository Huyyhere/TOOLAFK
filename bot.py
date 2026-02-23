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

BASE_URL     = "https://altare.sh"
MAX_ACC      = 20
RETRY_DELAY  = 30
MAX_HB_FAIL  = 5

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
                user_id INTEGER,
                name    TEXT,
                config  TEXT,
                PRIMARY KEY (user_id, name)
            )
        """)

def db_save(user_id, name, cfg):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounts VALUES (?, ?, ?)",
            (user_id, name, json.dumps(cfg, ensure_ascii=False))
        )

def db_delete(user_id, name):
    with db() as conn:
        conn.execute("DELETE FROM accounts WHERE user_id=? AND name=?", (user_id, name))

def db_count(user_id):
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (user_id,)).fetchone()[0]

def db_all():
    with db() as conn:
        return conn.execute("SELECT * FROM accounts").fetchall()


class Account:
    def __init__(self, user_id, name, cfg):
        self.user_id            = user_id
        self.name               = name
        self.cfg                = cfg
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
        self.restart_count      = 0
        self.status             = "đang khởi động"

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

    def do_heartbeat(self):
        try:
            r = requests.post(
                f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/heartbeat",
                headers=self.h(), json={}, timeout=10
            )
            return r.status_code in (200, 201, 204)
        except:
            return False

    def api_start(self):
        try:
            r = requests.post(
                f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/start",
                headers=self.h(), json={}, timeout=10
            )
            return r.status_code in (200, 201, 204)
        except:
            return False

    def api_stop(self):
        try:
            requests.post(
                f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/stop",
                headers=self.h(), json={}, timeout=10
            )
        except:
            pass

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S")

    def log(self, msg):
        print(f"[{self._ts()}] [{self.name}] {msg}")

    def push_discord(self):
        if not self.webhook:
            return
        earned  = round(self.balance - self.credits_start, 4) if self.credits_start else 0
        elapsed = str(datetime.now() - self.session_start).split(".")[0] if self.session_start else "?"
        hb_rate = round(self.hb_ok / max(self.hb_ok + self.hb_fail, 1) * 100)
        per_min = self.fetch_per_minute()
        self.notify_count += 1

        status_bar = "🟢 Hoạt động" if self.status == "hoạt động" else f"🔄 {self.status}"

        payload = {
            "username":   "Altare AFK",
            "avatar_url": "https://altare.sh/favicon.ico",
            "embeds": [{
                "author": {"name": f"Altare AFK  •  {self.name}"},
                "color": 0x2ecc71 if self.status == "hoạt động" else 0xe67e22,
                "fields": [
                    {
                        "name":   "Trạng thái",
                        "value":  f"`{status_bar}`  •  Khởi động lại: `{self.restart_count} lần`",
                        "inline": False
                    },
                    {
                        "name":   "Số dư",
                        "value":  f"```\n{self.balance:>12.4f} cr\n```",
                        "inline": True
                    },
                    {
                        "name":   "Kiếm được",
                        "value":  f"```diff\n+ {earned:.4f} cr\n```",
                        "inline": True
                    },
                    {
                        "name":   "Tốc độ",
                        "value":  f"```\n{per_min} cr/min\n```",
                        "inline": True
                    },
                    {
                        "name":   "Thời gian chạy",
                        "value":  f"```\n{elapsed}\n```",
                        "inline": True
                    },
                    {
                        "name":   "Heartbeat",
                        "value":  f"```\nOK {self.hb_ok}  Fail {self.hb_fail}  ({hb_rate}%)\n```",
                        "inline": True
                    },
                ],
                "footer":    {"text": f"Cập nhật #{self.notify_count}  •  {datetime.now().strftime('%H:%M:%S  %d/%m/%Y')}"},
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

    def _reset_state(self):
        self.hb_ok         = 0
        self.hb_fail       = 0
        self.session_start = datetime.now()
        self.credits_start = 0
        self.message_id    = None
        self.status        = "đang khởi động"

    def _do_restart(self):
        self.log("phiên bị lỗi — đang thử khởi động lại...")
        self.status = "đang khởi động lại"
        self.api_stop()
        time.sleep(RETRY_DELAY)

        for attempt in range(1, 6):
            self.log(f"thử lần {attempt}/5...")
            self._reset_state()
            if not self.tenant_id:
                self.tenant_id = self.detect_tenant()
            if self.tenant_id and self.api_start():
                self.restart_count += 1
                self.status = "hoạt động"
                self.log(f"khởi động lại thành công (lần {self.restart_count})")
                return True
            time.sleep(RETRY_DELAY)

        self.status  = "lỗi — không thể khởi động lại"
        self.running = False
        self.log("đã thử 5 lần nhưng thất bại, dừng hẳn")
        return False

    def _loop_heartbeat(self):
        consecutive_fail = 0
        while self.running:
            if self.do_heartbeat():
                self.hb_ok += 1
                consecutive_fail = 0
            else:
                self.hb_fail += 1
                consecutive_fail += 1
                self.log(f"heartbeat thất bại ({consecutive_fail}/{MAX_HB_FAIL})")

                if consecutive_fail >= MAX_HB_FAIL:
                    self.log(f"heartbeat thất bại {MAX_HB_FAIL} lần liên tiếp — trigger restart")
                    consecutive_fail = 0
                    if not self._do_restart():
                        break

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
                self.log(f"{bal:.4f} cr  +{earned:.4f}  {elapsed}  hb {hb_rate}%  restart×{self.restart_count}")
            time.sleep(self.stats_interval)

    def _loop_notify(self):
        time.sleep(3)
        while self.running:
            self.push_discord()
            time.sleep(self.notify_interval)

    def _loop_sse(self):
        raw  = self.token.replace("Bearer ", "")
        url  = f"https://api.altare.sh/subscribe?token={raw}"
        hdrs = {
            "Accept":        "text/event-stream",
            "Cache-Control": "no-cache",
            "Authorization": self.token,
            "Origin":        BASE_URL,
            "User-Agent":    "Mozilla/5.0"
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
            return False, "Không tìm được tenant ID — kiểm tra lại token."
        if not self.api_start():
            return False, "Gọi API start AFK thất bại."

        self.running       = True
        self.session_start = datetime.now()
        self.status        = "hoạt động"

        for fn in [self._loop_sse, self._loop_heartbeat, self._loop_stats, self._loop_notify]:
            threading.Thread(target=fn, daemon=True).start()

        self.log("đã bắt đầu")
        return True, "OK"

    def stop(self):
        self.running = False
        self.api_stop()
        self.log("đã dừng")


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
    rows   = db_all()
    loaded = 0

    print(f"\n{'─'*45}")
    print(f"  Bot: {client.user}")
    print(f"  Đang khôi phục {len(rows)} tài khoản từ DB...")
    print(f"{'─'*45}")

    for row in rows:
        uid  = row["user_id"]
        name = row["name"]
        cfg  = json.loads(row["config"])
        acc  = Account(uid, name, cfg)
        ok, msg = acc.start()
        if ok:
            runtime.setdefault(uid, {})[name] = acc
            loaded += 1
            print(f"  ✓  {name}  (user {uid})")
        else:
            print(f"  ✗  {name}  —  {msg}")

    print(f"{'─'*45}")
    print(f"  Khôi phục thành công: {loaded}/{len(rows)}")
    print(f"{'─'*45}\n")

    await tree.sync()


@tree.command(name="thêm", description="Gửi file JSON để thêm tài khoản AFK mới (tối đa 20)")
async def cmd_them(interaction: discord.Interaction, file: discord.Attachment):
    uid = interaction.user.id

    if db_count(uid) >= MAX_ACC:
        await interaction.response.send_message(
            f"Bạn đã đạt tối đa **{MAX_ACC} tài khoản**. Xoá bớt trước khi thêm mới.",
            ephemeral=True
        )
        return

    if not file.filename.endswith(".json"):
        await interaction.response.send_message("Chỉ chấp nhận file `.json`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        cfg = json.loads(await file.read())
    except:
        await interaction.followup.send("File JSON không hợp lệ — kiểm tra lại định dạng.", ephemeral=True)
        return

    token = cfg.get("token", "").strip()
    if not token:
        await interaction.followup.send("Thiếu trường `token` trong file JSON.", ephemeral=True)
        return

    name = cfg.get("name", "").strip() or file.filename.removesuffix(".json")

    if name in runtime.get(uid, {}):
        await interaction.followup.send(
            f"Tài khoản `{name}` đang chạy rồi.\nĐặt tên khác trong file JSON hoặc xoá cái cũ trước.",
            ephemeral=True
        )
        return

    acc = Account(uid, name, cfg)
    ok, msg = await asyncio.get_event_loop().run_in_executor(None, acc.start)

    if not ok:
        await interaction.followup.send(f"Lỗi khởi động: **{msg}**", ephemeral=True)
        return

    runtime.setdefault(uid, {})[name] = acc
    db_save(uid, name, cfg)

    slot_con_lai = MAX_ACC - db_count(uid)
    embed = discord.Embed(title="Đã thêm tài khoản AFK", color=0x2ecc71)
    embed.add_field(name="Tên",           value=f"`{name}`",                      inline=True)
    embed.add_field(name="Tenant",        value=f"`{acc.tenant_id[:18]}...`",      inline=True)
    embed.add_field(name="Slot còn lại",  value=f"`{slot_con_lai}/{MAX_ACC}`",     inline=True)
    embed.add_field(name="Heartbeat",     value=f"`{acc.heartbeat_interval}s`",    inline=True)
    embed.add_field(name="Stats",         value=f"`{acc.stats_interval}s`",        inline=True)
    embed.add_field(name="Notify",        value=f"`{acc.notify_interval}s`",       inline=True)
    embed.set_footer(text="Tài khoản đã được lưu — tự khôi phục khi bot restart")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="xóa", description="Dừng và xoá một tài khoản AFK")
@app_commands.describe(tài_khoản="Chọn tài khoản muốn xoá")
@app_commands.autocomplete(tài_khoản=autocomplete_acc)
async def cmd_xoa(interaction: discord.Interaction, tài_khoản: str):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})

    if tài_khoản not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy `{tài_khoản}`.\nDùng `/danh-sách` để xem tất cả.", ephemeral=True)
        return

    accs[tài_khoản].stop()
    del accs[tài_khoản]
    if not accs:
        del runtime[uid]

    db_delete(uid, tài_khoản)

    embed = discord.Embed(
        title="Đã xoá tài khoản",
        description=f"Tài khoản **{tài_khoản}** đã dừng và xoá khỏi database.",
        color=0xe74c3c
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="danh-sách", description="Xem tất cả tài khoản AFK của bạn")
async def cmd_danh_sach(interaction: discord.Interaction):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})

    if not accs:
        embed = discord.Embed(
            title="Chưa có tài khoản nào",
            description="Dùng `/thêm` và gửi kèm file JSON để bắt đầu.",
            color=0x95a5a6
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Tài khoản AFK của bạn  —  {len(accs)}/{MAX_ACC}",
        color=0x00d4aa
    )

    for name, acc in accs.items():
        earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
        elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
        hb_rate = round(acc.hb_ok / max(acc.hb_ok + acc.hb_fail, 1) * 100)
        icon    = "🟢" if acc.status == "hoạt động" else "🔄" if "khởi động" in acc.status else "🔴"
        embed.add_field(
            name=f"{icon}  {name}",
            value=(
                f"Số dư: `{acc.balance:.4f} cr`  •  Kiếm: `+{earned:.4f}`\n"
                f"Uptime: `{elapsed}`  •  HB: `{hb_rate}%`  •  Restart: `{acc.restart_count}×`"
            ),
            inline=False
        )

    embed.set_footer(text=f"Dùng /trạng-thái để xem chi tiết từng tài khoản")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="trạng-thái", description="Xem chi tiết một tài khoản AFK")
@app_commands.describe(tài_khoản="Chọn tài khoản muốn xem")
@app_commands.autocomplete(tài_khoản=autocomplete_acc)
async def cmd_trang_thai(interaction: discord.Interaction, tài_khoản: str):
    uid  = interaction.user.id
    accs = runtime.get(uid, {})

    if tài_khoản not in accs:
        await interaction.response.send_message(
            f"Không tìm thấy `{tài_khoản}`.\nDùng `/danh-sách` để xem.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    acc     = accs[tài_khoản]
    earned  = round(acc.balance - acc.credits_start, 4) if acc.credits_start else 0
    elapsed = str(datetime.now() - acc.session_start).split(".")[0] if acc.session_start else "?"
    hb_rate = round(acc.hb_ok / max(acc.hb_ok + acc.hb_fail, 1) * 100)
    per_min = await asyncio.get_event_loop().run_in_executor(None, acc.fetch_per_minute)
    icon    = "🟢" if acc.status == "hoạt động" else "🔄" if "khởi động" in acc.status else "🔴"

    embed = discord.Embed(
        title=f"{icon}  {tài_khoản}",
        color=0x2ecc71 if acc.status == "hoạt động" else 0xe67e22
    )
    embed.add_field(name="Trạng thái",      value=f"`{acc.status}`",              inline=True)
    embed.add_field(name="Khởi động lại",   value=f"`{acc.restart_count} lần`",   inline=True)
    embed.add_field(name="\u200b",          value="\u200b",                        inline=True)
    embed.add_field(name="Số dư",           value=f"`{acc.balance:.4f} cr`",      inline=True)
    embed.add_field(name="Kiếm được",       value=f"`+{earned:.4f} cr`",           inline=True)
    embed.add_field(name="Tốc độ",          value=f"`{per_min} cr/min`",           inline=True)
    embed.add_field(name="Uptime",          value=f"`{elapsed}`",                  inline=True)
    embed.add_field(name="Heartbeat",       value=f"`{hb_rate}% OK`",              inline=True)
    embed.add_field(name="HB OK / Fail",    value=f"`{acc.hb_ok} / {acc.hb_fail}`", inline=True)
    embed.add_field(name="Tenant ID",       value=f"`{acc.tenant_id}`",            inline=False)
    embed.set_footer(text=f"Cập nhật lúc {datetime.now().strftime('%H:%M:%S  %d/%m/%Y')}")

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="trợ-giúp", description="Hướng dẫn sử dụng bot")
async def cmd_tro_giup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Altare AFK Bot  —  Hướng dẫn",
        color=0x00d4aa
    )

    embed.add_field(
        name="Bước 1  —  Lấy token",
        value=(
            "1. Mở `altare.sh` → đăng nhập\n"
            "2. Nhấn `F12` → tab **Network**\n"
            "3. Click bất kỳ request nào tới `altare.sh`\n"
            "4. Tìm header **Authorization** → copy `Bearer eyJ...`"
        ),
        inline=False
    )

    embed.add_field(
        name="Bước 2  —  Tạo file config.json",
        value=(
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
            "`tenant_id` để trống, bot tự tìm.\n"
            "`name` là tên hiển thị — dùng để nhận biết khi có nhiều acc."
        ),
        inline=False
    )

    embed.add_field(
        name="Bước 3  —  Thêm tài khoản",
        value="Dùng `/thêm` và đính kèm file JSON vừa tạo.",
        inline=False
    )

    embed.add_field(
        name="Các lệnh",
        value=(
            "`/thêm`          Thêm tài khoản AFK mới (tối đa 20)\n"
            "`/xóa`           Dừng và xoá tài khoản\n"
            "`/danh-sách`     Xem tổng quan tất cả tài khoản\n"
            "`/trạng-thái`    Xem chi tiết một tài khoản\n"
            "`/trợ-giúp`      Hiện hướng dẫn này"
        ),
        inline=False
    )

    embed.add_field(
        name="Tính năng tự động",
        value=(
            "— Tự khôi phục toàn bộ tài khoản khi bot restart\n"
            f"— Tự khởi động lại khi heartbeat thất bại {MAX_HB_FAIL} lần liên tiếp\n"
            "— Thử lại tối đa 5 lần, mỗi lần cách nhau 30 giây\n"
            "— Mọi reply chỉ hiển thị riêng với bạn"
        ),
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


client.run(BOT_TOKEN)
