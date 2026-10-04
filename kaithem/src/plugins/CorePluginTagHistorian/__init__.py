# SPDX-License-Identifier: GPL-3.0-or-later

import datetime
import getpass
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
import traceback
import weakref
from collections import deque
from collections.abc import Callable
from urllib.parse import quote

import dateutil.parser
import pytz
import quart
import structlog
from scullery import scheduling

from kaithem.api import tags as tagsapi
from kaithem.api.modules import mutable_copy_resource
from kaithem.src import (
    dialogs,
    directories,
    messagebus,
    modules_state,
    pages,
    quart_app,
    tagpoints,
)

oldlogdir = os.path.join(directories.vardir, "logs")
logdir = directories.logdir
ramdbfile = (
    "/dev/shm/"
    + socket.gethostname()
    + "-"
    + getpass.getuser()
    + "-taghistory.sqlite"
)


logger = structlog.get_logger(__name__)

if not os.path.exists(logdir):
    try:
        os.makedirs(logdir)
    except Exception:
        logger.exception("Can't make log dir")

# Build a filename including the hostname and user.   This is because SQLite may not be happy to be involved with SyncThing.
# For that reason, should someone get the bright idea to sync a kaithem vardir, we must keep the history databases single-writer.

# Plus, there is nothing in the DB itself to tell us who wrote it, so this is very convenient.
historyFilemame = (
    socket.gethostname() + "-" + getpass.getuser() + "-taghistory.sqlite"
)

on_disk_history_db_path = os.path.join(logdir, historyFilemame)


def iso_now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ts_to_iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


class TagLogger:
    """Base class for the object associated with one tag point for logging
    a specific type(min,max,avg,etc) of data from that tag"""

    accumType = "latest"
    defaultAccum = 0

    def __init__(
        self,
        tag: tagsapi.GenericTagPointClass,
        interval: int | float,
        history_length=3 * 30 * 24 * 3600,
        target="disk",
    ):
        # We can have purely ram file based logging
        if target == "disk":
            self.h = historian
            self.filename = on_disk_history_db_path
        elif target == "ram":
            self.h = ramHistorian
            self.filename = ramdbfile
        else:
            raise ValueError("target not supported: " + target)

        self.target = target

        self.accumVal = self.defaultAccum
        self.accumCount = 0
        self.accumTime = 0
        self.history_length = history_length

        self.lastLogged = 0
        self.interval = interval

        self.getChannelID(tag)

        with self.h.lock:
            self.h.children[id(self)] = weakref.ref(self)

        tag.subscribe(self.accumulate)

        # Log immediately when we setup logger settings, so we have data to work with before it changes

        v, t, a = tag.get_vta()
        self.accumulate(v, t, a)

    def insertData(self, d):
        self.h.insertData(d)

    def getDataRange(self, minTime, maxTime, maxRecords=10000):
        return self.h.getDataRange(self.chID, minTime, maxTime, maxRecords)

    def getRecent(self, minTime, maxTime, maxRecords=10000):
        return self.h.getRecent(self.chID, minTime, maxTime, maxRecords)

    def __del__(self):
        try:
            if id(self) in self.h.children:
                del self.h.children[id(self)]
                logger.warning("Logger was deleted without being closed")
        # Del always does nuisance stuff at shutdown
        except Exception:
            pass

    def close(self):
        with self.h.lock:
            del self.h.children[id(self)]

    def accumulate(self, value, time, annotation):
        "Only ever called by the tag"
        self.accumVal = value
        self.accumTime = time
        self.accumCount = 1
        if isinstance(value, int | float | bytes):
            pass

        elif isinstance(value, str):
            value = value[: 1024 * 128]

        elif isinstance(value, list | dict | tuple):
            value = json.dumps(value)

        self.flush()

    # Only call from accumulate or from within the historian, which will use the taglock to call this.
    def flush(self, force=False):
        # Nothing accumulated to log.
        if not self.accumCount:
            return

        # Ratelimit how often we log, continue accumulating if nothing to log.
        if not force:
            if self.lastLogged > time.time() - self.interval:
                return

        self.insertData((self.chID, self.accumTime, self.accumVal))
        self.lastLogged = time.time()
        self.accumCount = 0
        self.accumVal = self.defaultAccum

    def getChannelID(self, tag):
        # Either get our stored channel name, or create a new onw
        with self.h.lock:
            # Have to make our own, we are in a new thread now.
            conn = sqlite3.Connection(self.filename)
            conn.row_factory = sqlite3.Row

            c = conn.cursor()
            c.execute(
                "SELECT id,name,unit,accumulate from channel WHERE name=?",
                (tag.name,),
            )
            self.chID = None

            if not isinstance(tag.unit, str):
                raise ValueError("bad tag unit " + str(tag.unit))

            if not isinstance(self.accumType, str):
                raise ValueError("bad tag accum " + str(self.accumType))

            for i in c:
                if (
                    i["name"] == tag.name
                    and i["unit"] == tag.unit
                    and i["accumulate"] == self.accumType
                ):
                    self.chID = i["id"]

            if not self.chID:
                conn.execute(
                    "INSERT INTO channel VALUES (?,?,?,?,?)",
                    (None, tag.name, tag.unit, self.accumType, "{}"),
                )
                conn.commit()

            c = conn.cursor()
            c.execute(
                "SELECT id from channel WHERE name=? AND unit=? AND accumulate=?",
                (tag.name, tag.unit, self.accumType),
            )
            self.chID = c.fetchone()[0]

            conn.close()


