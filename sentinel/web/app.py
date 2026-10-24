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
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from x402.mechanisms.evm import ClientEvmSigner

from sentinel import db, mcp_server, payments, results, wallets
from sentinel.web import Settings, auth, oauth, pages, ratelimit, users
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
    "/oauth/consent", "/healthz", "/favicon.ico", "/authorize",
})
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
) -> FastAPI:
    """Build the site, the OAuth server and /mcp as one app.

    connect: how requests borrow a database connection. mailer: where sign in links go (make_mailer(settings) when
    None). pay_fn and open_signer go to mcp_server.create_server (tests replace them). The pool is not touched here;
    mcp_server.main() closes it when uvicorn stops.
    """
    mail = mailer if mailer is not None else make_mailer(settings)
    provider = oauth.SentinelOAuthProvider(settings, connect)
    server = mcp_server.create_server(auth=provider, pay_fn=pay_fn, open_signer=open_signer, connect=connect)
    mcp_app = server.http_app(path=oauth.MCP_PATH, json_response=True, stateless_http=True)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=combine_lifespans(mcp_app.lifespan))
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
            "It did not come from a Sentinel page. Go back, reload the page and try again.", 403,
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

    @app.get("/account")
    async def account(request: Request) -> Response:
        """The user's email, wallet, limits and how to connect their AI app."""
        session, user, stale = await current(request)
        if session is None or user is None:
            response = _redirect(_sign_in_url("/account"))
            if stale:
                clear_cookie(response, session_cookie)
            return response
        view = await run_db(users.account_view, user["id"])
        address = view["wallet_address"]
        context = {
            **view,
            "csrf": auth.csrf_token(settings, session.value, "account"),
            "basescan_url": BASESCAN_ADDRESS + address if address else None,
            "faucet_url": FAUCET,
            "cap": usdc(view["per_payment_cap"]),
            "budget": usdc(view["weekly_budget"]),
            "spent": usdc(view["week_spend"]),
            "mcp_url": settings.resource_url,
            "claude_command": f"claude mcp add --transport http sentinel {settings.resource_url}",
        }
        return page(request, "account.html", context)

    async def account_post(request: Request) -> tuple[Response | None, dict[str, Any] | None, Mapping[str, Any]]:
        """The checks every account form shares: same origin, a live session and its CSRF token.

        Returns (refusal, user, form): refusal is None when every check passed.
        """
        if not auth.same_origin(settings, request.headers):
            return forbidden(request), None, {}
        form = await request.form()
        session, user, stale = await current(request)
        if session is None or user is None:
            response = _redirect(_sign_in_url("/account"))
            if stale:
                clear_cookie(response, session_cookie)
            return response, None, form
        if not auth.check_csrf(settings, session.value, "account", _first(form, "csrf")):
            return forbidden(request), None, form
        return None, user, form

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
    app.add_middleware(McpCors)  # added last, so outermost: even a preflight never reaches the routes
    return app
