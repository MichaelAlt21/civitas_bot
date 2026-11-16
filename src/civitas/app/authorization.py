"""Adapter for citizen admission checks used by the application services."""

from collections.abc import Awaitable, Callable

type Authorizer = Callable[[int | None, int], Awaitable[None] | None]


def as_async_authorizer(authorize: Authorizer) -> Callable[[int | None, int], Awaitable[None]]:
    async def check(guild_id: int | None, user_id: int) -> None:
        result = authorize(guild_id, user_id)
        if result is not None:
            await result

    return check
