import sys, types, weakref
from collections import deque
from test.bootstrap import config
from test.lib.util import decorator
from sqlalchemy.util import callable
from sqlalchemy import event, pool
from sqlalchemy.engine import base as engine_base
import re
import warnings

class ConnectionKiller(object):
    def __init__(self):
        self.proxy_refs = weakref.WeakKeyDictionary()
        self.testing_engines = weakref.WeakKeyDictionary()
        self.conns = set()

    def add_engine(self, engine):
        self.testing_engines[engine] = True

    def checkout(self, dbapi_con, con_record, con_proxy):
        self.proxy_refs[con_proxy] = True
        self.conns.add(dbapi_con)

    def _safe(self, fn):
        try:
            fn()
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception, e:
            warnings.warn(
                    "testing_reaper couldn't "
                    "rollback/close connection: %s" % e)

    def rollback_all(self):
        for rec in self.proxy_refs.keys():
            if rec is not None and rec.is_valid:
                self._safe(rec.rollback)

    def close_all(self):
        for rec in self.proxy_refs.keys():
            if rec is not None:
                self._safe(rec._close)

    def _after_test_ctx(self):
        for conn in self.conns:
            self._safe(conn.rollback)

    def _stop_test_ctx(self):
        self.close_all()
        for conn in self.conns:
            self._safe(conn.close)
        self.conns = set()
        for rec in self.testing_engines.keys():
            rec.dispose()

    def assert_all_closed(self):
        for rec in self.proxy_refs:
            if rec.is_valid:
                assert False

testing_reaper = ConnectionKiller()

def drop_all_tables(metadata, bind):
    testing_reaper.close_all()
    if hasattr(bind, 'close'):
        bind.close()
    metadata.drop_all(bind)

@decorator
def assert_conns_closed(fn, *args, **kw):
    try:
        fn(*args, **kw)
    finally:
        testing_reaper.assert_all_closed()

@decorator
def rollback_open_connections(fn, *args, **kw):
    """Decorator that rolls back all open connections after fn execution."""

    try:
        fn(*args, **kw)
    finally:
        testing_reaper.rollback_all()

@decorator
def close_first(fn, *args, **kw):
    """Decorator that closes all connections before fn execution."""

    testing_reaper.close_all()
    fn(*args, **kw)


@decorator
def close_open_connections(fn, *args, **kw):
    """Decorator that closes all connections after fn execution."""
    try:
        fn(*args, **kw)
    finally:
        testing_reaper.close_all()

def all_dialects(exclude=None):
    import sqlalchemy.databases as d
    for name in d.__all__:
        # TEMPORARY
        if exclude and name in exclude:
            continue
        mod = getattr(d, name, None)
        if not mod:
            mod = getattr(__import__('sqlalchemy.databases.%s' % name).databases, name)
        yield mod.dialect()

class ReconnectFixture(object):
    def __init__(self, dbapi):
        self.dbapi = dbapi
        self.connections = []

    def __getattr__(self, key):
        return getattr(self.dbapi, key)

    def connect(self, *args, **kwargs):
        conn = self.dbapi.connect(*args, **kwargs)
        self.connections.append(conn)
        return conn

    def shutdown(self):
        # TODO: this doesn't cover all cases
        # as nicely as we'd like, namely MySQLdb.
        # would need to implement R. Brewer's
        # proxy server idea to get better
        # coverage.
        for c in list(self.connections):
            c.close()
        self.connections = []

def reconnecting_engine(url=None, options=None):
    url = url or config.db_url
    dbapi = config.db.dialect.dbapi
    if not options:
        options = {}
    options['module'] = ReconnectFixture(dbapi)
    engine = testing_engine(url, options)
    engine.test_shutdown = engine.dialect.dbapi.shutdown
    return engine

def testing_engine(url=None, options=None):
    """Produce an engine configured by --options with optional overrides."""

    from sqlalchemy import create_engine
    from test.lib.assertsql import asserter

    if not options:
        use_reaper = True
    else:
        use_reaper = options.pop('use_reaper', True)

    url = url or config.db_url
    options = options or config.db_opts

    engine = create_engine(url, **options)
    if isinstance(engine.pool, pool.QueuePool):
        engine.pool._timeout = 0
        engine.pool._max_overflow = 0
    event.listen(engine, 'after_execute', asserter.execute)
    event.listen(engine, 'after_cursor_execute', asserter.cursor_execute)
    if use_reaper:
        event.listen(engine.pool, 'checkout', testing_reaper.checkout)
        testing_reaper.add_engine(engine)

    return engine

