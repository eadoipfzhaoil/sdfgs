import hashlib
import hmac
import os
from datetime import date, timedelta

from sqlalchemy import case, select, update
from sqlalchemy.orm import defer

import config
from models import Base, ChatMessage, Generation, Plan, Setting, SessionLocal, User, engine, utcnow

KINDS = ("chat", "image", "video")


# ---------- پسورد ----------
def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False


# ---------- تنظیمات ----------
def get_setting(db, key: str) -> str:
    s = db.get(Setting, key)
    return s.value if s else ""


def set_setting(db, key: str, value: str):
    s = db.get(Setting, key)
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


# ---------- پلن و سهمیه ----------
def free_plan(db) -> Plan:
    return db.scalar(select(Plan).where(Plan.is_free == True).order_by(Plan.id))  # noqa: E712


def refresh_user(db, u: User):
    today = date.today().isoformat()
    changed = False
    if u.usage_date != today:
        u.usage_date = today
        u.chat_used = u.image_used = u.video_used = 0
        changed = True
    if u.expires_at and u.expires_at < utcnow():
        u.plan = free_plan(db)
        u.expires_at = None
        changed = True
    if changed:
        db.commit()


def usage(u: User) -> dict:
    return {k: {"used": getattr(u, f"{k}_used"), "limit": getattr(u.plan, f"{k}_limit")} for k in KINDS}


def consume(db, u: User, kind: str) -> bool:
    assert kind in KINDS
    col = getattr(User, f"{kind}_used")
    limit = getattr(u.plan, f"{kind}_limit")
    res = db.execute(update(User).where(User.id == u.id, col < limit).values({f"{kind}_used": col + 1}))
    db.commit()
    if res.rowcount:
        setattr(u, f"{kind}_used", getattr(u, f"{kind}_used") + 1)
        return True
    return False


def refund(db, user_id: int, kind: str):
    assert kind in KINDS
    col = getattr(User, f"{kind}_used")
    db.execute(update(User).where(User.id == user_id).values({f"{kind}_used": case((col > 0, col - 1), else_=0)}))
    db.commit()


def activate(db, u: User, plan: Plan):
    now = utcnow()
    base = u.expires_at if (u.plan_id == plan.id and u.expires_at and u.expires_at > now) else now
    u.plan = plan
    u.expires_at = base + timedelta(days=plan.days)
    db.commit()


# ---------- راه‌اندازی ----------
def init_db():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(Plan.id).limit(1)):
            db.add(Plan(name="رایگان", price=0, days=0, chat_limit=10, image_limit=1, video_limit=0, is_free=True))
            db.add(Plan(name="اشتراک ماهانه", price=150000, days=30, chat_limit=200, image_limit=20, video_limit=3))
            db.commit()
        for k, v in (("card_number", config.CARD_NUMBER), ("card_holder", config.CARD_HOLDER)):
            if not db.get(Setting, k):
                db.add(Setting(key=k, value=v))
        db.commit()
        # کارهای نیمه‌تمام قبل از ری‌استارت را خراب‌شده حساب کن و سهمیه را برگردان
        stale = db.scalars(
            select(Generation).where(Generation.status.in_(["pending", "running"])).options(defer(Generation.data))
        ).all()
        for g in stale:
            g.status, g.error = "failed", "server restarted"
            refund(db, g.user_id, g.kind)
        db.commit()
