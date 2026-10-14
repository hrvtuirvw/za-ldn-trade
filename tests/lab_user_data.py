"""Fixed synthetic profile for public tests; never a product identity provider."""
from zasim.user_data import UserData


def make_lab_user_data():
    # No linked profile text; each optional display record is absent as in the
    # original null-record getters. Not a successful/synced/approved flag.
    return UserData(0x50434c42,0,2,'ZA-PC-Lab'.encode('utf-16-le').ljust(26,b'\0'),bytes(32),((0,0xfffffffe),)*3)
