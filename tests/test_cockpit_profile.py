import io
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup
import requests
try:
    import playwright.async_api
except ModuleNotFoundError as error:
    if error.name not in ('playwright', 'playwright.async_api'):
        raise
    # Browser rendering is outside these HTTP-only mocked tests.
    def unavailable_playwright():
        raise AssertionError('Browser rendering must not run in offline HTTP tests')

    browser_package = ModuleType('playwright')
    browser_api = ModuleType('playwright.async_api')
    browser_api.async_playwright = unavailable_playwright
    browser_api.TimeoutError = TimeoutError
    previous_package = sys.modules.get('playwright')
    sys.modules['playwright'] = browser_package
    sys.modules['playwright.async_api'] = browser_api
    try:
        from cli import build_parser, format_output, main
        from src.crawler import WebCrawler
    finally:
        sys.modules.pop('playwright.async_api', None)
        if previous_package is None:
            sys.modules.pop('playwright', None)
        else:
            sys.modules['playwright'] = previous_package
else:
    from cli import build_parser, format_output, main
    from src.crawler import WebCrawler
from src.core.link_manager import LinkManager
from src.core.http_evidence import EvidenceSession, FetchFailure, validate_target


class Response:
    def __init__(self, status=200, body=b'<html><head><title>Example</title></head><body></body></html>', headers=None):
        self.status_code = status
        self.headers = {'content-type': 'text/html; charset=utf-8', **(headers or {})}
        self.body = body
        self.closed = False
    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]
    def close(self):
        self.closed = True


