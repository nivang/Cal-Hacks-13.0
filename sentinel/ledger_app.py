"""The owner's ledger page: the books, the seller list, each agent's limits, and the payments CSV.

This is the only place sellers get approved or blocked and limits get changed. It runs as its own process,
separate from the MCP server, and every route sits behind SENTINEL_OWNER_PASSWORD, which the agent never holds.
So the agent asks (pay) and the owner decides what it may buy (here), and the two never share a door.
Reads and writes go through owner.py and ledger.py; nothing here touches the wallet.

Run:  uvicorn sentinel.ledger_app:create_app --factory --port 5000 then open http://127.0.0.1:5000
"""
import hashlib
import hmac
import os
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sentinel import ENV_FILE, db, guard, ledger, owner, results

COOKIE = "sentinel_owner"
CSP = "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"
PASSWORD_VAR = "SENTINEL_OWNER_PASSWORD"
MIN_PASSWORD = 12
EXPLORER = results.EXPLORER
Connect = Callable[[], AbstractContextManager[db.Connection]]

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")  # autoescape is on for .html
templates.env.filters["usd"] = lambda atomic: "" if atomic is None else f"${results.usdc_text(atomic)}"


def session_token(password: str) -> str:
    """The cookie value that proves the owner logged in. Derived from the password, so changing it logs everyone out."""
    return hmac.new(password.encode(), b"sentinel-owner-session", hashlib.sha256).hexdigest()


def create_app(connect: Connect = db.connection, password: str | None = None) -> FastAPI:
    """Build the ledger app. password defaults to SENTINEL_OWNER_PASSWORD; startup fails without a strong one."""
    if password is None:
        load_dotenv(ENV_FILE)
        password = os.environ.get(PASSWORD_VAR, "")
    if len(password) < MIN_PASSWORD:
        raise RuntimeError(f"Set {PASSWORD_VAR} in .env to at least {MIN_PASSWORD} characters before starting.")
    expected = session_token(password)
    app = FastAPI(title="Sentinel ledger", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_framing(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"  # the forms change who may be paid, so no clickjacking
        response.headers["Content-Security-Policy"] = CSP
        return response

    def require_owner(request: Request) -> None:
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), expected):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    Owner = Depends(require_owner)

    def page(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        with connect() as conn:
            agents = owner.list_agents(conn, guard.POLICY["budget_window_days"])
            balance = ledger.trial_balance(conn)
            context: dict[str, Any] = {
                "error": error,
                "agents": agents,
                "sellers": owner.list_sellers(conn),
                "attempts": owner.list_attempts(conn),
                "journal": owner.list_journal(conn),
                "balance": balance,
                "total_debit": sum(r["debit"] for r in balance),
                "total_credit": sum(r["credit"] for r in balance),
                "window_days": guard.POLICY["budget_window_days"],
                "explorer": EXPLORER,
            }
        return templates.TemplateResponse(request, "ledger.html", context, status_code=status_code)

    def done() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)  # back to the page, so a reload never resubmits the form

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_model=None)
    def login(request: Request, password_given: Annotated[str, Form(alias="password")]) -> Response:
        if not secrets.compare_digest(password_given.encode(), password.encode()):
            return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)
        response = done()
        response.set_cookie(COOKIE, expected, httponly=True, samesite="strict")
        return response

    @app.post("/logout", dependencies=[Owner])
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse, dependencies=[Owner])
    def dashboard(request: Request) -> HTMLResponse:
        return page(request)

    @app.post("/sellers", response_model=None, dependencies=[Owner])
    def save_seller(
        request: Request,
        name: Annotated[str, Form()],
        pay_to: Annotated[str, Form()],
        status: Annotated[str, Form()] = "approved",
    ) -> Response:
        try:
            with connect() as conn, db.transaction(conn):
                owner.upsert_seller(conn, name, pay_to, status)
        except ValueError as e:
            return page(request, str(e), 400)
        return done()

    @app.post("/agents/{agent}/limits", response_model=None, dependencies=[Owner])
    def save_limits(
        request: Request, agent: str, cap: Annotated[str, Form()], budget: Annotated[str, Form()],
    ) -> Response:
        try:
            with connect() as conn, db.transaction(conn):
                owner.set_agent_limits(conn, agent, owner.parse_usd(cap), owner.parse_usd(budget))
        except (ValueError, LookupError) as e:
            return page(request, str(e), 400)
        return done()

    @app.get("/payments.csv", dependencies=[Owner])
    def payments_csv() -> Response:
        with connect() as conn:
            body = owner.payments_csv(owner.list_attempts(conn, limit=100_000))
        filename = f"sentinel-payments-{date.today().isoformat()}.csv"
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    return app
