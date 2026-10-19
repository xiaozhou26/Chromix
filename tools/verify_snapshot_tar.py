#!/usr/bin/env python3
"""Validate a complete snapshot tar stream, including its trailing padding."""
import sys
import tarfile


CHUNK_SIZE = 1024 * 1024
ZERO_BLOCK = b"\0" * tarfile.BLOCKSIZE


class SnapshotTarInfo(tarfile.TarInfo):
    @classmethod
    def fromtarfile(cls, archive):
        try:
            return super().fromtarfile(archive)
        except tarfile.EOFHeaderError:
            # tar's logical EOF must not close the pipe before zstd finishes.
            stream = archive.fileobj
            if stream.read(tarfile.BLOCKSIZE) != ZERO_BLOCK:
                raise tarfile.ReadError("missing second tar end block") from None
            padding = 0
            while data := stream.read(CHUNK_SIZE):
                if data.strip(b"\0"):
                    raise tarfile.ReadError("nonzero data after tar end blocks")
                padding += len(data)
            if padding % tarfile.BLOCKSIZE:
                raise tarfile.ReadError("truncated tar padding")
            raise
        except tarfile.HeaderError as error:
            # tarfile otherwise treats some malformed later headers as EOF.
            raise tarfile.ReadError(str(error)) from None


def verify(stream):
    try:
        with tarfile.open(fileobj=stream, mode="r|", bufsize=CHUNK_SIZE,
                          tarinfo=SnapshotTarInfo) as archive:
            for _ in archive:
                # Python versions before 3.13 cache members even in stream mode.
                archive.members.clear()
    finally:
        # Even a bad tar must let the producer finish its checksum/decode checks.
        while stream.read(CHUNK_SIZE):
            pass


def main():
    try:
        verify(sys.stdin.buffer)
    except (tarfile.TarError, OSError, ValueError) as error:
        print(f"snapshot tar verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
