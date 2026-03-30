from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from real_estate_scanner.config import settings
from real_estate_scanner.db.crud import delete_user_for_admin, list_users_for_admin, set_user_bot_access
from real_estate_scanner.db.init_db import init_db
from real_estate_scanner.db.session import AsyncSessionLocal

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _is_admin_authenticated(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def _require_admin_redirect(request: Request) -> RedirectResponse | None:
    if _is_admin_authenticated(request):
        return None
    return RedirectResponse(url="/login", status_code=303)


def _build_redirect_url(*, search: str | None = None, allowed: str | None = None, message: str | None = None) -> str:
    params: dict[str, str] = {}
    if search:
        params["search"] = search
    if allowed:
        params["allowed"] = allowed
    if message:
        params["message"] = message
    if not params:
        return "/admin/users"
    return f"/admin/users?{urlencode(params)}"


def _parse_allowed_filter(raw_value: str | None) -> bool | None:
    if raw_value == "allowed":
        return True
    if raw_value == "denied":
        return False
    return None


def _format_dt(value) -> str:
    if value is None:
        return "—"
    localized = value
    if getattr(localized, "tzinfo", None) is not None:
        localized = localized.astimezone()
    return localized.strftime("%d.%m.%Y %H:%M")


templates.env.filters["datetime"] = _format_dt


def create_app() -> FastAPI:
    app = FastAPI(title="Real Estate Scanner Admin")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.ADMIN_SESSION_SECRET or "change_me",
        session_cookie="admin_session",
        same_site="lax",
        https_only=False,
    )

    @app.on_event("startup")
    async def on_startup() -> None:
        await init_db()

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        if _is_admin_authenticated(request):
            return RedirectResponse(url="/admin/users", status_code=303)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str | None = None):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": error,
                "admin_panel_enabled": settings.ADMIN_PANEL_ENABLED,
            },
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if not settings.ADMIN_PANEL_ENABLED:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Админ-панель отключена в настройках.",
                    "admin_panel_enabled": False,
                },
                status_code=403,
            )

        if username != settings.ADMIN_USERNAME or password != settings.ADMIN_PASSWORD:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Неверный логин или пароль.",
                    "admin_panel_enabled": True,
                },
                status_code=401,
            )

        request.session["is_admin"] = True
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(
        request: Request,
        search: str | None = None,
        allowed: str | None = None,
        message: str | None = None,
    ):
        redirect = _require_admin_redirect(request)
        if redirect is not None:
            return redirect

        allowed_filter = _parse_allowed_filter(allowed)
        async with AsyncSessionLocal() as session:
            users = await list_users_for_admin(
                session,
                search=search,
                allowed=allowed_filter,
            )

        return templates.TemplateResponse(
            request,
            "users.html",
            {
                "users": users,
                "search": search or "",
                "allowed_filter": allowed or "all",
                "message": message,
            },
        )

    @app.post("/admin/users/{telegram_user_id}/allow")
    async def allow_user(
        request: Request,
        telegram_user_id: int,
        search: str | None = Form(None),
        allowed: str | None = Form(None),
    ):
        redirect = _require_admin_redirect(request)
        if redirect is not None:
            return redirect

        async with AsyncSessionLocal() as session:
            changed = await set_user_bot_access(session, telegram_user_id, True)

        message = (
            f"Доступ выдан пользователю {telegram_user_id}."
            if changed
            else f"Пользователь {telegram_user_id} не найден."
        )
        return RedirectResponse(
            url=_build_redirect_url(search=search, allowed=allowed, message=message),
            status_code=303,
        )

    @app.post("/admin/users/{telegram_user_id}/deny")
    async def deny_user(
        request: Request,
        telegram_user_id: int,
        search: str | None = Form(None),
        allowed: str | None = Form(None),
    ):
        redirect = _require_admin_redirect(request)
        if redirect is not None:
            return redirect

        async with AsyncSessionLocal() as session:
            changed = await set_user_bot_access(session, telegram_user_id, False)

        message = (
            f"Доступ запрещён пользователю {telegram_user_id}."
            if changed
            else f"Пользователь {telegram_user_id} не найден."
        )
        return RedirectResponse(
            url=_build_redirect_url(search=search, allowed=allowed, message=message),
            status_code=303,
        )

    @app.post("/admin/users/{telegram_user_id}/delete")
    async def delete_user(
        request: Request,
        telegram_user_id: int,
        search: str | None = Form(None),
        allowed: str | None = Form(None),
    ):
        redirect = _require_admin_redirect(request)
        if redirect is not None:
            return redirect

        async with AsyncSessionLocal() as session:
            changed = await delete_user_for_admin(session, telegram_user_id)

        message = (
            f"Пользователь {telegram_user_id} удалён."
            if changed
            else f"Пользователь {telegram_user_id} не найден."
        )
        return RedirectResponse(
            url=_build_redirect_url(search=search, allowed=allowed, message=message),
            status_code=303,
        )

    return app


app = create_app()


def _resolve_web_host() -> str:
    explicit_host = os.getenv("HOST")
    if explicit_host:
        return explicit_host
    if os.getenv("RAILWAY_ENVIRONMENT"):
        return "0.0.0.0"
    return settings.ADMIN_WEB_HOST


def _resolve_web_port() -> int:
    explicit_port = os.getenv("PORT")
    if explicit_port:
        try:
            return int(explicit_port)
        except ValueError:
            pass
    return settings.ADMIN_WEB_PORT


def main() -> None:
    import uvicorn

    if not settings.ADMIN_PANEL_ENABLED:
        raise RuntimeError("ADMIN_PANEL_ENABLED is false in .env")
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD or not settings.ADMIN_SESSION_SECRET:
        raise RuntimeError("ADMIN_USERNAME, ADMIN_PASSWORD, and ADMIN_SESSION_SECRET must be set in .env")

    uvicorn.run(
        "real_estate_scanner.web.main:app",
        host=_resolve_web_host(),
        port=_resolve_web_port(),
        reload=False,
    )


if __name__ == "__main__":
    main()
