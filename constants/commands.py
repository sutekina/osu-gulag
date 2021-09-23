import asyncio
import copy
import importlib
import math
import os
import pprint
import random
import secrets
import signal
import struct
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from time import perf_counter_ns as clock_ns
from typing import Callable
from typing import Coroutine
from typing import NamedTuple
from typing import Optional
from typing import Sequence
from typing import TYPE_CHECKING
from typing import Union

import aiomysql
import cmyui.utils
import psutil
from cmyui.osu.oppai_ng import OppaiWrapper
from peace_performance_python.objects import Beatmap as PeaceMap
from peace_performance_python.objects import Calculator as PeaceCalculator

import misc.utils
import packets
from constants import regexes
from constants.gamemodes import GameMode
from constants.mods import Mods
from constants.mods import SPEED_CHANGING_MODS
from constants.privileges import Privileges
from misc.utils import seconds_readable
from objects import glob
from objects.beatmap import Beatmap
from objects.beatmap import ensure_local_osu_file
from objects.beatmap import RankedStatus
from objects.clan import Clan
from objects.clan import ClanPrivileges
from objects.match import MapPool
from objects.match import Match
from objects.match import MatchTeams
from objects.match import MatchTeamTypes
from objects.match import MatchWinConditions
from objects.match import SlotStatus
from objects.player import Player
from objects.score import SubmissionStatus

if TYPE_CHECKING:
    from objects.channel import Channel

BEATMAPS_PATH = Path.cwd() / '.data/osu'

Messageable = Union['Channel', Player]
CommandResponse = dict[str, Union[str, bool]]

@dataclass
class Context:
    player: Player
    trigger: str
    args: Sequence[str]

    recipient: Optional[Messageable] = None
    match: Optional[Match] = None

Callback = Callable[[Context], Coroutine[None, None, Optional[str]]]

class Command(NamedTuple):
    triggers: list[str]
    callback: Callback
    priv: Privileges
    hidden: bool
    doc: Optional[str]

class CommandSet:
    __slots__ = ('trigger', 'doc', 'commands')

    def __init__(self, trigger: str, doc: str) -> None:
        self.trigger = trigger
        self.doc = doc

        self.commands: list[Command] = []

    def add(self, priv: Privileges, aliases: list[str] = [],
            hidden: bool = False) -> Callable[[Callback], Callback]:
        def wrapper(f: Callback) -> Callback:
            self.commands.append(Command(
                # NOTE: this method assumes that functions without any
                # triggers will be named like '{self.trigger}_{trigger}'.
                triggers = (
                    [f.__name__.removeprefix(f'{self.trigger}_').strip()] +
                    aliases
                ),
                callback = f,
                priv = priv,
                hidden = hidden,
                doc = f.__doc__
            ))

            return f
        return wrapper

# TODO: refactor help commands into some base ver
#       since they're all the same anyways lol.

# not sure if this should be in glob or not,
# trying to think of some use cases lol..
regular_commands = []
command_sets = [
    mp_commands := CommandSet('mp', 'Multiplayer commands.'),
    pool_commands := CommandSet('pool', 'Mappool commands.'),
    clan_commands := CommandSet('clan', 'Clan commands.')
]

def command(priv: Privileges, aliases: list[str] = [],
            hidden: bool = False) -> Callable[[Callback], Callback]:
    def wrapper(f: Callback) -> Callback:
        regular_commands.append(Command(
            callback = f,
            priv = priv,
            hidden = hidden,
            triggers = [f.__name__.strip('_')] + aliases,
            doc = f.__doc__
        ))

        return f
    return wrapper

""" User commands
# The commands below are not considered dangerous,
# and are granted to any unbanned players.
"""

@command(Privileges.Normal, aliases=['', 'h'], hidden=True)
async def _help(ctx: Context) -> Optional[str]:
    """Show all documented commands the player can access."""
    prefix = glob.config.command_prefix
    l = ['Individual commands',
         '-----------']

    for cmd in regular_commands:
        if not cmd.doc or ctx.player.priv & cmd.priv != cmd.priv:
            # no doc, or insufficient permissions.
            continue

        l.append(f'{prefix}{cmd.triggers[0]}: {cmd.doc}')

    l.append('') # newline
    l.extend(['Command sets',
              '-----------'])

    for cmd_set in command_sets:
        l.append(f'{prefix}{cmd_set.trigger}: {cmd_set.doc}')

    return '\n'.join(l)

@command(Privileges.Normal)
async def roll(ctx: Context) -> Optional[str]:
    """Roll an n-sided die where n is the number you write (100 default)."""
    if ctx.args and ctx.args[0].isdecimal():
        max_roll = min(int(ctx.args[0]), 0x7fff)
    else:
        max_roll = 100

    if max_roll == 0:
        return "Roll what?"

    points = random.randrange(0, max_roll)
    return f'{ctx.player.name} rolls {points} points!'

@command(Privileges.Normal, hidden=True)
async def block(ctx: Context) -> Optional[str]:
    """Block another user from communicating with you."""
    target = await glob.players.get_ensure(name=' '.join(ctx.args))

    if not target:
        return 'User not found.'

    if (
        target is glob.bot or
        target is ctx.player
    ):
        return 'What?'

    if target.id in ctx.player.blocks:
        return f'{target.name} already blocked!'

    if target.id in ctx.player.friends:
        ctx.player.friends.remove(target.id)

    await ctx.player.add_block(target)
    return f'Added {target.name} to blocked users.'

@command(Privileges.Normal, hidden=True)
async def unblock(ctx: Context) -> Optional[str]:
    """Unblock another user from communicating with you."""
    target = await glob.players.get_ensure(name=' '.join(ctx.args))

    if not target:
        return 'User not found.'

    if (
        target is glob.bot or
        target is ctx.player
    ):
        return 'What?'

    if target.id not in ctx.player.blocks:
        return f'{target.name} not blocked!'

    await ctx.player.remove_block(target)
    return f'Removed {target.name} from blocked users.'

@command(Privileges.Normal)
async def reconnect(ctx: Context) -> Optional[str]:
    """Disconnect and reconnect to the server."""
    ctx.player.logout()

@command(Privileges.Normal)
async def changename(ctx: Context) -> Optional[str]:
    """Change your username."""
    name = ' '.join(ctx.args).strip()

    if not regexes.username.match(name):
        return 'Must be 2-15 characters in length.'

    if '_' in name and ' ' in name:
        return 'May contain "_" and " ", but not both.'

    if name in glob.config.disallowed_names:
        return 'Disallowed username; pick another.'

    if await glob.db.fetch('SELECT 1 FROM users WHERE name = %s', [name]):
        return 'Username already taken by another player.'

    # all checks passed, update their name
    safe_name = name.lower().replace(' ', '_')

    await glob.db.execute(
        'UPDATE users '
        'SET name = %s, safe_name = %s '
        'WHERE id = %s',
        [name, safe_name, ctx.player.id]
    )

    ctx.player.enqueue(
        packets.notification(f'Your username has been changed to {name}!')
    )
    ctx.player.logout()

@command(Privileges.Normal, aliases=['bloodcat', 'beatconnect', 'chimu', 'q'])
async def maplink(ctx: Context) -> Optional[str]:
    """Return a download link to the user's current map (situation dependant)."""
    bmap = None

    # priority: multiplayer -> spectator -> last np
    match = ctx.player.match
    spectating = ctx.player.spectating

    if match and match.map_id:
        bmap = await Beatmap.from_md5(match.map_md5)
    elif spectating and spectating.status.map_id:
        bmap = await Beatmap.from_md5(spectating.status.map_md5)
    elif time.time() < ctx.player.last_np['timeout']:
        bmap = ctx.player.last_np['bmap']
    else:
        return 'No map found!'

    # gatari.pw & nerina.pw are pretty much the only
    # reliable mirrors i know of? perhaps beatconnect
    return f'[https://osu.gatari.pw/d/{bmap.set_id} {bmap.full}]'

@command(Privileges.Normal, aliases=['last', 'r'])
async def recent(ctx: Context) -> Optional[str]:
    """Show information about your most recent score."""
    if ctx.args:
        if not (target := glob.players.get(name=' '.join(ctx.args))):
            return 'Player not found.'
    else:
        target = ctx.player

    if not (s := target.recent_score):
        return 'No scores found :o (only saves per play session)'

    l = [f'[{s.mode!r}] {s.bmap.embed}', f'{s.acc:.2f}%']

    if s.mods:
        l.insert(1, f'+{s.mods!r}')

    l = [' '.join(l)]

    if s.passed:
        rank = s.rank if s.status == SubmissionStatus.BEST else 'NA'
        l.append(f'PASS {{{s.pp:.2f}pp #{rank}}}')
    else:
        # XXX: prior to v3.2.0, gulag didn't parse total_length from
        # the osu!api, and thus this can do some zerodivision moments.
        # this can probably be removed in the future, or better yet
        # replaced with a better system to fix the maps.
        if s.bmap.total_length != 0:
            completion = s.time_elapsed / (s.bmap.total_length * 1000)
            l.append(f'FAIL {{{completion * 100:.2f}% complete}})')
        else:
            l.append('FAIL')

    return ' | '.join(l)

# TODO: !top (get top #1 score)
# TODO: !compare (compare to previous !last/!top post's map)