class ProfileTest(unittest.TestCase):
    def crawler(self, responses, profile='cockpit'):
        crawler = WebCrawler()
        crawler.update_config({'profile': profile, 'allow_private_ips': True, 'respect_robots': False,
                               'retries': 0, 'max_depth': 2, 'delay': 0})
        crawler.base_url = 'https://example.com'
        crawler.base_domain = 'example.com'
        crawler.link_manager = LinkManager('example.com')
        crawler.session = Mock()
        crawler.session.get.side_effect = responses
        crawler.rate_limiter = Mock()
        return crawler

    def test_redirect_keeps_source_and_uses_final_base(self):
        responses = [Response(301, headers={'location': '/nested/'}), Response(body=b'<html><head><link rel="canonical" href="canonical"><meta name="robots" content="noindex"></head><body><main><a href="target">Target</a></main></body></html>')]
        c = self.crawler(responses)
        result = c._crawl_url_with_requests('https://example.com/start', 0)
        self.assertEqual(result['url'], 'https://example.com/start')
        self.assertEqual(result['final_url'], 'https://example.com/nested/')
        self.assertEqual(result['canonical_url'], 'https://example.com/nested/canonical')
        self.assertEqual(result['robots'], 'noindex')
        self.assertEqual(result['request_count'], 2)
        self.assertEqual(c.link_manager.all_links[0]['target_url'], 'https://example.com/nested/target')
        self.assertEqual(c.link_manager.all_links[0]['source_url'], result['url'])
        self.assertEqual(result['redirects'][0]['status_code'], 301)
        self.assertTrue(all(r.closed for r in responses))
        c.session.head.assert_not_called()
        self.assertTrue(all(call.kwargs['stream'] and not call.kwargs['allow_redirects'] for call in c.session.get.call_args_list))

    def test_single_get_and_skips_heavy_extractors(self):
        c = self.crawler([Response()])
        with patch.object(c.seo_extractor, 'extract_json_ld', side_effect=AssertionError('heavy extractor called')):
            result = c._crawl_url_with_requests('https://example.com/', 0)
        self.assertEqual(result['title'], 'Example')
        self.assertEqual(c.session.get.call_count, 1)
        self.assertEqual(result['request_count'], 1)
        self.assertEqual(result['retry_count'], 0)
        self.assertTrue(result['html_usable'])
        self.assertFalse(c.config['enable_duplication_check'])
        self.assertFalse(c.config['enable_javascript'])

    def test_full_profile_http_evidence_and_graph_equivalent(self):
        body = b'<html><head><title>Same</title><link rel="canonical" href="/canonical"></head><body><nav><a href="/target">Go</a></nav></body></html>'
        results = []
        for profile in ('full', 'cockpit'):
            c = self.crawler([Response(body=body)], profile)
            result = c._crawl_url_with_requests('https://example.com/', 0)
            results.append((result, c.link_manager.all_links))
        for key in ('title', 'url', 'final_url', 'canonical_url', 'status_code', 'html_usable'):
            self.assertEqual(results[0][0][key], results[1][0][key])
        self.assertEqual(results[0][1], results[1][1])

    def test_non_html_error_status_and_empty_no_metadata(self):
        for response in (Response(headers={'content-type': 'application/json'}), Response(404), Response(body=b'')):
            c = self.crawler([response])
            result = c._crawl_url_with_requests('https://example.com/', 0)
            self.assertFalse(result['html_usable'])
            self.assertEqual(result['title'], '')
            self.assertEqual(c.link_manager.all_links, [])

    def test_stream_limit_preserves_status(self):
        c = self.crawler([Response(body=b'x' * 70)])
        c.config['max_file_size'] = 64
        result = c._crawl_url_with_requests('https://example.com/', 0)
        self.assertEqual(result['error_kind'], 'response_too_large')
        self.assertEqual(result['status_code'], 200)
        self.assertFalse(result['html_usable'])

    def test_declared_size_limit_no_body_read(self):
        response = Response(headers={'content-length': '9999'})
        response.iter_content = Mock(side_effect=AssertionError('body read'))
        c = self.crawler([response])
        c.config['max_file_size'] = 64
        result = c._crawl_url_with_requests('https://example.com/', 0)
        self.assertEqual(result['error_kind'], 'response_too_large')
        response.iter_content.assert_not_called()
        self.assertTrue(response.closed)

    def test_failed_redirect_does_not_inherit_source_status(self):
        c = self.crawler([Response(301, headers={'location': '/target'}), requests.Timeout('timeout')])
        result = c._crawl_url_with_requests('https://example.com/', 0)
        self.assertEqual(result['status_code'], 0)
        self.assertEqual(result['error_kind'], 'fetch_error')
        self.assertEqual(len(result['redirects']), 1)
        self.assertEqual(result['final_url'], 'https://example.com/target')

    def test_retry_counts_and_wait_are_separate(self):
        c = self.crawler([requests.Timeout('temporary'), Response()])
        c.config['retries'] = 1
        with patch('src.core.http_evidence.time.sleep') as sleep:
            result = c._crawl_url_with_requests('https://example.com/', 0)
        self.assertEqual(result['retry_count'], 1)
        self.assertEqual(result['backoff_ms'], 1000)
        self.assertEqual(result['request_count'], 2)
        self.assertEqual(c.rate_limiter.acquire.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_loop_and_external_redirect_blocked_before_get(self):
        for location, kind in (('/', 'redirect_loop'), ('https://other.example/path', 'blocked_target')):
            c = self.crawler([Response(301, headers={'location': location})])
            result = c._crawl_url_with_requests('https://example.com/', 0)
            self.assertEqual(result['error_kind'], kind)
            self.assertEqual(c.session.get.call_count, 1)

    def test_base_href_same_host_only(self):
        for base, expected in (('/other/', 'https://example.com/other/child'), ('https://other.example/', 'https://example.com/child')):
            c = self.crawler([Response(body=f'<html><head><base href="{base}"></head><body><a href="child">Go</a></body></html>'.encode())])
            c._crawl_url_with_requests('https://example.com/', 0)
            self.assertEqual(c.link_manager.all_links[0]['target_url'], expected)

    def test_html_parser_preserves_head_metadata_after_padding(self):
        body = b'<html><head><script type="application/ld+json">{}</script>' + b' ' * 1000 + b'<title>Correct</title></head><body></body></html>'
        result = self.crawler([Response(body=body)])._crawl_url_with_requests('https://example.com/', 0)
        self.assertEqual(result['title'], 'Correct')

    def test_schema_and_defaults(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(['https://example.com']).profile, 'full')
        for profile, version in (('full', 1), ('cockpit', 2)):
            args = parser.parse_args(['https://example.com', '--profile', profile])
            payload = json.loads(format_output({'urls': [{'url': 'https://example.com', 'nested': {'a': 1}}]}, args))
            self.assertEqual(payload['schema_version'], version)
            self.assertEqual(payload['urls'][0]['nested'], {'a': 1})


class CoverageTest(unittest.TestCase):
    def crawler(self):
        c = WebCrawler()
        c.update_config({'profile': 'cockpit', 'allow_private_ips': True, 'respect_robots': False})
        c.base_domain = 'example.com'
        c.link_manager = LinkManager('example.com')
        return c

    def test_not_running_with_results_does_not_imply_completion(self):
        c = self.crawler()
        c.stats['crawled'] = 1
        c.link_manager.add_url('https://example.com/', 0)
        c.link_manager.get_next_url()
        self.assertEqual(c.get_status()['status'], 'idle')
        self.assertFalse(c.get_status()['stats']['coverage_complete'])

    def test_actual_completion_exports_status_and_coverage(self):
        c = self.crawler()
        c.link_manager.add_url('https://example.com/', 0)
        c.link_manager.get_next_url()
        c.stats['crawled'] = 1
        with patch.object(c, '_crawl_worker_impl'):
            c._crawl_worker()
        status = c.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertTrue(status['stats']['coverage_complete'])
        args = build_parser().parse_args(['https://example.com/', '--profile', 'cockpit'])
        exported = json.loads(format_output(status, args))
        self.assertEqual(exported['status'], 'completed')
        self.assertTrue(exported['stats']['coverage_complete'])

    def test_stop_failure_demo_empty_and_limits_are_incomplete(self):
        for kind in ('stopped', 'failed', 'demo_stopped', 'empty', 'limit', 'missing_result'):
            c = self.crawler()
            c.link_manager.add_url('https://example.com/', 0)
            c.link_manager.get_next_url()
            c.stats['crawled'] = 1
            if kind == 'stopped':
                c.stop_crawl()
            elif kind == 'failed':
                with patch.object(c, '_crawl_worker_impl', side_effect=RuntimeError('failure')):
                    c._crawl_worker()
            else:
                if kind == 'demo_stopped':
                    c._demo_limit_reached = True
                elif kind == 'empty':
                    c.stats['crawled'] = 0
                elif kind == 'limit':
                    c.config['max_urls'] = 1
                    c.link_manager.add_url('https://example.com/pending', 1)
                elif kind == 'missing_result':
                    c.link_manager.add_url('https://example.com/missing', 1)
                    c.link_manager.get_next_url()
                with patch.object(c, '_crawl_worker_impl'):
                    c._crawl_worker()
            with self.subTest(kind=kind):
                self.assertFalse(c.get_status()['stats']['coverage_complete'])
                if kind in ('stopped', 'failed', 'demo_stopped'):
                    self.assertEqual(c.get_status()['status'], kind)

    def test_task_exception_produces_failed_terminal_status(self):
        c = self.crawler()
        c._initialize_components()
        c.link_manager.add_url('https://example.com/', 0)
        c.is_running = True
        with patch.object(c, '_crawl_url', side_effect=RuntimeError('task failed')):
            c._crawl_worker()
        self.assertEqual(c.get_status()['status'], 'failed')
        self.assertFalse(c.get_status()['stats']['coverage_complete'])

    def test_failed_cli_preserves_partial_export_and_exits_nonzero(self):
        output = io.StringIO()
        with patch('sys.argv', ['cli.py', 'https://example.com/', '--profile', 'cockpit', '--quiet']), patch('cli.run_crawl', return_value={'status': 'failed', 'stats': {'coverage_complete': False}, 'urls': [{'url': 'https://example.com/'}]}), patch('sys.stdout', output):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)
        partial = json.loads(output.getvalue())
        self.assertEqual(partial['status'], 'failed')
        self.assertEqual(len(partial['urls']), 1)

    def test_stop_during_worker_is_not_overwritten(self):
        c = self.crawler()
        with patch.object(c, '_crawl_worker_impl', side_effect=c.stop_crawl):
            c._crawl_worker()
        self.assertEqual(c.get_status()['status'], 'stopped')



