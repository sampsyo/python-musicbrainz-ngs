# This file is part of the musicbrainzngs library
# Copyright (C) Alastair Porter, Adrian Sampson, and others
# This file is distributed under a BSD-2-Clause type license.
# See the COPYING file for more information.

import re
import threading
import time
import logging
import xml.etree.ElementTree as etree
from xml.parsers import expat
import requests
from requests.auth import HTTPDigestAuth

from musicbrainzngs import mbxml
from musicbrainzngs import util
from musicbrainzngs import compat

_version = "0.5dev"
_log = logging.getLogger("musicbrainzngs")


# Constants for validation.

RELATABLE_TYPES = ['area', 'artist', 'label', 'recording', 'release', 'release-group', 'url', 'work']
RELATION_INCLUDES = [entity + '-rels' for entity in RELATABLE_TYPES]

VALID_INCLUDES = {
    'artist': [
        "recordings", "releases", "release-groups", "works", # Subqueries
        "various-artists", "discids", "media",
        "aliases", "tags", "user-tags", "ratings", "user-ratings", # misc
        "annotation"
    ] + RELATION_INCLUDES,
    'annotation': [

    ],
    'label': [
        "releases", # Subqueries
        "discids", "media",
        "aliases", "tags", "user-tags", "ratings", "user-ratings", # misc
        "annotation"
    ] + RELATION_INCLUDES,
    'recording': [
        "artists", "releases", # Subqueries
        "discids", "media", "artist-credits",
        "tags", "user-tags", "ratings", "user-ratings", # misc
        "annotation", "aliases"
    ] + RELATION_INCLUDES,
    'release': [
        "artists", "labels", "recordings", "release-groups", "media",
        "artist-credits", "discids", "puids", "echoprints", "isrcs",
        "recording-level-rels", "work-level-rels", "annotation", "aliases"
    ] + RELATION_INCLUDES,
    'release-group': [
        "artists", "releases", "discids", "media",
        "artist-credits", "tags", "user-tags", "ratings", "user-ratings", # misc
        "annotation", "aliases"
    ] + RELATION_INCLUDES,
    'work': [
        "artists", # Subqueries
        "aliases", "tags", "user-tags", "ratings", "user-ratings", # misc
        "annotation"
    ] + RELATION_INCLUDES,
    'url': RELATION_INCLUDES,
    'discid': [
        "artists", "labels", "recordings", "release-groups", "media",
        "artist-credits", "discids", "puids", "echoprints", "isrcs",
        "recording-level-rels", "work-level-rels"
    ] + RELATION_INCLUDES,
    'echoprint': ["artists", "releases"],
    'puid': ["artists", "releases", "puids", "echoprints", "isrcs"],
    'isrc': ["artists", "releases", "puids", "echoprints", "isrcs"],
    'iswc': ["artists"],
    'collection': ['releases'],
}
VALID_BROWSE_INCLUDES = {
    'releases': ["artist-credits", "labels", "recordings",
                "release-groups", "media", "discids"] + RELATION_INCLUDES,
    'recordings': ["artist-credits", "tags", "ratings", "user-tags",
                  "user-ratings"] + RELATION_INCLUDES,
    'labels': ["aliases", "tags", "ratings",
               "user-tags", "user-ratings"] + RELATION_INCLUDES,
    'artists': ["aliases", "tags", "ratings",
                "user-tags", "user-ratings"] + RELATION_INCLUDES,
    'urls': RELATION_INCLUDES,
    'release-groups': ["artist-credits", "tags", "ratings",
                       "user-tags", "user-ratings"] + RELATION_INCLUDES
}

