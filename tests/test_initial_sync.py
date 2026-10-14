import pytest
from zasim.client import ReliableTradeEngine
from zasim.trade import TradeNegotiation,TradeState,IncomingResult
from zasim.trade_protocol import Ready,SelectPokemon,ConfirmTrade,CompatibilityMatrix,TradeFrame,encode_frame,decode_frame
from zasim.reliable import ReliableFrame,parse_reliable

MATRIX=CompatibilityMatrix(bytes(1200))
OFFER=bytes([31])*344
PEER=bytes([47])*344

def inbound(command,sequence=0xfff0):
    return ReliableFrame(15 if sequence==0xfff0 else 7,sequence,0xfff0,encode_frame(command).wire_payload).pack()

def commands(engine,now):
    result=[]
    for item in engine.drain(now=now):
        frame=parse_reliable(item.payload)
        if frame.is_application_data and frame.payload[:1]==b'\x01':
            result.append(decode_frame(TradeFrame.from_wire_payload(frame.payload)))
    return result

@pytest.mark.parametrize('confirmed',[False,True])
def test_native_initial_sync_discards_premature_committed_selection(confirmed):
    n=TradeNegotiation(MATRIX)
    assert n.receive(SelectPokemon(0,PEER,False),now=0)==IncomingResult.ACCEPTED
    if confirmed:n.receive(ConfirmTrade(0),now=0)
    n.receive(Ready(0,MATRIX),now=0)
    assert n.local_state==TradeState.INITIAL_SYNC
    n.advance(0)
    assert (n.local_state,n.remote_state)==(TradeState.SELECTING,TradeState.SELECTING)
    assert n.remote_offer is None and n.remote_committed_offer is None


def test_native_initial_sync_preserves_preview_without_promoting_it():
    n=TradeNegotiation(MATRIX)
    n.receive(SelectPokemon(0,PEER,True),now=0)
    n.receive(Ready(0,MATRIX),now=0);n.advance(0)
    assert n.remote_offer==PEER and n.remote_committed_offer is None
    assert n.local_state==n.remote_state==TradeState.SELECTING


def test_full_game_path_sends_preview_then_waits_for_source_proven_peer_selecting():
    e=ReliableTradeEngine(offer=OFFER,auto_confirm=True)
    e.hold_title_until_native_ready(lambda:True)
    e.feed_reliable(inbound(Ready(0,MATRIX)),now=0)
    out=commands(e,0)
    assert [type(c) for c in out]==[Ready,SelectPokemon]
    assert out[1].preview is True
    assert e.negotiation.local_state==TradeState.SELECTING
    assert commands(e,.1)==[]
    # Native Select sender requires state2, even for an empty-cursor preview.
    e.feed_reliable(inbound(SelectPokemon(0,bytes(344),True),0xfff1),now=.2)
    out=commands(e,.2)
    assert len(out)==1 and isinstance(out[0],SelectPokemon) and not out[0].preview
    assert e.negotiation.local_state==TradeState.OFFERED
    assert e.remote_offer is None  # preview is never a committed approval input
    e.feed_reliable(inbound(SelectPokemon(0,PEER,False),0xfff2),now=.3)
    out=commands(e,.3)
    assert len(out)==1 and isinstance(out[0],ConfirmTrade)
    assert e.remote_offer==PEER


def test_legacy_explicit_mode_unchanged():
    e=ReliableTradeEngine(offer=OFFER)
    e.drain(now=0)
    e.feed_reliable(inbound(Ready(0,MATRIX)),now=.1)
    out=commands(e,.1)
    assert len(out)==1 and isinstance(out[0],SelectPokemon) and not out[0].preview


def test_two_preview_first_peers_do_not_deadlock_or_confirm_preview():
    a=ReliableTradeEngine(offer=OFFER,auto_confirm=True)
    b=ReliableTradeEngine(offer=PEER,auto_confirm=True)
    # The full-game gate is separately verified by UserData integration tests.
    # Inject valid observed Ready frames to release these pure transport engines.
    for e in (a,b):e.hold_title_until_native_ready(lambda:True)
    for e in (a,b):e.feed_reliable(inbound(Ready(0,MATRIX)),now=0)
    initial=[commands(e,0) for e in (a,b)]
    assert all(len(v)==2 and v[1].preview for v in initial)
    for e,peer in ((a,initial[1][1]),(b,initial[0][1])):
        e.feed_reliable(inbound(peer,0xfff1),now=.1)
        assert e.remote_offer is None
    commits=[commands(e,.1) for e in (a,b)]
    assert all(len(v)==1 and isinstance(v[0],SelectPokemon) and not v[0].preview for v in commits)
    for e,peer in ((a,commits[1][0]),(b,commits[0][0])):
        e.feed_reliable(inbound(peer,0xfff2),now=.2)
    confirms=[commands(e,.2) for e in (a,b)]
    assert all(len(v)==1 and isinstance(v[0],ConfirmTrade) for v in confirms)
    for e,peer in ((a,confirms[1][0]),(b,confirms[0][0])):
        e.feed_reliable(inbound(peer,0xfff3),now=.3)
    finals=[commands(e,1.8) for e in (a,b)]
    # Explicitly pick FinalAgreement; unACKed synthetic streams may also retry.
    from zasim.trade_protocol import FinalAgreement
    fs=[next(c for c in group if isinstance(c,FinalAgreement)) for group in finals]
    for e,peer in ((a,fs[1]),(b,fs[0])):e.feed_reliable(inbound(peer,0xfff4),now=1.9)
    assert a.complete and b.complete
