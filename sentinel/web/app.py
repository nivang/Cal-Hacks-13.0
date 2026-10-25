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

from sentinel import (
    chain,
    db,
    injection_detector,
    judge,
    mandates,
    mcp_server,
    owner,
    payments,
    results,
    texting,
    wallets,
)
from sentinel.web import Settings, auth, books, oauth, pages, ratelimit, users
from sentinel.web.mailer import Mailer, MailError, make_mailer, sign_in_message

Connect = Callable[[], AbstractContextManager[db.Connection]]
OpenSigner = Callable[..., AbstractAsyncContextManager[ClientEvmSigner]]  # open_user_wallet(user_id, *, create)
T = TypeVar("T")

STATIC_DIR = Path(__file__).parent / "static"
FAVICON = STATIC_DIR / "favicon.svg"
BINDING_TTL_S = oauth.PENDING_TTL_S  # the consent binding cookie lives as long as the pending request
BASESCAN_ADDRESS = "https://sepolia.basescan.org/address/"
FAUCET = "https://faucet.circle.com"

CSP_BEFORE_FORM = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'none'; object-src 'none'; "
)
CSP_AFTER_FORM = "; frame-ancestors 'none'"
SITE_CSP = CSP_BEFORE_FORM + "form-action 'self'" + CSP_AFTER_FORM

