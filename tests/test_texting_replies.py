"""Tests for how the texting agent talks: short answers with MORE for the rest, and progress lines once per turn.

Pure: a TextDesk with a fake client and judge, its outbox read directly. No database, no model.
"""
import pytest

from sentinel import texting

PHONE = "+15555550123"


@pytest.fixture
def desk():
    return texting.TextDesk("http://sentinel.test", b"k" * 32, "s" * 32, client=object(), judge_=object())


def outbox(desk):
    texts = [m.text for m in desk.outbox]
    desk.outbox.clear()
    return texts


def test_a_short_answer_is_sent_as_is(desk):
    convo = texting.Conversation()
    desk.answer(PHONE, convo, "Acme raised a Series B.")
    assert outbox(desk) == ["Acme raised a Series B."] and convo.more == []


def test_a_long_answer_waits_for_more_and_more_sends_the_rest(desk):
    convo = desk.conversation(PHONE)
    text = ("word " * 400).strip()
    desk.answer(PHONE, convo, text)
    first = outbox(desk)
    assert len(first) == 1 and first[0].endswith(texting.MORE_HINT)
    assert len(first[0]) <= texting.MAX_REPLY + len(texting.MORE_HINT)
    parts = [first[0].removesuffix(texting.MORE_HINT)]
    while convo.more:
        desk.send_more(PHONE)
        parts.append(outbox(desk)[0].removesuffix(texting.MORE_HINT))
    assert " ".join(parts).split() == text.split()
    desk.send_more(PHONE)
    assert outbox(desk) == [texting.NO_MORE]


def test_progress_lines_go_out_once_per_turn(desk):
    convo = texting.Conversation()
    desk.note(PHONE, convo, "research", "Researching... :)")
    desk.note(PHONE, convo, "research", "Researching... :)")
    desk.note(PHONE, convo, "checking", texting.CHECKING)
    assert outbox(desk) == ["Researching... :)", texting.CHECKING]
    convo.noted.clear()  # a new turn
    desk.note(PHONE, convo, "research", "Researching... :)")
    assert outbox(desk) == ["Researching... :)"]
