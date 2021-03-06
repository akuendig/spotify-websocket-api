#!/usr/bin/python
import re
import json
import operator
import binascii
import base64
import logging
from ssl import SSLError
from threading import Thread, Event, RLock

from aplus import Promise

from random import randint
import uuid

import requests
from ws4py.websocket import StreamClosed
from ws4py.client.threadedclient import WebSocketClient

import mechanize
import urllib
from urlparse import urlparse, parse_qs

from .proto import mercury_pb2, metadata_pb2, playlist4changes_pb2, \
    playlist4ops_pb2, playlist4service_pb2, toplist_pb2, bartender_pb2, \
    radio_pb2


base62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
APP_ID = 174829003346

logger = logging.getLogger(__name__)


class SpotifyDisconnectedError(Exception):
    def __init__(self):
        Exception.__init__(self)
        self.message = "The websocket to Spotify is disconnected"


class SpotifyCommandError(Exception):
    def __init__(self, major, minor, msg):
        Exception.__init__(self)
        self.major = major
        self.minor = minor
        self.message = msg


class SpotifyTimeoutError(Exception):
    def __init__(self):
        Exception.__init__(self)
        self.message = "Request to Spotify timed out"


class SpotifyClient(WebSocketClient):
    def __init__(self, url, protocols=None, extensions=None, heartbeat_freq=None,
                 ssl_options=None, headers=None):
        super(SpotifyClient, self).__init__(url, protocols, extensions, heartbeat_freq, ssl_options, headers)
        self.api_object = None

    def set_api(self, api):
        self.api_object = api

    def opened(self):
        self.api_object.login()

    def received_message(self, m):
        self.api_object.recv_packet(m)

    def closed(self, code, message=None):
        self.api_object.disconnect(original_ws=self)


class SpotifyUtil():
    @staticmethod
    def gid2id(gid):
        return binascii.hexlify(gid).rjust(32, "0")

    @staticmethod
    def id2uri(uritype, v):
        res = []
        v = int(v, 16)
        while v > 0:
            res = [v % 62] + res
            v /= 62
        id = ''.join([base62[i] for i in res])
        return "spotify:" + uritype + ":" + id.rjust(22, "0")

    @staticmethod
    def uri2id(uri):
        parts = uri.split(":")
        if len(parts) > 3 and parts[3] == "playlist":
            s = parts[4]
        else:
            s = parts[2]

        v = 0
        for c in s:
            v = v * 62 + base62.index(c)
        return hex(v)[2:-1].rjust(32, "0")

    @staticmethod
    def gid2uri(uritype, gid):
        id = SpotifyUtil.gid2id(gid)
        uri = SpotifyUtil.id2uri(uritype, id)
        return uri

    @staticmethod
    def get_uri_type(uri):
        uri_parts = uri.split(":")

        if len(uri_parts) >= 3 and uri_parts[1] == "local":
            return "local"
        elif len(uri_parts) >= 5:
            return uri_parts[3]
        elif len(uri_parts) >= 4 and uri_parts[3] == "starred":
            return "playlist"
        elif len(uri_parts) >= 3:
            return uri_parts[1]
        else:
            return False

    @staticmethod
    def is_local(uri):
        return SpotifyUtil.get_uri_type(uri) == "local"

    @staticmethod
    def url2uri(url):
        # http://open.spotify.com/user/spotify/playlist/0CoQk8bVPF1famEAZ5BYE4
        # spotify:user:spotify:playlist:0CoQk8bVPF1famEAZ5BYE4
        if not url.startswith("http://open.spotify.com"):
            return None

        url = url.replace("http://open.spotify.com/", '')
        url = url.replace("/", ":")

        return "spotify:" + url


