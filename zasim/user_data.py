"""Z-A 2.0.2 endpoint20 UserData, source-bound to native serializer0xc38b18
and decoder0x913190. Plain object is exactly0x58 bytes, no pointers.

Producer0xc90b04: trainer ID, gender, language, fixed UTF16 trainer name,
optional32-byte profile text, three optional (u32 id,u32 state) entries.
The last entries' game-facing labels are not named here. An absent source
record natively returns id0 at0xc11174 and state-2/u32 at0xa9d4dc.
We generate our own clearly synthetic PC profile, never copy the peer's.
"""
from dataclasses import dataclass
import struct
from .trade_protocol import ArchiveReader, encode_struct, encode_varuint, encode_blob

class UserDataError(ValueError):
    pass

def _uint(v,bits):
    if type(v) is not int or not 0<=v<(1<<bits):raise UserDataError('userdata_integer_range')
    return v

@dataclass(frozen=True)
class UserData:
    trainer_id: int
    gender: int
    language: int
    name_utf16: bytes
    profile_text: bytes
    entries: tuple[tuple[int,int],tuple[int,int],tuple[int,int]]
    def __post_init__(self):
        _uint(self.trainer_id,32);_uint(self.gender,8);_uint(self.language,8)
        if type(self.name_utf16) is not bytes or len(self.name_utf16)!=26:raise UserDataError('userdata_name_size')
        if type(self.profile_text) is not bytes or len(self.profile_text)!=32:raise UserDataError('userdata_text_size')
        if type(self.entries) is not tuple or len(self.entries)!=3:raise UserDataError('userdata_entry_count')
        for item in self.entries:
            if type(item) is not tuple or len(item)!=2:raise UserDataError('userdata_entry_shape')
            _uint(item[0],32);_uint(item[1],32)
    def native_object(self):
        return struct.pack('<IBB26s32s6I',self.trainer_id,self.gender,self.language,self.name_utf16,self.profile_text,*(v for item in self.entries for v in item))
    def encode(self):
        return encode_struct(encode_varuint(self.trainer_id),encode_varuint(self.gender),encode_varuint(self.language),encode_blob(self.name_utf16),encode_blob(self.profile_text),b'\xba\x03'+b''.join(encode_struct(encode_varuint(a),encode_varuint(b)) for a,b in self.entries))
    @classmethod
    def decode(cls,data):
        ar=ArchiveReader(data);ar.read_struct(6)
        tid=_uint(ar.read_varuint(),32);gender=_uint(ar.read_varuint(),8);language=_uint(ar.read_varuint(),8)
        name=ar.read_blob(26);profile=ar.read_blob(32)
        if ar._byte('UserData array marker')!=0xba:raise UserDataError('userdata_array_marker')
        if ar.read_varuint()!=3:raise UserDataError('userdata_entry_count')
        entries=[]
        for _ in range(3):
            ar.read_struct(2);entries.append((_uint(ar.read_varuint(),32),_uint(ar.read_varuint(),32)))
        ar.finish();return cls(tid,gender,language,name,profile,tuple(entries))


def make_pc_user_data(name="ZA-Trade", *, trainer_id=None):
    """Create an independent PC session profile, never copy the console's.

    The native name field has twelve UTF-16 code units plus a terminator.
    A longer LDN display name is shortened on a code-point boundary. The
    random session identifier is not the original trainer of the offered PA9.
    """
    import secrets
    import unicodedata
    from zasim.ldn.ldn_name import validate_ldn_nickname

    validate_ldn_nickname(name)
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise UserDataError("PC display name cannot contain control or format characters")
    units = bytearray()
    for character in name:
        encoded = character.encode("utf-16-le")
        if len(units) + len(encoded) > 24:
            break
        units.extend(encoded)
    if trainer_id is None:
        trainer_id = secrets.randbits(32)
    return UserData(
        trainer_id, 0, 2, bytes(units).ljust(26, b"\0"), bytes(32),
        ((0, 0xfffffffe),) * 3,
    )