#: These can be used to filter whenever releases are includes or browsed
VALID_RELEASE_TYPES = [
	"nat", "album", "single", "ep", "compilation", "soundtrack", "spokenword",
	"interview", "audiobook", "live", "remix", "other"
]
#: These can be used to filter whenever releases or release-groups are involved
VALID_RELEASE_STATUSES = ["official", "promotion", "bootleg", "pseudo-release"]
VALID_SEARCH_FIELDS = {
    'annotation': [
        'entity', 'name', 'text', 'type'
    ],
    'artist': [
        'arid', 'artist', 'artistaccent', 'alias', 'begin', 'comment',
        'country', 'end', 'ended', 'gender', 'ipi', 'sortname', 'tag', 'type'
    ],
    'label': [
        'alias', 'begin', 'code', 'comment', 'country', 'end', 'ended',
        'ipi', 'label', 'labelaccent', 'laid', 'sortname', 'type', 'tag'
    ],
    'recording': [
        'arid', 'artist', 'artistname', 'creditname', 'comment',
        'country', 'date', 'dur', 'format', 'isrc', 'number',
        'position', 'primarytype', 'puid', 'qdur', 'recording',
        'recordingaccent', 'reid', 'release', 'rgid', 'rid',
        'secondarytype', 'status', 'tnum', 'tracks', 'tracksrelease',
        'tag', 'type'
    ],
    'release-group': [
        'arid', 'artist', 'artistname', 'comment', 'creditname',
        'primarytype', 'rgid', 'releasegroup', 'releasegroupaccent',
        'releases', 'release', 'reid', 'secondarytype', 'status',
        'tag', 'type'
    ],
    'release': [
        'arid', 'artist', 'artistname', 'asin', 'barcode', 'creditname',
        'catno', 'comment', 'country', 'creditname', 'date', 'discids',
        'discidsmedium', 'format', 'laid', 'label', 'lang', 'mediums',
        'primarytype', 'puid', 'reid', 'release', 'releaseaccent',
        'rgid', 'script', 'secondarytype', 'status', 'tag', 'tracks',
        'tracksmedium', 'type'
    ],
    'work': [
        'alias', 'arid', 'artist', 'comment', 'iswc', 'lang', 'tag',
        'type', 'wid', 'work', 'workaccent'
    ],
}


# Exceptions.

class MusicBrainzError(Exception):
	"""Base class for all exceptions related to MusicBrainz."""
	pass

class UsageError(MusicBrainzError):
	"""Error related to misuse of the module API."""
	pass

class InvalidSearchFieldError(UsageError):
	pass

class InvalidIncludeError(UsageError):
	def __init__(self, msg='Invalid Includes', reason=None):
		super(InvalidIncludeError, self).__init__(self)
		self.msg = msg
		self.reason = reason

	def __str__(self):
		return self.msg

class InvalidFilterError(UsageError):
	def __init__(self, msg='Invalid Includes', reason=None):
		super(InvalidFilterError, self).__init__(self)
		self.msg = msg
		self.reason = reason

	def __str__(self):
		return self.msg

class WebServiceError(MusicBrainzError):
	"""Error related to MusicBrainz API requests."""
	def __init__(self, message=None, cause=None):
		"""Pass ``cause`` if this exception was caused by another
		exception.
		"""
		self.message = message
		self.cause = cause

	def __str__(self):
		if self.message:
			msg = "%s, " % self.message
		else:
			msg = ""
		msg += "caused by: %s" % str(self.cause)
		return msg

class NetworkError(WebServiceError):
	"""Problem communicating with the MB server."""
	pass

class ResponseError(WebServiceError):
	"""Bad response sent by the MB server."""
	pass

class AuthenticationError(WebServiceError):
	"""Received a HTTP 401 response while accessing a protected resource."""
	pass


# Helpers for validating and formatting allowed sets.

def _check_includes_impl(includes, valid_includes):
    for i in includes:
        if i not in valid_includes:
            raise InvalidIncludeError("Bad includes", "%s is not a valid include" % i)
def _check_includes(entity, inc):
    _check_includes_impl(inc, VALID_INCLUDES[entity])

def _check_filter(values, valid):
	for v in values:
		if v not in valid:
			raise InvalidFilterError(v)

