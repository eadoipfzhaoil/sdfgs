import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import defer
from starlette.middleware.sessions import SessionMiddleware

import ai
import config
import core
from models import ChatMessage, Generation, Payment, Plan, SessionLocal, User

log = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    core.init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=60 * 60 * 24 * 30, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = lambda v: f"{int(v):,}"


def render(request: Request, name: str, **ctx):
    ctx.setdefault("currency", config.CURRENCY)
    return templates.TemplateResponse(request, name, ctx)


# ---------- وابستگی‌ها ----------
def get_db():
    with SessionLocal() as db:
        yield db


class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def need_login(request: Request, exc: NeedLogin):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "ابتدا وارد شوید"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request, db=Depends(get_db)) -> User:
    uid = request.session.get("uid")
    u = db.get(User, uid) if uid else None
    if not u or u.banned:
        raise NeedLogin()
    core.refresh_user(db, u)
    return u


def admin_user(u: User = Depends(current_user)) -> User:
    if not u.is_admin:
        raise HTTPException(403, "دسترسی ندارید")
    return u


# ---------- PWA ----------
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest", media_type="application/manifest+json")


# ---------- احراز هویت ----------
@app.get("/")
def index(request: Request):
    return RedirectResponse("/app" if request.session.get("uid") else "/login", status_code=303)


