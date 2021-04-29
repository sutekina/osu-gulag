# -*- coding: utf-8 -*-

# NOTE: at some point, parts (or all) of this may
# be rewritten in cython (or c++ ported with cython)?
# i'm not sure how well it works with an async setup
# like this, but we'll see B) massive speed gains tho

import struct
import random
from collections import namedtuple
from enum import IntEnum
from enum import unique
from functools import cache
from functools import lru_cache
from functools import partialmethod
from typing import Optional
from typing import Sequence
from typing import TYPE_CHECKING

from constants.gamemodes import GameMode
from constants.mods import Mods
from constants.types import osuTypes
from objects import glob
from objects.match import Match
from objects.match import MatchTeams
from objects.match import MatchTeamTypes
from objects.match import MatchWinConditions
from objects.match import ScoreFrame
from objects.match import SlotStatus
from utils.misc import escape_enum
from utils.misc import pymysql_encode

if TYPE_CHECKING:
    from objects.player import Player

# tuple of some of struct's format specifiers
# for clean access within packet pack/unpack.
_specifiers = (
    '<b', '<B', # 8
    '<h', '<H', # 16
    '<i', '<I', '<f', # 32
    '<q', '<Q', '<d'  # 64
)

@unique
@pymysql_encode(escape_enum)
class Packets(IntEnum):
    OSU_CHANGE_ACTION = 0
    OSU_SEND_PUBLIC_MESSAGE = 1
    OSU_LOGOUT = 2
    OSU_REQUEST_STATUS_UPDATE = 3
    OSU_PING = 4
    CHO_USER_ID = 5
    CHO_SEND_MESSAGE = 7
    CHO_PONG = 8
    CHO_HANDLE_IRC_CHANGE_USERNAME = 9 # unused
    CHO_HANDLE_IRC_QUIT = 10
    CHO_USER_STATS = 11
    CHO_USER_LOGOUT = 12
    CHO_SPECTATOR_JOINED = 13
    CHO_SPECTATOR_LEFT = 14
    CHO_SPECTATE_FRAMES = 15
    OSU_START_SPECTATING = 16
    OSU_STOP_SPECTATING = 17
    OSU_SPECTATE_FRAMES = 18
    CHO_VERSION_UPDATE = 19
    OSU_ERROR_REPORT = 20
    OSU_CANT_SPECTATE = 21
    CHO_SPECTATOR_CANT_SPECTATE = 22
    CHO_GET_ATTENTION = 23
    CHO_NOTIFICATION = 24
    OSU_SEND_PRIVATE_MESSAGE = 25
    CHO_UPDATE_MATCH = 26
    CHO_NEW_MATCH = 27
    CHO_DISPOSE_MATCH = 28
    OSU_PART_LOBBY = 29
    OSU_JOIN_LOBBY = 30
    OSU_CREATE_MATCH = 31
    OSU_JOIN_MATCH = 32
    OSU_PART_MATCH = 33
    CHO_TOGGLE_BLOCK_NON_FRIEND_DMS = 34
    CHO_MATCH_JOIN_SUCCESS = 36
    CHO_MATCH_JOIN_FAIL = 37
    OSU_MATCH_CHANGE_SLOT = 38
    OSU_MATCH_READY = 39
    OSU_MATCH_LOCK = 40
    OSU_MATCH_CHANGE_SETTINGS = 41
    CHO_FELLOW_SPECTATOR_JOINED = 42
    CHO_FELLOW_SPECTATOR_LEFT = 43
    OSU_MATCH_START = 44
    CHO_ALL_PLAYERS_LOADED = 45
    CHO_MATCH_START = 46
    OSU_MATCH_SCORE_UPDATE = 47
    CHO_MATCH_SCORE_UPDATE = 48
    OSU_MATCH_COMPLETE = 49
    CHO_MATCH_TRANSFER_HOST = 50
    OSU_MATCH_CHANGE_MODS = 51
    OSU_MATCH_LOAD_COMPLETE = 52
    CHO_MATCH_ALL_PLAYERS_LOADED = 53
    OSU_MATCH_NO_BEATMAP = 54
    OSU_MATCH_NOT_READY = 55
    OSU_MATCH_FAILED = 56
    CHO_MATCH_PLAYER_FAILED = 57
    CHO_MATCH_COMPLETE = 58
    OSU_MATCH_HAS_BEATMAP = 59
    OSU_MATCH_SKIP_REQUEST = 60
    CHO_MATCH_SKIP = 61
    CHO_UNAUTHORIZED = 62 # unused
    OSU_CHANNEL_JOIN = 63
    CHO_CHANNEL_JOIN_SUCCESS = 64
    CHO_CHANNEL_INFO = 65
    CHO_CHANNEL_KICK = 66
    CHO_CHANNEL_AUTO_JOIN = 67
    OSU_BEATMAP_INFO_REQUEST = 68
    CHO_BEATMAP_INFO_REPLY = 69
    OSU_MATCH_TRANSFER_HOST = 70
    CHO_PRIVILEGES = 71
    CHO_FRIENDS_LIST = 72
    OSU_FRIEND_ADD = 73
    OSU_FRIEND_REMOVE = 74
    CHO_PROTOCOL_VERSION = 75
    CHO_MAIN_MENU_ICON = 76
    OSU_MATCH_CHANGE_TEAM = 77
    OSU_CHANNEL_PART = 78
    OSU_RECEIVE_UPDATES = 79
    CHO_MONITOR = 80 # unused
    CHO_MATCH_PLAYER_SKIPPED = 81
    OSU_SET_AWAY_MESSAGE = 82
    CHO_USER_PRESENCE = 83
    OSU_IRC_ONLY = 84
    OSU_USER_STATS_REQUEST = 85
    CHO_RESTART = 86
    OSU_MATCH_INVITE = 87
    CHO_MATCH_INVITE = 88
    CHO_CHANNEL_INFO_END = 89
    OSU_MATCH_CHANGE_PASSWORD = 90
    CHO_MATCH_CHANGE_PASSWORD = 91
    CHO_SILENCE_END = 92
    OSU_TOURNAMENT_MATCH_INFO_REQUEST = 93
    CHO_USER_SILENCED = 94
    CHO_USER_PRESENCE_SINGLE = 95
    CHO_USER_PRESENCE_BUNDLE = 96
    OSU_USER_PRESENCE_REQUEST = 97
    OSU_USER_PRESENCE_REQUEST_ALL = 98
    OSU_TOGGLE_BLOCK_NON_FRIEND_DMS = 99
    CHO_USER_DM_BLOCKED = 100
    CHO_TARGET_IS_SILENCED = 101
    CHO_VERSION_UPDATE_FORCED = 102
    CHO_SWITCH_SERVER = 103
    CHO_ACCOUNT_RESTRICTED = 104
    CHO_RTX = 105 # unused
    CHO_MATCH_ABORT = 106
    CHO_SWITCH_TOURNAMENT_SERVER = 107
    OSU_TOURNAMENT_JOIN_MATCH_CHANNEL = 108
    OSU_TOURNAMENT_LEAVE_MATCH_CHANNEL = 109

    def __repr__(self) -> str:
        return f'<{self.name} ({self.value})>'