def utf8_engine(url=None, options=None):
    """Hook for dialects or drivers that don't handle utf8 by default."""

    from sqlalchemy.engine import url as engine_url

    if config.db.dialect.name == 'mysql' and \
        config.db.driver in ['mysqldb', 'pymysql']:
        # note 1.2.1.gamma.6 or greater of MySQLdb 
        # needed here
        url = url or config.db_url
        url = engine_url.make_url(url)
        url.query['charset'] = 'utf8'
        url.query['use_unicode'] = '0'
        url = str(url)

    return testing_engine(url, options)

def mock_engine(dialect_name=None):
    """Provides a mocking engine based on the current testing.db.

    This is normally used to test DDL generation flow as emitted
    by an Engine.

    It should not be used in other cases, as assert_compile() and
    assert_sql_execution() are much better choices with fewer 
    moving parts.

    """

    from sqlalchemy import create_engine

    if not dialect_name:
        dialect_name = config.db.name

    buffer = []
    def executor(sql, *a, **kw):
        buffer.append(sql)
    def assert_sql(stmts):
        recv = [re.sub(r'[\n\t]', '', str(s)) for s in buffer]
        assert  recv == stmts, recv

    engine = create_engine(dialect_name + '://',
                           strategy='mock', executor=executor)
    assert not hasattr(engine, 'mock')
    engine.mock = buffer
    engine.assert_sql = assert_sql
    return engine

class ReplayableSession(object):
    """A simple record/playback tool.

    This is *not* a mock testing class.  It only records a session for later
    playback and makes no assertions on call consistency whatsoever.  It's
    unlikely to be suitable for anything other than DB-API recording.

    """

    Callable = object()
    NoAttribute = object()

    # Py3K
    #Natives = set([getattr(types, t)
    #               for t in dir(types) if not t.startswith('_')]). \
    #               union([type(t) if not isinstance(t, type) 
    #                        else t for t in __builtins__.values()]).\
    #               difference([getattr(types, t)
    #                        for t in ('FunctionType', 'BuiltinFunctionType',
    #                                  'MethodType', 'BuiltinMethodType',
    #                                  'LambdaType', )])
    # Py2K
    Natives = set([getattr(types, t)
                   for t in dir(types) if not t.startswith('_')]). \
                   difference([getattr(types, t)
                           for t in ('FunctionType', 'BuiltinFunctionType',
                                     'MethodType', 'BuiltinMethodType',
                                     'LambdaType', 'UnboundMethodType',)])
    # end Py2K

    def __init__(self):
        self.buffer = deque()

    def recorder(self, base):
        return self.Recorder(self.buffer, base)

    def player(self):
        return self.Player(self.buffer)

    class Recorder(object):
        def __init__(self, buffer, subject):
            self._buffer = buffer
            self._subject = subject

        def __call__(self, *args, **kw):
            subject, buffer = [object.__getattribute__(self, x)
                               for x in ('_subject', '_buffer')]

            result = subject(*args, **kw)
            if type(result) not in ReplayableSession.Natives:
                buffer.append(ReplayableSession.Callable)
                return type(self)(buffer, result)
            else:
                buffer.append(result)
                return result

        @property
        def _sqla_unwrap(self):
            return self._subject

        def __getattribute__(self, key):
            try:
                return object.__getattribute__(self, key)
            except AttributeError:
                pass

            subject, buffer = [object.__getattribute__(self, x)
                               for x in ('_subject', '_buffer')]
            try:
                result = type(subject).__getattribute__(subject, key)
            except AttributeError:
                buffer.append(ReplayableSession.NoAttribute)
                raise
            else:
                if type(result) not in ReplayableSession.Natives:
                    buffer.append(ReplayableSession.Callable)
                    return type(self)(buffer, result)
                else:
                    buffer.append(result)
                    return result

    class Player(object):
        def __init__(self, buffer):
            self._buffer = buffer

        def __call__(self, *args, **kw):
            buffer = object.__getattribute__(self, '_buffer')
            result = buffer.popleft()
            if result is ReplayableSession.Callable:
                return self
            else:
                return result

        @property
        def _sqla_unwrap(self):
            return None

        def __getattribute__(self, key):
            try:
                return object.__getattribute__(self, key)
            except AttributeError:
                pass
            buffer = object.__getattribute__(self, '_buffer')
            result = buffer.popleft()
            if result is ReplayableSession.Callable:
                return self
            elif result is ReplayableSession.NoAttribute:
                raise AttributeError(key)
            else:
                return result
