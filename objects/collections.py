# TODO: there is still a lot of inconsistency
# in a lot of these classes; needs refactor.
import asyncio
from typing import Any
from typing import Iterator
from typing import Optional
from typing import overload
from typing import Sequence
from typing import Union

import aiomysql
from cmyui.logging import Ansi
from cmyui.logging import log

import misc.utils
from constants.privileges import Privileges
from misc.utils import make_safe_name
from objects import glob
from objects.achievement import Achievement
from objects.channel import Channel
from objects.clan import Clan
from objects.clan import ClanPrivileges
from objects.match import MapPool
from objects.match import Match
from objects.player import Player

__all__ = (
    "Channels",
    "Matches",
    "Players",
    "MapPools",
    "Clans",
    "initialize_ram_caches",
)

# TODO: decorator for these collections which automatically
# adds debugging to their append/remove/insert/extend methods.


class Channels(list[Channel]):
    """The currently active chat channels on the server."""

    def __iter__(self) -> Iterator[Channel]:
        return super().__iter__()

    def __contains__(self, o: Union[Channel, str]) -> bool:
        """Check whether internal list contains `o`."""
        # Allow string to be passed to compare vs. name.
        if isinstance(o, str):
            return o in map(lambda c: c.name, self)
        else:
            return super().__contains__(o)

    @overload
    def __getitem__(self, index: int) -> Channel:
        ...

    @overload
    def __getitem__(self, index: str) -> Channel:
        ...

    @overload
    def __getitem__(self, index: slice) -> list[Channel]:
        ...

    def __getitem__(
        self,
        index: Union[int, slice, str],
    ) -> Union[Channel, list[Channel]]:
        # XXX: can be either a string (to get by name),
        # or a slice, for indexing the internal array.
        if isinstance(index, str):
            return self.get_by_name(index)  # type: ignore
        else:
            return super().__getitem__(index)

    def __repr__(self) -> str:
        # XXX: we use the "real" name, aka
        # #multi_1 instead of #multiplayer
        # #spect_1 instead of #spectator.
        return f'[{", ".join(c._name for c in self)}]'

    def get_by_name(self, name: str) -> Optional[Channel]:
        """Get a channel from the list by `name`."""
        for c in self:
            if c._name == name:
                return c

    def append(self, c: Channel) -> None:
        """Append `c` to the list."""
        super().append(c)

        if glob.app.debug:
            log(f"{c} added to channels list.")

    def remove(self, c: Channel) -> None:
        """Remove `c` from the list."""
        super().remove(c)

        if glob.app.debug:
            log(f"{c} removed from channels list.")

    @classmethod
    async def prepare(cls, db_cursor: aiomysql.DictCursor) -> "Channels":
        """Fetch data from sql & return; preparing to run the server."""
        log("Fetching channels from sql.", Ansi.LCYAN)
        await db_cursor.execute("SELECT * FROM channels")
        return cls(
            [
                Channel(
                    name=row["name"],
                    topic=row["topic"],
                    read_priv=Privileges(row["read_priv"]),
                    write_priv=Privileges(row["write_priv"]),
                    auto_join=row["auto_join"] == 1,
                )
                async for row in db_cursor
            ],
        )


class Matches(list[Optional[Match]]):
    """The currently active multiplayer matches on the server."""

    def __init__(self) -> None:
        super().__init__([None] * glob.config.max_multi_matches)

    def __iter__(self) -> Iterator[Optional[Match]]:
        return super().__iter__()

    def __repr__(self) -> str:
        return f'[{", ".join([m.name for m in self if m])}]'

    def __iter__(self) -> Iterator['Match']:
        return super().__iter__()

    def __repr__(self) -> str:
        return f'[{", ".join(m.name for m in self if m)}]'

    def get_free(self) -> Optional[int]:
        """Return the first free match id from `self`."""
        for idx, m in enumerate(self):
            if m is None:
                return idx

    def append(self, m: Match) -> bool:
        """Append `m` to the list."""
        if (free := self.get_free()) is not None:
            # set the id of the match to the lowest available free.
            m.id = free
            self[free] = m

            if glob.app.debug:
                log(f"{m} added to matches list.")

            return True
        else:
            log(f"Match list is full! Could not add {m}.")
            return False

    def remove(self, m: Match) -> None:
        """Remove `m` from the list."""
        for i, _m in enumerate(self):
            if m is _m:
                self[i] = None
                break

        if glob.app.debug:
            log(f"{m} removed from matches list.")