class BanchoPacket:
    """Abstract base class for bancho packets."""
    type: Optional[Packets] = None
    args: Optional[tuple[osuTypes]] = None
    length: Optional[int] = None

    def __init_subclass__(cls, type: Packets) -> None:
        super().__init_subclass__()

        cls.type = type
        cls.args = cls.__annotations__

        for x in ('type', 'args', 'length'):
            if x in cls.args:
                del cls.args[x]

    async def handle(self, p: 'Player') -> None: ...

Message = namedtuple('Message', ['sender', 'msg', 'recipient', 'sender_id'])
Channel = namedtuple('Channel', ['name', 'topic', 'players'])

class BanchoPacketReader:
    """\
    A class for reading bancho packets sequentially.

    Attributes
    -----------
    view: `memoryview`
        A low-level view to the underlying buffer passed in.

    packet_map: `dict[Packets (packet id), BanchoPacket (handler)]`
        The map of packets the packet reader will handle.

    _current: Optional[`BanchoPacket`]
        The current packet being read by the reader, if any.

    Intended Usage:
    ```
      for packet in BanchoPacketReader(conn.body):
          # once you're ready to handle the packet,
          # simply call it's .handle() method.
          await packet.handle()
    ```
    """
    __slots__ = ('view', 'packet_map', '_current')
    def __init__(self, data: bytes, packet_map: dict) -> None:
        self.view = memoryview(data)
        self.packet_map = packet_map

        self._current: Optional[BanchoPacket] = None

    def __iter__(self):
        return self

    def __next__(self):
        # do not break until we've read the
        # header of a packet we can handle.
        while True:
            p_type, p_len = self.read_header()

            if p_type not in self.packet_map:
                # packet type not handled, remove
                # from internal buffer and continue.
                if p_len != 0:
                    self.view = self.view[p_len:]
            else:
                # we can handle this one.
                break

        # we have a packet handler for this.
        self._current = self.packet_map[p_type]()
        self._current.length = p_len

        if self._current.args:
            self.read_arguments()

        return self._current

    def read_arguments(self) -> None:
        """Read all arguments from the internal buffer."""
        for arg_name, arg_type in self._current.args.items():
            # read value from buffer
            val: object = None

            # non-osu! datatypes
            if arg_type == osuTypes.i8:
                val = self.read_i8()
            elif arg_type == osuTypes.i16:
                val = self.read_i16()
            elif arg_type == osuTypes.i32:
                val = self.read_i32()
            elif arg_type == osuTypes.i64:
                val = self.read_i64()
            elif arg_type == osuTypes.u8:
                val = self.read_i8()
            elif arg_type == osuTypes.u16:
                val = self.read_u16()
            elif arg_type == osuTypes.u32:
                val = self.read_u32()
            elif arg_type == osuTypes.u64:
                val = self.read_u64()

            # osu!-specific data types
            elif arg_type == osuTypes.string:
                val = self.read_string()
            elif arg_type == osuTypes.i32_list:
                val = self.read_i32_list_i16l()
            elif arg_type == osuTypes.i32_list4l:
                val = self.read_i32_list_i32l()
            elif arg_type == osuTypes.message:
                val = self.read_message()
            elif arg_type == osuTypes.channel:
                val = self.read_channel()
            elif arg_type == osuTypes.match:
                val = self.read_match()
            elif arg_type == osuTypes.scoreframe:
                val = self.read_scoreframe()

            elif arg_type == osuTypes.raw:
                # return all packet data raw.
                val = self.view[:self._current.length]
                self.view = self.view[self._current.length:]
            else:
                # should never happen?
                raise ValueError

            # add to our packet object
            setattr(self._current, arg_name, val)

    def read_header(self) -> tuple[int, int]:
        """Read the header of an osu! packet (id & length)."""
        if len(self.view) < 7:
            # not even minimal data
            # remaining in buffer.
            raise StopIteration

        # read type & length from the body
        data = struct.unpack('<HxI', self.view[:7])
        self.view = self.view[7:]
        return Packets(data[0]), data[1]

    def ignore_packet(self) -> None:
        """Skip the current packet in the buffer."""
        self.view = self.view[self._current.length:]
        self._current = None

    """ type readers (functions to read different types from buf) """

    """ basic integral types (signed & unsigned) """
    def _read_integral(self, size: int, signed: bool) -> int:
        val = int.from_bytes(self.view[:size], 'little', signed=signed)
        self.view = self.view[size:]
        return val

    read_i8 = partialmethod(_read_integral, size=1, signed=True)
    read_u8 = partialmethod(_read_integral, size=1, signed=False)
    read_i16 = partialmethod(_read_integral, size=2, signed=True)
    read_u16 = partialmethod(_read_integral, size=2, signed=False)
    read_i32 = partialmethod(_read_integral, size=4, signed=True)
    read_u32 = partialmethod(_read_integral, size=4, signed=False)
    read_i64 = partialmethod(_read_integral, size=8, signed=True)
    read_u64 = partialmethod(_read_integral, size=8, signed=False)

    """ floating point types """
    def read_f32(self) -> float:
        val, = struct.unpack_from('<f', self.view[:4])
        self.view = self.view[4:]
        return val

    def read_f64(self) -> float:
        val, = struct.unpack_from('<d', self.view[:8])
        self.view = self.view[8:]
        return val

    """ integral list types """
    # XXX: some osu! packets use i16 for
    # array length, while others use i32
    def _read_i32_list(self, len_size: int) -> tuple[int]:
        length = int.from_bytes(self.view[:len_size], 'little')
        self.view = self.view[len_size:]

        val = struct.unpack(f'<{"I" * length}', self.view[:length * 4])
        self.view = self.view[length * 4:]
        return val

    read_i32_list_i16l = partialmethod(_read_i32_list, len_size=2)
    read_i32_list_i32l = partialmethod(_read_i32_list, len_size=4)

    """ string type (variable length encoding w/ uleb128) """
    def read_string(self) -> str:
        exists = self.view[0] == 0x0b
        self.view = self.view[1:]

        if not exists:
            # no string sent.
            return ''

        # non-empty string, decode str length (uleb128)
        length = shift = 0

        while True:
            b = self.view[0]
            self.view = self.view[1:]

            length |= (b & 0b01111111) << shift
            if (b & 0b10000000) == 0:
                break

            shift += 7

        val = self.view[:length].tobytes().decode() # copy
        self.view = self.view[length:]
        return val

    """ custom osu! types """

    def read_message(self) -> Message: # namedtuple
        """Read an osu! message from the internal buffer."""
        return Message(
            sender = self.read_string(),
            msg = self.read_string(),
            recipient = self.read_string(),
            sender_id = self.read_i32()
        )

    def read_channel(self) -> Channel: # namedtuple
        """Read an osu! channel from the internal buffer."""
        return Channel(
            name = self.read_string(),
            topic = self.read_string(),
            players = self.read_i32()
        )

    def read_match(self) -> Match:
        """Read an osu! match from the internal buffer."""
        m = Match()

        # ignore match id (i16) and inprogress (i8).
        self.view = self.view[3:]

        self.read_i8() # powerplay unused

        m.mods = Mods(self.read_i32())

        m.name = self.read_string()
        m.passwd = self.read_string()

        m.map_name = self.read_string()
        m.map_id = self.read_i32()
        m.map_md5 = self.read_string()

        for slot in m.slots:
            slot.status = SlotStatus(self.read_i8())

        for slot in m.slots:
            slot.team = MatchTeams(self.read_i8())

        for slot in m.slots:
            if slot.status & SlotStatus.has_player:
                # we don't need this, ignore it.
                self.view = self.view[4:]

        host_id = self.read_i32()
        m.host = glob.players.get(id=host_id)

        m.mode = GameMode(self.read_i8())
        m.win_condition = MatchWinConditions(self.read_i8())
        m.team_type = MatchTeamTypes(self.read_i8())
        m.freemods = self.read_i8() == 1

        # if we're in freemods mode,
        # read individual slot mods.
        if m.freemods:
            for slot in m.slots:
                slot.mods = Mods(self.read_i32())

        # read the seed (used for mania)
        m.seed = self.read_i32()

        return m

    def read_scoreframe(self) -> ScoreFrame:
        fmt = '<iBHHHHHHiHH?BB?'
        sf = ScoreFrame(*struct.unpack_from(fmt, self.view[:29]))
        self.view = self.view[29:]

        if sf.score_v2:
            sf.combo_portion = self.read_f32()
            sf.bonus_portion = self.read_f32()

        return sf