SITE_PATHS = frozenset({
    "/", "/signin", "/auth/verify", "/account", "/account/wallet", "/account/signout-everywhere", "/signout",
    "/account/live", "/account/payments", "/account/payments.csv", "/account/rules", "/account/rules/limits",
    "/account/rules/sellers", "/account/rules/auto-buy", "/account/books", "/account/tasks",
    "/account/tasks/revoke",
    "/account/texting", "/account/texting/code", "/account/texting/unlink",
    "/oauth/consent", "/healthz", "/favicon.ico", "/authorize",
})
DONE_TEXT = {  # the ?done= values the Rules page shows a banner for; anything else is ignored
    "limits": "Saved. Your new limits apply to the next payment.",
    "seller": "Saved. The seller list is updated.",
    "auto_buy": "Saved. The new setting applies to the next payment.",
}
TASK_DONE_TEXT = {  # the ?done= values the Tasks page shows a banner for
    "revoke": "Revoked. Nothing more can be bought for that task.",
}
SWEEP_EVERY_S = 60  # how often the server retries escrow captures and voids that failed
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
    texts: texting.TextDesk | None = None,
) -> FastAPI:
    """Build the site, the OAuth server and /mcp as one app.

    connect: how requests borrow a database connection. mailer: where sign in links go (make_mailer(settings) when
    None). pay_fn, open_signer and judge_ go to mcp_server.create_server (tests replace them). open_operator opens
    Sentinel's escrow operator (escrow.open_operator, which main passes); with it, payments use escrow when a seller
    offers it, and a background sweep retries escrow captures and voids every SWEEP_EVERY_S.
    texts: the texting desk (texting.from_env, which main passes); None turns /texting/* off.
    The pool is not touched here; mcp_server.main() closes it when uvicorn stops.
    """
    mail = mailer if mailer is not None else make_mailer(settings)
    provider = oauth.SentinelOAuthProvider(settings, connect)
    server = mcp_server.create_server(auth=provider, pay_fn=pay_fn, open_signer=open_signer, connect=connect,
                                      judge_=judge_, open_operator=open_operator)
    # Stateful sessions with SSE responses, the transport connected clients already use.
    mcp_app = server.http_app(path=oauth.MCP_PATH, json_response=False, stateless_http=False)

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
        # A configured but unusable detector must prevent startup, not silently disable the screen.
        await asyncio.to_thread(injection_detector.initialize)
        task = asyncio.create_task(sweep_forever()) if open_operator is not None else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            if texts is not None:
                await texts.close()

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
            "mcp_url": settings.resource_url,
            "claude_command": f"claude mcp add --transport http sentinel {settings.resource_url}",
            "cap": usdc(view["per_payment_cap"]),
            "budget": usdc(view["weekly_budget"]),
            "spent": usdc(view["week_spend"]),
            "percent": percent,
            "bar": "bg-red-600" if percent >= 100 else "bg-amber-500" if percent >= WARN_PERCENT else "bg-stone-900",
            "error": None,
        }

    section_names = {
        "/account": {"overview_spending", "overview_cap", "overview_wallet", "overview_setup", "overview_recent"},
        "/account/live": {"live_feed"},
        "/account/payments": {"payments_list"},
        "/account/rules": {"rules_limits", "rules_auto_buy", "rules_sellers", "seller_wallet"},
        "/account/tasks": {"tasks_escrow", "tasks_list"},
        "/account/books": {"books_summary", "books_journal", "books_totals"},
    }

    LIVE_SECTIONS = {"payments_list", "live_feed"}  # pages left open on a screen: they poll for changes

    def live_data(conn: db.Connection, view: Mapping[str, Any], section: str) -> dict[str, Any]:
        """The data of one polling section, read on the caller's connection."""
        if section == "live_feed":
            return books.live_view(conn, view["agent_id"])
        groups = books.payments_by_task(conn, view["agent_id"])
        return {"groups": groups, "payments": [row for group in groups for row in group["rows"]],
                "csv_limit": f"{books.CSV_LIMIT:,}"}

    async def live_section(request: Request, path: str, section: str) -> Response:
        """A polling section in one database checkout: session, account, change tag, then the data.

        Over a slow link every checkout and query is a round trip, so this does them all on one connection. The
        response carries an ETag from books.fingerprint; a poll that sends the same tag back gets 304 and no body.
        """
        raw = request.cookies.get(session_cookie)
        session = auth.read_session(settings, raw) if raw is not None else None
        if session is None:
            return sign_in_redirect(path, raw is not None)
        sent = request.headers.get("if-none-match")

        def load(conn: db.Connection) -> tuple[dict[str, Any], str | None, dict[str, Any] | None] | None:
            user = users.session_user(conn, session.user_id, session.epoch)
            if user is None:
                return None
            view = users.account_view(conn, user["id"], False)
            if view["agent_id"] is None:
                return view, None, {}
            tag = books.fingerprint(conn, view["agent_id"])
            etag = f'W/"{section}-{tag}"'
            if sent == etag:
                return view, etag, None
            return view, etag, live_data(conn, view, section)

        loaded = await run_db(load)
        if loaded is None:
            return sign_in_redirect(path, True)
        view, etag, data = loaded
        if data is None and etag is not None:
            return Response(status_code=304, headers={"ETag": etag})
        tab = path.rsplit("/", 1)[-1]
        response = page(request, "account_fragment.html",
                        {**account_context(session, view, tab), "section": section, **(data or {})})
        if etag is not None:
            response.headers["ETag"] = etag
        return response

    async def account_section(
        request: Request, path: str, session: auth.Session, user: Mapping[str, Any], section: str,
    ) -> Response:
        """Load only one authenticated region, without waiting for unrelated queries or services."""
        if section not in section_names.get(path, set()) | {"identity"}:
            return error(request, "Unknown section", "Reload the page and try again.", 400)
        if section == "identity":
            return page(request, "account_fragment.html", {"section": section, "email": user["email"]})
        view = await run_db(users.account_view, user["id"], section in {"overview_spending", "rules_limits"})
        aid = view["agent_id"]
        tab = "overview" if path == "/account" else path.rsplit("/", 1)[-1]
        context = {**account_context(session, view, tab), "section": section}
        if aid is not None:
            if section == "overview_setup":
                def setup(conn: db.Connection) -> dict[str, Any]:
                    return {
                        "has_seller": any(s["status"] == "approved" for s in books.list_sellers(conn, aid)),
                        "connected": books.has_live_grant(conn, str(user["id"])),
                    }
                found = await run_db(setup)
                steps = [
                    (True, "Create your wallet", None),
                    (found["has_seller"], "Approve a seller your agent may pay", "/account/rules"),
                    (found["connected"], "Connect your AI app", "/account/rules#connect-h"),
                ]
                context.update(steps=steps, all_set=all(done for done, _, _ in steps))
            elif section == "overview_recent":
                context["recent"] = await run_db(books.recent_payments, aid)
            elif section == "rules_sellers":
                sellers = await run_db(books.list_sellers, aid)
                context["lazy_wallets"] = True
                context.update(sellers=sellers, typed_name="", typed_pay_to="")
            elif section == "seller_wallet":
                address = request.headers.get("x-sentinel-wallet", "")
                sellers = await run_db(books.list_sellers, aid)
                if not any(s["pay_to"].lower() == address.lower() for s in sellers):
                    return error(request, "Unknown seller", "This seller is not on your list.", 404)
                with connect() as conn:
                    profile = await chain.cached_profile(conn, address)
                context.update(wallet_label=profile.summary() if profile else "could not check",
                               wallet_warn=bool(profile and (profile.flagged or profile.unused or profile.is_new())))
            elif section == "tasks_escrow":
                context["escrow_held"] = await run_db(books.escrow_now, aid)
            elif section == "tasks_list":
                context["tasks"] = await run_db(mandates.list_for_agent, aid)
                comparisons: dict[int, list[dict[str, Any]]] = {}
                for c in await run_db(mandates.list_comparisons, aid):
                    comparisons.setdefault(c["mandate_id"], []).append(c)
                context["comparisons"] = comparisons
            elif section == "books_summary":
                context["summary"] = await run_db(books.summary, aid)
                context["totals"] = await run_db(books.totals, aid)
            elif section == "books_journal":
                context["journal"] = await run_db(books.journal, aid)
            elif section == "books_totals":
                context["totals"] = await run_db(books.totals, aid)
        elif section == "overview_setup":
            context.update(steps=[(False, "Create your wallet", None),
                                  (False, "Approve a seller your agent may pay", "/account/rules"),
                                  (False, "Connect your AI app", "/account/rules#connect-h")], all_set=False)
        if section == "rules_limits":
            context.update(typed_cap=results.usdc_text(view["per_payment_cap"]) if aid is not None else "",
                           typed_budget=results.usdc_text(view["weekly_budget"]) if aid is not None else "")
        return page(request, "account_fragment.html", context)

    async def account_get(request: Request, path: str) -> tuple[Response | None, auth.Session | None, dict[str, Any]]:
        """The checks every account page shares: a live session, else the sign in redirect. Returns the user's view."""
        # Only render public layout here. The content request below still checks the live session in the database,
        # including revocation, before exposing any account data. Non-browser clients and the no-JS link get HTML.
        if (request.method == "GET" and request.url.path == path
                and "text/html" in request.headers.get("accept", "")
                and request.headers.get("x-sentinel-content") != "1"
                and not request.headers.get("x-sentinel-section")
                and request.query_params.get("full") != "1"):
            session = auth.read_session(settings, request.cookies.get(session_cookie))
            if session is None:
                return sign_in_redirect(path, False), None, {}
            tab, title = {
                "/account": ("overview", "Your account"),
                "/account/live": ("live", "Live"),
                "/account/payments": ("payments", "Payments"),
                "/account/tasks": ("tasks", "Tasks"),
                "/account/rules": ("rules", "Rules"),
                "/account/books": ("books", "Books"),
            }[path]
            return page(request, "account_loading.html", {
                "tab": tab, "title": title, "email": "", "loading": True,
                "account_template": "account.html" if tab == "overview" else f"account_{tab}.html",
                "mcp_url": settings.resource_url,
                "claude_command": f"claude mcp add --transport http sentinel {settings.resource_url}",
                "done": request.query_params.get("done"),
                "done_text": (TASK_DONE_TEXT if tab == "tasks" else DONE_TEXT).get(
                    request.query_params.get("done", ""), ""),
                "csrf": auth.csrf_token(settings, session.value, "account"),
                "full_url": str(request.url.include_query_params(full="1")),
            }), None, {}
        section = request.headers.get("x-sentinel-section")
        if section in LIVE_SECTIONS and section in section_names.get(path, set()):
            return await live_section(request, path, section), None, {}
        session, user, stale = await current(request)
        if session is None or user is None:
            return sign_in_redirect(path, stale), None, {}
        if section:
            return await account_section(request, path, session, user, section), None, {}
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
                return {"recent": [], "has_seller": False, "connected": books.has_live_grant(conn, user_id)}
            return {
                "recent": books.recent_payments(conn, aid),
                "has_seller": any(sl["status"] == "approved" for sl in books.list_sellers(conn, aid)),
                "connected": books.has_live_grant(conn, user_id),
            }

        found = await run_db(data)
        steps = [
            (aid is not None, "Create your wallet", None),
            (found["has_seller"], "Approve a seller your agent may pay", "/account/rules"),
            (found["connected"], "Connect your AI app", "/account/rules#connect-h"),
        ]
        context = {
            **account_context(session, view, "overview"), **found, "steps": steps,
            "all_set": all(done for done, _, _ in steps),
            "mcp_url": settings.resource_url,
            "claude_command": f"claude mcp add --transport http sentinel {settings.resource_url}",
        }
        return page(request, "account.html", context)

    @app.get("/account/live")
    async def account_live(request: Request) -> Response:
        """Live: each payment moving through Sentinel's checks as it happens, for a screen left open."""
        refusal, session, view = await account_get(request, "/account/live")
        if refusal is not None or session is None:
            return refusal or forbidden(request)
        aid = view["agent_id"]
        found = await run_db(books.live_view, aid) if aid is not None else {}
        return page(request, "account_live.html", {**account_context(session, view, "live"), **found})

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
        error: str | None = None, typed: Mapping[str, str] | None = None,
    ) -> Response:
        """Render Rules: limits and the sellers that apply to this agent."""
        aid = view["agent_id"]

        def data(conn: db.Connection) -> list[dict[str, Any]]:
            return books.list_sellers(conn, aid)

        sellers = await run_db(data) if aid is not None else []

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
            "sellers": sellers, "error": error,
            "done": done if done in DONE_TEXT and error is None else None,
            "done_text": DONE_TEXT.get(done or "", ""),
            "typed_cap": typed.get("cap", results.usdc_text(view["per_payment_cap"]) if aid is not None else ""),
            "typed_budget": typed.get("budget", results.usdc_text(view["weekly_budget"]) if aid is not None else ""),
            "typed_name": typed.get("name", ""),
            "typed_pay_to": typed.get("pay_to", ""),
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
        """Render Tasks: what the agent is working on, money in escrow now, and each task's price comparisons."""
        aid = view["agent_id"]

        def data(conn: db.Connection) -> dict[str, Any]:
            comparisons: dict[int, list[dict[str, Any]]] = {}
            for c in mandates.list_comparisons(conn, aid):
                comparisons.setdefault(c["mandate_id"], []).append(c)
            return {"tasks": mandates.list_for_agent(conn, aid),
                    "escrow_held": books.escrow_now(conn, aid), "comparisons": comparisons}

        empty: dict[str, Any] = {"tasks": [], "escrow_held": [], "comparisons": {}}
        found = await run_db(data) if aid is not None else empty
        done = request.query_params.get("done")
        context = {
            **account_context(session, view, "tasks"), **found, "error": error,
            "done": done if done in TASK_DONE_TEXT and error is None else None,
            "done_text": TASK_DONE_TEXT.get(done or "", ""),
        }
        return page(request, "account_tasks.html", context, status)

    @app.get("/account/tasks")
    async def account_tasks(request: Request) -> Response:
        """Tasks: what the agent is working on; the owner can revoke a task (stop all its spending)."""
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

    @app.post("/account/rules/auto-buy")
    async def save_auto_buy(request: Request) -> Response:
        """Turn the session user's own auto buy on or off; the agent is never a form field."""
        refusal, user, form = await account_post(request, "/account/rules")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        view = await run_db(users.account_view, user["id"])
        session, _, _ = await current(request)
        if session is None:
            return forbidden(request)
        choice = _first(form, "auto_buy")
        if view["agent"] is None:
            return await rules_page(request, session, view, status=400,
                                    error="Create your wallet first, on the Overview page.")
        if choice not in ("on", "off"):
            return forbidden(request)

        def save(conn: db.Connection) -> None:
            with db.transaction(conn):
                owner.set_auto_buy(conn, view["agent"], choice == "on")

        await run_db(save)
        log.info("user %s turned auto buy %s", user["id"], choice)
        return _redirect("/account/rules?done=auto_buy")

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

    async def task_action(request: Request, action: Callable[[db.Connection, Mapping[str, Any], Mapping[str, Any],
                                                              str], None], done: str) -> Response:
        """The checks every Tasks form shares, then action(conn, view, form, user id) in one transaction.

        A ValueError from the action is shown on the Tasks page; success goes back to it with a banner. The agent is
        always the session user's own, never a form field, so nobody can revoke another user's task.
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

    @app.post("/account/tasks/revoke")
    async def revoke_task(request: Request) -> Response:
        """Revoke one of the user's tasks: nothing more can be bought for it."""

        def revoke(conn: db.Connection, view: Mapping[str, Any], form: Mapping[str, Any], _user: str) -> None:
            mandates.revoke(conn, form_id(form, "task_id"), view["agent_id"])

        return await task_action(request, revoke, "revoke")

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

    # Texting (texting.py): the relay's two routes, then the page that links a phone

    @app.post("/texting/inbound")
    async def texting_inbound(request: Request) -> Response:
        """One incoming text from the relay: {"from": handle, "text": ...}. The reply arrives in the outbox."""
        if texts is None or not texts.relay_allowed(request.headers.get("authorization")):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "send JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "send a JSON object"}, status_code=400)
        accepted = texts.receive(body.get("from"), body.get("text"))
        return JSONResponse({"accepted": accepted}, status_code=202 if accepted else 400)

    @app.get("/texting/outbox")
    async def texting_outbox(request: Request) -> Response:
        """The texts waiting to be sent, held open up to ?wait= seconds (at most 30) until there is one."""
        if texts is None or not texts.relay_allowed(request.headers.get("authorization")):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            wait = min(max(float(request.query_params.get("wait") or 25), 0.0), 30.0)
        except ValueError:
            wait = 25.0
        return Response(texting.message_json(await texts.take(wait)), media_type="application/json")

    async def texting_page(request: Request, session: auth.Session, user: Mapping[str, Any],
                           code: str | None = None) -> Response:
        """Render Texting: the linked handle, or a code to link one."""
        view = await run_db(users.account_view, user["id"], False)
        handle = await run_db(texting.linked_handle, str(user["id"])) if texts is not None else None
        return page(request, "account_texting.html", {
            **account_context(session, view, "texting"), "enabled": texts is not None,
            "handle": texting.display_handle(handle) if handle else None, "code": code,
        })

    @app.get("/account/texting")
    async def account_texting(request: Request) -> Response:
        """Texting: link a phone so its texts reach this user's agent."""
        session, user, stale = await current(request)
        if session is None or user is None:
            return sign_in_redirect("/account/texting", stale)
        return await texting_page(request, session, user)

    @app.post("/account/texting/code")
    async def texting_code(request: Request) -> Response:
        """A fresh one time code; texting it from a phone links that phone to the session user."""
        refusal, user, _ = await account_post(request, "/account/texting")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        session, _, _ = await current(request)
        if session is None or texts is None:
            return forbidden(request)
        code = await run_db(texting.issue_code, settings.key("phone-link"), str(user["id"]))
        log.info("user %s asked for a texting code", user["id"])
        return await texting_page(request, session, user, code)

    @app.post("/account/texting/unlink")
    async def texting_unlink(request: Request) -> Response:
        """Unlink the session user's phone."""
        refusal, user, _ = await account_post(request, "/account/texting")
        if refusal is not None or user is None:
            return refusal or forbidden(request)
        await run_db(texting.unlink, str(user["id"]))
        log.info("user %s unlinked their texting handle", user["id"])
        return _redirect("/account/texting")

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
