"""Interactive directory of player commands installed in the current guild."""

import discord
from discord import app_commands

from .localization import message


_SECTIONS = {
    "citizen": {"start", "citizen", "profile", "help"},
    "wallet": {"wallet"},
    "jobs": {"jobs", "work"},
    "market": {"market", "company", "assets"},
    "government": {"government", "election", "treasury", "contracts"},
    "court": {"court", "prosecutor", "law", "decree"},
}
_PAGE_SIZE = 8


class CommandGuide(discord.ui.View):
    def __init__(self, commands, command_ids, locale, office_commands):
        super().__init__(timeout=600)
        self.locale = locale
        self.sections = {}
        for section, roots in {**_SECTIONS, "staff": set()}.items():
            rows = []
            for command in commands:
                words = command.qualified_name.split()
                restricted = words[0] == "admin" or len(words) > 2 and words[1] in {"staff", "commission"}
                if restricted:
                    if section != "staff" or " ".join(words[:2]) not in office_commands:
                        continue
                elif words[0] not in roots:
                    continue
                command_id = command_ids.get(words[0])
                name = command.qualified_name
                link = f"</{name}:{command_id}>" if command_id else f"`/{name}`"
                description = command.description
                localized = command._locale_description
                if localized and "key" in localized.extras:
                    description = message(locale, localized.extras["key"])
                rows.append(f"{link} — {description}")
            if rows:
                self.sections[section] = rows
        self.section = next(iter(self.sections))
        self.page = 0
        self.choose.placeholder = message(locale, "guide.choose")
        self.choose.options = [
            discord.SelectOption(label=message(locale, f"guide.{key}"), value=key)
            for key in self.sections
        ]
        self.previous.label = message(locale, "guide.previous")
        self.forward.label = message(locale, "guide.next")

    def render(self):
        rows = self.sections[self.section]
        start = self.page * _PAGE_SIZE
        pages = (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE
        self.previous.disabled = self.page == 0
        self.forward.disabled = self.page + 1 >= pages
        for option in self.choose.options:
            option.default = option.value == self.section
        embed = discord.Embed(
            title=message(self.locale, "guide.title"),
            description=message(self.locale, "guide.intro"),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name=message(self.locale, f"guide.{self.section}"),
                        value="\n".join(rows[start:start + 4]), inline=False)
        rest = rows[start + 4:start + _PAGE_SIZE]
        if rest:
            embed.add_field(name="\u200b", value="\n".join(rest), inline=False)
        embed.set_footer(text=f"{self.page + 1} / {pages}")
        return embed

    @discord.ui.select()
    async def choose(self, interaction, select):
        self.section = select.values[0]
        self.page = 0
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def forward(self, interaction, button):
        last = (len(self.sections[self.section]) - 1) // _PAGE_SIZE
        self.page = min(last, self.page + 1)
        await interaction.response.edit_message(embed=self.render(), view=self)


async def show_guide(interaction, tree, guild_scope, command_ids, staff):
    if interaction.guild_id != guild_scope.id:
        await interaction.response.send_message(message(interaction.locale, "access.wrong_guild"), ephemeral=True)
        return
    commands = [cmd for cmd in tree.walk_commands(guild=guild_scope) if isinstance(cmd, app_commands.Command)]
    office_commands = {prefix for prefix, members in staff.items() if interaction.user.id in members}
    if interaction.permissions.administrator:
        office_commands.update(("admin setup", "admin admission"))
    view = CommandGuide(commands, command_ids, interaction.locale, office_commands)
    await interaction.response.send_message(embed=view.render(), view=view, ephemeral=True)
