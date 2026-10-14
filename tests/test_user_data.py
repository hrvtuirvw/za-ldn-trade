from dataclasses import replace
import pytest
from zasim.user_data import UserData
from lab_user_data import make_lab_user_data
from zasim.peer_setup import PeerSetup,UpdateValue,SyncDataSet,RequestSync,DeleteAllValues,USER_DATA_KEY,encode,decode,dataset_checksum
from zasim.broadcast import BroadcastSender
from zasim.reliable import ReliableFrame,parse_reliable,BulkAcknowledgement,BulkAckEntry
from zasim.client import ZATradeClient,ReliableTradeEngine
from zasim.pia import PiaHeader,PiaMessage,DecodedDatagram
from zasim.trade_protocol import Ready,CompatibilityMatrix,encode_frame
from test_broadcast import setup_client
from archive_shape import archive_shape

@pytest.mark.parametrize('tid',[0,127,128,255,256,65535,65536,0xffffffff])
def test_user_archive(tid):
    u=replace(make_lab_user_data(),trainer_id=tid)
    assert UserData.decode(u.encode())==u and len(u.native_object())==88

def test_lab_identity_is_explicit_and_absent_optional_records():
    u=make_lab_user_data();assert u.name_utf16.rstrip(b'\0') and u.profile_text==bytes(32)
    assert u.entries==((0,0xfffffffe),)*3 and len(u.encode())==97
    assert 'array3(struct2(' in archive_shape(u.encode())

@pytest.mark.parametrize('kw',[{'trainer_id':-1},{'trainer_id':True},{'gender':256},{'language':-1},{'name_utf16':bytes(24)},{'profile_text':bytes(31)},{'entries':((0,0),)*2}])
def test_user_bounds(kw):
    with pytest.raises(ValueError):replace(make_lab_user_data(),**kw)

@pytest.mark.parametrize('pos',[0,1,2,4,7,8,9,10,36,38,70,71,72,73,75,80,96])
def test_truncated_user(pos):
    with pytest.raises(ValueError):UserData.decode(make_lab_user_data().encode()[:pos])

def peer():return replace(make_lab_user_data(),trainer_id=123,name_utf16='GAME-Lab'.encode('utf-16-le').ljust(26,b'\0'))
def msg(proto,payload):
    m=PiaMessage(31,32,len(payload),proto,0,253,payload)
    return DecodedDatagram(PiaHeader(src=11,dst=12),b'',b'',b'',b'',(m,),False)
def frame(proto,seq,payload):
    return msg(proto,ReliableFrame(15 if seq==0xfff0 else 7,seq,0xfff0,payload,3 if proto==11 else 0,(2,) if proto==11 else ()).pack())
def client():return ZATradeClient(setup_client().session,ReliableTradeEngine(),peer_user_data=make_lab_user_data())
def initial(c,checksum=True):
    u=peer();body=encode(UpdateValue(USER_DATA_KEY,u.encode()))
    c.feed_decoded(frame(10,0xfff0,body),now=0,tick=0)
    c.feed_decoded(frame(11,0xfff0,body),now=0,tick=0)
    if checksum:c.feed_decoded(frame(11,0xfff1,encode(SyncDataSet(dataset_checksum({USER_DATA_KEY:u.encode()})))),now=0,tick=0)
    return c.drain(now=0,tick=0)
def applications(out):
    result=[]
    for m in out:
        if m.protocol not in (10,11):continue
        f=parse_reliable(m.payload)
        if f.is_application_data:result.append((m.protocol,f))
    return result

def test_composed_publication_separate_streams_and_no_premature_ready():
    c=client();out=applications(initial(c));assert len(out)==4
    assert all(f.payload[0]==20 for _,f in out)
    assert [(p,f.sequence_id) for p,f in out]==[(11,0xfff0),(11,0xfff1),(11,0xfff2),(10,0xfff0)]
    assert c.peer_setup.remote.checksum_matches and c.peer_setup.remote_user==peer()
    assert c.peer_setup.remote_user!=make_lab_user_data()
    assert not c.trade.title_enabled and not c.trade.negotiation.ready_sent and not c.trade.complete

