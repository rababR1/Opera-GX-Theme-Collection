import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image


XCURSOR_MAGIC = b"Xcur"
XCURSOR_HEADER = struct.Struct("<4sIII")
XCURSOR_TOC = struct.Struct("<III")
XCURSOR_IMAGE = struct.Struct("<IIIIIIIII")
XCURSOR_IMAGE_TYPE = 0xFFFD0002

CUR_HEADER = struct.Struct("<HHH")
CUR_ENTRY = struct.Struct("<BBBBHHII")
ANI_RIFF = struct.Struct("<4sI4s")
ANI_CHUNK = struct.Struct("<4sI")
ANI_HEADER = struct.Struct("<IIIIIIIII")
ANI_U32 = struct.Struct("<I")


@dataclass
class CursorImage:
    width: int
    height: int
    hotspot_x: int
    hotspot_y: int
    nominal_size: int
    image: Image.Image


@dataclass
class CursorFrame:
    images: list[CursorImage]
    delay: float = 0.0


def parse_xcursor(blob: bytes) -> list[CursorFrame]:
    magic, header_size, version, toc_size = XCURSOR_HEADER.unpack_from(blob, 0)
    if magic != XCURSOR_MAGIC:
        raise ValueError("Not an Xcursor file")
    if version != 0x1:
        raise ValueError(f"Unsupported Xcursor version: {version}")
    if header_size != XCURSOR_HEADER.size:
        raise ValueError(f"Unexpected Xcursor header size: {header_size}")

    offset = XCURSOR_HEADER.size
    chunks: list[tuple[int, int, int]] = []
    for _ in range(toc_size):
        chunks.append(XCURSOR_TOC.unpack_from(blob, offset))
        offset += XCURSOR_TOC.size

    images_by_size: dict[int, list[tuple[CursorImage, float]]] = {}

    for chunk_type, chunk_subtype, position in chunks:
        if chunk_type != XCURSOR_IMAGE_TYPE:
            continue
        (
            size,
            actual_type,
            nominal_size,
            _version,
            width,
            height,
            hotspot_x,
            hotspot_y,
            delay_ms,
        ) = XCURSOR_IMAGE.unpack_from(blob, position)

        if size != XCURSOR_IMAGE.size or actual_type != chunk_type or nominal_size != chunk_subtype:
            raise ValueError("Unsupported Xcursor image chunk")

        image_start = position + XCURSOR_IMAGE.size
        image_size = width * height * 4
        raw = blob[image_start:image_start + image_size]
        if len(raw) != image_size:
            raise ValueError("Corrupt Xcursor image data")

        image = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).copy()
        cursor_image = CursorImage(
            width=width,
            height=height,
            hotspot_x=hotspot_x,
            hotspot_y=hotspot_y,
            nominal_size=nominal_size,
            image=image,
        )
        images_by_size.setdefault(nominal_size, []).append((cursor_image, delay_ms / 1000))

    frame_counts = {len(items) for items in images_by_size.values()}
    if len(frame_counts) != 1:
        raise ValueError("Animated Xcursor sizes have mismatched frame counts")

    ordered_sizes = sorted(images_by_size)
    frame_count = frame_counts.pop()
    frames: list[CursorFrame] = []
    for frame_index in range(frame_count):
        frame_images: list[CursorImage] = []
        delays: set[float] = set()
        for size in ordered_sizes:
            image, delay = images_by_size[size][frame_index]
            frame_images.append(image)
            delays.add(delay)
        if len(delays) != 1:
            raise ValueError("Animated Xcursor sizes have mismatched frame delays")
        frames.append(CursorFrame(images=frame_images, delay=delays.pop()))
    return frames


def encode_cur(frame: CursorFrame) -> bytes:
    header = CUR_HEADER.pack(0, 2, len(frame.images))
    directory: list[bytes] = []
    blobs: list[bytes] = []
    offset = CUR_HEADER.size + len(frame.images) * CUR_ENTRY.size

    for image in frame.images:
        png_io = BytesIO()
        image.image.save(png_io, format="PNG")
        png_blob = png_io.getvalue()
        width = image.width if image.width < 256 else 0
        height = image.height if image.height < 256 else 0
        directory.append(
            CUR_ENTRY.pack(
                width,
                height,
                0,
                0,
                image.hotspot_x,
                image.hotspot_y,
                len(png_blob),
                offset,
            )
        )
        blobs.append(png_blob)
        offset += len(png_blob)

    return b"".join([header, *directory, *blobs])


def encode_ani(frames: list[CursorFrame]) -> bytes:
    cur_chunks = []
    for frame in frames:
        cur_blob = encode_cur(frame)
        cur_chunks.append(ANI_CHUNK.pack(b"icon", len(cur_blob)))
        cur_chunks.append(cur_blob)
        if len(cur_blob) & 1:
            cur_chunks.append(b"\0")

    cur_list = b"".join(cur_chunks)
    rate_blob = b"".join(ANI_U32.pack(int(round(frame.delay * 60))) for frame in frames)
    anih_blob = ANI_HEADER.pack(36, len(frames), len(frames), 0, 0, 32, 1, 1, 1)

    body = b"".join(
        [
            ANI_CHUNK.pack(b"anih", len(anih_blob)),
            anih_blob,
            ANI_RIFF.pack(b"LIST", len(cur_list) + 4, b"fram"),
            cur_list,
            ANI_CHUNK.pack(b"rate", len(rate_blob)),
            rate_blob,
        ]
    )
    return ANI_RIFF.pack(b"RIFF", len(body) + 4, b"ACON") + body


def convert_file(source: Path, target: Path) -> None:
    frames = parse_xcursor(source.read_bytes())
    if len(frames) == 1:
        target.write_bytes(encode_cur(frames[0]))
    else:
        target.write_bytes(encode_ani(frames))


def main() -> None:
    source_dir = Path("Everforest/cursors")
    target_dir = Path("Everforest/cursors_windows")
    target_dir.mkdir(exist_ok=True)

    files_to_convert = {
        "left_ptr": "pointer.cur",
        "help": "help.cur",
        "progress": "progress.ani",
        "wait": "wait.ani",
        "text": "text.cur",
        "openhand": "hand.cur",
        "not-allowed": "not_allowed.cur",
        "size_hor": "east_west_resize.cur",
        "size_ver": "north_south_resize.cur",
        "size_fdiag": "north_east_south_west_resize.cur",
        "size_bdiag": "north_west_south_east_resize.cur",
    }

    for source_name, target_name in files_to_convert.items():
        source = source_dir / source_name
        if not source.exists():
            raise FileNotFoundError(source)
        convert_file(source, target_dir / target_name)


if __name__ == "__main__":
    main()