@command(Privileges.Normal, aliases=['w'], hidden=True)
async def _with(ctx: Context) -> Optional[str]:
    """Specify custom accuracy & mod combinations with `/np`."""
    if ctx.recipient is not glob.bot:
        return 'This command can only be used in DM with bot.'

    if time.time() >= ctx.player.last_np['timeout']:
        return 'Please /np a map first!'

    bmap: Beatmap = ctx.player.last_np['bmap']

    osu_file_path = BEATMAPS_PATH / f'{bmap.id}.osu'
    if not await ensure_local_osu_file(osu_file_path, bmap.id, bmap.md5):
        return ('Mapfile could not be found; '
                'this incident has been reported.')

    mode_vn = ctx.player.last_np['mode_vn']

    if mode_vn in (0, 1, 2): # osu, taiko, catch
        if not ctx.args or len(ctx.args) > 4:
            return 'Invalid syntax: !with <acc/nmiss/combo/mods ...>'

        # !with 95% 1m 429x hddt
        acc = mods = combo = nmiss = None

        # parse acc, misses, combo and mods from arguments.
        # tried to balance complexity vs correctness here
        for arg in map(str.lower, ctx.args):
            # mandatory suffix, combo & nmiss
            if (
                combo is None and
                arg.endswith('x') and
                arg[:-1].isdecimal()
            ):
                combo = int(arg[:-1])
                if combo > bmap.max_combo:
                    return 'Invalid combo.'
            elif (
                nmiss is None and
                arg.endswith('m') and
                arg[:-1].isdecimal()
            ):
                nmiss = int(arg[:-1])
                # TODO: store nobjects?
                if nmiss > bmap.max_combo:
                    return 'Invalid misscount.'
            else:
                # optional prefix/suffix, mods & accuracy
                arg_stripped = arg.removeprefix('+').removesuffix('%')
                if (
                    mods is None and
                    arg_stripped.isalpha() and
                    len(arg_stripped) % 2 == 0
                ):
                    mods = Mods.from_modstr(arg_stripped)
                    mods = mods.filter_invalid_combos(mode_vn)
                elif (
                    acc is None and
                    arg_stripped.replace('.', '', 1).isdecimal()
                ):
                    acc = float(arg_stripped)
                    if not 0 <= acc <= 100:
                        return 'Invalid accuracy.'
                else:
                    return f'Unknown argument: {arg}'

        msg = []

        if mode_vn == 0:
            with OppaiWrapper('oppai-ng/liboppai.so') as ezpp:
                if mods is not None:
                    ezpp.set_mods(int(mods))
                    msg.append(f'{mods!r}')

                if nmiss is not None:
                    ezpp.set_nmiss(nmiss)
                    msg.append(f'{nmiss}m')

                if combo is not None:
                    ezpp.set_combo(combo)
                    msg.append(f'{combo}x')

                if acc is not None:
                    ezpp.set_accuracy_percent(acc)
                    msg.append(f'{acc:.2f}%')

                ezpp.calculate(osu_file_path)
                pp, sr = ezpp.get_pp(), ezpp.get_sr()

                return f"{' '.join(msg)}: {pp:.2f}pp ({sr:.2f}*)"
        else:
            beatmap = PeaceMap(osu_file_path)
            peace = PeaceCalculator()

            if mods is not None:
                peace.set_mods(int(mods))
                msg.append(f'{mods!r}')

            if nmiss is not None:
                peace.set_miss(nmiss)
                msg.append(f'{nmiss}m')

            if combo is not None:
                peace.set_combo(combo)
                msg.append(f'{combo}x')

            if acc is not None:
                peace.set_acc(acc)
                msg.append(f'{acc:.2f}%')

            if mode_vn:
                peace.set_mode(mode_vn)

            calculated = peace.calculate(beatmap)

            if calculated.pp not in (math.inf, math.nan):
                return f"{' '.join(msg)}: {calculated.pp:.2f}pp ({calculated.stars:.2f}*)"
            else:
                # TODO: report to logserver
                return f"{' '.join(msg)}: 0pp (0*)"
    else: # mania
        if not ctx.args or len(ctx.args) > 2:
            return 'Invalid syntax: !with <score/mods ...>'

        score = 1000
        mods = Mods.NOMOD

        for param in (p.strip('+k') for p in ctx.args):
            if param.isdecimal(): # acc
                if not 0 <= (score := int(param)) <= 1000:
                    return 'Invalid score.'
                if score <= 500:
                    return '<=500k score is always 0pp.'
            elif len(param) % 2 == 0:
                mods = Mods.from_modstr(param)
                mods = mods.filter_invalid_combos(mode_vn)
            else:
                return 'Invalid syntax: !with <score/mods ...>'

        beatmap = PeaceMap(osu_file_path)
        peace = PeaceCalculator()

        if mods != Mods.NOMOD:
            peace.set_mods(int(mods))

        if mode_vn:
            peace.set_mode(mode_vn)

        peace.set_score(score * 1000)

        calc = peace.calculate(beatmap)
        return f'{score}k {mods!r}: {calc.pp:.2f}pp ({calc.stars:.2f}*)'

@command(Privileges.Normal, aliases=['req'])
async def request(ctx: Context) -> Optional[str]:
    """Request a beatmap for nomination."""
    if ctx.args:
        return 'Invalid syntax: !request'

    if time.time() >= ctx.player.last_np['timeout']:
        return 'Please /np a map first!'

    bmap = ctx.player.last_np['bmap']

    if bmap.status != RankedStatus.Pending:
        return 'Only pending maps may be requested for status change.'

    await glob.db.execute(
        'INSERT INTO map_requests '
        '(map_id, player_id, datetime, active) '
        'VALUES (%s, %s, NOW(), 1)',
        [bmap.id, ctx.player.id]
    )

    return 'Request submitted.'

@command(Privileges.Normal)
async def get_apikey(ctx: Context) -> Optional[str]:
    """Generate a new api key & assign it to the player."""
    if ctx.recipient is not glob.bot:
        return f'Command only available in DMs with {glob.bot.name}.'

    # remove old token
    if ctx.player.api_key:
        glob.api_keys.pop(ctx.player.api_key)

    # generate new token
    ctx.player.api_key = str(uuid.uuid4())

    await glob.db.execute(
        'UPDATE users '
        'SET api_key = %s '
        'WHERE id = %s',
        [ctx.player.api_key, ctx.player.id]
    )
    glob.api_keys[ctx.player.api_key] = ctx.player.id

    ctx.player.enqueue(packets.notification('/savelog & click popup for an easy copy.'))
    return f'Your API key is now: {ctx.player.api_key}'

""" Nominator commands
# The commands below allow users to
# manage  the server's state of beatmaps.
"""

@command(Privileges.Nominator, aliases=['reqs'], hidden=True)
async def requests(ctx: Context) -> Optional[str]:
    """Check the nomination request queue."""
    if ctx.args:
        return 'Invalid syntax: !requests'

    res = await glob.db.fetchall(
        'SELECT map_id, player_id, datetime '
        'FROM map_requests WHERE active = 1',
        _dict=False # return rows as tuples
    )

    if not res:
        return 'The queue is clean! (0 map request(s))'

    l = [f'Total requests: {len(res)}']

    for (map_id, player_id, dt) in res:
        # find player & map for each row, and add to output.
        if not (p := await glob.players.get_ensure(id=player_id)):
            l.append(f'Failed to find requesting player ({player_id})?')
            continue

        if not (bmap := await Beatmap.from_bid(map_id)):
            l.append(f'Failed to find requested map ({map_id})?')
            continue

        l.append(f'[{p.embed} @ {dt:%b %d %I:%M%p}] {bmap.embed}.')

    return '\n'.join(l)

_status_str_to_int_map = {
    'unrank': 0,
    'rank': 2,
    'love': 5
}
def status_to_id(s: str) -> int:
    return _status_str_to_int_map[s]

@command(Privileges.Nominator)
async def _map(ctx: Context) -> Optional[str]:
    """Changes the ranked status of the most recently /np'ed map."""
    if (
        len(ctx.args) != 2 or
        ctx.args[0] not in ('rank', 'unrank', 'love') or
        ctx.args[1] not in ('set', 'map')
    ):
        return 'Invalid syntax: !map <rank/unrank/love> <map/set>'

    if time.time() >= ctx.player.last_np['timeout']:
        return 'Please /np a map first!'

    bmap = ctx.player.last_np['bmap']
    new_status = RankedStatus(status_to_id(ctx.args[0]))

    if bmap.status == new_status:
        return f'{bmap.embed} is already {new_status!s}!'

    # update sql & cache based on scope
    # XXX: not sure if getting md5s from sql
    # for updating cache would be faster?
    # surely this will not scale as well..

    async with glob.db.pool.acquire() as conn:
        async with conn.cursor() as db_cursor:
            if ctx.args[1] == 'set':
                # update whole set
                await db_cursor.execute(
                    'UPDATE maps SET status = %s, '
                    'frozen = 1 WHERE set_id = %s',
                    [new_status, bmap.set_id]
                )

                # select all map ids for clearing map requests.
                await db_cursor.execute(
                    'SELECT id FROM maps '
                    'WHERE set_id = %s',
                    [bmap.set_id]
                )
                map_ids = [row[0] async for row in db_cursor]

                for bmap in glob.cache['beatmapset'][bmap.set_id].maps:
                    bmap.status = new_status

            else:
                # update only map
                await db_cursor.execute(
                    'UPDATE maps SET status = %s, '
                    'frozen = 1 WHERE id = %s',
                    [new_status, bmap.id]
                )

                map_ids = [bmap.id]

                if bmap.md5 in glob.cache['beatmap']:
                    glob.cache['beatmap'][bmap.md5].status = new_status

            # deactivate rank requests for all ids
            for map_id in map_ids:
                await db_cursor.execute(
                    'UPDATE map_requests '
                    'SET active = 0 '
                    'WHERE map_id = %s',
                    [map_id]
                )

    return f'{bmap.embed} updated to {new_status!s}.'

""" Mod commands
# The commands below are somewhat dangerous,
# and are generally for managing players.
"""