class AverageLogger(TagLogger):
    accumType = "mean"

    def accumulate(self, value, time, annotation):
        "Only ever called by the tag"
        self.accumVal += value
        self.accumTime += time
        self.accumCount += 1
        self.flush()

    # Only call from accumulate or from within the historian, which will use the taglock to call this.
    def flush(self, force=False):
        # Nothing accumulated to log.
        if not self.accumCount:
            return

        # Ratelimit how often we log, continue accumulating if nothing to log.
        if not force:
            if self.lastLogged > time.time() - self.interval:
                return

        self.insertData(
            (
                self.chID,
                (self.accumTime / self.accumCount),
                self.accumVal / self.accumCount,
            )
        )
        self.lastLogged = time.time()
        self.accumCount = 0
        self.accumVal = 0
        self.accumTime = 0


class MinLogger(TagLogger):
    accumType = "min"
    defaultAccum = 10**18

    def accumulate(self, value, time, annotation):
        "Only ever called by the tag"
        self.accumVal = min(self.accumVal, value)
        self.accumTime += time
        self.accumCount += 1

        self.flush()

    # Only call from accumulate or from within the historian, which will use the taglock to call this.
    def flush(self, force=False):
        # Nothing accumulated to log.
        if not self.accumCount:
            return

        # Ratelimit how often we log, continue accumulating if nothing to log.
        if not force:
            if self.lastLogged > time.time() - self.interval:
                return

        self.insertData(
            (self.chID, (self.accumTime / self.accumCount), self.accumVal)
        )
        self.lastLogged = time.time()
        self.accumCount = 0
        self.accumVal = 10**18
        self.accumTime = 0


class MaxLogger(MinLogger):
    accumType = "max"
    defaultAccum = -(10**18)

    def accumulate(self, value, time, annotation):
        "Only ever called by the tag"
        self.accumVal = max(self.accumVal, value)
        self.accumTime += time
        self.accumCount += 1

        self.flush()

    # Only call from accumulate or from within the historian, which will use the taglock to call this.
    def flush(self, force=False):
        # Nothing accumulated to log.
        if not self.accumCount:
            return

        # Ratelimit how often we log, continue accumulating if nothing to log.
        if not force:
            if self.lastLogged > time.time() - self.interval:
                return

        self.insertData(
            (self.chID, (self.accumTime / self.accumCount), self.accumVal)
        )
        self.lastLogged = time.time()
        self.accumCount = 0
        self.accumVal = -(10**18)
        self.accumTime = 0


accumTypes = {
    "latest": TagLogger,
    "mean": AverageLogger,
    "max": MaxLogger,
    "min": MinLogger,
}


