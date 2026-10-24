"""Sending the sign in email: through Resend in production, printed to stderr on a local run.

app.py builds the message with sign_in_message and sends it through the Mailer that make_mailer picked (tests pass a
recording one). ResendMailer posts to Resend's API with an Idempotency-Key of "magic-link/<magic_links.id>", so a
retried request never sends two emails, and retries network errors, 429 and 5xx a couple of times. Its errors name
only the HTTP status (or "network error"): never the response body, the address or the API key. ConsoleMailer is for
dev mode only (load_settings allows missing mail settings only on localhost) and is the one place a link is printed.
"""
import asyncio
import html
import importlib.metadata
import sys
from dataclasses import dataclass
from typing import Protocol, TextIO

import httpx

from sentinel.web import Settings

sleep = asyncio.sleep  # module level so tests can skip the backoff


def _version() -> str:
    """The installed sentinel version, for the User-Agent header."""
    try:
        return importlib.metadata.version("sentinel")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


USER_AGENT = f"sentinel/{_version()}"


@dataclass(frozen=True)
class Message:
    """One email: recipient, subject, plain text and HTML bodies, and the key that makes resending it a no op."""

    to: str
    subject: str
    text: str
    html: str
    idempotency_key: str  # "magic-link/<magic_links.id>"


class Mailer(Protocol):
    """Anything that can send a Message (ResendMailer, ConsoleMailer, the tests' recording mailer)."""

    async def send(self, message: Message) -> None:
        """Send the message or raise MailError."""
        ...


class MailError(Exception):
    """Sending failed. The message names the HTTP status (or "network error") only."""


class ResendMailer:
    """Sends mail through Resend's HTTP API, retrying network errors, 429 and 5xx with backoff."""

    URL = "https://api.resend.com/emails"

    def __init__(
        self,
        api_key: str,
        mail_from: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
        retries: int = 2,
        backoff_s: float = 0.5,
    ) -> None:
        """Keep the key and sender; transport lets tests answer with httpx.MockTransport."""
        self._api_key = api_key
        self._mail_from = mail_from
        self._transport = transport
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_s = backoff_s

    def __repr__(self) -> str:
        """Never show the API key."""
        return "ResendMailer()"

    async def send(self, message: Message) -> None:
        """POST the message to Resend; retry what is worth retrying, raise MailError otherwise."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
            "Idempotency-Key": message.idempotency_key,
        }
        body = {
            "from": self._mail_from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
            "html": message.html,
        }
        failure = "network error"
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout_s) as client:
            for attempt in range(self._retries + 1):
                if attempt:
                    await sleep(self._backoff_s * 2 ** (attempt - 1))
                try:
                    response = await client.post(self.URL, json=body, headers=headers)
                except httpx.TransportError:
                    failure = "network error"
                    continue
                if response.is_success:
                    return
                failure = str(response.status_code)
                if response.status_code != 429 and response.status_code < 500:
                    break  # a request Resend refuses will not pass on a retry
        if failure == "network error":
            raise MailError("Resend could not be reached (network error)")
        raise MailError(f"Resend answered {failure}")


class ConsoleMailer:
    """Prints the sign in link instead of mailing it. Dev mode only (a local run without mail settings)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        """Print to stream, or to sys.stderr as it is at send time."""
        self._stream = stream

    async def send(self, message: Message) -> None:
        """Print "Sign in link for <to>: <link>" (the whole text body when it holds no link)."""
        link = next((word for word in message.text.split() if word.startswith(("http://", "https://"))), None)
        stream = self._stream or sys.stderr
        print(f"Sign in link for {message.to}: {link or message.text}", file=stream, flush=True)


def sign_in_message(settings: Settings, *, to: str, link: str, link_id: int) -> Message:
    """The sign in email: the link, that it expires in 15 minutes and works once, and what to do if unexpected.

    The Idempotency-Key is "magic-link/<link_id>". Resend remembers keys per API key for 24 hours, so every
    deployment and database (staging, production, a restored copy) needs its own Resend API key, or link ids repeat.
    """
    text = (
        "Open this link to sign in to Sentinel:\n\n"
        f"{link}\n\n"
        "The link expires in 15 minutes and works once.\n\n"
        "If you did not ask to sign in, ignore this email.\n"
    )
    safe = html.escape(link, quote=True)
    body = (
        "<p>Open this link to sign in to Sentinel:</p>"
        f'<p><a href="{safe}">{safe}</a></p>'
        "<p>The link expires in 15 minutes and works once.</p>"
        "<p>If you did not ask to sign in, ignore this email.</p>"
    )
    return Message(
        to=to, subject="Sign in to Sentinel", text=text, html=body, idempotency_key=f"magic-link/{link_id}"
    )


def make_mailer(settings: Settings) -> Mailer:
    """ResendMailer when an API key is set, else ConsoleMailer (allowed in dev mode only)."""
    if settings.resend_api_key:
        if not settings.mail_from:
            raise ValueError("MAIL_FROM is required when RESEND_API_KEY is set")
        return ResendMailer(settings.resend_api_key, settings.mail_from)
    if not settings.dev_mode:
        raise ValueError("RESEND_API_KEY is required outside localhost")
    return ConsoleMailer()
