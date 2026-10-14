"""Local state/codec tests; not a full-game success claim."""
import os
import pytest
from zasim.sync_save import Command, Message, SyncSavePeer, SyncSaveError, encode, decode, PHASES
from zasim.client import ReliableTradeEngine, ClientProtocolError, run_live_trade
from zasim.trade import TradeState
from lab_user_data import make_lab_user_data
from zasim.reliable import ReliableFrame, ReliableFlags

@pytest.mark.parametrize('command', list(Command))
@pytest.mark.parametrize('phase', PHASES)
def test_codec(command,phase):
    value=Message(command,phase)
    assert encode(value)==bytes((2,command,0xb9,1,phase))
    assert decode(encode(value))==value

@pytest.mark.parametrize('wire', [b'',b'\x02',b'\x03\x01\xb9\x01\x03',b'\x02\x02\xb9\x01\x03',b'\x02\x01\xb9\x02\x03',b'\x02\x01\xba\x01\x03',b'\x02\x01\xb9\x01\x00',b'\x02\x01\xb9\x01\x10',b'\x02\x01\xb9\x01\x03\x00'])
def test_exact_rejection(wire):
    with pytest.raises(SyncSaveError):decode(wire)

def peer(operations=None,fail=None):
    operations=operations if operations is not None else []
    def op(name):
        def call():
            operations.append(name)
            return name!=fail
        return call
    return SyncSavePeer(**{name:op(name) for name in ('prepare','persist','commit','finish')})

def ack(p,phase,now):p.receive(encode(Message(Command.SYNCED,phase)),now=now)
def sent(p,now):return [decode(x).phase for x in p.drain(now=now)]

