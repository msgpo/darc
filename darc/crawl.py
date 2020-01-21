# -*- coding: utf-8 -*-
"""Web crawlers."""

import datetime
import os
import re
import shutil
import sys
import traceback
import urllib.parse
import urllib.robotparser

import requests
import selenium.common.exceptions
import selenium.webdriver
import selenium.webdriver.common.proxy
import stem
import stem.control
import stem.process
import stem.util.term
import urllib3

import darc.typing as typing
from darc.const import QUEUE_REQUESTS, QUEUE_SELENIUM, SE_EMPTY
from darc.error import UnsupportedLink, render_error
from darc.link import Link, parse_link
from darc.parse import extract_links, get_sitemap, read_sitemap
from darc.requests import norm_session, tor_session
from darc.save import (has_folder, has_html, has_raw, has_robots, has_sitemap, save_headers,
                       save_html, save_robots, save_sitemap)
from darc.selenium import norm_driver, tor_driver
from darc.sites import crawler_hook, loader_hook

# link regex mapping
LINK_MAP = [
    (r'.*?\.onion', tor_session, tor_driver),
    (r'.*', norm_session, norm_driver),
]


def request_session(link: Link) -> typing.Session:
    """Get requests session."""
    for regex, session, _ in LINK_MAP:
        if re.match(regex, link.host):
            return session()
    raise UnsupportedLink(link)


def request_driver(link: Link) -> typing.Driver:
    """Get selenium driver."""
    for regex, _, driver in LINK_MAP:
        if re.match(regex, link.host):
            return driver()
    raise UnsupportedLink(link)


