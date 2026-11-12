"""Send private Discord replies without parsing mentions or embedding links."""

import discord

_NO_MENTIONS = discord.AllowedMentions.none()
_MESSAGE_LIMIT = 2000


async def send_private_lines(interaction: discord.Interaction, *parts: str) -> None:
    page = ""
    for part in parts:
        candidate = f"{page}\n{part}" if page else part
        if page and len(candidate.encode("utf-16-le")) // 2 > _MESSAGE_LIMIT:
            await _send_page(interaction, page)
            page = part
        else:
            page = candidate
    if page:
        await _send_page(interaction, page)


async def _send_page(interaction: discord.Interaction, content: str) -> None:
    await interaction.followup.send(
        content, ephemeral=True, allowed_mentions=_NO_MENTIONS, suppress_embeds=True
    )
