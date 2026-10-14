"""Receive side of Z-A 2.0.2 BroadcastReliable (protocol 11/version 4).

Independent sequencing from protocol 10. Shared original MessageHeader parser:
0x25704d4, Broadcast transport RX 0x25716d0, destination gate 0x257198c,
ACK station-array writer 0x2564f00, serializer 0x2573ec0. This does not claim
that acknowledging transport data implements the application's setup service.
"""
from collections import deque, OrderedDict
import math
from .reliable import (ReliableFrame, ReliableFlags, ReliableReassembler,
                       BulkAckEntry, BulkAcknowledgement, parse_reliable,
                       parse_bulk_ack)

class BroadcastError(ValueError):
    pass

class BroadcastReceiver:
    def __init__(self, local_index: int, host_index: int):
        if not all(type(i) is int and 0 <= i < 23 for i in (local_index, host_index)):
            raise BroadcastError('station_index_out_of_range')
        if local_index == host_index:
            raise BroadcastError('duplicate_station_index')
        self.local_index = local_index
        self.host_index = host_index
        self.capacity = max(local_index, host_index) + 1
        self.receive = ReliableReassembler(next_sequence=0xfff0)
        self.initialized = False
        self.ack_owed = False
        self.applications = deque(maxlen=128)
        self.delivered = 0
        self.acks = 0

    def feed(self, payload: bytes):
        frame = parse_reliable(payload)
        if frame.flags & (ReliableFlags.RESET | ReliableFlags.RESET_ACK):
            raise BroadcastError('reset_not_implemented')
        if frame.destination_bit_count:
            if frame.destination_bit_count <= max(self.local_index, self.host_index):
                raise BroadcastError('destination_capacity_missing_member')
            if not frame.bitmap_words[0] & (1 << self.local_index):
                return ()
            self.capacity = frame.destination_bit_count
        if not frame.is_application_data:
            # No local broadcasts have been sent by this receive-only layer.
            # Parse rather than interpreting another station's ACK as ours.
            if frame.payload:
                parse_bulk_ack(frame.payload)
            return ()
        if frame.flags & ReliableFlags.COMPRESSED:
            raise BroadcastError('compressed_application_not_implemented')
        # Native 0x2571bfc..0x2571c0c: an uninitialized receive window
        # ignores non-initial frames. The first INITIALIZED frame establishes
        # its actual sequence, rather than assuming 0xfff0 after a late join.
        if not self.initialized:
            if not frame.flags & ReliableFlags.INITIALIZED:
                return ()
            self.receive.reset(frame.sequence_id)
        # A complete strict parse and in-window receipt precede the ACK.
        messages = self.receive.feed(frame)
        self.initialized = True
        self.ack_owed = True
        for message in messages:
            self.applications.append(message)
            self.delivered += 1
        return messages

    def drain_ack(self):
        if not self.ack_owed:
            return None
        # Original broadcast ACK index is the *sender station index*, not the
        # local station, stream number, or protocol-specific byte.
        entries = [BulkAckEntry(0xfff0, bytes(16)) for _ in range(self.capacity)]
        entries[self.host_index] = BulkAckEntry.from_received(
            self.receive.next_sequence, self.receive.pending_sequences)
        result = ReliableFrame(0, 0xfff0, 0xfff0,
                               BulkAcknowledgement(0, tuple(entries)).pack()).pack()
        self.ack_owed = False
        self.acks += 1
        return result


class BroadcastSender:
    """Bounded selected-host TX window, separate from protocol10.

    Destination bit is the native host station index. Host bulk ACK entry at
    OUR station index acknowledges our stream, not entries for other senders.
    Native0x2565014 first applies the destination bitmap, then requires our
    station index to be below the parsed entry count (not equal to bitmap
    width). Resync bit0 requests an expedited retry, never a sequence reset.
    """
    def __init__(self, local_index, host_index, capacity):
        BroadcastReceiver(local_index, host_index)
        if type(capacity) is not int or not max(local_index,host_index)<capacity<24:
            raise BroadcastError('sender_capacity_out_of_range')
        self.local_index=local_index;self.host_index=host_index;self.capacity=capacity
        self.next_sequence=0xfff0;self.pending=OrderedDict();self.sent_any=False
        self.acknowledged=0;self.retransmissions=0

    def queue(self,payload):
        if type(payload) is not bytes or not 2<=len(payload)<=1400:
            raise BroadcastError('sender_payload_bounds')
        if len(self.pending)>=32:raise BroadcastError('sender_window_full')
        seq=self.next_sequence;self.next_sequence=(seq+1)&0xffff
        flags=15 if not self.sent_any else 7;self.sent_any=True
        self.pending[seq]=[payload,flags,None,0]

    def feed_ack(self,payload):
        f=parse_reliable(payload)
        if f.flags:raise BroadcastError('sender_ack_frame_shape')
        if f.destination_bit_count and not f.bitmap_words[0] & (1<<self.local_index):
            return False
        ack=parse_bulk_ack(f.payload)
        if self.local_index>=len(ack.entries):return False
        selected=ack.entries[self.local_index]
        if self.pending:
            lowest=next(iter(self.pending))
            distance=((selected.acknowledgement_id-lowest+0x8000)&0xffff)-0x8000
            span=(self.next_sequence-lowest)&0xffff
            if ack.ack_flags & 1 or distance<0:
                for item in self.pending.values():item[2]=None
            # Native0x2574570..0x2574588 uses the original send span.
            # Selective ACK holes must not reduce that sequence-space bound.
            if distance<0 or distance>span:return True
        for sequence in tuple(self.pending):
            if selected.acknowledges(sequence):
                del self.pending[sequence];self.acknowledged+=1
        return True

    def drain(self,now):
        if type(now) not in (float,int) or not math.isfinite(now) or now<0:
            raise BroadcastError('sender_invalid_clock')
        out=[]
        for sequence,item in self.pending.items():
            body,flags,sent,attempts=item
            if sent is not None and now-sent<0.5:continue
            if attempts>=8:raise BroadcastError('sender_ack_timeout')
            if attempts:self.retransmissions+=1
            out.append(ReliableFrame(flags,sequence,next(iter(self.pending)),body,
                                     self.capacity,(1<<self.host_index,)).pack())
            item[2]=now;item[3]+=1
        return tuple(out)