def fetch_sitemap(link: Link):
    """Fetch sitemap."""
    robots_path = has_robots(link)
    if robots_path is not None:

        with open(robots_path) as file:
            robots_text = file.read()

    else:

        robots_link = parse_link(urllib.parse.urljoin(link.url, '/robots.txt'))
        with request_session(robots_link) as session:
            try:
                response = session.get(robots_link.url)
            except requests.RequestException as error:
                print(render_error(f'Failed on {robots_link.url} <{error}>',
                                   stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                return

        if response.ok:
            save_robots(robots_link, response.text)
            robots_text = response.text
        else:
            print(render_error(f'Failed on {robots_link.url} [{response.status_code}]',
                               stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
            robots_text = ''

    sitemaps = get_sitemap(link.url, robots_text, host=link.host)
    for sitemap_link in sitemaps:
        sitemap_path = has_sitemap(sitemap_link)
        if sitemap_path is not None:

            with open(sitemap_path) as file:
                sitemap_text = file.read()

        else:

            with request_session(sitemap_link) as session:
                try:
                    response = session.get(sitemap_link.url)
                except requests.RequestException as error:
                    print(render_error(f'Failed on {sitemap_link.url} <{error}>',
                                       stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                    continue

            if not response.ok:
                print(render_error(f'Failed on {sitemap_link.url} [{response.status_code}]',
                                   stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                continue

            sitemap_text = response.text
            save_sitemap(sitemap_link, sitemap_text)

        # add link to queue
        [QUEUE_REQUESTS.put(url) for url in read_sitemap(link.url, sitemap_text)]  # pylint: disable=expression-not-assigned


def crawler(url: str):
    """Single crawler for a entry link."""
    link = parse_link(url)
    try:
        # timestamp
        timestamp = datetime.datetime.now()

        path = has_raw(timestamp, link)
        if path is not None:

            print(stem.util.term.format(f'[REQUESTS] Cached {link.url}', stem.util.term.Color.YELLOW))  # pylint: disable=no-member
            with open(path, 'rb') as file:
                html = file.read()

            # add link to queue
            [QUEUE_SELENIUM.put(href) for href in extract_links(link.url, html)]  # pylint: disable=expression-not-assigned

            # load sitemap.xml
            try:
                fetch_sitemap(link)
            except Exception:
                error = f'[Error loading sitemap of {link.url}]' + os.linesep + traceback.format_exc() + '-' * shutil.get_terminal_size().columns  # pylint: disable=line-too-long
                print(render_error(error, stem.util.term.Color.CYAN), file=sys.stderr)  # pylint: disable=no-member

        else:

            # if it's a new host
            new_host = has_folder(link) is None

            print(f'Requesting {link.url}')

            # fetch sitemap.xml
            if new_host:
                try:
                    fetch_sitemap(link)
                except Exception:
                    error = f'[Error fetching sitemap of {link.url}]' + os.linesep + traceback.format_exc() + '-' * shutil.get_terminal_size().columns  # pylint: disable=line-too-long
                    print(render_error(error, stem.util.term.Color.CYAN), file=sys.stderr)  # pylint: disable=no-member

            with request_session(link) as session:
                try:
                    # requests session hook
                    response = crawler_hook(link, session)
                except requests.exceptions.InvalidSchema as error:
                    print(render_error(f'Failed on {link.url} <{error}>',
                                       stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                    return
                except requests.RequestException as error:
                    print(render_error(f'Failed on {link.url} <{error}>',
                                       stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                    QUEUE_REQUESTS.put(link.url)
                    return

            # save headers
            save_headers(timestamp, link, response)

            # check content type
            ct_type = response.headers.get('Content-Type', 'undefined').casefold()
            if 'html' not in ct_type:
                # text = response.content
                # save_file(link, text)
                print(render_error(f'Unexpected content type from {link.url} ({ct_type})',
                                   stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                return

            html = response.content
            if not html:
                print(render_error(f'Empty response from {link.url}',
                                   stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                QUEUE_REQUESTS.put((timestamp, link.url))
                return

            # save HTML
            save_html(timestamp, link, html, raw=True)

            # add link to queue
            [QUEUE_REQUESTS.put(href) for href in extract_links(link.url, html)]  # pylint: disable=expression-not-assigned

            if not response.ok:
                print(render_error(f'Failed on {link.url} [{response.status_code}]',
                                   stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                QUEUE_REQUESTS.put(link.url)
                return

            # add link to queue
            QUEUE_SELENIUM.put((timestamp, link.url))

            print(f'Requested {link.url}')
    except Exception:
        error = f'[Error from {link.url}]' + os.linesep + traceback.format_exc() + '-' * shutil.get_terminal_size().columns  # pylint: disable=line-too-long
        print(render_error(error, stem.util.term.Color.CYAN), file=sys.stderr)  # pylint: disable=no-member
        QUEUE_REQUESTS.put(link.url)


def loader(entry: typing.Tuple[typing.Datetime, str]):
    """Single loader for a entry link."""
    timestamp, url = entry
    link = parse_link(url)

    try:
        path = has_html(timestamp, link)
        if path is not None:

            print(stem.util.term.format(f'[SELENIUM] Cached {link.url}', stem.util.term.Color.YELLOW))  # pylint: disable=no-member
            with open(path, 'rb') as file:
                html = file.read()

            # add link to queue
            [QUEUE_REQUESTS.put(href) for href in extract_links(link.url, html)]  # pylint: disable=expression-not-assigned

        else:

            print(f'Loading {link.url}')

            # retrieve source from Chrome
            with request_driver(link) as driver:
                try:
                    # selenium driver hook
                    driver = loader_hook(link, driver)
                except urllib3.exceptions.HTTPError as error:
                    print(render_error(f'Fail to load {link.url} <{error}>',
                                       stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                    QUEUE_SELENIUM.put((timestamp, link.url))
                    return
                except selenium.common.exceptions.WebDriverException as error:
                    print(render_error(f'Fail to load {link.url} <{error}>',
                                       stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                    QUEUE_SELENIUM.put((timestamp, link.url))
                    return

                # get HTML source
                html = driver.page_source

                if html == SE_EMPTY:
                    print(render_error(f'Empty page from {link.url}',
                                       stem.util.term.Color.RED), file=sys.stderr)  # pylint: disable=no-member
                    QUEUE_SELENIUM.put((timestamp, link.url))
                    return

            # save HTML
            save_html(timestamp, link, html)

            # add link to queue
            [QUEUE_REQUESTS.put(href) for href in extract_links(link.url, html)]  # pylint: disable=expression-not-assigned

            print(f'Loaded {link.url}')
    except Exception:
        error = f'[Error from {link.url}]' + os.linesep + traceback.format_exc() + '-' * shutil.get_terminal_size().columns  # pylint: disable=line-too-long
        print(render_error(error, stem.util.term.Color.CYAN), file=sys.stderr)  # pylint: disable=no-member
        QUEUE_SELENIUM.put(link.url)