@app.get("/login")
def login_page(request: Request):
    return render(request, "auth.html", mode="login", error=None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    email = email.strip().lower()
    u = db.scalar(select(User).where(User.email == email))
    if not u or not core.check_pw(password, u.password_hash):
        return render(request, "auth.html", mode="login", error="ایمیل یا رمز عبور اشتباه است")
    if u.banned:
        return render(request, "auth.html", mode="login", error="حساب شما مسدود شده است")
    if email in config.ADMIN_EMAILS and not u.is_admin:
        u.is_admin = True
        db.commit()
    request.session["uid"] = u.id
    return RedirectResponse("/app", status_code=303)


@app.get("/register")
def register_page(request: Request):
    return render(request, "auth.html", mode="register", error=None)


@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...), db=Depends(get_db)):
    email = email.strip().lower()
    err = None
    if "@" not in email or len(email) > 200:
        err = "ایمیل نامعتبر است"
    elif len(password) < 6:
        err = "رمز عبور حداقل ۶ کاراکتر باشد"
    elif db.scalar(select(User.id).where(User.email == email)):
        err = "این ایمیل قبلاً ثبت شده است"
    if err:
        return render(request, "auth.html", mode="register", error=err)
    u = User(
        email=email,
        password_hash=core.hash_pw(password),
        is_admin=email in config.ADMIN_EMAILS,
        plan=core.free_plan(db),
    )
    db.add(u)
    db.commit()
    request.session["uid"] = u.id
    return RedirectResponse("/app", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- صفحه اصلی برنامه ----------
@app.get("/app")
def app_page(request: Request, u: User = Depends(current_user)):
    return render(request, "app.html", user=u, usage=core.usage(u))


@app.get("/api/usage")
def api_usage(u: User = Depends(current_user)):
    return core.usage(u)


# ---------- چت ----------
class ChatIn(BaseModel):
    message: str


@app.get("/api/chat/history")
def chat_history(u: User = Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(
        select(ChatMessage).where(ChatMessage.user_id == u.id).order_by(ChatMessage.id.desc()).limit(50)
    ).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


@app.post("/api/chat/clear")
def chat_clear(u: User = Depends(current_user), db=Depends(get_db)):
    db.query(ChatMessage).filter(ChatMessage.user_id == u.id).delete()
    db.commit()
    return {"ok": True}


@app.post("/api/chat")
def api_chat(body: ChatIn, u: User = Depends(current_user), db=Depends(get_db)):
    msg = body.message.strip()[:4000]
    if not msg:
        raise HTTPException(400, "پیام خالی است")
    if not core.consume(db, u, "chat"):
        raise HTTPException(429, "سهمیه چت امروز شما تمام شده؛ برای افزایش، اشتراک بخرید")
    hist = db.scalars(
        select(ChatMessage).where(ChatMessage.user_id == u.id).order_by(ChatMessage.id.desc()).limit(12)
    ).all()
    msgs = [{"role": h.role, "content": h.content} for h in reversed(hist)] + [{"role": "user", "content": msg}]
    try:
        reply = ai.chat(msgs)
    except Exception:
        log.exception("chat failed")
        core.refund(db, u.id, "chat")
        raise HTTPException(502, "خطا در ارتباط با هوش مصنوعی؛ دوباره تلاش کنید")
    db.add(ChatMessage(user_id=u.id, role="user", content=msg))
    db.add(ChatMessage(user_id=u.id, role="assistant", content=reply))
    db.commit()
    return {"reply": reply, "usage": core.usage(u)}


# ---------- ساخت عکس / ویدیو ----------
class GenIn(BaseModel):
    kind: str
    prompt: str


def _mime(data: bytes, kind: str) -> str:
    if kind == "video":
        return "video/mp4"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/png"


def run_generation(gid: int):
    with SessionLocal() as db:
        g = db.get(Generation, gid)
        g.status = "running"
        db.commit()
        try:
            data = ai.image(g.prompt) if g.kind == "image" else ai.video(g.prompt)
            g.data, g.mime, g.status = data, _mime(data, g.kind), "done"
            db.commit()
        except Exception as e:
            log.exception("generation %s failed", gid)
            g.status, g.error = "failed", str(e)[:300]
            db.commit()
            core.refund(db, g.user_id, g.kind)


@app.post("/api/generate")
def api_generate(body: GenIn, u: User = Depends(current_user), db=Depends(get_db)):
    if body.kind not in ("image", "video"):
        raise HTTPException(400, "نوع نامعتبر")
    prompt = body.prompt.strip()[:1000]
    if not prompt:
        raise HTTPException(400, "توضیح خالی است")
    if body.kind == "video" and not config.REPLICATE_API_TOKEN:
        raise HTTPException(503, "ساخت ویدیو هنوز تنظیم نشده است")
    busy = db.scalar(
        select(func.count()).select_from(Generation).where(
            Generation.user_id == u.id, Generation.status.in_(["pending", "running"])
        )
    )
    if busy >= 2:
        raise HTTPException(429, "دو درخواست شما در حال پردازش است؛ کمی صبر کنید")
    label = "عکس" if body.kind == "image" else "ویدیو"
    if not core.consume(db, u, body.kind):
        raise HTTPException(429, f"سهمیه {label} امروز شما تمام شده؛ برای افزایش، اشتراک بخرید")
    g = Generation(user_id=u.id, kind=body.kind, prompt=prompt)
    db.add(g)
    db.commit()
    threading.Thread(target=run_generation, args=(g.id,), daemon=True).start()
    return {"id": g.id, "usage": core.usage(u)}


def _gen_dict(g: Generation) -> dict:
    return {
        "id": g.id,
        "kind": g.kind,
        "prompt": g.prompt,
        "status": g.status,
        "error": g.error,
        "url": f"/media/{g.id}" if g.status == "done" else None,
    }


@app.get("/api/generation/{gid}")
def api_generation(gid: int, u: User = Depends(current_user), db=Depends(get_db)):
    g = db.get(Generation, gid, options=[defer(Generation.data)])
    if not g or g.user_id != u.id:
        raise HTTPException(404)
    return _gen_dict(g)


@app.get("/api/generations")
def api_generations(kind: str = "image", u: User = Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(
        select(Generation)
        .where(Generation.user_id == u.id, Generation.kind == kind, Generation.status == "done")
        .options(defer(Generation.data))
        .order_by(Generation.id.desc())
        .limit(20)
    ).all()
    return [_gen_dict(g) for g in rows]


@app.get("/media/{gid}")
def media(gid: int, u: User = Depends(current_user), db=Depends(get_db)):
    g = db.get(Generation, gid)
    if not g or not g.data or (g.user_id != u.id and not u.is_admin):
        raise HTTPException(404)
    return Response(g.data, media_type=g.mime or "application/octet-stream")


# ---------- خرید اشتراک ----------
@app.get("/plans")
def plans_page(request: Request, ok: int = 0, u: User = Depends(current_user), db=Depends(get_db)):
    plans = db.scalars(select(Plan).where(Plan.active == True, Plan.is_free == False).order_by(Plan.id)).all()  # noqa: E712
    pays = db.scalars(
        select(Payment).where(Payment.user_id == u.id).options(defer(Payment.receipt)).order_by(Payment.id.desc()).limit(10)
    ).all()
    return render(
        request, "plans.html", user=u, plans=plans, payments=pays, ok=ok,
        card_number=core.get_setting(db, "card_number"), card_holder=core.get_setting(db, "card_holder"),
    )


@app.post("/plans/{pid}/buy")
async def buy(pid: int, receipt: UploadFile = File(...), u: User = Depends(current_user), db=Depends(get_db)):
    plan = db.get(Plan, pid)
    if not plan or not plan.active or plan.is_free:
        raise HTTPException(404, "پلن پیدا نشد")
    if not (receipt.content_type or "").startswith("image/"):
        raise HTTPException(400, "فقط عکس رسید قابل قبول است")
    data = await receipt.read()
    if not data or len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "حجم عکس باید کمتر از ۸ مگابایت باشد")
    db.add(Payment(user_id=u.id, plan_id=plan.id, amount=plan.price, receipt=data, receipt_mime=receipt.content_type))
    db.commit()
    return RedirectResponse("/plans?ok=1", status_code=303)


# ---------- پنل ادمین ----------
@app.get("/admin")
def admin_page(request: Request, admin: User = Depends(admin_user), db=Depends(get_db)):
    now = core.utcnow()
    stats = {
        "users": db.scalar(select(func.count()).select_from(User)),
        "active_paid": db.scalar(select(func.count()).select_from(User).where(User.expires_at > now)),
        "pending": db.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending")),
        "income": db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "approved")),
    }
    pending = db.scalars(
        select(Payment).where(Payment.status == "pending").options(defer(Payment.receipt)).order_by(Payment.id)
    ).all()
    return render(
        request, "admin.html", user=admin, stats=stats, pending=pending,
        plans=db.scalars(select(Plan).order_by(Plan.id)).all(),
        users=db.scalars(select(User).order_by(User.id.desc()).limit(100)).all(),
        card_number=core.get_setting(db, "card_number"), card_holder=core.get_setting(db, "card_holder"),
    )


