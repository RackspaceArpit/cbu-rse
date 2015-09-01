#!/usr/bin/env python

"""
@file rse.py
@author Kurt Griffiths, Xuan Yu, et al.
$Author$  <=== populated-by-subversion
$Revision$  <=== populated-by-subversion
$Date$  <=== populated-by-subversion

@brief
Rackspace RSE Server. Run with -h for command-line options.

@pre
Servers have syncronized clocks (ntpd).
Python 2.7 with the following installed: pymongo, webob, and argparse
ulimit -n 4096 # or better
sysctl -w net.core.somaxconn="4096" # or better
"""

import os
import sys
import time
import logging
import logging.handlers
import os.path
import ConfigParser

import pymongo
import argparse

from rax.http import rawr

import auth_cache
from controllers.shared import *
from controllers.health_controller import *
from controllers.main_controller import *


class RseApplication(rawr.Rawr):
    """RSE app for encapsulating initialization"""

    def __init__(self):
        rawr.Rawr.__init__(self)

        # Initialize config paths
        dir_path = os.path.dirname(os.path.abspath(__file__))
        local_config_path = os.path.join(dir_path, 'rse.conf')
        global_config_path = '/etc/rse.conf'
        default_config_path = os.path.join(dir_path, 'rse.default.conf')

        # Parse options
        config = ConfigParser.ConfigParser(
            defaults={
                'timeout': '5',
                'authtoken-prefix': '',
                'replica-set': '[none]',
                'filelog': 'yes',
                'console': 'no',
                'syslog': 'no',
                'event-ttl': '120'
            }
        )

        config.read(default_config_path)

        if os.path.exists(local_config_path):
            config.read(local_config_path)
        elif os.path.exists(global_config_path):
            config.read(global_config_path)

        # Add the log message handler to the logger
        # Set up a specific logger with our desired output level
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG if config.get(
            'logging', 'verbose') else logging.WARNING)

        formatter = logging.Formatter(
            '%(asctime)s - RSE - PID %(process)d - %(funcName)s:%(lineno)d - %(levelname)s - %(message)s')

        if config.getboolean('logging', 'console'):
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        if config.getboolean('logging', 'filelog'):
            handler = logging.handlers.RotatingFileHandler(
                config.get('logging', 'filelog-path'), maxBytes=5 * 1024 * 1024, backupCount=5)
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        if config.getboolean('logging', 'syslog'):
            handler = logging.handlers.SysLogHandler(
                address=config.get('logging', 'syslog-address'))
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        # FastCache for Auth Token
        authtoken_prefix = config.get('authcache', 'authtoken-prefix')
        provider = config.get('authcache', 'provider')
        logger.debug(
            {
                'token prefix': authtoken_prefix,
                'cache provider': provider,
            }
        )
        if provider == 'memcached':
            authtoken_cache = auth_cache.MemcachedAuthCache(config, logger)
        elif provider == 'cassandra':
            authtoken_cache = auth_cache.CassandraAuthCache(config, logger)
        else:
            logger.error('No auth_cache provider for: %s', provider)
            sys.exit(1)

        # Connnect to MongoDB
        mongo_db, mongo_db_master = self.init_database(logger, config)

        # Get auth requirements
        test_mode = config.getboolean('rse', 'test')

        # Setup routes
        shared = Shared(logger, authtoken_cache)

        health_options = dict(shared=shared,
                              mongo_db=mongo_db_master,
                              test_mode=test_mode)
        self.add_route(r"/health$", HealthController, health_options)

        main_options = dict(shared=shared,
                            mongo_db=mongo_db,
                            test_mode=test_mode)
        self.add_route(r"/.+", MainController, main_options)

    def init_database(self, logger, config):
        event_ttl = config.getint('rse', 'event-ttl')
        mongo_uri = config.get('mongodb', 'uri')
        db_name = config.get('mongodb', 'database')
        use_ssl = config.getboolean('mongodb', 'use_ssl')

        db_connections_ok = False
        for i in range(10):
            try:
                # Master instance connection for the health checker
                connection_master = pymongo.MongoClient(
                    mongo_uri,
                    read_preference=pymongo.ReadPreference.PRIMARY,
                    ssl=use_ssl
                )
                mongo_db_master = connection_master[db_name]

                # General connection for regular requests
                # Note: Use one global connection to the DB across all handlers
                # (pymongo manages its own connection pool)
                replica_set = config.get('mongodb', 'replica-set')
                if replica_set == '[none]':
                    connection = pymongo.MongoClient(
                        mongo_uri,
                        read_preference=pymongo.ReadPreference.SECONDARY,
                        ssl=use_ssl
                    )
                else:
                    try:
                        connection = pymongo.MongoReplicaSetClient(
                            mongo_uri,
                            replicaSet=replica_set,
                            read_preference=pymongo.ReadPreference.SECONDARY,
                            ssl=use_ssl
                        )
                    except Exception as ex:
                        logger.error(
                            "Mongo connection exception: %s" % (ex.message))
                        sys.exit(1)

                mongo_db = connection[db_name]
                mongo_db_master = connection_master[db_name]
                db_connections_ok = True

            except pymongo.errors.AutoReconnect:
                logger.warning(
                    "Got AutoReconnect on startup while attempting to connect to DB. Retrying...")
                time.sleep(0.5)

            except Exception as ex:
                logger.error(
                    "Error on startup while attempting to connect to DB: " + str_utf8(ex))
                sys.exit(1)

        if not db_connections_ok:
            logger.error("Could not set up db connections")
            sys.exit(1)

        # Initialize events collection
        db_events_collection_ok = False
        for i in range(10):
            try:
                # get rid of deprecated indexes so they don't bloat our working
                # set size
                try:
                    mongo_db_master.events.drop_index('uuid_1_channel_1')
                except pymongo.errors.OperationFailure:
                    # Index already deleted
                    pass

                try:
                    mongo_db_master.events.drop_index('created_at_1')
                except pymongo.errors.OperationFailure:
                    # Index already deleted
                    pass

                # Order matters - want exact matches first, and ones that will pair down the result set the fastest
                # NOTE: MongoDB does not use multiple indexes per query, so we want to put all query fields in the
                # index.
                mongo_db_master.events.ensure_index(
                    [('channel', pymongo.ASCENDING), ('_id', pymongo.ASCENDING), ('uuid', pymongo.ASCENDING)], name='get_events')

                # Drop TTL index if a different number of seconds was requested
                index_info = mongo_db_master.events.index_information()

                if 'ttl' in index_info:
                    index = index_info['ttl']
                    if ('expireAfterSeconds' not in index) or index['expireAfterSeconds'] != event_ttl:
                        mongo_db_master.events.drop_index('ttl')

                mongo_db_master.events.ensure_index(
                    'created_at', expireAfterSeconds=event_ttl, name='ttl')

                # WARNING: Counter must start at a value greater than 0 per the RSE spec, so
                # we set to 0 since the id generation logic always adds one to get
                # the next id, so we will start at 1 for the first event
                if not mongo_db_master.counters.find_one({'_id': 'last_known_id'}):
                    mongo_db_master.counters.insert(
                        {'_id': 'last_known_id', 'c': 0})

                db_events_collection_ok = True
                break

            except pymongo.errors.AutoReconnect:
                logger.warning(
                    "Got AutoReconnect on startup while attempting to set up events collection. Retrying...")
                time.sleep(0.5)

            except Exception as ex:
                logger.error(
                    "Error on startup while attempting to initialize events collection: " + str_utf8(ex))
                sys.exit(1)

        if not db_events_collection_ok:
            logger.error("Could not setup events connections")
            sys.exit(1)

        return (mongo_db, mongo_db_master)

# WSGI app
app = RseApplication()

# If running this script directly, startup a basic WSGI server for testing
if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    httpd = make_server('', 8000, app)
    print "Serving on port 8000..."
    httpd.serve_forever()