@command(Privileges.Mod, hidden=True)
async def notes(ctx: Context) -> Optional[str]:
    """Retrieve the logs of a specified player by name."""
    if len(ctx.args) != 2 or not ctx.args[1].isdecimal():
        return 'Invalid syntax: !notes <name> <days_back>'

    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return f'"{ctx.args[0]}" not found.'

    days = int(ctx.args[1])

    if days > 365:
        return 'Please contact a developer to fetch >365 day old information.'
    elif days <= 0:
        return 'Invalid syntax: !notes <name> <days_back>'

    elif days <= 0:
        return 'Invalid syntax: !notes <name> <days_back>'

    res = await glob.db.fetchall(
        'SELECT `msg`, `time` '
        'FROM `logs` WHERE `to` = %s '
        'AND UNIX_TIMESTAMP(`time`) >= UNIX_TIMESTAMP(NOW()) - %s '
        'ORDER BY `time` ASC',
        [t.id, days * 86400]
    )

    if not res:
        return f'No notes found on {t} in the past {days} days.'

    return '\n'.join(['[{time}] {msg}'.format(**row) for row in res])

@command(Privileges.Mod, hidden=True)
async def addnote(ctx: Context) -> Optional[str]:
    """Add a note to a specified player by name."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !addnote <name> <note ...>'

    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return f'"{ctx.args[0]}" not found.'

    log_msg = f'{ctx.player} added note: {" ".join(ctx.args[1:])}'

    await glob.db.execute(
        'INSERT INTO logs '
        '(`from`, `to`, `msg`, `time`) '
        'VALUES (%s, %s, %s, NOW())',
        [ctx.player.id, t.id, log_msg]
    )

    return f'Added note to {t}.'

# some shorthands that can be used as
# reasons in many moderative commands.
SHORTHAND_REASONS = {
    'aa': 'having their appeal accepted',
    'cc': 'using a modified osu! client',
    '3p': 'using 3rd party programs',
    'rx': 'using 3rd party programs (relax)',
    'tw': 'using 3rd party programs (timewarp)',
    'au': 'using 3rd party programs (auto play)'
}

DURATION_MULTIPLIERS = {
    's': 1, 'm': 60, 'h': 3600,
    'd': 86400, 'w': 604800
}

@command(Privileges.Mod, hidden=True)
async def silence(ctx: Context) -> Optional[str]:
    """Silence a specified player with a specified duration & reason."""
    if len(ctx.args) < 3:
        return 'Invalid syntax: !silence <name> <duration> <reason>'

    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return f'"{ctx.args[0]}" not found.'

    if (
        t.priv & Privileges.Staff and
        not ctx.player.priv & Privileges.Dangerous
    ):
        return 'Only developers can manage staff members.'

    if not (r_match := regexes.scaled_duration.match(ctx.args[1])):
        return 'Invalid syntax: !silence <name> <duration> <reason>'

    multiplier = DURATION_MULTIPLIERS[r_match['scale']]

    duration = int(r_match['duration']) * multiplier
    reason = ' '.join(ctx.args[2:])

    if reason in SHORTHAND_REASONS:
        reason = SHORTHAND_REASONS[reason]

    await t.silence(ctx.player, duration, reason)
    return f'{t} was silenced.'

@command(Privileges.Mod, hidden=True)
async def unsilence(ctx: Context) -> Optional[str]:
    """Unsilence a specified player."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !unsilence <name>'

    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return f'"{ctx.args[0]}" not found.'

    if not t.silenced:
        return f'{t} is not silenced.'

    if (
        t.priv & Privileges.Staff and
        not ctx.player.priv & Privileges.Dangerous
    ):
        return 'Only developers can manage staff members.'

    await t.unsilence(ctx.player)
    return f'{t} was unsilenced.'

""" Admin commands
# The commands below are relatively dangerous,
# and are generally for managing players.
"""

@command(Privileges.Admin, aliases=['u'], hidden=True)
async def user(ctx: Context) -> Optional[str]:
    """Return general information about a given user."""
    if not ctx.args:
        # no username specified, use ctx.player
        p = ctx.player
    else:
        # username given, fetch the player
        p = await glob.players.get_ensure(name=' '.join(ctx.args))

        if not p:
            return 'Player not found.'

    priv_readable = '|'.join(reversed([
        priv.name for priv in Privileges
        if p.priv & priv and bin(priv).count('1') == 1
    ]))

    current_time = time.time()
    login_delta = current_time - p.login_time
    last_recv_delta = current_time - p.last_recv_time

    if current_time < p.last_np['timeout']:
        last_np = p.last_np['bmap'].embed
    else:
        last_np = None

    return '\n'.join((
        f'[{"Bot" if p.bot_client else "Player"}] {p.full_name} ({p.id})',
        f'Privileges: {priv_readable}',
        f'Channels: {[p._name for p in p.channels]}',
        f'Logged in: {login_delta:.2f} sec ago',
        f'Last server interaction: {last_recv_delta:.2f} sec ago',
        f'osu! build: {p.osu_ver} | Tourney: {p.tourney_client}',
        f'Silenced: {p.silenced} | Spectating: {p.spectating}',
        f'Last /np: {last_np}',
        f'Recent score: {p.recent_score}',
        f'Match: {p.match}',
        f'Spectators: {p.spectators}'
    ))

@command(Privileges.Admin, hidden=True)
async def restrict(ctx: Context) -> Optional[str]:
    """Restrict a specified player's account, with a reason."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !restrict <name> <reason>'

    # find any user matching (including offline).
    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return f'"{ctx.args[0]}" not found.'

    if (
        t.priv & Privileges.Staff and
        not ctx.player.priv & Privileges.Dangerous
    ):
        return 'Only developers can manage staff members.'

    if t.restricted:
        return f'{t} is already restricted!'

    reason = ' '.join(ctx.args[1:])

    if reason in SHORTHAND_REASONS:
        reason = SHORTHAND_REASONS[reason]

    await t.restrict(admin=ctx.player, reason=reason)

    return f'{t} was restricted.'

@command(Privileges.Admin, hidden=True)
async def unrestrict(ctx: Context) -> Optional[str]:
    """Unrestrict a specified player's account, with a reason."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !unrestrict <name> <reason>'

    # find any user matching (including offline).
    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return f'"{ctx.args[0]}" not found.'

    if (
        t.priv & Privileges.Staff and
        not ctx.player.priv & Privileges.Dangerous
    ):
        return 'Only developers can manage staff members.'

    if not t.restricted:
        return f'{t} is not restricted!'

    reason = ' '.join(ctx.args[1:])

    if reason in SHORTHAND_REASONS:
        reason = SHORTHAND_REASONS[reason]

    await t.unrestrict(ctx.player, reason)

    return f'{t} was unrestricted.'

@command(Privileges.Admin, hidden=True)
async def alert(ctx: Context) -> Optional[str]:
    """Send a notification to all players."""
    if len(ctx.args) < 1:
        return 'Invalid syntax: !alert <msg>'

    notif_txt = ' '.join(ctx.args)

    glob.players.enqueue(packets.notification(notif_txt))
    return 'Alert sent.'

@command(Privileges.Admin, aliases=['alertu'], hidden=True)
async def alertuser(ctx: Context) -> Optional[str]:
    """Send a notification to a specified player by name."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !alertu <name> <msg>'

    if not (t := glob.players.get(name=ctx.args[0])):
        return 'Could not find a user by that name.'

    notif_txt = ' '.join(ctx.args[1:])

    t.enqueue(packets.notification(notif_txt))
    return 'Alert sent.'

# NOTE: this is pretty useless since it doesn't switch anything other
# than the c[e4-6].ppy.sh domains; it exists on bancho as a tournament
# server switch mechanism, perhaps we could leverage this in the future.
@command(Privileges.Admin, hidden=True)
async def switchserv(ctx: Context) -> Optional[str]:
    """Switch your client's internal endpoints to a specified IP address."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !switch <endpoint>'

    new_bancho_ip = ctx.args[0]

    ctx.player.enqueue(packets.switchTournamentServer(new_bancho_ip))
    return 'Have a nice journey..'

@command(Privileges.Admin, aliases=['restart'])
async def shutdown(ctx: Context) -> Optional[str]:
    """Gracefully shutdown the server."""
    if ctx.trigger == 'restart':
        _signal = signal.SIGUSR1
    else:
        _signal = signal.SIGTERM

    if ctx.args: # shutdown after a delay
        if not (r_match := regexes.scaled_duration.match(ctx.args[0])):
            return f'Invalid syntax: !{ctx.trigger} <delay> <msg ...>'

        multiplier = DURATION_MULTIPLIERS[r_match['scale']]

        delay = int(r_match['duration']) * multiplier

        if delay < 15:
            return 'Minimum delay is 15 seconds.'

        if len(ctx.args) > 1:
            # alert all online players of the reboot.
            alert_msg = (f'The server will {ctx.trigger} in {ctx.args[0]}.\n\n'
                         f'Reason: {" ".join(ctx.args[1:])}')

            glob.players.enqueue(packets.notification(alert_msg))

        glob.loop.call_later(delay, os.kill, os.getpid(), _signal)
        return f'Enqueued {ctx.trigger}.'
    else: # shutdown immediately
        os.kill(os.getpid(), _signal)
        return ':D'

""" Developer commands
# The commands below are either dangerous or
# simply not useful for any other roles.
"""

