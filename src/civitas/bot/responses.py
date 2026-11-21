"""Send private Discord replies without parsing mentions or embedding links."""

import discord

_NO_MENTIONS = discord.AllowedMentions.none()
_MESSAGE_LIMIT = 2000


def proportion_bar(value: int, total: int) -> str:
    """Render a ten-cell scale and its percentage using integer arithmetic."""
    if total <= 0:
        return "—"
    percent = value * 100 // total
    filled = min(10, (20 * value + total) // (2 * total))
    return f"{'▰' * filled}{'▱' * (10 - filled)}  {percent}%"


async def send_private_lines(interaction: discord.Interaction, *parts: str) -> None:
    await send_lines(interaction, *parts, ephemeral=True)


async def send_lines(interaction: discord.Interaction, *parts: str, ephemeral: bool) -> None:
    page = ""
    for part in parts:
        candidate = f"{page}\n{part}" if page else part
        if page and len(candidate.encode("utf-16-le")) // 2 > _MESSAGE_LIMIT:
            await _send_page(interaction, page, ephemeral=ephemeral)
            page = part
        else:
            page = candidate
    if page:
        await _send_page(interaction, page, ephemeral=ephemeral)


async def _send_page(interaction: discord.Interaction, content: str, *, ephemeral: bool) -> None:
    await interaction.followup.send(
        content, ephemeral=ephemeral, allowed_mentions=_NO_MENTIONS, suppress_embeds=True
    )