def write_uleb128(num: int) -> bytearray:
    """ Write `num` into an unsigned LEB128. """
    if num == 0:
        return bytearray(b'\x00')

    ret = bytearray()
    length = 0

    while num > 0:
        ret.append(num & 0b01111111)
        num >>= 7
        if num != 0:
            ret[length] |= 0b10000000
        length += 1

    return ret

def write_string(s: str) -> bytearray:
    """ Write `s` into bytes (ULEB128 & string). """
    if s:
        encoded = s.encode()
        ret = bytearray(b'\x0b')
        ret += write_uleb128(len(encoded))
        ret += encoded
    else:
        ret = bytearray(b'\x00')

    return ret

def write_i32_list(l: Sequence[int]) -> bytearray:
    """ Write `l` into bytes (int32 list). """
    ret = bytearray(len(l).to_bytes(2, 'little'))

    for i in l:
        ret += i.to_bytes(4, 'little')

    return ret

def write_message(sender: str, msg: str, recipient: str,
                  sender_id: int) -> bytearray:
    """ Write params into bytes (osu! message). """
    ret = bytearray(write_string(sender))
    ret += write_string(msg)
    ret += write_string(recipient)
    ret += sender_id.to_bytes(4, 'little', signed=True)
    return ret

def write_channel(name: str, topic: str,
                  count: int) -> bytearray:
    """ Write params into bytes (osu! channel). """
    ret = bytearray(write_string(name))
    ret += write_string(topic)
    ret += count.to_bytes(2, 'little')
    return ret

