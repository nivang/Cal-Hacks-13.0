"""The one ASGI app Sentinel serves: the sign in site, the OAuth server and the MCP endpoint on one origin.

create_app builds it: FastAPI site routes first (landing, magic link sign in, account, consent, health), then
/static, then FastMCP's app mounted at "/" last, which serves /mcp, /authorize, /token, /register, /revoke and the
discovery documents through web/oauth.py's SentinelOAuthProvider. mcp_server.main() runs it with uvicorn; tests run
it in process (tests/conftest.py, fixture app).

Every site POST must come from our own pages (auth.same_origin), and forms bound to a session also carry a CSRF
token (auth.csrf_token: "account" for the account page, "consent:<nonce>" for consent). Two pure ASGI middlewares
wrap everything: SecurityHeaders adds the CSP and friends to site pages and /authorize (a route may set its own
CSP, as the consent page does to let the browser follow the redirect after Allow), and McpCors lets browser based
MCP clients (MCP Inspector in direct mode) call /mcp. StripQuery keeps query strings (sign in links, pending
requests) out of uvicorn's access log.

Database work runs in worker threads on a connection of its own (connect), never held across a network call (CDP,
mail). Logs name user ids and client ids only: never links, tokens, cookies, CSRF values, pending requests or emails.
"""
import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from x402.mechanisms.evm import ClientEvmSigner

from sentinel import chain, db, guard, judge, mandates, mcp_server, owner, payments, results, wallets
from sentinel.web import Settings, auth, books, oauth, pages, ratelimit, users
from sentinel.web.mailer import Mailer, MailError, make_mailer, sign_in_message

Connect = Callable[[], AbstractContextManager[db.Connection]]
OpenSigner = Callable[..., AbstractAsyncContextManager[ClientEvmSigner]]  # open_user_wallet(user_id, *, create)
T = TypeVar("T")

STATIC_DIR = Path(__file__).parent / "static"
FAVICON = STATIC_DIR / "favicon.svg"
BINDING_TTL_S = oauth.PENDING_TTL_S  # the consent binding cookie lives as long as the pending request
TAILWIND = "https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.3/dist/index.global.js"
BASESCAN_ADDRESS = "https://sepolia.basescan.org/address/"
FAUCET = "https://faucet.circle.com"

CSP_BEFORE_FORM = (
    f"default-src 'none'; script-src {TAILWIND}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'none'; object-src 'none'; "
)
CSP_AFTER_FORM = "; frame-ancestors 'none'"
SITE_CSP = CSP_BEFORE_FORM + "form-action 'self'" + CSP_AFTER_FORM

SITE_PATHS = frozenset({
    "/", "/signin", "/auth/verify", "/account", "/account/wallet", "/account/signout-everywhere", "/signout",
    "/account/payments", "/account/payments.csv", "/account/rules", "/account/rules/limits",
    "/account/rules/sellers", "/account/books", "/account/tasks", "/account/tasks/confirm", "/account/tasks/revoke",
    "/account/tasks/allow", "/account/tasks/capture", "/account/tasks/void",
    "/oauth/consent", "/healthz", "/favicon.ico", "/authorize",
})
DONE_TEXT = {  # the ?done= values the Rules page shows a banner for; anything else is ignored
    "limits": "Saved. Your new limits apply to the next payment.",
    "seller": "Saved. The seller list is updated.",
}
TASK_DONE_TEXT = {  # the ?done= values the Tasks page shows a banner for
    "confirm": "Confirmed. Your agent can now spend this task's full budget.",
    "revoke": "Revoked. Nothing more can be bought for that task.",
    "allow": "Allowed once. Tell your agent to try that payment again.",
    "capture": "Paid. The seller has the money and your agent can collect the data.",
    "void": "Refunded. The money is back in your wallet and the seller was not paid.",
}
SWEEP_EVERY_S = 60  # how often the server finishes what is still held in escrow
WARN_PERCENT = 80  # the weekly budget bar turns amber from here
MCP_PATHS = frozenset({"/mcp", "/mcp/"})
MCP_ALLOW_HEADERS = "authorization, content-type, mcp-protocol-version, mcp-session-id, last-event-id"
MCP_EXPOSE_HEADERS = "WWW-Authenticate, mcp-session-id"
MCP_ALLOW_METHODS = "GET, POST, DELETE, OPTIONS"

log = logging.getLogger(__name__)


# Middleware and logging


def csp(form_action: str = "'self'") -> str:
    """The site's Content-Security-Policy with this form-action list."""
    return CSP_BEFORE_FORM + f"form-action {form_action}" + CSP_AFTER_FORM


def is_site_path(path: str) -> bool:
    """True for the paths that get the site's security headers: site pages, /static and /authorize."""
    return path in SITE_PATHS or path.startswith("/static/")