def _check_filter_and_make_params(entity, includes, release_status=[], release_type=[]):
    """Check that the status or type values are valid. Then, check that
    the filters can be used with the given includes. Return a params
    dict that can be passed to _do_mb_query.
    """
    if isinstance(release_status, compat.basestring):
        release_status = [release_status]
    if isinstance(release_type, compat.basestring):
        release_type = [release_type]
    _check_filter(release_status, VALID_RELEASE_STATUSES)
    _check_filter(release_type, VALID_RELEASE_TYPES)

    if (release_status
            and "releases" not in includes and entity != "release"):
        raise InvalidFilterError("Can't have a status with no release include")
    if (release_type
            and "release-groups" not in includes and "releases" not in includes
            and entity not in ["release-group", "release"]):
        raise InvalidFilterError("Can't have a release type"
                "with no releases or release-groups involved")

    # Build parameters.
    params = {}
    if len(release_status):
        params["status"] = "|".join(release_status)
    if len(release_type):
        params["type"] = "|".join(release_type)
    return params

def _docstring(entity, browse=False):
    def _decorator(func):
        if browse:
            includes = ", ".join(VALID_BROWSE_INCLUDES.get(entity, []))
        else:
            includes = ", ".join(VALID_INCLUDES.get(entity, []))
        if func.__doc__:
            func.__doc__ = func.__doc__.format(includes=includes,
                    fields=", ".join(VALID_SEARCH_FIELDS.get(entity, [])))
        return func

    return _decorator


# Global authentication and endpoint details.

user = password = ""
hostname = "musicbrainz.org"
_client = ""
_useragent = ""

def auth(u, p):
	"""Set the username and password to be used in subsequent queries to
	the MusicBrainz XML API that require authentication.
	"""
	global user, password
	user = u
	password = p

def set_useragent(app, version, contact=None):
    """Set the User-Agent to be used for requests to the MusicBrainz webservice.
    This must be set before requests are made."""
    global _useragent, _client
    if not app or not version:
        raise ValueError("App and version can not be empty")
    if contact is not None:
        _useragent = "%s/%s python-musicbrainz-ngs/%s ( %s )" % (app, version, _version, contact)
    else:
        _useragent = "%s/%s python-musicbrainz-ngs/%s" % (app, version, _version)
    _client = "%s-%s" % (app, version)
    _log.debug("set user-agent to %s" % _useragent)

def set_hostname(new_hostname):
    """Set the base hostname for MusicBrainz webservice requests.
    Defaults to 'musicbrainz.org'."""
    global hostname
    hostname = new_hostname

# Rate limiting.

limit_interval = 1.0
limit_requests = 1
do_rate_limit = True

def set_rate_limit(limit_or_interval=1.0, new_requests=1):
    """Sets the rate limiting behavior of the module. Must be invoked
    before the first Web service call.
    If the `limit_or_interval` parameter is set to False then
    rate limiting will be disabled. If it is a number then only
    a set number of requests (`new_requests`) will be made per
    given interval (`limit_or_interval`).
    """
    global limit_interval
    global limit_requests
    global do_rate_limit
    if isinstance(limit_or_interval, bool):
        do_rate_limit = limit_or_interval
    else:
        if limit_or_interval <= 0.0:
            raise ValueError("limit_or_interval can't be less than 0")
        if new_requests <= 0:
            raise ValueError("new_requests can't be less than 0")
        do_rate_limit = True
        limit_interval = limit_or_interval
        limit_requests = new_requests

