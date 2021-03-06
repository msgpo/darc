# -*- coding: utf-8 -*-
"""Link Database
===================

The :mod:`darc` project utilises `Redis`_ based database
to provide tele-process communication.

.. note::

   In its first implementation, the :mod:`darc` project used
   :class:`~multiprocessing.Queue` to support such communication.
   However, as noticed when runtime, the :class:`~multiprocessing.Queue`
   object will be much affected by the lack of memory.

There will be three databases, all following the save naming
convension with ``queue_`` prefix:

* the hostname database -- ``queue_hostname`` (:class:`~darc.model.tasks.hostname.HostnameQueueModel`)
* the :mod:`requests` database -- ``queue_requests`` (:class:`~darc.model.tasks.requests.RequestsQueueModel`)
* the :mod:`selenium` database -- ``queue_selenium`` (:class:`~darc.model.tasks.selenium.SeleniumQueueModel`)

For ``queue_hostname``, it is a `Redis`_ **set** data type;
and for ``queue_requests`` and ``queue_selenium``, they
are both `Redis`_ **sorted set** data type.

If :data:`~darc.const.FLAG_DB` is :data:`True`, then the
module uses the RDS storage described by the :mod:`peewee`
models as backend.

.. _Redis: https://redis.io/

"""

import contextlib
import datetime
import math
import os
import pickle
import pprint
import shutil
import sys
import textwrap
import time
import warnings

import peewee
import redis.lock as redis_lock
import stem.util.term

import darc.typing as typing
from darc._compat import nullcontext
from darc.const import CHECK
from darc.const import DB as database
from darc.const import FLAG_DB
from darc.const import REDIS as redis
from darc.const import TIME_CACHE, VERBOSE
from darc.error import LockWarning, RedisCommandFailed, render_error
from darc.link import Link
from darc.model.tasks import HostnameQueueModel, RequestsQueueModel, SeleniumQueueModel
from darc.parse import _check

# use lock?
REDIS_LOCK = bool(int(os.getenv('DARC_REDIS_LOCK', '0')))

# Redis retry interval
REDIS_RETRY = float(os.getenv('DARC_REDIS_RETRY', '10'))
if not math.isfinite(REDIS_RETRY):
    REDIS_RETRY = None

# lock blocking timeout
LOCK_TIMEOUT = float(os.getenv('DARC_LOCK_TIMEOUT', '10'))
if not math.isfinite(LOCK_TIMEOUT):
    LOCK_TIMEOUT = None

# bulk size
BULK_SIZE = int(os.getenv('DARC_BULK_SIZE', '100'))

# max pool
MAX_POOL = float(os.getenv('DARC_MAX_POOL', '100'))
if math.isfinite(MAX_POOL):
    MAX_POOL = math.floor(MAX_POOL)


def _redis_command(command: str, *args, **kwargs) -> typing.Any:
    """Wrapper function for Redis command.

    Args:
        command: Command name.
        *args: Arbitrary arguments for the Redis command.

    Keyword Args:
        **kwargs: Arbitrary keyword arguments for the Redis command.

    Return:
        Values returned from the Redis command.

    Warns:
        RedisCommandFailed: Warns at each round when the command failed.

    See Also:
        Between each retry, the function sleeps for :data:`~darc.db.REDIS_RETRY`
        second(s) if such value is **NOT** :data:`None`.

    """
    _arg_msg = None

    method = getattr(redis, command)
    while True:
        try:
            value = method(*args, **kwargs)
        except Exception as error:
            if _arg_msg is None:
                _args = ', '.join(map(repr, args))
                _kwargs = ', '.join(f'{k}={v!r}' for k, v in kwargs.items())
                if _kwargs:
                    if _args:
                        _args += ', '
                    _args += _kwargs
                _arg_msg = textwrap.shorten(_args, shutil.get_terminal_size().columns)

            warning = warnings.formatwarning(error, RedisCommandFailed, __file__, 85,
                                             f'value = redis.{command}({_arg_msg})')
            print(render_error(warning, stem.util.term.Color.YELLOW), end='', file=sys.stderr)  # pylint: disable=no-member

            if REDIS_RETRY is not None:
                time.sleep(REDIS_RETRY)
            continue
        break
    return value