# XXX: deprecated
# def write_mapInfoReply(maps: Sequence[BeatmapInfo]) -> bytearray:
#     """ Write `maps` into bytes (osu! map info). """
#     ret = bytearray(len(maps).to_bytes(4, 'little'))
#
#     # Write files
#     for m in maps:
#         ret += struct.pack('<hiiiBbbbb',
#             m.id, m.map_id, m.set_id, m.thread_id, m.status,
#             m.osu_rank, m.fruits_rank, m.taiko_rank, m.mania_rank
#         )
#         ret += write_string(m.map_md5)
#
#     return ret

def write_match(m: Match, send_pw: bool = True) -> bytearray:
    """ Write `m` into bytes (osu! match). """
    if m.passwd:
        passwd = write_string(m.passwd) if send_pw \
            else b'\x0b\x00'
    else:
        passwd = b'\x00'

    # 0 is for match type
    ret = bytearray(struct.pack('<HbbI', m.id, m.in_progress, 0, m.mods))
    ret += write_string(m.name)
    ret += passwd

    ret += write_string(m.map_name)
    ret += (m.map_id).to_bytes(4, 'little', signed=True)
    ret += write_string(m.map_md5)

    ret.extend([s.status for s in m.slots])
    ret.extend([s.team for s in m.slots])

    for s in m.slots:
        if s.status & SlotStatus.has_player:
            ret += (s.player.id).to_bytes(4, 'little')

    ret += (m.host.id).to_bytes(4, 'little')
    ret.extend((m.mode, m.win_condition,
                m.team_type, m.freemods))

    if m.freemods:
        for s in m.slots:
            ret += (s.mods).to_bytes(4, 'little')

    ret += (m.seed).to_bytes(4, 'little')
    return ret

