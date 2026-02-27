import time
import uuid
import hashlib
import urllib.parse
from decimal import Decimal, InvalidOperation

import aiosqlite
import httpx

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN, OKPAY_ID, OKPAY_TOKEN, RETURN_URL, CALLBACK_URL

DB_PATH = "data.db"

# 指令价格
COST = {
    "ddz": Decimal("1.5"),    # 单地址
    "dt": Decimal("1.8"),     # 单头
    "zdz_2": Decimal("4.5"),  # 真地址(备用)
    "jdz": Decimal("1.8"),    # 假地址
}


# ========== OkayPay 客户端 ==========
class OkayPayClient:
    def __init__(self, merchant_id: str, token: str):
        self.id = str(merchant_id).strip()
        self.token = str(token).strip()
        self.base = "https://api.okaypay.me/shop/"

    @staticmethod
    def _php_truthy(value) -> bool:
        """
        尽量模拟 PHP 中 array_filter()（无回调）的真假判断：
        false, 0, 0.0, "", "0", None, 空数组 等都视为 false
        """
        if value is None:
            return False
        if value is False:
            return False
        if value == 0 or value == 0.0:
            return False
        if isinstance(value, str) and (value == "" or value == "0"):
            return False
        if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
            return False
        return True

    def _sign_data(self, data: dict, *, keep_zero: bool, use_urldecode_plus: bool) -> dict:
        payload = dict(data)
        payload["id"] = self.id

        if keep_zero:
            # 保留 0，只过滤 None 和 ""
            filtered = {k: v for k, v in payload.items() if v is not None and v != ""}
        else:
            # 严格模拟 PHP array_filter（无回调）
            filtered = {k: v for k, v in payload.items() if self._php_truthy(v)}

        # 签名前按键名排序
        sorted_items = sorted(filtered.items(), key=lambda x: x[0])

        # http_build_query 等价近似
        query = urllib.parse.urlencode(sorted_items, doseq=True)

        # PHP 的 urldecode 会把 %XX 解码，并把 + 变为空格
        if use_urldecode_plus:
            decoded = urllib.parse.unquote_plus(query)
        else:
            decoded = urllib.parse.unquote(query)

        sign_src = f"{decoded}&token={self.token}"
        sign = hashlib.md5(sign_src.encode("utf-8")).hexdigest().upper()

        body = dict(filtered)
        body["sign"] = sign
        return body

    @staticmethod
    def _is_auth_failed(resp: dict) -> bool:
        if not isinstance(resp, dict):
            return False
        status = str(resp.get("status", "")).lower()
        msg = f"{resp.get('msg', '')}{resp.get('message', '')}"
        return ("身份认证失败" in msg) or (status in {"warning", "error"} and "认证" in msg)

    async def _post(self, endpoint: str, data: dict) -> dict:
        url = self.base + endpoint

        # 尝试多种签名兼容模式，优先使用更接近 PHP array_filter 的方式
        # 解决“身份认证失败”场景（签名规则差异）
        strategies = [
            # 1) PHP array_filter + urldecode
            {"keep_zero": False, "use_urldecode_plus": True},
            # 2) 保留0 + urldecode
            {"keep_zero": True, "use_urldecode_plus": True},
            # 3) PHP array_filter + unquote
            {"keep_zero": False, "use_urldecode_plus": False},
            # 4) 保留0 + unquote
            {"keep_zero": True, "use_urldecode_plus": False},
        ]

        last_resp = None
        last_err = None

        async with httpx.AsyncClient(timeout=15) as client:
            for st in strategies:
                try:
                    body = self._sign_data(data, **st)
                    r = await client.post(url, data=body)
                    r.raise_for_status()
                    resp = r.json()
                    last_resp = resp

                    # 如果是认证失败，换下一套签名再试
                    if self._is_auth_failed(resp):
                        continue

                    return resp
                except Exception as e:
                    last_err = e
                    continue

        if last_resp is not None:
            return last_resp
        raise RuntimeError(f"OkayPay 请求失败: {last_err}")

    async def pay_link(self, unique_id: str, amount: Decimal, name: str = "TG充值") -> dict:
        # 注意：有些商户接口把 status=0 视为无效字段并参与签名造成认证失败，这里移除
        data = {
            "unique_id": unique_id,
            "name": name,
            "amount": str(amount),
            "return_url": RETURN_URL,
            "coin": "USDT",
            "callback_url": CALLBACK_URL,
        }
        return await self._post("payLink", data)

    async def check_deposit(self, unique_id: str) -> dict:
        data = {"unique_id": unique_id}
        return await self._post("checkDeposit", data)