def test_real_durability_callbacks_and_all_native_barriers(tmp_path):
    staged=tmp_path/'pending';saved=tmp_path/'received';payload=b'synthetic-PC-receipt'
    with staged.open('xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
    calls=[]
    def prepare():calls.append('prepare');return staged.read_bytes()==payload
    def persist():
        calls.append('persist');os.replace(staged,saved)
        # Exercise the real platform adapter, not a POSIX-only test clone.
        # File fsync above and every protocol barrier remain mandatory.
        from zasim.file_save import _sync_output_directory
        _sync_output_directory(tmp_path)
        return saved.read_bytes()==payload
    def commit():calls.append('commit');return saved.read_bytes()==payload
    def finish():calls.append('finish');return saved.read_bytes()==payload
    p=SyncSavePeer(prepare=prepare,persist=persist,commit=commit,finish=finish)
    assert not p.complete and sent(p,0)==[]
    p.begin(now=0);assert sent(p,0)==[3] and not saved.exists()
    ack(p,3,1);assert sent(p,1)==[6] and calls==['prepare']
    ack(p,6,2);assert saved.read_bytes()==payload and sent(p,2)==[]
    assert sent(p,2.06)==[] and sent(p,2.07)==[11]
    ack(p,11,3);assert sent(p,3)==[14] and not p.complete
    ack(p,14,4);assert p.complete and calls==['prepare','persist','commit','finish']
    for phase in PHASES:ack(p,phase,5)
    p.begin(now=6);assert sent(p,6)==[] and len(calls)==4

@pytest.mark.parametrize('phase',[6,11,14])
def test_no_future_ack(phase):
    p=peer();p.begin(now=0);sent(p,0)
    with pytest.raises(SyncSaveError):ack(p,phase,.5)
    assert not p.complete and p.operations==['prepare'] and sent(p,.5)==[]

def test_wrong_role_and_before_begin():
    p=peer()
    with pytest.raises(SyncSaveError):ack(p,3,0)
    p.begin(now=0);sent(p,0)
    with pytest.raises(SyncSaveError):p.receive(encode(Message(Command.STATE,3)),now=1)
    assert not p.complete

@pytest.mark.parametrize('fail',['prepare','persist','commit','finish'])
def test_disk_failures_never_announce_later_phase(fail):
    p=peer(fail=fail)
    with pytest.raises(SyncSaveError):
        p.begin(now=0);sent(p,0);ack(p,3,1);sent(p,1);ack(p,6,2);sent(p,2.1);ack(p,11,3);sent(p,3);ack(p,14,4)
    assert p.failed and not p.complete
    with pytest.raises(SyncSaveError):sent(p,5)

def test_stop_never_starts_or_advances_save_operation():
    calls=[];p=peer(calls);p.suspend();p.begin(now=0);assert calls==[] and sent(p,0)==[]
    p=peer(calls);p.begin(now=0);sent(p,0);ack(p,3,1);p.suspend();ack(p,6,2)
    assert calls==['prepare'] and sent(p,2)==[] and not p.complete

def test_duplicate_synced_does_not_repeat_disk_io():
    calls=[];p=peer(calls);p.begin(now=0);sent(p,0);ack(p,3,1);sent(p,1);ack(p,6,2)
    ack(p,6,2.01);assert calls==['prepare','persist'];assert sent(p,2.1)==[11]

@pytest.mark.parametrize('now',[float('nan'),float('inf'),-1,True])
def test_invalid_clock(now):
    with pytest.raises(SyncSaveError):peer().begin(now=now)

def test_sync_send_requires_both_final_and_shares_real_reliable_sequence():
    t=ReliableTradeEngine()
    wire=encode(Message(Command.STATE,3))
    with pytest.raises(ClientProtocolError):t.queue_sync_save(wire,now=0)
    t.negotiation.local_state=t.negotiation.remote_state=TradeState.FINAL_AGREEMENT
    n=t.send_next;t.queue_sync_save(wire,now=1)
    assert t.send_next==(n+1)&0xffff and t.unacknowledged==(n,)
    emitted=t.drain(now=1);frame=__import__('zasim.reliable',fromlist=['parse_reliable']).parse_reliable(emitted[0].payload)
    assert frame.payload==wire
    with pytest.raises(ClientProtocolError):t.queue_sync_save(encode(Message(Command.SYNCED,3)),now=1)

def test_endpoint2_unicast_dispatch_uses_shared_receive_window():
    t=ReliableTradeEngine();seen=[];t.other_application_handler=seen.append
    wire=encode(Message(Command.SYNCED,3));flags=ReliableFlags.APPLICATION_DATA|ReliableFlags.MESSAGE_START|ReliableFlags.MESSAGE_END|ReliableFlags.INITIALIZED
    t.feed_reliable(ReliableFrame(flags,0xfff0,0xfff0,wire).pack(),now=0)
    assert seen==[wire] and t._receive.next_sequence==0xfff1

@pytest.mark.parametrize('kwargs',[{'sync_save':1},{'sync_save':True},{'sync_save':True,'peer_user_data':make_lab_user_data()}])
def test_sync_activation_requires_real_output_and_native_profile(kwargs):
    with pytest.raises((TypeError,ValueError)):
        run_live_trade(b'',auto_confirm=True,**kwargs)

def test_composed_broadcast_barriers_and_actual_final_send_guard():
    from test_broadcast import setup_client
    from test_user_data import frame,applications
    from zasim.client import ZATradeClient
    base=setup_client();operations=[];p=peer(operations)
    c=ZATradeClient(base.session,base.trade,sync_save_peer=p)
    c.trade.negotiation.local_state=c.trade.negotiation.remote_state=TradeState.FINAL_AGREEMENT
    c.sync_start_guard=lambda:False
    assert not applications(c.drain(now=0,tick=0)) and not p.started
    c.sync_start_guard=lambda:True
    first=applications(c.drain(now=.1,tick=1))
    assert [(proto,decode(f.payload).phase) for proto,f in first]==[(10,3)]
    c.feed_decoded(frame(11,0xfff0,encode(Message(Command.SYNCED,3))),now=.2,tick=2)
    second=applications(c.drain(now=.2,tick=2))
    assert [(proto,decode(f.payload).phase) for proto,f in second]==[(10,6)]
    c.feed_decoded(frame(11,0xfff1,encode(Message(Command.SYNCED,6))),now=.3,tick=3)
    assert operations==['prepare','persist']
    third=applications(c.drain(now=.4,tick=4))
    assert [(proto,decode(f.payload).phase) for proto,f in third]==[(10,11)]
    c.feed_decoded(frame(11,0xfff2,encode(Message(Command.SYNCED,11))),now=.45,tick=5)
    fourth=applications(c.drain(now=.45,tick=5))
    assert [(proto,decode(f.payload).phase) for proto,f in fourth]==[(10,14)]
    c.feed_decoded(frame(11,0xfff3,encode(Message(Command.SYNCED,14))),now=.5,tick=6)
    assert p.complete and operations==['prepare','persist','commit','finish']
    assert c.broadcast.delivered==4
    # All four commands share and advance the normal Reliable10 send window.
    assert [f.sequence_id for _,f in first+second+third+fourth]==[0xfff0,0xfff1,0xfff2,0xfff3]


def test_foreign_broadcast_cannot_advance_save():
    from dataclasses import replace
    from test_broadcast import setup_client
    from test_user_data import frame
    from zasim.client import ZATradeClient
    base=setup_client();p=peer();c=ZATradeClient(base.session,base.trade,sync_save_peer=p)
    p.begin(now=0);sent(p,0)
    incoming=frame(11,0xfff0,encode(Message(Command.SYNCED,3)))
    incoming=replace(incoming,header=replace(incoming.header,src=99))
    with pytest.raises(ValueError,match='host variable id changed'):
        c.feed_decoded(incoming,now=.1,tick=1)
    assert not p.acknowledged and p.phase==3 and c.broadcast is None

@pytest.mark.parametrize('cleanup_type', [None, OSError, KeyboardInterrupt, SystemExit])
def test_live_adapter_sync_accepts_already_published_durable_receipt(monkeypatch,tmp_path,cleanup_type):
    """Actual run_live_trade and disk I/O; network/peer are MODELS, not game proof."""
    import test_za_client as fixtures
    import zasim.client as live
    from zasim.trade_protocol import FinalAgreement
    events=[]
    def stop():
        if cleanup_type is not None:
            raise cleanup_type('synthetic cleanup failure after save sync')
    fixtures._install_live_boundary_fakes(monkeypatch,events=events,
        outbound_factory=lambda:[fixtures._synthetic_trade_outbound(FinalAgreement(0))],
        send_callback=lambda:None,stop_callback=stop,trade_complete=True)
    base=live.ZATradeClient
    target=tmp_path/'received.epa9'
    phases=[]
    class ModelClient(base):
        def __init__(self,session,trade,**kw):super().__init__(session,trade)
        def drain(self,*,now,tick):
            out=super().drain(now=now,tick=tick)
            if self.emitted and self.sync_start_guard():
                assert target.is_file() and not list(tmp_path.glob('.*.pending-*'))
                self.sync_save_peer.begin(now=now)
                for wire in self.sync_save_peer.drain(now=now):
                    phase=decode(wire).phase;phases.append(phase)
                    self.sync_save_peer.receive(encode(Message(Command.SYNCED,phase)),now=now)
            return out
    monkeypatch.setattr(live,'ZATradeClient',ModelClient)
    clock=[0.0]
    monkeypatch.setattr(live.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(live.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+max(seconds,.01)))
    result=live.run_live_trade(fixtures._occupied_party(),output=target,auto_confirm=True,
        transport_factory=live.LiveTransport,peer_user_data=make_lab_user_data(),sync_save=True,
        settle_grace=0,timeout=2,log=lambda *a,**k:None)
    assert result.completed is (cleanup_type is None)
    assert result.native_save_sync_complete is True
    assert result.output_path == target
    if cleanup_type is None:
        assert result.warning is None
    else:
        assert 'cleanup failed' in result.warning and 'do not retry' in result.warning
    assert events==['start',('send',b'wire','169.254.1.1'),'stop']
    assert phases==list(PHASES)
    assert target.read_bytes()==fixtures._occupied_party(133).to_bytes(encrypted=True)

def test_transport_ack_is_not_application_synced_and_state_reannounces():
    p=peer();p.begin(now=0);assert sent(p,0)==[3]
    assert sent(p,.999)==[] and sent(p,1)==[3] and sent(p,1.999)==[]
    assert sent(p,2)==[3]
    ack(p,3,2.1);assert sent(p,2.1)==[6]
    assert sent(p,3.1)==[6] and p.operations==['prepare']
    ack(p,6,3.2);assert sent(p,3.2)==[] and sent(p,3.3)==[11]
    assert sent(p,4.3)==[11] and p.operations==['prepare','persist']
    p.suspend();assert sent(p,6)==[]
