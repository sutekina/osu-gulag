#!/usr/bin/python3.9
# -*- coding: utf-8 -*-

# if you're interested in development, my test server is
# usually up at 51.161.34.235. just switch the ip of any
# switcher to the one above, toggle it off and on again, and
# you should be connected. registration is done ingame with
# osu!'s built-in registration.
# certificate: https://akatsuki.pw/static/ca.crt

import asyncio
import os
from pathlib import Path

import aiohttp
import cmyui
import datadog
import orjson # go zoom
from cmyui import Ansi
from cmyui import log

import bg_loops
from constants.privileges import Privileges
from objects import glob
from objects.achievement import Achievement
from objects.collections import *
from objects.channel import Channel
from objects.clan import Clan
from objects.match import MapPool
from objects.player import Player
from utils.updater import Updater

__all__ = ()

# current version of gulag
# NOTE: this is used internally for the updater, it may be
# worth reading through it's code before playing with it.
glob.version = cmyui.Version(3, 2, 2)

async def setup_collections() -> None:
    """Setup & cache many global collections (mostly from sql)."""

    res = await glob.db.fetch('SELECT name FROM users WHERE id = 1')

    # global players list
    glob.players = PlayerList()

    glob.bot = Player(
        id = 1, name = res['name'], priv = Privileges.Normal,
        login_time = float(0x7fffffff) # never auto-dc
    )
    glob.players.append(glob.bot)

    # global channels list
    glob.channels = ChannelList()
    async for res in glob.db.iterall('SELECT * FROM channels'):
        chan = Channel(
            name = res['name'],
            topic = res['topic'],
            read_priv = Privileges(res['read_priv']),
            write_priv = Privileges(res['write_priv']),
            auto_join = res['auto_join'] == 1
        )

        glob.channels.append(chan)

    # global matches list
    glob.matches = MatchList()

    # global clans list
    glob.clans = ClanList()
    async for res in glob.db.iterall('SELECT * FROM clans'):
        clan = Clan(**res)

        await clan.members_from_sql()
        glob.clans.append(clan)

    # global mappools list
    glob.pools = MapPoolList()
    async for res in glob.db.iterall('SELECT * FROM tourney_pools'):
        pool = MapPool(
            id = res['id'],
            name = res['name'],
            created_at = res['created_at'],
            created_by = await glob.players.get_ensure(id=res['created_by'])
        )

        await pool.maps_from_sql()
        glob.pools.append(pool)

    # global achievements (sorted by vn gamemodes)
    glob.achievements = {0: [], 1: [], 2: [], 3: []}
    async for res in glob.db.iterall('SELECT * FROM achievements'):
        # NOTE: achievement conditions are stored as
        # stringified python expressions in the database
        # to allow for easy custom achievements.
        condition = eval(f'lambda score: {res.pop("cond")}')
        achievement = Achievement(**res, cond=condition)

        # NOTE: achievements are grouped by modes internally.
        glob.achievements[res['mode']].append(achievement)

async def before_serving() -> None:
    """Called before the server begins serving connections."""
    # retrieve a client session to use for http connections.
    glob.http = aiohttp.ClientSession(json_serialize=orjson.dumps)

    # retrieve a pool of connections to use for mysql interaction.
    glob.db = cmyui.AsyncSQLPool()
    await glob.db.connect(glob.config.mysql)

    # run the sql & submodule updater (uses http & db).
    updater = Updater(glob.version)
    await updater.run()
    await updater.log_startup()

    # cache many global collections/objects from sql,
    # such as channels, mappools, clans, bot, etc.
    await setup_collections()

    # setup tasks for upcoming donor expiry dates.
    await bg_loops.donor_expiry()

    # setup a loop to kick inactive ghosted players.
    loop = asyncio.get_running_loop()
    loop.create_task(bg_loops.disconnect_ghosts())

    # if the surveillance webhook has a value, run
    # automatic (still very primitive) detections on
    # replays deemed by the server's configurable values.
    if glob.config.webhooks['surveillance']:
        loop.create_task(bg_loops.replay_detections())

    # reroll the bot's random status every `interval` sec.
    loop.create_task(bg_loops.reroll_bot_status(interval=300))

if __name__ == '__main__':
    # set cwd to /gulag.
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    # create /.data and its subdirectories.
    data_path = Path.cwd() / '.data'
    data_path.mkdir(exist_ok=True)

    for sub_dir in ('avatars', 'logs', 'osu', 'osr', 'ss'):
        subdir = data_path / sub_dir
        subdir.mkdir(exist_ok=True)

    # make sure oppai-ng is built and ready.
    if not (Path.cwd() / 'oppai-ng/oppai').exists():
        glob.oppai_built = False
        log('No oppai-ng compiled binary found. PP for all '
            'scores will be set to 0; instructions can be '
            'found in the README file.', Ansi.LRED)    
    else:
        glob.oppai_built = True

    # create a server object, which serves as a map of domains.
    app = cmyui.Server(name=f'gulag v{glob.version}',
                       gzip=4, verbose=glob.config.debug)

    # add our endpoint's domains to the server;
    # each may potentially hold many individual endpoints.
    from domains.cho import domain as cho_domain # c[e4-6]?.ppy.sh
    from domains.osu import domain as osu_domain # osu.ppy.sh
    from domains.ava import domain as ava_domain # a.ppy.sh
    app.add_domains({cho_domain, osu_domain, ava_domain})

    # enqueue a task to run once the
    # server begins serving connections.
    app.before_serving = before_serving

    # support for https://datadoghq.com
    if all(glob.config.datadog.values()):
        datadog.initialize(**glob.config.datadog)

        # NOTE: this will start datadog's
        #       client in another thread.
        glob.datadog = datadog.ThreadStats()
        glob.datadog.start(flush_interval=15)

        # wipe any previous stats from the page.
        glob.datadog.gauge('gulag.online_players', 0)
    else:
        glob.datadog = None

    # start up the server; this starts
    # an event loop internally, using
    # uvloop if it's installed.
    app.run(glob.config.server_addr)