def write_scoreframe(s: ScoreFrame) -> bytearray:
    """ Write `s` into bytes (osu! scoreframe). """
    return bytearray(struct.pack('<iBHHHHHHiHH?BB?',
        s.time, s.id, s.num300, s.num100, s.num50, s.num_geki,
        s.num_katu, s.num_miss, s.total_score, s.current_combo,
        s.max_combo, s.perfect, s.current_hp, s.tag_byte, s.score_v2
    ))

def write(packid: int, *args: Sequence[object]) -> bytes:
    """ Write `args` into bytes. """
    ret = bytearray(struct.pack('<Hx', packid))

    for p_args, p_type in args:
        if p_type == osuTypes.raw:
            ret += p_args
        elif p_type == osuTypes.string:
            ret += write_string(p_args)
        elif p_type == osuTypes.i32_list:
            ret += write_i32_list(p_args)
        elif p_type == osuTypes.message:
            ret += write_message(*p_args)
        elif p_type == osuTypes.channel:
            ret += write_channel(*p_args)
        elif p_type == osuTypes.match:
            ret += write_match(*p_args)
        elif p_type == osuTypes.scoreframe:
            ret += write_scoreframe(p_args)
        #elif p_type == osuTypes.mapInfoReply:
        #    ret += write_mapInfoReply(p_args)
        else:
            # not a custom type, use struct to pack the data.
            ret += struct.pack(_specifiers[p_type], p_args)

    # add size
    ret[3:3] = struct.pack('<I', len(ret) - 3)
    return bytes(ret)

#
# packets
#

# packet id: 5
@cache
def userID(id: int) -> bytes:
    # id responses:
    # -1: authentication failed
    # -2: old client
    # -3: banned
    # -4: banned
    # -5: error occurred
    # -6: needs supporter
    # -7: password reset
    # -8: requires verification
    # ??: valid id
    return write(
        Packets.CHO_USER_ID,
        (id, osuTypes.i32)
    )

# packet id: 7
def sendMessage(sender: str, msg: str, recipient: str,
                sender_id: int) -> bytes:
    return write(
        Packets.CHO_SEND_MESSAGE,
        ((sender, msg, recipient, sender_id), osuTypes.message)
    )

# packet id: 8
@cache
def pong() -> bytes:
    return write(Packets.CHO_PONG)

# packet id: 9
# NOTE: deprecated
def changeUsername(old: str, new: str) -> bytes:
    return write(
        Packets.CHO_HANDLE_IRC_CHANGE_USERNAME,
        (f'{old}>>>>{new}', osuTypes.string)
    )

BOT_STATUSES = (
    (6, 'gay porn...'), # watching
)
# since the bot is always online and is
# also automatically added to all player's
# friends list, their stats are requested
# *very* frequently, and should be cached.
# NOTE: this is cleared once in a while by
# `bg_loops.reroll_bot_status` to keep fresh.
@cache
def botStats():
    # pick at random from list of potential statuses.
    status_id, status_txt = random.choice(BOT_STATUSES)

    return write(
        Packets.CHO_USER_STATS,
        (glob.bot.id, osuTypes.i32), # id
        (status_id, osuTypes.u8), # action
        (status_txt, osuTypes.string), # info_text
        ('', osuTypes.string), # map_md5
        (0, osuTypes.i32), # mods
        (0, osuTypes.u8), # mode
        (0, osuTypes.i32), # map_id
        (0, osuTypes.i64), # rscore
        (0.0, osuTypes.f32), # acc
        (0, osuTypes.i32), # plays
        (0, osuTypes.i64), # tscore
        (0, osuTypes.i32), # rank
        (0, osuTypes.i16) # pp
    )