class GraphTest(unittest.TestCase):
    def test_occurrences_keep_anchor_placement_and_index_status(self):
        manager = LinkManager('example.com')
        manager.record_result({'url': 'https://example.com/target', 'status_code': 404})
        soup = BeautifulSoup('<nav><a href="/target">Menu</a></nav><main><a href="/target">Body</a></main><footer><a href="/target">Footer</a></footer>', 'html.parser')
        # A poisonous list proves no per-link result-list iteration.
        class Unscannable:
            def __iter__(self):
                raise AssertionError('linear scan')
        manager.collect_all_links(soup, 'https://example.com/', Unscannable())
        self.assertEqual(len(manager.all_links), 1)
        link = manager.all_links[0]
        self.assertEqual(link['target_status'], 404)
        self.assertEqual([x['placement'] for x in link['occurrences']], ['navigation', 'body', 'footer'])
        self.assertEqual([x['anchor_text'] for x in link['occurrences']], ['Menu', 'Body', 'Footer'])

    def test_menu_substring_does_not_classify_body(self):
        soup = BeautifulSoup('<article class="menu-item-description"><a href="/a">Go</a></article>', 'html.parser')
        # Explicit menu token is still navigation; arbitrary substrings are not.
        soup.article['class'] = ['foodmenucontent']
        self.assertEqual(LinkManager('example.com')._detect_link_placement(soup.a), 'body')

    def test_future_target_status_and_reset(self):
        manager = LinkManager('example.com')
        manager.collect_all_links(BeautifulSoup('<a href="/a">A</a>', 'html.parser'), 'https://example.com/', [])
        self.assertIsNone(manager.all_links[0]['target_status'])
        manager.update_link_statuses([{'url': 'https://example.com/a', 'status_code': 200}])
        self.assertEqual(manager.all_links[0]['target_status'], 200)
        manager.reset()
        self.assertEqual(manager.target_evidence, {})
        self.assertEqual(manager.status_index, {})


