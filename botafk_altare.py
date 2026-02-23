import discord
from discord import app_commands
import aiohttp
import asyncio
import json
import time
import sqlite3
import os
import uuid
import logging
from getpass import getpass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
log = logging.getLogger("afk_bot")

def _clear():
    os.system("cls" if os.name == "nt" else "clear")

BOT_TOKEN = getpass("Nhập token bot Discord: ").strip()
_clear()
log.info("Token đã nhận. Đang khởi động bot...")

BASE_URL            = "https://altare.sh"
MAX_ACC             = 50
RETRY_DELAY         = 30
MAX_HB_FAIL         = 5
GLOBAL_LOG_WEBHOOK  = "https://discord.com/api/webhooks/1475494025506197580/oTJbBsz4jbKC_ERoZkrC6yHhVirItTYnH3UmUOnMmDuvNKvcB3zMLBxiJnO7QzvU3CEP"
GLOBAL_LOG_INTERVAL = 60
WEBHOOK_RATE_LIMIT  = 1.2
CMD_COOLDOWN        = 15
CONFIGS_DIR         = "configs"

os.makedirs(CONFIGS_DIR, exist_ok=True)

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

runtime: dict[str, "Account"] = {}
executor = ThreadPoolExecutor(max_workers=32)
_cooldowns: dict[int, float] = defaultdict(float)


def check_cooldown(uid: int) -> float:
    rem = CMD_COOLDOWN - (time.monotonic() - _cooldowns[uid])
    return round(rem, 1) if rem > 0 else 0.0

def set_cooldown(uid: int):
    _cooldowns[uid] = time.monotonic()

def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S  %d/%m/%Y")