class SecurityHeaders:
    """Pure ASGI middleware adding the site's security headers to site paths and /authorize.

    Headers are only added when the response has not set them already (setdefault), so a route can send its own CSP.
    HSTS is added when the site is served over https.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        """Wrap app; settings decides HSTS."""
        self.app = app
        self.headers = {
            "Content-Security-Policy": SITE_CSP,
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        }
        if settings.https:
            self.headers["Strict-Transport-Security"] = "max-age=31536000"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the headers to the response start of a site request; pass everything else through untouched."""
        if scope["type"] != "http" or not is_site_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LocalhostAlias:
    """Pure ASGI middleware for local runs: a page opened at 127.0.0.1 or [::1] is sent on to the configured address.

    Cookies, the same origin check and the OAuth issuer all use PUBLIC_BASE_URL (http://localhost:8000), and a browser
    keeps a separate cookie jar per host name. A visit to http://127.0.0.1:8000 would fail every form with a 403, so
    GET and HEAD requests for site pages get a 307 to the same path and query on the configured host. Only the loopback
    names on the configured port are rewritten, and only when the site runs on localhost (dev mode); /healthz, /mcp and
    every other method are left alone.
    """

    ALIASES = frozenset({"localhost", "127.0.0.1", "[::1]"})

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        """Wrap app; settings gives the address to send people to."""
        self.app = app
        self.enabled = settings.dev_mode
        self.base = settings.base_url
        self.netloc = urlsplit(settings.base_url).netloc.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Redirect an aliased GET or HEAD of a site page; pass everything else through."""
        if (
            self.enabled and scope["type"] == "http" and scope["method"] in ("GET", "HEAD")
            and is_site_path(scope["path"]) and scope["path"] != "/healthz"
        ):
            host = Headers(scope=scope).get("host", "").lower()
            if host and host != self.netloc and self.is_alias_of_base(host):
                query = scope.get("query_string", b"").decode("latin-1")
                target = self.base + scope["path"] + ("?" + query if query else "")
                await RedirectResponse(target, status_code=307)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def is_alias_of_base(self, host: str) -> bool:
        """True when host is another loopback name on the same port as the configured address."""
        name, _, port = host.rpartition(":") if not host.endswith("]") and ":" in host else (host, "", "")
        base_name, _, base_port = self.netloc.rpartition(":") if ":" in self.netloc else (self.netloc, "", "")
        return name in self.ALIASES and base_name in self.ALIASES and port == base_port


class McpCors:
    """Pure ASGI middleware that lets browser based MCP clients call /mcp (MCP Inspector in direct connect mode).

    Answers the OPTIONS preflight itself and adds Access-Control-Allow-Origin "*" plus the exposed headers
    (WWW-Authenticate, mcp-session-id) to every /mcp response, the 401 challenge included. No cookies are involved:
    /mcp authenticates by bearer token only, so a wildcard origin grants nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer preflights for /mcp, decorate /mcp responses, pass everything else through."""
        if scope["type"] != "http" or scope["path"] not in MCP_PATHS:
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            response = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": MCP_ALLOW_METHODS,
                    "Access-Control-Allow-Headers": MCP_ALLOW_HEADERS,
                    "Access-Control-Expose-Headers": MCP_EXPOSE_HEADERS,
                    "Access-Control-Max-Age": "600",
                },
            )
            await response(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Access-Control-Allow-Origin"] = "*"
                headers["Access-Control-Expose-Headers"] = MCP_EXPOSE_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_cors)


class StripQuery(logging.Filter):
    """Removes the query string from uvicorn access log records, so sign in links and pending requests never land in
    a log. uvicorn passes (client, method, path with query, http version, status) as record.args."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite args[2] without its query string; always keep the record."""
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def attach_strip_query(logger_name: str = "uvicorn.access") -> StripQuery:
    """Attach StripQuery to the logger once (calling again is a no op) and return the attached filter.

    It sits on the logger, not a handler: uvicorn's dictConfig replaces the logger's handlers but keeps its filters.
    """
    logger = logging.getLogger(logger_name)
    for existing in logger.filters:
        if isinstance(existing, StripQuery):
            return existing
    added = StripQuery()
    logger.addFilter(added)
    return added


# Small helpers


def usdc(atomic: int | None) -> str | None:
    """Atomic units as "$0.50", or None."""
    return None if atomic is None else f"${results.usdc_text(int(atomic))}"


def _equal(a: str | None, b: str) -> bool:
    """Constant time comparison that treats a missing value as different."""
    return a is not None and hmac.compare_digest(a.encode(), b.encode())


def _first(form: Mapping[str, Any], name: str) -> str | None:
    """A form field as a string, or None when it is missing or a file."""
    value = form.get(name)
    return value if isinstance(value, str) else None


def _sign_in_url(next_path: str) -> str:
    """The sign in page that comes back to next_path afterwards."""
    return "/signin?" + urlencode({"next": next_path})


def _redirect(url: str) -> RedirectResponse:
    """A 303 See Other, so a POST becomes a GET."""
    return RedirectResponse(url, status_code=303)


# The app