_fake_users = []
@command(Privileges.Dangerous, aliases=['fu'])
async def fakeusers(ctx: Context) -> Optional[str]:
    """Add fake users to the online player list (for testing)."""
    # NOTE: this is mostly just for speedtesting things
    # regarding presences/stats. it's implementation is
    # indeed quite cursed, but rather efficient.
    if (
        len(ctx.args) != 2 or
        ctx.args[0] not in ('add', 'rm') or
        not ctx.args[1].isdecimal()
    ):
        return 'Invalid syntax: !fakeusers <add/rm> <amount>'

    action = ctx.args[0]
    amount = int(ctx.args[1])
    if not 0 < amount <= 100_000:
        return 'Amount must be in range 0-100k.'

    # we start at half way through
    # the i32 space for fake user ids.
    FAKE_ID_START = 0x7fffffff >> 1

    # data to send to clients (all new user info)
    # we'll send all the packets together at end (more efficient)
    data = bytearray()

    if action == 'add':
        const_uinfo = { # non important stuff
            'utc_offset': 0,
            'osu_ver': 'dn',
            'pm_private': False,
            'clan': None,
            'clan_priv': None,
            'priv': Privileges.Normal | Privileges.Verified,
            'silence_end': 0,
            'login_time': 0x7fffffff # never auto-dc
        }

        _stats = packets.userStats(ctx.player)

        if _fake_users:
            current_fakes = max([x.id for x in _fake_users]) - (FAKE_ID_START - 1)
        else:
            current_fakes = 0

        start_id = FAKE_ID_START + current_fakes
        end_id = start_id + amount
        vn_std = GameMode.vn_std

        base_player = Player(id=0, name='', **const_uinfo)
        base_player.stats[vn_std] = copy.copy(ctx.player.stats[vn_std])
        new_fakes = []

        # static part of the presence packet,
        # no need to redo this every iteration.
        static_presence = struct.pack(
            '<BBBffi',
            19, # -5 (EST) + 24
            38, # country (canada)
            0b11111, # all in-game privs
            0.0, 0.0, # lat, lon
            1 # rank #1
        )

        for i in range(start_id, end_id):
            # create new fake player from base
            name = f'fake #{i - (FAKE_ID_START - 1)}'
            fake = copy.copy(base_player)
            fake.id = i
            fake.name = name

            # append userpresence packet
            data += struct.pack(
                '<HxIi',
                83, # packetid
                21 + len(name), # packet len
                i # userid
            )
            data += f'\x0b{chr(len(name))}{name}'.encode()
            data += static_presence
            data += _stats

            new_fakes.append(fake)

        # extend all added fakes to the real list
        _fake_users.extend(new_fakes)
        glob.players.extend(new_fakes)
        del new_fakes

        msg = 'Added.'
    else: # remove
        len_fake_users = len(_fake_users)
        if amount > len_fake_users:
            return f'Too many! only {len_fake_users} remaining.'

        to_remove = _fake_users[len_fake_users - amount:]
        logout_packet_header = b'\x0c\x00\x00\x05\x00\x00\x00'

        for fake in to_remove:
            if not fake.online:
                # already auto-dced
                _fake_users.remove(fake)
                continue

            data += logout_packet_header
            data += fake.id.to_bytes(4, 'little') # 4 bytes pid
            data += b'\x00' # 1 byte 0

            glob.players.remove(fake)
            _fake_users.remove(fake)

        msg = 'Removed.'

    data = bytes(data) # bytearray -> bytes

    # only enqueue data to real users.
    for o in [x for x in glob.players if x.id < FAKE_ID_START]:
        o.enqueue(data)

    return msg

@command(Privileges.Dangerous)
async def stealth(ctx: Context) -> Optional[str]:
    """Toggle the developer's stealth, allowing them to be hidden."""
    # NOTE: this command is a large work in progress and currently
    # half works; eventually it will be moved to the Admin level.
    ctx.player.stealth = not ctx.player.stealth

    return f'Stealth {"enabled" if ctx.player.stealth else "disabled"}.'

@command(Privileges.Dangerous)
async def recalc(ctx: Context) -> Optional[str]:
    """Recalculate pp for a given map, or all maps."""
    # NOTE: at the moment this command isn't very optimal and re-parses
    # the beatmap file each iteration; this will be heavily improved.
    if len(ctx.args) != 1 or ctx.args[0] not in ('map', 'all'):
        return 'Invalid syntax: !recalc <map/all>'

    if ctx.args[0] == 'map':
        # by specific map, use their last /np
        if time.time() >= ctx.player.last_np['timeout']:
            return 'Please /np a map first!'

        bmap: Beatmap = ctx.player.last_np['bmap']

        osu_file_path = BEATMAPS_PATH / f'{bmap.id}.osu'
        if not await ensure_local_osu_file(osu_file_path, bmap.id, bmap.md5):
            return ('Mapfile could not be found; '
                    'this incident has been reported.')

        async with glob.db.pool.acquire() as conn:
            async with (
                conn.cursor(aiomysql.DictCursor) as select_cursor,
                conn.cursor(aiomysql.Cursor) as update_cursor
            ):
                with OppaiWrapper('oppai-ng/liboppai.so') as ezpp:
                    ezpp.set_mode(0) # TODO: other modes
                    for table in ('scores_vn', 'scores_rx', 'scores_ap'):
                        await select_cursor.execute(
                            'SELECT id, acc, mods, max_combo, nmiss '
                            f'FROM {table} '
                            'WHERE map_md5 = %s AND mode = 0', # TODO: ""
                            [bmap.md5]
                        )

                        async for row in select_cursor:
                            ezpp.set_mods(row['mods'])
                            ezpp.set_nmiss(row['nmiss']) # clobbers acc
                            ezpp.set_combo(row['max_combo'])
                            ezpp.set_accuracy_percent(row['acc'])

                            ezpp.calculate(osu_file_path)

                            await update_cursor.execute(
                                f'UPDATE {table} '
                                'SET pp = %s '
                                'WHERE id = %s',
                                [ezpp.get_pp(), row['id']]
                            )

        return 'Map recalculated.'
    else:
        # recalc all plays on the server, on all maps
        staff_chan = glob.channels['#staff'] # log any errs here

        async def recalc_all() -> None:
            staff_chan.send_bot(f'{ctx.player} started a full recalculation.')
            st = time.time()

            async with glob.db.pool.acquire() as conn:
                async with (
                    conn.cursor(aiomysql.Cursor) as bmap_select_cursor,
                    conn.cursor(aiomysql.DictCursor) as score_select_cursor,
                    conn.cursor(aiomysql.Cursor) as update_cursor
                ):
                    await bmap_select_cursor.execute(
                        'SELECT id, md5 '
                        'FROM maps '
                        'WHERE passes > 0'
                    )

                    map_count = bmap_select_cursor.rowcount
                    staff_chan.send_bot(f'Recalculating {map_count} maps.')

                    async for bmap_row in bmap_select_cursor:
                        bmap_id, bmap_md5 = bmap_row

                        osu_file_path = BEATMAPS_PATH / f'{bmap_id}.osu'
                        if not await ensure_local_osu_file(osu_file_path, bmap_id, bmap_md5):
                            staff_chan.send_bot("[Recalc] Couldn't find "
                                                f"{bmap_id} / {bmap_md5}")
                            continue

                        with OppaiWrapper('oppai-ng/liboppai.so') as ezpp:
                            ezpp.set_mode(0) # TODO: other modes
                            for table in ('scores_vn', 'scores_rx', 'scores_ap'):
                                await score_select_cursor.execute(
                                    'SELECT id, acc, mods, max_combo, nmiss '
                                    f'FROM {table} '
                                    'WHERE map_md5 = %s AND mode = 0', # TODO: ""
                                    [bmap_md5]
                                )

                                async for row in score_select_cursor:
                                    ezpp.set_mods(row['mods'])
                                    ezpp.set_nmiss(row['nmiss']) # clobbers acc
                                    ezpp.set_combo(row['max_combo'])
                                    ezpp.set_accuracy_percent(row['acc'])

                                    ezpp.calculate(osu_file_path)

                                    await update_cursor.execute(
                                        f'UPDATE {table} '
                                        'SET pp = %s '
                                        'WHERE id = %s',
                                        [ezpp.get_pp(), row['id']]
                                    )

                        # leave at least 1/100th of
                        # a second for handling conns.
                        await asyncio.sleep(0.01)

            elapsed = misc.utils.seconds_readable(int(time.time() - st))
            staff_chan.send_bot(f'Recalculation complete. | Elapsed: {elapsed}')

        glob.loop.create_task(recalc_all())

        return 'Starting a full recalculation.'

@command(Privileges.Dangerous, hidden=True)
async def debug(ctx: Context) -> Optional[str]:
    """Toggle the console's debug setting."""
    glob.app.debug = not glob.app.debug
    return f"Toggled {'on' if glob.app.debug else 'off'}."

# NOTE: these commands will likely be removed
#       with the addition of a good frontend.
str_priv_dict = {
    'normal': Privileges.Normal,
    'verified': Privileges.Verified,
    'whitelisted': Privileges.Whitelisted,
    'supporter': Privileges.Supporter,
    'premium': Privileges.Premium,
    'alumni': Privileges.Alumni,
    'tournament': Privileges.Tournament,
    'nominator': Privileges.Nominator,
    'mod': Privileges.Mod,
    'admin': Privileges.Admin,
    'dangerous': Privileges.Dangerous
}

@command(Privileges.Dangerous, hidden=True)
async def addpriv(ctx: Context) -> Optional[str]:
    """Set privileges for a specified player (by name)."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !addpriv <name> <role1 role2 role3 ...>'

    bits = Privileges(0)

    for m in [m.lower() for m in ctx.args[1:]]:
        if m not in str_priv_dict:
            return f'Not found: {m}.'

        bits |= str_priv_dict[m]

    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return 'Could not find user.'

    await t.add_privs(bits)
    return f"Updated {t}'s privileges."

@command(Privileges.Dangerous, hidden=True)
async def rmpriv(ctx: Context) -> Optional[str]:
    """Set privileges for a specified player (by name)."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !rmpriv <name> <role1 role2 role3 ...>'

    bits = Privileges(0)

    for m in [m.lower() for m in ctx.args[1:]]:
        if m not in str_priv_dict:
            return f'Not found: {m}.'

        bits |= str_priv_dict[m]

    if not (t := await glob.players.get_ensure(name=ctx.args[0])):
        return 'Could not find user.'

    await t.remove_privs(bits)
    return f"Updated {t}'s privileges."

@command(Privileges.Dangerous)
async def wipemap(ctx: Context) -> Optional[str]:
    if ctx.args:
        return 'Invalid syntax: !wipemap'

    if time.time() >= ctx.player.last_np['timeout']:
        return 'Please /np a map first!'

    map_md5 = ctx.player.last_np['bmap'].md5

    # delete scores from all tables
    async with glob.db.pool.acquire() as conn:
        async with conn.cursor() as db_cursor:
            for t in ('vn', 'rx', 'ap'):
                await db_cursor.execute(
                    f'DELETE FROM scores_{t} '
                    'WHERE map_md5 = %s',
                    [map_md5]
                )

    return 'Scores wiped.'