class _rate_limit(object):
    """A decorator that limits the rate at which the function may be
    called. The rate is controlled by the `limit_interval` and
    `limit_requests` global variables.  The limiting is thread-safe;
    only one thread may be in the function at a time (acts like a
    monitor in this sense). The globals must be set before the first
    call to the limited function.
    """
    def __init__(self, fun):
        self.fun = fun
        self.last_call = 0.0
        self.lock = threading.Lock()
        self.remaining_requests = None # Set on first invocation.

    def _update_remaining(self):
        """Update remaining requests based on the elapsed time since
        they were last calculated.
        """
        # On first invocation, we have the maximum number of requests
        # available.
        if self.remaining_requests is None:
            self.remaining_requests = float(limit_requests)

        else:
            since_last_call = time.time() - self.last_call
            self.remaining_requests += since_last_call * \
                                       (limit_requests / limit_interval)
            self.remaining_requests = min(self.remaining_requests,
                                          float(limit_requests))

        self.last_call = time.time()

    def __call__(self, *args, **kwargs):
        with self.lock:
            if do_rate_limit:
                self._update_remaining()

                # Delay if necessary.
                while self.remaining_requests < 0.999:
                    time.sleep((1.0 - self.remaining_requests) *
                               (limit_requests / limit_interval))
                    self._update_remaining()

                # Call the original function, "paying" for this call.
                self.remaining_requests -= 1.0
            return self.fun(*args, **kwargs)


# Core (internal) functions for calling the MB API.

# Get the XML parsing exceptions to catch. The behavior chnaged with Python 2.7
# and ElementTree 1.3.
if hasattr(etree, 'ParseError'):
	ETREE_EXCEPTIONS = (etree.ParseError, expat.ExpatError)
else:
	ETREE_EXCEPTIONS = (expat.ExpatError)

@_rate_limit
def _mb_request(path, method='GET', auth_required=False, client_required=False,
				args=None, data=None, body=None):
	"""Makes a request for the specified `path` (endpoint) on /ws/2 on
	the globally-specified hostname. Parses the responses and returns
	the resulting object.  `auth_required` and `client_required` control
	whether exceptions should be raised if the client and
	username/password are left unspecified, respectively.
	"""
	if args is None:
		args = {}
	else:
		args = dict(args) or {}

	if _useragent == "":
		raise UsageError("set a proper user-agent with "
						 "set_useragent(\"application name\", \"application version\", \"contact info (preferably URL or email for your application)\")")

	if client_required:
		args["client"] = _client

	headers = {}
	if body:
		headers['Content-Type'] = 'application/xml; charset=UTF-8'
	else:
		# Explicitly indicate zero content length if no request data
		# will be sent (avoids HTTP 411 error).
		headers['Content-Length'] = '0'

	req = requests.Request(
		method,
		'http://{0}/ws/2/{1}'.format(hostname, path),
		params=args,
		auth=HTTPDigestAuth(user, password) if auth_required else None,
		headers=headers,
		data=body,
	)

	# Make request (with retries).
	session = requests.Session()
	adapter = requests.adapters.HTTPAdapter(max_retries=8)
	session.mount('http://', adapter)
	session.mount('https://', adapter)
	try:
		resp = session.send(req.prepare(), allow_redirects=True)
	except requests.RequestException as exc:
		raise NetworkError(cause=exc)
	if resp.status_code != 200:
		raise ResponseError(
			'API responded with code {0}'.format(resp.status_code)
		)

	# Parse the response.
	try:
		return mbxml.parse_message(resp.content)
	except UnicodeError as exc:
		raise ResponseError(cause=exc)
	except Exception as exc:
		if isinstance(exc, ETREE_EXCEPTIONS):
			raise ResponseError(cause=exc)
		else:
			raise

def _is_auth_required(entity, includes):
	""" Some calls require authentication. This returns
	True if a call does, False otherwise
	"""
	if "user-tags" in includes or "user-ratings" in includes:
		return True
	elif entity.startswith("collection"):
		return True
	else:
		return False