def _redis_get_lock(name: str,
                    timeout: typing.Optional[float] = None,
                    sleep: float = 0.1,
                    blocking_timeout: typing.Optional[float] = None,
                    lock_class: typing.Optional[redis_lock.Lock] = None,
                    thread_local: bool = True) -> typing.Union[redis_lock.Lock, nullcontext]:
    """Get a lock for Redis operations.

    Args:
        name: Lock name.
        timeout: Maximum life for the lock.
        sleep: Amount of time to sleep per loop iteration when the
            lock is in blocking mode and another client is currently
            holding the lock.
        blocking_timeout: Maximum amount of time in seconds to spend
            trying to acquire the lock.
        lock_class: Lock implementation.
        thread_local: Whether the lock token is placed in thread-local
            storage.

    Returns:
        Return a new :class:`redis.lock.Lock` object using key ``name``
        that mimics the behavior of :class:`threading.Lock`.

    Seel Also:
        If :data:`~darc.db.REDIS_LOCK` is :data:`False`, returns a
        :class:`contextlib.nullcontext` instead.

    """
    if REDIS_LOCK:
        return _redis_command('lock', name, timeout, sleep, blocking_timeout, lock_class, thread_local)
    return nullcontext()


def have_hostname(link: Link) -> bool:
    """Check if current link is a new host.

    Args:
        link: Link to check against.

    Returns:
        If such link is a new host.

    See Also:
        * :func:`darc.db._have_hostname_db`
        * :func:`darc.db._have_hostname_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            return _have_hostname_db(link)
    return _have_hostname_redis(link)


def _have_hostname_db(link: Link) -> bool:
    """Check if current link is a new host.

    The function checks the :class:`~darc.models.tasks.hostname.HostnameQueueModel` table.

    Args:
        link: Link to check against.

    Returns:
        If such link is a new host.

    """
    timestamp = datetime.datetime.now()
    if TIME_CACHE is None:
        threshold = -1
    else:
        threshold = timestamp - TIME_CACHE

    model, created = HostnameQueueModel.get_or_create(hostname=link.host, defaults=dict(
        timestamp=timestamp,
    ))
    if created:
        return False
    return model.timestamp > threshold


def _have_hostname_redis(link: Link) -> bool:
    """Check if current link is a new host.

    The function checks the ``queue_hostname`` database.

    Args:
        link: Link to check against.

    Returns:
        If such link is a new host.

    """
    with _redis_get_lock('lock_queue_hostname'):
        code = _redis_command('sadd', 'queue_hostname', link.host)
    flag = not bool(code)  # 1 - success; 0 - failed
    return flag


def drop_hostname(link: Link):
    """Remove link from the hostname database.

    Args:
        link: Link to be removed.

    See Also:
        * :func:`darc.db._drop_hostname_db`
        * :func:`darc.db._drop_hostname_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            return _drop_hostname_db(link)
    return _drop_hostname_redis(link)


def _drop_hostname_db(link: Link):
    """Remove link from the hostname database.

    The function updates the :class:`~darc.models.tasks.hostname.HostnameQueueModel` table.

    Args:
        link: Link to be removed.

    """
    model: HostnameQueueModel = HostnameQueueModel.get_or_none(HostnameQueueModel.hostname == link.host)
    if model is None:
        return
    model.delete_instance()


def _drop_hostname_redis(link: Link):
    """Remove link from the hostname database.

    The function updates the ``queue_hostname`` database.

    Args:
        link: Link to be removed.

    """
    with _redis_get_lock('lock_queue_hostname'):
        _redis_command('srem', 'queue_hostname', link.host)


def drop_requests(link: Link):
    """Remove link from the :mod:`requests` database.

    Args:
        link: Link to be removed.

    See Also:
        * :func:`darc.db._drop_requests_db`
        * :func:`darc.db._drop_requests_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            return _drop_requests_db(link)
    return _drop_requests_redis(link)


