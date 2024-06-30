#!/usr/bin/env python3

import google.auth
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.errors
import argparse
import codecs
import http.client
import http.server
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import os

logging.basicConfig(level=20, datefmt='%I:%M:%S', format='[%(asctime)s] %(message)s')

TOKEN_FILE = 'spotify_token.json'

class SpotifyAPI:
    
    def __init__(self, auth):
        self._auth = auth
    
    def get(self, url, params={}, tries=3):
        if not url.startswith('https://api.spotify.com/v1/'):
            url = 'https://api.spotify.com/v1/' + url
        if params:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    
        for _ in range(tries):
            try:
                req = urllib.request.Request(url)
                req.add_header('Authorization', 'Bearer ' + self._auth)
                res = urllib.request.urlopen(req)
                reader = codecs.getreader('utf-8')
                return json.load(reader(res))
            except Exception as err:
                logging.info('Couldn\'t load URL: {} ({})'.format(url, err))
                time.sleep(2)
                logging.info('Trying again...')
        sys.exit(1)
    
    def list(self, url, params={}):
        last_log_time = time.time()
        response = self.get(url, params)
        items = response['items']

        while response['next']:
            if time.time() > last_log_time + 15:
                last_log_time = time.time()
                logging.info(f"Loaded {len(items)}/{response['total']} items")

            response = self.get(response['next'])
            items += response['items']
        return items
    
    @staticmethod
    def authorize(client_id, scope):
        url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
            'response_type': 'token',
            'client_id': client_id,
            'scope': scope,
            'redirect_uri': f'http://127.0.0.1:{SpotifyAPI._SERVER_PORT}/redirect'
        })
        logging.info(f'Logging in (click if it doesn\'t open automatically): {url}')
        webbrowser.open(url)

       
        server = SpotifyAPI._AuthorizationServer('127.0.0.1', SpotifyAPI._SERVER_PORT)
        try:
            while True:
                server.handle_request()
        except SpotifyAPI._Authorization as auth:
            # Save the token for future use
            with open(TOKEN_FILE, 'w') as f:
                json.dump({'access_token': auth.access_token}, f)
            return SpotifyAPI(auth.access_token)

    
    @staticmethod
    def get_spotify_api(client_id, scope):
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'r') as f:
                token_data = json.load(f)
            return SpotifyAPI(token_data['access_token'])
        else:
            return SpotifyAPI.authorize(client_id, scope)

    _SERVER_PORT = 43019
    
    class _AuthorizationServer(http.server.HTTPServer):
        def __init__(self, host, port):
            http.server.HTTPServer.__init__(self, (host, port), SpotifyAPI._AuthorizationHandler)
        
        def handle_error(self, request, client_address):
            raise
    
    class _AuthorizationHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            
            if self.path.startswith('/redirect'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<script>location.replace("token?" + location.hash.slice(1));</script>')
            
            elif self.path.startswith('/token?'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<script>close()</script>Thanks! You may now close this window.')

                access_token = re.search('access_token=([^&]*)', self.path).group(1)
                logging.info(f'Received access token from Spotify: {access_token}')
                raise SpotifyAPI._Authorization(access_token)
            
            else:
                self.send_error(404)
        
        def log_message(self, format, *args):
            pass
    
    class _Authorization(Exception):
        def __init__(self, access_token):
            self.access_token = access_token

def get_authenticated_service():
    SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
    CLIENT_SECRETS_FILE = "client_secret.json"

    flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, SCOPES)
    credentials = flow.run_local_server(port=0)
    return googleapiclient.discovery.build("youtube", "v3", credentials=credentials)

def create_youtube_playlist(youtube, title, description=""):
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description
            },
            "status": {
                "privacyStatus": "private"
            }
        }
    )
    response = request.execute()
    return response["id"]

def add_video_to_playlist(youtube, video_id, playlist_id):
    request = youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id
                }
            }
        }
    )
    response = request.execute()
    return response

def search_youtube_video(youtube, query):
    request = youtube.search().list(
        part="snippet",
        maxResults=1,
        q=query
    )
    response = request.execute()
    items = response.get("items", [])
    if items:
        return items[0]["id"]["videoId"]
    return None

def main():
    parser = argparse.ArgumentParser(description='Exports your Spotify playlists to YouTube.')
    parser.add_argument('--token', metavar='OAUTH_TOKEN', help='use a Spotify OAuth token (requires the `playlist-read-private` permission)')
    parser.add_argument('--dump', default='playlists', choices=['liked,playlists', 'playlists,liked', 'playlists', 'liked'],
                        help='dump playlists or liked songs, or both (default: playlists)')
    parser.add_argument('--format', default='txt', choices=['json', 'txt'], help='output format (default: txt)')
    args = parser.parse_args()

    if args.token:
        spotify = SpotifyAPI(args.token)
    else:
        spotify = SpotifyAPI.get_spotify_api(client_id='5c098bcc800e45d49e476265bc9b6934',
                                             scope='playlist-read-private playlist-read-collaborative user-library-read')

    
    logging.info('Loading user info...')
    me = spotify.get('me')
    logging.info('Logged in as {display_name} ({id})'.format(**me))

    playlists = []
    liked_albums = []

    if 'liked' in args.dump:
        logging.info('Loading liked albums and songs...')
        liked_tracks = spotify.list('users/{user_id}/tracks'.format(user_id=me['id']), {'limit': 50})
        liked_albums = spotify.list('me/albums', {'limit': 50})
        playlists += [{'name': 'Liked Songs', 'tracks': liked_tracks}]

    if 'playlists' in args.dump:
        logging.info('Loading playlists...')
        playlist_data = spotify.list('users/{user_id}/playlists'.format(user_id=me['id']), {'limit': 50})
        logging.info(f'Found {len(playlist_data)} playlists')

        for playlist in playlist_data:
            logging.info('Loading playlist: {name} ({tracks[total]} songs)'.format(**playlist))
            playlist['tracks'] = spotify.list(playlist['tracks']['href'], {'limit': 100})
        playlists += playlist_data

    youtube = get_authenticated_service()

    for playlist in playlists:
        playlist_name = playlist['name']
        logging.info(f'Creating YouTube playlist: {playlist_name}')
        playlist_id = create_youtube_playlist(youtube, playlist_name)

        for track in playlist['tracks']:
            if track['track'] is None:
                continue
            song_name = track['track']['name']
            artists = ', '.join([artist['name'] for artist in track['track']['artists']])
            query = f"{song_name} {artists}"
            logging.info(f'Searching YouTube for: {query}')
            video_id = search_youtube_video(youtube, query)
            if video_id:
                logging.info(f'Adding video to playlist: {song_name} - {artists}')
                add_video_to_playlist(youtube, video_id, playlist_id)
            else:
                logging.info(f'No video found for: {song_name} - {artists}')

    logging.info('Finished exporting playlists to YouTube.')

if __name__ == '__main__':
    main()