# packet id: 11
def userStats(p: 'Player') -> bytes:
    if p is glob.bot:
        return botStats()

    gm_stats = p.gm_stats
    if gm_stats.pp > 0x7fff:
        # over osu! pp cap, we'll have to
        # show their pp as ranked score.
        rscore = gm_stats.pp
        pp = 0
    else:
        rscore = gm_stats.rscore
        pp = gm_stats.pp

    return write(
        Packets.CHO_USER_STATS,
        (p.id, osuTypes.i32),
        (p.status.action, osuTypes.u8),
        (p.status.info_text, osuTypes.string),
        (p.status.map_md5, osuTypes.string),
        (p.status.mods, osuTypes.i32),
        (p.status.mode.as_vanilla, osuTypes.u8),
        (p.status.map_id, osuTypes.i32),
        (rscore, osuTypes.i64),
        (gm_stats.acc / 100.0, osuTypes.f32),
        (gm_stats.plays, osuTypes.i32),
        (gm_stats.tscore, osuTypes.i64),
        (gm_stats.rank, osuTypes.i32),
        (pp, osuTypes.i16) # why not u16 peppy :(
    )

# packet id: 12
@cache
def logout(userID: int) -> bytes:
    return write(
        Packets.CHO_USER_LOGOUT,
        (userID, osuTypes.i32),
        (0, osuTypes.u8)
    )

# packet id: 13
@cache
def spectatorJoined(id: int) -> bytes:
    return write(
        Packets.CHO_SPECTATOR_JOINED,
        (id, osuTypes.i32)
    )

# packet id: 14
@cache
def spectatorLeft(id: int) -> bytes:
    return write(
        Packets.CHO_SPECTATOR_LEFT,
        (id, osuTypes.i32)
    )

# packet id: 15
# TODO: perhaps optimize this and match
# frames to be a bit more efficient, since
# they're literally spammed between clients.
def spectateFrames(data: bytes) -> bytes:
    return write(
        Packets.CHO_SPECTATE_FRAMES,
        (data, osuTypes.raw)
    )

# packet id: 19
@cache
def versionUpdate() -> bytes:
    return write(Packets.CHO_VERSION_UPDATE)

# packet id: 22
@cache
def spectatorCantSpectate(id: int) -> bytes:
    return write(
        Packets.CHO_SPECTATOR_CANT_SPECTATE,
        (id, osuTypes.i32)
    )

# packet id: 23
@cache
def getAttention() -> bytes:
    return write(Packets.CHO_GET_ATTENTION)

# packet id: 24
@lru_cache(maxsize=4)
def notification(msg: str) -> bytes:
    return write(
        Packets.CHO_NOTIFICATION,
        (msg, osuTypes.string)
    )

# packet id: 26
def updateMatch(m: Match, send_pw: bool = True) -> bytes:
    return write(
        Packets.CHO_UPDATE_MATCH,
        ((m, send_pw), osuTypes.match)
    )

# packet id: 27
def newMatch(m: Match) -> bytes:
    return write(
        Packets.CHO_NEW_MATCH,
        ((m, True), osuTypes.match)
    )

# packet id: 28
@cache
def disposeMatch(id: int) -> bytes:
    return write(
        Packets.CHO_DISPOSE_MATCH,
        (id, osuTypes.i32)
    )

# packet id: 34
@cache
def toggleBlockNonFriendPM() -> bytes:
    return write(Packets.CHO_TOGGLE_BLOCK_NON_FRIEND_DMS)

# packet id: 36
def matchJoinSuccess(m: Match) -> bytes:
    return write(
        Packets.CHO_MATCH_JOIN_SUCCESS,
        ((m, True), osuTypes.match)
    )

# packet id: 37
@cache
def matchJoinFail() -> bytes:
    return write(Packets.CHO_MATCH_JOIN_FAIL)

# packet id: 42
@cache
def fellowSpectatorJoined(id: int) -> bytes:
    return write(
        Packets.CHO_FELLOW_SPECTATOR_JOINED,
        (id, osuTypes.i32)
    )