def test_native_ready_releases_title_but_never_trade_completion():
    c=client();initial(c)
    native=encode_frame(Ready(0,CompatibilityMatrix(bytes(1200)))).wire_payload
    c.feed_decoded(frame(10,0xfff1,native),now=.1,tick=1)
    out=applications(c.drain(now=.1,tick=1));ready=[f for p,f in out if p==10 and f.payload[:2]==b'\x01\x00']
    assert len(ready)==1 and ready[0].sequence_id==0xfff1
    assert c.trade.title_enabled and c.trade.negotiation.remote_ready and not c.trade.complete

def test_cross_stream_reordering_holds_real_ready_until_checksum():
    c=client();initial(c,False)
    native=encode_frame(Ready(0,CompatibilityMatrix(bytes(1200)))).wire_payload
    c.feed_decoded(frame(10,0xfff1,native),now=.1,tick=1)
    assert not c.trade.title_enabled
    assert not any(f.payload[0]==1 for p,f in applications(c.drain(now=.1,tick=1)))
    checksum=dataset_checksum({USER_DATA_KEY:peer().encode()})
    c.feed_decoded(frame(11,0xfff1,encode(SyncDataSet(checksum))),now=.2,tick=2)
    assert any(f.payload[:2]==b'\x01\x00' for p,f in applications(c.drain(now=.2,tick=2)))

def test_bad_checksum_requests_sync_but_does_not_start_title():
    s=PeerSetup(make_lab_user_data());s.begin();s.receive(encode(UpdateValue(USER_DATA_KEY,peer().encode())))
    s.receive(encode(SyncDataSet(1)));assert not s.ready_for_title
    assert s.drain()==((10,encode(RequestSync())),)

def test_request_sync_sends_own_profile_not_peer():
    s=PeerSetup(make_lab_user_data());s.begin();s.receive(encode(UpdateValue(USER_DATA_KEY,peer().encode())))
    s.receive(encode(RequestSync()));out=s.drain()
    assert out[0]==(10,encode(UpdateValue(USER_DATA_KEY,make_lab_user_data().encode())))
    assert out[1][0]==11

def ack(entries,flags=0):return ReliableFrame(0,0xfff0,0xfff0,BulkAcknowledgement(flags,tuple(BulkAckEntry(v,bytes(16)) for v in entries)).pack()).pack()
def test_broadcast_ack_selects_our_station_only():
    s=BroadcastSender(1,0,3);s.queue(b'\x14\x04\xb9\0');sent=s.drain(0)
    assert len(sent)==1 and parse_reliable(sent[0]).bitmap_words==(1,)
    s.feed_ack(ack([0xfff1,0xfff0,0xfff0]));assert len(s.pending)==1
    s.feed_ack(ack([0xfff0,0xfff1,0xfff0]));assert not s.pending

def test_sender_retry_preserves_sequence_payload_and_bitmap():
    s=BroadcastSender(2,0,3);s.queue(b'\x14\x04\xb9\0');a=s.drain(0)
    assert not s.drain(.4) and s.drain(.5)==a and s.retransmissions==1

@pytest.mark.parametrize('payload',[ReliableFrame(32,0xfff0,0xfff0,parse_reliable(ack([0xfff1]*3)).payload).pack(),ack([0xfff1]*3)[:-1]])
def test_bad_ack_does_not_clear_window(payload):
    s=BroadcastSender(1,0,3);s.queue(b'\x14\x04\xb9\0');s.drain(0)
    with pytest.raises(ValueError):s.feed_ack(payload)
    assert len(s.pending)==1