class SecurityTest(unittest.TestCase):
    def test_private_ip_denied_public_allowed(self):
        for address, allowed in (('127.0.0.1', False), ('10.0.0.1', False), ('::1', False), ('8.8.8.8', True)):
            with patch('src.core.http_evidence.socket.getaddrinfo', return_value=[(0, 0, 0, '', (address, 443))]):
                if allowed:
                    validate_target('https://example.com/', 'https://example.com/')
                else:
                    with self.assertRaises(FetchFailure):
                        validate_target('https://example.com/', 'https://example.com/')
        validate_target('http://127.0.0.1/', 'http://127.0.0.1/', allow_private=True)

    def test_auxiliary_request_session_guard(self):
        session = EvidenceSession()
        session.policy = dict(base_url='https://example.com/', allow_private=False, crawl_external=False)
        request = requests.Request('GET', 'https://other.example/sitemap.xml').prepare()
        with self.assertRaises(FetchFailure):
            session.send(request)

    def test_credentials_and_protocol_denied(self):
        for url in ('file:///etc/passwd', 'https://user:pass@example.com', 'http://example.com:9999'):
            with self.assertRaises(FetchFailure):
                validate_target(url, 'https://example.com', allow_private=True)


if __name__ == '__main__':
    unittest.main()


class ConnectionPinningTest(unittest.TestCase):
    @staticmethod
    def addresses(ip, port=80):
        import socket
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 80))]

    def session(self, allow_private=False):
        session = EvidenceSession()
        session.policy = dict(base_url='http://rebind.test/', allow_private=allow_private, crawl_external=False)
        self.addCleanup(session.close)
        return session

    def test_rebinding_at_tcp_boundary_never_connects_private(self):
        session = self.session()
        with patch('socket.getaddrinfo', side_effect=[self.addresses('93.184.216.34'), self.addresses('127.0.0.1')]), patch('socket.socket.connect') as connect:
            with self.assertRaises(FetchFailure):
                session.get('http://rebind.test/', timeout=1)
            connect.assert_not_called()

    def test_numeric_pinned_dial_avoids_second_hostname_resolution(self):
        session = self.session()
        names = []
        def dns(host, port, *args, **kwargs):
            names.append(host)
            return self.addresses('93.184.216.34' if host == '93.184.216.34' or names.count('rebind.test') <= 2 else '127.0.0.1', port)
        with patch('socket.getaddrinfo', side_effect=dns), patch('socket.socket.connect', side_effect=OSError('stop before network')) as connect:
            with self.assertRaises(requests.ConnectionError):
                session.get('http://rebind.test/', timeout=1)
            self.assertEqual(names.count('rebind.test'), 2)
            self.assertTrue(connect.called)
            self.assertTrue(all(c.args[0][0] == '93.184.216.34' for c in connect.call_args_list))

    def test_https_keeps_hostname_for_sni_and_certificate(self):
        session = self.session()
        adapter = session.get_adapter('https://rebind.test/')
        pool = adapter.poolmanager.connection_from_url('https://rebind.test/')
        connection = pool._new_conn()
        sock = Mock()
        with patch('socket.getaddrinfo', return_value=self.addresses('93.184.216.34', 443)), patch('src.core.http_evidence.create_connection', return_value=sock) as dial, patch('urllib3.connection._ssl_wrap_socket_and_match_hostname', return_value=SimpleNamespace(socket=sock, is_verified=True)) as tls:
            connection.connect()
            self.assertEqual(dial.call_args.args[0], ('93.184.216.34', 443))
            self.assertEqual(tls.call_args.kwargs['server_hostname'], 'rebind.test')
            self.assertEqual(connection.host, 'rebind.test')
            self.assertTrue(connection.is_verified)

    def test_pool_policy_does_not_change_other_sessions(self):
        from urllib3 import PoolManager
        before = PoolManager().pool_classes_by_scheme.copy()
        public = self.session()
        private = self.session(allow_private=True)
        self.assertEqual(PoolManager().pool_classes_by_scheme, before)
        pool = private.get_adapter('http://').poolmanager.connection_from_url('http://127.0.0.1/')
        with patch('src.core.http_evidence.create_connection', return_value=Mock()) as connect:
            pool._new_conn()._new_conn()
            self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 80))
        with self.assertRaises(FetchFailure):
            public.get_adapter('http://').poolmanager.connection_from_url('http://127.0.0.1/')._new_conn()._new_conn()

    def test_proxy_fails_closed(self):
        session = self.session()
        with patch('socket.getaddrinfo', return_value=self.addresses('93.184.216.34')):
            with self.assertRaises(FetchFailure):
                session.get('http://rebind.test/', proxies={'http': 'http://127.0.0.1:8888'})


