# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Application logic for Mimic.

The Mimic application must serve two distinct types of requests:

* Control requests are used to interact with Mimc and are handled internally.
* Target requests represent the user's application and processed according
  to the files retrieved from the provided tree.

The target application may include Python code which will expect to run
in a pristine environment, so measures are taken not to invoke any sort of
WSGI framework until it is determined that the request is a control request.
"""

import httplib
import logging
import os
import rfc822
import sys
import time
import urlparse
import yaml

from . import common
from . import control
from . import shell
from . import target_env
from . import target_info

from google.appengine.api import app_identity
from google.appengine.api import appinfo
from google.appengine.api import namespace_manager
from google.appengine.api import users
from google.appengine.ext.webapp.util import run_wsgi_app

# TODO: see if "app.yaml" can be made into a link to the actual
# app.yaml file in the user's workspace.
_NOT_FOUND_PAGE = """
<html>
  <body>
    <h3>404 Not Found</h3>
    Your application's <code>app.yaml</code> file does not have a handler for
    the requested path: <code>%s</code><br>
    <br>
    See <a
    href="https://developers.google.com/appengine/docs/python/config/appconfig">
    https://developers.google.com/appengine/docs/python/config/appconfig</a>
  </body>
</html>
"""

# store most recently seen project_id (dev_appserver only)
_dev_appserver_state = {}


def RespondWithStatus(status_code, expiration_s=0,
                      content_type='text/plain; charset=utf-8',
                      data=None, headers=None):
  """Respond with a status code and optional text/plain; charset=utf-8 data."""
  print 'Content-Type: %s' % content_type
  print 'Status: %d %s' % (status_code, httplib.responses[status_code])
  if expiration_s:
    print 'Expires: %s' % rfc822.formatdate(time.time() + expiration_s)
    print 'Cache-Control: public, max-age=%s' % expiration_s
  if headers:
    for k, v in headers:
      print '{0}: {1}'.format(k, v)
  print ''
  if data is not None:
    print data,


def ServeStaticPage(tree, page):
  """Respond by serving a single static file.

  Args:
    tree: A tree object to use to retrieve files.
    page: A StaticPage object describing the file to be served.
  """
  file_path = page.file_path
  logging.info('Serving static page %s', file_path)
  file_data = tree.GetFileContents(file_path)
  if file_data is None:
    RespondWithStatus(httplib.NOT_FOUND,
                      content_type='text/html; charset=utf-8',
                      data=_NOT_FOUND_PAGE % file_path)
    return
  if page.mime_type is not None:
    content_type = page.mime_type
  else:
    content_type = common.GuessMimeType(file_path)
  # should not raise ConfigurationError, but even that would be ok
  expiration_s = appinfo.ParseExpiration(page.expiration)
  RespondWithStatus(httplib.OK, content_type=content_type,
                    data=file_data, expiration_s=expiration_s)


def ServeScriptPage(tree, config, page, namespace):
  """Respond by invoking a python cgi script.

  Args:
    tree: A tree object to use to retrieve files.
    config: The app's config loaded from the app's app.yaml.
    page: A ScriptPage object describing the file to be served.
    namespace: The datastore and memcache namespace used for metadata.
  """
  logging.info('Serving script page %s', page.script_path)
  env = target_env.TargetEnvironment(tree, config, namespace)
  try:
    env.RunScript(page.script_path, control.LoggingHandler(namespace))
  except target_env.ScriptNotFoundError:
    RespondWithStatus(httplib.NOT_FOUND,
                      data='Error: could not find script %s' % page.script_path)


def _IsAuthorized(page, users_mod):
  """Check whether access to the page is authorized."""
  # page does not require login
  if page.login == target_info.LOGIN_NONE:
    return True

  # admins are always allowed in
  # call get_current_user even though checking is_current_user_admin suffices
  if (users_mod.get_current_user() is not None and
      users_mod.is_current_user_admin()):
    return True

  # treat task queue and cron requests as admin equivalents
  # note: mimic currently does not actually provide cron support
  if (os.environ.get(common.HTTP_X_APPENGINE_QUEUENAME) or
      os.environ.get(common.HTTP_X_APPENGINE_CRON)):
    return True

  # login required and user is currently logged in
  if (page.login == target_info.LOGIN_REQUIRED and
      users_mod.get_current_user() is not None):
    return True

  return False


def _CurrentUrl(force_https=False):
  """Reconstruct the current URL."""
  if force_https:
    scheme = 'https'
  else:
    scheme = os.environ['wsgi.url_scheme']
  url = '{0}://{1}{2}'.format(scheme, os.environ['HTTP_HOST'],
                              os.environ['PATH_INFO'])
  query = os.environ['QUERY_STRING']
  if query:
    url = '{0}?{1}'.format(url, query)
  return url


def RunTargetApp(tree, path_info, namespace, users_mod):
  """Top level handling of target application requests.

  Args:
    tree: A tree object to use to retrieve files.
    path_info: The path to be served.
    namespace: The datastore and memcache namespace used for metadata.
    users_mod: A users module to use for authentication.
  """
  app_yaml = tree.GetFileContents('app.yaml')
  if app_yaml is None:
    RespondWithStatus(httplib.NOT_FOUND, data='Error: no app.yaml file.')
    return
  try:
    config = yaml.safe_load(app_yaml)
  except yaml.YAMLError:
    errmsg = ('Error: app.yaml configuration is missing or invalid: {0}'
              .format(sys.exc_info()[1]))
    RespondWithStatus(httplib.NOT_FOUND, data=errmsg)
    return
  # bail if yaml.safe_load fails to return dict due to malformed yaml input
  if not isinstance(config, dict):
    errmsg = 'Error: app.yaml configuration is missing or invalid.'
    RespondWithStatus(httplib.NOT_FOUND, data=errmsg)
    return
  page = target_info.FindPage(config, path_info)

  if not page:
    RespondWithStatus(httplib.NOT_FOUND,
                      content_type='text/html; charset=utf-8',
                      data=_NOT_FOUND_PAGE % path_info)
    return

  # in production redirect to https for handlers specifying 'secure: always'
  if (page.secure == target_info.SECURE_ALWAYS
      and not common.IsDevMode()
      and os.environ['wsgi.url_scheme'] != 'https'):
    https_url = _CurrentUrl(force_https=True)
    RespondWithStatus(httplib.FOUND, headers=[('Location', https_url)])
    return

  if not _IsAuthorized(page, users_mod):
    user = users_mod.get_current_user()
    if user:
      url = users_mod.create_logout_url(_CurrentUrl())
      message = ('User <b>{0}</b> is not authorized to view this page.<br>'
                 'Please <a href="{1}">logout</a> and then login as an '
                 'authorized user.'.format(user.nickname(), url))
    else:
      url = users_mod.create_login_url(_CurrentUrl())
      message = ('You are not authorized to view this page. '
                 'You may need to <a href="{0}">login</a>.'.format(url))
    RespondWithStatus(httplib.FORBIDDEN, data=message,
                      headers=[('Content-Type', 'text/html; charset=utf-8')])
    return
  # dispatch the page
  if isinstance(page, target_info.StaticPage):
    ServeStaticPage(tree, page)
  elif isinstance(page, target_info.ScriptPage):
    ServeScriptPage(tree, config, page, namespace)
  else:
    raise NotImplementedError('Unrecognized page {0!r}'.format(page))


def GetProjectIdFromHttpHost(environ):
  """Returns the project id from the HTTP_HOST environ var.

  For appspot.com domains, a project id is extracted from the left most
  portion of the subdomain. If no subdomain is specified, or if the project
  id cannot be determined, None is returned. Finally, when the HTTP host
  is 'localhost' or an IPv4 address, None is also returned.

  For custom domains, it's not possible to determine with certainty the
  subdomain vs. the default version hostname. In this case we end up using
  the left most component of the HTTP host.

  Example mapping of HTTP host to project id:

  HTTP Host                                Project Id
  ---------                                ------------
  proj1.your-app-id.appspot.com        ->  'proj1'
  proj1.your-app-id.appspot.com:12345  ->  'proj1'
  proj1-dot-your-app-id.appspot.com    ->  'proj1'
  your-app-id.appspot.com              ->  None
  some-other-app-id.appspot.com        ->  None
  www.mydomain.com                     ->  'www'
  proj2.www.mydomain.com               ->  'proj2'
  localhost                            ->  None
  localhost:8080                       ->  None
  192.168.0.1                          ->  None

  Args:
    environ: The request environ.
  Returns:
    The project id or None.
  """
  # The project id is sent as a "subdomain" of the app, e.g.
  # 'project-id-dot-your-app-id.appspot.com' or
  # 'project-id.your-app-id.appspot.com'

  http_host = environ['HTTP_HOST']
  # use a consistent delimiter
  http_host = http_host.replace('-dot-', '.')
  # remove port number
  http_host = http_host.split(':')[0]

  if (http_host == 'localhost' or
      common.IPV4_REGEX.match(http_host) or
      common.TOP_LEVEL_APPSPOT_COM_REGEX.match(http_host) or
      http_host == app_identity.get_default_version_hostname()):
    return None

  return http_host.split('.')[0]


def GetProjectIdFromQueryParam(environ):
  """Returns the project id from the query string.

  Args:
    environ: The request environ.
  Returns:
    The project id or None.
  """

  qs = environ.get('QUERY_STRING')
  if not qs:
    return None
  # use strict_parsing=False to gracefully ignore bad query strings
  params = dict(urlparse.parse_qsl(qs, strict_parsing=False))
  return params.get(common.config.PROJECT_ID_QUERY_PARAM)


def GetProjectIdFromPathInfo(environ):
  """Returns the project id from the request path.

  Args:
    environ: The request environ.
  Returns:
    The project id or None.
  """
  path_info = environ['PATH_INFO']
  m = common.config.PROJECT_ID_FROM_PATH_INFO_RE.match(path_info)
  if not m:
    return None
  return m.group(1)


def GetProjectId(environ, use_sticky_project_id):
  """Returns the project id from the HTTP request.

  A number of sources for project id are tried in order. See implementation
  details. In addition, note the special dev_appserver case where the project id
  extracted from the query string is returned in subsequent requests if the
  project id cannot be otherwise determined.

  Args:
    environ: The request environ.
    use_sticky_project_id: whether or not to remember the most recently
                           encountered project_id, for use in the dev_appserver
  Returns:
    The project id or None.
  """
  # for task queues, use persisted namespace as the project id
  project_id = environ.get(common.HTTP_X_APPENGINE_CURRENT_NAMESPACE)
  if project_id:
    return project_id
  project_id = GetProjectIdFromQueryParam(environ)
  if project_id:
    if use_sticky_project_id:
      _dev_appserver_state['project_id'] = project_id
    return project_id
  project_id = GetProjectIdFromPathInfo(environ)
  if project_id:
    return project_id
  project_id = GetProjectIdFromHttpHost(environ)
  if project_id:
    return project_id
  if use_sticky_project_id:
    project_id = _dev_appserver_state.get('project_id')
  return project_id


def GetNamespace():
  namespace = GetProjectId(os.environ, common.IsDevMode()) or ''
  # throws BadValueError
  namespace_manager.validate_namespace(namespace)
  return namespace


def RunMimic(create_tree_func, access_key, users_mod=users):
  """Entry point for mimic.

  Args:
    create_tree_func: A callable that creates a common.Tree.
    access_key: Key which grants access to the tree
    users_mod: A users module to use for authentication (default is the
        AppEngine users module).
  """
  # use PATH_INFO to determine if this is a control or target request
  path_info = os.environ['PATH_INFO']

  is_control_request = path_info.startswith(common.CONTROL_PREFIX)
  if is_control_request:
    # some control requests don't require a tree, like /version_id
    requires_tree = control.ControlRequestRequiresTree(path_info)
    requires_namespace = control.ControlRequestRequiresNamespace(path_info)
  else:
    # requests to the target app always require a tree
    requires_tree = True
    requires_namespace = True

  if requires_namespace:
    namespace = GetNamespace()
  else:
    namespace = None

  # Obtains the original namespace for later recovery in the finally clause.
  saved_namespace = namespace_manager.get_namespace()
  namespace_manager.set_namespace(namespace)

  try:
    if requires_tree:
      tree = create_tree_func(namespace, access_key)
    else:
      tree = None

    if is_control_request:
      run_wsgi_app(control.MakeControlApp(tree, namespace))
    elif path_info.startswith(common.SHELL_PREFIX):
      run_wsgi_app(shell.MakeShellApp(tree, namespace))
    else:
      RunTargetApp(tree, path_info, namespace, users_mod)
  finally:
    # Restore the original namespace
    namespace_manager.set_namespace(saved_namespace)
