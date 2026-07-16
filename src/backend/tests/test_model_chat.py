"""Direct coverage for ``ChatModel(app_state)`` (#1576).

Exercises the chat methods on ``app_state.model.chat`` — the app_state-owned
form app code now reaches — covering the branches the backstop-only tests
in ``test_model.py`` don't touch (the method bodies run only when callers
go through the owned instance, not the module-level free functions). Mirrors
the ``test_model_users.py`` pattern.
"""

import pytest

from klangk_backend.model.chat import MSG_AGENT, MSG_USER


@pytest.fixture
async def chat(app_state, db):
    """``app_state.model.chat`` with the schema initialized."""
    return app_state.model.chat


async def test_get_chat_messages_before_anchor_missing_returns_empty(
    chat, workspace, user
):
    """A ``before_id`` that doesn't exist resolves to an empty list (the
    anchor-lookup ``None`` branch in the method body)."""
    msgs = await chat.get_chat_messages_before(
        workspace["id"], "nonexistent-id"
    )
    assert msgs == []


async def test_get_chat_messages_includes_mentions(chat, workspace, user):
    """``get_chat_messages`` via the owned instance returns the mentions
    resolved at insert time (the mentions-append branch)."""
    await chat.add_chat_message(
        workspace["id"],
        user["id"],
        user["email"],
        f"hello @{user['handle']}",
    )
    msgs = await chat.get_chat_messages(workspace["id"])
    assert len(msgs) == 1
    assert msgs[0]["mentions"] == [user["id"]]


async def test_get_chat_messages_before_includes_mentions(
    chat, workspace, user
):
    """``get_chat_messages_before`` via the owned instance returns mentions
    for older messages (the mentions-append branch)."""
    first = await chat.add_chat_message(
        workspace["id"],
        user["id"],
        user["email"],
        f"earlier @{user['handle']}",
    )
    # Anchor added after, so ``first`` is "before" it.
    anchor = await chat.add_chat_message(
        workspace["id"],
        user["id"],
        user["email"],
        "later message",
    )
    older = await chat.get_chat_messages_before(workspace["id"], anchor["id"])
    assert [m["id"] for m in older] == [first["id"]]
    assert older[0]["mentions"] == [user["id"]]


async def test_add_and_delete_via_instance(chat, workspace, user):
    """Round-trip add/list/delete through the owned instance."""
    msg = await chat.add_chat_message(
        workspace["id"], user["id"], user["email"], "hi", message_type=MSG_USER
    )
    assert msg["message"] == "hi"
    assert msg["message_type"] == MSG_USER
    assert msg["user_handle"] == user["handle"]
    deleted = await chat.delete_chat_message(msg["id"], user["id"])
    assert deleted is True
    # Wrong author can't delete.
    other = await chat.add_chat_message(
        workspace["id"], user["id"], user["email"], "keep"
    )
    assert await chat.delete_chat_message(other["id"], "someone-else") is False


async def test_parse_mentions_no_candidates(chat, workspace, user):
    """A message with no @handles short-circuits to an empty list."""
    # Open a connection to pass to the mid-transaction helper.
    db = await chat.app_state.db.get_db()
    try:
        assert (
            await chat.parse_mentions(db, "plain text", workspace["id"]) == []
        )
    finally:
        await db.commit()
        await db.close()


async def test_get_chat_messages_empty(chat, workspace):
    """An empty workspace returns no messages and no mention lookup runs."""
    assert await chat.get_chat_messages(workspace["id"]) == []


async def test_agent_message_type(chat, workspace, user):
    """An agent-typed message is stored and read back with its type."""
    await chat.add_chat_message(
        workspace["id"],
        user["id"],
        user["email"],
        "agent reply",
        message_type=MSG_AGENT,
    )
    msgs = await chat.get_chat_messages(workspace["id"])
    assert msgs[0]["message_type"] == MSG_AGENT
