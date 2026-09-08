import queue
import threading

import pytest

from smartops_desktop.core import validate_workflow
from smartops_desktop.discovery import safe_http_url, fingerprint_layers, NetworkJournal
from smartops_desktop.desktop_discovery import probe_windows_at
from smartops_desktop.worker import replay


def test_url_privacy_strips_query_fragment_and_credentials_are_not_preserved():
    assert safe_http_url('https://example.org/report?a=secret#x') == 'https://example.org/report'


def test_fingerprint_layers_only_reports_available_evidence():
    fp = {'web': {'tag':'button'}, 'nexacro': {'available': False}, 'anchors': [{'text':'Export'}], 'visual': {'available': True}}
    assert fingerprint_layers(fp) == ['web', 'anchors', 'visual']


def test_workflow_keeps_multilayer_fingerprint():
    step = {'action':'click', 'selector':'#export', 'fingerprint': {'web': {'id':'export'}, 'anchors':[{'text':'Report'}]}, 'detected_by':['web','anchors']}
    clean = validate_workflow({'schema_version':1, 'name':'x', 'steps':[step]})
    assert clean['steps'][0]['fingerprint']['web']['id'] == 'export'


def test_oversized_fingerprint_rejected():
    with pytest.raises(ValueError, match='fingerprint'):
        validate_workflow({'schema_version':1, 'name':'x', 'steps':[{'action':'click','selector':'#x','fingerprint':{'x':'a'*110000}}]})


def test_desktop_click_requires_fingerprint():
    with pytest.raises(ValueError, match='fingerprint'):
        validate_workflow({'schema_version':1, 'name':'x', 'steps':[{'action':'desktop_click'}]})


def test_desktop_probe_is_safe_off_windows():
    result = probe_windows_at(10, 10, '/tmp', 1)
    assert 'windows_uia' in result


class Request:
    url = 'https://example.org/api/export?token=secret'
    method = 'POST'
    resource_type = 'xhr'


class Response:
    url = 'https://example.org/api/export?id=22'
    status = 200
    request = Request()


class PageEvents:
    def __init__(self): self.handlers = {}
    def on(self, name, callback): self.handlers[name] = callback


def test_network_journal_never_records_query_or_body():
    p = PageEvents(); j = NetworkJournal(); j.attach(p)
    p.handlers['request'](Request()); p.handlers['response'](Response())
    events = j.snapshot()
    assert events[0]['url'] == 'https://example.org/api/export'
    assert 'secret' not in str(events)
    assert 'body' not in str(events).lower()


def test_desktop_replay_is_explicitly_gated(tmp_path):
    flow = {'schema_version':1,'name':'x','steps':[{'action':'desktop_click','fingerprint':{'geometry':{'screen_x':1,'screen_y':2}}}]}
    with pytest.raises(ValueError, match='discovery layer'):
        replay(flow, {}, tmp_path, queue.Queue(), threading.Event())