class Players(list[Player]):
    """The currently active players on the server."""

    __slots__ = ("_lock",)

    def __init__(self, *args, **kwargs):
        self._lock = asyncio.Lock()
        super().__init__(*args, **kwargs)

    def __iter__(self) -> Iterator[Player]:
        return super().__iter__()

    def __contains__(self, p: Union[Player, str]) -> bool:
        # allow us to either pass in the player
        # obj, or the player name as a string.
        if isinstance(p, str):
            return p in [player.name for player in self]
        else:
            return super().__contains__(p)

    def __repr__(self) -> str:
        return f'[{", ".join(map(repr, self))}]'

    @property
    def ids(self) -> set[int]:
        """Return a set of the current ids in the list."""
        return {p.id for p in self}

    @property
    def staff(self) -> set[Player]:
        """Return a set of the current staff online."""
        return {p for p in self if p.priv & Privileges.STAFF}

    @property
    def restricted(self) -> set[Player]:
        """Return a set of the current restricted players."""
        return {p for p in self if not p.priv & Privileges.NORMAL}

    @property
    def unrestricted(self) -> set[Player]:
        """Return a set of the current unrestricted players."""
        return {p for p in self if p.priv & Privileges.NORMAL}

    def enqueue(self, data: bytes, immune: Sequence[Player] = []) -> None:
        """Enqueue `data` to all players, except for those in `immune`."""
        for p in self:
            if p not in immune:
                p.enqueue(data)

    @staticmethod
    def _parse_attr(kwargs: dict[str, Any]) -> tuple[str, object]:
        """Get first matched attr & val from input kwargs. Used in get() methods."""
        for attr in ("token", "id", "name"):
            if (val := kwargs.pop(attr, None)) is not None:
                if attr == "name":
                    attr = "safe_name"
                    val = make_safe_name(val)

                return attr, val
        else:
            raise ValueError("Incorrect call to Players.get()")

    def get(self, **kwargs: object) -> Optional[Player]:
        """Get a player by token, id, or name from cache."""
        attr, val = self._parse_attr(kwargs)

        for p in self:
            if getattr(p, attr) == val:
                return p

    async def get_sql(self, **kwargs: object) -> Optional[Player]:
        """Get a player by token, id, or name from sql."""
        attr, val = self._parse_attr(kwargs)

        # try to get from sql.
        res = await glob.db.fetch(
            "SELECT id, name, priv, pw_bcrypt, "
            "silence_end, clan_id, clan_priv, api_key "
            f"FROM users WHERE {attr} = %s",
            [val],
        )

        if not res:
            return

        # encode pw_bcrypt from str -> bytes.
        res["pw_bcrypt"] = res["pw_bcrypt"].encode()

        if res["clan_id"] != 0:
            res["clan"] = glob.clans.get(id=res["clan_id"])
            res["clan_priv"] = ClanPrivileges(res["clan_priv"])
        else:
            res["clan"] = res["clan_priv"] = None

        return Player(**res, token="")

    async def from_cache_or_sql(self, **kwargs: object) -> Optional[Player]:
        """Try to get player from cache, or sql as fallback."""
        if p := self.get(**kwargs):
            return p
        elif p := await self.get_sql(**kwargs):
            return p

    async def from_login(
        self,
        name: str,
        pw_md5: str,
        sql: bool = False,
    ) -> Optional[Player]:
        """Return a player with a given name & pw_md5, from cache or sql."""
        if not (p := self.get(name=name)):
            if not sql:  # not to fetch from sql.
                return

            if not (p := await self.get_sql(name=name)):
                # no player found in sql either.
                return

        if glob.cache["bcrypt"][p.pw_bcrypt] == pw_md5.encode():
            return p

    def append(self, p: Player) -> None:
        """Append `p` to the list."""
        if p in self:
            if glob.app.debug:
                log(f"{p} double-added to global player list?")
            return

        super().append(p)

    def remove(self, p: Player) -> None:
        """Remove `p` from the list."""
        if p not in self:
            if glob.app.debug:
                log(f"{p} removed from player list when not online?")
            return

        super().remove(p)