def create_app(
    settings: Settings,
    connect: Connect = db.connection,
    mailer: Mailer | None = None,
    pay_fn: Callable[..., Any] = payments.pay_for,
    open_signer: OpenSigner = wallets.open_user_wallet,
    judge_: judge.Judge | None = None,
    open_operator: mcp_server.OpenOperator | None = None,
) -> FastAPI:
    """Build the site, the OAuth server and /mcp as one app.

    connect: how requests borrow a database connection. mailer: where sign in links go (make_mailer(settings) when
    None). pay_fn, open_signer and judge_ go to mcp_server.create_server (tests replace them). open_operator opens
    Sentinel's escrow operator (escrow.open_operator, which main passes); with it, payments use escrow when a seller
    offers it, the owner can pay or refund a held payment, and a background sweep finishes holds every SWEEP_EVERY_S.
    The pool is not touched here; mcp_server.main() closes it when uvicorn stops.
    """
    mail = mailer if mailer is not None else make_mailer(settings)
    provider = oauth.SentinelOAuthProvider(settings, connect)
    server = mcp_server.create_server(
        auth=provider, pay_fn=pay_fn, open_signer=open_signer, connect=connect, judge_=judge_,
        open_operator=open_operator,
    )
    mcp_app = server.http_app(path=oauth.MCP_PATH, json_response=True, stateless_http=True)

    async def sweep_forever() -> None:
        """Every SWEEP_EVERY_S, finish what is still in escrow (payments.sweep). A failed round waits for the next."""
        assert open_operator is not None
        while True:
            await asyncio.sleep(SWEEP_EVERY_S)
            try:
                async with open_operator() as operator:
                    with connect() as conn:
                        settled = await payments.sweep(conn, operator)
                if settled:
                    log.info("escrow sweep settled %s held payments", settled)
            except Exception:
                log.warning("escrow sweep failed; trying again next round", exc_info=True)

    @asynccontextmanager
    async def sweeper(_app: Any) -> AsyncIterator[None]:
        task = asyncio.create_task(sweep_forever()) if open_operator is not None else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=combine_lifespans(mcp_app.lifespan, sweeper))
    attach_strip_query()

    session_cookie = auth.session_cookie_name(settings)

    async def run_db(fn: Callable[..., T], *args: Any) -> T:
        """Run fn(conn, *args) on a connection of its own in a worker thread."""

        def work() -> T:
            with connect() as conn:
                return fn(conn, *args)

        return await asyncio.to_thread(work)

    def clear_cookie(response: Response, name: str) -> None:
        """Expire one of our cookies with the same flags it was set with."""
        response.delete_cookie(name, path="/", secure=settings.https, httponly=True, samesite="lax")

    def set_session(response: Response, user: Mapping[str, Any]) -> None:
        """Sign the user in on this response."""
        value = auth.make_session(settings, str(user["id"]), int(user["session_epoch"]))
        response.set_cookie(session_cookie, value, **auth.cookie_kwargs(settings, auth.SESSION_TTL_S))

    async def current(request: Request) -> tuple[auth.Session | None, dict[str, Any] | None, bool]:
        """(session, user, stale) for this request. stale: a cookie was sent but is not a live session (tampered,
        expired, or ended by "sign out everywhere"), so the response should clear it."""
        raw = request.cookies.get(session_cookie)
        if raw is None:
            return None, None, False
        session = auth.read_session(settings, raw)
        if session is None:
            return None, None, True
        user = await run_db(users.session_user, session.user_id, session.epoch)
        if user is None:
            return None, None, True
        return session, user, False

    def page(
        request: Request,
        name: str,
        context: Mapping[str, Any] | None = None,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        stale: bool = False,
    ) -> Response:
        """Render a page, clearing a stale session cookie on the way."""
        response = pages.render(request, name, {"base_url": settings.base_url, **(context or {})}, status, headers)
        if stale:
            clear_cookie(response, session_cookie)
        return response

    def error(request: Request, title: str, message: str, status: int, stale: bool = False) -> Response:
        """Render the error page, clearing a stale session cookie on the way."""
        response = pages.render_error(request, title, message, status)
        if stale:
            clear_cookie(response, session_cookie)
        return response

    def forbidden(request: Request) -> Response:
        """The answer to a POST that did not come from our own pages."""
        return error(
            request, "That request was refused",
            f"It did not come from a Sentinel page. Open the site at {settings.base_url}, reload and try again.", 403,
        )

    # Landing, health, favicon

    @app.get("/")
    async def landing(request: Request) -> Response:
        """What Sentinel is and how to connect it."""
        _, user, stale = await current(request)
        return page(request, "landing.html", {"signed_in": user is not None, "mcp_url": settings.resource_url},
                    stale=stale)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness only: no database."""
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.ico")
    async def favicon() -> FileResponse:
        """The shield icon."""
        return FileResponse(FAVICON, media_type="image/svg+xml")

    # Sign in

    @app.get("/signin")
    async def sign_in_form(request: Request) -> Response:
        """The email form. Already signed in: straight on to where the user was going."""
        next_path = auth.safe_next(request.query_params.get("next"))
        _, user, stale = await current(request)
        if user is not None:
            return _redirect(next_path or "/account")
        return page(request, "sign_in.html", {"next": next_path, "email": ""}, stale=stale)

    @app.post("/signin")
    async def sign_in(request: Request) -> Response:
        """Send a magic link: normalize, check the limits and store the link in one transaction, then mail it."""
        if not auth.same_origin(settings, request.headers):
            return forbidden(request)
        form = await request.form()
        typed = (_first(form, "email") or "").strip()
        next_path = auth.safe_next(_first(form, "next"))
        try:
            email, email_key = auth.normalize_email(typed)
        except ValueError as exc:
            return page(request, "sign_in.html", {"next": next_path, "email": typed, "error": str(exc)}, 400)
        limit_key = auth.limit_key(email_key)
        ip_hash = ratelimit.ip_hash(settings, request.client.host if request.client else "")

        def issue(conn: db.Connection) -> tuple[str, int, str]:
            with db.transaction(conn):
                ratelimit.check_link_limits(conn, limit_key=limit_key, ip_hash=ip_hash)
                token, link_id = auth.issue_link(
                    conn, settings, email=email, email_key=email_key, limit_key=limit_key, ip_hash=ip_hash,
                    next_path=next_path,
                )
            return token, link_id, users.stored_email(conn, email_key) or email

        try:
            token, link_id, to = await run_db(issue)
        except ratelimit.RateLimited as exc:
            response = error(
                request, "Too many sign in links",
                f"Wait about {max(1, -(-exc.retry_after // 60))} minutes, then ask for a new link.", 429,
            )
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
        link = f"{settings.base_url}/auth/verify?" + urlencode({"token": token})
        try:
            await mail.send(sign_in_message(settings, to=to, link=link, link_id=link_id))
        except MailError as exc:
            log.error("sign in mail for link %s failed: %s", link_id, exc)
            return error(request, "The email could not be sent", "Try again in a minute.", 502)
        log.info("sign in link %s sent", link_id)
        return page(request, "check_email.html", {"email": typed})

    @app.get("/auth/verify")
    async def confirm(request: Request) -> Response:
        """Show a "Sign in as ..." button; the link is spent only by the POST, so mail scanners cannot use it."""
        token = request.query_params.get("token") or ""
        link = await run_db(auth.peek_link, settings, token)
        if link is None:
            return error(request, "This sign in link does not work",
                         "It has expired or was already used. Ask for a new one on the sign in page.", 400)
        return page(request, "confirm.html", {"email": link["email"], "token": token})

    @app.post("/auth/verify")
    async def verify(request: Request) -> Response:
        """Spend the link, find or create the user, make sure they have a wallet, and sign them in."""
        if not auth.same_origin(settings, request.headers):
            return forbidden(request)
        form = await request.form()
        token = _first(form, "token") or ""

        def spend(conn: db.Connection) -> tuple[dict[str, Any], dict[str, Any]] | None:
            with db.transaction(conn):
                link = auth.consume_link(conn, settings, token)
                if link is None:
                    return None
                return link, users.find_or_create_user(conn, link["email"], link["email_key"])

        spent = await run_db(spend)
        if spent is None:
            return error(request, "This sign in link does not work",
                         "It has expired or was already used. Ask for a new one on the sign in page.", 400)
        link, user = spent
        try:
            await users.ensure_wallet(connect, user["id"], open_signer)
        except Exception:
            log.warning("could not set up the wallet of user %s; they can retry", user["id"], exc_info=True)
        log.info("user %s signed in", user["id"])
        response = _redirect(auth.safe_next(link["next_path"]) or "/account")
        set_session(response, user)
        return response

    # Account

    def percent_spent(spent: int, budget: int) -> int:
        """How much of the weekly budget is used, 0 to 100 (100 once it is reached or passed)."""
        return min(100, spent * 100 // budget) if budget > 0 else 0

    def sign_in_redirect(path: str, stale: bool) -> Response:
        """Send a visitor without a live session to sign in, coming back to path afterwards."""
        response = _redirect(_sign_in_url(path))
        if stale:
            clear_cookie(response, session_cookie)
        return response

    def account_context(session: auth.Session, view: Mapping[str, Any], tab: str) -> dict[str, Any]:
        """What every account page needs: the user's view, the CSRF token, the tab and the formatted limits."""
        address = view["wallet_address"]
        has_agent = view["agent"] is not None
        percent = percent_spent(view["week_spend"], view["weekly_budget"]) if has_agent else 0
        return {
            **view,
            "tab": tab,
            "csrf": auth.csrf_token(settings, session.value, "account"),
            "basescan_url": BASESCAN_ADDRESS + address if address else None,
            "faucet_url": FAUCET,
            "cap": usdc(view["per_payment_cap"]),
            "budget": usdc(view["weekly_budget"]),
            "spent": usdc(view["week_spend"]),
            "percent": percent,
            "bar": "bg-red-600" if percent >= 100 else "bg-amber-500" if percent >= WARN_PERCENT else "bg-stone-900",
            "error": None,
        }

    async def account_get(request: Request, path: str) -> tuple[Response | None, auth.Session | None, dict[str, Any]]:
        """The checks every account page shares: a live session, else the sign in redirect. Returns the user's view."""
        session, user, stale = await current(request)
        if session is None or user is None:
            return sign_in_redirect(path, stale), None, {}
        view = await run_db(users.account_view, user["id"])
        return None, session, view

    @app.get("/account")
    async def account(request: Request) -> Response:
        """Overview: the week's spending, the wallet, what is left to set up and the latest payments."""
        refusal, session, view = await account_get(request, "/account")
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        user_id = str(session.user_id)
        aid = view["agent_id"]

        def data(conn: db.Connection) -> dict[str, Any]:
            if aid is None:
                return {"recent": [], "has_seller": False, "connected": books.has_live_grant(conn, user_id),
                        "waiting": {"tasks": 0, "held": 0}}
            return {
                "recent": books.recent_payments(conn, aid),
                "has_seller": any(sl["status"] == "approved" for sl in books.list_sellers(conn, aid)),
                "connected": books.has_live_grant(conn, user_id),
                "waiting": mandates.count_pending(conn, aid),
            }

        found = await run_db(data)
        steps = [
            (aid is not None, "Create your wallet", None),
            (found["has_seller"], "Approve a seller your agent may pay", "/account/rules"),
            (found["connected"], "Connect your AI app", "#connect-h"),
        ]
        context = {
            **account_context(session, view, "overview"), **found, "steps": steps,
            "all_set": all(done for done, _, _ in steps),
            "mcp_url": settings.resource_url,
            "claude_command": f"claude mcp add --transport http sentinel {settings.resource_url}",
        }
        return page(request, "account.html", context)

    @app.get("/account/payments")
    async def account_payments(request: Request) -> Response:
        """Payments: this user's own payment log."""
        refusal, session, view = await account_get(request, "/account/payments")
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        aid = view["agent_id"]
        groups = await run_db(books.payments_by_task, aid) if aid is not None else []
        context = {**account_context(session, view, "payments"), "groups": groups,
                   "payments": [row for group in groups for row in group["rows"]]}
        return page(request, "account_payments.html", {**context, "csv_limit": f"{books.CSV_LIMIT:,}"})

    @app.get("/account/payments.csv")
    async def account_payments_csv(request: Request) -> Response:
        """This user's own payments as a CSV download (spreadsheet formulas defused by owner.payments_csv)."""
        refusal, session, view = await account_get(request, "/account/payments")  # next must be an allowed page
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        if view["agent_id"] is None:
            return _redirect("/account/payments")
        rows = await run_db(books.list_payments, view["agent_id"], books.CSV_LIMIT)
        filename = f"sentinel-payments-{date.today().isoformat()}.csv"
        return Response(
            owner.payments_csv(rows), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def rules_page(
        request: Request, session: auth.Session, view: Mapping[str, Any], *, status: int = 200,
        error: str | None = None, typed: Mapping[str, str] | None = None, confirm: Mapping[str, Any] | None = None,
    ) -> Response:
        """Render Rules: limits, the sellers that apply to this agent and, in dev mode, the demo sellers."""
        aid = view["agent_id"]

        def data(conn: db.Connection) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
            return books.list_sellers(conn, aid), books.demo_sellers(conn, aid, settings.dev_mode)

        sellers, demo = await run_db(data) if aid is not None else ([], [])

        async def wallet(seller: dict[str, Any]) -> None:
            with connect() as conn:  # cached for an hour, so the page is quick after the first look
                profile = await chain.cached_profile(conn, seller["pay_to"])
            seller["wallet"] = profile.summary() if profile else "could not check"
            seller["wallet_warn"] = bool(profile and (profile.flagged or profile.unused or profile.is_new()))

        await asyncio.gather(*(wallet(s) for s in sellers))
        typed = typed or {}
        done = request.query_params.get("done")
        context = {
            **account_context(session, view, "rules"),
            "sellers": sellers, "demo": demo, "error": error,
            "done": done if done in DONE_TEXT and error is None else None,
            "done_text": DONE_TEXT.get(done or "", ""),
            "typed_cap": typed.get("cap", results.usdc_text(view["per_payment_cap"]) if aid is not None else ""),
            "typed_budget": typed.get("budget", results.usdc_text(view["weekly_budget"]) if aid is not None else ""),
            "typed_name": typed.get("name", ""),
            "typed_pay_to": typed.get("pay_to", ""),
            "confirm": confirm,
        }
        return page(request, "account_rules.html", context, status)

    @app.get("/account/rules")
    async def account_rules(request: Request) -> Response:
        """Rules: the user's limits and seller list."""
        refusal, session, view = await account_get(request, "/account/rules")
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        return await rules_page(request, session, view)

    async def tasks_page(
        request: Request, session: auth.Session, view: Mapping[str, Any], *, status: int = 200,
        error: str | None = None,
    ) -> Response:
        """Render Tasks: what the agent is working on (provisional first) and the payments held for the owner."""
        aid = view["agent_id"]

        def data(conn: db.Connection) -> dict[str, Any]:
            return {"tasks": mandates.list_for_agent(conn, aid), "held": mandates.list_held(conn, aid),
                    "escrow_held": mandates.list_escrow_held(conn, aid)}

        found = await run_db(data) if aid is not None else {"tasks": [], "held": [], "escrow_held": []}
        done = request.query_params.get("done")
        context = {
            **account_context(session, view, "tasks"), **found, "error": error,
            "done": done if done in TASK_DONE_TEXT and error is None else None,
            "done_text": TASK_DONE_TEXT.get(done or "", ""),
            "provisional_cap": usdc(guard.POLICY["intent_provisional_budget"]),
        }
        return page(request, "account_tasks.html", context, status)

    @app.get("/account/tasks")
    async def account_tasks(request: Request) -> Response:
        """Tasks: confirm or revoke what the agent is working on, and allow held payments once."""
        refusal, session, view = await account_get(request, "/account/tasks")
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        return await tasks_page(request, session, view)

    @app.get("/account/books")
    async def account_books(request: Request) -> Response:
        """Books: this user's journal and totals, from their own settled payments only."""
        refusal, session, view = await account_get(request, "/account/books")
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        aid = view["agent_id"]

        def data(conn: db.Connection) -> dict[str, Any]:
            return {
                "journal": books.journal(conn, aid), "totals": books.totals(conn, aid),
                "summary": books.summary(conn, aid),
            }

        found = await run_db(data) if aid is not None else {}
        return page(request, "account_books.html", {**account_context(session, view, "books"), **found})

    async def account_post(
        request: Request, path: str = "/account"
    ) -> tuple[Response | None, dict[str, Any] | None, Mapping[str, Any]]:
        """The checks every account form shares: same origin, a live session and its CSRF token.

        Returns (refusal, user, form): refusal is None when every check passed. Without a session the visitor goes
        to sign in and comes back to path.
        """
        if not auth.same_origin(settings, request.headers):
            return forbidden(request), None, {}
        form = await request.form()
        session, user, stale = await current(request)
        if session is None or user is None:
            return sign_in_redirect(path, stale), None, form
        if not auth.check_csrf(settings, session.value, "account", _first(form, "csrf")):
            return forbidden(request), None, form
        return None, user, form

    @app.post("/account/rules/limits")
    async def save_limits(request: Request) -> Response:
        """Set the session user's own per payment cap and weekly budget; the agent is never a form field."""
        refusal, user, form = await account_post(request, "/account/rules")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        view = await run_db(users.account_view, user["id"])
        session, _, _ = await current(request)
        if session is None:
            return forbidden(request)
        typed = {"cap": _first(form, "cap") or "", "budget": _first(form, "budget") or ""}
        if view["agent"] is None:
            return await rules_page(request, session, view, status=400,
                                    error="Create your wallet first, on the Overview page.", typed=typed)

        def save(conn: db.Connection) -> None:
            with db.transaction(conn):
                owner.set_agent_limits(conn, view["agent"], owner.parse_usd(typed["cap"]),
                                       owner.parse_usd(typed["budget"]))

        try:
            await run_db(save)
        except ValueError as exc:
            return await rules_page(request, session, view, status=400, error=str(exc), typed=typed)
        log.info("user %s changed their limits", user["id"])
        return _redirect("/account/rules?done=limits")

    @app.post("/account/rules/sellers")
    async def save_seller(request: Request) -> Response:
        """Approve or block a seller on this user's own list; the operator's rows are never touched."""
        refusal, user, form = await account_post(request, "/account/rules")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        view = await run_db(users.account_view, user["id"])
        session, _, _ = await current(request)
        if session is None:
            return forbidden(request)
        typed = {"name": _first(form, "name") or "", "pay_to": _first(form, "pay_to") or ""}
        if view["agent_id"] is None:
            return await rules_page(request, session, view, status=400,
                                    error="Create your wallet first, on the Overview page.", typed=typed)
        status = _first(form, "status") or ""
        address = typed["pay_to"].strip()
        if status == "approved" and _first(form, "confirm") != "1" and owner.ADDRESS.fullmatch(address):
            warnings = await seller_warnings(view["agent_id"], address)
            if warnings:
                return await rules_page(request, session, view, typed=typed,
                                        confirm={"name": typed["name"], "pay_to": address, "warnings": warnings})

        def save(conn: db.Connection) -> None:
            with db.transaction(conn):
                books.upsert_seller(
                    conn, view["agent_id"], typed["name"], typed["pay_to"], _first(form, "status") or ""
                )

        try:
            await run_db(save)
        except ValueError as exc:
            return await rules_page(request, session, view, status=400, error=str(exc), typed=typed)
        log.info("user %s changed their seller list", user["id"])
        return _redirect("/account/rules?done=seller")

    async def seller_warnings(agent_id: int, address: str) -> list[str]:
        """Reasons to look twice before approving this address: it imitates a known one, or has never been used.

        Nothing when the agent already approved it. A failed chain lookup is itself a warning.
        """
        def known(conn: db.Connection) -> tuple[list[str], bool]:
            listed = books.list_sellers(conn, agent_id)
            mine = conn.execute("SELECT wallet_address FROM users WHERE agent_id = %s", (agent_id,)).fetchone()
            addresses = [s["pay_to"] for s in listed if s["status"] == "approved"]
            if mine is not None and mine["wallet_address"]:
                addresses.append(mine["wallet_address"])
            return addresses, any(a.lower() == address.lower() for a in addresses)

        addresses, already = await run_db(known)
        if already:
            return []
        warnings = []
        imitated = guard.imitates(address, addresses)
        if imitated:
            warnings.append(f"It looks like {imitated} (same start and end, different middle). Scammers make "
                            "lookalike addresses so a payment goes to the wrong wallet.")
        try:
            profile = await chain.wallet_profile(address)
        except chain.ChainError:
            warnings.append("Sentinel could not check this wallet's history on Base Sepolia right now.")
            return warnings
        if profile.flagged:
            warnings.append("The Base Sepolia explorer flags this wallet as a scam.")
        elif profile.unused:
            warnings.append("It has no activity at all on Base Sepolia, so it may have just been created. Real "
                            "sellers usually have a history.")
        elif profile.is_new():
            warnings.append(f"It is new: {profile.summary()}. Scam sellers often use fresh wallets.")
        return warnings

    async def task_action(request: Request, action: Callable[[db.Connection, Mapping[str, Any], Mapping[str, Any],
                                                              str], None], done: str) -> Response:
        """The checks every Tasks form shares, then action(conn, view, form, user id) in one transaction.

        A ValueError from the action is shown on the Tasks page; success goes back to it with a banner. The agent is
        always the session user's own, never a form field, so nobody can confirm or allow anything for another user.
        """
        refusal, user, form = await account_post(request, "/account/tasks")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        view = await run_db(users.account_view, user["id"])
        session, _, _ = await current(request)
        if session is None:
            return forbidden(request)
        if view["agent_id"] is None:
            return await tasks_page(request, session, view, status=400,
                                    error="Create your wallet first, on the Overview page.")

        def run(conn: db.Connection) -> None:
            with db.transaction(conn):
                action(conn, view, form, str(user["id"]))

        try:
            await run_db(run)
        except ValueError as exc:
            return await tasks_page(request, session, view, status=400, error=str(exc))
        log.info("user %s: task %s", user["id"], done)
        return _redirect(f"/account/tasks?done={done}")

    def form_id(form: Mapping[str, Any], name: str) -> int:
        """A positive integer id from a form field, or ValueError the page can show."""
        raw = (_first(form, name) or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            raise ValueError("That request was not understood. Reload the page and try again.")
        return int(raw)

    @app.post("/account/tasks/confirm")
    async def confirm_task(request: Request) -> Response:
        """Confirm one of the user's provisional tasks with a budget up to their weekly budget."""

        def confirm(conn: db.Connection, view: Mapping[str, Any], form: Mapping[str, Any], _user: str) -> None:
            budget = owner.parse_usd(_first(form, "budget") or "")
            mandates.confirm(conn, form_id(form, "task_id"), view["agent_id"], budget, view["weekly_budget"])

        return await task_action(request, confirm, "confirm")

    @app.post("/account/tasks/revoke")
    async def revoke_task(request: Request) -> Response:
        """Revoke one of the user's tasks: nothing more can be bought for it."""

        def revoke(conn: db.Connection, view: Mapping[str, Any], form: Mapping[str, Any], _user: str) -> None:
            mandates.revoke(conn, form_id(form, "task_id"), view["agent_id"])

        return await task_action(request, revoke, "revoke")

    @app.post("/account/tasks/allow")
    async def allow_held(request: Request) -> Response:
        """Allow one held payment of the user's own agent once; the agent's next identical pay goes through."""

        def allow(conn: db.Connection, view: Mapping[str, Any], form: Mapping[str, Any], user_id: str) -> None:
            mandates.allow_once(conn, form_id(form, "request_id"), view["agent_id"], user_id)

        return await task_action(request, allow, "allow")

    async def escrow_action(request: Request, capture: bool) -> Response:
        """Pay the seller (capture) or refund (void) one payment of the user's own agent held in escrow."""
        refusal, user, form = await account_post(request, "/account/tasks")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        view = await run_db(users.account_view, user["id"])
        session, _, _ = await current(request)
        if session is None:
            return forbidden(request)
        if view["agent_id"] is None or open_operator is None:
            return await tasks_page(request, session, view, status=400,
                                    error="Escrow is not set up on this server, so nothing can be paid or refunded.")
        try:
            attempt_id = form_id(form, "request_id")
            async with open_operator() as operator:
                with connect() as conn:
                    outcome = await payments.settle_held(conn, operator, attempt_id, view["agent_id"], capture)
        except (ValueError, LookupError) as exc:
            return await tasks_page(request, session, view, status=400, error=str(exc))
        except Exception:
            log.warning("escrow %s failed for user %s", "capture" if capture else "void", user["id"], exc_info=True)
            outcome = "processing"
        if outcome == "processing":
            return await tasks_page(request, session, view, status=502,
                                    error="Coinbase did not confirm that in time. Try again in a minute.")
        log.info("user %s: escrow %s request %s", user["id"], outcome, attempt_id)
        return _redirect(f"/account/tasks?done={'capture' if capture else 'void'}")

    @app.post("/account/tasks/capture")
    async def capture_held(request: Request) -> Response:
        """The owner pays the seller for a delivery Sentinel held in escrow."""
        return await escrow_action(request, True)

    @app.post("/account/tasks/void")
    async def void_held(request: Request) -> Response:
        """The owner refunds a payment Sentinel held in escrow; the seller is not paid."""
        return await escrow_action(request, False)

    @app.post("/account/wallet")
    async def retry_wallet(request: Request) -> Response:
        """Try again to create the user's wallet after CDP failed at sign in."""
        refusal, user, form = await account_post(request)
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        try:
            await users.ensure_wallet(connect, user["id"], open_signer)
        except Exception:
            log.warning("wallet retry failed for user %s", user["id"], exc_info=True)
            return error(request, "Your wallet is not ready yet",
                         "Coinbase did not answer in time. Go back and try again in a minute.", 502)
        return _redirect(auth.safe_next(_first(form, "next")) or "/account")

    @app.post("/account/signout-everywhere")
    async def sign_out_everywhere(request: Request) -> Response:
        """End every session and every connected app of this user at once."""
        refusal, user, _ = await account_post(request)
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        await run_db(users.sign_out_everywhere, user["id"])
        response = _redirect("/")
        clear_cookie(response, session_cookie)
        return response

    @app.post("/signout")
    async def sign_out(request: Request) -> Response:
        """End this browser's session. With a live session the account CSRF token is required."""
        if not auth.same_origin(settings, request.headers):
            return forbidden(request)
        form = await request.form()
        session, user, _ = await current(request)
        if session is not None and user is not None:
            if not auth.check_csrf(settings, session.value, "account", _first(form, "csrf")):
                return forbidden(request)
            log.info("user %s signed out", user["id"])
        response = _redirect("/")
        clear_cookie(response, session_cookie)
        return response

    # Consent

    def nonce_used(conn: db.Connection, nonce: str) -> bool:
        """True when this consent request already has a code."""
        return conn.execute("SELECT 1 FROM oauth_codes WHERE request_nonce = %s", (nonce,)).fetchone() is not None

    async def consent_state(request: Request, req: str | None) -> tuple[Response | None, dict[str, Any]]:
        """Run the consent checks in order (plan step 7) and return (refusal, context).

        Pending request, registered client, this browser's binding cookie, a live session, a wallet, and a nonce
        without a code yet. A missing wallet is not a refusal: the page shows the retry form instead of Allow.
        """
        pending = oauth.read_pending(settings, req)
        if pending is None or req is None:
            return error(request, "This connection request does not work",
                         "It has expired or is not valid. Go back to your AI app and connect again.", 400), {}
        client = await provider.get_client(pending.client_id)
        if client is None:
            return error(request, "This connection request does not work",
                         "The app that sent you here is not registered any more. Go back to it and connect again.",
                         400), {}
        binding = request.cookies.get(oauth.binding_cookie_name(settings, pending.nonce))
        if not _equal(binding, oauth.binding_value(settings, pending.nonce)):
            return error(request, "Finish connecting in the other browser",
                         "Finish connecting in the browser where you clicked Connect, or start again from your AI "
                         "app in this one.", 400), {}
        session, user, stale = await current(request)
        if session is None or user is None:
            response = _redirect(_sign_in_url(f"{oauth.CONSENT_PATH}?req={req}"))
            if stale:
                clear_cookie(response, session_cookie)
            return response, {}

        user_id = str(user["id"])

        def state(conn: db.Connection) -> tuple[users.Identity | None, bool]:
            return users.identity_for(conn, user_id), nonce_used(conn, pending.nonce)

        identity, used = await run_db(state)
        if used:
            return error(request, "Already approved",
                         "You already approved this connection. Go back to your AI app.", 409), {}
        redirect_uri = pending.redirect_uri
        return None, {
            "pending": pending,
            "session": session,
            "user": user,
            "identity": identity,
            "req": req,
            "client_name": oauth.sanitize_client_name(client.client_name),
            "redirect_display": oauth.redirect_display(redirect_uri),
            "trusted": oauth.is_trusted_redirect(redirect_uri),
            "email": user["email"],
            "wallet": identity.wallet if identity else None,
            "basescan_url": BASESCAN_ADDRESS + identity.wallet if identity else None,
            "faucet_url": FAUCET,
            "csrf": auth.csrf_token(settings, session.value, "consent:" + pending.nonce),
            "retry_next": f"{oauth.CONSENT_PATH}?req={req}",
            "account_csrf": auth.csrf_token(settings, session.value, "account"),
        }

    @app.get("/oauth/consent")
    async def consent_page(request: Request) -> Response:
        """Ask the signed in user to allow or deny the app that sent them here."""
        refusal, context = await consent_state(request, request.query_params.get("req"))
        if refusal is not None:
            return refusal
        source = oauth.form_action_source(context["pending"].redirect_uri)
        form_action = "'self'" if source == "'none'" else f"'self' {source}"
        return page(request, "consent.html", context, headers={"Content-Security-Policy": csp(form_action)})

    @app.post("/oauth/consent")
    async def consent_answer(request: Request) -> Response:
        """Allow (write the code and send it to the app) or Deny (tell the app no); both clear the binding cookie."""
        if not auth.same_origin(settings, request.headers):
            return forbidden(request)
        form = await request.form()
        refusal, context = await consent_state(request, _first(form, "req"))
        if refusal is not None:
            return refusal
        pending: oauth.PendingRequest = context["pending"]
        session: auth.Session = context["session"]
        user_id = str(context["user"]["id"])
        if not auth.check_csrf(settings, session.value, "consent:" + pending.nonce, _first(form, "csrf")):
            return forbidden(request)
        action = _first(form, "action")
        if action == "deny":
            location = oauth.deny_redirect(settings, pending)
            log.info("consent denied: user %s, client %s", user_id, pending.client_id)
        elif action == "allow":
            def allow(conn: db.Connection) -> str | None:
                return oauth.approve(conn, settings, pending, user_id, session_epoch=session.epoch)

            try:
                approved = await run_db(allow)
            except PermissionError:
                response = error(request, "You were signed out",
                                 "Sign in again, then go back to your AI app and connect again.", 403)
                clear_cookie(response, session_cookie)
                return response
            except db.IntegrityError:
                return error(request, "This connection request does not work",
                             "The app that sent you here is not registered any more. Go back to it and connect "
                             "again.", 400)
            except ValueError:
                return error(request, "Your wallet is not ready yet",
                             "Sentinel connects an app only once your wallet exists. Open your account page to "
                             "retry, then connect again.", 409)
            if approved is None:
                return error(request, "Already approved",
                             "You already approved this connection. Go back to your AI app.", 409)
            location = approved
        else:
            return error(request, "That request was refused", "Choose Allow or Deny.", 400)
        response = _redirect(location)
        clear_cookie(response, oauth.binding_cookie_name(settings, pending.nonce))  # only this flow's cookie
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/", mcp_app)
    app.add_middleware(SecurityHeaders, settings=settings)
    app.add_middleware(LocalhostAlias, settings=settings)
    app.add_middleware(McpCors)  # added last, so outermost: even a preflight never reaches the routes
    return app
