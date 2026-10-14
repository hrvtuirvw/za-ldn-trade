"""Standard handshake must keep exact synthetic authenticated wire bytes."""
from pathlib import Path
import hashlib
import json
import pytest
import zasim.handshake as handshake
from test_handshake_public import test_real_generic_handshake_never_sends_trade_and_preserves_errors as real_handshake

CORE_FIELDS = ('handshake_only','completed','elapsed_seconds','output','connected',
    'rtt_round_trips','rtt_samples_ticks','missing_milestones','reliable_messages_sent',
    'boxtrade_commands_sent','session_phase_timeout','session_message_type_counts',
    'packets_received','packets_sent','decode_failures','ignored_peer_packets',
    'ignored_premature_messages','net_requests_received','net_responses_sent',
    'net_keepalive_requests_received','net_keepalive_acks_sent','session_updates_received',
    'session_joins_sent','session_finalizes_sent','rtt_requests_received',
    'rtt_responses_received','rtt_requests_sent','rtt_responses_sent','ignored_title_messages')

@pytest.mark.parametrize('compressed', [False,True])
def test_standard_handshake_wire_and_milestones_unchanged(monkeypatch, compressed):
    trace=[];reports=[]
    base_writer=handshake._LivePacketWriter
    class Writer(base_writer):
        def __init__(self,*a,**kw):
            super().__init__(*a,**kw);self._header_nonce=1234
        def encode(self,item):
            raw,destination=super().encode(item)
            trace.append({'wire_hex':raw.hex(),'destination':destination})
            return raw,destination
    original=handshake.run_handshake_diagnostic
    def run(**kwargs):
        result=original(**kwargs);reports.append({k:result[k] for k in CORE_FIELDS});return result
    monkeypatch.setattr(handshake,'_LivePacketWriter',Writer)
    monkeypatch.setattr(handshake,'run_handshake_diagnostic',run)
    real_handshake(monkeypatch,compressed,None)
    payload={'outbound':trace,'normal_report':reports}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    reference=Path(__file__).with_name('handshake_wire_reference.json')
    assert digest==json.loads(reference.read_text())[str(compressed)]
