"""
bo_codec — codec for the osapiens mobile client's business-object serialization
(port of com.osapiens.operations.utils.SerializationUtil), plus helpers that turn a decoded
object into retrieval-friendly text: locale resolution, flattening, chunking.

Wire format: one type byte, then a big-endian DataOutput payload.
  0 null | 10 map | 11 list | 12 set | 13 array | 14 byte[] | 15 sorted map | 16 sorted set
  20 boolean | 21 byte | 22 short | 23 int | 24 long | 25 float | 26 double
  27 string (writeUTF: u16 len + modified UTF-8) | 28 BigInteger | 29 UUID | 31 large string
  33 geoshape (i32 len + TWKB bytes) | 30 thrift, 35 multipart: not expected inside #value
Collections are prefixed by an i32 count (-1 = null). Map entries are key object + value object.
"""
from __future__ import annotations

import base64
import re
import struct
import uuid
from typing import Any, Iterator

T_NULL, T_MAP, T_LIST, T_SET, T_ARRAY, T_BYTES, T_MAP_SORTED, T_SET_SORTED = 0, 10, 11, 12, 13, 14, 15, 16
T_BOOL, T_BYTE, T_SHORT, T_INT, T_LONG, T_FLOAT, T_DOUBLE, T_STRING, T_BIGINT, T_UUID = 20, 21, 22, 23, 24, 25, 26, 27, 28, 29
T_THRIFT, T_STRING_LARGE, T_GEOSHAPE, T_MULTIPART = 30, 31, 33, 35

MAGIC_TYPES = {T_NULL, T_MAP, T_LIST, T_SET, T_ARRAY, T_BYTES, T_MAP_SORTED, T_SET_SORTED, T_BOOL, T_BYTE,
               T_SHORT, T_INT, T_LONG, T_FLOAT, T_DOUBLE, T_STRING, T_BIGINT, T_UUID, T_STRING_LARGE, T_GEOSHAPE}


class DecodeError(ValueError):
    pass


class _Reader:
    __slots__ = ("b", "i")

    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise DecodeError(f"truncated: need {n} bytes at {self.i}, have {len(self.b) - self.i}")
        out = self.b[self.i:self.i + n]
        self.i += n
        return out

    def u8(self) -> int: return self.take(1)[0]
    def i8(self) -> int: return struct.unpack(">b", self.take(1))[0]
    def i16(self) -> int: return struct.unpack(">h", self.take(2))[0]
    def u16(self) -> int: return struct.unpack(">H", self.take(2))[0]
    def i32(self) -> int: return struct.unpack(">i", self.take(4))[0]
    def i64(self) -> int: return struct.unpack(">q", self.take(8))[0]
    def f32(self) -> float: return struct.unpack(">f", self.take(4))[0]
    def f64(self) -> float: return struct.unpack(">d", self.take(8))[0]


def _modified_utf8(b: bytes) -> str:
    # Java "modified UTF-8": NUL as C0 80, supplementary chars as encoded surrogate pairs.
    b = b.replace(b"\xc0\x80", b"\x00")
    s = b.decode("utf-8", "surrogatepass")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in s):
        s = s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return s


def _read(r: _Reader, nested: bool = True) -> Any:
    t = r.u8()
    if t == T_NULL:
        return None
    if t in (T_MAP, T_MAP_SORTED):
        n = r.i32()
        if n < 0:
            return None
        out: dict = {}
        for _ in range(n):
            k = _read(r, nested)
            v = _read(r, nested)
            out[k if isinstance(k, (str, int, float, bool)) or k is None else str(k)] = v
        return out
    if t in (T_LIST, T_SET, T_SET_SORTED, T_ARRAY):
        n = r.i32()
        if n < 0:
            return None
        return [_read(r, nested) for _ in range(n)]
    if t == T_BYTES:
        n = r.i32()
        if n < 0:
            return None
        raw = r.take(n)
        # nested serialized objects (e.g. named-field specs) start with a known type byte
        if nested and raw and raw[0] in MAGIC_TYPES:
            try:
                return decode(raw)
            except DecodeError:
                pass
        return {"$bytes": len(raw), "$b64": base64.b64encode(raw).decode()} if n <= 4096 else {"$bytes": len(raw)}
    if t == T_BOOL:
        return r.u8() != 0
    if t == T_BYTE:
        return r.i8()
    if t == T_SHORT:
        return r.i16()
    if t == T_INT:
        return r.i32()
    if t == T_LONG:
        return r.i64()
    if t == T_FLOAT:
        return r.f32()
    if t == T_DOUBLE:
        return r.f64()
    if t == T_STRING:
        return _modified_utf8(r.take(r.u16()))
    if t == T_STRING_LARGE:
        return r.take(r.i32()).decode("utf-8", "replace")
    if t == T_BIGINT:
        n = r.u8()
        return int.from_bytes(r.take(n), "big", signed=True)
    if t == T_UUID:
        hi, lo = r.i64(), r.i64()
        return str(uuid.UUID(int=((hi & 0xFFFFFFFFFFFFFFFF) << 64) | (lo & 0xFFFFFFFFFFFFFFFF)))
    if t == T_GEOSHAPE:
        n = r.i32()
        return None if n < 0 else {"$twkb_bytes": n, "$b64": base64.b64encode(r.take(n)).decode()}
    raise DecodeError(f"unsupported type tag {t} at offset {r.i - 1}")