def _drop_requests_db(link: Link):
    """Remove link from the :mod:`requests` database.

    The function updates the :class:`~darc.model.tasks.requests.RequestsQueueModel` table.

    Args:
        link: Link to be removed.

    """
    model: RequestsQueueModel = RequestsQueueModel.get_or_none(RequestsQueueModel.text == link.url)
    if model is None:
        return
    model.delete_instance()


def _drop_requests_redis(link: Link):
    """Remove link from the :mod:`requests` database.

    The function updates the ``queue_requests`` database.

    Args:
        link: Link to be removed.

    """
    with _redis_get_lock('lock_queue_requests'):
        _redis_command('zrem', 'queue_requests', pickle.dumps(link))


def drop_selenium(link: Link):
    """Remove link from the :mod:`selenium` database.

    Args:
        link: Link to be removed.

    See Also:
        * :func:`darc.db._drop_selenium_db`
        * :func:`darc.db._drop_selenium_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            return _drop_selenium_db(link)
    return _drop_selenium_redis(link)


def _drop_selenium_db(link: Link):
    """Remove link from the :mod:`selenium` database.

    The function updates the :class:`~darc.model.tasks.selenium.SeleniumQueueModel` table.

    Args:
        link: Link to be removed.

    """
    model: SeleniumQueueModel = SeleniumQueueModel.get_or_none(SeleniumQueueModel.text == link.url)
    if model is None:
        return
    model.delete_instance()


def _drop_selenium_redis(link: Link):
    """Remove link from the :mod:`selenium` database.

    The function updates the ``queue_selenium`` database.

    Args:
        link: Link to be removed.

    """
    with _redis_get_lock('lock_queue_selenium'):
        _redis_command('zrem', 'queue_selenium', pickle.dumps(link))


def save_requests(entries: typing.List[Link], single: bool = False,
                  score=None, nx=False, xx=False):
    """Save link to the :mod:`requests` database.

    The function updates the ``queue_requests`` database.

    Args:
        entries: Links to be added to the :mod:`requests` database.
            It can be either a :obj:`list` of links, or a single
            link string (if ``single`` set as :data:`True`).
        single: Indicate if ``entries`` is a :obj:`list` of links
            or a single link string.
        score: Score to for the Redis sorted set.
        nx: Only create new elements and not to
            update scores for elements that already exist.
        xx: Only update scores of elements that
            already exist. New elements will not be added.

    Notes:
        The ``entries`` will be dumped through :mod:`pickle` so that
        :mod:`darc` do not need to parse them again.

    When ``entries`` is a list of :class:`~darc.link.Link` instances,
    we tries to perform *bulk* update to easy the memory consumption.
    The *bulk* size is defined by :data:`~darc.db.BULK_SIZE`.

    See Also:
        * :func:`darc.db._save_requests_db`
        * :func:`darc.db._save_requests_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            return _save_requests_db(entries, single, score, nx, xx)
    return _save_requests_redis(entries, single, score, nx, xx)


def _save_requests_db(entries: typing.List[Link], single: bool = False,
                      score=None, nx=False, xx=False):
    """Save link to the :mod:`requests` database.

    The function updates the :class:`~darc.model.tasks.requests.RequestsQueueModel` table.

    Args:
        entries: Links to be added to the :mod:`requests` database.
            It can be either a :obj:`list` of links, or a single
            link string (if ``single`` set as :data:`True`).
        single: Indicate if ``entries`` is a :obj:`list` of links
            or a single link string.
        score: Score to for the Redis sorted set.
        nx: Only create new elements and not to
            update scores for elements that already exist.
        xx: Only update scores of elements that
            already exist. New elements will not be added.

    """
    if not entries:
        return
    if score is None:
        score = datetime.datetime.now()

    if not single:
        if nx:
            with database.atomic():
                insert_many = [dict(
                    text=link.url,
                    hash=link.name,
                    link=link,
                    timestamp=score,
                ) for link in entries]
                for batch in peewee.chunked(insert_many, BULK_SIZE):
                    (RequestsQueueModel
                     .insert_many(insert_many)
                     .on_conflict_ignore()
                     .execute())
            return

        if xx:
            entries_text = [link.url for link in entries]
            (RequestsQueueModel
             .update(timestamp=score)
             .where(RequestsQueueModel.text.in_(entries_text))
             .execute())
            return

        with database.atomic():
            replace_many = [dict(
                text=link.url,
                hash=link.name,
                link=link,
                timestamp=score
            ) for link in entries]
            for batch in peewee.chunked(replace_many, BULK_SIZE):
                RequestsQueueModel.replace_many(batch).execute()
        return

    if nx:
        RequestsQueueModel.get_or_create(
            text=entries.url,
            defaults=dict(
                hash=entries.name,
                link=entries,
                timestamp=score,
            )
        )
        return

    if xx:
        with contextlib.suppress(peewee.DoesNotExist):
            model = RequestsQueueModel.get(RequestsQueueModel.text == entries.url)
            model.timestamp = score
            model.save()
        return

    RequestsQueueModel.replace(
        text=entries.url,
        hash=entries.name,
        link=entries,
        timestamp=score
    ).execute()


def _save_requests_redis(entries: typing.List[Link], single: bool = False,
                         score=None, nx=False, xx=False):
    """Save link to the :mod:`requests` database.

    The function updates the ``queue_requests`` database.

    Args:
        entries: Links to be added to the :mod:`requests` database.
            It can be either a :obj:`list` of links, or a single
            link string (if ``single`` set as :data:`True`).
        single: Indicate if ``entries`` is a :obj:`list` of links
            or a single link string.
        score: Score to for the Redis sorted set.
        nx: Forces ``ZADD`` to only create new elements and not to
            update scores for elements that already exist.
        xx: Forces ``ZADD`` to only update scores of elements that
            already exist. New elements will not be added.

    """
    if score is None:
        score = time.time()

    if not single:
        for i in range(0, len(entries), BULK_SIZE):
            mapping = {
                pickle.dumps(link): score for link in entries[i:i + BULK_SIZE]
            }
            with _redis_get_lock('lock_queue_requests'):
                _redis_command('zadd', 'queue_requests', mapping, nx=nx, xx=xx)
        return

    mapping = {
        pickle.dumps(entries): score,
    }
    with _redis_get_lock('lock_queue_requests'):
        _redis_command('zadd', 'queue_requests', mapping, nx=nx, xx=xx)


def save_selenium(entries: typing.List[Link], single: bool = False,
                  score=None, nx=False, xx=False):
    """Save link to the :mod:`selenium` database.

    Args:
        entries: Links to be added to the :mod:`selenium` database.
            It can be either a :obj:`list` of links, or a single
            link string (if ``single`` set as :data:`True`).
        single: Indicate if ``entries`` is a :obj:`list` of links
            or a single link string.
        score: Score to for the Redis sorted set.
        nx: Only create new elements and not to
            update scores for elements that already exist.
        xx: Only update scores of elements that
            already exist. New elements will not be added.

    Notes:
        The ``entries`` will be dumped through :mod:`pickle` so that
        :mod:`darc` do not need to parse them again.

    When ``entries`` is a list of :class:`~darc.link.Link` instances,
    we tries to perform *bulk* update to easy the memory consumption.
    The *bulk* size is defined by :data:`~darc.db.BULK_SIZE`.

    See Also:
        * :func:`darc.db._save_selenium_db`
        * :func:`darc.db._save_selenium_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            return _save_selenium_db(entries, single, score, nx, xx)
    return _save_selenium_redis(entries, single, score, nx, xx)