@command(Privileges.Dangerous, hidden=True)
async def menu(ctx: Context) -> Optional[str]:
    """Temporary command to illustrate the menu option idea."""
    ctx.player.send_current_menu()

@command(Privileges.Dangerous, aliases=['re'])
async def reload(ctx: Context) -> Optional[str]:
    """Reload a python module."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !reload <module>'

    parent, *children = ctx.args[0].split('.')

    try:
        mod = __import__(parent)
    except ModuleNotFoundError:
        return 'Module not found.'

    try:
        for child in children:
            mod = getattr(mod, child)
    except AttributeError:
        return f'Failed at {child}.'

    try:
        mod = importlib.reload(mod)
    except TypeError as exc:
        return f'{exc.args[0]}.'

    return f'Reloaded {mod.__name__}'

@command(Privileges.Normal)
async def server(ctx: Context) -> Optional[str]:
    """Retrieve performance data about the server."""

    build_str = f'sutekina v{glob.version!r} ({glob.config.domain})'

    # get info about this process
    proc = psutil.Process(os.getpid())
    uptime = int(time.time() - proc.create_time())

    # get info about our cpu
    with open('/proc/cpuinfo') as f:
        header = 'model name\t: '
        trailer = '\n'

        model_names = Counter(
            line[len(header):-len(trailer)]
            for line in f.readlines()
            if line.startswith('model name')
        )

    # list of all cpus installed with thread count
    cpus_info = ' | '.join([f'{v}x {k}' for k, v in model_names.most_common()])

    # get system-wide ram usage
    sys_ram = psutil.virtual_memory()

    # output ram usage as `{gulag_used}MB / {sys_used}MB / {sys_total}MB`
    gulag_ram = proc.memory_info()[0]
    ram_values = (gulag_ram, sys_ram.used, sys_ram.total)
    ram_info = ' / '.join([f'{v // 1024 ** 2}MB' for v in ram_values])

    # divide up pkg versions, 3 displayed per line, e.g.
    # aiohttp v3.6.3 | aiomysql v0.0.21 | bcrypt v3.2.0
    # cmyui v1.7.3 | datadog v0.40.1 | geoip2 v4.1.0
    # maniera v1.0.0 | mysql-connector-python v8.0.23 | orjson v3.5.1
    # psutil v5.8.0 | py3rijndael v0.3.3 | uvloop v0.15.2
    reqs = (Path.cwd() / 'ext/requirements.txt').read_text().splitlines()
    pkg_sections = [reqs[i:i+3] for i in range(0, len(reqs), 3)]

    mirror_url = glob.config.mirror
    using_osuapi = glob.config.osu_api_key != ''
    advanced_mode = glob.config.advanced
    auto_logging = glob.config.automatically_report_problems

    return '\n'.join([
        f'{build_str} | uptime: {seconds_readable(uptime)}',
        f'cpu(s): {cpus_info}',
        f'ram: {ram_info}',
        f'mirror: {mirror_url} | osu!api connection: {using_osuapi}',
        f'advanced mode: {advanced_mode} | auto logging: {auto_logging}',
        '',
        'requirements',
        '\n'.join([' | '.join([
            f'{pkg} v{pkg_version(pkg)}'
            for pkg in section
        ]) for section in pkg_sections])
    ])

""" Advanced commands (only allowed with `advanced = True` in config) """

# NOTE: some of these commands are potentially dangerous, and only
# really intended for advanced users looking for access to lower level
# utilities. Some may give direct access to utilties that could perform
# harmful tasks to the underlying machine, so use at your own risk.

if glob.config.advanced:
    from sys import modules as installed_mods

    __py_namespace = globals() | {
        mod: __import__(mod) for mod in (
            'asyncio', 'dis', 'os', 'sys', 'struct', 'discord',
            'cmyui', 'datetime', 'time', 'inspect', 'math',
            'importlib'
        ) if mod in installed_mods
    }

    @command(Privileges.Dangerous)
    async def py(ctx: Context) -> Optional[str]:
        """Allow for (async) access to the python interpreter."""
        # This can be very good for getting used to gulag's API; just look
        # around the codebase and find things to play with in your server.
        # Ex: !py return (await glob.players.get(name='cmyui')).status.action
        if not ctx.args:
            return 'owo'

        # turn our input args into a coroutine definition string.
        definition = '\n '.join([
            'async def __py(ctx):',
            ' '.join(ctx.args)
        ])

        try: # def __py(ctx)
            exec(definition, __py_namespace)  # add to namespace
            ret = await __py_namespace['__py'](ctx) # await it's return
        except Exception as exc: # return exception in osu! chat
            ret = f'{exc.__class__}: {exc}'

        if '__py' in __py_namespace:
            del __py_namespace['__py']

        if ret is None:
            return 'Success'

        # TODO: perhaps size checks?

        if not isinstance(ret, str):
            ret = pprint.pformat(ret, compact=True)

        return ret

""" Multiplayer commands
# The commands below for multiplayer match management.
# Most commands are open to player usage.
"""

@mp_commands.add(Privileges.Normal, aliases=['h'])
async def mp_help(ctx: Context) -> Optional[str]:
    """Show all documented multiplayer commands the player can access."""
    prefix = glob.config.command_prefix
    cmds = []

    for cmd in mp_commands.commands:
        if not cmd.doc or ctx.player.priv & cmd.priv != cmd.priv:
            # no doc, or insufficient permissions.
            continue

        cmds.append(f'{prefix}mp {cmd.triggers[0]}: {cmd.doc}')

    return '\n'.join(cmds)

@mp_commands.add(Privileges.Normal, aliases=['st'])
async def mp_start(ctx: Context) -> Optional[str]:
    """Start the current multiplayer match, with any players ready."""
    if len(ctx.args) > 1:
        return 'Invalid syntax: !mp start <force/seconds>'

    # this command can be used in a few different ways;
    # !mp start: start the match now (make sure all players are ready)
    # !mp start force: start the match now (don't check for ready)
    # !mp start N: start the match in N seconds (don't check for ready)
    # !mp start cancel: cancel the current match start timer

    if not ctx.args:
        # !mp start
        if ctx.match.starting['start'] is not None:
            time_remaining = int(ctx.match.starting['time'] - time.time())
            return f'Match starting in {time_remaining} seconds.'

        if any([s.status == SlotStatus.not_ready for s in ctx.match.slots]):
            return 'Not all players are ready (`!mp start force` to override).'
    else:
        if ctx.args[0].isdecimal():
            # !mp start N
            if ctx.match.starting['start'] is not None:
                time_remaining = int(ctx.match.starting['time'] - time.time())
                return f'Match starting in {time_remaining} seconds.'

            # !mp start <seconds>
            duration = int(ctx.args[0])
            if not 0 < duration <= 300:
                return 'Timer range is 1-300 seconds.'

            def _start() -> None:
                """Remove any pending timers & start the match."""
                # remove start & alert timers
                ctx.match.starting['start'] = None
                ctx.match.starting['alerts'] = None
                ctx.match.starting['time'] = None

                # make sure player didn't leave the
                # match since queueing this start lol..
                if ctx.player not in ctx.match:
                    ctx.match.chat.send_bot('Player left match? (cancelled)')
                    return

                ctx.match.start()
                ctx.match.chat.send_bot('Starting match.')

            def _alert_start(t: int) -> None:
                """Alert the match of the impending start."""
                ctx.match.chat.send_bot(f'Match starting in {t} seconds.')

            # add timers to our match object,
            # so we can cancel them if needed.
            ctx.match.starting['start'] = glob.loop.call_later(duration, _start)
            ctx.match.starting['alerts'] = [
                glob.loop.call_later(duration - t, lambda t=t: _alert_start(t))
                for t in (60, 30, 10, 5, 4, 3, 2, 1) if t < duration
            ]
            ctx.match.starting['time'] = time.time() + duration

            return f'Match will start in {duration} seconds.'
        elif ctx.args[0] in ('cancel', 'c'):
            # !mp start cancel
            if ctx.match.starting['start'] is None:
                return 'Match timer not active!'

            ctx.match.starting['start'].cancel()
            for alert in ctx.match.starting['alerts']:
                alert.cancel()

            ctx.match.starting['start'] = None
            ctx.match.starting['alerts'] = None
            ctx.match.starting['time'] = None

            return 'Match timer cancelled.'
        elif ctx.args[0] not in ('force', 'f'):
            return 'Invalid syntax: !mp start <force/seconds>'
        # !mp start force simply passes through

    ctx.match.start()
    return 'Good luck!'

@mp_commands.add(Privileges.Normal, aliases=['a'])
async def mp_abort(ctx: Context) -> Optional[str]:
    """Abort the current in-progress multiplayer match."""
    if not ctx.match.in_progress:
        return 'Abort what?'

    ctx.match.unready_players(expected=SlotStatus.playing)

    ctx.match.in_progress = False
    ctx.match.enqueue(packets.matchAbort())
    ctx.match.enqueue_state()
    return 'Match aborted.'

@mp_commands.add(Privileges.Normal)
async def mp_map(ctx: Context) -> Optional[str]:
    """Set the current match's current map by id."""
    if len(ctx.args) != 1 or not ctx.args[0].isdecimal():
        return 'Invalid syntax: !mp map <beatmapid>'

    map_id = int(ctx.args[0])

    if map_id == ctx.match.map_id:
        return 'Map already selected.'

    if not (bmap := await Beatmap.from_bid(map_id)):
        return 'Beatmap not found.'

    ctx.match.map_id = bmap.id
    ctx.match.map_md5 = bmap.md5
    ctx.match.map_name = bmap.full

    ctx.match.mode = bmap.mode

    ctx.match.enqueue_state()
    return f'Selected: {bmap.embed}.'

