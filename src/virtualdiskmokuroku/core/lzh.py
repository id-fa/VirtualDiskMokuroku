"""LZH 書庫 (-lh0- / -lh4- 〜 -lh7-) の読み取り。

Virtual CD-ROM Case が「常に圧縮して保存する」で作る .caz は、UNLHA32.DLL で .cas を 1 つ入れた LZH 書庫
(ヘッダレベル 2、-lh5-) なので、それを読むための最小限の実装。外部のライブラリや DLL に頼らない。

ヘッダは LHa のレベル 0 / 1 / 2 を読む。圧縮方式は無圧縮 (-lh0-) と、LHa 2.x 以降の静的ハフマン方式
(-lh4- 〜 -lh7-。辞書は 4KB 〜 64KB) に対応する。古い -lh1- / -lzs- などは対象外。
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import CatalogError
from ..i18n import tr

_METHODS = {b"-lh0-": 0, b"-lh4-": 12, b"-lh5-": 13, b"-lh6-": 15, b"-lh7-": 16}  # 辞書のビット数 (0 は無圧縮)
_THRESHOLD = 3
_MAX_MATCH = 256
_NC = 255 + _MAX_MATCH + 2 - _THRESHOLD  # 文字と一致長を合わせた記号の数 (510)
_NT = 19  # 符号長の符号 (c_len を書くための記号) の数
_NPT = 0x80
_CBIT, _TBIT = 9, 5
_HEADER_LEVEL_0_1 = struct.Struct("<BB5sIIIBB")


@dataclass(slots=True)
class LzhMember:
    name: str
    method: bytes
    packed_size: int
    original_size: int
    offset: int  # 圧縮データの位置


def is_lzh(data: bytes) -> bool:
    """LZH 書庫らしいか (対応していない方式でも True。方式の判定は ``iter_members`` が行う)。"""
    return len(data) > 22 and data[2] == 0x2D and data[3] == 0x6C and data[6] == 0x2D and data[0] >= 22  # "-l?-"


def iter_members(data: bytes, *, encoding: str = "cp932") -> Iterator[LzhMember]:
    """書庫内のファイルを順に返す。レベル 0 / 1 / 2 のヘッダに対応。"""
    pos = 0
    while pos < len(data) and data[pos] != 0:
        if pos + 22 > len(data):
            raise CatalogError(tr('LZH 書庫のヘッダが壊れています'))
        level = data[pos + 20]
        method = data[pos + 2 : pos + 7]
        if method not in _METHODS:
            raise CatalogError(tr('対応していない LZH の圧縮方式です: {method}').format(method=method.decode("ascii", "replace")))
        if level in (0, 1):
            head_size, _check, _method, packed, original, _time, _attr, _level = _HEADER_LEVEL_0_1.unpack_from(data, pos)
            name_length = data[pos + 21]
            name = data[pos + 22 : pos + 22 + name_length]
            offset = pos + head_size + 2
            if level == 1:
                # ヘッダ末尾の 2 バイトが最初の拡張ヘッダのサイズ。拡張ヘッダ (種類, 内容, 次のサイズ) はパックサイズに含まれる
                size = struct.unpack_from("<H", data, offset - 2)[0]
                while size:
                    if data[offset] == 0x01:
                        name = data[offset + 1 : offset + size - 2]
                    packed -= size
                    offset += size
                    size = struct.unpack_from("<H", data, offset - 2)[0]
        elif level == 2:
            head_size, packed, original = struct.unpack_from("<H", data, pos)[0], *struct.unpack_from("<II", data, pos + 7)
            name = b""
            cursor = pos + 24
            size = struct.unpack_from("<H", data, cursor)[0]
            cursor += 2
            while size:
                if data[cursor] == 0x01:
                    name = data[cursor + 1 : cursor + size - 2]
                cursor += size
                size = struct.unpack_from("<H", data, cursor - 2)[0]
            offset = pos + head_size
        else:
            raise CatalogError(tr('対応していない LZH のヘッダ形式です (レベル {level})').format(level=level))
        if offset + packed > len(data):
            raise CatalogError(tr('LZH 書庫が途中で切れています'))
        yield LzhMember(name.decode(encoding, "replace"), method, packed, original, offset)
        pos = offset + packed


def extract_member(data: bytes, member: LzhMember) -> bytes:
    """メンバーの中身を取り出す。"""
    packed = data[member.offset : member.offset + member.packed_size]
    dicbit = _METHODS[member.method]
    if dicbit == 0:
        return bytes(packed[: member.original_size])
    return _Decoder(packed, dicbit).decode(member.original_size)


def extract_first(data: bytes) -> bytes:
    """書庫の最初のファイルの中身 (.caz 用。中身は .cas が 1 つだけ)。"""
    for member in iter_members(data):
        return extract_member(data, member)
    raise CatalogError(tr('LZH 書庫にファイルがありません'))


class _Decoder:
    """LHa 2.x の静的ハフマン方式 (-lh4- 〜 -lh7-) のデコーダ。LHa for UNIX の huf.c / maketbl.c と同じ手順。"""

    def __init__(self, data: bytes, dicbit: int):
        self.data = data
        self.pos = 0
        self.dicbit = dicbit
        self.np = dicbit + 1
        self.pbit = 4 if dicbit <= 13 else 5
        self.bitbuf = 0
        self.subbitbuf = 0
        self.bitcount = 0
        self.blocksize = 0
        self.c_len = [0] * _NC
        self.pt_len = [0] * _NPT
        self.c_table = [0] * 4096
        self.pt_table = [0] * 256
        self.left = [0] * (2 * _NC - 1)
        self.right = [0] * (2 * _NC - 1)
        self.fillbuf(16)

    # ------------------------------------------------------------------ ビット入力
    def fillbuf(self, n: int) -> None:
        self.bitbuf = (self.bitbuf << n) & 0xFFFF
        while n > self.bitcount:
            n -= self.bitcount
            self.bitbuf |= (self.subbitbuf << n) & 0xFFFF
            if self.pos < len(self.data):
                self.subbitbuf = self.data[self.pos]
                self.pos += 1
            else:
                self.subbitbuf = 0
            self.bitcount = 8
        self.bitcount -= n
        self.bitbuf |= self.subbitbuf >> self.bitcount

    def getbits(self, n: int) -> int:
        value = self.bitbuf >> (16 - n)
        self.fillbuf(n)
        return value

    # ------------------------------------------------------------------ ハフマン表
    def make_table(self, nchar: int, bitlen: list[int], tablebits: int, table: list[int]) -> None:
        count = [0] * 17
        for length in bitlen[:nchar]:
            if length > 16:
                raise CatalogError(tr('LZH の圧縮データが壊れています'))
            count[length] += 1
        start = [0] * 18
        for i in range(1, 17):
            start[i + 1] = start[i] + (count[i] << (16 - i))
        if start[17] != 1 << 16:
            raise CatalogError(tr('LZH の圧縮データが壊れています'))
        jutbits = 16 - tablebits
        weight = [0] * 17
        for i in range(1, tablebits + 1):
            start[i] >>= jutbits
            weight[i] = 1 << (tablebits - i)
        for i in range(tablebits + 1, 17):
            weight[i] = 1 << (16 - i)
        i = start[tablebits + 1] >> jutbits
        if i != 0:
            for j in range(i, 1 << tablebits):
                table[j] = 0
        avail = nchar
        mask = 1 << (15 - tablebits)
        left, right = self.left, self.right
        for ch in range(nchar):
            length = bitlen[ch]
            if length == 0:
                continue
            nextcode = start[length] + weight[length]
            if length <= tablebits:
                for j in range(start[length], nextcode):
                    table[j] = ch
            else:
                k = start[length]
                node, index = table, k >> jutbits
                for _ in range(length - tablebits):
                    if node[index] == 0:
                        right[avail] = left[avail] = 0
                        node[index] = avail
                        avail += 1
                    index = node[index]
                    node = right if k & mask else left
                    k = (k << 1) & 0xFFFF
                node[index] = ch
            start[length] = nextcode

    def read_pt_len(self, nn: int, nbit: int, i_special: int) -> None:
        n = self.getbits(nbit)
        if n == 0:
            c = self.getbits(nbit)
            for i in range(nn):
                self.pt_len[i] = 0
            for i in range(256):
                self.pt_table[i] = c
            return
        i = 0
        while i < min(n, _NPT):
            c = self.bitbuf >> 13
            if c != 7:
                self.fillbuf(3)
            else:
                mask = 1 << 12
                while mask & self.bitbuf:
                    mask >>= 1
                    c += 1
                self.fillbuf(c - 3)
            self.pt_len[i] = c
            i += 1
            if i == i_special:
                c = self.getbits(2)
                while c > 0 and i < _NPT:
                    self.pt_len[i] = 0
                    i += 1
                    c -= 1
        while i < nn:
            self.pt_len[i] = 0
            i += 1
        self.make_table(nn, self.pt_len, 8, self.pt_table)

    def read_c_len(self) -> None:
        n = self.getbits(_CBIT)
        if n == 0:
            c = self.getbits(_CBIT)
            for i in range(_NC):
                self.c_len[i] = 0
            for i in range(4096):
                self.c_table[i] = c
            return
        i = 0
        while i < min(n, _NC):
            c = self.pt_table[self.bitbuf >> 8]
            if c >= _NT:
                mask = 1 << 7
                while True:
                    c = self.right[c] if self.bitbuf & mask else self.left[c]
                    mask >>= 1
                    if c < _NT:
                        break
            self.fillbuf(self.pt_len[c])
            if c <= 2:
                if c == 0:
                    c = 1
                elif c == 1:
                    c = self.getbits(4) + 3
                else:
                    c = self.getbits(_CBIT) + 20
                while c > 0 and i < _NC:
                    self.c_len[i] = 0
                    i += 1
                    c -= 1
            else:
                self.c_len[i] = c - 2
                i += 1
        while i < _NC:
            self.c_len[i] = 0
            i += 1
        self.make_table(_NC, self.c_len, 12, self.c_table)

    # ------------------------------------------------------------------ 復号
    def decode_c(self) -> int:
        if self.blocksize == 0:
            self.blocksize = self.getbits(16)
            self.read_pt_len(_NT, _TBIT, 3)
            self.read_c_len()
            self.read_pt_len(self.np, self.pbit, -1)
        self.blocksize -= 1
        j = self.c_table[self.bitbuf >> 4]
        if j < _NC:
            self.fillbuf(self.c_len[j])
        else:
            self.fillbuf(12)
            mask = 1 << 15
            while True:
                j = self.right[j] if self.bitbuf & mask else self.left[j]
                mask >>= 1
                if j < _NC:
                    break
            self.fillbuf(self.c_len[j] - 12)
        return j

    def decode_p(self) -> int:
        j = self.pt_table[self.bitbuf >> 8]
        if j < self.np:
            self.fillbuf(self.pt_len[j])
        else:
            self.fillbuf(8)
            mask = 1 << 15
            while True:
                j = self.right[j] if self.bitbuf & mask else self.left[j]
                mask >>= 1
                if j < self.np:
                    break
            self.fillbuf(self.pt_len[j] - 8)
        if j != 0:
            j = (1 << (j - 1)) + self.getbits(j - 1)
        return j

    def decode(self, original_size: int) -> bytes:
        out = bytearray()
        append = out.append
        while len(out) < original_size:
            c = self.decode_c()
            if c < 256:
                append(c)
            else:
                length = c - 256 + _THRESHOLD
                start = len(out) - self.decode_p() - 1
                if start < 0:
                    raise CatalogError(tr('LZH の圧縮データが壊れています'))
                if start + length <= len(out):
                    out += out[start : start + length]
                else:
                    for k in range(length):  # 直前の繰り返し (コピー元とコピー先が重なる)
                        append(out[start + k])
        del out[original_size:]
        return bytes(out)