class MapPools(list[MapPool]):
    """The currently active mappools on the server."""

    def __iter__(self) -> Iterator[MapPool]:
        return super().__iter__()

    @overload
    def __getitem__(self, index: int) -> MapPool:
        ...

    @overload
    def __getitem__(self, index: str) -> MapPool:
        ...

    @overload
    def __getitem__(self, index: slice) -> list[MapPool]:
        ...

    def __getitem__(
        self,
        index: Union[int, slice, str],
    ) -> Union[MapPool, list[MapPool]]:
        """Allow slicing by either a string (for name), or slice."""
        if isinstance(index, str):
            return self.get_by_name(index)  # type: ignore
        else:
            return super().__getitem__(index)

    def __contains__(self, o: Union[MapPool, str]) -> bool:
        """Check whether internal list contains `o`."""
        # Allow string to be passed to compare vs. name.
        if isinstance(o, str):
            return o in [p.name for p in self]
        else:
            return o in self

    def get_by_name(self, name: str) -> Optional[MapPool]:
        """Get a pool from the list by `name`."""
        for p in self:
            if p.name == name:
                return p

    def append(self, mp: MapPool) -> None:
        """Append `mp` to the list."""
        super().append(mp)

        if glob.app.debug:
            log(f"{mp} added to mappools list.")

    def remove(self, mp: MapPool) -> None:
        """Remove `mp` from the list."""
        super().remove(mp)

        if glob.app.debug:
            log(f"{mp} removed from mappools list.")

    @classmethod
    async def prepare(cls, db_cursor: aiomysql.DictCursor) -> "MapPools":
        """Fetch data from sql & return; preparing to run the server."""
        log("Fetching mappools from sql.", Ansi.LCYAN)
        await db_cursor.execute("SELECT * FROM tourney_pools")
        obj = cls(
            [
                MapPool(
                    id=row["id"],
                    name=row["name"],
                    created_at=row["created_at"],
                    created_by=await glob.players.from_cache_or_sql(
                        id=row["created_by"],
                    ),
                )
                async for row in db_cursor
            ],
        )

        for pool in obj:
            await pool.maps_from_sql(db_cursor)

        return obj


class Clans(list[Clan]):
    """The currently active clans on the server."""

    def __iter__(self) -> Iterator[Clan]:
        return super().__iter__()

    @overload
    def __getitem__(self, index: int) -> Clan:
        ...

    @overload
    def __getitem__(self, index: str) -> Clan:
        ...

    @overload
    def __getitem__(self, index: slice) -> list[Clan]:
        ...

    def __getitem__(self, index):
        """Allow slicing by either a string (for name), or slice."""
        if isinstance(index, str):
            return self.get(name=index)
        else:
            return super().__getitem__(index)

    def __contains__(self, o: Union[Clan, str]) -> bool:
        """Check whether internal list contains `o`."""
        # Allow string to be passed to compare vs. name.
        if isinstance(o, str):
            return o in [c.name for c in self]
        else:
            return o in self

    def get(self, **kwargs: object) -> Optional[Clan]:
        """Get a clan by name, tag, or id."""
        for attr in ("name", "tag", "id"):
            if val := kwargs.pop(attr, None):
                break
        else:
            raise ValueError("Incorrect call to Clans.get()")

        for c in self:
            if getattr(c, attr) == val:
                return c

    def append(self, c: Clan) -> None:
        """Append `c` to the list."""
        super().append(c)

        if glob.app.debug:
            log(f"{c} added to clans list.")

    def remove(self, c: Clan) -> None:
        """Remove `m` from the list."""
        super().remove(c)

        if glob.app.debug:
            log(f"{c} removed from clans list.")

    @classmethod
    async def prepare(cls, db_cursor: aiomysql.DictCursor) -> "Clans":
        """Fetch data from sql & return; preparing to run the server."""
        log("Fetching clans from sql.", Ansi.LCYAN)
        await db_cursor.execute("SELECT * FROM clans")
        obj = cls([Clan(**row) async for row in db_cursor])

        for clan in obj:
            await clan.members_from_sql(db_cursor)

        return obj


async def initialize_ram_caches(db_cursor: aiomysql.DictCursor) -> None:
    """Setup & cache the global collections before listening for connections."""
    # dynamic (active) sets, only in ram
    glob.matches = Matches()
    glob.players = Players()

    # static (inactive) sets, in ram & sql
    glob.channels = await Channels.prepare(db_cursor)
    glob.clans = await Clans.prepare(db_cursor)
    glob.pools = await MapPools.prepare(db_cursor)

    bot_name = await misc.utils.fetch_bot_name(db_cursor)

    # create bot & add it to online players
    glob.bot = Player(
        id=1,
        name=bot_name,
        login_time=float(0x7FFFFFFF),  # (never auto-dc)
        priv=Privileges.NORMAL,
        bot_client=True,
    )
    glob.players.append(glob.bot)

    # global achievements (sorted by vn gamemodes)
    glob.achievements = []

    await db_cursor.execute("SELECT * FROM achievements")
    async for row in db_cursor:
        # NOTE: achievement conditions are stored as stringified python
        # expressions in the database to allow for extensive customizability.
        condition = eval(f'lambda score, mode_vn: {row.pop("cond")}')
        achievement = Achievement(**row, cond=condition)

        glob.achievements.append(achievement)

    # static api keys
    await db_cursor.execute("SELECT id, api_key FROM users WHERE api_key IS NOT NULL")

    glob.api_keys = {row["api_key"]: row["id"] async for row in db_cursor}