def _do_mb_query(entity, id, includes=[], params={}):
	"""Make a single GET call to the MusicBrainz XML API. `entity` is a
	string indicated the type of object to be retrieved. The id may be
	empty, in which case the query is a search. `includes` is a list
	of strings that must be valid includes for the entity type. `params`
	is a dictionary of additional parameters for the API call. The
	response is parsed and returned.
	"""
	# Build arguments.
	if not isinstance(includes, list):
		includes = [includes]
	_check_includes(entity, includes)
	auth_required = _is_auth_required(entity, includes)
	args = dict(params)
	if len(includes) > 0:
		inc = " ".join(includes)
		args["inc"] = inc

	# Build the endpoint components.
	path = '%s/%s' % (entity, id)
	return _mb_request(path, 'GET', auth_required, args=args)

def _do_mb_search(entity, query='', fields={},
		  limit=None, offset=None, strict=False):
	"""Perform a full-text search on the MusicBrainz search server.
	`query` is a lucene query string when no fields are set,
	but is escaped when any fields are given. `fields` is a dictionary
	of key/value query parameters. They keys in `fields` must be valid
	for the given entity type.
	"""
	# Encode the query terms as a Lucene query string.
	query_parts = []
	if query:
		clean_query = util._unicode(query)
		if fields:
			clean_query = re.sub(r'([+\-&|!(){}\[\]\^"~*?:\\])',
					r'\\\1', clean_query)
			if strict:
				query_parts.append('"%s"' % clean_query)
			else:
				query_parts.append(clean_query.lower())
		else:
			query_parts.append(clean_query)
	for key, value in fields.items():
		# Ensure this is a valid search field.
		if key not in VALID_SEARCH_FIELDS[entity]:
			raise InvalidSearchFieldError(
				'%s is not a valid search field for %s' % (key, entity)
			)

		# Escape Lucene's special characters.
		value = util._unicode(value)
		value = re.sub(r'([+\-&|!(){}\[\]\^"~*?:\\\/])', r'\\\1', value)
		if value:
			if strict:
				query_parts.append('%s:"%s"' % (key, value))
			else:
				value = value.lower() # avoid AND / OR
				query_parts.append('%s:(%s)' % (key, value))
	if strict:
		full_query = ' AND '.join(query_parts).strip()
	else:
		full_query = ' '.join(query_parts).strip()

	if not full_query:
		raise ValueError('at least one query term is required')

	# Additional parameters to the search.
	params = {'query': full_query}
	if limit:
		params['limit'] = str(limit)
	if offset:
		params['offset'] = str(offset)

	return _do_mb_query(entity, '', [], params)

def _do_mb_delete(path):
	"""Send a DELETE request for the specified object.
	"""
	return _mb_request(path, 'DELETE', True, True)

def _do_mb_put(path):
	"""Send a PUT request for the specified object.
	"""
	return _mb_request(path, 'PUT', True, True)

def _do_mb_post(path, body):
	"""Perform a single POST call for an endpoint with a specified
	request body.
	"""
	return _mb_request(path, 'POST', True, True, body=body)


# The main interface!

# Single entity by ID

@_docstring('artist')
def get_artist_by_id(id, includes=[], release_status=[], release_type=[]):
    """Get the artist with the MusicBrainz `id` as a dict with an 'artist' key.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("artist", includes,
                                           release_status, release_type)
    return _do_mb_query("artist", id, includes, params)

@_docstring('label')
def get_label_by_id(id, includes=[], release_status=[], release_type=[]):
    """Get the label with the MusicBrainz `id` as a dict with a 'label' key.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("label", includes,
                                           release_status, release_type)
    return _do_mb_query("label", id, includes, params)

@_docstring('recording')
def get_recording_by_id(id, includes=[], release_status=[], release_type=[]):
    """Get the recording with the MusicBrainz `id` as a dict
    with a 'recording' key.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("recording", includes,
                                           release_status, release_type)
    return _do_mb_query("recording", id, includes, params)

@_docstring('release')
def get_release_by_id(id, includes=[], release_status=[], release_type=[]):
    """Get the release with the MusicBrainz `id` as a dict with a 'release' key.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("release", includes,
                                           release_status, release_type)
    return _do_mb_query("release", id, includes, params)

