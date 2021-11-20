"""
Server for hosting website locally

On windows, run `server.exe`.

(executable created with: `pyinstaller --onefile server.py`)

Otherwise, requires python 3.3 or greater.

Run `python3 server.py`

Visit `http://localhost:8000` in your web browser.
"""

import argparse
import http.client
import json
import os
import posixpath
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler

if getattr(sys, 'frozen', False):
    DIR = os.path.dirname(sys.executable)
elif __file__:
    DIR = os.path.dirname(__file__)
DIR = os.path.abspath(DIR)

# make sure our working directory for this server is the same as this file is in
os.chdir(DIR)

with open('metadata.json') as f:
    VERSION_INFO = json.load(f)


CANONICAL_URLS = VERSION_INFO.get('meta', {}).get('canonical-urls', {})

DEFAULT_SEARCH_URL = CANONICAL_URLS.get('html')
STATIC_ASSETS_REPO_URL = CANONICAL_URLS.get('static-assets')
if DEFAULT_SEARCH_URL is None or STATIC_ASSETS_REPO_URL is None:
    print("'<html>' and '<static-assets>' tags are required in law-xml/index.xml under '<canonical-urls>'.")
    print("Try pulling the latest law-xml changes and building repositories again.")
    sys.exit(1)

STATIC_ASSETS_DIR = os.path.join(DIR, 'static-assets')
if not os.path.exists(STATIC_ASSETS_DIR):
    os.mkdir(STATIC_ASSETS_DIR)

LAW_DOCS_PATH = os.path.join(os.path.dirname(DIR), 'law-docs')


PORTAL_CLIENT_CLASS = None
PORTAL_HOST = None
SEARCH_CLIENT_CLASS = None
SEARCH_HOST = None

filetypes = {
    'html',
    'pdf',
    'jpg',
    'svg',
    'png',
    'gif',
    'css',
    'js',
    'mustache',
    'json',
    'bulk',
    'map',
    'ttf',
    'eot',
    'woff',
    'woff2',
}

HISTORICAL_VERSIONS_PATH_PREFIXES = ('/_publication', '/_date', '/_compare', )
PORTAL_PATH_PREFIXES = ('/_portal', '/_api') + HISTORICAL_VERSIONS_PATH_PREFIXES


# custom 404 error template
try:
    with open("404.html", "r") as f:
        ERROR_404_TEMPLATE = f.read()
except FileNotFoundError:
    ERROR_404_TEMPLATE = None


def download_static_assets(static_assets_repo_url=STATIC_ASSETS_REPO_URL, force=False):
    from io import BytesIO
    from zipfile import ZipFile
    import urllib.request
    import shutil

    # skip download if already exists
    if not (len(os.listdir(STATIC_ASSETS_DIR)) == 0 or force):
        return

    print("\nDownloading static assets...\n")

    try:
        # fix for CERTIFICATE_VERIFY_FAILED
        def _fix_cert():
            import ssl
            if not os.environ.get('PYTHONHTTPSVERIFY', '') and getattr(ssl, '_create_unverified_context', None):
                ssl._create_default_https_context = ssl._create_unverified_context


        ZIP_URL = "{}/archive/main.zip".format(static_assets_repo_url)
        BACKUP_ZIP_URL = "{}/archive/master.zip".format(static_assets_repo_url)
        _fix_cert()
        try:
            data = urllib.request.urlopen(ZIP_URL)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                data = urllib.request.urlopen(BACKUP_ZIP_URL)
            else:
                raise e

        with ZipFile(BytesIO(data.read())) as zip_file:
            files = zip_file.namelist()
            root_dir = files.pop(0)

            for member in files:
                member_path = os.path.join(
                    STATIC_ASSETS_DIR, os.path.relpath(member, root_dir))
                # skip empty directories
                if not os.path.basename(member):
                    if not os.path.exists(member_path):
                        os.mkdir(member_path)
                    continue

                # copy file (taken from zipfile's extract)
                source = zip_file.open(member)
                target = open(member_path, "wb")
                with source, target:
                    shutil.copyfileobj(source, target)

    except Exception as e:
        print("ERROR: {}".format(str(e)))
        sys.exit(1)


