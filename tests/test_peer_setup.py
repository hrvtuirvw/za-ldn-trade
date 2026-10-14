import pytest
from zasim.peer_setup import *
from zasim.trade_protocol import encode_struct,encode_varuint
from archive_shape import archive_shape

@pytest.mark.parametrize('command',[UpdateValue(0,b''),UpdateValue(USER_DATA_KEY,b'synthetic'),UpdateValue(0xffffffff,b'x'*4096),SyncDataSet(0),SyncDataSet(0xffffffff),RequestSync()])
def test_codec_roundtrip(command):assert decode(encode(command))==command

@pytest.mark.parametrize('payload',[b'',b'\x14\x00',b'\x15\x04\xb9\x00',b'\x14\x01\xb9\x01\0',encode(RequestSync())+b'\0',encode(UpdateValue(4,b'x'))[:-1],b'\x14\x03'+encode_struct(encode_varuint(2**32))])
def test_invalid_archive(payload):
    with pytest.raises(ValueError):decode(payload)

@pytest.mark.parametrize('value',[-1,2**32,True,None])
def test_invalid_scalar(value):
    with pytest.raises(ValueError):SyncDataSet(value)

def test_key_matches_source_name():assert fnv1a32(b'UserData')==USER_DATA_KEY

def test_dataset_requires_matching_hash_and_updates():
    d=PeerDataSet();assert not d.checksum_matches
    d.apply(UpdateValue(50,b'abc'));assert not d.checksum_matches
    d.apply(SyncDataSet(dataset_checksum({50:b'abc'})));assert d.checksum_matches
    d.apply(UpdateValue(50,b'xy'));assert not d.checksum_matches

def test_dataset_sorted_by_key_and_empty_zero():
    assert dataset_checksum({})==0
    assert dataset_checksum({50:b'abc',12:b'xy'})==dataset_checksum({12:b'xy',50:b'abc'})
    assert dataset_checksum({50:b'abc',12:b'xy'})!=dataset_checksum({12:b'abc',50:b'xy'})

def test_request_does_not_fabricate_records_or_sync():
    d=PeerDataSet();d.apply(RequestSync());assert not d.records and not d.checksum_matches

def test_schema_does_not_expose_blob_or_integer_values():
    s=archive_shape(encode_struct(encode_varuint(0x12345678),b'\xbc\x06secret'))
    assert s=='struct2(uint5,blob6)' and 'secret' not in s

def test_partial_schema_preserves_only_prior_type_shape():
    s=archive_shape(encode_struct(encode_varuint(9),b'\xbb\x00'))
    assert 'uint1' in s and 'shape_tag_187' in s