def _save_selenium_db(entries: typing.List[Link], single: bool = False,
                      score=None, nx=False, xx=False):
    """Save link to the :mod:`selenium` database.

    The function updates the :class:`~darc.model.tasks.selenium.SeleniumQueueModel` table.

    Args:
        entries: Links to be added to the :mod:`selenium` database.
            It can be either a :obj:`list` of links, or a single
            link string (if ``single`` set as :data:`True`).
        single: Indicate if ``entries`` is a :obj:`list` of links
            or a single link string.
        score: Score to for the Redis sorted set.
        nx: Only create new elements and not to
            update scores for elements that already exist.
        xx: Only update scores of elements that
            already exist. New elements will not be added.

    """
    if not entries:
        return
    if score is None:
        score = datetime.datetime.now()

    if not single:
        if nx:
            with database.atomic():
                insert_many = [dict(
                    text=link.url,
                    hash=link.name,
                    link=link,
                    timestamp=score,
                ) for link in entries]
                for batch in peewee.chunked(insert_many, BULK_SIZE):
                    (SeleniumQueueModel
                     .insert_many(insert_many)
                     .on_conflict_ignore()
                     .execute())
            return

        if xx:
            entries_text = [link.url for link in entries]
            (SeleniumQueueModel
             .update(timestamp=score)
             .where(SeleniumQueueModel.text.in_(entries_text))
             .execute())
            return

        with database.atomic():
            replace_many = [dict(
                text=link.url,
                hash=link.name,
                link=link,
                timestamp=score
            ) for link in entries]
            for batch in peewee.chunked(replace_many, BULK_SIZE):
                SeleniumQueueModel.replace_many(batch).execute()
        return

    if nx:
        SeleniumQueueModel.get_or_create(
            text=entries.url,
            defaults=dict(
                hash=entries.name,
                link=entries,
                timestamp=score,
            )
        )
        return

    if xx:
        with contextlib.suppress(peewee.DoesNotExist):
            model = SeleniumQueueModel.get(SeleniumQueueModel.text == entries.url)
            model.timestamp = score
            model.save()
        return

    SeleniumQueueModel.replace(
        text=entries.url,
        hash=entries.name,
        link=entries,
        timestamp=score
    ).execute()