class RequestHandler(SimpleHTTPRequestHandler):
    if ERROR_404_TEMPLATE:
        error_message_format = ERROR_404_TEMPLATE

    def _proxy(self, Client, host, upstream_name, method='GET', body=None):
        """
        proxy the current request to the given host using the given
        http.client Client class. 404 if not configured to proxy
        the path.
        """
        if Client is None:
            self.send_response(400)
            self.end_headers()
            message = 'Local server not configured to proxy {0}. Please run with the "--{0}-proxy-url" flag '.format(
                upstream_name)
            self.wfile.write(message.encode('utf-8'))
            return
        client = Client(host)
        req_headers = {}
        req_headers.update(self.headers)
        req_headers.pop('Host', None)
        req_headers.update({
            'X-Forwarded-For': self.address_string(),
            'X-Forwarded-Host': self.headers['Host'],
            'X-Forwarded-Proto': 'http',
        })
        if method in http.client._METHODS_EXPECTING_BODY:
            client.request(method, self.path, headers=req_headers, body=body)
        else:
            client.request(method, self.path, headers=req_headers)
        try:
            resp = client.getresponse()
        except:
            self.send_response(500)
            self.end_headers()
            host = client.host
            scheme = type(client).__name__[:-10].lower()
            url = urllib.parse.urlunsplit((host, scheme, '', ''))
            message = 'Something went wrong proxying to {}'.format(url)
            self.wfile.write(message.encode('utf-8'))
        else:
            self.send_response(resp.code)
            for k, v in resp.headers.items():
                if k not in ('Transfer-Encoding', 'Connection'):
                    self.send_header(k, v)
            self.end_headers()
            self.copyfile(resp, self.wfile)

    def do_GET(self):
        if self.path.startswith(PORTAL_PATH_PREFIXES):
            return self._proxy(PORTAL_CLIENT_CLASS, PORTAL_HOST, 'portal')

        if self.path.startswith('/_search'):
            return self._proxy(SEARCH_CLIENT_CLASS, SEARCH_HOST, 'search')

        redirect = self.server.redirects.get(self.path)
        if redirect:
            sa = self.server.socket.getsockname()
            location = 'http://{}:{}{}'.format(*sa, redirect)
            self.send_response(302)
            self.send_header('Location', location)
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith(PORTAL_PATH_PREFIXES):
            content_len = int(self.headers.get('Content-Length'))
            body = self.rfile.read(content_len)
            return self._proxy(PORTAL_CLIENT_CLASS, PORTAL_HOST, 'portal', method='POST', body=body)

    # Need to handle OPTIONS since they are sent before POST
    def do_OPTIONS(self):
        # Just proxy it to portal
        return self._proxy(PORTAL_CLIENT_CLASS, PORTAL_HOST, 'portal', method='OPTIONS')

    def do_DELETE(self):
        return self._proxy(PORTAL_CLIENT_CLASS, PORTAL_HOST, 'portal', method='DELETE')

    def do_PUT(self):
        content_len = int(self.headers.get('Content-Length'))
        body = self.rfile.read(content_len)
        return self._proxy(PORTAL_CLIENT_CLASS, PORTAL_HOST, 'portal', method='PUT', body=body)

    def translate_path(self, path):
        """Translate a /-separated PATH to the local filename syntax.

        Components that mean special things to the local file system
        (e.g. drive or directory names) are ignored.  (XXX They should
        probably be diagnosed.)

        """
        # replace colons (not allowed in win paths) with tilde
        path = path.replace(':', '~')
        # abandon query parameters
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        try:
            path = urllib.parse.unquote(path, errors='surrogatepass')
        except UnicodeDecodeError:
            path = urllib.parse.unquote(path)
        path = posixpath.normpath(path)
        words = path.split('/')
        words = filter(None, words)
        path = DIR
        for word in words:
            if os.path.dirname(word) or word in (os.curdir, os.pardir):
                # Ignore components that are not a simple file/directory name
                continue
            path = os.path.join(path, word)

        if path is None:
            return ''
        path_ext = path.rsplit('.', 1)
        html_path = path + '.html'

        if path.endswith('.pdf'):
            return os.path.join(self.server.law_docs_path, os.path.relpath(path, DIR))
        elif len(path_ext) > 1 and path_ext[1] in filetypes:
            if os.path.isfile(path):
                return path
            else:
                return os.path.join(STATIC_ASSETS_DIR, os.path.relpath(path, DIR))
        elif os.path.isfile(html_path):
            return html_path
        elif os.path.isdir(path):
            index_page = os.path.join(path, 'index.html')
            return index_page
        else:
            return path

    def end_headers(self):
        # Commenting this since proxied response from the portal already contains
        # this header. Having two `Allow-Origin` headers makes browser drop
        # requests automatically.
        # self.send_header('Access-Control-Allow-Origin', '*')
        SimpleHTTPRequestHandler.end_headers(self)

    def address_string(self):
        """
        This function is used for logging. We comment out getfqdn because,
        on windows, getfqdn is very slow - it will make multiple DNS 
        requests that each time out after many seconds.
        """
        host, _ = self.client_address[:2]
        #return socket.getfqdn(host)
        return host

