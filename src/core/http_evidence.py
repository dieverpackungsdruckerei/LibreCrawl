"""Bounded HTTP retrieval and minimal evidence for headless crawls."""
import time
import errno
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
import requests
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import create_connection
from urllib3.exceptions import NewConnectionError, ConnectTimeoutError

REDIRECT_CODES = {301, 302, 303, 307, 308}


class FetchFailure(Exception):
    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


def validate_target(url, base_url, allow_private=False, crawl_external=False):
    parsed = urlparse(url)
    base = urlparse(base_url or url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise FetchFailure('Invalid HTTP target', 'blocked_target')
    if not crawl_external and parsed.hostname.removeprefix('www.') != base.hostname.removeprefix('www.'):
        raise FetchFailure('Target outside allowed hostname', 'blocked_target')
    if parsed.port != base.port and parsed.port not in (None, 80, 443):
        raise FetchFailure('Target port outside allowed origin', 'blocked_target')
    if not allow_private:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise FetchFailure('Target resolves to a non-public IP address', 'blocked_target')


def _connection_addresses(host, port, allow_private):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    else:
        addresses = [str(literal)]
    if not addresses or (not allow_private and any(not ipaddress.ip_address(address).is_global for address in addresses)):
        raise FetchFailure('Connection target resolves to a non-public IP address', 'blocked_target')
    return addresses


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """Session-local pools retain original Host, TLS SNI and certificate name."""
    def __init__(self, allow_private):
        self.allow_private = allow_private
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        allow_private = self.allow_private

        class PinnedConnect:
            def _new_conn(self):
                started = time.monotonic()
                timeout = self.timeout if self.timeout is None or isinstance(self.timeout, (int, float)) else socket.getdefaulttimeout()
                try:
                    addresses = _connection_addresses(self._dns_host, self.port, allow_private)
                except OSError as exc:
                    raise NewConnectionError(self, f'Pinned DNS lookup failed: {exc}') from exc
                last_error = None
                for address in addresses:
                    remaining = None if timeout is None else timeout - (time.monotonic() - started)
                    if remaining is not None and remaining <= 0:
                        raise ConnectTimeoutError(self, 'Pinned connection time budget exhausted') from last_error
                    try:
                        return create_connection((address, self.port), remaining,
                                                 source_address=self.source_address, socket_options=self.socket_options)
                    except OSError as exc:
                        if not isinstance(exc, (ConnectionError, TimeoutError)) and exc.errno not in {
                            errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
                            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.EADDRNOTAVAIL,
                        }:
                            raise NewConnectionError(self, f'Pinned connection failed: {exc}') from exc
                        last_error = exc
                if isinstance(last_error, TimeoutError):
                    raise ConnectTimeoutError(self, 'Pinned connection timed out') from last_error
                raise NewConnectionError(self, f'Pinned connection failed: {last_error}') from last_error

        class PinnedHTTPConnection(PinnedConnect, HTTPConnection):
            pass

        class PinnedHTTPSConnection(PinnedConnect, HTTPSConnection):
            pass

        class PinnedHTTPPool(HTTPConnectionPool):
            ConnectionCls = PinnedHTTPConnection

        class PinnedHTTPSPool(HTTPSConnectionPool):
            ConnectionCls = PinnedHTTPSConnection

        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http': PinnedHTTPPool, 'https': PinnedHTTPSPool}

    def send(self, request, **kwargs):
        if requests.utils.select_proxy(request.url, kwargs.get('proxies')):
            raise FetchFailure('Proxy transport cannot pin destination IP', 'blocked_target')
        return super().send(request, **kwargs)


class EvidenceSession(requests.Session):
    """Guard every actual request, including auxiliary fetches and redirects."""
    def __init__(self):
        super().__init__()
        self._policy = None

    @property
    def policy(self):
        return self._policy

    @policy.setter
    def policy(self, value):
        self._policy = value
        if value is not None:
            self.trust_env = False
            for scheme in ('http://', 'https://'):
                previous = self.adapters.get(scheme)
                self.mount(scheme, _PinnedAdapter(value.get('allow_private', False)))
                if previous is not None:
                    previous.close()

    def send(self, request, **kwargs):
        if self.policy:
            validate_target(request.url, **self.policy)
        return super().send(request, **kwargs)


def fetch_page(crawler, url, depth):
    started = time.monotonic()
    result = crawler.seo_extractor.create_empty_result(url, depth, 0, '')
    result.update(final_url=url, content_type='', x_robots_tag='', robots='',
                  canonical_url='', html_usable=False, redirects=[], error='',
                  error_kind='', request_time_ms=0, parse_time_ms=0,
                  retry_count=0, backoff_ms=0, request_count=0, size=0,
                  is_internal=crawler.link_manager.is_internal(url))
    current = url
    seen = set()
    try:
        for hop in range(11):
            validate_target(current, crawler.base_url or url,
                            crawler.config.get('allow_private_ips', True) if crawler.config.get('profile') != 'cockpit' else crawler.config.get('allow_private_ips', False),
                            crawler.config['crawl_external'])
            if current in seen:
                raise FetchFailure('Redirect loop', 'redirect_loop')
            if hop and crawler.config['respect_robots'] and not crawler._check_robots_txt(current):
                raise FetchFailure('Redirect target denied by robots.txt', 'robots_denied')
            seen.add(current)
            result['final_url'] = current
            response = None
            for attempt in range(crawler.config.get('retries', 3) + 1):
                try:
                    if crawler.rate_limiter:
                        crawler.rate_limiter.acquire()
                    result['request_count'] += 1
                    response = crawler.session.get(current, timeout=crawler.config['timeout'],
                                                   allow_redirects=False, stream=True)
                    break
                except requests.RequestException:
                    if attempt >= crawler.config.get('retries', 3):
                        raise
                    result['retry_count'] += 1
                    time.sleep(1)
                    result['backoff_ms'] += 1000
            try:
                result['status_code'] = response.status_code
                result['content_type'] = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                result['x_robots_tag'] = response.headers.get('x-robots-tag', '')
                if response.status_code in REDIRECT_CODES and crawler.config['follow_redirects']:
                    location = response.headers.get('location', '').strip()
                    if not location:
                        raise FetchFailure('Redirect without Location', 'redirect_invalid')
                    target = urljoin(current, location).split('#', 1)[0]
                    result['redirects'].append({'url': current, 'status_code': response.status_code, 'target': target})
                    if hop >= 10:
                        raise FetchFailure('Redirect limit exceeded', 'redirect_limit')
                    current = target
                    continue
                limit = crawler.config.get('max_file_size', 50 * 1024 * 1024)
                length = response.headers.get('content-length', '')
                if length.isdigit() and limit > 0 and int(length) > limit:
                    raise FetchFailure('Response exceeds configured size limit', 'response_too_large')
                content = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if limit > 0 and len(content) + len(chunk) > limit:
                        raise FetchFailure('Response exceeds configured size limit', 'response_too_large')
                    content.extend(chunk)
                result['size'] = len(content)
                result['html_usable'] = bool(response.status_code == 200 and content and
                    result['content_type'] in ('text/html', 'application/xhtml+xml'))
                return result, bytes(content)
            finally:
                response.close()
    except Exception as exc:
        result['html_usable'] = False
        result['error'] = str(exc)
        result['error_kind'] = getattr(exc, 'kind', 'fetch_error')
        # A failed target must never inherit the redirect source's status.
        if result['error_kind'] in ('fetch_error', 'blocked_target', 'robots_denied', 'redirect_loop', 'redirect_limit'):
            result['status_code'] = 0
        return result, b''
    finally:
        result['request_time_ms'] = round((time.monotonic() - started) * 1000, 2)
        result['response_time'] = result['request_time_ms']


def minimal_metadata(soup, result):
    head = soup.head
    title = head.find('title') if head else None
    canonical = head.find('link', rel='canonical') if head else None
    robots = head.find('meta', attrs={'name': 'robots'}) if head else None
    result['title'] = title.get_text(' ', strip=True) if title else ''
    result['canonical_url'] = urljoin(result['final_url'], canonical.get('href', '')) if canonical else ''
    result['robots'] = robots.get('content', '') if robots else ''
    result['landmarks'] = {'main': bool(soup.find('main')), 'navigation': bool(soup.find('nav')),
                           'footer': bool(soup.find('footer'))}
