"""LZH 書庫 (-lh0- / -lh4- 〜 -lh7-) の読み取り。

Virtual CD-ROM Case が「常に圧縮して保存する」で作る .caz は、UNLHA32.DLL で .cas を 1 つ入れた LZH 書庫
(ヘッダレベル 2、-lh5-) なので、それを読むための最小限の実装。外部のライブラリや DLL に頼らない。

ヘッダは LHa のレベル 0 / 1 / 2 を読む。圧縮方式は無圧縮 (-lh0-) と、LHa 2.x 以降の静的ハフマン方式
(-lh4- 〜 -lh7-。辞書は 4KB 〜 64KB) に対応する。古い -lh1- / -lzs- などは対象外。
"""

from __future__ import annotations

import functools
import os
import shutil
import struct
import subprocess
from collections.abc import Callable, Iterator
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
_TREE = 31  # ハフマン表の要素 (記号, 符号長) の符号長がこの値なら、表の幅を越える符号で left / right の木をたどる
_BIT_MASKS = [(1 << i) - 1 for i in range(65)]  # 下位 i ビットのマスク
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


def extract_first(
    data: bytes, *, path: str | os.PathLike[str] | None = None, progress: Callable[[str, int], None] | None = None
) -> bytes:
    """書庫の最初のファイルの中身 (.caz 用。中身は .cas が 1 つだけ)。

    ``path`` (書庫のファイル) を渡すと、7-Zip がインストールされていればそれで展開する (ネイティブなので内蔵の
    デコーダより桁違いに速い)。7-Zip が無いか失敗したときは内蔵のデコーダで展開する。進捗は展開を始める前に
    ``progress("decompress:7zip", 書庫のサイズ)`` または ``progress("decompress", 書庫のサイズ)`` で通知する。
    """
    if path is not None and (exe := find_7zip()) is not None:
        try:
            members = list(iter_members(data))
            single, expected = len(members) == 1, members[0].original_size if members else 0
        except CatalogError:
            single, expected = True, None  # ヘッダが読めなくても (対応していない方式など) 7-Zip なら読めるかもしれない
        if single:
            if progress is not None:
                progress("decompress:7zip", len(data))
            extracted = _extract_with_7zip(exe, path)
            if extracted is not None and expected in (None, len(extracted)):
                return extracted
    if progress is not None:
        progress("decompress", len(data))
    for member in iter_members(data):
        return extract_member(data, member)
    raise CatalogError(tr('LZH 書庫にファイルがありません'))