@_docstring('release-group')
def get_release_group_by_id(id, includes=[],
                            release_status=[], release_type=[]):
    """Get the release group with the MusicBrainz `id` as a dict
    with a 'release-group' key.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("release-group", includes,
                                           release_status, release_type)
    return _do_mb_query("release-group", id, includes, params)

@_docstring('work')
def get_work_by_id(id, includes=[]):
    """Get the work with the MusicBrainz `id` as a dict with a 'work' key.

    *Available includes*: {includes}"""
    return _do_mb_query("work", id, includes)

@_docstring('url')
def get_url_by_id(id, includes=[]):
    """Get the url with the MusicBrainz `id` as a dict with a 'url' key.

    *Available includes*: {includes}"""
    return _do_mb_query("url", id, includes)


# Searching

@_docstring('annotation')
def search_annotations(query='', limit=None, offset=None, strict=False, **fields):
    """Search for annotations and return a dict with an 'annotation-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('annotation', query, fields, limit, offset, strict)

@_docstring('artist')
def search_artists(query='', limit=None, offset=None, strict=False, **fields):
    """Search for artists and return a dict with an 'artist-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('artist', query, fields, limit, offset, strict)

@_docstring('label')
def search_labels(query='', limit=None, offset=None, strict=False, **fields):
    """Search for labels and return a dict with a 'label-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('label', query, fields, limit, offset, strict)

@_docstring('recording')
def search_recordings(query='', limit=None, offset=None,
                      strict=False, **fields):
    """Search for recordings and return a dict with a 'recording-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('recording', query, fields, limit, offset, strict)

@_docstring('release')
def search_releases(query='', limit=None, offset=None, strict=False, **fields):
    """Search for recordings and return a dict with a 'recording-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('release', query, fields, limit, offset, strict)

@_docstring('release-group')
def search_release_groups(query='', limit=None, offset=None,
			  strict=False, **fields):
    """Search for release groups and return a dict
    with a 'release-group-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('release-group', query, fields, limit, offset, strict)

@_docstring('work')
def search_works(query='', limit=None, offset=None, strict=False, **fields):
    """Search for works and return a dict with a 'work-list' key.

    *Available search fields*: {fields}"""
    return _do_mb_search('work', query, fields, limit, offset, strict)


# Lists of entities
@_docstring('release')
def get_releases_by_discid(id, includes=[], release_status=[], release_type=[]):
    """Search for releases with a :musicbrainz:`Disc ID`.

    The result is a dict with either a 'disc' or a 'cdstub' key.
    A 'disc' has a 'release-list' and a 'cdstub' key has direct 'artist'
    and 'title' keys.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("discid", includes, release_status,
                                           release_type=release_type)
    return _do_mb_query("discid", id, includes, params)

@_docstring('recording')
def get_recordings_by_echoprint(echoprint, includes=[], release_status=[],
                                release_type=[]):
    """Search for recordings with an `echoprint <http://echoprint.me>`_.
    The result is a dict with an 'echoprint' key,
    which again includes a 'recording-list'.

    The preferred fingerprint method is :musicbrainz:`AcoustID`.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("echoprint", includes,
                                           release_status, release_type)
    return _do_mb_query("echoprint", echoprint, includes, params)

@_docstring('recording')
def get_recordings_by_puid(puid, includes=[], release_status=[],
                           release_type=[]):
    """Search for recordings with a :musicbrainz:`PUID`.
    The result is a dict with a 'puid' key,
    which again includes a 'recording-list'.

    The preferred fingerprint method is :musicbrainz:`AcoustID`.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("puid", includes,
                                           release_status, release_type)
    return _do_mb_query("puid", puid, includes, params)

