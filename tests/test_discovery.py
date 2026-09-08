import queue
import threading
import pytest

from smartops_desktop.discovery import ElementFingerprint, LAYER_ORDER, normalize_capture, safe_http_url, fingerprint_layers, NetworkJournal
from smartops_desktop.core import validate_workflow
from smartops_desktop.desktop_discovery import probe_windows_at, DesktopInputRecorder, _looks_sensitive
from smartops_desktop.worker import replay


def test_normalize_web_capture_marks_all_layers():
    result = normalize_capture({"action":"click","selector":"#go","label":"Go"}, page_url="https://example.test?a=x")
    assert result["best_layer"] == "web"
    assert [e["layer"] for e in result["evidence"]] == list(LAYER_ORDER)
    assert result["page_url"] == "https://example.test/"


def test_supplied_evidence_can_beat_web():
    result = normalize_capture({"action":"click","selector":"div:nth-of-type(9)","evidence":[
        {"layer":"web","available":True,"confidence":.3,"identity":{"selector":"div:nth-of-type(9)"}},
        {"layer":"nexacro","available":True,"confidence":.95,"identity":{"id":"btnSearch","form":"frmMain"}},
    ]})
    assert result["best_layer"] == "nexacro"


def test_confidence_is_clamped():
    fp=ElementFingerprint("click"); fp.add("web",True,5,{"selector":"#x"}); assert fp.evidence[0].confidence==1.0


def test_url_privacy_strips_query_fragment():
    assert safe_http_url('https://example.org/report?a=secret#x') == 'https://example.org/report'


def test_fingerprint_layers_only_available():
    fp=normalize_capture({"action":"click","evidence":[{"layer":"anchor","available":True,"confidence":.6,"identity":{"text":"Export"}}]})
    assert fingerprint_layers(fp)==['anchor']


def test_workflow_keeps_fingerprint_and_rejects_huge_one():
    step={'action':'click','selector':'#export','fingerprint':normalize_capture({'action':'click','selector':'#export'}),'detected_by':['web']}
    clean=validate_workflow({'schema_version':1,'name':'x','steps':[step]})
    assert clean['steps'][0]['fingerprint']['best_layer']=='web'
    with pytest.raises(ValueError,match='fingerprint'):
        validate_workflow({'schema_version':1,'name':'x','steps':[{'action':'click','selector':'#x','fingerprint':{'x':'a'*130000}}]})


def test_desktop_click_requires_fingerprint_and_replay_is_gated(tmp_path):
    with pytest.raises(ValueError,match='fingerprint'):
        validate_workflow({'schema_version':1,'name':'x','steps':[{'action':'desktop_click'}]})
    flow={'schema_version':1,'name':'x','steps':[{'action':'desktop_click','fingerprint':normalize_capture({'action':'desktop_click'})}]}
    with pytest.raises(ValueError,match='discovery'):
        replay(flow,{},tmp_path,queue.Queue(),threading.Event())


def test_desktop_probe_safe_on_non_windows():
    assert 'windows_uia' in probe_windows_at(10,10,'/tmp',1)


class Request:
    url='https://example.org/api/export?token=secret'; method='POST'; resource_type='xhr'
class Response:
    url='https://example.org/api/export?id=22'; status=200; request=Request()
class PageEvents:
    def __init__(self): self.handlers={}
    def on(self,name,callback): self.handlers[name]=callback


def test_network_journal_is_privacy_stripped():
    p=PageEvents(); j=NetworkJournal(); j.attach(p); p.handlers['request'](Request()); p.handlers['response'](Response())
    events=j.snapshot(); assert events[0]['url']=='https://example.org/api/export'; assert 'secret' not in str(events)


def test_native_layer_dedupes_browser_mouse_and_keys(tmp_path):
    recorder = DesktopInputRecorder(lambda step: None, threading.Event(), tmp_path)
    recorder.mark_browser_event(100, 200)
    recorder.mark_browser_key("Enter")
    assert recorder._is_browser_duplicate(103, 204) is True
    assert recorder._is_browser_key_duplicate("enter") is True


def test_native_sensitive_identity_is_blocked():
    assert _looks_sensitive(None, "Password", "loginPassword", "Edit") is True
    assert _looks_sensitive(None, "Production quantity", "qty", "Edit") is False