class TagHistorian:
    # Generated puely randomly
    appID = 707898159

    def __init__(self, file):
        newfile = not os.path.exists(file)

        self.lock = threading.RLock()
        self.filename = file
        self.journalFilename = file + ".pending.sqlite"
        self.needsStartupReconcile = False
        self.children: dict[
            int, weakref.ReferenceType[TagLogger]
        ] = {}

        self.history = sqlite3.Connection(file)
        self.history.row_factory = sqlite3.Row

        try:
            self._ensureSchema(self.history, newfile)
        except Exception:
            logger.exception(
                "Tag history db unreadable, archiving and recreating"
            )
            self.history.close()
            self._archiveCorruptDb(file)
            newfile = True
            self.history = sqlite3.Connection(file)
            self.history.row_factory = sqlite3.Row
            self._ensureSchema(self.history, True)

        # Unacked samples in arrival order: (channelID, epochTime, value)
        self.pending_data_items: deque[tuple] = deque()

        # Immutable snapshot of the batch currently being written.
        self.inflight: tuple[tuple, ...] = ()

        # Journal connections are short-lived, like every other connection
        # here, so they work from any (e.g. messagebus worker) thread.
        self._initJournal()

        # Batches that were committed but whose ack/cleanup failed are
        # dropped; everything else is loaded for replay, in order.
        try:
            recovered = self._reconcileJournal()
            self.pending_data_items.extend(recovered)
        except Exception:
            logger.exception("Startup journal reconcile failed; will retry")
            self.needsStartupReconcile = True
            recovered = []

        self.history.close()

        self.lastFlushed = time.time()

        self.lastGarbageCollected = 0

        self.flushInterval = 10 * 60

        self.gcInterval = 3600 * 2

        # Plain function (held by this instance), not an ephemeral bound
        # method, so messagebus's weakref stays alive for the subscription.
        def forceFlushStable(*a):
            self.flush(True)

        self.forceFlushStable = forceFlushStable

        messagebus.subscribe("/system/save", self.forceFlushStable)
        messagebus.subscribe("/system/shutdown", self.forceFlushStable)

        def f():
            self.flush()

        self.flusher_f = f
        self.flusher = scheduling.scheduler.every_minute(f)

        # Continue unacknowledged batches promptly after a restart.
        self.startup_flush = None
        if recovered:
            self.startup_flush = scheduling.scheduler.schedule(
                self.flush, time.time() + 5
            )

    def _ensureSchema(self, conn, newfile):
        if newfile:
            conn.execute("PRAGMA application_id = 707898159")

        conn.execute(
            """CREATE TABLE IF NOT EXISTS channel  (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name text, unit text, accumulate text, metadata text)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS record  (channel INTEGER,
            time TEXT, value REAL, FOREIGN KEY(channel) REFERENCES channel(id))"""
        )
        # Needed for idempotent replay, range merges and retention checks.
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_record_channel_time
            ON record(channel, time)"""
        )
        conn.execute(
            """CREATE VIEW IF NOT EXISTS SimpleViewLocalTime AS SELECT
            channel.name as Channel,
            channel.accumulate as Type,
            record.time as LocalTime,
              record.value as Value,
              channel.unit as Unit FROM record INNER JOIN channel ON channel.id = record.channel;"""
        )
        conn.execute(
            """CREATE VIEW IF NOT EXISTS SimpleViewUTC AS SELECT
              channel.name as Channel,
              channel.accumulate as Type,
              record.time as UTCTime,
              record.value as Value,
              channel.unit as Unit FROM record INNER JOIN channel ON channel.id = record.channel;"""
        )

        # TODO: Legacy compatibility
        try:
            conn.execute("""ALTER TABLE record
                RENAME COLUMN tagName TO name;""")
            conn.execute("""ALTER TABLE channel
                    RENAME COLUMN tagName TO name;""")
        except Exception:
            pass

        conn.commit()

    # ----- Persistent recovery journal ------------------------------------

    def _jconn(self):
        conn = sqlite3.Connection(self.journalFilename)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pendingbatch
            (batch INTEGER, seq INTEGER, channel INTEGER,
             ts REAL, value REAL, PRIMARY KEY(batch, seq))"""
        )
        return conn

    def _initJournal(self):
        try:
            conn = self._jconn()
            conn.commit()
            conn.close()
        except Exception:
            # A broken journal must not take logging down; archive it.
            logger.exception("Recovery journal unreadable, recreating")
            try:
                self._archiveCorruptDb(self.journalFilename)
            except Exception:
                logger.exception("Could not archive bad recovery journal")
            conn = self._jconn()
            conn.commit()
            conn.close()

    def _journalAdd(self, items):
        "Durably record the batch BEFORE touching the main db."
        conn = self._jconn()
        try:
            c = conn.execute(
                "SELECT COALESCE(max(batch),0)+1 FROM pendingbatch"
            )
            batchid = c.fetchone()[0]
            conn.executemany(
                "INSERT INTO pendingbatch VALUES (?,?,?,?,?)",
                [
                    (batchid, seq, i[0], i[1], i[2])
                    for seq, i in enumerate(items)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return batchid

    def _journalClear(self, batchid):
        "Best effort; _reconcileJournal is the durable backstop."
        conn = self._jconn()
        try:
            conn.execute(
                "DELETE FROM pendingbatch WHERE batch=?", (batchid,)
            )
            conn.commit()
        finally:
            conn.close()

    def _rowsPresent(self, rows):
        """rows: iterable of (channel, epochTs, value).
        True only if every (channel, iso time, value) is in record."""
        byChannel = {}
        for channel, ts, value in rows:
            byChannel.setdefault(channel, []).append((ts, value))

        conn = sqlite3.Connection(self.filename)
        try:
            for channel, chRows in byChannel.items():
                lo = ts_to_iso(min(i[0] for i in chRows))
                hi = ts_to_iso(max(i[0] for i in chRows))
                c = conn.execute(
                    "SELECT time, value FROM record "
                    "WHERE channel=? AND time>=? AND time<=?",
                    (channel, lo, hi),
                )
                existing = {(i[0], i[1]) for i in c}
                for ts, value in chRows:
                    if (ts_to_iso(ts), value) not in existing:
                        return False
        finally:
            conn.close()
        return True

    def _reconcileJournal(self):
        """Drop journal batches whose rows are already committed (commit
        succeeded but ack/cleanup failed). Return remaining rows, in journal
        order, as (channel, epoch, value)."""
        conn = self._jconn()
        try:
            c = conn.execute(
                "SELECT batch, seq, channel, ts, value FROM pendingbatch "
                "ORDER BY batch, seq"
            )
            rows = c.fetchall()
            if not rows:
                return []

            byBatch = {}
            order = []
            for batch, seq, channel, ts, value in rows:
                if batch not in byBatch:
                    byBatch[batch] = []
                    order.append(batch)
                byBatch[batch].append((seq, channel, ts, value))

            recovered = []
            for batch in order:
                items = byBatch[batch]
                present = self._rowsPresent(
                    [(ch, ts, v) for seq, ch, ts, v in items]
                )
                if present:
                    try:
                        conn.execute(
                            "DELETE FROM pendingbatch WHERE batch=?",
                            (batch,),
                        )
                        conn.commit()
                    except Exception:
                        logger.exception(
                            "Could not clear confirmed batch from journal"
                        )
                else:
                    for seq, channel, ts, value in items:
                        recovered.append((channel, ts, value))
        finally:
            conn.close()
        return recovered

    # ----- Corruption handling --------------------------------------------

    def _archiveCorruptDb(self, file):
        archive = file + ".error_archived"
        if os.path.exists(archive):
            archive = file + ".error_archived_" + iso_now().replace(":", "")
        shutil.move(file, archive)
        return archive

    def _archiveAndRebuild(self):
        """Move the corrupt db aside, create a fresh one, and salvage the
        channel table so stored channel ids keep matching pending samples."""
        archive = self._archiveCorruptDb(self.filename)

        fresh = sqlite3.Connection(self.filename)
        fresh.row_factory = sqlite3.Row
        self._ensureSchema(fresh, True)

        try:
            fresh.execute("ATTACH ? AS olddb", (archive,))
            fresh.execute(
                """INSERT OR IGNORE INTO channel
                (name, unit, accumulate, metadata)
                SELECT name, unit, accumulate, metadata
                FROM olddb.channel"""
            )
            fresh.commit()
            fresh.execute("DETACH olddb")
        except Exception:
            # Journal batches are retained; samples stay recoverable even if
            # channel mapping could not be salvaged.
            logger.exception(
                "Could not salvage channel table from corrupt db"
            )
        fresh.close()

    # ----- Writing ---------------------------------------------------------

    def _writeBatch(self, conn, items):
        """Insert rows not already present. Always idempotent, so a replay
        after an ambiguous commit failure can never duplicate data."""
        byChannel = {}
        for channel, ts, value in items:
            byChannel.setdefault(channel, []).append((ts, value))

        c = conn.cursor()
        for channel, chRows in byChannel.items():
            lo = ts_to_iso(min(i[0] for i in chRows))
            hi = ts_to_iso(max(i[0] for i in chRows))
            c.execute(
                "SELECT time FROM record "
                "WHERE channel=? AND time>=? AND time<=?",
                (channel, lo, hi),
            )
            existing = {i[0] for i in c}
            c.executemany(
                "INSERT INTO record VALUES (?,?,?)",
                [
                    (channel, ts_to_iso(ts), value)
                    for ts, value in chRows
                    if ts_to_iso(ts) not in existing
                ],
            )

    def _gc(self, conn, force, boundTs=None):
        """Delete expired rows. boundTs is the oldest timestamp among
        unacked items; rows at or after it are preserved, so nothing not yet
        reliably written can be deleted."""
        # Attempt to detect impossible times indicating the clock is wrong.
        if time.time() < 1597447271:
            return

        boundIso = ts_to_iso(boundTs) if boundTs is not None else None
        c = conn.cursor()

        for i in self.children:
            x = self.children[i]()
            if not x or not x.history_length:
                continue

            cutoff = ts_to_iso(time.time() - x.history_length)

            q = "SELECT count(*) FROM record WHERE channel=? AND time<?"
            params: list = [x.chID, cutoff]
            if boundIso:
                q += " AND time<?"
                params.append(boundIso)

            c.execute(q, params)
            count = c.fetchone()[0]

            # Only delete records in large blocks to limit disk wear.
            if count > (1024 if force else 8192):
                q = "DELETE FROM record WHERE channel=? AND time<?"
                params = [x.chID, cutoff]
                if boundIso:
                    q += " AND time<?"
                    params.append(boundIso)
                c.execute(q, params)

    def insertData(self, d):
        with self.lock:
            self.pending_data_items.append(d)

    def forceFlush(self, *a):
        self.flush(True)

    def _requeue(self, items):
        "Put failed samples back at the front, preserving original order."
        for i in reversed(items):
            self.pending_data_items.appendleft(i)

    def _safeGCOnly(self, force):
        try:
            conn = sqlite3.Connection(self.filename)
            try:
                with conn:
                    self._gc(conn, force)
            finally:
                conn.close()
        except Exception:
            logger.exception("History GC failed")

    def _safeReconcile(self):
        try:
            self.pending_data_items.extend(self._reconcileJournal())
        except Exception:
            logger.exception("Journal reconcile failed")

    def flush(self, force=False):
        with self.lock:
            if self.needsStartupReconcile:
                self.needsStartupReconcile = False
                self._safeReconcile()

            # Recovered/retry batches bypass the interval gate, so they get
            # another attempt roughly every minute.
            if not force and not self.pending_data_items:
                if time.time() - self.lastFlushed < self.flushInterval:
                    return
            self.lastFlushed = time.time()

            needsGC = force or (
                self.lastGarbageCollected < time.time() - self.gcInterval
            )
            if needsGC:
                self.lastGarbageCollected = time.time()

            # Drain per-tag accumulators into the pending queue. It is
            # important that a change right after the previous one is
            # eventually recorded.
            for i in self.children:
                x = self.children[i]()
                if x and x.accumCount:
                    x.flush(force)

            # Snapshot the queue, but keep every item until acknowledged.
            items = []
            while self.pending_data_items:
                items.append(self.pending_data_items.popleft())
            items = tuple(items)

            # Oldest unacked timestamp bounds GC for this transaction.
            boundTs = items[0][1] if items else None
            self.inflight = items

            if not items:
                self.inflight = ()
                if needsGC:
                    self._safeGCOnly(force)
                return

            try:
                batchid = self._journalAdd(items)
            except Exception:
                logger.exception(
                    "Could not write recovery journal; "
                    "keeping samples in memory"
                )
                self._requeue(items)
                self.inflight = ()
                return

            conn = sqlite3.Connection(self.filename)
            committed = False
            try:
                with conn:
                    if needsGC:
                        self._gc(conn, force, boundTs)
                    self._writeBatch(conn, items)
                # The with-block returned: COMMIT succeeded and is durable.
                committed = True
            except sqlite3.OperationalError:
                # Locked, disk full, io error etc: retry in order, later.
                logger.exception("History flush failed; will retry")
            except sqlite3.DatabaseError:
                # Corruption: archive and rebuild, then replay the batch.
                logger.exception(
                    "History db corrupt; archiving and rebuilding"
                )
                try:
                    conn.close()
                    self._archiveAndRebuild()
                    conn2 = sqlite3.Connection(self.filename)
                    try:
                        with conn2:
                            self._writeBatch(conn2, items)
                        committed = True
                    finally:
                        conn2.close()
                except Exception:
                    logger.exception(
                        "History flush failed after rebuild; will retry"
                    )
            finally:
                try:
                    conn.close()
                except Exception:
                    logger.exception("Error closing history connection")

            if committed:
                # Commit is durable; nothing after this point may reinsert.
                self.inflight = ()
                try:
                    self._journalClear(batchid)
                except Exception:
                    logger.exception(
                        "Journal ack failed after commit; will reconcile"
                    )
                    self._safeReconcile()
            else:
                self._requeue(items)
                self.inflight = ()
                # This attempt's journal batch must not pile up across
                # retries. Safe whether or not the commit actually landed:
                # reconcile drops present rows, absent ones get replayed.
                try:
                    self._journalClear(batchid)
                except Exception:
                    logger.exception(
                        "Journal clear failed after failed flush; "
                        "will reconcile"
                    )

    # ----- Queries ---------------------------------------------------------

    def _pendingRows(self, channel, minTime, maxTime):
        "Snapshot deque + inflight; deduped (iso time, value) rows."
        out = set()
        for ch, ts, value in list(self.pending_data_items) + list(
            self.inflight
        ):
            if ch == channel and minTime <= ts <= maxTime:
                out.add((ts_to_iso(ts), value))
        return out

    def getDataRange(self, channel, minTime, maxTime, maxRecords=10000):
        with self.lock:
            conn = sqlite3.Connection(self.filename)
            c = conn.execute(
                "SELECT time,value FROM record WHERE time>? AND time<? "
                "AND channel=? ORDER BY time ASC LIMIT ?",
                (
                    ts_to_iso(minTime),
                    ts_to_iso(maxTime),
                    channel,
                    maxRecords,
                ),
            )
            committed = set(c.fetchall())
            conn.close()

            pending = self._pendingRows(channel, minTime, maxTime)

        # Merge committed and not-yet-committed by time, without duplicates.
        return sorted(committed | pending)[:maxRecords]

    def getRecent(self, channel, minTime, maxTime, maxRecords=10000):
        with self.lock:
            conn = sqlite3.Connection(self.filename)
            c = conn.execute(
                "SELECT time,value FROM record WHERE time>? AND time<? "
                "AND channel=? ORDER BY time DESC LIMIT ?",
                (
                    ts_to_iso(minTime),
                    ts_to_iso(maxTime),
                    channel,
                    maxRecords,
                ),
            )
            committed = set(c.fetchall())
            conn.close()

            pending = self._pendingRows(channel, minTime, maxTime)

        # Latest maxRecords, returned in ascending time order.
        return sorted(committed | pending)[-maxRecords:]



try:
    historian = TagHistorian(on_disk_history_db_path)
    ramHistorian = TagHistorian(ramdbfile)
    os.chmod(ramdbfile, 0o750)
    os.chmod(ramdbfile + ".pending.sqlite", 0o750)
except Exception:
    messagebus.post_message(
        "/system/notifications/errors",
        "Failed to create tag historian, logging will not work."
        + "\n"
        + traceback.format_exc(),
    )


loggers: dict[tuple[str, str], TagLogger] = {}

pending_tag_creation_handlers: dict[tuple[str, str], Callable] = {}


class LoggerType(modules_state.ResourceType):
    def blurb(self, module, resource, data):
        return f"""
        <div>
            <form  action="/plugin-tag-history/{quote(data["tag"], safe="")}" method="post">
                <button>View Logs</button>
            </form>
        </div>
        """

    def on_load(self, module, resource, data):
        cls = accumTypes[data["logger_type"]]

        def f(v=None):
            t = tagpoints.allTagsAtomic.get(data["tag"], None)
            if not t:
                return
            t = t()
            if not t:
                return
            loggers[module, resource] = cls(
                t,
                float(data["interval"]),
                int(data["history_length"]),
                data["log_target"],
            )

            t.configLoggers[str((module, resource))] = loggers[module, resource]

            pending_tag_creation_handlers.pop((module, resource), None)
            messagebus.unsubscribe("/system/tags/created", f)

        pending_tag_creation_handlers[module, resource] = f

        if data["tag"] in tagpoints.allTagsAtomic:
            f()
        else:
            # Do it later when ye olde tag existe.
            messagebus.subscribe("/system/tags/created", f)

    def on_move(self, module, resource, to_module, to_resource, data):
        x = loggers.pop((module, resource), None)
        if x:
            loggers[to_module, to_resource] = x

    def on_update(self, module, resource, data):
        self.on_load(module, resource, data)

    def on_unload(self, module, resource, data):
        try:
            loggers[module, resource].close()
        except Exception:
            logger.exception("Error closing logger")

        del loggers[module, resource]

    def on_create_request(self, module, resource, kwargs):
        d = {"resource": {"type": self.type}}
        d.update(kwargs)
        d.pop("name")
        d.pop("Save", None)

        return d

    def on_update_request(self, module, resource, data, kwargs):
        d = mutable_copy_resource(data)
        d.update(kwargs)
        d.pop("name", None)
        d.pop("Save", None)
        return d

    def create_page(self, module, path):
        d = dialogs.SimpleDialog("New Logger")
        d.text_input("name", title="Logger Name")
        d.text_input(
            "tag",
            title="Tag Point to Log",
            suggestions=[(i, i) for i in tagsapi.all_tags_raw().keys()],
        )
        d.selection(
            "logger_type",
            options=list(accumTypes.keys()),
            title="Accumulate Mode",
        )
        d.selection("log_target", options=["disk", "ram"])
        d.text_input("interval", title="Interval(seconds)", default=str(60))
        d.text_input(
            "history_length",
            title="History Length(seconds)",
            default=str(24 * 30 * 3600),
        )

        d.submit_button("Save")
        return d.render(self.get_create_target(module, path))

    def edit_page(self, module, resource, data):
        d = dialogs.SimpleDialog("Editing Logger")
        d.text_input(
            "tag",
            title="Tag Point to Log",
            default=data["tag"],
            suggestions=[(i, i) for i in tagsapi.all_tags_raw().keys()],
        )
        d.selection(
            "logger_type",
            options=list(accumTypes.keys()),
            default=data["logger_type"],
            title="Accumulate Mode",
        )
        d.selection(
            "log_target", options=["disk", "ram"], default=data["log_target"]
        )
        d.text_input(
            "interval", title="Interval(seconds)", default=data["interval"]
        )
        d.text_input(
            "history_length",
            title="History Length(seconds)",
            default=data["history_length"],
        )

        d.submit_button("Save")
        return d.render(self.get_update_target(module, resource))


drt = LoggerType("logger", mdi_icon="sine-wave")
modules_state.resource_types["logger"] = drt


t = os.path.join(os.path.dirname(__file__), "html", "logpage.html")


@quart_app.app.route("/plugin-tag-history", methods=["GET", "POST"])
@quart_app.app.route("/plugin-tag-history/<path:path>", methods=["GET", "POST"])
@quart_app.wrap_sync_route_handler
def logpage(path: str = "", **kwargs):
    pages.require("system_admin")
    path = "/" + path
    # This page could be slow because of the db stuff, so we restrict it more
    if not quart.request.method.lower() == "post":
        raise RuntimeError("POST only")

    if "exportRows" not in kwargs:
        return pages.get_template(t).render(tagName=path, data=kwargs)
    else:
        tag = tagpoints.allTags[path]()
        if tag is None:
            raise RuntimeError("This tag seems to no longer exist")

        for key, i in tag.configLoggers.items():
            assert isinstance(i, TagLogger)
            if i.accumType == kwargs["exportType"]:
                tz = pytz.timezone("Etc/UTC")
                logtime = tz.localize(
                    dateutil.parser.parse(kwargs["logtime"])
                ).timestamp()
                raw = i.getDataRange(
                    logtime, time.time() + 10000000, int(kwargs["exportRows"])
                )

                if kwargs["exportFormat"] == "csv.iso":
                    filename = (
                        path.replace("/", "_")
                        .replace(".", "_")
                        .replace(":", "_")[1:]
                        + "_"
                        + kwargs["exportType"]
                        + tz.localize(
                            dateutil.parser.parse(kwargs["logtime"])
                        ).isoformat()
                        + ".csv"
                    )

                    d = [
                        "Time(ISO), "
                        + path.replace(",", "")
                        + " <accum "
                        + kwargs["exportType"]
                        + ">"
                    ]
                    for i in raw:
                        d.append(str(i[0]) + "," + str(i[1])[:128])
                    return quart.Response(
                        "\r\n".join(d) + "\r\n",
                        content_type="text/csv",
                        headers={
                            "Content-Disposition": "attachment; filename="
                            + filename
                        },
                    )

        raise RuntimeError("Logger not found")