class SpotifyAPI():
    INIT = 0
    CONNECTING = 1
    CONNECTED = 2
    DISCONNECTING = 3
    DISCONNECTED = 4

    def __init__(self, login_callback_func=None, settings=None, fb_access_token=None):
        self.auth_server = "play.spotify.com"

        self.logged_in_marker = Event()
        self.heartbeat_marker = Event()
        self.heartbeat_thread = None
        self.userid = None
        self.username = None
        self.password = None
        self.account_type = None
        self.country = None

        self.fb_access_token = fb_access_token

        self.settings = settings

        self.state = SpotifyAPI.INIT

        self.ws = None
        self.ws_lock = RLock()
        self.seq = 0
        self.cmd_promises = {}
        self.login_callback_func = login_callback_func

    @property
    def is_logged_in(self):
        return self.state == SpotifyAPI.CONNECTED

    def connect(self, username, password, timeout=10):
        with self.ws_lock:
            if self.state == SpotifyAPI.CONNECTED or self.state == SpotifyAPI.CONNECTING:
                return True
            elif self.state == SpotifyAPI.DISCONNECTING:
                return False
            else:
                self.state = SpotifyAPI.CONNECTING

        if not self.settings and not self.auth(username, password):
            return False

        self.username = username
        self.password = password

        with self.ws_lock:
            if self.state is not SpotifyAPI.CONNECTING:
                return False

            logger.info("Connecting to {}...".format(self.settings["wss"]))

            try:
                self.ws = SpotifyClient(self.settings["wss"])
                self.ws.set_api(self)
                self.ws.daemon = True
                self.ws.connect()
            except:
                return False

        if not self.logged_in_marker.wait(timeout=timeout):
            logger.debug("Aborting after unsuccessfully connecting for {} seconds.")
            self.disconnect()
            return False
        else:
            return self.is_logged_in

    def reconnect(self):
        assert self.username and self.password

        logger.debug("Reconnecting...")

        self.disconnect()

        while not self.connect(self.username, self.password, timeout=10):
            logger.debug("Unsuccessful login")

    def disconnect(self, clear_settings=False, original_ws=None):
        with self.ws_lock:
            if self.state == SpotifyAPI.INIT or self.ws is None:
                # Nothing to do when we have not connected
                return
            elif self.state == SpotifyAPI.DISCONNECTING or self.state == SpotifyAPI.DISCONNECTED:
                # Nothing to do if we are already disconnected
                return
            elif original_ws and self.ws is not original_ws:
                # Prevent old web socket that disconnects from closing the already opened
                # new web socket.
                return
            elif self.state == SpotifyAPI.CONNECTING:
                # We assume that something went wrong and that we need to start from scratch
                clear_settings = True

            self.state = SpotifyAPI.DISCONNECTING

            logger.debug("Disconnecting...")

            self.ws.close()
            self.ws = None

            self.logged_in_marker = Event()

            self.seq = 0
            self.cmd_promises = {}

            if clear_settings:
                self.settings = None

            self.state = SpotifyAPI.DISCONNECTED
            logger.debug("Disconnected")

    def shutdown(self):
        logger.debug("Shutting down...")
        self.disconnect()
        self.heartbeat_marker.set()

    @staticmethod
    def get_facebook_token(email, password):
        # Browser
        br = mechanize.Browser()

        # Browser options
        br.set_handle_equiv(True)
        br.set_handle_redirect(True)
        br.set_handle_referer(True)
        br.set_handle_robots(False)

        # Follows refresh 0 but not hangs on refresh > 0
        br.set_handle_refresh(mechanize._http.HTTPRefreshProcessor(), max_time=1)

        # Want debugging messages?
        # br.set_debug_http(True)
        # br.set_debug_redirects(True)
        # br.set_debug_responses(True)

        br.addheaders = [
            ('User-agent',
             'Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.0.1) Gecko/2008071615  Fedora/3.0.1-1.fc9 Firefox/3.0.1')
        ]

        payload = {
            'client_id': APP_ID,
            'redirect_uri': 'https://www.facebook.com/connect/login_success.html',
            'response_type': 'token',
        }

        br.open('https://graph.facebook.com/oauth/authorize?' + urllib.urlencode(payload))

        br.select_form(nr=0)
        br['email'] = email
        br['pass'] = password

        resp = br.submit()

        fragment = parse_qs(urlparse(resp.geturl()).fragment)

        return fragment['access_token'][0]

    def auth(self, username, password):
        assert username and password

        if not self.username:
            self.username = username
            self.password = password

        def early_exit(err):
            logger.error(err)

            if self.login_callback_func:
                self.login_callback_func(False)

            return False

        logger.debug("Authenticating...")

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/31.0.1650.63 Safari/537.36"
        }

        session = requests.session()

        resp = session.get("https://" + self.auth_server, headers=headers)
        data = resp.text

        # csrftoken
        rx = re.compile("\"csrftoken\":\"(.*?)\"")
        r = rx.search(data)

        if not r or len(r.groups()) < 1:
            early_exit("There was a problem authenticating, no auth secret found")

        secret = r.groups()[0]

        #trackingID
        rx = re.compile("\"trackingId\":\"(.*?)\"")
        r = rx.search(data)

        if not r or len(r.groups()) < 1:
            early_exit("There was a problem authenticating, no auth trackingId found")

        trackingId = r.groups()[0]

        #referrer
        rx = re.compile("\"referrer\":\"(.*?)\"")
        r = rx.search(data)

        if not r or len(r.groups()) < 1:
            early_exit("There was a problem authenticating, no auth referrer found")

        referrer = r.groups()[0]

        #landingURL
        rx = re.compile("\"landingURL\":\"(.*?)\"")
        r = rx.search(data)

        if not r or len(r.groups()) < 1:
            early_exit("There was a problem authenticating, no auth landingURL found")

        landingURL = r.groups()[0]

        login_payload = {
            "type": "sp",
            "username": username,
            "password": password,
            "secret": secret,
            "trackingId": trackingId,
            "referrer": referrer,
            "landingURL": landingURL,
            "cf": "",
        }

        logger.info("Normal login with payload:\n{}".format(login_payload))

        resp = session.post("https://" + self.auth_server + "/xhr/json/auth.php", data=login_payload, headers=headers)
        resp_json = resp.json()

        if resp_json["status"] != "OK":
            # Try facebook login
            token = self.fb_access_token or self.get_facebook_token(username, password)
            self.fb_access_token = token

            login_payload = {
                'type': 'fb',
                'fbuid': 1659862757,
                'token': token,
                'secret': secret,
                'trackingId': trackingId,
                "referrer": referrer,
                'landingURL': landingURL,
                'cf': '',
                'f': 'login',
                's': 'direct',
            }

            logger.info("Normal login failed, trying Facebook login with payload:\n{}".format(login_payload))

            resp = session.post("https://" + self.auth_server + "/xhr/json/auth.php", data=login_payload,
                                headers=headers)
            resp_json = resp.json()

            if resp_json["status"] != "OK":
                self.fb_access_token = None

                early_exit("There was a problem authenticating, authentication failed: {}".format(resp_json))

        self.settings = resp.json()["config"]

        #Get wss settings
        resolver_payload = {
            "client": "24:0:0:" + str(self.settings["version"])
        }

        resp = session.get('http://' + self.settings["aps"]["resolver"]["hostname"], params=resolver_payload,
                           headers=headers)

        resp_json = resp.json()
        wss_hostname = resp_json["ap_list"][0].split(":")[0]

        self.settings["wss"] = "wss://" + wss_hostname + "/"

        logger.debug(str(self.settings))

        return True

    def login(self):
        logger.info("Logging in")
        credentials = self.settings["credentials"][0].split(":", 2)
        credentials[2] = credentials[2].decode("string_escape")
        # credentials_enc = json.dumps(credentials, separators=(',',':'))

        self.wrap_request("connect", credentials, self.login_callback)

    def login_callback(self, resp):
        logger.debug("Login Complete")
        self.user_info_request(self.populate_userdata_callback)

    def populate_userdata_callback(self, resp):
        # Send screen size
        self.send_command("sp/log", [41, 1, 0, 0, 0, 0])

        self.userid = resp["user"]
        self.country = resp["country"]
        self.account_type = resp["catalogue"]

        # If you're thinking about changing this: don't.
        # I don't want to play cat and mouse with Spotify.
        # I just want an open-library that works for paying
        # users.
        magic = base64.b64encode(resp["catalogue"]) == "cHJlbWl1bQ=="

        if magic:
            self.state = SpotifyAPI.CONNECTED

            if not self.heartbeat_thread:
                self.heartbeat_thread = Thread(target=self.heartbeat_handler)
                self.heartbeat_thread.daemon = True
                self.heartbeat_thread.start()
        else:
            logger.error("Please upgrade to Premium")
            self.disconnect()

        self.logged_in_marker.set()

        if self.login_callback_func:
            self.login_callback_func(self.is_logged_in)

    def track_uri(self, track, callback=None, prefix="mp3160"):
        track = self.recurse_alternatives(track)

        if not track:
            if callback:
                callback(False)
                return
            else:
                return False

        args = [prefix, SpotifyUtil.gid2id(track.gid)]
        return self.wrap_request("sp/track_uri", args, callback)

    def is_track_available(self, track, country):
        allowed_countries = []
        forbidden_countries = []
        available = False

        for restriction in track.restriction:
            allowed_str = restriction.countries_allowed
            allowed_countries += [allowed_str[i:i + 2] for i in range(0, len(allowed_str), 2)]

            forbidden_str = restriction.countries_forbidden
            forbidden_countries += [forbidden_str[i:i + 2] for i in range(0, len(forbidden_str), 2)]

            allowed = not restriction.HasField("countries_allowed") or country in allowed_countries
            forbidden = self.country in forbidden_countries and len(forbidden_countries) > 0

            if country in allowed_countries and country in forbidden_countries:
                allowed = True
                forbidden = False

            # guessing at names here, corrections welcome
            account_type_map = {
                "premium": 1,
                "unlimited": 1,
                "free": 0
            }

            applicable = account_type_map[self.account_type] in restriction.catalogue

            # enable this to help debug restriction issues
            if False:
                print(restriction)
                print(allowed_countries)
                print(forbidden_countries)
                print("allowed: " + str(allowed))
                print("forbidden: " + str(forbidden))
                print("applicable: " + str(applicable))

            available = True == allowed and False == forbidden and True == applicable
            if available:
                break

        if available:
            logger.info(SpotifyUtil.gid2uri("track", track.gid) + " is available!")
        else:
            logger.info(SpotifyUtil.gid2uri("track", track.gid) + " is NOT available!")

        return available

    def recurse_alternatives(self, track, country=None):
        country = self.country if country is None else country
        if self.is_track_available(track, country):
            return track
        else:
            for alternative in track.alternative:
                if self.is_track_available(alternative, country):
                    return alternative
            return False

    def generate_multiget_args(self, metadata_type, requests):
        args = [0]

        if len(requests.request) == 1:
            req = base64.encodestring(requests.request[0].SerializeToString())
            args.append(req)
        else:
            header = mercury_pb2.MercuryRequest()
            header.body = "GET"
            header.uri = "hm://metadata/" + metadata_type + "s"
            header.content_type = "vnd.spotify/mercury-mget-request"

            header_str = base64.encodestring(header.SerializeToString())
            req = base64.encodestring(requests.SerializeToString())
            args.extend([header_str, req])

        return args

    def metadata_request(self, uris, callback=None):
        mercury_requests = mercury_pb2.MercuryMultiGetRequest()

        if type(uris) != list:
            uris = [uris]

        for uri in uris:
            uri_type = SpotifyUtil.get_uri_type(uri)
            if uri_type == "local":
                logger.warning("Track with URI " + uri + " is a local track, we can't request metadata, skipping")
                continue

            id = SpotifyUtil.uri2id(uri)

            mercury_request = mercury_pb2.MercuryRequest()
            mercury_request.body = "GET"
            mercury_request.uri = "hm://metadata/" + uri_type + "/" + id

            mercury_requests.request.extend([mercury_request])

        args = self.generate_multiget_args(SpotifyUtil.get_uri_type(uris[0]), mercury_requests)

        return self.wrap_request("sp/hm_b64", args, callback, self.parse_metadata)

    @staticmethod
    def parse_metadata(resp):
        header = mercury_pb2.MercuryReply()
        header.ParseFromString(base64.decodestring(resp[0]))

        if header.status_message == "vnd.spotify/mercury-mget-reply":
            if len(resp) < 2:
                return False

            mget_reply = mercury_pb2.MercuryMultiGetReply()
            mget_reply.ParseFromString(base64.decodestring(resp[1]))
            items = []

            for reply in mget_reply.reply:
                if reply.status_code != 200:
                    continue

                item = SpotifyAPI.parse_metadata_item(reply.content_type, reply.body)
                items.append(item)

            return items
        else:
            return SpotifyAPI.parse_metadata_item(header.status_message, base64.decodestring(resp[1]))

    @staticmethod
    def parse_metadata_item(content_type, body):
        if content_type == "vnd.spotify/metadata-album":
            obj = metadata_pb2.Album()
        elif content_type == "vnd.spotify/metadata-artist":
            obj = metadata_pb2.Artist()
        elif content_type == "vnd.spotify/metadata-track":
            obj = metadata_pb2.Track()
        else:
            logger.error("Unrecognised metadata type " + content_type)
            return False

        obj.ParseFromString(body)

        return obj

    def toplist_request(self, toplist_content_type="track", toplist_type="user", username=None, region="global",
                        callback=None):
        if username is None:
            username = self.userid

        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        if toplist_type == "user":
            mercury_request.uri = "hm://toplist/toplist/user/" + username
        elif toplist_type == "region":
            mercury_request.uri = "hm://toplist/toplist/region"
            if region is not None and region != "global":
                mercury_request.uri += "/" + region
        else:
            return False
        mercury_request.uri += "?type=" + toplist_content_type

        # playlists don't appear to work?
        if toplist_type == "user" and toplist_content_type == "playlist":
            if username != self.userid:
                return False
            mercury_request.uri = "hm://socialgraph/suggestions/topplaylists"

        req = base64.encodestring(mercury_request.SerializeToString())

        args = [0, req]

        return self.wrap_request("sp/hm_b64", args, callback, self.parse_toplist)

    @staticmethod
    def parse_toplist(resp):
        obj = toplist_pb2.Toplist()
        res = base64.decodestring(resp[1])
        obj.ParseFromString(res)
        return obj

    def discover_request(self, callback=None):
        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://bartender/stories/skip/0/take/50"
        req = base64.encodestring(mercury_request.SerializeToString())

        args = [0, req]
        return self.wrap_request("sp/hm_b64", args, callback, self.parse_discover)

    @staticmethod
    def parse_discover(resp):
        obj = bartender_pb2.StoryList()

        try:
            res = base64.decodestring(resp[1])
            obj.ParseFromString(res)
        except Exception as e:
            logger.error(
                "There was a problem while parsing discover info. Message: " + str(e) + ". Resp: " + str(resp))
            obj = False

        return obj

    def radio_stations_request(self, callback=None):
        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://radio/stations"
        req = base64.encodestring(mercury_request.SerializeToString())

        args = [0, req]
        return self.wrap_request("sp/hm_b64", args, callback, self.parse_radio_stations)

    @staticmethod
    def parse_radio_stations(resp):
        obj = radio_pb2.StationList()
        try:
            res = base64.decodestring(resp[1])
            obj.ParseFromString(res)
            return obj
        except Exception as e:
            logger.error(
                "There was a problem while parsing radio stations info. Message: " + str(e) + ". Resp: " + str(resp))
            return False

    def radio_genres_request(self, callback=None):
        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://radio/genres/"
        req = base64.encodestring(mercury_request.SerializeToString())

        args = [0, req]
        return self.wrap_request("sp/hm_b64", args, callback, self.parse_radio_genres)

    @staticmethod
    def parse_radio_genres(resp):
        obj = radio_pb2.GenreList()
        try:
            res = base64.decodestring(resp[1])
            obj.ParseFromString(res)
            return obj
        except Exception as e:
            logger.error(
                "There was a problem while parsing radio genre list info. Message: " + str(e) + ". Resp: " + str(resp))
            return False

    # Station uri can be a track, artist, or genre (spotify:genre:[genre_id])
    def radio_tracks_request(self, stationUri, stationId=None, salt=None, num_tracks=20, callback=None):
        if salt is None:
            max32int = pow(2, 31) - 1
            salt = randint(1, max32int)

        if stationId is None:
            stationId = uuid.uuid4().hex

        radio_request = radio_pb2.RadioRequest()
        radio_request.salt = salt
        radio_request.length = num_tracks
        radio_request.stationId = stationId
        radio_request.uris.append(stationUri)
        req_args = base64.encodestring(radio_request.SerializeToString())

        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://radio/"
        req = base64.encodestring(mercury_request.SerializeToString())

        args = [0, req, req_args]
        return self.wrap_request("sp/hm_b64", args, callback, self.parse_radio_tracks)

    @staticmethod
    def parse_radio_tracks(resp):
        obj = radio_pb2.Tracks()
        try:
            res = base64.decodestring(resp[1])
            obj.ParseFromString(res)
            return obj
        except Exception as e:
            logger.error("There was a problem while parsing radio tracks. Message: " + str(e) + ". Resp: " + str(resp))
            return False

    def playlists_request(self, user, fromnum=0, num=100, callback=None):
        if num > 100:
            logger.error("You may only request up to 100 playlists at once")
            return False

        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://playlist/user/" + user + "/rootlist?from=" + str(fromnum) + "&length=" + str(num)
        req = base64.encodestring(mercury_request.SerializeToString())

        args = [0, req]

        return self.wrap_request("sp/hm_b64", args, callback, self.parse_playlist)

    def playlist_request(self, uri, fromnum=0, num=100, callback=None):
        # mercury_requests = mercury_pb2.MercuryRequest()

        playlist = uri[8:].replace(":", "/")
        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://playlist/" + playlist + "?from=" + str(fromnum) + "&length=" + str(num)

        req = base64.encodestring(mercury_request.SerializeToString())
        args = [0, req]

        return self.wrap_request("sp/hm_b64", args, callback, self.parse_playlist)

    @staticmethod
    def parse_playlist(resp):
        obj = playlist4changes_pb2.ListDump()
        try:
            res = base64.decodestring(resp[1])
            obj.ParseFromString(res)
            return obj
        except:
            return False

    def my_music_request(self, tpe="albums", callback=None):
        if tpe == "albums":
            action = "albumscoverlist"
            extras = ""
        elif tpe == "artists":
            action = "artistscoverlist"
            extras = "?includefollowedartists=true"
        else:
            if callback:
                callback([])
                return
            else:
                return []

        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "GET"
        mercury_request.uri = "hm://collection-web/v1/" + self.userid + "/" + action + extras

        req = base64.encodestring(mercury_request.SerializeToString())
        args = [0, req]

        return self.wrap_request("sp/hm_b64", args, callback, self.parse_my_music)

    @staticmethod
    def parse_my_music(resp):
        return json.loads(base64.decodestring(resp[1]))

    def playlist_op_track(self, playlist_uri, track_uri, op, callback=None):
        playlist = playlist_uri.split(":")

        if playlist_uri == "rootlist":
            user = self.userid
            playlist_id = "rootlist"
        else:
            user = playlist[2]
            if playlist[3] == "starred":
                playlist_id = "starred"
            else:
                playlist_id = "playlist/" + playlist[4]

        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = op
        mercury_request.uri = "hm://playlist/user/" + user + "/" + playlist_id + "?syncpublished=1"
        req = base64.encodestring(mercury_request.SerializeToString())
        args = [0, req, base64.encodestring(track_uri)]
        return self.wrap_request("sp/hm_b64", args, callback)

    def playlist_add_track(self, playlist_uri, track_uri, callback=None):
        return self.playlist_op_track(playlist_uri, track_uri, "ADD", callback)

    def playlist_remove_track(self, playlist_uri, track_uri, callback=None):
        return self.playlist_op_track(playlist_uri, track_uri, "REMOVE", callback)

    def set_starred(self, track_uri, starred=True, callback=None):
        if starred:
            return self.playlist_add_track("spotify:user:" + self.userid + ":starred", track_uri, callback)
        else:
            return self.playlist_remove_track("spotify:user:" + self.userid + ":starred", track_uri, callback)

    def playlist_op(self, op, path, optype="update", name=None, index=None, callback=None):
        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = op
        mercury_request.uri = "hm://" + path

        req = base64.encodestring(mercury_request.SerializeToString())

        op = playlist4ops_pb2.Op()
        if optype == "update":
            op.kind = playlist4ops_pb2.Op.UPDATE_LIST_ATTRIBUTES
            op.update_list_attributes.new_attributes.values.name = name
        elif optype == "remove":
            op.kind = playlist4ops_pb2.Op.REM
            op.rem.fromIndex = index
            op.rem.length = 1

        mercury_request_payload = mercury_pb2.MercuryRequest()
        mercury_request_payload.uri = op.SerializeToString()

        payload = base64.encodestring(mercury_request_payload.SerializeToString())

        args = [0, req, payload]
        return self.wrap_request("sp/hm_b64", args, callback, self.new_playlist_callback)

    def new_playlist(self, name, callback=None):
        return self.playlist_op("PUT", "playlist/user/" + self.userid, name=name, callback=callback)

    def rename_playlist(self, playlist_uri, name, callback=None):
        path = "playlist/user/" + self.userid + "/playlist/" + playlist_uri.split(":")[4] + "?syncpublished=true"
        return self.playlist_op("MODIFY", path, name=name, callback=callback)

    def remove_playlist(self, playlist_uri, callback=None):
        return self.playlist_op_track("rootlist", playlist_uri, "REMOVE", callback=callback)
        # return self.playlist_op("REMOVE", "playlist/user/"+self.username+"/rootlist?syncpublished=true",
        #optype="remove", index=index, callback=callback)

    def new_playlist_callback(self, data):
        try:
            reply = playlist4service_pb2.CreateListReply()
            reply.ParseFromString(base64.decodestring(data[1]))
        except:
            return False

        mercury_request = mercury_pb2.MercuryRequest()
        mercury_request.body = "ADD"
        mercury_request.uri = "hm://playlist/user/" + self.userid + "/rootlist?add_first=1&syncpublished=1"
        req = base64.encodestring(mercury_request.SerializeToString())
        args = [0, req, base64.encodestring(reply.uri)]

        self.send_command("sp/hm_b64", args)

        return reply.uri

    def search_request(self, query, query_type="all", max_results=50, offset=0, callback=None):
        if max_results > 50:
            logger.warning("Maximum of 50 results per request, capping at 50")
            max_results = 50

        search_types = {
            "tracks": 1,
            "albums": 2,
            "artists": 4,
            "playlists": 8

        }

        query_type = [k for k, v in search_types.items()] if query_type == "all" else query_type
        query_type = [query_type] if type(query_type) != list else query_type
        query_type = reduce(operator.or_,
                            [search_types[type_name] for type_name in query_type if type_name in search_types])

        args = [query, query_type, max_results, offset]

        return self.wrap_request("sp/search", args, callback)

    def user_info_request(self, callback=None):
        return self.wrap_request("sp/user_info", [], callback)

    def heartbeat(self):
        self.send_command("sp/echo", "h")

    def send_track_end(self, lid, track_uri, ms_played, callback=None):
        ms_played = int(ms_played)
        ms_played_union = ms_played
        n_seeks_forward = 0
        n_seeks_backward = 0
        ms_seeks_forward = 0
        ms_seeks_backward = 0
        ms_latency = 100
        display_track = None
        play_context = "unknown"
        source_start = "unknown"
        source_end = "unknown"
        reason_start = "unknown"
        reason_end = "unknown"
        referrer = "unknown"
        referrer_version = "0.1.0"
        referrer_vendor = "com.spotify"
        max_continuous = ms_played
        args = [lid, ms_played, ms_played_union, n_seeks_forward, n_seeks_backward, ms_seeks_forward, ms_seeks_backward,
                ms_latency, display_track, play_context, source_start, source_end, reason_start, reason_end, referrer,
                referrer_version, referrer_vendor, max_continuous]
        return self.wrap_request("sp/track_end", args, callback)

    def send_track_event(self, lid, event, ms_where, callback=None):
        if event == "pause" or event == "stop":
            ev_n = 4
        elif event == "unpause" or "continue" or "play":
            ev_n = 3
        else:
            return False
        return self.wrap_request("sp/track_event", [lid, ev_n, int(ms_where)], callback)

    def send_track_progress(self, lid, ms_played, callback=None):
        source_start = "unknown"
        reason_start = "unknown"
        ms_latency = 100
        play_context = "unknown"
        display_track = ""
        referrer = "unknown"
        referrer_version = "0.1.0"
        referrer_vendor = "com.spotify"
        args = [lid, source_start, reason_start, int(ms_played), int(ms_latency), play_context, display_track, referrer,
                referrer_version, referrer_vendor]
        return self.wrap_request("sp/track_progress", args, callback)

    def send_command(self, name, args=None, callback=None):
        promise = Promise()

        msg = {
            "name": name,
            "id": str(self.seq),
            "args": args or []
        }

        msg_enc = json.dumps(msg, separators=(',', ':'))

        try:
            with self.ws_lock:
                if self.ws is None or self.state == SpotifyAPI.DISCONNECTED:
                    return Promise.rejected(SpotifyDisconnectedError())

                pid = self.seq

                if callback:
                    promise.addCallback(callback)

                self.cmd_promises[pid] = promise
                self.seq += 1

                self.ws.send(msg_enc)

                logger.debug("Sent PID({}) with msg: {}".format(pid, msg_enc))
        except (SSLError, StreamClosed) as e:
            logger.error("SSL error ({}), attempting to continue".format(e))
            promise.rejected(SpotifyDisconnectedError())

        return promise

    def wrap_request(self, command, args, callback=None, transform=None, retries=3, timeout=10):
        assert retries >= 1
        assert not callback or hasattr(callback, '__call__')
        assert not transform or hasattr(transform, '__call__')

        if callback:
            promise = self.send_command(command, args).then(transform)
            promise.done(callback)
            return promise
        else:
            last_exception = None

            for attempt in range(0, retries):
                promise = self.send_command(command, args).then(transform)

                try:
                    return promise.get(timeout)
                except SpotifyDisconnectedError as e:
                    raise e
                except SpotifyCommandError as e:
                    raise e
                except Exception as e:
                    last_exception = e

            raise last_exception or SpotifyTimeoutError()

    def recv_packet(self, msg):
        logger.debug("recv " + str(msg))
        packet = json.loads(str(msg))
        if "error" in packet:
            self.handle_error(packet)
        elif "message" in packet:
            self.handle_message(packet["message"])
        elif "id" in packet:
            pid = packet["id"]

            if pid in self.cmd_promises:
                promise = self.cmd_promises.pop(pid)
                promise.fulfill(packet["result"])
            else:
                logger.warning("Unhandled command response with id " + str(pid))

    def work_callback(self, resp):
        logger.debug("Got ack for message reply")

    def handle_message(self, msg):
        cmd = msg[0]

        if len(msg) > 1:
            payload = msg[1]
        else:
            payload = None

        if cmd == "do_work":
            logger.debug("Got do_work message, payload: " + payload)
            self.wrap_request("sp/work_done", ["v1"], self.work_callback)
        elif cmd == "ping_flash2":
            if len(msg[1]) >= 20:
                key = [
                    {'idx': 8, 'xor': 146},
                    {'idx': 14,
                     'map': [54, 55, 56, 56, 57, 58, 59, 60, 61, 62, 62, 63, 64, 65, 66, 67, 68, 68, 69, 70, 71, 72, 73,
                             73, 74, 75, 76, 77, 78, 79, 79, 80, 81, 82, 83, 84, 85, 85, 86, 87, 88, 89, 90, 90, 91, 92,
                             93, 94, 95, 96, 96, 97, 98, 99, 100, 101, 102, 102, 103, 104, 105, 106, 107, 107, 0, 0, 1,
                             2, 3, 4, 5, 5, 6, 7, 8, 9, 10, 11, 11, 12, 13, 14, 15, 16, 17, 17, 18, 19, 20, 21, 22, 22,
                             23, 24, 25, 26, 27, 28, 28, 29, 30, 31, 32, 33, 34, 34, 35, 36, 37, 38, 39, 39, 40, 41, 42,
                             43, 44, 45, 45, 46, 47, 48, 49, 50, 51, 51, 52, 53, 163, 164, 164, 165, 166, 167, 168, 169,
                             170, 170, 171, 172, 173, 174, 175, 175, 176, 177, 178, 179, 180, 181, 181, 182, 183, 184,
                             185, 186, 187, 187, 188, 189, 190, 191, 192, 192, 193, 194, 195, 196, 197, 198, 198, 199,
                             200, 201, 202, 203, 204, 204, 205, 206, 207, 208, 209, 209, 210, 211, 212, 213, 214, 215,
                             215, 216, 108, 109, 110, 111, 112, 113, 113, 114, 115, 116, 117, 118, 119, 119, 120, 121,
                             122, 123, 124, 124, 125, 126, 127, 128, 129, 130, 130, 131, 132, 133, 134, 135, 136, 136,
                             137, 138, 139, 140, 141, 141, 142, 143, 144, 145, 146, 147, 147, 148, 149, 150, 151, 152,
                             153, 153, 154, 155, 156, 157, 158, 158, 159, 160, 161, 162]},
                    {'idx': 17, 'xor': 173},
                    {'idx': 5, 'xor': 4},
                    {'idx': 10, 'xor': 178},
                    {'idx': 6, 'xor': 200},
                    {'idx': 4, 'xor': 124},
                    {'idx': 3,
                     'map': [232, 231, 230, 229, 236, 235, 234, 233, 224, 223, 222, 221, 228, 227, 226, 225, 248, 247,
                             246, 245, 252, 251, 250, 249, 240, 239, 238, 237, 244, 243, 242, 241, 200, 199, 198, 197,
                             204, 203, 202, 201, 192, 191, 190, 189, 196, 195, 194, 193, 216, 215, 214, 213, 220, 219,
                             218, 217, 208, 207, 206, 205, 212, 211, 210, 209, 29, 29, 29, 29, 30, 29, 29, 29, 28, 28,
                             28, 28, 29, 29, 29, 28, 31, 31, 31, 30, 31, 31, 31, 31, 30, 30, 30, 30, 30, 30, 30, 30, 26,
                             26, 26, 26, 26, 26, 26, 26, 25, 255, 254, 253, 26, 25, 25, 25, 28, 27, 27, 27, 28, 28, 28,
                             28, 27, 27, 27, 26, 27, 27, 27, 27, 36, 35, 35, 35, 36, 36, 36, 36, 35, 35, 35, 34, 35, 35,
                             35, 35, 37, 37, 37, 37, 38, 37, 37, 37, 36, 36, 36, 36, 37, 37, 37, 36, 32, 32, 32, 32, 33,
                             33, 33, 32, 32, 31, 31, 31, 32, 32, 32, 32, 34, 34, 34, 34, 34, 34, 34, 34, 33, 33, 33, 33,
                             34, 33, 33, 33, 42, 42, 42, 42, 42, 42, 42, 42, 41, 41, 41, 41, 42, 41, 41, 41, 44, 43, 43,
                             43, 44, 44, 44, 44, 43, 43, 43, 42, 43, 43, 43, 43, 39, 39, 39, 38, 39, 39, 39, 39, 38, 38,
                             38, 38, 38, 38, 38, 38, 40, 40, 40, 40, 41, 41, 41, 40, 40, 39, 39, 39, 40, 40, 40, 40]},
                    {'idx': 7,
                     'map': [6, 5, 7, 7, 9, 8, 10, 9, 0, 0, 2, 1, 3, 2, 4, 4, 17, 16, 18, 18, 20, 19, 21, 21, 11, 11,
                             13, 12, 14, 14, 16, 15, 28, 28, 30, 29, 31, 30, 32, 32, 23, 22, 24, 23, 25, 25, 27, 26, 39,
                             39, 41, 40, 42, 42, 44, 43, 34, 33, 35, 35, 37, 36, 38, 37, 51, 50, 52, 51, 53, 53, 55, 54,
                             45, 44, 46, 46, 48, 47, 49, 49, 62, 61, 63, 63, 65, 64, 66, 65, 56, 56, 58, 57, 59, 58, 60,
                             60, 73, 72, 74, 74, 76, 75, 77, 77, 67, 67, 69, 68, 70, 70, 72, 71, 84, 84, 86, 85, 87, 86,
                             88, 88, 79, 78, 80, 79, 81, 81, 83, 82, 95, 95, 97, 96, 98, 98, 100, 99, 90, 89, 91, 91,
                             93, 92, 94, 93, 107, 106, 108, 107, 109, 109, 111, 110, 101, 100, 102, 102, 104, 103, 105,
                             105, 118, 117, 119, 119, 121, 120, 122, 121, 112, 112, 114, 113, 115, 114, 116, 116, 129,
                             128, 130, 130, 132, 131, 133, 133, 123, 123, 125, 124, 126, 126, 128, 127, 140, 140, 142,
                             141, 143, 142, 144, 144, 135, 134, 136, 135, 137, 137, 139, 138, 151, 151, 153, 152, 154,
                             154, 156, 155, 146, 145, 147, 147, 149, 148, 150, 149, 163, 162, 164, 163, 165, 165, 167,
                             166, 157, 156, 158, 158, 160, 159, 161, 161, 174, 173, 175, 175, 177, 176, 178, 177, 168,
                             168, 170, 169, 171, 170, 172, 172]},
                    {'idx': 12,
                     'map': [65, 68, 60, 63, 75, 78, 70, 73, 45, 47, 40, 42, 55, 57, 50, 52, 25, 27, 20, 22, 35, 37, 30,
                             32, 5, 7, 0, 2, 15, 17, 10, 12, 146, 148, 141, 143, 156, 158, 151, 153, 126, 128, 120, 123,
                             136, 138, 131, 133, 105, 108, 100, 103, 115, 118, 110, 113, 85, 88, 80, 83, 95, 98, 90, 93,
                             226, 229, 221, 224, 236, 239, 231, 234, 206, 209, 201, 204, 216, 219, 211, 214, 186, 189,
                             181, 183, 196, 199, 191, 194, 166, 168, 161, 163, 176, 178, 171, 173, 30, 30, 30, 30, 31,
                             32, 31, 31, 28, 28, 28, 28, 29, 29, 29, 29, 26, 26, 26, 26, 27, 27, 27, 27, 246, 249, 241,
                             244, 25, 25, 252, 254, 38, 39, 38, 38, 39, 40, 39, 39, 36, 37, 36, 36, 37, 38, 37, 37, 34,
                             35, 34, 34, 35, 36, 35, 35, 32, 33, 32, 32, 33, 34, 33, 33, 46, 47, 46, 46, 47, 48, 47, 47,
                             44, 45, 44, 44, 45, 46, 45, 45, 42, 43, 42, 42, 43, 44, 43, 43, 40, 41, 40, 40, 41, 42, 41,
                             41, 54, 55, 54, 54, 55, 56, 55, 55, 52, 53, 52, 52, 53, 54, 53, 53, 50, 51, 50, 50, 51, 52,
                             51, 51, 48, 49, 48, 48, 49, 50, 49, 49, 63, 63, 62, 62, 64, 64, 63, 63, 60, 61, 60, 60, 61,
                             62, 61, 61, 58, 59, 58, 58, 59, 60, 59, 59, 56, 57, 56, 56, 57, 58, 57, 57]},
                ]
                input = [int(x) for x in msg[1].split(" ")]
                output = []
                for k in key:
                    idx = k.get('idx', None)
                    xor = k.get('xor', None)
                    arr = k.get('map', None)
                    val = input[idx]
                    if xor is not None:
                        output.append(val ^ xor)
                    else:
                        output.append(arr[val])
                pong = u' '.join(map(unicode, output))
                logger.debug("Sending pong %s" % pong)
                self.send_command("sp/pong_flash2", [pong, ])
        elif cmd == "login_complete":
            pass
            # self.login_callback(None)

    def handle_error(self, err):
        if len(err) < 2:
            logger.error("Unknown error " + str(err))

        major = err["error"][0]
        minor = err["error"][1]

        major_err = {
            8: "Rate request error",
            12: "Track error",
            13: "Hermes error",
            14: "Hermes service error",
        }

        minor_err = {
            1: "failed to send to backend",
            8: "rate limited",
            408: "timeout",
            429: "too many requests",
        }

        if major in major_err:
            major_str = major_err[major]
        else:
            major_str = "unknown major (" + str(major) + ")"

        if minor in minor_err:
            minor_str = minor_err[minor]
        else:
            minor_str = "unknown minor (" + str(minor) + ")"

        if minor == 0:
            error_str = major_str
        else:
            error_str = major_str + " - " + minor_str

        if 'id' in err:
            pid = err["id"]

            if pid in self.cmd_promises:
                promise = self.cmd_promises.pop(pid)
                promise.reject(SpotifyCommandError(major, minor, error_str))

        logger.error(error_str)

    def heartbeat_handler(self):
        stop = False

        while not stop:
            self.heartbeat()
            stop = self.heartbeat_marker.wait(timeout=18)
