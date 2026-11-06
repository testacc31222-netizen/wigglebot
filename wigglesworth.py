"""A mention-free honorary title command for explicitly whitelisted members."""
import random
import discord
from discord.ext import commands
import ui
from quest_rules import explicitly_whitelisted

TITLES = (
    'Keeper of the Wobbly Crown',
    'Royal Biscuit Inspector',
    'Grand Marshal of Tiny Adventures',
    'Guardian of the Emergency Snacks',
    'Distinguished Professor of Wiggles',
    'Knight of the Slightly Crooked Table',
)


def setup(bm):
    if bm.bot.get_command('wigglesworth'):
        return

    @bm.bot.command(name='wigglesworth')
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.member)
    async def wigglesworth(ctx):
        if not explicitly_whitelisted(ctx.author, bm.get_config(ctx.guild.id)):
            return await ctx.send(embed=ui.err('This command is for whitelisted members and roles only.'),
                                  allowed_mentions=discord.AllowedMentions.none())
        name = discord.utils.escape_markdown(discord.utils.escape_mentions(ctx.author.display_name))
        title = random.choice(TITLES)
        await ctx.send(embed=ui.embed('The Wigglesworth Decree',
                       f'By the authority of the Wobbly Crown, **{name}** is hereby named:\n\n'
                       f'**{title}**\n\nA fun honorary title for this moment. No roles or rewards are changed.',
                       'brand'), allowed_mentions=discord.AllowedMentions.none())

    @wigglesworth.error
    async def wigglesworth_error(ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            text = f'The crown is resting. Try again in {error.retry_after:.0f} seconds.'
        elif isinstance(error, commands.NoPrivateMessage):
            text = 'Use this command in the server with a whitelisted account.'
        else:
            raise error
        await ctx.send(embed=ui.info(text), allowed_mentions=discord.AllowedMentions.none())