def back():
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/card")
def admin_card(card_number: str = Form(...), card_holder: str = Form(...), a=Depends(admin_user), db=Depends(get_db)):
    core.set_setting(db, "card_number", card_number.strip())
    core.set_setting(db, "card_holder", card_holder.strip())
    return back()


@app.post("/admin/plan/new")
def plan_new(
    name: str = Form(...), price: int = Form(0), days: int = Form(30), chat_limit: int = Form(0),
    image_limit: int = Form(0), video_limit: int = Form(0), a=Depends(admin_user), db=Depends(get_db),
):
    db.add(Plan(name=name.strip(), price=max(price, 0), days=max(days, 1), chat_limit=max(chat_limit, 0),
                image_limit=max(image_limit, 0), video_limit=max(video_limit, 0)))
    db.commit()
    return back()


@app.post("/admin/plan/{pid}/edit")
def plan_edit(
    pid: int, name: str = Form(...), price: int = Form(0), days: int = Form(0), chat_limit: int = Form(0),
    image_limit: int = Form(0), video_limit: int = Form(0), a=Depends(admin_user), db=Depends(get_db),
):
    p = db.get(Plan, pid)
    if p:
        p.name, p.price, p.days = name.strip(), max(price, 0), max(days, 0)
        p.chat_limit, p.image_limit, p.video_limit = max(chat_limit, 0), max(image_limit, 0), max(video_limit, 0)
        db.commit()
    return back()


@app.post("/admin/plan/{pid}/toggle")
def plan_toggle(pid: int, a=Depends(admin_user), db=Depends(get_db)):
    p = db.get(Plan, pid)
    if p and not p.is_free:
        p.active = not p.active
        db.commit()
    return back()


@app.get("/admin/receipt/{pid}")
def receipt_img(pid: int, a=Depends(admin_user), db=Depends(get_db)):
    p = db.get(Payment, pid)
    if not p or not p.receipt:
        raise HTTPException(404)
    return Response(p.receipt, media_type=p.receipt_mime)


@app.post("/admin/payment/{pid}/{action}")
def payment_action(pid: int, action: str, a=Depends(admin_user), db=Depends(get_db)):
    p = db.get(Payment, pid)
    if p and p.status == "pending" and action in ("approve", "reject"):
        if action == "approve":
            p.status = "approved"
            core.activate(db, p.user, p.plan)
        else:
            p.status = "rejected"
        db.commit()
    return back()


@app.post("/admin/user/give")
def user_give(user_id: int = Form(...), plan_id: int = Form(...), a=Depends(admin_user), db=Depends(get_db)):
    u, p = db.get(User, user_id), db.get(Plan, plan_id)
    if u and p:
        core.activate(db, u, p)
    return back()


@app.post("/admin/user/{uid}/ban")
def user_ban(uid: int, a=Depends(admin_user), db=Depends(get_db)):
    u = db.get(User, uid)
    if u and not u.is_admin:
        u.banned = not u.banned
        db.commit()
    return back()
