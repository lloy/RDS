# Copyright 2011 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import functools
import datetime
import time
import osprofiler.sqlalchemy
import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy import MetaData
# from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import literal_column
from sqlalchemy.sql.expression import select
from sqlalchemy import exc as sqla_exc

from trove.common import cfg
from trove.openstack.common import log as logging
from trove.common.i18n import _
from trove.db.sqlalchemy import mappers

_ENGINE = None
_MAKER = None


LOG = logging.getLogger(__name__)

CONF = cfg.CONF


def utcnow():
    ts = time.time()
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


class Session(sqlalchemy.orm.session.Session):
    """Custom Session class to avoid SqlAlchemy Session monkey patching."""


class Query(sqlalchemy.orm.query.Query):
    """Subclass of sqlalchemy.query with soft_delete() method."""
    def soft_delete(self, synchronize_session='evaluate'):
        return self.update({'deleted': literal_column('id'),
                            'updated_at': literal_column('updated_at'),
                            'deleted_at': utcnow()},
                           synchronize_session=synchronize_session)


def _is_db_connection_error(args):
    """Return True if error in connecting to db."""
    # NOTE(adam_g): This is currently MySQL specific and needs to be extended
    #               to support Postgres and others.
    # For the db2, the error code is -30081 since the db2 is still not ready
    conn_err_codes = ('2002', '2003', '2006', '2013', '-30081')
    for err_code in conn_err_codes:
        if args.find(err_code) != -1:
            return True
    return False


def configure_db(options, models_mapper=None):
    global _ENGINE
    if not _ENGINE:
        _ENGINE = _create_engine(options)
    if models_mapper:
        models_mapper.map(_ENGINE)
    else:
        from trove.instance import models as base_models
        from trove.datastore import models as datastores_models
        from trove.dns import models as dns_models
        from trove.extensions.mysql import models as mysql_models
        from trove.guestagent import models as agent_models
        from trove.quota import models as quota_models
        from trove.backup import models as backup_models
        from trove.extensions.security_group import models as secgrp_models
        from trove.configuration import models as configurations_models
        from trove.conductor import models as conductor_models
        from trove.cluster import models as cluster_models

        model_modules = [
            base_models,
            datastores_models,
            dns_models,
            mysql_models,
            agent_models,
            quota_models,
            backup_models,
            secgrp_models,
            configurations_models,
            conductor_models,
            cluster_models,
        ]

        models = {}
        for module in model_modules:
            models.update(module.persisted_models())
        mappers.map(_ENGINE, models)


def _thread_yield(dbapi_con, con_record):
    """Ensure other greenthreads get a chance to be executed.

    If we use eventlet.monkey_patch(), eventlet.greenthread.sleep(0) will
    execute instead of time.sleep(0).
    Force a context switch. With common database backends (eg MySQLdb and
    sqlite), there is no implicit yield caused by network I/O since they are
    implemented by C libraries that eventlet cannot monkey patch.
    """
    time.sleep(0)


def _ping_listener(engine, dbapi_conn, connection_rec, connection_proxy):
    """Ensures that MySQL connections are alive.
    """
    # cursor = dbapi_conn.cursor()
    save_should_close_with_result = connection.should_close_with_result
    connection.should_close_with_result = Fals
    try:
        # ping_sql = 'select 1'
        # cursor.execute(ping_sql)
        dbapi_conn.scalar(select([1]))
    except Exception as ex:
        if engine.dialect.is_disconnect(ex, dbapi_conn, cursor):
            msg = 'Database server has gone away: %s' % ex
            LOG.warning(msg)
            # if the database server has gone away, all connections in the pool
            # have become invalid and we can safely close all of them here,
            # rather than waste time on checking of every single connection
            engine.dispose()

            # this will be handled by SQLAlchemy and will force it to create
            # a new connection and retry the original action
            raise sqla_exc.DisconnectionError(msg)
        else:
            raise

def _connect_ping_listener(connection, branch):
    """Ping the server at connection startup.
    """
    if branch:
        return

    save_should_close_with_result = connection.should_close_with_result
    connection.should_close_with_result = False
    try:
        connection.scalar(select([1]))
    except Exception as ex:
        connection.scalar(select([1]))
    finally:
        connection.should_close_with_result = save_should_close_with_result



def _set_session_sql_mode(dbapi_con, connection_rec, sql_mode=None):
    """Set the sql_mode session variable.

    MySQL supports several server modes. The default is None, but sessions
    may choose to enable server modes like TRADITIONAL, ANSI,
    several STRICT_* modes and others.

    Note: passing in '' (empty string) for sql_mode clears
    the SQL mode for the session, overriding a potentially set
    server default.
    """

    cursor = dbapi_con.cursor()
    cursor.execute("SET SESSION sql_mode = %s", [sql_mode])


