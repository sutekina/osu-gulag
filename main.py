#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-

# if you're interested in development, my test server is usually
# up at https://c.cmyui.xyz. just use the same `-devserver cmyui.xyz`
# connection method you would with any other modern server and you
# should have no problems connecting. registration is done in-game
# with osu!'s built-in registration. the api can also be tested here,
# e.g https://osu.cmyui.xyz/api/get_player_scores?id=3&scope=best

__all__ = ()

if __name__ != '__main__':
    raise RuntimeError('gulag should only be run directly!')

import os
import sys

# set cwd to /gulag
os.chdir(os.path.dirname(os.path.realpath(__file__)))

try:
    from objects import glob
except ModuleNotFoundError as exc:
    if exc.msg == "No module named 'config'":
        import shutil
        shutil.copy('ext/config.sample.py', 'config.py')
        sys.exit('\x1b[0;92mA config file has been generated, '
                 'please configure it to your needs.\x1b[0m')
    else:
        raise

from pathlib import Path

import aiohttp
import cmyui
import datadog
import orjson # go zoom
import geoip2.database
import subprocess
from cmyui import Ansi
from cmyui import log

import bg_loops
import utils.misc
from constants.privileges import Privileges
from objects.achievement import Achievement
from objects.collections import Players
from objects.collections import Matches
from objects.collections import Channels
from objects.collections import Clans
from objects.collections import MapPools
from objects.player import Player
from utils.updater import Updater

utils.misc.install_excepthook()

# current version of gulag
# NOTE: this is used internally for the updater, it may be
# worth reading through it's code before playing with it.
glob.version = cmyui.Version(3, 3, 0)

OPPAI_PATH = Path.cwd() / 'oppai-ng'
GEOLOC_DB_FILE = Path.cwd() / 'ext/GeoLite2-City.mmdb'

async def fetch_bot_name() -> str:
    """Fetch the bot's name from the database, if available."""
    res = await glob.db.fetch(
        'SELECT name FROM users '
        'WHERE id = 1', _dict=False
    )

    if not res:
        log("Couldn't find bot account in the database, "
            "defaulting to BanchoBot for their name.", Ansi.LYELLOW)
        return 'BanchoBot'

    return res[0]

async def setup_collections() -> None:
    """Setup & cache many global collections."""
    # dynamic (active) sets, only in ram
    glob.players = Players()
    glob.matches = Matches()

    # static (inactive) sets, in ram & sql
    glob.channels = await Channels.prepare()
    glob.clans = await Clans.prepare()
    glob.pools = await MapPools.prepare()

    # create bot & add it to online players
    glob.bot = Player(
        id=1, name=await fetch_bot_name(), priv=Privileges.Normal,
        login_time=float(0x7fffffff), bot_client=True
    ) # never auto-dc the bot ^
    glob.players.append(glob.bot)

    # global achievements (sorted by vn gamemodes)
    glob.achievements = {0: [], 1: [], 2: [], 3: []}
    async for row in glob.db.iterall('SELECT * FROM achievements'):
        # NOTE: achievement conditions are stored as
        # stringified python expressions in the database
        # to allow for easy custom achievements.
        condition = eval(f'lambda score: {row.pop("cond")}')
        achievement = Achievement(**row, cond=condition)

        # NOTE: achievements are grouped by modes internally.
        glob.achievements[row['mode']].append(achievement)

    # static api keys
    glob.api_keys = {
        row['api_key']: row['id']
        for row in await glob.db.fetchall(
            'SELECT id, api_key FROM users '
            'WHERE api_key IS NOT NULL'
        )
    }

async def before_serving() -> None:
    """Called before the server begins serving connections."""
    # retrieve a client session to use for http connections.
    glob.http = aiohttp.ClientSession(json_serialize=orjson.dumps) # type: ignore

    # retrieve a pool of connections to use for mysql interaction.
    glob.db = cmyui.AsyncSQLPool()
    await glob.db.connect(glob.config.mysql)

    # run the sql & submodule updater (uses http & db).
    updater = Updater(glob.version)
    await updater.run()
    await updater.log_startup()

    # open a connection to our local geoloc database,
    # if the database file is present.
    if GEOLOC_DB_FILE.exists():
        glob.geoloc_db = geoip2.database.Reader(str(GEOLOC_DB_FILE))
    else:
        glob.geoloc_db = None

    # cache many global collections/objects from sql,
    # such as channels, mappools, clans, bot, etc.
    await setup_collections()

    new_coros = []

    # create a task for each donor expiring in 30d.
    new_coros.extend(await bg_loops.donor_expiry())

    # setup a loop to kick inactive ghosted players.
    new_coros.append(bg_loops.disconnect_ghosts())

    # if the surveillance webhook has a value, run
    # automatic (still very primitive) detections on
    # replays deemed by the server's configurable values.
    if glob.config.webhooks['surveillance']:
        new_coros.append(bg_loops.replay_detections())

    # reroll the bot's random status every `interval` sec.
    new_coros.append(bg_loops.reroll_bot_status(interval=300))

    for coro in new_coros:
        glob.app.add_pending_task(coro)

