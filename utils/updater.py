# -*- coding: utf-8 -*-

# this file is for management of gulag version updates;
# it will automatically keep track of your running version,
# and when it detects a change, it will apply any nescessary
# changes to your sql database & keep cmyui_pkg up to date.

import re
import importlib.metadata
from pathlib import Path
from typing import Optional

import aiomysql

from cmyui.logging import Ansi
from cmyui.logging import log
from cmyui.logging import printc
from cmyui.version import Version
from pip._internal.cli.main import main as pip_main

from objects import glob

__all__ = ('Updater',)

SQL_UPDATES_FILE = Path.cwd() / 'ext/updates.sql'

VERSION_RGX = re.compile(r'^# v(?P<ver>\d+\.\d+\.\d+)$')

class Updater:
    def __init__(self, version: Version) -> None:
        self.version = version

    async def run(self) -> None:
        """Prepare, and run the updater."""
        prev_ver = await self.get_prev_version()

        if not prev_ver:
            # first time running the server.
            prev_ver = self.version

            printc('\n'.join([
                'Welcome to sutekina!',
                'If you have any issues with the server,',
                'feel free to join our public Discord :)',
                '',
                'https://discord.gg/ShEQgUx',
                'Enjoy the server!'
            ]), Ansi.LCYAN)

        if glob.has_internet:
            await self._update_cmyui() # pip install -U cmyui
        await self._update_sql(prev_ver) # run updates.sql

    @staticmethod
    async def get_prev_version() -> Optional[Version]:
        """Get the last launched version of the server."""
        res = await glob.db.fetch(
            'SELECT ver_major, ver_minor, ver_micro '
            'FROM startups ORDER BY datetime DESC LIMIT 1',
            _dict=False # get tuple
        )

        if res:
            return Version(*map(int, res))

    async def log_startup(self) -> None:
        """Log this startup to sql for future use."""
        ver = self.version
        await glob.db.execute(
            'INSERT INTO startups '
            '(ver_major, ver_minor, ver_micro, datetime) '
            'VALUES (%s, %s, %s, NOW())',
            [ver.major, ver.minor, ver.micro]
        )

    async def _get_latest_cmyui(self) -> Version:
        """Get the latest version release of cmyui_pkg from pypi."""
        url = 'https://pypi.org/pypi/cmyui/json'
        async with glob.http.get(url) as resp:
            if not resp or resp.status != 200:
                return self.version

            if not (json := await resp.json()):
                return self.version

            # return most recent release version
            return Version.from_str(json['info']['version'])

    async def _update_cmyui(self) -> None:
        """Check if cmyui_pkg has a newer release; update if available."""
        module_ver = Version.from_str(importlib.metadata.version('cmyui'))
        latest_ver = await self._get_latest_cmyui()

        if module_ver < latest_ver:
            # package is not up to date; update it.
            log(f'Updating cmyui_pkg (v{module_ver!r} -> '
                                    f'v{latest_ver!r}).', Ansi.LMAGENTA)
            pip_main(['install', '-Uq', 'cmyui']) # Update quiet

    async def _update_sql(self, prev_version: Version) -> None:
        """Apply any structural changes to sql since the last startup."""
        if self.version == prev_version:
            # already up to date.
            return

        # version changed; there may be sql changes.
        content = SQL_UPDATES_FILE.read_text()

        queries = []
        q_lines = []

        current_ver = None


        for line in content.splitlines():
            if not line:
                continue

            if line.startswith('#'):
                # may be normal comment or new version
                if r_match := VERSION_RGX.fullmatch(line):
                    current_ver = Version.from_str(r_match['ver'])

                continue
            elif not current_ver:
                continue

            # we only need the updates between the
            # previous and new version of the server.
            if prev_version < current_ver <= self.version:
                if line.endswith(';'):
                    if q_lines:
                        q_lines.append(line)
                        queries.append(' '.join(q_lines))
                        q_lines = []
                    else:
                        queries.append(line)
                else:
                    q_lines.append(line)

        if not queries:
            return

        log(f'Updating sql (v{prev_version!r} -> '
                          f'v{self.version!r}).', Ansi.LMAGENTA)

        updated = False

        # NOTE: this using a transaction is pretty pointless with mysql since
        # any structural changes to tables will implciticly commit the changes.
        # https://dev.mysql.com/doc/refman/5.7/en/implicit-commit.html
        async with glob.db.pool.acquire() as conn:
            async with conn.cursor() as db_cursor:
                await conn.begin()
                for query in queries:
                    try:
                        await db_cursor.execute(query)
                    except aiomysql.MySQLError:
                        await conn.rollback()
                        break
                else:
                    # all queries ran
                    # without problems.
                    await conn.commit()
                    updated = True

        if not updated:
            log(f'Failed: {query}', Ansi.GRAY)
            log("SQL failed to update - unless you've been "
                "modifying sql and know what caused this, "
                "please please contact cmyui#0425.", Ansi.LRED)

            await glob.app.after_serving()
            raise KeyboardInterrupt

    # TODO _update_config?
