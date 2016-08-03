from __future__ import print_function
import sys
import uuid
from werkzeug.exceptions import InternalServerError, ServiceUnavailable, BadRequest
from flask import current_app
import ujson

from webserver.external import messybrainz
from webserver.redis_connection import _redis


#: Maximum overall listen size in bytes, to prevent egregious spamming.
MAX_LISTEN_SIZE = 10240

#: The maximum number of tags per listen.
MAX_TAGS_PER_LISTEN = 50

#: The maximum length of a tag
MAX_TAG_SIZE = 64

#: The maximum number of listens returned in a single GET request.
MAX_ITEMS_PER_GET = 100

#: The default number of listens returned in a single GET request.
DEFAULT_ITEMS_PER_GET = 25

MAX_ITEMS_PER_MESSYBRAINZ_LOOKUP = 10


def insert_payload(payload, user, listen_type="import"):
    """ Convert the payload into augmented listens then submit them.
        Returns: augmented_listens
    """
    augmented_listens = _get_augmented_listens(payload, user)
    _send_listens_to_redis(listen_type, augmented_listens)
    return augmented_listens


def _validate_listen(listen):
    """Make sure that required keys are present, filled out and not too large."""

    if 'listened_at' not in listen:
        log_raise_400("JSON document must contain the key listened_at at the top level.", listen)

    try:
        listen['listened_at'] = int(listen['listened_at'])
    except ValueError:
        log_raise_400("JSON document must contain an int value for listened_at.", listen)

    if 'listened_at' in listen and 'track_metadata' in listen and len(listen) > 2:
        log_raise_400("JSON document may only contain listened_at and "
                       "track_metadata top level keys", listen)

    # Basic metadata
    try:
        if not listen['track_metadata']['track_name']:
            log_raise_400("JSON document does not contain required "
                           "track_metadata.track_name.", listen)
        if not listen['track_metadata']['artist_name']:
            log_raise_400("JSON document does not contain required "
                           "track_metadata.artist_name.", listen)
    except KeyError:
        log_raise_400("JSON document does not contain a valid metadata.track_name "
                       "and/or track_metadata.artist_name.", listen)

    if 'additional_info' in listen['track_metadata']:
        # Tags
        if 'tags' in listen['track_metadata']['additional_info']:
            tags = listen['track_metadata']['additional_info']['tags']
            if len(tags) > MAX_TAGS_PER_LISTEN:
                log_raise_400("JSON document may not contain more than %d items in "
                               "track_metadata.additional_info.tags." % MAX_TAGS_PER_LISTEN, listen)
            for tag in tags:
                if len(tag) > MAX_TAG_SIZE:
                    log_raise_400("JSON document may not contain track_metadata.additional_info.tags "
                                   "longer than %d characters." % MAX_TAG_SIZE, listen)
        # MBIDs
        if 'release_mbid' in listen['track_metadata']['additional_info']:
            lmbid = listen['track_metadata']['additional_info']['release_mbid']
            if not is_valid_uuid(lmbid):
                log_raise_400("Release MBID format invalid.", listen)
        if 'recording_mbid' in listen['track_metadata']['additional_info']:
            cmbid = listen['track_metadata']['additional_info']['recording_mbid']
            if not is_valid_uuid(cmbid):
                log_raise_400("Recording MBID format invalid.", listen)
        ambids = listen['track_metadata']['additional_info'].get('artist_mbids', [])
        for ambid in ambids:
            if not is_valid_uuid(ambid):
                log_raise_400("Artist MBID format invalid.", listen)

        # Duration
        if 'duration' in listen['track_metadata']['additional_info']:
            try:
                int(listen['track_metadata']['additional_info']['duration'])
            except ValueError:
                log_raise_400("Track Duration invalid.", listen)

        # Choosen_by_user
        if 'choosen_by_user' in listen['track_metadata']['additional_info']:
            try:
                if int(listen['track_metadata']['additional_info']['choosen_by_user']) not in (0, 1):
                    raise ValueError
            except ValueError:
                log_raise_400("choosen_by_user format invalid.", listen)


def _send_listens_to_redis(listen_type, listens):
    p = _redis.pipeline()
    for listen in listens:
        if listen_type == 'playing_now':
            try:
                p.rpush('playing_now', ujson.dumps(listen).encode('utf-8'))
            except Exception, e:
                print(e)
                current_app.logger.error("Redis rpush playing_now write error: " + str(sys.exc_info()[0]))
                raise InternalServerError("Cannot record playing_now at this time.")
        else:
            try:
                p.rpush('listens', ujson.dumps(listen).encode('utf-8'))
            except Exception, e:
                print(e)
                current_app.logger.error("Redis rpush listens write error: " + str(sys.exc_info()[0]))
                raise InternalServerError("Cannot record listen at this time.")

    p.execute()


# lifted from AcousticBrainz
def is_valid_uuid(u):
    try:
        u = uuid.UUID(u)
        return True
    except ValueError:
        return False