async def after_serving() -> None:
    """Called after the server stops serving connections."""
    if hasattr(glob, 'http'):
        await glob.http.close()

    if hasattr(glob, 'db') and glob.db.pool is not None:
        await glob.db.close()

    if hasattr(glob, 'geoloc_db') and glob.geoloc_db is not None:
        glob.geoloc_db.close()

    if hasattr(glob, 'datadog') and glob.datadog is not None:
        glob.datadog.stop() # stop thread
        glob.datadog.flush() # flush any leftover

def detect_mysqld_running() -> bool:
    """Detect whether theres a mysql server running locally."""
    for path in (
        '/var/run/mysqld/mysqld.pid',
        '/var/run/mariadb/mariadb.pid'
    ):
        if os.path.exists(path):
            # path found
            return True
    else:
        # not found, try pgrep
        return os.system('pgrep mysqld') == 0

def ensure_platform() -> None:
    """Ensure we're running on an appropriate platform for gulag."""
    if sys.platform != 'linux':
        log('gulag currently only supports linux', Ansi.LRED)
        if sys.platform == 'win32':
            log("you could also try wsl(2), i'd recommend ubuntu 18.04 "
                "(i use it to test gulag)", Ansi.LBLUE)
        sys.exit()

    if sys.version_info < (3, 9):
        sys.exit('gulag uses many modern python features, '
                 'and the minimum python version is 3.9.')

def ensure_services() -> None:
    """Ensure all required services are running in the background."""
    # make sure nginx & mysqld are running.
    if (
        glob.config.mysql['host'] in ('localhost', '127.0.0.1') and
        not detect_mysqld_running()
    ):
        sys.exit('Please start your mysqld server.')

    if not os.path.exists('/var/run/nginx.pid'):
        sys.exit('Please start your nginx server.')

def main() -> None:
    """Attempt to start up gulag."""
    # make sure we're running on an appropriate
    # platform with all required software.
    ensure_platform()

    # make sure all required services
    # are being run in the background.
    ensure_services()

    # warn the user if gulag is running on root.
    if os.geteuid() == 0:
        log('It is not recommended to run gulag as root, '
            'especially in production..', Ansi.LYELLOW)

        if glob.config.advanced:
            log('The risk is even greater with features '
                'such as config.advanced enabled.', Ansi.LRED)

    # create /.data and its subdirectories.
    data_path = Path.cwd() / '.data'
    data_path.mkdir(exist_ok=True)

    for sub_dir in ('avatars', 'logs', 'osu', 'osr', 'ss'):
        subdir = data_path / sub_dir
        subdir.mkdir(exist_ok=True)

    achievements_path = data_path / 'assets/medals/client'
    if not achievements_path.exists():
        # create directory & download achievement images
        achievements_path.mkdir(parents=True)
        utils.misc.download_achievement_images(achievements_path)

    # make sure oppai-ng binary is built and ready.
    if not OPPAI_PATH.exists():
        log('No oppai-ng submodule found, attempting to clone.', Ansi.LMAGENTA)
        p = subprocess.Popen(args=['git', 'submodule', 'init'],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if p.wait() == 1:
            sys.exit('Failed to initialize git submodules.')

        p = subprocess.Popen(args=['git', 'submodule', 'update'],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if p.wait() == 1:
            sys.exit('Failed to update git submodules.')

    if not (OPPAI_PATH / 'oppai').exists():
        log('No oppai-ng binary found, attempting to build.', Ansi.LMAGENTA)
        p = subprocess.Popen(args=['./build'], cwd='oppai-ng',
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if p.wait() == 1:
            sys.exit('Failed to build oppai-ng automatically.')

    # create a server object, which serves as a map of domains.
    app = glob.app = cmyui.Server(
        name=f'gulag v{glob.version}',
        gzip=4, debug=glob.config.debug
    )

    # add our endpoint's domains to the server;
    # each may potentially hold many individual endpoints.
    from domains.cho import domain as cho_domain # c[e4-6]?.ppy.sh
    from domains.osu import domain as osu_domain # osu.ppy.sh
    from domains.ava import domain as ava_domain # a.ppy.sh
    from domains.map import domain as map_domain # b.ppy.sh
    app.add_domains({cho_domain, osu_domain,
                     ava_domain, map_domain})

    # enqueue tasks to run once the server
    # begins, and stops serving connections.
    # these make sure we set everything up
    # and take it down nice and graceful.
    app.before_serving = before_serving
    app.after_serving = after_serving

    # support for https://datadoghq.com
    if all(glob.config.datadog.values()):
        datadog.initialize(**glob.config.datadog)
        glob.datadog = datadog.ThreadStats()
        glob.datadog.start(flush_in_thread=True,
                           flush_interval=15)

        # wipe any previous stats from the page.
        glob.datadog.gauge('gulag.online_players', 0)
    else:
        glob.datadog = None

    # start up the server; this starts an event loop internally,
    # using uvloop if it's installed. it uses SIGUSR1 for restarts.
    # NOTE: eventually the event loop creation will likely be
    # moved into the gulag codebase for increased flexibility.
    app.run(glob.config.server_addr, handle_restart=True)

main()