def decode(blob: bytes, nested: bool = True) -> Any:
    """Decode one serialized value (the whole `#value` cell, or any SerializationUtil payload).
    nested=False leaves embedded byte arrays as {"$b64": ...} instead of decoding them."""
    if blob is None:
        return None
    r = _Reader(bytes(blob))
    v = _read(r, nested)
    if r.i != len(r.b):
        raise DecodeError(f"{len(r.b) - r.i} trailing bytes after value")
    return v


def decode_sequence(blob: bytes) -> list[Any]:
    """Decode a byte string that holds several values written back to back (e.g. a
    BusinessObjectNamedField spec: name, path list, dataType int)."""
    r = _Reader(bytes(blob))
    out = []
    while r.i < len(r.b):
        out.append(_read(r))
    return out


VALUE_TYPES = {1: "INT", 2: "FLOAT", 3: "STRING", 4: "BOOLEAN", 5: "ANY", 6: "BLOB_BASE64",
               7: "GEOSHAPE", 8: "GPSPOSITION_FLOAT", 9: "GPSPOSITION_DOUBLE"}


def named_field(blob: bytes) -> dict[str, Any]:
    """BusinessObjectNamedField spec → {name, path, dataType}. The promoted column `name` mirrors
    the value found at `path` inside the decoded #value object."""
    name, path, dtype = (decode_sequence(blob) + [None, None, None])[:3]
    return {"name": name, "path": path, "dataType": VALUE_TYPES.get(dtype, dtype)}


# ------------------------------------------------------------------ encoder (tests / fixtures)


def encode(obj: Any) -> bytes:
    out = bytearray()

    def w(o: Any) -> None:
        if o is None:
            out.append(T_NULL)
        elif isinstance(o, bool):
            out.append(T_BOOL); out.append(1 if o else 0)
        elif isinstance(o, int):
            if -2**31 <= o < 2**31:
                out.append(T_INT); out.extend(struct.pack(">i", o))
            else:
                out.append(T_LONG); out.extend(struct.pack(">q", o))
        elif isinstance(o, float):
            out.append(T_DOUBLE); out.extend(struct.pack(">d", o))
        elif isinstance(o, str):
            b = o.encode("utf-8")
            if len(b) >= 65535:
                out.append(T_STRING_LARGE); out.extend(struct.pack(">i", len(b))); out.extend(b)
            else:
                out.append(T_STRING); out.extend(struct.pack(">H", len(b))); out.extend(b)
        elif isinstance(o, (bytes, bytearray)):
            out.append(T_BYTES); out.extend(struct.pack(">i", len(o))); out.extend(o)
        elif isinstance(o, dict):
            out.append(T_MAP); out.extend(struct.pack(">i", len(o)))
            for k, v in o.items():
                w(k); w(v)
        elif isinstance(o, (list, tuple, set)):
            out.append(T_LIST); out.extend(struct.pack(">i", len(o)))
            for v in o:
                w(v)
        else:
            raise TypeError(f"cannot encode {type(o)}")

    w(obj)
    return bytes(out)


# ------------------------------------------------------------------ text helpers for retrieval