# packet id: 43
@cache
def fellowSpectatorLeft(id: int) -> bytes:
    return write(
        Packets.CHO_FELLOW_SPECTATOR_LEFT,
        (id, osuTypes.i32)
    )

# packet id: 46
def matchStart(m: Match) -> bytes:
    return write(
        Packets.CHO_MATCH_START,
        ((m, True), osuTypes.match)
    )

# packet id: 48
# NOTE: this is actually unused, since it's
#       much faster to just send the bytes back
#       rather than parsing them.. though I might
#       end up doing it eventually for security reasons
def matchScoreUpdate(frame: ScoreFrame) -> bytes:
    return write(
        Packets.CHO_MATCH_SCORE_UPDATE,
        (frame, osuTypes.scoreframe)
    )

# packet id: 50
@cache
def matchTransferHost() -> bytes:
    return write(Packets.CHO_MATCH_TRANSFER_HOST)

# packet id: 53
@cache
def matchAllPlayerLoaded() -> bytes:
    return write(Packets.CHO_MATCH_ALL_PLAYERS_LOADED)

# packet id: 57
@cache
def matchPlayerFailed(slot_id: int) -> bytes:
    return write(
        Packets.CHO_MATCH_PLAYER_FAILED,
        (slot_id, osuTypes.i32)
    )

# packet id: 58
@cache
def matchComplete() -> bytes:
    return write(Packets.CHO_MATCH_COMPLETE)

# packet id: 61
@cache
def matchSkip() -> bytes:
    return write(Packets.CHO_MATCH_SKIP)

# packet id: 64
@lru_cache(maxsize=16)
def channelJoin(name: str) -> bytes:
    return write(
        Packets.CHO_CHANNEL_JOIN_SUCCESS,
        (name, osuTypes.string)
    )

# packet id: 65
@lru_cache(maxsize=8)
def channelInfo(name: str, topic: str,
                p_count: int) -> bytes:
    return write(
        Packets.CHO_CHANNEL_INFO,
        ((name, topic, p_count), osuTypes.channel)
    )

# packet id: 66
@lru_cache(maxsize=8)
def channelKick(name: str) -> bytes:
    return write(
        Packets.CHO_CHANNEL_KICK,
        (name, osuTypes.string)
    )

# packet id: 67
@lru_cache(maxsize=8)
def channelAutoJoin(name: str, topic: str,
                    p_count: int) -> bytes:
    return write(
        Packets.CHO_CHANNEL_AUTO_JOIN,
        ((name, topic, p_count), osuTypes.channel)
    )

# packet id: 69
#def beatmapInfoReply(maps: Sequence[BeatmapInfo]) -> bytes:
#    return write(
#        Packets.CHO_BEATMAP_INFO_REPLY,
#        (maps, osuTypes.mapInfoReply)
#    )

# packet id: 71
@cache
def banchoPrivileges(priv: int) -> bytes:
    return write(
        Packets.CHO_PRIVILEGES,
        (priv, osuTypes.i32)
    )

# packet id: 72
def friendsList(*friends) -> bytes:
    return write(
        Packets.CHO_FRIENDS_LIST,
        (friends, osuTypes.i32_list)
    )

# packet id: 75
@cache
def protocolVersion(ver: int) -> bytes:
    return write(
        Packets.CHO_PROTOCOL_VERSION,
        (ver, osuTypes.i32)
    )

# packet id: 76
@cache
def mainMenuIcon() -> bytes:
    return write(
        Packets.CHO_MAIN_MENU_ICON,
        ('|'.join(glob.config.menu_icon), osuTypes.string)
    )

# packet id: 80
# NOTE: deprecated
@cache
def monitor() -> bytes:
    # this is an older (now removed) 'anticheat' feature of the osu!
    # client; basically, it would do some checks (most likely for aqn),
    # screenshot your desktop (and send it to osu! servers), then trigger
    # the processlist to be sent to bancho as well (also now unused).

    # this doesn't work on newer clients, and i had no plans
    # of trying to put it to use - just coded for completion.
    return write(Packets.CHO_MONITOR)

# packet id: 81
@cache
def matchPlayerSkipped(pid: int) -> bytes:
    return write(
        Packets.CHO_MATCH_PLAYER_SKIPPED,
        (pid, osuTypes.i32)
    )

