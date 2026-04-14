#!/usr/bin/env python3
"""
LibreCrawl CLI — headless crawling for automated tests and CI/CD.

Usage:
    python cli.py <url> [options]

Examples:
    # Basic crawl, JSON to stdout
    python cli.py https://example.com

    # Shallow crawl, save to file
    python cli.py https://example.com --max-depth 2 --max-urls 100 --output results.json

    # CI pipeline: exit 1 if SEO issues found
    python cli.py https://example.com --fail-on-issues --quiet && echo "No issues"

    # CSV export, no JavaScript rendering
    python cli.py https://example.com --format csv --output crawl.csv --no-js
"""

import argparse
import json
import os
import signal
import sys
import time

# Ensure the project root is on the path when called from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.crawler import WebCrawler
from src.export import (
    generate_csv_export,
    generate_json_export,
    generate_issues_json_export,
)


def build_parser():
    p = argparse.ArgumentParser(
        prog='librecrawl',
        description='LibreCrawl CLI — headless crawling for automated tests and CI/CD',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('url', help='Start URL to crawl')

    crawl = p.add_argument_group('Crawl options')
    crawl.add_argument('--max-depth',    '-d', type=int,   default=3,
                       help='Maximum crawl depth (default: 3)')
    crawl.add_argument('--max-urls',     '-n', type=int,   default=500,
                       help='Maximum number of URLs to crawl (default: 500)')
    crawl.add_argument('--delay',              type=float, default=0.5,
                       help='Delay between requests in seconds (default: 0.5)')
    crawl.add_argument('--concurrency',        type=int,   default=5,
                       help='Number of concurrent crawl threads (default: 5)')
    crawl.add_argument('--no-js',              action='store_true',
                       help='Disable JavaScript rendering (faster, uses requests only)')
    crawl.add_argument('--no-robots',          action='store_true',
                       help='Ignore robots.txt')
    crawl.add_argument('--external',           action='store_true',
                       help='Also crawl external links')

    output = p.add_argument_group('Output options')
    output.add_argument('--format',      '-f', choices=['json', 'csv'], default='json',
                        help='Output format (default: json)')
    output.add_argument('--output',      '-o', metavar='FILE',
                        help='Write output to FILE instead of stdout')
    output.add_argument('--fields',            metavar='FIELD,...',
                        help='Comma-separated list of fields for CSV export '
                             '(default: url,status_code,title,meta_description,h1,word_count,response_time)')
    output.add_argument('--quiet',       '-q', action='store_true',
                        help='Suppress progress output on stderr')
    output.add_argument('--fail-on-issues',    action='store_true',
                        help='Exit with code 1 if any SEO issues are detected (useful for CI)')

    return p


def run_crawl(args):
    crawler = WebCrawler()
    crawler.db_save_enabled = False  # no DB overhead in CLI mode

    config = {
        'max_depth':          args.max_depth,
        'max_urls':           args.max_urls,
        'delay':              args.delay,
        'concurrency':        args.concurrency,
        'enable_javascript':  not args.no_js,
        'respect_robots':     not args.no_robots,
        'crawl_external':     args.external,
    }
    crawler.update_config(config)

    # Handle Ctrl+C gracefully
    def _on_sigint(*_):
        if not args.quiet:
            print('\nStopping crawl...', file=sys.stderr)
        crawler.stop_crawl()
        sys.exit(130)

    signal.signal(signal.SIGINT, _on_sigint)

    ok, msg = crawler.start_crawl(args.url)
    if not ok:
        print(f'Error: {msg}', file=sys.stderr)
        sys.exit(1)

    # Polling loop with progress output to stderr
    terminal_statuses = ('completed', 'failed', 'stopped', 'demo_stopped')
    while True:
        s = crawler.get_status()
        if not args.quiet:
            pct     = s.get('progress', 0)
            crawled = s['stats'].get('crawled', 0)
            found   = s['stats'].get('discovered', 0)
            speed   = s['stats'].get('speed', 0)
            print(
                f'\r  {pct:5.1f}%  {crawled}/{found} URLs  {speed:.1f} URLs/s   ',
                end='',
                file=sys.stderr,
                flush=True,
            )
        if s['status'] in terminal_statuses:
            break
        time.sleep(0.5)

    if not args.quiet:
        print(file=sys.stderr)  # newline after progress line

    return crawler.get_status()


def format_output(status, args):
    urls   = status.get('urls', [])
    issues = status.get('issues', [])

    if args.format == 'csv':
        default_fields = 'url,status_code,title,meta_description,h1,word_count,response_time'
        fields = [f.strip() for f in (args.fields or default_fields).split(',')]
        return generate_csv_export(urls, fields)

    # JSON: combine stats + urls + issues into one document
    fields = list(urls[0].keys()) if urls else []
    urls_data   = json.loads(generate_json_export(urls, fields))['data'] if urls else []
    issues_data = json.loads(generate_issues_json_export(issues)) if issues else {}

    return json.dumps({
        'crawl_date': time.strftime('%Y-%m-%d %H:%M:%S'),
        'stats':      status.get('stats', {}),
        'urls':       urls_data,
        'issues':     issues_data,
    }, indent=2, ensure_ascii=False)


def main():
    args = build_parser().parse_args()

    if not args.quiet:
        print(f'Crawling: {args.url}', file=sys.stderr)

    status = run_crawl(args)
    output = format_output(status, args)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        if not args.quiet:
            issues_count = len(status.get('issues', []))
            urls_count   = len(status.get('urls', []))
            print(
                f'Saved to {args.output}  '
                f'({urls_count} URLs, {issues_count} issues)',
                file=sys.stderr,
            )
    else:
        print(output)

    if args.fail_on_issues and status.get('issues'):
        sys.exit(1)


if __name__ == '__main__':
    main()