@mp_commands.add(Privileges.Normal)
async def mp_mods(ctx: Context) -> Optional[str]:
    """Set the current match's mods, from string form."""
    if len(ctx.args) != 1 or len(ctx.args[0]) % 2 != 0:
        return 'Invalid syntax: !mp mods <mods>'

    mods = Mods.from_modstr(ctx.args[0])
    mods = mods.filter_invalid_combos(ctx.match.mode.as_vanilla)

    if ctx.match.freemods:
        if ctx.player is ctx.match.host:
            # allow host to set speed-changing mods.
            ctx.match.mods = mods & SPEED_CHANGING_MODS

        # set slot mods
        ctx.match.get_slot(ctx.player).mods = mods & ~SPEED_CHANGING_MODS
    else:
        # not freemods, set match mods.
        ctx.match.mods = mods

    ctx.match.enqueue_state()
    return 'Match mods updated.'

@mp_commands.add(Privileges.Normal, aliases=['fm', 'fmods'])
async def mp_freemods(ctx: Context) -> Optional[str]:
    """Toggle freemods status for the match."""
    if len(ctx.args) != 1 or ctx.args[0] not in ('on', 'off'):
        return 'Invalid syntax: !mp freemods <on/off>'

    if ctx.args[0] == 'on':
        # central mods -> all players mods.
        ctx.match.freemods = True

        for s in ctx.match.slots:
            if s.status & SlotStatus.has_player:
                # the slot takes any non-speed
                # changing mods from the match.
                s.mods = ctx.match.mods & ~SPEED_CHANGING_MODS

        ctx.match.mods &= SPEED_CHANGING_MODS
    else:
        # host mods -> central mods.
        ctx.match.freemods = False

        host = ctx.match.get_host_slot() # should always exist
        # the match keeps any speed-changing mods,
        # and also takes any mods the host has enabled.
        ctx.match.mods &= SPEED_CHANGING_MODS
        ctx.match.mods |= host.mods

        for s in ctx.match.slots:
            if s.status & SlotStatus.has_player:
                s.mods = Mods.NOMOD

    ctx.match.enqueue_state()
    return 'Match freemod status updated.'

@mp_commands.add(Privileges.Normal)
async def mp_host(ctx: Context) -> Optional[str]:
    """Set the current match's current host by id."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp host <name>'

    if not (t := glob.players.get(name=ctx.args[0])):
        return 'Could not find a user by that name.'

    if t is ctx.match.host:
        return "They're already host, silly!"

    if t not in ctx.match:
        return 'Found no such player in the match.'

    ctx.match.host = t
    ctx.match.host.enqueue(packets.matchTransferHost())
    ctx.match.enqueue_state(lobby=False)
    return 'Match host updated.'

@mp_commands.add(Privileges.Normal)
async def mp_randpw(ctx: Context) -> Optional[str]:
    """Randomize the current match's password."""
    ctx.match.passwd = secrets.token_hex(8)
    return 'Match password randomized.'

@mp_commands.add(Privileges.Normal, aliases=['inv'])
async def mp_invite(ctx: Context) -> Optional[str]:
    """Invite a player to the current match by name."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp invite <name>'

    if not (t := glob.players.get(name=ctx.args[0])):
        return 'Could not find a user by that name.'
    elif t is glob.bot:
        p.send("I'm too busy!", sender=glob.bot)
        return

    if t is glob.bot:
        return "I'm too busy!"

    if t is ctx.player:
        return "You can't invite yourself!"

    t.enqueue(packets.matchInvite(ctx.player, t.name))
    return f'Invited {t} to the match.'

@mp_commands.add(Privileges.Normal)
async def mp_addref(ctx: Context) -> Optional[str]:
    """Add a referee to the current match by name."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp addref <name>'

    if not (t := glob.players.get(name=ctx.args[0])):
        return 'Could not find a user by that name.'

    if t not in ctx.match:
        return 'User must be in the current match!'

    if t in ctx.match.refs:
        return f'{t} is already a match referee!'

    ctx.match._refs.add(t)
    return f'{t.name} added to match referees.'

@mp_commands.add(Privileges.Normal)
async def mp_rmref(ctx: Context) -> Optional[str]:
    """Remove a referee from the current match by name."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp addref <name>'

    if not (t := glob.players.get(name=ctx.args[0])):
        return 'Could not find a user by that name.'

    if t not in ctx.match.refs:
        return f'{t} is not a match referee!'

    if t is ctx.match.host:
        return 'The host is always a referee!'

    ctx.match._refs.remove(t)
    return f'{t.name} removed from match referees.'

@mp_commands.add(Privileges.Normal)
async def mp_listref(ctx: Context) -> Optional[str]:
    """List all referees from the current match."""
    return ', '.join(map(str, ctx.match.refs)) + '.'

@mp_commands.add(Privileges.Normal)
async def mp_lock(ctx: Context) -> Optional[str]:
    """Lock all unused slots in the current match."""
    for slot in ctx.match.slots:
        if slot.status == SlotStatus.open:
            slot.status = SlotStatus.locked

    ctx.match.enqueue_state()
    return 'All unused slots locked.'

@mp_commands.add(Privileges.Normal)
async def mp_unlock(ctx: Context) -> Optional[str]:
    """Unlock locked slots in the current match."""
    for slot in ctx.match.slots:
        if slot.status == SlotStatus.locked:
            slot.status = SlotStatus.open

    ctx.match.enqueue_state()
    return 'All locked slots unlocked.'

@mp_commands.add(Privileges.Normal)
async def mp_teams(ctx: Context) -> Optional[str]:
    """Change the team type for the current match."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp teams <type>'

    team_type = ctx.args[0]

    if team_type in ('ffa', 'freeforall', 'head-to-head'):
        ctx.match.team_type = MatchTeamTypes.head_to_head
    elif team_type in ('tag', 'coop', 'co-op', 'tag-coop'):
        ctx.match.team_type = MatchTeamTypes.tag_coop
    elif team_type in ('teams', 'team-vs', 'teams-vs'):
        ctx.match.team_type = MatchTeamTypes.team_vs
    elif team_type in ('tag-teams', 'tag-team-vs', 'tag-teams-vs'):
        ctx.match.team_type = MatchTeamTypes.tag_team_vs
    else:
        return 'Unknown team type. (ffa, tag, teams, tag-teams)'

    # find the new appropriate default team.
    # defaults are (ffa: neutral, teams: red).
    if ctx.match.team_type in (
        MatchTeamTypes.head_to_head,
        MatchTeamTypes.tag_coop
    ):
        new_t = MatchTeams.neutral
    else:
        new_t = MatchTeams.red

    # change each active slots team to
    # fit the correspoding team type.
    for s in ctx.match.slots:
        if s.status & SlotStatus.has_player:
            s.team = new_t

    if ctx.match.is_scrimming:
        # reset score if scrimming.
        ctx.match.reset_scrim()

    ctx.match.enqueue_state()
    return 'Match team type updated.'

@mp_commands.add(Privileges.Normal, aliases=['cond'])
async def mp_condition(ctx: Context) -> Optional[str]:
    """Change the win condition for the match."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp condition <type>'

    cond = ctx.args[0]

    if cond == 'pp':
        # special case - pp can't actually be used as an ingame
        # win condition, but gulag allows it to be passed into
        # this command during a scrims to use pp as a win cond.
        if not ctx.match.is_scrimming:
            return 'PP is only useful as a win condition during scrims.'
        if ctx.match.use_pp_scoring:
            return 'PP scoring already enabled.'

        ctx.match.use_pp_scoring = True
    else:
        if ctx.match.use_pp_scoring:
            ctx.match.use_pp_scoring = False

        if cond == 'score':
            ctx.match.win_condition = MatchWinConditions.score
        elif cond in ('accuracy', 'acc'):
            ctx.match.win_condition = MatchWinConditions.accuracy
        elif cond == 'combo':
            ctx.match.win_condition = MatchWinConditions.combo
        elif cond in ('scorev2', 'v2'):
            ctx.match.win_condition = MatchWinConditions.scorev2
        else:
            return 'Invalid win condition. (score, acc, combo, scorev2, *pp)'

    ctx.match.enqueue_state(lobby=False)
    return 'Match win condition updated.'

@mp_commands.add(Privileges.Normal, aliases=['autoref'])
async def mp_scrim(ctx: Context) -> Optional[str]:
    """Start a scrim in the current match."""
    if (
        len(ctx.args) != 1 or
        not (r_match := regexes.best_of.fullmatch(ctx.args[0]))
    ):
        return 'Invalid syntax: !mp scrim <bo#>'

    if not 0 <= (best_of := int(r_match[1])) < 16:
        return 'Best of must be in range 0-15.'

    winning_pts = (best_of // 2) + 1

    if winning_pts != 0:
        # setting to real num
        if ctx.match.is_scrimming:
            return 'Already scrimming!'

        if best_of % 2 == 0:
            return 'Best of must be an odd number!'

        ctx.match.is_scrimming = True
        msg = (f'A scrimmage has been started by {ctx.player.name}; '
               f'first to {winning_pts} points wins. Best of luck!')
    else:
        # setting to 0
        if not ctx.match.is_scrimming:
            return 'Not currently scrimming!'

        ctx.match.is_scrimming = False
        ctx.match.reset_scrim()
        msg = 'Scrimming cancelled.'

    ctx.match.winning_pts = winning_pts
    return msg

@mp_commands.add(Privileges.Normal, aliases=['end'])
async def mp_endscrim(ctx: Context) -> Optional[str]:
    """End the current matches ongoing scrim."""
    if not ctx.match.is_scrimming:
        return 'Not currently scrimming!'

    ctx.match.is_scrimming = False
    ctx.match.reset_scrim()
    return 'Scrimmage ended.' # TODO: final score (get_score method?)

@mp_commands.add(Privileges.Normal, aliases=['rm'])
async def mp_rematch(ctx: Context) -> Optional[str]:
    """Restart a scrim, or roll back previous match point."""
    if ctx.args:
        return 'Invalid syntax: !mp rematch'

    if ctx.player is not ctx.match.host:
        return 'Only available to the host.'

    if not ctx.match.is_scrimming:
        if ctx.match.winning_pts == 0:
            msg = 'No scrim to rematch; to start one, use !mp scrim.'
        else:
            # re-start scrimming with old points
            ctx.match.is_scrimming = True
            msg = (
                f'A rematch has been started by {ctx.player.name}; '
                f'first to {ctx.match.winning_pts} points wins. Best of luck!'
            )
    else:
        # reset the last match point awarded
        if not ctx.match.winners:
            return "No match points have yet been awarded!"

        if (recent_winner := ctx.match.winners[-1]) is None:
            return 'The last point was a tie!'

        ctx.match.match_points[recent_winner] -= 1 # TODO: team name
        ctx.match.winners.pop()

        msg = f'A point has been deducted from {recent_winner}.'

    return msg

@mp_commands.add(Privileges.Admin, aliases=['f'], hidden=True)
async def mp_force(ctx: Context) -> Optional[str]:
    """Force a player into the current match by name."""
    # NOTE: this overrides any limits such as silences or passwd.
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp force <name>'

    if not (t := glob.players.get(name=ctx.args[0])):
        return 'Could not find a user by that name.'

    t.join_match(ctx.match, ctx.match.passwd)
    return 'Welcome.'

# mappool-related mp commands

@mp_commands.add(Privileges.Normal, aliases=['lp'])
async def mp_loadpool(ctx: Context) -> Optional[str]:
    """Load a mappool into the current match."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp loadpool <name>'

    if ctx.player is not ctx.match.host:
        return 'Only available to the host.'

    name = ctx.args[0]

    if not (pool := glob.pools.get(name)):
        return 'Could not find a pool by that name!'

    if ctx.match.pool is pool:
        return f'{pool!r} already selected!'

    ctx.match.pool = pool
    return f'{pool!r} selected.'

