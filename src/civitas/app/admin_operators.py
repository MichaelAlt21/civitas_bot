from collections.abc import Iterable

from .citizens import is_discord_id


class AdminOperatorPolicy:
    def __init__(self, *, discord_guild_id: int, operator_user_ids: Iterable[int]) -> None:
        self._discord_guild_id = discord_guild_id
        self._operator_user_ids = frozenset(operator_user_ids)

    def allows(self, discord_guild_id: int | None, actor_user_id: int) -> bool:
        return (
            is_discord_id(discord_guild_id)
            and discord_guild_id == self._discord_guild_id
            and is_discord_id(actor_user_id)
            and actor_user_id in self._operator_user_ids
        )
