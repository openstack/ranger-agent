# Copyright (c) 2012 OpenStack Foundation
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging
from ord.api import config as api_config
from ord.api.controllers.v1 import api
from ord.api import middleware
from ord.i18n import _
from ord.i18n import _LW
from ord.openstack.common import log
from ord import service
import os
from oslo_config import cfg
import oslo_messaging as messaging
from paste import deploy
import pecan
from werkzeug import serving

LOG = log.getLogger(__name__)

CONF = cfg.CONF

OPTS = [
    cfg.StrOpt('api_paste_config',
               default="api-paste.ini",
               help="Configuration file for WSGI definition of API."
               ),
    cfg.IntOpt('api_workers', default=1,
               help='Number of workers for ORD API server.'),
]

API_OPTS = [
    cfg.BoolOpt('pecan_debug',
                default=False,
                help='Toggle Pecan Debug Middleware.'),
]

CONF.register_opts(OPTS)
CONF.register_opts(API_OPTS, group='api')


def get_pecan_config():
    # Set up the pecan configuration
    filename = api_config.__file__.replace('.pyc', '.py')
    return pecan.configuration.conf_from_file(filename)


def setup_app(pecan_config=None, extra_hooks=None):
    app_hooks = []
    if extra_hooks:
        app_hooks.extend(extra_hooks)

    if not pecan_config:
        pecan_config = get_pecan_config()

    pecan.configuration.set_config(dict(pecan_config), overwrite=True)

    # NOTE(sileht): pecan debug won't work in multi-process environment
    pecan_debug = CONF.api.pecan_debug
    if service.get_workers('api') != 1 and pecan_debug:
        pecan_debug = False
        LOG.warning(_LW('pecan_debug cannot be enabled, if workers is > 1, '
                        'the value is overrided with False'))

    app = pecan.make_app(
        pecan_config.app.root,
        debug=pecan_debug,
        force_canonical=getattr(pecan_config.app, 'force_canonical', True),
        hooks=app_hooks,
        wrap_app=middleware.ParsableErrorMiddleware,
        guess_content_type_from_ext=False
    )

    transport = messaging.get_rpc_transport(cfg.CONF)
    target = messaging.Target(topic='ord-listener-q', server=cfg.CONF.host)
    endpoints = [api.ListenerQueueHandler()]
    server = messaging.get_rpc_server(transport,
                                      target,
                                      endpoints,
                                      executor='eventlet')

    server.start()
    LOG.info("*********************************started")

    return app


class VersionSelectorApplication(object):
    def __init__(self):
        pc = get_pecan_config()

        def not_found(environ, start_response):
            start_response('404 Not Found', [])
            return []
        self.v1 = setup_app(pecan_config=pc)

    def __call__(self, environ, start_response):
        return self.v1(environ, start_response)


def load_app():
    # Build the WSGI app
    cfg_file = None
    cfg_path = cfg.CONF.api_paste_config
    if not os.path.isabs(cfg_path):
        cfg_file = CONF.find_file(cfg_path)
    elif os.path.exists(cfg_path):
        cfg_file = cfg_path

    if not cfg_file:
        raise cfg.ConfigFilesNotFoundError([cfg.CONF.api_paste_config])
    LOG.info("Full WSGI config used: %s" % cfg_file)
    return deploy.loadapp("config:" + cfg_file)


def build_server():
    app = load_app()
    # Create the WSGI server and start it
    host, port = cfg.CONF.api.host, cfg.CONF.api.port

    LOG.info(_('Starting server in PID %s') % os.getpid())
    LOG.info(_("Configuration:"))
    cfg.CONF.log_opt_values(LOG, logging.INFO)

    if host == '0.0.0.0':
        LOG.info(_(
            'serving on 0.0.0.0:%(sport)s, view at http://127.0.0.1:%(vport)s')
            % ({'sport': port, 'vport': port}))
    else:
        LOG.info(_("serving on http://%(host)s:%(port)s") % (
                 {'host': host, 'port': port}))

    workers = service.get_workers('api')
    serving.run_simple(cfg.CONF.api.host, cfg.CONF.api.port,
                       app, processes=workers)


def app_factory(global_config, **local_conf):
    return VersionSelectorApplication()