okpay = OkayPayClient(OKPAY_ID, OKPAY_TOKEN)


# ========== 数据库 ==========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            points TEXT DEFAULT '0',
            created_at INTEGER
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            unique_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount TEXT,
            coin TEXT,
            order_id TEXT,
            pay_url TEXT,
            status INTEGER DEFAULT 0,   -- 0未支付 1已支付
            credited INTEGER DEFAULT 0, -- 0未入账 1已入账
            created_at INTEGER,
            paid_at INTEGER
        )
        """)
        await db.commit()


async def ensure_user(user) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT OR IGNORE INTO users(user_id, username, full_name, points, created_at)
        VALUES(?, ?, ?, '0', ?)
        """, (user.id, user.username or "", user.full_name or "", int(time.time())))
        await db.execute("""
        UPDATE users SET username=?, full_name=? WHERE user_id=?
        """, (user.username or "", user.full_name or "", user.id))
        await db.commit()


async def get_points(user_id: int) -> Decimal:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT points FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return Decimal("0")
            return Decimal(row[0])


async def deduct_points_if_enough(user_id: int, amount: Decimal) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        # 原子扣款
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT points FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            current = Decimal(row[0]) if row else Decimal("0")
        if current < amount:
            await db.execute("ROLLBACK")
            return False
        new_points = current - amount
        await db.execute("UPDATE users SET points=? WHERE user_id=?", (str(new_points), user_id))
        await db.commit()
        return True


async def create_order(unique_id: str, user_id: int, amount: Decimal, order_id: str, pay_url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO orders(unique_id, user_id, amount, coin, order_id, pay_url, status, credited, created_at)
        VALUES(?, ?, ?, 'USDT', ?, ?, 0, 0, ?)
        """, (unique_id, user_id, str(amount), order_id, pay_url, int(time.time())))
        await db.commit()


async def get_order(unique_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
        SELECT unique_id, user_id, amount, order_id, pay_url, status, credited
        FROM orders WHERE unique_id=?
        """, (unique_id,)) as cur:
            return await cur.fetchone()


