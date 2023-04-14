from datetime import datetime, time, timedelta, timezone
from glowbot.db import HLL_Player
import discord
from discord.commands import Option
from discord.commands import SlashCommandGroup
from discord.ext import commands, tasks
from glowbot.config import global_config
import logging

SEEDING_INCREMENT_TIMER = 3 # Minutes - how often the RCON is queried for seeding checks

class BotTasks(commands.Cog):
    """
    Cog to handle bot tasks/scheduling.
    """

    def __init__(self, bot):
        self.bot = bot
        self.client = bot.client
        self.logger = logging.getLogger(__name__)

        # Initialize RCON connections
        self.client.connect()

        # Start tasks during init
        self.update_seeders.start()

    @tasks.loop(minutes=SEEDING_INCREMENT_TIMER)
    async def update_seeders(self):
        """
        Check if a server is in seeding status and record seeding statistics.
        If RCON reports `seeding_threshold` is not met, server qualifies as "seeding".
        Accumulate total "seeding" time for users including "unspent" seeding time to be used
        for rewards to those who seed.
        """
        # Ensure that we are during active seeding hours, if set.
        try:
            seeding_start_time_str = global_config['hell_let_loose']['seeding_start_time_utc']
            seeding_end_time_str = global_config['hell_let_loose']['seeding_end_time_utc']

            seeding_start_time = time.fromisoformat(seeding_start_time_str)
            seeding_end_time = time.fromisoformat(seeding_end_time_str)

            time_now = datetime.now(timezone.utc).time()

            # https://stackoverflow.com/questions/20518122/python-working-out-if-time-now-is-between-two-times
            def is_now(start, end, now):
                if start <= end:
                    return start <= now <= end
                else:
                    return start <= now or now < end

            if not is_now(seeding_start_time, seeding_end_time, time_now):
                self.logger.debug(f'Not within seeding time range of \"{seeding_start_time_str} - {seeding_end_time_str}\" UTC')
                status_string = "Outside seeding window"
                # presence updates rate limited to 5 updates / 20s:
                if self.bot.ws is not None:
                    # websocket may not be created first time this is run. it is created during bot.run().
                    # i don't think update_seeders.start() should be run from __init__()...
                    # but from where? discord.on_ready() seems promising, but documenation
                    # claims it could be called more than once.
                    await self.bot.change_presence(status=discord.Status.idle, activity=discord.Game(status_string))
                return

        except ValueError as e:
            # If we excepted here, then the string is incorrect in fromisoformat (or something worse!)
            self.logger.error(f'Can\'t set seeding hours: {e}')
            pass
        except TypeError as e:
            # If we excepted here, then seeding times are undefined, carry on
            pass

        result = await self.client.get_player_list()

        # Run once per RCON:
        for rcon_server_url in result.keys():
            player_list = result[rcon_server_url]

            # Check if player count is below seeding threshold
            if len(player_list) < global_config['hell_let_loose']['seeding_threshold']:
                self.logger.debug(f'Server \"{rcon_server_url}\" qualifies for seeding status at this time.')

                # Iterate through current players and accumulate their seeding time
                for player in player_list:
                    seeder_query = await HLL_Player.filter(steam_id_64__contains=player['steam_id_64'])
                    player_name = player['name']
                    steam_id_64 = player['steam_id_64']
                    if not seeder_query:
                        # New seeder, make a record
                        self.logger.debug(f'Generating new seeder record for \"{player_name}/{steam_id_64}\"')
                        s = HLL_Player(
                                steam_id_64=steam_id_64,
                                player_name=player_name,
                                discord_id=None,
                                seeding_time_balance=timedelta(minutes=0),
                                total_seeding_time=timedelta(minutes=0),
                                last_seed_check=datetime.now(),
                            )
                        await s.save()
                    elif len(seeder_query) != 1:
                        self.logger.error(f'Multiple steam64id\'s found for \"{steam_id_64}\"!')
                    else:
                        # Account for seeding time for player
                        seeder = seeder_query[0]
                        additional_time = timedelta(minutes=SEEDING_INCREMENT_TIMER)
                        old_seed_balance = seeder.seeding_time_balance
                        seeder.seeding_time_balance += additional_time
                        seeder.total_seeding_time += additional_time
                        seeder.last_seed_check = datetime.now()

                        try:
                            await seeder.save()
                            self.logger.debug(f'Successfully updated seeding record for \"{seeder.player_name}\"')
                        except Exception as e:
                            self.logger.error(f'Failed updating record \"{seeder.player_name}\" during seeding: {e}')

                        if global_config['hell_let_loose']['allow_seeder_reward_message'] is True:
                            # Check if user has gained an hour of seeding awards.
                            m, s = divmod(seeder.seeding_time_balance.seconds, 60)
                            new_hourly, _ = divmod(m, 60)

                            m, s = divmod(old_seed_balance.seconds, 60)
                            old_hourly, _ = divmod(m, 60)

                            if new_hourly > old_hourly:
                                self.logger.debug(f'Player \"{seeder.player_name}/{seeder.steam_id_64}\" has 1 hour seeding time')
                                msg_result = await self.client.send_player_message(
                                    rcon_server_url,
                                    seeder.steam_id_64,
                                    global_config['hell_let_loose']['seeder_reward_message'],
                                )
                                if not msg_result:
                                    self.logger.error(f'Failed to send seeder reward message to player \"{seeder.steam_id_64}\"')

                self.logger.debug(f'Seeder status updated for server \"{rcon_server_url}\"')
                status_string = f"Seeding - {len(player_list)}<{global_config['hell_let_loose']['seeding_threshold']}"
                # presence updates rate limited to 5 updates / 20s:
                if self.bot.ws is not None:
                    await self.bot.change_presence(status=discord.Status.online, activity=discord.Game(status_string))
            else:
                self.logger.debug("Server %s does not qualify as seeding status at this time (player_count = %s, must be > %s).  Skipping." % (
                        rcon_server_url,
                        len(player_list),
                        global_config['hell_let_loose']['seeding_threshold'],
                    )
                )
                # status_string = f"Seeding done - {len(player_list)}/100"
                status_string = f"Seeding done"
                # presence updates rate limited to 5 updates / 20s:
                if self.bot.ws is not None:
                    await self.bot.change_presence(status=discord.Status.idle, activity=discord.Game(status_string))

    def cog_unload(self):
        pass

def setup(bot):
    bot.add_cog(BotTasks(bot))

def teardown(bot):
    pass