def _save_selenium_redis(entries: typing.List[Link], single: bool = False,
                         score=None, nx=False, xx=False):
    """Save link to the :mod:`selenium` database.

    The function updates the ``queue_selenium`` database.

    Args:
        entries: Links to be added to the :mod:`selenium` database.
            It can be either an *iterable* of links, or a single
            link string (if ``single`` set as :data:`True`).
        single: Indicate if ``entries`` is an *iterable* of links
            or a single link string.
        score: Score to for the Redis sorted set.
        nx: Forces ``ZADD`` to only create new elements and not to
            update scores for elements that already exist.
        xx: Forces ``ZADD`` to only update scores of elements that
            already exist. New elements will not be added.

    When ``entries`` is a list of :class:`~darc.link.Link` instances,
    we tries to perform *bulk* update to easy the memory consumption.
    The *bulk* size is defined by :data:`~darc.db.BULK_SIZE`.

    Notes:
        The ``entries`` will be dumped through :mod:`pickle` so that
        :mod:`darc` do not need to parse them again.

    """
    if not entries:
        return
    if score is None:
        score = time.time()

    if not single:
        for i in range(0, len(entries), BULK_SIZE):
            mapping = {
                pickle.dumps(link): score for link in entries[i:i + BULK_SIZE]
            }
            with _redis_get_lock('lock_queue_selenium'):
                _redis_command('zadd', 'queue_selenium', mapping, nx=nx, xx=xx)
        return

    mapping = {
        pickle.dumps(entries): score,
    }
    with _redis_get_lock('lock_queue_selenium'):
        _redis_command('zadd', 'queue_selenium', mapping, nx=nx, xx=xx)