async def mark_order_paid_and_credit(unique_id: str) -> tuple[bool, str]:
    """
    返回: (True/False, 文案)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("""
        SELECT user_id, amount, status, credited FROM orders WHERE unique_id=?
        """, (unique_id,)) as cur:
            row = await cur.fetchone()

        if not row:
            await db.execute("ROLLBACK")
            return False, "订单不存在"

        user_id, amount_str, status, credited = row
        amount = Decimal(amount_str)

        if credited == 1:
            await db.execute("ROLLBACK")
            return True, "该订单已入账，无需重复操作。"

        # 入账
        await db.execute(
            "UPDATE orders SET status=1, credited=1, paid_at=? WHERE unique_id=?",
            (int(time.time()), unique_id)
        )

        async with db.execute("SELECT points FROM users WHERE user_id=?", (user_id,)) as cur:
            u = await cur.fetchone()
            current = Decimal(u[0]) if u else Decimal("0")
        new_points = current + amount  # 1 USDT = 1 积分
        await db.execute("UPDATE users SET points=? WHERE user_id=?", (str(new_points), user_id))

        await db.commit()
        return True, f"充值成功✅ 已到账 {amount} 积分"


# ========== 文本 ==========
START_TEXT = (
    "本机器人为全网个户底价,虽然不确定是不是源头,但你找不到比这更低的价格了\n"
    "1usdt=1积分"
)

INSUFFICIENT_TEXT = "积分不足❌️,请输入/cz [金额]进行补充!"
MAINTAIN_TEXT = "当前接口可能正在维护,请五分钟后再试,如任然不行,不要咨询客服,等待即可"


def start_keyboard():
    # Telegram 按钮本身不支持 Markdown 加粗，这里用全角样式模拟强调
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("【单地址-1.5积分】", callback_data="btn_ddz")],
        [InlineKeyboardButton("【单头-1.8积分】", callback_data="btn_dt")],
        [InlineKeyboardButton("【真地址-4积分】", callback_data="btn_zdz")],
        [InlineKeyboardButton("【真地址(备用)-4.5积分】", callback_data="btn_zdz2")],
        [InlineKeyboardButton("【假地址-1.8积分】", callback_data="btn_jdz")],
    ])


# ========== Handler ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    await update.message.reply_text(START_TEXT, reply_markup=start_keyboard())


async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "btn_ddz":
        await q.message.reply_text("使用指令:规范格式 /ddz [姓名] [身份证]")
    elif data == "btn_dt":
        await q.message.reply_text("请发送 /dt [姓名] [身份证]")
    elif data == "btn_zdz":
        await q.message.reply_text("正在维护中...稍安勿躁")
    elif data == "btn_zdz2":
        await q.message.reply_text("请使用指令/zdz_2 [姓名] [身份证]")
    elif data == "btn_jdz":
        await q.message.reply_text("请使用指令 /jdz [姓名] [身份证]")
    elif data.startswith("checkpay:"):
        unique_id = data.split(":", 1)[1]
        await handle_check_payment(q, unique_id)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    points = await get_points(user.id)

    text = (
        f"用户基础信息\n"
        f"TGID: <code>{user.id}</code>\n"
        f"用户名: @{user.username if user.username else '未设置'}\n"
        f"积分: <b>{points}</b>"
    )

    photos = await context.bot.get_user_profile_photos(user.id, limit=1)
    if photos.total_count > 0:
        file_id = photos.photos[0][0].file_id
        await update.message.reply_photo(photo=file_id, caption=text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def parse_name_id_args(text: str) -> bool:
    parts = text.strip().split()
    return len(parts) == 3


async def paid_command(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str):
    user = update.effective_user
    await ensure_user(user)

    if not parse_name_id_args(update.message.text):
        await update.message.reply_text("格式错误❌️")
        return

    need = COST[cmd]
    ok = await deduct_points_if_enough(user.id, need)
    if not ok:
        await update.message.reply_text(INSUFFICIENT_TEXT)
        return

    await update.message.reply_text(MAINTAIN_TEXT)


async def cmd_dt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paid_command(update, context, "dt")


async def cmd_ddz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paid_command(update, context, "ddz")


async def cmd_zdz2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paid_command(update, context, "zdz_2")


async def cmd_jdz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await paid_command(update, context, "jdz")


def _extract_pay_result(resp: dict):
    """
    兼容多种返回格式，返回:
    (success: bool, order_id: str, pay_url: str)
    """
    if not isinstance(resp, dict):
        return False, "", ""

    code = resp.get("code")
    status = str(resp.get("status", "")).lower()
    data = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}

    order_id = (
        data.get("order_id")
        or data.get("orderId")
        or resp.get("order_id")
        or resp.get("orderId")
        or ""
    )
    pay_url = (
        data.get("pay_url")
        or data.get("payUrl")
        or data.get("url")
        or data.get("link")
        or resp.get("pay_url")
        or resp.get("payUrl")
        or ""
    )

    ok_code = code in (10000, 0, 200)
    ok_status = status in {"success", "ok", "1", "true"}

    success = bool(pay_url) and (ok_code or ok_status)
    return success, str(order_id), str(pay_url)


async def cz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)

    # /cz [金额]
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("请使用 /cz [金额]")
        return

    try:
        amount = Decimal(context.args[0])
    except InvalidOperation:
        await update.message.reply_text("请使用 /cz [金额]")
        return

    if amount < Decimal("3"):
        await update.message.reply_text("金额过低,请至少充值3U❌️")
        return
    if amount > Decimal("10000"):
        await update.message.reply_text("金额过高,最高单次充值一万U!❌️")
        return

    unique_id = f"cz_{user.id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    try:
        resp = await okpay.pay_link(unique_id=unique_id, amount=amount, name=f"TG充值_{user.id}")
    except Exception:
        await update.message.reply_text("创建支付失败，请稍后再试❌️")
        return

    success, order_id, pay_url = _extract_pay_result(resp)

    if not success:
        await update.message.reply_text(f"创建支付失败❌️\n返回: {resp}")
        return

    await create_order(unique_id, user.id, amount, order_id or "", pay_url)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 去支付", url=pay_url)],
        [InlineKeyboardButton("✅ 我已支付，点击查询", callback_data=f"checkpay:{unique_id}")]
    ])

    await update.message.reply_text(
        f"订单已创建\n"
        f"订单号: <code>{order_id or '未知'}</code>\n"
        f"金额: <b>{amount} USDT</b>\n"
        f"请先完成支付，再点击“我已支付，点击查询”",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )


def _extract_paid_status(resp: dict) -> bool:
    """
    兼容多种 checkDeposit 返回格式
    """
    if not isinstance(resp, dict):
        return False

    code = resp.get("code")
    status = str(resp.get("status", "")).lower()
    data = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}

    pay_status = data.get("status", data.get("pay_status", 0))
    paid_flag = str(pay_status) in {"1", "true", "paid", "success"}

    if code in (10000, 0, 200) and paid_flag:
        return True
    if status in {"success", "ok"} and paid_flag:
        return True
    return False


async def handle_check_payment(q, unique_id: str):
    row = await get_order(unique_id)
    if not row:
        await q.message.reply_text("订单不存在或已失效❌️")
        return

    _, user_id, amount, order_id, pay_url, status, credited = row
    if q.from_user.id != user_id:
        await q.message.reply_text("这不是你的订单❌️")
        return

    if credited == 1:
        await q.message.reply_text("该订单已入账，无需重复查询。")
        return

    try:
        resp = await okpay.check_deposit(unique_id)
    except Exception:
        await q.message.reply_text("查询失败，请稍后重试❌️")
        return

    if _extract_paid_status(resp):
        ok, msg = await mark_order_paid_and_credit(unique_id)
        await q.message.reply_text(msg if ok else "入账失败，请联系管理员")
    else:
        await q.message.reply_text("暂未检测到支付成功，请完成支付后再查询。")


async def set_commands(app: Application):
    cmds = [
        BotCommand("start", "开始"),
        BotCommand("info", "查看基础信息"),
        BotCommand("dt", "单头查询"),
        BotCommand("ddz", "单地址查询"),
        BotCommand("zdz_2", "真地址(备用)"),
        BotCommand("jdz", "假地址查询"),
        BotCommand("cz", "充值"),
    ]
    await app.bot.set_my_commands(cmds)


async def on_startup(app: Application):
    await init_db()
    await set_commands(app)
    print("Bot started.")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("dt", cmd_dt))
    app.add_handler(CommandHandler("ddz", cmd_ddz))
    app.add_handler(CommandHandler("zdz_2", cmd_zdz2))
    app.add_handler(CommandHandler("jdz", cmd_jdz))
    app.add_handler(CommandHandler("cz", cz))
    app.add_handler(CallbackQueryHandler(button_click))

    app.run_polling()


if __name__ == "__main__":
    main()