class ConnectionFallbackTest(unittest.TestCase):
    def connection(self):
        session = EvidenceSession()
        self.addCleanup(session.close)
        session.policy = dict(base_url='https://host.test/', allow_private=False, crawl_external=False)
        pool = session.get_adapter('https://').poolmanager.connection_from_url('https://host.test/')
        connection = pool._new_conn()
        connection.timeout = 5
        return connection

    def test_failed_ipv6_falls_back_to_ipv4_with_remaining_budget(self):
        clock, calls = [0.0], []
        expected = Mock()
        def dial(address, timeout, **kwargs):
            calls.append((address, timeout))
            if len(calls) == 1:
                clock[0] += 2
                raise ConnectionRefusedError('first route unreachable')
            return expected
        with patch('src.core.http_evidence._connection_addresses', return_value=['2606:4700:4700::1111', '93.184.216.34']), patch('src.core.http_evidence.time', SimpleNamespace(monotonic=lambda: clock[0])), patch('src.core.http_evidence.create_connection', side_effect=dial):
            self.assertIs(self.connection()._new_conn(), expected)
        self.assertEqual(calls, [(('2606:4700:4700::1111', 443), 5), (('93.184.216.34', 443), 3)])

    def test_all_fail_does_not_reset_total_connect_budget(self):
        from urllib3.exceptions import ConnectTimeoutError
        clock, calls = [0.0], []
        def dial(address, timeout, **kwargs):
            calls.append((address, timeout))
            clock[0] += timeout
            raise TimeoutError('first address exhausted budget')
        with patch('src.core.http_evidence._connection_addresses', return_value=['93.184.216.34', '1.1.1.1']), patch('src.core.http_evidence.time', SimpleNamespace(monotonic=lambda: clock[0])), patch('src.core.http_evidence.create_connection', side_effect=dial):
            with self.assertRaises(ConnectTimeoutError):
                self.connection()._new_conn()
        self.assertEqual(calls, [(('93.184.216.34', 443), 5)])

    def test_nonconnection_errors_do_not_try_second_address(self):
        from urllib3.exceptions import NewConnectionError
        import errno
        with patch('src.core.http_evidence._connection_addresses', return_value=['93.184.216.34', '1.1.1.1']), patch('src.core.http_evidence.create_connection', side_effect=OSError(errno.EACCES, 'permission denied')) as dial:
            with self.assertRaises(NewConnectionError):
                self.connection()._new_conn()
            self.assertEqual(dial.call_count, 1)