def _mysql_get_effective_sql_mode(engine):
    """Returns the effective SQL mode for connections from the engine pool.

    Returns ``None`` if the mode isn't available, otherwise returns the mode.

    """
    # Get the real effective SQL mode. Even when unset by
    # our own config, the server may still be operating in a specific
    # SQL mode as set by the server configuration.
    # Also note that the checkout listener will be called on execute to
    # set the mode if it's registered.
    row = engine.execute("SHOW VARIABLES LIKE 'sql_mode'").fetchone()
    if row is None:
        return
    return row[1]


def _mysql_check_effective_sql_mode(engine):
    """Logs a message based on the effective SQL mode for MySQL connections."""
    realmode = _mysql_get_effective_sql_mode(engine)

    if realmode is None:
        LOG.warning(_LW('Unable to detect effective SQL mode'))
        return

    LOG.debug('MySQL server mode set to %s', realmode)

    # 'TRADITIONAL' mode enables several other modes, so
    # we need a substring match here
    if not ('TRADITIONAL' in realmode.upper() or
            'STRICT_ALL_TABLES' in realmode.upper()):
        LOG.warning("MySQL SQL mode is %s" % realmode)


def _mysql_set_mode_callback(engine, sql_mode):
    if sql_mode is not None:
        mode_callback = functools.partial(_set_session_sql_mode,
                                          sql_mode=sql_mode)
        sqlalchemy.event.listen(engine, 'connect', mode_callback)
    _mysql_check_effective_sql_mode(engine)


def _create_engine(options):
    engine_args = {
        'pool_recycle': CONF.database.idle_timeout,
        'encoding': 'utf8',
        'pool_size': 64,
        'convert_unicode': True,
    }

    mysql_sql_mode = CONF.database.mysql_sql_mode
    retry_interval = CONF.database.retry_interval
    LOG.info(_("Creating SQLAlchemy engine with args: %s") % engine_args)

    sql_connection = options['database']['connection']
    db_engine = create_engine(sql_connection, **engine_args)

    sqlalchemy.event.listen(db_engine, "engine_connect", _connect_ping_listener)
    sqlalchemy.event.listen(db_engine, 'checkin', _thread_yield)
    if db_engine.name in ('mysql'):
        # ping_callback = functools.partial(_ping_listener, db_engine)
        # sqlalchemy.event.listen(db_engine, 'checkout', ping_callback)
        if mysql_sql_mode:
            _mysql_set_mode_callback(db_engine, mysql_sql_mode)
    try:
        db_engine.connect()
    except sqla_exc.OperationalError as e:
        if not _is_db_connection_error(e.args[0]):
            raise

        remaining = CONF.database.max_retries
        if remaining == -1:
            remaining = 'infinite'
        while True:
            msg = "SQL connection failed. %s attempts left."
            print msg % remaining
            # LOG.warning(msg % remaining)
            if remaining != 'infinite':
                remaining -= 1
            time.sleep(retry_interval)
            try:
                db_engine.connect()
                break
            except sqla_exc.OperationalError as e:
                if (remaining != 'infinite' and remaining == 0) or \
                        not _is_db_connection_error(e.args[0]):
                    raise
    # if CONF.profiler.enabled and CONF.profiler.trace_sqlalchemy:
    osprofiler.sqlalchemy.add_tracing(sqlalchemy, db_engine, "db")
    return db_engine


def get_maker(engine, autocommit=True, expire_on_commit=False):
    """Return a SQLAlchemy sessionmaker using the given engine."""
    return sqlalchemy.orm.sessionmaker(bind=engine,
                                       class_=Session,
                                       autocommit=autocommit,
                                       expire_on_commit=expire_on_commit,
                                       query_cls=Query)


def get_session(autocommit=True, expire_on_commit=False):
    """Helper method to grab session."""
    global _MAKER, _ENGINE
    if not _MAKER:
        if not _ENGINE:
            msg = "***The Database has not been setup!!!***"
            LOG.exception(msg)
            raise RuntimeError(msg)
        _MAKER = get_maker(_ENGINE,
                           autocommit=autocommit,
                           expire_on_commit=expire_on_commit)
    return _MAKER()


def raw_query(model, autocommit=True, expire_on_commit=False):
    return get_session(autocommit, expire_on_commit).query(model)


def clean_db():
    global _ENGINE
    meta = MetaData()
    meta.reflect(bind=_ENGINE)
    with contextlib.closing(_ENGINE.connect()) as con:
        trans = con.begin()
        for table in reversed(meta.sorted_tables):
            if table.name != "migrate_version":
                con.execute(table.delete())
        trans.commit()


def drop_db(options):
    meta = MetaData()
    engine = _create_engine(options)
    meta.bind = engine
    meta.reflect()
    meta.drop_all()