@_docstring('recording')
def get_recordings_by_isrc(isrc, includes=[], release_status=[],
                           release_type=[]):
    """Search for recordings with an :musicbrainz:`ISRC`.
    The result is a dict with an 'isrc' key,
    which again includes a 'recording-list'.

    *Available includes*: {includes}"""
    params = _check_filter_and_make_params("isrc", includes,
                                           release_status, release_type)
    return _do_mb_query("isrc", isrc, includes, params)

@_docstring('work')
def get_works_by_iswc(iswc, includes=[]):
    """Search for works with an :musicbrainz:`ISWC`.
    The result is a dict with a`work-list`.

    *Available includes*: {includes}"""
    return _do_mb_query("iswc", iswc, includes)


def _browse_impl(entity, includes, valid_includes, limit, offset, params, release_status=[], release_type=[]):
    _check_includes_impl(includes, valid_includes)
    p = {}
    for k,v in params.items():
        if v:
            p[k] = v
    if len(p) > 1:
        raise Exception("Can't have more than one of " + ", ".join(params.keys()))
    if limit: p["limit"] = limit
    if offset: p["offset"] = offset
    filterp = _check_filter_and_make_params(entity, includes, release_status, release_type)
    p.update(filterp)
    return _do_mb_query(entity, "", includes, p)

# Browse methods
# Browse include are a subset of regular get includes, so we check them here
# and the test in _do_mb_query will pass anyway.
@_docstring('artists', browse=True)
def browse_artists(recording=None, release=None, release_group=None,
                   includes=[], limit=None, offset=None):
    """Get all artists linked to a recording, a release or a release group.
    You need to give one MusicBrainz ID.

    *Available includes*: {includes}"""
    # optional parameter work?
    valid_includes = VALID_BROWSE_INCLUDES['artists']
    params = {"recording": recording,
              "release": release,
              "release-group": release_group}
    return _browse_impl("artist", includes, valid_includes,
                        limit, offset, params)

@_docstring('labels', browse=True)
def browse_labels(release=None, includes=[], limit=None, offset=None):
    """Get all labels linked to a relase. You need to give a MusicBrainz ID.

    *Available includes*: {includes}"""
    valid_includes = VALID_BROWSE_INCLUDES['labels']
    params = {"release": release}
    return _browse_impl("label", includes, valid_includes,
                        limit, offset, params)

@_docstring('recordings', browse=True)
def browse_recordings(artist=None, release=None, includes=[],
                      limit=None, offset=None):
    """Get all recordings linked to an artist or a release.
    You need to give one MusicBrainz ID.

    *Available includes*: {includes}"""
    valid_includes = VALID_BROWSE_INCLUDES['recordings']
    params = {"artist": artist,
              "release": release}
    return _browse_impl("recording", includes, valid_includes,
                        limit, offset, params)

@_docstring('releases', browse=True)
def browse_releases(artist=None, label=None, recording=None,
                    release_group=None, release_status=[], release_type=[],
                    includes=[], limit=None, offset=None):
    """Get all releases linked to an artist, a label, a recording
    or a release group. You need to give one MusicBrainz ID.

    You can filter by :data:`musicbrainz.VALID_RELEASE_TYPES` or
    :data:`musicbrainz.VALID_RELEASE_STATUSES`.

    *Available includes*: {includes}"""
    # track_artist param doesn't work yet
    valid_includes = VALID_BROWSE_INCLUDES['releases']
    params = {"artist": artist,
              "label": label,
              "recording": recording,
              "release-group": release_group}
    return _browse_impl("release", includes, valid_includes, limit, offset,
                        params, release_status, release_type)

@_docstring('release-groups', browse=True)
def browse_release_groups(artist=None, release=None, release_type=[],
                          includes=[], limit=None, offset=None):
    """Get all release groups linked to an artist or a release.
    You need to give one MusicBrainz ID.

    You can filter by :data:`musicbrainz.VALID_RELEASE_TYPES`.

    *Available includes*: {includes}"""
    valid_includes = VALID_BROWSE_INCLUDES['release-groups']
    params = {"artist": artist,
              "release": release}
    return _browse_impl("release-group", includes, valid_includes,
                        limit, offset, params, [], release_type)