def get_http_client_info(upstream_name, url):
    """
    return the http.client class and host needed to reach the given url
    """
    if not url:
        return None, None
    scheme, host, *_ = urllib.parse.urlparse(url)
    if not scheme:
        print('Must include scheme in {}-proxy-url (e.g. https://example.com, rather than example.com)'.format(upstream_name))
    host = host.replace('localhost', '127.0.0.1')  # fix slow network issues on Win
    Client = getattr(http.client, scheme.upper() + 'Connection')
    print('PROXYING: "/_{}" to {}'.format(upstream_name, url))
    return Client, host


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', '-b', default='127.0.0.1', metavar='ADDRESS',
                        help='Specify alternate bind address '
                             '[default: localhost]')
    parser.add_argument('--port', action='store',
                        default=8000, type=int,
                        nargs='?',
                        help='Specify alternate port [default: 8000]')
    parser.add_argument('--search-proxy-url', default=DEFAULT_SEARCH_URL, metavar='SEARCH_URL',
                        help='url to proxy search requests to. [default: {}'.format(DEFAULT_SEARCH_URL))
    parser.add_argument('--portal-proxy-url', default=None, metavar='PORTAL_URL',
                        help='url to proxy portal requests to. [default: None]')
    parser.add_argument('--no-open-browser', default=False, action="store_true",
                        help='do not open the library in default browser after starting server.')
    parser.add_argument('--law-docs-path', default=LAW_DOCS_PATH,
                        help='a path to the law-docs directory.')
    parser.add_argument('--static-assets-repo-url', default=STATIC_ASSETS_REPO_URL,
                        help='a git repository url from which to download static assets.')
    parser.add_argument('--force-update-static-assets', default=False, action="store_true",
                        help='download static assets if already exists on a disk.')
    args = parser.parse_args()
    server_address = (args.bind, args.port)

    httpd = HTTPServer(server_address, RequestHandler)

    raw_redirects = []
    try:
        with open(os.path.join(DIR, 'redirects.json')) as f:
            raw_redirects = json.load(f)
    except:
        pass
    redirects = {r[0]: r[1] for r in raw_redirects}
    httpd.redirects = redirects

    httpd.law_docs_path = args.law_docs_path

    sa = httpd.socket.getsockname()
    url = 'http://{}:{}'.format(sa[0], sa[1])

    print("Visit {} in your webbrowser to view library...".format(url))
    print("\n\n*** This server is designed for local use. Do not use in production. ***\n\n")

    PORTAL_CLIENT_CLASS, PORTAL_HOST = get_http_client_info(
        'portal', args.portal_proxy_url)
    SEARCH_CLIENT_CLASS, SEARCH_HOST = get_http_client_info(
        'search', args.search_proxy_url)

    download_static_assets(args.static_assets_repo_url,
                           force=args.force_update_static_assets)

    def visit_library():
        time.sleep(2)
        webbrowser.open(url, new=2)

    if not args.no_open_browser:
        thread = threading.Thread(target=visit_library)
        thread.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received, exiting.")
        httpd.server_close()
        sys.exit(0)