@mp_commands.add(Privileges.Normal, aliases=['ulp'])
async def mp_unloadpool(ctx: Context) -> Optional[str]:
    """Unload the current matches mappool."""
    if ctx.args:
        return 'Invalid syntax: !mp unloadpool'

    if ctx.player is not ctx.match.host:
        return 'Only available to the host.'

    if not ctx.match.pool:
        return 'No mappool currently selected!'

    ctx.match.pool = None
    return 'Mappool unloaded.'

@mp_commands.add(Privileges.Normal)
async def mp_ban(ctx: Context) -> Optional[str]:
    """Ban a pick in the currently loaded mappool."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp ban <pick>'

    if not ctx.match.pool:
        return 'No pool currently selected!'

    mods_slot = ctx.args[0]

    # separate mods & slot
    if not (r_match := regexes.mappool_pick.fullmatch(mods_slot)):
        return 'Invalid pick syntax; correct example: HD2'

    # not calling mods.filter_invalid_combos here intentionally.
    mods = Mods.from_modstr(r_match[1])
    slot = int(r_match[2])

    if (mods, slot) not in ctx.match.pool.maps:
        return f'Found no {mods_slot} pick in the pool.'

    if (mods, slot) in ctx.match.bans:
        return 'That pick is already banned!'

    ctx.match.bans.add((mods, slot))
    return f'{mods_slot} banned.'

@mp_commands.add(Privileges.Normal)
async def mp_unban(ctx: Context) -> Optional[str]:
    """Unban a pick in the currently loaded mappool."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp unban <pick>'

    if not ctx.match.pool:
        return 'No pool currently selected!'

    mods_slot = ctx.args[0]

    # separate mods & slot
    if not (r_match := regexes.mappool_pick.fullmatch(mods_slot)):
        return 'Invalid pick syntax; correct example: HD2'

    # not calling mods.filter_invalid_combos here intentionally.
    mods = Mods.from_modstr(r_match[1])
    slot = int(r_match[2])

    if (mods, slot) not in ctx.match.pool.maps:
        return f'Found no {mods_slot} pick in the pool.'

    if (mods, slot) not in ctx.match.bans:
        return 'That pick is not currently banned!'

    ctx.match.bans.remove((mods, slot))
    return f'{mods_slot} unbanned.'

@mp_commands.add(Privileges.Normal)
async def mp_pick(ctx: Context) -> Optional[str]:
    """Pick a map from the currently loaded mappool."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !mp pick <pick>'

    if not ctx.match.pool:
        return 'No pool currently loaded!'

    mods_slot = ctx.args[0]

    # separate mods & slot
    if not (r_match := regexes.mappool_pick.fullmatch(mods_slot)):
        return 'Invalid pick syntax; correct example: HD2'

    # not calling mods.filter_invalid_combos here intentionally.
    mods = Mods.from_modstr(r_match[1])
    slot = int(r_match[2])

    if (mods, slot) not in ctx.match.pool.maps:
        return f'Found no {mods_slot} pick in the pool.'

    if (mods, slot) in ctx.match.bans:
        return f'{mods_slot} has been banned from being picked.'

    # update match beatmap to the picked map.
    bmap = ctx.match.pool.maps[(mods, slot)]
    ctx.match.map_md5 = bmap.md5
    ctx.match.map_id = bmap.id
    ctx.match.map_name = bmap.full

    # TODO: some kind of abstraction allowing
    # for something like !mp pick fm.
    if ctx.match.freemods:
        # if freemods are enabled, disable them.
        ctx.match.freemods = False

        for s in ctx.match.slots:
            if s.status & SlotStatus.has_player:
                s.mods = Mods.NOMOD

    # update match mods to the picked map.
    ctx.match.mods = mods

    ctx.match.enqueue_state()

    return f'Picked {bmap.embed}. ({mods_slot})'

""" Mappool management commands
# The commands below are for event managers
# and tournament hosts/referees to help automate
# tedious processes of running tournaments.
"""

@pool_commands.add(Privileges.Tournament, aliases=['h'], hidden=True)
async def pool_help(ctx: Context) -> Optional[str]:
    """Show all documented mappool commands the player can access."""
    prefix = glob.config.command_prefix
    cmds = []

    for cmd in pool_commands.commands:
        if not cmd.doc or ctx.player.priv & cmd.priv != cmd.priv:
            # no doc, or insufficient permissions.
            continue

        cmds.append(f'{prefix}pool {cmd.triggers[0]}: {cmd.doc}')

    return '\n'.join(cmds)

@pool_commands.add(Privileges.Tournament, aliases=['c'], hidden=True)
async def pool_create(ctx: Context) -> Optional[str]:
    """Add a new mappool to the database."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !pool create <name>'

    name = ctx.args[0]

    if glob.pools.get(name):
        return 'Pool already exists by that name!'

    # insert pool into db
    await glob.db.execute(
        'INSERT INTO tourney_pools '
        '(name, created_at, created_by) '
        'VALUES (%s, NOW(), %s)',
        [name, ctx.player.id]
    )

    # add to cache (get from sql for id & time)
    res = await glob.db.fetch('SELECT * FROM tourney_pools '
                              'WHERE name = %s', [name])

    res['created_by'] = await glob.players.get_ensure(id=res['created_by'])

    glob.pools.append(MapPool(**res))

    return f'{name} created.'