@_docstring('urls', browse=True)
def browse_urls(resource=None, includes=[], limit=None, offset=None):
    """Get urls by actual URL string.
    You need to give a URL string as 'resource'

    *Available includes*: {includes}"""
    # optional parameter work?
    valid_includes = VALID_BROWSE_INCLUDES['urls']
    params = {"resource": resource}
    return _browse_impl("url", includes, valid_includes,
                        limit, offset, params)

# browse_work is defined in the docs but has no browse criteria

# Collections
def get_collections():
    """List the collections for the currently :func:`authenticated <auth>` user
    as a dict with a 'collection-list' key."""
    # Missing <release-list count="n"> the count in the reply
    return _do_mb_query("collection", '')

def get_releases_in_collection(collection):
    """List the releases in a collection.
    Returns a dict with a 'collection' key, which again has a 'release-list'.
    """
    return _do_mb_query("collection", "%s/releases" % collection)

# Submission methods

def submit_barcodes(release_barcode):
    """Submits a set of {release_id1: barcode, ...}"""
    query = mbxml.make_barcode_request(release_barcode)
    return _do_mb_post("release", query)

def submit_puids(recording_puids):
    """Submit PUIDs.
    Submits a set of {recording_id1: [puid1, ...], ...}
    or {recording_id1: puid, ...}.
    """
    rec2puids = dict()
    for (rec, puids) in recording_puids.items():
        rec2puids[rec] = puids if isinstance(puids, list) else [puids]
    query = mbxml.make_puid_request(rec2puids)
    return _do_mb_post("recording", query)

def submit_echoprints(recording_echoprints):
    """Submit echoprints.
    Submits a set of {recording_id1: [echoprint1, ...], ...}
    or {recording_id1: echoprint, ...}.
    """
    rec2echos = dict()
    for (rec, echos) in recording_echoprints.items():
        rec2echos[rec] = echos if isinstance(echos, list) else [echos]
    query = mbxml.make_echoprint_request(rec2echos)
    return _do_mb_post("recording", query)

def submit_isrcs(recording_isrcs):
    """Submit ISRCs.
    Submits a set of {recording-id1: [isrc1, ...], ...}
    or {recording_id1: isrc, ...}.
    """
    rec2isrcs = dict()
    for (rec, isrcs) in recording_isrcs.items():
        rec2isrcs[rec] = isrcs if isinstance(isrcs, list) else [isrcs]
    query = mbxml.make_isrc_request(rec2isrcs)
    return _do_mb_post("recording", query)

def submit_tags(artist_tags={}, recording_tags={}):
    """Submit user tags.
    Artist or recording parameters are of the form:
    {entity_id1: [tag1, ...], ...}
    """
    query = mbxml.make_tag_request(artist_tags, recording_tags)
    return _do_mb_post("tag", query)

def submit_ratings(artist_ratings={}, recording_ratings={}):
    """ Submit user ratings.
    Artist or recording parameters are of the form:
    {entity_id1: rating, ...}
    """
    query = mbxml.make_rating_request(artist_ratings, recording_ratings)
    return _do_mb_post("rating", query)

def add_releases_to_collection(collection, releases=[]):
    """Add releases to a collection.
    Collection and releases should be identified by their MBIDs
    """
    # XXX: Maximum URI length of 16kb means we should only allow ~400 releases
    releaselist = ";".join(releases)
    _do_mb_put("collection/%s/releases/%s" % (collection, releaselist))

def remove_releases_from_collection(collection, releases=[]):
    """Remove releases from a collection.
    Collection and releases should be identified by their MBIDs
    """
    releaselist = ";".join(releases)
    _do_mb_delete("collection/%s/releases/%s" % (collection, releaselist))
