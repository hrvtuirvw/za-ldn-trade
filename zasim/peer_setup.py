"""Strict codecs for the recovered GFA endpoint-20 peer data service.

Original command registration 0x779768..0x77978c: UpdateValue=0,
DeleteValue=1, DeleteAllValues=2, SyncDataSet=3, RequestSync=4.
0/2/3/4 are recovered here; command1 remains fail-closed.
UserData is decoded separately using its source-bound six-field schema.
"""
from dataclasses import dataclass
from collections import deque
import zlib
from .trade_protocol import ArchiveReader, encode_struct, encode_varuint, encode_blob
from .user_data import UserData

ENDPOINT=20
USER_DATA_KEY=0x2abe85e2  # FNV1a32('UserData'); original consumer 0xdf1b30.

class SetupError(ValueError):
    pass

def u32(value):
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise SetupError('setup_u32_required')
    return value

@dataclass(frozen=True)
class UpdateValue:
    key: int
    value: bytes
    def __post_init__(self):
        u32(self.key)
        if not isinstance(self.value, bytes) or len(self.value)>4096:
            raise SetupError('setup_blob_bounds')

@dataclass(frozen=True)
class SyncDataSet:
    checksum: int
    def __post_init__(self):u32(self.checksum)

@dataclass(frozen=True)
class RequestSync:
    pass

@dataclass(frozen=True)
class DeleteAllValues:
    """Native0x2dc33f4 struct0; callback0x2dc2498 selects the source peer."""
    pass

def encode(command):
    if isinstance(command,UpdateValue):
        return bytes((ENDPOINT,0))+encode_struct(encode_varuint(command.key),encode_struct(encode_blob(command.value)))
    if isinstance(command,SyncDataSet):
        return bytes((ENDPOINT,3))+encode_struct(encode_varuint(command.checksum))
    if isinstance(command,RequestSync):
        return bytes((ENDPOINT,4))+encode_struct()
    if isinstance(command,DeleteAllValues):
        return bytes((ENDPOINT,2))+encode_struct()
    raise SetupError('setup_command_not_implemented')

def decode(payload):
    if not isinstance(payload,bytes) or len(payload)<4 or len(payload)>4128 or payload[0]!=ENDPOINT:
        raise SetupError('setup_payload_bounds_or_endpoint')
    ar=ArchiveReader(payload[2:])
    if payload[1]==0:
        ar.read_struct(2);key=u32(ar.read_varuint());ar.read_struct(1)
        result=UpdateValue(key,ar.read_blob())
    elif payload[1]==3:
        ar.read_struct(1);result=SyncDataSet(u32(ar.read_varuint()))
    elif payload[1]==4:
        ar.read_struct(0);result=RequestSync()
    elif payload[1]==2:
        ar.read_struct(0);result=DeleteAllValues()
    else:raise SetupError('setup_command_not_implemented')
    ar.finish();return result

def fnv1a32(data):
    result=0x811c9dc5
    for value in data:result=((result^value)*0x1000193)&0xffffffff
    return result

def dataset_checksum(records):
    # Original 0xc4b2f0 sorts by u32 key, folds each value using FNV1a,
    # then ordinary reflected CRC32 of LE32(previous_crc + fnv(value)).
    # Native empty dataset returns zero. Keys order but are not hashed.
    result=0
    if len(records)>32:raise SetupError('setup_record_count')
    for key in sorted(records):
        u32(key)
        result=zlib.crc32(((result+fnv1a32(records[key]))&0xffffffff).to_bytes(4,'little'))
    return result

class PeerDataSet:
    def __init__(self):
        self.records={};self.advertised_checksum=None
    def apply(self, command):
        if isinstance(command,UpdateValue):
            if command.key not in self.records and len(self.records)>=32:raise SetupError('setup_record_count')
            self.records[command.key]=command.value
        elif isinstance(command,SyncDataSet):self.advertised_checksum=command.checksum
        elif isinstance(command,DeleteAllValues):
            self.records.clear();self.advertised_checksum=None
        elif not isinstance(command,RequestSync):raise SetupError('setup_command_not_implemented')
    @property
    def checksum_matches(self):
        return self.advertised_checksum is not None and self.advertised_checksum==dataset_checksum(self.records)


class PeerSetup:
    """Selected-peer data service only. Does not manufacture title Ready.

    Native join callback0xc1e3c4 queues per-peer UpdateValue via0xc1e58c.
    Local data publication also broadcasts UpdateValue;0x2c91e8 broadcasts
    its actual checksum. Native request callback0x2dc2500 republishes data.
    """
    def __init__(self,local_user):
        if not isinstance(local_user,UserData):raise TypeError('local UserData required')
        self.local_user=local_user;self.local_blob=local_user.encode()
        self.local_checksum=dataset_checksum({USER_DATA_KEY:self.local_blob})
        self.remote=PeerDataSet();self.remote_user=None;self.published=False
        self.responses=deque();self.requested=False;self.response_count=0

    def publication(self,initial=False):
        update=encode(UpdateValue(USER_DATA_KEY,self.local_blob))
        result=[(10,update)]
        if initial:
            result.append((11,encode(DeleteAllValues())))
            result.append((11,update))
        result.append((11,encode(SyncDataSet(self.local_checksum))))
        return tuple(result)

    def begin(self):
        if self.published:return ()
        self.published=True
        return self.publication(initial=True)

    def receive(self,payload):
        command=decode(payload)
        if isinstance(command,UpdateValue) and command.key==USER_DATA_KEY:
            self.remote_user=UserData.decode(command.value)
        elif isinstance(command,DeleteAllValues):
            self.remote_user=None;self.requested=False
        self.remote.apply(command)
        if isinstance(command,RequestSync):
            if self.response_count>=8:raise SetupError('setup_request_budget')
            self.responses.extend(self.publication());self.response_count+=1
        if isinstance(command,SyncDataSet) and not self.remote.checksum_matches and not self.requested:
            self.responses.append((10,encode(RequestSync())));self.requested=True

    def drain(self):
        out=tuple(self.responses);self.responses.clear();return out

    @property
    def ready_for_title(self):
        return self.published and self.remote_user is not None and self.remote.checksum_matches