def test_native_bitmap_routed_four_entry_ack_for_three_bit_destination():
    s=BroadcastSender(2,0,3);s.queue(b'\x14\x04\xb9\0');s.drain(0)
    body=parse_reliable(ack([0xfff0,0xfff0,0xfff1,0xfff0])).payload
    assert s.feed_ack(ReliableFrame(0,0xfff0,0xfff0,body,3,(4,)).pack())
    assert not s.pending

def test_ack_for_other_destination_or_short_array_does_not_apply():
    s=BroadcastSender(2,0,3);s.queue(b'\x14\x04\xb9\0');s.drain(0)
    body=parse_reliable(ack([0xfff1]*4)).payload
    assert not s.feed_ack(ReliableFrame(0,0xfff0,0xfff0,body,3,(2,)).pack())
    assert not s.feed_ack(ack([0xfff1]*2)) and len(s.pending)==1

def test_native_resync_hint_expedites_only_unacknowledged_frames():
    s=BroadcastSender(1,0,3);s.queue(b'\x14\x04\xb9\0');a=s.drain(0)
    s.feed_ack(ack([0xfff0]*4,1))
    assert s.drain(.1)==a and s.retransmissions==1

def test_ack_past_current_send_span_cannot_clear_window():
    s=BroadcastSender(2,0,3);s.queue(b'\x14\x04\xb9\0');s.drain(0)
    s.feed_ack(ack([0xfff0,0xfff0,0xfff2,0xfff0]))
    assert len(s.pending)==1 and s.acknowledged==0

def test_selective_hole_does_not_shrink_native_send_span():
    s=BroadcastSender(2,0,3)
    for _ in range(3):s.queue(b'\x14\x04\xb9\0')
    s.drain(0)
    entries=[BulkAckEntry(0xfff0,bytes(16)) for _ in range(4)]
    entries[2]=BulkAckEntry.from_received(0xfff0,[0xfff2])
    s.feed_ack(ReliableFrame(0,0xfff0,0xfff0,BulkAcknowledgement(0,tuple(entries)).pack()).pack())
    assert len(s.pending)==2
    s.feed_ack(ack([0xfff0,0xfff0,0xfff3,0xfff0]));assert not s.pending

def test_no_setup_or_title_before_session_install():
    c=client();c.session.update_sequence=None
    c.feed_decoded(frame(10,0xfff0,encode(UpdateValue(USER_DATA_KEY,peer().encode()))),now=0,tick=0)
    assert c.peer_setup.remote_user is None and not c.trade.title_enabled

def test_delete_all_source_bound_roundtrip():
    assert encode(DeleteAllValues())==b'\x14\x02\xb9\x00'
    assert decode(encode(DeleteAllValues()))==DeleteAllValues()
    with pytest.raises(ValueError):decode(b'\x14\x02\xb9\x01\0')

def test_delete_all_clears_remote_only_and_requires_fresh_sync():
    s=PeerSetup(make_lab_user_data());s.begin();s.receive(encode(UpdateValue(USER_DATA_KEY,peer().encode())))
    s.receive(encode(SyncDataSet(dataset_checksum(s.remote.records))));assert s.ready_for_title
    own=s.local_blob;s.receive(encode(DeleteAllValues()))
    assert not s.remote.records and s.remote_user is None and not s.ready_for_title
    assert s.local_blob==own and s.published

def test_cold_start_clear_update_sync_observed_sequence():
    c=client();u=peer();body=encode(UpdateValue(USER_DATA_KEY,u.encode()))
    c.feed_decoded(frame(10,0xfff0,body),now=0,tick=0)
    c.feed_decoded(frame(11,0xfff0,encode(DeleteAllValues())),now=0,tick=0)
    assert c.peer_setup.remote_user is None
    c.feed_decoded(frame(11,0xfff1,body),now=0,tick=0)
    c.feed_decoded(frame(11,0xfff2,encode(SyncDataSet(dataset_checksum({USER_DATA_KEY:u.encode()})))),now=0,tick=0)
    out=applications(c.drain(now=0,tick=0))
    assert len(out)==4 and c.peer_setup.ready_for_title and not c.trade.title_enabled