def db():
    conn = sqlite3.connect("afk.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                acc_id    TEXT PRIMARY KEY,
                name      TEXT NOT NULL,
                file_path TEXT NOT NULL,
                added_by  INTEGER NOT NULL,
                added_at  TEXT NOT NULL
            )
        """)

def db_insert(acc_id: str, name: str, file_path: str, added_by: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
            (acc_id, name, file_path, added_by, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

def db_delete(acc_id: str):
    with db() as conn:
        conn.execute("DELETE FROM accounts WHERE acc_id=?", (acc_id,))

def db_count() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

def db_all():
    with db() as conn:
        return conn.execute("SELECT * FROM accounts ORDER BY added_at").fetchall()

def db_get(acc_id: str):
    with db() as conn:
        return conn.execute("SELECT * FROM accounts WHERE acc_id=?", (acc_id,)).fetchone()


_webhook_last_sent: dict[str, float] = {}

async def send_webhook(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    message_id: str | None = None
) -> str | None:
    key  = url.split("/messages/")[0]
    wait = WEBHOOK_RATE_LIMIT - (time.monotonic() - _webhook_last_sent.get(key, 0))
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        if message_id is None:
            async with session.post(url + "?wait=true", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                _webhook_last_sent[key] = time.monotonic()
                if r.status in (200, 204):
                    return (await r.json()).get("id")
        else:
            async with session.patch(f"{url}/messages/{message_id}", json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                _webhook_last_sent[key] = time.monotonic()
                if r.status in (200, 204):
                    return message_id
    except Exception as e:
        log.warning(f"Webhook error: {e}")
    return None


class Account:
    def __init__(self, acc_id: str, name: str, cfg: dict, added_by: int):
        self.acc_id             = acc_id
        self.name               = name
        self.cfg                = cfg
        self.added_by           = added_by
        raw_token               = cfg.get("token", "").strip()
        self.token              = self._normalize_token(raw_token)
        self.tenant_id          = cfg.get("tenant_id", "").strip()
        self.webhook            = cfg.get("discord_webhook", "").strip()
        self.heartbeat_interval = cfg.get("heartbeat_interval", 30)
        self.stats_interval     = cfg.get("stats_interval", 60)
        self.notify_interval    = cfg.get("notify_interval_seconds", 10)

        self.running        = False
        self.session_start: datetime | None = None
        self.credits_start  = 0.0
        self.balance        = 0.0
        self.hb_ok          = 0
        self.hb_fail        = 0
        self.message_id: str | None = None
        self.notify_count   = 0
        self.restart_count  = 0
        self.status         = "đang khởi động"
        self._per_min_cache = 0.35
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def _normalize_token(raw: str) -> str:
        raw = raw.strip()
        # Xoá prefix Bearer nếu có để chuẩn hóa
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        # JWT phải bắt đầu bằng eyJ
        return f"Bearer {raw}"

    def _headers(self) -> dict:
        h = {
            "Authorization": self.token,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Origin":        BASE_URL,
            "Referer":       f"{BASE_URL}/billing/rewards/afk",
            "User-Agent":    "Mozilla/5.0",
        }
        if self.tenant_id:
            h["altare-selected-tenant-id"] = self.tenant_id
        return h

    def _get(self, url: str) -> dict | list | None:
        import requests
        try:
            r = requests.get(url, headers=self._headers(), timeout=15)
            log.debug(f"[{self.name}] GET {url} → {r.status_code}")
            if r.ok:
                return r.json()
            log.warning(f"[{self.name}] GET {url} → HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.warning(f"[{self.name}] GET {url} → lỗi kết nối: {e}")
        return None

    def _post(self, url: str) -> tuple[bool, int, str]:
        import requests
        try:
            r = requests.post(url, headers=self._headers(), json={}, timeout=15)
            log.debug(f"[{self.name}] POST {url} → {r.status_code}")
            ok = r.status_code in (200, 201, 204)
            if not ok:
                log.warning(f"[{self.name}] POST {url} → HTTP {r.status_code}: {r.text[:300]}")
            return ok, r.status_code, r.text
        except Exception as e:
            log.warning(f"[{self.name}] POST {url} → lỗi kết nối: {e}")
            return False, 0, str(e)

    def _sync_detect_tenant(self) -> str | None:
        data = self._get(f"{BASE_URL}/api/tenants")
        if not data:
            log.warning(f"[{self.name}] detect_tenant: không lấy được danh sách tenant")
            return None
        items = data.get("items", data) if isinstance(data, dict) else data
        if not items:
            log.warning(f"[{self.name}] detect_tenant: danh sách tenant rỗng")
            return None
        tid = items[0].get("id") or items[0].get("tenantId")
        log.info(f"[{self.name}] tenant_id = {tid}")
        return tid

    def _sync_fetch_balance(self) -> float | None:
        data = self._get(f"{BASE_URL}/api/tenants")
        if not data:
            return None
        items = data.get("items", data) if isinstance(data, dict) else data
        for item in (items if isinstance(items, list) else []):
            if item.get("id") == self.tenant_id:
                c = item.get("creditsCents")
                return round(c / 100, 4) if c is not None else None
        if isinstance(items, list) and items:
            c = items[0].get("creditsCents")
            return round(c / 100, 4) if c is not None else None
        return None

    def _sync_fetch_per_minute(self) -> float:
        data = self._get(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards")
        if data:
            afk = data.get("afk") if isinstance(data.get("afk"), dict) else {}
            return afk.get("perMinute") or data.get("perMinute") or 0.35
        return 0.35

    def _sync_heartbeat(self) -> bool:
        ok, _, _ = self._post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/heartbeat")
        return ok

    def _sync_api_start(self) -> tuple[bool, str]:
        ok, code, body = self._post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/start")
        if ok:
            return True, "OK"
        return False, f"HTTP {code}: {body[:200]}"

    def _sync_api_stop(self):
        self._post(f"{BASE_URL}/api/tenants/{self.tenant_id}/rewards/afk/stop")

    async def detect_tenant(self) -> str | None:
        return await asyncio.get_event_loop().run_in_executor(executor, self._sync_detect_tenant)

    async def fetch_balance(self) -> float | None:
        return await asyncio.get_event_loop().run_in_executor(executor, self._sync_fetch_balance)

    async def fetch_per_minute(self) -> float:
        pm = await asyncio.get_event_loop().run_in_executor(executor, self._sync_fetch_per_minute)
        self._per_min_cache = pm
        return pm

    async def do_heartbeat(self) -> bool:
        return await asyncio.get_event_loop().run_in_executor(executor, self._sync_heartbeat)

    async def api_start(self) -> tuple[bool, str]:
        return await asyncio.get_event_loop().run_in_executor(executor, self._sync_api_start)

    async def api_stop(self):
        await asyncio.get_event_loop().run_in_executor(executor, self._sync_api_stop)

    def elapsed_str(self) -> str:
        return str(datetime.now() - self.session_start).split(".")[0] if self.session_start else "?"

    def hb_rate(self) -> int:
        return round(self.hb_ok / max(self.hb_ok + self.hb_fail, 1) * 100)

    def earned(self) -> float:
        return round(self.balance - self.credits_start, 4) if self.credits_start else 0.0

    def status_icon(self) -> str:
        if self.status == "hoạt động":
            return "🟢"
        if "khởi động" in self.status:
            return "🔄"
        return "🔴"

    def _reset_state(self):
        self.hb_ok         = 0
        self.hb_fail       = 0
        self.session_start = datetime.now()
        self.credits_start = 0.0
        self.message_id    = None
        self.status        = "đang khởi động"

    async def push_discord(self, session: aiohttp.ClientSession):
        if not self.webhook:
            return
        self.notify_count += 1
        icon = self.status_icon()
        color = 0x2ecc71 if self.status == "hoạt động" else 0xe67e22

        payload = {
            "username":   "Altare AFK Monitor",
            "avatar_url": "https://altare.sh/favicon.ico",
            "embeds": [{
                "author": {
                    "name":     f"{'─' * 6}  {self.name}  {'─' * 6}",
                    "icon_url": "https://altare.sh/favicon.ico"
                },
                "color": color,
                "fields": [
                    {
                        "name":   "📡  Trạng thái",
                        "value":  f"{icon}  **{self.status.capitalize()}**\n> Khởi động lại: `{self.restart_count} lần`",
                        "inline": False
                    },
                    {
                        "name":   "💰  Số dư hiện tại",
                        "value":  f"```fix\n{self.balance:.4f} cr\n```",
                        "inline": True
                    },
                    {
                        "name":   "📈  Kiếm được",
                        "value":  f"```diff\n+ {self.earned():.4f} cr\n```",
                        "inline": True
                    },
                    {
                        "name":   "⚡  Tốc độ",
                        "value":  f"```fix\n{self._per_min_cache} cr/phút\n```",
                        "inline": True
                    },
                    {
                        "name":   "⏱️  Thời gian chạy",
                        "value":  f"```fix\n{self.elapsed_str()}\n```",
                        "inline": True
                    },
                    {
                        "name":   "💓  Heartbeat",
                        "value":  f"```fix\n✓ {self.hb_ok}  ✗ {self.hb_fail}  ({self.hb_rate()}% OK)\n```",
                        "inline": True
                    },
                ],
                "footer": {
                    "text":     f"Cập nhật #{self.notify_count}  •  {now_str()}",
                    "icon_url": "https://altare.sh/favicon.ico"
                },
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }]
        }
        self.message_id = await send_webhook(session, self.webhook, payload, self.message_id)

    async def _loop_heartbeat(self):
        consecutive_fail = 0
        while self.running:
            ok = await self.do_heartbeat()
            if ok:
                self.hb_ok += 1
                consecutive_fail = 0
            else:
                self.hb_fail += 1
                consecutive_fail += 1
                log.warning(f"[{self.name}] heartbeat thất bại ({consecutive_fail}/{MAX_HB_FAIL})")
                if consecutive_fail >= MAX_HB_FAIL:
                    consecutive_fail = 0
                    if not await self._do_restart():
                        break
            await asyncio.sleep(self.heartbeat_interval)

    async def _loop_stats(self):
        while self.running:
            bal = await self.fetch_balance()
            if bal is not None:
                if not self.credits_start:
                    self.credits_start = bal
                self.balance = bal
                log.info(
                    f"[{self.name}] "
                    f"{bal:.4f} cr  +{self.earned():.4f}  "
                    f"{self.elapsed_str()}  hb {self.hb_rate()}%  "
                    f"restart×{self.restart_count}"
                )
            await asyncio.sleep(self.stats_interval)

    async def _loop_notify(self, session: aiohttp.ClientSession):
        await asyncio.sleep(3)
        while self.running:
            await self.push_discord(session)
            await asyncio.sleep(self.notify_interval)

    async def _loop_sse(self, session: aiohttp.ClientSession):
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
                async with session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=None, connect=15)) as r:
                    if r.status == 200:
                        async for _ in r.content:
                            if not self.running:
                                return
                    else:
                        await asyncio.sleep(15)
            except Exception:
                if self.running:
                    await asyncio.sleep(15)
            if self.running:
                await asyncio.sleep(5)

    async def _do_restart(self) -> bool:
        self.status = "đang khởi động lại"
        await self.api_stop()
        await asyncio.sleep(RETRY_DELAY)
        for attempt in range(1, 6):
            log.info(f"[{self.name}] thử khởi động lại lần {attempt}/5...")
            self._reset_state()
            if not self.tenant_id:
                self.tenant_id = await self.detect_tenant()
            if self.tenant_id:
                ok_r, msg_r = await self.api_start()
                if ok_r:
                    self.restart_count += 1
                    self.status = "hoạt động"
                    log.info(f"[{self.name}] khởi động lại thành công (lần {self.restart_count})")
                    return True
                log.warning(f"[{self.name}] api_start thất bại: {msg_r}")
            await asyncio.sleep(RETRY_DELAY)
        self.status  = "lỗi — không thể khởi động lại"
        self.running = False
        log.error(f"[{self.name}] thất bại sau 5 lần, dừng hẳn")
        return False

    async def start(self) -> tuple[bool, str]:
        if not self.tenant_id:
            self.tenant_id = await self.detect_tenant()
        if not self.tenant_id:
            log.error(f"[{self.name}] Không tìm được tenant. Token có thể sai hoặc đã hết hạn.")
            return False, (
                "Không tìm được Tenant ID.\n\n"
                "**Cách lấy token đúng:**\n"
                "1. Mở altare.sh → đăng nhập\n"
                "2. F12 → tab **Network**\n"
                "3. Refresh trang\n"
                "4. Click bất kỳ request nào → Headers\n"
                "5. Copy giá trị **Authorization** (bắt đầu bằng `Bearer eyJ`)"
            )
        ok_start, msg_start = await self.api_start()
        if not ok_start:
            return False, f"Gọi API start thất bại: {msg_start}"
        self.running       = True
        self.session_start = datetime.now()
        self.status        = "hoạt động"
        session = aiohttp.ClientSession()
        loop    = asyncio.get_event_loop()
        self._tasks = [
            loop.create_task(self._loop_sse(session)),
            loop.create_task(self._loop_heartbeat()),
            loop.create_task(self._loop_stats()),
            loop.create_task(self._loop_notify(session)),
        ]
        log.info(f"[{self.name}] đã bắt đầu  (id={self.acc_id[:8]})")
        return True, "OK"

    async def stop(self):
        self.running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        await self.api_stop()
        log.info(f"[{self.name}] đã dừng")


# ═══════════════════════════════════════════════
#  GLOBAL LOG  —  gửi tổng toàn bộ hệ thống
# ═══════════════════════════════════════════════

_global_log_message_id: str | None = None

async def global_log_loop():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            await asyncio.sleep(GLOBAL_LOG_INTERVAL)
            try:
                await push_global_log(session)
            except Exception as e:
                log.warning(f"Global log lỗi: {e}")

async def push_global_log(session: aiohttp.ClientSession):
    global _global_log_message_id
    all_accs = list(runtime.values())
    if not all_accs:
        return

    total_balance = sum(a.balance for a in all_accs)
    total_earned  = sum(a.earned() for a in all_accs)
    total_hb_ok   = sum(a.hb_ok for a in all_accs)
    total_hb_fail = sum(a.hb_fail for a in all_accs)
    total_hb_rate = round(total_hb_ok / max(total_hb_ok + total_hb_fail, 1) * 100)
    active_count  = sum(1 for a in all_accs if a.status == "hoạt động")
    error_count   = sum(1 for a in all_accs if "lỗi" in a.status)
    total_pm      = round(sum(a._per_min_cache for a in all_accs), 4)

    # Bảng danh sách từng acc
    rows = []
    for i, a in enumerate(all_accs, 1):
        icon = a.status_icon()
        row  = db_get(a.acc_id)
        adder_id = row["added_by"] if row else "?"
        rows.append(
            f"{icon} **{i}. {a.name}**\n"
            f"┣ 💰 Số dư: `{a.balance:.4f} cr`  •  📈 Kiếm: `+{a.earned():.4f} cr`\n"
            f"┣ ⏱️ Uptime: `{a.elapsed_str()}`  •  💓 HB: `{a.hb_rate()}%`  •  🔄 Restart: `{a.restart_count}×`\n"
            f"┗ 👤 Thêm bởi: <@{adder_id}>"
        )

    acc_list = "\n\n".join(rows) if rows else "*(trống)*"

    payload = {
        "username":   "Altare Hệ Thống",
        "avatar_url": "https://altare.sh/favicon.ico",
        "embeds": [{
            "title": "🖥️  TỔNG QUAN HỆ THỐNG  —  ALTARE AFK",
            "color": 0x00d4aa,
            "fields": [
                # ─── Hàng 1: chỉ số tổng lớn ───
                {
                    "name":   "━━━━━━━━  📊 CHỈ SỐ TỔNG  ━━━━━━━━",
                    "value":  "\u200b",
                    "inline": False
                },
                {
                    "name":   "💰  Tổng số dư",
                    "value":  f"```fix\n{total_balance:.4f} cr\n```",
                    "inline": True
                },
                {
                    "name":   "📈  Tổng kiếm được",
                    "value":  f"```diff\n+ {total_earned:.4f} cr\n```",
                    "inline": True
                },
                {
                    "name":   "⚡  Tốc độ tổng",
                    "value":  f"```fix\n{total_pm} cr/phút\n```",
                    "inline": True
                },
                # ─── Hàng 2: trạng thái hệ thống ───
                {
                    "name":   "🖥️  Tổng tài khoản",
                    "value":  f"```fix\n{len(all_accs)} / {MAX_ACC} slot\n```",
                    "inline": True
                },
                {
                    "name":   "🟢  Đang hoạt động",
                    "value":  f"```fix\n{active_count} tài khoản\n```",
                    "inline": True
                },
                {
                    "name":   "🔴  Đang lỗi",
                    "value":  f"```fix\n{error_count} tài khoản\n```",
                    "inline": True
                },
                # ─── Hàng 3: heartbeat tổng ───
                {
                    "name":   "💓  Heartbeat tổng",
                    "value":  f"```fix\n✓ {total_hb_ok}   ✗ {total_hb_fail}   ({total_hb_rate}% OK)\n```",
                    "inline": False
                },
                # ─── Danh sách từng acc ───
                {
                    "name":   "━━━━━━━━  📋 DANH SÁCH TÀI KHOẢN  ━━━━━━━━",
                    "value":  acc_list[:4000],
                    "inline": False
                },
            ],
            "footer": {
                "text":     f"🔄 Tự động cập nhật mỗi {GLOBAL_LOG_INTERVAL}s  •  {now_str()}",
                "icon_url": "https://altare.sh/favicon.ico"
            },
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }]
    }

    result = await send_webhook(session, GLOBAL_LOG_WEBHOOK, payload, _global_log_message_id)
    if result:
        _global_log_message_id = result


async def autocomplete_acc(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"{a.status_icon()} {a.name}", value=a.acc_id)
        for a in runtime.values()
        if current.lower() in a.name.lower() or current.lower() in a.acc_id
    ][:25]


@client.event
async def on_ready():
    db_init()
    rows   = db_all()
    loaded = 0
    print(f"\n{'═'*55}")
    print(f"  Bot      : {client.user}")
    print(f"  Configs  : ./{CONFIGS_DIR}/")
    print(f"  Database : afk.db")
    print(f"  Cooldown : {CMD_COOLDOWN}s / lệnh")
    print(f"  Khôi phục: {len(rows)} tài khoản...")
    print(f"{'═'*55}")
    for row in rows:
        fpath = row["file_path"]
        if not os.path.exists(fpath):
            print(f"  ✗  {row['name']}  —  file không tồn tại: {fpath}")
            continue
        with open(fpath, encoding="utf-8") as f:
            cfg = json.load(f)
        acc = Account(row["acc_id"], row["name"], cfg, row["added_by"])
        ok, msg = await acc.start()
        if ok:
            runtime[acc.acc_id] = acc
            loaded += 1
            print(f"  ✓  {row['name']}  id={row['acc_id'][:8]}  by={row['added_by']}")
        else:
            print(f"  ✗  {row['name']}  —  {msg}")
    print(f"{'═'*55}")
    print(f"  Khôi phục thành công: {loaded}/{len(rows)}")
    print(f"{'═'*55}\n")
    await tree.sync()
    asyncio.get_event_loop().create_task(global_log_loop())
    async with aiohttp.ClientSession() as s:
        await push_global_log(s)


# ═══════════════════════════════════════════════
#  LỆNH /thêm
# ═══════════════════════════════════════════════

@tree.command(name="thêm", description="Thêm tài khoản AFK vào hệ thống chung")
async def cmd_them(interaction: discord.Interaction, file: discord.Attachment):
    uid       = interaction.user.id
    remaining = check_cooldown(uid)
    if remaining:
        embed = discord.Embed(
            title="⏳  Chờ một chút!",
            description=f"Bạn cần chờ thêm **{remaining}s** trước khi dùng lệnh tiếp theo.",
            color=0xf39c12
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    set_cooldown(uid)

    if db_count() >= MAX_ACC:
        embed = discord.Embed(
            title="❌  Hết slot",
            description=f"Hệ thống đã đạt tối đa **{MAX_ACC} tài khoản**.\nXoá bớt trước khi thêm mới.",
            color=0xe74c3c
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if not file.filename.endswith(".json"):
        embed = discord.Embed(
            title="❌  Sai định dạng",
            description="Chỉ chấp nhận file **`.json`**.",
            color=0xe74c3c
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        cfg = json.loads(await file.read())
    except Exception:
        await interaction.followup.send(
            embed=discord.Embed(title="❌  File JSON không hợp lệ", description="Kiểm tra lại định dạng file.", color=0xe74c3c),
            ephemeral=True
        )
        return

    token = cfg.get("token", "").strip()
    if not token:
        await interaction.followup.send(
            embed=discord.Embed(title="❌  Thiếu token", description="File JSON phải có trường `token`.", color=0xe74c3c),
            ephemeral=True
        )
        return

    # Kiểm tra token có vẻ hợp lệ không (JWT bắt đầu bằng eyJ)
    raw_jwt = token.replace("Bearer ", "").replace("bearer ", "").strip()
    if not raw_jwt.startswith("eyJ"):
        embed_warn = discord.Embed(
            title="⚠️  Token có thể không hợp lệ",
            description=(
                "Token của bạn không có dạng JWT (`eyJ...`)\n\n"
                "**Cách lấy token đúng từ altare.sh:**\n"
                "1. Mở altare.sh → đăng nhập\n"
                "2. Nhấn `F12` → chọn tab **Network**\n"
                "3. Refresh trang (`Ctrl+R`)\n"
                "4. Click vào bất kỳ request nào tới `altare.sh`\n"
                "5. Tìm tab **Headers** → tìm dòng **Authorization**\n"
                "6. Copy toàn bộ giá trị (bắt đầu bằng `Bearer eyJ...`)\n\n"
                "Bot vẫn sẽ thử khởi động, nhưng có thể thất bại."
            ),
            color=0xf39c12
        )
        await interaction.followup.send(embed=embed_warn, ephemeral=True)

    name   = cfg.get("name", "").strip() or file.filename.removesuffix(".json")
    acc_id = str(uuid.uuid4())
    fpath  = os.path.join(CONFIGS_DIR, f"{acc_id}.json")

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    acc = Account(acc_id, name, cfg, uid)
    ok, msg = await acc.start()

    if not ok:
        os.remove(fpath)
        await interaction.followup.send(
            embed=discord.Embed(title="❌  Khởi động thất bại", description=f"**Lỗi:** {msg}", color=0xe74c3c),
            ephemeral=True
        )
        return

    runtime[acc_id] = acc
    db_insert(acc_id, name, fpath, uid)
    slot_con_lai = MAX_ACC - db_count()

    embed = discord.Embed(
        title="✅  Thêm tài khoản thành công!",
        color=0x2ecc71
    )
    embed.add_field(
        name="📋  Thông tin tài khoản",
        value=(
            f"**Tên:** {name}\n"
            f"**ID:** `{acc_id[:12]}...`\n"
            f"**Tenant:** `{acc.tenant_id[:24]}...`"
        ),
        inline=True
    )
    embed.add_field(
        name="⚙️  Cấu hình",
        value=(
            f"**Heartbeat:** `{acc.heartbeat_interval}s`\n"
            f"**Cập nhật số dư:** `{acc.stats_interval}s`\n"
            f"**Thông báo:** `{acc.notify_interval}s`"
        ),
        inline=True
    )
    embed.add_field(
        name="🖥️  Hệ thống",
        value=(
            f"**Slot còn lại:** `{slot_con_lai}/{MAX_ACC}`\n"
            f"**File config:** `{acc_id[:8]}....json`"
        ),
        inline=True
    )
    embed.set_footer(text=f"Thêm bởi {interaction.user}  •  {now_str()}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    async with aiohttp.ClientSession() as s:
        await push_global_log(s)


# ═══════════════════════════════════════════════
#  LỆNH /xóa
# ═══════════════════════════════════════════════

@tree.command(name="xóa", description="Dừng và xoá một tài khoản AFK")
@app_commands.describe(tài_khoản="Chọn tài khoản muốn xoá")
@app_commands.autocomplete(tài_khoản=autocomplete_acc)
async def cmd_xoa(interaction: discord.Interaction, tài_khoản: str):
    uid       = interaction.user.id
    remaining = check_cooldown(uid)
    if remaining:
        embed = discord.Embed(
            title="⏳  Chờ một chút!",
            description=f"Bạn cần chờ thêm **{remaining}s** trước khi dùng lệnh tiếp theo.",
            color=0xf39c12
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    set_cooldown(uid)

    acc = runtime.get(tài_khoản)
    if not acc:
        embed = discord.Embed(
            title="❌  Không tìm thấy",
            description="Tài khoản này không tồn tại.\nDùng `/danh-sách` để xem toàn bộ.",
            color=0xe74c3c
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    row  = db_get(tài_khoản)
    name = acc.name
    await acc.stop()
    del runtime[tài_khoản]
    db_delete(tài_khoản)

    if row and os.path.exists(row["file_path"]):
        os.remove(row["file_path"])
        log.info(f"Đã xoá file config: {row['file_path']}")

    embed = discord.Embed(
        title="🗑️  Đã xoá tài khoản",
        color=0xe74c3c
    )
    embed.add_field(
        name="📋  Tài khoản đã xoá",
        value=(
            f"**Tên:** {name}\n"
            f"**Trạng thái:** Đã dừng hoàn toàn\n"
            f"**File config:** Đã xoá\n"
            f"**Database:** Đã cập nhật"
        ),
        inline=False
    )
    embed.set_footer(text=f"Xoá bởi {interaction.user}  •  {now_str()}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    async with aiohttp.ClientSession() as s:
        await push_global_log(s)


# ═══════════════════════════════════════════════
#  LỆNH /danh-sách
# ═══════════════════════════════════════════════

@tree.command(name="danh-sách", description="Xem toàn bộ tài khoản AFK trong hệ thống")
async def cmd_danh_sach(interaction: discord.Interaction):
    uid       = interaction.user.id
    remaining = check_cooldown(uid)
    if remaining:
        embed = discord.Embed(
            title="⏳  Chờ một chút!",
            description=f"Bạn cần chờ thêm **{remaining}s** trước khi dùng lệnh tiếp theo.",
            color=0xf39c12
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    set_cooldown(uid)

    if not runtime:
        embed = discord.Embed(
            title="📋  Danh sách tài khoản",
            description="Chưa có tài khoản nào trong hệ thống.\n\nDùng `/thêm` để bắt đầu!",
            color=0x95a5a6
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    all_accs      = list(runtime.values())
    total_balance = sum(a.balance for a in all_accs)
    total_earned  = sum(a.earned() for a in all_accs)
    active_count  = sum(1 for a in all_accs if a.status == "hoạt động")

    embed = discord.Embed(
        title=f"📋  Danh sách tài khoản AFK  —  {len(all_accs)}/{MAX_ACC}",
        color=0x00d4aa
    )

    # Tổng quan nhanh ở đầu
    embed.add_field(
        name="📊  Tóm tắt hệ thống",
        value=(
            f"🟢 Đang chạy: **{active_count}**  •  "
            f"💰 Tổng số dư: **{total_balance:.4f} cr**  •  "
            f"📈 Tổng kiếm: **+{total_earned:.4f} cr**"
        ),
        inline=False
    )

    # Từng acc
    for i, acc in enumerate(all_accs, 1):
        icon = acc.status_icon()
        embed.add_field(
            name=f"{icon}  {i}. {acc.name}",
            value=(
                f"💰 `{acc.balance:.4f} cr`  📈 `+{acc.earned():.4f}`\n"
                f"⏱️ `{acc.elapsed_str()}`  💓 `{acc.hb_rate()}%`  🔄 `{acc.restart_count}×`\n"
                f"👤 <@{acc.added_by}>"
            ),
            inline=True
        )

    embed.set_footer(text=f"Dùng /trạng-thái để xem chi tiết  •  {now_str()}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════
#  LỆNH /trạng-thái
# ═══════════════════════════════════════════════

@tree.command(name="trạng-thái", description="Xem chi tiết một tài khoản AFK")
@app_commands.describe(tài_khoản="Chọn tài khoản muốn xem")
@app_commands.autocomplete(tài_khoản=autocomplete_acc)
async def cmd_trang_thai(interaction: discord.Interaction, tài_khoản: str):
    uid       = interaction.user.id
    remaining = check_cooldown(uid)
    if remaining:
        embed = discord.Embed(
            title="⏳  Chờ một chút!",
            description=f"Bạn cần chờ thêm **{remaining}s** trước khi dùng lệnh tiếp theo.",
            color=0xf39c12
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    set_cooldown(uid)

    acc = runtime.get(tài_khoản)
    if not acc:
        embed = discord.Embed(
            title="❌  Không tìm thấy",
            description="Tài khoản này không tồn tại.\nDùng `/danh-sách` để xem toàn bộ.",
            color=0xe74c3c
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    per_min = await acc.fetch_per_minute()
    row     = db_get(tài_khoản)
    icon    = acc.status_icon()
    color   = 0x2ecc71 if acc.status == "hoạt động" else 0xe67e22

    embed = discord.Embed(
        title=f"{icon}  Chi tiết — {acc.name}",
        color=color
    )

    embed.add_field(
        name="📡  Trạng thái vận hành",
        value=(
            f"**Trạng thái:** {icon} {acc.status.capitalize()}\n"
            f"**Khởi động lại:** `{acc.restart_count} lần`\n"
            f"**ID tài khoản:** `{acc.acc_id[:16]}...`"
        ),
        inline=False
    )
    embed.add_field(
        name="💰  Tài chính",
        value=(
            f"**Số dư:** `{acc.balance:.4f} cr`\n"
            f"**Kiếm được:** `+{acc.earned():.4f} cr`\n"
            f"**Tốc độ:** `{per_min} cr/phút`"
        ),
        inline=True
    )
    embed.add_field(
        name="⏱️  Thời gian",
        value=(
            f"**Uptime:** `{acc.elapsed_str()}`\n"
            f"**Heartbeat:** `{acc.heartbeat_interval}s`\n"
            f"**Thêm lúc:** `{row['added_at'] if row else '?'}`"
        ),
        inline=True
    )
    embed.add_field(
        name="💓  Heartbeat",
        value=(
            f"**Tỉ lệ OK:** `{acc.hb_rate()}%`\n"
            f"**Thành công:** `{acc.hb_ok}`\n"
            f"**Thất bại:** `{acc.hb_fail}`"
        ),
        inline=True
    )
    embed.add_field(
        name="🔑  Thông tin kỹ thuật",
        value=(
            f"**Tenant ID:**\n`{acc.tenant_id}`"
        ),
        inline=False
    )
    embed.add_field(
        name="👤  Người thêm",
        value=f"<@{acc.added_by}>",
        inline=True
    )
    if row:
        embed.add_field(
            name="📁  File config",
            value=f"`{os.path.basename(row['file_path'])[:20]}...`",
            inline=True
        )

    embed.set_footer(text=f"Cập nhật lúc {now_str()}")
    await interaction.followup.send(embed=embed, ephemeral=True)



client.run(BOT_TOKEN)