def load_requests(check: bool = CHECK) -> typing.List[Link]:
    """Load link from the :mod:`requests` database.

    Args:
        check: If perform checks on loaded links,
            default to :data:`~darc.const.CHECK`.

    Returns:
        List of loaded links from the :mod:`requests` database.

    Note:
        At runtime, the function will load links with maximum number
        at :data:`~darc.db.MAX_POOL` to limit the memory usage.

    See Also:
        * :func:`darc.db._load_requests_db`
        * :func:`darc.db._load_requests_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            link_pool = _load_requests_db()
    else:
        link_pool = _load_requests_redis()

    if check:
        link_pool = _check(link_pool)

    if VERBOSE:
        print(stem.util.term.format('-*- [REQUESTS] LINK POOL -*-',
                                    stem.util.term.Color.MAGENTA))  # pylint: disable=no-member
        print(render_error(pprint.pformat(sorted(link.url for link in link_pool)),
                           stem.util.term.Color.MAGENTA))  # pylint: disable=no-member
        print(stem.util.term.format('-' * shutil.get_terminal_size().columns,
                                    stem.util.term.Color.MAGENTA))  # pylint: disable=no-member
    return link_pool


def _load_requests_db() -> typing.List[Link]:
    """Load link from the :mod:`requests` database.

    The function reads the :class:`~darc.model.tasks.requests.RequestsQueueModel` table.

    Returns:
        List of loaded links from the :mod:`requests` database.

    Note:
        At runtime, the function will load links with maximum number
        at :data:`~darc.db.MAX_POOL` to limit the memory usage.

    """
    now = datetime.datetime.now()
    if TIME_CACHE is None:
        sec_delta = 0
        max_score = now
    else:
        sec_delta = TIME_CACHE
        max_score = now - sec_delta

    with database.atomic():
        query: typing.List[RequestsQueueModel] = (
            RequestsQueueModel
            .select(RequestsQueueModel.link)
            .where(RequestsQueueModel.timestamp <= max_score)
            .order_by(RequestsQueueModel.timestamp)
            .limit(MAX_POOL)
        )
        link_pool = [model.link for model in query]

        if TIME_CACHE is not None:
            new_score = now + sec_delta
            _save_requests_db(link_pool, score=new_score)  # force update records
    return link_pool


def _load_requests_redis() -> typing.List[Link]:
    """Load link from the :mod:`requests` database.

    The function reads the ``queue_requests`` database.

    Returns:
        List of loaded links from the :mod:`requests` database.

    Note:
        At runtime, the function will load links with maximum number
        at :data:`~darc.db.MAX_POOL` to limit the memory usage.

    """
    now = time.time()
    if TIME_CACHE is None:
        sec_delta = 0
        max_score = now
    else:
        sec_delta = TIME_CACHE.total_seconds()
        max_score = now - sec_delta

    try:
        with _redis_get_lock('lock_queue_requests', blocking_timeout=LOCK_TIMEOUT):
            link_pool = [pickle.loads(link) for link in _redis_command('zrangebyscore', 'queue_requests',
                                                                       min=0, max=max_score, start=0, num=MAX_POOL)]
            if TIME_CACHE is not None:
                new_score = now + sec_delta
                _save_requests_redis(link_pool, score=new_score)  # force update records
    except redis_lock.LockError:
        warning = warnings.formatwarning(f'[REQUESTS] Failed to acquire Redis lock after {LOCK_TIMEOUT} second(s)',
                                         LockWarning, __file__, 250, "_redis_get_lock('lock_queue_requests')")
        print(render_error(warning, stem.util.term.Color.YELLOW), end='', file=sys.stderr)  # pylint: disable=no-member
        link_pool = list()
    return link_pool


def load_selenium(check: bool = CHECK) -> typing.List[Link]:
    """Load link from the :mod:`selenium` database.

    Args:
        check: If perform checks on loaded links,
            default to :data:`~darc.const.CHECK`.

    Returns:
        List of loaded links from the :mod:`selenium` database.

    Note:
        At runtime, the function will load links with maximum number
        at :data:`~darc.db.MAX_POOL` to limit the memory usage.

    See Also:
        * :func:`darc.db._load_selenium_db`
        * :func:`darc.db._load_selenium_redis`

    """
    if FLAG_DB:
        with database.connection_context():
            link_pool = _load_selenium_db()
    else:
        link_pool = _load_selenium_redis()

    if check:
        link_pool = _check(link_pool)

    if VERBOSE:
        print(stem.util.term.format('-*- [SELENIUM] LINK POOL -*-',
                                    stem.util.term.Color.MAGENTA))  # pylint: disable=no-member
        print(render_error(pprint.pformat(sorted(link.url for link in link_pool)),
                           stem.util.term.Color.MAGENTA))  # pylint: disable=no-member
        print(stem.util.term.format('-' * shutil.get_terminal_size().columns,
                                    stem.util.term.Color.MAGENTA))  # pylint: disable=no-member
    return link_pool


def _load_selenium_db() -> typing.List[Link]:
    """Load link from the :mod:`selenium` database.

    The function reads the :class:`~darc.model.tasks.selenium.SeleniumQueueModel` table.

    Returns:
        List of loaded links from the :mod:`selenium` database.

    Note:
        At runtime, the function will load links with maximum number
        at :data:`~darc.db.MAX_POOL` to limit the memory usage.

    """
    now = datetime.datetime.now()
    if TIME_CACHE is None:
        sec_delta = 0
        max_score = now
    else:
        sec_delta = TIME_CACHE
        max_score = now - sec_delta

    with database.atomic():
        query: typing.List[SeleniumQueueModel] = (
            SeleniumQueueModel
            .select(SeleniumQueueModel.link)
            .where(SeleniumQueueModel.timestamp <= max_score)
            .order_by(SeleniumQueueModel.timestamp)
            .limit(MAX_POOL)
        )
        link_pool = [model.link for model in query]

        if TIME_CACHE is not None:
            new_score = now + sec_delta
            _save_selenium_db(link_pool, score=new_score)  # force update records
    return link_pool


def _load_selenium_redis() -> typing.List[Link]:
    """Load link from the :mod:`selenium` database.

    The function reads the ``queue_selenium`` database.

    Args:
        check: If perform checks on loaded links,
            default to :data:`~darc.const.CHECK`.

    Returns:
        List of loaded links from the :mod:`selenium` database.

    Note:
        At runtime, the function will load links with maximum number
        at :data:`~darc.db.MAX_POOL` to limit the memory usage.

    """
    now = time.time()
    if TIME_CACHE is None:
        sec_delta = 0
        max_score = now
    else:
        sec_delta = TIME_CACHE.total_seconds()
        max_score = now - sec_delta

    try:
        with _redis_get_lock('lock_queue_selenium', blocking_timeout=LOCK_TIMEOUT):
            link_pool = [pickle.loads(link) for link in _redis_command('zrangebyscore', 'queue_selenium',
                                                                       min=0, max=max_score, start=0, num=MAX_POOL)]
            if TIME_CACHE is not None:
                new_score = now + sec_delta
                _save_selenium_redis(link_pool, score=new_score)  # force update records
    except redis_lock.LockError:
        warning = warnings.formatwarning(f'[SELENIUM] Failed to acquire Redis lock after {LOCK_TIMEOUT} second(s)',
                                         LockWarning, __file__, 299, "_redis_get_lock('lock_queue_selenium')")
        print(render_error(warning, stem.util.term.Color.YELLOW), end='', file=sys.stderr)  # pylint: disable=no-member
        link_pool = list()
    return link_pool