@pool_commands.add(Privileges.Tournament, aliases=['del', 'd'], hidden=True)
async def pool_delete(ctx: Context) -> Optional[str]:
    """Remove a mappool from the database."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !pool delete <name>'

    name = ctx.args[0]

    if not (pool := glob.pools.get(name)):
        return 'Could not find a pool by that name!'

    # delete from db
    await glob.db.execute(
        'DELETE FROM tourney_pools '
        'WHERE name = %s',
        [name]
    )

    # remove from cache
    glob.pools.remove(pool)

    return f'{name} deleted.'

@pool_commands.add(Privileges.Tournament, aliases=['a'], hidden=True)
async def pool_add(ctx: Context) -> Optional[str]:
    """Add a new map to a mappool in the database."""
    if len(ctx.args) != 2:
        return 'Invalid syntax: !pool add <name> <pick>'

    if time.time() >= ctx.player.last_np['timeout']:
        return 'Please /np a map first!'

    name, mods_slot = ctx.args
    mods_slot = mods_slot.upper() # ocd
    bmap = ctx.player.last_np['bmap']

    # separate mods & slot
    if not (r_match := regexes.mappool_pick.fullmatch(mods_slot)):
        return 'Invalid pick syntax; correct example: HD2'

    if len(r_match[1]) % 2 != 0:
        return 'Invalid mods.'

    # not calling mods.filter_invalid_combos here intentionally.
    mods = Mods.from_modstr(r_match[1])
    slot = int(r_match[2])

    if not (pool := glob.pools.get(name)):
        return 'Could not find a pool by that name!'

    if (mods, slot) in pool.maps:
        return f'{mods_slot} is already {pool.maps[(mods, slot)].embed}!'

    if bmap in pool.maps.values():
        return 'Map is already in the pool!'

    # insert into db
    await glob.db.execute(
        'INSERT INTO tourney_pool_maps '
        '(map_id, pool_id, mods, slot) '
        'VALUES (%s, %s, %s, %s)',
        [bmap.id, pool.id, mods, slot]
    )

    # add to cache
    pool.maps[(mods, slot)] = bmap

    return f'{bmap.embed} added to {name}.'

@pool_commands.add(Privileges.Tournament, aliases=['rm', 'r'], hidden=True)
async def pool_remove(ctx: Context) -> Optional[str]:
    """Remove a map from a mappool in the database."""
    if len(ctx.args) != 2:
        return 'Invalid syntax: !pool remove <name> <pick>'

    name, mods_slot = ctx.args
    mods_slot = mods_slot.upper() # ocd

    # separate mods & slot
    if not (r_match := regexes.mappool_pick.fullmatch(mods_slot)):
        return 'Invalid pick syntax; correct example: HD2'

    # not calling mods.filter_invalid_combos here intentionally.
    mods = Mods.from_modstr(r_match[1])
    slot = int(r_match[2])

    if not (pool := glob.pools.get(name)):
        return 'Could not find a pool by that name!'

    if (mods, slot) not in pool.maps:
        return f'Found no {mods_slot} pick in the pool.'

    # delete from db
    await glob.db.execute(
        'DELETE FROM tourney_pool_maps '
        'WHERE mods = %s AND slot = %s',
        [mods, slot]
    )

    # remove from cache
    del pool.maps[(mods, slot)]

    return f'{mods_slot} removed from {name}.'

@pool_commands.add(Privileges.Tournament, aliases=['l'], hidden=True)
async def pool_list(ctx: Context) -> Optional[str]:
    """List all existing mappools information."""
    if not (pools := glob.pools):
        return 'There are currently no pools!'

    l = [f'Mappools ({len(pools)})']

    for pool in pools:
        l.append(
            f'[{pool.created_at:%Y-%m-%d}] {pool.id}. '
            f'{pool.name}, by {pool.created_by}.'
        )

    return '\n'.join(l)

@pool_commands.add(Privileges.Tournament, aliases=['i'], hidden=True)
async def pool_info(ctx: Context) -> Optional[str]:
    """Get all information for a specific mappool."""
    if len(ctx.args) != 1:
        return 'Invalid syntax: !pool info <name>'

    name = ctx.args[0]

    if not (pool := glob.pools.get(name)):
        return 'Could not find a pool by that name!'

    _time = pool.created_at.strftime('%H:%M:%S%p')
    _date = pool.created_at.strftime('%Y-%m-%d')
    datetime_fmt = f'Created at {_time} on {_date}'
    l = [f'{pool.id}. {pool.name}, by {pool.created_by} | {datetime_fmt}.']

    for (mods, slot), bmap in pool.maps.items():
        l.append(f'{mods!r}{slot}: {bmap.embed}')

    return '\n'.join(l)

""" Clan managment commands
# The commands below are for managing gulag
# clans, for users, clan staff, and server staff.
"""

@clan_commands.add(Privileges.Normal, aliases=['h'])
async def clan_help(ctx: Context) -> Optional[str]:
    """Show all documented clan commands the player can access."""
    prefix = glob.config.command_prefix
    cmds = []

    for cmd in clan_commands.commands:
        if not cmd.doc or ctx.player.priv & cmd.priv != cmd.priv:
            # no doc, or insufficient permissions.
            continue

        cmds.append(f'{prefix}clan {cmd.triggers[0]}: {cmd.doc}')

    return '\n'.join(cmds)

@clan_commands.add(Privileges.Normal, aliases=['c'])
async def clan_create(ctx: Context) -> Optional[str]:
    """Create a clan with a given tag & name."""
    if len(ctx.args) < 2:
        return 'Invalid syntax: !clan create <tag> <name>'

    if not 1 <= len(tag := ctx.args[0].upper()) <= 6:
        return 'Clan tag may be 1-6 characters long.'

    if not 2 <= len(name := ' '.join(ctx.args[1:])) <= 16:
        return 'Clan name may be 2-16 characters long.'

    if ctx.player.clan:
        return f"You're already a member of {ctx.player.clan}!"

    if glob.clans.get(name=name):
        return 'That name has already been claimed by another clan.'

    if glob.clans.get(tag=tag):
        return 'That tag has already been claimed by another clan.'

    created_at = datetime.now()

    # add clan to sql (generates id)
    clan_id = await glob.db.execute(
        'INSERT INTO clans '
        '(name, tag, created_at, owner) '
        'VALUES (%s, %s, %s, %s)',
        [name, tag, created_at, ctx.player.id]
    )

    # add clan to cache
    clan = Clan(id=clan_id, name=name, tag=tag,
                created_at=created_at, owner=ctx.player.id)
    glob.clans.append(clan)

    # set owner's clan & clan priv (cache & sql)
    ctx.player.clan = clan
    ctx.player.clan_priv = ClanPrivileges.Owner

    clan.owner = ctx.player.id
    clan.members.add(ctx.player.id)

    if 'full_name' in ctx.player.__dict__:
        del ctx.player.full_name # wipe cached_property

    if 'full_name' in p.__dict__:
        del p.full_name # wipe cached_property

    await glob.db.execute(
        'UPDATE users '
        'SET clan_id = %s, '
        'clan_priv = 3 ' # ClanPrivileges.Owner
        'WHERE id = %s',
        [clan_id, ctx.player.id]
    )

    # announce clan creation
    if announce_chan := glob.channels['#announce']:
        msg = f'\x01ACTION founded {clan!r}.'
        announce_chan.send(msg, sender=ctx.player, to_self=True)

    return f'{clan!r} created.'

@clan_commands.add(Privileges.Normal, aliases=['delete', 'd'])
async def clan_disband(ctx: Context) -> Optional[str]:
    """Disband a clan (admins may disband others clans)."""
    if ctx.args:
        # disband a specified clan by tag
        if ctx.player not in glob.players.staff:
            return 'Only staff members may disband the clans of others.'

        if not (clan := glob.clans.get(tag=' '.join(ctx.args).upper())):
            return 'Could not find a clan by that tag.'
    else:
        # disband the player's clan
        if not (clan := ctx.player.clan):
            return "You're not a member of a clan!"

    # delete clan from sql
    await glob.db.execute(
        'DELETE FROM clans '
        'WHERE id = %s',
        [clan.id]
    )

    # remove all members from the clan,
    # reset their clan privs (cache & sql).
    # NOTE: only online players need be to be uncached.
    for member_id in clan.members:
        if member := glob.players.get(id=member_id):
            member.clan = None
            member.clan_priv = None
            if 'full_name' in member.__dict__:
                del member.full_name # wipe cached_property

    await glob.db.execute(
        'UPDATE users '
        'SET clan_id = 0, '
        'clan_priv = 0 '
        'WHERE clan_id = %s',
        [clan.id]
    )

    # remove clan from cache
    glob.clans.remove(clan)

    # announce clan disbanding
    if announce_chan := glob.channels['#announce']:
        msg = f'\x01ACTION disbanded {clan!r}.'
        announce_chan.send(msg, sender=ctx.player, to_self=True)

    return f'{clan!r} disbanded.'

@clan_commands.add(Privileges.Normal, aliases=['i'])
async def clan_info(ctx: Context) -> Optional[str]:
    """Lookup information of a clan by tag."""
    if not ctx.args:
        return 'Invalid syntax: !clan info <tag>'

    if not (clan := glob.clans.get(tag=' '.join(ctx.args).upper())):
        return 'Could not find a clan by that tag.'

    msg = [f"{clan!r} | Founded {clan.created_at:%b %d, %Y}."]

    # get members privs from sql
    res = await glob.db.fetchall(
        'SELECT name, clan_priv '
        'FROM users '
        'WHERE clan_id = %s '
        'ORDER BY clan_priv DESC',
        [clan.id], _dict=False
    )

    for member_name, clan_priv in res:
        priv_str = ('Member', 'Officer', 'Owner')[clan_priv - 1]
        msg.append(f'[{priv_str}] {member_name}')

    return '\n'.join(msg)

# TODO: !clan inv, !clan join, !clan leave

@clan_commands.add(Privileges.Normal, aliases=['l'])
async def clan_list(ctx: Context) -> Optional[str]:
    """List all existing clans information."""
    if ctx.args:
        if len(ctx.args) != 1 or not ctx.args[0].isdecimal():
            return 'Invalid syntax: !clan list (page)'
        else:
            offset = 25 * int(ctx.args[0])
    else:
        offset = 0

    if offset >= (total_clans := len(glob.clans)):
        return 'No clans found.'

    msg = [f'sutekina clans listing ({total_clans} total).']

    for idx, clan in enumerate(glob.clans, offset):
        msg.append(f'{idx + 1}. {clan!r}')

    return '\n'.join(msg)

async def process_commands(p: Player, target: Messageable,
                           msg: str) -> Optional[CommandResponse]:
    # response is either a CommandResponse if we hit a command,
    # or simply False if we don't have any command hits.
    start_time = clock_ns()

    prefix_len = len(glob.config.command_prefix)
    trigger, *args = msg[prefix_len:].strip().split(' ')

    # case-insensitive triggers
    trigger = trigger.lower()

    for cmd_set in command_sets:
        # check if any command sets match.
        if trigger == cmd_set.trigger:
            # matching set found;
            if not args:
                args = ['help']

            if trigger == 'mp':
                # multi set is a bit of a special case,
                # as we do some additional checks.
                if not (m := p.match):
                    # player not in a match
                    return

                if target is not m.chat:
                    # message not in match channel
                    return

                if args[0] != 'help' and (p not in m.refs and
                                          not p.priv & Privileges.Tournament):
                    # doesn't have privs to use !mp commands (allow help).
                    return

                target = m # send match for mp commands instead of chan

            trigger, *args = args # get subcommand
            # case-insensitive triggers
            trigger = trigger.lower()

            commands = cmd_set.commands
            break
    else:
        # no set commands matched, check normal commands.
        commands = regular_commands

    for cmd in commands:
        if (
            trigger in cmd.triggers and
            p.priv & cmd.priv == cmd.priv
        ):
            # found matching trigger with sufficient privs
            ctx = Context(player=p, trigger=trigger, args=args)

            if isinstance(target, Match):
                ctx.match = target
            else:
                ctx.recipient = target

            # command found & we have privileges, run it.
            if res := await cmd.callback(ctx):
                elapsed = cmyui.utils.magnitude_fmt_time(clock_ns() - start_time)

                return {
                    'resp': f'{res} | Elapsed: {elapsed}',
                    'hidden': cmd.hidden
                }

            return {'hidden': False}
