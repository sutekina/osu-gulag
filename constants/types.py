from enum import IntEnum
from enum import unique

from misc.utils import escape_enum
from misc.utils import pymysql_encode

__all__ = ('osuTypes',)

@unique
@pymysql_encode(escape_enum)
class osuTypes(IntEnum):
    # integral
    i8  = 0
    u8  = 1
    i16 = 2
    u16 = 3
    i32 = 4
    u32 = 5
    f32 = 6
    i64 = 7
    u64 = 8
    f64 = 9

    # osu
    message           = 11
    channel           = 12
    match             = 13
    scoreframe        = 14
    mapInfoRequest    = 15
    mapInfoReply      = 16
    replayFrameBundle = 17

    # misc
    i32_list   = 18 # 2 bytes len
    i32_list4l = 19 # 4 bytes len
    string     = 20
    raw        = 21