_LOCALE_RE = re.compile(r"^[a-z]{2,3}([-_][A-Za-z]{2,4})?$")
_LOCALE_KEYS = ("locale", "language", "lang", "languageCode", "Locale", "Language", "Lang", "LanguageCode")
_TEXT_KEYS = ("value", "text", "title", "label", "name", "description",
              "Value", "Text", "Title", "Label", "Name", "Description")


def _lang(s: str) -> str:
    return s.replace("_", "-").split("-")[0].lower()


def resolve_locale(v: Any, locale: str = "en") -> str | None:
    """If `v` is a translated text (list of {locale, value} maps, or a map keyed by locale
    codes), return the text for `locale` (language match, else first). Otherwise None."""
    want = _lang(locale)
    if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
        pairs = []
        for x in v:
            lk = next((k for k in _LOCALE_KEYS if k in x and isinstance(x[k], str)), None)
            tk = next((k for k in _TEXT_KEYS if k in x and isinstance(x[k], str)), None)
            if lk and tk:
                pairs.append((x[lk], x[tk]))
        if pairs and len(pairs) == len(v):
            for loc, txt in pairs:
                if _lang(loc) == want:
                    return txt
            return pairs[0][1]
        return None
    if isinstance(v, dict) and v and all(isinstance(k, str) and _LOCALE_RE.match(k) for k in v) \
            and all(isinstance(x, str) for x in v.values()):
        for k, txt in v.items():
            if _lang(k) == want:
                return txt
        return next(iter(v.values()))
    return None


def is_translated(v: Any) -> bool:
    return resolve_locale(v) is not None


def flatten(obj: Any, locale: str = "en", prefix: str = "", max_depth: int = 12) -> Iterator[tuple[str, str]]:
    """Yield (path, text) for every human-readable scalar in a decoded object, with translated
    texts reduced to one locale and byte payloads skipped."""
    if max_depth < 0:
        return
    t = resolve_locale(obj, locale)
    if t is not None:
        yield prefix or "$", t
        return
    if isinstance(obj, dict):
        if "$b64" in obj or "$bytes" in obj:
            return
        for k, v in obj.items():
            yield from flatten(v, locale, f"{prefix}.{k}" if prefix else str(k), max_depth - 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, locale, f"{prefix}[{i}]", max_depth - 1)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        yield prefix or "$", str(obj)
    elif isinstance(obj, str):
        if obj.strip():
            yield prefix or "$", obj


def to_text(obj: Any, locale: str = "en", skip_paths: tuple[str, ...] = ()) -> str:
    """One line per field: `path: text`, ordered as stored. Good input for FTS and embeddings."""
    lines = []
    for path, text in flatten(obj, locale):
        if any(path == p or path.startswith(p + ".") or path.startswith(p + "[") for p in skip_paths):
            continue
        lines.append(f"{path}: {text}")
    return "\n".join(lines)


def get_path(obj: Any, path: str) -> Any:
    """Dotted path lookup: 'Codes', 'Groups.CAUSE', 'a.b[2].c'. Returns None when absent."""
    cur = obj
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
    return cur


def display_title(obj: Any, locale: str = "en") -> str | None:
    """Best-effort human label of an object: Title/Name/Label/Description (translated or plain)."""
    if not isinstance(obj, dict):
        return None
    for k in ("Title", "title", "Name", "name", "Label", "label", "DisplayName", "displayName", "Description", "description"):
        if k in obj:
            t = resolve_locale(obj[k], locale)
            if t:
                return t
            if isinstance(obj[k], str) and obj[k].strip():
                return obj[k]
    return None


def chunks(obj: Any, chunk_path: str | None, locale: str = "en") -> Iterator[tuple[str, Any, dict]]:
    """Split a decoded entry into retrieval units. Without chunk_path: the whole object.
    With chunk_path (e.g. 'Codes'): one unit per element of that list, carrying parent context.
    Yields (suffix, unit, context) — suffix distinguishes units of the same row."""
    if not chunk_path:
        yield "", obj, {}
        return
    parent_title = display_title(obj, locale)
    items = get_path(obj, chunk_path)
    if isinstance(items, dict):
        items = [{"$key": k, **(v if isinstance(v, dict) else {"$value": v})} for k, v in items.items()]
    if not isinstance(items, list):
        return
    for i, item in enumerate(items):
        ctx = {"parent_path": chunk_path, "parent_title": parent_title}
        yield f"#{chunk_path}[{i}]", item, ctx