def _get_augmented_listens(payload, user_id):
    """ Converts the payload to augmented list after lookup
        in the MessyBrainz database
    """
    augmented_listens = []
    msb_listens = []
    for l in payload:
        listen = l.copy()   # Create a local object to prevent the mutation of the passed object
        _validate_listen(listen)

        listen['user_id'] = user_id

        msb_listens.append(listen)
        if len(msb_listens) >= MAX_ITEMS_PER_MESSYBRAINZ_LOOKUP:
            augmented_listens.extend(_messybrainz_lookup(msb_listens))
            msb_listens = []

    if msb_listens:
        augmented_listens.extend(_messybrainz_lookup(msb_listens))
    return augmented_listens


def _messybrainz_lookup(listens):

    msb_listens = []
    for listen in listens:
        messy_dict = {
            'artist': listen['track_metadata']['artist_name'],
            'title': listen['track_metadata']['track_name'],
        }
        if 'release_name' in listen['track_metadata']:
            messy_dict['release'] = listen['track_metadata']['release_name']

        if 'additional_info' in listen['track_metadata']:
            ai = listen['track_metadata']['additional_info']
            if 'artist_mbids' in ai and isinstance(ai['artist_mbids'], list):
                messy_dict['artist_mbids'] = ai['artist_mbids']
            if 'release_mbid' in ai:
                messy_dict['release_mbid'] = ai['release_mbid']
            if 'recording_mbid' in ai:
                messy_dict['recording_mbid'] = ai['recording_mbid']
            if 'track_number' in ai:
                messy_dict['track_number'] = ai['track_number']
            if 'spotify_id' in ai:
                messy_dict['spotify_id'] = ai['spotify_id']
        msb_listens.append(messy_dict)

    try:
        msb_responses = messybrainz.submit_listens(msb_listens)
    except messybrainz.exceptions.BadDataException as e:
        log_raise_400(str(e))
    except messybrainz.exceptions.NoDataFoundException:
        return []
    except messybrainz.exceptions.ErrorAddingException as e:
        raise ServiceUnavailable(str(e))

    augmented_listens = []
    for listen, messybrainz_resp in zip(listens, msb_responses['payload']):
        messybrainz_resp = messybrainz_resp['ids']

        if 'additional_info' not in listen['track_metadata']:
            listen['track_metadata']['additional_info'] = {}

        try:
            listen['recording_msid'] = messybrainz_resp['recording_msid']
            listen['track_metadata']['additional_info']['artist_msid'] = messybrainz_resp['artist_msid']
        except KeyError:
            current_app.logger.error("MessyBrainz did not return a proper set of ids")
            raise InternalServerError

        try:
            listen['track_metadata']['additional_info']['release_msid'] = messybrainz_resp['release_msid']
        except KeyError:
            pass

        artist_mbids = messybrainz_resp.get('artist_mbids', [])
        release_mbid = messybrainz_resp.get('release_mbid', None)
        recording_mbid = messybrainz_resp.get('recording_mbid', None)

        if 'artist_mbids'   not in listen['track_metadata']['additional_info'] and \
           'release_mbid'   not in listen['track_metadata']['additional_info'] and \
           'recording_mbid' not in listen['track_metadata']['additional_info']:

            if len(artist_mbids) > 0 and release_mbid and recording_mbid:
                listen['track_metadata']['additional_info']['artist_mbids'] = artist_mbids
                listen['track_metadata']['additional_info']['release_mbid'] = release_mbid
                listen['track_metadata']['additional_info']['recording_mbid'] = recording_mbid

        augmented_listens.append(listen)
    return augmented_listens


def convert_backup_to_native_format(data):
    """
    Converts the imported listen-payload from the lastfm backup file
    to the native payload format.
    """
    payload = []
    for native_lis in data:
        listen = {}
        listen['track_metadata'] = {}
        listen['track_metadata']['additional_info'] = {}

        if 'timestamp' in native_lis and 'unixtimestamp' in native_lis['timestamp']:
            listen['listened_at'] = native_lis['timestamp']['unixtimestamp']

        if 'track' in native_lis:
            if 'name' in native_lis['track']:
                listen['track_metadata']['track_name'] = native_lis['track']['name']
            if 'mbid' in native_lis['track']:
                listen['track_metadata']['additional_info']['recording_mbid'] = native_lis['track']['mbid']
            if 'artist' in native_lis['track']:
                if 'name' in native_lis['track']['artist']:
                    listen['track_metadata']['artist_name'] = native_lis['track']['artist']['name']
                if 'mbid' in native_lis['track']['artist']:
                    listen['track_metadata']['additional_info']['artist_mbids'] = [native_lis['track']['artist']['mbid']]
        payload.append(listen)
    return payload


def log_raise_400(msg, data=""):
    """ Helper function for logging issues with request data and showing error page.
        Logs the message and data, raises BadRequest exception which shows 400 Bad
        Request to the user.
    """

    if isinstance(data, dict):
        data = ujson.dumps(data)

    current_app.logger.debug("BadRequest: %s\nJSON: %s" % (msg, data))
    raise BadRequest(msg)