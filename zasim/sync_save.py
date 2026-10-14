"""Z-A 2.0.2 SyncSave, separate from BoxTrade's FinalAgreement.

Original binding: ca20cc supplies endpoints1/2; ca2794 copies both bytes;
966218/9662a4 register CommandState(0), CommandSynced(1), in that order.
Codecs963554/2dc5850: struct1(uint8). Original master gate9648fc requires
all participating states to equal its own. 960c20 waits at3,6,11,14;
only the real task-completion callback dfda8c moves BoxTrade to state6.

This PC is the non-master. Its phase announcements require actual local
persistence callbacks. No game memory, predicates, or save data are patched.
"""
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
import math
from typing import Callable

ENDPOINT = 2
PHASES = (3, 6, 11, 14)

class SyncSaveError(ValueError):
    pass

class Command(IntEnum):
    STATE = 0
    SYNCED = 1

@dataclass(frozen=True)
class Message:
    command: Command
    phase: int

    def __post_init__(self):
        if not isinstance(self.command, Command):
            raise SyncSaveError('invalid_sync_command')
        if type(self.phase) is not int or self.phase not in PHASES:
            raise SyncSaveError('unsupported_sync_phase')

def encode(message: Message) -> bytes:
    if not isinstance(message, Message):
        raise SyncSaveError('invalid_sync_message')
    # These four enum values are canonical one-byte unsigned archive scalars.
    return bytes((ENDPOINT, message.command, 0xb9, 1, message.phase))

def decode(payload: bytes) -> Message:
    raw = bytes(payload)
    if len(raw) != 5 or raw[0] != ENDPOINT or raw[2:4] != b'\xb9\x01':
        raise SyncSaveError('invalid_sync_archive_or_endpoint')
    try:
        command = Command(raw[1])
    except ValueError:
        raise SyncSaveError('unknown_sync_command') from None
    return Message(command, raw[4])

class SyncSavePeer:
    """Single exchange, selected authenticated master only, fail-closed phases.

    prepare checks the already-fsynced write-ahead PA9. persist atomically
    installs/fsyncs the approved PC receipt; commit verifies durable bytes;
    finish verifies once more after the master's final synchronized phase.
    Callbacks must return True only after performing/checking their operation.
    A thrown error or False permanently prevents later announcements.
    """
    def __init__(self, *, prepare: Callable[[], bool], persist: Callable[[], bool],
                 commit: Callable[[], bool], finish: Callable[[], bool]):
        if not all(callable(f) for f in (prepare,persist,commit,finish)):
            raise TypeError('all persistence operations must be callable')
        self._prepare, self._persist, self._commit, self._finish = prepare,persist,commit,finish
        self.started = False
        self.complete = False
        self.failed = False
        self.phase = None
        self.acknowledged = []
        self.operations = []
        self._outgoing = deque()
        self._saved_announce_at = None
        self._last_now = 0.0
        self.suspended = False
        self._reannounce_at = None

    def _clock(self, now):
        if not isinstance(now,(int,float)) or isinstance(now,bool) or not math.isfinite(now) or now < self._last_now:
            raise SyncSaveError('invalid_sync_clock')
        self._last_now = float(now)
        return self._last_now

    def _operation(self, name, operation):
        try:
            if operation() is not True:
                raise SyncSaveError('sync_'+name+'_not_durable')
        except BaseException:
            self.failed = True
            self._outgoing.clear()
            raise
        self.operations.append(name)

    def _announce(self, phase):
        self.phase = phase
        self._reannounce_at = None
        self._outgoing.append(encode(Message(Command.STATE,phase)))

    def suspend(self):
        self.suspended = True
        self._outgoing.clear()
        self._saved_announce_at = None
        self._reannounce_at = None

    def begin(self, *, now: float):
        self._clock(now)
        if self.suspended:
            return
        if self.failed:
            raise SyncSaveError('sync_failed')
        if self.started:
            return
        self._operation('prepare',self._prepare)
        self.started = True
        self._announce(3)

    def receive(self, payload: bytes, *, now: float):
        now = self._clock(now)
        msg = decode(payload)
        if self.suspended:
            return
        if self.failed or not self.started:
            raise SyncSaveError('sync_before_preparation_or_after_failure')
        if msg.command is not Command.SYNCED:
            raise SyncSaveError('sync_wrong_peer_role')
        if msg.phase in self.acknowledged:
            return  # duplicate of this same exchange, never repeat disk work
        if msg.phase != self.phase or self._saved_announce_at is not None:
            raise SyncSaveError('sync_future_or_out_of_order')
        self.acknowledged.append(msg.phase)
        if msg.phase == 3:
            self._announce(6)
        elif msg.phase == 6:
            self._operation('persist',self._persist)
            # Original non-master path waits 2..302 frame ticks after local
            # storage completes (9656d8,960c40). Use the lower bound at30Hz;
            # this is not a substitute for any remote Synced or local I/O gate.
            self._saved_announce_at = now + 2/30
        elif msg.phase == 11:
            self._operation('commit',self._commit)
            self._announce(14)
        else:
            self._operation('finish',self._finish)
            self.complete = True

    def drain(self, *, now: float):
        now = self._clock(now)
        if self.suspended:
            return ()
        if self.failed:
            raise SyncSaveError('sync_failed')
        if self._saved_announce_at is not None and now >= self._saved_announce_at:
            self._saved_announce_at = None
            self._announce(11)
        # Native non-master 95f768..95f7c0 reannounces its current state
        # every30 ticks until matching Synced, independently of Reliable ACK.
        # A transport ACK can precede creation of the remote endpoint handler.
        awaiting = (self.started and not self.complete and
                    self.phase not in self.acknowledged and
                    self._saved_announce_at is None)
        if awaiting and not self._outgoing and self._reannounce_at is not None and now >= self._reannounce_at:
            self._outgoing.append(encode(Message(Command.STATE,self.phase)))
        outgoing = tuple(self._outgoing)
        self._outgoing.clear()
        if outgoing and awaiting:
            self._reannounce_at = now + 1.0
        return outgoing