@functools.lru_cache(maxsize=1)
def find_7zip() -> str | None:
    """インストールされている 7-Zip の 7z.exe を探す (レジストリ → Program Files → PATH)。無ければ None。"""
    candidates: list[str] = []
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\7-Zip") as key:
                    for value_name in ("Path64", "Path"):
                        try:
                            candidates.append(os.path.join(winreg.QueryValueEx(key, value_name)[0], "7z.exe"))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        folder = os.environ.get(variable)
        if folder:
            candidates.append(os.path.join(folder, "7-Zip", "7z.exe"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("7z")


def _extract_with_7zip(exe: str, path: str | os.PathLike[str]) -> bytes | None:
    """7z.exe で書庫の中身を標準出力へ展開して受け取る (ディスクには書かない)。失敗したら None。"""
    try:
        completed = subprocess.run(
            [exe, "e", "-so", "-y", os.fspath(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


class _Decoder:
    """LHa 2.x の静的ハフマン方式 (-lh4- 〜 -lh7-) のデコーダ。LHa for UNIX の huf.c / maketbl.c と同じ手順。

    純 Python のまま速くするため、1 記号あたりの命令数を切り詰めている:

    - ビット入力は 1 つの整数 (``bitbuf``) に 32 ビットずつ溜め、``nbits`` に残りのビット数を持つ。先頭 n ビットの
      覗き見は ``(bitbuf >> (nbits - n)) & mask``、消費は ``nbits -= 符号長`` だけで済む
    - ハフマン表の要素は ``(記号, 符号長)`` のタプルで、1 回の引きと分解で記号と消費するビット数の両方が分かる。表の幅
      (12 ビット / 8 ビット) より長い符号は、符号長の欄を ``_TREE`` にして left / right の木をたどる (まれ)
    - ``decode`` の中は全部ローカル変数で回し、ブロックの記号数は ``for`` の range に任せる。一致の複写はスライスで
      行う (複写元と複写先が重なる場合は周期の繰り返し)
    """

    def __init__(self, data: bytes, dicbit: int):
        self.data = data + bytes(8)  # 末尾を越えて読んでも 0 が出るように余白を付ける
        self.pos = 0
        self.np = dicbit + 1
        self.pbit = 4 if dicbit <= 13 else 5
        self.bitbuf = 0
        self.nbits = 0
        self.c_len = [0] * _NC
        self.pt_len = [0] * _NPT
        self.c_table: list = [0] * 4096
        self.pt_table: list = [0] * 256
        self.left = [0] * (2 * _NC - 1)
        self.right = [0] * (2 * _NC - 1)

    # ------------------------------------------------------------------ ビット入力 (ブロックの見出し用。復号ループでは展開して使う)
    def _refill(self) -> None:
        self.bitbuf = ((self.bitbuf & _BIT_MASKS[self.nbits]) << 32) | int.from_bytes(self.data[self.pos : self.pos + 4], "big")
        self.pos += 4
        self.nbits += 32

    def peek16(self) -> int:
        if self.nbits < 16:
            self._refill()
        return (self.bitbuf >> (self.nbits - 16)) & 0xFFFF

    def getbits(self, n: int) -> int:
        if n == 0:
            return 0
        if self.nbits < n:
            self._refill()
        self.nbits -= n
        return (self.bitbuf >> self.nbits) & _BIT_MASKS[n]

    def decode_symbol(self, table: list, tablebits: int, nchar: int, lens: list[int]) -> int:
        """表 (``table``) とビット入力から記号 (``nchar`` 未満) を 1 つ読む (ブロックの見出しを読むときに使う)。"""
        peek = self.peek16()
        j, length = table[peek >> (16 - tablebits)]
        if length != _TREE:
            self.nbits -= length
            return j
        left, right = self.left, self.right
        bit = 15 - tablebits
        while j >= nchar:
            j = right[j] if (peek >> bit) & 1 else left[j]
            bit -= 1
        self.nbits -= lens[j]
        return j

    # ------------------------------------------------------------------ ハフマン表
    def make_table(self, nchar: int, bitlen: list[int], tablebits: int, table: list) -> None:
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
            table[i : 1 << tablebits] = [0] * ((1 << tablebits) - i)
        avail = nchar
        mask = 1 << (15 - tablebits)
        left, right = self.left, self.right
        for ch in range(nchar):
            length = bitlen[ch]
            if length == 0:
                continue
            nextcode = start[length] + weight[length]
            if length <= tablebits:
                table[start[length] : nextcode] = [(ch, length)] * weight[length]
            else:
                # 表の幅を越える符号: 表の要素には木の節 (記号番号 >= nchar) を入れ、残りのビットで left / right をたどる
                k = start[length]
                index = k >> jutbits
                entry = table[index]
                if entry == 0:
                    right[avail] = left[avail] = 0
                    node = avail
                    table[index] = (node, _TREE)
                    avail += 1
                else:
                    node = entry[0]
                branch = right if k & mask else left
                k = (k << 1) & 0xFFFF
                for _ in range(length - tablebits - 1):
                    if branch[node] == 0:
                        right[avail] = left[avail] = 0
                        branch[node] = avail
                        avail += 1
                    node = branch[node]
                    branch = right if k & mask else left
                    k = (k << 1) & 0xFFFF
                branch[node] = ch
            start[length] = nextcode

    def read_pt_len(self, nn: int, nbit: int, i_special: int) -> None:
        n = self.getbits(nbit)
        if n == 0:
            c = self.getbits(nbit)
            for i in range(nn):
                self.pt_len[i] = 0
            self.pt_table[:] = [(c, 0)] * 256  # 符号長 0: ビットを消費せず常にこの記号
            return
        i = 0
        while i < min(n, _NPT):
            peek = self.peek16()
            c = peek >> 13
            if c != 7:
                self.nbits -= 3
            else:
                mask = 1 << 12
                while mask & peek:
                    mask >>= 1
                    c += 1
                if c > 16:
                    raise CatalogError(tr('LZH の圧縮データが壊れています'))
                self.nbits -= c - 3
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
            self.c_table[:] = [(c, 0)] * 4096
            return
        i = 0
        while i < min(n, _NC):
            c = self.decode_symbol(self.pt_table, 8, _NT, self.pt_len)
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

    def read_block_header(self) -> int:
        """ブロックの見出し (記号数と 3 つのハフマン表) を読み、ブロックの記号数を返す。"""
        blocksize = self.getbits(16)
        self.read_pt_len(_NT, _TBIT, 3)
        self.read_c_len()
        self.read_pt_len(self.np, self.pbit, -1)
        return blocksize

    # ------------------------------------------------------------------ 復号
    def decode(self, original_size: int) -> bytes:
        out = bytearray()
        append = out.append
        data, pos = self.data, self.pos
        bitbuf, nbits = self.bitbuf, self.nbits
        c_table, pt_table = self.c_table, self.pt_table
        c_len, pt_len = self.c_len, self.pt_len
        left, right = self.left, self.right
        np_ = self.np
        masks = _BIT_MASKS
        from_bytes = int.from_bytes
        while len(out) < original_size:
            self.pos, self.bitbuf, self.nbits = pos, bitbuf, nbits
            blocksize = self.read_block_header()
            pos, bitbuf, nbits = self.pos, self.bitbuf, self.nbits
            if blocksize == 0:
                raise CatalogError(tr('LZH の圧縮データが壊れています'))
            # ブロックの記号を順に読む。元のサイズに達しているかは一致のときとブロックの終わりだけで確かめる
            # (文字がはみ出しても最後に切り捨てる。ビット入力は末尾に余白があるので読み過ぎても安全)
            for _ in range(blocksize):
                # --- 文字または一致長の記号 (c_table は 12 ビット幅) ---
                if nbits < 32:
                    bitbuf = ((bitbuf & masks[nbits]) << 32) | from_bytes(data[pos : pos + 4], "big")
                    pos += 4
                    nbits += 32
                c, length = c_table[(bitbuf >> (nbits - 12)) & 0xFFF]
                if length != _TREE:
                    nbits -= length
                else:
                    peek = (bitbuf >> (nbits - 16)) & 0xFFFF
                    bit = 3
                    while c >= _NC:
                        c = right[c] if (peek >> bit) & 1 else left[c]
                        bit -= 1
                    nbits -= c_len[c]
                if c < 256:
                    append(c)
                    continue

                # --- 一致: 長さと、距離の記号 (pt_table は 8 ビット幅) ---
                match = c - (256 - _THRESHOLD)
                if nbits < 32:
                    bitbuf = ((bitbuf & masks[nbits]) << 32) | from_bytes(data[pos : pos + 4], "big")
                    pos += 4
                    nbits += 32
                j, length = pt_table[(bitbuf >> (nbits - 8)) & 0xFF]
                if length != _TREE:
                    nbits -= length
                else:
                    peek = (bitbuf >> (nbits - 16)) & 0xFFFF
                    bit = 7
                    while j >= np_:
                        j = right[j] if (peek >> bit) & 1 else left[j]
                        bit -= 1
                    nbits -= pt_len[j]
                if j != 0:
                    extra = j - 1
                    if nbits < extra:
                        bitbuf = ((bitbuf & masks[nbits]) << 32) | from_bytes(data[pos : pos + 4], "big")
                        pos += 4
                        nbits += 32
                    nbits -= extra
                    j = (1 << extra) + ((bitbuf >> nbits) & masks[extra])
                out_len = len(out)
                start = out_len - j - 1
                if start < 0:
                    if out_len >= original_size:
                        break
                    raise CatalogError(tr('LZH の圧縮データが壊れています'))
                if j + 1 >= match:
                    out += out[start : start + match]
                else:
                    # 直前の繰り返し (複写元と複写先が重なる): 周期 j + 1 のパターンを必要な長さだけ並べる
                    out += (out[start:] * (match // (j + 1) + 1))[:match]
                if out_len + match >= original_size:
                    break
        del out[original_size:]
        self.pos, self.bitbuf, self.nbits = pos, bitbuf, nbits
        return bytes(out)
