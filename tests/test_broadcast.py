import pytest
from zasim.broadcast import BroadcastReceiver, BroadcastError
from zasim.reliable import ReliableFrame, parse_reliable, parse_bulk_ack
from zasim.client import ZATradeClient, ReliableTradeEngine
from zasim.session import ZASession
from zasim.session_update import SessionStation
from zasim.pia import PiaMessage, PiaHeader, DecodedDatagram

def frame(seq=0xfff0, flags=15, bits=3, mask=2, body=b'\x14\x00test'):
    return ReliableFrame(flags,seq,0xfff0,body,bits,(mask,) if bits else ()).pack()

def test_independent_station_array_ack():
    r=BroadcastReceiver(1,0);m=r.feed(frame());assert len(m)==1
    f=parse_reliable(r.drain_ack());a=parse_bulk_ack(f.payload)
    assert f.sequence_id==f.lowest_pending_ack==0xfff0 and f.flags==0 and f.destination_bit_count==0
    assert len(a.entries)==3
    assert [e.acknowledgement_id for e in a.entries]==[0xfff1,0xfff0,0xfff0]
    assert r.drain_ack() is None

def test_native_initialization_gate_no_false_ack():
    r=BroadcastReceiver(1,0);assert not r.feed(frame(flags=7))
    assert not r.initialized and r.drain_ack() is None
    assert len(r.feed(frame(flags=15)))==1

def test_initial_frame_can_establish_nondefault_sequence():
    r=BroadcastReceiver(1,0);r.feed(frame(seq=1234))
    a=parse_bulk_ack(parse_reliable(r.drain_ack()).payload)
    assert a.entries[0].acknowledgement_id==1235

def test_host_station_not_assumed_zero():
    r=BroadcastReceiver(1,2);r.feed(frame());a=parse_bulk_ack(parse_reliable(r.drain_ack()).payload)
    assert [e.acknowledgement_id for e in a.entries]==[0xfff0,0xfff0,0xfff1]

def test_not_targeted_is_not_delivered_or_acked():
    r=BroadcastReceiver(1,0);assert not r.feed(frame(mask=1));assert r.drain_ack() is None

def test_duplicate_delivery_once_but_reack():
    r=BroadcastReceiver(1,0);r.feed(frame());r.drain_ack()
    assert not r.feed(frame());assert r.delivered==1;assert r.drain_ack()

def test_gap_selective_ack_and_ordered_delivery():
    r=BroadcastReceiver(1,0);r.feed(frame());r.drain_ack()
    assert not r.feed(frame(0xfff2,7))
    a=parse_bulk_ack(parse_reliable(r.drain_ack()).payload).entries[0]
    assert a.acknowledgement_id==0xfff1 and a.acknowledges(0xfff2) and not a.acknowledges(0xfff1)
    assert len(r.feed(frame(0xfff1,7)))==2
    assert parse_bulk_ack(parse_reliable(r.drain_ack()).payload).entries[0].acknowledgement_id==0xfff3

@pytest.mark.parametrize('body',[frame()[:-1],frame()+b'\0',bytes([255])+frame()[1:]])
def test_malformed_not_acked(body):
    r=BroadcastReceiver(1,0)
    with pytest.raises(ValueError):r.feed(body)
    assert r.drain_ack() is None

@pytest.mark.parametrize('indices',[(0,0),(-1,1),(1,23),(True,2),(None,1)])
def test_invalid_indices(indices):
    with pytest.raises(BroadcastError):BroadcastReceiver(*indices)

@pytest.mark.parametrize('flags',[32,64,31])
def test_unsupported_controls_do_not_ack(flags):
    r=BroadcastReceiver(1,0)
    with pytest.raises(ValueError):r.feed(frame(flags=flags))
    assert r.drain_ack() is None

def setup_client():
    s=ZASession(application_data=b'\x00\x5c\x16\x00\x06'+bytes(0x5c-5),our_mac=bytes.fromhex('020000000002'),host_mac=bytes.fromhex('020000000001'),our_ip='192.0.2.2',host_ip='192.0.2.1',our_var=12,player_id=b'x'*16)
    s.manager.host_var=11;s.manager.state='connected';s.station_index=1;s.update_sequence=1
    s.stations=(SessionStation(s.manager.host_mac.ljust(8,b'\0')+b'\x00\x0b',0,1),SessionStation(s.manager.our_mac+b'\x00\x0c',1,1))
    return ZATradeClient(s,ReliableTradeEngine())

def datagram(source=11,protocol=11,port=0,ps=253):
    p=frame();m=PiaMessage(31,32,len(p),protocol,port,ps,p)
    return DecodedDatagram(PiaHeader(src=source,dst=12),b'',b'',b'',b'',(m,),False)

def test_composed_dispatch_and_original_trade_state_separate():
    c=setup_client();c.feed_decoded(datagram(),now=1,tick=1)
    assert c.broadcast.delivered==1
    out=c.drain(now=1,tick=1);acks=[m for m in out if m.protocol==11]
    assert len(acks)==1 and acks[0].message_flags==64 and acks[0].destination==11
    assert c.trade.remote_offer is None and not c.trade.complete

@pytest.mark.parametrize('kw',[{'port':1},{'ps':252},{'protocol':12}])
def test_unselected_route_ignored(kw):
    c=setup_client();c.feed_decoded(datagram(**kw),now=1,tick=1);assert c.broadcast is None

def test_changed_host_rejected_by_existing_session_gate():
    c=setup_client()
    with pytest.raises(ValueError):c.feed_decoded(datagram(source=13),now=1,tick=1)
    assert c.broadcast is None

def test_no_installed_session_no_broadcast():
    c=setup_client();c.session.update_sequence=None;c.feed_decoded(datagram(),now=1,tick=1);assert c.broadcast is None