# since the bot is always online and is
# also automatically added to all player's
# friends list, their presence is requested
# *very* frequently; only build it once.
@cache
def botPresence():
    return write(
        Packets.CHO_USER_PRESENCE,
        (glob.bot.id, osuTypes.i32),
        (glob.bot.name, osuTypes.string),
        (-5 + 24, osuTypes.u8),
        (245, osuTypes.u8), # satellite provider
        (31, osuTypes.u8),
        (1234.0, osuTypes.f32), # send coordinates waaay
        (4321.0, osuTypes.f32), # off the map for the bot
        (0, osuTypes.i32)
    )

# packet id: 83
def userPresence(p: 'Player') -> bytes:
    if p is glob.bot:
        return botPresence()

    return write(
        Packets.CHO_USER_PRESENCE,
        (p.id, osuTypes.i32),
        (p.name, osuTypes.string),
        (p.utc_offset + 24, osuTypes.u8),
        (p.country[0], osuTypes.u8),
        (p.bancho_priv | (p.status.mode.as_vanilla << 5), osuTypes.u8),
        (p.location[1], osuTypes.f32), # long
        (p.location[0], osuTypes.f32), # lat
        (p.gm_stats.rank, osuTypes.i32)
    )

# packet id: 86
@cache
def restartServer(ms: int) -> bytes:
    return write(
        Packets.CHO_RESTART,
        (ms, osuTypes.i32)
    )

# packet id: 88
@lru_cache(maxsize=4)
def matchInvite(p: 'Player', t_name: str) -> bytes:
    msg = f'Come join my game: {p.match.embed}.'
    return write(
        Packets.CHO_MATCH_INVITE,
        ((p.name, msg, t_name, p.id), osuTypes.message)
    )

# packet id: 89
@cache
def channelInfoEnd() -> bytes:
    return write(Packets.CHO_CHANNEL_INFO_END)

# packet id: 91
def matchChangePassword(new: str) -> bytes:
    return write(
        Packets.CHO_MATCH_CHANGE_PASSWORD,
        (new, osuTypes.string)
    )

# packet id: 92
def silenceEnd(delta: int) -> bytes:
    return write(
        Packets.CHO_SILENCE_END,
        (delta, osuTypes.i32)
    )

# packet id: 94
@cache
def userSilenced(pid: int) -> bytes:
    return write(
        Packets.CHO_USER_SILENCED,
        (pid, osuTypes.i32)
    )

""" not sure why 95 & 96 exist? unused in gulag """

# packet id: 95
@cache
def userPresenceSingle(pid: int) -> bytes:
    return write(
        Packets.CHO_USER_PRESENCE_SINGLE,
        (pid, osuTypes.i32)
    )

# packet id: 96
def userPresenceBundle(pid_list: list[int]) -> bytes:
    return write(
        Packets.CHO_USER_PRESENCE_BUNDLE,
        (pid_list, osuTypes.i32_list)
    )

# packet id: 100
def userDMBlocked(target: str) -> bytes:
    return write(
        Packets.CHO_USER_DM_BLOCKED,
        (('', '', target, 0), osuTypes.message)
    )

# packet id: 101
def targetSilenced(target: str) -> bytes:
    return write(
        Packets.CHO_TARGET_IS_SILENCED,
        (('', '', target, 0), osuTypes.message)
    )

# packet id: 102
@cache
def versionUpdateForced() -> bytes:
    return write(Packets.CHO_VERSION_UPDATE_FORCED)

# packet id: 103
def switchServer(t: int) -> bytes:
    # increment endpoint index if
    # idletime >= t && match == null
    return write(
        Packets.CHO_SWITCH_SERVER,
        (t, osuTypes.i32)
    )

# packet id: 104
@cache
def accountRestricted() -> bytes:
    return write(Packets.CHO_ACCOUNT_RESTRICTED)

# packet id: 105
# NOTE: deprecated
def RTX(msg: str) -> bytes:
    # bit of a weird one, sends a request to the client
    # to show some visual effects on screen for 5 seconds:
    # - black screen, freezes game, beeps loudly.
    # within the next 3-8 seconds at random.
    return write(
        Packets.CHO_RTX,
        (msg, osuTypes.string)
    )

# packet id: 106
@cache
def matchAbort() -> bytes:
    return write(Packets.CHO_MATCH_ABORT)

# packet id: 107
def switchTournamentServer(ip: str) -> bytes:
    # the client only reads the string if it's
    # not on the client's normal endpoints,
    # but we can send it either way xd.
    return write(
        Packets.CHO_SWITCH_TOURNAMENT_SERVER,
        (ip, osuTypes.string)
    )
